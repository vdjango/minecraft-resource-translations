"""Shared navigation catalogue for the Vue pages and accessible server HTML."""
import json
import re
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINKS = json.loads((ROOT/'frontend/navigation.json').read_text(encoding='utf-8'))


def navigation(name):
    path = '/admin' if name.startswith('admin') else '/' if name in ('index', 'project') else '/'+name
    anchors = ''.join('<a href="'+escape(link['href'])+'"'+(' class="nav-active" aria-current="page"' if path==link['href'] else '')+'>'+escape(link['label'])+'</a>' for link in LINKS)
    return ('<nav class="site-nav" aria-label="主导航"><a class="brand" href="/mirror"><span class="mark">B</span><span class="brand-type"><strong>BUGGCRAFT</strong><small>RESOURCE MIRROR &amp; TRANSLATIONS</small></span></a><div class="nav-links site-desktop-links">'+anchors+'</div><div class="site-nav-tools"><details class="site-mobile-menu"><summary aria-label="打开导航菜单">菜单 ☰</summary><div>'+anchors+'</div></details></div></nav>')


def sync_navigation():
    for file in (ROOT/'frontend/src/pages').glob('*.vue'):
        source = file.read_text(encoding='utf-8')
        if '<nav class="site-nav"' in source:
            source = re.sub(r'<nav class="site-nav".*?</nav>', '<SiteNav />', source, count=1, flags=re.S)
        if '<SiteNav' in source and "import SiteNav from '../SiteNav.vue';" not in source:
            source = source.replace('<script setup lang="ts">', '<script setup lang="ts">\nimport SiteNav from \'../SiteNav.vue\';', 1)
        file.write_text(source, encoding='utf-8')
    for file in (ROOT/'src/mc_translate/web').glob('*.html'):
        source = file.read_text(encoding='utf-8')
        source = re.sub(r'<nav\b.*?</nav>', lambda _: navigation(file.stem), source, count=1, flags=re.S)
        source = source.replace('<SiteNav />', navigation(file.stem))
        file.write_text(source, encoding='utf-8')


if __name__ == '__main__':
    sync_navigation()
