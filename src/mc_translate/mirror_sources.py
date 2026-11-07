"""Incremental source discovery. Downloads only read the cached, verified index."""
import asyncio
import hashlib
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin

from aiohttp import ClientSession, ClientTimeout, TCPConnector

from .federation import PublicResolver, origin
from .mirror_catalog import write_json

PRESET = Path(__file__).parent/'web/mirror-sources.json'
MAX_MAPPINGS = 100000
BUILTIN_SOURCES = [
    dict(id='aliyun', name='阿里云 Maven', url='https://maven.aliyun.com/repository/public/', kind='maven', region='cn', enabled=True),
    dict(id='huawei', name='华为云 Maven', url='https://repo.huaweicloud.com/repository/maven/', kind='maven', region='cn', enabled=True),
    dict(id='nju', name='南京大学 Maven / BMCLAPI 目录', url='https://mirror.nju.edu.cn/bmclapi/', kind='maven', region='cn', enabled=True),
    dict(id='central', name='Maven Central', url='https://repo.maven.apache.org/maven2/', kind='maven', region='global', enabled=True),
    dict(id='bmclapi', name='BMCLAPI（可选）', url='https://bmclapi2.bangbang93.com/', kind='minecraft', region='cn', enabled=False),
    dict(id='minguncle', name='MingUncle（可选）', url='https://mirror.minguncle.cn/', kind='minecraft', region='cn', enabled=False),
]


def safe_path(value):
    return (isinstance(value,str) and len(value)<=1024 and value.startswith('/')
            and bool(re.fullmatch(r'/[A-Za-z0-9_./+@-]*',value))
            and all(part not in ('.','..') for part in value.split('/')))


def resource_path(value):
    if not safe_path(value):
        return False
    if value.startswith('/assets/'):
        return bool(re.fullmatch(r'/assets/[a-f0-9]{2}/[a-f0-9]{40}',value)) and value[8:10]==value[-40:-38]
    return value.startswith(('/maven/','/fabric-maven/','/quilt-maven/','/neoforge-maven/',
        '/v1/objects/','/v1/packages/','/v1/products/java-runtime/','/java/v1/','/mods/'))


