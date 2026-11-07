"""Pack/room downloads share verified repository indexes and existing mirror bytes."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.config import Config
from mc_translate.mirror import create_mirror, settings_path, validate_settings
from mc_translate.mirror_catalog import write_json


class FileAccelerationCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(database=str(Path(self.temp.name)/'translations.sqlite'),
                             mirror_root=str(Path(self.temp.name)/'mirror'))
        self.settings = validate_settings(dict(auto_refresh=False, quality_enabled=False,
            room_cache_enabled=False, room_cache_public_lookup_enabled=False))
        write_json(settings_path(self.config), self.settings)
        self.client = TestClient(TestServer(await create_mirror(self.config)))
        await self.client.start_server()
        self.mirror = self.client.app['mirror']
        self.payload = b'public library file' * 500
        self.sha1 = hashlib.sha1(self.payload).hexdigest()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.origin = 'https://libraries.minecraft.net/org/example/lib/1/lib-1.jar'
        self.path = '/maven/org/example/lib/1/lib-1.jar'
        self.target = 'https://maven.aliyun.com/repository/public/org/example/lib/1/lib-1.jar'
        self.mirror.ensure = AsyncMock(side_effect=AssertionError('foreground origin download'))

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def mapping(self):
        await self.mirror.db.execute('INSERT INTO expected VALUES(?,?,?,?)',
            (self.origin,self.sha1,len(self.payload),'libraries'))
        await self.mirror.db.execute('INSERT INTO source_files VALUES(?,?,?,?,?,?)',
            ('aliyun',self.path,self.target,self.origin,self.sha1,time.time()+3600))
        await self.mirror.db.commit()

    async def lookup(self, wanted=None):
        response = await self.client.post('/room-cache/resolve',json={'wanted':wanted or [self.sha1]})
        self.assertEqual(response.status,200,await response.text())
        return (await response.json())['files']

    async def copy(self, checked=1):
        path = self.mirror.root/'objects'/self.digest[:2]/self.digest
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(self.payload)
        await self.mirror.db.execute('INSERT INTO objects(digest,size,accessed) VALUES(?,?,?)',
            (self.digest,len(self.payload),time.time()))
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,?,?,?,?)',
            (self.origin,self.digest,len(self.payload),self.sha1,checked,1,'application/octet-stream'))
        await self.mirror.db.commit()
        return path

    async def test_repository_match_works_without_room_or_temporary_cache(self):
        await self.mapping()
        rows = await self.lookup()
        self.assertEqual(rows[0]['action'],'redirect')
        self.assertEqual(rows[0]['downloads'],[self.target])
        self.assertEqual(rows[0]['size'],len(self.payload))
        self.assertEqual((await self.mirror.room_cache.one('SELECT COUNT(*) n FROM files'))['n'],0)
        self.mirror.ensure.assert_not_awaited()

    async def test_expired_disabled_and_unknown_size_mappings_are_not_routes(self):
        await self.mapping()
        await self.mirror.db.execute('UPDATE source_files SET expires=1')
        self.assertEqual(await self.lookup(),[])
        await self.mirror.db.execute('UPDATE source_files SET expires=?',(time.time()+3600,))
        self.settings['auto_sources']=False
        write_json(settings_path(self.config),self.settings)
        self.assertEqual(await self.lookup(),[])
        self.settings['auto_sources']=True
        write_json(settings_path(self.config),self.settings)
        await self.mirror.db.execute('UPDATE expected SET size=NULL')
        self.assertEqual(await self.lookup(),[])

    async def test_slow_aliyun_uses_another_hash_identical_repository(self):
        await self.mapping()
        target = 'https://repo.huaweicloud.com/repository/maven/org/example/lib/1/lib-1.jar'
        await self.mirror.db.execute('INSERT INTO source_files VALUES(?,?,?,?,?,?)',
            ('huawei',self.path,target,self.origin,self.sha1,time.time()+3600))
        self.mirror.quality.recent['aliyun']=[dict(at=time.time(),outcome='error',speed=0,bytes=0)]
        rows=await self.lookup()
        self.assertEqual(rows[0]['downloads'],[target])

    async def test_equal_quality_respects_domestic_preference(self):
        await self.mapping()
        central = 'https://repo.maven.apache.org/maven2/org/example/lib/1/lib-1.jar'
        await self.mirror.db.execute('INSERT INTO source_files VALUES(?,?,?,?,?,?)',
            ('central',self.path,central,self.origin,self.sha1,time.time()+3600))
        rows = await self.lookup()
        self.assertEqual(rows[0]['downloads'],[self.target,central])

    async def test_regular_copy_has_no_room_lease_and_supports_range(self):
        path=await self.copy()
        for digest in (self.sha1,self.digest):
            rows=await self.lookup([digest])
            self.assertEqual(rows[0]['action'],'cached')
            response=await self.client.get(rows[0]['url'],headers={'Range':'bytes=0-15'})
            self.assertEqual(response.status,206)
            self.assertEqual(await response.read(),self.payload[:16])
        path.unlink()
        self.assertEqual(await self.lookup(),[])
        self.mirror.ensure.assert_not_awaited()

    async def test_unchecked_copy_and_cache_miss_do_not_trigger_download(self):
        await self.copy(checked=0)
        self.assertEqual(await self.lookup(),[])
        self.assertEqual(await self.lookup(['f'*40]),[])
        self.mirror.ensure.assert_not_awaited()
