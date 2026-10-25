import json
from pathlib import Path

from mc_translate.config import Config
from mc_translate.management import Settings
from tests.test_platform import PlatformFixture


class ManagementCase(PlatformFixture):
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

    async def test_application_attempts_count_failures_and_accept_http_homepages(self):
        failed = await self.client.post("/v1/clients/applications", json={})
        self.assertEqual(failed.status, 400)
        accepted = await self.client.post(
            "/v1/clients/applications",
            json=dict(name="HTTP launcher", contact="maintainer", website="http://example.org", purpose="Test"),
        )
        self.assertEqual(accepted.status, 201)
        self.assertEqual((await accepted.json())["state"], "pending")
        self.assertEqual((await self.client.post("/v1/clients/applications", json={})).status, 400)
        limited = await self.client.post("/v1/clients/applications", json={})
        self.assertEqual(limited.status, 429)
        self.assertEqual((await limited.json())["code"], "application_daily_limit")

    async def test_developer_approval_quota_owner_and_immediate_revocation(self):
        self.config.admin_token = "admin-test"
        admin = {"Authorization": "Bearer admin-test"}
        response = await self.client.post(
            "/v1/clients/applications",
            json=dict(
                name="Test launcher",
                contact="test@example.org",
                website="https://example.org",
                purpose="Integration test",
            ),
        )
        self.assertEqual(response.status, 201)
        application = await response.json()
        headers = {"Authorization": "Bearer " + application["access_token"]}
        self.assertEqual(
            (await self.client.get("/v1/clients/application", headers=headers)).status,
            200,
        )
        self.assertEqual(
            (
                await self.client.post(
                    "/v1/translations:ensure",
                    headers=headers,
                    json=dict(self.payload(), purpose="detail"),
                )
            ).status,
            403,
        )
        response = await self.client.post(
            "/v1/admin/clients",
            headers=admin,
            json=dict(
                id=application["application_id"], state="active", fields_per_hour=1
            ),
        )
        self.assertEqual(response.status, 200)
        self.assertNotIn(application["access_token"], await response.text())
        result = await self.ensure(headers=headers)
        job = result["results"][0]["tickets"][0]["job_id"]
        self.assertEqual((await self.done(job, headers))["state"], "ready")
        self.assertEqual(
            (await self.client.get("/v1/jobs/" + job, headers=self.headers)).status, 404
        )
        response = await self.client.post(
            "/v1/translations:ensure",
            headers=dict(headers, **{"Idempotency-Key": "quota-test-0001"}),
            json=dict(self.payload("body"), purpose="detail"),
        )
        self.assertEqual(response.status, 429)
        await self.client.post(
            "/v1/admin/clients",
            headers=admin,
            json=dict(id=application["application_id"], state="revoked"),
        )
        self.assertEqual(
            (await self.client.get("/v1/jobs/" + job, headers=headers)).status, 403
        )
        invalid = {"Authorization": "Bearer bct_invalid"}
        self.assertEqual(
            (await self.client.get("/v1/capabilities", headers=invalid)).status, 401
        )

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
