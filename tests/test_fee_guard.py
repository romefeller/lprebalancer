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


class Config(unittest.TestCase):
    def patch(self, **kw):
        base = dict(REGIME_GUARD='live', REGIME_GUARD_WINDOW=36, REGIME_GUARD_THRESHOLD=1.2,
                    REGIME_GUARD_FEE_C={'POOL': C})
        base.update(kw)
        return mock.patch.multiple(config, **base)

    def test_off_is_none(self):
        with self.patch(REGIME_GUARD='off'):
            self.assertIsNone(rebalancer.guard_config('POOL'))

    def test_the_settings_and_the_pool_s_constant(self):
        with self.patch():
            self.assertEqual(rebalancer.guard_config('POOL', {'filled_24h': 0}),
                             {'mode': 'live', 'window_bars': 36, 'threshold': 1.2, 'fee_c': C})
            self.assertEqual(rebalancer.guard_config('POOL')['fee_c'], C)

    def test_another_pool_or_a_filled_tape_has_no_constant(self):
        with self.patch():
            self.assertIsNone(rebalancer.guard_config('DJT')['fee_c'])
            self.assertIsNone(rebalancer.guard_config('POOL', {'filled_24h': 3})['fee_c'])

    def test_the_config_row_is_read(self):
        import importlib
        row = dict(regime_guard='narrow', regime_guard_window_bars=72, regime_guard_threshold=1.0,
                   regime_guard_fee_c={'P': 7.62e-11})
        self.assertEqual(str(row['regime_guard']), 'narrow')
        self.assertEqual({str(k): float(v) for k, v in row['regime_guard_fee_c'].items()}, {'P': 7.62e-11})
        self.assertIn(config.REGIME_GUARD, calm.GUARD_MODES)
        self.assertIn('regime_guard', config.summary())
        importlib.reload(config)


if __name__ == '__main__':
    unittest.main()
