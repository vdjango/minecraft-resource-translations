"""Real HTTP metadata -> requested bytes -> public Range download lifecycle."""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from mc_translate.config import Config
from mc_translate.mirror import create_mirror, settings_path, validate_settings
from mc_translate.room_cache import keys
from mc_translate.mirror_catalog import write_json

SECRET = 'room-origin-integration-secret-0123456789'


def token(files, *, room='a' * 32, write=True, expires=None):
    groups = {kind: [file['sha256'] for file in files if file['path'].startswith('mods/') == (kind == 'mods')]
              for kind in ('mods', 'resources')}
    now = int(time.time())
    claims = dict(v=1, iss='default', room=room, config='c' * 64, write=write, iat=now,
                  exp=now + 300 if expires is None else expires)
    for kind, digests in groups.items():
        claims[kind + '_count'] = len(digests)
        claims[kind + '_hash'] = hashlib.sha256('\n'.join(sorted(digests)).encode()).hexdigest()
    body = base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode()).rstrip(b'=')
    signature = base64.urlsafe_b64encode(hmac.digest(SECRET.encode(), body, 'sha256')).rstrip(b'=')
    return (body + b'.' + signature).decode()


class RoomCacheCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(database=str(Path(self.temp.name) / 'translations.sqlite'),
                             mirror_root=str(Path(self.temp.name) / 'mirror'))
        self.settings = validate_settings(dict(auto_sources=False, auto_refresh=False, quality_enabled=False,
                                               room_cache_public_lookup_enabled=False,
                                               room_cache_probe_enabled=False, reserve_gib=10))
        write_json(settings_path(self.config), self.settings)
        self.env = patch.dict(os.environ, {'TRANSLATION_ROOM_CACHE_SECRET': SECRET, 'TRANSLATION_ROOM_CACHE_KEYS': '{}'})
        self.env.start()
        self.disk = patch('mc_translate.room_cache.shutil.disk_usage', return_value=SimpleNamespace(
            free=1024 ** 4, total=2 * 1024 ** 4, used=1024 ** 4))
        self.disk.start()
        self.payload = b'byte-identical room content\n' * 1000
        self.file = dict(path='mods/private.jar', size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest(),
                         sha1=hashlib.sha1(self.payload).hexdigest(), downloads=[])
        await self.start()

    async def start(self):
        self.client = TestClient(TestServer(await create_mirror(self.config)))
        await self.client.start_server()
        self.cache = self.client.app['mirror'].room_cache

    async def asyncTearDown(self):
        await self.client.close()
        self.disk.stop()
        self.env.stop()
        self.temp.cleanup()

    def auth(self, files=None, **kwargs):
        return {'Authorization': 'Bearer ' + token(files or [self.file], **kwargs)}

    async def plan(self, files=None, **kwargs):
        files = files or [self.file]
        response = await self.client.post('/room-cache/plan', json={'files': files}, headers=self.auth(files, **kwargs))
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    def url(self):
        return '/room-cache/files/' + self.file['sha256']

    async def upload(self, payload=None, headers=None):
        response = await self.client.put(self.url(), data=self.payload if payload is None else payload,
                                         headers=headers or self.auth())
        return response

    async def test_node_invitation_http_claim_is_one_use(self):
        from mc_translate.room_node_access import NodeAccess
        registry = NodeAccess(self.config.mirror_root)
        invitation = registry.invite('https://origin.example.org', '测试节点')
        encoded = invitation.split('.', 1)[1]
        ticket = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))['ticket']
        payload = {'ticket': ticket, 'node_id': 'node_1'}
        response = await self.client.post('/room-cache/nodes/claim', json=payload)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        credentials = await response.json()
        self.assertEqual(keys(self.config.mirror_root)[credentials['issuer']], credentials['secret'])
        replay = await self.client.post('/room-cache/nodes/claim', json=payload)
        self.assertEqual(replay.status, 403)
        malformed = await self.client.post('/room-cache/nodes/claim', json=[])
        self.assertEqual(malformed.status, 400)

    async def test_metadata_first_and_public_download_has_no_identity_gate(self):
        result = await self.plan()
        self.assertEqual([item['sha256'] for item in result['upload']], [self.file['sha256']])
        self.assertEqual(list((self.cache.root / 'objects').iterdir()), [])
        self.assertEqual((await self.upload()).status, 200)
        response = await self.client.get(self.url())  # Deliberately no Authorization.
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.payload)
        response = await self.client.get(self.url(), headers={'Range': 'bytes=101-999'})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.headers['Content-Range'], f'bytes 101-999/{len(self.payload)}')
        self.assertEqual(await response.read(), self.payload[101:1000])
        head = await self.client.head(self.url())
        self.assertEqual(head.status, 200)
        self.assertEqual(int(head.headers['Content-Length']), len(self.payload))
        self.assertEqual((await self.plan())['upload'], [])
        self.assertEqual((await self.cache.snapshot())['files'], 1)

    async def test_batch_manifest_reuses_verified_mirror_and_releases_closed_room(self):
        files = [dict(path=f'mods/item-{n}.jar', size=64,
                      sha256=hashlib.sha256(f'room-{n}'.encode()).hexdigest(), downloads=[])
                 for n in range(43)]
        mirror_file = files[0]
        digest = mirror_file['sha256']
        path = self.cache.mirror.root / 'objects' / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'mirror file')
        mirror_file['size'] = path.stat().st_size
        await self.cache.mirror.db.execute('INSERT INTO objects(digest,size,accessed) VALUES(?,?,?)',
                                           (digest, mirror_file['size'], time.time()))
        await self.cache.mirror.db.commit()
        public_file = files[1]
        public_url = 'https://edge.forgecdn.net/files/1234/567/public.jar'
        await self.cache.import_catalog({'files': [dict(url=public_url, sha256=public_file['sha256'],
            size=public_file['size'], state='available', speed_kibps=2048)]})
        result = await self.plan(files)
        self.assertEqual(result['summary']['total'], 43)
        self.assertEqual(result['summary']['cached'], 1)
        self.assertEqual(result['summary']['redirect'], 1)
        self.assertEqual(result['summary']['upload'], 41)
        self.assertEqual(result['summary']['bytes_to_upload'], 41 * 64)
        self.assertEqual(result['files'][0]['reason_code'], 'mirror_cached')
        self.assertEqual(result['files'][1]['downloads'], [public_url])
        response = await self.client.get('/room-cache/files/' + digest)
        self.assertEqual((response.status, await response.read()), (200, b'mirror file'))
        released = await self.client.post('/room-cache/release', headers=self.auth(files))
        self.assertEqual(released.status, 200)
        self.assertEqual((await released.json())['released'], 43)
        self.assertEqual((await self.client.get('/room-cache/files/' + digest)).status, 404)
        self.assertTrue(path.is_file())  # Other mirror resources are never deleted.

    async def test_only_authorized_provider_can_register_or_supply_requested_bytes(self):
        self.assertEqual((await self.client.post('/room-cache/plan', json={'files': [self.file]})).status, 401)
        self.assertEqual((await self.upload()).status, 403)  # No plan requested it yet.
        await self.plan()
        for headers in ({}, self.auth(write=False), self.auth(expires=int(time.time()) - 1),
                        {'Authorization': self.auth()['Authorization'][:-1] + '!'}, self.auth(room='b' * 32)):
            response = await self.client.put(self.url(), data=self.payload, headers=headers)
            self.assertIn(response.status, (401, 403))
        wrong = dict(self.file, sha256='f' * 64)
        response = await self.client.post('/room-cache/plan', json={'files': [wrong]}, headers=self.auth())
        self.assertEqual(response.status, 400)
        # Public resolver cannot introduce a new source/path or allow an upload.
        response = await self.client.post('/room-cache/resolve', json={'wanted': ['f' * 64], 'files': [wrong]})
        self.assertEqual(response.status, 400)

    async def test_corrupt_upload_is_never_published_and_can_be_repaired(self):
        await self.plan()
        self.assertEqual((await self.upload(b'x' * len(self.payload))).status, 422)
        self.assertFalse(self.cache.path(self.file['sha256']).exists())
        self.assertEqual(list((self.cache.root / 'tmp').iterdir()), [])
        self.assertEqual((await self.client.get(self.url())).status, 503)
        self.assertEqual((await self.upload()).status, 200)

    async def test_public_sha1_lookup_and_expiry_recover_without_manual_cache_management(self):
        await self.plan()
        self.assertEqual((await self.upload()).status, 200)
        response = await self.client.post('/room-cache/resolve', json={'wanted': [self.file['sha1']]})
        self.assertEqual((await response.json())['files'][0]['action'], 'cached')
        await self.cache.db.execute('UPDATE files SET lease_until=1')
        await self.cache.db.commit()
        await self.cache.cleanup()
        self.assertFalse(self.cache.path(self.file['sha256']).exists())
        self.assertEqual((await self.cache.one('SELECT COUNT(*) n FROM files'))['n'], 0)
        poll=await self.client.post('/room-cache/plan',json={},headers=self.auth())
        self.assertEqual((await poll.json())['upload'], [])
        response = await self.client.post('/room-cache/resolve', json={
            'wanted': [self.file['sha256']], 'failed': [self.file['sha256']]})
        self.assertEqual(response.status, 200)
        # Expired metadata is gone. A new explicit manifest starts fresh demand.
        self.assertEqual(len((await self.plan(files=[self.file]))['upload']), 1)
        self.assertEqual((await self.upload()).status, 200)
        self.assertEqual(await (await self.client.get(self.url())).read(), self.payload)

    async def test_plan_distinguishes_received_bytes_from_verified_cached_files(self):
        await self.plan()
        self.cache.upload_progress[self.file['sha256']]=123
        result=await self.plan()
        self.assertEqual((result['summary']['receiving'],result['summary']['receiving_bytes']),(1,123))
        self.assertEqual(result['summary']['cached'],0)
        self.cache.upload_progress.clear()
        receipt=await (await self.upload()).json()
        self.assertEqual(receipt,dict(state='cached',sha256=self.file['sha256'],size=len(self.payload)))
        result=await self.plan()
        self.assertEqual(result['summary']['cached'],1)
        self.assertEqual(result['summary']['cached_bytes'],len(self.payload))

    async def test_unknown_speed_redirects_without_probe_then_failed_file_requests_fallback(self):
        url = 'https://mediafilez.forgecdn.net/files/1234/567/private.jar'
        self.file['downloads'] = [url]
        result = await self.plan()
        self.assertEqual(result['upload'], [])
        self.assertEqual(result['files'][0]['action'], 'redirect')
        response = await self.client.get(self.url(), allow_redirects=False)
        self.assertEqual(response.status, 307)
        self.assertEqual(response.headers['Location'], url)  # mediafilez is never replaced.
        response = await self.client.post('/room-cache/resolve', json={'wanted': [self.file['sha256']], 'failed': [self.file['sha256']]})
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.plan())['upload'][0]['sha256'], self.file['sha256'])

    async def test_catalog_hash_and_speed_decide_without_fetching(self):
        url = 'https://edge.forgecdn.net/files/1234/567/private.jar'
        self.file['downloads'] = [url]
        self.assertEqual((await self.plan())['files'][0]['action'], 'redirect')
        entry = dict(url=url, sha256=self.file['sha256'], sha1=self.file['sha1'],
                     size=self.file['size'], state='slow', speed_kibps=10)
        for state, speed, action in (('slow', 10, 'upload'),
                                     ('available', 800, 'redirect'),
                                     ('unavailable', 0, 'upload')):
            entry.update(state=state, speed_kibps=speed)
            await self.cache.import_catalog({'files': [entry]})
            self.assertEqual((await self.plan())['files'][0]['action'], action)
        entry['sha256'] = '0' * 64
        entry['sha1'] = '0' * 40
        await self.cache.import_catalog({'files': [entry]})
        self.assertEqual((await self.plan())['files'][0]['action'], 'redirect')

    async def test_room_intersection_keeps_one_object_until_last_independent_lease(self):
        await self.plan()  # A starts at 0 hours; 8-hour lease.
        self.assertEqual((await self.upload()).status, 200)
        first = await self.cache.one('SELECT MAX(lease_until) expiry FROM files')
        # Represent the passage of five hours without mocking aiohttp's clock.
        await self.cache.db.execute('UPDATE files SET lease_until=lease_until-18000')
        await self.cache.db.commit()
        await self.plan(room='b' * 32)  # B now gets a fresh eight hours, no second body.
        async with self.cache.db.execute('SELECT lease_until FROM files ORDER BY lease_until') as cursor:
            leases = [row[0] for row in await cursor.fetchall()]
        self.assertAlmostEqual(leases[1] - leases[0], 18000, delta=3)
        self.assertAlmostEqual(leases[1], first['expiry'], delta=3)
        self.assertEqual(len(list((self.cache.root / 'objects').iterdir())), 1)
        # A's expiry alone does not evict B's active reference.
        await self.cache.db.execute('UPDATE files SET lease_until=0 WHERE lease_until=?', (leases[0],))
        await self.cache.db.commit()
        await self.cache.cleanup()
        self.assertTrue(self.cache.path(self.file['sha256']).exists())
        await self.plan(room='b' * 32)
        newest = (await self.cache.one('SELECT MAX(lease_until) expiry FROM files'))['expiry']
        self.assertAlmostEqual(newest, leases[1], delta=.01)  # Polling never renews B.
        await self.cache.db.execute('UPDATE files SET lease_until=1')
        await self.cache.db.commit()
        self.cache.serving[self.file['sha256']] = 1
        await self.cache.cleanup()
        self.assertTrue(self.cache.path(self.file['sha256']).exists())
        self.cache.serving.clear()
        await self.cache.cleanup()
        self.assertFalse(self.cache.path(self.file['sha256']).exists())

    async def test_restart_preserves_index_and_removes_partial_upload(self):
        await self.plan()
        self.assertEqual((await self.upload()).status, 200)
        (self.cache.root / 'tmp' / 'interrupted.part').write_bytes(b'partial')
        await self.client.close()
        await self.start()
        self.assertEqual(await (await self.client.get(self.url())).read(), self.payload)
        self.assertEqual(list((self.cache.root / 'tmp').iterdir()), [])

    async def test_quota_and_two_uploads_cannot_race_space_reservation(self):
        self.settings['room_cache_quota_mib'] = 1
        write_json(settings_path(self.config), self.settings)
        self.payload = b'x' * (2 * 1024 ** 2)
        self.file.update(size=len(self.payload), sha256=hashlib.sha256(self.payload).hexdigest(), sha1=hashlib.sha1(self.payload).hexdigest())
        await self.plan()
        self.assertEqual((await self.upload()).status, 507)
        self.assertEqual((await self.cache.snapshot())['files'], 0)
        self.assertEqual(self.cache.reserved, 0)

    async def test_zero_local_room_quota_still_allows_independent_cloud_staging(self):
        profile=dict(id='roomcloud',provider='s3',bucket='room-files',domain='https://s3.example.net',
            endpoint='https://s3-api.example.net',region='cn-nb1',addressing_style='path',private=True,
            prefix='room',upload_host='https://up-z0.qiniup.com',quota_gib=10,daily_upload_gib=1)
        self.settings.update(room_cache_quota_mib=0,room_cache_staging_mib=1,
            qiniu_profiles=[profile],qiniu_routes={**self.settings['qiniu_routes'],
                'room_mods':dict(mode='cloud',profile='roomcloud',path='room/mods')})
        write_json(settings_path(self.config),self.settings)
        await self.plan()
        response=await self.upload()
        self.assertEqual(response.status,200,await response.text())
        usage=await self.cache.storage_usage()
        self.assertEqual(usage,dict(local_bytes=0,staging_bytes=len(self.payload)))
        self.assertEqual(self.cache.reserved_by_kind,dict(local=0,staging=0))

    async def test_overlapping_uploads_are_bounded_and_do_not_publish_partial_file(self):
        await self.plan()
        gate = asyncio.Event()
        async def chunks():
            yield self.payload[:1]
            await gate.wait()
            yield self.payload[1:]
        first = asyncio.create_task(self.client.put(self.url(), data=chunks(), headers={**self.auth(), 'Content-Length': str(len(self.payload))}))
        try:
            async with asyncio.timeout(3):
                while not self.cache.uploading:
                    await asyncio.sleep(.01)
            self.assertEqual((await self.upload()).status, 429)
            self.assertFalse(self.cache.path(self.file['sha256']).exists())
        finally:
            gate.set()
        self.assertEqual((await first).status, 200)
        self.assertEqual(self.cache.reserved, 0)

    async def test_room_submissions_do_not_pollute_operator_catalog(self):
        self.file['downloads'] = ['https://edge.forgecdn.net/files/1234/567/private.jar']
        await self.plan()
        self.assertEqual((await self.cache.snapshot())['catalog']['files'], 0)
        self.assertIsNone(await self.cache.one('SELECT host FROM sources LIMIT 1'))

    async def test_public_batch_lookup_uses_bounded_queries_and_survives_room_cache_disabled(self):
        files=[dict(url=f'https://edge.forgecdn.net/files/1/{i}/example.jar',sha1=f'{i:040x}',
                    size=65536,state='available') for i in range(1,301)]
        await self.cache.import_catalog({'files':files})
        self.settings['room_cache_enabled']=False
        write_json(settings_path(self.config),self.settings)
        statements=[]
        await self.cache.db.set_trace_callback(statements.append)
        response=await self.client.post('/room-cache/resolve',json={'wanted':[file['sha1'] for file in files]})
        self.assertEqual(response.status,200,await response.text())
        self.assertEqual(len((await response.json())['files']),300)
        selects=[sql for sql in statements if sql.startswith('SELECT')]
        self.assertLess(len(selects),20)  # Must not grow by one query per file.
        result=await self.cache.public_files(dict(query=files[0]['sha1']))
        self.assertEqual(result['total'],1)
        self.assertEqual(result['files'][0]['url'],files[0]['url'])

    def test_room_ttl_does_not_modify_other_resource_policy_and_is_bounded(self):
        settings = validate_settings({'room_cache_ttl_hours': 32})
        self.assertEqual(settings['room_cache_ttl_hours'], 32)
        self.assertEqual(settings['policies'], self.settings['policies'])
        for value in (0, 33, True):
            with self.assertRaises(ValueError):
                validate_settings({'room_cache_ttl_hours': value})
