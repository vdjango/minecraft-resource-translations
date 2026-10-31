"""Independent download/cache process; never uses translation workers or database."""
import argparse
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import time
import random
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, quote

import aiosqlite
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from .config import Config, load_env
from .mirror_catalog import Inventory, MANIFEST, allowed_url
from .mirror_policy import POLICIES, MODES, category_for
from .mirror_routing import ROUTING_DEFAULTS, Egress, requested_bytes, source_target, source_targets, validate_sources
from .federation import origin, PublicResolver
from .mirror_sources import BUILTIN_SOURCES, SourceDiscovery, effective_sources, source_status, validate_registries
from .mirror_source_management import SourceManagement
from .mirror_quality import QUALITY_DEFAULTS, SourceQuality
from .room_cache import RoomCache, DEFAULTS as ROOM_CACHE_DEFAULTS, validate_settings as validate_room_cache

GIB = 1024 ** 3
DEFAULTS = dict(enabled=True, public_url='https://mirror.bugg.icu', quota_gib=400,
                reserve_gib=100, concurrency=4, metadata_ttl=300, policies=POLICIES,
                auto_refresh=True, refresh_minutes=30, eviction='oldest_version',
                protect_latest=2, nodes=[], node_weight=1, auto_sources=True, source_prefer_cn=True,
                source_cleanup=False, source_registries=BUILTIN_SOURCES, **ROUTING_DEFAULTS, **QUALITY_DEFAULTS,
                **ROOM_CACHE_DEFAULTS)


class PinnedFileResponse(web.FileResponse):
    async def prepare(self, request):
        try:
            return await super().prepare(request)
        finally:
            self.mirror.serving[self.digest] -= 1
            if not self.mirror.serving[self.digest]:
                self.mirror.serving.pop(self.digest)


def settings_path(config):
    return Path(config.mirror_root).resolve() / 'settings.json'


