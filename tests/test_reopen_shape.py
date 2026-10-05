"""The band reopened after an exit (sql/026): its centre offset against the
exit (calm.offset_band, calm.band_share_a; the pre-open swap and the deposit
caps follow its share of token A) and one band one rung wider when a touch
soon is likely (calm.p_touch_width, rebalancer.reopen_width)."""
import math
import unittest
from unittest import mock

import numpy as np
from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import config
import rebalancer
from test_deploy_all import Patched, bal, SOL, USDC, RES

ORIGINAL_BW = rebalancer.balance_wallet


class Offset(unittest.TestCase):
    def test_centred_for_no_side_or_no_offset(self):
        self.assertEqual(calm.offset_band(100.0, 1.01, 0, 0.15), (100.0 / 1.01, 100.0 * 1.01))
        self.assertEqual(calm.offset_band(100.0, 1.01, 1, 0.0), (100.0 / 1.01, 100.0 * 1.01))

    def test_against_the_exit(self):
        lo, hi = calm.offset_band(100.0, 1.01, 1, 0.15)                   # left above: sits a little below
        c = math.sqrt(lo * hi)
        self.assertAlmostEqual(math.log(c / 100.0) / math.log(1.01), -0.15)
        lo2, hi2 = calm.offset_band(100.0, 1.01, -1, 0.15)                # left below: a little above
        self.assertAlmostEqual(math.log(math.sqrt(lo2 * hi2) / 100.0) / math.log(1.01), 0.15)

    def test_bad_inputs_are_refused(self):
        for args in ((100.0, 1.01, 2, 0.1), (100.0, 1.01, 1, 1.0), (100.0, 1.01, 1, -0.1), (100.0, 1.0, 1, 0.1),
                     (0.0, 1.01, 1, 0.1), (-1.0, 1.01, 1, 0.1)):
            with self.assertRaises(ValueError):
                calm.offset_band(*args)

    @given(st.floats(0.001, 1e5), st.floats(1.001, 1.2), st.sampled_from([-1, 0, 1]), st.floats(0, 0.99))
    def test_the_price_stays_inside_and_the_width_is_kept(self, p, k, side, frac):
        lo, hi = calm.offset_band(p, k, side, frac)
        self.assertLess(lo, p * (1 + 1e-12)); self.assertGreater(hi, p * (1 - 1e-12))
        self.assertAlmostEqual(hi / lo, k * k, places=9)


class Share(unittest.TestCase):
    def test_half_when_centred_and_the_edges(self):
        self.assertAlmostEqual(calm.band_share_a(100.0, 100.0 / 1.01, 100.0 * 1.01), 0.5)
        self.assertAlmostEqual(calm.band_share_a(100.0, 100.0, 101.0), 1.0)
        self.assertAlmostEqual(calm.band_share_a(101.0, 100.0, 101.0), 0.0)

    def test_the_hand_computation(self):
        p, lo, hi = 100.0, 99.0, 102.0
        x, y = 1 / math.sqrt(p) - 1 / math.sqrt(hi), math.sqrt(p) - math.sqrt(lo)
        self.assertAlmostEqual(calm.band_share_a(p, lo, hi), x * p / (x * p + y))

    def test_bad_bands_are_refused(self):
        for args in ((100.0, 101.0, 102.0), (100.0, 98.0, 99.0), (100.0, 100.0, 100.0), (100.0, 0.0, 101.0)):
            with self.assertRaises(ValueError):
                calm.band_share_a(*args)

    def test_a_band_left_above_needs_less_of_token_a(self):
        self.assertLess(calm.band_share_a(100.0, *calm.offset_band(100.0, 1.01, 1, 0.15)), 0.5)
        self.assertGreater(calm.band_share_a(100.0, *calm.offset_band(100.0, 1.01, -1, 0.15)), 0.5)

    @settings(max_examples=80)
    @given(st.floats(0.01, 1000), st.floats(1.002, 1.2), st.floats(-0.95, 0.95), st.floats(0.001, 1000))
    def test_units_do_not_matter_and_higher_prices_hold_less_a(self, p, k, f, m):
        side = 1 if f > 0 else -1 if f < 0 else 0
        lo, hi = calm.offset_band(p, k, side, abs(f))
        s = calm.band_share_a(p, lo, hi)
        self.assertTrue(0 <= s <= 1)
        self.assertAlmostEqual(calm.band_share_a(p * m, lo * m, hi * m), s, places=6)
        q = min(hi, p * 1.0001)
        self.assertLessEqual(calm.band_share_a(q, lo, hi), s + 1e-9)


