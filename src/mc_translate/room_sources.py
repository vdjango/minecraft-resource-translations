"""Background discovery of byte-identical public room files.

Requests on the room path use SQLite only. Publisher searches, fingerprinting
and small availability samples run here, independently of host uploads.
"""
import asyncio
import json
import logging
import random
import re
import time
import tomllib
import zipfile
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

from .federation import PublicResolver

STATES = dict(queued='等待批量匹配', checking='正在查询 CurseForge',
              waiting_file='等待首个实体文件以识别指纹', matched='已映射 CurseForge',
              slow='CurseForge 来源较慢，保留临时缓存',
              not_found='未找到相同文件，保留临时缓存', retry='查询暂不可用，稍后重试')
CDN_HOSTS = {'edge.forgecdn.net', 'mediafilez.forgecdn.net', 'media.forgecdn.net'}


def fingerprint(path):
    """MurmurHash2 seed 1, excluding CurseForge's four whitespace bytes."""
    length = 0
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            length += len(chunk.translate(None, b'\x09\x0a\x0d\x20'))
    mask, multiplier, tail = 0xffffffff, 0x5bd1e995, b''
    value = (1 ^ length) & mask
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            data = tail + chunk.translate(None, b'\x09\x0a\x0d\x20')
            end = len(data) & ~3
            for offset in range(0, end, 4):
                part = int.from_bytes(data[offset:offset + 4], 'little')
                part = part * multiplier & mask
                part ^= part >> 24
                part = part * multiplier & mask
                value = (value * multiplier & mask) ^ part
            tail = data[end:]
    for index, byte in enumerate(tail):
        value ^= byte << (index * 8)
    if tail:
        value = value * multiplier & mask
    value ^= value >> 13
    value = value * multiplier & mask
    return (value ^ (value >> 15)) & mask


def identities(path):
    """Read bounded archive metadata in place; never extract or run a JAR."""
    result = []
    try:
        with zipfile.ZipFile(path) as archive:
            for name in ('fabric.mod.json', 'quilt.mod.json', 'META-INF/mods.toml',
                         'META-INF/neoforge.mods.toml', 'mcmod.info'):
                try:
                    entry = archive.getinfo(name)
                    if entry.file_size > 256 * 1024:
                        continue
                    with archive.open(entry) as stream:
                        raw = stream.read(256 * 1024 + 1)
                    if len(raw) > 256 * 1024:
                        continue
                    data = (tomllib.loads(raw.decode('utf-8')) if name.endswith('.toml')
                            else json.loads(raw.decode('utf-8-sig')))
                    if name == 'fabric.mod.json':
                        candidates = [data]
                    elif name == 'quilt.mod.json':
                        loader = data.get('quilt_loader', {})
                        candidates = [dict(loader, name=loader.get('metadata', {}).get('name', ''))]
                    elif name.endswith('.toml'):
                        candidates = data.get('mods', [])
                    else:
                        candidates = data if isinstance(data, list) else data.get('modList', [])
                    for item in candidates[:8]:
                        if isinstance(item, dict):
                            result.extend(item.get(key) for key in ('name', 'displayName', 'id', 'modId', 'modid'))
                except (KeyError, ValueError, TypeError, AttributeError, RuntimeError, OSError):
                    continue
    except (zipfile.BadZipFile, OSError):
        pass
    return list(dict.fromkeys(value.strip()[:120] for value in result
                             if isinstance(value, str) and value.strip()))[:6]


def matching_file(file, sha1, size):
    """Names and Murmur fingerprints are hints; SHA-1 + length are mandatory."""
    if (type(size) is not int or not 0 < size <= 2*1024**3 or not re.fullmatch('[0-9a-f]{40}',sha1)
            or not isinstance(file, dict) or file.get('fileLength') != size
            or file.get('isAvailable') is False or file.get('isEarlyAccessContent')
            or file.get('isServerPack') or type(file.get('modId')) is not int
            or type(file.get('id')) is not int or min(file['modId'], file['id']) <= 0):
        return None
    hashes = file.get('hashes')
    if not isinstance(hashes, list) or not any(isinstance(h, dict) and h.get('algo') == 1
            and str(h.get('value', '')).lower() == sha1 for h in hashes):
        return None
    from .room_cache import public_url
    url = file.get('downloadUrl')
    if not public_url(url) or urlsplit(url).hostname not in CDN_HOSTS:
        return None
    return dict(url=url, project=file['modId'], file_id=file['id'], sha1=sha1, size=size)


