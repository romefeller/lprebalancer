"""Calm mode: the signal, the touch probability, the decision, and the loop
pieces that carry it. Offline."""
import math
import time
import unittest
import contextlib
import pathlib
import tempfile
from unittest import mock

import numpy as np

import _fixtures
_fixtures.ensure_profile()

import calm        # noqa: E402
import config      # noqa: E402
import rebalancer  # noqa: E402


def bars(n=1000, sigma=0.001, seed=4, start=None):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, sigma, n)))
    high = close * np.exp(np.abs(rng.normal(0, sigma / 2, n)))
    low = close * np.exp(-np.abs(rng.normal(0, sigma / 2, n)))
    t0 = start or (time.time() - (n + 2) * 300)
    ts = t0 + np.arange(n) * 300
    return ts, close.copy(), high, low, close, np.full(n, 1e5)


class Signal(unittest.TestCase):
    def test_ewma_sigma_is_causal_and_recovers_the_scale(self):
        b = bars(sigma=0.002)
        s = calm.ewma_sigma(b[4])
        self.assertAlmostEqual(float(np.median(s[100:])), 0.002, delta=0.0006)
        c2 = b[4].copy(); c2[600:] *= 1.5
        self.assertTrue(np.allclose(calm.ewma_sigma(c2)[:600], s[:600]))

    def test_hysteresis(self):
        self.assertTrue(calm.is_calm(0.0015, False, 0.0015, 1.25))
        self.assertFalse(calm.is_calm(0.0016, False, 0.0015, 1.25))
        self.assertTrue(calm.is_calm(0.0018, True, 0.0015, 1.25))     # stays calm inside the band
        self.assertFalse(calm.is_calm(0.0019, True, 0.0015, 1.25))
        self.assertFalse(calm.is_calm(None, True, 0.0015, 1.25))

    def test_p_touch_falls_with_distance_and_uses_highs_and_lows(self):
        ts, o, h, l, c, v = bars(sigma=0.0015)
        s = calm.ewma_sigma(c)
        near = calm.p_touch(h, l, c, s, 0.002, 0.002, 6, float(s[-1]))
        far = calm.p_touch(h, l, c, s, 0.03, 0.03, 6, float(s[-1]))
        self.assertGreater(near, far); self.assertLess(far, 0.05)
        # wicks alone can touch: closes never move, highs do
        flat = np.full(1000, 100.0)
        hi = flat * 1.02
        sig = np.full(1000, 0.001)
        self.assertEqual(calm.p_touch(hi, flat, flat, sig, 0.01, 0.01, 3, 0.001), 1.0)
        self.assertIsNone(calm.p_touch(hi, flat, flat, sig, 0.01, 0.01, 3000, 0.001))


