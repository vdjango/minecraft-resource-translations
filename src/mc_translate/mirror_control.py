"""Small management bridge. Bulk bytes never pass through the translation app."""
import asyncio
import json
import sqlite3
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web

from .mirror import read_settings, settings_path, validate_settings
from .mirror_catalog import write_json
from .mirror_policy import inventory_selection
from .mirror_sources import source_status


class MirrorControl:
    def __init__(self, api):
        self.api = api
        self.config = api.config
        self.http = None

    async def start(self):
        self.http = ClientSession(timeout=ClientTimeout(total=3), connector=TCPConnector(limit=2))

    async def close(self):
        if self.http:
            await self.http.close()

    async def call(self, body=None):
        token_path = Path(self.config.mirror_root) / 'control-token'
        if not token_path.exists() or not self.http:
            return None
        try:
            token = await asyncio.to_thread(token_path.read_text, encoding='ascii')
            async with self.http.request('POST' if body else 'GET',
                                         f'http://127.0.0.1:{self.config.mirror_port}/_internal/control',
                                         headers={'X-Mirror-Control': token.strip()}, json=body,
                                         timeout=ClientTimeout(total=30 if body and body.get('action') in ('cloud_migrate', 'cloud_test') else 3)) as response:
                if response.status != 200:
                    if response.status == 409:
                        raise web.HTTPConflict(text='mirror_job_running')
                    if response.status == 507:
                        raise ValueError('cache quota is below active downloads or protected versions')
                    if response.status == 400:
                        raise ValueError((await response.text())[:200])
                    return None
                return await response.json()
        except (ClientError, asyncio.TimeoutError, OSError):
            return None

    async def admin(self, request):
        self.api.require_admin(request)
        if request.method == 'POST':
            # Exact verified file mappings can exceed the normal small API body.
            data = await request.clone(client_max_size=1024*1024).json()
            if not isinstance(data, dict):
                raise ValueError('expected a JSON object')
            if 'action' in data:
                if data['action'] not in ('inventory', 'prefetch', 'cancel', 'refresh', 'source_scan', 'source_files', 'source_preview', 'source_cleanup', 'quality_probe', 'quality_history', 'cache_files', 'room_files', 'cloud_migrate', 'cloud_retry', 'cloud_probe', 'cloud_test'):
                    raise ValueError('invalid mirror action')
                arguments = dict(action=data['action'])
                if data['action'] in ('cache_files', 'room_files'):
                    from .mirror_observation import page_arguments
                    page_arguments(data)
                    arguments.update({key: data[key] for key in ('query', 'category', 'cursor') if key in data})
                if data['action'] in ('quality_probe', 'quality_history'):
                    source = str(data.get('source', ''))
                    if len(source) > 64:
                        raise ValueError('invalid source')
                    arguments['source'] = source or None
                if data['action']=='source_files':
                    source,offset = str(data.get('source','')),int(data.get('offset',0))
                    if len(source)>64 or not 0<=offset<=30000:
                        raise ValueError('invalid source pagination')
                    arguments.update(source=source,offset=offset)
                if data['action'] == 'cloud_migrate':
                    route, limit = str(data.get('route', '')), data.get('limit', 20)
                    if route not in read_settings(self.config)['qiniu_routes'] or type(limit) is not int or not 1 <= limit <= 200:
                        raise ValueError('invalid cloud migration selection')
                    arguments.update(route=route, limit=limit)
                if data['action'] == 'cloud_retry':
                    job_id = data.get('job_id')
                    if type(job_id) is not int or job_id <= 0:
                        raise ValueError('invalid cloud job')
                    arguments['job_id'] = job_id
                if data['action'] in ('cloud_probe', 'cloud_test'):
                    profile = str(data.get('profile', ''))
                    if profile not in {item['id'] for item in read_settings(self.config)['qiniu_profiles']}:
                        raise ValueError('invalid cloud profile')
                    arguments['profile'] = profile
                result = await self.call(arguments)
                if result is None:
                    return web.json_response(dict(code='mirror_offline'), status=503)
                return web.json_response(result)
            previous = read_settings(self.config, effective=False)
            if isinstance(data.get('policies'), dict):
                data['policies'] = {**previous['policies'], **data['policies']}
            values = validate_settings({**previous, **data})
            await asyncio.to_thread(self._protect_cloud_locations, values)
            await asyncio.to_thread(write_json, settings_path(self.config), values)
            try:
                await self.call(dict(action='trim'))
            except ValueError:
                await asyncio.to_thread(write_json, settings_path(self.config), previous)
                raise
        status = await self.call()
        if status is None:
            settings = read_settings(self.config)
            status = dict(running=False, settings={**settings,'sources':[]}, root=str(Path(self.config.mirror_root).resolve()),
                          source_mappings=sum(row['enabled'] and row['mode']=='redirect' for row in settings['sources']))
            status['automatic_sources'] = source_status(self.config.mirror_root)
        inventory = Path(self.config.mirror_root) / 'inventory-report.json'
        if inventory.exists():
            def report():
                return json.loads(inventory.read_text(encoding='utf-8'))
            status['inventory'] = await asyncio.to_thread(report)
            status['selection'] = inventory_selection(status['inventory'], status['settings']['policies'])
        return web.json_response(status)

    async def room_nodes(self, request):
        self.api.require_admin(request)
        from .room_node_access import NodeAccess
        registry = NodeAccess(self.config.mirror_root)
        invitation = None
        if request.method == 'POST':
            data = await request.json()
            if not isinstance(data, dict):
                raise web.HTTPBadRequest(text='Invalid node operation')
            if data.get('action') == 'invite':
                invitation = await asyncio.to_thread(registry.invite, read_settings(self.config)['public_url'], data.get('name', ''))
            elif data.get('action') == 'revoke':
                await asyncio.to_thread(registry.revoke, data.get('issuer'))
            else:
                raise ValueError('未知节点接入操作。')
        from .room_cache import keys
        nodes = await asyncio.to_thread(registry.list)
        known = {item['issuer'] for item in nodes}
        nodes += [dict(issuer=issuer, name=issuer, state='connected', enabled=True, legacy=True)
                  for issuer in keys(self.config.mirror_root) if issuer not in known]
        return web.json_response(dict(nodes=nodes, invitation=invitation), headers={'Cache-Control': 'no-store'})
    def _protect_cloud_locations(self, values):
        database = Path(self.config.mirror_root) / 'mirror.sqlite3'
        if not database.exists():
            return
        with sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True) as connection:
            try:
                used = {row[0] for row in connection.execute("SELECT DISTINCT profile FROM object_locations WHERE state='ready'")}
            except sqlite3.OperationalError:
                return
        before = {item['id']: item for item in read_settings(self.config, effective=False)['qiniu_profiles']}
        after = {item['id']: item for item in values['qiniu_profiles']}
        for profile in used:
            if profile not in before or profile not in after or any(before[profile][key] != after[profile][key] for key in ('bucket', 'domain', 'prefix', 'private', 'upload_host')):
                raise ValueError('A Qiniu profile holding files cannot be removed or repointed; add a new profile instead')

    async def status(self, request):
        result = await self.call()
        fields = ('running', 'cached_bytes', 'cached_files', 'active_downloads', 'exclusions', 'policies')
        public = {**{k: result[k] for k in fields}, 'enabled': result['settings']['enabled'],
                  'protocol': 'buggcraft-resources-v1', 'accepting': result.get('accepting', False),
                  'source_mappings': result.get('source_mappings',sum(n['enabled'] and n['mode']=='redirect' for n in result['settings']['sources'])),
                  'nodes': [dict(url=n['url'], weight=n['weight']) for n in result.get('nodes', []) if n['healthy'] and n['enabled']]} if result else dict(running=False)
        return web.json_response(public,
                                 headers={'Cache-Control': 'no-store'})
