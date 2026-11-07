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
from .mirror_catalog import Inventory, MANIFEST, allowed_url, route_for, prefetch_matches_platform, selected_prefetch_platforms
from .mirror_policy import POLICIES, MODES, category_for
from .mirror_routing import ROUTING_DEFAULTS, Egress, requested_bytes, source_target, source_targets, validate_sources
from .federation import origin, PublicResolver
from .mirror_sources import BUILTIN_SOURCES, SourceDiscovery, effective_sources, source_status, validate_registries
from .mirror_source_management import SourceManagement
from .mirror_quality import QUALITY_DEFAULTS, SourceQuality
from .mirror_observation import MirrorObservation, room_files
from .room_cache import RoomCache, DEFAULTS as ROOM_CACHE_DEFAULTS, validate_settings as validate_room_cache
from .mirror_storage import LOCAL_ROUTES, StorageError, validate_cloud_settings
from .mirror_cloud import MirrorCloud

GIB = 1024 ** 3
DEFAULTS = dict(enabled=True, public_url='https://mirror.bugg.icu', quota_gib=400,
                reserve_gib=100, concurrency=4, metadata_ttl=300, policies=POLICIES,
                prefetch_platform=['windows'],
                auto_refresh=True, refresh_minutes=30, eviction='oldest_version',
                protect_latest=2, nodes=[], node_weight=1, auto_sources=True, source_prefer_cn=True,
                source_cleanup=False, source_registries=BUILTIN_SOURCES, **ROUTING_DEFAULTS, **QUALITY_DEFAULTS,
                **ROOM_CACHE_DEFAULTS, qiniu_profiles=[], qiniu_routes=LOCAL_ROUTES,
                qiniu_paused=True, qiniu_signed_url_minutes=240)


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
    routes = data.get('qiniu_routes', {})
    if not isinstance(routes, dict):
        routes = {}
    slow = data.get('quality_slow_kibps', data.get('room_cache_slow_kibps', DEFAULTS['quality_slow_kibps']))
    return {**DEFAULTS, **data, 'prefetch_platform': selected_prefetch_platforms(data.get('prefetch_platform', DEFAULTS['prefetch_platform'])),
            'quality_slow_kibps': slow, 'room_cache_slow_kibps': slow,
            'policies': {**POLICIES, **data.get('policies', {})},
            'qiniu_routes': {name: {**route, **routes.get(name, {})} for name, route in LOCAL_ROUTES.items()},
            'sources': effective_sources(config) if effective else data.get('sources', [])}


