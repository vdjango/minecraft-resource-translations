import json
import asyncio
import time
from unittest.mock import patch
from pathlib import Path

from mc_translate.config import Config
from mc_translate.management import Settings, key_hash
from tests.test_platform import PlatformFixture


class ManagementCase(PlatformFixture):
    async def test_inventory_view_requires_login_and_forwards_only_view_filters(self):
        from unittest.mock import AsyncMock
        self.config.admin_token='admin-test'
        payload=dict(action='inventory_files',category='java-raw',handling='platform',offset=50,
                     query='libjava',summary_only=False,unrelated='ignored')
        response=await self.client.post('/v1/admin/mirror',json=payload)
        self.assertEqual(response.status,401)
        with patch('mc_translate.mirror_control.MirrorControl.call',AsyncMock(return_value=dict(total=24006,files=[]))) as call:
            response=await self.client.post('/v1/admin/mirror',json=payload,headers={'Authorization':'Bearer admin-test'})
            self.assertEqual(response.status,200)
            self.assertEqual((await response.json())['total'],24006)
            self.assertNotIn('unrelated',call.call_args.args[0])
            self.assertEqual(call.call_args.args[0]['offset'],50)

    async def test_cloud_save_keeps_legacy_and_multiple_platform_settings(self):
        from unittest.mock import AsyncMock
        from mc_translate.mirror import settings_path, read_settings
        from mc_translate.mirror_catalog import write_json
        self.config.admin_token='admin-test'
        self.config.mirror_root=str(Path(self.temp.name)/'mirror')
        headers={'Authorization':'Bearer admin-test'}
        with patch('mc_translate.mirror_control.MirrorControl.call', AsyncMock(return_value=None)):
            for platforms in ('all', ['windows','macos'], []):
                write_json(settings_path(self.config), {'prefetch_platform':platforms})
                response=await self.client.post('/v1/admin/mirror',json={'qiniu_paused':False},headers=headers)
                self.assertEqual(response.status, 200, await response.text())
                saved=read_settings(self.config, effective=False)
                self.assertEqual(saved['prefetch_platform'], ['windows','macos','linux'] if platforms=='all' else platforms)
                self.assertFalse(saved['qiniu_paused'])

    async def test_admin_cookie_accepts_configured_mirror_origin_and_rejects_other_sites(self):
        from mc_translate.mirror import settings_path
        from mc_translate.mirror_catalog import write_json
        self.config.admin_token='admin-test'
        self.config.public_url='https://translate.example.com'
        self.config.mirror_root=str(Path(self.temp.name)/'mirror')
        write_json(settings_path(self.config),{'public_url':'https://mirror.example.com'})
        response=await self.client.post('/v1/admin/session',headers={'Authorization':'Bearer admin-test'})
        self.assertEqual(response.status,200)
        headers={'Cookie':'translation_admin='+response.cookies['translation_admin'].value,
                 'Origin':'https://mirror.example.com'}
        # An intentionally invalid action reaches validation, without mutating settings.
        response=await self.client.post('/v1/admin/mirror',json={'action':'invalid_probe'},headers=headers)
        self.assertEqual(response.status,400)
        headers.update({'Host':'mirror-proxy.example.com','Origin':'https://mirror-proxy.example.com',
                        'X-Forwarded-Proto':'https'})
        response=await self.client.post('/v1/admin/mirror',json={'action':'invalid_probe'},headers=headers)
        self.assertEqual(response.status,400)
        headers['Origin']='https://unrelated.example.com'
        response=await self.client.post('/v1/admin/mirror',json={'action':'invalid_probe'},headers=headers)
        self.assertEqual(response.status,403)
        self.assertEqual((await response.json())['code'],'invalid_origin')

    async def test_model_directory_cold_reads_share_one_provider_request(self):
        self.config.admin_token = "admin-test"
        headers = {"Authorization": "Bearer admin-test"}
        api = self.app["api"]
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def slow_provider(identity, candidate):
            calls.append(identity)
            entered.set()
            await release.wait()
            return ["model-one"]
        with patch.object(api, "refresh_model_list", side_effect=slow_provider):
            first = asyncio.create_task(self.client.get("/v1/admin/models", headers=headers))
            await asyncio.wait_for(entered.wait(), 1)
            second = asyncio.create_task(self.client.get("/v1/admin/models", headers=headers))
            await asyncio.sleep(0.03)
            self.assertEqual(len(calls), 1)
            release.set()
            for response in await asyncio.gather(first, second):
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["models"], ["model-one"])

    async def test_expired_model_directory_returns_without_waiting_for_provider(self):
        self.config.admin_token = "admin-test"
        api = self.app["api"]
        identity = key_hash(self.config.base_url + "\n" + self.config.api_key)
        api.model_lists[identity] = (time.monotonic() - 1, ["cached-model"])
        release = asyncio.Event()
        async def slow_provider(*args):
            await release.wait()
            return ["new-model"]
        with patch.object(api, "refresh_model_list", side_effect=slow_provider):
            response = await asyncio.wait_for(self.client.get("/v1/admin/models", headers={"Authorization": "Bearer admin-test"}), 1)
            self.assertEqual(await response.json(), dict(models=["cached-model"], stale=True))
            release.set()
            await asyncio.gather(*list(api.model_refreshes.values()))

    async def test_owner_chooses_credentials_once_in_browser(self):
        account=self.app['api'].admin_accounts
        self.assertFalse(account.initialized)
        first_page=await (await self.client.get('/admin')).text()
        self.assertIn('"authMode":"setup"',first_page)
        self.assertIn('设置管理员账户',first_page)
        token=account.link_path.read_text().strip().split('#',1)[1]
        body=dict(username='owner',password='chosen-password-2026',password_confirm='chosen-password-2026')
        response=await self.client.post('/v1/admin/setup',json=body,headers={'X-Admin-Setup':'wrong'})
        self.assertEqual(response.status,403)
        response=await self.client.post('/v1/admin/setup',json=body,headers={'X-Admin-Setup':token})
        self.assertEqual(response.status,200)
        self.assertTrue(response.cookies['translation_admin']['httponly'])
        self.assertEqual(response.cookies['translation_admin']['path'],'/')
        self.assertEqual((await self.client.get('/v1/admin/settings')).status,200)
        html=await (await self.client.get('/admin/tasks')).text()
        self.assertIn('"authMode":"session"',html)
        self.assertNotIn('id="panel" class="hidden"',html)
        self.assertFalse(account.link_path.exists())
        self.assertNotIn(body['password'],account.path.read_text())
        self.assertEqual((await self.client.post('/v1/admin/setup',json=body,headers={'X-Admin-Setup':token})).status,403)
        await self.client.delete('/v1/admin/session')
        self.assertEqual((await self.client.get('/v1/admin/settings')).status,401)
        self.assertEqual((await self.client.post('/v1/admin/session',json=dict(username='owner',password='wrong'))).status,401)
        self.assertEqual((await self.client.post('/v1/admin/session',json=dict(username='owner',password=body['password']))).status,200)

    async def test_settings_auth_redaction_atomic_persistence(self):
        self.assertEqual((await self.client.get("/v1/admin/settings")).status, 401)
        self.config.admin_token = "admin-test"
        headers = {"Authorization": "Bearer admin-test"}
        changes = dict(
            base_url="https://example.org/v1",
            model="custom-model",
            api_key="private-test-key",
            daily_call_limit=500,
        )
        response = await self.client.post(
            "/v1/admin/settings", headers=headers, json=changes
        )
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["api_key_configured"])
        self.assertNotIn("private-test-key", json.dumps(data))
        self.assertEqual(self.config.model, "custom-model")
        saved = Settings(Config(database=self.config.database))
        self.assertEqual(saved.config.api_key, "private-test-key")
        self.assertEqual(saved.config.daily_call_limit, 500)
        for change in (
            {"base_url": "http://example.org/v1"},
            {"daily_call_limit": -1},
            {"api_key": "bad\nheader"},
            {"admin_token": "replacement"},
        ):
            response = await self.client.post(
                "/v1/admin/settings", headers=headers, json=change
            )
            self.assertEqual(response.status, 400)
        self.assertEqual(
            Settings(Config(database=self.config.database)).config.model, "custom-model"
        )
        self.assertTrue(
            (Path(self.config.database).parent / "runtime-config.json").is_file()
        )

    async def test_public_application_removed_and_admin_issues_key(self):
        self.config.admin_token = "admin-test"
        admin = {"Authorization": "Bearer admin-test"}
        self.assertEqual((await self.client.post("/v1/clients/applications", json={})).status, 405)
        self.assertEqual((await self.client.post("/v1/admin/clients", json={"action": "create", "name": "Launcher"})).status, 401)
        self.assertEqual((await self.client.post("/v1/admin/clients", headers=admin,
            json={"action": "create", "name": ""})).status, 400)
        created = await self.client.post("/v1/admin/clients", headers=admin,
            json={"action": "create", "name": "Launcher", "fields_per_hour": 1})
        self.assertEqual(created.status, 201)
        key = (await created.json())["access_token"]
        self.assertTrue(key.startswith("bct_"))
        listing = await (await self.client.get("/v1/admin/clients", headers=admin)).text()
        self.assertIn("Launcher", listing)
        self.assertNotIn(key, listing)
        self.assertEqual((await self.client.get("/v1/clients/application",
            headers={"Authorization": "Bearer " + key})).status, 200)

    async def test_admin_client_quota_owner_and_immediate_revocation(self):
        self.config.admin_token = "admin-test"
        admin = {"Authorization": "Bearer admin-test"}
        response = await self.client.post("/v1/admin/clients", headers=admin,
            json=dict(action="create", name="Test launcher", contact="test@example.org",
                      website="https://example.org", purpose="Integration test", fields_per_hour=1))
        self.assertEqual(response.status, 201)
        issued = await response.json()
        headers = {"Authorization": "Bearer " + issued["access_token"]}
        result = await self.ensure(headers=headers)
        job = result["results"][0]["tickets"][0]["job_id"]
        self.assertEqual((await self.done(job, headers))["state"], "ready")
        self.assertEqual((await self.client.get("/v1/jobs/" + job, headers=self.headers)).status, 404)
        response = await self.client.post("/v1/translations:ensure",
            headers=dict(headers, **{"Idempotency-Key": "quota-test-0001"}),
            json=dict(self.payload("body"), purpose="detail"))
        self.assertEqual(response.status, 429)
        await self.client.post("/v1/admin/clients", headers=admin,
            json=dict(id=issued["id"], state="revoked"))
        self.assertEqual((await self.client.get("/v1/jobs/" + job, headers=headers)).status, 403)
        invalid = {"Authorization": "Bearer bct_invalid"}
        self.assertEqual((await self.client.get("/v1/capabilities", headers=invalid)).status, 401)

    async def test_catalogue_summary_only_is_partial_and_body_update_invalidates(self):
        result = await self.ensure()
        await self.done(result["results"][0]["tickets"][0]["job_id"])

        async def catalog():
            data = await (await self.client.get("/v1/catalog/projects")).json()
            self.schema("CatalogueResponse", data)
            return data["projects"][0]

        item = await catalog()
        self.assertEqual(item["translation_state"], "partial")
        self.assertEqual(item["field_states"], dict(summary="ready", body="missing"))
        result = await self.ensure(self.payload("body"), key="body-test-00001")
        await self.done(result["results"][0]["tickets"][0]["job_id"])
        self.assertEqual((await catalog())["translation_state"], "ready")
        source = await self.app["engine"].sources.download(self.source["resource"])
        from mc_translate.hashing import digest, text_hash

        source["fields"]["body"]["text"] += "\nNew text"
        source["fields"]["body"]["source_hash"] = text_hash(
            source["fields"]["body"]["text"], "body", "markdown"
        )
        source["snapshot_hash"] = digest("new-source")
        await self.store.save_source(source)
        item = await catalog()
        self.assertEqual(item["translation_state"], "partial")
        self.assertTrue(item["update"]["source_changed"])
