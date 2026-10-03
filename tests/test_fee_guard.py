"""The fee/variance guard: the trailing fee yield over the gamma loss decides
whether the band is concentrated (calm.fee_variance_ratio, calm.guard_width),
wired into calm.regime_view and rebalancer.guard_config (sql/024)."""
import math
import unittest
from unittest import mock

import numpy as np
from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import config
import rebalancer

W = calm.WIDTHS
C = 7.62e-11


def walk(n=800, seed=1, sigma=0.001, vol=2e5):
    rng = np.random.default_rng(seed)
    close = 120 * np.exp(np.cumsum(rng.normal(0, sigma, n)))
    high, low = close * (1 + sigma), close * (1 - sigma)
    ts = 1_700_000_000 + 300 * np.arange(n)
    return ts, close, high, low, close, np.full(n, vol)


class Ratio(unittest.TestCase):
    def test_the_hand_computation(self):
        close = [100.0, 101.0, 100.0, 101.0]
        r = [math.log(101 / 100), math.log(100 / 101), math.log(101 / 100)]
        g = sum(x * x for x in r) / 8
        self.assertAlmostEqual(calm.fee_variance_ratio(close, [1e6] * 3, 3, C), C * 3e6 / g)

    def test_only_the_trailing_window_counts(self):
        close = [50.0, 90.0, 100.0, 101.0, 100.0, 101.0]
        vol = [9e9, 9e9, 9e9, 1e6, 1e6, 1e6]
        self.assertAlmostEqual(calm.fee_variance_ratio(close, vol, 3, C),
                               calm.fee_variance_ratio(close[-4:], vol[-3:], 3, C))

    def test_no_ratio_without_good_inputs(self):
        ok = ([100.0, 101.0, 100.0], [1.0, 1.0])
        for close, vol, n, c in ((ok[0], ok[1], 2, None), (ok[0], ok[1], 2, 0.0), (ok[0], ok[1], 2, -1.0),
                                 (ok[0], ok[1], 0, C), (ok[0], ok[1], 3, C), (ok[0], [1.0], 2, C),
                                 ([100.0, float('nan'), 100.0], ok[1], 2, C), ([100.0, 0.0, 100.0], ok[1], 2, C),
                                 (ok[0], [1.0, -1.0], 2, C), (ok[0], [1.0, float('inf')], 2, C),
                                 ([100.0, 100.0, 100.0], ok[1], 2, C), (ok[0], ok[1], 2, float('nan'))):
            self.assertIsNone(calm.fee_variance_ratio(close, vol, n, c), (close, vol, n, c))

    def test_zero_volume_is_a_ratio_of_zero(self):
        self.assertEqual(calm.fee_variance_ratio([100.0, 101.0, 100.0], [0.0, 0.0], 2, C), 0.0)

    @settings(max_examples=60, deadline=None)
    @given(st.floats(1e-12, 1e-9), st.floats(1.0, 1e7), st.floats(1.01, 10), st.integers(0, 50))
    def test_it_scales_with_the_fee_constant_volume_and_inverse_variance(self, c, v, k, seed):
        ts, _o, _h, _l, close, _v = walk(40, seed)
        vol = np.full(40, v)
        base = calm.fee_variance_ratio(close, vol, 30, c)
        self.assertAlmostEqual(calm.fee_variance_ratio(close, vol, 30, 2 * c) / base, 2.0, places=6)
        self.assertAlmostEqual(calm.fee_variance_ratio(close, 3 * vol, 30, c) / base, 3.0, places=6)
        # returns k times larger: variance k^2 larger
        stretched = close[0] * np.exp(k * np.log(close / close[0]))
        self.assertAlmostEqual(calm.fee_variance_ratio(stretched, vol, 30, c) * k * k / base, 1.0, places=6)
        self.assertGreater(base, 0)


class Width(unittest.TestCase):
    def test_every_mode(self):
        for mode, ratio, want in (('off', 0.1, 1.015), ('off', 9.0, 1.015),
                                  ('live', 0.99, 1.05), ('live', 1.0, 1.015), ('live', 5.0, 1.015),
                                  ('narrow', 0.99, 1.05), ('narrow', 1.0, 1.01), ('narrow', 5.0, 1.01),
                                  ('live', None, 1.015), ('narrow', None, 1.015)):
            self.assertEqual(calm.guard_width(1.015, ratio, widths=W, threshold=1.0, mode=mode), want, (mode, ratio))

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            calm.guard_width(1.015, 1.0, widths=W, threshold=1.0, mode='paper')

    @given(st.sampled_from(W), st.floats(0, 10), st.floats(0.1, 3), st.sampled_from(calm.GUARD_MODES))
    def test_the_answer_is_a_rung_and_low_ratios_are_never_narrower(self, choice, ratio, thr, mode):
        k = calm.guard_width(choice, ratio, widths=W, threshold=thr, mode=mode)
        self.assertIn(k, W)
        if mode != 'off' and ratio < thr:
            self.assertEqual(k, W[-1])


