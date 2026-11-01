"""Metadata-first acceleration: authorized providers, public file downloads."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import aiosqlite
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from .federation import PublicResolver
from .mirror_routing import requested_bytes

DEFAULTS = dict(room_cache_enabled=True, room_cache_ttl_hours=8,
                room_cache_quota_mib=10240, room_cache_max_file_mib=512,
                room_cache_slow_kibps=100, room_cache_probe_enabled=True,
                room_cache_probe_hours=6, room_cache_probe_daily_mib=32)
REASONS = dict(no_source='未找到可用的公开下载来源', slow='来源持续低于慢速阈值',
               failed='成员下载失败，等待房主补齐', unavailable='来源不可达',
               direct='使用公开来源，不占用缓存', cached='临时文件已就绪',
               capacity='临时缓存空间不足', waiting='等待房主提交文件信息',
               expired='此前缓存需求已到期，等待新的缺失文件需求')
HOSTS = {'edge.forgecdn.net', 'mediafilez.forgecdn.net', 'media.forgecdn.net',
         'cdn.modrinth.com', 'mod.mcimirror.top', 'mirror.minguncle.cn'}
DISCOURAGED = {'cdn.modrinth.com', 'mod.mcimirror.top'}


def validate_settings(settings):
    for key in ('room_cache_enabled', 'room_cache_probe_enabled'):
        if type(settings[key]) is not bool:
            raise ValueError('invalid ' + key)
    for key, low, high in [('room_cache_ttl_hours', 1, 32),
                           ('room_cache_quota_mib', 1, 1048576),
                           ('room_cache_max_file_mib', 1, 2048),
                           ('room_cache_slow_kibps', 1, 102400),
                           ('room_cache_probe_hours', 1, 168),
                           ('room_cache_probe_daily_mib', 1, 1024)]:
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError('invalid ' + key)


def keys():
    try:
        result = json.loads(os.environ.get('TRANSLATION_ROOM_CACHE_KEYS', '{}'))
    except ValueError:
        return {}
    if not isinstance(result, dict):
        return {}
    secret = os.environ.get('TRANSLATION_ROOM_CACHE_SECRET', '')
    if secret:
        result = {**result, 'default': secret}
    return {k: v for k, v in result.items() if isinstance(k, str) and isinstance(v, str) and len(v) >= 32}


def authorize(request, *, write=False):
    try:
        scheme, token = request.headers.get('Authorization', '').split(' ', 1)
        if scheme != 'Bearer' or len(token) > 2048:
            raise ValueError()
        body, signature = token.encode('ascii').split(b'.')
        raw = base64.b64decode(body + b'=' * (-len(body) % 4), altchars=b'-_', validate=True)
        claims = json.loads(raw)
        secret = keys().get(claims['iss'], '')
        expected = base64.urlsafe_b64encode(hmac.digest(secret.encode(), body, 'sha256')).rstrip(b'=')
        now = time.time()
        if (not secret or not hmac.compare_digest(signature, expected) or claims['v'] != 1
                or type(claims['iat']) is not int or type(claims['exp']) is not int
                or not now - 330 <= claims['iat'] <= now + 30
                or not now < claims['exp'] <= claims['iat'] + 300
                or not re.fullmatch('[0-9a-f]{32}', claims['room'])
                or not re.fullmatch('[0-9a-f]{64}', claims['config'])
                or type(claims['write']) is not bool or write and not claims['write']):
            raise ValueError()
        scope = hashlib.sha256((claims['iss'] + ':' + claims['room'] + ':' + claims['config']).encode()).hexdigest()
        return scope, claims
    except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
        raise web.HTTPUnauthorized(text='房间文件授权无效或已过期，请重新连接房间。') from None


def public_url(value):
    try:
        url = urlsplit(value)
        return (isinstance(value, str) and len(value) <= 2048 and url.scheme == 'https'
                and url.hostname in HOSTS and not url.username and not url.password
                and url.port in (None, 443) and not url.fragment and not url.query
                and not any(p in ('.', '..') for p in url.path.split('/'))
                and not any(c in value for c in ('\\', '\x00', '\r', '\n')))
    except (ValueError, TypeError):
        return False


def manifest(files, claims):
    if not isinstance(files, list) or len(files) > 3000:
        raise ValueError('文件清单过大或无效。')
    names, groups = set(), {'mods': [], 'resources': []}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError('文件信息无效。')
        path, digest, size = item.get('path'), item.get('sha256'), item.get('size')
        if (not isinstance(path, str) or len(path) > 260 or '\\' in path or ':' in path
                or any(not ch.isprintable() for ch in path) or len(PurePosixPath(path).parts) != 2
                or path.split('/')[0] not in ('mods', 'resourcepacks', 'shaderpacks')
                or path.rsplit('/', 1)[-1] in ('.', '..', '') or path.casefold() in names
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or type(size) is not int or not 0 <= size <= 2 * 1024 ** 3
                or path.startswith('mods/') and not path.lower().endswith('.jar')):
            raise ValueError('文件路径、大小或 SHA-256 无效。')
        sha1 = item.get('sha1', '')
        if sha1 and (not isinstance(sha1, str) or not re.fullmatch('[0-9a-f]{40}', sha1)):
            raise ValueError('SHA-1 无效。')
        urls = item.get('downloads', [])
        if not isinstance(urls, list) or len(urls) > 8 or any(not public_url(url) for url in urls):
            raise ValueError('下载来源不受支持。')
        names.add(path.casefold())
        groups['mods' if path.startswith('mods/') else 'resources'].append(digest)
    for kind, digests in groups.items():
        expected = claims.get(kind + '_hash', '')
        actual = hashlib.sha256('\n'.join(sorted(digests)).encode()).hexdigest()
        if claims.get(kind + '_count', 0) != len(digests) or expected and actual != expected:
            raise ValueError('文件清单与节点授权的房间配置不一致。')
    return files


class PinnedResponse(web.FileResponse):
    async def prepare(self, request):
        try:
            return await super().prepare(request)
        finally:
            self.cache.serving[self.digest] -= 1
            if not self.cache.serving[self.digest]:
                self.cache.serving.pop(self.digest)


class RoomCache:
    def __init__(self, mirror, settings):
        self.mirror, self.settings = mirror, settings
        self.root = mirror.root / 'room-content'
        self.lock = asyncio.Lock()
        self.uploading, self.serving = set(), {}
        self.upload_progress = {}
        self.reserved = 0

    async def open(self):
        (self.root / 'objects').mkdir(parents=True, exist_ok=True)
        (self.root / 'tmp').mkdir(exist_ok=True)
        for path in (self.root / 'tmp').glob('*.part'):
            path.unlink(missing_ok=True)
        self.db = await aiosqlite.connect(self.root / 'index.sqlite3')
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files(scope TEXT, digest TEXT, path TEXT, size INTEGER,
                sha1 TEXT, urls TEXT, failed INTEGER DEFAULT 0, need INTEGER DEFAULT 0,
                reason TEXT, touched REAL, lease_until REAL DEFAULT 0, demanded REAL DEFAULT 0,
                PRIMARY KEY(scope,digest));
            CREATE INDEX IF NOT EXISTS files_age ON files(touched);
            CREATE INDEX IF NOT EXISTS files_sha1 ON files(sha1);
            CREATE INDEX IF NOT EXISTS files_digest ON files(digest);
            CREATE TABLE IF NOT EXISTS objects(digest TEXT PRIMARY KEY, size INTEGER, created REAL,
                local INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sources(host TEXT PRIMARY KEY, url TEXT, checked REAL DEFAULT 0,
                speed REAL DEFAULT 0, bad INTEGER DEFAULT 0, samples INTEGER DEFAULT 0, touched REAL);
            CREATE TABLE IF NOT EXISTS budget(day INTEGER PRIMARY KEY, bytes INTEGER);
        ''')
        async with self.db.execute('PRAGMA table_info(files)') as cursor:
            columns = {row['name'] for row in await cursor.fetchall()}
        for column in ('lease_until', 'demanded'):
            if column not in columns:
                await self.db.execute(f'ALTER TABLE files ADD COLUMN {column} REAL DEFAULT 0')
        async with self.db.execute('PRAGMA table_info(objects)') as cursor:
            object_columns = {row['name'] for row in await cursor.fetchall()}
        if 'local' not in object_columns:
            await self.db.execute('ALTER TABLE objects ADD COLUMN local INTEGER NOT NULL DEFAULT 1')
        await self.db.commit()
        self.http = ClientSession(timeout=ClientTimeout(total=8, connect=3, sock_read=4),
            connector=TCPConnector(limit=1, resolver=PublicResolver()), auto_decompress=False,
            headers={'User-Agent': 'Buggcraft-RoomCache/1 (+https://bugg.icu)', 'Accept-Encoding': 'identity'})
        await self.cleanup()
        self.worker = asyncio.create_task(self.maintain())

    async def close(self):
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        await self.http.close()
        await self.db.close()

    async def one(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            return await cursor.fetchone()

    def enabled(self):
        if not self.settings()['enabled'] or not self.settings()['room_cache_enabled']:
            raise web.HTTPServiceUnavailable(text='节点文件兜底暂未启用。')

    def path(self, digest):
        return self.root / 'objects' / digest

    async def cached(self, digest):
        row = await self.one('SELECT * FROM objects WHERE digest=?', (digest,))
        live = await self.one('SELECT 1 FROM files WHERE digest=? AND lease_until>? LIMIT 1', (digest, time.time()))
        if (row and live and (self.path(digest).is_file()
                             or await self.mirror.cloud.download_url('room', digest))):
            return row

    async def lease(self, row, action, reason):
        if action not in ('upload', 'cached'):
            return action, reason
        now = time.time()
        if not row['lease_until'] or row['lease_until'] <= now and row['demanded'] > row['lease_until']:
            await self.db.execute('UPDATE files SET lease_until=? WHERE scope=? AND digest=?',
                (now + self.settings()['room_cache_ttl_hours'] * 3600, row['scope'], row['digest']))
        elif row['lease_until'] <= now and action == 'upload':
            return 'pending', 'expired'
        return action, reason

    async def decision(self, row):
        if await self.cached(row['digest']):
            return 'cached', 'cached', []
        urls = [url for url in json.loads(row['urls']) if urlsplit(url).hostname not in DISCOURAGED]
        if row['failed']:
            return 'upload', 'failed', urls
        if not urls:
            return 'upload', 'no_source', []
        fast = []
        for url in urls:
            sample = await self.one('SELECT * FROM sources WHERE host=?', (urlsplit(url).hostname,))
            if (not sample or sample['checked'] < time.time() - self.settings()['room_cache_probe_hours'] * 7200
                    or sample['bad'] < 2):
                fast.append(url)
        return ('redirect', 'direct', fast) if fast else ('upload', 'slow', urls)

    async def plan(self, request):
        self.enabled()
        scope, claims = authorize(request, write=True)
        try:
            data = await request.clone(client_max_size=2 * 1024 ** 2).json()
            if not isinstance(data, dict) or set(data) - {'files', 'failed'}:
                raise ValueError('下载计划无效。')
            failed = data.get('failed', [])
            if not isinstance(failed, list) or len(failed) > 3000 or any(not isinstance(d, str) or not re.fullmatch('[0-9a-f]{64}', d) for d in failed):
                raise ValueError('失败文件清单无效。')
            files = None
            if 'files' in data:
                if not claims['write']:
                    raise web.HTTPForbidden(text='只有房主可以提交文件清单。')
                files = manifest(data['files'], claims)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='文件信息或房间配置校验失败。') from None
        async with self.lock:
            if files is not None:
                for file in files:
                    previous = await self.one('SELECT size,sha1 FROM files WHERE digest=? LIMIT 1', (file['sha256'],))
                    if previous and (previous['size'] != file['size'] or previous['sha1'] and file.get('sha1') and previous['sha1'] != file['sha1']):
                        raise web.HTTPBadRequest(text='同一文件哈希的大小或校验信息不一致。')
                for file in files:
                    await self.db.execute('''INSERT INTO files(scope,digest,path,size,sha1,urls,touched)
                        VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,digest) DO UPDATE SET
                        urls=excluded.urls,touched=excluded.touched''',
                        (scope, file['sha256'], file['path'], file['size'], file.get('sha1', ''),
                         json.dumps(file.get('downloads', [])), time.time()))
                    for url in file.get('downloads', []):
                        host = urlsplit(url).hostname
                        if host not in DISCOURAGED:
                            await self.db.execute('''INSERT INTO sources(host,url,touched) VALUES(?,?,?)
                                ON CONFLICT(host) DO UPDATE SET url=excluded.url,touched=excluded.touched''', (host, url, time.time()))
            for digest in failed:
                await self.db.execute('UPDATE files SET failed=1,demanded=? WHERE scope=? AND digest=?', (time.time(), scope, digest))
            async with self.db.execute('SELECT * FROM files WHERE scope=? ORDER BY path', (scope,)) as cursor:
                rows = await cursor.fetchall()
            result, requested = [], []
            for row in rows:
                action, reason, urls = await self.decision(row)
                action, reason = await self.lease(row, action, reason)
                if action == 'upload' and row['size'] > self.settings()['room_cache_max_file_mib'] * 1024 ** 2:
                    action, reason = 'pending', 'capacity'
                await self.db.execute('UPDATE files SET need=?,reason=? WHERE scope=? AND digest=?',
                                      (int(action == 'upload'), reason, scope, row['digest']))
                item = dict(sha256=row['digest'], size=row['size'], path=row['path'],
                            action=action, reason=REASONS[reason], downloads=urls,
                            url='/room-cache/files/' + row['digest'])
                result.append(item)
                if action == 'upload' and claims['write']:
                    requested.append(item)
            await self.db.commit()
        return web.json_response(dict(protocol='room-content-cache-v1', files=result, upload=requested,
            retry_after=5, state='ready' if rows else 'waiting', ttl_hours=self.settings()['room_cache_ttl_hours']),
            headers={'Cache-Control': 'no-store'})

    async def resolve(self, request):
        """Public hash lookup; cannot register paths, URLs or upload bodies."""
        self.enabled()
        try:
            data = await request.clone(client_max_size=256 * 1024).json()
            if not isinstance(data, dict) or set(data) - {'wanted', 'failed'}:
                raise ValueError()
            wanted, failed = data.get('wanted', []), data.get('failed', [])
            if (not isinstance(wanted, list) or not isinstance(failed, list) or len(wanted) > 3000
                    or len(failed) > 3000 or any(not isinstance(d, str) or not re.fullmatch('(?:[0-9a-f]{40}|[0-9a-f]{64})', d) for d in [*wanted, *failed])
                    or not set(failed) <= set(wanted)):
                raise ValueError()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='文件哈希清单无效。') from None
        result = []
        async with self.lock:
            for wanted_hash in dict.fromkeys(wanted):
                column = 'sha1' if len(wanted_hash) == 40 else 'digest'
                row = await self.one(f'SELECT * FROM files WHERE {column}=? ORDER BY touched DESC LIMIT 1', (wanted_hash,))
                if not row:
                    continue
                digest = row['digest']
                if wanted_hash in failed and not await self.cached(digest):
                    await self.db.execute('UPDATE files SET failed=1,demanded=? WHERE digest=?', (time.time(), digest))
                    row = await self.one('SELECT * FROM files WHERE scope=? AND digest=?', (row['scope'], digest))
                action, reason, urls = await self.decision(row)
                result.append(dict(sha256=digest, sha1=row['sha1'], size=row['size'], action=action, reason=REASONS[reason],
                    downloads=urls, url='/room-cache/files/' + digest))
            await self.db.commit()
        return web.json_response(dict(protocol='room-content-cache-v1', files=result, state='ready' if result else 'waiting', retry_after=5),
                                 headers={'Cache-Control': 'no-store'})

    async def upload(self, request):
        self.enabled()
        scope, _ = authorize(request, write=True)
        digest = request.match_info['digest']
        row = await self.one('SELECT * FROM files WHERE scope=? AND digest=?', (scope, digest))
        if not row or not row['need'] or row['lease_until'] <= time.time():
            raise web.HTTPForbidden(text='源站没有请求此文件，请先提交文件信息。')
        size = row['size']
        if request.content_length != size or size > self.settings()['room_cache_max_file_mib'] * 1024 ** 2:
            raise web.HTTPRequestEntityTooLarge(max_size=size, actual_size=request.content_length or 0)
        async with self.lock:
            if await self.cached(digest):
                return web.json_response(dict(state='cached'), headers={'Cache-Control': 'no-store'})
            if digest in self.uploading or len(self.uploading) >= 2:
                raise web.HTTPTooManyRequests(text='文件补传正在进行，请稍后继续。', headers={'Retry-After': '5'})
            used = (await self.one('SELECT COALESCE(SUM(size),0) n FROM objects WHERE local=1'))['n']
            free = (await asyncio.to_thread(shutil.disk_usage, self.root)).free
            if (used + self.reserved + size > self.settings()['room_cache_quota_mib'] * 1024 ** 2
                    or free - self.reserved - size < self.settings()['reserve_gib'] * 1024 ** 3):
                raise web.HTTPInsufficientStorage(text='客户端临时缓存容量不足，已保留其他缓存和正在下载的文件。')
            self.uploading.add(digest)
            self.reserved += size
        temp = self.root / 'tmp' / (secrets.token_hex(12) + '.part')
        self.upload_progress[digest] = 0
        try:
            count, sha256, sha1 = 0, hashlib.sha256(), hashlib.sha1()
            with temp.open('xb') as output:
                async for chunk in request.content.iter_chunked(128 * 1024):
                    count += len(chunk)
                    self.upload_progress[digest] = count
                    if count > size:
                        raise web.HTTPBadRequest(text='文件大小不匹配。')
                    sha256.update(chunk)
                    sha1.update(chunk)
                    await asyncio.to_thread(output.write, chunk)
            if count != size or sha256.hexdigest() != digest or row['sha1'] and sha1.hexdigest() != row['sha1']:
                raise web.HTTPUnprocessableEntity(text='文件哈希校验失败，请房主重新核对本地文件。')
            async with self.lock:
                # Atomic publish; no partial response can ever be downloaded.
                await asyncio.to_thread(os.replace, temp, self.path(digest))
                await self.db.execute('INSERT OR REPLACE INTO objects(digest,size,created,local) VALUES(?,?,?,1)',
                                      (digest, size, time.time()))
                await self.db.commit()
            route = ('room_mods' if row['path'].startswith('mods/') else
                     'room_resourcepacks' if row['path'].startswith('resourcepacks/') else 'room_shaderpacks')
            try:
                await self.mirror.cloud.enqueue('room', digest, size, route)
            except Exception:
                logging.exception('Room cloud copy could not be queued; local file remains available')
            return web.json_response(dict(state='cached'), headers={'Cache-Control': 'no-store'})
        finally:
            temp.unlink(missing_ok=True)
            self.uploading.discard(digest)
            self.upload_progress.pop(digest, None)
            self.reserved -= size

    async def download(self, request):
        self.enabled()
        digest = request.match_info['digest']
        async with self.lock:
            row = await self.one('SELECT * FROM files WHERE digest=? ORDER BY touched DESC LIMIT 1', (digest,))
            if not row:
                raise web.HTTPNotFound(text='尚未收录此文件信息。')
            cached = await self.cached(digest)
            if cached:
                route = ('room_mods' if row['path'].startswith('mods/') else
                         'room_resourcepacks' if row['path'].startswith('resourcepacks/') else 'room_shaderpacks')
                lease = await self.one('SELECT MAX(lease_until) expires FROM files WHERE digest=?', (digest,))
                cloud_url = await self.mirror.cloud.download_url('room', digest,
                    expires_seconds=max(1, int(lease['expires'] - time.time())))
                if cloud_url and (self.settings()['qiniu_routes'][route]['mode'] == 'cloud'
                                  or not self.path(digest).is_file()):
                    return web.Response(status=307, headers={'Location': cloud_url,
                        'Cache-Control': 'no-store', 'Cloudflare-CDN-Cache-Control': 'no-store'})
                if not self.path(digest).is_file():
                    cached = None
            if cached:
                async with self.mirror.space_lock:
                    blocked = await self.mirror.egress.admit(requested_bytes(request, cached['size']),
                        self.settings(), sum(self.serving.values()) + sum(self.mirror.serving.values()))
                if blocked:
                    raise web.HTTPServiceUnavailable(text='源站临时达到下载并发或流量上限，请稍后重试。', headers={'Retry-After': '5'})
                self.serving[digest] = self.serving.get(digest, 0) + 1
                response = PinnedResponse(self.path(digest), headers={'Cache-Control': 'no-store'})
                response.cache, response.digest = self, digest
                return response
            action, reason, urls = await self.decision(row)
        if action == 'redirect':
            raise web.HTTPTemporaryRedirect(location=urls[0], headers={'Cache-Control': 'no-store'})
        raise web.HTTPServiceUnavailable(text=REASONS[reason] + '；正在等待房主补传。', headers={'Retry-After': '5'})

    async def cleanup(self):
        async with self.lock:
            async with self.db.execute('SELECT digest FROM objects WHERE NOT EXISTS (SELECT 1 FROM files WHERE files.digest=objects.digest AND lease_until>?)', (time.time(),)) as cursor:
                for row in await cursor.fetchall():
                    digest = row['digest']
                    if digest not in self.serving and digest not in self.uploading and self.mirror.cloud.active_digest != digest:
                        try:
                            await self.mirror.cloud.delete_room(digest)
                            await self.mirror.cloud.discard_room_jobs(digest)
                        except (OSError, RuntimeError):
                            continue
                        self.path(digest).unlink(missing_ok=True)
                        await self.db.execute('DELETE FROM objects WHERE digest=?', (digest,))
            await self.db.execute('DELETE FROM files WHERE touched<? AND lease_until<=?', (time.time() - 32 * 3600, time.time()))
            # Old failure evidence must not force permanent caching. A fresh
            # failed download can request another lease without exposing TTLs.
            await self.db.execute('UPDATE files SET failed=0 WHERE lease_until>0 AND lease_until<=? AND demanded<=lease_until', (time.time(),))
            await self.db.execute('DELETE FROM sources WHERE touched<?', (time.time() - 32 * 3600,))
            await self.db.execute('DELETE FROM budget WHERE day<?', (int(time.time() // 86400) - 2,))
            await self.db.commit()

    async def probe(self):
        settings = self.settings()
        if not settings['room_cache_probe_enabled'] or not settings['room_cache_enabled'] or not keys():
            return
        row = await self.one('SELECT * FROM sources WHERE checked<? ORDER BY checked LIMIT 1',
                             (time.time() - settings['room_cache_probe_hours'] * 3600,))
        if not row:
            return
        day, maximum = int(time.time() // 86400), 128 * 1024
        spent = await self.one('SELECT bytes FROM budget WHERE day=?', (day,))
        if spent and spent['bytes'] + maximum > settings['room_cache_probe_daily_mib'] * 1024 ** 2:
            return
        start, count, success, missing = time.monotonic(), 0, False, False
        try:
            url = row['url']
            for _ in range(4):
                if not public_url(url):
                    break
                async with self.http.get(url, allow_redirects=False, headers={'Range': 'bytes=0-131071'}) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        url = response.headers.get('Location', '')
                        continue
                    if response.status not in (200, 206):
                        missing = response.status in (404, 410)
                        break
                    async for chunk in response.content.iter_chunked(16384):
                        count += min(len(chunk), maximum - count)
                        if count >= maximum:
                            break
                    success = count > 0
                    break
        except (ClientError, asyncio.TimeoutError, OSError, ValueError):
            pass
        elapsed = max(time.monotonic() - start, .001)
        speed = count / elapsed / 1024
        bad = not success or speed < settings['room_cache_slow_kibps']
        async with self.lock:
            await self.db.execute('UPDATE sources SET checked=?,speed=?,bad=?,samples=samples+1 WHERE host=?',
                                  (time.time(), speed, row['bad'] if missing else min(row['bad'] + 1, 100) if bad else 0, row['host']))
            await self.db.execute('INSERT INTO budget VALUES(?,?) ON CONFLICT(day) DO UPDATE SET bytes=bytes+excluded.bytes', (day, count))
            await self.db.commit()

    async def maintain(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.cleanup()
                await self.probe()
            except (OSError, aiosqlite.Error):
                # A sampling failure never blocks room traffic or stops cleanup.
                continue

    async def snapshot(self):
        totals = await self.one('SELECT COUNT(*) files,COALESCE(SUM(size*local),0) bytes FROM objects')
        async with self.db.execute('SELECT path,size,reason,touched FROM files ORDER BY touched DESC LIMIT 50') as cursor:
            recent = [dict(row) for row in await cursor.fetchall()]
        async with self.db.execute('SELECT host,speed,bad,samples,checked FROM sources ORDER BY host') as cursor:
            sources = [dict(row) for row in await cursor.fetchall()]
        return dict(configured=bool(keys()), **dict(totals), uploads=len(self.uploading),
                    downloads=sum(self.serving.values()), recent=recent, sources=sources, reasons=REASONS)
