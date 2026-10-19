"""The payout governor (sql/053): the loss of a band, the share it allows,
the fee split it drives, and the measurement from the ledger. The equity
mode (sql/054): the two states, the target, the book read, the switch."""
import datetime as dt
import math
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import config  # noqa: E402
import db  # noqa: E402
import fees  # noqa: E402
from lp import governor  # noqa: E402

SOL = fees.NATIVE_MINT
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'


class Loss(unittest.TestCase):
    def test_no_move_no_loss(self):
        self.assertAlmostEqual(governor.band_loss(99, 101, 100, 100, [(100, 10 ** 9)], 1.0), 0.0, places=9)

    def test_any_move_inside_loses_the_exact_lvr(self):
        # hold minus LP for an in-range step is L (sqrt(P1) - sqrt(P0))^2 / sqrt(P0)
        for p1 in (99.2, 99.9, 100.4, 100.9):
            got = governor.band_loss(99, 101, 100, p1, [(100, 1000)], 1.0)
            self.assertAlmostEqual(got, -1000 * (math.sqrt(p1) - 10) ** 2 / 10, places=9)

    def test_a_move_out_of_the_band_loses_and_the_scale_applies(self):
        a = governor.band_loss(99, 101, 100, 98, [(100, 1000)], 1.0)
        b = governor.band_loss(99, 101, 100, 98, [(100, 1000)], 10 ** -7.5)
        self.assertLess(a, 0)
        self.assertAlmostEqual(b, a * 10 ** -7.5, places=15)

    def test_an_increase_is_held_from_its_own_price(self):
        one = governor.band_loss(99, 101, 100, 100.5, [(100, 1000)], 1.0)
        two = governor.band_loss(99, 101, 100, 100.5, [(100, 1000), (100.5, 2000)], 1.0)
        self.assertAlmostEqual(two, one * 1.0 + 0.0, places=9)  # the add at the close price loses nothing

    def test_a_partial_removal_scales_the_hold(self):
        full = governor.band_loss(99, 101, 100, 100.5, [(100, 1000)], 1.0)
        half = governor.band_loss(99, 101, 100, 100.5, [(100, 1000), (100.2, 500)], 1.0)
        self.assertAlmostEqual(half, full / 2, places=9)

    def test_no_liquidity_is_none(self):
        self.assertIsNone(governor.band_loss(99, 101, 100, 100, [(100, 0)], 1.0))
        self.assertIsNone(governor.band_loss(99, 101, 100, 100, [], 1.0))

    def test_value_scale_sol_usdc(self):
        self.assertAlmostEqual(governor.value_scale(9, 6), 10 ** -7.5)


class Share(unittest.TestCase):
    def test_the_audited_weeks(self):
        # W40: fees 25.9, loss -14.8; W42: fees 115.3, loss -115.0
        self.assertAlmostEqual(governor.payout_share(25.9, -14.8, 0.2, 0.5), 0.8 * 11.1 / 25.9, places=9)
        self.assertAlmostEqual(governor.payout_share(115.3, -115.0, 0.2, 0.5), 0.8 * 0.3 / 115.3, places=9)

    def test_ratio_is_fees_over_loss(self):
        self.assertEqual(governor.ratio(43.91, -46.78), round(43.91 / 46.78, 3))
        self.assertEqual(governor.ratio(30.0, -10.0), 3.0)
        self.assertIsNone(governor.ratio(0, -5)); self.assertIsNone(governor.ratio(10, 0)); self.assertIsNone(governor.ratio(10, 2))
        self.assertIsNone(governor.ratio(10, None))
        self.assertAlmostEqual(governor.payout_share(30, -10, 0.5, 1.0), (1 - 1 / 3) * 0.5)   # no cap: follows R

    def test_a_losing_window_pays_nothing_and_the_cap_holds(self):
        self.assertEqual(governor.payout_share(10, -20, 0.2, 0.5), 0.0)
        self.assertEqual(governor.payout_share(0, 0, 0.2, 0.5), 0.0)
        self.assertEqual(governor.payout_share(10, 0, 0.0, 0.5), 0.5)
        self.assertEqual(governor.payout_share(10, 5, 0.0, 1.0), 1.0)    # a positive "loss" counts as none


