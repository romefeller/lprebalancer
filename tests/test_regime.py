"""Regime mode: the width follows the market, with no fixed budget."""
import math
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures
_fixtures.ensure_profile()

import calm        # noqa: E402
import rebalancer  # noqa: E402

W = calm.WIDTHS


def tape(n=1000, sigma=0.001, seed=3, jump_at=None, hot=None):
    rng = np.random.default_rng(seed)
    s = np.full(n, sigma)
    if hot is not None:
        s[hot:] = sigma * 4
    r = rng.normal(0, 1, n) * s
    c = 100 * np.exp(np.cumsum(r))
    h = c * np.exp(np.abs(rng.normal(0, 1, n)) * s / 2)
    l = c * np.exp(-np.abs(rng.normal(0, 1, n)) * s / 2)
    ts = time.time() - (n + 1) * 300 + np.arange(n) * 300
    return ts, c.copy(), h, l, c, np.full(n, 1e5)


class Maths(unittest.TestCase):
    def test_wider_bands_touch_less_and_the_choice_is_the_narrowest_under_threshold(self):
        b = tape()
        p = float(b[4][-1])
        v = calm.regime_view(b, p, p / 1.01, p * 1.01, horizon_minutes=120, threshold=0.25)
        ps = [v['probs'][f'{(k - 1) * 100:g}'] for k in W]
        self.assertTrue(all(a >= b_ - 1e-12 for a, b_ in zip(ps, ps[1:])))           # monotone
        first_ok = next(k for k, q in zip(W, ps) if q <= 0.25)
        self.assertEqual(v['choice'], first_ok)

    def test_calm_tape_chooses_1pct_hot_tape_goes_wide(self):
        calm_b = tape(sigma=0.0004)
        p = float(calm_b[4][-1])
        self.assertEqual(calm.regime_view(calm_b, p, p / 1.01, p * 1.01)['mode'], 'CALM')
        hot_b = tape(sigma=0.0004, hot=900)                  # volatility x4 in the last 100 bars
        p = float(hot_b[4][-1])
        v = calm.regime_view(hot_b, p, p / 1.01, p * 1.01)
        self.assertNotEqual(v['mode'], 'CALM'); self.assertGreaterEqual(v['choice'], 1.015)
        hotter = tape(sigma=0.0004, hot=900); hotter = hotter[:4] + (hotter[4],) + hotter[5:]
        b8 = tape(sigma=0.0004, hot=950)
        b8 = (b8[0], b8[1] * 1, b8[2], b8[3], b8[4], b8[5])
        # a much hotter recent tape: x10 volatility reads wider still
        rng = np.random.default_rng(1)
        c = hot_b[4].copy(); c[-50:] = c[-51] * np.exp(np.cumsum(rng.normal(0, 0.004, 50)))
        h = np.maximum(hot_b[2], c * 1.002); l = np.minimum(hot_b[3], c * 0.998)
        v2 = calm.regime_view((hot_b[0], c, h, l, c, hot_b[5]), float(c[-1]), c[-1] / 1.01, c[-1] * 1.01)
        self.assertGreater(v2['choice'], v['choice'])

    def test_velocity_conditioning_uses_the_matching_tercile(self):
        t = {'u': np.r_[np.zeros(300), np.full(300, 10.0)], 'd': np.zeros(600),
             'v': np.r_[np.full(300, -1.0), np.full(300, 1.0)]}
        self.assertEqual(calm.p_touch_cond(t, 0.01, 0.01, 0.002, vel_now=-2.0), 0.0)   # cooling origins never touched
        self.assertEqual(calm.p_touch_cond(t, 0.01, 0.01, 0.002, vel_now=2.0), 1.0)    # heating origins always did
        self.assertAlmostEqual(calm.p_touch_cond(t, 0.01, 0.01, 0.002), 0.5)          # unconditioned
        self.assertEqual(calm.p_touch_cond(t, 0.0, 0.01, 0.002), 1.0)                 # at the edge

    def test_instability_and_velocity_are_causal(self):
        b = tape()
        s = calm.ewma_sigma(b[4]); v = calm.velocity(s); i = calm.instability(s)
        c2 = b[4].copy(); c2[700:] *= 1.3
        s2 = calm.ewma_sigma(c2)
        self.assertTrue(np.allclose(calm.velocity(s2)[:700], v[:700]))
        self.assertTrue(np.allclose(calm.instability(s2)[:700], i[:700]))


class Decide(unittest.TestCase):
    def v(self, held, choice, inside=True):
        return {'held': held, 'choice': choice, 'inside': inside}

    def test_steps(self):
        self.assertEqual(calm.regime_decide(self.v(1.01, 1.02)), 'widen')      # 3 steps
        self.assertEqual(calm.regime_decide(self.v(1.01, 1.015)), 'widen')     # 2 steps
        self.assertIsNone(calm.regime_decide(self.v(1.01, 1.0125)))             # 1 step: hold
        self.assertEqual(calm.regime_decide(self.v(1.05, 1.03)), 'narrow')
        self.assertIsNone(calm.regime_decide(self.v(1.01, 1.05, inside=False)))  # exit: the loop's
        self.assertIsNone(calm.regime_decide(None))


