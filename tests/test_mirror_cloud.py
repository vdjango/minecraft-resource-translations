"""Cloud copies are optional and only replace local bytes after verification."""
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
        with patch.object(self.mirror.cloud, 'check_download', AsyncMock(return_value=True)):
            await self.mirror.cloud.process(job)
        self.assertEqual((await self.mirror.one('SELECT local FROM objects WHERE digest=?', (digest,)))['local'], 0)
        self.assertFalse(path.exists())
        self.assertEqual((await self.mirror.one('SELECT bytes FROM totals WHERE id=1'))['bytes'], 0)
        self.assertIsNotNone(await self.mirror.cached(url))
        response = await self.client.get('/assets/' + sha1[:2] + '/' + sha1, allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertIn('game/assets/sha256/', response.headers['Location'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_configuration_and_paused_migration(self):
        settings = validate_settings({**self.settings, 'qiniu_profiles':[PROFILE, {**PROFILE,
            'id':'backup', 'bucket':'bugg-backup', 'prefix':'backup', 'private':True}],
            'qiniu_routes':{**self.settings['qiniu_routes'],
            'room_mods':{'mode':'copy', 'profile':'backup', 'path':'room/mods'}}})
        self.assertEqual(settings['qiniu_routes']['room_mods']['profile'], 'backup')
        self.assertEqual((await self.mirror.cloud.snapshot())['jobs'], [])
        self.assertTrue((await self.mirror.cloud.snapshot())['paused'])
        self.assertEqual(await self.mirror.cloud.migrate('assets', 20), 0)

    async def test_private_room_copy_redirect_and_expiry_cleanup(self):
        settings = validate_settings({**self.settings, 'qiniu_profiles': [{**PROFILE, 'private': True}],
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