class Split(unittest.TestCase):
    F = [(SOL, 'SOL', 0.02, 100.0), (USDC, 'USDC', 3.0, 1.0)]        # $5 of fees

    def by_kind(self, parts):
        out = {}
        for p in parts:
            out[(p['symbol'], p['kind'])] = out.get((p['symbol'], p['kind']), 0) + p['amount']
        return out

    def test_share_pays_that_share_of_the_value_from_usdc(self):
        k = self.by_kind(fees.split(self.F, USDC, 1.0, 0.05, share=0.2))
        self.assertAlmostEqual(k[('USDC', 'paid')], 1.0)                 # 20% of $5
        self.assertAlmostEqual(k[('USDC', 'reinvested')], 2.0)
        self.assertAlmostEqual(k[('SOL', 'reinvested')], 0.02)

    def test_share_never_pays_more_usdc_than_was_earned(self):
        k = self.by_kind(fees.split([(SOL, 'SOL', 0.05, 100.0), (USDC, 'USDC', 0.5, 1.0)], USDC, 1.0, 0.05, share=0.5))
        self.assertAlmostEqual(k[('USDC', 'paid')], 0.5)
        self.assertNotIn(('USDC', 'reinvested'), k)

    def test_zero_share_reinvests_everything_and_none_keeps_the_old_rule(self):
        k = self.by_kind(fees.split(self.F, USDC, 1.0, 0.05, share=0.0))
        self.assertNotIn(('USDC', 'paid'), k)
        self.assertEqual(self.by_kind(fees.split(self.F, USDC, 1.0, 0.05))[('USDC', 'paid')], 3.0)

    def test_gas_low_still_reinvests_and_total_is_conserved(self):
        parts = fees.split(self.F, USDC, 0.01, 0.05, share=0.3)
        self.assertNotIn('paid', {p['kind'] for p in parts})
        for share in (0.0, 0.1, 0.33, 0.5, 1.0):
            parts = fees.split(self.F, USDC, 1.0, 0.05, share=share)
            self.assertAlmostEqual(sum(p['usd'] for p in parts), 5.0)

    def test_a_bad_share_raises(self):
        with self.assertRaises(ValueError):
            fees.split(self.F, USDC, 1.0, 0.05, share=1.5)


class Measure(unittest.TestCase):
    """measure() on the test ledger: one band that rode inside and earned."""

    def setUp(self):
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute("delete from fee_growth where pool = %s", (POOL,))
            cur.execute("""insert into fee_growth (ts, dex, pool, sqrt_price, g0, g1, rewards, dec_a, dec_b, mint_a, mint_b)
                           values (now(), 'raydium-clmm', %s, 0, 0, 0, '[]', 9, 6, %s, %s)""", (POOL, SOL, USDC))
            cur.execute("""insert into positions (mint, config_name, pool, dex, opened_at, closed_at, lower_price, upper_price,
                                                  deposit_usd, withdraw_usd)
                           values ('M1', 'sol-usdc', %s, 'raydium-clmm', now() - interval '3 hours', now() - interval '1 hour',
                                   99, 101, 100, 99.9),
                                  ('M2', 'sol-usdc', 'X', 'meteora-dlmm', now() - interval '3 hours', now() - interval '2 hours',
                                   99, 101, 100, 100),
                                  ('M3', 'sol-usdc', %s, 'raydium-clmm', now() - interval '20 days', now() - interval '19 days',
                                   99, 101, 100, 100)""", (POOL, POOL))
            cur.execute("insert into band_profile (mint, last_event, fees_usd) values ('M1', 'rebalance', 0.5), "
                        "('M2', 'rebalance', 9), ('M3', 'rebalance', 9)")
            L = 10 ** 9
            cur.execute("""insert into snapshots (ts, mint, price, liquidity) values
                           (now() - interval '3 hours', 'M1', 100, %s), (now() - interval '1 hour', 'M1', 100.5, %s)""", (L, L))
            self.L = L

    def tearDown(self):
        _fixtures.reset_ledger()

    def test_only_this_window_and_measurable_bands(self):
        m = governor.measure('sol-usdc', 7)
        self.assertEqual((m['bands'], m['left_out']), (1, 1))       # M2 is DLMM; M3 is 19 days old
        self.assertAlmostEqual(m['fees'], 0.5)
        want = governor.band_loss(99, 101, 100, 100.5, [(100, self.L), (100.5, self.L)], 10 ** -7.5)
        self.assertAlmostEqual(m['loss'], want, places=12)
        self.assertLess(m['loss'], 0)