class Loop(unittest.TestCase):
    def test_an_exit_under_regime_does_not_wait_for_the_gap(self):
        now = time.time(); sent = []
        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [now - 30], 'failures': 0}
        with mock.patch.object(rebalancer.config, 'REGIME_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'CALM_MIN_GAP', 600), \
                mock.patch.object(rebalancer.config, 'CALM_MAX_MOVES', 48), \
                mock.patch.object(rebalancer.config, 'MAX_REBALANCES_PER_DAY', 6), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('reached the harvest'))):
            with self.assertRaises(RuntimeError):
                rebalancer.rebalance(state, {'positionMint': 'M'}, 'price went above', band=1.01,
                                     calm_move=True, exit_move=True)
            rebalancer.rebalance(state, {'positionMint': 'M'}, 'regime WARM', band=1.02, calm_move=True)
        self.assertEqual(sent[-1], 'rebalance_deferred')                        # a voluntary move still waits

    def test_regime_off_never_reads_the_tape(self):
        with mock.patch.object(rebalancer.config, 'REGIME_ENABLED', False), \
                mock.patch.object(rebalancer, 'tape5', side_effect=AssertionError('read')):
            self.assertIsNone(rebalancer.regime_view({}, {'price': 1, 'lowerPrice': 0.9, 'upperPrice': 1.1}))
            self.assertIsNone(rebalancer.regime_choice_now('P', 1.0))

    def test_choice_now_refuses_a_stale_tape(self):
        b = tape()
        old = tuple(list(b[:1]) and [b[0] - 3600]) + b[1:]
        with mock.patch.object(rebalancer.config, 'REGIME_ENABLED', True), \
                mock.patch.object(rebalancer, 'tape5', lambda pool, price: old):
            self.assertIsNone(rebalancer.regime_choice_now('P', float(b[4][-1])))
        with mock.patch.object(rebalancer.config, 'REGIME_ENABLED', True), \
                mock.patch.object(rebalancer, 'tape5', lambda pool, price: b):
            self.assertIn(rebalancer.regime_choice_now('P', float(b[4][-1])), W)

    def test_view_records_mode_changes_and_the_guard(self):
        b = tape(sigma=0.0004); p = float(b[4][-1]); events = []
        state = {'calm_times': [time.time() - 10] * 3}
        with mock.patch.object(rebalancer.config, 'REGIME_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'CALM_MAX_MOVES', 48), \
                mock.patch.object(rebalancer, 'tape5', lambda pool, price: b), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: events.append(a)):
            v = rebalancer.regime_view(state, {'price': p, 'lowerPrice': p / 1.01, 'upperPrice': p * 1.01})
        self.assertEqual(v['moves_24h'], 3); self.assertEqual(v['guard'], 48)
        self.assertEqual(state['regime_mode'], 'CALM'); self.assertEqual(events[0][0], 'REGIME')
        self.assertIs(rebalancer.LAST_REGIME['view'], v)


class RollingTape(unittest.TestCase):
    def test_merge_dedupes_by_timestamp_keeps_newest_and_caps_the_length(self):
        a = (np.array([0., 300, 600]), np.ones(3), np.ones(3), np.ones(3), np.array([1., 2, 3]), np.ones(3))
        b = (np.array([600., 900]), np.ones(2), np.ones(2), np.ones(2), np.array([30., 4]), np.ones(2))
        m = rebalancer._merge([a, b])
        self.assertEqual(list(m[0]), [0, 300, 600, 900]); self.assertEqual(list(m[4]), [1, 2, 30, 4])
        with mock.patch.object(rebalancer, 'TAPE5_BARS', 2):
            self.assertEqual(list(rebalancer._merge([a, b])[0]), [600, 900])
        self.assertIsNone(rebalancer._merge([None, None]))

    def test_tape_pages_back_until_full_and_survives_a_restart(self):
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp())
        now = 1_790_000_000
        def page(before=None):
            end = int(before) if before else now
            ts = np.arange(end - 1000 * 300, end, 300, dtype=float)
            c = np.full(len(ts), 100.0)
            return ts, c, c, c, c, np.ones(len(ts))
        calls = []
        def fake(pool, live_price=None, before=None):
            calls.append(before); return page(before)
        rebalancer._TAPE5.clear()
        with mock.patch.object(rebalancer, 'ROOT', d), mock.patch.object(rebalancer.calm, 'tape_5m', fake):
            b = rebalancer.tape5('POOLADDRESS1', 100.0)
            self.assertEqual(len(b[0]), rebalancer.TAPE5_BARS)
            self.assertEqual(len(calls), 3)                          # newest, then two pages back
            rebalancer._TAPE5.clear(); calls.clear()
            b2 = rebalancer.tape5('POOLADDRESS1', 100.0)            # from disk: one refresh only
            self.assertEqual(len(calls), 1); self.assertEqual(len(b2[0]), rebalancer.TAPE5_BARS)
            rebalancer._TAPE5.clear(); calls.clear()
            b3 = rebalancer.tape5('POOLADDRESS1', 5.0)              # wrong orientation on disk: rebuilt
            self.assertEqual(calls[0], None)
        rebalancer._TAPE5.clear()
