"""Exercise management pages, application limits and session auth in a real browser."""

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

from aiohttp import web
from playwright.sync_api import expect, sync_playwright

from mc_translate.api import create_app
from mc_translate.config import Config
from mc_translate.management import Settings


def main():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        config = Config(database=str(Path(directory) / "test.sqlite"), admin_token="browser-admin")

        async def start():
            app = await create_app(config, workers=False, mail_sender=lambda *_: None)
            async with app["store"].transaction() as db:
                for i in range(51):
                    await db.execute(
                        "INSERT INTO client_keys(id,hash,prefix,name,contact,website,purpose,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"z{i:03}", f"hash{i}", "fixture", f"Fixture {i}", "fixture@example.org", "http://example.org", "Test", 0, 0),
                    )
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            return runner, site._server.sockets[0].getsockname()[1]

        runner, port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.route("**/v1/admin/models", lambda route: route.fulfill(json={"models": ["gpt-6.1-sol", "browser-model"]}))
                page.on("pageerror", lambda error: errors.append(str(error)))
                base = f"http://127.0.0.1:{port}"
                screenshots = Path(__file__).resolve().parents[1] / "build/browser-management"
                screenshots.mkdir(parents=True, exist_ok=True)

                def records(route):
                    from urllib.parse import parse_qs, urlsplit
                    query = parse_qs(urlsplit(route.request.url).query)
                    start = int(query.get("cursor", [0])[0])
                    stamp = time.time()
                    if "/tasks" in route.request.url:
                        state = query.get("state", [""])[0]
                        tasks = [dict(id=i, title="Resource " + str(i), key="modrinth/project/fixture" + str(i), field="body", language="zh-Hans", state=state or ("failed" if i % 3 == 0 else "ready"), attempt=1, completed=2, total=4, created=stamp, updated=stamp, next_attempt=stamp + 100, error={"code":"model_timeout"} if i % 3 == 0 else None) for i in range(start, min(start + 100, 125))]
                        route.fulfill(json={"tasks":tasks, "pause":None, "next_cursor":"100" if start == 0 else None})
                    else:
                        calls = [dict(id=i, model="gpt-6.1-sol", status="pending" if i == 1 else "succeeded", created=stamp, tokens=1200, input_tokens=800, cached_tokens=100, output_tokens=400, actual=True, details_known=True, cost=0.00325) for i in range(start, min(start + 100, 125))]
                        route.fulfill(json={"calls":calls, "next_cursor":"100" if start == 0 else None, "key":"2026-10-25", "summary":dict(tokens=150000, calls=125, succeeded=125, failed=0, unconfirmed=0, cost=0.40625, cost_unknown=0)})

                page.route("**/v1/admin/tasks?**", records)
                page.route("**/v1/admin/usage?**", records)
                page.goto(base + "/developers")
                for name, value in dict(name="Browser launcher", contact="test@example.org", website="http://example.org/home", purpose="Browser integration").items():
                    page.locator("#" + name).fill(value)
                page.locator("#application button").click()
                page.wait_for_timeout(300)
                expect(page.locator("#issued-key")).to_contain_text("bct_")
                key = page.locator("#issued-key").inner_text()
                page.locator("#status-key").fill(key)
                page.locator("#status-form button").click()
                expect(page.locator("#application-status")).to_contain_text("等待管理员审批")
                assert "字段/小时" not in page.locator("#application-status").inner_text()
                for _ in range(2):
                    assert page.evaluate("async () => (await fetch('/v1/clients/applications',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status") == 400
                assert page.evaluate("async () => (await fetch('/v1/clients/applications',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).status") == 429

                page.goto(base + "/admin")
                page.locator("#token").fill("browser-admin")
                page.locator("#login button").click()
                expect(page.locator("#message")).to_contain_text("管理数据已更新")
                assert page.locator("#token").input_value() == ""
                assert page.evaluate("document.body.innerText.includes('{\"')") is False

                page.goto(base + "/admin/model")
                expect(page.locator("#page-title")).to_have_text("模型与额度")
                page.locator("#base-url").fill("https://example.org/v1")
                expect(page.locator("#model-state")).to_contain_text("已读取 2 个模型")
                page.locator("#model").select_option("browser-model")
                page.locator("#api-key").fill("private-browser-key")
                page.locator("#price-input").fill("2")
                page.locator("#price-cached").fill("0.5")
                page.locator("#price-output").fill("8")
                page.locator('#settings button[type="submit"]').click()
                expect(page.locator("#message")).to_contain_text("模型配置已更新")
                assert page.locator("#api-key").input_value() == ""
                assert Settings(Config(database=config.database)).config.model_prices["browser-model"]["output"] == 8

                page.goto(base + "/admin/tasks")
                expect(page.locator("#page-title")).to_have_text("翻译任务")
                assert page.locator("#model-section").count() == 0
                expect(page.locator("#tasks article")).to_have_count(20)
                assert page.locator("#tasks article").first.bounding_box()["height"] < 95
                page.locator("#tasks article").first.get_by_role("button", name="详情").click()
                expect(page.locator("#record-dialog")).to_contain_text("模型请求超时")
                page.locator("#record-dialog").get_by_role("button", name="关闭").click()
                for index in range(6):
                    page.locator(".record-pagination").get_by_role("button", name="下一页").click()
                    expect(page.locator("#tasks article").first).to_contain_text(f"Resource {(index + 1) * 20}")
                expect(page.locator("#tasks article")).to_have_count(5)
                expect(page.locator(".record-pagination").get_by_role("button", name="下一页")).to_be_disabled()
                page.locator("#task-filter").select_option("deferred")
                expect(page.locator("#tasks article")).to_have_count(20)
                expect(page.locator("#tasks article").first.locator(".status-badge")).to_have_text("延后重试")
                page.screenshot(path=str(screenshots / "tasks-desktop.png"))
                page.goto(base + "/admin/usage")
                expect(page.locator("#usage-summary")).to_be_visible()
                expect(page.locator("#usage-calls article")).to_have_count(20)
                expect(page.locator("#usage-calls article").nth(1).locator(".status-badge")).to_have_text("调用中")
                page.locator("#usage-calls article").first.get_by_role("button", name="明细").click()
                expect(page.locator("#record-dialog")).to_contain_text("其中缓存输入")
                page.locator("#record-dialog").get_by_role("button", name="关闭").click()
                page.screenshot(path=str(screenshots / "usage-desktop.png"))
                page.goto(base + "/admin/mail")
                page.locator("#mail-mail_enabled").select_option("true")
                page.locator("#mail-smtp_username").fill("sender@163.com")
                page.locator("#mail-smtp_password").fill("private-smtp-browser")
                page.locator("#mail-mail_from").fill("sender@163.com")
                page.locator("#mail-mail_to").fill("recipient@qq.com")
                page.get_by_role("button", name="保存邮件配置").click()
                expect(page.locator("#message")).to_contain_text("邮件配置已保存")
                page.get_by_role("button", name="发送测试邮件").click()
                expect(page.locator("#message")).to_contain_text("测试邮件已加入队列")
                page.wait_for_timeout(250)
                page.get_by_role("button", name="刷新发送状态").click()
                expect(page.locator("#mail-status")).to_contain_text("SMTP 已接受")

                page.goto(base + "/admin/clients")
                expect(page.locator("#clients article")).to_have_count(20)
                application = page.locator("#clients article").filter(has_text="Browser launcher")
                page.on("dialog", lambda dialog: dialog.accept(dialog.default_value))
                application.get_by_role("button", name="审批").click()
                expect(page.locator("#record-dialog")).to_contain_text("http://example.org/home")
                page.locator("#record-dialog").get_by_role("button", name="批准申请").click()
                page.get_by_role("button", name="确认启用").click()
                expect(application.locator(".status-badge")).to_contain_text("已启用")
                assert "字段/小时" in application.inner_text()
                page.locator(".record-pagination").get_by_role("button", name="下一页").click()
                expect(page.locator("#clients article")).to_have_count(20)
                page.locator(".record-pagination").get_by_role("button", name="下一页").click()
                expect(page.locator("#clients article")).to_have_count(12)
                expect(page.locator(".record-pagination").get_by_role("button", name="下一页")).to_be_disabled()
                page.locator(".record-pagination select").select_option("50")
                expect(page.locator("#clients article")).to_have_count(50)
                application.get_by_role("button", name="管理").click()
                page.locator("#record-dialog").get_by_role("button", name="撤销密钥").click()
                expect(application.locator(".status-badge")).to_contain_text("已撤销")
                page.screenshot(path=str(screenshots / "clients-desktop.png"))

                for slug in ("tasks", "usage", "clients"):
                    page.goto(base + "/admin/" + slug)
                    expect(page.locator("#panel")).to_be_visible()
                    page.set_viewport_size({"width":390, "height":844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), slug
                    page.screenshot(path=str(screenshots / (slug + "-mobile.png")))
                    page.set_viewport_size({"width":1440, "height":1000})

                page.goto(base + "/admin")
                page.screenshot(path=str(screenshots / "admin-desktop.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.get_by_role("button", name="切换到浅色主题").click()
                page.screenshot(path=str(screenshots / "admin-mobile-light.png"), full_page=True)
                page.goto(base + "/developers")
                page.locator("#status-key").fill(key)
                page.locator("#status-form button").click()
                expect(page.locator("#application-status")).to_contain_text("接入密钥已撤销")
                assert not errors, errors
                browser.close()
            print(json.dumps(dict(management="split pages, HTTP application, three daily failures, session auth, model price persistence, email queue, 52-client pagination, revocation", responsive="390px", page_errors=errors)))
        finally:
            asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)


if __name__ == "__main__":
    main()