class EquityShare(unittest.TestCase):
    ARGS = (5.0, 2.0, 0.05, 0.3)

    def test_floor_until_the_enter_level_then_high(self):
        self.assertEqual(governor.equity_share(-6.5, False, *self.ARGS), (0.05, False))
        self.assertEqual(governor.equity_share(4.99, False, *self.ARGS), (0.05, False))
        self.assertEqual(governor.equity_share(5.0, False, *self.ARGS), (0.3, True))

    def test_high_until_the_exit_level_then_floor(self):
        self.assertEqual(governor.equity_share(3.0, True, *self.ARGS), (0.3, True))      # inside the gap: stays
        self.assertEqual(governor.equity_share(2.01, True, *self.ARGS), (0.3, True))
        self.assertEqual(governor.equity_share(2.0, True, *self.ARGS), (0.05, False))
        self.assertEqual(governor.equity_share(3.0, False, *self.ARGS), (0.05, False))   # inside the gap: stays


class Target(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 18, tzinfo=dt.timezone.utc)

    def test_each_flow_grows_from_its_own_day(self):
        flows = [(self.NOW - dt.timedelta(days=30), 1000.0), (self.NOW - dt.timedelta(days=15), 500.0),
                 (self.NOW, -100.0)]
        want = 1000 * 1.02 + 500 * 1.02 ** 0.5 - 100
        self.assertAlmostEqual(governor.target_usd(flows, self.NOW, 2.0), want, places=9)
        self.assertAlmostEqual(governor.target_usd(flows, self.NOW, 0.0), 1400.0, places=9)

    def test_a_flow_stamped_after_now_does_not_shrink(self):
        self.assertAlmostEqual(governor.target_usd([(self.NOW + dt.timedelta(days=1), 100.0)], self.NOW, 2.0), 100.0)


