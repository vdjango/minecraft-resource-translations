"""Deterministic multilingual/no-translation UI checks; no upstream/model calls."""
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright
from browser_regression import Handler, SOURCE, RESOURCE, ORIGINAL, TRANSLATED
from mc_translate.languages import language_options


class MultilingualHandler(Handler):
    requested = []
    ensured = []

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path.endswith('/capabilities'):
            return self.send(dict(generation_available=True, default_target_language='zh-Hans', languages=language_options()))
        if '/catalog/' in url.path:
            language = parse_qs(url.query).get('target_language', ['zh-Hans'])[0]
            self.requested.append(language)
            if language == 'de':
                projects = [dict(resource=dict(RESOURCE, project_id=f'Bulk{i:03}', entity_id=f'Bulk{i:03}'), title=f'Bulk {i}', kind='mod',summary='Original', summary_source_hash='sha256:summary', translation_state='missing',field_states=dict(summary='missing', body='missing')) for i in range(50)]
                return self.send(dict(projects=projects,next_cursor=None,total_count=50))
            return self.send(dict(projects=[dict(resource=RESOURCE, title='Fixture', kind='mod', summary='Original', localized_summary={'zh-Hans':'中文简介', 'ja':'日本語の説明', 'en':'A fast mod.'}.get(language, language), translation_state='ready', field_states=dict(summary='ready', body='ready'), metadata=SOURCE['metadata'], update=dict(code='not_required' if language=='en' else 'up_to_date'))], next_cursor=None, total_count=1))
        if '/artifacts/' in url.path:
            language=url.path.rsplit('/',1)[1]
            text={'zh-Hans':TRANSLATED, 'ja':ORIGINAL.replace('English heading','日本語の紹介'), 'en':ORIGINAL}.get(language,ORIGINAL)
            return self.send(dict(text=text, format='html'))
        return super().do_GET()

    def do_POST(self):
        if self.path.endswith('translations:ensure'):
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            self.ensured.append(data)
            return self.send(dict(results=[]))
        if not self.path.endswith('translations:resolve'):
            return super().do_POST()
        data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        language=data['target_language']
        if language == 'de':
            return self.send(dict(results=[dict(resource=item['resource'],fields=[dict(field='summary',state='ready',matches_request=True,is_current=True,translation=dict(text='Deutsch summary',translation_required=True))]) for item in data['items']]))
        original=language=='en'
        fields=[]
        for field in data['items'][0]['fields']:
            translation=dict(quality='original' if original else 'machine', translation_required=not original, no_translation_reason='already_target_language' if original else None, translation_revision='tr_'+language)
            if field['field']=='summary':
                translation['text']={'zh-Hans':'中文简介','ja':'日本語の説明','en':'A fast mod.'}[language]
            else:
                translation['artifact']=dict(artifact_sha256=language)
            fields.append(dict(field=field['field'], requested_source_hash=field['source_hash'], state='ready', matches_request=True,is_current=True, translation=translation, update=dict(action='none',code='not_required' if original else 'up_to_date',notice='无需翻译，保留原文' if original else '')))
        return self.send(dict(results=[dict(resource=RESOURCE, fields=fields)]))


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),MultilingualHandler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='msedge',headless=True)
            page=browser.new_page(viewport=dict(width=1440,height=1000))
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('https://example.com/**',lambda route:route.abort())
            base=f'http://127.0.0.1:{server.server_port}'
            page.goto(base)
            page.wait_for_selector('.resource-title')
            assert page.locator('#target-language').input_value()=='zh-Hans'
            assert page.locator('#target-language option').count()==12
            for language, heading in [('zh-Hans','中文标题'),('ja','日本語の紹介'),('en','English heading')]:
                page.locator('#target-language').select_option(language)
                page.wait_for_function('lang=>document.querySelector(".translation-badge").textContent.includes(lang)',arg='无需翻译' if language=='en' else {'zh-Hans':'简体中文','ja':'日本語'}[language])
                page.locator('.resource-title').click()
                page.wait_for_function('rev=>document.getElementById("translated-body").dataset.revision===rev',arg='tr_'+language)
                assert page.locator('#translated-body h2').inner_text()==heading
                assert page.locator('#translated-body script').count()==0
                assert page.locator('#translated-body table td').count()==1
                if language=='en':
                    assert '无需翻译' in page.locator('#detail-status').inner_text()
                    assert page.locator('#report').is_hidden()
                    assert 'AI' not in page.locator('#body-language-label').inner_text()
                page.locator('#detail-dialog .close').click()
            page.reload()
            page.wait_for_selector('.resource-title')
            assert page.locator('#target-language').input_value()=='en'
            checks=0
            for width in (320,390,760,1024,1440,1920,2560):
                page.set_viewport_size(dict(width=width,height=1000))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
                assert page.locator('#target-language').is_visible()
                if width>=1024:
                    centers=page.locator('.toolbar').evaluate('(e)=>[...e.querySelectorAll("input,select,button")].map(x=>{const r=x.getBoundingClientRect();return r.y+r.height/2})')
                    assert max(centers)-min(centers)<3,centers
                checks+=1
            assert not errors,errors
            assert {'zh-Hans','ja','en'}<=set(MultilingualHandler.requested)
            page.locator('#page-size').select_option('50')
            page.locator('#target-language').select_option('de')
            page.wait_for_function('()=>document.querySelectorAll(".resource-summary").length===50&&[...document.querySelectorAll(".resource-summary")].every(e=>e.textContent==="Deutsch summary")')
            assert [len(batch['items']) for batch in MultilingualHandler.ensured]==[20,20,10]
            assert len({item['resource']['project_id'] for batch in MultilingualHandler.ensured for item in batch['items']})==50
            print(f'PASS: 12 language options, default/persistence, three languages, original-ready rendering, HTML safety, {checks} responsive widths, 50-row automatic generation in 20/20/10 batches; no paid calls.')
            browser.close()
    finally:
        server.shutdown()


if __name__=='__main__':
    main()