class View(unittest.TestCase):
    def setUp(self):
        self.bars = walk()
        self.p = float(self.bars[4][-1])

    def view(self, guard=None):
        return calm.regime_view(self.bars, self.p, self.p / 1.01, self.p * 1.01, widths=W,
                                horizon_minutes=120, threshold=0.25, guard=guard)

    def test_no_guard_or_off_is_the_touch_rule(self):
        a, b = self.view(), self.view({'mode': 'off', 'window_bars': 72, 'threshold': 1.0, 'fee_c': C})
        self.assertEqual(a['choice'], b['choice'])
        self.assertIsNone(a['guard']); self.assertIsNone(b['guard'])

    def test_the_guard_applies_to_the_touch_choice(self):
        plain = self.view()
        ratio = calm.fee_variance_ratio(self.bars[4], self.bars[5], 72, C)
        for mode in ('live', 'narrow'):
            for thr in (ratio / 2, ratio * 2):
                v = self.view({'mode': mode, 'window_bars': 72, 'threshold': thr, 'fee_c': C})
                want = calm.guard_width(plain['choice'], ratio, widths=W, threshold=thr, mode=mode)
                self.assertEqual(v['choice'], want)
                self.assertEqual(v['choice_pct'], round((want - 1) * 100, 2))
                self.assertEqual(v['guard']['acting'], want != plain['choice'])
                self.assertEqual(v['guard']['touch_choice_pct'], plain['choice_pct'])
                self.assertAlmostEqual(v['guard']['ratio'], round(ratio, 3))
                self.assertEqual(v['mode'], 'CALM' if want == W[0] else 'WARM' if want <= 1.02 else 'HOT')

    def test_no_fee_constant_leaves_the_choice(self):
        v = self.view({'mode': 'narrow', 'window_bars': 72, 'threshold': 1e-9, 'fee_c': None})
        self.assertEqual(v['choice'], self.view()['choice'])
        self.assertIsNone(v['guard']['ratio']); self.assertFalse(v['guard']['acting'])

    def test_a_forced_wide_band_is_widened_by_regime_decide(self):
        v = self.view({'mode': 'live', 'window_bars': 72, 'threshold': 1e9, 'fee_c': C})
        self.assertEqual(v['choice'], W[-1])
        held = dict(v, held=W[0], inside=True)
        self.assertEqual(calm.regime_decide(held, widths=W, steps=3), 'widen')


def fr_per_l(p, da=9, db=6):
    return 2 * math.sqrt(p * 10.0 ** (db - da)) / 10.0 ** db


