import asyncio
import hashlib
import json
import socket
import tempfile
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.api import create_app
from mc_translate.config import Config
from mc_translate.federation import origin
from test_platform import PlatformFixture


class FederationCase(PlatformFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.config.public_url = str(self.client.make_url('/')).rstrip('/')
        self.config.admin_token = 'federation-admin'
        self.config.federation_allow_private = True
        self.app['federation'].hub = self.config.public_url
        self.mirrors = []
        self.node_directories = []

    async def asyncTearDown(self):
        for mirror in self.mirrors:
            await mirror.close()
        for directory in self.node_directories:
            directory.cleanup()
        await super().asyncTearDown()

    async def mirror(self, approve=True):
        directory = tempfile.TemporaryDirectory()
        self.node_directories.append(directory)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        app = await create_app(Config(database=str(Path(directory.name)/'mirror.sqlite'), public_url=f'http://127.0.0.1:{port}', federation_role='mirror', federation_hub=self.config.public_url, federation_name='Test mirror', federation_region='test-local', federation_contact='test-maintainer', federation_allow_private=True), workers=True)
        client = TestClient(TestServer(app, port=port))
        self.mirrors.append(client)
        await client.start_server()
        for _ in range(100):
            if app['federation'].credentials:
                break
            await asyncio.sleep(.02)
        self.assertIsNotNone(app['federation'].credentials)
        node_id = app['federation'].credentials['id']
        if approve:
            response = await self.client.post('/v1/admin/nodes', json={'id':node_id,'state':'active'}, headers={'Authorization':'Bearer federation-admin'})
            self.assertEqual(response.status,200,await response.text())
            await app['federation'].join_or_heartbeat()
        return client

    async def test_join_requires_approval_health_proof_and_online_lease(self):
        mirror = await self.mirror(approve=False)
        self.assertEqual((await mirror.post('/v1/translations:resolve',json=self.payload())).status,503)
        discovery = await (await self.client.get('/v1/federation/nodes')).json()
        self.assertEqual([node['id'] for node in discovery['nodes']],['hub'])
        node_id=mirror.app['federation'].credentials['id']
        response=await self.client.post('/v1/admin/nodes',json={'id':node_id,'state':'active'},headers={'Authorization':'Bearer federation-admin'})
        self.assertEqual(response.status,200)
        await mirror.app['federation'].join_or_heartbeat()
        discovery=await (await self.client.get('/v1/federation/nodes?region=test-local')).json()
        self.assertEqual(discovery['selection'],'client_rtt')
        self.assertEqual(discovery['nodes'][0]['id'],node_id)
        async with self.store.transaction(invalidate=False) as db:
            await db.execute('UPDATE federation_nodes SET last_seen=0')
        self.assertEqual(len((await (await self.client.get('/v1/federation/nodes')).json())['nodes']),1)
        self.assertNotIn('token',json.dumps(await (await self.client.get('/v1/admin/nodes',headers={'Authorization':'Bearer federation-admin'})).json()))

    async def test_shared_reads_artifacts_and_no_model_or_caller_token_on_mirror(self):
        ensured=await self.ensure()
        await self.done(ensured['results'][0]['tickets'][0]['job_id'])
        mirror=await self.mirror()
        response=await mirror.post('/v1/translations:resolve',json=self.payload(),headers={'Authorization':'Bearer bct_caller-secret-never-forwarded'})
        self.assertEqual(response.status,200,await response.text())
        result=await response.json()
        self.assertEqual(result['results'][0]['fields'][0]['state'],'ready')
        artifact=result['results'][0]['fields'][0]['translation']['artifact']
        self.assertTrue(artifact['url'].startswith(str(mirror.make_url('/'))))
        artifact_path='/v1/artifacts/'+artifact['artifact_sha256']
        response=await mirror.get(artifact_path)
        body=await response.read()
        self.assertEqual(response.status,200,body)
        self.assertEqual(hashlib.sha256(body).hexdigest(),artifact['artifact_sha256'])
        self.assertEqual((await mirror.app['store'].one('SELECT COUNT(*) AS n FROM translations'))['n'],0)
        self.assertEqual((await mirror.app['store'].one('SELECT COUNT(*) AS n FROM usage'))['n'],0)
        self.assertEqual((await mirror.post('/v1/translations:ensure',json=self.payload(),headers=self.headers)).status,409)
        self.assertTrue((Path(mirror.app['store'].path).parent/'federation-node.json').is_file())
        file=mirror.app['federation'].artifacts/artifact['artifact_sha256']
        file.write_bytes(b'corrupted local artifact')
        response=await mirror.get(artifact_path)
        self.assertEqual(response.status,200)
        self.assertEqual(await response.read(),body)

    async def test_cold_requests_coalesce_and_correlation_ids_are_preserved(self):
        mirror=await self.mirror()
        before=sum(counters['requests'] for key,counters in self.app['traffic'].pending.items() if key[1]=='resolve')
        async def request(index):
            payload=self.payload()
            payload['items'][0]['request_id']=f'caller-{index}'
            response=await mirror.post('/v1/translations:resolve',json=payload)
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['results'][0]['request_id'],f'caller-{index}')
        await asyncio.gather(*(request(index) for index in range(12)))
        after=sum(counters['requests'] for key,counters in self.app['traffic'].pending.items() if key[1]=='resolve')
        self.assertEqual(after-before,1)
        response=await mirror.post('/v1/translations:resolve',json=self.payload())
        self.assertEqual(response.headers['X-Translation-Cache'],'hit')

    async def test_revocation_and_bad_artifact_are_not_silently_served(self):
        ensured=await self.ensure()
        await self.done(ensured['results'][0]['tickets'][0]['job_id'])
        mirror=await self.mirror()
        artifact=(await self.resolve())['translation']['artifact']['artifact_sha256']
        async with self.store.transaction() as db:
            await db.execute('UPDATE artifacts SET data=? WHERE hash=?',(b'{"wrong":true}',artifact))
        self.assertEqual((await mirror.get('/v1/artifacts/'+artifact)).status,502)
        node_id=mirror.app['federation'].credentials['id']
        response=await self.client.post('/v1/admin/nodes',json={'id':node_id,'state':'revoked'},headers={'Authorization':'Bearer federation-admin'})
        self.assertEqual(response.status,200)
        self.assertEqual(len((await (await self.client.get('/v1/federation/nodes')).json())['nodes']),1)
        with self.assertRaises(ValueError):
            await mirror.app['federation'].join_or_heartbeat()
        self.assertEqual((await mirror.post('/v1/translations:resolve',json=self.payload())).status,503)

    async def test_public_origins_block_private_addresses_and_redirect_tricks(self):
        for url in ('http://public.example','https://127.0.0.1','https://192.168.1.1','https://[::1]','https://localhost','https://node.local','https://example.org:8443','https://user@example.org','https://example.org/path','https://example.org?target=x','https://example.org foo'):
            with self.assertRaises(ValueError,msg=url):
                origin(url)
        self.assertEqual(origin('https://node.example/'),'https://node.example')
        self.assertEqual(origin('http://127.0.0.1:18448',True),'http://127.0.0.1:18448')