class Decide(unittest.TestCase):
    def v(self, **kw):
        base = {'calm': True, 'tight_held': False, 'p_touch_fresh': 0.1, 'threshold': 0.25}
        base.update(kw)
        return base

    def test_narrow_only_when_calm_budgeted_and_a_fresh_band_is_safe(self):
        self.assertEqual(calm.decide(self.v(), enabled=True, budget_left=3), 'narrow')
        self.assertIsNone(calm.decide(self.v(), enabled=False, budget_left=3))
        self.assertIsNone(calm.decide(self.v(), enabled=True, budget_left=0))
        self.assertIsNone(calm.decide(self.v(calm=False), enabled=True, budget_left=3))
        self.assertIsNone(calm.decide(self.v(p_touch_fresh=0.4), enabled=True, budget_left=3))
        self.assertIsNone(calm.decide(None, enabled=True, budget_left=3))

    def test_tight_band_widens_recentres_or_holds(self):
        t = dict(tight_held=True)
        self.assertEqual(calm.decide(self.v(calm=False, p_touch=0.1, **t), enabled=True, budget_left=3), 'widen')
        self.assertEqual(calm.decide(self.v(p_touch=0.5, **t), enabled=True, budget_left=3), 'recentre')
        self.assertEqual(calm.decide(self.v(p_touch=0.5, **t), enabled=True, budget_left=0), 'widen')
        self.assertIsNone(calm.decide(self.v(p_touch=0.1, **t), enabled=True, budget_left=3))
        # outside: the loop's exit path decides, not this
        self.assertIsNone(calm.decide(self.v(p_touch=1.0, **t), enabled=True, budget_left=3))

    def test_view_marks_the_tight_band_and_its_risk(self):
        b = bars(sigma=0.001)
        p = float(b[4][-1])
        v = calm.view(b, p, p / 1.01, p * 1.01, was_calm=False, cut=0.0015, exit_mult=1.25,
                      band=1.01, horizon_minutes=30, threshold=0.25)
        self.assertTrue(v['tight_held']); self.assertTrue(v['calm'])
        self.assertIsNotNone(v['p_touch']); self.assertIsNotNone(v['p_touch_fresh'])
        w = calm.view(b, p, p / 1.08, p * 1.08, was_calm=False, cut=0.0015, exit_mult=1.25,
                      band=1.01, horizon_minutes=30, threshold=0.25)
        self.assertFalse(w['tight_held']); self.assertNotIn('p_touch', w)
        o = calm.view(b, p * 1.05, p / 1.01, p * 1.01, was_calm=True, cut=0.0015, exit_mult=1.25,
                      band=1.01, horizon_minutes=30, threshold=0.25)
        self.assertEqual(o['p_touch'], 1.0)
        self.assertIsNone(calm.view(None, p, 1, 2, was_calm=False, cut=0.0015, exit_mult=1.25,
                                    band=1.01, horizon_minutes=30, threshold=0.25))


class Tape(unittest.TestCase):
    def test_forming_bar_dropped_and_inverted_pairs_flipped(self):
        now = time.time()
        rows = [[now - (1000 - i) * 300, 0.01, 0.0102, 0.0099, 0.01, 5.0] for i in range(1000)]
        rows.append([now - 60, 0.01, 0.02, 0.005, 0.01, 1.0])       # still forming
        with mock.patch.object(calm.engine, 'curl', lambda *a, **k: {'data': {'attributes': {'ohlcv_list': rows}}}):
            out = calm.tape_5m('P', live_price=100.0)
        ts, o, h, l, c, v = out
        self.assertEqual(len(ts), 1000)
        self.assertAlmostEqual(c[-1], 100.0)
        self.assertAlmostEqual(h[-1], 1 / 0.0099); self.assertAlmostEqual(l[-1], 1 / 0.0102)
        with mock.patch.object(calm.engine, 'curl', lambda *a, **k: {'data': {'attributes': {'ohlcv_list': rows}}}):
            self.assertIsNone(calm.tape_5m('P', live_price=5.0))       # neither way up matches