class RealYield(unittest.TestCase):
    L = 6e10

    def row(self, t, f, mint='A', l=None, inr=True, p=120.0):
        return (t, mint, f, self.L if l is None else l, inr, p)

    def test_the_hand_computation(self):
        rows = [self.row(0, 0.0), self.row(120, 0.01), self.row(240, 0.03, p=121.0)]
        y, cov = calm.real_fee_yield(rows, [], 9, 6)
        want = 0.01 / (self.L * fr_per_l(120.0)) + 0.02 / (self.L * fr_per_l(120.5))
        self.assertAlmostEqual(y / want, 1.0, places=9)
        self.assertEqual(cov, 240.0)

    def test_a_harvest_between_two_snapshots_is_added_back(self):
        rows = [self.row(0, 0.05), self.row(120, 0.01)]
        y, _ = calm.real_fee_yield(rows, [(60, 'A', 0.06), (60, 'B', 9.0), (0, 'A', 9.0), (121, 'A', 9.0)], 9, 6)
        self.assertAlmostEqual(y * self.L * fr_per_l(120.0), 0.02)       # 0.01 - 0.05 + 0.06

    def test_pairs_that_say_nothing_are_skipped(self):
        for b in (self.row(120, 0.02, mint='B'), self.row(120, 0.02, inr=False), self.row(500, 0.02),
                  self.row(120, 0.02, l=5e10), self.row(0, 0.02), self.row(120, 0.0005),
                  self.row(120, None), self.row(120, 0.02, p=0.0), self.row(120, 0.02, l=0.0)):
            a = self.row(0, 0.001)
            if b[3] == 0.0:
                a = self.row(0, 0.001, l=0.0)
            self.assertEqual(calm.real_fee_yield([a, b], [], 9, 6), (0.0, 0.0), b)
        self.assertEqual(calm.real_fee_yield([self.row(0, 0.0, inr=False), self.row(120, 0.01)], [], 9, 6), (0.0, 0.0))

    def test_the_gap_limit_is_inclusive(self):
        y, cov = calm.real_fee_yield([self.row(0, 0.0), self.row(400, 0.01)], [], 9, 6)
        self.assertEqual(cov, 400.0); self.assertGreater(y, 0)

    @settings(max_examples=60, deadline=None)
    @given(st.lists(st.floats(0, 0.05), min_size=2, max_size=12), st.floats(1e9, 1e12), st.floats(10, 1000))
    def test_it_adds_up_and_scales_with_fees_and_inverse_liquidity(self, steps, l, p):
        acc = np.cumsum(steps)
        rows = [(120 * k, 'A', float(a), l, True, p) for k, a in enumerate(acc)]
        y, cov = calm.real_fee_yield(rows, [], 9, 6)
        self.assertAlmostEqual(y * l * fr_per_l(p), float(acc[-1] - acc[0]), places=9)
        self.assertEqual(cov, 120.0 * (len(acc) - 1))
        y2, _ = calm.real_fee_yield([(t, m, 2 * f, 2 * ll, i, pp) for t, m, f, ll, i, pp in rows], [], 9, 6)
        self.assertAlmostEqual(y2, y, places=15)


class RealRatio(unittest.TestCase):
    def setUp(self):
        self.close = walk(100)[4]
        self.g = float(np.sum(np.diff(np.log(self.close[-73:])) ** 2) / 8)

    def test_full_cover_is_yield_over_gamma(self):
        self.assertAlmostEqual(calm.real_variance_ratio(1e-5, 72 * 300, self.close, 72), 1e-5 / self.g)

    def test_part_cover_is_scaled_up_and_over_cover_is_not(self):
        self.assertAlmostEqual(calm.real_variance_ratio(1e-5, 36 * 300, self.close, 72), 2e-5 / self.g)
        self.assertAlmostEqual(calm.real_variance_ratio(1e-5, 90 * 300, self.close, 72), 1e-5 / self.g)

    def test_no_ratio_without_cover_tape_or_variance(self):
        for fy, cov, close, n in ((1e-5, 36 * 300 - 1, self.close, 72), (None, 72 * 300, self.close, 72),
                                  (1e-5, None, self.close, 72), (-1e-9, 72 * 300, self.close, 72),
                                  (1e-5, 72 * 300, self.close[:72], 72), (1e-5, 0, self.close, 0),
                                  (1e-5, 72 * 300, np.full(100, 120.0), 72),
                                  (1e-5, 72 * 300, np.r_[self.close[:-1], np.nan], 72),
                                  (1e-5, 72 * 300, np.r_[self.close[:-1], 0.0], 72)):
            self.assertIsNone(calm.real_variance_ratio(fy, cov, close, n), (fy, cov, n))
        self.assertIsNotNone(calm.real_variance_ratio(0.0, 36 * 300, self.close, 72))


class RealView(unittest.TestCase):
    def test_the_real_source_uses_the_fee_yield(self):
        bars = walk(); p = float(bars[4][-1])
        ratio = calm.real_variance_ratio(1e-5, 72 * 300, bars[4], 72)
        for thr in (ratio / 2, ratio * 2):
            v = calm.regime_view(bars, p, p / 1.01, p * 1.01, widths=W, threshold=0.25,
                                 guard={'mode': 'narrow', 'source': 'real', 'window_bars': 72, 'threshold': thr,
                                        'fee_yield': (1e-5, 72 * 300)})
            self.assertEqual(v['choice'], W[0] if thr < ratio else W[-1])
            self.assertEqual(v['guard']['source'], 'real')
        v = calm.regime_view(bars, p, p / 1.01, p * 1.01, widths=W, threshold=0.25,
                             guard={'mode': 'narrow', 'source': 'real', 'window_bars': 72, 'threshold': 1.0,
                                    'fee_yield': None})
        self.assertIsNone(v['guard']['ratio'])


