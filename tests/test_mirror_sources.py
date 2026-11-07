import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.config import Config
from mc_translate.mirror import create_mirror, read_settings, settings_path, validate_settings, upstreams
from mc_translate.mirror_catalog import write_json
from mc_translate.mirror_routing import source_target, source_targets
from mc_translate.mirror_sources import resource_path, validate_registries


def provider(id, region='cn', kind='maven'):
    return dict(id=id,name=id,url='https://'+id+'.example/repository/',kind=kind,region=region,enabled=True)


class SourceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(database=str(self.root/'translations.sqlite'),mirror_root=str(self.root/'mirror'))
        write_json(settings_path(self.config),validate_settings(dict(auto_refresh=False,auto_sources=False)))
        self.client = TestClient(TestServer(await create_mirror(self.config)))
        await self.client.start_server()
        self.mirror = self.client.app['mirror']
        self.mirror.discovery.directory = AsyncMock(return_value=[])
        self.mirror.scheduler.cancel()
        await asyncio.gather(self.mirror.scheduler,return_exceptions=True)
        self.payload = b'file with verified content'
        self.sha = hashlib.sha1(self.payload).hexdigest()
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.path = '/maven/com/example/unique/1.0/unique-1.0.jar'

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def settings(self, registries, **extra):
        write_json(settings_path(self.config),validate_settings(dict(auto_refresh=False,auto_sources=True,source_registries=registries,**extra)))

    def inventory(self, rows=None):
        write_json(self.mirror.root/'inventory.json',dict(report=dict(complete=True),files=rows or [dict(
            path=self.path,url=upstreams(self.path)[0],sha1=self.sha,size=len(self.payload),category='libraries',priority=1)]))

    async def discover(self):
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(return_value=self.sha)) as check:
            await self.mirror.discover_sources()
            return check

    async def seed_cache(self, paths=None):
        paths = paths or [self.path]
        dest = self.mirror.root/'objects'/self.digest[:2]/self.digest
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(self.payload)
        await self.mirror.db.execute('INSERT OR REPLACE INTO objects(digest,size,accessed,priority) VALUES(?,?,?,?)',(self.digest,len(self.payload),time.time(),0))
        for path in paths:
            await self.mirror.db.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?,?,?,?)',
                (upstreams(path)[0],self.digest,len(self.payload),self.sha,1,time.time(),'application/java-archive'))
        await self.mirror.db.execute('UPDATE totals SET bytes=?,files=? WHERE id=1',(len(self.payload),len(paths)))
        await self.mirror.db.commit()
        return dest

    async def test_multiple_providers_incremental_selection_and_no_checks_in_download(self):
        self.settings([provider('foreign','global'),provider('domestic')]);self.inventory()
        check = await self.discover();self.assertEqual(check.await_count,2)
        settings = read_settings(self.config)
        choices = source_targets(settings,self.path,'redirect')
        self.assertIn('domestic.example',choices[0]);self.assertEqual(len(choices),2)
        check = await self.discover();check.assert_not_awaited()
        self.mirror.upstream_available = AsyncMock(return_value=True)
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(side_effect=AssertionError('request must not scan'))):
            response = await self.client.get(self.path,allow_redirects=False)
        self.assertEqual(response.status,307);self.assertEqual(response.headers['Location'],choices[0])
        self.assertEqual((await self.client.head(self.path,allow_redirects=False)).status,307)
        self.assertEqual((await self.mirror.sources.summary())[0]['redirects']+(await self.mirror.sources.summary())[1]['redirects'],1)
        await self.mirror.sources.flush()
        self.assertEqual(sum(row['redirects'] for row in await self.mirror.sources.summary()),1)
        self.mirror.origin_health[choices[0]] = dict(healthy=False)
        self.mirror.upstream_available = AsyncMock(side_effect=lambda url,**kw:url!=choices[0])
        response = await self.client.get(self.path,allow_redirects=False)
        self.assertEqual(response.headers['Location'],choices[1])
        self.settings([provider('foreign','global')]);await self.mirror.sources.synchronize()
        self.assertEqual(len(source_targets(read_settings(self.config),self.path,'redirect')),1)
        await self.mirror.sources.flush()
        self.assertEqual((await self.mirror.sources.summary())[0]['redirects'],1)
        self.settings([provider('foreign','global'),provider('domestic')]);await self.mirror.sources.synchronize()
        self.assertEqual(sum(row['redirects'] for row in await self.mirror.sources.summary()),2)

    async def test_checksum_change_withdraws_mapping_and_transient_failure_expires(self):
        self.settings([provider('domestic')]);self.inventory();await self.discover()
        for record in self.mirror.discovery.records.values():record['next_check']=0
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(side_effect=TimeoutError)):
            await self.mirror.discover_sources()
        self.assertIsNotNone(source_target(read_settings(self.config),self.path,'redirect'))
        for record in self.mirror.discovery.records.values():record.update(next_check=0,verified_at=time.time()-9*86400)
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(side_effect=TimeoutError)):
            await self.mirror.discover_sources()
        self.assertIsNone(source_target(read_settings(self.config),self.path,'redirect'))
        for record in self.mirror.discovery.records.values():record['next_check']=0
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(return_value='0'*40)):
            await self.mirror.discover_sources()
        self.assertEqual((await self.mirror.sources.files({}))['total'],0)

    async def test_new_source_unique_catalogue_file_and_known_hash_conflict(self):
        source = provider('custom',kind='catalogue');source['url']='https://custom.example/files.json'
        self.settings([source]);self.inventory()
        unique = self.path.replace('unique','exclusive')
        rows = [dict(path=unique,sha1=self.sha,target='https://custom.example/exclusive.jar'),
                dict(path=self.path,sha1='f'*40,target='https://custom.example/conflicting.jar')]
        with patch.object(self.mirror.discovery,'catalogue',AsyncMock(return_value=rows)):
            await self.discover()
        settings = read_settings(self.config)
        self.assertEqual(source_target(settings,unique,'redirect'),'https://custom.example/exclusive.jar')
        self.assertIsNone(source_target(settings,self.path,'redirect'))
        files = await self.mirror.sources.files({'source':'custom'})
        self.assertEqual(files['total'],1);self.assertEqual(files['files'][0]['path'],unique)

    async def test_maven_directory_discovers_unique_file_without_global_inventory(self):
        self.settings([provider('custom')])
        self.mirror.discovery.directory = AsyncMock(return_value=['com/example/exclusive.jar'])
        await self.discover()
        mapping=source_target(read_settings(self.config),'/maven/com/example/exclusive.jar','redirect')
        self.assertEqual(mapping,'https://custom.example/repository/com/example/exclusive.jar')
        self.assertEqual((await self.mirror.sources.files({'source':'custom'}))['total'],1)

    async def test_file_statistics_pagination_and_cleanup_preview(self):
        self.settings([provider('domestic')])
        self.inventory([dict(path='/maven/com/example/'+str(i)+'.jar',sha1=self.sha,priority=i) for i in range(52)])
        await self.discover()
        page = await self.mirror.sources.files({'source':'domestic'})
        self.assertEqual(page['total'],52);self.assertEqual(len(page['files']),50);self.assertEqual(page['next_offset'],50)
        self.assertEqual(len((await self.mirror.sources.files({'source':'domestic','offset':50}))['files']),2)
        self.inventory();await self.discover();await self.seed_cache()
        stats = await self.mirror.sources.summary()
        self.assertEqual(stats[0]['files'],1);self.assertEqual(stats[0]['local_files'],1)
        preview = await self.mirror.sources.preview()
        self.assertEqual(preview['files'],1);self.assertEqual(preview['reclaimable_bytes'],len(self.payload))

    async def test_source_local_usage_deduplicates_objects_and_excludes_cloud_only(self):
        self.settings([provider('domestic')])
        paths=[self.path,self.path.replace('unique','shared')]
        self.inventory([dict(path=path,sha1=self.sha,priority=0) for path in paths])
        await self.discover();await self.seed_cache(paths)
        stats=(await self.mirror.sources.summary())[0]
        self.assertEqual((stats['files'],stats['local_files'],stats['local_bytes']),
                         (2,1,len(self.payload)))
        await self.mirror.db.execute('UPDATE objects SET local=0')
        await self.mirror.db.commit()
        stats=(await self.mirror.sources.summary())[0]
        self.assertEqual((stats['files'],stats['local_files'],stats['local_bytes']),(2,0,0))
        page=await self.mirror.sources.files({'source':'domestic'})
        self.assertTrue(all(row['local_bytes'] is None for row in page['files']))

    async def test_cleanup_rechecks_remote_and_respects_serving_and_shared_objects(self):
        self.settings([provider('domestic')]);self.inventory();await self.discover()
        other = '/maven/com/example/shared.jar'
        dest = await self.seed_cache([self.path,other])
        self.assertEqual((await self.mirror.sources.preview())['reclaimable_bytes'],0)
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(return_value=self.sha)),patch.object(self.mirror.discovery,'available',AsyncMock(return_value=False)):
            await self.mirror.sources.cleanup()
        self.assertTrue(dest.exists());self.assertIsNotNone(await self.mirror.cached(upstreams(self.path)[0]))
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(return_value=self.sha)),patch.object(self.mirror.discovery,'available',AsyncMock(return_value=True)):
            self.mirror.serving[self.digest]=1;await self.mirror.sources.cleanup()
            self.assertIsNotNone(await self.mirror.cached(upstreams(self.path)[0]))
            self.mirror.serving.clear();await self.mirror.sources.cleanup()
        self.assertIsNone(await self.mirror.cached(upstreams(self.path)[0]));self.assertTrue(dest.exists())
        self.assertIsNotNone(await self.mirror.cached(upstreams(other)[0]))
        self.assertEqual((await self.mirror.one('SELECT bytes FROM totals'))['bytes'],len(self.payload))

    async def test_cleanup_last_reference_releases_bytes_and_auto_cleanup_is_opt_in(self):
        self.settings([provider('domestic')]);self.inventory();await self.discover();dest=await self.seed_cache()
        self.assertFalse(read_settings(self.config)['source_cleanup'])
        with patch.object(self.mirror.discovery,'checksum',AsyncMock(return_value=self.sha)),patch.object(self.mirror.discovery,'available',AsyncMock(return_value=True)):
            await self.mirror.sources.cleanup()
        self.assertFalse(dest.exists())
        totals = await self.mirror.one('SELECT bytes,files FROM totals')
        self.assertEqual((totals['bytes'],totals['files']),(0,0))

    def test_registry_validation_rejects_private_self_and_duplicate_sources(self):
        public='https://mirror.bugg.icu'
        for url in ('http://public.example/','https://127.0.0.1/','https://localhost/',public+'/',
                    'https://public.example/a/../','https://public.example/repo/?secret=1'):
            with self.assertRaises(ValueError):validate_registries([dict(provider('invalid'),url=url)],public)
        with self.assertRaises(ValueError):validate_registries([provider('one'),provider('one')],public)
        self.assertFalse(resource_path('/v1/admin/tasks'))
        self.assertFalse(resource_path('/assets/app.js'))
        self.assertFalse(resource_path('/maven/../admin'))
        self.assertTrue(resource_path('/maven/org/example/file.jar'))