class EquityBook(unittest.TestCase):
    """equity_measure and equity_share_now on the test ledger: a $1,000
    baseline 30 days ago, $500 in 15 days ago, $1,610 of equity now."""

    def setUp(self):
        _fixtures.ensure_profile()
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute("update config set governor_paying = false, governor_switched_at = null where name = 'sol-usdc'")
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, profile) values "
                        "(now() - interval '30 days', 'baseline', 5, 500, 1000, 100, 'sol-usdc'), "
                        "(now() - interval '15 days', 'deposit', 0, 500, 500, 100, 'sol-usdc')")
        db.open_position('m1', POOL, 'SOL/USDC', 95, 105, 5, 's', 1500, 't')
        self.equity(1610.0)

    def tearDown(self):
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute("update config set governor_paying = false, governor_switched_at = null where name = 'sol-usdc'")

    def equity(self, usd):
        db.snapshot('m1', 100.0, True, '1', 0, 0, 0, usd / 2, usd / 2)

    def test_the_measure_reads_equity_and_grows_the_money_put_in(self):
        m = governor.equity_measure('sol-usdc', 2.0)
        target = 1000 * 1.02 + 500 * 1.02 ** 0.5
        self.assertAlmostEqual(m['equity_usd'], 1610.0, places=2)
        self.assertAlmostEqual(m['target_usd'], target, places=1)
        self.assertAlmostEqual(m['put_in_usd'], 1500.0)
        self.assertAlmostEqual(m['gap_pct'], (1610 / target - 1) * 100, places=1)
        with mock.patch.object(db, '_uncounted_usd', lambda p: 10.0):        # rent the snapshots miss counts
            self.assertAlmostEqual(governor.equity_measure('sol-usdc', 2.0)['equity_usd'], 1620.0, places=2)

    def test_no_baseline_or_nothing_left_in_is_none(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, profile) values "
                        "(now() - interval '1 day', 'withdrawal', 0, 1500, 1500, 100, 'sol-usdc')")
        self.assertIsNone(governor.equity_measure('sol-usdc', 0.0))            # all of it taken out: no target
        with db.cursor(commit=True) as cur:
            cur.execute("delete from capital_flows")
        self.assertIsNone(governor.equity_measure('sol-usdc', 2.0))
        cfg = dict(db.load_config('sol-usdc'), governor_mode='equity')
        self.assertIsNone(db.governor_view('sol-usdc', cfg, 20.0))
        self.assertEqual(db.governor_state('no-such-profile'), (False, None))

    def test_the_profit_mode_still_pays_the_053_share(self):
        with mock.patch.multiple(config, PROFILE='sol-usdc', GOVERNOR_MODE='profit', GOVERNOR_DAYS=7,
                                 GOVERNOR_KEEP=0.2, GOVERNOR_CAP=0.5), \
                mock.patch.object(governor, 'measure', lambda p, d: {'fees': 10.0, 'loss': -5.0}):
            self.assertEqual(governor.share_now(), (0.4, {'fees': 10.0, 'loss': -5.0, 'mode': 'profit', 'ratio': 2.0,
                                                          'share': 0.4, 'days': 7, 'keep': 0.2, 'cap': 0.5}))
        # sql/058: no cap, the payout follows R; keep 0.5 pays half of the profit share
        with mock.patch.multiple(config, PROFILE='sol-usdc', GOVERNOR_MODE='profit', GOVERNOR_DAYS=7,
                                 GOVERNOR_KEEP=0.5, GOVERNOR_CAP=1.0), \
                mock.patch.object(governor, 'measure', lambda p, d: {'fees': 30.0, 'loss': -10.0}):
            share, m = governor.share_now()
        self.assertAlmostEqual(share, (1 - 10 / 30) * 0.5); self.assertEqual((m['ratio'], m['cap']), (3.0, 1.0))
        with mock.patch.multiple(config, PROFILE='sol-usdc', GOVERNOR_MODE='profit'), \
                mock.patch.object(governor, 'measure', side_effect=RuntimeError('x')):
            share, m = governor.share_now()
        self.assertEqual(share, 0.0)
        self.assertIn('x', m['error'])

    def run_now(self):
        with mock.patch.multiple(config, PROFILE='sol-usdc', GOVERNOR_GROWTH_PCT=2.0, GOVERNOR_ENTER_PCT=5.0,
                                 GOVERNOR_EXIT_PCT=2.0, GOVERNOR_FLOOR=0.05, GOVERNOR_HIGH=0.3, GOVERNOR_MODE='equity'):
            return governor.share_now()

    def test_it_switches_once_writes_the_state_and_switches_back(self):
        share, m = self.run_now()                                   # +5.6% over 1,524.95: high
        self.assertEqual((share, m['paying'], m['switched']), (0.3, True, True))
        self.assertEqual(db.governor_state('sol-usdc')[0], True)
        share, m = self.run_now()
        self.assertEqual((share, m['switched']), (0.3, False))       # no second switch
        self.equity(1570.0)                                         # +3.3%: inside the gap, still high
        self.assertEqual(self.run_now()[0], 0.3)
        self.equity(1540.0)                                         # +1.3%: back to the floor
        share, m = self.run_now()
        self.assertEqual((share, m['paying'], m['switched']), (0.05, False, True))
        self.assertEqual(db.governor_state('sol-usdc')[0], False)
        self.equity(1570.0)                                         # inside the gap from below: floor
        self.assertEqual(self.run_now()[0], 0.05)

    def test_no_book_or_an_error_pays_the_floor(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from capital_flows")
        self.assertEqual(self.run_now()[0], 0.05)
        with mock.patch.object(db, 'governor_capital', side_effect=RuntimeError('db down')):
            share, m = self.run_now()
        self.assertEqual(share, 0.05)
        self.assertIn('db down', m['error'])

    def test_the_book_view_shows_the_state_the_target_and_the_switch_levels(self):
        cfg = dict(db.load_config('sol-usdc'), governor_mode='equity')
        v = db.governor_view('sol-usdc', cfg, 20.0)
        self.assertEqual((v['mode'], v['state'], v['share']), ('equity', 'PAY', 0.3))   # the view applies the rule
        self.assertEqual(db.governor_state('sol-usdc')[0], False)                   # and writes nothing
        self.assertAlmostEqual(v['enter_at_usd'], v['target_usd'] * 1.05, places=1)
        self.assertAlmostEqual(v['exit_at_usd'], v['target_usd'] * 1.02, places=1)
        self.assertAlmostEqual(v['paid_per_day_usd'], 6.0)
        self.assertAlmostEqual(v['reinvested_per_day_usd'], 14.0)
        v = db.governor_view('sol-usdc', dict(cfg, governor_high=0.4, governor_enter_pct=5.5, governor_exit_pct=3,
                                              governor_growth_pct=0), None)
        self.assertEqual((v['share'], v['enter_pct'], v['exit_pct']), (0.4, 5.5, 3.0))
        self.assertAlmostEqual(v['target_usd'], 1500.0, places=2)
        self.assertIsNone(v['fees_per_day_usd']); self.assertIsNone(v['paid_per_day_usd'])
        self.equity(1500.0)
        v = db.governor_view('sol-usdc', cfg, 20.0)
        self.assertEqual((v['state'], v['share']), ('REINVEST', 0.05))
        self.assertLess(v['gap_usd'], 0)


if __name__ == '__main__':
    unittest.main()
