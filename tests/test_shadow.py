"""The fee-to-loss shadow (sql/056): the pause rule's state machine
(lp.shadow.shadow_step), the per-poll read (lp.shadow.r_shadow), the shared
ratio read (lp.pauses.fee_loss_recent), the risk_profile row, the watched
pools in the fee sampler (lp.board.sample_fee_growth) and the config parse."""
import datetime as dt
import time
import unittest
from unittest import mock

import numpy as np
from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import config
import db
from venues import solana_state
import lp.board
import lp.capital
import lp.paths
import lp.pauses
import lp.shadow
from lp import tuning

Q64 = 2 ** 64
NOW = 1_800_000_000.0
SOL = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def sample(g0, g1, sp=1.0, ts=NOW):
    return {'id': int(ts), 'g0': g0, 'g1': g1, 'sqrt_price': int(sp * Q64), 'dec_a': 9, 'dec_b': 6,
            'ts': dt.datetime.fromtimestamp(ts, dt.UTC)}


class Step(unittest.TestCase):
    def test_hysteresis(self):
        step = lp.shadow.shadow_step
        self.assertFalse(step(True, 0.99))              # in, below out: out
        self.assertTrue(step(True, 1.0))                # at the out line: stays in
        self.assertTrue(step(True, 1.2))                # between the lines: unchanged
        self.assertFalse(step(False, 1.2))
        self.assertFalse(step(False, 1.5))              # at the in line: stays out
        self.assertTrue(step(False, 1.51))              # out, above in: in
        self.assertTrue(step(True, None))               # unknown: unchanged
        self.assertFalse(step(False, None))
        self.assertEqual((tuning.R_SHADOW_OUT, tuning.R_SHADOW_IN, tuning.R_SHADOW_HOURS), (1.0, 1.5, 3))

    @settings(max_examples=200, deadline=None)
    @given(st.booleans(), st.floats(min_value=0, max_value=20, allow_nan=False), st.floats(0.1, 2), st.floats(0.1, 2))
    def test_never_flips_inside_the_band_and_always_outside(self, inside, r, lo, gap):
        hi = lo + gap
        out = lp.shadow.shadow_step(inside, r, lo, hi)
        if lo <= r <= hi:
            self.assertEqual(out, inside)
        elif r < lo:
            self.assertFalse(out)
        else:
            self.assertTrue(out)


class Read(unittest.TestCase):
    """fee_loss_recent: the hot pause's ratio, shared with the shadow."""

    def bars(self, t0, n=40, step=0.001):
        ts = np.array([t0 + 300 * i for i in range(n)], dtype=float)
        close = 100.0 * np.exp(np.cumsum(np.full(n, step)))
        return ts, close, close, close, close, np.zeros(n)

    def test_ratio_rounded_and_coverage(self):
        first, last = sample(0, 0, 0.5, NOW - 3 * 3600 + 60), sample(2 * Q64, 3 * Q64, 0.5, NOW)
        bars = self.bars(NOW - 3 * 3600 + 60)
        want = calm.fee_loss_ratio(solana_state.fee_yield(first, last, 120.0, 1.0), bars[0], bars[4],
                                   first['ts'].timestamp(), last['ts'].timestamp())
        with mock.patch.object(db, 'fee_state_span', lambda pool, hours: (first, last, 3 * 3600 - 60)), \
                mock.patch.object(db, 'tape_load', lambda pool, t0: bars):
            got = lp.pauses.fee_loss_recent('P', 120.0, 1.0, 3)
        self.assertEqual(got, round(want, 3))
        self.assertGreater(got, 0)
        with mock.patch.object(db, 'fee_state_span', lambda pool, hours: (first, last, 0.5 * 3600)), \
                mock.patch.object(db, 'tape_load', lambda pool, t0: bars):
            self.assertIsNone(lp.pauses.fee_loss_recent('P', 120.0, 1.0, 3))      # too little coverage
        with mock.patch.object(db, 'fee_state_span', lambda pool, hours: None):
            self.assertIsNone(lp.pauses.fee_loss_recent('P', 120.0, 1.0, 3))
        with mock.patch.object(db, 'fee_state_span', lambda pool, hours: (first, last, 3 * 3600 - 60)), \
                mock.patch.object(db, 'tape_load', lambda pool, t0: None):
            self.assertIsNone(lp.pauses.fee_loss_recent('P', 120.0, 1.0, 3))

    def test_hot_pause_view_still_uses_it(self):
        with mock.patch.object(config, 'HOT_PAUSE_EVERY_HOT', False), \
                mock.patch.object(config, 'HOT_PAUSE_HOT_PCT', 2.0), \
                mock.patch.object(config, 'HOT_PAUSE_FG_THRESHOLD', 0.8), \
                mock.patch.object(lp.pauses, 'fee_loss_recent', lambda pool, a, b, hours: 0.5):
            v = lp.pauses.hot_pause_view('P', 120.0, 1.0, 1.03)
        self.assertEqual((v['hot'], v['ratio'], v['bad']), (True, 0.5, True))


