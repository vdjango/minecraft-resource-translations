"""Browser regression for first installation and the unified source controls."""
import asyncio
import tempfile
import threading
from pathlib import Path

from aiohttp import web
from playwright.sync_api import expect,sync_playwright

from mc_translate.api import create_app
from mc_translate.config import Config
from mc_translate.mirror import read_settings


def main():
    loop=asyncio.new_event_loop()
    thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
    with tempfile.TemporaryDirectory() as directory:
        config=Config(database=str(Path(directory)/'translations.sqlite'),mirror_root=str(Path(directory)/'resources'),mirror_port=1)
        async def start():
            app=await create_app(config,workers=False)
            async def control(body=None):
                if body and body['action']=='source_files':
                    return dict(files=[dict(source='aliyun',path='/maven/org/example/exclusive.jar',
                        target='https://maven.aliyun.com/repository/public/org/example/exclusive.jar',sha1='a'*40,
                        local_bytes=1024,redirects=7)],total=1,offset=0,next_offset=None)
                if body and body['action']=='source_preview':return dict(files=1,reclaimable_bytes=1024)
                return dict(running=True,settings=read_settings(config),root=config.mirror_root,cached_files=1,
                    cached_bytes=1024,disk_free=100*1024**3,active_downloads=0,nodes=[],job=dict(state='idle'),
                    automatic_sources=dict(generated_at=1,matched=1,remaining=0,providers=[]),
                    source_statistics=[dict(source='aliyun',files=1,local_files=1,local_bytes=1024,redirects=7)])
            app['installation'].control.call=control
            runner=web.AppRunner(app);await runner.setup()
            site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
            return runner,site._server.sockets[0].getsockname()[1]
        runner,port=asyncio.run_coroutine_threadsafe(start(),loop).result(10)
        errors=[]
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                page=browser.new_page(viewport=dict(width=1440,height=1100))
                page.on('pageerror',lambda error:errors.append(str(error)))
                base=f'http://127.0.0.1:{port}'
                screenshots=Path('build/browser-setup-sources');screenshots.mkdir(parents=True,exist_ok=True)
                page.goto(base+'/docs');expect(page).to_have_url(base+'/setup')
                expect(page.get_by_label('管理员用户名')).to_be_visible()
                page.screenshot(path=str(screenshots/'setup-desktop.png'),full_page=True)
                page.set_viewport_size(dict(width=390,height=844))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(screenshots/'setup-mobile.png'),full_page=True)
                page.set_viewport_size(dict(width=1440,height=1100))
                page.get_by_label('管理员用户名').fill('browser-owner')
                page.get_by_label('管理员密码',exact=True).fill('wizard-browser-password')
                page.get_by_label('确认密码',exact=True).fill('wizard-browser-password')
                page.get_by_role('button',name='保存并继续 →',exact=True).click()
                expect(page.get_by_label('平台公开地址')).to_be_visible()
                page.get_by_label('平台公开地址').fill(base)
                page.get_by_role('button',name='保存并继续 →',exact=True).click()
                expect(page.get_by_text('暂不启用新译文生成',exact=False)).to_be_visible()
                page.get_by_role('button',name='保存并继续 →',exact=True).click()
                expect(page.get_by_label('缓存上限 / GiB')).to_be_visible()
                page.reload();expect(page.get_by_label('缓存上限 / GiB')).to_be_visible()
                page.get_by_label('缓存上限 / GiB').fill('50')
                page.get_by_role('button',name='保存并继续 →',exact=True).click()
                expect(page.get_by_text('暂不启用邮件',exact=False)).to_be_visible()
                page.get_by_role('button',name='保存并继续 →',exact=True).click()
                page.get_by_role('button',name='完成安装，进入后台 →',exact=True).click()
                expect(page).to_have_url(base+'/admin')
                page.goto(base+'/admin/settings#sources')
                expect(page.locator('#mirror-source-providers .source-provider')).to_have_count(6)
                expect(page.locator('#mirror-source-statistics')).to_contain_text('阿里云 Maven')
                page.locator('#mirror-source-statistics button').first.click()
                expect(page.locator('#mirror-source-files')).to_contain_text('exclusive.jar')
                expect(page.locator('#mirror-source-files')).to_contain_text('7')
                page.locator('#mirror-source-preview').click()
                expect(page.locator('#mirror-source-clean')).to_be_enabled()
                page.locator('.source-provider-details summary').click()
                page.locator('#mirror-source-add').click()
                expect(page.locator('#mirror-source-providers .source-provider')).to_have_count(7)
                row=page.locator('.source-provider').last
                row.locator('[data-field=name]').fill('Extra repository')
                row.locator('[data-field=url]').fill('https://extra.example/repository/')
                page.get_by_role('button',name='保存资源分发设置',exact=True).click()
                expect(page.locator('#message')).to_contain_text('已保存')
                page.reload();expect(page.locator('.source-provider')).to_have_count(7)
                page.screenshot(path=str(screenshots/'sources-desktop.png'),full_page=True)
                page.set_viewport_size(dict(width=390,height=844))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(screenshots/'sources-mobile.png'),full_page=True)
                page.goto(base+'/docs#source-discovery');expect(page.locator('#source-discovery')).to_be_visible()
                assert not errors,errors
                browser.close()
                print('PASS: first-install entry, six steps, resume, source statistics, files, cleanup preview, add/save/reload, desktop/mobile; no JavaScript errors')
        finally:
            asyncio.run_coroutine_threadsafe(runner.cleanup(),loop).result(10)
            loop.call_soon_threadsafe(loop.stop);thread.join(5)


if __name__=='__main__':main()
