import json
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from mc_translate.api import create_app
from mc_translate.config import Config


class InstallationCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.config = Config(database=str(self.root/'data/translations.sqlite3'),
                             mirror_root=str(self.root/'resources'), mirror_port=1)
        self.client = TestClient(TestServer(await create_app(self.config, workers=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def account(self):
        response = await self.client.post('/v1/setup/account', json=dict(username='owner',
            password='my-install-password-2026', password_confirm='my-install-password-2026'))
        self.assertEqual(response.status, 200)

    async def configure(self, section, settings):
        response = await self.client.post('/v1/setup/configure', json=dict(section=section, settings=settings))
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def test_every_browser_entry_redirects_without_private_link(self):
        for path in ('/', '/docs', '/developers', '/admin', '/admin/settings', '/admin/setup',
                     '/projects/modrinth/AANobbMI', '/missing-page', '/assets/admin.html'):
            response = await self.client.get(path, allow_redirects=False)
            self.assertEqual(response.status, 302, path)
            self.assertEqual(response.headers['Location'], '/setup')
            self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual((await self.client.get('/setup')).status, 200)
        self.assertEqual((await self.client.get('/assets/setup.css')).status, 200)
        response = await self.client.get('/v1/catalog/projects')
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['code'], 'setup_required')
        self.assertEqual((await self.client.get('/health')).status, 200)
        status = await (await self.client.get('/v1/setup/status')).json()
        self.assertFalse(status['initialized'])
        self.assertNotIn('resources', status)
        response = await self.client.post('/v1/setup/account', json=dict(username='attacker'),
                                          headers={'Origin':'https://unrelated.example'})
        self.assertEqual(response.status, 400)

    async def test_account_then_all_settings_before_unlock_and_restart(self):
        self.assertEqual((await self.client.post('/v1/setup/configure',json=dict(section='site',settings={'public_url':'https://mine.example'}))).status,401)
        await self.account()
        self.assertEqual((await self.client.get('/docs', allow_redirects=False)).status,302)
        self.assertEqual((await self.client.post('/v1/setup/finish',json={})).status,400)
        await self.configure('site', {'public_url':'https://mine.example'})
        await self.configure('translation', {'api_key':''})
        await self.configure('resources', {'quota_gib':50,'policies':{'mods':'disabled','java':'redirect'}})
        await self.configure('mail', {'mail_enabled':False})
        # Progress and ownership survive a process restart before final completion.
        await self.client.close()
        self.client=TestClient(TestServer(await create_app(Config(database=self.config.database,
            mirror_root=self.config.mirror_root,mirror_port=1),workers=False)))
        await self.client.start_server()
        status=await (await self.client.get('/v1/setup/status')).json()
        self.assertTrue(status['required']);self.assertTrue(status['initialized'])
        self.assertEqual(status['sections'],['site','translation','resources','mail'])
        self.assertEqual((await self.client.post('/v1/setup/account',json={})).status,409)
        response=await self.client.post('/v1/admin/session',json=dict(username='owner',password='my-install-password-2026'))
        self.assertEqual(response.status,200)
        # The configured HTTPS cookie is secure; use the returned session explicitly
        # in this loopback HTTP test harness, as a real deployment uses HTTPS.
        cookie=response.cookies['translation_admin'].value
        headers={'Cookie':'translation_admin='+cookie}
        response=await self.client.post('/v1/setup/finish',json={},headers=headers)
        self.assertEqual(response.status,200)
        self.assertEqual((await self.client.get('/docs')).status,200)
        self.assertEqual((await self.client.get('/v1/catalog/projects')).status,200)
        self.assertTrue(json.loads((self.root/'data/installation.json').read_text())['complete'])
        saved=json.loads((self.root/'resources/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['quota_gib'],50)
        self.assertEqual(saved['policies']['java'],'redirect')
        self.assertEqual(self.client.app['api'].config.public_url,'https://mine.example')
        self.assertEqual((await self.client.post('/v1/setup/account',json={})).status,409)

    async def test_existing_installs_are_not_reopened(self):
        await self.account()
        await self.client.close()
        (self.root/'data/installation.json').unlink()
        self.client=TestClient(TestServer(await create_app(self.config,workers=False)))
        await self.client.start_server()
        self.assertFalse((await (await self.client.get('/v1/setup/status')).json())['required'])
        self.assertEqual((await self.client.get('/docs')).status,200)
        self.assertEqual((await self.client.get('/setup',allow_redirects=False)).headers['Location'],'/admin')
