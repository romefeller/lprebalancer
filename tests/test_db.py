"""The ledger, on an isolated database: a whole position lifecycle, and the
invariant the stats promise — TOTAL fees only ever go up."""
import time
import unittest
from decimal import Decimal

import _fixtures
from _fixtures import ADAPTIVE_POOL, BTC_QUOTE_POOL, LIVE_POOL
import db


class Config(unittest.TestCase):
    def setUp(self):
        _fixtures.ensure_profile()

    def test_exactly_one_active_profile(self):
        _fixtures.ensure_profile('other', pool='x' * 44)
        with db.cursor() as cur:
            cur.execute('select count(*) n from config where active')
            self.assertEqual(cur.fetchone()['n'], 1)
        db.activate('sol-usdc')
        self.assertEqual(db.load_config()['name'], 'sol-usdc')
        with self.assertRaises(SystemExit):
            db.activate('nope')

    def test_set_param_validates_the_column(self):
        row = db.set_param('sol-usdc', 'capital_usd', '200')
        self.assertEqual(row['capital_usd'], Decimal('200'))
        row = db.set_param('sol-usdc', 'bands', '1.04, 1.10')
        self.assertEqual([float(b) for b in row['bands']], [1.04, 1.10])
        with self.assertRaises(SystemExit):
            db.set_param('sol-usdc', 'no_such_column', 1)
        with self.assertRaises(SystemExit):
            db.set_param('sol-usdc', 'name', 'x')
        db.set_param('sol-usdc', 'capital_usd', '190')
        db.set_param('sol-usdc', 'bands', '1.03,1.05,1.08,1.12,1.18,1.25,1.40')

    def test_sanity_constraint_refuses_nonsense(self):
        import psycopg2
        for k, v in (('poll_seconds', 5), ('max_usd', 10), ('side_cap_fraction', 0.2),
                     ('swap_cost_bps', 900), ('gas_reserve_sol', -1)):
            with self.assertRaises(psycopg2.errors.CheckViolation, msg=k):
                db.set_param('sol-usdc', k, v)

    def test_add_reads_the_pair_from_orca_and_refuses_adaptive_fee(self):
        with self.assertRaises(SystemExit) as cm:
            db.add('zec', ADAPTIVE_POOL)
        self.assertIn('adaptive-fee', str(cm.exception))
        with self.assertRaises(SystemExit):
            db.add('bogus', 'notapool')
        row = db.add('sol-cbbtc', BTC_QUOTE_POOL, capital_usd=100)
        self.assertEqual(row['pair_label'], 'SOL/cbBTC')
        self.assertEqual((row['token_a'], row['token_b']), ('SOL', 'cbBTC'))
        self.assertEqual(row['max_usd'], Decimal('140'))
        self.assertFalse(row['active'])
        self.assertGreater(row['_pool']['tvl_usd'], 0)
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name in ('sol-cbbtc', 'other')")