def walk(n=800, seed=3, sigma=0.0012):
    rng = np.random.default_rng(seed)
    close = 120 * np.exp(np.cumsum(rng.normal(0, sigma, n)))
    return (1_700_000_000 + 300 * np.arange(n), close, close * (1 + sigma), close * (1 - sigma), close, np.ones(n))


class TouchSoon(unittest.TestCase):
    def test_it_is_the_regime_view_probability_at_that_horizon(self):
        bars = walk(); p = float(bars[4][-1])
        v = calm.regime_view(bars, p, p / 1.01, p * 1.01, widths=calm.WIDTHS, horizon_minutes=30, threshold=0.25)
        for k, (pct, prob) in zip(calm.WIDTHS, v['probs']):
            self.assertEqual(calm.p_touch_width(bars, k, 30), prob)
        for h in (2, 155, 240):
            v = calm.regime_view(bars, p, p / 1.01, p * 1.01, widths=(1.01,), horizon_minutes=h)
            self.assertEqual(calm.p_touch_width(bars, 1.01, h), v['probs'][0][1])

    def test_wider_is_less_likely_and_no_tape_is_none(self):
        bars = walk()
        self.assertGreaterEqual(calm.p_touch_width(bars, 1.01, 30), calm.p_touch_width(bars, 1.015, 30))
        self.assertIsNone(calm.p_touch_width(None, 1.01, 30))
        self.assertIsNone(calm.p_touch_width(tuple(np.array([]) for _ in range(6)), 1.01, 30))
        short = tuple(x[:50] for x in walk())
        self.assertIsNone(calm.p_touch_width(short, 1.01, 30))


class TouchState(unittest.TestCase):
    def test_the_horizon_in_bars_and_the_values_now(self):
        bars = walk()
        _ts, _o, high, low, close, _v = bars
        sigma = calm.ewma_sigma(close)
        vel = calm.velocity(sigma)
        for minutes, H in ((30, 6), (2, 1), (155, 31), (150, 30), (120, 24)):
            table, sg, s_now, v_now = calm.touch_state(bars, minutes)
            want = calm.touch_table(high, low, close, sigma, H, vel)
            np.testing.assert_array_equal(table['u'], want['u'], err_msg=str(minutes))
            np.testing.assert_array_equal(table['v'], want['v'])
            np.testing.assert_array_equal(sg, sigma)
            self.assertEqual((s_now, v_now), (float(sigma[-1]), float(vel[-1])))


class Widen(unittest.TestCase):
    def go(self, k=1.01, p=0.7, fresh=True, bars=('bars',), boom=None, **cfg):
        base = dict(REOPEN_WIDEN_P=0.5, REOPEN_WIDEN_BAND=1.015, REOPEN_WIDEN_MIN=30, REGIME_WIDTHS=calm.WIDTHS,
                    REGIME_ENABLED=True)
        base.update(cfg)
        seen = []
        with mock.patch.multiple(config, **base), \
             mock.patch.object(rebalancer, 'tape5', side_effect=boom, return_value=bars if bars != ('none',) else None), \
             mock.patch.object(rebalancer.calm, 'tape_fresh', return_value=fresh), \
             mock.patch.object(rebalancer.calm, 'p_touch_width', return_value=p) as pt, \
             mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append((ev, kw))):
            return rebalancer.reopen_width(k, 'POOL', 120.0), seen, pt

    def test_a_likely_touch_widens_this_band(self):
        k, seen, pt = self.go()
        self.assertEqual(k, 1.015)
        self.assertEqual(seen[0][0], 'REOPEN_WIDE'); self.assertEqual(seen[0][1]['p_touch'], 0.7)
        pt.assert_called_once_with(('bars',), 1.01, 30)

    def test_the_threshold_is_inclusive(self):
        self.assertEqual(self.go(p=0.5)[0], 1.015)
        self.assertEqual(self.go(p=0.4999)[0], 1.01)

    def test_off_wider_or_no_band_is_left_alone(self):
        self.assertEqual(self.go(REOPEN_WIDEN_P=0)[0], 1.01)
        self.assertEqual(self.go(REGIME_ENABLED=False)[0], 1.01)        # calm mode: never
        self.assertEqual(self.go(k=1.02)[0], 1.02)
        self.assertIsNone(self.go(k=None)[0])

    def test_stale_missing_or_unknown_tape_keeps_the_width(self):
        self.assertEqual(self.go(fresh=False)[0], 1.01)
        k, seen, _ = self.go(bars=('none',))
        self.assertEqual((k, seen), (1.01, []))                          # no tape: no failure notice either
        self.assertEqual(self.go(p=None)[0], 1.01)
        k, seen, _ = self.go(boom=RuntimeError('db'))
        self.assertEqual(k, 1.01); self.assertEqual(seen[0][0], 'reopen_widen_failed')


