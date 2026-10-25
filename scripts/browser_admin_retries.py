"""Real local admin APIs: no login flash, retry actions, filters and failures."""
import asyncio
from pathlib import Path
import sys
import tempfile
import threading
import time

from aiohttp import web
from playwright.sync_api import expect, sync_playwright

from mc_translate.api import create_app
from mc_translate.config import Config
from mc_translate.store import billing_period

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_platform import FakeModel, FakeSources, REF


def main():
    loop=asyncio.new_event_loop()
    thread=threading.Thread(target=loop.run_forever,daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as directory:
        async def start():
            app=await create_app(Config(database=str(Path(directory)/'test.sqlite'),admin_token='browser-admin'),workers=False,model_factory=FakeModel,sources_factory=FakeSources)
            store=app['store']
            source=await app['engine'].sources.fetch(REF)
            for field in ('summary','body'):
                await store.enqueue(REF,field,source['fields'][field]['source_hash'],'zh-Hans','detail')
                task=await store.claim()
                if field=='summary':
                    await store.task_state(task,'failed',dict(code='model_timeout'))
                else:
                    await store.publish(task,source,'Translated body','markdown')
            day,month=billing_period()
            async with store.transaction() as db:
                await db.executemany('INSERT INTO usage(id,day,month,tokens,created,status) VALUES(?,?,?,?,?,?)',[(status,day,month,10,time.time(),status) for status in ('succeeded','failed')])
            runner=web.AppRunner(app)
            await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0)
            await site.start()
            return runner,app,site._server.sockets[0].getsockname()[1]
        runner,app,port=asyncio.run_coroutine_threadsafe(start(),loop).result(15)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport=dict(width=1440,height=1000))
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.add_init_script('''window.loginFlash=false;new MutationObserver(()=>{const e=document.getElementById('login-card');if(e&&!e.classList.contains('hidden'))window.loginFlash=true;}).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['class']});''')
                base=f'http://127.0.0.1:{port}'
                page.goto(base+'/admin')
                page.locator('#token').fill('browser-admin')
                page.locator('#login button').click()
                expect(page.locator('#panel')).to_be_visible()
                def slow_auth(route):
                    time.sleep(0.15)
                    route.continue_()
                page.route('**/v1/admin/session',slow_auth)
                for slug in ('tasks','usage','clients','feedback','mail'):
                    page.goto(base+'/admin/'+slug)
                    expect(page.locator('#panel')).to_be_visible()
                    expect(page.locator('#login-card')).to_be_hidden()
                    assert page.evaluate('()=>window.loginFlash') is False,slug
                page.goto(base+'/admin/tasks')
                page.get_by_role('button',name='成功',exact=True).click()
                expect(page.locator('#tasks article')).to_have_count(1)
                expect(page.locator('#tasks .status-badge')).to_have_text('成功')
                page.get_by_role('button',name='失败',exact=True).click()
                expect(page.locator('#tasks article')).to_have_count(1)
                for width in (320,390,760,1024,1440,1920):
                    page.set_viewport_size(dict(width=width,height=1000))
                    assert page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth+1'),width
                page.set_viewport_size(dict(width=1440,height=1000))
                page.locator('#tasks article').get_by_role('button',name='重试',exact=True).click()
                expect(page.locator('#tasks article')).to_have_count(0)
                expect(page.locator('#message')).to_contain_text('重试已提交')
                queued=asyncio.run_coroutine_threadsafe(app['store'].one("SELECT state,attempt,error FROM tasks WHERE field='summary'"),loop).result(5)
                assert queued==dict(state='queued',attempt=0,error=None),queued
                page.goto(base+'/admin/usage')
                page.locator('#usage-filter').select_option('failed')
                expect(page.locator('#usage-calls article')).to_have_count(1)
                expect(page.locator('#usage-calls .status-badge')).to_have_text('失败')
                page.locator('#usage-filter').select_option('succeeded')
                expect(page.locator('#usage-calls article')).to_have_count(1)
                expect(page.locator('#usage-calls .status-badge')).to_have_text('成功')
                page.route('**/v1/admin/tasks?**',lambda route:route.fulfill(status=503,json={'code':'temporary_failure'}))
                page.goto(base+'/admin/tasks')
                expect(page.locator('#message')).not_to_be_empty()
                expect(page.locator('#login-card')).to_be_hidden()
                assert page.evaluate('()=>window.loginFlash') is False
                page.unroute('**/v1/admin/tasks?**')
                page.unroute('**/v1/admin/session')
                page.route('**/v1/admin/session',lambda route:route.fulfill(status=503,json={'code':'temporary_failure'}))
                page.goto(base+'/admin')
                expect(page.locator('#retry-session')).to_be_visible()
                expect(page.locator('#login-card')).to_be_hidden()
                assert page.evaluate('()=>window.loginFlash') is False
                page.unroute('**/v1/admin/session')
                page.locator('#retry-session').click()
                expect(page.locator('#panel')).to_be_visible()
                page.locator('#logout').click()
                expect(page.locator('#login-card')).to_be_visible()
                assert not errors,errors
                browser.close()
            print('PASS: no login flash across five admin pages, data/auth network recovery, logout, success/failure filters, real task retry, six layout widths; no model calls.')
        finally:
            asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)


if __name__=='__main__':
    main()
