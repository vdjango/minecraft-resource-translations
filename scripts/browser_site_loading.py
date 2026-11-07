"""Focused offline browser smoke: loading feedback, navigation and mirror admin.

Uses the built assets from sibling sites when available. No paid/model calls,
production credentials or external sources are used.
"""
import json
import mimetypes
import os
import re
import sys
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from mc_translate.config import Config
from mc_translate.management import Settings
from mc_translate.mirror import validate_settings
from mc_translate.spa_shell import render_shell
from browser_regression import Handler as CatalogueHandler

REPOSITORY=Path(__file__).resolve().parents[1]
WORKSPACE=REPOSITORY.parent.parent if REPOSITORY.parent.name=='.worktrees' else REPOSITORY.parent
WEB=REPOSITORY/'src/mc_translate/web'
FRONTEND=REPOSITORY/'frontend'
PLATFORM_ROUTES=[('/projects/modrinth/Fixture00' if path.startswith('/projects/:') else path,entry)
                 for path,entry in re.findall(r"\{path:'([^']+)',entry:'([^']+)'",
                     (FRONTEND/'src/platformRoutes.ts').read_text(encoding='utf-8'))]
PLATFORM_ROUTES.extend(('/admin/mirror/settings/'+tab,'admin-mirror') for tab in
                      ('downloads','storage','cloud','distribution','sources','quality','room-cache','cloudflare'))
NODE_REPOSITORY=Path(os.environ.get('BUGGCRAFT_NODE_REPOSITORY',WORKSPACE/'minecraft-tunnel-native'))
SETTINGS=validate_settings({})
ADMIN_CHILDREN={
    '/admin':['/admin','/admin/traffic'],
    '/admin/mirror':['/admin/mirror','/admin/mirror/resources','/admin/mirror/tasks',
                     '/admin/mirror/temporary','/admin/mirror/sources','/admin/mirror/quality'],
    '/admin/tasks':['/admin/tasks','/admin/usage','/admin/feedback'],
    '/admin/nodes':['/admin/nodes','/admin/clients'],
    '/admin/mirror/settings':['/admin/mirror/settings','/admin/settings','/admin/mail'],
}
MIRROR=dict(running=True, settings=SETTINGS, cached_files=320, cached_bytes=12345678,
            disk_free=50*1024**3, root='/fixture/cache',active_downloads=0,job=dict(state='idle'),
            source_quality=dict(daily_bytes=262144,daily_limit=32*1024**2,sources={}))


class PlatformHandler(CatalogueHandler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/mirror-api/status':
            time.sleep(.5)
            return self.send(dict(MIRROR,policies=SETTINGS['policies']))
        if path=='/v1/admin/session':return self.send(dict(authenticated=True))
        if path=='/v1/admin/mirror':return self.send(MIRROR)
        if path=='/v1/admin/models':return self.send(dict(models=['gpt-6.1-sol']))
        if path=='/v1/admin/settings':
            # Nonexistent temporary DB path prevents loading any local settings.
            return self.send(Settings(Config(database=str(WEB/'__fixture_nonexistent__/db.sqlite'))).public())
        if path=='/projects/modrinth/Fixture00':
            return self.send(render_shell(WEB,'project','<title>Fixture project</title>',
                                          seed={'title':'Fixture project','summary':'Project summary',
                                                'body':'<p>Readable detail</p>','detail':'{}'}).encode(),
                             'text/html; charset=utf-8')
        entries={'/mirror':'mirror','/developers':'developers',
                 '/admin/settings':'admin-model','/admin/mirror':'admin-mirror'}
        entries.update(PLATFORM_ROUTES)
        if path.startswith('/admin/mirror/'):
            entries[path]='admin-mirror'
        if path in entries:
            time.sleep(.25)
            return self.send(render_shell(WEB, entries[path], '<title>Browser fixture</title>',
                                          seed={'authMode':'session'}).encode(), 'text/html; charset=utf-8')
        if path=='/docs':time.sleep(.4)
        return super().do_GET()

    def do_POST(self):
        if urlsplit(self.path).path=='/v1/admin/mirror':
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            return self.send(dict(samples=[]) if body.get('action')=='quality_history' else MIRROR)
        return super().do_POST()


class StaticHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        path=urlsplit(self.path).path
        root=self.server.root
        if path.startswith(('/v1/','/api/')):
            self.send_response(503);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"error":"offline_fixture"}');return
        if root.name=='site':
            relative='download/index.html' if path=='/download' else 'index.html' if path=='/' else path.lstrip('/')
        else:
            relative='index.html' if path=='/' else path.removeprefix('/assets/').lstrip('/')
            if '.' not in Path(relative).name:relative+='.html'
        file=(root/relative).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            self.send_error(404);return
        raw=file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type','application/javascript' if file.suffix=='.js' else mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(len(raw)));self.end_headers()
        try:self.wfile.write(raw)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass


