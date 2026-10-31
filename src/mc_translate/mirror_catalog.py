"""Official-source inventory. Shared by the isolated mirror and its offline CLI."""
import asyncio
import hashlib
import json
import re
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, unquote

from aiohttp import ClientSession, ClientTimeout, TCPConnector

MANIFEST = 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'
JAVA = 'https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json'
HOSTS = {
    'piston-meta.mojang.com', 'launchermeta.mojang.com', 'launcher.mojang.com',
    'piston-data.mojang.com', 'resources.download.minecraft.net', 'libraries.minecraft.net',
    'maven.minecraftforge.net', 'files.minecraftforge.net', 'maven.neoforged.net',
    'meta.fabricmc.net', 'maven.fabricmc.net', 'meta.quiltmc.org', 'maven.quiltmc.org',
    'authlib-injector.yushi.moe',
    'cdn.modrinth.com', 'edge.forgecdn.net', 'mediafilez.forgecdn.net',
}
PRIORITY = ContextVar('mirror_version_priority', default=0)


def allowed_url(url):
    u = urlsplit(url)
    return (u.scheme == 'https' and u.hostname in HOSTS and u.port in (None, 443)
            and not (u.username or u.password or u.fragment or u.query)
            and not any(part in ('.', '..') for part in unquote(u.path).split('/'))
            and not re.search(r'%(?!20|40)', u.path, re.I)
            and bool(re.fullmatch(r'[A-Za-z0-9/_.+ @%\-]+', u.path)))


def route_for(url):
    if not allowed_url(url):
        raise ValueError('unsupported official URL')
    u = urlsplit(url)
    prefix = {
        'resources.download.minecraft.net': '/assets',
        'libraries.minecraft.net': '/maven',
        'maven.minecraftforge.net': '/maven',
        'maven.neoforged.net': '/neoforge-maven',
        'meta.fabricmc.net': '/fabric-meta', 'maven.fabricmc.net': '/fabric-maven',
        'meta.quiltmc.org': '/quilt-meta', 'maven.quiltmc.org': '/quilt-maven',
        'authlib-injector.yushi.moe': '/mirrors/authlib-injector',
        'cdn.modrinth.com': '/mods/modrinth', 'edge.forgecdn.net': '/mods/curseforge',
        'mediafilez.forgecdn.net': '/mods/curseforge-mediafilez',
    }.get(u.hostname, '')
    return prefix + u.path


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