class SwapTarget(Patched):
    REC = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}}

    def run_it(self, b, share_a):
        calls = []
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or ({'sent': True, 'signature': 's'}, None))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: b), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            rebalancer.balance_wallet({'failures': 0}, dict(b), self.REC, share_a=share_a)
        return calls

    def test_an_off_centre_band_swaps_to_its_share(self):
        b = bal(0.2, 200.0, price=120.0)
        C = rebalancer.deployable_usd(b)
        _, _, _, ta, tb, *_ = self.run_it(b, 0.425)[0]
        self.assertAlmostEqual(float(ta), C * 0.425 + rebalancer.OPEN_RENT_HEADROOM_SOL * 120.0, places=2)
        self.assertAlmostEqual(float(tb), C * 0.575, places=2)

    def test_a_wallet_already_at_the_share_does_not_swap(self):
        C = 220.0
        b = bal(C * 0.425 / 120.0 + RES(), C * 0.575)
        self.assertEqual(self.run_it(b, 0.425), [])
        b2 = bal(C * 0.44 / 120.0 + RES(), C * 0.56)                     # 1.5 points off: within 2%
        self.assertEqual(self.run_it(b2, 0.425), [])

    def test_a_wallet_at_one_half_swaps_toward_the_share_when_far(self):
        C = 220.0
        b = bal(C * 0.5 / 120.0 + RES(), C * 0.5)
        self.assertEqual(len(self.run_it(b, 0.40)), 1)                    # 10 points off
        self.assertEqual(self.run_it(b, None), [])                       # centred: no swap, as before


class SwapBoundary(Patched):
    REC = SwapTarget.REC

    def test_two_points_off_is_kept_a_hair_more_swaps(self):
        run = lambda a_usd, b_usd: SwapTarget.run_it(self, bal(a_usd / 128.0, b_usd, price=128.0, native=None), 0.25)
        self.assertEqual(run(27.0, 73.0), [])                             # 27 of 100 vs 25: exactly 2 points
        self.assertEqual(run(23.0, 77.0), [])
        self.assertEqual(len(run(27.5, 72.5)), 1)                         # 2.5 points
        self.assertEqual(len(run(22.5, 77.5)), 1)
        self.assertEqual(len(run(26.5, 73.5)), 0)                         # 1.5 points
        self.assertEqual(len(run(28.0, 72.0)), 1)

    def test_a_kept_wallet_is_returned_as_it_is(self):
        w = bal(27.0 / 128.0, 73.0, price=128.0, native=None)
        with mock.patch.object(rebalancer, 'chain', side_effect=AssertionError('no swap')):
            self.assertEqual(rebalancer.balance_wallet({'failures': 0}, dict(w), self.REC, share_a=0.25), w)


class Caps(Patched):
    def test_an_off_centre_band_may_take_more_of_its_bigger_side(self):
        b = bal(5.0, 1000.0, price=120.0)
        C = rebalancer.capital(b)
        a0, b0 = rebalancer.deposit_caps(b)
        self.assertAlmostEqual(b0, C * 0.55); self.assertAlmostEqual(a0 * 120.0, C * 0.55)
        a1, b1 = rebalancer.deposit_caps(b, share_a=0.40)
        self.assertAlmostEqual(b1, C * (0.60 + 0.05))
        self.assertAlmostEqual(a1 * 120.0, C * 0.55)                      # the smaller side keeps the side cap
        a2, b2 = rebalancer.deposit_caps(b, share_a=0.62)
        self.assertAlmostEqual(a2 * 120.0, C * 0.67); self.assertAlmostEqual(b2, C * 0.55)
        with mock.patch.object(config, 'SIDE_CAP_FRACTION', 0.9):
            a3, b3 = rebalancer.deposit_caps(bal(50.0, 9000.0, price=120.0), share_a=0.8)
            C3 = rebalancer.capital(bal(50.0, 9000.0, price=120.0))
            self.assertAlmostEqual(a3 * 120.0, C3 * 1.0)                  # 0.8 + 0.4 capped at the whole capital
            self.assertAlmostEqual(b3, C3 * 0.9)


