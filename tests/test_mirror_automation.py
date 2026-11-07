"""Automatic rebuild must honor scope and keep failures out of the hot loop."""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiosqlite

from mc_translate.mirror import validate_settings
from mc_translate.mirror_automation import AutomaticSync, prefetch_exclusion


class AutomaticCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = await aiosqlite.connect(':memory:')
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript('''CREATE TABLE cache(url,object);CREATE TABLE objects(digest,local);
            CREATE TABLE object_locations(scope,digest,state);CREATE TABLE mirror_failed_files(url);''')
        self.auto = AutomaticSync(SimpleNamespace(root=self.root, db=self.db))
        self.settings = validate_settings({'prefetch_platform':['windows','macos'], 'policies':{'core':'cache','java':'cache'}})

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    def item(self, name, category='java-raw', platform='windows'):
        return dict(url='https://piston-data.mojang.com/v1/objects/'+hashlib.sha1(name.encode()).hexdigest()+'/'+name,
                    category=category, platform=platform)

    async def test_scope_includes_mac_and_windows_but_not_server_or_duplicate_java(self):
        files=[self.item('java.dll'),self.item('libjvm.dylib',platform='macos'),
               self.item('libjvm.so',platform='linux'),self.item('java.lzma',category='java-lzma'),
               self.item('server.jar',category='game-server'),self.item('client.txt',category='game-client_mappings')]
        selected=await self.auto.batch(files,self.settings)
        self.assertEqual({r['url'].rsplit('/',1)[-1] for r in selected},{'java.dll','libjvm.dylib'})
        self.assertEqual(prefetch_exclusion(files[2],self.settings),'platform')
        self.assertEqual(self.auto.state['round_counts'],dict(queued=2,platform=1,
            duplicate_compression=1,server_file=1,developer_mapping=1))

    async def test_unchanged_inventory_refills_after_cache_clear_and_skips_failed_files(self):
        files=[self.item('one.dll'),self.item('two.dll'),self.item('three.dll')]
        await self.db.execute('INSERT INTO objects VALUES(?,1)',('digest',))
        await self.db.execute('INSERT INTO cache VALUES(?,?)',(files[0]['url'],'digest'))
        await self.db.execute('INSERT INTO mirror_failed_files VALUES(?)',(files[1]['url'],))
        selected=await self.auto.batch(files,self.settings)
        self.assertEqual([r['url'] for r in selected],[files[2]['url']])
        self.assertEqual(self.auto.state['round_counts'],dict(existing=1,retrying=1,queued=1))
        await self.db.execute('DELETE FROM cache')
        self.auto.state['next_at']=0
        selected=await self.auto.batch(files,self.settings)
        self.assertEqual({r['url'] for r in selected},{files[0]['url'],files[2]['url']})
        self.assertEqual(self.auto.state['round_counts'],dict(retrying=1,queued=2))
        restored=AutomaticSync(SimpleNamespace(root=self.root,db=self.db))
        self.assertEqual(restored.state['scanned'],6)

    async def test_cloud_ready_copy_is_reused_and_capacity_defers_rebuild(self):
        item=self.item('one.dll')
        await self.db.execute('INSERT INTO objects VALUES(?,0)',('digest',))
        await self.db.execute('INSERT INTO cache VALUES(?,?)',(item['url'],'digest'))
        await self.db.execute("INSERT INTO object_locations VALUES('mirror',?,'ready')",('digest',))
        self.assertEqual(await self.auto.batch([item],self.settings),[])
        self.auto.wait_capacity()
        self.assertGreater(self.auto.state['next_at'],time.time()+290)
        self.assertEqual(json.loads(self.auto.path.read_text())['state'],'waiting_capacity')