class Inventory:
    def __init__(self, root, concurrency=4):
        self.root = Path(root)
        self.concurrency = concurrency
        self.items = {}
        self.errors = []
        self.versions = 0
        self.done = 0
        self.progress = {}
        self.changed_urls = set()

    def add(self, item, category, url=None):
        url = url or item.get('url')
        if not url or not allowed_url(url):
            if url:
                self.errors.append({'url': url, 'error': 'unsupported URL'})
            return
        size = item.get('size')
        sha1 = item.get('sha1')
        path = route_for(url)
        if category.startswith('java-') and path.startswith('/v1/'):
            path = '/java' + path
        priority = max(self.items.get(url, {}).get('priority', 0), PRIORITY.get())
        self.items[url] = dict(url=url, path=path, category=category,
                               size=size, sha1=sha1, priority=priority)
        self.changed_urls.add(url)

    async def fetch_json(self, session, url, sha1=None, category='metadata'):
        if not allowed_url(url):
            raise ValueError('unsupported metadata URL')
        key = hashlib.sha256(url.encode()).hexdigest()
        file = self.root / 'metadata' / (key + '.json')
        if file.exists() and time.time() - file.stat().st_mtime < (31536000 if sha1 else 300):
            raw = await asyncio.to_thread(file.read_bytes)
            if not sha1 or hashlib.sha1(raw).hexdigest() == sha1:
                self.add(dict(url=url, size=len(raw), sha1=sha1), category)
                return json.loads(raw)
        for attempt in range(3):
            try:
                async with session.get(url, allow_redirects=False) as response:
                    response.raise_for_status()
                    if response.status != 200:
                        raise ValueError('metadata redirect refused')
                    chunks, length = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        length += len(chunk)
                        if length > 16 * 1024 * 1024:
                            raise ValueError('metadata too large')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
                if sha1 and hashlib.sha1(raw).hexdigest() != sha1:
                    raise ValueError('metadata SHA-1 mismatch')
                data = json.loads(raw)
                await asyncio.to_thread(file.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(file.write_bytes, raw)
                self.add(dict(url=url, size=len(raw), sha1=sha1), category)
                return data
            except (OSError, ValueError, asyncio.TimeoutError, __import__('aiohttp').ClientError):
                if attempt == 2:
                    raise
                await asyncio.sleep(attempt + 1)

    def report(self):
        unique = {}
        groups = {}
        unknown = 0
        for item in self.items.values():
            key = item.get('sha1') or item['url']
            if key in unique:
                continue
            unique[key] = item
            group = groups.setdefault(item['category'], dict(files=0, bytes=0, unknown_sizes=0))
            group['files'] += 1
            if item['size'] is None:
                unknown += 1
                group['unknown_sizes'] += 1
            else:
                group['bytes'] += item['size']
        return dict(generated_at=time.time(), versions=self.versions, scanned_versions=self.done,
                    unique_files=len(unique), known_bytes=sum(g['bytes'] for g in groups.values()),
                    unknown_sizes=unknown, categories=groups, errors=self.errors,
                    complete=not self.errors and self.done == self.versions,
                    scope='vanilla-all-versions-all-platforms-and-current-mojang-java',
                    excluded=['ordinary mods', 'modpacks', 'resourcepacks', 'shaders'],
                    unmeasured=['historical Java runtimes', 'Forge/NeoForge/Fabric/Quilt Maven full history',
                                'OptiFine', 'LiteLoader', 'legacy Oracle Java', 'authlib-injector history'])

    async def scan(self, include_java=True, version_ids=None, incremental=False):
        self.root.mkdir(parents=True, exist_ok=True)
        state_file = self.root / 'inventory-state.json'
        previous = {}
        if incremental and state_file.exists() and (self.root / 'inventory.json').exists():
            def load():
                return (json.loads(state_file.read_text(encoding='utf-8')),
                        json.loads((self.root / 'inventory.json').read_text(encoding='utf-8')))
            previous, saved = await asyncio.to_thread(load)
            if saved['report']['complete']:
                self.items = {item['url']: item for item in saved['files']}
            else:
                previous = {}
        state = dict(versions={}, indices={}, java={})
        self.progress = dict(phase='manifest', incremental=bool(previous))
        changed_versions = 0
        sem = asyncio.Semaphore(self.concurrency)
        async with ClientSession(timeout=ClientTimeout(total=60), connector=TCPConnector(limit=self.concurrency),
                                 headers={'User-Agent': 'Buggcraft-Mirror/0.1 (+https://bugg.icu)'}) as session:
            manifest = await self.fetch_json(session, MANIFEST)
            versions = [v for v in manifest['versions'] if version_ids is None or v['id'] in version_ids]
            self.versions = len(versions)
            indices = {}

            async def version(v):
                async with sem:
                    try:
                        PRIORITY.set(datetime.fromisoformat(v['releaseTime'].replace('Z', '+00:00')).timestamp() if v.get('releaseTime') else 0)
                        signature = v.get('sha1') or v['url']
                        state['versions'][v['id']] = signature
                        if previous.get('versions', {}).get(v['id']) == signature:
                            return
                        data = await self.fetch_json(session, v['url'], v.get('sha1'))
                        for name, item in data.get('downloads', {}).items():
                            self.add(item, 'game-' + name)
                        for lib in data.get('libraries', []):
                            downloads = lib.get('downloads', {})
                            if 'artifact' in downloads:
                                self.add(downloads['artifact'], 'libraries')
                            for item in downloads.get('classifiers', {}).values():
                                self.add(item, 'natives')
                        for log in data.get('logging', {}).values():
                            self.add(log['file'], 'logging')
                        if 'assetIndex' in data:
                            index = dict(data['assetIndex'], priority=PRIORITY.get())
                            if index['url'] in indices:
                                index['priority'] = max(index['priority'], indices[index['url']]['priority'])
                            indices[index['url']] = index
                    except Exception as exc:
                        self.errors.append(dict(url=v['url'], error=type(exc).__name__))
                    finally:
                        self.done += 1
                        self.progress = dict(phase='versions', done=self.done, total=self.versions)

            changed_versions = sum(previous.get('versions', {}).get(v['id']) != (v.get('sha1') or v['url']) for v in versions)
            await asyncio.gather(*(version(v) for v in versions))

            async def assets(index):
                async with sem:
                    try:
                        PRIORITY.set(index.get('priority', 0))
                        signature = index.get('sha1') or index['url']
                        state['indices'][index['url']] = signature
                        # A new version may reuse an old index. Read the hash-cached
                        # JSON to promote all shared assets to that version's priority.
                        data = await self.fetch_json(session, index['url'], index.get('sha1'), 'assets-metadata')
                        for item in data['objects'].values():
                            sha = item['hash']
                            self.add(dict(sha1=sha, size=item['size']), 'assets',
                                     f'https://resources.download.minecraft.net/{sha[:2]}/{sha}')
                    except Exception as exc:
                        self.errors.append(dict(url=index['url'], error=type(exc).__name__))
            self.progress = dict(phase='assets', total=len(indices))
            state['indices'].update(previous.get('indices', {}))
            await asyncio.gather(*(assets(index) for index in indices.values()))
            if include_java:
                try:
                    runtimes = await self.fetch_json(session, JAVA, category='java-metadata')
                    manifests = {entry['manifest']['url']: entry['manifest']
                                 for platform in runtimes.values() for releases in platform.values() for entry in releases}

                    async def java(item):
                        async with sem:
                            try:
                                signature = item.get('sha1') or item['url']
                                state['java'][item['url']] = signature
                                if previous.get('java', {}).get(item['url']) == signature:
                                    return
                                data = await self.fetch_json(session, item['url'], item.get('sha1'), 'java-metadata')
                                for file in data.get('files', {}).values():
                                    for kind, download in file.get('downloads', {}).items():
                                        self.add(download, 'java-' + kind)
                            except Exception as exc:
                                self.errors.append(dict(url=item['url'], error=type(exc).__name__))
                    await asyncio.gather(*(java(item) for item in manifests.values()))
                except Exception as exc:
                    self.errors.append(dict(url=JAVA, error=type(exc).__name__))
        report = self.report()
        report.update(incremental=bool(previous), changed_versions=changed_versions)
        # A failed refresh must not replace a verified catalogue used by prefetch.
        if report['complete'] or not (self.root / 'inventory.json').exists():
            await asyncio.to_thread(write_json, self.root / 'inventory.json', dict(report=report, files=list(self.items.values())))
        await asyncio.to_thread(write_json, self.root / 'inventory-report.json', report)
        if report['complete'] and version_ids is None:
            await asyncio.to_thread(write_json, state_file, state)
        self.progress = dict(phase='finished', **report)
        return report
