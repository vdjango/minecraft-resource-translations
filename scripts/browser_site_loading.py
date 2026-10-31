"""Focused offline browser smoke: loading feedback, navigation and mirror admin.

Uses the built assets from sibling sites when available. No paid/model calls,
production credentials or external sources are used.
"""
import json
import mimetypes
import os
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
from browser_regression import Handler as CatalogueHandler

REPOSITORY=Path(__file__).resolve().parents[1]
WORKSPACE=REPOSITORY.parent.parent if REPOSITORY.parent.name=='.worktrees' else REPOSITORY.parent
WEB=REPOSITORY/'src/mc_translate/web'
NODE_REPOSITORY=Path(os.environ.get('BUGGCRAFT_NODE_REPOSITORY',WORKSPACE/'minecraft-tunnel-native'))
SETTINGS=validate_settings({})
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
        files={'/mirror':'mirror.html','/developers':'developers.html',
               '/admin/settings':'admin-model.html','/admin/mirror':'admin-mirror.html'}
        if path in files:
            time.sleep(.25)
            return self.send((WEB/files[path]).read_bytes().replace(b'{{AUTH_MODE}}',b'login'), 'text/html; charset=utf-8')
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
            page.evaluate('window.navigationSentinel=42')
            page.locator('.site-desktop-links a[href="/docs"]').click()
            page.wait_for_selector('#site-navigation-overlay:not([hidden])')
            page.wait_for_function("location.pathname==='/docs' && !!document.querySelector('[data-vue-page]') && !document.documentElement.hasAttribute('data-page-loading')")
            assert page.evaluate('window.navigationSentinel')==42, 'Navigation reloaded the document'
            page.go_back()
            page.wait_for_function("location.pathname==='/mirror' && !!document.querySelector('.mirror-hero')")
            page.go_forward()
            page.wait_for_function("location.pathname==='/docs' && !!document.querySelector('[data-vue-page]')")
            page.goto(base+'/admin/mirror#quality')
            page.wait_for_selector('#panel:not(.hidden)')
            page.wait_for_selector('#quality-sources tr')
            assert page.locator('#settings-quality').is_visible()
            assert not page.locator('#settings-downloads').is_visible()
            page.locator('#quality-probe-all').click()
            page.wait_for_function("document.querySelector('#message').textContent.includes('错峰')")
            page.evaluate('scrollTo(0,0)')
            page.screenshot(path=str(WEB.parents[2]/'docs/source-quality-admin.png'),full_page=True)
            page.evaluate('window.navigationSentinel=42')
            page.locator('.admin-sidebar a[href="/admin/settings"]').click()
            page.wait_for_selector('#settings')
            page.wait_for_function("document.querySelector('#price-reference')?.textContent.includes('OpenAI')")
            assert page.locator('#mirror-settings').count()==0
            assert page.evaluate('window.navigationSentinel')==42
            page.locator('.admin-sidebar a[href="/admin/mirror"]').click()
            page.wait_for_selector('#mirror-settings')
            page.set_viewport_size(dict(width=390,height=844))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), 'Mobile page overflows'
            assert not errors, errors
            print('PASS platform: visible navigation feedback, retained document, back/forward, independent admin, pricing, mobile')
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
