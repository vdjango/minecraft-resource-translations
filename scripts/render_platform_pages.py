"""Render accessible HTML shells from the unified Vue page sources.

Run before the frontend build. Dynamic catalogues and authentication are filled
by PublicPages; live downloads and node status are progressively enhanced by Vue.
"""
import re
from pathlib import Path
from site_navigation import navigation, sync_navigation

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / 'frontend/src/pages'
WEB = ROOT / 'src/mc_translate/web'


def template(path):
    return path.read_text(encoding='utf-8').split('<template>', 1)[1].rsplit('</template>', 1)[0]


def render(only=None):
    for name in ('index', 'mirror', 'docs', 'developers', *(p.stem for p in sorted(PAGES.glob('admin*.vue')))):
        if only is not None and name not in only:
            continue
        html_path = WEB / (name + '.html')
        original = html_path.read_text(encoding='utf-8')
        body = template(PAGES / (name + '.vue'))
        for component in ('ResourceDownloads', 'ServiceNetwork'):
            body = body.replace('<' + component + ' />', template(ROOT / 'frontend/src' / (component + '.vue')))
        body = body.replace('<RoomClientCacheSettings v-show="selected===\'room-cache\'" />',
                            template(ROOT / 'frontend/src/RoomClientCacheSettings.vue'))
        body = body.replace('<SiteNav />', navigation(name))
        # Use an unchanged page as the shared non-JavaScript login fallback.
        access_source = (WEB / 'admin-mail.html').read_text(encoding='utf-8')
        access = re.search(r'<section id="admin-access".*?</form></section></section>', access_source, re.S)
        if access and name.startswith('admin'):
            fallback = access[0].replace('<h2>登录管理工作台</h2>', '<h2>{{AUTH_TITLE}}</h2>')
            body = re.sub(r'<section id="admin-access".*?</section>', lambda _: fallback, body, flags=re.S)
        body = body.replace(':data-mode="seed.authMode"', 'data-mode="{{AUTH_MODE}}"')
        body = body.replace(' v-html="seed.catalogue"', '')
        body = re.sub(r'<tr v-for=.*?</tr>', '', body, flags=re.S)
        body = re.sub(r'<a v-for="node in nodes".*?</a>', '', body, flags=re.S)
        body = re.sub(r'<button v-for="tab in settingsTabs".*?</button>',
                      ''.join(f'<a href="#{key}">{label}</a>' for key, label in
                              ([('downloads','下载策略'),('storage','存储与同步'),('distribution','节点与流量'),('sources','公开来源'),('quality','来源质量'),('room-cache','临时加速'),('cloudflare','域名与缓存')] if name=='admin-mirror' else [('translation','翻译服务')])), body, flags=re.S)
        body = re.sub(r' v-show="[^"]*"', '', body)
        body = re.sub(r' @click="[^"]*"', '', body)
        body = re.sub(r' v-if="[^"]*"', '', body)
        body = re.sub(r'{{\s*seed.serviceState.*?}}', '正在连接翻译服务', body)
        body = re.sub(r'{{(?!AUTH_MODE|AUTH_TITLE).*?}}', '—', body)
        # Vue's v-pre is harmless in the server shell and protects code examples.
        head = original.split('<body', 1)[0]
        if name in ('admin-mirror', 'admin-model'):
            title = '镜像源管理' if name=='admin-mirror' else '翻译设置'
            head = re.sub(r'<title>.*?</title>', '<title>'+title+' · Buggcraft</title>', head)
        for css in ('mirror.css', 'platform.css', *(('network.css',) if name == 'developers' else ())):
            if '/assets/' + css not in head and '"assets/' + css not in head:
                head = head.replace('</head>', f'<link rel="stylesheet" href="/assets/{css}"></head>')
        html_path.write_text(head + '<body>' + body + '</body></html>', encoding='utf-8')
    if only is None:
        sync_navigation()


if __name__ == '__main__':
    render()