class Ledger(unittest.TestCase):
    def setUp(self):
        _fixtures.ensure_profile()
        _fixtures.reset_ledger()

    def snap(self, mint, accrued_a, accrued_b, accrued_usd, price=100.0, wallet=80.0, pos=160.0):
        db.snapshot(mint, price, True, '1', accrued_a, accrued_b, accrued_usd, wallet, pos)

    def test_empty_ledger_reports_zeros_not_errors(self):
        s = db.stats()
        self.assertEqual(s['fees_total_usd'], 0.0)
        self.assertIsNone(s['fees_per_day_usd']); self.assertIsNone(s['apr_pct'])
        self.assertIsNone(s['equity_usd']); self.assertEqual(s['positions_opened'], 0)

    def test_rate_is_suppressed_for_the_first_hour(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 'sig', 190, 'test')
        self.snap('m1', 0.001, 0.1, 0.2)
        self.assertIsNone(db.stats()['fees_per_day_usd'])

    def test_lifecycle_total_is_monotonic_across_a_rebalance(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 'sig1', 190, 'test')
        self.snap('m1', 0.0, 0.0, 0.0)
        self.snap('m1', 0.001, 0.10, 0.20)
        s1 = db.stats()
        self.assertAlmostEqual(s1['fees_unrealised_usd'], 0.20)
        self.assertAlmostEqual(s1['fees_total_usd'], 0.20)
        self.assertAlmostEqual(s1['fees_today_usd'], 0.20)
        self.assertEqual(s1['fees_realised_usd'], 0.0)

        # harvest and close: the accrual becomes realised, the position's own
        # counter resets, and the total must not move.
        db.record_harvest('m1', 0.001, 0.10, 0.20, 'hsig')
        db.close_position('m1', 'csig', None)
        # Between the close and the new position's first snapshot, the latest
        # snapshot still belongs to the dead position. Its accrual is now in
        # the wallet and counted as realised; it must not count twice. This is
        # the exact state in which a $0.26 harvest was reported as $0.51.
        mid = db.stats()
        self.assertAlmostEqual(mid['fees_unrealised_usd'], 0.0)
        self.assertAlmostEqual(mid['fees_total_usd'], 0.20)
        self.assertAlmostEqual(mid['fees_today_usd'], 0.20)
        db.open_position('m2', LIVE_POOL, 'SOL/USDC', 96, 106, 5, 'sig2', 190, 'rebalance')
        self.snap('m2', 0.0, 0.0, 0.0)
        s2 = db.stats()
        self.assertAlmostEqual(s2['fees_realised_usd'], 0.20)
        self.assertAlmostEqual(s2['fees_unrealised_usd'], 0.0)
        self.assertAlmostEqual(s2['fees_total_usd'], 0.20)
        self.assertGreaterEqual(s2['fees_total_usd'], s1['fees_total_usd'])
        self.assertEqual(s2['positions_opened'], 2); self.assertEqual(s2['positions_open_now'], 1)
        self.assertEqual(s2['harvests'], 1)

        # the new position earns: total climbs from where it was
        self.snap('m2', 0.0005, 0.05, 0.10)
        s3 = db.stats()
        self.assertAlmostEqual(s3['fees_total_usd'], 0.30)
        self.assertAlmostEqual(s3['fees_total_a'], 0.0015)
        self.assertAlmostEqual(s3['fees_total_b'], 0.15)
        # today = harvested today + accrual since the first snapshot of today.
        # A day-0 snapshot of m1 at 0 makes the accrual delta of m2 read 0.10.
        self.assertAlmostEqual(s3['fees_today_usd'], 0.30)

    def test_token_amounts_survive_exactly(self):
        db.record_harvest('m', Decimal('0.000265000000000001'), Decimal('0.123456'), 0.05, 's')
        with db.cursor() as cur:
            cur.execute('select fee_a, fee_b from harvests')
            r = cur.fetchone()
        self.assertEqual(r['fee_a'], Decimal('0.000265000000000001'))
        self.assertEqual(r['fee_b'], Decimal('0.123456'))

    def test_equity_and_pnl(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 's', 190, 't')
        self.snap('m1', 0, 0, 0, wallet=80.0, pos=160.0)
        self.snap('m1', 0, 0, 0.1, wallet=80.5, pos=161.0)
        s = db.stats()
        self.assertAlmostEqual(s['equity_start_usd'], 240.0)
        self.assertAlmostEqual(s['equity_usd'], 241.5)
        self.assertAlmostEqual(s['pnl_usd'], 1.5)

    def test_events_count_failures_and_rebands(self):
        db.event('REBAND', '5% -> 12%'); db.event('open_failed', 'x'); db.event('BREAKER', 'y')
        s = db.stats()
        self.assertEqual(s['rebands'], 1); self.assertEqual(s['failures'], 1)

    def test_daily_and_history(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 's', 190, 't')
        # the first snapshot lands after some accrual; that accrual still counts
        self.snap('m1', 0.0002, 0.02, 0.05); self.snap('m1', 0.001, 0.1, 0.25)
        db.record_harvest('m1', 0.001, 0.1, 0.25, 'h')
        d = db.daily()
        self.assertEqual(len(d), 1)
        # earned today: 0.25 accrued, then harvested. Once, not twice.
        self.assertAlmostEqual(float(d[0]['fee_usd']), 0.25)
        self.assertAlmostEqual(float(d[0]['fee_a']), 0.001)
        # a second position opened the same day adds its own accrual
        db.close_position('m1', 'c', None)
        db.open_position('m2', LIVE_POOL, 'SOL/USDC', 96, 106, 5, 's2', 190, 'r')
        self.snap('m2', 0.0001, 0.01, 0.03)
        self.assertAlmostEqual(float(db.daily()[0]['fee_usd']), 0.28)
        self.assertEqual(len(db.history()), 3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
