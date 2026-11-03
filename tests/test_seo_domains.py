from pathlib import Path
from unittest.mock import patch
from aiohttp.test_utils import make_mocked_request
from test_platform import PlatformFixture
from mc_translate.public_pages import PublicPages


class SeoDomainsCase(PlatformFixture):
    async def test_domain_separation_prerender_and_canonical(self):
        self.config.public_url='https://translate.bugg.icu'
        with patch.dict('os.environ',{'TRANSLATION_MIRROR_PUBLIC_URL':'https://mirror.bugg.icu'}):
            pages=PublicPages(self.app['api'],Path(__file__).parents[1]/'src/mc_translate/web')
        mirror=make_mocked_request('GET','/',headers={'Host':'mirror.bugg.icu'})
        translation=make_mocked_request('GET','/',headers={'Host':'translate.bugg.icu'})
        first=(await pages.page(mirror,'index')).text
        second=(await pages.page(translation,'index')).text
        self.assertIn('vue-page-mirror.js',first)
        self.assertIn('rel="canonical" href="https://mirror.bugg.icu/"',first)
        self.assertIn('<h1>Minecraft',first)
        self.assertIn('User-Agent:',first)
        self.assertIn('vue-page-index.js',second)
        self.assertIn('rel="canonical" href="https://translate.bugg.icu/"',second)
        self.assertNotIn('vue-page-index.js',first)
        sitemap=(await pages.sitemap_index(mirror)).text
        self.assertIn('https://mirror.bugg.icu/docs',sitemap)
        self.assertNotIn('projects-',sitemap)
        self.assertNotIn('/admin',sitemap)
        self.assertIn('Sitemap: https://mirror.bugg.icu/',(await pages.robots(mirror)).text)
