import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from mc_translate.api import create_app
from mc_translate.config import Config
from mc_translate.mirror import create_mirror, upstreams, validate_settings, read_settings, settings_path
from mc_translate.mirror_catalog import Inventory, allowed_url, write_json
from mc_translate.mirror_policy import POLICIES, category_for
from mc_translate.mirror_routing import source_target


class MirrorCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(database=str(self.root/'translations.sqlite'), mirror_root=str(self.root/'mirror'))
        write_json(settings_path(self.config), validate_settings({'policies': {
            **POLICIES, 'libraries': 'cache', 'assets': 'cache', 'java': 'cache'}}))
        self.payload = b'validated game resource\n' * 100
        self.calls = 0
        self.response_status = 200
        self.gate = None

        async def origin(request):
            self.calls += 1
            if self.gate:
                await self.gate.wait()
            return web.Response(body=self.payload, status=self.response_status)

        app = web.Application()
        app.router.add_get('/data', origin)
        self.origin = TestServer(app)
        await self.origin.start_server()
        self.client = TestClient(TestServer(await create_mirror(self.config)))
        await self.client.start_server()
        self.mirror = self.client.app['mirror']
        self.assertEqual(self.mirror.http.headers['Accept-Encoding'], 'identity')
        await self.mirror.http.close()
        self.http = ClientSession()
        self.mirror.http = SimpleNamespace(get=lambda *a, **kw: self.http.get(self.origin.make_url('/data')), close=self.http.close)

    async def asyncTearDown(self):
        await self.client.close()
        await self.origin.close()
        self.temp.cleanup()

    def asset(self):
        sha = hashlib.sha1(self.payload).hexdigest()
        return '/assets/' + sha[:2] + '/' + sha

    def policies(self, **values):
        settings = read_settings(self.config)
        settings['policies'].update(values)
        write_json(settings_path(self.config), validate_settings(settings))

    async def test_redirect_and_disabled_do_not_contact_origin_even_with_cache(self):
        path = self.asset()
        await (await self.client.get(path)).read()
        self.assertEqual(self.calls, 1)
        self.policies(assets='redirect')
        for method in ('GET', 'HEAD'):
            response = await self.client.request(method, path, allow_redirects=False, headers={'Range': 'bytes=0-3'})
            self.assertEqual(response.status, 307)
            self.assertEqual(response.headers['Location'], 'https://resources.download.minecraft.net'+path[7:])
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.calls, 1)
        self.policies(assets='disabled')
        self.assertEqual((await self.client.get(path, allow_redirects=False)).status, 307)
        self.policies(assets='cache')
        self.assertEqual((await self.client.get(path)).status, 200)
        self.assertEqual(self.calls, 1)

    async def test_shared_object_host_uses_metadata_category_and_explicit_java_route(self):
        digest = hashlib.sha1(self.payload).hexdigest()
        path = '/v1/objects/'+digest+'/java.exe'
        url = upstreams(path)[0]
        await self.mirror.db.execute('INSERT INTO expected VALUES(?,?,?,?)', (url, digest, len(self.payload), 'java-raw'))
        await self.mirror.db.commit()
        self.policies(java='redirect')
        for resource in (path, '/java'+path):
            response = await self.client.get(resource, allow_redirects=False)
            self.assertEqual(response.status, 307)
            self.assertEqual(response.headers['X-Mirror-Category'], 'java')
            self.assertEqual(response.headers['Location'], url)
        self.assertEqual(self.calls, 0)

    async def test_mods_default_off_and_allow_only_fixed_download_paths(self):
        path = '/mods/modrinth/data/abc123/versions/def456/example.jar'
        self.assertEqual((await self.client.get(path, allow_redirects=False)).status, 307)
        self.policies(mods='redirect')
        response = await self.client.get(path, allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertEqual(response.headers['Location'], 'https://cdn.modrinth.com/data/abc123/versions/def456/example.jar')
        self.assertEqual(self.calls, 0)
        self.policies(mods='cache')
        response = await self.client.get(path)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.payload)
        for bad in ('/mods/https://evil.com/file.jar', '/mods/modrinth/data/a/versions/b/file.zip',
                    '/mods/curseforge/files/1/2/file.jar?url=https://evil.com'):
            self.assertIn((await self.client.get(bad)).status, (400, 404))

    async def test_prefetch_skips_redirected_categories_and_honors_live_change(self):
        sha = hashlib.sha1(self.payload).hexdigest()
        files = [
            dict(url='https://resources.download.minecraft.net/'+sha[:2]+'/'+sha, category='assets', size=len(self.payload), sha1=sha),
            dict(url='https://libraries.minecraft.net/example.jar', category='libraries', size=len(self.payload), sha1=sha),
            dict(url='https://piston-data.mojang.com/v1/objects/'+sha+'/java.exe', category='java-raw', size=len(self.payload), sha1=sha),
            dict(url='https://piston-data.mojang.com/v1/objects/'+sha+'/client.jar', category='game-client', size=len(self.payload), sha1=sha),
        ]
        write_json(self.mirror.root/'inventory.json', dict(report={'complete': True}, files=files))
        self.policies(assets='redirect', libraries='disabled', java='redirect')
        await self.mirror.run_job('prefetch')
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.mirror.job_state['skipped'], 3)
        self.assertEqual(self.mirror.job_state['cached'], 1)
        self.assertEqual(self.mirror.job_state['state'], 'finished')

    async def test_policy_validation_and_legacy_settings_defaults(self):
        write_json(settings_path(self.config), {'concurrency': 2})
        settings = read_settings(self.config)
        self.assertEqual(settings['policies'], POLICIES)
        for invalid in ({'assets': 'stream'}, {'unknown': 'cache'}, [], {'java': 1}):
            with self.assertRaises(ValueError):
                validate_settings({'policies': invalid})
        self.assertEqual(category_for('https://libraries.minecraft.net/x.jar'), 'libraries')
        self.assertEqual(category_for('https://maven.minecraftforge.net/x.jar'), 'loaders')

    async def test_backup_redirects_immediately_then_falls_back_and_recovers(self):
        path = self.asset()
        self.policies(assets='backup')
        self.gate = asyncio.Event()
        response = await self.client.get(path, allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertEqual(response.headers['X-Mirror-Backup'], 'pending')
        self.gate.set()
        await asyncio.gather(*list(self.mirror.backups.values()))
        url = upstreams(path)[0]
        self.mirror.origin_health[url] = (time.monotonic(), False)
        self.response_status = 503
        local = await self.client.get(path, headers={'Range':'bytes=0-9'}, allow_redirects=False)
        self.assertEqual(local.status, 206)
        self.assertEqual(await local.read(), self.payload[:10])
        self.assertEqual(local.headers['X-Mirror-Mode'], 'fallback')
        self.assertEqual(local.headers['Cache-Control'], 'no-store')
        self.mirror.origin_health[url] = (time.monotonic(), True)
        self.assertEqual((await self.client.head(path, allow_redirects=False)).status, 307)
        self.policies(assets='cache')
        self.assertEqual((await self.client.get(path, allow_redirects=False)).status, 200)
        self.assertEqual(self.calls, 1)

    async def test_node_selection_excludes_dead_nodes_and_direct_route_never_loops(self):
        settings = read_settings(self.config)
        settings.update(node_weight=0, nodes=[dict(url='https://node.example.com',weight=3,enabled=True)])
        write_json(settings_path(self.config), validate_settings(settings))
        self.assertIsNone(self.mirror.select_node(settings))
        self.mirror.node_health['https://node.example.com'] = dict(healthy=True,at=time.time(),policies={'assets':'redirect'})
        self.assertIsNone(self.mirror.select_node(settings, 'assets'))
        self.mirror.node_health['https://node.example.com']['policies']['assets'] = 'cache'
        response = await self.client.get(self.asset(), allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertEqual(response.headers['Location'], 'https://node.example.com/_mirror_node'+self.asset())
        self.assertEqual(self.calls, 0)
        local = await self.client.get('/_mirror_node'+self.asset(), allow_redirects=False)
        self.assertEqual(local.status, 200)
        self.assertEqual(await local.read(), self.payload)
        self.mirror.node_health['https://node.example.com']['healthy'] = False
        self.assertEqual((await self.client.get(self.asset(),allow_redirects=False)).status, 200)

    async def test_lru_eviction_reclaims_object_and_all_aliases_with_hard_limit(self):
        settings = read_settings(self.config)
        settings.update(quota_gib=1,eviction='lru',protect_latest=0)
        write_json(settings_path(self.config), settings)
        await (await self.client.get(self.asset())).read()
        await (await self.client.get('/maven/alias.jar')).read()
        # Simulate an almost-full GiB without creating a GiB fixture file.
        await self.mirror.db.execute('UPDATE objects SET size=?', (1024**3-8,))
        await self.mirror.db.execute('UPDATE totals SET bytes=?', (1024**3-8,))
        await self.mirror.db.commit()
        self.payload=b'new bytes'
        response = await self.client.get('/maven/new.jar')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.payload)
        status = await self.mirror.status()
        self.assertEqual(status['cached_bytes'], len(self.payload))
        self.assertEqual(status['cached_files'], 1)
        self.assertIsNone(await self.mirror.cached('https://libraries.minecraft.net/alias.jar'))

    async def test_protected_new_version_cannot_be_evicted_by_old_download(self):
        settings = read_settings(self.config)
        settings.update(quota_gib=1,eviction='oldest_version',protect_latest=1)
        write_json(settings_path(self.config), settings)
        await (await self.client.get(self.asset())).read()
        await self.mirror.db.execute('INSERT INTO priorities VALUES(?,?)', (upstreams(self.asset())[0], 100))
        await self.mirror.db.execute('UPDATE objects SET size=?,priority=100', (1024**3-8,))
        await self.mirror.db.execute('UPDATE totals SET bytes=?', (1024**3-8,))
        await self.mirror.db.commit()
        response = await self.client.get('/maven/old.jar', allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertEqual(response.headers['X-Mirror-Reason'], 'local_507')
        self.assertEqual((await self.client.get(self.asset())).status, 200)
        self.assertEqual(self.mirror.reserved, 0)

    async def test_quota_reduction_evicts_more_than_one_batch_and_skips_pinned_objects(self):
        settings = read_settings(self.config)
        settings.update(quota_gib=1, eviction='lru', protect_latest=0)
        rows = [(f'{i:064x}', 4*1024**2, i, 0) for i in range(300)]
        await self.mirror.db.executemany('INSERT INTO objects VALUES(?,?,?,?)', rows)
        await self.mirror.db.execute('UPDATE totals SET bytes=?', (300*4*1024**2,))
        await self.mirror.db.commit()
        self.mirror.serving[rows[0][0]] = 1
        # Reserve almost the entire budget, forcing over 256 victims.
        self.mirror.reserved = 1024**3-8*1024**2
        async with self.mirror.space_lock:
            await self.mirror.make_space(0, settings)
        self.mirror.reserved = 0
        self.assertEqual((await self.mirror.status())['cached_bytes'], 8*1024**2)
        self.assertIsNotNone(await self.mirror.one('SELECT digest FROM objects WHERE digest=?', (rows[0][0],)))
        self.mirror.serving.clear()

    async def test_incremental_inventory_skips_unchanged_version_and_asset_indices(self):
        inventory = Inventory(self.mirror.root)
        manifest=dict(versions=[dict(id='1.0',url='https://piston-meta.mojang.com/v1/packages/'+'a'*40+'/1.0.json',sha1='a'*40,releaseTime='2026-01-01T00:00:00Z')])
        version=dict(downloads={},libraries=[],assetIndex=dict(url='https://piston-meta.mojang.com/v1/packages/'+'b'*40+'/assets.json',sha1='b'*40))
        async def fetch(session,url,*args,**kwargs):
            if 'version_manifest' in url:return manifest
            if url.endswith('1.0.json'):return version
            return {'objects':{}}
        with patch.object(inventory, 'fetch_json', new=AsyncMock(side_effect=fetch)) as requests:
            await inventory.scan(include_java=False)
            self.assertEqual(requests.await_count,3)
        newer=Inventory(self.mirror.root)
        with patch.object(newer, 'fetch_json', new=AsyncMock(side_effect=fetch)) as requests:
            report=await newer.scan(include_java=False,incremental=True)
            self.assertEqual(requests.await_count,1)
            self.assertEqual(report['changed_versions'],0)
            self.assertTrue(report['incremental'])

    async def test_exact_source_mapping_skips_cache_and_prefetch_even_in_cache_policy(self):
        item = dict(url='https://libraries.minecraft.net/org/example/library.jar', path='/maven/org/example/library.jar',
                    category='libraries',size=len(self.payload),sha1=hashlib.sha1(self.payload).hexdigest())
        settings = read_settings(self.config)
        settings['sources'] = [dict(prefix=item['path'], target='https://repo.maven.apache.org/maven2/org/example/library.jar',mode='redirect',enabled=True)]
        write_json(settings_path(self.config),validate_settings(settings))
        with patch.object(self.mirror, 'upstream_available', new=AsyncMock(return_value=True)):
            response = await self.client.get(item['path'], allow_redirects=False)
        self.assertEqual(response.status,307)
        self.assertEqual(response.headers['X-Mirror-Reason'],'source_mapping')
        self.assertEqual(response.headers['Location'],settings['sources'][0]['target'])
        self.assertEqual(self.calls,0)
        write_json(self.mirror.root/'inventory.json',dict(report={'complete':True},files=[item]))
        await self.mirror.run_job('prefetch')
        self.assertEqual(self.mirror.job_state['skipped'],1)
        self.assertEqual(self.calls,0)

    async def test_quality_cache_decision_preserves_category_and_storage_limits(self):
        path = self.asset()
        target = 'https://source.example/slow-file.jar'
        with patch('mc_translate.mirror.source_targets', return_value=[target]), \
                patch.object(self.mirror.quality, 'needs_cache', return_value=True), \
                patch.object(self.mirror, 'upstream_available', new=AsyncMock(return_value=True)):
            # A slow-source hint does not turn an explicitly redirected class into storage.
            self.policies(assets='redirect')
            response = await self.client.get(path, allow_redirects=False)
            self.assertEqual((response.status, response.headers['Location']), (307, target))
            self.assertEqual(self.calls, 0)
            self.policies(assets='cache')
            response = await self.client.get(path, allow_redirects=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), self.payload)
            self.assertEqual(self.calls, 1)
            # A quality decision still cannot bypass the normal capacity check.
            with patch.object(self.mirror, 'ensure', new=AsyncMock(side_effect=web.HTTPInsufficientStorage())):
                response = await self.client.get(path, allow_redirects=False)
                self.assertEqual(response.status, 307)
                self.assertEqual(response.headers['X-Mirror-Reason'], 'local_507')

    async def test_source_validation_exact_and_longest_prefix_mapping(self):
        rows=[dict(prefix='/maven/org/',target='https://repo.example.com/org/',mode='redirect',enabled=True),
              dict(prefix='/maven/org/ex/',target='https://repo2.example.com/ex/',mode='redirect',enabled=True),
              dict(prefix='/maven/org/ex/file.jar',target='https://repo3.example.com/file.jar',mode='redirect',enabled=True)]
        settings=validate_settings({'sources':rows})
        self.assertEqual(source_target(settings,'/maven/org/ex/file.jar','redirect'),'https://repo3.example.com/file.jar')
        self.assertEqual(source_target(settings,'/maven/org/ex/other.jar','redirect'),'https://repo2.example.com/ex/other.jar')
        self.assertEqual(source_target(settings,'/maven/org/else.jar','redirect'),'https://repo.example.com/org/else.jar')
        self.assertIsNone(source_target(settings,'/maven/org/ex/file.jar/evil','fallback'))
        for target in ('http://repo.example.com/org/','https://127.0.0.1/org/','https://mirror.bugg.icu/org/',
                       'https://repo.example.com/../org/','https://repo.example.com/org/?x=1'):
            with self.assertRaises(ValueError):
                validate_settings({'sources':[dict(rows[0],target=target)]})

    async def test_monthly_budget_persists_and_redirects_without_origin_work(self):
        settings=read_settings(self.config)
        settings.update(monthly_egress_gib=1,egress_threshold=90)
        write_json(settings_path(self.config),settings)
        state=await self.mirror.egress.snapshot(settings)
        await self.mirror.db.execute('INSERT INTO egress VALUES(?,?)',(state['period'],1024**3))
        await self.mirror.db.commit()
        response=await self.client.get(self.asset(),allow_redirects=False)
        self.assertEqual(response.status,307)
        self.assertEqual(response.headers['X-Mirror-Reason'],'monthly_budget')
        self.assertEqual(self.calls,0)
        self.assertFalse((await (await self.client.get('/mirror-api/status')).json())['accepting'])

    async def test_range_egress_count_and_bandwidth_offload(self):
        response=await self.client.get(self.asset(),headers={'Range':'bytes=0-9'})
        self.assertEqual(await response.read(),self.payload[:10])
        settings=read_settings(self.config)
        self.assertEqual((await self.mirror.egress.snapshot(settings))['bytes'],10)
        await self.client.head(self.asset())
        self.assertEqual((await self.mirror.egress.snapshot(settings))['bytes'],10)
        settings['bandwidth_mbps']=1
        write_json(settings_path(self.config),settings)
        self.mirror.egress.recent.clear()
        self.mirror.egress.recent.append((int(time.monotonic()),8_000_000))
        self.mirror.egress.window_bytes=8_000_000
        response=await self.client.get(self.asset(),allow_redirects=False)
        self.assertEqual(response.status,307)
        self.assertEqual(response.headers['X-Mirror-Reason'],'bandwidth_budget')
        self.assertEqual(self.calls,1)

    async def test_saturated_node_does_not_hop_again_and_external_failure_uses_official(self):
        settings=read_settings(self.config)
        settings.update(max_serving=1,nodes=[dict(url='https://node.example.com',weight=1,enabled=True)],
                        sources=[dict(prefix='/assets/',target='https://fallback.example.com/assets/',mode='fallback',enabled=True)])
        write_json(settings_path(self.config),validate_settings(settings))
        self.mirror.node_health['https://node.example.com']=dict(healthy=True,at=time.time(),policies={'assets':'cache'})
        self.mirror.serving['pinned']=1
        first=await self.client.get(self.asset(),allow_redirects=False)
        self.assertEqual(first.headers['X-Mirror-Mode'],'node')
        with patch.object(self.mirror,'upstream_available',new=AsyncMock(return_value=True)):
            direct=await self.client.get('/_mirror_node'+self.asset(),allow_redirects=False)
        self.assertEqual(direct.headers['X-Mirror-Mode'],'external')
        with patch.object(self.mirror,'upstream_available',new=AsyncMock(return_value=False)):
            direct=await self.client.get('/_mirror_node'+self.asset(),allow_redirects=False)
        self.assertEqual(direct.headers['Location'],upstreams(self.asset())[0])
        self.mirror.serving.clear()

    async def test_checksum_dedup_range_head_and_cache_hits_when_busy(self):
        path = self.asset()
        first = await self.client.get(path)
        self.assertEqual(await first.read(), self.payload)
        self.assertEqual(first.headers['X-Mirror-Verified'], 'upstream-sha1')
        self.assertIn('immutable', first.headers['Cache-Control'])
        self.assertEqual(self.calls, 1)
        other = await self.client.get('/maven/org/example/example.jar')
        self.assertEqual(other.status, 200)
        await other.read()
        status = await self.mirror.status()
        self.assertEqual(status['cached_bytes'], len(self.payload))
        self.assertEqual(status['cached_files'], 2)
        # Existing cache must stay available even while every download slot is occupied.
        self.mirror.inflight.update({str(n): asyncio.create_task(asyncio.sleep(10)) for n in range(4)})
        ranged = await self.client.get(path, headers={'Range': 'bytes=5-15'})
        self.assertEqual(ranged.status, 206)
        self.assertEqual(await ranged.read(), self.payload[5:16])
        head = await self.client.head(path)
        self.assertEqual(head.status, 200)
        self.assertEqual(int(head.headers['Content-Length']), len(self.payload))
        self.assertEqual(await head.read(), b'')
        self.assertEqual(self.calls, 2)

    async def test_bad_checksum_does_not_publish_or_leave_partial(self):
        path = '/assets/aa/' + 'a'*40
        response = await self.client.get(path, allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertEqual(response.headers['X-Mirror-Reason'], 'local_502')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual((await self.mirror.status())['cached_files'], 0)
        self.assertFalse(list((self.mirror.root/'tmp').iterdir()))
        self.assertEqual(self.mirror.reserved, 0)

    async def test_disk_reserve_denies_new_file(self):
        with patch('mc_translate.mirror.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            response = await self.client.get(self.asset(), allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertEqual(response.headers['X-Mirror-Reason'], 'local_507')
        self.assertEqual((await self.mirror.status())['cached_files'], 0)
        self.assertEqual(self.mirror.reserved, 0)

    async def test_simultaneous_miss_is_downloaded_once(self):
        self.gate = asyncio.Event()
        tasks = [asyncio.create_task(self.client.get(self.asset())) for _ in range(3)]
        for _ in range(100):
            if self.calls:
                break
            await asyncio.sleep(.01)
        self.gate.set()
        responses = await asyncio.gather(*tasks)
        for response in responses:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), self.payload)
        self.assertEqual(self.calls, 1)

    async def test_fixed_origins_control_auth_and_errors(self):
        for path in ('/https://example.com/a', '/fabric-maven/net/fabricmc/fabric-api/foo.jar', '/assets/ab/'+'a'*40, '/maven/test.jar?url=https://example.com'):
            response = await self.client.get(path)
            self.assertIn(response.status, (400, 404))
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual((await self.client.get('/_internal/control')).status, 403)
        headers={'X-Mirror-Control': self.mirror.secret}
        self.assertEqual((await self.client.get('/_internal/control', headers=headers)).status, 200)
        self.assertEqual((await self.client.post('/_internal/control', headers=headers, json=[])).status, 400)
        self.response_status = 302
        self.assertEqual((await self.client.get(self.asset(), allow_redirects=False)).status, 307)
        self.assertTrue(upstreams('/v1/packages/'+'a'*40+'/1.14 Pre-Release.json')[0].endswith('1.14%20Pre-Release.json'))
        self.assertTrue(allowed_url('https://piston-meta.mojang.com/v1/packages/'+'a'*40+'/a%20b.json'))
        for url in ('https://evil.com/a', 'https://libraries.minecraft.net/%2e%2e/a', 'https://libraries.minecraft.net/a?url=x'):
            self.assertFalse(allowed_url(url))

    async def test_metadata_reads_all_streamed_chunks(self):
        value = {'objects': {str(n): {'size': n} for n in range(3000)}}
        raw = json.dumps(value).encode()
        async def streamed(request):
            response = web.StreamResponse()
            await response.prepare(request)
            for start in range(0, len(raw), 1024):
                await response.write(raw[start:start+1024])
                await asyncio.sleep(.001)
            await response.write_eof()
            return response
        app = web.Application()
        app.router.add_get('/data', streamed)
        server = TestServer(app)
        await server.start_server()
        try:
            session = SimpleNamespace(get=lambda *a, **kw: self.http.get(server.make_url('/data')))
            result = await Inventory(self.root).fetch_json(session, 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json', hashlib.sha1(raw).hexdigest())
            self.assertEqual(result, value)
        finally:
            await server.close()

    async def test_translation_and_admin_work_without_mirror_process(self):
        self.config.admin_token = 'test-admin'
        self.config.mirror_port = 1
        client = TestClient(TestServer(await create_app(self.config, workers=False)))
        await client.start_server()
        try:
            self.assertEqual((await client.get('/health')).status, 200)
            self.assertEqual((await client.get('/v1/catalog/projects')).status, 200)
            for path in ('/mirror', '/admin/mirror'):
                response = await client.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertEqual((await client.get('/v1/admin/mirror')).status, 401)
            headers={'Authorization': 'Bearer test-admin'}
            response = await client.post('/v1/admin/mirror', json={'quota_gib': 200}, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['settings']['quota_gib'], 200)
            response = await client.post('/v1/admin/mirror', json={'policies': {'assets':'redirect'}}, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())['settings']['policies']['libraries'], 'cache')
            self.assertEqual((await client.post('/v1/admin/mirror', json={'policies': {'assets':'invalid'}}, headers=headers)).status, 400)
            self.assertEqual((await client.post('/v1/admin/mirror', json={'concurrency': 100}, headers=headers)).status, 400)
            self.assertEqual((await client.post('/v1/admin/mirror', json=[], headers=headers)).status, 400)
            self.assertEqual((await client.post('/v1/admin/mirror', json={'public_url': 123}, headers=headers)).status, 400)
            sources=[dict(prefix='/maven/'+('a'*160)+str(n)+'.jar',target='https://repo.example.com/'+('a'*160)+str(n)+'.jar',mode='redirect',enabled=True) for n in range(200)]
            self.assertGreater(len(json.dumps(sources)),65536)
            self.assertEqual((await client.post('/v1/admin/mirror',json={'sources':sources},headers=headers)).status,200)
            # Other translation APIs retain the smaller request limit.
            self.assertEqual((await client.post('/v1/admin/mirror',json={'sources':sources})).status,401)
            self.assertEqual((await client.post('/v1/admin/mirror', json={'action':'prefetch'}, headers=headers)).status, 503)
            self.assertEqual((await client.get('/health')).status, 200)
        finally:
            await client.close()

    async def test_mutable_manifest_refresh_ignores_old_inventory_digest(self):
        url = 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'
        await self.mirror.db.execute('INSERT INTO expected VALUES(?,?,?,?)',
                                     (url, '0'*40, 1, 'metadata'))
        await self.mirror.db.commit()
        response = await self.client.get('/mc/game/version_manifest_v2.json')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.payload)
        self.assertEqual(response.headers['X-Mirror-Verified'], 'local-sha256')

    async def test_extensionless_loader_metadata_refreshes_after_ttl(self):
        path = '/fabric-meta/v2/versions/loader'
        first = await self.client.get(path)
        self.assertEqual(first.status, 200)
        await first.read()
        self.assertEqual(self.calls, 1)
        await self.mirror.db.execute('UPDATE cache SET fetched=0')
        await self.mirror.db.commit()
        second = await self.client.get(path)
        self.assertEqual(second.status, 200)
        await second.read()
        self.assertEqual(self.calls, 2)


if __name__ == '__main__':
    unittest.main()
