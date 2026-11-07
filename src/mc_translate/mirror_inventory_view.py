"""Explain catalogue entries without confusing them with stored objects.

Reads the existing catalogue and indexed storage records only. Opening this view
never probes a source, downloads a resource, or walks the object directory.
"""
import asyncio
import json
import time
from collections import Counter
from urllib.parse import unquote, urlsplit

from .mirror_automation import prefetch_exclusion
from .mirror_policy import category_for


CATEGORIES = {
    'game-client': ('游戏客户端核心', '各版本的 client.jar'),
    'game-server': ('游戏服务端核心', '独立服务器使用的 server.jar'),
    'game-windows_server': ('Windows 服务端', 'Windows 服务端程序'),
    'game-client_mappings': ('客户端开发映射', '开发和调试使用的名称映射'),
    'game-server_mappings': ('服务端开发映射', '开发和调试使用的名称映射'),
    'metadata': ('游戏版本清单', '版本信息、下载地址与校验值'),
    'assets': ('原版素材文件', '游戏纹理、声音等素材，文件名为哈希'),
    'assets-metadata': ('原版素材索引', '素材名称和文件哈希的对应表'),
    'libraries': ('Java 依赖库', '游戏运行所需的 Java 库'),
    'natives': ('平台原生库', 'Windows、macOS、Linux 各自的原生库'),
    'logging': ('日志配置', '游戏日志输出配置'),
    'java-raw': ('Java 运行时文件', '按操作系统区分的运行时原始文件'),
    'java-lzma': ('Java 压缩副本', '运行时文件的 LZMA 版本，自动缓存跳过重复格式'),
    'java-metadata': ('Java 运行时清单', '运行时的文件列表与校验值'),
    'loaders': ('加载器文件', '已登记的加载器元数据与安装文件'),
    'mods': ('模组文件', '按需登记的模组下载文件'),
}
REASONS = {
    'redirect_policy': '类别设置为仅重定向',
    'platform': '未选择的平台',
    'server_file': '服务端文件，不自动预下载',
    'developer_mapping': '开发映射，不自动预下载',
    'duplicate_compression': '已有原始格式，跳过重复压缩格式',
}
POLICIES = {'cache': '缓存并提供下载', 'backup': '备份并重定向',
            'redirect': '仅重定向', 'disabled': '仅重定向'}


