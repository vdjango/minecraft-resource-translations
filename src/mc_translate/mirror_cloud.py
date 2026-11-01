"""Indexed background Kodo copies shared by mirror files and room file leases."""
import asyncio
import json
import logging
import time

from .mirror_storage import QiniuStorage, StorageError, credentials

GIB = 1024 ** 3


class MirrorCloud:
    def __init__(self, mirror, settings):
        self.mirror, self.settings = mirror, settings
        self.worker = None
        self.health_task = None
        self.active = None
        self.active_digest = None
        self.progress = 0
        self.health = {}
        self.room_release_offset = 0

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
            CREATE TABLE IF NOT EXISTS cloud_budget(profile TEXT,day INTEGER,uploaded INTEGER,
                PRIMARY KEY(profile,day));
        ''')
        await self.mirror.db.execute("UPDATE cloud_jobs SET state='queued',retry_at=0 WHERE state='uploading'")
        await self.mirror.db.commit()

    def start(self):
        self.worker = asyncio.create_task(self.run())
        self.health_task = asyncio.create_task(self.health_loop())

    async def close(self):
        tasks = [task for task in (self.worker, self.health_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def profile(self, identifier):
        return next((profile for profile in self.settings()['qiniu_profiles'] if profile['id'] == identifier), None)

    def storage(self, identifier):
        profile = self.profile(identifier)
        if not profile:
            raise StorageError('Qiniu profile no longer exists')
        return QiniuStorage(profile, signed_url_minutes=self.settings()['qiniu_signed_url_minutes'])

    def route(self, name):
        return self.settings()['qiniu_routes'][name]

    async def location(self, scope, digest):
        return await self.mirror.one('''SELECT * FROM object_locations WHERE scope=? AND digest=?
            AND state='ready' ORDER BY verified_at DESC LIMIT 1''', (scope, digest))

    async def download_url(self, scope, digest, *, expires_seconds=None):
        row = await self.location(scope, digest)
        if row:
            health = self.health.get(row['profile'])
            if health and not health['healthy'] and time.time() - health['checked'] < 300:
                return None
            try:
                return self.storage(row['profile']).download_url(row['object_key'], expires_seconds=expires_seconds)
            except StorageError:
                logging.warning('Cloud location unavailable: %s/%s', scope, digest[:12])
        return None

    async def enqueue(self, scope, digest, size, route_name):
        route = self.route(route_name)
        if route['mode'] == 'local':
            return False
        profile = self.profile(route['profile'])
        if not profile:
            raise StorageError('Qiniu profile no longer exists')
        key = f"{profile['prefix']}/{route['path']}/sha256/{digest[:2]}/{digest}"
        if await self.mirror.one("SELECT 1 FROM object_locations WHERE scope=? AND digest=? AND profile=? AND object_key=? AND state='ready'",
                                 (scope, digest, route['profile'], key)):
            return False
        cursor = await self.mirror.db.execute('''INSERT OR IGNORE INTO cloud_jobs
            (scope,digest,profile,route,object_key,size,state,created) VALUES(?,?,?,?,?,?,'queued',?)''',
            (scope, digest, route['profile'], route_name, key, size, time.time()))
        await self.mirror.db.commit()
        return cursor.rowcount > 0

    async def retry(self, job_id):
        cursor = await self.mirror.db.execute("UPDATE cloud_jobs SET state='queued',retry_at=0,error=NULL WHERE id=? AND state IN ('failed','waiting_configuration','waiting_capacity')", (job_id,))
        await self.mirror.db.commit()
        return bool(cursor.rowcount)

    async def snapshot(self):
        async with self.mirror.db.execute("SELECT profile,COUNT(*) files,COALESCE(SUM(size),0) bytes FROM object_locations WHERE state='ready' GROUP BY profile") as cursor:
            occupied = {row['profile']: dict(files=row['files'], bytes=row['bytes']) for row in await cursor.fetchall()}
        async with self.mirror.db.execute('SELECT id,scope,digest,profile,route,size,state,attempts,created,started,finished,error FROM cloud_jobs ORDER BY id DESC LIMIT 20') as cursor:
            jobs = [dict(row) for row in await cursor.fetchall()]
        profiles = [dict(id=p['id'], bucket=p['bucket'], domain=p['domain'], prefix=p['prefix'],
                         configured=bool(credentials(p['id'])), quota_gib=p['quota_gib'],
                         daily_upload_gib=p['daily_upload_gib'], health=self.health.get(p['id']),
                         **occupied.get(p['id'], {'files': 0, 'bytes': 0}))
                    for p in self.settings()['qiniu_profiles']]
        return dict(paused=self.settings()['qiniu_paused'], profiles=profiles,
                    jobs=jobs, active=self.active, progress=self.progress,
                    accounting='tracked_objects_only; actual Kodo billing must be checked separately')

    async def run(self):
        while True:
            try:
                if self.settings()['qiniu_paused']:
                    await asyncio.sleep(3)
                    continue
                row = await self.mirror.one("SELECT * FROM cloud_jobs WHERE state='queued' AND retry_at<=? ORDER BY id LIMIT 1", (time.time(),))
                if row:
                    await self.process(row)
                else:
                    await asyncio.sleep(3)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception('Cloud worker retrying')
                await asyncio.sleep(5)

    async def check_download(self, profile, key):
        try:
            url = self.storage(profile).download_url(key)
            async with self.mirror.probe_http.get(url, headers={'Range':'bytes=0-0'},
                                                  allow_redirects=False) as response:
                healthy = response.status in (200, 206)
        except Exception:
            healthy = False
        self.health[profile] = dict(healthy=healthy, checked=time.time())
        return healthy

    async def health_loop(self):
        await asyncio.sleep(25)
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
        profile = self.profile(row['profile'])
        if not profile or not credentials(row['profile']):
            await self._state(row['id'], 'waiting_configuration', '七牛空间或密钥未配置')
            return
        now = time.time()
        day = int(now // 86400)
        used = await self.mirror.one("SELECT COALESCE(SUM(size),0) bytes FROM object_locations WHERE profile=? AND state='ready'", (row['profile'],))
        spent = await self.mirror.one('SELECT uploaded FROM cloud_budget WHERE profile=? AND day=?', (row['profile'], day))
        if used['bytes'] + row['size'] > profile['quota_gib'] * GIB:
            await self._state(row['id'], 'waiting_capacity', '本服务的七牛空间预算已满')
            return
        if (spent['uploaded'] if spent else 0) + row['size'] > profile['daily_upload_gib'] * GIB:
            await self.mirror.db.execute("UPDATE cloud_jobs SET retry_at=? WHERE id=?", ((day + 1) * 86400, row['id']))
            await self.mirror.db.commit()
            return
        source = (self.mirror.root / 'objects' / row['digest'][:2] / row['digest'] if row['scope'] == 'mirror'
                  else self.mirror.room_cache.path(row['digest']))
        if not source.is_file():
            await self._state(row['id'], 'failed', '本地副本不存在，无法安全迁移')
            return
        self.active, self.active_digest, self.progress = row['id'], row['digest'], 0
        await self.mirror.db.execute("UPDATE cloud_jobs SET state='uploading',started=?,attempts=attempts+1 WHERE id=?", (now, row['id']))
        await self.mirror.db.commit()
        try:
            loop = asyncio.get_running_loop()
            def progress(done, total):
                loop.call_soon_threadsafe(setattr, self, 'progress', min(int(done), row['size']))
            await self.storage(row['profile']).put_verified(source, row['object_key'], row['digest'], row['size'], progress=progress)
            await self.mirror.db.execute('''INSERT OR REPLACE INTO object_locations
                (scope,digest,profile,route,object_key,size,state,verified_at,last_error)
                VALUES(?,?,?,?,?,?,'ready',?,NULL)''',
                (row['scope'], row['digest'], row['profile'], row['route'], row['object_key'], row['size'], time.time()))
            await self.mirror.db.execute('''INSERT INTO cloud_budget VALUES(?,?,?)
                ON CONFLICT(profile,day) DO UPDATE SET uploaded=uploaded+excluded.uploaded''',
                (row['profile'], day, row['size']))
            await self.mirror.db.execute("UPDATE cloud_jobs SET state='finished',finished=?,error=NULL WHERE id=?", (time.time(), row['id']))
            await self.mirror.db.commit()
            self.progress = row['size']
            healthy = await self.check_download(row['profile'], row['object_key'])
            if healthy and self.route(row['route'])['mode'] == 'cloud':
                await self.release_local(row['scope'], row['digest'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception('Cloud copy failed for job %s', row['id'])
            await self._state(row['id'], 'failed', str(exc)[:160])
        finally:
            self.active = None
            self.active_digest = None

    async def _state(self, job_id, state, error):
        await self.mirror.db.execute('UPDATE cloud_jobs SET state=?,finished=?,error=? WHERE id=?',
                                     (state, time.time(), error, job_id))
        await self.mirror.db.commit()

    async def release_local(self, scope, digest):
        if scope == 'mirror':
            async with self.mirror.space_lock:
                if self.mirror.serving.get(digest) or any(not task.done() for task in self.mirror.inflight.values()):
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
        async with cache.lock:
            if cache.serving.get(digest) or digest in cache.uploading or not await self.location(scope, digest):
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
                WHERE o.local=1 AND EXISTS(SELECT 1 FROM files f WHERE f.digest=o.digest AND f.path LIKE ?)
                ORDER BY o.created DESC LIMIT 1000''', (kind + '%',)) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                queued += await self.enqueue('room', row['digest'], row['size'], route_name)
                if queued >= limit:
                    break
        else:
            async with self.mirror.db.execute('''SELECT c.object digest,MAX(c.size) size,c.url,MAX(e.category) category
                FROM cache c LEFT JOIN expected e ON e.url=c.url JOIN objects o ON o.digest=c.object
                WHERE o.local=1 GROUP BY c.object ORDER BY MAX(c.fetched) DESC LIMIT 1000''') as cursor:
                rows = await cursor.fetchall()
            from .mirror_policy import category_for
            for row in rows:
                if category_for(row['url'], row['category']) == route_name:
                    queued += await self.enqueue('mirror', row['digest'], row['size'], route_name)
                    if queued >= limit:
                        break
        return queued
