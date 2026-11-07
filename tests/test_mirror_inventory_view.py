"""Catalogue composition, actual copies and routing remain distinct."""
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.config import Config
from mc_translate.mirror import create_mirror, settings_path, validate_settings
from mc_translate.mirror_catalog import write_json


class InventoryViewCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(database=str(Path(self.temp.name)/'translation.sqlite'),
                             mirror_root=str(Path(self.temp.name)/'mirror'))
        write_json(settings_path(self.config), validate_settings(dict(auto_sources=False, auto_refresh=False,
            quality_enabled=False, room_cache_probe_enabled=False, prefetch_platform=['windows'],
            policies=dict(core='redirect', loaders='cache', mods='cache', java='cache', libraries='redirect', assets='redirect'))))
        self.client = TestClient(TestServer(await create_mirror(self.config)))
        await self.client.start_server()
        self.mirror = self.client.app['mirror']
        self.view = self.mirror.observation.inventory_view
        self.items = [dict(url='https://piston-data.mojang.com/v1/objects/'+f'{i:040x}'+'/'+name,
                           path='/'+name,category=category,size=100+i,sha1=f'{i:040x}',platforms=platform)
                      for i,(name,category,platform) in enumerate([
                          ('client.jar','game-client',[]),('server.jar','game-server',[]),
                          ('java.dll','java-raw',['windows']),('libjava.so','java-raw',['linux']),
                          ('java.dll.lzma','java-lzma',['windows']),('java-metadata.json','java-metadata',[]),
                          ('client.txt','game-client_mappings',[])])]
        write_json(self.mirror.root/'inventory.json',dict(files=self.items))

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_explains_every_entry_and_filters_real_files(self):
        page = await self.view.files({})
        self.assertEqual(page['total'],7)
        self.assertEqual(sum(c['total'] for c in page['categories']),7)
        self.assertEqual(page['eligible'],2)
        self.assertEqual(page['reasons'],dict(redirect_policy=1,server_file=1,platform=1,
                                            duplicate_compression=1,developer_mapping=1))
        windows = await self.view.files(dict(category='java-raw',handling='eligible'))
        self.assertEqual([f['name'] for f in windows['files']],['java.dll'])
        linux = await self.view.files(dict(handling='platform'))
        self.assertEqual(linux['files'][0]['name'],'libjava.so')
        self.assertIn('未选择',linux['files'][0]['reason_label'])
        self.assertEqual((await self.view.files(dict(query=self.items[2]['sha1'])))['matched'],1)

    async def test_actual_copy_is_independent_of_policy_and_not_a_completed_job(self):
        item=self.items[0]
        digest='a'*64
        await self.mirror.db.execute('INSERT INTO objects(digest,size,local) VALUES(?,?,?)',(digest,100,0))
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,?,?,?,?)',
                                    (item['url'],digest,100,item['sha1'],1,time.time(),'application/octet-stream'))
        await self.mirror.db.execute("INSERT INTO object_locations(scope,digest,profile,route,object_key,size,state,verified_at) VALUES(?,?,?,?,?,?,?,?)",
                                    ('mirror',digest,'rainyun1','core','mirror/core/object',100,'ready',time.time()))
        await self.mirror.db.commit()
        page=await self.view.files(dict(handling='stored'))
        self.assertEqual(page['matched'],1)
        self.assertFalse(page['files'][0]['eligible'])
        self.assertEqual(page['files'][0]['state'],'stored')
        self.assertEqual(page['files'][0]['cloud'][0]['profile'],'rainyun1')
        self.assertEqual(page['storage']['local_files'],0)
        self.assertEqual(page['storage']['cloud'][0]['files'],1)
        self.assertEqual(len((await self.mirror.observation.files(dict(storage='cloud')))['files']),1)
        self.assertEqual((await self.mirror.observation.files(dict(storage='local')))['files'],[])

    async def test_pagination_summary_refresh_and_control_auth(self):
        files=[dict(self.items[2],url=self.items[2]['url']+'?id='+str(i)) for i in range(57)]
        write_json(self.mirror.root/'inventory.json',dict(files=files))
        first=await self.view.files({})
        second=await self.view.files(dict(offset=first['next_offset']))
        self.assertEqual((len(first['files']),len(second['files'])),(50,7))
        self.assertIsNone(second['next_offset'])
        summary=await self.view.files(dict(summary_only=True))
        self.assertEqual(summary['files'],[])
        self.assertEqual(summary['total'],57)
        response=await self.client.post('/_internal/control',json=dict(action='inventory_files'))
        self.assertEqual(response.status,403)
        response=await self.client.post('/_internal/control',headers={'X-Mirror-Control':self.mirror.secret},
                                        json=dict(action='inventory_files',handling='platform'))
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['matched'],0)
        for data in (dict(handling='bad'),dict(offset=-1),dict(query='x'*161)):
            with self.assertRaises(ValueError):
                await self.view.files(data)
