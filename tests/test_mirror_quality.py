import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from mc_translate.mirror_quality import QUALITY_DEFAULTS, SourceQuality, assessment


class AssessmentCase(unittest.TestCase):
    def samples(self, speed=4*1024**2, outcome='ok'):
        now = time.time()
        return [dict(at=now-i*21600, outcome=outcome, speed=speed, bytes=262144) for i in range(12)]

    def test_long_term_grades_and_failures(self):
        self.assertEqual(assessment(self.samples()[:2], QUALITY_DEFAULTS)['grade'], 'observing')
        self.assertEqual(assessment(self.samples(), QUALITY_DEFAULTS)['grade'], 'excellent')
        slow = assessment(self.samples(700*1024), QUALITY_DEFAULTS)
        self.assertEqual(slow['grade'], 'good')
        self.assertTrue(slow['cache_priority'])
        self.assertEqual(assessment(self.samples(outcome='error'), QUALITY_DEFAULTS)['grade'], 'poor')
        self.assertEqual(assessment(self.samples(outcome='missing'), QUALITY_DEFAULTS)['samples'], 0)

    def test_unknown_alternative_prevents_duplicate_caching(self):
        quality = SourceQuality(SimpleNamespace(sources=SimpleNamespace(owners={('/file','slow'):'slow',('/file','fast'):'fast'})))
        quality.settings = lambda: {**QUALITY_DEFAULTS}
        quality.metric = lambda source: dict(cache_priority=source=='slow', grade='poor' if source=='slow' else 'excellent')
        self.assertTrue(quality.needs_cache('/file',['slow']))
        self.assertFalse(quality.needs_cache('/file',['slow','fast']))
        self.assertEqual(quality.ranked('/file',['slow','fast']), ['fast','slow'])


class SamplingCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await aiosqlite.connect(':memory:', isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        async def one(sql, params=()):
            async with self.db.execute(sql,params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
        self.quality = SourceQuality(SimpleNamespace(db=self.db, one=one))
        self.settings = {**QUALITY_DEFAULTS, 'enabled': True, 'public_url':'https://mirror.example',
                         'source_registries':[dict(id='test', url='https://source.example',kind='maven',enabled=True)]}
        self.quality.settings = lambda: self.settings
        with patch.object(self.quality, 'run', new=self.never):
            await self.quality.open()
        await self.quality.http.close()
        self.quality.http = ClientSession()
        self.ranges = []
        async def file(request):
            self.ranges.append(request.headers.get('Range'))
            return web.Response(body=b'x'*300000)
        async def dispatch(request):
            return web.Response(status=302,headers={'Location':'/file'})
        app = web.Application()
        app.router.add_get('/file',file)
        app.router.add_get('/dispatch',dispatch)
        self.server = TestServer(app)
        await self.server.start_server()
        self.allowed_targets = patch('mc_translate.mirror_quality.public_sample_url', side_effect=lambda value:value)
        self.allowed_targets.start()

    async def never(self):
        import asyncio
        await asyncio.Event().wait()

    async def asyncTearDown(self):
        self.allowed_targets.stop()
        await self.quality.close()
        await self.server.close()
        await self.db.close()

    async def test_bounded_sample_and_missing_is_not_outage(self):
        self.assertTrue(await self.quality.probe('test',str(self.server.make_url('/file')),None))
        self.assertEqual(self.ranges, ['bytes=0-262143'])
        sample = (await self.quality.history('test'))[0]
        self.assertEqual((sample['bytes'], sample['outcome']), (262144,'ok'))
        await self.quality.probe('test',str(self.server.make_url('/missing')),None)
        self.assertEqual(self.quality.metric('test')['missing'],1)
        self.assertEqual(self.quality.metric('test')['samples'],1)
        self.settings['quality_daily_mib'] = 0
        self.assertFalse(await self.quality.probe('test',str(self.server.make_url('/file')),None))
        self.assertEqual(len(self.ranges),1)

    async def test_manual_queue_stagger_and_changed_address_reset(self):
        await self.quality.schedule('test')
        self.assertGreater(self.quality.next_check['test'],time.time())
        # Even a manually queued item cannot bypass the global interval.
        self.quality.next_global = time.time()+120
        self.quality.next_check['test'] = 0
        await self.quality.tick()
        self.assertEqual(len(self.ranges),0)
        await self.quality.probe('test',str(self.server.make_url('/file')),None)
        self.settings['source_registries'][0]['url']='https://replacement.example'
        self.assertEqual(self.quality.metric('test')['samples'],0)
        await self.quality.sync_sources()
        self.assertEqual(await self.quality.history('test'),[])

    async def test_snapshot_exposes_file_coverage_for_game_mirrors(self):
        await self.db.execute('CREATE TABLE source_files(source TEXT,expires REAL)')
        await self.db.executemany('INSERT INTO source_files(source,expires) VALUES(?,?)',
                                  [('bmclapi',time.time()+3600), ('bmclapi',time.time()+3600),
                                   ('bmclapi',time.time()-1), ('test',time.time()+3600)])
        self.settings['source_registries'].append(
            dict(id='bmclapi', url='https://bmclapi2.bangbang93.com', kind='minecraft', enabled=True))
        snapshot = await self.quality.snapshot()
        self.assertEqual(snapshot['sources']['bmclapi']['mapped_files'], 2)
        self.assertEqual(snapshot['sources']['test']['mapped_files'], 1)

    async def test_game_mirror_dispatch_samples_the_download_node(self):
        await self.quality.probe('test',str(self.server.make_url('/dispatch')),300000)
        sample = (await self.quality.history('test'))[0]
        self.assertEqual((sample['status'],sample['outcome'],sample['bytes']), (200,'ok',262144))
        self.assertEqual(self.ranges,['bytes=0-262143'])

    async def test_redirect_cannot_request_a_private_target(self):
        self.allowed_targets.stop()
        from mc_translate.mirror_quality import public_sample_url
        with self.assertRaisesRegex(ValueError,'Private sample'):
            public_sample_url('http://127.0.0.1/private')
        with self.assertRaisesRegex(ValueError,'Private sample'):
            public_sample_url('https://[::1]/private')
        self.allowed_targets.start()
