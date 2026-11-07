"""Batch public matching must not block host transfers or replace file bytes."""
import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.config import Config
from mc_translate.mirror import create_mirror, settings_path, validate_settings
from mc_translate.mirror_catalog import write_json
from mc_translate.room_sources import fingerprint, identities, matching_file
from test_room_cache import token, SECRET


class PublicSourceCase(unittest.IsolatedAsyncioTestCase):
    async def test_raising_speed_threshold_keeps_and_queues_the_backup(self):
        self.publisher()
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        digest=self.file['sha256']
        self.assertFalse(await self.lookup.cloud_needed(digest))
        settings=self.lookup.cache.settings()
        settings['quality_slow_kibps']=2000
        write_json(settings_path(self.config),settings)
        self.assertTrue(await self.lookup.cloud_needed(digest))
        await self.cache.db.execute('UPDATE objects SET created=?',(time.time()-61,))
        await self.cache.db.commit()
        await self.lookup.settle()
        self.assertTrue(self.cache.path(digest).is_file())
        self.assertIsNotNone(await self.mirror.one("SELECT 1 FROM cloud_jobs WHERE scope='room' AND digest=?",(digest,)))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(mirror_root=str(Path(self.temp.name)/'mirror'))
        settings = validate_settings(dict(auto_sources=False,auto_refresh=False,quality_enabled=False,
            qiniu_paused=True,qiniu_profiles=[dict(id='test',bucket='test',domain='https://cdn.example.org',
                prefix='room',upload_host='https://up-z0.qiniup.com',quota_gib=1,daily_upload_gib=1,private=True)],
            qiniu_routes={'room_mods':dict(mode='cloud',profile='test',path='mods')}))
        write_json(settings_path(self.config),settings)
        self.env = patch.dict(os.environ,{'TRANSLATION_ROOM_CACHE_SECRET':SECRET})
        self.env.start()
        self.disk = patch('mc_translate.room_cache.shutil.disk_usage',return_value=SimpleNamespace(free=1024**4))
        self.disk.start()
        self.client = TestClient(TestServer(await create_mirror(self.config)))
        await self.client.start_server()
        self.mirror = self.client.app['mirror']
        self.cache = self.mirror.room_cache
        self.lookup = self.cache.discovery
        self.lookup.worker.cancel()
        await asyncio.gather(self.lookup.worker,return_exceptions=True)
        self.payload = b'identical public file\n' * 50
        self.file = dict(path='mods/example-fabric-1.21.jar',size=len(self.payload),
            sha1=hashlib.sha1(self.payload).hexdigest(),sha256=hashlib.sha256(self.payload).hexdigest(),downloads=[])
        self.published = dict(id=4567,modId=123,fileFingerprint=1234,gameId=432,
            fileLength=self.file['size'],hashes=[dict(algo=1,value=self.file['sha1'])],
            downloadUrl='https://edge.forgecdn.net/files/4/567/example.jar')
        self.lookup.sample = AsyncMock(return_value=('available',1000,True))

    async def asyncTearDown(self):
        await self.client.close()
        self.disk.stop()
        self.env.stop()
        self.temp.cleanup()

    async def plan(self, files=None):
        files = files or [self.file]
        response = await self.client.post('/room-cache/plan',json={'files':files},
            headers={'Authorization':'Bearer '+token(files)})
        self.assertEqual(response.status,200,await response.text())
        return await response.json()

    async def upload(self):
        response = await self.client.put('/room-cache/files/'+self.file['sha256'],data=self.payload,
            headers={'Authorization':'Bearer '+token([self.file])})
        self.assertEqual(response.status,200,await response.text())
        return await response.json()

    def publisher(self, found=True):
        async def query(method,path,**kwargs):
            if path=='/fingerprints/432':
                return {'exactMatches':[dict(file=self.published)] if found else []}
            if path.endswith('/files/'+str(self.published['id'])):
                return self.published
            return []
        self.lookup.request = AsyncMock(side_effect=query)

    async def test_plan_and_first_upload_never_wait_for_publisher_or_cloud(self):
        self.lookup.request = AsyncMock(side_effect=AssertionError('network on room path'))
        plan = await self.plan()
        self.assertEqual(plan['public_lookup'],'background')
        self.assertEqual(plan['summary']['upload'],1)
        receipt = await self.upload()
        self.assertEqual(receipt['state'],'cached')
        self.lookup.request.assert_not_awaited()
        self.assertFalse(await self.lookup.cloud_needed(self.file['sha256']))
        self.assertIsNone(await self.mirror.one("SELECT id FROM cloud_jobs WHERE scope='room'"))
        response = await self.client.get('/room-cache/files/'+self.file['sha256'])
        self.assertEqual(await response.read(),self.payload)

    async def test_upload_without_claimed_sha1_registers_actual_verified_hash(self):
        self.file.pop('sha1')
        await self.plan()
        self.assertIsNone(await self.cache.one('SELECT digest FROM public_lookups'))
        await self.upload()
        lookup=await self.cache.one('SELECT sha1 FROM public_lookups')
        self.assertEqual(lookup['sha1'],hashlib.sha1(self.payload).hexdigest())

    async def test_fingerprints_are_batched_and_mismatching_sha1_is_rejected(self):
        self.file['fingerprint']=1234
        other = dict(self.file,path='mods/unrelated.jar',sha256='e'*64,sha1='d'*40,fingerprint=9876)
        self.publisher()
        await self.plan([self.file,other])
        await self.lookup.cycle()
        calls = [call for call in self.lookup.request.call_args_list if call.args[1]=='/fingerprints/432']
        self.assertEqual(len(calls),1)
        self.assertEqual(set(calls[0].kwargs['json']['fingerprints']),{1234,9876})
        result = await self.plan([self.file,other])
        self.assertEqual({item['sha256']:item['action'] for item in result['files']},
                         {self.file['sha256']:'redirect',other['sha256']:'upload'})
        catalog = await self.cache.one('SELECT * FROM catalog_files')
        self.assertEqual(catalog['sha256'],'')  # SHA-256 claim was never uploaded/verified.

    async def test_late_mapping_releases_only_idle_temporary_copy(self):
        self.publisher()
        self.file['downloads']=['https://cdn.modrinth.com/data/test/versions/test/example.jar']
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        digest = self.file['sha256']
        self.assertEqual((await self.plan())['files'][0]['action'],'redirect')
        response = await self.client.get('/room-cache/files/'+digest,allow_redirects=False)
        self.assertEqual(response.headers['Location'],self.published['downloadUrl'])
        self.assertEqual(await self.cache.fast_public_source(self.file['downloads'][0]),self.published['downloadUrl'])
        await self.cache.db.execute('UPDATE objects SET created=?',(time.time()-61,))
        await self.cache.db.commit()
        self.cache.serving[digest]=1
        await self.lookup.settle()
        self.assertTrue(self.cache.path(digest).is_file())
        self.cache.serving.clear()
        await self.lookup.settle()
        self.assertFalse(self.cache.path(digest).exists())
        self.assertIsNone(await self.mirror.one("SELECT id FROM cloud_jobs WHERE scope='room'"))

    async def test_failed_public_download_uses_received_fallback_even_after_mapping(self):
        self.publisher()
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        digest = self.file['sha256']
        response = await self.client.post('/room-cache/resolve',json={'wanted':[digest],'failed':[digest]})
        self.assertEqual((await response.json())['files'][0]['action'],'cached')
        response = await self.client.get('/room-cache/files/'+digest,allow_redirects=False)
        self.assertEqual(response.status,200)
        self.assertEqual(await response.read(),self.payload)
        self.assertTrue(await self.lookup.cloud_needed(digest))

    async def test_existing_cloud_copy_survives_mapping_for_already_issued_downloads(self):
        self.publisher()
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        digest=self.file['sha256']
        await self.cache.db.execute('UPDATE objects SET created=?',(time.time()-61,))
        await self.cache.db.commit()
        await self.mirror.db.execute("INSERT INTO object_locations VALUES('room',?,'test','room_mods','sample',?,'ready',?,NULL)",
                                     (digest,len(self.payload),time.time()))
        await self.mirror.db.commit()
        self.mirror.cloud.delete_room=AsyncMock(side_effect=AssertionError('existing signed downloads must survive'))
        await self.lookup.settle()
        self.assertFalse(self.cache.path(digest).exists())
        self.assertEqual((await self.cache.one('SELECT local FROM objects WHERE digest=?',(digest,)))['local'],0)
        self.assertIsNotNone(await self.mirror.cloud.location('room',digest))
        self.mirror.cloud.delete_room.assert_not_awaited()

    async def test_modrinth_only_or_private_file_is_retained_without_downloading_modrinth(self):
        self.publisher(found=False)
        self.file['downloads']=['https://cdn.modrinth.com/data/test/versions/test/example.jar']
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        job = await self.cache.one('SELECT state FROM public_lookups')
        self.assertEqual(job['state'],'not_found')
        self.assertTrue(await self.lookup.cloud_needed(self.file['sha256']))
        await self.lookup.settle()
        self.assertIsNotNone(await self.mirror.one("SELECT id FROM cloud_jobs WHERE scope='room'"))
        self.assertTrue(self.cache.path(self.file['sha256']).is_file())
        self.assertTrue(all('modrinth' not in call.args[1] for call in self.lookup.request.call_args_list))

    async def test_lookup_error_has_bounded_grace_and_independent_backoff(self):
        await self.plan()
        await self.upload()
        self.lookup.request=AsyncMock(side_effect=ValueError('CurseForge 元数据 HTTP 429'))
        await self.lookup.cycle()
        job=await self.cache.one('SELECT * FROM public_lookups')
        self.assertEqual(job['state'],'retry')
        self.assertGreater(job['next_at'],time.time())
        self.assertIn('429',job['error'])
        self.assertTrue(await self.lookup.cloud_needed(self.file['sha256']))
        self.lookup.request.reset_mock()
        await self.lookup.cycle()
        self.lookup.request.assert_not_awaited()

    async def test_room_close_preserves_public_mapping_but_stops_room_work(self):
        self.publisher()
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        response=await self.client.post('/room-cache/release',json={},
            headers={'Authorization':'Bearer '+token([self.file])})
        self.assertEqual(response.status,200)
        await self.cache.cleanup()
        self.assertIsNone(await self.cache.one('SELECT digest FROM files'))
        result=await (await self.client.post('/room-cache/resolve',json={'wanted':[self.file['sha1']]})).json()
        self.assertEqual(result['files'][0]['downloads'],[self.published['downloadUrl']])
        self.assertEqual((await self.lookup.snapshot())['recent'],[])
        self.lookup.request.reset_mock()
        await self.lookup.cycle()
        self.lookup.request.assert_not_awaited()

    async def test_slow_matching_source_keeps_body_and_cloud_policy(self):
        self.publisher()
        self.lookup.sample=AsyncMock(return_value=('slow',42,False))
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        self.assertEqual((await self.plan())['files'][0]['action'],'cached')
        self.assertTrue(await self.lookup.cloud_needed(self.file['sha256']))

    async def test_cdn_unavailable_tries_other_public_host_without_changing_file(self):
        from mc_translate.room_sources import PublicFileDiscovery
        match=matching_file(self.published,self.file['sha1'],self.file['size'])
        self.lookup.sample_one=AsyncMock(side_effect=[ValueError('CurseForge 下载 HTTP 404'),('available',2048,True)])
        self.assertEqual(await PublicFileDiscovery.sample(self.lookup,match),('available',2048,True))
        self.assertEqual(match['url'],'https://mediafilez.forgecdn.net/files/4/567/example.jar')

    async def test_background_worker_cancels_with_ready_wake_event(self):
        self.lookup.wake.set()
        worker=asyncio.create_task(self.lookup.run())
        await asyncio.sleep(0)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(worker,timeout=2)

    async def test_discovery_reads_shared_health_without_extra_network_probe(self):
        from mc_translate.room_sources import PublicFileDiscovery
        match=matching_file(self.published,self.file['sha1'],self.file['size'])
        with patch.object(self.lookup.http,'get',side_effect=AssertionError('discovery cannot duplicate samples')):
            self.assertEqual(await PublicFileDiscovery.sample_one(self.lookup,match),('available',0,False))
        self.assertIsNone(await self.cache.one('SELECT bytes FROM budget'))

    async def test_shared_slow_and_recovered_health_applies_to_discovery(self):
        from mc_translate.room_sources import PublicFileDiscovery
        match=dict(url=self.published['downloadUrl'],size=128*1024)
        metric=self.mirror.quality.content_metric
        with patch.object(self.mirror.quality,'content_metric',return_value=dict(health='slow',recent_speed_bps=64*1024)):
            self.assertEqual(await PublicFileDiscovery.sample_one(self.lookup,match),('slow',64,False))
        with patch.object(self.mirror.quality,'content_metric',return_value=dict(health='healthy',recent_speed_bps=2048*1024)):
            self.assertEqual(await PublicFileDiscovery.sample_one(self.lookup,match),('available',2048,True))

    async def test_private_file_and_failed_search_are_deleted_after_room_close(self):
        self.publisher(found=False)
        await self.plan()
        await self.upload()
        await self.lookup.cycle()
        digest=self.file['sha256']
        self.assertIsNotNone(await self.cache.one('SELECT 1 FROM public_lookups WHERE digest=?',(digest,)))
        response=await self.client.post('/room-cache/release',json={},
            headers={'Authorization':'Bearer '+token([self.file])})
        self.assertEqual(response.status,200)
        await self.cache.cleanup()
        self.assertFalse(self.cache.path(digest).exists())
        self.assertIsNone(await self.cache.one('SELECT 1 FROM files WHERE digest=?',(digest,)))
        self.assertIsNone(await self.cache.one('SELECT 1 FROM public_lookups WHERE digest=?',(digest,)))
        self.assertIsNone(await self.cache.one('SELECT 1 FROM catalog_files'))
        result=await (await self.client.post('/room-cache/resolve',json={'wanted':[self.file['sha1']]})).json()
        self.assertEqual(result['files'],[])

    async def test_project_refresh_indexes_new_hashes_without_entities(self):
        self.publisher()
        self.file['fingerprint']=1234
        await self.plan()
        await self.lookup.cycle()
        await self.cache.db.execute('UPDATE public_projects SET next_at=0')
        await self.cache.db.commit()
        new=dict(self.published,id=4568,hashes=[dict(algo=1,value='c'*40)],
            downloadUrl='https://edge.forgecdn.net/files/4/568/example-new.jar')
        await self.cache.db.execute("UPDATE public_lookups SET url='https://mediafilez.forgecdn.net/files/4/567/example.jar'")
        await self.cache.db.commit()
        self.lookup.request=AsyncMock(return_value=[new])
        await self.lookup.refresh_project()
        result=await (await self.client.post('/room-cache/resolve',json={'wanted':['c'*40]})).json()
        self.assertEqual(result['files'][0]['downloads'],['https://mediafilez.forgecdn.net/files/4/568/example-new.jar'])
        self.assertEqual(list((self.cache.root/'objects').iterdir()),[])


class ArchiveCase(unittest.TestCase):
    def test_identity_reading_is_bounded_and_never_extracts_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ambiguous.jar'
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('fabric.mod.json',json.dumps({'id':'actual_mod','name':'Actual Mod'}))
                archive.writestr('../outside.txt','never extracted')
                archive.writestr('mcmod.info','x'*(256*1024+1))
            self.assertEqual(identities(path),['Actual Mod','actual_mod'])
            self.assertEqual(list(Path(folder).iterdir()),[path])
            # Fingerprint excludes all four whitespace bytes consistently.
            left,right=Path(folder)/'left',Path(folder)/'right'
            left.write_bytes(b'ab c\tde\r\nfgh')
            right.write_bytes(b'abcdefgh')
            self.assertEqual(fingerprint(left),fingerprint(right))

    def test_name_and_fingerprint_never_override_missing_or_different_sha1(self):
        file=dict(id=1,modId=2,fileLength=100,fileName='exact same name.jar',
                  downloadUrl='https://edge.forgecdn.net/files/1/2/exact.jar',hashes=[])
        self.assertIsNone(matching_file(file,'a'*40,100))
        file['hashes']=[dict(algo=1,value='b'*40)]
        self.assertIsNone(matching_file(file,'a'*40,100))
