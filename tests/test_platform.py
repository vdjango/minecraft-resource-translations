import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from jsonschema import Draft202012Validator

from mc_translate.api import CONTRACT, create_app
from mc_translate.config import Config
from mc_translate.content import assemble, prepare, validate_translation
from mc_translate.hashing import digest, resource_ref, text_hash
from mc_translate.sources import Sources

REF = resource_ref("modrinth", "Fixture01")


class FakeSources(Sources):
    body = "A fast mod.\n\nInstall with `fabric` on Minecraft 1.21.\n\n[Docs](https://example.com)"
    summary = "A fast Minecraft mod."
    downloads = 0

    async def download(self, resource):
        type(self).downloads += 1
        fields = {}
        for name, text, format in [
            ("title", "Fixture", "plain"),
            ("summary", self.summary, "plain"),
            ("body", self.body, "markdown"),
        ]:
            fields[name] = dict(
                text=text,
                format=format,
                source_language="und",
                source_hash=text_hash(text, name, format),
            )
        return dict(
            resource=resource,
            kind="mod",
            source_url="https://modrinth.com/project/Fixture01",
            metadata=dict(
                created_at="2024-01-01T00:00:00Z", author_ids=["team1"], slug="fixture"
            ),
            fields=fields,
            snapshot_hash=digest(
                dict(
                    schema="mc-snapshot-v1",
                    fields={n: f["source_hash"] for n, f in fields.items()},
                )
            ),
            metadata_hash=digest("meta"),
        )


class FakeModel:
    calls = 0
    gate = None

    def __init__(self, *args):
        pass

    async def translate(self, segments, language):
        type(self).calls += 1
        if self.gate:
            await self.gate.wait()
        return {
            s.id: s.text.replace("A fast Minecraft mod.", "一个快速的 Minecraft 模组。")
            .replace("A changed summary.", "修改后的简介。")
            .replace("A fast mod.", "一个快速的模组。")
            .replace("Install with", "安装使用")
            .replace(" on Minecraft ", "，Minecraft ")
            for s in segments
        }


class PlatformFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(
            database=str(Path(self.temp.name) / "test.sqlite"),
            api_key="fake",
            workers=2,
            public_url="http://localhost",
        )
        FakeSources.downloads = 0
        FakeSources.body = "A fast mod.\n\nInstall with `fabric` on Minecraft 1.21.\n\n[Docs](https://example.com)"
        FakeSources.summary = "A fast Minecraft mod."
        FakeModel.calls = 0
        FakeModel.gate = None
        self.app = await create_app(
            self.config, model_factory=FakeModel, sources_factory=FakeSources
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.store = self.app["store"]
        self.source = await self.app["engine"].sources.fetch(REF)
        self.headers = await self.session()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def session(self):
        response = await self.client.post(
            "/v1/sessions/anonymous", json={"client_id": "tests"}
        )
        self.assertEqual(response.status, 200)
        return {"Authorization": "Bearer " + (await response.json())["access_token"]}

    def payload(self, field="summary", hash=None):
        return dict(
            target_language="zh-Hans",
            profile="minecraft-v1",
            items=[
                dict(
                    request_id="r1",
                    resource=dict(REF),
                    fields=[
                        dict(
                            field=field,
                            source_hash=hash
                            or self.source["fields"][field]["source_hash"],
                        )
                    ],
                )
            ],
        )

    async def ensure(self, payload=None, headers=None, key="test-key-00001"):
        response = await self.client.post(
            "/v1/translations:ensure",
            json=dict(payload or self.payload(), purpose="detail"),
            headers=dict(headers or self.headers, **{"Idempotency-Key": key}),
        )
        self.assertEqual(response.status, 202, await response.text())
        return await response.json()

    async def done(self, job, headers=None):
        for _ in range(200):
            response = await self.client.get(
                "/v1/jobs/" + job, headers=headers or self.headers
            )
            data = await response.json()
            if data.get("state") in ("ready", "failed", "source_mismatch"):
                return data
            await asyncio.sleep(0.02)
        self.fail("worker timed out")

    async def resolve(self, payload=None):
        response = await self.client.post(
            "/v1/translations:resolve", json=payload or self.payload()
        )
        self.assertEqual(response.status, 200, await response.text())
        data = await response.json()
        self.schema("ResolveResponse", data)
        return data["results"][0]["fields"][0]

    def schema(self, name, value):
        Draft202012Validator(
            {
                "$ref": "#/components/schemas/" + name,
                "components": CONTRACT["components"],
            }
        ).validate(value)


class PlatformCase(PlatformFixture):
    async def test_read_only_lookup_then_generation_and_artifact(self):
        before = FakeSources.downloads
        self.assertEqual((await self.resolve())["state"], "missing")
        self.assertEqual(FakeSources.downloads, before)
        self.assertEqual(FakeModel.calls, 0)
        result = await self.ensure(self.payload("body"))
        self.schema("EnsureResponse", result)
        task = await self.done(result["results"][0]["tickets"][0]["job_id"])
        self.schema("Ticket", task)
        self.assertEqual(task["state"], "ready")
        field = await self.resolve(self.payload("body"))
        self.assertTrue(field["matches_request"])
        identity = field["translation"]["artifact"]["artifact_sha256"]
        response = await self.client.get("/v1/artifacts/" + identity)
        raw = await response.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), identity)
        artifact = json.loads(raw)
        self.schema("Artifact", artifact)
        self.assertIn("一个快速的模组", artifact["text"])
        self.assertIn("`fabric`", artifact["text"])
        self.assertIn("1.21", artifact["text"])
        self.assertIn("[Docs](https://example.com)", artifact["text"])
        etag = response.headers["ETag"]
        self.assertEqual(
            (
                await self.client.get(
                    "/v1/artifacts/" + identity, headers={"If-None-Match": etag}
                )
            ).status,
            304,
        )
        calls = FakeModel.calls
        await self.ensure(self.payload("body"), key="another-key-0001")
        self.assertEqual(FakeModel.calls, calls)

    async def test_idempotency_isolation_and_deduplication(self):
        FakeModel.gate = asyncio.Event()
        one = await self.ensure()
        replay = await self.ensure()
        self.assertEqual(one, replay)
        other = await self.session()
        two = await self.ensure(headers=other)
        first = one["results"][0]["tickets"][0]["job_id"]
        second = two["results"][0]["tickets"][0]["job_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(
            (await self.client.get("/v1/jobs/" + first, headers=other)).status, 404
        )
        conflicting = await self.client.post(
            "/v1/translations:ensure",
            json=dict(self.payload("body"), purpose="detail"),
            headers=dict(self.headers, **{"Idempotency-Key": "test-key-00001"}),
        )
        self.assertEqual(conflicting.status, 409)
        FakeModel.gate.set()
        self.assertEqual((await self.done(first))["state"], "ready")
        self.assertEqual((await self.done(second, other))["state"], "ready")
        self.assertEqual(FakeModel.calls, 1)

    async def test_updates_never_return_wrong_version(self):
        initial = await self.ensure()
        await self.done(initial["results"][0]["tickets"][0]["job_id"])
        old_hash = self.source["fields"]["summary"]["source_hash"]
        FakeSources.summary = "A changed summary."
        new = await self.app["engine"].sources.download(REF)
        await self.store.save_source(new)
        historical = await self.resolve(self.payload(hash=old_hash))
        self.assertEqual(historical["state"], "ready")
        self.assertFalse(historical["is_current"])
        self.assertEqual(historical["requested_source_hash"], old_hash)
        missing = await self.resolve(
            self.payload(hash=new["fields"]["summary"]["source_hash"])
        )
        self.assertEqual(missing["state"], "missing")
        self.assertNotIn("translation", missing)
        self.app["engine"].sources.last_checked.clear()
        bad = await self.ensure(
            self.payload(hash="sha256:" + "a" * 64), key="mismatch-key-001"
        )
        mismatch = await self.done(bad["results"][0]["tickets"][0]["job_id"])
        self.assertEqual(mismatch["state"], "source_mismatch")
        self.schema("Ticket", mismatch)

    async def test_invalid_input_auth_and_quotas(self):
        response = await self.client.post(
            "/v1/translations:ensure", json=dict(self.payload(), purpose="detail")
        )
        self.assertEqual(response.status, 401)
        invalid = self.payload()
        invalid["items"][0]["resource"]["project_id"] = "../../etc/passwd"
        self.assertEqual(
            (await self.client.post("/v1/translations:resolve", json=invalid)).status,
            400,
        )
        invalid = self.payload()
        invalid["items"][0]["text"] = "not allowed"
        self.assertEqual(
            (await self.client.post("/v1/translations:resolve", json=invalid)).status,
            400,
        )
        self.assertEqual(
            (
                await self.client.post("/v1/translations:resolve", data="x" * 70000)
            ).status,
            413,
        )
        self.config.session_fields_per_hour = 1
        await self.ensure()
        limited = await self.client.post(
            "/v1/translations:ensure",
            json=dict(self.payload("body"), purpose="detail"),
            headers=dict(self.headers, **{"Idempotency-Key": "quota-test-0001"}),
        )
        self.assertEqual(limited.status, 429)

    async def test_background_sync_updates_changed_field_after_external_refresh(self):
        initial = await self.ensure()
        await self.done(initial["results"][0]["tickets"][0]["job_id"])
        engine = self.app["engine"]
        FakeSources.summary = "A changed summary."
        # Simulate a manual refresh that already stored the new source. The
        # background loop must compare translation revisions, not two identical
        # source snapshots, or this update would be missed indefinitely.
        new = await engine.sources.download(REF)
        await self.store.save_source(new)
        async with self.store.transaction() as db:
            await db.execute("UPDATE resources SET checked=0")
        engine.sources.last_checked.clear()
        await engine.synchronize_once()
        payload = self.payload(hash=new["fields"]["summary"]["source_hash"])
        for _ in range(200):
            current = await self.resolve(payload)
            if current["state"] == "ready":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(current["state"], "ready")
        self.assertTrue(current["is_current"])
        self.assertEqual(FakeModel.calls, 2)
        tasks = await self.store.rows("SELECT field FROM tasks")
        self.assertEqual({t["field"] for t in tasks}, {"summary"})
        await engine.synchronize_once()
        self.assertEqual(FakeModel.calls, 2)

    async def test_web_manifest_catalogue_history_feedback_admin(self):
        self.schema(
            "Capabilities", await (await self.client.get("/v1/capabilities")).json()
        )
        self.schema(
            "Manifest",
            await (
                await self.client.get("/v1/projects/modrinth/Fixture01/manifest")
            ).json(),
        )
        for path in (
            "/",
            "/docs",
            "/admin",
            "/assets/app.js",
            "/openapi.json",
            "/hash-vectors.json",
        ):
            self.assertEqual((await self.client.get(path)).status, 200, path)
        result = await self.ensure()
        await self.done(result["results"][0]["tickets"][0]["job_id"])
        field = await self.resolve()
        response = await self.client.get("/v1/catalog/search?q=快速")
        data = await response.json()
        self.schema("CatalogueResponse", data)
        self.assertEqual(len(data["projects"]), 1)
        self.schema(
            "HistoryResponse",
            await (
                await self.client.get("/v1/projects/modrinth/Fixture01/history")
            ).json(),
        )
        feedback = dict(
            resource=REF,
            field="summary",
            source_hash=self.source["fields"]["summary"]["source_hash"],
            translation_revision=field["translation"]["translation_revision"],
            category="incorrect",
            message="测试反馈",
        )
        response = await self.client.post(
            "/v1/feedback", json=feedback, headers=self.headers
        )
        self.assertEqual(response.status, 202)
        self.schema("FeedbackReceipt", await response.json())
        self.assertEqual((await self.client.get("/v1/admin/overview")).status, 401)
        self.config.admin_token = "test-secret"
        data = await (
            await self.client.get(
                "/v1/admin/overview", headers={"Authorization": "Bearer test-secret"}
            )
        ).json()
        self.assertEqual(len(data["feedback"]), 1)

    async def test_budget_reservations_atomic(self):
        self.config.daily_token_limit = 100
        results = await asyncio.gather(
            *(self.store.reserve(60, self.config) for _ in range(8))
        )
        self.assertEqual(sum(bool(v) for v in results), 1)
        await self.store.settle(next(v for v in results if v), 20)
        self.assertIsNone(await self.store.reserve(60, self.config))
        async with self.store.transaction() as db:
            await db.execute("UPDATE budget_pause SET resume_at=? WHERE id=1", (time.time() - 1,))
        self.assertIsNotNone(await self.store.reserve(60, self.config))

    async def test_durable_restart_recovers_queue_and_outputs(self):
        await self.app["engine"].close()
        job = await self.app["engine"].enqueue(
            REF,
            "body",
            self.source["fields"]["body"]["source_hash"],
            "zh-Hans",
            "detail",
            await self.store.owner(self.headers["Authorization"][7:]),
        )
        claimed = await self.store.claim()
        self.assertIsNotNone(claimed)
        await self.client.close()
        self.app = await create_app(
            self.config, model_factory=FakeModel, sources_factory=FakeSources
        )
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.store = self.app["store"]
        self.assertEqual((await self.done(job))["state"], "ready")


class ContentCase(unittest.TestCase):
    def test_placeholders_reject_changes_and_keep_technical_literals(self):
        text = "Install **Fast** with `java -jar x.jar` on 1.21.\n\n```sh\nrm x\n```\n[Link](https://example.com)"
        segments, template, placeholders = prepare(text, "markdown", "Fast")
        results = {
            s.id: s.text.replace("Install", "安装")
            .replace("with", "使用")
            .replace(" on ", "，版本 ")
            for s in segments
        }
        output = assemble(template, results, placeholders)
        self.assertIn("`java -jar x.jar`", output)
        self.assertIn("```sh\nrm x\n```", output)
        self.assertIn("[Link](https://example.com)", output)
        original = next(s.text for s in segments if "⟦MC_" in s.text)
        with self.assertRaises(ValueError):
            validate_translation(original, "changed")

    def test_line_endings_only_normalized(self):
        self.assertEqual(
            text_hash("a\r\nb\rc", "summary"), text_hash("a\nb\nc", "summary")
        )
        self.assertNotEqual(text_hash("a ", "summary"), text_hash("a", "summary"))
