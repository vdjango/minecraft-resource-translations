"""Shared routing must retain private fallbacks until quality is proven."""
import time
import unittest
from types import SimpleNamespace
from mc_translate.content_routing import ContentRouting,recent_health
from mc_translate.mirror_quality import QUALITY_DEFAULTS


class RoutingCase(unittest.TestCase):
    def sample(self,at,speed=64*1024,outcome='ok'):
        return dict(at=at,speed=speed,outcome=outcome)

    def test_slow_requires_spread_observations_and_recovery_hysteresis(self):
        rows=[self.sample(1000),self.sample(1300)]
        self.assertEqual(recent_health(rows,128,1300)['health'],'unknown')
        rows.append(self.sample(1600))
        self.assertEqual(recent_health(rows,128,1600)['health'],'slow')
        rows.append(self.sample(1900,300*1024))
        self.assertEqual(recent_health(rows,128,1900)['health'],'slow')
        rows.append(self.sample(2200,300*1024))
        self.assertEqual(recent_health(rows,128,2200)['health'],'healthy')
        self.assertEqual(recent_health([self.sample(t) for t in [1000,1001,1002]],128,1002)['health'],'unknown')
        self.assertEqual(recent_health(rows,128,6000)['health'],'unknown')

    def test_file_404_is_not_a_cdn_outage(self):
        self.assertEqual(recent_health([self.sample(1000,outcome='missing')],128,1000)['health'],'unknown')
        self.assertEqual(recent_health([self.sample(1000,outcome='error')],128,1000)['health'],'unavailable')

    def routing(self,metrics=None):
        quality=SimpleNamespace(settings=lambda:QUALITY_DEFAULTS,
            content_metric=lambda url:dict(health='unknown',recent_at=None,recent_speed_bps=None,**(metrics or {}).get(url,{})))
        return ContentRouting(SimpleNamespace(mirror=SimpleNamespace(quality=quality)))

    def item(self,host,speed=0,state='available'):
        return dict(url='https://'+host+'/file.jar',state=state,speed_kibps=speed,updated=time.time())

    def test_zero_is_unknown_and_fast_curseforge_precedes_modrinth(self):
        cf=self.item('edge.forgecdn.net');mr=self.item('cdn.modrinth.com',2048)
        routing=self.routing()
        self.assertFalse(routing.evidence(cf)['confirmed_fast'])
        self.assertEqual(routing.ranked([mr,cf])[0]['url'],cf['url'])
        self.assertEqual(routing.fast([cf,mr])[0]['url'],mr['url'])

    def test_current_cdn_health_overrides_old_positive_snapshot(self):
        item=self.item('edge.forgecdn.net',4096)
        quality=SimpleNamespace(settings=lambda:QUALITY_DEFAULTS,content_metric=lambda url:
            dict(health='slow',recent_at=time.time(),recent_speed_bps=64*1024))
        routing=ContentRouting(SimpleNamespace(mirror=SimpleNamespace(quality=quality)))
        self.assertEqual(routing.ranked([item]),[])
        self.assertFalse(routing.evidence(item)['confirmed_fast'])
