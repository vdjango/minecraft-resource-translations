import asyncio
import copy

from test_platform import FakeModel, FakeSources, PlatformFixture

from mc_translate.content import assemble, prepare, validate_translation
from mc_translate.hashing import digest, resource_ref, text_hash
from mc_translate.metadata import curseforge_metadata, modrinth_metadata


class ResourceInformationTests(PlatformFixture):
    async def test_update_notice_tracks_new_translation_lifecycle(self):
        first = await self.ensure()
        await self.done(first["results"][0]["tickets"][0]["job_id"])
        old_hash = self.source["fields"]["summary"]["source_hash"]
        FakeSources.summary = "A changed summary."
        new = await self.app["engine"].sources.download(self.source["resource"])
        await self.store.save_source(new)
        self.app["engine"].sources.last_checked.clear()
        payload = self.payload(hash=new["fields"]["summary"]["source_hash"])
        missing = await self.resolve(payload)
        self.assertEqual(missing["update"]["code"], "update_pending")
        self.assertTrue(missing["update"]["previous_translation_available"])
        self.assertNotIn("translation", missing)
        historical = await self.resolve(self.payload(hash=old_hash))
        self.assertFalse(historical["is_current"])
        self.assertEqual(historical["update"]["action"], "refresh_source")
        FakeModel.gate = asyncio.Event()
        job = await self.ensure(payload, key="update-lifecycle-001")
        for _ in range(100):
            progress = await self.resolve(payload)
            if progress["update"]["code"] == "update_running":
                break
            await asyncio.sleep(0.01)
        self.assertEqual(progress["update"]["code"], "update_running")
        FakeModel.gate.set()
        await self.done(job["results"][0]["tickets"][0]["job_id"])
        self.assertEqual((await self.resolve(payload))["update"]["code"], "up_to_date")

    async def test_signed_keyset_pagination_filter_binding_and_metadata_changes(self):
        for i in range(5):
            source = copy.deepcopy(self.source)
            source["resource"] = resource_ref("modrinth", "Page" + str(i))
            source["metadata"].update(
                authors=[
                    dict(id="a", name="Author", url="https://modrinth.com/user/a")
                ],
                updated_at="2026-10-20T00:00:00Z",
                tags=["optimization"],
                game_versions=["1.21.1"],
            )
            source["metadata_hash"] = digest(source["metadata"])
            await self.store.save_source(source)
        seen = []
        cursor = ""
        for _ in range(3):
            response = await self.client.get(
                "/v1/catalog/projects", params=dict(limit="2", cursor=cursor)
            )
            page = await response.json()
            self.schema("CatalogueResponse", page)
            self.assertEqual(page["total_count"], 6)
            seen.extend(p["resource"]["project_id"] for p in page["projects"])
            cursor = page["next_cursor"]
        self.assertIsNone(cursor)
        self.assertEqual(len(set(seen)), 6)
        page = await (await self.client.get("/v1/catalog/projects?limit=2")).json()
        cursor = page["next_cursor"]
        for params in [
            dict(limit="3", cursor=cursor),
            dict(limit="2", kind="shader", cursor=cursor),
            dict(limit="2", cursor=cursor[:-3] + "bad"),
        ]:
            response = await self.client.get("/v1/catalog/projects", params=params)
            self.assertEqual(response.status, 409)
        # Changing author metadata retains identity and creates a metadata event.
        source["metadata"]["authors"][0]["name"] = "Changed Author"
        source["metadata_hash"] = digest(source["metadata"])
        await self.store.save_source(source)
        history = await (
            await self.client.get("/v1/projects/modrinth/Page4/history")
        ).json()
        self.assertIn("metadata_updated", [e["type"] for e in history["events"]])

    async def test_html_structure_protected_and_artifact_format_preserved(self):
        html = '<h2>English heading</h2><p>A fast mod. <strong>Powerful features</strong></p><table><tr><td>English cell</td></tr></table><img src="https://example.com/a.png"><pre><code>x &lt; y</code></pre>'
        segments, template, placeholders = prepare(html, "html", "Fixture")
        result = {
            s.id: s.text.replace("English heading", "中文标题")
            .replace("A fast mod.", "快速模组。")
            .replace("Powerful features", "强大功能")
            .replace("English cell", "中文单元格")
            for s in segments
        }
        output = assemble(template, result, placeholders)
        self.assertIn("<h2>中文标题</h2>", output)
        self.assertIn("<strong>强大功能</strong>", output)
        self.assertIn("<td>中文单元格</td>", output)
        self.assertIn('<img src="https://example.com/a.png">', output)
        self.assertIn("<pre><code>x &lt; y</code></pre>", output)
        with self.assertRaises(ValueError):
            validate_translation(
                "⟦MC_1111111111111111⟧text⟦MC_2222222222222222⟧",
                "⟦MC_2222222222222222⟧中文⟦MC_1111111111111111⟧",
            )

    async def test_html_translation_published_as_renderable_html_artifact(self):
        source = copy.deepcopy(self.source)
        html = '<h2>A fast mod.</h2><p><strong>Install with</strong> Fabric.</p><img src="https://example.com/a.png">'
        source["fields"]["body"].update(
            text=html, format="html", source_hash=text_hash(html, "body", "html")
        )
        source["snapshot_hash"] = digest(
            {k: v["source_hash"] for k, v in source["fields"].items()}
        )
        await self.store.save_source(source)
        payload = self.payload("body", source["fields"]["body"]["source_hash"])
        result = await self.ensure(payload)
        ticket = await self.done(result["results"][0]["tickets"][0]["job_id"])
        self.assertEqual(ticket["state"], "ready")
        translated = (await self.resolve(payload))["translation"]
        self.assertEqual(translated["format"], "html")
        artifact = await (
            await self.client.get(
                "/v1/artifacts/" + translated["artifact"]["artifact_sha256"]
            )
        ).json()
        self.assertEqual(artifact["format"], "html")
        self.assertIn("<h2>一个快速的模组。</h2>", artifact["text"])
        self.assertIn('<img src="https://example.com/a.png">', artifact["text"])

    async def test_provider_metadata_dependencies_belong_to_each_version(self):
        mr = modrinth_metadata(
            dict(
                id="project",
                published="2020",
                updated="2026",
                categories=["optimization"],
                game_versions=["1.21"],
                loaders=["fabric"],
                license={"id": "MIT"},
            ),
            [dict(user=dict(id="u1", username="Author"), role="Owner")],
            [
                dict(
                    id="v1",
                    project_id="project",
                    game_versions=["1.21"],
                    loaders=["fabric"],
                    dependencies=[dict(project_id="dep", dependency_type="required")],
                ),
                dict(id="v2", project_id="project", dependencies=[]),
            ],
        )
        self.assertEqual(mr["authors"][0]["name"], "Author")
        self.assertEqual(len(mr["releases"][0]["dependencies"]), 1)
        self.assertEqual(mr["releases"][1]["dependencies"], [])
        cf = curseforge_metadata(
            dict(
                id=123,
                classId=6,
                authors=[dict(id=1, name="Author")],
                latestFilesIndexes=[dict(gameVersion="1.21", modLoader=6)],
            ),
            [
                dict(
                    id=1,
                    modId=123,
                    gameVersions=["1.21", "NeoForge"],
                    dependencies=[dict(modId=321, relationType=3)],
                )
            ],
        )
        self.assertEqual(cf["loaders"], ["neoforge"])
        self.assertEqual(cf["releases"][0]["dependencies"][0]["relation"], "required")