class OffIsTheOldBehaviour(Patched):
    REC = SwapTarget.REC
    """share_a None gives exactly the pre-026 caps and swap decision."""
    @settings(max_examples=200, deadline=None)
    @given(a=st.floats(0.0, 5), b=st.floats(0, 800), price=st.floats(20, 300), cap=st.floats(0.5, 0.9))
    def test_caps(self, a, b, price, cap):
        with mock.patch.object(config, 'SIDE_CAP_FRACTION', cap):
            w = bal(a, b, price=price)
            ca, cb = rebalancer.deposit_caps(w)
            cq = rebalancer.capital(w) / w['quoteUsd']
            res = rebalancer.native_reserve(w)
            self.assertEqual(ca, min(max(w['balanceA'] - res, 0), cq * cap / price))
            self.assertEqual(cb, min(max(w['balanceB'], 0), cq * cap))

    @settings(max_examples=100, deadline=None)
    @given(a=st.floats(0.06, 4), b=st.floats(0, 500), price=st.floats(50, 300))
    def test_swap_decision(self, a, b, price):
        w = bal(a, b, price=price)
        res = rebalancer.native_reserve(w)
        usd_a, usd_b, C = max(a - res, 0) * price, b, rebalancer.capital(w)
        need = C * rebalancer.side_target_fraction() * 0.97
        old_swaps = not (min(usd_a, usd_b) >= need or abs(usd_a - usd_b) <= 0.04 * (usd_a + usd_b))
        self.assertEqual(bool(SwapTarget.run_it(self, w, None)), old_swaps)


class ShareProperty(Patched):
    @settings(max_examples=150, deadline=None)
    @given(a=st.floats(0.0, 5), b=st.floats(0, 800), price=st.floats(20, 300), cap=st.floats(0.5, 1.0),
           frac=st.floats(0, 0.5), side=st.sampled_from([-1, 1]), k=st.floats(1.005, 1.05))
    def test_offset_caps_always_pass_the_open_guard(self, a, b, price, cap, frac, side, k):
        with mock.patch.object(config, 'SIDE_CAP_FRACTION', cap):
            w = bal(a, b, price=price)
            lo, hi = calm.offset_band(price, k, side, frac)
            ca, cb = rebalancer.deposit_caps(w, share_a=calm.band_share_a(price, lo, hi))
            C = rebalancer.capital(w)
            if C <= 0:
                return
            self.assertLessEqual((ca * price + cb) * w['quoteUsd'], 2.0 * C + 1e-6)
            self.assertLessEqual(min(ca * price, cb) * w['quoteUsd'], config.MAX_USD + 1e-6)


class MainLoopExit(unittest.TestCase):
    """The loop's exit hands rebalance the right side and the widened band."""
    def test_side_and_width(self):
        src = open(rebalancer.__file__, encoding='utf-8').read()      # the file: other tests patch main
        i = src.index("if not status.get('inRange'):")
        block = src[i:i + 1600]
        self.assertIn("k = reopen_width(verdict['band'], status.get('whirlpool') or config.POOL, price)", block)
        self.assertIn("exit_side=1 if side == 'above' else -1", block)
        self.assertIn("side = verdict['side']", block)
        self.assertEqual(rebalancer.poll_verdict(
            {'at': 0, 'knobs': rebalancer.poll_knobs(), 'regime': None, 'calm': None, 'forecast': None,
             'band': {'price': 103.0, 'lower': 98.0, 'upper': 102.0, 'in_range': False, 'fees_usd': 0},
             'gates': {'calm_times': [], 'last_rebalance': 0, 'last_harvest': 0, 'breaker_ok': True, 'busy': None}}
        )['side'], 'above')


