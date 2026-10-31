import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from mc_translate.pricing import calculate, rate
from mc_translate.store import Store


class PricingCase(unittest.TestCase):
    def test_cache_writes_are_not_counted_twice(self):
        usage = dict(input_tokens=12000, output_tokens=500, total_tokens=12500,
            input_tokens_details=dict(cached_tokens=4000, cache_write_tokens=7000,
                cache_write_tokens_details=dict(ttl_30m_tokens=3000, ttl_12h_tokens=4000)))
        bill = calculate(usage, 'gpt-6.1-sol')
        self.assertAlmostEqual(bill['cost'], (1000*2+4000*.1+3000*2.5+4000*5+500*10)/1e6)
        self.assertEqual(bill['basis'], 'official')
        self.assertEqual(bill['written'], 7000)

    def test_gateway_missing_cache_details_has_reference_cost(self):
        bill = calculate(dict(prompt_tokens=1000, completion_tokens=100), 'gpt-6.1-sol')
        self.assertEqual(bill['total'], 1100)
        self.assertEqual(bill['cost'], .003)
        self.assertEqual(bill['basis'], 'official_estimate')
        self.assertIsNone(calculate(dict(total_tokens=1100), 'gpt-6.1-sol')['cost'])

    def test_long_context_alias_and_custom_override(self):
        usage = dict(prompt_tokens=300000, completion_tokens=1000, total_tokens=301000,
                     prompt_tokens_details=dict(cached_tokens=100000))
        bill = calculate(usage, 'gpt-6.1-sol-2026-10-01')
        self.assertAlmostEqual(bill['cost'], ((200000*2+100000*.1)*2+1000*10*1.5)/1e6)
        self.assertTrue(bill['snapshot']['long_context'])
        bill = calculate(usage, 'gpt-6.1-sol', {'gpt-6.1-sol':dict(input=1, cached_input=.5, output=2)})
        self.assertEqual(bill['basis'], 'custom')
        self.assertAlmostEqual(bill['cost'], .252)
        self.assertIsNone(rate('supplier/gpt-6.1-sol'))
        self.assertIsNone(calculate({**usage, 'prompt_tokens_details':dict(cached_tokens=300001)}, 'gpt-6.1-sol')['cost'])


class BillingStoreCase(unittest.IsolatedAsyncioTestCase):
    async def test_settlement_and_historical_backfill_preserve_snapshots(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder)/'usage.sqlite')
            store = await Store(path).open()
            try:
                await store.db.execute("INSERT INTO usage(id,day,month,created,tokens,actual,model) VALUES(1,'2026-11-01','2026-11',0,2000,0,'gpt-6.1-sol')")
                config = SimpleNamespace(model='gpt-6.1-sol', model_prices={})
                await store.settle(1, dict(prompt_tokens=1000, completion_tokens=100), config)
                row = await store.one('SELECT * FROM usage WHERE id=1')
                self.assertEqual(row['cost'], .003)
                self.assertEqual(row['cost_basis'], 'official_estimate')
                snapshot = row['price_snapshot']
                await store.db.execute("INSERT INTO usage(id,day,month,created,tokens,actual,model,input_tokens,cached_tokens,output_tokens,details_known) VALUES(2,'2026-11-01','2026-11',0,1100,1,'gpt-6.1-sol',1000,0,100,1)")
            finally:
                await store.close()
            store = await Store(path).open()
            try:
                self.assertEqual((await store.one('SELECT * FROM usage WHERE id=1'))['price_snapshot'], snapshot)
                self.assertEqual((await store.one('SELECT * FROM usage WHERE id=2'))['cost_basis'], 'official_backfill')
            finally:
                await store.close()