class InventoryView:
    def __init__(self, mirror):
        self.mirror = mirror
        self.lock = asyncio.Lock()
        self.signature = None
        self.catalogue = {'files': []}

    async def read(self):
        path = self.mirror.root / 'inventory.json'
        async with self.lock:
            try:
                stat = await asyncio.to_thread(path.stat)
                signature = (stat.st_mtime_ns, stat.st_size)
                if signature != self.signature:
                    def load():
                        return json.loads(path.read_text(encoding='utf-8'))
                    catalogue = await asyncio.to_thread(load)
                    if not isinstance(catalogue.get('files'), list):
                        raise ValueError('invalid inventory')
                    self.catalogue, self.signature = catalogue, signature
                return self.catalogue, stat.st_mtime
            except FileNotFoundError:
                return {'files': []}, None

    async def files(self, data):
        query, category, handling, offset = (data.get('query', ''), data.get('category', ''),
                                           data.get('handling', ''), data.get('offset', 0))
        if (not isinstance(query, str) or len(query) > 160 or not isinstance(category, str)
                or len(category) > 64 or handling not in ('', 'eligible', 'stored', 'excluded', *REASONS)
                or type(offset) is not int or not 0 <= offset <= 1000000):
            raise ValueError('清单筛选条件无效')
        query = query.strip().lower()
        catalogue, updated = await self.read()
        # Import lazily: the mirror constructs this view during module loading.
        from .mirror import read_settings
        settings = read_settings(self.mirror.config, effective=False)
        async with self.mirror.db.execute('SELECT c.url,c.object,c.size,o.local FROM cache c '
                                          'JOIN objects o ON o.digest=c.object') as cursor:
            cached = {row['url']: dict(row) for row in await cursor.fetchall()}
        async with self.mirror.db.execute("SELECT digest,profile,object_key FROM object_locations "
                                          "WHERE scope='mirror' AND state='ready'") as cursor:
            locations = {}
            for row in await cursor.fetchall():
                locations.setdefault(row['digest'], []).append(dict(row))
        async with self.mirror.db.execute('SELECT * FROM mirror_failed_files') as cursor:
            failed = {row['url']: dict(row) for row in await cursor.fetchall()}
        async with self.mirror.db.execute('SELECT digest,size,local FROM objects') as cursor:
            objects = [dict(row) for row in await cursor.fetchall()]
        async with self.mirror.db.execute("SELECT scope,profile,COUNT(*) files,COALESCE(SUM(size),0) bytes "
                                          "FROM object_locations WHERE state='ready' GROUP BY scope,profile") as cursor:
            cloud_spaces = [dict(row) for row in await cursor.fetchall()]

        def assemble():
            groups, reasons, rows = {}, Counter(), []
            matched = eligible = stored = 0
            for item in catalogue['files']:
                key = item.get('category', 'unknown')
                label, purpose = CATEGORIES.get(key, (key, '资源清单中的已登记文件'))
                group = groups.setdefault(key, dict(key=key, label=label, purpose=purpose,
                    total=0, eligible=0, local=0, cloud=0, stored=0, reasons=Counter()))
                reason = prefetch_exclusion(item, settings)
                record = cached.get(item['url'], {})
                copies = locations.get(record.get('object'), [])
                local = bool(record.get('local'))
                exists = local or bool(copies)
                group['total'] += 1
                group['eligible'] += not reason
                group['local'] += local
                group['cloud'] += bool(copies)
                group['stored'] += exists
                eligible += not reason
                stored += exists
                if reason:
                    reasons[reason] += 1
                    group['reasons'][reason] += 1
                if (category and key != category or handling == 'eligible' and reason
                        or handling == 'stored' and not exists or handling == 'excluded' and not reason
                        or handling in REASONS and reason != handling):
                    continue
                path = unquote(str(item.get('path') or urlsplit(item['url']).path))
                if query and query not in (path+' '+item['url']+' '+str(item.get('sha1') or '')).lower():
                    continue
                matched += 1
                if data.get('summary_only') or not offset < matched <= offset+50:
                    continue
                route = category_for(item['url'], key)
                policy = settings['policies'].get(route, 'redirect')
                target = settings['qiniu_routes'][route]
                error = failed.get(item['url'])
                rows.append(dict(url=item['url'], path=path, name=path.rsplit('/', 1)[-1],
                    category=key, label=label, size=item.get('size'), sha1=item.get('sha1'),
                    platforms=item.get('platforms') or [], eligible=not reason,
                    reason=reason, reason_label=REASONS.get(reason), policy=policy,
                    policy_label=POLICIES[policy], destination=dict(target),
                    local=local, cloud=copies, digest=record.get('object'),
                    state='stored' if exists else 'retrying' if error and not reason else
                          'eligible' if not reason else 'excluded', failure=error))
            local_objects = [o for o in objects if o['local']]
            return dict(total=len(catalogue['files']), eligible=eligible, stored=stored,
                excluded=len(catalogue['files'])-eligible, reasons=dict(reasons),
                categories=sorted(groups.values(), key=lambda g: (-g['total'], g['key'])),
                files=rows, matched=matched, offset=offset, limit=50,
                next_offset=offset+50 if offset+50 < matched else None,
                updated_at=updated, generated_at=time.time(),
                storage=dict(local_files=len(local_objects), local_bytes=sum(o['size'] for o in local_objects),
                             cloud=cloud_spaces),
                scope='version_catalogue',
                coverage='游戏版本、原版素材、依赖库与 Java 清单；模组等按需文件见缓存资源。')
        # Classification of a large catalogue must not occupy the download event loop.
        return await asyncio.to_thread(assemble)
