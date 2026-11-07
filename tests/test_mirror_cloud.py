"""Cloud copies are optional and only replace local bytes after verification."""
import asyncio
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.config import Config
from mc_translate.mirror import create_mirror, settings_path, validate_settings
from mc_translate.mirror_catalog import write_json
from mc_translate.mirror_policy import POLICIES
from mc_translate.mirror_storage import LOCAL_ROUTES, sha256_file


PROFILE = dict(id='small', bucket='bugg-test', domain='https://kodo.example.net',
               prefix='mirror-test', upload_host='https://up-z0.qiniup.com', private=False,
               quota_gib=1, daily_upload_gib=1)


class FakeKodo:
    objects = {}

    def __init__(self, profile, **kwargs):
        self.profile = profile

    async def put_verified(self, path, key, digest, size, *, progress=None):
        assert sha256_file(path) == (digest, size)
        self.objects[(self.profile['id'], key)] = Path(path).read_bytes()

    def download_url(self, key, *, expires_seconds=None):
        return self.profile['domain'] + '/' + key

    def endpoint_download_url(self, key, *, expires_seconds=None, method='GET'):
        return 'https://s3.example.net/' + key

    async def delete(self, key):
        self.objects.pop((self.profile['id'], key), None)


class CloudCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(database=str(Path(self.temp.name) / 'translations.sqlite'),
                             mirror_root=str(Path(self.temp.name) / 'mirror'))
        self.fake = patch('mc_translate.mirror_cloud.QiniuStorage', FakeKodo)
        self.credential = patch('mc_translate.mirror_cloud.credentials', return_value=('ak', 'sk'))
        self.fake.start()
        self.credential.start()
        FakeKodo.objects.clear()
        self.settings = validate_settings(dict(auto_sources=False, auto_refresh=False,
            quality_enabled=False, policies={**POLICIES, 'assets':'cache'},
            qiniu_profiles=[PROFILE], qiniu_paused=True,
            qiniu_routes={**LOCAL_ROUTES, 'assets': {'mode':'cloud', 'profile':'small', 'path':'game/assets'}}))
        write_json(settings_path(self.config), self.settings)
        self.client = TestClient(TestServer(await create_mirror(self.config)))
        await self.client.start_server()
        self.mirror = self.client.app['mirror']

    async def asyncTearDown(self):
        await self.client.close()
        self.credential.stop()
        self.fake.stop()
        self.temp.cleanup()

    async def test_automatic_retry_recovers_missing_staging_and_leaves_other_work_ready(self):
        digest = 'b' * 64
        url = 'https://libraries.minecraft.net/example/1/example-1.jar'
        await self.mirror.db.execute('INSERT INTO objects(digest,size) VALUES(?,?)', (digest, 20))
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,?,?,?,?)', (url, digest, 20, None, 0, 1, 'application/octet-stream'))
        await self.mirror.db.execute('UPDATE totals SET bytes=20,files=1 WHERE id=1')
        await self.mirror.cloud.enqueue('mirror', digest, 20, 'assets')
        await self.mirror.db.execute("UPDATE cloud_jobs SET state='failed',retry_at=0")
        await self.mirror.cloud.retry_blocked()
        self.assertIsNone(await self.mirror.one('SELECT id FROM cloud_jobs WHERE digest=?', (digest,)))
        failure = await self.mirror.one('SELECT * FROM mirror_failed_files WHERE url=?', (url,))
        self.assertGreater(failure['next_retry'], time.time())
        self.assertEqual((await self.mirror.one('SELECT bytes FROM totals WHERE id=1'))['bytes'], 0)
        self.assertEqual((await self.mirror.one('SELECT local FROM objects WHERE digest=?', (digest,)))['local'], 0)

    async def test_cloud_only_file_keeps_index_and_redirects(self):
        payload = b'verified asset' * 200
        digest = hashlib.sha256(payload).hexdigest()
        sha1 = hashlib.sha1(payload).hexdigest()
        url = 'https://resources.download.minecraft.net/' + sha1[:2] + '/' + sha1
        path = Path(self.config.mirror_root) / 'objects' / digest[:2] / digest
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
        await self.mirror.db.execute('INSERT INTO objects(digest,size) VALUES(?,?)', (digest, len(payload)))
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,?,?,?,?)',
                                     (url, digest, len(payload), sha1, 1, 1, 'application/octet-stream'))
        await self.mirror.db.execute('UPDATE totals SET bytes=?,files=1 WHERE id=1', (len(payload),))
        await self.mirror.db.commit()
        self.assertTrue(await self.mirror.cloud.enqueue('mirror', digest, len(payload), 'assets'))
        job = await self.mirror.one("SELECT * FROM cloud_jobs WHERE scope='mirror'")
        # Unrelated ongoing downloads must not pin an already uploaded object.
        self.mirror.inflight['unrelated-file'] = asyncio.create_task(asyncio.sleep(0))
        with patch.object(self.mirror.cloud, 'check_download', AsyncMock(return_value=True)):
            await self.mirror.cloud.process(job)
        await self.mirror.inflight.pop('unrelated-file')
        self.assertEqual((await self.mirror.one('SELECT local FROM objects WHERE digest=?', (digest,)))['local'], 0)
        self.assertFalse(path.exists())
        self.assertEqual((await self.mirror.one('SELECT bytes FROM totals WHERE id=1'))['bytes'], 0)
        snapshot=await self.mirror.cloud.snapshot()
        self.assertEqual(snapshot['queue']['finished']['count'],1)
        self.assertEqual(snapshot['jobs'][0]['file_path'],url)
        self.assertFalse(snapshot['jobs'][0]['local'])
        self.assertIn('game/assets/sha256/',snapshot['jobs'][0]['object_key'])
        self.assertIsNotNone(await self.mirror.cached(url))
        response = await self.client.get('/assets/' + sha1[:2] + '/' + sha1, allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertIn('game/assets/sha256/', response.headers['Location'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        origin_only = await self.client.get('/assets/' + sha1[:2] + '/' + sha1,
                                            headers={'X-Buggcraft-Origin-Only': '1'}, allow_redirects=False)
        self.assertEqual(origin_only.status, 307)
        self.assertEqual(origin_only.headers['Location'],response.headers['Location'])

    async def test_shared_local_reference_prevents_cloud_cleanup(self):
        payload=b'shared library and asset'
        digest=hashlib.sha256(payload).hexdigest()
        sha1=hashlib.sha1(payload).hexdigest()
        path=Path(self.config.mirror_root)/'objects'/digest[:2]/digest
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
        await self.mirror.db.execute('INSERT INTO objects(digest,size) VALUES(?,?)',(digest,len(payload)))
        for url in ('https://resources.download.minecraft.net/'+sha1[:2]+'/'+sha1,
                    'https://libraries.minecraft.net/com/example/shared/1/shared-1.jar'):
            await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,?,?,?,?)',
                (url,digest,len(payload),sha1,1,1,'application/octet-stream'))
        await self.mirror.db.commit()
        self.assertTrue(await self.mirror.cloud.enqueue('mirror',digest,len(payload),'assets'))
        job=await self.mirror.one("SELECT * FROM cloud_jobs WHERE scope='mirror'")
        with patch.object(self.mirror.cloud,'check_download',AsyncMock(return_value=True)):
            await self.mirror.cloud.process(job)
        self.assertTrue(path.exists())
        self.assertEqual((await self.mirror.one('SELECT local FROM objects WHERE digest=?',(digest,)))['local'],1)

    async def test_paused_cloud_prefetch_does_not_fill_local_disk(self):
        files=[dict(url='https://resources.download.minecraft.net/ab/'+'ab'*20,
                    category='assets',sha1='ab'*20,size=512,path='/assets/ab/'+'ab'*20)]
        write_json(self.mirror.root/'inventory.json',dict(report={'complete':True},files=files))
        await self.mirror.run_job('prefetch')
        self.assertEqual(self.mirror.job_state['skip_reasons']['cloud_paused'],1)
        self.assertEqual((await self.mirror.one('SELECT bytes FROM totals WHERE id=1'))['bytes'],0)

    async def test_cloud_preparation_redirects_player_without_waiting_for_copy(self):
        gate=asyncio.Event()
        async def prepare(*args,**kwargs):
            await gate.wait()
        with patch.object(self.mirror,'ensure',AsyncMock(side_effect=prepare)), \
                patch.object(self.mirror,'upstream_available',AsyncMock(return_value=True)):
            response=await asyncio.wait_for(self.client.get('/assets/ab/'+'ab'*20,allow_redirects=False),1)
            self.assertEqual(response.status,307)
            self.assertEqual(response.headers['X-Mirror-Reason'],'cloud_upload_pending')
            self.assertIn('resources.download.minecraft.net',response.headers['Location'])
            self.assertTrue(self.mirror.backups)
            gate.set();await asyncio.gather(*list(self.mirror.backups.values()))

    async def test_configuration_and_paused_migration(self):
        settings = validate_settings({**self.settings, 'qiniu_profiles':[PROFILE, {**PROFILE,
            'id':'backup', 'bucket':'bugg-backup', 'prefix':'backup', 'private':True}],
            'qiniu_routes':{**self.settings['qiniu_routes'],
            'room_mods':{'mode':'copy', 'profile':'backup', 'path':'room/mods'}}})
        self.assertEqual(settings['qiniu_routes']['room_mods']['profile'], 'backup')
        self.assertEqual((await self.mirror.cloud.snapshot())['jobs'], [])
        self.assertTrue((await self.mirror.cloud.snapshot())['paused'])
        self.assertEqual(await self.mirror.cloud.migrate('assets', 20), 0)

    async def test_migration_finds_older_categories_and_pages_all_jobs(self):
        # Older files must remain selectable behind more than 1,000 newer files.
        records = [(f'{index:064x}', 1) for index in range(1002)]
        await self.mirror.db.executemany('INSERT INTO objects(digest,size) VALUES(?,?)', records)
        urls = ['https://resources.download.minecraft.net/ab/' + 'a'*40] + [
            f'https://piston-data.mojang.com/v1/objects/{index:040x}/client.jar' for index in range(1,1002)]
        await self.mirror.db.executemany('INSERT INTO cache VALUES(?,?,?,?,?,?,?)', [
            (url, digest, 1, None, index, index, 'application/octet-stream')
            for index, (url, (digest, _)) in enumerate(zip(urls, records))])
        await self.mirror.db.commit()
        self.assertEqual(await self.mirror.cloud.migrate('assets', 20), 1)
        self.assertEqual(await self.mirror.cloud.migrate('assets', 20), 0)
        for index in range(30):
            await self.mirror.cloud.enqueue('mirror', f'{index+2000:064x}', 1, 'assets')
        snapshot = await self.mirror.cloud.snapshot()
        self.assertEqual(snapshot['worker_state'], 'paused')
        self.assertEqual(snapshot['categories']['assets']['queued']['count'], 31)
        page = await self.mirror.cloud.jobs_page('assets', 'queued', 20)
        self.assertEqual(page['total'], 31)
        self.assertEqual(len(page['jobs']), 11)
        self.assertEqual(page['jobs'][-1]['file_path'], urls[0])
        response = await self.client.post('/_internal/control', headers={'X-Mirror-Control':self.mirror.secret},
            json={'action':'cloud_jobs','route':'assets','offset':20})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['total'], 31)
        with self.assertRaises(ValueError):
            await self.mirror.cloud.jobs_page('not-a-category')

    async def test_upload_speed_and_waiting_budget_are_observable(self):
        self.mirror.cloud.worker.cancel()
        await asyncio.gather(self.mirror.cloud.worker, return_exceptions=True)
        await self.mirror.cloud.enqueue('mirror', 'd'*64, 1000, 'assets')
        row = await self.mirror.one('SELECT * FROM cloud_jobs')
        write_json(settings_path(self.config), {**self.settings,'qiniu_paused':False})
        cloud = self.mirror.cloud
        cloud.active, cloud.phase, cloud.upload_started = row['id'], 'uploading', time.monotonic()-2
        now = time.monotonic()
        cloud.progress_samples.extend([(now-2, 0), (now, 500)])
        snapshot = await cloud.snapshot()
        self.assertEqual(snapshot['worker_state'], 'uploading')
        self.assertGreater(snapshot['upload_bytes_per_second'], 200)
        cloud.record_progress(1000, 1000)
        self.assertEqual((await cloud.snapshot())['phase'], 'verifying')
        self.assertEqual((await cloud.snapshot())['upload_bytes_per_second'], 0)
        cloud.active = None
        await self.mirror.db.execute("UPDATE cloud_jobs SET retry_at=?", (time.time()+3600,))
        await self.mirror.db.commit()
        self.assertEqual((await cloud.snapshot())['worker_state'], 'waiting_budget')

    async def test_custom_domain_failure_falls_back_to_verified_s3_endpoint(self):
        class Response:
            status=206
            class Content:
                async def read(self, size):return b'x'
            content=Content()
            async def __aenter__(self):return self
            async def __aexit__(self,*_args):return None
        class Probe:
            def get(self,url,**_kwargs):
                if 'kodo.example.net' in url:raise OSError('TLS reset')
                return Response()
        class Storage(FakeKodo):
            def download_url(self,key,**_kwargs):return 'https://kodo.example.net/'+key
            def endpoint_download_url(self,key,**_kwargs):return 'https://s3.example.net/'+key
        with patch.object(self.mirror,'probe_http',Probe()), patch.object(self.mirror.cloud,'storage',return_value=Storage(PROFILE)):
            self.assertTrue(await self.mirror.cloud.check_download('small','game/assets/test'))
            health=self.mirror.cloud.health['small']
            self.assertFalse(health['domain_healthy'])
            self.assertTrue(health['healthy'])
            self.assertEqual(health['download_via'],'endpoint')
            self.assertIn('自定义域名不可用',health['warning'])
        digest='a'*64
        await self.mirror.db.execute("INSERT INTO object_locations VALUES('mirror',?,'small','assets','game/assets/test',1,'ready',1,NULL)",(digest,))
        await self.mirror.db.commit()
        with patch.object(self.mirror.cloud,'storage',return_value=Storage(PROFILE)):
            self.assertEqual(await self.mirror.cloud.download_url('mirror',digest),'https://s3.example.net/game/assets/test')

    async def test_auto_reconciliation_queues_existing_category_objects(self):
        payload=b'preexisting cache bytes'
        digest=hashlib.sha256(payload).hexdigest()
        sha1=hashlib.sha1(payload).hexdigest()
        url='https://resources.download.minecraft.net/'+sha1[:2]+'/'+sha1
        path=self.mirror.root/'objects'/digest[:2]/digest
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
        await self.mirror.db.execute('INSERT INTO objects(digest,size,local) VALUES(?,?,1)',(digest,len(payload)))
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,?,?,?,?)',(url,digest,len(payload),sha1,1,1,'application/octet-stream'))
        await self.mirror.db.commit()
        await self.mirror.cloud.reconcile_batch()
        self.assertEqual((await self.mirror.one('SELECT COUNT(*) n FROM cloud_jobs WHERE digest=?',(digest,)))['n'],1)
        self.assertEqual((await self.mirror.cloud.snapshot())['automatic']['queued'],1)
        await self.mirror.cloud.reconcile_batch()
        self.assertEqual((await self.mirror.one('SELECT COUNT(*) n FROM cloud_jobs WHERE digest=?',(digest,)))['n'],1)

    async def test_reconciliation_checkpoint_survives_restart_and_explains_skips(self):
        from mc_translate.mirror_cloud import MirrorCloud
        batch = patch('mc_translate.mirror_cloud.INDEX_BATCH', 100)
        batch.start()
        self.addCleanup(batch.stop)
        for number in range(105):
            digest = hashlib.sha256(str(number).encode()).hexdigest()
            url = 'https://libraries.minecraft.net/example/' + str(number)
            await self.mirror.db.execute('INSERT INTO objects(digest,size,local) VALUES(?,10,1)', (digest,))
            await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,10,NULL,1,1,NULL)', (url,digest))
        await self.mirror.db.commit()
        await self.mirror.cloud.reconcile_batch()
        before = dict(self.mirror.cloud.automatic)
        self.assertEqual(before['scanned_total'], 100)
        # Libraries retain the local route in this fixture; they create no cloud task.
        self.assertEqual(before['outcomes']['local_only'], 100)
        restored = MirrorCloud(self.mirror, self.mirror.cloud.settings)
        await restored.open()
        self.assertEqual(restored.automatic, before)
        await restored.reconcile_batch()
        self.assertEqual(restored.automatic['scanned_total'], 105)
        self.assertEqual(restored.automatic['state'], 'watching')
        self.assertEqual(restored.automatic['outcomes']['local_only'], 105)
        await restored.reconcile_batch()
        self.assertEqual(restored.automatic['scanned_total'], 105)
        self.assertEqual(restored.automatic['round'], 1)

    async def test_redirect_policy_migrates_existing_bytes_without_fetching_new_files(self):
        settings = {**self.settings, 'policies': {**self.settings['policies'], 'assets':'redirect'}}
        write_json(settings_path(self.config), settings)
        digest = 'b' * 64
        url = 'https://resources.download.minecraft.net/ab/' + 'a' * 40
        await self.mirror.db.execute('INSERT INTO objects(digest,size,local) VALUES(?,10,1)', (digest,))
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,10,NULL,1,1,NULL)', (url,digest))
        await self.mirror.db.commit()
        await self.mirror.cloud.reconcile_batch()
        self.assertEqual(self.mirror.cloud.automatic['outcomes'], {'queued':1})
        self.assertEqual(self.mirror.cloud.automatic['queued'], 1)
        self.assertEqual(self.mirror.cloud.settings()['policies']['assets'], 'redirect')
        settings['policies']['assets'] = 'cache'
        write_json(settings_path(self.config), settings)
        await self.mirror.db.execute("INSERT INTO object_locations VALUES('mirror',?,'small','assets',?,10,'ready',1,NULL)",
            (digest, 'mirror-test/game/assets/sha256/bb/'+digest))
        await self.mirror.db.commit()
        await self.mirror.cloud.reconcile_batch()
        self.assertEqual(self.mirror.cloud.automatic['outcomes'], {'already_cloud':1})
        self.assertEqual((await self.mirror.one('SELECT COUNT(*) n FROM cloud_jobs'))['n'], 1)

    async def test_concurrent_uploads_report_each_file_and_continue_after_failure(self):
        cloud = self.mirror.cloud
        gate = asyncio.Event()
        started = []
        async def upload(path, key, digest, size, *, progress=None):
            started.append(digest)
            progress(size//2, size)
            await gate.wait()
            if digest == '1'*64:
                raise OSError('one file failed')
        for number in range(1, 4):
            digest = str(number)*64
            path = self.mirror.root/'objects'/digest[:2]/digest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'data')
            await cloud.enqueue('mirror', digest, 4, 'assets')
        write_json(settings_path(self.config), {**self.settings, 'qiniu_paused':False, 'concurrency':2})
        async def wait_until(predicate):
            for _ in range(150):
                if predicate():
                    return
                await asyncio.sleep(.02)
            self.fail('cloud worker did not make progress')
        with patch.object(cloud.storage('small'), 'put_verified', AsyncMock(side_effect=upload)), \
             patch.object(cloud, 'check_download', AsyncMock(return_value=True)):
            await wait_until(lambda: len(started)==2)
            snapshot = await cloud.snapshot()
            self.assertEqual(len(snapshot['active_jobs']), 2)
            self.assertEqual(snapshot['upload_concurrency'], 2)
            self.assertTrue(all(job['progress']==2 for job in snapshot['active_jobs']))
            self.assertTrue(all(cloud.uploading(str(number)*64) for number in (1,2)))
            gate.set()
            await wait_until(lambda: len(started)==3 and not cloud.transfers)
        states = []
        async with self.mirror.db.execute('SELECT state FROM cloud_jobs ORDER BY id') as cursor:
            states = [row['state'] for row in await cursor.fetchall()]
        self.assertEqual(states, ['failed', 'finished', 'finished'])
        self.assertFalse(cloud.reservations)

    async def test_parallel_uploads_reserve_space_and_daily_budget_atomically(self):
        cloud = self.mirror.cloud
        for limit in ('quota_gib', 'daily_upload_gib'):
            with self.subTest(limit=limit):
                await self.mirror.db.execute('DELETE FROM cloud_jobs')
                await self.mirror.db.execute('DELETE FROM object_locations')
                await self.mirror.db.execute('DELETE FROM cloud_budget')
                profile = {**PROFILE, 'quota_gib':2, 'daily_upload_gib':2, limit:1}
                write_json(settings_path(self.config), {**self.settings, 'qiniu_profiles':[profile]})
                jobs = []
                for number in range(2):
                    digest = str(number+7)*64
                    path = self.mirror.root/'objects'/digest[:2]/digest
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'not transferred in this test')
                    await cloud.enqueue('mirror', digest, 600_000_000, 'assets')
                async with self.mirror.db.execute('SELECT * FROM cloud_jobs ORDER BY id') as cursor:
                    jobs = await cursor.fetchall()
                gate = asyncio.Event()
                async def upload(*args, **kwargs):
                    await gate.wait()
                with patch.object(cloud.storage('small'), 'put_verified', AsyncMock(side_effect=upload)) as put, \
                     patch.object(cloud, 'check_download', AsyncMock(return_value=True)):
                    tasks = [asyncio.create_task(cloud.process(job)) for job in jobs]
                    for _ in range(100):
                        row = await self.mirror.one('SELECT state,retry_at FROM cloud_jobs WHERE id=?', (jobs[1]['id'],))
                        if row['state']=='waiting_capacity' or row['retry_at']>time.time():
                            break
                        await asyncio.sleep(.01)
                    self.assertEqual(put.await_count, 1)
                    self.assertEqual(len(cloud.reservations), 1)
                    gate.set()
                    await asyncio.gather(*tasks)
                self.assertEqual((await self.mirror.one('SELECT uploaded FROM cloud_budget'))['uploaded'], 600_000_000)

    async def test_network_upload_failure_retries_without_removing_local_bytes(self):
        from mc_translate.mirror_storage import S3Storage
        cloud = self.mirror.cloud
        digest = '4'*64
        path = self.mirror.root/'objects'/digest[:2]/digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'data')
        await cloud.enqueue('mirror', digest, 4, 'assets')
        row = await self.mirror.one('SELECT * FROM cloud_jobs')
        failure = S3Storage._error('upload', OSError('connection closed'))
        with patch.object(cloud.storage('small'), 'put_verified', AsyncMock(side_effect=failure)):
            await cloud.process(row)
        result = await self.mirror.one('SELECT state,attempts,retry_at,error FROM cloud_jobs')
        self.assertEqual(result['state'], 'queued')
        self.assertEqual(result['attempts'], 1)
        self.assertGreater(result['retry_at'], time.time())
        self.assertIn('自动重试', result['error'])
        self.assertTrue(path.exists())
        self.assertFalse(cloud.reservations)
        self.assertFalse(cloud.transfers)

    async def test_batch_reconciliation_does_not_hold_writes_across_other_commits(self):
        self.mirror.cloud.reconcile_task.cancel()
        await asyncio.gather(self.mirror.cloud.reconcile_task, return_exceptions=True)
        for number in range(220):
            digest = f'{number:064x}'
            url = 'https://resources.download.minecraft.net/ab/'+f'{number:040x}'
            await self.mirror.db.execute('INSERT INTO objects(digest,size,local) VALUES(?,10,1)', (digest,))
            await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,10,NULL,1,1,NULL)', (url,digest))
        await self.mirror.db.commit()
        async def other_activity():
            for _ in range(100):
                await self.mirror.db.commit()
                await asyncio.sleep(0)
        await asyncio.gather(self.mirror.cloud.reconcile_batch(), other_activity())
        self.assertEqual((await self.mirror.one('SELECT COUNT(*) n FROM cloud_jobs'))['n'], 220)
        self.assertEqual(self.mirror.cloud.automatic['queued'], 220)

    async def test_selected_cleanup_rechecks_aliases_and_preserves_cloud_copy(self):
        settings = {**self.settings, 'policies': {**self.settings['policies'], 'assets':'redirect'}}
        write_json(settings_path(self.config), settings)
        digest = 'e' * 64
        payload = b'old local resource'
        path = self.mirror.root/'objects'/digest[:2]/digest
        path.parent.mkdir(parents=True);path.write_bytes(payload)
        asset = 'https://resources.download.minecraft.net/ab/'+'a'*40
        library = 'https://libraries.minecraft.net/example/shared.jar'
        await self.mirror.db.execute('INSERT INTO objects(digest,size,local) VALUES(?,?,1)', (digest,len(payload)))
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,NULL,1,1,NULL)', (asset,digest,len(payload)))
        await self.mirror.db.execute('UPDATE totals SET bytes=?,files=1 WHERE id=1', (len(payload),))
        await self.mirror.db.commit()
        preview = await self.mirror.cloud.local_cleanup_candidates()
        self.assertEqual([row['digest'] for row in preview['files']], [digest])
        with self.assertRaises(ValueError):
            await self.mirror.cloud.cleanup_local(None)
        self.assertTrue(path.exists())
        # A cached library alias added after the preview protects the same bytes.
        await self.mirror.db.execute('INSERT INTO cache VALUES(?,?,?,NULL,1,1,NULL)', (library,digest,len(payload)))
        await self.mirror.db.commit()
        self.assertEqual((await self.mirror.cloud.cleanup_local([digest]))['removed'], 0)
        self.assertTrue(path.exists())
        await self.mirror.db.execute('DELETE FROM cache WHERE url=?', (library,))
        await self.mirror.db.execute("INSERT INTO object_locations VALUES('mirror',?,'small','assets','stored-file',?,'ready',1,NULL)", (digest,len(payload)))
        await self.mirror.db.commit()
        result = await self.mirror.cloud.cleanup_local([digest])
        self.assertEqual(result, dict(removed=1,freed_bytes=len(payload),skipped=0))
        self.assertFalse(path.exists())
        self.assertEqual((await self.mirror.one('SELECT local FROM objects WHERE digest=?',(digest,)))['local'], 0)
        self.assertIsNotNone(await self.mirror.cloud.location('mirror',digest))
        self.assertIsNotNone(await self.mirror.one('SELECT 1 FROM cache WHERE url=?',(asset,)))
        self.assertEqual((await self.mirror.one('SELECT bytes FROM totals WHERE id=1'))['bytes'], 0)

    async def test_cloud_budget_is_atomic_and_checks_do_not_spend(self):
        settings = {**self.settings, 'qiniu_profiles': [{**PROFILE,
            'quota_unit': 'GB', 'monthly_egress_gib': 1, 'egress_threshold': 90}]}
        write_json(settings_path(self.config), settings)
        await self.mirror.db.execute("INSERT INTO object_locations VALUES('mirror','digest','small','assets','test',600000000,'ready',1,NULL)")
        await self.mirror.db.commit()
        for _ in range(3):
            self.assertIsNotNone(await self.mirror.cloud.download_url('mirror', 'digest'))
        profile = self.mirror.cloud.profile('small')
        self.assertEqual((await self.mirror.cloud.egress(profile))['bytes'], 0)
        results = await asyncio.gather(*[self.mirror.cloud.download_url('mirror', 'digest', reserve=True) for _ in range(3)])
        self.assertEqual(sum(bool(result) for result in results), 1)
        self.assertEqual((await self.mirror.cloud.egress(profile))['bytes'], 600000000)
        self.assertIsNone(await self.mirror.cloud.download_url('mirror', 'digest'))

    async def test_capped_room_cloud_preserves_last_local_copy(self):
        settings = {**self.settings, 'qiniu_profiles': [{**PROFILE,
            'private': True, 'monthly_egress_gib': 400}]}
        write_json(settings_path(self.config), settings)
        cache = self.mirror.room_cache
        cache.path('d' * 64).write_bytes(b'room backup')
        await cache.db.execute('INSERT INTO objects(digest,size,created) VALUES(?,?,?)', ('d' * 64, 11, time.time()))
        await cache.db.commit()
        await self.mirror.db.execute("INSERT INTO object_locations VALUES('room',?,'small','room_mods','test',11,'ready',1,NULL)", ('d' * 64,))
        await self.mirror.db.commit()
        self.assertFalse(await self.mirror.cloud.release_local('room', 'd' * 64))
        self.assertTrue(cache.path('d' * 64).is_file())

    async def test_private_room_copy_redirect_and_expiry_cleanup(self):
        settings = validate_settings({**self.settings, 'qiniu_profiles': [{**PROFILE, 'private': True, 'monthly_egress_gib': 400}],
            'qiniu_routes': {**self.settings['qiniu_routes'],
                'room_mods': {'mode': 'cloud', 'profile': 'small', 'path': 'room/mods'}}})
        write_json(settings_path(self.config), settings)
        payload = b'private room mod' * 100
        digest = hashlib.sha256(payload).hexdigest()
        cache = self.mirror.room_cache
        cache.path(digest).write_bytes(payload)
        await cache.db.execute('INSERT INTO objects(digest,size,created) VALUES(?,?,?)',
                               (digest, len(payload), time.time()))
        await cache.db.execute('''INSERT INTO files(scope,digest,path,size,sha1,urls,touched,lease_until)
            VALUES(?,?,?,?,?,?,?,?)''',
            ('room-1', digest, 'mods/test.jar', len(payload), hashlib.sha1(payload).hexdigest(),
             '[]', time.time(), time.time() + 3600))
        await cache.db.commit()
        self.assertTrue(await self.mirror.cloud.enqueue('room', digest, len(payload), 'room_mods'))
        job = await self.mirror.one("SELECT * FROM cloud_jobs WHERE scope='room'")
        with patch.object(self.mirror.cloud, 'check_download', AsyncMock(return_value=True)):
            await self.mirror.cloud.process(job)
        self.assertFalse(cache.path(digest).exists())
        response = await self.client.get('/room-cache/files/' + digest, allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertIn('/room/mods/sha256/', response.headers['Location'])
        await cache.db.execute('UPDATE files SET lease_until=? WHERE digest=?', (time.time() - 1, digest))
        await cache.db.commit()
        await cache.cleanup()
        self.assertIsNone(await cache.one('SELECT * FROM objects WHERE digest=?', (digest,)))
        self.assertIsNone(await self.mirror.cloud.location('room', digest))
        self.assertFalse(FakeKodo.objects)