class Loop(unittest.TestCase):
    def test_calm_off_never_touches_the_tape(self):
        with mock.patch.object(rebalancer.config, 'CALM_ENABLED', False), \
                mock.patch.object(rebalancer, 'tape5', side_effect=AssertionError('fetched')):
            self.assertIsNone(rebalancer.calm_view({}, {'price': 1, 'lowerPrice': 0.9, 'upperPrice': 1.1}))

    def test_budget_counts_the_last_24h(self):
        now = time.time()
        with mock.patch.object(rebalancer.config, 'CALM_MAX_MOVES', 4):
            self.assertEqual(rebalancer.calm_budget_left({'calm_times': [now - 10, now - 90000]}), 3)
            self.assertEqual(rebalancer.calm_budget_left({}), 4)
            self.assertEqual(rebalancer.calm_budget_left({'calm_times': [now] * 9}), 0)

    def test_reopen_band_after_a_tight_exit(self):
        st = {'calm_times': []}
        with mock.patch.object(rebalancer.config, 'CALM_MAX_MOVES', 4), \
                mock.patch.object(rebalancer.config, 'CALM_THRESHOLD', 0.25), \
                mock.patch.object(rebalancer.config, 'CALM_BAND', 1.01):
            self.assertEqual(rebalancer.calm_reopen_band({'calm': True, 'p_touch_fresh': 0.1}, st), 1.01)
            self.assertIsNone(rebalancer.calm_reopen_band({'calm': True, 'p_touch_fresh': 0.3}, st))
            self.assertIsNone(rebalancer.calm_reopen_band({'calm': False, 'p_touch_fresh': 0.1}, st))
            self.assertIsNone(rebalancer.calm_reopen_band(None, st))

    def bal(self, a, b):
        return {'price': 100.0, 'quoteUsd': 1.0, 'balanceA': a, 'balanceB': b, 'nativeSide': 'A',
                'tokenA': 'SOL', 'tokenB': 'USDC'}

    REC = {'token_a': {'address': 'So11111111111111111111111111111111111111112'},
           'token_b': {'address': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'}}

    def test_swap_gate(self):
        calls = []
        chain = lambda *a, **k: (calls.append((a, k)) or ({'sent': True, 'signature': 's'}, None))
        with mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'wallet', lambda p: self.bal(1.0, 100.0)), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None), \
                mock.patch.object(rebalancer.config, 'CAPITAL_USD', 190.0), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05):
            with mock.patch.object(rebalancer.config, 'REBALANCE_SWAP', False):
                self.assertEqual(rebalancer.balance_wallet({}, self.bal(0.06, 300.0), self.REC)['balanceA'], 0.06)
            self.assertEqual(calls, [])
            with mock.patch.object(rebalancer.config, 'REBALANCE_SWAP', True):
                # balanced: nothing
                rebalancer.balance_wallet({}, self.bal(1.1, 110.0), self.REC)
                self.assertEqual(calls, [])
                # one side short of half the capital: one Jupiter rebalance, then a fresh read
                after = rebalancer.balance_wallet({'failures': 0}, self.bal(0.06, 300.0), self.REC)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0][0], 'rebalance'); self.assertEqual(calls[0][1]['dex'], 'jupiter')
                self.assertIn('--execute', calls[0][0])
                self.assertEqual(after['balanceA'], 1.0)

    def test_failed_swap_opens_nothing_and_counts(self):
        state = {'failures': 0}
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (None, 'impact too high')), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.config, 'REBALANCE_SWAP', True), \
                mock.patch.object(rebalancer.config, 'CAPITAL_USD', 190.0):
            self.assertIsNone(rebalancer.balance_wallet(state, self.bal(0.06, 300.0), self.REC))
        self.assertEqual(state['failures'], 1)

    def test_calm_moves_have_their_own_gap_and_budget_under_one_ceiling(self):
        now = time.time()
        sent, halted = [], []
        with mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'halt', lambda r: halted.append(r)), \
                mock.patch.object(rebalancer, 'chain', side_effect=AssertionError('should not trade')), \
                mock.patch.object(rebalancer.config, 'MIN_REBALANCE_GAP', 3600), \
                mock.patch.object(rebalancer.config, 'CALM_MIN_GAP', 600), \
                mock.patch.object(rebalancer.config, 'MAX_REBALANCES_PER_DAY', 6), \
                mock.patch.object(rebalancer.config, 'CALM_MAX_MOVES', 12):
            # a calm move 5 min after a calm move: deferred by the calm gap
            rebalancer.rebalance({'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [now - 300]},
                                 {'positionMint': 'M'}, 'x', calm_move=True)
            self.assertEqual(sent[-1], 'rebalance_deferred')
            # a normal move 20 min after a normal one: deferred by the normal gap
            rebalancer.rebalance({'last_rebalance': now - 1200, 'rebalance_times': [now - 1200]},
                                 {'positionMint': 'M'}, 'x')
            self.assertEqual(sent[-1], 'rebalance_deferred')
            # the hard ceiling halts whatever the kind
            rebalancer.rebalance({'last_rebalance': 0, 'rebalance_times': [now - 9000] * 6,
                                  'calm_times': [now - 9000] * 12}, {'positionMint': 'M'}, 'x', calm_move=True)
            self.assertIn('hard ceiling', halted[-1])
            # the normal ceiling still halts a normal move, but not a calm one
            n = len(halted)
            with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (None, 'stop here')), \
                    mock.patch.object(rebalancer, 'read_status', lambda *a: ({'positionMint': 'M'}, None)), \
                    mock.patch.object(rebalancer, 'save', lambda s: None), \
                    mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                    mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
                rebalancer.rebalance({'last_rebalance': 0, 'rebalance_times': [now - 9000] * 6, 'calm_times': [],
                                      'failures': 0}, {'positionMint': 'M'}, 'x', calm_move=True)
            self.assertEqual(len(halted), n)
            rebalancer.rebalance({'last_rebalance': 0, 'rebalance_times': [now - 9000] * 6, 'calm_times': []},
                                 {'positionMint': 'M'}, 'x')
            self.assertIn('at the ceiling', halted[-1])


