"""Indexed cloud copies shared by mirror files and room file leases."""
import asyncio
import hashlib
import json
import logging
import time
from collections import deque

from .mirror_storage import QiniuStorage, S3Storage, StorageError, credentials, profile_unit
from .mirror_tls import CloudTLS

GIB = 1024 ** 3
INDEX_BATCH = 500
ROOM_INDEX_BATCH = 100
QUEUE_WINDOW = 600


class MirrorCloud:
    def __init__(self, mirror, settings):
        self.mirror, self.settings = mirror, settings
        self.worker = None
        self.health_task = None
        self.reconcile_task = None
        self.reconcile_cursors = dict(mirror=0, room=0)
        self.reconcile_bounds = None
        self.reconcile_signature = None
        self.automatic = dict(state='starting', scanned=0, queued=0, scanned_total=0,
                              queued_total=0, round=0, last_at=None, error=None, outcomes={})
        self.active = None
        self.active_digest = None
        self.progress = 0
        self.progress_samples = deque(maxlen=120)
        self.upload_started = 0
        self.phase = 'idle'
        self.health = {}
        self.room_release_offset = 0
        self.egress_lock = asyncio.Lock()
        self.storages = {}
        self.upload_tasks = {}
        self.transfers = {}
        self.upload_lock = asyncio.Lock()
        self.reservations = {}
        self.health_locks = {}
        self.uploaded_progress = 0
        self.aggregate_samples = deque(maxlen=120)
        self.tls = CloudTLS(self)
        from .room_direct_uploads import RoomDirectUploads
        self.direct = RoomDirectUploads(self)

    async def open(self):
        await self.mirror.db.executescript('''
            CREATE TABLE IF NOT EXISTS object_locations(
                scope TEXT NOT NULL,digest TEXT NOT NULL,profile TEXT NOT NULL,
                route TEXT NOT NULL,object_key TEXT NOT NULL,size INTEGER NOT NULL,
                state TEXT NOT NULL,verified_at REAL,last_error TEXT,
                PRIMARY KEY(scope,digest,profile,object_key));
            CREATE INDEX IF NOT EXISTS cloud_locations_digest ON object_locations(scope,digest,state);
            CREATE TABLE IF NOT EXISTS cloud_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,digest TEXT NOT NULL,
                profile TEXT NOT NULL,route TEXT NOT NULL,object_key TEXT NOT NULL,
                size INTEGER NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,started REAL,finished REAL,retry_at REAL DEFAULT 0,error TEXT,
                UNIQUE(scope,digest,profile,object_key));
            CREATE INDEX IF NOT EXISTS cloud_jobs_queue ON cloud_jobs(state,retry_at,id);
            CREATE INDEX IF NOT EXISTS cloud_jobs_route ON cloud_jobs(route,state,id);
            CREATE TABLE IF NOT EXISTS cloud_budget(profile TEXT,day INTEGER,uploaded INTEGER,
                PRIMARY KEY(profile,day));
            CREATE TABLE IF NOT EXISTS cloud_egress(profile TEXT,month TEXT,bytes INTEGER NOT NULL,
                redirects INTEGER NOT NULL,PRIMARY KEY(profile,month));
            CREATE TABLE IF NOT EXISTS cloud_reconciliation(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        ''')
        checkpoint = await self.mirror.one('SELECT data FROM cloud_reconciliation WHERE id=1')
        if checkpoint:
            try:
                saved = json.loads(checkpoint['data'])
                self.automatic.update(saved['automatic'])
                self.reconcile_cursors = saved['cursors']
                self.reconcile_bounds = saved['bounds']
                self.reconcile_signature = saved['signature']
            except (ValueError, KeyError, TypeError):
                logging.warning('Cloud reconciliation checkpoint could not be restored')
        await self.mirror.db.execute("UPDATE cloud_jobs SET state='queued',retry_at=0 WHERE state='uploading'")
        await self.mirror.db.commit()

        await self.direct.open()

    def start(self):
        self.worker = asyncio.create_task(self.run())
        self.health_task = asyncio.create_task(self.health_loop())
        self.reconcile_task = asyncio.create_task(self.reconcile_loop())
        self.tls.start()

    async def close(self):
        await self.tls.close()
        tasks = [task for task in (self.worker, self.health_task, self.reconcile_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def profile(self, identifier):
        return next((profile for profile in self.settings()['qiniu_profiles'] if profile['id'] == identifier), None)

    def storage(self, identifier):
        profile = self.profile(identifier)
        if not profile:
            raise StorageError('Cloud profile no longer exists')
        minutes = self.settings()['qiniu_signed_url_minutes']
        signature = (json.dumps(profile, sort_keys=True), credentials(identifier), minutes)
        cached = self.storages.get(identifier)
        if not cached or cached[0] != signature:
            storage = QiniuStorage if profile.get('provider', 'qiniu') == 'qiniu' else S3Storage
            cached = self.storages[identifier] = (signature, storage(profile, signed_url_minutes=minutes))
        return cached[1]

    def route(self, name):
        return self.settings()['qiniu_routes'][name]

    async def location(self, scope, digest):
        return await self.mirror.one('''SELECT * FROM object_locations WHERE scope=? AND digest=?
            AND state='ready' ORDER BY verified_at DESC LIMIT 1''', (scope, digest))

    async def egress(self, profile):
        month = time.strftime('%Y-%m', time.gmtime(time.time() + 8 * 3600))
        row = await self.mirror.one('SELECT bytes,redirects FROM cloud_egress WHERE profile=? AND month=?',
                                    (profile['id'], month))
        limit = profile.get('monthly_egress_gib', 0) * profile_unit(profile)
        return dict(month=month, bytes=row['bytes'] if row else 0,
                    redirects=row['redirects'] if row else 0, limit_bytes=limit,
                    stop_bytes=limit * profile.get('egress_threshold', 90) // 100)

    async def download_url(self, scope, digest, *, expires_seconds=None, reserve=False, method='GET'):
        async with self.mirror.db.execute("SELECT * FROM object_locations WHERE scope=? AND digest=? AND state='ready' ORDER BY verified_at DESC", (scope, digest)) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            profile = self.profile(row['profile'])
            if not profile:
                continue
            health = self.health.get(row['profile'])
            if health and not health['healthy'] and time.time() - health['checked'] < 300:
                continue
            try:
                async with self.egress_lock:
                    budget = await self.egress(profile)
                    if budget['limit_bytes'] and budget['bytes'] + row['size'] > budget['stop_bytes']:
                        continue
                    options = {'expires_seconds': expires_seconds}
                    if profile.get('provider') in ('s3', 'rainyun'):
                        options['method'] = method
                    storage = self.storage(row['profile'])
                    download = storage.endpoint_download_url if health and health.get('download_via') == 'endpoint' else storage.download_url
                    url = download(row['object_key'], **options)
                    if reserve:
                        await self.mirror.db.execute('''INSERT INTO cloud_egress VALUES(?,?,?,1)
                            ON CONFLICT(profile,month) DO UPDATE SET bytes=bytes+excluded.bytes,redirects=redirects+1''',
                            (row['profile'], budget['month'], row['size']))
                        await self.mirror.db.commit()
                    return url
            except StorageError:
                logging.warning('Cloud location unavailable: %s/%s', scope, digest[:12])
        return None

    async def enqueue(self, scope, digest, size, route_name, *, commit=True):
        if scope == 'room' and not await self.mirror.room_cache.discovery.cloud_needed(digest):
            return False
        route = self.route(route_name)
        if route['mode'] == 'local':
            return False
        profile = self.profile(route['profile'])
        if not profile:
            raise StorageError('Cloud profile no longer exists')
        key = f"{profile['prefix']}/{route['path']}/sha256/{digest[:2]}/{digest}"
        if await self.mirror.one("SELECT 1 FROM object_locations WHERE scope=? AND digest=? AND profile=? AND object_key=? AND state='ready'",
                                 (scope, digest, route['profile'], key)):
            return False
        cursor = await self.mirror.db.execute('''INSERT OR IGNORE INTO cloud_jobs
            (scope,digest,profile,route,object_key,size,state,created) VALUES(?,?,?,?,?,?,'queued',?)''',
            (scope, digest, route['profile'], route_name, key, size, time.time()))
        if commit:
            await self.mirror.db.commit()
        return cursor.rowcount > 0

    async def retry(self, job_id):
        cursor = await self.mirror.db.execute("UPDATE cloud_jobs SET state='queued',retry_at=0,error=NULL WHERE id=? AND state IN ('failed','waiting_configuration','waiting_capacity')", (job_id,))
        await self.mirror.db.commit()
        return bool(cursor.rowcount)

    def uploading(self, digest):
        return self.active_digest == digest or any(item['digest'] == digest for item in self.transfers.values())

    def upload_concurrency(self):
        return max(1, min(8, self.settings()['concurrency']))

    async def local_cleanup_candidates(self, *, after='', digests=None):
        """Preview a bounded page; every alias must be redirect-only."""
        import re
        if not isinstance(after, str) or after and not re.fullmatch('[0-9a-f]{64}', after):
            raise ValueError('清理分页无效')
        if digests is not None and (not isinstance(digests, list) or not 1 <= len(digests) <= 50
                or any(not isinstance(d, str) or not re.fullmatch('[0-9a-f]{64}', d) for d in digests)):
            raise ValueError('请选择 1 至 50 个有效文件')
        args = digests if digests is not None else [after]
        clause = 'digest IN ('+','.join('?' for _ in digests)+')' if digests is not None else 'digest>?'
        async with self.mirror.db.execute('SELECT digest,size FROM objects WHERE local=1 AND '+clause+' ORDER BY digest LIMIT 501', args) as cursor:
            objects = await cursor.fetchall()
        files = []
        settings = self.settings()
        from .mirror_policy import category_for
        examined = None
        for row in objects[:500]:
            digest = examined = row['digest']
            if self.mirror.serving.get(digest) or self.uploading(digest):
                continue
            if await self.mirror.one("SELECT 1 FROM cloud_jobs WHERE scope='mirror' AND digest=? AND state IN ('queued','uploading') LIMIT 1", (digest,)):
                continue
            async with self.mirror.db.execute('SELECT c.url,e.category FROM cache c LEFT JOIN expected e ON e.url=c.url WHERE c.object=?', (digest,)) as cursor:
                refs = await cursor.fetchall()
            categories = {category_for(ref['url'], ref['category']) for ref in refs}
            if (not refs or any(settings['policies'][name] not in ('redirect','disabled') for name in categories)
                    or any(ref['url'] in self.mirror.inflight or ref['url'] in self.mirror.backups for ref in refs)):
                continue
            files.append(dict(digest=digest, size=row['size'], path=refs[0]['url'], categories=sorted(categories)))
            if len(files) >= 50:
                break
        more = bool(examined and (len(objects)>500 or objects and examined != objects[-1]['digest']))
        return dict(files=files, next_cursor=examined if more else None,
                    bytes=sum(file['size'] for file in files))

    async def cleanup_local(self, digests):
        if digests is None:
            raise ValueError('请先选择要清理的文件')
        removed = freed = 0
        async with self.mirror.space_lock:
            # Re-evaluate policies, all shared aliases and active transfers at
            # execution time; the preview alone never authorizes a deletion.
            candidates = await self.local_cleanup_candidates(digests=digests)
            for row in candidates['files']:
                digest = row['digest']
                path = self.mirror.root/'objects'/digest[:2]/digest
                await asyncio.to_thread(path.unlink, missing_ok=True)
                if await self.location('mirror', digest):
                    await self.mirror.db.execute('UPDATE objects SET local=0 WHERE digest=?', (digest,))
                    count = 0
                else:
                    count = (await self.mirror.one('SELECT COUNT(*) n FROM cache WHERE object=?', (digest,)))['n']
                    await self.mirror.db.execute('DELETE FROM cache WHERE object=?', (digest,))
                    await self.mirror.db.execute('DELETE FROM objects WHERE digest=?', (digest,))
                await self.mirror.db.execute('UPDATE totals SET bytes=MAX(0,bytes-?),files=MAX(0,files-?) WHERE id=1', (row['size'],count))
                removed += 1
                freed += row['size']
            await self.mirror.db.commit()
        return dict(removed=removed, freed_bytes=freed, skipped=len(set(digests))-removed)

    async def jobs_page(self, route='', state='', offset=0):
        if (not isinstance(route, str) or not isinstance(state, str)
                or route and route not in self.settings()['qiniu_routes']
                or state and state not in ('queued', 'uploading', 'finished', 'failed', 'waiting_capacity', 'waiting_configuration')
                or type(offset) is not int or not 0 <= offset <= 10000000):
            raise ValueError('云端任务筛选无效，请刷新任务列表。')
        filters, args = [], []
        for name, value in (('route', route), ('state', state)):
            if value:
                filters.append(name + '=?')
                args.append(value)
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        total = await self.mirror.one('SELECT COUNT(*) count FROM cloud_jobs' + where, args)
        async with self.mirror.db.execute('SELECT * FROM cloud_jobs' + where + ' ORDER BY id DESC LIMIT 20 OFFSET ?', (*args, offset)) as cursor:
            jobs = [dict(row) for row in await cursor.fetchall()]
        await self.describe_jobs(jobs)
        return dict(jobs=jobs, total=total['count'], offset=offset, limit=20)

    async def describe_jobs(self, visible):
        digests = list({job['digest'] for job in visible})
        files = {}
        if digests:
            marks = ','.join('?' for _ in digests)
            async with self.mirror.db.execute('''SELECT c.object digest,MIN(c.url) path,MAX(o.local) local
                FROM cache c JOIN objects o ON o.digest=c.object WHERE c.object IN ('''+marks+''')
                GROUP BY c.object''', digests) as cursor:
                files.update({('mirror', row['digest']): dict(row) for row in await cursor.fetchall()})
            async with self.mirror.room_cache.db.execute('''SELECT f.digest,MIN(f.path) path,MAX(o.local) local
                FROM files f LEFT JOIN objects o ON o.digest=f.digest WHERE f.digest IN ('''+marks+''')
                GROUP BY f.digest''', digests) as cursor:
                files.update({('room', row['digest']): dict(row) for row in await cursor.fetchall()})
        for job in visible:
            file = files.get((job['scope'], job['digest']), {})
            job.update(file_path=file.get('path'), local=bool(file.get('local')),
                       download_health=self.health.get(job['profile']))

    def record_progress(self, done, total, job_id=None):
        if job_id is not None:
            item = self.transfers.get(job_id)
            if item is None:
                return
            before = item['progress']
            item['progress'] = min(max(int(done), 0), total)
            now = time.monotonic()
            if not self.aggregate_samples:
                self.aggregate_samples.append((item['started'], self.uploaded_progress))
            self.uploaded_progress += max(0, item['progress']-before)
            if now-self.aggregate_samples[-1][0] >= .25 or item['progress']==total:
                self.aggregate_samples.append((now, self.uploaded_progress))
            samples = item['samples']
            if not samples or now - samples[-1][0] >= .25 or item['progress'] == total:
                samples.append((now, item['progress']))
            if item['progress'] >= total:
                item['phase'] = 'verifying'
            return
        self.progress = min(max(int(done), 0), total)
        now = time.monotonic()
        if not self.progress_samples or now - self.progress_samples[-1][0] >= .25 or self.progress == total:
            self.progress_samples.append((now, self.progress))
        if self.progress >= total:
            self.phase = 'verifying'

    async def snapshot(self):
        async with self.mirror.db.execute("SELECT profile,COUNT(*) files,COALESCE(SUM(size),0) bytes FROM object_locations WHERE state='ready' GROUP BY profile") as cursor:
            occupied = {row['profile']: dict(files=row['files'], bytes=row['bytes']) for row in await cursor.fetchall()}
        async with self.mirror.db.execute('SELECT id,scope,digest,profile,route,object_key,size,state,attempts,created,started,finished,retry_at,error FROM cloud_jobs ORDER BY id DESC LIMIT 20') as cursor:
            jobs = [dict(row) for row in await cursor.fetchall()]
        async with self.mirror.db.execute('SELECT state,COUNT(*) count,COALESCE(SUM(size),0) bytes FROM cloud_jobs GROUP BY state') as cursor:
            queue = {row['state']: dict(count=row['count'], bytes=row['bytes']) for row in await cursor.fetchall()}
        async with self.mirror.db.execute('SELECT route,state,COUNT(*) count,COALESCE(SUM(size),0) bytes FROM cloud_jobs GROUP BY route,state') as cursor:
            categories = {}
            for row in await cursor.fetchall():
                categories.setdefault(row['route'], {})[row['state']] = dict(count=row['count'], bytes=row['bytes'])
        active_jobs = []
        if self.transfers:
            ids = list(self.transfers)
            async with self.mirror.db.execute('SELECT * FROM cloud_jobs WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY id', ids) as cursor:
                active_jobs = [dict(row) for row in await cursor.fetchall()]
            for job in active_jobs:
                item = self.transfers.get(job['id'])
                if item:
                    recent = [sample for sample in item['samples'] if sample[0] >= time.monotonic()-8]
                    rate = ((recent[-1][1]-recent[0][1])/max(.001, recent[-1][0]-recent[0][0])
                            if len(recent)>1 and item['phase']=='uploading' and time.monotonic()-recent[-1][0]<3 else 0)
                    job.update(progress=item['progress'], phase=item['phase'],
                               elapsed_seconds=round(time.monotonic()-item['started']), upload_bytes_per_second=round(rate))
        elif self.active:
            row = await self.mirror.one('SELECT * FROM cloud_jobs WHERE id=?', (self.active,))
            if row:
                active_jobs = [dict(row, progress=self.progress, phase=self.phase)]
        await self.describe_jobs([*jobs, *active_jobs])
        active_job = active_jobs[0] if active_jobs else None
        now = time.monotonic()
        samples = list(self.progress_samples)
        recent = [sample for sample in samples if sample[0] >= now - 8]
        speed = ((recent[-1][1] - recent[0][1]) / max(.001, recent[-1][0] - recent[0][0])
                 if len(recent) > 1 and self.active and self.phase == 'uploading' and now - recent[-1][0] < 3 else 0)
        if self.transfers:
            recent = [sample for sample in self.aggregate_samples if sample[0]>=now-8]
            speed = ((recent[-1][1]-recent[0][1])/max(.001,recent[-1][0]-recent[0][0])
                     if len(recent)>1 and now-recent[-1][0]<3 else 0)
        phase = ('uploading' if any(job.get('phase')=='uploading' for job in active_jobs) else 'verifying') if active_jobs else 'idle'
        paused = self.settings()['qiniu_paused']
        blocked = await self.mirror.one("SELECT MIN(retry_at) next_retry FROM cloud_jobs WHERE state='queued' AND retry_at>?", (time.time(),))
        worker_state = ('paused' if paused else phase if active_jobs else 'queued' if queue.get('queued') else 'attention' if queue.get('failed') or queue.get('waiting_configuration') or queue.get('waiting_capacity') else 'idle')
        if worker_state == 'queued':
            available = await self.mirror.one("SELECT 1 FROM cloud_jobs WHERE state='queued' AND retry_at<=? LIMIT 1", (time.time(),))
            if not available:
                worker_state = 'waiting_budget'
        profiles = [dict(id=p['id'], name=p.get('name', ''), bucket=p['bucket'], domain=p['domain'], prefix=p['prefix'],
                         provider=p.get('provider', 'qiniu'), quota_unit=p.get('quota_unit', 'GiB'),
                         configured=bool(credentials(p['id'])), quota_gib=p['quota_gib'],
                         daily_upload_gib=p['daily_upload_gib'], health=self.health.get(p['id']),
                         egress=await self.egress(p),
                         tls=self.tls.status(p) if p.get('provider') == 'rainyun' else None,
                         **occupied.get(p['id'], {'files': 0, 'bytes': 0}))
                    for p in self.settings()['qiniu_profiles']]
        return dict(paused=paused, profiles=profiles, categories=categories,
                    automatic=dict(self.automatic),
                    jobs=jobs, queue=queue, active=active_job['id'] if active_job else None, active_job=active_job,
                    active_jobs=active_jobs, upload_concurrency=self.upload_concurrency(),
                    queue_window=QUEUE_WINDOW, index_batch=INDEX_BATCH,
                    progress=active_job.get('progress', 0) if active_job else 0,
                    worker_state=worker_state, phase=phase,
                    upload_bytes_per_second=round(speed), elapsed_seconds=active_job.get('elapsed_seconds', round(now-self.upload_started)) if active_job else 0,
                    next_retry_at=blocked['next_retry'],
                    accounting='tracked_objects_and_reserved_redirect_bytes_only; actual provider billing must be checked separately')

    async def save_reconciliation(self):
        data = dict(automatic=self.automatic, cursors=self.reconcile_cursors,
                    bounds=self.reconcile_bounds, signature=self.reconcile_signature)
        await self.mirror.db.execute('INSERT OR REPLACE INTO cloud_reconciliation VALUES(1,?)',
                                    (json.dumps(data),))
        await self.mirror.db.commit()

    async def reconciliation_outcome(self, scope, row, route_name):
        return (await self.reconcile_entries(scope, [(row, route_name)]))[0]

    async def reconcile_entries(self, scope, entries):
        """Compare a batch by indexed keys. Only bytes already on disk enter here.

        Download policy controls future fetches, not migration of existing copies.
        SQL batches avoid a database round trip for each individual file.
        """
        outcomes, pending = [], {}
        for row, name in entries:
            route = self.route(name)
            if route['mode'] == 'local':
                outcomes.append('local_only')
                continue
            profile = self.profile(route['profile'])
            if not profile:
                raise StorageError('Cloud profile no longer exists')
            key = f"{profile['prefix']}/{route['path']}/sha256/{row['digest'][:2]}/{row['digest']}"
            identity = (row['digest'], route['profile'], key)
            outcomes.append(identity)
            pending[identity] = (scope, row['digest'], route['profile'], name, key, row['size'], time.time())
        results = {}
        candidates = list(pending.items())
        for offset in range(0, len(candidates), 100):
            batch = candidates[offset:offset+100]
            digests = list({identity[0] for identity, _ in batch})
            marks = ','.join('?' for _ in digests)
            async with self.mirror.db.execute("SELECT digest,profile,object_key FROM object_locations WHERE scope=? AND state='ready' AND digest IN ("+marks+')', (scope, *digests)) as cursor:
                ready = {tuple(row) for row in await cursor.fetchall()}
            async with self.mirror.db.execute('SELECT digest,profile,object_key FROM cloud_jobs WHERE scope=? AND digest IN ('+marks+')', (scope, *digests)) as cursor:
                existing = {tuple(row) for row in await cursor.fetchall()}
            insert = []
            for identity, values in batch:
                if identity in ready:
                    results[identity] = 'already_cloud'
                else:
                    results[identity] = 'existing_job'
                    if identity not in existing:
                        insert.append(values)
                        results[identity] = 'queued'
            if insert:
                # No RETURNING cursor: other mirror coroutines can commit this
                # shared connection between awaits. Finish all write statements
                # inside one worker call before another commit can run.
                cursor = await self.mirror.db.executemany('''INSERT OR IGNORE INTO cloud_jobs
                    (scope,digest,profile,route,object_key,size,created,state) VALUES(?,?,?,?,?,?,?,'queued')''', insert)
                await cursor.close()
        # Duplicate aliases in the same batch must count as one upload.
        seen = set()
        for index, outcome in enumerate(outcomes):
            if isinstance(outcome, tuple):
                outcomes[index] = 'existing_job' if outcome in seen and results[outcome]=='queued' else results[outcome]
                seen.add(outcome)
        return outcomes

    async def reconcile_batch(self):
        """Bounded index walk; enqueue existing bytes, never fetch files here."""
        settings = self.settings()
        if not settings['qiniu_profiles']:
            self.automatic.update(state='unconfigured', last_at=time.time())
            await self.save_reconciliation()
            return
        signature = hashlib.sha256(json.dumps(dict(version=2, routes=settings['qiniu_routes'],
            profiles=[p['id'] for p in settings['qiniu_profiles']], policies=settings['policies']),
            sort_keys=True).encode()).hexdigest()
        now = time.time()
        if signature != self.reconcile_signature or self.reconcile_bounds is None:
            # Snapshot rowid ceilings so each reference is counted once per pass,
            # even when uploads remove local copies while the pass is running.
            if signature == self.reconcile_signature and now < self.automatic.get('next_at', 0):
                return
            mirror = await self.mirror.one('SELECT COALESCE(MAX(rowid),0) n FROM cache')
            room = await self.mirror.room_cache.one('SELECT COALESCE(MAX(rowid),0) n FROM objects')
            self.reconcile_bounds = dict(mirror=mirror['n'], room=room['n'])
            self.reconcile_cursors = dict(mirror=0, room=0)
            self.reconcile_signature = signature
            self.automatic.update(state='scanning', scanned=0, queued=0,
                round=self.automatic['round']+1, started_at=now, outcomes={}, error=None)
        pending = await self.mirror.one("SELECT COUNT(*) n FROM cloud_jobs WHERE state IN ('queued','uploading')")
        if pending['n'] >= QUEUE_WINDOW:
            self.automatic.update(state='draining', last_at=time.time())
            await self.save_reconciliation()
            return
        scanned = queued = 0
        outcomes = dict(self.automatic.get('outcomes', {}))
        def count(outcome):
            nonlocal queued
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            queued += outcome == 'queued'
        from .mirror_policy import category_for
        async with self.mirror.db.execute('''SELECT c.rowid position,c.url,c.object digest,c.size,e.category
            FROM cache c JOIN objects o ON o.digest=c.object LEFT JOIN expected e ON e.url=c.url
            WHERE c.rowid>? AND c.rowid<=? AND o.local=1 ORDER BY c.rowid LIMIT ?''',
            (self.reconcile_cursors['mirror'], self.reconcile_bounds['mirror'], min(INDEX_BATCH, QUEUE_WINDOW-pending['n']))) as cursor:
            rows = await cursor.fetchall()
        for outcome in await self.reconcile_entries('mirror', [(row, category_for(row['url'], row['category'])) for row in rows]):
            count(outcome)
        limit = min(INDEX_BATCH, QUEUE_WINDOW-pending['n'])
        self.reconcile_cursors['mirror'] = rows[-1]['position'] if len(rows) == limit else self.reconcile_bounds['mirror']
        scanned += len(rows)
        cache = self.mirror.room_cache
        async with cache.db.execute('SELECT rowid position,digest,size FROM objects WHERE local=1 AND rowid>? AND rowid<=? ORDER BY rowid LIMIT ?',
                                    (self.reconcile_cursors['room'], self.reconcile_bounds['room'], ROOM_INDEX_BATCH)) as cursor:
            rows = await cursor.fetchall()
        refs = {}
        if rows:
            digests = [row['digest'] for row in rows]
            async with cache.db.execute('SELECT DISTINCT digest,path FROM files WHERE lease_until>? AND digest IN ('+','.join('?' for _ in digests)+')', (time.time(), *digests)) as cursor:
                for ref in await cursor.fetchall():
                    refs.setdefault(ref['digest'], set()).add(cache.route_name(ref['path']))
        entries = []
        for row in rows:
            routes = refs.get(row['digest'], set())
            if routes and not await cache.discovery.cloud_needed(row['digest']):
                count('public_source')
                continue
            if not routes:
                count('expired')
            for route in routes:
                entries.append((row, route))
        for outcome in await self.reconcile_entries('room', entries):
            count(outcome)
        self.reconcile_cursors['room'] = rows[-1]['position'] if len(rows) == ROOM_INDEX_BATCH else self.reconcile_bounds['room']
        scanned += len(rows)
        done = self.reconcile_cursors == self.reconcile_bounds
        self.automatic.update(state='watching' if done else 'scanning', outcomes=outcomes,
                              scanned=self.automatic['scanned']+scanned, queued=self.automatic['queued']+queued,
                              scanned_total=self.automatic['scanned_total']+scanned,
                              queued_total=self.automatic['queued_total']+queued,
                              last_at=time.time(), error=None)
        if done:
            self.automatic.update(completed_at=time.time(), next_at=time.time()+300)
            self.reconcile_bounds = None
        await self.save_reconciliation()

    async def reconcile_loop(self):
        # Let startup and authentication requests finish before the first batch.
        await asyncio.sleep(5)
        while True:
            try:
                await self.reconcile_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Automatic cloud reconciliation will retry')
                self.automatic.update(state='retrying', error='自动整理暂未完成，稍后分批重试；现有任务继续执行', last_at=time.time())
            await asyncio.sleep(1 if self.reconcile_bounds is not None else 30)

    async def run(self):
        last_retry_check = 0
        try:
            while True:
                try:
                    if time.time()-last_retry_check >= 30:
                        await self.retry_blocked()
                        last_retry_check = time.time()
                    for job_id, task in list(self.upload_tasks.items()):
                        if task.done():
                            del self.upload_tasks[job_id]
                            task.result()
                    available = self.upload_concurrency()-len(self.upload_tasks)
                    if not self.settings()['qiniu_paused'] and available > 0:
                        async with self.mirror.db.execute("SELECT * FROM cloud_jobs WHERE state='queued' AND retry_at<=? ORDER BY id LIMIT ?", (time.time(), self.upload_concurrency()+len(self.upload_tasks))) as cursor:
                            rows = await cursor.fetchall()
                        for row in rows:
                            if available <= 0:
                                break
                            if row['id'] not in self.upload_tasks:
                                self.upload_tasks[row['id']] = asyncio.create_task(self.process(row))
                                available -= 1
                    await asyncio.sleep(.2 if self.upload_tasks else 1)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception('Cloud worker retrying')
                    await asyncio.sleep(2)
        finally:
            tasks = list(self.upload_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.upload_tasks.clear()

    async def retry_blocked(self):
        """Recheck failures independently; never hold the main transfer queue."""
        async with self.mirror.db.execute("SELECT * FROM cloud_jobs WHERE state IN ('failed','waiting_capacity','waiting_configuration') AND retry_at<=? ORDER BY retry_at,id LIMIT 20", (time.time(),)) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            source = (self.mirror.root/'objects'/row['digest'][:2]/row['digest'] if row['scope']=='mirror' else self.mirror.room_cache.path(row['digest']))
            if not source.is_file():
                # Rebuild missing ordinary bytes through the download policy,
                # rather than repeatedly uploading a nonexistent staging path.
                if row['scope'] == 'mirror' and not await self.location('mirror', row['digest']):
                    obj = await self.mirror.one('SELECT size,local FROM objects WHERE digest=?', (row['digest'],))
                    if obj and obj['local']:
                        await self.mirror.db.execute('UPDATE objects SET local=0 WHERE digest=?', (row['digest'],))
                        await self.mirror.db.execute('UPDATE totals SET bytes=MAX(0,bytes-?) WHERE id=1', (obj['size'],))
                    async with self.mirror.db.execute('SELECT url FROM cache WHERE object=?', (row['digest'],)) as cursor:
                        urls = await cursor.fetchall()
                    for item in urls:
                        await self.mirror.remember_failure(item['url'], RuntimeError('上传暂存已不存在，自动重新准备资源'))
                    await self.mirror.db.execute('DELETE FROM cloud_jobs WHERE id=?', (row['id'],))
                else:
                    await self.mirror.db.execute('UPDATE cloud_jobs SET retry_at=? WHERE id=?', (time.time()+21600,row['id']))
                continue
            await self.mirror.db.execute("UPDATE cloud_jobs SET state='queued',error=NULL,retry_at=0 WHERE id=?", (row['id'],))
        await self.mirror.db.commit()

    async def check_download(self, profile, key):
        storage = self.storage(profile)
        async def probe(url):
            try:
                async with self.mirror.probe_http.get(url, headers={'Range':'bytes=0-0'},
                                                      allow_redirects=False) as response:
                    if response.status not in (200, 206):
                        return '下载验证返回 HTTP ' + str(response.status)
                    if not await response.content.read(1):
                        return '下载验证返回空文件'
                    return None
            except Exception as exc:
                return '下载验证失败：' + (str(exc).split('?')[0][:160] or type(exc).__name__)
        error = await probe(storage.download_url(key))
        health = dict(healthy=error is None, domain_healthy=error is None,
                      download_via='domain', checked=time.time(), error=error)
        if error and hasattr(storage, 'endpoint_download_url'):
            fallback_error = await probe(storage.endpoint_download_url(key))
            health.update(healthy=fallback_error is None, download_via='endpoint',
                          domain_error=error, error=fallback_error,
                          warning='自定义域名不可用，已切换同一空间的 S3 HTTPS 地址' if not fallback_error else None)
        self.health[profile] = health
        return health['healthy']

    async def health_loop(self):
        while True:
            try:
                for profile in self.settings()['qiniu_profiles']:
                    row = await self.mirror.one("SELECT object_key FROM object_locations WHERE profile=? AND state='ready' ORDER BY verified_at DESC LIMIT 1", (profile['id'],))
                    if row and await self.check_download(profile['id'], row['object_key']):
                        await self.release_verified_local(profile['id'])
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Cloud download health check failed')
            await asyncio.sleep(120)

    async def download_ready(self, profile, key):
        # Each object is independently verified by the storage adapter. Checking
        # the same download endpoint once per short window avoids thousands of
        # redundant TLS handshakes while preserving periodic health checks.
        async with self.health_locks.setdefault(profile, asyncio.Lock()):
            health = self.health.get(profile)
            if health and health['healthy'] and time.time()-health['checked'] < 60:
                return True
            return await self.check_download(profile, key)

    async def release_verified_local(self, profile):
        cloud_routes = [name for name, route in self.settings()['qiniu_routes'].items()
                        if route['mode'] == 'cloud']
        if not cloud_routes:
            return
        placeholders = ','.join('?' for _ in cloud_routes)
        async with self.mirror.db.execute('''SELECT l.digest,l.route FROM object_locations l
            JOIN objects o ON o.digest=l.digest AND o.local=1
            WHERE l.scope='mirror' AND l.profile=? AND l.state='ready'
            AND l.route IN (''' + placeholders + ''') LIMIT 20''', (profile, *cloud_routes)) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            await self.release_local('mirror', row['digest'])
        async with self.mirror.room_cache.db.execute('''SELECT digest FROM objects WHERE local=1
            ORDER BY created DESC LIMIT 20 OFFSET ?''', (self.room_release_offset,)) as cursor:
            rows = await cursor.fetchall()
        if not rows and self.room_release_offset:
            self.room_release_offset = 0
            return
        self.room_release_offset += len(rows)
        for row in rows:
            location = await self.location('room', row['digest'])
            if location and location['profile'] == profile and self.route(location['route'])['mode'] == 'cloud':
                await self.release_local('room', row['digest'])

    async def process(self, row):
        if row['scope'] == 'room' and not await self.mirror.room_cache.discovery.cloud_needed(row['digest']):
            await self.mirror.db.execute("DELETE FROM cloud_jobs WHERE id=? AND state='queued'",(row['id'],))
            await self.mirror.db.commit()
            return
        profile = self.profile(row['profile'])
        if not profile or not credentials(row['profile']):
            await self._state(row['id'], 'waiting_configuration', '云端空间或密钥未配置')
            return
        now = time.time()
        day = int(now // 86400)
        source = (self.mirror.root / 'objects' / row['digest'][:2] / row['digest'] if row['scope'] == 'mirror'
                  else self.mirror.room_cache.path(row['digest']))
        if not source.is_file():
            await self._state(row['id'], 'failed', '本地副本不存在，无法安全迁移')
            return
        async with self.upload_lock:
            used = await self.mirror.one("SELECT COALESCE(SUM(size),0) bytes FROM object_locations WHERE profile=? AND state='ready'", (row['profile'],))
            spent = await self.mirror.one('SELECT uploaded FROM cloud_budget WHERE profile=? AND day=?', (row['profile'], day))
            reserved = [entry for entry in self.reservations.values() if entry['profile']==row['profile']]
            direct = await self.direct.reserved(row['profile'], day)
            reserved.append(dict(profile=row['profile'], day=day, size=direct['today']))
            # Expired unfinished grants still reserve physical space until deleted.
            used = dict(used, bytes=used['bytes'] + direct['bytes'] - direct['today'])
            if used['bytes'] + sum(entry['size'] for entry in reserved) + row['size'] > profile['quota_gib'] * profile_unit(profile):
                if used['bytes'] + row['size'] <= profile['quota_gib'] * profile_unit(profile):
                    await self.mirror.db.execute('UPDATE cloud_jobs SET retry_at=?,error=? WHERE id=?',
                        (time.time()+5, '等待在途上传释放预留容量，稍后自动继续', row['id']))
                    await self.mirror.db.commit()
                else:
                    await self._state(row['id'], 'waiting_capacity', '本服务的云端空间预算已满')
                return
            if (spent['uploaded'] if spent else 0) + sum(entry['size'] for entry in reserved if entry['day']==day) + row['size'] > profile['daily_upload_gib'] * profile_unit(profile):
                retry_at = (day+1)*86400 if (spent['uploaded'] if spent else 0)+row['size'] > profile['daily_upload_gib']*profile_unit(profile) else time.time()+5
                await self.mirror.db.execute("UPDATE cloud_jobs SET retry_at=?,error=? WHERE id=?",
                    (retry_at, '今日上传预算已用完，下一预算日自动继续' if retry_at==(day+1)*86400 else '等待在途上传释放预留预算，稍后自动继续', row['id']))
                await self.mirror.db.commit()
                return
            await self.mirror.db.execute("UPDATE cloud_jobs SET state='uploading',started=?,attempts=attempts+1,error=NULL,retry_at=0 WHERE id=?", (now, row['id']))
            await self.mirror.db.commit()
            self.reservations[row['id']] = dict(profile=row['profile'], day=day, size=row['size'])
            started = time.monotonic()
            self.transfers[row['id']] = dict(digest=row['digest'], progress=0, phase='uploading', started=started,
                                             samples=deque([(started, 0)], maxlen=120))
        try:
            loop = asyncio.get_running_loop()
            def progress(done, total):
                loop.call_soon_threadsafe(self.record_progress, done, row['size'], row['id'])
            await self.storage(row['profile']).put_verified(source, row['object_key'], row['digest'], row['size'], progress=progress)
            self.record_progress(row['size'], row['size'], row['id'])
            async with self.upload_lock:
                await self.mirror.db.execute('''INSERT OR REPLACE INTO object_locations
                    (scope,digest,profile,route,object_key,size,state,verified_at,last_error)
                    VALUES(?,?,?,?,?,?,'ready',?,NULL)''',
                    (row['scope'], row['digest'], row['profile'], row['route'], row['object_key'], row['size'], time.time()))
                await self.mirror.db.execute('''INSERT INTO cloud_budget VALUES(?,?,?)
                    ON CONFLICT(profile,day) DO UPDATE SET uploaded=uploaded+excluded.uploaded''',
                    (row['profile'], day, row['size']))
                await self.mirror.db.execute("UPDATE cloud_jobs SET state='finished',finished=?,error=NULL WHERE id=?", (time.time(), row['id']))
                await self.mirror.db.commit()
                self.reservations.pop(row['id'], None)
            healthy = await self.download_ready(row['profile'], row['object_key'])
            if healthy and self.route(row['route'])['mode'] == 'cloud':
                await self.release_local(row['scope'], row['digest'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if getattr(exc, 'retryable', False) and row['attempts']+1 < 3:
                delay = 5 * 3**row['attempts']
                await self.mirror.db.execute("UPDATE cloud_jobs SET state='queued',retry_at=?,error=? WHERE id=?",
                    (time.time()+delay, str(exc)[:130]+f'；{delay} 秒后自动重试', row['id']))
                await self.mirror.db.commit()
                logging.warning('Cloud upload %s will retry: %s', row['id'], str(exc)[:160])
            else:
                logging.exception('Cloud copy failed for job %s', row['id'])
                await self._state(row['id'], 'failed', str(exc)[:160])
        finally:
            async with self.upload_lock:
                self.reservations.pop(row['id'], None)
                self.transfers.pop(row['id'], None)

    async def _state(self, job_id, state, error):
        job = await self.mirror.one('SELECT attempts FROM cloud_jobs WHERE id=?', (job_id,))
        delay = min(21600, 300*2**min((job['attempts'] if job else 0), 6))
        await self.mirror.db.execute('UPDATE cloud_jobs SET state=?,finished=?,error=?,retry_at=? WHERE id=?',
                                     (state, time.time(), error, time.time()+delay, job_id))
        await self.mirror.db.commit()

    async def routes_verified(self, scope, digest, names):
        """Shared hashes must reach every current destination before deletion."""
        for name in names:
            route = self.route(name)
            profile = self.profile(route['profile'])
            if route['mode'] != 'cloud' or not profile:
                return False
            key = f"{profile['prefix']}/{route['path']}/sha256/{digest[:2]}/{digest}"
            if not await self.mirror.one("SELECT 1 FROM object_locations WHERE scope=? AND digest=? AND profile=? AND object_key=? AND state='ready'",
                                         (scope, digest, route['profile'], key)):
                return False
            health = self.health.get(route['profile'])
            if health and not health['healthy']:
                return False
        return True

    async def release_local(self, scope, digest):
        if scope == 'mirror':
            async with self.mirror.space_lock:
                if self.mirror.serving.get(digest):
                    return False
                # A continuous prefetch stream must not pin every uploaded
                # object. Protect only this digest and its remaining consumers.
                if await self.mirror.one("SELECT 1 FROM cloud_jobs WHERE scope='mirror' AND digest=? AND state!='finished' LIMIT 1", (digest,)):
                    return False
                async with self.mirror.db.execute(
                        'SELECT c.url,e.category FROM cache c LEFT JOIN expected e ON e.url=c.url WHERE c.object=?',
                        (digest,)) as cursor:
                    references = await cursor.fetchall()
                from .mirror_policy import category_for
                if not await self.routes_verified(scope, digest, {category_for(ref['url'], ref['category']) for ref in references}):
                    return False
                row = await self.mirror.one('SELECT size,local FROM objects WHERE digest=?', (digest,))
                if not row or not row['local'] or not await self.location(scope, digest):
                    return False
                path = self.mirror.root / 'objects' / digest[:2] / digest
                await asyncio.to_thread(path.unlink, missing_ok=True)
                await self.mirror.db.execute('UPDATE objects SET local=0 WHERE digest=?', (digest,))
                await self.mirror.db.execute('UPDATE totals SET bytes=MAX(bytes-?,0) WHERE id=1', (row['size'],))
                await self.mirror.db.commit()
                return True
        cache = self.mirror.room_cache
        location = await self.location(scope, digest)
        if not location or self.route(location['route'])['mode'] != 'cloud':
            return False
        async with cache.lock:
            if cache.serving.get(digest) or digest in cache.uploading or not await self.location(scope, digest):
                return False
            if await cache.needs_local(digest):
                return False
            async with cache.db.execute('SELECT DISTINCT path FROM files WHERE digest=? AND lease_until>?', (digest, time.time())) as cursor:
                references = await cursor.fetchall()
            if not await self.routes_verified(scope, digest, {cache.route_name(ref['path']) for ref in references}):
                return False
            if await self.mirror.one("SELECT 1 FROM cloud_jobs WHERE scope='room' AND digest=? AND state!='finished' LIMIT 1", (digest,)):
                return False
            row = await cache.one('SELECT local FROM objects WHERE digest=?', (digest,))
            if not row or not row['local']:
                return False
            await asyncio.to_thread(cache.path(digest).unlink, missing_ok=True)
            await cache.db.execute('UPDATE objects SET local=0 WHERE digest=?', (digest,))
            await cache.db.commit()
            return True

    async def delete_room(self, digest):
        async with self.mirror.db.execute("SELECT * FROM object_locations WHERE scope='room' AND digest=? AND state='ready'", (digest,)) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            await self.storage(row['profile']).delete(row['object_key'])
            await self.mirror.db.execute('DELETE FROM object_locations WHERE scope=? AND digest=? AND profile=? AND object_key=?',
                                         ('room', digest, row['profile'], row['object_key']))
        await self.mirror.db.commit()

    async def discard_room_jobs(self, digest):
        await self.mirror.db.execute("DELETE FROM cloud_jobs WHERE scope='room' AND digest=? AND state!='uploading'", (digest,))
        await self.mirror.db.commit()

    async def migrate(self, route_name, limit):
        if route_name not in self.settings()['qiniu_routes'] or not 1 <= limit <= 200:
            raise ValueError('invalid cloud migration selection')
        route = self.route(route_name)
        if route['mode'] == 'local':
            raise ValueError('enable copy or cloud mode for this resource type first')
        queued = 0
        if route_name.startswith('room_'):
            kind = {'room_mods': 'mods/', 'room_resourcepacks': 'resourcepacks/',
                    'room_shaderpacks': 'shaderpacks/'}[route_name]
            async with self.mirror.room_cache.db.execute('''SELECT o.digest,o.size FROM objects o
                WHERE o.local=1 AND EXISTS(SELECT 1 FROM files f WHERE f.digest=o.digest AND f.path LIKE ? AND f.lease_until>?)
                ORDER BY o.created DESC''', (kind + '%', time.time())) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                queued += await self.enqueue('room', row['digest'], row['size'], route_name)
                if queued >= limit:
                    break
        else:
            async with self.mirror.db.execute('''SELECT c.object digest,c.size,c.url,e.category
                FROM cache c LEFT JOIN expected e ON e.url=c.url JOIN objects o ON o.digest=c.object
                WHERE o.local=1 ORDER BY c.fetched DESC''') as cursor:
                rows = await cursor.fetchall()
            from .mirror_policy import category_for
            seen = set()
            for row in rows:
                if row['digest'] not in seen and category_for(row['url'], row['category']) == route_name:
                    seen.add(row['digest'])
                    queued += await self.enqueue('mirror', row['digest'], row['size'], route_name)
                    if queued >= limit:
                        break
        return queued
