import copy
import json
import xml.etree.ElementTree as ET

from test_platform import FakeModel, FakeSources, PlatformFixture
from mc_translate.hashing import digest, text_hash
from mc_translate.public_pages import render_body


class PublicPagesCase(PlatformFixture):
    async def translated(self):
        for name in ("summary", "body"):
            task = await self.ensure(self.payload(name), key="seo-ready-" + name)
            self.assertEqual((await self.done(task["results"][0]["tickets"][0]["job_id"]))["state"], "ready")

    async def test_ready_detail_is_usable_and_html_contains_translation_without_javascript(self):
        await self.translated()
        calls = FakeModel.calls, FakeSources.downloads
        catalogue = await (await self.client.get("/v1/catalog/projects")).json()
        self.assertEqual(catalogue["projects"][0]["translation_state"], "ready")
        detail = await (await self.client.get("/v1/projects/modrinth/Fixture01/detail")).json()
        self.assertTrue(all(f["state"] == "ready" and f["is_current"] for f in detail["fields"]))
        self.assertIn("一个快速的模组", detail["fields"][1]["translation"]["text"])
        response = await self.client.get("/projects/modrinth/Fixture01")
        html = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Cloudflare-CDN-Cache-Control"], "no-store")
        self.assertIn("一个快速的模组", html)
        self.assertIn('<code>fabric</code>', html)
        self.assertIn('rel="canonical" href="http://localhost/projects/modrinth/Fixture01"', html)
        self.assertNotIn("{{BODY}}", html)
        self.assertEqual(calls, (FakeModel.calls, FakeSources.downloads))
        response = await self.client.get("/")
        self.assertIn('href="/projects/modrinth/Fixture01"', await response.text())
        self.assertIn("中文", await (await self.client.get("/")).text())

    async def test_source_update_invalidates_etag_and_never_calls_old_translation_ready(self):
        await self.translated()
        first = await self.client.get("/projects/modrinth/Fixture01")
        etag = first.headers["ETag"]
        response = await self.client.get("/projects/modrinth/Fixture01", headers={"If-None-Match": etag})
        self.assertEqual(response.status, 304)
        source = copy.deepcopy(self.source)
        source["fields"]["body"].update(text="New body.", source_hash=text_hash("New body.", "body", "markdown"))
        source["snapshot_hash"] = digest("changed")
        await self.store.save_source(source)
        response = await self.client.get("/projects/modrinth/Fixture01", headers={"If-None-Match": etag})
        self.assertEqual(response.status, 200)
        self.assertNotEqual(response.headers["ETag"], etag)
        self.assertIn("New body.", await response.text())
        self.assertNotIn("简介与正文已就绪", await response.text())
        detail = await (await self.client.get("/v1/projects/modrinth/Fixture01/detail")).json()
        self.assertEqual(detail["fields"][1]["state"], "missing")

    async def test_crawl_entry_points_and_real_404(self):
        for path in ("/sitemap.xml", "/sitemaps/pages.xml", "/sitemaps/projects-1.xml"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200)
            root = ET.fromstring(await response.text())
            for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
                target = loc.text.removeprefix("http://localhost")
                self.assertEqual((await self.client.get(target)).status, 200, target)
        for path in ("/sitemaps/projects-0.xml", "/sitemaps/projects-999.xml", "/projects/modrinth/Missing"):
            self.assertEqual((await self.client.get(path)).status, 404)
        self.assertIn("Sitemap: http://localhost/sitemap.xml", await (await self.client.get("/robots.txt")).text())
        self.assertIn("noindex", (await self.client.get("/admin")).headers["X-Robots-Tag"])
        self.assertIn("noindex", (await self.client.get("/?q=anything")).headers["X-Robots-Tag"])

    async def test_markup_sanitizer_and_language_isolation(self):
        unsafe = '<script>alert(1)</script><img src="https://a.test/p.png" onerror="x()"><a href="javascript:x()">x</a><iframe src="https://x.test"></iframe><h2>Heading</h2>'
        clean = render_body(unsafe, "html")
        for text in ("<script", "onerror", "javascript:", "<iframe"):
            self.assertNotIn(text, clean)
        self.assertIn('<h2>Heading</h2>', clean)
        self.assertIn('loading="lazy"', clean)
        await self.translated()
        response = await self.client.get("/projects/modrinth/Fixture01?target_language=ja")
        self.assertNotIn("一个快速的模组", await response.text())
        self.assertIn('target_language=ja', await response.text())