def serve(handler,root=None):
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    server.root=root
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}'


def main():
    servers=[]
    try:
        server,base=serve(PlatformHandler);servers.append(server)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport=dict(width=1440,height=1000))
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('https://**/*',lambda route:route.abort())
            page.goto(base+'/mirror')
            page.wait_for_selector('[data-vue-page]')
            page.wait_for_selector('.mirror-facts[aria-busy=false]')
            page.evaluate('window.navigationSentinel=42;window.siteNav=document.querySelector(".site-nav")')
            page.locator('.site-desktop-links a[href="/docs"]').click()
            page.wait_for_function("location.pathname==='/docs' && !!document.querySelector('[data-vue-page]') && !document.documentElement.hasAttribute('data-page-loading')")
            assert page.evaluate('window.navigationSentinel')==42, 'Navigation reloaded the document'
            assert page.evaluate('document.querySelector(".site-nav")===window.siteNav'), 'Site navigation remounted'
            page.go_back()
            page.wait_for_function("location.pathname==='/mirror' && !!document.querySelector('.mirror-hero')")
            page.go_forward()
            page.wait_for_function("location.pathname==='/docs' && !!document.querySelector('[data-vue-page]')")
            page.evaluate('window.navigationSentinel=42;window.siteNav=document.querySelector(".site-nav")')
            page.locator('.site-desktop-links a[href="/admin"]').click()
            page.wait_for_function("location.pathname==='/admin' && !!document.querySelector('.admin-top-links')")
            assert page.evaluate('window.navigationSentinel')==42
            assert page.evaluate('document.querySelector(".site-nav")===window.siteNav'), 'Public-to-admin navigation replaced the site shell'
            page.locator('.admin-top-links a[href="/admin/mirror"]').click()
            page.wait_for_selector('#panel[data-mirror-view="overview"]:not(.hidden)')
            page.locator('.admin-sidebar a[href="/admin/mirror/quality"]').click()
            page.wait_for_selector('#panel:not(.hidden)')
            page.wait_for_selector('#quality-sources tr')
            assert page.locator('#quality-probe-all').is_visible()
            page.locator('#quality-probe-all').click()
            page.wait_for_function("document.querySelector('#message').textContent.includes('错峰')")
            page.goto(base+'/admin/mirror/settings/quality')
            page.wait_for_selector('#panel:not(.hidden)')
            assert page.locator('#settings-quality').is_visible()
            assert not page.locator('#settings-downloads').is_visible()
            assert page.locator('.settings-content-tabs a[aria-current="page"]').count()==1
            assert page.locator('.admin-sidebar a[href="/admin/mirror/tasks"]').count()==0
            assert page.locator('.site-nav a[href="/mirror"]').count()>=1
            page.evaluate('window.siteNav=document.querySelector(".site-nav");window.adminSidebar=document.querySelector(".admin-sidebar")')
            page.locator('.settings-content-tabs a[href="/admin/mirror/settings/storage"]').click()
            page.wait_for_function("location.pathname==='/admin/mirror/settings/storage' && document.querySelector('#settings-storage:not(.hidden)')")
            assert page.evaluate('document.querySelector(".site-nav")===window.siteNav && document.querySelector(".admin-sidebar")===window.adminSidebar'), 'Nested settings navigation replaced the menus'
            page.locator('.settings-content-tabs a[href="/admin/mirror/settings/quality"]').click()
            page.wait_for_function("location.pathname==='/admin/mirror/settings/quality' && document.querySelector('#settings-quality:not(.hidden)')")
            page.evaluate('scrollTo(0,0)')
            page.screenshot(path=str(WEB.parents[2]/'docs/source-quality-admin.png'),full_page=True)
            page.evaluate('window.navigationSentinel=42;window.siteNav=document.querySelector(".site-nav");window.adminSidebar=document.querySelector(".admin-sidebar")')
            page.locator('.admin-sidebar a[href="/admin/settings"]').click()
            page.wait_for_selector('#settings')
            page.wait_for_function("document.querySelector('#price-reference')?.textContent.includes('OpenAI')")
            assert page.locator('#mirror-settings').count()==0
            assert page.evaluate('window.navigationSentinel')==42
            assert page.evaluate('document.querySelector(".site-nav")===window.siteNav && document.querySelector(".admin-sidebar")===window.adminSidebar'), 'Admin shell remounted'
            page.locator('.admin-top-links a[href="/admin/mirror"]').click()
            page.wait_for_selector('#panel[data-mirror-view="overview"]:not(.hidden)')
            assert page.locator('.admin-sidebar a[href="/admin/mirror/resources"]').count()==1
            assert page.locator('.admin-sidebar a[href="/admin/settings"]').count()==0
            assert page.evaluate('document.querySelector(".site-nav")===window.siteNav && document.querySelector(".admin-sidebar")===window.adminSidebar'), 'Admin navigation changed identity'
            page.set_viewport_size(dict(width=390,height=844))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'Mobile page overflows'
            assert not errors, errors
            print('PASS platform: visible navigation feedback, retained document, back/forward, independent admin, pricing, mobile')
            audit=browser.new_page(viewport=dict(width=1440,height=1000))
            audit.route('https://**/*',lambda route:route.abort())
            layout_issues=[]
            for path,entry in PLATFORM_ROUTES:
                audit.goto(base+path)
                audit.wait_for_selector('[data-vue-page]')
                if path.startswith('/admin') and path!='/admin/setup':
                    audit.wait_for_selector('.admin-sidebar a')
                    assert audit.locator('.site-nav').count()==1, f'{path}: global navigation missing or duplicated'
                    assert audit.locator('.admin-top-links a[aria-current="location"]').count()==1, f'{path}: top-level category mismatch'
                    category=audit.locator('.admin-top-links a[aria-current="location"]').get_attribute('href')
                    children=audit.locator('.admin-sidebar a').evaluate_all('(nodes)=>nodes.map(node=>node.getAttribute("href"))')
                    assert children==ADMIN_CHILDREN[category], f'{path}: sidebar does not belong to {category}: {children}'
                    if path.startswith('/admin/mirror/settings'):
                        assert audit.locator('.settings-content-tabs a').count()==8, f'{path}: mirror subnavigation missing'
                        assert audit.locator('.settings-content-tabs a[aria-current="page"]').count()==1, f'{path}: mirror tab mismatch'
                elif path!='/admin/setup':
                    assert audit.locator('.site-nav').count()==1, f'{path}: global navigation missing or duplicated'
                for width in (1440,390):
                    audit.set_viewport_size(dict(width=width,height=900))
                    audit.wait_for_timeout(20)
                    dimensions=audit.evaluate('({body:document.body.scrollWidth,root:document.documentElement.scrollWidth,viewport:innerWidth})')
                    if max(dimensions['body'],dimensions['root'])>dimensions['viewport']+1:
                        layout_issues.append((path,width,dimensions))
            assert not layout_issues, f'Page layout overflows: {layout_issues}'
            audit.close()
            print(f'PASS platform layout: {len(PLATFORM_ROUTES)} routes at desktop and mobile widths')
            detail=browser.new_page()
            detail.goto(base+'/projects/modrinth/Fixture00')
            detail.wait_for_selector('#project-page h1')
            assert detail.locator('.site-nav').count()==1, 'Project detail has no global navigation'
            detail.evaluate('window.navigationSentinel=42;window.siteNav=document.querySelector(".site-nav")')
            detail.locator('.site-desktop-links a[href="/mirror"]').click()
            detail.wait_for_selector('.mirror-hero')
            assert detail.evaluate('window.navigationSentinel')==42
            assert detail.evaluate('document.querySelector(".site-nav")===window.siteNav'), 'Project navigation reloaded the page'
            detail.close()
            for name,root,start,target in [
                ('official/download',NODE_REPOSITORY/'site','/','/download'),
                ('node portal',NODE_REPOSITORY/'web/tunnel/web','/','/network'),
            ]:
                if not root.exists():continue
                server,origin=serve(StaticHandler,root.resolve());servers.append(server)
                page=browser.new_page()
                page.route('https://**/*',lambda route:route.abort())
                page.goto(origin+start)
                page.wait_for_selector('[data-vue-page]')
                page.evaluate("target=>{window.navigationSentinel=42;const a=document.createElement('a');a.href=target;a.id='smoke-link';a.textContent='Navigate';document.body.append(a);}",target)
                page.locator('#smoke-link').click()
                page.wait_for_function("path=>location.pathname===path && !!document.querySelector('[data-vue-page]') && !document.querySelector('#smoke-link')",arg=target)
                assert page.evaluate('window.navigationSentinel')==42
                page.go_back();page.wait_for_function("path=>location.pathname===path && !!document.querySelector('[data-vue-page]')",arg=start)
                print('PASS '+name+': Vue navigation and cached revisit')
                page.close()
            browser.close()
    finally:
        for server in servers:server.shutdown();server.server_close()


if __name__=='__main__':main()