class PublicFileDiscovery:
    def __init__(self, cache):
        self.cache = cache
        self.wake = asyncio.Event()
        self.active = set()
        self.worker = None
        self.rate_lock = asyncio.Lock()
        self.last_request = 0
        self.settle_cursor = ''

    def enabled(self):
        settings = self.cache.settings()
        return settings['enabled'] and settings['room_cache_enabled'] and settings['room_cache_public_lookup_enabled']

    async def open(self):
        await self.cache.db.executescript('''
            CREATE TABLE IF NOT EXISTS public_lookups(
                digest TEXT PRIMARY KEY,sha1 TEXT NOT NULL,size INTEGER NOT NULL,path TEXT NOT NULL,
                fingerprint INTEGER,hints TEXT NOT NULL DEFAULT '[]',state TEXT NOT NULL DEFAULT 'queued',
                checked REAL NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,
                created REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,
                project INTEGER,file_id INTEGER,url TEXT);
            CREATE INDEX IF NOT EXISTS public_lookup_due ON public_lookups(next_at,state);
            CREATE TABLE IF NOT EXISTS public_projects(
                id INTEGER PRIMARY KEY,checked REAL NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,
                touched REAL NOT NULL,aliases TEXT NOT NULL DEFAULT '[]');
        ''')
        await self.cache.db.execute("UPDATE public_lookups SET state='queued' WHERE state='checking'")
        await self.cache.db.execute('''INSERT OR IGNORE INTO public_lookups(digest,sha1,size,path,created)
            SELECT digest,sha1,size,path,? FROM files WHERE length(sha1)=40
            AND (lease_until>? OR (lease_until=0 AND touched>?)) GROUP BY digest''',
            (time.time(),time.time(),time.time()-self.cache.settings()['room_cache_ttl_hours']*3600))
        # Bind original publisher addresses only to server-verified uploads.
        # This lets ordinary /mods/... requests reuse the same CF mapping.
        await self.cache.db.execute('''INSERT OR IGNORE INTO catalog_files(url,sha256,sha1,size,state,speed_kibps,updated)
            SELECT j.value,f.digest,f.sha1,f.size,'slow',0,? FROM files f
            JOIN objects o ON o.digest=f.digest,json_each(f.urls) j WHERE length(f.sha1)=40
            AND j.value LIKE 'https://cdn.modrinth.com/%' ''',(time.time(),))
        await self.cache.db.commit()
        self.http = ClientSession(timeout=ClientTimeout(total=12, connect=4, sock_read=8),
            connector=TCPConnector(limit=4, resolver=PublicResolver()),
            headers={'User-Agent': 'Buggcraft-RoomSources/1 (+https://bugg.icu)'})
        self.worker = asyncio.create_task(self.run())
        self.wake.set()

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.http.close()

    async def register(self, files):
        """Called inside the room DB lock; no network or extra file reads."""
        now = time.time()
        await self.cache.db.executemany('''INSERT INTO public_lookups
            (digest,sha1,size,path,fingerprint,hints,created) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(digest) DO UPDATE SET fingerprint=COALESCE(public_lookups.fingerprint,excluded.fingerprint),
            state=CASE WHEN public_lookups.state='waiting_file' AND excluded.fingerprint IS NOT NULL
                THEN 'queued' ELSE public_lookups.state END''',
            [(file['sha256'], file['sha1'], file['size'], file['path'], file.get('fingerprint'),
              json.dumps(file.get('identities', [])), now) for file in files if file.get('sha1')])
        self.wake.set()

    async def uploaded(self, digest):
        async with self.cache.lock:
            await self.cache.db.execute('''INSERT OR IGNORE INTO public_lookups(digest,sha1,size,path,created)
                SELECT digest,sha1,size,path,? FROM files WHERE digest=? AND length(sha1)=40 LIMIT 1''',
                (time.time(),digest))
            await self.cache.db.execute("UPDATE public_lookups SET state='queued',next_at=0 WHERE digest=? AND state='waiting_file'", (digest,))
            await self.cache.db.commit()
        self.wake.set()

    async def request(self, method, path, **kwargs):
        config = self.cache.mirror.config
        headers = {'x-api-key': config.curseforge_key} if config.curseforge_key else {}
        async with self.rate_lock:
            await asyncio.sleep(max(0, self.last_request + .15 - time.monotonic()))
            self.last_request = time.monotonic()
        async with self.http.request(method, config.curseforge_url.rstrip('/') + path,
                headers=headers, allow_redirects=False, **kwargs) as response:
            if response.status != 200:
                raise ValueError('CurseForge 元数据 HTTP ' + str(response.status))
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 8 * 1024 ** 2:
                    raise ValueError('CurseForge 元数据超出单次限制')
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get('data'), (dict, list)):
                raise ValueError('CurseForge 元数据格式无效')
            return value['data']

    async def sample(self, match):
        # Forge's two public CDN hosts do not always have the same reachability.
        # Keep the exact file path, and record only the host that actually serves
        # this hash-bound file's expected size.
        url = urlsplit(match['url'])
        hosts = list(dict.fromkeys([url.hostname,'mediafilez.forgecdn.net','edge.forgecdn.net']))
        last = None
        for host in hosts:
            candidate = dict(match,url=url._replace(netloc=host).geturl())
            try:
                result = await self.sample_one(candidate)
                match['url'] = candidate['url']
                return result
            except (ClientError,asyncio.TimeoutError,ValueError,OSError) as exc:
                last = exc
        raise last

    async def sample_one(self, match):
        """Consult shared health; the staggered worker owns all active probes."""
        quality = self.cache.mirror.quality
        metric = quality.content_metric(match['url'])
        quality.targets_checked = 0
        quality.wake.set()
        if metric['health'] == 'unavailable':
            raise ValueError('CDN temporarily unavailable')
        speed = (metric['recent_speed_bps'] or 0) / 1024
        if metric['health'] == 'slow':
            return 'slow', speed, False
        return 'available', speed, metric['health'] == 'healthy' and speed > 0

    async def record(self, row, match):
        # Remember the verified metadata candidate even if the download sample
        # fails. A retry can check this exact file instead of searching again.
        async with self.cache.lock:
            await self.cache.db.execute('UPDATE public_lookups SET project=?,file_id=?,url=? WHERE digest=?',
                (match['project'],match['file_id'],match['url'],row['digest']))
            await self.cache.db.commit()
        state, speed, verified_fast = await self.sample(match)
        now = time.time()
        # Do not turn a host's unverified SHA-256 claim into a global mapping.
        async with self.cache.lock:
            await self.cache.db.execute('''INSERT INTO catalog_files VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET sha256='',sha1=excluded.sha1,size=excluded.size,
                state=excluded.state,speed_kibps=excluded.speed_kibps,updated=excluded.updated''',
                (match['url'],'',match['sha1'],match['size'],state,speed,now))
            await self.cache.db.execute('''UPDATE public_lookups SET state=?,checked=?,next_at=?,
                attempts=0,error=?,project=?,file_id=?,url=? WHERE digest=?''',
                ('matched' if state=='available' else 'slow',now,now+random.uniform(20,28)*3600,
                 None if verified_fast else '已登记地址；保留实体直到快速可达性确认',
                 match['project'],match['file_id'],match['url'],row['digest']))
            await self.cache.db.execute('''INSERT INTO public_projects(id,touched,aliases,next_at) VALUES(?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET touched=excluded.touched,aliases=excluded.aliases''',
                (match['project'],now,row['hints'],now+random.uniform(20,28)*3600))
            await self.cache.db.commit()

    async def search(self, row):
        """A few candidate names, then exact hashes, with a per-file deadline."""
        hints = json.loads(row['hints'])
        name = PurePosixPath(row['path']).stem
        name = re.sub(r'(?i)(?:neo)?forge|fabric|quilt|minecraft|(?:mc)?\d[\w.+-]*', ' ', name)
        hints += [' '.join(re.findall(r'[A-Za-z][A-Za-z_ ]{2,}', name)).replace('_',' ').strip()]
        terms = list(dict.fromkeys(term[:80] for term in hints if len(term.strip()) >= 3))[:2]
        kind = 6 if row['path'].startswith('mods/') else 12 if row['path'].startswith('resourcepacks/') else 6552
        for term in terms:
            projects = await self.request('GET','/mods/search',params=dict(gameId=432,classId=kind,
                                          searchFilter=term,pageSize=20,sortField=6,sortOrder='desc'))
            if not isinstance(projects, list):
                continue
            normalize = lambda value: re.sub('[^a-z0-9]','',str(value).lower())
            words = set(re.findall('[a-z]{3,}',term.lower()))
            ranked = sorted((p for p in projects if isinstance(p,dict) and p.get('gameId')==432),
                key=lambda p: (normalize(term) in (normalize(p.get('name')),normalize(p.get('slug'))),
                    len(words & set(re.findall('[a-z]{3,}',str(p.get('name','')).lower())))), reverse=True)
            for project in ranked:
                for file in project.get('latestFiles',[]) or []:
                    if match := matching_file(file,row['sha1'],row['size']):
                        return match
            for project in ranked[:2]:
                if type(project.get('id')) is not int or project['id'] <= 0:
                    continue
                versions = re.findall(r'(?<!\d)1\.\d{1,2}(?:\.\d{1,2})?(?!\d)',row['path'])[:1]
                for query in ([{'gameVersion':versions[0]},{}] if versions else [{}]):
                    files = await self.request('GET',f'/mods/{project["id"]}/files',
                                               params=dict(pageSize=50,index=0,**query))
                    for file in files if isinstance(files,list) else []:
                        if match := matching_file(file,row['sha1'],row['size']):
                            return match
        return None

    async def finish(self, row, state, error=None):
        now = time.time()
        attempts = row['attempts'] + 1 if state == 'retry' else 0
        delay = min(3600, 30 * 2**min(attempts,7)) if state=='retry' else random.uniform(20,28)*3600
        async with self.cache.lock:
            await self.cache.db.execute('UPDATE public_lookups SET state=?,checked=?,next_at=?,attempts=?,error=? WHERE digest=?',
                (state,now,now+delay,attempts,error,row['digest']))
            await self.cache.db.commit()

    async def check_one(self, row, match=None):
        try:
            async with asyncio.timeout(25):
                if not match and row['project'] and row['file_id']:
                    file = await self.request('GET',f'/mods/{row["project"]}/files/{row["file_id"]}')
                    match = matching_file(file,row['sha1'],row['size'])
                if not match:
                    match = await self.search(row)
                if match:
                    await self.record(row,match)
                else:
                    await self.finish(row,'not_found')
        except (ClientError, asyncio.TimeoutError, ValueError, OSError) as exc:
            # Host URLs and API credentials never appear in task errors.
            message = ('CurseForge 连接或读取超时，后台稍后重试' if isinstance(exc,asyncio.TimeoutError)
                       else str(exc) if isinstance(exc,ValueError) else 'CurseForge 连接失败：'+type(exc).__name__)
            if row['url']:
                async with self.cache.lock:
                    await self.cache.db.execute("UPDATE catalog_files SET state='unavailable',updated=? WHERE url=?",(time.time(),row['url']))
                    await self.cache.db.commit()
            await self.finish(row,'retry',message[:160])
        finally:
            self.active.discard(row['digest'])

    async def cycle(self):
        now = time.time()
        async with self.cache.db.execute('''SELECT p.* FROM public_lookups p WHERE p.next_at<=?
            AND p.state NOT IN ('checking','waiting_file') AND EXISTS(SELECT 1 FROM files f
            WHERE f.digest=p.digest AND (f.lease_until>? OR (f.lease_until=0 AND f.touched>?)))
            ORDER BY CASE WHEN p.fingerprint IS NOT NULL OR EXISTS(SELECT 1 FROM objects o
                WHERE o.digest=p.digest AND o.local=1) THEN 0 ELSE 1 END,p.checked,p.created LIMIT 12''',
            (now,now,now-self.cache.settings()['room_cache_ttl_hours']*3600)) as cursor:
            rows = [dict(row) for row in await cursor.fetchall()]
        if not rows:
            return False
        slots = asyncio.Semaphore(2)
        async def prepare(row):
            path = self.cache.path(row['digest'])
            # First, metadata-only search can finish while the host uploads.
            # When bytes arrive, fingerprint the already verified local object.
            if row['fingerprint'] is None and path.is_file():
                async with slots:
                    row['fingerprint'], hints = await asyncio.gather(asyncio.to_thread(fingerprint,path),
                                                                   asyncio.to_thread(identities,path))
                row['hints'] = json.dumps(list(dict.fromkeys(json.loads(row['hints'])+hints))[:6])
            async with self.cache.lock:
                await self.cache.db.execute("UPDATE public_lookups SET fingerprint=?,hints=?,state='checking' WHERE digest=?",
                    (row['fingerprint'],row['hints'],row['digest']))
                await self.cache.db.commit()
            self.active.add(row['digest'])
            return row
        prepared = await asyncio.gather(*(prepare(row) for row in rows))
        fingerprints = list({row['fingerprint'] for row in prepared if row['fingerprint'] is not None})
        matches = []
        if fingerprints:
            try:
                data = await self.request('POST','/fingerprints/432',json={'fingerprints':fingerprints})
                matches = data.get('exactMatches',[]) if isinstance(data,dict) else []
            except (ClientError,asyncio.TimeoutError,ValueError,OSError):
                pass  # Independent name search still works when this API is down.
        slots = asyncio.Semaphore(3)
        async def check(row):
            async with slots:
                match = next((found for candidate in matches if isinstance(candidate,dict)
                    and (found := matching_file(candidate.get('file'),row['sha1'],row['size']))),None)
                await self.check_one(row,match)
                if row['fingerprint'] is None and not self.cache.path(row['digest']).is_file():
                    async with self.cache.lock:
                        await self.cache.db.execute("UPDATE public_lookups SET state='waiting_file' WHERE digest=? AND state='not_found'",(row['digest'],))
                        await self.cache.db.commit()
                elif row['fingerprint'] is None and self.cache.path(row['digest']).is_file():
                    # Upload may have completed during the metadata search.
                    async with self.cache.lock:
                        await self.cache.db.execute("UPDATE public_lookups SET state='queued',next_at=0 WHERE digest=? AND state='not_found'",(row['digest'],))
                        await self.cache.db.commit()
        await asyncio.gather(*(check(row) for row in prepared))
        return True

    async def cloud_needed(self, digest):
        if not self.enabled():
            return True
        row = await self.cache.one('SELECT * FROM public_lookups WHERE digest=?',(digest,))
        if not row:
            return True
        failed = await self.cache.one('SELECT 1 FROM files WHERE digest=? AND failed=1 AND demanded>=? AND lease_until>? LIMIT 1',
                                     (digest,row['checked'],time.time()))
        if failed:
            return True
        if row['state']=='matched':
            # Matching bytes is not proof of current download quality. Honor
            # later threshold changes and retain bodies until a fast sample
            # has actually been confirmed; otherwise backups get skipped.
            source = await self.cache.one('''SELECT url,state,speed_kibps,updated FROM catalog_files
                WHERE url=? AND sha1=? AND size=?''', (row['url'],row['sha1'],row['size']))
            return not (source and self.cache.routing.evidence(source)['confirmed_fast'])
        obj = await self.cache.one('SELECT created FROM objects WHERE digest=?',(digest,))
        # Bound cloud staging delay even if metadata services are unavailable.
        return not (obj and obj['created'] > time.time()-60 and row['state'] in ('queued','checking','waiting_file'))

    async def settle(self):
        """Promote necessary bytes; discard a fast-mapped temporary body safely."""
        async with self.cache.db.execute('SELECT * FROM objects WHERE local=1 AND digest>? ORDER BY digest LIMIT 200',(self.settle_cursor,)) as cursor:
            objects = await cursor.fetchall()
        self.settle_cursor = objects[-1]['digest'] if len(objects)==200 else ''
        for obj in objects:
            digest = obj['digest']
            if digest in self.cache.uploading or digest in self.cache.serving or self.cache.mirror.cloud.uploading(digest):
                continue
            async with self.cache.db.execute('SELECT path FROM files WHERE digest=? AND lease_until>?',(digest,time.time())) as cursor:
                refs = await cursor.fetchall()
            if not refs:
                continue
            if await self.cloud_needed(digest):
                for route in {self.cache.route_name(ref['path']) for ref in refs}:
                    await self.cache.mirror.cloud.enqueue('room',digest,obj['size'],route)
            elif obj['created'] < time.time()-60:
                row = await self.cache.one('SELECT state,error FROM public_lookups WHERE digest=?',(digest,))
                if not row or row['state']!='matched':
                    continue
                async with self.cache.lock:
                    if digest in self.cache.uploading or digest in self.cache.serving or self.cache.mirror.cloud.uploading(digest):
                        continue
                    await self.cache.mirror.cloud.discard_room_jobs(digest)
                    await asyncio.to_thread(self.cache.path(digest).unlink,missing_ok=True)
                    # A signed cloud URL may already be in use. Keep that
                    # object until its room lease expires; never break an
                    # ongoing download to save a few remaining hours.
                    if await self.cache.mirror.cloud.location('room',digest):
                        await self.cache.db.execute('UPDATE objects SET local=0 WHERE digest=?',(digest,))
                    else:
                        await self.cache.db.execute('DELETE FROM objects WHERE digest=?',(digest,))
                    await self.cache.db.commit()

    async def refresh_project(self):
        """Index only a known project's recent versions, not the whole website."""
        row = await self.cache.one('SELECT * FROM public_projects WHERE next_at<? AND touched>? ORDER BY next_at LIMIT 1',
                                   (time.time(),time.time()-30*86400))
        if not row:
            return
        try:
            files = await self.request('GET',f'/mods/{row["id"]}/files',params=dict(pageSize=50,index=0))
            known = await self.cache.one("SELECT url FROM public_lookups WHERE project=? AND state='matched' ORDER BY checked DESC LIMIT 1",(row['id'],))
            host = urlsplit(known['url']).hostname if known and known['url'] else None
            entries = []
            for file in files if isinstance(files,list) else []:
                sha1 = next((str(h.get('value','')).lower() for h in (file.get('hashes') or [])
                             if isinstance(h,dict) and h.get('algo')==1),'') if isinstance(file,dict) else ''
                if re.fullmatch('[0-9a-f]{40}',sha1) and (match := matching_file(file,sha1,file.get('fileLength'))):
                    if host in CDN_HOSTS:
                        match['url']=urlsplit(match['url'])._replace(netloc=host).geturl()
                    entries.append((match['url'],'',sha1,match['size'],'available',0,time.time()))
            async with self.cache.lock:
                await self.cache.db.executemany('INSERT OR IGNORE INTO catalog_files VALUES(?,?,?,?,?,?,?)',entries)
                await self.cache.db.execute('UPDATE public_projects SET checked=?,next_at=? WHERE id=?',
                    (time.time(),time.time()+random.uniform(20,28)*3600,row['id']))
                await self.cache.db.commit()
        except (ClientError,asyncio.TimeoutError,ValueError,OSError):
            async with self.cache.lock:
                await self.cache.db.execute('UPDATE public_projects SET next_at=? WHERE id=?',(time.time()+3600,row['id']))
                await self.cache.db.commit()

    async def run(self):
        while True:
            try:
                # Keep the wait in this task: wait_for() can swallow a shutdown
                # cancellation when a ready Event completes at the same time
                # on Python 3.11.
                async with asyncio.timeout(5):
                    await self.wake.wait()
            except TimeoutError:
                pass
            self.wake.clear()
            try:
                if not self.enabled():
                    if self.cache.settings()['enabled'] and self.cache.settings()['room_cache_enabled']:
                        await self.settle()
                    continue
                more = await self.cycle()
                await self.settle()
                await self.refresh_project()
                if more:
                    self.wake.set()
                await asyncio.sleep(.5)
            except Exception:
                logging.exception('Public file discovery will retry; room downloads remain available')
                async with self.cache.lock:
                    await self.cache.db.execute("UPDATE public_lookups SET state='retry',next_at=?,error='后台查询稍后重试' WHERE state='checking'",(time.time()+60,))
                    await self.cache.db.commit()
                self.active.clear()

    async def snapshot(self):
        async with self.cache.db.execute('SELECT state,COUNT(*) count FROM public_lookups GROUP BY state') as cursor:
            counts = {row['state']:row['count'] for row in await cursor.fetchall()}
        async with self.cache.db.execute('''SELECT path,size,state,checked,next_at,error,url,project,file_id
            FROM public_lookups p WHERE EXISTS(SELECT 1 FROM files f WHERE f.digest=p.digest
            AND (f.lease_until>? OR (f.lease_until=0 AND f.touched>?)))
            ORDER BY checked DESC,created DESC LIMIT 20''',
            (time.time(),time.time()-self.cache.settings()['room_cache_ttl_hours']*3600)) as cursor:
            recent = [dict(row) for row in await cursor.fetchall()]
        return dict(enabled=self.enabled(),active=len(self.active),counts=counts,recent=recent,states=STATES,
                    batch_size=12,concurrency=3,refresh_hours='20–28',cloud_grace_seconds=60)