@lru_cache(maxsize=8)
def _settings_data(path, stamp, size):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_settings(config, *, effective=True):
    path = settings_path(config)
    try:
        stat = path.stat()
        data = _settings_data(str(path), stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        data = {}
    return {**DEFAULTS, **data, 'policies': {**POLICIES, **data.get('policies', {})},
            'sources': effective_sources(config) if effective else data.get('sources', [])}


def validate_settings(data):
    if not isinstance(data, dict) or set(data) - DEFAULTS.keys():
        raise ValueError('invalid mirror settings')
    result = {**DEFAULTS, **data}
    policies = result['policies']
    if (not isinstance(policies, dict) or set(policies) - POLICIES.keys()
            or any(type(value) is not str or value not in MODES for value in policies.values())):
        raise ValueError('invalid resource policies')
    result['policies'] = {**POLICIES, **policies}
    if result['eviction'] not in ('stop', 'lru', 'oldest_version'):
        raise ValueError('invalid eviction strategy')
    if not isinstance(result['nodes'], list) or len(result['nodes']) > 16:
        raise ValueError('invalid nodes')
    nodes = []
    for node in result['nodes']:
        if (not isinstance(node, dict) or set(node) != {'url', 'weight', 'enabled'}
                or type(node['weight']) is not int or not 1 <= node['weight'] <= 100
                or type(node['enabled']) is not bool):
            raise ValueError('invalid node')
        url = origin(node['url'])
        if url in [n['url'] for n in nodes]:
            raise ValueError('duplicate node')
        nodes.append(dict(node, url=url))
    result['nodes'] = nodes
    if type(result['enabled']) is not bool or type(result['auto_refresh']) is not bool or type(result['auto_sources']) is not bool:
        raise ValueError('invalid enabled')
    if not isinstance(result['public_url'], str):
        raise ValueError('invalid public URL')
    parsed = urlsplit(result['public_url'])
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError('invalid public URL')
    for key, low, high in [('quota_gib', 1, 100000), ('reserve_gib', 10, 100000),
                           ('concurrency', 1, 8), ('metadata_ttl', 60, 3600), ('refresh_minutes', 5, 1440),
                           ('protect_latest', 0, 50), ('node_weight', 0, 100)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('invalid ' + key)
    result['public_url'] = result['public_url'].rstrip('/')
    for key, low, high in [('monthly_egress_gib', 0, 1000000), ('egress_threshold', 10, 100),
                           ('bandwidth_mbps', 0, 100000), ('max_serving', 1, 512)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('invalid ' + key)
    result['sources'] = validate_sources(result['sources'], result['public_url'])
    for key in ('source_prefer_cn','source_cleanup'):
        if type(result[key]) is not bool:
            raise ValueError('invalid '+key)
    result['source_registries'] = validate_registries(result['source_registries'], result['public_url'])
    for key in ('quality_enabled', 'quality_adaptive_cache'):
        if type(result[key]) is not bool:
            raise ValueError('invalid ' + key)
    for key, low, high in [('quality_interval_hours', 1, 168), ('quality_daily_mib', 1, 1024),
                           ('quality_sample_kib', 64, 1024), ('quality_fast_kibps', 64, 102400),
                           ('quality_slow_kibps', 1, 102400)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('invalid ' + key)
    if result['quality_slow_kibps'] >= result['quality_fast_kibps']:
        raise ValueError('slow speed must be below fast speed')
    validate_room_cache(result)
    return result


def mutable_metadata(url):
    return (urlsplit(url).hostname in ('meta.fabricmc.net', 'meta.quiltmc.org')
            or url.endswith(('.json', '.xml')) and not re.search(r'/[a-f0-9]{40}/', url))


def upstreams(path):
    """Fixed origin mapping; never accepts arbitrary URLs, queries or mods."""
    if len(path) > 1024 or any(c in path for c in ('%', '\\', '\x00', ':')) or any(p in ('.', '..') for p in path.split('/')):
        return []
    if path.startswith('/java/v1/'):
        return upstreams(path[5:])
    if re.fullmatch(r'/mods/modrinth/data/[A-Za-z0-9_-]+/versions/[A-Za-z0-9_-]+/[A-Za-z0-9._ +@-]+\.(?:jar|zip|mrpack|litemod)', path):
        return ['https://cdn.modrinth.com' + quote(path[len('/mods/modrinth'):], safe='/@+')]
    if re.fullmatch(r'/mods/curseforge/files/[0-9]+/[0-9]+/[A-Za-z0-9._ +@-]+\.(?:jar|zip|mrpack|litemod)', path):
        return ['https://edge.forgecdn.net' + quote(path[len('/mods/curseforge'):], safe='/@+')]
    if path in ('/mc/game/version_manifest.json', '/mc/game/version_manifest_v2.json'):
        return [MANIFEST.replace('version_manifest_v2.json', path.rsplit('/', 1)[1])]
    if re.fullmatch(r'/assets/[a-f0-9]{2}/[a-f0-9]{40}', path):
        return ['https://resources.download.minecraft.net' + path[7:]] if path[8:10] == path[-40:-38] else []
    if re.fullmatch(r'/v1/(?:packages|objects)/[a-f0-9]{40}/[A-Za-z0-9._ @-]+', path):
        return [('https://piston-meta.mojang.com' if '/packages/' in path else 'https://piston-data.mojang.com') + quote(path, safe='/@')]
    if path.startswith('/v1/products/java-runtime/') and re.fullmatch(r'/[A-Za-z0-9/_.-]+', path):
        return ['https://launchermeta.mojang.com' + path]
    mappings = [
        ('/fabric-meta/', 'https://meta.fabricmc.net/'),
        ('/quilt-meta/', 'https://meta.quiltmc.org/'),
        ('/mirrors/authlib-injector/', 'https://authlib-injector.yushi.moe/'),
        ('/fabric-maven/', 'https://maven.fabricmc.net/'),
        ('/quilt-maven/', 'https://maven.quiltmc.org/'),
        ('/neoforge-maven/', 'https://maven.neoforged.net/'),
    ]
    for prefix, origin in mappings:
        if path.startswith(prefix):
            suffix = path[len(prefix):]
            # Loader Maven only. In particular Fabric API is an ordinary mod.
            if prefix == '/fabric-maven/' and not suffix.startswith(('net/fabricmc/fabric-loader/', 'net/fabricmc/intermediary/', 'net/fabricmc/tiny-', 'net/fabricmc/sponge-mixin/')):
                return []
            if prefix == '/quilt-maven/' and not suffix.startswith(('repository/release/org/quiltmc/quilt-loader/', 'repository/release/org/quiltmc/hashed/')):
                return []
            if prefix == '/neoforge-maven/' and not suffix.startswith(('releases/net/neoforged/', 'releases/cpw/mods/', 'releases/net/minecraftforge/')):
                return []
            return [origin + suffix]
    if path.startswith('/maven/'):
        suffix = path[7:]
        if not re.fullmatch(r'[A-Za-z0-9/_.+\-]+', suffix):
            return []
        if suffix.startswith('net/fabricmc/fabric-api/'):
            return []
        if suffix.startswith(('net/fabricmc/fabric-loader/', 'net/fabricmc/intermediary/', 'net/fabricmc/tiny-', 'net/fabricmc/sponge-mixin/')):
            return ['https://maven.fabricmc.net/' + suffix]
        if suffix.startswith('net/neoforged/'):
            return ['https://maven.neoforged.net/releases/' + suffix]
        if suffix.startswith(('net/minecraftforge/', 'de/oceanlabs/', 'cpw/mods/')):
            return ['https://maven.minecraftforge.net/' + suffix]
        # Mojang's library repository contains runtime dependencies, not mod hosting.
        return ['https://libraries.minecraft.net/' + suffix]
    return []


class Mirror:
    def __init__(self, config):
        self.config = config
        self.root = Path(config.mirror_root).resolve()
        self.db = None
        self.http = None
        self.inflight = {}
        self.backups = {}
        self.probes = {}
        self.origin_health = {}
        self.node_health = {}
        self.touched = {}
        self.serving = {}
        self.reserved = 0
        self.space_lock = asyncio.Lock()
        self.job = None
        self.scheduler = None
        self.discovery = SourceDiscovery(self.root)
        self.source_job = None
        self.next_refresh = None
        self.last_refresh = None
        self.job_state = dict(state='idle')
        self.inventory = None
        self.started = time.time()
        self.token_path = self.root / 'control-token'
        self.sources = SourceManagement(self)
        self.quality = SourceQuality(self)
        self.room_cache = RoomCache(self, lambda: read_settings(self.config))

    async def open(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'objects').mkdir(exist_ok=True)
        (self.root / 'tmp').mkdir(exist_ok=True)
        if not self.token_path.exists():
            self.token_path.write_text(secrets.token_urlsafe(32), encoding='ascii')
            if os.name != 'nt':
                self.token_path.chmod(0o600)
        self.secret = self.token_path.read_text(encoding='ascii').strip()
        self.db = await aiosqlite.connect(self.root / 'mirror.sqlite3')
        self.db.row_factory = aiosqlite.Row
        await self.db.execute('PRAGMA journal_mode=WAL')
        await self.db.executescript('''
            CREATE TABLE IF NOT EXISTS cache(url TEXT PRIMARY KEY, object TEXT, size INTEGER, sha1 TEXT, checked INTEGER, fetched REAL, type TEXT);
            CREATE TABLE IF NOT EXISTS expected(url TEXT PRIMARY KEY, sha1 TEXT, size INTEGER, category TEXT);
            CREATE TABLE IF NOT EXISTS objects(digest TEXT PRIMARY KEY, size INTEGER NOT NULL, accessed REAL NOT NULL DEFAULT 0, priority REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS totals(id INTEGER PRIMARY KEY CHECK(id=1), bytes INTEGER, files INTEGER);
            CREATE INDEX IF NOT EXISTS cache_object ON cache(object);
            CREATE TABLE IF NOT EXISTS priorities(url TEXT PRIMARY KEY, priority REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS priorities_order ON priorities(priority DESC);
        ''')
        async with self.db.execute('PRAGMA table_info(objects)') as cursor:
            object_columns = {row['name'] for row in await cursor.fetchall()}
        for name in ('accessed', 'priority'):
            if name not in object_columns:
                await self.db.execute(f'ALTER TABLE objects ADD COLUMN {name} REAL NOT NULL DEFAULT 0')
        await self.db.execute('CREATE INDEX IF NOT EXISTS eviction_objects ON objects(priority,accessed)')
        await self.db.execute('CREATE INDEX IF NOT EXISTS eviction_access ON objects(accessed)')
        if not await self.one('SELECT id FROM totals WHERE id=1'):
            await self.db.execute('INSERT OR IGNORE INTO objects(digest,size,accessed) SELECT object,MAX(size),MAX(fetched) FROM cache GROUP BY object')
            await self.db.execute('INSERT INTO totals VALUES(1,(SELECT COALESCE(SUM(size),0) FROM objects),(SELECT COUNT(*) FROM cache))')
        await self.db.commit()
        await self.sources.open()
        await self.quality.open()
        self.egress = Egress(self.db)
        await self.egress.open()
        self.http = ClientSession(timeout=ClientTimeout(total=1800, connect=15, sock_read=60),
                                  connector=TCPConnector(limit=8, limit_per_host=4),
                                  headers={'User-Agent': 'Buggcraft-Mirror/0.1 (+https://bugg.icu)',
                                           'Accept-Encoding': 'identity'},
                                  auto_decompress=False)
        self.probe_http = ClientSession(timeout=ClientTimeout(total=3),
                                        connector=TCPConnector(limit=4, limit_per_host=2),
                                        headers={'User-Agent': 'Buggcraft-Mirror/0.1', 'Accept-Encoding': 'identity'})
        self.node_http = ClientSession(timeout=ClientTimeout(total=3), connector=TCPConnector(limit=4, resolver=PublicResolver()))
        # Interrupted partial files are never published; remove only our temp files.
        for file in (self.root / 'tmp').glob('*.part'):
            file.unlink(missing_ok=True)
        await self.room_cache.open()
        self.scheduler = asyncio.create_task(self.refresh_loop())
        return self

    async def close(self):
        await self.room_cache.close()
        await self.quality.close()
        tasks = [t for t in [self.scheduler, self.job, self.source_job, *self.backups.values(), *self.probes.values(), *self.inflight.values()] if t]
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.probe_http.close()
        await self.node_http.close()
        await self.http.close()
        await self.discovery.close()
        await self.sources.flush()
        await self.db.close()

    async def one(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            return await cursor.fetchone()

    async def status(self, *, compact=False):
        totals = await self.one('SELECT bytes,files FROM totals WHERE id=1')
        used, count = totals['bytes'], totals['files']
        disk = await asyncio.to_thread(shutil.disk_usage, self.root)
        settings = read_settings(self.config)
        source_mappings = sum(row['enabled'] and row['mode']=='redirect' for row in settings['sources'])
        if compact:
            settings = {**settings,'sources':[]}
        return dict(running=True, started_at=self.started, settings=settings, policies=settings['policies'],
                    source_mappings=source_mappings,
                    cached_bytes=used, cached_files=count, disk_total=disk.total, disk_free=disk.free,
                    active_downloads=len(self.inflight), job={**self.job_state, **({'progress': self.inventory.progress} if self.inventory else {})},
                    refresh=dict(next_at=self.next_refresh, last=self.last_refresh),
                    automatic_sources=source_status(self.root),
                    source_statistics=await self.sources.summary(),
                    source_quality=await self.quality.snapshot(),
                    room_client_cache=await self.room_cache.snapshot(),
                    egress=await self.egress.snapshot(settings),
                    accepting=not await self.egress.reason(1, settings, sum(self.serving.values())),
                    nodes=[dict(n, healthy=self.node_health.get(n['url'], {}).get('healthy', False)) for n in settings['nodes']],
                    root=str(self.root), scope='official-game-assets-libraries-java-loaders',
                    exclusions=[*(['mods'] if read_settings(self.config)['policies']['mods'] == 'disabled' else []),
                                'modpacks', 'resourcepacks', 'shaders'],
                    unavailable=['OptiFine redistribution', 'legacy Oracle Java', 'LiteLoader API', 'BMCLAPI Forge query API'])

    async def cached(self, url):
        item = await self.one('SELECT * FROM cache WHERE url=?', (url,))
        if item and (self.root / 'objects' / item['object'][:2] / item['object']).is_file():
            return item
        return None

    async def make_space(self, size, settings, incoming_priority=None):
        """Called under the reservation lock. Bounded indexed batches, no tree scans."""
        if settings['eviction'] == 'stop':
            return
        used = (await self.one('SELECT bytes FROM totals WHERE id=1'))['bytes']
        free = (await asyncio.to_thread(shutil.disk_usage, self.root)).free
        needed = max(used + self.reserved + size - settings['quota_gib'] * GIB,
                     settings['reserve_gib'] * GIB - free + self.reserved + size)
        if needed <= 0:
            return
        protected = None
        if settings['protect_latest']:
            protected = (await self.one('SELECT MIN(priority) p FROM (SELECT DISTINCT priority FROM priorities WHERE priority>0 ORDER BY priority DESC LIMIT ?)', (settings['protect_latest'],)))['p']
        order = 'accessed' if settings['eviction'] == 'lru' else 'priority,accessed'
        admission = incoming_priority if settings['eviction'] == 'oldest_version' else None
        order += ',digest'
        columns = order.split(',')
        after = None
        while needed > 0:
            clause = ' AND (' + order + ') > (' + ','.join('?' for _ in columns) + ')' if after else ''
            args = (protected, protected, admission, admission, *(after or ()))
            async with self.db.execute('SELECT digest,size,priority,accessed FROM objects WHERE (? IS NULL OR priority<?) AND (? IS NULL OR priority<=?)'+clause+' ORDER BY '+order+' LIMIT 256', args) as cursor:
                candidates = await cursor.fetchall()
            if not candidates:
                break
            after = tuple(candidates[-1][column] for column in columns)
            for item in candidates:
                digest = item['digest']
                if self.serving.get(digest):
                    continue
                file = self.root/'objects'/digest[:2]/digest
                try:
                    await asyncio.to_thread(file.unlink, missing_ok=True)
                except OSError:
                    continue
                count = (await self.one('SELECT COUNT(*) n FROM cache WHERE object=?', (digest,)))['n']
                await self.db.execute('DELETE FROM cache WHERE object=?', (digest,))
                await self.db.execute('DELETE FROM objects WHERE digest=?', (digest,))
                await self.db.execute('UPDATE totals SET bytes=bytes-?,files=files-? WHERE id=1', (item['size'], count))
                needed -= item['size']
                if needed <= 0:
                    break
            await self.db.commit()

    async def category(self, url):
        expected = await self.one('SELECT category FROM expected WHERE url=?', (url,))
        return category_for(url, expected['category'] if expected else None)

    async def remember_metadata(self, url, file):
        """Learn shared Mojang object categories before nested downloads arrive."""
        if urlsplit(url).hostname not in ('piston-meta.mojang.com', 'launchermeta.mojang.com', 'launcher.mojang.com'):
            return
        if not url.endswith('.json') or file.stat().st_size > 16 * 1024 * 1024:
            return
        def parse():
            data = json.loads(file.read_text(encoding='utf-8'))
            rows = []
            def add(item, category):
                if isinstance(item, dict) and allowed_url(item.get('url', '')):
                    rows.append((item['url'], item.get('sha1'), item.get('size'), category))
            if not isinstance(data, dict):
                return rows
            for name, item in data.get('downloads', {}).items():
                add(item, 'game-' + name)
            for lib in data.get('libraries', []):
                downloads = lib.get('downloads', {})
                add(downloads.get('artifact'), 'libraries')
                for item in downloads.get('classifiers', {}).values():
                    add(item, 'natives')
            add(data.get('assetIndex'), 'assets-metadata')
            for item in data.get('logging', {}).values():
                add(item.get('file'), 'logging')
            for item in data.get('files', {}).values():
                for kind, download in item.get('downloads', {}).items():
                    add(download, 'java-' + kind)
            if '/products/java-runtime/' in url:
                for platform in data.values():
                    for releases in platform.values():
                        for release in releases:
                            add(release.get('manifest'), 'java-metadata')
            return rows
        try:
            rows = await asyncio.to_thread(parse)
        except (ValueError, TypeError, AttributeError):
            return
        if rows:
            await self.db.executemany('INSERT OR REPLACE INTO expected VALUES(?,?,?,?)', rows)
            await self.db.commit()

    async def download(self, url):
        if not allowed_url(url):
            raise web.HTTPNotFound()
        settings = read_settings(self.config)
        old = await self.cached(url)
        mutable = mutable_metadata(url)
        if old and (not mutable or time.time() - old['fetched'] < settings['metadata_ttl']):
            return old
        # Mutable manifests may change after the inventory snapshot was taken.
        expected = None if mutable else await self.one('SELECT * FROM expected WHERE url=?', (url,))
        sha1 = expected['sha1'] if expected else None
        if re.search(r'/(?:objects|packages)/([a-f0-9]{40})/', url):
            sha1 = re.search(r'/(?:objects|packages)/([a-f0-9]{40})/', url)[1]
        if urlsplit(url).hostname == 'resources.download.minecraft.net':
            sha1 = url.rsplit('/', 1)[1]
        part = self.root / 'tmp' / (secrets.token_hex(16) + '.part')
        reservation = 0
        try:
            async with self.http.get(url, allow_redirects=False) as response:
                if response.status == 404:
                    raise web.HTTPNotFound()
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise web.HTTPBadGateway(text='Upstream response unavailable; redirects are not followed.')
                size = response.content_length
                if size is not None and size > 2 * GIB:
                    raise web.HTTPRequestEntityTooLarge(max_size=2 * GIB, actual_size=size)
                async with self.space_lock:
                    settings = read_settings(self.config)
                    priority = await self.one('SELECT priority FROM priorities WHERE url=?', (url,))
                    await self.make_space(size if size is not None else 2 * GIB, settings, priority['priority'] if priority else 0)
                    used = (await self.one('SELECT bytes FROM totals WHERE id=1'))['bytes']
                    free = (await asyncio.to_thread(shutil.disk_usage, self.root)).free
                    reservation = size if size is not None else 2 * GIB
                    if used + self.reserved + reservation > settings['quota_gib'] * GIB or free - self.reserved - reservation < settings['reserve_gib'] * GIB:
                        reservation = 0
                        raise web.HTTPInsufficientStorage(text='Mirror quota or disk reserve reached.')
                    self.reserved += reservation
                h1, h256, written = hashlib.sha1(), hashlib.sha256(), 0
                with part.open('wb') as file:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        written += len(chunk)
                        if written > reservation or written > 2 * GIB:
                            raise web.HTTPBadGateway(text='Upstream size mismatch')
                        h1.update(chunk)
                        h256.update(chunk)
                        await asyncio.to_thread(file.write, chunk)
                if size is not None and written != size or expected and expected['size'] is not None and written != expected['size']:
                    raise web.HTTPBadGateway(text='Upstream size mismatch')
                if sha1 and h1.hexdigest() != sha1:
                    raise web.HTTPBadGateway(text='Upstream checksum mismatch')
                digest = h256.hexdigest()
                dest = self.root / 'objects' / digest[:2] / digest
                dest.parent.mkdir(exist_ok=True)
                async with self.space_lock:
                    if dest.exists():
                        part.unlink()
                    else:
                        await asyncio.to_thread(part.replace, dest)
                    content_type = response.headers.get('Content-Type', '').split(';')[0] or mimetypes.guess_type(url)[0] or 'application/octet-stream'
                    priority = await self.one('SELECT priority FROM priorities WHERE url=?', (url,))
                    inserted = await self.db.execute('INSERT OR IGNORE INTO objects(digest,size,accessed,priority) VALUES(?,?,?,?)',
                                                    (digest, written, time.time(), priority['priority'] if priority else 0))
                    if inserted.rowcount:
                        await self.db.execute('UPDATE totals SET bytes=bytes+? WHERE id=1', (written,))
                    else:
                        await self.db.execute('UPDATE objects SET priority=MAX(priority,?) WHERE digest=?', (priority['priority'] if priority else 0, digest))
                    if not await self.one('SELECT url FROM cache WHERE url=?', (url,)):
                        await self.db.execute('UPDATE totals SET files=files+1 WHERE id=1')
                    await self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?,?)',
                                          (url, digest, written, h1.hexdigest(), int(bool(sha1)), time.time(), content_type))
                    await self.db.commit()
                await self.remember_metadata(url, dest)
                return await self.cached(url)
        except (ClientError, asyncio.TimeoutError) as exc:
            if old:
                return old
            raise web.HTTPBadGateway(text='Official upstream temporarily unavailable') from exc
        finally:
            part.unlink(missing_ok=True)
            async with self.space_lock:
                self.reserved -= reservation

    async def ensure(self, url):
        old = await self.cached(url)
        mutable = mutable_metadata(url)
        if old and (not mutable or time.time() - old['fetched'] < read_settings(self.config)['metadata_ttl']):
            return old
        if url not in self.inflight:
            if len(self.inflight) >= read_settings(self.config)['concurrency']:
                raise web.HTTPServiceUnavailable(headers={'Retry-After': '3'}, text='Download queue busy')
            task = asyncio.create_task(self.download(url))
            self.inflight[url] = task
            def finished(task):
                self.inflight.pop(url, None)
                if not task.cancelled():
                    task.exception()
            task.add_done_callback(finished)
        return await asyncio.shield(self.inflight[url])

    def backup(self, url):
        """A small bounded task set; never wait for a backup before redirecting."""
        if url in self.backups or len(self.backups) >= 2:
            return
        async def save():
            try:
                await self.ensure(url)
            except (web.HTTPException, OSError) as exc:
                logging.warning('Background backup unavailable: %s %s', url, exc)
        task = asyncio.create_task(save())
        self.backups[url] = task
        def done(task):
            self.backups.pop(url, None)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(done)

    async def upstream_available(self, url, external=False):
        health = self.origin_health.get(url)
        if health and time.monotonic() - health[0] < 60:
            return health[1]
        if url not in self.probes:
            if len(self.probes) >= 4:
                # Known failures keep using the backup; untested requests redirect.
                return health[1] if health else not external
            async def check():
                try:
                    async with (self.node_http if external else self.probe_http).head(url, allow_redirects=False) as response:
                        available = 200 <= response.status < 400 or response.status in (405, 501)
                except (ClientError, asyncio.TimeoutError, OSError):
                    available = False
                if len(self.origin_health) >= 2048:
                    self.origin_health.pop(next(iter(self.origin_health)))
                self.origin_health[url] = (time.monotonic(), available)
                return available
            task = asyncio.create_task(check())
            self.probes[url] = task
            task.add_done_callback(lambda _: self.probes.pop(url, None))
        # Downloads do not wait for an origin probe. A failed probe changes the
        # next request to the already verified local copy.
        return health[1] if health else not external

    def redirect(self, target, category, reason, mode='redirect'):
        return web.Response(status=307, headers={'Location': target, 'Cache-Control': 'no-store',
            'Cloudflare-CDN-Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*',
            'X-Mirror-Mode': mode, 'X-Mirror-Category': category, 'X-Mirror-Reason': reason})

    async def fallback(self, settings, path, url, category, reason, direct=False):
        # At most one federation hop; a saturated destination goes to an external source.
        node = None if direct else self.select_node(settings, category, include_local=False)
        if node:
            return self.redirect(node+'/_mirror_node'+quote(path, safe='/@+'), category, reason, 'node')
        target = source_target(settings, path, 'fallback')
        if target and await self.upstream_available(target, external=True):
            return self.redirect(target, category, reason, 'external')
        return self.redirect(url, category, reason)

    async def resource(self, request):
        settings = read_settings(self.config)
        own_origin_only = request.headers.get('X-Buggcraft-Origin-Only') == '1'
        if request.query_string:
            raise web.HTTPBadRequest(text='Queries are not supported')
        direct = request.path.startswith('/_mirror_node/')
        path = request.path[len('/_mirror_node'):] if direct else request.path
        urls = upstreams(path)
        mapped_targets = [] if own_origin_only else source_targets(settings,path,'redirect')
        if not urls and not mapped_targets:
            raise web.HTTPNotFound()
        category = 'java' if path.startswith('/java/') else await self.category(urls[0]) if urls else 'libraries'
        mode = 'cache' if own_origin_only and settings['enabled'] else settings['policies'][category]
        mapped_targets = self.quality.ranked(path, mapped_targets)
        adaptive = bool(urls and self.quality.needs_cache(path, mapped_targets))
        if adaptive and mode == 'backup' and request.method == 'GET':
            self.backup(urls[0])
        # A quality hint cannot override a category's redirect/disabled policy.
        for mapped in ([] if adaptive and mode == 'cache' else mapped_targets):
            healthy = await self.upstream_available(mapped, external=True)
            if mapped in self.origin_health and not healthy:
                continue
            if request.method=='GET':
                self.sources.hit(path,mapped)
            return self.redirect(mapped, category, 'source_mapping', 'external')
        if mapped_targets and not (adaptive and mode == 'cache'):
            if not urls:
                raise web.HTTPServiceUnavailable(headers={'Retry-After':'30'})
            return await self.fallback(settings,path,urls[0],category,'mapped_source_unavailable',direct)
        if not settings['enabled'] or mode == 'disabled':
            if own_origin_only:
                raise web.HTTPServiceUnavailable(text='Buggcraft 源站未启用，无法提供此文件。',
                                                 headers={'Retry-After':'30'})
            return self.redirect(urls[0], category, 'local_disabled')
        if mode == 'cache':
            known = await self.cached(urls[0])
            expected = None if known else await self.one('SELECT size FROM expected WHERE url=?', (urls[0],))
            planned = known['size'] if known else expected['size'] if expected and expected['size'] is not None else 1
            reason = await self.egress.reason(requested_bytes(request, planned), settings, sum(self.serving.values()))
            if reason:
                if own_origin_only:
                    raise web.HTTPServiceUnavailable(text='Buggcraft 源站当前容量或流量不足。',
                                                     headers={'Retry-After':'30'})
                return await self.fallback(settings, path, urls[0], category, reason, direct)
        if mode == 'cache' and not direct:
            target = None if own_origin_only else self.select_node(settings, category)
            if target:
                return self.redirect(target+'/_mirror_node'+quote(path, safe='/@+'), category, 'weighted_node', 'node')
        item, fallback = None, False
        if mode == 'backup':
            item = await self.cached(urls[0])
            if item:
                fallback = not await self.upstream_available(urls[0])
            self.backup(urls[0])
        if mode == 'redirect' or mode == 'backup' and not fallback:
            return web.Response(status=307, headers={
                'Location': urls[0], 'Cache-Control': 'no-store',
                'Cloudflare-CDN-Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*',
                'X-Mirror-Mode': mode, 'X-Mirror-Category': category,
                'X-Mirror-Backup': 'ready' if item else 'pending' if mode == 'backup' else 'off',
            })
        if not fallback:
            try:
                item = await self.ensure(urls[0])
            except (web.HTTPNotFound, web.HTTPInsufficientStorage, web.HTTPServiceUnavailable, web.HTTPBadGateway, web.HTTPRequestEntityTooLarge, OSError) as exc:
                if own_origin_only:
                    raise web.HTTPServiceUnavailable(text='Buggcraft 源站暂时无法取得文件，不会重定向到第三方来源。',
                                                     headers={'Retry-After':'30'}) from exc
                return await self.fallback(settings, path, urls[0], category, 'local_'+str(getattr(exc, 'status', 'io')), direct)
        immutable = bool(re.search(r'/[a-f0-9]{40}(?:/|$)', urls[0]))
        # Pin under the same lock used by eviction, rechecking after any download.
        async with self.space_lock:
            item = await self.cached(urls[0])
            if item is None:
                reason = 'cache_changed'
            else:
                reason = await self.egress.admit(requested_bytes(request, item['size']), settings, sum(self.serving.values()))
            if not reason:
                self.serving[item['object']] = self.serving.get(item['object'], 0) + 1
        if reason:
            if own_origin_only:
                raise web.HTTPServiceUnavailable(text='Buggcraft 源站当前无法提供文件。',
                                                 headers={'Retry-After':'30'})
            return await self.fallback(settings, path, urls[0], category, reason, direct)
        response = PinnedFileResponse(self.root / 'objects' / item['object'][:2] / item['object'], headers={
            'Cache-Control': 'no-store' if fallback else 'public, max-age=31536000, immutable' if immutable else 'public, max-age=300',
            'Access-Control-Allow-Origin': '*', 'Access-Control-Expose-Headers': 'X-Checksum-Sha1, X-Mirror-Verified',
            'Content-Type': item['type'], 'X-Checksum-Sha1': item['sha1'],
            'X-Mirror-Verified': 'upstream-sha1' if item['checked'] else 'local-sha256',
            'X-Content-Type-Options': 'nosniff',
            'X-Mirror-Mode': 'fallback' if fallback else 'cache', 'X-Mirror-Category': category,
        })
        response.mirror, response.digest = self, item['object']
        if time.time() - self.touched.get(item['object'], 0) > 60:
            if len(self.touched) > 4096:
                self.touched.clear()
            self.touched[item['object']] = time.time()
            await self.db.execute('UPDATE objects SET accessed=? WHERE digest=?', (time.time(), item['object']))
            await self.db.commit()
        return response

    def select_node(self, settings, category=None, include_local=True):
        candidates = [(None, settings['node_weight'])] if include_local and settings['node_weight'] else []
        for node in settings['nodes']:
            health = self.node_health.get(node['url'], {})
            if (node['enabled'] and node['url'] != settings['public_url']
                    and health.get('healthy') and health.get('accepting', True) and time.time()-health.get('at', 0)<90
                    and (category is None or health.get('policies', {}).get(category) == 'cache')):
                candidates.append((node['url'], node['weight']))
        if not candidates:
            return None
        return random.choices([c[0] for c in candidates], weights=[c[1] for c in candidates])[0]

    async def public_status(self, request):
        status = await self.status(compact=True)
        return web.json_response({**{k: status[k] for k in ('running', 'cached_bytes', 'cached_files', 'active_downloads', 'exclusions', 'policies')},
                                 'enabled': status['settings']['enabled'], 'protocol': 'buggcraft-resources-v1',
                                 'source_mappings': status['source_mappings'],
                                 'accepting': not await self.egress.reason(1, status['settings'], sum(self.serving.values())),
                                 'nodes': [dict(url=n['url'], weight=n['weight']) for n in status['nodes'] if n['healthy'] and n['enabled']]},
                                 headers={'Cache-Control': 'no-store'})

    async def control(self, request):
        if not secrets.compare_digest(request.headers.get('X-Mirror-Control', ''), self.secret):
            raise web.HTTPForbidden()
        if request.method == 'POST':
            data = await request.json()
            if not isinstance(data, dict):
                raise web.HTTPBadRequest(text='Expected a JSON object')
            action = data.get('action')
            if action == 'quality_probe':
                await self.quality.schedule(data.get('source'))
                return web.json_response(await self.status(compact=True))
            if action == 'quality_history':
                return web.json_response(dict(samples=await self.quality.history(str(data.get('source', '')))))
            if action in ('source_files','source_preview'):
                return web.json_response(await self.sources.files(data) if action=='source_files' else await self.sources.preview())
            if action == 'source_scan':
                if self.source_job and not self.source_job.done():
                    raise web.HTTPConflict(text='Source scan already running')
                self.source_job = asyncio.create_task(self.discover_sources())
                return web.json_response(await self.status(compact=True))
            if action == 'cancel':
                if self.job and not self.job.done():
                    self.job.cancel()
                    await asyncio.gather(self.job, return_exceptions=True)
            elif action == 'trim':
                async with self.space_lock:
                    settings = read_settings(self.config)
                    await self.make_space(0, settings)
                    used = (await self.one('SELECT bytes FROM totals WHERE id=1'))['bytes']
                    if used + self.reserved > settings['quota_gib'] * GIB:
                        raise web.HTTPInsufficientStorage(text='Active downloads or protected versions exceed quota')
            elif action in ('inventory', 'prefetch', 'refresh', 'source_cleanup'):
                if self.job and not self.job.done():
                    raise web.HTTPConflict(text='A mirror job is already running')
                self.job_state = dict(state='running', action=action, started_at=time.time())
                self.job = asyncio.create_task(self.run_job(action))
            else:
                raise web.HTTPBadRequest()
        return web.json_response(await self.status(compact=True), headers={'Cache-Control': 'no-store'})

    async def run_job(self, action):
        try:
            if action=='source_cleanup':
                await self.sources.cleanup()
                return
            if action in ('inventory', 'refresh'):
                self.inventory = Inventory(self.root, read_settings(self.config)['concurrency'])
                report = await self.inventory.scan(incremental=action == 'refresh')
                if not report['complete']:
                    self.job_state.update(state='incomplete', report=report)
                    return
                items = list(self.inventory.items.values())
                await self.db.executemany('INSERT OR REPLACE INTO expected VALUES(?,?,?,?)',
                                         [(i['url'], i['sha1'], i['size'], i['category']) for i in items])
                await self.db.executemany('INSERT OR REPLACE INTO priorities VALUES(?,?)',
                                         [(i['url'], i.get('priority', 0)) for i in items])
                await self.db.execute('UPDATE objects SET priority=MAX(priority,COALESCE((SELECT MAX(p.priority) FROM cache c JOIN priorities p ON p.url=c.url WHERE c.object=objects.digest),0))')
                await self.db.commit()
                if read_settings(self.config)['auto_sources']:
                    await self.discover_sources()
                self.job_state.update(state='finished', report=report)
            if action in ('prefetch', 'refresh'):
                data = json.loads(await asyncio.to_thread((self.root / 'inventory.json').read_text, encoding='utf-8'))
                if not data['report']['complete']:
                    raise ValueError('Inventory incomplete; run inventory again first')
                files = data['files']
                if action == 'refresh':
                    files = [i for i in files if i['url'] in self.inventory.changed_urls]
                quality_settings = read_settings(self.config)
                files.sort(key=lambda i: (i.get('priority', 0), self.quality.needs_cache(
                    i.get('path', ''), source_targets(quality_settings, i.get('path', ''), 'redirect'))), reverse=True)
                await self.db.executemany('INSERT OR REPLACE INTO expected VALUES(?,?,?,?)',
                                         [(i['url'], i['sha1'], i['size'], i['category']) for i in files])
                await self.db.executemany('INSERT OR REPLACE INTO priorities VALUES(?,?)',
                                         [(i['url'], i.get('priority', 0)) for i in files])
                await self.db.execute('UPDATE objects SET priority=MAX(priority,COALESCE((SELECT MAX(p.priority) FROM cache c JOIN priorities p ON p.url=c.url WHERE c.object=objects.digest),0))')
                await self.db.commit()
                # Sequential background prefetch leaves capacity for interactive downloads.
                self.job_state.update(state='running', done=0, total=len(files), errors=0, failed=[], skipped=0, cached=0)
                for item in files:
                    if not read_settings(self.config)['enabled']:
                        raise ValueError('Mirror disabled')
                    for attempt in range(3):
                        settings = read_settings(self.config)
                        category = category_for(item['url'], item['category'])
                        mapped = source_targets(settings, item.get('path', ''), 'redirect')
                        if (settings['policies'][category] not in ('cache', 'backup')
                                or mapped and not self.quality.needs_cache(item.get('path', ''), mapped)):
                            self.job_state['skipped'] += 1
                            break
                        try:
                            await self.ensure(item['url'])
                            self.job_state['cached'] += 1
                            break
                        except web.HTTPInsufficientStorage:
                            self.job_state.update(state='disk_limit')
                            return
                        except (web.HTTPException, OSError) as exc:
                            if attempt < 2 and getattr(exc, 'status', 502) not in (400, 404):
                                await asyncio.sleep(2 ** attempt)
                                continue
                            self.job_state['errors'] += 1
                            failure = dict(url=item['url'], error=str(exc)[:160])
                            self.job_state['failed'] = [*self.job_state['failed'][-19:], failure]
                            logging.warning('Mirror prefetch failed: %s %s', item['url'], exc)
                            break
                    self.job_state['done'] += 1
                if action == 'refresh' and read_settings(self.config)['policies']['loaders'] in ('cache', 'backup'):
                    # Mutable loader feeds are small and independent of the vanilla catalogue.
                    for path in ('/fabric-meta/v2/versions/loader', '/quilt-meta/v3/versions/loader',
                                 '/maven/net/minecraftforge/forge/maven-metadata.xml',
                                 '/neoforge-maven/releases/net/neoforged/neoforge/maven-metadata.xml'):
                        try:
                            await self.ensure(upstreams(path)[0])
                        except (web.HTTPException, OSError) as exc:
                            self.job_state['errors'] += 1
                            self.job_state['failed'] = [*self.job_state['failed'][-19:], dict(path=path, error=str(exc)[:160])]
                self.job_state['state'] = 'incomplete' if self.job_state['errors'] else 'finished'
        except asyncio.CancelledError:
            self.job_state['state'] = 'cancelled'
            raise
        except Exception as exc:
            logging.exception('Mirror job failed')
            self.job_state.update(state='failed', error=str(exc)[:240])
        finally:
            if action == 'refresh':
                self.last_refresh = dict(at=time.time(), **self.job_state)

    async def refresh_loop(self):
        """One refresh at a time, independent from requests and translation workers."""
        scheduled_interval = None
        last_nodes_check = 0
        last_source_check = 0
        while True:
            settings = read_settings(self.config)
            if (settings['auto_sources'] and time.time()-last_source_check>=60
                    and (self.source_job is None or self.source_job.done())):
                self.source_job = asyncio.create_task(self.discover_sources())
                last_source_check = time.time()
            if time.time() - last_nodes_check >= 20:
                async def check_node(node):
                    data = {}
                    try:
                        async with self.node_http.get(node['url']+'/mirror-api/status', allow_redirects=False) as response:
                            raw = bytearray()
                            async for chunk in response.content.iter_chunked(1024):
                                raw.extend(chunk)
                                if len(raw) > 8192:
                                    raise ValueError('node status too large')
                            data = json.loads(raw)
                            healthy = (response.status == 200 and isinstance(data, dict)
                                       and data.get('protocol') == 'buggcraft-resources-v1'
                                       and isinstance(data.get('policies'), dict)
                                       and data.get('running') is True and data.get('enabled') is not False)
                    except (ValueError, ClientError, asyncio.TimeoutError, OSError):
                        healthy = False
                    self.node_health[node['url']] = dict(healthy=healthy, at=time.time(),
                        policies=data.get('policies', {}) if isinstance(data, dict) else {},
                        accepting=data.get('accepting') is True if isinstance(data, dict) else False)
                await asyncio.gather(*(check_node(n) for n in settings['nodes'] if n['enabled'] and n['url'] != settings['public_url']))
                last_nodes_check = time.time()
            interval = settings['refresh_minutes'] * 60
            if not settings['enabled'] or not settings['auto_refresh']:
                self.next_refresh, scheduled_interval = None, None
            else:
                if scheduled_interval != interval or self.next_refresh is None:
                    scheduled_interval, self.next_refresh = interval, time.time() + interval
                if time.time() >= self.next_refresh and (not self.job or self.job.done()):
                    self.job_state = dict(state='running', action='refresh', started_at=time.time())
                    self.job = asyncio.create_task(self.run_job('refresh'))
                    self.next_refresh = time.time() + interval
            await asyncio.sleep(5)

    async def discover_sources(self):
        try:
            await self.discovery.refresh()
            await self.sources.synchronize()
            await self.sources.flush()
            if read_settings(self.config)['source_cleanup'] and (not self.job or self.job.done()):
                self.job_state = dict(state='running',action='source_cleanup',started_at=time.time())
                self.job = asyncio.create_task(self.run_job('source_cleanup'))
        except Exception:
            logging.exception('Automatic source discovery will retry')


@web.middleware
async def mirror_boundary(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return web.Response(status=exc.status, text=exc.text, headers={'Cache-Control': 'no-store',
            **{key: exc.headers[key] for key in ('Location', 'Retry-After', 'Content-Range', 'WWW-Authenticate') if key in exc.headers}})


async def create_mirror(config):
    mirror = await Mirror(config).open()
    app = web.Application(middlewares=[mirror_boundary], client_max_size=8192)
    app['mirror'] = mirror
    async def claim_node(request):
        from .room_node_access import NodeAccess
        data = await request.json()
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text='Invalid node invitation')
        try:
            credentials = await asyncio.to_thread(NodeAccess(mirror.root).claim, data.get('ticket'), data.get('node_id'))
        except ValueError as exc:
            raise web.HTTPForbidden(text=str(exc)) from None
        return web.json_response(dict(protocol='room-content-cache-v1', **credentials), headers={'Cache-Control': 'no-store'})
    app.router.add_post('/room-cache/nodes/claim', claim_node)
    app.router.add_get('/mirror-api/status', mirror.public_status)
    app.router.add_get('/_internal/control', mirror.control)
    app.router.add_post('/_internal/control', mirror.control)
    app.router.add_post('/room-cache/plan', mirror.room_cache.plan)
    app.router.add_post('/room-cache/resolve', mirror.room_cache.resolve)
    app.router.add_put('/room-cache/files/{digest:[0-9a-f]{64}}', mirror.room_cache.upload)
    app.router.add_get('/room-cache/files/{digest:[0-9a-f]{64}}', mirror.room_cache.download)
    app.router.add_get('/{path:.*}', mirror.resource)
    async def close(app):
        await mirror.close()
    app.on_cleanup.append(close)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root')
    parser.add_argument('--env', default=os.environ.get('TRANSLATION_ENV_FILE', '.env'))
    args = parser.parse_args()
    load_env(args.env)
    config = Config.from_env()
    if args.root:
        config.mirror_root = args.root
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_mirror(config), host='127.0.0.1', port=config.mirror_port, access_log=None)


if __name__ == '__main__':
    main()