def validate_registries(rows, public_url):
    if not isinstance(rows,list) or len(rows)>24:
        raise ValueError('at most 24 public sources are allowed')
    result, ids, urls = [], set(), set()
    for row in rows:
        if (not isinstance(row,dict) or set(row)!={'id','name','url','kind','region','enabled'}
                or not isinstance(row['id'],str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',row['id'])
                or not isinstance(row['name'],str) or not 1<=len(row['name'].strip())<=80
                or row['kind'] not in ('maven','minecraft','catalogue')
                or row['region'] not in ('cn','global') or type(row['enabled']) is not bool
                or not isinstance(row['url'],str) or len(row['url'])>1400):
            raise ValueError('invalid public source')
        parsed = urlsplit(row['url'])
        base = origin(f'{parsed.scheme}://{parsed.netloc}')
        if (base==public_url or parsed.query or parsed.fragment or not safe_path(parsed.path or '/')
                or row['kind']!='catalogue' and not parsed.path.endswith('/')):
            raise ValueError('invalid public source URL')
        url = base+(parsed.path or '/')
        if row['id'] in ids or url in urls:
            raise ValueError('duplicate public source')
        ids.add(row['id']);urls.add(url)
        result.append(dict(row,name=row['name'].strip(),url=url))
    return result


def stamp(path):
    try:
        stat = Path(path).stat()
        return stat.st_mtime_ns,stat.st_size
    except FileNotFoundError:
        return 0,0


@lru_cache(maxsize=16)
def document(path, modified, size):
    if not modified:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {}


@lru_cache(maxsize=8)
def _effective(path, modified, size, generated, gen_modified, gen_size, preset_modified, preset_size, minute):
    settings = document(path,modified,size)
    if not settings.get('auto_sources',True):
        return settings.get('sources',[])
    automatic = document(generated,gen_modified,gen_size)
    preset_rows = document(str(PRESET),preset_modified,preset_size).get('sources',[])
    bundled = {(row['prefix'],row['target']) for row in preset_rows}
    legacy = [row for row in settings.get('sources',[])
              if (row['prefix'],row['target']) not in bundled or row['mode']!='redirect']
    registries = settings.get('source_registries',BUILTIN_SOURCES)
    enabled = {row['id']:row['url'] for row in registries if row['enabled']}
    rows = []
    if automatic.get('sources') is not None:
        for item in automatic.get('sources',[]):
            choices = [entry for entry in item.get('candidates',[])
                       if enabled.get(entry['source'])==entry.get('base') and entry.get('expires',0)>minute*60]
            choices.sort(key=lambda entry:(
                entry['region']!='cn' if settings.get('source_prefer_cn',True) else False,
                entry['latency_ms'],entry['source']))
            if choices:
                rows.append(dict(prefix=item['prefix'],target=choices[0]['target'],
                                 targets=[entry['target'] for entry in choices],
                                 mode='redirect',enabled=True))
    preset_age = document(str(PRESET),preset_modified,preset_size).get('generated_at',0)
    if not automatic and time.time()-preset_age<8*86400 and enabled.get('central')=='https://repo.maven.apache.org/maven2/':
        discovered = {row['prefix'] for row in rows}
        rows.extend(row for row in preset_rows if row['prefix'] not in discovered)
    keys = {(row['prefix'],row['mode']) for row in legacy}
    return [*legacy,*(row for row in rows if (row['prefix'],row['mode']) not in keys)][:MAX_MAPPINGS]


def effective_sources(config):
    root = Path(config.mirror_root).resolve()
    path,generated = root/'settings.json',root/'automatic-sources.json'
    return _effective(str(path),*stamp(path),str(generated),*stamp(generated),*stamp(PRESET),int(time.time()//60))


def source_status(root):
    path = Path(root)/'automatic-sources.json'
    data = document(str(path),*stamp(path))
    return {key:data.get(key) for key in ('generated_at','checked','matched','remaining','last_error','providers')}


class SourceDiscovery:
    def __init__(self, root):
        self.root = Path(root)
        self.state_path = self.root/'source-checks.json'
        self.output = self.root/'automatic-sources.json'
        self.records = document(str(self.state_path),*stamp(self.state_path)).get('records',{})
        self.catalogues = {}
        self.crawls_path = self.root/'source-crawls.json'
        self.crawls = document(str(self.crawls_path),*stamp(self.crawls_path)).get('sources',{})
        self.identities_path = self.root/'source-identities.json'
        self.identities = document(str(self.identities_path),*stamp(self.identities_path))
        self.http = None
        self.lock = asyncio.Lock()
        self.progress = {}

    async def close(self):
        if self.http:
            await self.http.close()

    def client(self):
        if not self.http:
            self.http = ClientSession(timeout=ClientTimeout(total=8),
                connector=TCPConnector(limit=4,limit_per_host=2,resolver=PublicResolver()),
                headers={'User-Agent':'Buggcraft-Automatic-Sources/1.0'})
        return self.http

    async def checksum(self, target):
        async with self.client().get(target+'.sha1',allow_redirects=False) as response:
            raw = await response.content.read(257)
            if response.status in (404,410):
                return None
            if response.status!=200 or len(raw)>256:
                raise ValueError('checksum source temporarily unavailable')
            match = re.fullmatch(rb'\s*([a-fA-F0-9]{40})(?:\s+[^\r\n]+)?\s*',raw)
            return match[1].decode().lower() if match else None

    async def available(self, target, size):
        async with self.client().head(target,allow_redirects=False) as response:
            return response.status==200 and (size is None or response.content_length==size)

    async def catalogue(self, source):
        """Registered JSON inventories advertise files absent from other sources."""
        old = self.catalogues.get(source['url'])
        if old and time.time()-old['at']<1800:
            return old['files']
        async with self.client().get(source['url'],allow_redirects=False) as response:
            if response.status!=200:
                raise ValueError('catalogue unavailable')
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw)>16*1024*1024:
                    raise ValueError('catalogue exceeds 16 MiB')
        data = json.loads(raw)
        if not isinstance(data,dict) or not isinstance(data.get('files'),list) or len(data['files'])>MAX_MAPPINGS:
            raise ValueError('invalid source catalogue')
        files = []
        host = urlsplit(source['url']).netloc
        for item in data['files']:
            if (not isinstance(item,dict) or not resource_path(item.get('path'))
                    or not re.fullmatch('[a-f0-9]{40}',str(item.get('sha1','')))):
                continue
            parsed = urlsplit(str(item.get('url','')))
            if (parsed.scheme!='https' or parsed.netloc!=host or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not safe_path(parsed.path)):
                continue
            files.append(dict(path=item['path'],sha1=item['sha1'],target=parsed.geturl()))
        self.catalogues[source['url']] = dict(at=time.time(),files=files)
        return files

    async def directory(self, source):
        """Bounded directory discovery, including files unique to this repository.

        No binary downloads: follow public directory links and verify SHA-1
        sidecars. Repositories without listing support keep inventory matching.
        """
        state = self.crawls.setdefault(source['url'],dict(queue=[''],seen=[],files=[],restart_at=0))
        now = time.time()
        if not state['queue'] and now>=state.get('restart_at',0):
            state.update(queue=[''],seen=[],restart_at=now+86400)
        if not state['queue']:
            return state['files']
        suffix = state['queue'].pop(0)
        class Links(HTMLParser):
            def __init__(self):super().__init__();self.links=[]
            def handle_starttag(self,tag,attrs):
                if tag=='a':self.links.extend(value for key,value in attrs if key=='href' and value)
        try:
            async with self.client().get(source['url']+suffix,allow_redirects=False) as response:
                if response.status!=200 or 'html' not in response.headers.get('Content-Type',''):
                    state['seen'].append(suffix)
                    return state['files']
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw)>512*1024:raise ValueError('directory listing too large')
            links = Links();links.feed(raw.decode('utf-8',errors='replace'))
            seen = set(state['seen'])|set(state['queue'])|{suffix}
            known = set(state['files'])
            for href in links.links[:2000]:
                url = urljoin(source['url']+suffix,href)
                if not url.startswith(source['url']):continue
                child = url[len(source['url']):]
                if not safe_path('/'+child):continue
                # Never leave this directory or follow navigation/sort links.
                if not child.startswith(suffix) or child==suffix:continue
                if child.endswith('/') and child not in seen and len(seen)<2000:
                    state['queue'].append(child);seen.add(child)
                elif child.endswith(('.jar','.zip','.tar.gz','.tar.xz')) and len(known)<MAX_MAPPINGS:
                    known.add(child)
            state['files'] = sorted(known)
            state['seen'].append(suffix)
        except Exception:
            # Retry this page later, without losing previously discovered files.
            state['queue'].append(suffix)
        return state['files']

    async def refresh(self):
        if self.lock.locked():
            return
        async with self.lock:
            settings_path = self.root/'settings.json'
            settings = document(str(settings_path),*stamp(settings_path))
            if not settings.get('auto_sources',True):
                return
            registries = [row for row in settings.get('source_registries',BUILTIN_SOURCES) if row['enabled']]
            inventory_path = self.root/'inventory.json'
            inventory = await asyncio.to_thread(lambda:document(str(inventory_path),*stamp(inventory_path)))
            preset_data = document(str(PRESET),*stamp(PRESET))
            files = {row['path']:row for row in inventory.get('files',[])[:MAX_MAPPINGS]
                     if inventory.get('report',{}).get('complete') and safe_path(row.get('path'))
                     and re.fullmatch('[a-f0-9]{40}',str(row.get('sha1','')))}
            if not inventory.get('report',{}).get('complete'):
                files.update({path:dict(path=path,sha1=sha1,priority=0) for path,sha1 in preset_data.get('checksums',{}).items()})
            files = dict(sorted(files.items(),key=lambda pair:pair[1].get('priority',0),reverse=True))
            now,last_error = time.time(),None
            candidates,catalogue_errors = [],{}
            self.progress = dict(phase='listing', source='', done=0, total=None, active=[])
            for source in registries:
                self.progress['source'] = source['name']
                if source['kind']=='catalogue':
                    try:
                        for item in await self.catalogue(source):
                            known = files.get(item['path'])
                            if not known or known['sha1']==item['sha1']:
                                candidates.append((source,item['path'],item['sha1'],item['target']))
                    except Exception as exc:
                        last_error = f'{source["name"]}: {type(exc).__name__}: {str(exc)[:120]}'
                        catalogue_errors[source['id']] = last_error
                        # Keep the previous verified index on transient index failure.
                        for old in self.records.values():
                            known = files.get(old['path'])
                            if (old.get('base')==source['url'] and (not known or known['sha1']==old['sha1'])):
                                candidates.append((source,old['path'],old['sha1'],old['target']))
                    continue
                if source['kind']=='maven':
                    # Scan one directory page per provider per pass. The cursor
                    # persists across restarts and cannot grow without a bound.
                    for suffix in await self.directory(source):
                        path = '/maven/'+suffix
                        if path not in files:
                            candidates.append((source,path,None,source['url']+suffix))
                for path,item in files.items():
                    if source['kind']=='maven':
                        if not path.startswith('/maven/'):
                            continue
                        target = source['url']+path[len('/maven/'):]
                    else:
                        target = source['url']+path.lstrip('/')
                    candidates.append((source,path,item['sha1'],target))
            preset = {row['prefix']:row['target'] for row in document(str(PRESET),*stamp(PRESET)).get('sources',[])}
            current,due = {},[]
            for source,path,sha1,target in candidates:
                key = hashlib.sha256((source['url']+'\n'+path+'\n'+target).encode()).hexdigest()
                old = self.records.get(key,{})
                record = dict(old) if old and (sha1 is None or old.get('sha1')==sha1) else dict(sha1=sha1,next_check=0,matched=False)
                if (not old and source['id']=='central' and preset.get(path)==target
                        and now-preset_data.get('generated_at',0)<7*86400):
                    verified = preset_data['generated_at']
                    record.update(matched=True,next_check=verified+7*86400,verified_at=verified,latency_ms=1000)
                record.update(source=source['id'],base=source['url'],region=source['region'],path=path,target=target)
                current[key] = record
                if record['next_check']<=now:
                    due.append(record)
            groups = {source['id']:[] for source in registries}
            for record in due:
                groups[record['source']].append(record)
            due = [record for offset in range(min(128,max((len(rows) for rows in groups.values()),default=0)))
                   for rows in groups.values() for record in rows[offset:offset+1]][:128]
            semaphore = asyncio.Semaphore(4)
            self.progress = dict(phase='checksums', done=0, total=len(due), active=[])
            async def check(record):
                nonlocal last_error
                async with semaphore:
                    active = dict(source=record['source'], path=record['path'])
                    self.progress['active'].append(active)
                    started = time.monotonic()
                    try:
                        checksum = await self.checksum(record['target'])
                        matched = checksum is not None and (record['sha1'] is None or checksum==record['sha1'])
                        if matched and record['sha1'] is None:
                            record['sha1'] = checksum
                        record.update(matched=matched,next_check=now+(7*86400 if matched else 86400),
                                      verified_at=now,latency_ms=round((time.monotonic()-started)*1000,2))
                        record.pop('error',None)
                    except Exception as exc:
                        record['next_check'] = now+300
                        record['error'] = f'{type(exc).__name__}: {str(exc)[:120]}'
                        last_error = f'{record["source"]}: {record["error"]}'
                        if now-record.get('verified_at',0)>8*86400:
                            record['matched'] = False
                    finally:
                        self.progress['active'].remove(active)
                        self.progress['done'] += 1
            await asyncio.gather(*(check(record) for record in due))
            by_path = {}
            for record in current.values():
                if record.get('matched'):
                    canonical = files.get(record['path'],{}).get('sha1') or self.identities.get(record['path'])
                    if canonical and canonical!=record['sha1']:
                        record.update(matched=False,next_check=now+86400,error='checksum_conflict')
                        continue
                    if len(self.identities)<MAX_MAPPINGS or record['path'] in self.identities:
                        self.identities[record['path']] = record['sha1']
                    by_path.setdefault(record['path'],[]).append(dict(source=record['source'],base=record['base'],region=record['region'],
                        target=record['target'],sha1=record['sha1'],latency_ms=record.get('latency_ms',10000),
                        expires=record.get('verified_at',now)+8*86400))
            mappings = [dict(prefix=path,candidates=choices) for path,choices in by_path.items()][:MAX_MAPPINGS]
            providers = [dict(id=source['id'],name=source['name'],url=source['url'],
                verified=sum(bool(row.get('matched')) for row in current.values() if row['source']==source['id']),
                checked=sum(bool(row.get('verified_at')) for row in current.values() if row['source']==source['id']),
                errors=sum(bool(row.get('error')) for row in current.values() if row['source']==source['id']),
                error=catalogue_errors.get(source['id'])) for source in registries]
            result = dict(inventory_checked=bool(inventory.get('report',{}).get('complete')),generated_at=time.time(),checked=len(due),matched=len(mappings),
                remaining=sum(row['next_check']<=now for row in current.values()),last_error=last_error,sources=mappings,providers=providers)
            self.records = current
            await asyncio.to_thread(write_json,self.state_path,dict(records=current))
            await asyncio.to_thread(write_json,self.output,result)
            await asyncio.to_thread(write_json,self.crawls_path,dict(sources=self.crawls))
            await asyncio.to_thread(write_json,self.identities_path,self.identities)
            self.progress.update(phase='finished', active=[])