class Recovery(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.patch(rebalancer, 'STATE', pathlib.Path(tmp) / 'runtime.json')
        self.patch(rebalancer.time, 'sleep', lambda _: None)
        self.events = []
        self.patch(rebalancer, 'notify', lambda ev, **kw: self.events.append((ev, kw)))
        self.patch(rebalancer, 'notify_book', lambda ev, **kw: self.events.append((ev, kw)))
        for name in ['open_position', 'close_position', 'snapshot', 'record_harvest', 'event']:
            self.patch(rebalancer.db, name, mock.Mock())
        self.patch(rebalancer.config, 'CALM_ENABLED', True)
        self.patch(rebalancer.config, 'CALM_MAX_MOVES', 12)
        self.patch(rebalancer.config, 'CALM_THRESHOLD', .25)
        self.patch(rebalancer.config, 'DEX', 'orca')
        self.patch(rebalancer.config, 'EXECUTE_DEXES', ('orca',))
        self.patch(rebalancer.config, 'CAPITAL_USD', 190)
        self.patch(rebalancer.config, 'MAX_USD', 260)
        self.bal = dict(price=100, quoteUsd=1, balanceA=1.2, balanceB=120,
                        nativeSide='A', tokenA='SOL', tokenB='USDC')
        self.patch(rebalancer, 'wallet', lambda _: dict(self.bal))
        self.patch(rebalancer, 'best_band_for', lambda _: dict(
            band=1.08, price=100, record={}, net_day_pct=.2, rebal_per_day=.1))
        self.fresh = dict(calm=True, bar_age_s=420, p_touch_fresh=.06)
        self.patch(rebalancer, 'calm_view', lambda *a: self.fresh)
        self.sent = []
        def chain(*args, **kwargs):
            self.sent.append(args)
            if args[0] == 'open':
                return dict(positionMint='new', signature='open-sig', depositUsd=200), None
            return {'signature': args[0] + '-sig'}, None
        self.patch(rebalancer, 'chain', chain)
        self.patch(rebalancer, 'read_status', lambda *a: (dict(positionMint='new', positionUsd=200), None))
        self.state = dict(last_rebalance=0, rebalance_times=[], calm_times=[], failures=0)
        self.status = dict(positionMint='old', whirlpool=config.POOL, price=100,
                           inRange=True, liquidity=1, positionUsd=200, feesAccrued_USD=.1)

    def patch(self, obj, name, value):
        return self.stack.enter_context(mock.patch.object(obj, name, value))

    def fail_then_resume(self, fresh=None):
        self.patch(rebalancer, 'balance_wallet', mock.Mock(side_effect=[None, self.bal]))
        rebalancer.rebalance(self.state, self.status, 'calm: tight band re-centred before a touch',
                             band=1.01, calm_move=True)
        self.assertEqual([a[0] for a in self.sent], ['harvest', 'close'])
        self.assertTrue(self.state['pending_reopen']['closed'])
        restarted = rebalancer.load()
        self.assertEqual(restarted['pending_reopen']['band'], 1.01)
        if fresh is not None:
            self.fresh = fresh
        self.assertTrue(rebalancer.resume_reopen(restarted))
        return restarted

    def test_swap_failure_and_restart_reopen_tight_once_without_spending_another_move(self):
        state = self.fail_then_resume()
        self.assertEqual([a[0] for a in self.sent], ['harvest', 'close', 'open'])
        args = self.sent[-1]
        self.assertAlmostEqual(float(args[2]), 100/1.01, places=5)
        self.assertEqual(float(args[3]), 101)
        self.assertEqual(len(state['calm_times']), 1)
        self.assertNotIn('pending_reopen', rebalancer.load())
        opened = next(kw for ev, kw in self.events if ev == 'OPEN')
        self.assertTrue(opened['calm_band'])
        self.assertIsNone(opened['expected_net_day_pct'])
        self.assertIsNone(opened['modelled_rebalances_per_day'])

    def test_recovery_widens_once_if_calm_has_actually_ended(self):
        self.fail_then_resume(dict(calm=False, bar_age_s=420, p_touch_fresh=.4))
        self.assertAlmostEqual(float(self.sent[-1][2]), 100/1.08, places=5)
        self.assertEqual(float(self.sent[-1][3]), 108)

    def test_recovery_waits_for_fresh_data_instead_of_opening_a_temporary_wide_band(self):
        state = self.fail_then_resume(dict(calm=True, bar_age_s=1800, p_touch_fresh=.06))
        self.assertEqual([a[0] for a in self.sent], ['harvest', 'close'])
        self.assertIn('pending_reopen', state)
        self.assertEqual(len(state['calm_times']), 1)


class AuditFixes(unittest.TestCase):
    """2026-09-26 audit of the recovery branch."""

    def test_empty_harvest_does_not_block_the_close(self):
        calls, sent = [], []
        def chain(*a, **k):
            calls.append(a[0])
            if a[0] == 'harvest':
                return {'harvested': 'M', 'signature': None, 'note': 'nothing to claim'}, None
            if a[0] == 'close':
                return {'closed': 'M', 'signature': 'c'}, None
            return None, 'unexpected'
        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [], 'failures': 0}
        with mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'reopen', lambda *a, **k: sent.append('REOPEN')), \
                mock.patch.object(rebalancer.db, 'close_position', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            rebalancer.rebalance(state, {'positionMint': 'M', 'price': 100, 'positionUsd': 190},
                                 'price went above')
        self.assertEqual(calls, ['harvest', 'close'])
        self.assertIn('harvest_skipped', sent); self.assertIn('REOPEN', sent)
        self.assertEqual(state['failures'], 0)

    def test_failed_close_clears_the_reopen_intent(self):
        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [], 'failures': 0}
        def chain(*a, **k):
            if a[0] == 'harvest':
                return {'signature': 'h'}, None
            return None, 'close exploded'
        with mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'read_status', lambda *a: ({'positionMint': 'M'}, None)), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'wallet', lambda p: {}), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None), \
                mock.patch.object(rebalancer.db, 'record_harvest', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'snapshot', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            rebalancer.rebalance(state, {'positionMint': 'M', 'price': 100, 'whirlpool': 'P'},
                                 'calm: tight band', band=1.01, calm_move=True)
        self.assertNotIn('pending_reopen', state)
        self.assertEqual(state['failures'], 1)

    def test_intent_older_than_a_day_is_dropped(self):
        state = {'pending_reopen': {'mint': 'M', 'pool': config.POOL, 'dex': config.DEX, 'band': 1.01,
                                    'reason': 'x', 'started_at': time.time() - 90000, 'closed': True}}
        with mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'reopen', side_effect=AssertionError('replayed')):
            self.assertFalse(rebalancer.resume_reopen(state))
        self.assertNotIn('pending_reopen', state)
