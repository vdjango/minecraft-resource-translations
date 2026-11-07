"""Metadata-first acceleration: authorized providers, public file downloads."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
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
from .content_routing import ContentRouting
from .file_acceleration import public_index, download_mirror_copy

DEFAULTS = dict(room_cache_enabled=True, room_cache_ttl_hours=8,
                room_cache_public_lookup_enabled=True,
                room_cache_quota_mib=10240, room_cache_staging_mib=1024, room_cache_max_file_mib=512,
                room_cache_slow_kibps=128, room_cache_probe_enabled=False,
                room_cache_probe_hours=6, room_cache_probe_daily_mib=32)
REASONS = dict(no_source='未找到可用的公开下载来源', slow='来源持续低于慢速阈值',
               failed='成员下载失败，等待房主补齐', unavailable='来源不可达',
               direct='使用公开来源，不占用缓存', cached='临时文件已就绪',
               mirror_cached='镜像站已有相同 SHA-256 文件，无需房主上传',
               capacity='临时缓存空间不足', local_disabled='此类别使用本地存储，但本地房间容量为 0；请改用云空间或调整本地容量', waiting='等待房主提交文件信息',
               expired='此前缓存需求已到期，等待新的缺失文件需求')
HOSTS = {'edge.forgecdn.net', 'mediafilez.forgecdn.net', 'media.forgecdn.net',
         'cdn.modrinth.com', 'mod.mcimirror.top', 'mirror.minguncle.cn'}
DISCOURAGED = {'cdn.modrinth.com', 'mod.mcimirror.top'}


def validate_settings(settings):
    for key in ('room_cache_enabled', 'room_cache_probe_enabled', 'room_cache_public_lookup_enabled'):
        if type(settings[key]) is not bool:
            raise ValueError('invalid ' + key)
    for key, low, high in [('room_cache_ttl_hours', 1, 32),
                           ('room_cache_quota_mib', 0, 1048576),
                           ('room_cache_staging_mib', 1, 1048576),
                           ('room_cache_max_file_mib', 1, 2048),
                           ('room_cache_slow_kibps', 1, 102400),
                           ('room_cache_probe_hours', 1, 168),
                           ('room_cache_probe_daily_mib', 1, 1024)]:
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError('invalid ' + key)


def keys(root=None):
    try:
        result = json.loads(os.environ.get('TRANSLATION_ROOM_CACHE_KEYS', '{}'))
    except ValueError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    secret = os.environ.get('TRANSLATION_ROOM_CACHE_SECRET', '')
    if secret:
        result = {**result, 'default': secret}
    result = {k: v for k, v in result.items() if isinstance(k, str) and isinstance(v, str) and len(v) >= 32}
    if root is not None:
        from .room_node_access import NodeAccess
        result = NodeAccess(root).apply(result)
    return result


def authorize(request, *, write=False):
    try:
        scheme, token = request.headers.get('Authorization', '').split(' ', 1)
        if scheme != 'Bearer' or len(token) > 2048:
            raise ValueError()
        body, signature = token.encode('ascii').split(b'.')
        raw = base64.b64decode(body + b'=' * (-len(body) % 4), altchars=b'-_', validate=True)
        claims = json.loads(raw)
        mirror = getattr(request, 'app', {}).get('mirror')
        secret = keys(mirror.root if mirror else None).get(claims['iss'], '')
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
        fingerprint = item.get('fingerprint')
        hints = item.get('identities', [])
        if (fingerprint is not None and (type(fingerprint) is not int or not 0 <= fingerprint <= 0xffffffff)
                or not isinstance(hints, list) or len(hints) > 6
                or any(not isinstance(value, str) or len(value) > 120 or not value.isprintable() for value in hints)):
            raise ValueError('公开来源识别信息无效。')
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
            if getattr(self, 'mirror_copy', False):
                self.cache.mirror.serving[self.digest] -= 1
                if not self.cache.mirror.serving[self.digest]:
                    self.cache.mirror.serving.pop(self.digest)


class RoomCache:
    def __init__(self, mirror, settings):
        self.mirror, self.settings = mirror, settings
        self.root = mirror.root / 'room-content'
        self.lock = asyncio.Lock()
        self.uploading, self.serving = set(), {}
        self.upload_progress = {}
        self.cleanup_task = None
        self.reserved = 0
        self.reserved_by_kind = dict(local=0, staging=0)
        from .room_sources import PublicFileDiscovery
        self.discovery = PublicFileDiscovery(self)
        self.routing = ContentRouting(self)

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
            CREATE TABLE IF NOT EXISTS catalog_files(
                url TEXT PRIMARY KEY, sha256 TEXT NOT NULL DEFAULT '', sha1 TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL, state TEXT NOT NULL, speed_kibps REAL NOT NULL DEFAULT 0,
                updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS catalog_sha256 ON catalog_files(sha256,size);
            CREATE INDEX IF NOT EXISTS catalog_sha1 ON catalog_files(sha1,size);
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
        await self.discovery.open()
        self.worker = asyncio.create_task(self.maintain())

    async def close(self):
        await self.discovery.close()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            await asyncio.gather(self.cleanup_task, return_exceptions=True)
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

    @staticmethod
    def route_name(path):
        return ('room_mods' if path.startswith('mods/') else
                'room_resourcepacks' if path.startswith('resourcepacks/') else 'room_shaderpacks')

    def local_reference_sql(self):
        clauses = ["f.path NOT LIKE 'mods/%' AND f.path NOT LIKE 'resourcepacks/%' AND f.path NOT LIKE 'shaderpacks/%'"]
        for prefix, route in [('mods', 'room_mods'), ('resourcepacks', 'room_resourcepacks'), ('shaderpacks', 'room_shaderpacks')]:
            if self.mirror.cloud.route(route)['mode'] != 'cloud':
                clauses.append("f.path LIKE '" + prefix + "/%'")
        return '(' + ' OR '.join('(' + clause + ')' for clause in clauses) + ')'

    async def needs_local(self, digest):
        return bool(await self.one('SELECT 1 FROM files f WHERE f.digest=? AND f.lease_until>? AND ' +
                                   self.local_reference_sql() + ' LIMIT 1', (digest, time.time())))

    async def storage_usage(self):
        # Classify from current routes rather than a stale persisted flag: route
        # changes and shared hashes must immediately have the same quota meaning.
        row = await self.one('''SELECT COALESCE(SUM(o.size),0) bytes,
            COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM files f WHERE f.digest=o.digest
            AND f.lease_until>? AND ''' + self.local_reference_sql() + ''') THEN o.size ELSE 0 END),0) local_bytes
            FROM objects o WHERE o.local=1''', (time.time(),))
        return dict(local_bytes=row['local_bytes'], staging_bytes=row['bytes']-row['local_bytes'])

    async def cached(self, digest):
        row = await self.one('SELECT * FROM objects WHERE digest=?', (digest,))
        live = await self.one('SELECT 1 FROM files WHERE digest=? AND lease_until>? LIMIT 1', (digest, time.time()))
        if (row and live and (self.path(digest).is_file()
                             or await self.mirror.cloud.download_url('room', digest))):
            return dict(row)
        # Reuse the verified SHA-256 object index of the ordinary mirror.
        # This is a read-only reference: no extra upload or second disk copy.
        mirror_row = await self.mirror.one('SELECT size FROM objects WHERE digest=?', (digest,))
        mirror_path = self.mirror.root / 'objects' / digest[:2] / digest
        if live and mirror_row and (mirror_path.is_file() or await self.mirror.cloud.download_url('mirror', digest)):
            return dict(mirror_row, mirror_copy=True)

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

    async def decision(self, row, *, existing=None, catalog=None):
        urls = [url for url in json.loads(row['urls']) if urlsplit(url).hostname not in DISCOURAGED]
        # Decide from the maintained, hash-matched catalog. A room submission
        # never triggers a server-side fetch or probe of its supplied URLs.
        if catalog is None:
            async with self.db.execute('''SELECT url,state,speed_kibps,updated FROM catalog_files
                    WHERE size=? AND (sha256=? OR (?<>'' AND sha1=?))''',
                    (row['size'], row['digest'], row['sha1'], row['sha1'])) as cursor:
                known = await cursor.fetchall()
            public, _ = await public_index(self.mirror, [row['sha1']] if row['sha1'] else [])
            known = [*known, *(item for item in public.get(row['sha1'], []) if item['size'] == row['size'])]
        else:
            known = catalog
        known = [item for item in known if not row['failed'] or item['updated'] > row['demanded']]
        ranked = self.routing.ranked(known)
        fast = [item['url'] for item in ranked if item['confirmed_fast']
                and urlsplit(item['url']).hostname not in DISCOURAGED]
        if fast:
            return 'redirect', 'direct', fast
        if existing is None:
            existing = await self.cached(row['digest'])
        if existing and existing['size'] == row['size']:
            return 'cached', 'mirror_cached' if existing.get('mirror_copy') else 'cached', []
        if row['failed']:
            return 'upload', 'failed', urls
        unmeasured = [item['url'] for item in ranked if urlsplit(item['url']).hostname not in DISCOURAGED]
        if unmeasured:
            return 'redirect', 'direct', unmeasured
        secondary = [item['url'] for item in ranked if item['confirmed_fast']]
        if secondary:
            return 'redirect', 'direct', secondary
        if known:
            # A newly supplied hash-bound publisher URL can be tried before
            # uploading when the old indexed URL is known to be slow.
            fresh = [url for url in urls if url not in {item['url'] for item in known}
                     and self.mirror.quality.content_metric(url)['health'] not in ('slow', 'unavailable')]
            if fresh:
                return 'redirect', 'direct', fresh
            reason = 'slow' if any(item['state'] == 'slow' or item['speed_kibps'] for item in known) else 'unavailable'
            return 'upload', reason, urls
        if not urls:
            return 'upload', 'no_source', []
        urls = [url for url in urls if self.mirror.quality.content_metric(url)['health'] not in ('slow', 'unavailable')]
        if not urls:
            return 'upload', 'slow', []
        # Unknown files use the current direct-first path; a real client
        # failure requests only that file from the room owner.
        return 'redirect', 'direct', urls

    async def planning_index(self, rows):
        """Resolve an entire room manifest from local indexes in bounded SQL batches."""
        catalog, existing = {}, {}
        for start in range(0, len(rows), 300):
            batch = rows[start:start + 300]
            digests = [row['digest'] for row in batch]
            sha1s = list({row['sha1'] for row in batch if row['sha1']})
            by_sha256 = {row['digest']: row for row in batch}
            by_sha1 = {row['sha1']: row for row in batch if row['sha1']}
            placeholders = ','.join('?' for _ in digests)
            clauses = ['sha256 IN (' + placeholders + ')']
            args = list(digests)
            if sha1s:
                clauses.append('sha1 IN (' + ','.join('?' for _ in sha1s) + ')')
                args.extend(sha1s)
            async with self.db.execute('SELECT url,state,speed_kibps,updated,sha256,sha1,size FROM catalog_files WHERE ' + ' OR '.join(clauses), args) as cursor:
                for source in await cursor.fetchall():
                    match = by_sha256.get(source['sha256']) or by_sha1.get(source['sha1'])
                    if match and source['size'] == match['size']:
                        catalog.setdefault(match['digest'], []).append(source)
            async with self.db.execute('SELECT digest,size FROM objects WHERE digest IN (' + placeholders + ')', digests) as cursor:
                room_objects = {row['digest']: row for row in await cursor.fetchall()}
            async with self.db.execute('SELECT DISTINCT digest FROM files WHERE lease_until>? AND digest IN (' + placeholders + ')', [time.time(), *digests]) as cursor:
                room_live = {row['digest'] for row in await cursor.fetchall()}
            async with self.mirror.db.execute('SELECT digest,size FROM objects WHERE digest IN (' + placeholders + ')', digests) as cursor:
                mirror_objects = {row['digest']: row for row in await cursor.fetchall()}
            async with self.mirror.db.execute("SELECT scope,digest FROM object_locations WHERE state='ready' AND digest IN (" + placeholders + ')', digests) as cursor:
                cloud_copies = {(row['scope'], row['digest']) for row in await cursor.fetchall()}
            for row in batch:
                digest = row['digest']
                own = room_objects.get(digest)
                if own and digest in room_live and own['size'] == row['size'] and (self.path(digest).is_file() or await self.mirror.cloud.download_url('room', digest)):
                    existing[digest] = dict(own, cloud_ready=('room', digest) in cloud_copies)
                    continue
                other = mirror_objects.get(digest)
                path = self.mirror.root / 'objects' / digest[:2] / digest
                if other and other['size'] == row['size'] and (path.is_file() or await self.mirror.cloud.download_url('mirror', digest)):
                    existing[digest] = dict(other, mirror_copy=True, cloud_ready=('mirror', digest) in cloud_copies)
        public, _ = await public_index(self.mirror, list({row['sha1'] for row in rows if row['sha1']}))
        for row in rows:
            catalog.setdefault(row['digest'], []).extend(item for item in public.get(row['sha1'], [])
                                                         if item['size'] == row['size'])
        return catalog, existing

    async def import_catalog(self, data):
        """Import operator-owned file evidence without downloading files."""
        items, mode = data.get('files'), data.get('mode', 'merge')
        if mode not in ('merge', 'replace') or not isinstance(items, list) or not items or len(items) > 3000:
            raise ValueError('文件目录格式无效，单次最多导入 3000 项。')
        rows = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('文件目录项目格式无效。')
            url, sha256, sha1 = item.get('url'), item.get('sha256', ''), item.get('sha1', '')
            size, state, speed = item.get('size'), item.get('state'), item.get('speed_kibps', 0)
            if (not public_url(url) or not (sha256 or sha1)
                    or sha256 and not re.fullmatch('[0-9a-f]{64}', sha256)
                    or sha1 and not re.fullmatch('[0-9a-f]{40}', sha1)
                    or type(size) is not int or not 0 <= size <= 2 * 1024 ** 3
                    or state not in ('available', 'slow', 'unavailable')
                    or type(speed) not in (int, float) or not math.isfinite(speed)
                    or not 0 <= speed <= 1048576):
                raise ValueError('目录需包含可信 HTTPS 地址、文件哈希、大小和状态。')
            rows.append((url, sha256, sha1, size, state, float(speed), time.time()))
        async with self.lock:
            if mode == 'replace':
                await self.db.execute('DELETE FROM catalog_files')
            await self.db.executemany('''INSERT INTO catalog_files VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET sha256=excluded.sha256,sha1=excluded.sha1,
                size=excluded.size,state=excluded.state,speed_kibps=excluded.speed_kibps,
                updated=excluded.updated''', rows)
            await self.db.commit()
        return dict(imported=len(rows), mode=mode)

    async def fast_public_source(self, url, *, preferred_only=False):
        """Choose a hash-matched fast URL from operator-imported evidence."""
        async with self.db.execute('''SELECT sha256,sha1,size FROM catalog_files
                WHERE url=?''', (url,)) as cursor:
            known = await cursor.fetchone()
        if not known:
            return None
        async with self.db.execute('''SELECT url,state,speed_kibps,updated FROM catalog_files
                WHERE size=?
                AND ((?<>'' AND sha256=?) OR (?<>'' AND sha1=?))
                ORDER BY updated DESC LIMIT 32''', (known['size'], known['sha256'], known['sha256'],
                            known['sha1'], known['sha1'])) as cursor:
            alternatives = await cursor.fetchall()
        for item in self.routing.ranked(alternatives):
            if preferred_only and urlsplit(item['url']).hostname in DISCOURAGED:
                continue
            if item['confirmed_fast'] or urlsplit(item['url']).hostname not in DISCOURAGED:
                return item['url']
        return None

    async def public_files(self, data):
        """Browse only verified public mappings; no room paths or private hints."""
        query, after = data.get('query',''), data.get('cursor','')
        provider, state = data.get('provider',''), data.get('state','')
        if (not isinstance(query,str) or len(query)>160 or not isinstance(after,str) or len(after)>4096
                or provider not in ('','curseforge','modrinth','modrinth_only')
                or state not in ('','available','slow','unavailable')):
            raise ValueError('Invalid public mapping filter')
        clauses, args = [], []
        if query:
            if re.fullmatch('[0-9a-f]{40}',query):
                clauses.append('c.sha1=?');args.append(query)
            elif re.fullmatch('[0-9a-f]{64}',query):
                clauses.append('c.sha256=?');args.append(query)
            else:
                clauses.append('instr(lower(c.url),lower(?))>0');args.append(query)
        if state:
            clauses.append('c.state=?');args.append(state)
        cf = "(c.url LIKE 'https://edge.forgecdn.net/%' OR c.url LIKE 'https://mediafilez.forgecdn.net/%' OR c.url LIKE 'https://media.forgecdn.net/%')"
        if provider == 'curseforge':
            clauses.append(cf)
        elif provider.startswith('modrinth'):
            clauses.append("c.url LIKE 'https://cdn.modrinth.com/%'")
            if provider == 'modrinth_only':
                clauses.append('''NOT EXISTS(SELECT 1 FROM catalog_files other WHERE other.size=c.size
                    AND ((c.sha1<>'' AND other.sha1=c.sha1) OR (c.sha256<>'' AND other.sha256=c.sha256))
                    AND (other.url LIKE 'https://edge.forgecdn.net/%' OR other.url LIKE 'https://mediafilez.forgecdn.net/%'
                         OR other.url LIKE 'https://media.forgecdn.net/%'))''')
        where = ' AND '.join(clauses) or '1'
        total = await self.one('SELECT COUNT(*) total FROM catalog_files c WHERE '+where,args)
        async with self.db.execute('SELECT c.* FROM catalog_files c WHERE '+where+' AND c.url>? ORDER BY c.url LIMIT 51',
                                   [*args,after]) as cursor:
            rows = [self.routing.evidence(row) for row in await cursor.fetchall()]
        return dict(files=rows[:50],total=total['total'],cursor=rows[49]['url'] if len(rows)>50 else '')

    async def cached_catalog_url(self, url):
        """Find a verified, live single-file room copy for an indexed URL."""
        if not self.settings()['enabled'] or not self.settings()['room_cache_enabled']:
            return None
        async with self.db.execute('''SELECT DISTINCT f.digest FROM catalog_files c
                JOIN files f ON (c.sha256<>'' AND f.digest=c.sha256)
                    OR (c.sha256='' AND c.sha1<>'' AND f.sha1=c.sha1 AND f.size=c.size)
                WHERE c.url=? AND f.lease_until>? LIMIT 4''', (url, time.time())) as cursor:
            matches = await cursor.fetchall()
        for item in matches:
            if await self.cached(item['digest']):
                return '/room-cache/files/' + item['digest']
        return None

    async def plan(self, request):
        self.enabled()
        scope, claims = authorize(request, write=True)
        try:
            data = await request.clone(client_max_size=2 * 1024 ** 2).json()
            if not isinstance(data, dict) or set(data) - {'files', 'failed', 'direct_upload'} or type(data.get('direct_upload', False)) is not bool:
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
        with self.routing.batch():
            async with self.lock:
                if files is not None:
                    # One metadata request, then indexed batch comparison; never
                    # probe or download every candidate on the player's path.
                    for start in range(0, len(files), 300):
                        batch = files[start:start + 300]
                        marks = ','.join('?' for _ in batch)
                        async with self.db.execute('SELECT digest,size,sha1 FROM files WHERE digest IN (' + marks + ')',
                                                   [file['sha256'] for file in batch]) as cursor:
                            previous = {row['digest']: row for row in await cursor.fetchall()}
                        for file in batch:
                            old = previous.get(file['sha256'])
                            if old and (old['size'] != file['size'] or old['sha1'] and file.get('sha1') and old['sha1'] != file['sha1']):
                                raise web.HTTPBadRequest(text='同一文件哈希的大小或校验信息不一致。')
                    await self.db.executemany('''INSERT INTO files(scope,digest,path,size,sha1,urls,touched)
                            VALUES(?,?,?,?,?,?,?) ON CONFLICT(scope,digest) DO UPDATE SET
                            urls=excluded.urls,touched=excluded.touched''',
                            [(scope, file['sha256'], file['path'], file['size'], file.get('sha1', ''),
                              json.dumps(file.get('downloads', [])), time.time()) for file in files])
                    await self.discovery.register(files)
                for digest in failed:
                    await self.db.execute('UPDATE files SET failed=1,demanded=? WHERE scope=? AND digest=?', (time.time(), scope, digest))
                async with self.db.execute('SELECT * FROM files WHERE scope=? ORDER BY path', (scope,)) as cursor:
                    rows = await cursor.fetchall()
                catalog, existing = await self.planning_index(rows)
                result, requested = [], []
                summary = dict(total=len(rows), cached=0, redirect=0, upload=0, pending=0,
                               cached_bytes=0, cloud_ready=0, cloud_pending=0,
                               receiving=0, receiving_bytes=0, bytes_to_upload=0, reasons={})
                for row in rows:
                    action, reason, urls = await self.decision(row, existing=existing.get(row['digest'], False),
                                                               catalog=catalog.get(row['digest'], []))
                    action, reason = await self.lease(row, action, reason)
                    if action == 'upload' and self.mirror.cloud.route(self.route_name(row['path']))['mode'] != 'cloud' and not self.settings()['room_cache_quota_mib']:
                        action, reason = 'pending', 'local_disabled'
                    elif action == 'upload' and row['size'] > self.settings()['room_cache_max_file_mib'] * 1024 ** 2:
                        action, reason = 'pending', 'capacity'
                    await self.db.execute('UPDATE files SET need=?,reason=? WHERE scope=? AND digest=?',
                                          (int(action == 'upload'), reason, scope, row['digest']))
                    item = dict(sha256=row['digest'], size=row['size'], path=row['path'],
                                action=action, reason=REASONS[reason], reason_code=reason, downloads=urls,
                                url='/room-cache/files/' + row['digest'])
                    result.append(item)
                    summary[action] = summary.get(action, 0) + 1
                    summary['reasons'][reason] = summary['reasons'].get(reason, 0) + 1
                    if action == 'cached':
                        summary['cached_bytes'] += row['size']
                        if existing[row['digest']].get('cloud_ready'):
                            summary['cloud_ready'] += 1
                        elif self.mirror.cloud.route(self.route_name(row['path']))['mode'] != 'local':
                            summary['cloud_pending'] += 1
                    if row['digest'] in self.upload_progress:
                        summary['receiving'] += 1
                        summary['receiving_bytes'] += self.upload_progress[row['digest']]
                    if action == 'upload' and claims['write']:
                        requested.append(item)
                        summary['bytes_to_upload'] += row['size']
                await self.db.commit()
            if data.get('direct_upload') and claims['write']:
                await self.mirror.cloud.direct.attach(scope, requested)
            return web.json_response(dict(protocol='room-content-cache-v1', files=result, upload=requested,
                public_lookup='background' if self.discovery.enabled() else 'disabled',
                summary=summary, retry_after=5, state='ready' if rows else 'waiting',
                ttl_hours=self.settings()['room_cache_ttl_hours']),
                headers={'Cache-Control': 'no-store'})

    async def release(self, request):
        """End this room's leases without touching another room's shared objects."""
        scope, _ = authorize(request, write=True)
        async with self.lock:
            cursor = await self.db.execute('UPDATE files SET lease_until=?,need=0 WHERE scope=?',
                                           (time.time(), scope))
            await self.db.commit()
        await self.mirror.cloud.direct.release(scope)
        if not self.cleanup_task or self.cleanup_task.done():
            self.cleanup_task = asyncio.create_task(self.cleanup())
        return web.json_response(dict(released=cursor.rowcount), headers={'Cache-Control': 'no-store'})

    async def resolve(self, request):
        """Public hash lookup; cannot register paths, URLs or upload bodies."""
        if not self.settings()['enabled']:
            raise web.HTTPServiceUnavailable(text='Resource routing is disabled')
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
        result, rows_by_hash, public_by_hash = [], {}, {}
        wanted = list(dict.fromkeys(wanted))
        now = time.time()
        with self.routing.batch():
            async with self.lock:
                # One HTTP manifest, bounded indexed SQL batches; no per-file query
                # or network probe before the download plan becomes usable.
                for start in range(0, len(wanted), 300):
                    batch = wanted[start:start+300]
                    marks = ','.join('?' for _ in batch)
                    async with self.db.execute('SELECT * FROM files WHERE (sha1 IN ('+marks+
                            ') OR digest IN ('+marks+')) AND (lease_until>? OR (lease_until=0 AND touched>?)) '
                            'ORDER BY touched DESC', [*batch,*batch,now,now-self.settings()['room_cache_ttl_hours']*3600]) as cursor:
                        for record in await cursor.fetchall():
                            if not self.settings()['room_cache_enabled']:
                                continue
                            row = dict(record)
                            for key in (row['digest'],row['sha1']):
                                if key in batch:
                                    rows_by_hash.setdefault(key,row)
                    async with self.db.execute('SELECT * FROM catalog_files WHERE sha1 IN ('+marks+
                            ') OR sha256 IN ('+marks+') ORDER BY updated DESC', [*batch,*batch]) as cursor:
                        for item in await cursor.fetchall():
                            for key in (item['sha1'],item['sha256']):
                                if key in batch:
                                    public_by_hash.setdefault(key,[]).append(item)
                rows = list({row['digest']:row for row in rows_by_hash.values()}.values())
                resource_sources, mirror_copies = await public_index(self.mirror, wanted)
                for digest, candidates in resource_sources.items():
                    public_by_hash.setdefault(digest, []).extend(candidates)
                catalog, existing = await self.planning_index(rows)
                failed_digests = {rows_by_hash[key]['digest'] for key in failed if key in rows_by_hash}
                await self.db.executemany('UPDATE files SET failed=1,demanded=? WHERE digest=?',
                                          [(now,digest) for digest in failed_digests])
                for wanted_hash in wanted:
                    row = rows_by_hash.get(wanted_hash)
                    if not row:
                        matches = [item for item in self.routing.ranked(public_by_hash.get(wanted_hash,[]))
                                   if wanted_hash not in failed and (item['confirmed_fast'] or urlsplit(item['url']).hostname not in DISCOURAGED)]
                        copy = mirror_copies.get(wanted_hash)
                        if copy and not any(item['confirmed_fast'] for item in matches):
                            result.append(dict(sha256=copy['object'],sha1=copy['sha1'],size=copy['size'],
                                action='cached',reason=REASONS['mirror_cached'],downloads=[],
                                url='/room-cache/files/'+copy['object']))
                        elif matches:
                            size = matches[0]['size']
                            result.append(dict(sha256=wanted_hash if len(wanted_hash)==64 else '',
                                sha1=wanted_hash if len(wanted_hash)==40 else '',size=size,action='redirect',
                                reason=REASONS['direct'],downloads=[item['url'] for item in matches if item['size']==size],url=''))
                        continue
                    digest = row['digest']
                    if digest in failed_digests:
                        row.update(failed=1,demanded=now)
                    action, reason, urls = await self.decision(row,existing=existing.get(digest,False),
                                                              catalog=catalog.get(digest,[]))
                    result.append(dict(sha256=digest,sha1=row['sha1'],size=row['size'],action=action,reason=REASONS[reason],
                                       downloads=urls,url='/room-cache/files/'+digest))
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
                return web.json_response(dict(state='cached', sha256=digest, size=size), headers={'Cache-Control': 'no-store'})
            if digest in self.uploading or len(self.uploading) >= 2:
                raise web.HTTPTooManyRequests(text='文件补传正在进行，请稍后继续。', headers={'Retry-After': '5'})
            kind = 'local' if await self.needs_local(digest) else 'staging'
            usage = await self.storage_usage()
            quota_key = 'room_cache_quota_mib' if kind == 'local' else 'room_cache_staging_mib'
            used = usage[kind + '_bytes']
            free = (await asyncio.to_thread(shutil.disk_usage, self.root)).free
            if (used + self.reserved_by_kind[kind] + size > self.settings()[quota_key] * 1024 ** 2
                    or free - self.reserved - size < self.settings()['reserve_gib'] * 1024 ** 3):
                raise web.HTTPInsufficientStorage(text=('房间本地保留容量不足' if kind == 'local' else '房间云端上传中转暂存已满，请等待上传释放') + '；未影响云空间容量，已保留其他文件。')
            self.uploading.add(digest)
            self.reserved += size
            self.reserved_by_kind[kind] += size
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
                # These URLs now refer to bytes verified on the server, rather
                # than just a host's unverified hash declaration.
                await self.db.execute('UPDATE files SET sha1=? WHERE digest=? AND sha1=\'\'',(sha1.hexdigest(),digest))
                await self.db.executemany('''INSERT OR IGNORE INTO catalog_files VALUES(?,?,?,?,?,?,?)''',
                    [(url,digest,sha1.hexdigest(),size,
                      'slow' if urlsplit(url).hostname in DISCOURAGED else 'available',0,time.time())
                     for url in json.loads(row['urls'])])
                await self.db.commit()
            try:
                await self.discovery.uploaded(digest)
                async with self.db.execute('SELECT DISTINCT path FROM files WHERE digest=? AND lease_until>?', (digest, time.time())) as cursor:
                    references = await cursor.fetchall()
                for route in {self.route_name(ref['path']) for ref in references}:
                    await self.mirror.cloud.enqueue('room', digest, size, route)
            except Exception:
                logging.exception('Room cloud copy could not be queued; local file remains available')
            return web.json_response(dict(state='cached', sha256=digest, size=size), headers={'Cache-Control': 'no-store'})
        finally:
            temp.unlink(missing_ok=True)
            self.uploading.discard(digest)
            self.upload_progress.pop(digest, None)
            self.reserved -= size
            self.reserved_by_kind[kind] -= size

    async def download(self, request):
        if not self.settings()['enabled']:
            raise web.HTTPServiceUnavailable(text='Resource routing is disabled')
        public = await download_mirror_copy(self, request)
        if public is not None:
            return public
        self.enabled()
        digest = request.match_info['digest']
        async with self.lock:
            row = await self.one('SELECT * FROM files WHERE digest=? ORDER BY touched DESC LIMIT 1', (digest,))
            if not row:
                raise web.HTTPNotFound(text='尚未收录此文件信息。')
            cached = await self.cached(digest)
            action, reason, urls = await self.decision(row, existing=cached or False)
            if action == 'redirect':
                raise web.HTTPTemporaryRedirect(location=urls[0], headers={'Cache-Control': 'no-store'})
            if cached:
                route = ('room_mods' if row['path'].startswith('mods/') else
                         'room_resourcepacks' if row['path'].startswith('resourcepacks/') else 'room_shaderpacks')
                lease = await self.one('SELECT MAX(lease_until) expires FROM files WHERE digest=?', (digest,))
                room_local = self.path(digest).is_file()
                mirror_path = self.mirror.root / 'objects' / digest[:2] / digest
                mirror_local = mirror_path.is_file()
                use_cloud = self.settings()['qiniu_routes'][route]['mode'] == 'cloud' or not room_local
                cloud_scope = 'mirror' if cached.get('mirror_copy') else 'room'
                cloud_url = await self.mirror.cloud.download_url(cloud_scope, digest,
                    expires_seconds=max(1, int(lease['expires'] - time.time())),
                    reserve=request.method != 'HEAD', method=request.method) if use_cloud else None
                if cloud_url:
                    return web.Response(status=307, headers={'Location': cloud_url,
                        'Cache-Control': 'no-store', 'Cloudflare-CDN-Cache-Control': 'no-store'})
                if not room_local and not mirror_local:
                    cached = None
            if cached:
                async with self.mirror.space_lock:
                    blocked = await self.mirror.egress.admit(requested_bytes(request, cached['size']),
                        self.settings(), sum(self.serving.values()) + sum(self.mirror.serving.values()))
                    if blocked:
                        raise web.HTTPServiceUnavailable(text='源站临时达到下载并发或流量上限，请稍后重试。', headers={'Retry-After': '5'})
                    self.serving[digest] = self.serving.get(digest, 0) + 1
                    if not room_local:
                        self.mirror.serving[digest] = self.mirror.serving.get(digest, 0) + 1
                response = PinnedResponse(self.path(digest) if room_local else mirror_path,
                                          headers={'Cache-Control': 'no-store'})
                response.cache, response.digest, response.mirror_copy = self, digest, not room_local
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
                    # A previously issued PUT remains usable for its short TTL.
                    # Keep its key tracked until expiry before deleting the bytes.
                    if await self.mirror.one('SELECT 1 FROM room_direct_uploads WHERE digest=? AND expires>? LIMIT 1', (digest,time.time()-120)):
                        continue
                    if digest not in self.serving and digest not in self.uploading and not self.mirror.cloud.uploading(digest):
                        try:
                            await self.mirror.cloud.delete_room(digest)
                            await self.mirror.cloud.discard_room_jobs(digest)
                        except (OSError, RuntimeError):
                            continue
                        self.path(digest).unlink(missing_ok=True)
                        await self.db.execute('DELETE FROM objects WHERE digest=?', (digest,))
            # Temporary files have no historical list. Active references in
            # other rooms survive; expired metadata and failures are discarded.
            await self.db.execute('DELETE FROM files WHERE (lease_until>0 AND lease_until<=?) OR (lease_until=0 AND touched<?)',
                (time.time(), time.time()-self.settings()['room_cache_ttl_hours']*3600))
            # Unpublished uploads and their names/failed searches are temporary.
            # Only validated public mappings survive the final room reference.
            tables = await self.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_lookups'")
            if tables:
                await self.db.execute('''DELETE FROM public_lookups
                    WHERE NOT EXISTS(SELECT 1 FROM files WHERE files.digest=public_lookups.digest)''')
            await self.db.execute('DELETE FROM sources WHERE touched<?', (time.time() - 32 * 3600,))
            await self.db.execute('DELETE FROM budget WHERE day<?', (int(time.time() // 86400) - 2,))
            await self.db.commit()

    async def probe(self):
        await self.mirror.quality.schedule()

    async def maintain(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.cleanup()
                await self.mirror.cloud.direct.cleanup()
                async with self.lock:
                    await self.db.execute("DELETE FROM public_lookups WHERE state NOT IN ('matched','checking') AND NOT EXISTS(SELECT 1 FROM files WHERE files.digest=public_lookups.digest)")
                    await self.db.commit()
                # Planning and maintenance never fetch host-supplied URLs.
            except (OSError, aiosqlite.Error):
                # A sampling failure never blocks room traffic or stops cleanup.
                continue

    async def snapshot(self):
        totals = await self.one('SELECT COUNT(*) files,COALESCE(SUM(size*local),0) bytes FROM objects')
        catalog = await self.one('''SELECT COUNT(*) files,
            SUM(CASE WHEN state='available' THEN 1 ELSE 0 END) available,
            SUM(CASE WHEN state='slow' THEN 1 ELSE 0 END) slow,
            SUM(CASE WHEN state='unavailable' THEN 1 ELSE 0 END) unavailable,
            MAX(updated) updated FROM catalog_files''')
        async with self.db.execute('SELECT path,size,reason,touched FROM files WHERE lease_until>? OR (lease_until=0 AND touched>?) ORDER BY touched DESC LIMIT 50',
                (time.time(), time.time()-self.settings()['room_cache_ttl_hours']*3600)) as cursor:
            recent = [dict(row) for row in await cursor.fetchall()]
        async with self.db.execute('SELECT host,speed,bad,samples,checked FROM sources ORDER BY host') as cursor:
            sources = [dict(row) for row in await cursor.fetchall()]
        return dict(configured=bool(keys(self.mirror.root)), **dict(totals), uploads=len(self.uploading),
                    **await self.storage_usage(), reserved=dict(self.reserved_by_kind),
                    downloads=sum(self.serving.values()), recent=recent, sources=sources,
                    catalog=dict(catalog), discovery=await self.discovery.snapshot(), reasons=REASONS)
