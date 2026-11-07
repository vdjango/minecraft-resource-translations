"""Exact-file direct upload grants and verified cloud-only room copies."""
import hashlib
import time
import unittest
from unittest.mock import patch

import test_room_cache as fixtures
from mc_translate.mirror import settings_path
from mc_translate.mirror_catalog import write_json
from mc_translate.mirror_storage import LOCAL_ROUTES, S3Storage, StorageError


class FakeS3(S3Storage):
    objects = {}

    def __init__(self, profile, **_):
        self.profile = profile

    def direct_upload(self, key, digest, size, *, expires):
        return dict(method='PUT', url='https://cloud.example.test/'+key, headers={})

    async def verify_direct(self, key, digest, size):
        data = self.objects.get(key,b'')
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise StorageError('checksum mismatch')

    def download_url(self, key, **_):
        return 'https://cloud.example.test/'+key

    endpoint_download_url = download_url

    async def delete(self, key):
        self.objects.pop(key,None)


class DirectRoomTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RoomCacheCase.asyncSetUp
    asyncTearDown = fixtures.RoomCacheCase.asyncTearDown
    start = fixtures.RoomCacheCase.start
    auth = fixtures.RoomCacheCase.auth
    url = fixtures.RoomCacheCase.url

    async def configure(self, **overrides):
        self.settings.update(room_cache_quota_mib=0,qiniu_paused=False,
            qiniu_profiles=[dict(id='direct',name='Direct S3',bucket='direct-test',domain='https://cloud.example.test',
                prefix='mirror',provider='rainyun',endpoint='https://s3.example.test',region='test-1',
                upload_host='',private=True,quota_gib=1,daily_upload_gib=1)],
            qiniu_routes={**LOCAL_ROUTES,'room_mods':dict(mode='cloud',profile='direct',path='rooms/mods')})
        self.settings.update(overrides)
        write_json(settings_path(self.config),self.settings)
        self.fake = patch('mc_translate.mirror_cloud.S3Storage',FakeS3)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        FakeS3.objects.clear()

    async def plan(self, headers=None):
        response = await self.client.post('/room-cache/plan',json={'files':[self.file],'direct_upload':True},headers=headers or self.auth())
        self.assertEqual(response.status,200,await response.text())
        return await response.json()

    async def confirm(self, identifier, headers=None):
        response = await self.client.post('/room-cache/uploads/complete',json={'uploads':[identifier]},headers=headers or self.auth())
        return response,await response.json() if response.status==200 else None

    async def test_missing_file_grant_cloud_verification_and_download_without_local_bytes(self):
        await self.configure()
        plan = await self.plan()
        ticket = plan['upload'][0]['direct_upload']
        direct = self.cache.mirror.cloud.direct
        row = await self.cache.mirror.one('SELECT * FROM room_direct_uploads WHERE id=?',(ticket['id'],))
        self.assertEqual((await self.confirm(ticket['id']))[1]['files'][0]['state'],'pending')
        self.assertIsNone(await self.cache.cached(self.file['sha256']))
        FakeS3.objects[row['object_key']] = self.payload
        result = (await self.confirm(ticket['id']))[1]['files'][0]
        self.assertEqual(result['state'],'cached')
        self.assertFalse(self.cache.path(self.file['sha256']).exists())
        self.assertEqual((await self.plan())['summary']['cloud_ready'],1)
        response = await self.client.get(self.url(),allow_redirects=False)
        self.assertEqual(response.status,307)
        self.assertEqual(response.headers['Location'],'https://cloud.example.test/'+row['object_key'])
        await self.confirm(ticket['id'])  # Idempotent; never count the budget twice.
        spent = await self.cache.mirror.one('SELECT uploaded FROM cloud_budget')
        self.assertEqual(spent['uploaded'],self.file['size'])
        self.assertEqual((await direct.reserved('direct',int(time.time()//86400)))['bytes'],0)

    async def test_room_close_and_foreign_room_cannot_publish_an_upload(self):
        await self.configure()
        ticket = (await self.plan())['upload'][0]['direct_upload']
        row = await self.cache.mirror.one('SELECT * FROM room_direct_uploads WHERE id=?',(ticket['id'],))
        FakeS3.objects[row['object_key']] = self.payload
        foreign = (await self.confirm(ticket['id'],self.auth(room='b'*32)))[1]
        self.assertEqual(foreign['files'][0]['state'],'rejected')
        self.assertEqual((await self.confirm('f'*32))[1]['files'][0]['state'],'rejected')
        response,_ = await self.confirm(ticket['id'],self.auth(write=False))
        self.assertEqual(response.status,401)
        await self.client.post('/room-cache/release',headers=self.auth())
        self.assertEqual((await self.confirm(ticket['id']))[1]['files'][0]['state'],'rejected')
        reserved = await self.cache.mirror.cloud.direct.reserved('direct',int(time.time()//86400))
        self.assertEqual(reserved['bytes'],self.file['size'])
        await self.cache.mirror.db.execute('UPDATE room_direct_uploads SET expires=?',(time.time()-121,))
        await self.cache.mirror.db.commit()
        await self.cache.mirror.cloud.direct.cleanup()
        self.assertFalse(FakeS3.objects)

    async def test_paused_upload_protection_and_cloud_capacity_are_honored(self):
        await self.configure(qiniu_paused=True)
        self.assertNotIn('direct_upload',(await self.plan())['upload'][0])
        self.settings['qiniu_paused']=False
        write_json(settings_path(self.config),self.settings)
        await self.cache.mirror.db.execute("INSERT INTO object_locations VALUES('mirror',?,'direct','mods','full',?,'ready',?,NULL)",('f'*64,1024**3,time.time()))
        await self.cache.mirror.db.commit()
        item = (await self.plan())['upload'][0]
        self.assertTrue(item['direct_deferred'])
        self.assertNotIn('direct_upload',item)
