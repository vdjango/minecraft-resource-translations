import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import patch

from aiohttp import web

from mc_translate.api import create_app
from mc_translate.traffic import Traffic, latency
from test_platform import FakeModel, FakeSources, PlatformFixture


class TrafficCase(PlatformFixture):
    async def snapshot(self, days=7):
        self.config.admin_token = "traffic-admin"
        response = await self.client.get("/v1/admin/traffic", params={"days": days}, headers={"Authorization": "Bearer traffic-admin"})
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_requests_hits_failures_and_session_independent_counts(self):
        payload = self.payload()
        launcher = {"User-Agent": "Example-Launcher/1.0", "X-Translation-Instance": "anonymous-install-12345"}
        for _ in range(2):
            response = await self.client.post("/v1/translations:resolve", json=payload, headers=launcher)
            self.assertEqual(response.status, 200)
            self.assertIn("app;dur=", response.headers["Server-Timing"])
        payload["items"][0]["request_id"] = "another-launch"
        response = await self.client.post("/v1/translations:resolve", json=payload, headers=launcher)
        self.assertEqual(response.status, 200)
        self.assertTrue(self.app["api"].lookup_cache)
        await self.client.post("/v1/translations:resolve", json={})
        await self.client.get("/health")
        result = await self.snapshot()
        self.assertEqual(result["today"]["requests"], 4)
        self.assertEqual(result["today"]["fields"], 3)
        self.assertEqual(result["today"]["missing_fields"], 3)
        self.assertEqual(result["today"]["cache_hits"], 2)
        self.assertEqual(result["today"]["errors"], 1)
        self.assertEqual(result["today"]["reported_instances"], 1)
        self.assertEqual(result["today"]["estimated_instances"], 0)
        self.assertEqual(result["model"]["calls"], 0)
        self.assertEqual((await self.snapshot())["today"]["requests"], 4)
        self.assertEqual((await self.client.get("/v1/admin/traffic")).status, 401)
        self.assertEqual((await self.client.get("/v1/admin/traffic?days=365", headers={"Authorization": "Bearer traffic-admin"})).status, 400)

    async def test_instance_estimates_and_privacy(self):
        for headers in (
            {"User-Agent": "Private-Launcher/1.0"},
            {"User-Agent": "Private-Launcher/1.0"},
            {"User-Agent": "Private-Launcher/1.0", "X-Translation-Instance": "private-instance-123456"},
            {"User-Agent": "Private-Launcher/1.0", "X-Translation-Instance": "private-instance-654321"},
            {"User-Agent": "Browser", "Sec-Fetch-Site": "same-origin"},
        ):
            response = await self.client.post("/v1/translations:resolve", json=self.payload(), headers=headers)
            self.assertEqual(response.status, 200)
        result = await self.snapshot()
        self.assertEqual(result["today"]["estimated_instances"], 1)
        self.assertEqual(result["today"]["reported_instances"], 2)
        self.assertEqual(result["today"]["launcher_instances"], 3)
        self.assertEqual(result["today"]["web_visitors_estimated"], 1)
        rows = json.dumps(await self.store.rows("SELECT * FROM traffic_visitors"))
        for private in ("Private-Launcher", "private-instance-123456", "127.0.0.1"):
            self.assertNotIn(private, rows)

    async def test_client_keys_represent_apps_not_installations(self):
        from mc_translate.management import key_hash
        stamp = time.time()
        async with self.store.transaction() as db:
            await db.execute("INSERT INTO client_keys(id,hash,prefix,name,contact,website,purpose,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)", ("app-1", key_hash("bct_traffic-test-key"), "bct_test", "Test launcher", "maintainer", "https://example.org", "tests", "active", stamp, stamp))
        for instance in ("launcher-installation-1", "launcher-installation-2"):
            await self.client.post("/v1/translations:resolve", json=self.payload(), headers={"Authorization": "Bearer bct_traffic-test-key", "X-Translation-Instance": instance})
        result = await self.snapshot()
        self.assertEqual(result["today"]["active_integrations"], 1)
        self.assertEqual(result["today"]["reported_instances"], 2)
        self.assertEqual(result["top_clients"][0]["name"], "Test launcher")
        self.assertEqual(result["top_clients"][0]["requests"], 2)
        self.assertNotIn("bct_traffic-test-key", json.dumps(result))

    async def test_flush_not_invalidate_cache_and_restart_preserves_stats(self):
        await self.client.post("/v1/translations:resolve", json=self.payload(), headers={"User-Agent": "Test-Launcher"})
        version = self.store.version
        first = await self.snapshot()
        self.assertEqual(self.store.version, version)
        await self.client.close()
        self.app = await create_app(self.config, model_factory=FakeModel, sources_factory=FakeSources)
        from aiohttp.test_utils import TestClient, TestServer
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.store = self.app["store"]
        await self.client.post("/v1/translations:resolve", json=self.payload(), headers={"User-Agent": "Test-Launcher"})
        second = await self.snapshot()
        self.assertEqual(second["today"]["requests"], 2)
        self.assertEqual(second["today"]["estimated_instances"], 1)
        self.assertEqual(second["collection_started_at"], first["collection_started_at"])

    async def test_aggregation_failure_retries_without_losing_counts(self):
        await asyncio.gather(*(self.client.post("/v1/translations:resolve", json=self.payload()) for _ in range(8)))
        traffic = self.app["traffic"]
        @asynccontextmanager
        async def unavailable(**kwargs):
            raise OSError("test only")
            yield
        with patch.object(self.store, "transaction", unavailable):
            with self.assertLogs("mc_translate.traffic", level="WARNING"):
                await traffic.flush()
        self.assertTrue(traffic.last_error)
        self.assertEqual(sum(row["requests"] for row in traffic.pending.values()), 8)
        await traffic.flush()
        self.assertFalse(traffic.last_error)
        self.assertEqual((await self.snapshot())["today"]["requests"], 8)

    async def test_validation_and_field_cache_do_not_accept_invalid_or_wrong_language(self):
        payload = self.payload()
        await self.client.post("/v1/translations:resolve", json=payload)
        for request_id in ("", "x" * 65, None, 1):
            changed = copy.deepcopy(payload)
            changed["items"][0]["request_id"] = request_id
            self.assertEqual((await self.client.post("/v1/translations:resolve", json=changed)).status, 400)
        changed = copy.deepcopy(payload)
        changed["items"][0]["request_id"] = "valid-new-id"
        changed["items"][0]["fields"][0]["extra"] = True
        self.assertEqual((await self.client.post("/v1/translations:resolve", json=changed)).status, 400)
        changed = copy.deepcopy(payload)
        changed["items"] += copy.deepcopy(changed["items"])
        self.assertEqual((await self.client.post("/v1/translations:resolve", json=changed)).status, 400)
        ensured = await self.ensure()
        await self.done(ensured["results"][0]["tickets"][0]["job_id"])
        ready = await (await self.client.post("/v1/translations:resolve", json=payload)).json()
        self.assertEqual(ready["results"][0]["fields"][0]["state"], "ready")
        changed = dict(payload, target_language="ja")
        japanese = await (await self.client.post("/v1/translations:resolve", json=changed)).json()
        self.assertEqual(japanese["results"][0]["fields"][0]["state"], "missing")

    async def test_histogram_and_bounded_buffers(self):
        self.assertEqual(latency({"requests": 100, "latency_4": 95, "latency_5": 5})["p95_le_ms"], 20)
        self.assertIsNone(latency({"requests": 1, "latency_11": 1})["p95_le_ms"])
        self.assertIsNone(latency({})["within_20ms_ratio"])
        self.app["traffic"].max_visitors = 1
        for instance in ("first-installation-1234", "second-installation-1234"):
            await self.client.post("/v1/translations:resolve", json=self.payload(), headers={"X-Translation-Instance": instance})
        self.assertEqual(len(self.app["traffic"].visitors), 1)
        self.assertEqual(self.app["traffic"].dropped_visitors, 1)