def validate_settings(data):
    if not isinstance(data, dict) or set(data) - DEFAULTS.keys():
        raise ValueError('invalid mirror settings')
    result = {**DEFAULTS, **data}
    # Keep legacy clients compatible while exposing one shared slow-speed line.
    if 'room_cache_slow_kibps' in data and 'quality_slow_kibps' not in data:
        result['quality_slow_kibps'] = data['room_cache_slow_kibps']
    result['room_cache_slow_kibps'] = result['quality_slow_kibps']
    policies = result['policies']
    if (not isinstance(policies, dict) or set(policies) - POLICIES.keys()
            or any(type(value) is not str or value not in MODES for value in policies.values())):
        raise ValueError('invalid resource policies')
    result['policies'] = {**POLICIES, **policies}
    if result['eviction'] not in ('stop', 'lru', 'oldest_version'):
        raise ValueError('invalid eviction strategy')
    result['prefetch_platform'] = selected_prefetch_platforms(result['prefetch_platform'])
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
                           ('quality_slow_kibps', 1, 102400), ('quality_content_minutes', 5, 120),
                           ('quality_backup_minutes', 5, 120), ('quality_modrinth_minutes', 5, 120)]:
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('invalid ' + key)
    if result['quality_slow_kibps'] >= result['quality_fast_kibps']:
        raise ValueError('slow speed must be below fast speed')
    validate_room_cache(result)
    result['qiniu_routes'] = {name: {**route, **result['qiniu_routes'].get(name, {})}
                              for name, route in LOCAL_ROUTES.items()}
    validate_cloud_settings(result)
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
    if re.fullmatch(r'/mods/curseforge-mediafilez/files/[0-9]+/[0-9]+/[A-Za-z0-9._ +@-]+\.(?:jar|zip|mrpack|litemod)', path):
        return ['https://mediafilez.forgecdn.net' + quote(path[len('/mods/curseforge-mediafilez'):], safe='/@+')]
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
        self.failed_urls = set()
        self.node_health = {}
        self.touched = {}
        self.serving = {}
        self.reserved = 0
        self.space_lock = asyncio.Lock()
        self.job = None
        self.scheduler = None
        self.discovery = SourceDiscovery(self.root)
        self.source_job = None
        self.source_scan_state = dict(state='idle')
        self.next_refresh = None
        self.refresh_deferred_at = None
        self.last_refresh = None
        self.job_state = dict(state='idle')
        self.inventory = None
        from .mirror_automation import AutomaticSync
        self.automation = AutomaticSync(self)
        self.started = time.time()
        self.token_path = self.root / 'control-token'
        self.sources = SourceManagement(self)
        self.quality = SourceQuality(self)
        self.observation = MirrorObservation(self)
        self.room_cache = RoomCache(self, lambda: read_settings(self.config,effective=False))
        self.cloud = MirrorCloud(self, lambda: read_settings(self.config))

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
            CREATE TABLE IF NOT EXISTS objects(digest TEXT PRIMARY KEY, size INTEGER NOT NULL, accessed REAL NOT NULL DEFAULT 0, priority REAL NOT NULL DEFAULT 0, local INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS totals(id INTEGER PRIMARY KEY CHECK(id=1), bytes INTEGER, files INTEGER);
            CREATE INDEX IF NOT EXISTS cache_object ON cache(object);
            CREATE TABLE IF NOT EXISTS priorities(url TEXT PRIMARY KEY, priority REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS priorities_order ON priorities(priority DESC);
            CREATE TABLE IF NOT EXISTS mirror_failed_files(
                url TEXT PRIMARY KEY, category TEXT NOT NULL, status INTEGER,
                error TEXT NOT NULL, attempts INTEGER NOT NULL,
                first_failed REAL NOT NULL, last_failed REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS failed_files_order ON mirror_failed_files(last_failed);
        ''')
        async with self.db.execute('PRAGMA table_info(mirror_failed_files)') as cursor:
            failure_columns = {row['name'] for row in await cursor.fetchall()}
        if 'next_retry' not in failure_columns:
            await self.db.execute('ALTER TABLE mirror_failed_files ADD COLUMN next_retry REAL NOT NULL DEFAULT 0')
        async with self.db.execute('PRAGMA table_info(objects)') as cursor:
            object_columns = {row['name'] for row in await cursor.fetchall()}
        for name in ('accessed', 'priority', 'local'):
            if name not in object_columns:
                await self.db.execute(f'ALTER TABLE objects ADD COLUMN {name} {"INTEGER NOT NULL DEFAULT 1" if name == "local" else "REAL NOT NULL DEFAULT 0"}')
        await self.db.execute('CREATE INDEX IF NOT EXISTS eviction_objects ON objects(priority,accessed)')
        await self.db.execute('CREATE INDEX IF NOT EXISTS eviction_access ON objects(accessed)')
        async with self.db.execute('SELECT url FROM mirror_failed_files') as cursor:
            self.failed_urls = {row['url'] for row in await cursor.fetchall()}
        if not await self.one('SELECT id FROM totals WHERE id=1'):
            await self.db.execute('INSERT OR IGNORE INTO objects(digest,size,accessed) SELECT object,MAX(size),MAX(fetched) FROM cache GROUP BY object')
            await self.db.execute('INSERT INTO totals VALUES(1,(SELECT COALESCE(SUM(size),0) FROM objects),(SELECT COUNT(*) FROM cache))')
        await self.db.commit()
        await self.observation.open()
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
        await self.cloud.open()
        await self.room_cache.open()
        self.cloud.start()
        self.scheduler = asyncio.create_task(self.refresh_loop())
        return self

    async def close(self):
        await self.cloud.close()
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

    async def remember_failure(self, url, exc):
        detail = (exc.text if isinstance(exc, web.HTTPException) else str(exc))[:240]
        await self.db.execute('''INSERT INTO mirror_failed_files
            (url,category,status,error,attempts,first_failed,last_failed)
            VALUES(?,?,?,?,1,?,?) ON CONFLICT(url) DO UPDATE SET
            status=excluded.status,error=excluded.error,attempts=attempts+1,
            last_failed=excluded.last_failed''',
            (url, category_for(url), getattr(exc, 'status', None), detail, time.time(), time.time()))
        failure = await self.one('SELECT attempts,status FROM mirror_failed_files WHERE url=?', (url,))
        delay = min(21600, (1800 if failure['status'] in (404, 410) else 60) * 2**min(failure['attempts']-1, 8))
        await self.db.execute('UPDATE mirror_failed_files SET next_retry=? WHERE url=?', (time.time()+delay+secrets.randbelow(max(1, delay//5)), url))
        await self.db.commit()
        self.failed_urls.add(url)

    async def clear_failure(self, url):
        if url not in self.failed_urls:
            return
        await self.db.execute('DELETE FROM mirror_failed_files WHERE url=?', (url,))
        await self.db.commit()
        self.failed_urls.discard(url)

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
                    refresh=dict(next_at=self.next_refresh, last=self.last_refresh,
                                 deferred_at=self.refresh_deferred_at),
                    automation=dict(self.automation.state),
                    automatic_sources=source_status(self.root),
                    source_statistics=await self.sources.summary(),
                    source_quality=await self.quality.snapshot(),
                    room_client_cache=await self.room_cache.snapshot(),
                    cloud_storage=await self.cloud.snapshot(),
                    operations=await self.observation.snapshot(),
                    egress=await self.egress.snapshot(settings),
                    accepting=not await self.egress.reason(1, settings, sum(self.serving.values())),
                    nodes=[dict(n, healthy=self.node_health.get(n['url'], {}).get('healthy', False)) for n in settings['nodes']],
                    root=str(self.root), scope='official-game-assets-libraries-java-loaders',
                    exclusions=[*(['mods'] if read_settings(self.config)['policies']['mods'] == 'disabled' else []),
                                'modpacks', 'resourcepacks', 'shaders'],
                    unavailable=['OptiFine redistribution', 'legacy Oracle Java', 'LiteLoader API', 'BMCLAPI Forge query API'])

    async def cached(self, url):
        item = await self.one('SELECT * FROM cache WHERE url=?', (url,))
        if item and ((self.root / 'objects' / item['object'][:2] / item['object']).is_file()
                     or await self.cloud.download_url('mirror', item['object'])):
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
            async with self.db.execute('SELECT digest,size,priority,accessed FROM objects WHERE local=1 AND (? IS NULL OR priority<?) AND (? IS NULL OR priority<=?)'+clause+' ORDER BY '+order+' LIMIT 256', args) as cursor:
                candidates = await cursor.fetchall()
            if not candidates:
                break
            after = tuple(candidates[-1][column] for column in columns)
            for item in candidates:
                digest = item['digest']
                if self.serving.get(digest) or self.cloud.uploading(digest):
                    continue
                file = self.root/'objects'/digest[:2]/digest
                try:
                    await asyncio.to_thread(file.unlink, missing_ok=True)
                except OSError:
                    continue
                if await self.cloud.location('mirror', digest):
                    await self.db.execute('UPDATE objects SET local=0 WHERE digest=?', (digest,))
                    await self.db.execute('UPDATE totals SET bytes=MAX(bytes-?,0) WHERE id=1', (item['size'],))
                    needed -= item['size']
                    if needed <= 0:
                        break
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

    async def alternate_download(self, url, settings, original):
        path = route_for(url)
        mapped = self.quality.ranked(path, source_targets(settings, path, 'redirect'))[:2]
        peer = self.select_node(settings, category_for(url), include_local=False)
        targets = [(target, '来源映射') for target in mapped if target != url]
        if peer:
            targets.append((peer + '/_mirror_node' + quote(path, safe='/@+'), '协作节点'))
        failures = []
        for target, kind in targets:
            try:
                return await self.download(url, target)
            except (web.HTTPException, ClientError, asyncio.TimeoutError, OSError) as exc:
                detail = exc.text if isinstance(exc, web.HTTPException) else str(exc)
                failures.append(f'{kind} {urlsplit(target).hostname}: HTTP {getattr(exc, "status", "连接错误")} {detail[:80]}')
        if failures:
            initial = original.text if isinstance(original, web.HTTPException) else str(original)
            raise web.HTTPBadGateway(text=f'主源失败：{initial[:100]}；替代来源也失败：{"; ".join(failures)[:220]}') from original
        return None

    async def download(self, url, fetch_url=None, allow_alternates=True):
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
        category = category_for(url, expected['category'] if expected else None)
        route = settings['qiniu_routes'][category]
        part = self.root / 'tmp' / (secrets.token_hex(16) + '.part')
        reservation = 0
        progress = dict(bytes=0, total=None, started_at=time.time(), category=category,
                        source_host=urlsplit(fetch_url or url).hostname,
                        destination=route['mode'], profile=route['profile'])
        self.observation.transfers[url] = progress
        try:
            async with self.http.get(fetch_url or url, allow_redirects=False) as response:
                if response.status == 404:
                    raise web.HTTPNotFound(text='上游返回 HTTP 404：文件不存在或路径已变更')
                if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise web.HTTPBadGateway(text=f'上游返回 HTTP {response.status}；重定向或压缩响应不能作为原文件缓存')
                size = response.content_length
                progress['total'] = size
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
                        progress['bytes'] = written
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
                        previous = await self.one('SELECT local FROM objects WHERE digest=?', (digest,))
                        if not previous['local']:
                            await self.db.execute('UPDATE totals SET bytes=bytes+? WHERE id=1', (written,))
                        await self.db.execute('UPDATE objects SET local=1,priority=MAX(priority,?) WHERE digest=?', (priority['priority'] if priority else 0, digest))
                    if not await self.one('SELECT url FROM cache WHERE url=?', (url,)):
                        await self.db.execute('UPDATE totals SET files=files+1 WHERE id=1')
                    await self.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?,?)',
                                          (url, digest, written, h1.hexdigest(), int(bool(sha1)), time.time(), content_type))
                    await self.db.commit()
                await self.remember_metadata(url, dest)
                try:
                    await self.cloud.enqueue('mirror', digest, written, category)
                except Exception:
                    logging.exception('Cloud copy could not be queued; local file remains available')
                return await self.cached(url)
        except web.HTTPException as exc:
            if fetch_url is None and allow_alternates and reservation == 0 and exc.status in (404, 502):
                alternate = await self.alternate_download(url, settings, exc)
                if alternate is not None:
                    return alternate
            raise
        except (ClientError, asyncio.TimeoutError) as exc:
            if old:
                return old
            if fetch_url is None and allow_alternates and reservation == 0:
                alternate = await self.alternate_download(url, settings, exc)
                if alternate is not None:
                    return alternate
            raise web.HTTPBadGateway(text=f'连接上游失败：{type(exc).__name__}: {str(exc)[:120]}') from exc
        finally:
            self.observation.transfers.pop(url, None)
            part.unlink(missing_ok=True)
            async with self.space_lock:
                self.reserved -= reservation

    async def ensure(self, url, *, allow_alternates=True):
        old = await self.cached(url)
        mutable = mutable_metadata(url)
        if old and (not mutable or time.time() - old['fetched'] < read_settings(self.config)['metadata_ttl']):
            return old
        key = (url, allow_alternates)
        if key not in self.inflight:
            if len(self.inflight) >= read_settings(self.config)['concurrency']:
                raise web.HTTPServiceUnavailable(headers={'Retry-After': '3'}, text='Download queue busy')
            task = asyncio.create_task(self.download(url, allow_alternates=allow_alternates))
            self.inflight[key] = task
            def finished(task):
                self.inflight.pop(key, None)
                if not task.cancelled():
                    task.exception()
            task.add_done_callback(finished)
        return await asyncio.shield(self.inflight[key])

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
        if category == 'mods' and not direct and settings['enabled']:
            public = await self.room_cache.fast_public_source(url)
            if public:
                return self.redirect(public, category, 'catalog_public_source')
        # At most one federation hop; a saturated destination goes to an external source.
        node = None if direct or settings['qiniu_routes'][category]['mode'] == 'cloud' else self.select_node(settings, category, include_local=False)
        if node:
            return self.redirect(node+'/_mirror_node'+quote(path, safe='/@+'), category, reason, 'node')
        target = source_target(settings, path, 'fallback')
        if target and await self.upstream_available(target, external=True):
            return self.redirect(target, category, reason, 'external')
        # NeoForge's publisher can be unreachable from domestic clients. The
        # enabled Minecraft repositories use /maven/, not our loader alias.
        # This fixed artifact mapping needs no per-download API/latency probe.
        # Explicit file mappings above continue to take precedence.
        suffix = path[len('/neoforge-maven/releases/'):] if path.startswith('/neoforge-maven/releases/') else (
            path[len('/maven/'):] if path.startswith('/maven/') else '')
        neo = re.fullmatch(r'net/neoforged/neoforge/([A-Za-z0-9._-]+)/neoforge-\1-installer\.jar', suffix)
        if (not direct and category == 'loaders' and neo and settings['auto_sources']
                and settings['source_prefer_cn']):
            candidates = [row['url'] + 'maven/' + suffix for row in settings['source_registries']
                          if row['enabled'] and row['kind'] == 'minecraft' and row['region'] == 'cn']
            for candidate in self.quality.ranked(path, candidates):
                await self.upstream_available(candidate, external=True)
                if candidate in self.origin_health and not self.origin_health[candidate][1]:
                    continue
                return self.redirect(candidate, category, reason, 'external')
        return self.redirect(url, category, reason)

    async def resource(self, request):
        settings = read_settings(self.config)
        requested_origin_only = request.headers.get('X-Buggcraft-Origin-Only') == '1'
        if request.query_string:
            raise web.HTTPBadRequest(text='Queries are not supported')
        direct = request.path.startswith('/_mirror_node/')
        path = request.path[len('/_mirror_node'):] if direct else request.path
        urls = upstreams(path)
        # Pack archives route to their publisher without occupying mirror
        # storage. A resource pack may also be ZIP, so only a caller-provided
        # pack hint or the unambiguous .mrpack extension selects this path.
        is_pack = path.lower().endswith('.mrpack') or (
            path.lower().endswith('.zip') and request.headers.get('X-Buggcraft-Pack-Archive') == '1')
        mapped_targets = [] if direct else source_targets(settings,path,'redirect')
        if urls and path.startswith('/mods/') and is_pack:
            # Keep whole archives out of storage, but honour an operator's
            # explicit redirect mapping instead of sending every pack back
            # to a publisher that may be unreachable for this player.
            target = self.quality.ranked(path, mapped_targets)[0] if mapped_targets else urls[0]
            return self.redirect(target, 'mods', 'pack_archive_redirect',
                                 'external' if mapped_targets else 'redirect')
        if not urls and not mapped_targets:
            raise web.HTTPNotFound()
        category = 'java' if path.startswith('/java/') else await self.category(urls[0]) if urls else 'libraries'
        mode = settings['policies'][category]
        # An older launcher may send this header for every resource. It must
        # not override the operator's redirect/disabled/backup policy.
        # A cloud-only route is intentionally served by a verified object-storage
        # redirect. An older launcher's origin-only hint must not force a local copy.
        own_origin_only = (requested_origin_only and settings['enabled'] and mode == 'cache'
                           and settings['qiniu_routes'][category]['mode'] != 'cloud')
        if own_origin_only:
            mapped_targets = []
        fast_mod_url = (await self.room_cache.fast_public_source(urls[0], preferred_only=mode in ('cache','backup'))
                        if settings['enabled'] and category == 'mods' and not own_origin_only and urls else None)
        if fast_mod_url:
            # The operator's hash-matched catalogue can direct the player to
            # a fast source without fetching the file through this server.
            if mode == 'backup' and request.method == 'GET':
                self.backup(urls[0])
            return self.redirect(fast_mod_url, category, 'catalog_fast_source')
        if category == 'mods' and mode == 'cache' and urls:
            room_copy = await self.room_cache.cached_catalog_url(urls[0])
            if room_copy:
                return self.redirect(settings['public_url'] + room_copy, category,
                                     'verified_room_file', 'cache')
        mapped_targets = self.quality.ranked(path, mapped_targets)
        adaptive = bool(settings['enabled'] and urls and self.quality.needs_cache(path, mapped_targets))
        adaptive_copy = await self.cached(urls[0]) if adaptive and mode == 'cache' else None
        if adaptive and mode == 'cache' and not adaptive_copy and request.method == 'GET':
            # Current downloads keep using a working source while the bounded
            # backup queue prepares acceleration for subsequent requests.
            self.backup(urls[0])
        if adaptive and mode == 'backup' and request.method == 'GET':
            self.backup(urls[0])
        # A quality hint cannot override a category's redirect/disabled policy.
        for mapped in ([] if adaptive_copy else mapped_targets):
            healthy = await self.upstream_available(mapped, external=True)
            if mapped in self.origin_health and not healthy:
                continue
            if request.method=='GET':
                self.sources.hit(path,mapped)
                if settings['enabled'] and mode == 'backup' and urls:
                    self.backup(urls[0])
            return self.redirect(mapped, category, 'source_mapping', 'external')
        if mapped_targets and not adaptive_copy:
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
            if not known and settings['qiniu_routes'][category]['mode'] == 'cloud':
                indexed = await self.one('SELECT * FROM cache WHERE url=?', (urls[0],))
                if indexed and await self.cloud.location('mirror', indexed['object']):
                    known = indexed
            if known and settings['qiniu_routes'][category]['mode'] == 'cloud' and not own_origin_only:
                cloud_url = await self.cloud.download_url('mirror', known['object'],
                    reserve=request.method != 'HEAD', method=request.method)
                if cloud_url:
                    return self.redirect(cloud_url, category, 'verified_cloud_copy', 'cloud')
                # Existing local files are migrated on demand after a route changes.
                if (self.root / 'objects' / known['object'][:2] / known['object']).is_file():
                    try:
                        await self.cloud.enqueue('mirror', known['object'], known['size'], category)
                    except StorageError:
                        logging.warning('Cloud route unavailable for %s', category)
                else:
                    # A cloud budget or health failure must not redownload a
                    # verified object merely to recreate a local staging copy.
                    return await self.fallback(settings, path, urls[0], category, 'cloud_unavailable', direct)
            if settings['qiniu_routes'][category]['mode'] == 'cloud':
                # Cloud preparation must not keep the player's request waiting
                # for a complete origin download and a later upload.
                if request.method == 'GET':
                    self.backup(urls[0])
                return await self.fallback(settings, path, urls[0], category, 'cloud_upload_pending', direct)
            stale = bool(known and mutable_metadata(urls[0])
                         and time.time() - known['fetched'] >= settings['metadata_ttl'])
            expected = None if known else await self.one('SELECT size FROM expected WHERE url=?', (urls[0],))
            planned = known['size'] if known else expected['size'] if expected and expected['size'] is not None else 1
            reason = (await self.egress.reason(requested_bytes(request, planned), settings, sum(self.serving.values()))
                      if settings['qiniu_routes'][category]['mode'] != 'cloud' else None)
            if reason:
                if own_origin_only:
                    raise web.HTTPServiceUnavailable(text='Buggcraft 源站当前容量或流量不足。',
                                                     headers={'Retry-After':'30'})
                if known:
                    cloud_url = await self.cloud.download_url('mirror', known['object'], reserve=request.method != 'HEAD', method=request.method)
                    if cloud_url:
                        return self.redirect(cloud_url, category, reason, 'cloud')
                return await self.fallback(settings, path, urls[0], category, reason, direct)
        if mode == 'cache' and not direct and settings['qiniu_routes'][category]['mode'] != 'cloud':
            target = None if own_origin_only else self.select_node(settings, category)
            if target:
                return self.redirect(target+'/_mirror_node'+quote(path, safe='/@+'), category, 'weighted_node', 'node')
        if mode == 'cache' and not own_origin_only and (not known or stale):
            # Player downloads must not queue behind cache population or
            # metadata refresh. The bounded, deduplicated background queue
            # prepares later requests while this one uses a working source.
            if request.method == 'GET':
                self.backup(urls[0])
            return await self.fallback(settings, path, urls[0], category,
                                       'cache_refresh_pending' if stale else 'cache_miss', direct)
        item, fallback = None, False
        if mode == 'backup':
            item = await self.cached(urls[0])
            if not item and settings['qiniu_routes'][category]['mode'] == 'cloud':
                indexed = await self.one('SELECT * FROM cache WHERE url=?', (urls[0],))
                if indexed and await self.cloud.location('mirror', indexed['object']):
                    item = indexed
            if item:
                fallback = (self.quality.content_metric(urls[0])['health'] in ('slow', 'unavailable')
                            or not await self.upstream_available(urls[0]))
            if not item or (self.root / 'objects' / item['object'][:2] / item['object']).is_file():
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
                item = await self.ensure(urls[0], allow_alternates=not (direct or own_origin_only))
            except (web.HTTPNotFound, web.HTTPInsufficientStorage, web.HTTPServiceUnavailable, web.HTTPBadGateway, web.HTTPRequestEntityTooLarge, OSError) as exc:
                if own_origin_only:
                    raise web.HTTPServiceUnavailable(text='Buggcraft 源站暂时无法取得文件，不会重定向到第三方来源。',
                                                     headers={'Retry-After':'30'}) from exc
                return await self.fallback(settings, path, urls[0], category, 'local_'+str(getattr(exc, 'status', 'io')), direct)
        local_path = self.root / 'objects' / item['object'][:2] / item['object']
        cloud_url = None
        if not own_origin_only and (settings['qiniu_routes'][category]['mode'] == 'cloud' or not local_path.is_file()):
            cloud_url = await self.cloud.download_url('mirror', item['object'], reserve=request.method != 'HEAD', method=request.method) if item else None
            if cloud_url:
                return self.redirect(cloud_url, category, 'verified_cloud_copy', 'cloud')
        if settings['qiniu_routes'][category]['mode'] == 'cloud':
            # This copy is only a staging file until cloud verification completes.
            # Never route cloud-only downloads through the local egress budget.
            return await self.fallback(settings, path, urls[0], category, 'cloud_upload_pending', direct)
        if not local_path.is_file():
            if own_origin_only:
                raise web.HTTPServiceUnavailable(text='Buggcraft 源站暂无此文件的本地副本。', headers={'Retry-After':'30'})
            return await self.fallback(settings, path, urls[0], category, 'cloud_unavailable', direct)
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
            cloud_url = await self.cloud.download_url('mirror', item['object'], reserve=request.method != 'HEAD', method=request.method)
            if cloud_url:
                return self.redirect(cloud_url, category, reason, 'cloud')
            return await self.fallback(settings, path, urls[0], category, reason, direct)
        response = PinnedFileResponse(local_path, headers={
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
            data = await request.clone(client_max_size=2 * 1024 * 1024).json()
            if not isinstance(data, dict):
                raise web.HTTPBadRequest(text='Expected a JSON object')
            action = data.get('action')
            if action == 'inventory_files':
                try:
                    result = await self.observation.inventory_view.files(data)
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(result, headers={'Cache-Control': 'no-store'})
            if action == 'content_files':
                try:
                    result = await self.room_cache.public_files(data)
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(result,headers={'Cache-Control':'no-store'})
            if action in ('cloud_cleanup_preview', 'cloud_cleanup_local'):
                try:
                    result = await (self.cloud.local_cleanup_candidates(after=data.get('after', ''))
                        if action == 'cloud_cleanup_preview' else self.cloud.cleanup_local(data.get('digests')))
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(result, headers={'Cache-Control': 'no-store'})
            if action == 'room_catalog_import':
                try:
                    result = await self.room_cache.import_catalog(data)
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(result, headers={'Cache-Control': 'no-store'})
            if action in ('cache_files', 'room_files'):
                try:
                    result = await (self.observation.files(data) if action == 'cache_files' else room_files(self.room_cache, data))
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(result, headers={'Cache-Control': 'no-store'})
            if action == 'cloud_jobs':
                try:
                    result = await self.cloud.jobs_page(data.get('route', ''), data.get('state', ''), data.get('offset', 0))
                except ValueError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(result, headers={'Cache-Control': 'no-store'})
            if action == 'cloud_migrate':
                try:
                    queued = await self.cloud.migrate(data['route'], data['limit'])
                except (KeyError, ValueError) as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(dict(queued=queued, cloud_storage=await self.cloud.snapshot()), headers={'Cache-Control': 'no-store'})
            if action == 'cloud_retry':
                return web.json_response(dict(retried=await self.cloud.retry(data['job_id']), cloud_storage=await self.cloud.snapshot()), headers={'Cache-Control': 'no-store'})
            if action == 'cloud_tls_issue':
                try:
                    self.cloud.tls.schedule(data['profile'])
                except (KeyError, StorageError) as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(dict(ok=True, cloud_storage=await self.cloud.snapshot()), headers={'Cache-Control': 'no-store'})
            if action in ('cloud_probe', 'cloud_test'):
                try:
                    storage = self.cloud.storage(data['profile'])
                    result = await (storage.test() if action == 'cloud_test' else storage.probe())
                    if result and action == 'cloud_probe':
                        item = await self.one("SELECT object_key FROM object_locations WHERE profile=? AND state='ready' ORDER BY verified_at DESC LIMIT 1", (data['profile'],))
                        if item:
                            result = await self.cloud.check_download(data['profile'], item['object_key'])
                except StorageError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(dict(ok=result, cloud_storage=await self.cloud.snapshot()), headers={'Cache-Control': 'no-store'})
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
            elif action in ('inventory', 'prefetch', 'refresh', 'retry_failures', 'source_cleanup'):
                if self.job and not self.job.done():
                    raise web.HTTPConflict(text='A mirror job is already running')
                self.job_state = dict(state='running', action=action, trigger='manual', started_at=time.time())
                self.job = asyncio.create_task(self.run_job(action))
            else:
                raise web.HTTPBadRequest()
        return web.json_response(await self.status(compact=True), headers={'Cache-Control': 'no-store'})

    async def run_job(self, action):
        await self.observation.record()
        try:
            await self._run_job(action)
        finally:
            self.job_state['finished_at'] = time.time()
            await self.observation.record()

    async def _run_job(self, action):
        try:
            if action=='source_cleanup':
                await self.sources.cleanup()
                return
            if action in ('inventory', 'refresh'):
                self.job_state['phase'] = 'catalogue'
                self.inventory = Inventory(self.root, read_settings(self.config)['concurrency'])
                report = await self.inventory.scan(incremental=action == 'refresh')
                if not report['complete'] and action == 'inventory':
                    self.job_state.update(state='incomplete', report=report)
                    return
                if not report['complete']:
                    self.job_state['catalog_errors'] = report['errors'][:20]
                items = list(self.inventory.items.values())
                await self.db.executemany('INSERT OR REPLACE INTO expected VALUES(?,?,?,?)',
                                         [(i['url'], i['sha1'], i['size'], i['category']) for i in items])
                await self.db.executemany('INSERT OR REPLACE INTO priorities VALUES(?,?)',
                                         [(i['url'], i.get('priority', 0)) for i in items])
                await self.db.execute('UPDATE objects SET priority=MAX(priority,COALESCE((SELECT MAX(p.priority) FROM cache c JOIN priorities p ON p.url=c.url WHERE c.object=objects.digest),0))')
                await self.db.commit()
                if read_settings(self.config)['auto_sources']:
                    self.job_state['phase'] = 'sources'
                    await self.discover_sources()
                self.job_state.update(state='finished', report=report)
            if action in ('prefetch', 'refresh', 'retry_failures', 'automatic'):
                self.job_state['phase'] = 'selection'
                if action == 'retry_failures':
                    async with self.db.execute(
                            'SELECT url FROM mirror_failed_files WHERE next_retry<=? ORDER BY next_retry ASC LIMIT 100', (time.time(),)) as cursor:
                        failures = [row['url'] for row in await cursor.fetchall()]
                    files = []
                    for url in failures:
                        if not url or not allowed_url(url):
                            await self.clear_failure(url)
                            continue
                        known = await self.one('SELECT sha1,size,category FROM expected WHERE url=?', (url,))
                        files.append(dict(url=url, path=route_for(url), category=known['category'] if known else category_for(url),
                                          priority=0, size=known['size'] if known else None,
                                          sha1=known['sha1'] if known else None))
                elif action == 'refresh':
                    files = [i for i in self.inventory.items.values() if i['url'] in self.inventory.changed_urls]
                else:
                    data = json.loads(await asyncio.to_thread((self.root / 'inventory.json').read_text, encoding='utf-8'))
                    if not data['report']['complete'] and action != 'automatic':
                        raise ValueError('Inventory incomplete; run inventory again first')
                    files = data['files']
                quality_settings = read_settings(self.config)
                if action == 'automatic':
                    files = await self.automation.batch(files, quality_settings)
                from .mirror_automation import prefetch_exclusion
                if action == 'retry_failures' and (self.root/'inventory.json').exists():
                    inventory = json.loads(await asyncio.to_thread((self.root/'inventory.json').read_text, encoding='utf-8'))
                    known_items = {item['url']:item for item in inventory.get('files',[])}
                    files = [known_items.get(item['url'], item) for item in files]
                before_platform_filter = len(files)
                excluded = [item for item in files if prefetch_exclusion(item, quality_settings)]
                if action == 'retry_failures':
                    for item in excluded:
                        await self.clear_failure(item['url'])
                files = [item for item in files if not prefetch_exclusion(item, quality_settings)]
                self.job_state['platform_skipped'] = before_platform_filter - len(files)
                self.job_state['prefetch_platform'] = quality_settings['prefetch_platform']
                files.sort(key=lambda i: (i.get('priority', 0), self.quality.needs_cache(
                    i.get('path', ''), source_targets(quality_settings, i.get('path', ''), 'redirect'))), reverse=True)
                await self.db.executemany('INSERT OR REPLACE INTO expected VALUES(?,?,?,?)',
                                         [(i['url'], i['sha1'], i['size'], i['category']) for i in files])
                await self.db.executemany('INSERT OR REPLACE INTO priorities VALUES(?,?)',
                                         [(i['url'], i.get('priority', 0)) for i in files])
                await self.db.execute('UPDATE objects SET priority=MAX(priority,COALESCE((SELECT MAX(p.priority) FROM cache c JOIN priorities p ON p.url=c.url WHERE c.object=objects.digest),0))')
                await self.db.commit()
                plan = {name: dict(files=0, known_bytes=0) for name in POLICIES}
                for item in files:
                    entry = plan[category_for(item['url'], item['category'])]
                    entry['files'] += 1
                    entry['known_bytes'] += item.get('size') or 0
                self.job_state.update(state='running', phase='download', done=len(excluded), total=len(files)+len(excluded), errors=0,
                                      failed=[], skipped=len(excluded), cached=0, reused=0, plan=plan,
                                      skip_reasons=dict(redirect_policy=sum(prefetch_exclusion(item, quality_settings)=='redirect_policy' for item in excluded), source_mapping=0, cloud_paused=0))
                remaining = iter(files)
                disk_limited = False
                async def fetch_item(item):
                    nonlocal disk_limited
                    if not read_settings(self.config)['enabled']:
                        raise ValueError('Mirror disabled')
                    for attempt in range(3):
                        settings = read_settings(self.config)
                        category = category_for(item['url'], item['category'])
                        if settings['qiniu_routes'][category]['mode'] == 'cloud' and settings['qiniu_paused']:
                            self.job_state['skipped'] += 1
                            self.job_state['skip_reasons']['cloud_paused'] += 1
                            break
                        mapped = source_targets(settings, item.get('path', ''), 'redirect')
                        if (settings['policies'][category] not in ('cache', 'backup')
                                or mapped and not self.quality.needs_cache(item.get('path', ''), mapped)):
                            self.job_state['skipped'] += 1
                            reason = ('redirect_policy' if settings['policies'][category] not in ('cache', 'backup')
                                      else 'source_mapping')
                            self.job_state['skip_reasons'][reason] += 1
                            await self.clear_failure(item['url'])
                            break
                        try:
                            existing = await self.cached(item['url'])
                            await self.ensure(item['url'])
                            await self.clear_failure(item['url'])
                            self.job_state['cached'] += 1
                            if existing:
                                self.job_state['reused'] += 1
                            break
                        except web.HTTPInsufficientStorage as exc:
                            disk_limited = True
                            self.job_state['error'] = '本机暂存达到容量或磁盘保留线，未处理文件留待下次同步。' + (exc.text or '')
                            return
                        except (web.HTTPException, OSError) as exc:
                            if attempt < 2 and getattr(exc, 'status', 502) not in (400, 404):
                                await asyncio.sleep(2 ** attempt)
                                continue
                            self.job_state['errors'] += 1
                            await self.remember_failure(item['url'], exc)
                            failure = dict(url=item['url'], status=getattr(exc, 'status', None),
                                           error=(exc.text if isinstance(exc, web.HTTPException) else str(exc))[:180])
                            self.job_state['failed'] = [*self.job_state['failed'][-19:], failure]
                            logging.warning('Mirror prefetch failed: %s %s', item['url'], exc)
                            break
                    self.job_state['done'] += 1
                async def worker():
                    while not disk_limited:
                        item = next(remaining, None)
                        if item is None:
                            return
                        await fetch_item(item)
                # Keep two slots available for interactive downloads when possible.
                worker_count = max(1, read_settings(self.config)['concurrency'] - 2)
                await asyncio.gather(*(worker() for _ in range(worker_count)))
                if disk_limited:
                    self.job_state['state'] = 'disk_limit'
                    self.automation.wait_capacity()
                    return
                if action == 'refresh' and read_settings(self.config)['policies']['loaders'] in ('cache', 'backup'):
                    self.job_state['phase'] = 'loader_metadata'
                    # Mutable loader feeds are small and independent of the vanilla catalogue.
                    async def refresh_loader(path):
                        try:
                            url = upstreams(path)[0]
                            await self.ensure(url)
                            await self.clear_failure(url)
                        except (web.HTTPException, OSError) as exc:
                            self.job_state['errors'] += 1
                            await self.remember_failure(url, exc)
                            self.job_state['failed'] = [*self.job_state['failed'][-19:], dict(path=path,
                                status=getattr(exc, 'status', None),
                                error=(exc.text if isinstance(exc, web.HTTPException) else str(exc))[:180])]
                    await asyncio.gather(*(refresh_loader(path) for path in (
                        '/fabric-meta/v2/versions/loader', '/quilt-meta/v3/versions/loader',
                        '/maven/net/minecraftforge/forge/maven-metadata.xml',
                        '/neoforge-maven/releases/net/neoforged/neoforge/maven-metadata.xml')))
                self.job_state['state'] = 'incomplete' if self.job_state['errors'] or self.job_state.get('catalog_errors') else 'finished'
        except asyncio.CancelledError:
            self.job_state['state'] = 'cancelled'
            raise
        except Exception as exc:
            logging.exception('Mirror job failed')
            self.job_state.update(state='failed', error=str(exc)[:240])
            self.automation.state['next_at'] = time.time()+300
        finally:
            if action == 'refresh':
                self.last_refresh = dict(at=time.time(), **self.job_state)

    async def refresh_loop(self):
        """One refresh at a time, independent from requests and translation workers."""
        scheduled_interval = None
        last_nodes_check = 0
        last_source_check = 0
        last_settings_stamp = None
        while True:
            settings = read_settings(self.config)
            try:
                stamp = settings_path(self.config).stat().st_mtime_ns
            except OSError:
                stamp = 0
            if last_settings_stamp != stamp:
                self.automation.state['next_at'] = 0
                last_settings_stamp = stamp
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
                    scheduled_interval, self.next_refresh = interval, time.time() + (interval if (self.root/'inventory.json').exists() else 0)
                if time.time() >= self.next_refresh:
                    if self.job and not self.job.done():
                        self.refresh_deferred_at = time.time()
                        self.next_refresh = time.time() + interval
                    else:
                        self.refresh_deferred_at = None
                        self.job_state = dict(state='running', action='refresh', trigger='scheduled', started_at=time.time())
                        self.job = asyncio.create_task(self.run_job('refresh'))
                        self.next_refresh = time.time() + interval
                if not self.job or self.job.done():
                    due = await self.one('SELECT 1 FROM mirror_failed_files WHERE next_retry<=? LIMIT 1', (time.time(),))
                    capacity_wait = (self.automation.state.get('state') == 'waiting_capacity'
                                     and time.time() < self.automation.state['next_at'])
                    action = ('retry_failures' if due and not capacity_wait else
                              'automatic' if time.time() >= self.automation.state['next_at']
                              and (self.root/'inventory.json').exists() else None)
                    if action:
                        self.job_state = dict(state='running', action=action, trigger='automatic', started_at=time.time())
                        self.job = asyncio.create_task(self.run_job(action))
            await asyncio.sleep(5)

    async def discover_sources(self):
        self.source_scan_state = dict(state='running', started_at=time.time())
        try:
            await self.discovery.refresh()
            await self.sources.synchronize()
            await self.sources.flush()
            self.source_scan_state.update(state='finished', finished_at=time.time())
            if read_settings(self.config)['source_cleanup'] and (not self.job or self.job.done()):
                self.job_state = dict(state='running',action='source_cleanup',started_at=time.time())
                self.job = asyncio.create_task(self.run_job('source_cleanup'))
        except Exception as exc:
            self.source_scan_state.update(state='failed', finished_at=time.time(), error=str(exc)[:160])
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
    app.router.add_post('/room-cache/release', mirror.room_cache.release)
    app.router.add_post('/room-cache/uploads/complete', mirror.cloud.direct.complete)
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