class Poll(unittest.TestCase):
    def run_polls(self, ratios, state=None):
        state = state if state is not None else {}
        events = []
        rv = {'mode': 'WARM'}
        with mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(db, 'event', lambda k, d: events.append((k, d))), \
                mock.patch.object(config, 'POOL', 'HELD'):
            for r in ratios:
                with mock.patch.object(lp.pauses, 'fee_loss_recent', lambda pool, a, b, hours, r=r: r):
                    got = lp.shadow.r_shadow(state, {'price': 100.0, 'quoteUsd': 1.0, 'whirlpool': 'HELD'}, rv)
        return got, rv, state, events

    def test_flips_write_one_event_each_and_the_view_carries_the_rule(self):
        got, rv, state, events = self.run_polls([2.0, 1.4, 0.9, 1.2, 1.6])
        self.assertEqual(got, 1.6)
        self.assertEqual([k for k, _ in events], ['R_SHADOW', 'R_SHADOW'])
        self.assertIn('OUT: fees 0.9x', events[0][1]); self.assertIn('IN: fees 1.6x', events[1][1])
        self.assertIn('nothing traded', events[0][1])
        self.assertEqual((rv['fee_loss_3h'], rv['shadow_in']), (1.6, True))
        self.assertEqual((rv['shadow']['hours'], rv['shadow']['out_below'], rv['shadow']['in_above']), (3, 1.0, 1.5))
        self.assertTrue(state['r_shadow']['in'])

    def test_starts_in_and_unknown_keeps_state(self):
        got, rv, state, events = self.run_polls([None])
        self.assertIsNone(got); self.assertEqual(events, [])
        self.assertEqual((rv['fee_loss_3h'], rv['shadow_in']), (None, True))
        got, rv, state, events = self.run_polls([0.5, None], state={'r_shadow': {'in': True, 'since': 1}})
        self.assertEqual(len(events), 1); self.assertFalse(rv['shadow_in'])

    def test_no_view_no_quote_or_a_failed_read_leaves_the_view(self):
        self.assertIsNone(lp.shadow.r_shadow({}, {'price': 1, 'quoteUsd': 1}, None))
        rv = {'mode': 'CALM'}
        self.assertIsNone(lp.shadow.r_shadow({}, {'price': 1}, rv)); self.assertNotIn('shadow_in', rv)
        told = []
        with mock.patch.object(lp.pauses, 'fee_loss_recent', mock.Mock(side_effect=RuntimeError('rpc'))), \
                mock.patch.object(lp.shadow.books, 'notify', lambda k, **kw: told.append((k, kw))), \
                mock.patch.object(config, 'POOL', 'HELD'):
            self.assertIsNone(lp.shadow.r_shadow({}, {'price': 1, 'quoteUsd': 1}, rv))
        self.assertEqual(told[0][0], 'r_shadow_unread'); self.assertNotIn('shadow_in', rv)

    def test_risk_row_carries_the_shadow(self):
        row = db.risk_row({'mode': 'WARM', 'fee_loss_3h': 1.234, 'shadow_in': False}, {}, {})
        self.assertEqual((row['fee_loss_3h'], row['shadow_in']), (1.234, False))
        self.assertIn('fee_loss_3h', db.RISK_COLUMNS); self.assertIn('shadow_in', db.RISK_COLUMNS)
        self.assertEqual(db.risk_row(None, None, None)['shadow_in'], None)


class Watch(unittest.TestCase):
    def test_watched_pools_are_read_and_stored_but_never_ranked(self):
        calls, stored = [], []
        cands = [('raydium-clmm', 'HELD', None), ('raydium-clmm', 'R2', {'pair': 'SOL/USDC'})]
        watch = (('orca', 'W1'), ('raydium-clmm', 'R2'), ('meteora-dlmm', 'M9'), ('orca', 'W2'))
        states = {'HELD': {'s': 1}, 'R2': {'s': 2}, 'W1': {'s': 3}}
        with mock.patch.object(lp.board, 'venue_candidates', lambda: cands), \
                mock.patch.object(config, 'VENUE_WATCH', watch), \
                mock.patch.object(config, 'CAPS', {'venues': True}), \
                mock.patch.object(config, 'VENUE_SAMPLE_S', 600), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(solana_state, 'fee_states', lambda pools: calls.append(list(pools)) or states), \
                mock.patch.object(db, 'record_fee_state', lambda d, a, s: stored.append((d, a))):
            lp.board.sample_fee_growth({'last_fee_sample': time.time() - 700})
            ranked = {a for _, a, _ in lp.board.venue_candidates()}
        # one RPC batch: the candidates, then the watched pools not already in it, on a known layout
        self.assertEqual(calls, [[('raydium-clmm', 'HELD'), ('raydium-clmm', 'R2'), ('orca', 'W1'), ('orca', 'W2')]])
        self.assertEqual(stored, [('raydium-clmm', 'HELD'), ('raydium-clmm', 'R2'), ('orca', 'W1')])   # W2: no state
        self.assertEqual(ranked, {'HELD', 'R2'})                                                    # ranking untouched

    def test_config_parse(self):
        self.assertEqual(config._watch(['orca:A', 'bad', 'raydium-clmm:B:C', 7, None]), (('orca', 'A'), ('raydium-clmm', 'B:C')))
        self.assertEqual(config._watch(None), ())
        self.assertIsInstance(config.VENUE_WATCH, tuple)


if __name__ == '__main__':
    unittest.main()