class Reopen(unittest.TestCase):
    """reopen() opens the offset band after an exit, and only then."""
    def bw(self, shares):
        inner = rebalancer.balance_wallet
        def f(s, bb, r, share_a=None):
            shares.append(share_a)
            return bb if inner is ORIGINAL_BW else inner(s, bb, r, share_a=share_a)
        return f

    def go(self, exit_side=1, frac=0.15, band=1.01, recovering=False):
        b = bal(1.0, 100.0, price=120.0)
        opened, shares, caps = [], [], []
        def chain(*a, **k):
            if a[0] == 'open':
                opened.append((float(a[2]), float(a[3])))
                return None, 'stop here'
            raise AssertionError(a)
        with mock.patch.object(config, 'REOPEN_OFFSET', frac), mock.patch.object(config, 'REGIME_ENABLED', False), \
             mock.patch.object(config, 'CALM_ENABLED', True), \
             mock.patch.object(rebalancer, 'best_band_for', return_value={'band': band, 'price': 120.0, 'record': {}}), \
             mock.patch.object(rebalancer, 'wallet', return_value=dict(b)), \
             mock.patch.object(rebalancer, 'quote_known', return_value=True), \
             mock.patch.object(rebalancer, 'gas_for_open', return_value=True), \
             mock.patch.object(rebalancer, 'calm_view', return_value={'calm': True, 'bar_age_s': 1, 'p_touch_fresh': 0.0}), \
             mock.patch.object(rebalancer, 'balance_wallet', self.bw(shares)), \
             mock.patch.object(rebalancer, 'deposit_caps', lambda bb, share_a=None: (caps.append(share_a) or (0.9, 100.0))), \
             mock.patch.object(rebalancer, 'capital', return_value=200.0), \
             mock.patch.object(rebalancer.guards, 'open_request', return_value=True), \
             mock.patch.object(rebalancer, 'chain', chain), \
             mock.patch.object(rebalancer, 'read_status', return_value=(None, None)), \
             mock.patch.object(rebalancer, 'notify'), mock.patch.object(rebalancer, 'save'), \
             mock.patch.object(rebalancer, 'halt'), mock.patch.object(rebalancer.db, 'event'), \
             mock.patch.object(rebalancer.time, 'sleep'):
            rebalancer.reopen({'failures': 0}, 'price went above', band=band, recovering=recovering, exit_side=exit_side)
        self.caps = caps
        return opened, shares

    def test_an_exit_opens_the_offset_band_and_swaps_to_its_share(self):
        opened, shares = self.go()
        lo, hi = calm.offset_band(120.0, 1.01, 1, 0.15)
        self.assertAlmostEqual(opened[0][0], lo, places=5); self.assertAlmostEqual(opened[0][1], hi, places=5)
        self.assertAlmostEqual(shares[0], calm.band_share_a(120.0, lo, hi))
        self.assertEqual(self.caps, shares)                               # the caps follow the same share

    def test_no_exit_no_offset_or_recovery_opens_centred(self):
        for kw in ({'exit_side': 0}, {'frac': 0.0}, {'recovering': True}):
            opened, shares = self.go(**kw)
            self.assertAlmostEqual(opened[0][0], 120.0 / 1.01, places=5, msg=kw)
            self.assertAlmostEqual(opened[0][1], 120.0 * 1.01, places=5, msg=kw)
            self.assertEqual(shares, [None], kw)
            self.assertEqual(self.caps, [None], kw)

    def test_the_band_is_built_on_the_live_price_after_the_swap(self):
        opened, _ = self.go_after(130.0)
        lo, hi = calm.offset_band(130.0, 1.01, 1, 0.15)
        self.assertAlmostEqual(opened[0][0], lo, places=5); self.assertAlmostEqual(opened[0][1], hi, places=5)

    def go_after(self, live):
        with mock.patch.object(rebalancer, 'balance_wallet', lambda s, bb, r, share_a=None: dict(bb, price=live)):
            return self.go()


class ExitSide(unittest.TestCase):
    """rebalance() hands the exit's side to reopen(); a move to another pool reopens centred."""
    def go(self, exit_side=1, target=None):
        import _fixtures as fx
        got = []
        state = dict(rebalancer.STATE_DEFAULTS)
        status = {'positionMint': 'M', 'whirlpool': 'P', 'price': 100.0, 'inRange': False, 'liquidity': '1',
                  'feesAccruedA': 0.0, 'feesAccruedB': 0.0, 'feesAccrued_USD': 0.0, 'positionUsd': 190.0,
                  'lowerPrice': 99.0, 'upperPrice': 101.0}
        def chain(cmd, *a, **k):
            return ({'signature': 's'}, None) if cmd == 'close' else (None, 'nothing to claim')
        with mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: None), \
                mock.patch.object(rebalancer.db, 'close_position', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'record_band_profile', lambda *a: None), \
                mock.patch.object(rebalancer, 'repoint_with_leftovers', lambda *a: None), \
                mock.patch.object(rebalancer, 'sell_left_behind', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'reopen', lambda s, r, band=None, exit_side=0: got.append(exit_side)), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            rebalancer.rebalance(state, status, 'price went above', band=1.01, calm_move=True, exit_move=True,
                                 exit_side=exit_side, target=target)
        return got

    def test_the_side_reaches_reopen(self):
        self.assertEqual(self.go(1), [1])
        self.assertEqual(self.go(-1), [-1])
        self.assertEqual(self.go(0), [0])

    def test_a_move_to_another_pool_reopens_centred(self):
        self.assertEqual(self.go(1, target={'address': 'Q', 'dex': 'orca'}), [0])


class Config(unittest.TestCase):
    def test_the_summary_names_the_shape(self):
        for k in ('reopen_offset_frac', 'reopen_widen_p', 'reopen_widen_band', 'reopen_widen_minutes'):
            self.assertIn(k, config.summary())


if __name__ == '__main__':
    unittest.main()
