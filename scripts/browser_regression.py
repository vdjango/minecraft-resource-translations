"""Run with Python + Playwright and installed Microsoft Edge; no paid calls.

Exercises the actual Web assets against deterministic local API fixtures.
The separate backend suite verifies the real signed cursor and artifact API.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

WEB = Path(__file__).resolve().parents[1] / "src/mc_translate/web"
RESOURCE = dict(
    provider="modrinth",
    project_id="Fixture00",
    entity_type="project",
    entity_id="Fixture00",
)
ORIGINAL = '<h2>English heading</h2><p>A fast mod. <strong>Features</strong></p><table><tr><td>English cell</td></tr></table><img src="https://example.com/picture.png"><script>window.unsafeExecuted=true</script><a href="javascript:alert(1)">Unsafe</a>'
TRANSLATED = (
    ORIGINAL.replace("English heading", "中文标题")
    .replace("A fast mod.", "快速模组。")
    .replace("English cell", "中文单元格")
)
SOURCE = dict(
    resource=RESOURCE,
    kind="mod",
    source_url="https://modrinth.com/project/Fixture00",
    snapshot_hash="sha256:fixture",
    metadata=dict(
        authors=[dict(name="Fixture Author")],
        tags=["optimization"],
        game_versions=["1.21.1"],
        loaders=["fabric"],
        releases=[],
    ),
    fields={
        "title": dict(text="Fixture 000"),
        "summary": dict(text="A fast mod.", source_hash="sha256:summary"),
        "body": dict(text=ORIGINAL, format="html", source_hash="sha256:body"),
    },
)


class Handler(BaseHTTPRequestHandler):
    resolve_count = 0

    def log_message(self, *args):
        pass

    def send(self, value, content_type="application/json", status=200):
        raw = (
            json.dumps(value, ensure_ascii=False).encode()
            if content_type == "application/json"
            else value
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # Navigation may cancel deliberately delayed fixture requests.

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path.startswith("/v1/"):
            if url.path.endswith('/federation/health'):
                time.sleep(1.2)
                return self.send(dict(role='hub'))
            if "catalog/" in url.path:
                params = parse_qs(url.query)
                limit = int(params.get("limit", [20])[0])
                start = int(params.get("cursor", [0])[0])
                query = params.get("q", [""])[0]
                total = 3 if query else 75
                # A visible delay catches rapid clicks and button state races.
                time.sleep(0.1)
                projects = [
                    dict(
                        resource=dict(
                            RESOURCE,
                            project_id=f"Fixture{i:02d}",
                            entity_id=f"Fixture{i:02d}",
                        ),
                        title=f"Fixture {i:03d}",
                        summary="Original",
                        localized_summary="中文简介",
                        kind="mod",
                        translation_state="ready",
                        metadata=SOURCE["metadata"],
                    )
                    for i in range(start, min(start + limit, total))
                ]
                return self.send(
                    dict(
                        projects=projects,
                        total_count=total,
                        next_cursor=str(start + limit)
                        if start + limit < total
                        else None,
                    )
                )
            if url.path.endswith("/source"):
                return self.send(SOURCE)
            if url.path.endswith("/history"):
                return self.send(dict(events=[]))
            if "/artifacts/" in url.path:
                return self.send(dict(text=TRANSLATED, format="html"))
            if url.path.endswith("/capabilities"):
                time.sleep(1.2)
                return self.send(dict(generation_available=True))
            return self.send({}, status=404)
        relative = (
            "index.html"
            if url.path == "/"
            else "docs.html"
            if url.path == "/docs"
            else url.path.removeprefix("/assets/")
        )
        file = (WEB / relative.lstrip("/")).resolve()
        if not file.is_relative_to(WEB) or not file.is_file():
            return self.send({}, status=404)
        mime = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "application/javascript",
        }.get(file.suffix, "text/plain; charset=utf-8")
        self.send(file.read_bytes(), mime)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("sessions/anonymous"):
            return self.send(dict(access_token="fixture-token", expires_in=3600))
        if self.path.endswith("translations:ensure"):
            time.sleep(5)  # Queueing must not block polling or ready translations.
            return self.send(dict(results=[]))
        if self.path.endswith("translations:resolve"):
            type(self).resolve_count += 1
            fields = []
            for field in body["items"][0]["fields"]:
                pending = field["field"] == "body" and self.resolve_count < 3
                translated = (
                    dict(text="中文简介", format="plain")
                    if field["field"] == "summary"
                    else dict(
                        artifact=dict(artifact_sha256="fixture-artifact"),
                        translation_revision="tr_fixture",
                    )
                )
                fields.append(
                    dict(
                        field=field["field"],
                        requested_source_hash=field["source_hash"],
                        state="running" if pending else "ready",
                        matches_request=not pending,
                        is_current=True,
                        translation=translated,
                        update=dict(
                            notice="原文已更新，正在翻译新版本" if pending else "",
                            action="ensure" if pending else "none",
                        ),
                    )
                )
            return self.send(dict(results=[dict(resource=RESOURCE, fields=fields)]))
        self.send({}, status=404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1000))
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("https://example.com/**", lambda route: route.abort())
            base = f"http://127.0.0.1:{server.server_port}"
            started = time.monotonic()
            page.goto(base, wait_until='domcontentloaded')
            page.wait_for_function(
                "document.querySelectorAll('.resource-card').length === 20"
            )
            assert time.monotonic() - started < 1.15, 'Catalog blocked by capabilities/federation discovery'
            assert "75" in page.locator("#count").inner_text()
            assert page.locator("#previous-page").is_disabled()
            first_page = page.locator(".resource-title").all_text_contents()
            for index in (1, 2, 3):
                page.locator("#more").click()
                page.wait_for_function(
                    "index => document.getElementById('page-info').textContent.startsWith('第 '+index+' 页')",
                    arg=index + 1,
                )
                assert (
                    page.locator(".resource-title").first.inner_text()
                    == f"Fixture {index * 20:03d}"
                )
            assert page.locator("#more").is_disabled()
            assert page.locator(".resource-card").count() == 15
            for index in (2, 1, 0):
                page.locator("#previous-page").click()
                page.wait_for_function(
                    "index => document.getElementById('page-info').textContent.startsWith('第 '+index+' 页')",
                    arg=index + 1,
                )
            assert page.locator(".resource-title").all_text_contents() == first_page
            page.locator("#page-size").select_option("50")
            page.wait_for_function(
                "document.querySelectorAll('.resource-card').length === 50"
            )
            page.locator("#more").click()
            page.wait_for_function(
                "document.querySelectorAll('.resource-card').length === 25"
            )
            page.locator("#query").fill("fixture")
            page.locator("#search-form button").click()
            page.wait_for_function(
                "document.querySelectorAll('.resource-card').length === 3"
            )
            assert page.locator("#previous-page").is_disabled()
            assert "第 1 页" in page.locator("#page-info").inner_text()
            page.locator(".resource-title").first.click()
            page.wait_for_function(
                "document.getElementById('detail-status').textContent.includes('原文已更新')"
            )
            assert page.locator("#translated-body h2").inner_text() == "English heading"
            assert "原文" in page.locator("#body-language-label").inner_text()
            page.wait_for_function(
                "document.getElementById('translated-body').dataset.revision === 'tr_fixture'", timeout=4500
            )
            assert page.locator("#translated-body h2").inner_text() == "中文标题"
            assert (
                page.locator("#translated-body table td").inner_text() == "中文单元格"
            )
            assert page.locator("#translated-body img").count() == 1
            assert page.locator("#translated-body script").count() == 0
            assert page.evaluate("window.unsafeExecuted === undefined")
            assert page.locator("#translated-body a").get_attribute("href") is None
            page.locator("#toggle-original").click()
            assert page.locator("#original-body h2").inner_text() == "English heading"
            page.locator("[data-tab=releases]").click()
            assert page.locator("#panel-releases").is_visible()
            page.locator("[data-tab=dependencies]").click()
            assert page.locator("#dependency-list").inner_text()
            page.locator("#detail-dialog .close").click()
            page.locator(".resource-title").first.click()
            page.wait_for_function(
                "document.getElementById('translated-body').dataset.revision === 'tr_fixture'"
            )
            assert "中文标题" in page.locator("#translated-body").inner_text()
            page.goto(base + "/docs")
            for language, filename in (
                ("python", "client.py"),
                ("java", "Client.java"),
                ("c", "client.c"),
                ("cpp", "client.cpp"),
                ("csharp", "Program.cs"),
            ):
                page.locator(f"[data-language={language}]").click()
                page.wait_for_function(
                    "document.getElementById('example-code').textContent.length > 500"
                )
                assert (
                    page.locator("#example-download")
                    .get_attribute("href")
                    .endswith(filename)
                )
                assert page.locator("#example-command .copy-code").count() == 1
                assert (
                    page.request.get(base + "/assets/examples/" + filename).status
                    == 200
                )
            for path in ("/", "/docs"):
                page.set_viewport_size(dict(width=390, height=844))
                page.goto(base + path)
                page.wait_for_timeout(300)
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth + 1"
                ), path
            assert not errors, errors
            browser.close()
            print(
                json.dumps(
                    dict(
                        pagination="75 projects: 20/50, next/previous/search reset",
                        rendering="HTML original + translation, original while pending, XSS, reopen",
                        docs="5 languages, downloads, copy controls",
                        responsive="390px",
                        page_errors=errors,
                    )
                )
            )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