class Config(unittest.TestCase):
    def patch(self, **kw):
        base = dict(REGIME_GUARD='narrow', REGIME_GUARD_SOURCE='volume', REGIME_GUARD_WINDOW=36,
                    REGIME_GUARD_THRESHOLD=1.2, REGIME_GUARD_POOLS=('POOL',), REGIME_GUARD_FEE_C={'POOL': C},
                    POOL='POOL')
        base.update(kw)
        return mock.patch.multiple(config, **base)

    def test_off_or_an_untested_pool_is_none(self):
        with self.patch(REGIME_GUARD='off'):
            self.assertIsNone(rebalancer.guard_config('POOL'))
        with self.patch():
            self.assertIsNone(rebalancer.guard_config('DJT'))

    def test_the_volume_source(self):
        with self.patch():
            self.assertEqual(rebalancer.guard_config('POOL', {'filled_24h': 0}),
                             {'mode': 'narrow', 'source': 'volume', 'window_bars': 36, 'threshold': 1.2, 'fee_c': C})
            self.assertEqual(rebalancer.guard_config('POOL')['fee_c'], C)
            self.assertIsNone(rebalancer.guard_config('POOL', {'filled_24h': 3})['fee_c'])
        with self.patch(REGIME_GUARD_FEE_C={}):
            self.assertIsNone(rebalancer.guard_config('POOL')['fee_c'])

    def test_the_real_source_asks_for_the_fee_yield(self):
        with self.patch(REGIME_GUARD_SOURCE='real'), \
             mock.patch.object(rebalancer, 'guard_fee_yield', return_value=(1e-5, 900.0)) as fy:
            self.assertEqual(rebalancer.guard_config('POOL', {'filled_24h': 5}),
                             {'mode': 'narrow', 'source': 'real', 'window_bars': 36, 'threshold': 1.2,
                              'fee_yield': (1e-5, 900.0)})
            fy.assert_called_once_with('POOL', 36)

    def test_the_summary_names_the_guard(self):
        for k in ('regime_guard', 'regime_guard_source', 'regime_guard_pools', 'regime_steps'):
            self.assertIn(k, config.summary())


class FeeYieldFromTheBook(unittest.TestCase):
    """guard_fee_yield reads this profile's snapshots and harvests (test DB)."""
    POOL = 'GUARDPOOL'

    def setUp(self):
        import datetime as dt
        import db
        _fixtures.reset_ledger()
        now = db.now()
        self.rec = {'token_a': {'address': 'So11111111111111111111111111111111111111112', 'decimals': 9},
                    'token_b': {'address': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'decimals': 6}}
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, config_name, pool, opened_at) values "
                        "('GA', 'sol-usdc', %s, %s), ('GB', 'other', %s, %s), ('GC', 'sol-usdc', 'ELSE', %s)",
                        (self.POOL, now, self.POOL, now, now))
            for k, f in enumerate((0.0, 0.01, 0.03)):
                ts = now - dt.timedelta(seconds=240 - 120 * k)
                for m in ('GA', 'GB', 'GC'):
                    cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, accrued_usd) "
                                "values (%s,%s,120,true,'60000000000',%s)", (ts, m, f))
            old = now - dt.timedelta(hours=7)
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, accrued_usd) "
                        "values (%s,'GA',120,true,'60000000000',0)", (old,))

    def run_with(self, **kw):
        base = dict(PROFILE='sol-usdc', POOL=self.POOL)
        base.update(kw)
        with mock.patch.multiple(config, **base), mock.patch.object(rebalancer, 'pool_record', return_value=self.rec):
            return rebalancer.guard_fee_yield(self.POOL, 72)

    def test_only_this_profile_s_positions_on_the_pool_in_the_window(self):
        y, cov = self.run_with()
        self.assertAlmostEqual(y * 6e10 * fr_per_l(120.0), 0.03, places=6)
        self.assertAlmostEqual(cov, 240.0, places=3)

    def test_another_pool_a_non_dollar_quote_or_a_failure_is_none(self):
        self.assertIsNone(self.run_with(POOL='HELD'))
        rec = dict(self.rec, token_b={'address': 'So11111111111111111111111111111111111111112', 'decimals': 9})
        with mock.patch.multiple(config, PROFILE='sol-usdc', POOL=self.POOL), \
             mock.patch.object(rebalancer, 'pool_record', return_value=rec):
            self.assertIsNone(rebalancer.guard_fee_yield(self.POOL, 72))
        with mock.patch.multiple(config, PROFILE='sol-usdc', POOL=self.POOL), \
             mock.patch.object(rebalancer, 'pool_record', side_effect=RuntimeError('rpc')):
            self.assertIsNone(rebalancer.guard_fee_yield(self.POOL, 72))


if __name__ == '__main__':
    unittest.main()
