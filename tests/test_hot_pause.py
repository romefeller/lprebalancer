"""The pause in bad HOT moments (sql/029): the fee / in-band-loss ratio
(solana_state.fee_yield, calm.fee_loss_ratio), the pause's state machine
(calm.hot_pause_step), the signal (rebalancer.hot_pause_view), the close
(rebalancer.hot_pause) and the wait (rebalancer.hot_paused)."""
import pathlib
import datetime as dt
import math
import time
import unittest
from unittest import mock

import numpy as np
from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import config
from venues import solana_state
import db
import rebalancer
import lp.loop
import lp.moves
import lp.paths
import lp.pauses
import health

Q64 = 2 ** 64
NOW = 1_800_000_000.0


def sample(g0, g1, sp=1.0, dec_a=9, dec_b=6, ts=NOW):
    return {'g0': g0, 'g1': g1, 'sqrt_price': int(sp * Q64), 'dec_a': dec_a, 'dec_b': dec_b,
            'ts': dt.datetime.fromtimestamp(ts, dt.UTC)}


class FeeYield(unittest.TestCase):
    def test_exact_per_full_range_dollar(self):
        # dg0 = 2 raw A per raw L, dg1 = 3 raw B per raw L, sqrt price 0.5 raw
        a, b = sample(0, 0, 0.5), sample(2 * Q64, 3 * Q64, 0.5)
        fee = 2 / 1e9 * 120.0 + 3 / 1e6 * 1.0
        full = 2 * 0.5 / 1e6 * 1.0
        self.assertAlmostEqual(solana_state.fee_yield(a, b, 120.0, 1.0), fee / full)

    def test_both_counters_are_differences_and_both_prices_count(self):
        a, b = sample(5 * Q64, 4 * Q64, 0.5), sample(7 * Q64, 10 * Q64, 0.5)
        fee = 2 / 1e9 * 120.0 + 6 / 1e6 * 2.0
        self.assertAlmostEqual(solana_state.fee_yield(a, b, 120.0, 2.0), fee / (2 * 0.5 / 1e6 * 2.0))

    def test_counters_wrap_at_u128(self):
        a, b = sample(2 ** 128 - Q64, 0), sample(Q64, 0)
        self.assertAlmostEqual(solana_state.fee_yield(a, b, 1e9, 1e6), (2 * 1.0) / (2 * 1.0))

    def test_unpriced_or_empty_is_none(self):
        a, b = sample(0, 0), sample(Q64, Q64)
        self.assertIsNone(solana_state.fee_yield(a, b, 0.0, 1.0))
        self.assertIsNone(solana_state.fee_yield(a, b, 1.0, None))
        self.assertIsNone(solana_state.fee_yield(a, sample(Q64, Q64, 0.0), 1.0, 1.0))

    def test_matches_the_position_fee_formula(self):
        # A position of L raw liquidity earns L * dg; its full-range value is 2 L sqrt(p).
        L, sp = 5e9, 11.0
        a, b = sample(0, 0, sp), sample(7 * Q64 // 10 ** 6, 9 * Q64 // 10 ** 4, sp)
        fee_pos = L * (int(b['g0']) / Q64 / 1e9 * 120.0 + int(b['g1']) / Q64 / 1e6)
        full_pos = 2 * L * sp / 1e6
        self.assertAlmostEqual(solana_state.fee_yield(a, b, 120.0, 1.0), fee_pos / full_pos, places=12)


class FeeLossRatio(unittest.TestCase):
    def bars(self, closes, t0=NOW):
        return np.arange(len(closes)) * 300.0 + t0, np.asarray(closes, dtype=float)

    def test_exact(self):
        ts, c = self.bars([100.0, 101.0, 100.0, 100.5])
        g = sum(math.log(c[i + 1] / c[i]) ** 2 for i in range(3)) / 8
        self.assertAlmostEqual(calm.fee_loss_ratio(0.002, ts, c, NOW, NOW + 1200), 0.002 / g)

    def test_only_bars_inside_the_window(self):
        ts, c = self.bars([50.0, 100.0, 101.0, 100.0, 300.0])
        g = (math.log(1.01) ** 2 + math.log(100 / 101) ** 2) / 8
        self.assertAlmostEqual(calm.fee_loss_ratio(1.0, ts, c, NOW + 300, NOW + 1200), 1 / g)
        # a bar that ends after t1 is out; one that starts at t0 is in
        self.assertAlmostEqual(calm.fee_loss_ratio(1.0, ts, c, NOW + 300, NOW + 1199, min_cover=0.5),
                               1 / (math.log(1.01) ** 2 / 8))

    def test_an_empty_or_reversed_window_is_none(self):
        ts, c = self.bars([100.0, 101.0, 100.0])
        self.assertIsNone(calm.fee_loss_ratio(1.0, ts, c, NOW + 900, NOW, min_cover=0.0))
        self.assertIsNone(calm.fee_loss_ratio(1.0, ts, c, NOW, NOW, min_cover=0.0))

    def test_coverage(self):
        ts, c = self.bars([100.0, 101.0, 100.0, 101.0])      # 4 bars
        self.assertIsNotNone(calm.fee_loss_ratio(1.0, ts, c, NOW, NOW + 1500))   # 4 >= 0.8 * 5
        self.assertIsNone(calm.fee_loss_ratio(1.0, ts, c, NOW, NOW + 1800))      # 4 < 0.8 * 6
        self.assertIsNotNone(calm.fee_loss_ratio(1.0, ts, c, NOW, NOW + 1800, min_cover=0.6))

    def test_none_cases(self):
        ts, c = self.bars([100.0, 101.0, 100.0])
        self.assertIsNone(calm.fee_loss_ratio(None, ts, c, NOW, NOW + 900))
        self.assertIsNone(calm.fee_loss_ratio(1.0, ts, c, NOW + 900, NOW + 900))
        self.assertIsNone(calm.fee_loss_ratio(1.0, ts[:1], c[:1], NOW, NOW + 300))   # one bar: no return
        flat_ts, flat = self.bars([100.0, 100.0, 100.0])
        self.assertIsNone(calm.fee_loss_ratio(1.0, flat_ts, flat, NOW, NOW + 900))  # nothing moved

    @settings(max_examples=60, deadline=None)
    @given(st.floats(1e-6, 1.0), st.floats(0.1, 10.0), st.integers(3, 80), st.integers(0, 10_000))
    def test_property_linear_in_fees_and_scale_free_in_price(self, y, scale, n, seed):
        rng = np.random.default_rng(seed)
        c = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
        ts = np.arange(n) * 300.0 + NOW
        r = calm.fee_loss_ratio(y, ts, c, NOW, NOW + n * 300)
        self.assertAlmostEqual(calm.fee_loss_ratio(2 * y, ts, c, NOW, NOW + n * 300), 2 * r, delta=1e-9 * r)
        self.assertAlmostEqual(calm.fee_loss_ratio(y, ts, c * scale, NOW, NOW + n * 300), r, delta=1e-6 * r)


class Step(unittest.TestCase):
    def step(self, paused, bad, now=NOW):
        return calm.hot_pause_step(paused, bad, now, resume_s=1800, max_s=43200)

    def test_table(self):
        self.assertEqual(self.step(None, True), 'pause')
        self.assertIsNone(self.step(None, False))
        p = {'since': NOW - 3600, 'last_bad': NOW - 1800}
        self.assertEqual(self.step(p, False), 'resume')                      # clear exactly 30 min
        self.assertIsNone(self.step(p, False, NOW - 1))
        self.assertIsNone(self.step(p, True))                                # still bad
        old = {'since': NOW - 43200, 'last_bad': NOW}
        self.assertEqual(self.step(old, True), 'resume')                     # the limit, bad or not
        self.assertIsNone(self.step(old, True, NOW - 1))


def status(**kw):
    s = {'positionMint': 'M', 'whirlpool': 'P', 'price': 120.0, 'quoteUsd': 1.0, 'inRange': True,
         'lowerPrice': 118.0, 'upperPrice': 122.0}
    s.update(kw)
    return s


class View(unittest.TestCase):
    def view(self, choice=1.03, span=True, secs=6 * 3600, closes=None, ratio_y=None, boom=None):
        reads, told = [], []
        first, last = sample(0, 0, ts=NOW - secs), sample(Q64, Q64, ts=NOW)
        c = closes if closes is not None else 100.0 * np.exp(np.cumsum(np.tile([0.004, -0.004], 36)))
        bars = (np.arange(len(c)) * 300.0 + NOW - secs, None, None, None, np.asarray(c), None)

        def span_f(pool, hours):
            reads.append(('span', pool, hours))
            if boom:
                raise boom
            return (first, last, secs) if span else None

        def tape_f(pool, since):
            reads.append(('tape', pool, since)); return bars
        with mock.patch.object(db, 'fee_state_span', span_f), \
                mock.patch.object(db, 'tape_load', tape_f), \
                mock.patch.object(solana_state, 'fee_yield',
                                  lambda f, l, a, b: (reads.append(('yield', a, b)) or ratio_y)), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append(ev)), \
                mock.patch.object(config, 'HOT_PAUSE_HOT_PCT', 2.0), \
                mock.patch.object(config, 'HOT_PAUSE_FG_HOURS', 6.0), \
                mock.patch.object(config, 'HOT_PAUSE_FG_THRESHOLD', 0.8):
            out = lp.pauses.hot_pause_view('P', 120.0, 1.0, choice)
        return out, reads, told, c

    def g(self, c):
        return float(np.sum(np.diff(np.log(np.asarray(c))) ** 2)) / 8   # every bar ends by t1

    def test_not_hot_reads_nothing(self):
        out, _, _, _ = self.view(choice=1.0201, ratio_y=1e-12)
        self.assertTrue(out['hot'])
        for k in (None, 1.01, 1.02):
            out, reads, _, _ = self.view(choice=k)
            self.assertFalse(out['bad']); self.assertEqual(reads, [])
            self.assertEqual(out['hot'], None if k is None else False)

    def test_bad_and_good(self):
        _, _, _, c = self.view()
        g = self.g(c)
        out, reads, _, _ = self.view(ratio_y=0.79 * g)
        self.assertTrue(out['hot']); self.assertTrue(out['bad']); self.assertAlmostEqual(out['ratio'], 0.79)
        self.assertEqual(reads[0], ('span', 'P', 6))
        self.assertEqual(reads[1], ('tape', 'P', NOW - 6 * 3600))
        self.assertEqual(reads[2], ('yield', 120.0, 1.0))
        out, _, _, _ = self.view(ratio_y=0.8 * g)
        self.assertFalse(out['bad']); self.assertAlmostEqual(out['ratio'], 0.8)

    def test_short_or_missing_history_is_not_bad(self):
        _, _, _, c = self.view()
        y = 0.1 * self.g(c)
        out, _, told, _ = self.view(span=False, ratio_y=y)
        self.assertFalse(out['bad']); self.assertEqual(told, [])
        out, reads, told, _ = self.view(secs=0.8 * 6.0 * 3600, ratio_y=y)     # exactly the minimum: read
        self.assertEqual(told, []); self.assertEqual([r[0] for r in reads], ['span', 'tape', 'yield'])
        out, reads, _, _ = self.view(secs=17279, ratio_y=y)
        self.assertFalse(out['bad']); self.assertEqual([r[0] for r in reads], ['span'])
        self.assertTrue(self.view(secs=17281, ratio_y=y, closes=c[:58])[0]["bad"])
        self.assertFalse(self.view(ratio_y=None)[0]['bad'])                  # unpriced fees

    def test_read_error_is_not_bad_and_is_told(self):
        out, _, told, _ = self.view(ratio_y=0.0, boom=RuntimeError('db'))
        self.assertFalse(out['bad']); self.assertIsNone(out['ratio']); self.assertEqual(told, ['hot_pause_unread'])

    def test_no_tape_is_not_bad(self):
        told = []
        with mock.patch.object(db, 'fee_state_span',
                               lambda p, hours: (sample(0, 0, ts=NOW - 21600), sample(1, 1), 21600)), \
                mock.patch.object(db, 'tape_load', lambda p, s: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append(ev)), \
                mock.patch.object(config, 'HOT_PAUSE_FG_HOURS', 6.0):
            out = lp.pauses.hot_pause_view('P', 1.0, 1.0, 1.05)
        self.assertFalse(out['bad']); self.assertIsNone(out['ratio']); self.assertEqual(told, [])

    def test_fractional_hours_ask_whole_hours(self):
        seen = []
        with mock.patch.object(db, 'fee_state_span', lambda p, hours: seen.append(hours)), \
                mock.patch.object(config, 'HOT_PAUSE_FG_HOURS', 0.4):
            lp.pauses.hot_pause_view('P', 1.0, 1.0, 1.05)
        self.assertEqual(seen, [1])


class Pause(unittest.TestCase):
    def go(self, view=None, rv=None, st=None, closed=True, budget=5, quote=1.0, bal_ok=True, allowed=True,
           now=NOW, rec_boom=False, pools=frozenset()):
        calls, saved = [], []
        state = st if st is not None else {}
        v = view or {'hot': True, 'ratio': 0.5, 'bad': True}

        def reb(s, stt, why, **kw):
            calls.append(('rebalance', dict(s.get('hot_pause') or {}), kw))

        def record():
            if rec_boom:
                raise RuntimeError('x')
            return {'rec': 1}
        with mock.patch.object(lp.pauses, 'hot_pause_view', lambda *a: (calls.append(('view',) + a) or v)), \
                mock.patch.object(lp.moves, 'rebalance', reb), \
                mock.patch.object(db, 'position_closed', lambda m: (calls.append(('closed?', m)) or closed)), \
                mock.patch.object(lp.capital, 'wallet',
                                  lambda p: ({'balanceA': 1.0, 'price': 120.0} if bal_ok else {})), \
                mock.patch.object(lp.capital, 'pool_record', record), \
                mock.patch.object(lp.swaps, 'balance_wallet',
                                  lambda s, b, r, share_a=None: calls.append(('swap', r, share_a))), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: budget), \
                mock.patch.object(lp.regime, 'voluntary_move_allowed', lambda s: allowed), \
                mock.patch.object(lp.capital, 'position_usd', lambda st: 210.0), \
                mock.patch.object(lp.books, 'notify_book', lambda ev, **kw: calls.append(('book', ev))), \
                mock.patch.object(db, 'event', lambda *a: calls.append(('event', a[0]))), \
                mock.patch.object(lp.paths, 'save', lambda s: saved.append(dict(s))), \
                mock.patch.object(time, 'time', lambda: now), \
                mock.patch.object(config, 'HOT_PAUSE_COOLDOWN_S', 3600), \
                mock.patch.object(config, 'HOT_PAUSE_ENABLED', True), \
                mock.patch.object(config, 'HOT_PAUSE_POOLS', frozenset(pools)), \
                mock.patch.object(config, 'MACRO_PAUSE_ENABLED', False), \
                mock.patch.object(config, 'POOL', 'P'):
            r = lp.pauses.hot_pause(state, status(quoteUsd=quote),
                                     rv if rv is not None else {'choice': 1.03, 'choice_pct': 3.0, 'stale': False})
        return r, calls, state, saved

    def test_bad_closes_and_waits_half_and_half(self):
        r, calls, state, saved = self.go()
        self.assertIs(r, True)
        self.assertEqual(calls[0], ('view', 'P', 120.0, 1.0, 1.03))
        self.assertEqual([c[0] for c in calls[1:]], ['book', 'event', 'rebalance', 'closed?', 'swap'])
        reb = calls[3]
        self.assertEqual(reb[2], {'calm_move': True, 'exit_move': True, 'close_only': True})
        self.assertEqual(reb[1]['since'], NOW)                        # the pause is saved before the close
        self.assertEqual(reb[1]['mint'], 'M'); self.assertEqual(reb[1]['withdraw_usd'], 210.0)
        self.assertIs(reb[1]['booked'], False); self.assertIs(reb[1]['swapped'], False)
        self.assertTrue(reb[1]['reason'].startswith('hot pause: +/-3.0% chosen and fees 0.5x'))
        self.assertEqual(saved[0]['hot_pause']['pool'], 'P')
        self.assertEqual(calls[4], ('closed?', 'M'))
        self.assertEqual(calls[5], ('swap', {'rec': 1}, 0.5))
        hp = state['hot_pause']
        self.assertEqual({k: hp[k] for k in ('since', 'last_bad', 'pool', 'ratio', 'told', 'booked', 'swapped')},
                         {'since': NOW, 'last_bad': NOW, 'pool': 'P', 'ratio': 0.5, 'told': NOW,
                          'booked': True, 'swapped': True})
        self.assertIs(saved[-1]['hot_pause']['swapped'], True)        # the swap is marked before it runs

    def test_a_close_that_did_not_land_waits_for_nothing(self):
        for closed in (False, None):
            r, calls, state, _ = self.go(closed=closed)
            self.assertIs(r, True); self.assertNotIn('hot_pause', state)
            self.assertNotIn('swap', [c[0] for c in calls])

    def test_an_unreadable_wallet_skips_the_swap(self):
        r, calls, state, _ = self.go(bal_ok=False)
        self.assertIs(r, True); self.assertIs(state['hot_pause']['swapped'], True)
        self.assertNotIn('swap', [c[0] for c in calls])

    def test_unavailable_record_still_swaps_with_none(self):
        r, calls, _, _ = self.go(rec_boom=True)
        self.assertEqual(calls[-1], ('swap', None, 0.5))

    def test_not_bad_stale_unpriced_no_budget_or_gap_does_nothing(self):
        for kw in ({'view': {'hot': True, 'ratio': 0.9, 'bad': False}},
                   {'rv': {'choice': 1.03, 'stale': True}}, {'rv': {}}, {'quote': None}, {'budget': 0},
                   {'allowed': False}):
            r, calls, state, _ = self.go(**kw)
            self.assertIs(r, False, kw)
            self.assertNotIn('rebalance', [c[0] for c in calls], kw)
            self.assertNotIn('hot_pause', state, kw)

    def test_no_pause_within_the_cooldown_of_a_resume(self):
        r, calls, _, _ = self.go(st={'hot_pause_resumed': NOW - 3599})
        self.assertIs(r, False); self.assertEqual(calls, [])
        r, calls, _, _ = self.go(st={'hot_pause_resumed': NOW - 3600})
        self.assertIs(r, True)

    def test_a_held_band_clears_a_left_over_pause(self):
        _, _, state, _ = self.go(view={'hot': False, 'ratio': None, 'bad': False},
                                 st={'hot_pause': {'since': 1, 'last_bad': 1, 'pool': 'P'}})
        self.assertNotIn('hot_pause', state)

    def test_only_the_listed_pools_pause(self):
        # sql/033: the swing pauses its SOL/USDC hours, never its DJT ones.
        for pools, paused in ((frozenset(), True), ({'P'}, True), ({'P', 'Q'}, True), ({'Q'}, False)):
            r, calls, state, _ = self.go(pools=pools)
            self.assertIs(r, paused, pools)
            self.assertEqual('rebalance' in [c[0] for c in calls], paused, pools)
            if not paused:
                self.assertEqual(calls, [], pools)                    # an unguarded pool reads nothing
                self.assertNotIn('hot_pause', state)

    def test_the_list_is_checked_against_the_band_pool(self):
        for st_kw, pools, want in (({'whirlpool': 'W'}, {'W'}, True), ({'whirlpool': 'W'}, {'P'}, False),
                                   ({'whirlpool': None}, {'P'}, True)):
            seen = []
            with mock.patch.object(lp.pauses, 'hot_pause_view', lambda *a: (seen.append(a[0]) or {'bad': False})), \
                    mock.patch.object(config, 'HOT_PAUSE_ENABLED', True), \
                    mock.patch.object(config, 'HOT_PAUSE_POOLS', frozenset(pools)), \
                    mock.patch.object(config, 'MACRO_PAUSE_ENABLED', False), \
                    mock.patch.object(config, 'POOL', 'P'):
                lp.pauses.hot_pause({}, status(**st_kw), {'choice': 1.03})
            self.assertEqual(bool(seen), want, (st_kw, pools))

    def test_one_move_left_is_enough(self):
        self.assertIs(self.go(budget=1)[0], True)

    def test_the_status_pool_or_the_profile_pool(self):
        seen = []
        for st_kw, want in (({'whirlpool': 'W'}, 'W'), ({'whirlpool': None}, 'P')):
            with mock.patch.object(lp.pauses, 'hot_pause_view', lambda *a: (seen.append(a[0]) or {'bad': False})), \
                    mock.patch.object(config, 'HOT_PAUSE_ENABLED', True), \
                    mock.patch.object(config, 'MACRO_PAUSE_ENABLED', False), \
                    mock.patch.object(config, 'POOL', 'P'):
                self.assertIs(lp.pauses.hot_pause({}, status(**st_kw), {'choice': 1.03}), False)
            self.assertEqual(seen[-1], want)

    def test_token_a_usd_is_the_pool_price_times_the_quote(self):
        _, calls, _, _ = self.go(quote=0.5)
        self.assertEqual(calls[0], ('view', 'P', 60.0, 0.5, 1.03))


class Paused(unittest.TestCase):
    def go(self, p, view=None, enabled=True, pool='P', now=NOW, bal=None, choice=1.03, closed=True, pools=frozenset()):
        told, events, sampled, books = [], [], [], []
        state = {'hot_pause': dict(p), 'calm_times': [NOW - 99999]}
        v = view or {'hot': True, 'ratio': 0.5, 'bad': True}
        b = bal if bal is not None else {'balanceA': 1.0, 'price': 120.0, 'quoteUsd': 1.0}
        with mock.patch.object(config, 'HOT_PAUSE_ENABLED', enabled), mock.patch.object(config, 'POOL', pool), \
                mock.patch.object(config, 'HOT_PAUSE_POOLS', frozenset(pools)), \
                mock.patch.object(config, 'HOT_PAUSE_RESUME_S', 1800), mock.patch.object(config, 'HOT_PAUSE_MAX_S', 43200), \
                mock.patch.object(lp.board, 'sample_fee_growth', lambda s: sampled.append('fg')), \
                mock.patch.object(lp.books, 'daily_report', lambda s: sampled.append('daily')), \
                mock.patch.object(lp.housekeeping, 'run_audits', lambda s: sampled.append('audit')), \
                mock.patch.object(lp.pauses, 'hot_pause_swap', lambda s, pp: (books.append('swap'), pp.update(swapped=True))), \
                mock.patch.object(db, 'position_closed', lambda m: closed), \
                mock.patch.object(db, 'close_position', lambda *a: books.append(('close',) + a)), \
                mock.patch.object(lp.harvest, 'band_profile', lambda *a: books.append(('profile',) + a)), \
                mock.patch.object(lp.capital, 'wallet', lambda pl: b), \
                mock.patch.object(lp.regime, 'regime_choice_now', lambda pl, px: choice), \
                mock.patch.object(lp.pauses, 'hot_pause_view', lambda *a: (told.append(('view',) + a) or v)), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append((ev, kw))), \
                mock.patch.object(db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(time, 'time', lambda: now):
            r = lp.pauses.hot_paused(state)
        self.books = books
        return r, state, told, events, sampled

    P = {'since': NOW - 3600, 'last_bad': NOW - 600, 'pool': 'P', 'told': NOW - 60, 'booked': True, 'swapped': True,
         'mint': 'M', 'withdraw_usd': 210.0, 'reason': 'hot pause: x'}

    def test_still_bad_keeps_waiting_and_moves_last_bad(self):
        r, state, told, _, sampled = self.go(self.P)
        self.assertIs(r, True); self.assertEqual(state['hot_pause']['last_bad'], NOW)
        self.assertEqual(sampled, ['fg', 'daily', 'audit'])
        self.assertEqual(told, [('view', 'P', 120.0, 1.0, 1.03)])          # told 1 min ago: quiet
        self.assertEqual(self.books, [])
        self.assertNotIn('hot_pause_resumed', state)

    def test_clear_for_resume_reopens(self):
        p = dict(self.P, last_bad=NOW - 1800)
        r, state, told, events, _ = self.go(p, view={'hot': False, 'ratio': None, 'bad': False})
        self.assertIs(r, False); self.assertNotIn('hot_pause', state); self.assertEqual(state['hot_pause_resumed'], NOW)
        self.assertEqual(told[-1][0], 'HOT_RESUME'); self.assertEqual(told[-1][1]['paused_minutes'], 60)
        self.assertIn('clear for 30 min', told[-1][1]['reason']); self.assertEqual(events[0][0], 'HOT_RESUME')

    def test_clear_but_not_long_enough_waits(self):
        p = dict(self.P, last_bad=NOW - 1799)
        r, state, _, _, _ = self.go(p, view={'hot': False, 'ratio': None, 'bad': False})
        self.assertIs(r, True); self.assertEqual(state['hot_pause']['last_bad'], NOW - 1799)

    def test_the_limit_resumes_even_when_bad(self):
        p = dict(self.P, since=NOW - 43200)
        r, state, told, _, _ = self.go(p)
        self.assertIs(r, False); self.assertIn('limit', told[-1][1]['reason'])
        self.assertEqual(state['hot_pause_resumed'], NOW)

    def test_switch_off_or_pool_change_resumes_at_once(self):
        for kw in ({'enabled': False}, {'pool': 'Q'}):
            r, state, told, _, sampled = self.go(self.P, **kw)
            self.assertIs(r, False, kw); self.assertNotIn('hot_pause', state, kw)
            self.assertEqual(told[0][0], 'HOT_RESUME', kw); self.assertEqual(sampled, [], kw)
            self.assertEqual(state['hot_pause_resumed'], NOW, kw)

    def test_a_pool_off_the_list_resumes_at_once(self):
        # The swing switched to DJT during a SOL/USDC pause: DJT is not
        # guarded, so the pause ends and the loop opens there.
        for pools, pool, waits in (({'P'}, 'P', True), ({'P'}, 'Q', False), ({'Q'}, 'Q', False),
                                   (frozenset(), 'P', True)):
            r, state, told, _, _ = self.go(dict(self.P, pool='P'), pools=pools, pool=pool)
            self.assertIs(r, waits, (pools, pool))
            self.assertEqual('hot_pause' in state, waits, (pools, pool))
            if not waits:
                self.assertEqual(told[0][0], 'HOT_RESUME')

    def test_tells_every_half_hour(self):
        p = dict(self.P, told=NOW - 1800)
        r, state, told, _, _ = self.go(p)
        self.assertIs(r, True); self.assertEqual(told[-1][0], 'hot_paused'); self.assertEqual(state['hot_pause']['told'], NOW)
        self.assertEqual(told[-1][1]['clear_minutes'], 0)
        r, state, told, _, _ = self.go(dict(self.P, told=NOW - 1799))
        self.assertNotIn('hot_paused', [t[0] for t in told])

    def test_unpriced_wallet_counts_as_clear(self):
        p = dict(self.P, last_bad=NOW - 1800)
        for b in ({'balanceA': 1.0, 'price': 120.0, 'quoteUsd': None}, {'balanceA': 1.0, 'quoteUsd': 1.0}):
            r, state, told, _, _ = self.go(p, bal=b)
            self.assertIs(r, False, b); self.assertNotIn('view', [t[0] for t in told], b)

    def test_pool_price_times_quote(self):
        r, state, told, _, _ = self.go(self.P, bal={'balanceA': 1.0, 'price': 100.0, 'quoteUsd': 2.0}, choice=None)
        self.assertEqual(told[0], ('view', 'P', 200.0, 2.0, None))

    def test_a_stop_before_the_books_books_the_close_once(self):
        p = dict(self.P, booked=False)
        r, state, _, _, _ = self.go(p, closed=False)
        self.assertEqual(self.books, [('close', 'M', None, 210.0), ('profile', 'M', 'rebalance', 'hot pause: x')])
        self.assertIs(state['hot_pause']['booked'], True)
        self.assertEqual(state['calm_times'], [NOW - 99999, NOW - 3600])
        for closed in (True, None):                                   # already booked, or no such row
            r, state, _, _, _ = self.go(p, closed=closed)
            self.assertEqual(self.books, [], closed); self.assertIs(state['hot_pause']['booked'], True)
            self.assertEqual(state['calm_times'], [NOW - 99999])
        r, state, _, _, _ = self.go(dict(p, mint=None), closed=False)
        self.assertEqual(self.books, [])

    def test_a_stop_before_the_swap_swaps_once(self):
        r, state, _, _, _ = self.go(dict(self.P, swapped=False))
        self.assertEqual(self.books, ['swap']); self.assertIs(state['hot_pause']['swapped'], True)
        self.go(self.P)
        self.assertEqual(self.books, [])


class Swap(unittest.TestCase):
    def test_marks_then_swaps_half_and_half(self):
        calls, saved = [], []
        state = {'hot_pause': {'swapped': False}}
        with mock.patch.object(lp.paths, 'save', lambda s: saved.append(dict(s['hot_pause']))), \
                mock.patch.object(lp.capital, 'wallet', lambda p: {'balanceA': 1.0}), \
                mock.patch.object(lp.capital, 'pool_record', lambda: {'r': 1}), \
                mock.patch.object(lp.swaps, 'balance_wallet', lambda s, b, r, share_a=None: calls.append((b, r, share_a))):
            lp.pauses.hot_pause_swap(state, state['hot_pause'])
        self.assertEqual(saved, [{'swapped': True}]); self.assertEqual(calls, [({'balanceA': 1.0}, {'r': 1}, 0.5)])


class CloseOnly(unittest.TestCase):
    """A pause's close (calm move, close only) leaves no reopen intent behind."""
    def go(self, close_only):
        state = dict(lp.paths.STATE_DEFAULTS)
        reopened = []
        st = {'positionMint': 'M', 'whirlpool': 'P', 'price': 100.0, 'inRange': True, 'liquidity': '1',
              'feesAccruedA': 0.0, 'feesAccruedB': 0.0, 'feesAccrued_USD': 0.0, 'positionUsd': 190.0,
              'lowerPrice': 99.0, 'upperPrice': 101.0}
        with mock.patch.object(lp.signers, 'chain',
                               lambda cmd, *a, **k: ({'signature': 's'}, None) if cmd == 'close' else (None, 'nothing')), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: None), \
                mock.patch.object(lp.books, 'notify_book', lambda ev, **kw: None), \
                mock.patch.object(db, 'close_position', lambda *a: None), \
                mock.patch.object(db, 'record_band_profile', lambda *a: None), \
                mock.patch.object(lp.moves, 'reopen', lambda *a, **k: reopened.append(1)), \
                mock.patch.object(time, 'sleep', lambda s: None):
            lp.moves.rebalance(state, st, 'hot pause', calm_move=True, exit_move=True, close_only=close_only)
        return state, reopened

    def test_no_intent_and_no_reopen(self):
        state, reopened = self.go(True)
        self.assertNotIn('pending_reopen', state); self.assertEqual(reopened, [])
        self.assertEqual(len(state['calm_times']), 1)                  # counted as a calm move

    def test_a_calm_move_still_leaves_its_intent(self):
        state, reopened = self.go(False)
        self.assertEqual(reopened, [1]); self.assertTrue(state['pending_reopen']['closed'])


class Loop(unittest.TestCase):
    """Where the loop calls the pause (read from the file: other tests patch main)."""
    src = (pathlib.Path(lp.loop.__file__)).read_text(encoding='utf-8')

    def test_held_band_checks_before_the_exit(self):
        i = self.src.index('if (config.HOT_PAUSE_ENABLED or config.MACRO_PAUSE_ENABLED) and pauses.hot_pause(state, status, rv):')
        j = self.src.index("if not status.get('inRange'):\n            seen = polls.poll_seen(")
        k = self.src.index('fo = board.venue_failover(state, status)')
        self.assertLess(k, i); self.assertLess(i, j)
        self.assertIn("state.pop('hot_pause', None)", self.src[i:j])

    def test_no_position_waits_before_dormant_and_reopen(self):
        i = self.src.index("if state.get('hot_pause') and pauses.hot_paused(state):")
        j = self.src.index('if pauses.macro_hold(state):')
        self.assertLess(self.src.index('if dormant(state, b0):'), j)
        self.assertLess(j, self.src.index("notify('no_position'"))
        self.assertLess(j, self.src.index('if regime.resume_reopen(state):'))
        self.assertLess(self.src.index('            swaps.sell_left_behind(state)\n            if state.get'), i)
        self.assertLess(i, self.src.index('if dormant(state, b0):'))
        self.assertLess(i, self.src.index('if regime.resume_reopen(state):'))


class PositionClosed(unittest.TestCase):
    def test_open_closed_and_unknown(self):
        import db
        _fixtures.ensure_profile()
        with db.cursor(commit=True) as cur:
            cur.execute("delete from positions where mint like 'HP%'")
        db.open_position('HP1', 'P', 'SOL/USDC', 1.0, 2.0, 1.0, 'sig', 10.0)
        self.assertIs(db.position_closed('HP1'), False)
        db.close_position('HP1', 'c', 10.0)
        self.assertIs(db.position_closed('HP1'), True)
        self.assertIsNone(db.position_closed('HP-none'))


class PauseOn(unittest.TestCase):
    def test_table(self):
        for enabled, pools, pool, want in ((False, frozenset(), 'P', False), (False, {'P'}, 'P', False),
                                           (True, frozenset(), 'P', True), (True, {'P'}, 'P', True),
                                           (True, {'P'}, 'Q', False), (True, {'P', 'Q'}, 'Q', True)):
            with mock.patch.object(config, 'HOT_PAUSE_ENABLED', enabled), \
                    mock.patch.object(config, 'HOT_PAUSE_POOLS', frozenset(pools)):
                self.assertIs(lp.pauses.hot_pause_on(pool), want, (enabled, pools, pool))


class Config(unittest.TestCase):
    def test_pools_default_to_every_pool_and_refuse_empty_entries(self):
        import db
        _fixtures.ensure_profile()
        self.assertIsNone(db.load_config('sol-usdc')['hot_pause_pools'])
        try:
            db.set_param('sol-usdc', 'hot_pause_pools', 'P1, P2')
            self.assertEqual(db.load_config('sol-usdc')['hot_pause_pools'], ['P1', 'P2'])
            for bad in ('', 'P1,'):
                with self.assertRaises(Exception, msg=bad):
                    db.set_param('sol-usdc', 'hot_pause_pools', bad)
        finally:
            with db.cursor(commit=True) as cur:
                cur.execute("update config set hot_pause_pools = null where name = 'sol-usdc'")

    def test_defaults_and_constraint(self):
        import db
        _fixtures.ensure_profile()
        row = db.load_config('sol-usdc')
        self.assertFalse(row['hot_pause_enabled'])
        self.assertEqual(float(row['hot_pause_fg_threshold']), 0.8)
        self.assertEqual(float(row['hot_pause_fg_hours']), 6.0)
        self.assertEqual(row['hot_pause_resume_minutes'], 30)
        self.assertEqual(float(row['hot_pause_max_hours']), 12.0)
        self.assertEqual(float(row['hot_pause_hot_pct']), 2.0)
        self.assertEqual(row['hot_pause_cooldown_minutes'], 60)
        for k, v in (('hot_pause_cooldown_minutes', '1441'), ('hot_pause_fg_threshold', '0'), ('hot_pause_fg_hours', '25'), ('hot_pause_resume_minutes', '4'),
                     ('hot_pause_max_hours', '49'), ('hot_pause_hot_pct', '0')):
            with self.assertRaises(Exception, msg=k):
                db.set_param('sol-usdc', k, v)


if __name__ == '__main__':
    unittest.main()


class MacroView(unittest.TestCase):
    def go(self, enabled=True, near=None, nxt=NOW + 86400, st=None, boom=None, now=NOW):
        told, asked = [], []

        def near_f(b, a):
            asked.append((b, a))
            if boom:
                raise boom
            return near
        state = st if st is not None else {}
        with mock.patch.object(config, 'MACRO_PAUSE_ENABLED', enabled), \
                mock.patch.object(config, 'MACRO_PAUSE_BEFORE_S', 900), mock.patch.object(config, 'MACRO_PAUSE_AFTER_S', 7200), \
                mock.patch.object(db, 'macro_event_near', near_f), \
                mock.patch.object(db, 'macro_next_ts', lambda: nxt), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append(ev)), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(time, 'time', lambda: now):
            r = lp.pauses.macro_view(state)
        return r, told, asked, state

    def test_off_reads_nothing(self):
        r, told, asked, _ = self.go(enabled=False, near={'ts': NOW, 'kind': 'FOMC'})
        self.assertIsNone(r); self.assertEqual((told, asked), ([], []))

    def test_window_and_until(self):
        r, told, asked, _ = self.go(near={'ts': NOW + 600, 'kind': 'FOMC'})
        self.assertEqual(r, {'ts': NOW + 600, 'kind': 'FOMC', 'until': NOW + 600 + 7200})
        self.assertEqual(asked, [(900, 7200)]); self.assertEqual(told, [])
        self.assertIsNone(self.go(near=None)[0])

    def test_unreadable_calendar_is_no_window_and_told(self):
        r, told, _, _ = self.go(boom=RuntimeError('db'))
        self.assertIsNone(r); self.assertEqual(told, ['macro_unread'])

    def test_empty_calendar_told_once_a_day(self):
        for nxt in (None, NOW + 60 * 86400 + 1):
            _, told, _, st = self.go(nxt=nxt)
            self.assertEqual(told, ['macro_calendar_empty'], nxt); self.assertEqual(st['macro_calendar_checked'], NOW)
        _, told, _, _ = self.go(nxt=NOW + 60 * 86400)
        self.assertEqual(told, [])
        _, told, _, _ = self.go(nxt=None, st={'macro_calendar_checked': NOW - 86399})
        self.assertEqual(told, [])
        _, told, _, _ = self.go(nxt=None, st={'macro_calendar_checked': NOW - 86400})
        self.assertEqual(told, ['macro_calendar_empty'])

    def test_no_state_skips_the_calendar_check(self):
        told = []
        with mock.patch.object(config, 'MACRO_PAUSE_ENABLED', True), \
                mock.patch.object(db, 'macro_event_near', lambda b, a: None), \
                mock.patch.object(db, 'macro_next_ts', side_effect=AssertionError('read')), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append(ev)):
            self.assertIsNone(lp.pauses.macro_view(None))
        self.assertEqual(told, [])


def book_fake(calls):
    """notify_book's real signature, so a payload key named like its first
    parameter fails here as it would live (audit 2026-10-04, C1)."""
    def notify_book(event, **payload):
        calls.append(('book', event, payload))
    return notify_book


class MacroPause(unittest.TestCase):
    M = {'ts': NOW + 600, 'kind': 'FOMC', 'until': NOW + 7800}

    def go(self, m=M, budget=5, venue_ok=True, rv=None, hot_enabled=False, st=None):
        calls = []
        state = st if st is not None else {}
        with mock.patch.object(lp.pauses, 'macro_view', lambda s: m), \
                mock.patch.object(lp.pauses, 'hot_pause_view', side_effect=AssertionError('no HOT read')), \
                mock.patch.object(lp.pauses, 'hot_pause_close',
                                  lambda s, st, why, now, **kw: (calls.append(('close', why, now, kw)) or True)), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: budget), \
                mock.patch.object(lp.regime, 'voluntary_move_allowed', side_effect=AssertionError('no gap check')), \
                mock.patch.object(health, 'allowed', lambda key, now: (calls.append(('breaker', key)) or (venue_ok, 0))), \
                mock.patch.object(lp.books, 'notify_book', book_fake(calls)), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: calls.append(('notify', ev, kw))), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(db, 'event', lambda *a: calls.append(('event', a[0]))), \
                mock.patch.object(config, 'HOT_PAUSE_ENABLED', hot_enabled), \
                mock.patch.object(config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(config, 'MACRO_PAUSE_AFTER_S', 7200), \
                mock.patch.object(time, 'time', lambda: NOW):
            r = lp.pauses.hot_pause(state, status(), rv)
        return r, calls, state

    def test_a_window_closes_whatever_the_market_or_the_hot_switch(self):
        r, calls, _ = self.go(rv=None)                                 # no regime view, HOT switch off
        self.assertIs(r, True)
        self.assertEqual([c[0] for c in calls], ['breaker', 'book', 'event', 'close'])
        self.assertEqual(calls[0][1], 'venue:raydium-clmm')
        self.assertEqual(calls[1][1], 'MACRO_PAUSE')
        self.assertEqual({k: calls[1][2][k] for k in ('kind', 'event_at', 'resume_minutes', 'held', 'price')},
                         {'kind': 'FOMC', 'event_at': '01-15 08:10', 'resume_minutes': 130, 'held': True, 'price': 120.0})
        close = calls[3]
        self.assertEqual(close[2], NOW); self.assertEqual(close[3], {'kind': 'macro', 'until': NOW + 7800})
        self.assertEqual(close[1], 'macro pause: FOMC at 01-15 08:10 UTC; waiting 50/50 until 120 min after it')

    def test_the_real_notify_book_accepts_the_payload(self):
        sent = []
        with mock.patch.object(lp.pauses, 'macro_view', lambda s: self.M), \
                mock.patch.object(lp.pauses, 'hot_pause_close', lambda *a, **k: True), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: 5), \
                mock.patch.object(health, 'allowed', lambda key, now: (True, 0)), \
                mock.patch.object(health, 'summary', lambda: []), \
                mock.patch.object(db, 'stats', lambda: {'equity_usd': 1.0}), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch.object(db, 'event', lambda *a: None):
            self.assertIs(lp.pauses.hot_pause({}, status(), None), True)
        self.assertEqual(sent[0][0], 'MACRO_PAUSE'); self.assertEqual(sent[0][1]['kind'], 'FOMC')

    def test_budget_or_breaker_hold_the_close_and_say_so_once(self):
        for kw in ({'budget': 0}, {'venue_ok': False}):
            st = {}
            r, calls, st = self.go(st=st, **kw)
            self.assertIs(r, False, kw)
            self.assertEqual([c[1] for c in calls if c[0] == 'notify'], ['macro_blocked'], kw)
            self.assertEqual(st['macro_blocked_told'], NOW + 600)
            r, calls, _ = self.go(st=st, **kw)
            self.assertEqual([c for c in calls if c[0] in ('notify', 'close')], [], kw)   # once per window
        r, calls, _ = self.go(budget=1)
        self.assertIs(r, True)

    def test_no_window_and_hot_off_does_nothing(self):
        r, calls, _ = self.go(m=None, rv={'choice': 1.03})
        self.assertIs(r, False); self.assertEqual(calls, [])


class MacroHold(unittest.TestCase):
    def go(self, m):
        calls, state = [], {'pending_reopen': {'x': 1}}
        with mock.patch.object(lp.pauses, 'macro_view', lambda s: m), \
                mock.patch.object(lp.books, 'notify_book', book_fake(calls)), \
                mock.patch.object(db, 'event', lambda *a: calls.append(('event', a[0]))), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(config, 'POOL', 'P'), \
                mock.patch.object(time, 'time', lambda: NOW):
            r = lp.pauses.macro_hold(state)
        return r, calls, state

    def test_no_window_holds_nothing(self):
        r, calls, state = self.go(None)
        self.assertIs(r, False); self.assertEqual(calls, []); self.assertNotIn('hot_pause', state)

    def test_a_window_with_no_band_waits_and_keeps_the_reopen_intent(self):
        r, calls, state = self.go({'ts': NOW + 600, 'kind': 'FOMC', 'until': NOW + 7800})
        self.assertIs(r, True)
        p = state['hot_pause']
        self.assertEqual({k: p[k] for k in ('kind', 'until', 'mint', 'booked', 'swapped', 'pool', 'since')},
                         {'kind': 'macro', 'until': NOW + 7800, 'mint': None, 'booked': True, 'swapped': False,
                          'pool': 'P', 'since': NOW})
        self.assertEqual(calls[0][1], 'MACRO_PAUSE'); self.assertIs(calls[0][2]['held'], False)
        self.assertEqual(calls[0][2]['resume_minutes'], 130); self.assertEqual(calls[1], ('event', 'MACRO_PAUSE'))
        self.assertEqual(state['pending_reopen'], {'x': 1})


class MacroUnread(unittest.TestCase):
    def test_told_once_an_hour(self):
        told = []
        state = {'macro_calendar_checked': NOW}
        for t, want in ((NOW, 1), (NOW + 3599, 1), (NOW + 3600, 2)):
            with mock.patch.object(config, 'MACRO_PAUSE_ENABLED', True), \
                    mock.patch.object(db, 'macro_event_near', side_effect=RuntimeError('no table')), \
                    mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append(ev)), \
                    mock.patch.object(lp.paths, 'save', lambda s: None), \
                    mock.patch.object(time, 'time', lambda: t):
                self.assertIsNone(lp.pauses.macro_view(state))
            self.assertEqual(len(told), want, t)
        self.assertEqual(state['macro_unread_told'], NOW + 3600)

    def test_no_state_still_tells_and_never_raises(self):
        told = []
        with mock.patch.object(config, 'MACRO_PAUSE_ENABLED', True), \
                mock.patch.object(db, 'macro_event_near', side_effect=RuntimeError('no table')), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append(ev)):
            self.assertIsNone(lp.pauses.macro_view(None))
        self.assertEqual(told, ['macro_unread'])


class Close(unittest.TestCase):
    def test_records_kind_and_until_before_the_close(self):
        seen, saved = [], []
        state = {}
        with mock.patch.object(lp.moves, 'rebalance', lambda s, st, why, **kw: seen.append(dict(s['hot_pause']))), \
                mock.patch.object(db, 'position_closed', lambda m: True), \
                mock.patch.object(lp.pauses, 'hot_pause_swap', lambda s, p: None), \
                mock.patch.object(lp.capital, 'position_usd', lambda st: 5.0), \
                mock.patch.object(lp.paths, 'save', lambda s: saved.append(1)), \
                mock.patch.object(config, 'POOL', 'P'):
            self.assertIs(lp.pauses.hot_pause_close(state, status(), 'why', NOW, kind='macro', until=NOW + 9), True)
        self.assertEqual(seen[0]['kind'], 'macro'); self.assertEqual(seen[0]['until'], NOW + 9)
        self.assertIsNone(seen[0]['ratio']); self.assertIs(state['hot_pause']['booked'], True)
        self.assertEqual(seen[0]['since'], NOW); self.assertEqual(seen[0]['withdraw_usd'], 5.0)


class MacroPaused(unittest.TestCase):
    def go(self, p, m=None, now=NOW, macro=True, hot=False, view=None):
        told, events = [], []
        self.swaps = 0
        state = {'hot_pause': dict(p)}

        def swap(s, pp):
            self.swaps += 1; pp['swapped'] = True
        with mock.patch.object(config, 'MACRO_PAUSE_ENABLED', macro), mock.patch.object(config, 'HOT_PAUSE_ENABLED', hot), \
                mock.patch.object(lp.pauses, 'hot_pause_swap', swap), \
                mock.patch.object(config, 'POOL', 'P'), \
                mock.patch.object(config, 'HOT_PAUSE_RESUME_S', 1800), mock.patch.object(config, 'HOT_PAUSE_MAX_S', 43200), \
                mock.patch.object(lp.pauses, 'macro_view', lambda s: m), \
                mock.patch.object(lp.board, 'sample_fee_growth', lambda s: None), \
                mock.patch.object(lp.books, 'daily_report', lambda s: None), \
                mock.patch.object(lp.housekeeping, 'run_audits', lambda s: None), \
                mock.patch.object(lp.capital, 'wallet', lambda pl: {'balanceA': 1.0, 'price': 120.0, 'quoteUsd': 1.0}), \
                mock.patch.object(lp.regime, 'regime_choice_now', lambda pl, px: 1.03), \
                mock.patch.object(lp.pauses, 'hot_pause_view',
                                  lambda *a: (told.append(('view',)) or (view or {'hot': True, 'ratio': 0.5, 'bad': True}))), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append((ev, kw))), \
                mock.patch.object(db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(time, 'time', lambda: now):
            r = lp.pauses.hot_paused(state)
        return r, state, told, events

    P = {'since': NOW - 1200, 'last_bad': NOW - 1200, 'pool': 'P', 'told': NOW - 60, 'booked': True, 'swapped': True,
         'kind': 'macro', 'until': NOW + 6000}

    def test_waits_for_its_window_without_reading_the_market(self):
        r, state, told, _ = self.go(self.P)
        self.assertIs(r, True); self.assertNotIn(('view',), told); self.assertIn('hot_pause', state)
        self.assertEqual(told, [])                                     # told a minute ago
        self.assertEqual(self.swaps, 0); self.assertIs(state['hot_pause']['swapped'], True)

    def test_tells_with_the_minutes_left(self):
        r, state, told, _ = self.go(dict(self.P, told=NOW - 1800))
        self.assertEqual(told[-1][0], 'hot_paused')
        self.assertEqual(told[-1][1]['until_minutes'], 100); self.assertEqual(told[-1][1]['kind'], 'macro')
        self.assertEqual(state['hot_pause']['told'], NOW)

    def test_resumes_when_the_window_ends(self):
        r, state, told, events = self.go(dict(self.P, until=NOW))
        self.assertIs(r, False); self.assertNotIn('hot_pause', state); self.assertNotIn('hot_pause_resumed', state)
        self.assertEqual(told[-1][0], 'HOT_RESUME'); self.assertEqual(told[-1][1]['reason'], 'the macro window is over')
        self.assertEqual(told[-1][1]['paused_minutes'], 20); self.assertEqual(events[0][0], 'HOT_RESUME')
        self.assertNotIn(('view',), told)
        r, _, _, _ = self.go(dict(self.P, until=NOW + 1))
        self.assertIs(r, True)

    def test_a_window_still_open_extends_the_pause(self):
        r, state, _, _ = self.go(dict(self.P, until=NOW - 5), m={'ts': NOW, 'kind': 'FOMC', 'until': NOW + 7200})
        self.assertIs(r, True); self.assertEqual(state['hot_pause']['until'], NOW + 7200)
        r, state, _, _ = self.go(dict(self.P, until=NOW + 9000), m={'ts': NOW, 'kind': 'FOMC', 'until': NOW + 7200})
        self.assertEqual(state['hot_pause']['until'], NOW + 9000)     # never shortened

    def test_a_hot_pause_waits_through_a_window_then_reads_the_market(self):
        hp = dict(self.P, kind='hot', until=None, last_bad=NOW - 3600)
        r, state, told, _ = self.go(hp, m={'ts': NOW, 'kind': 'FOMC', 'until': NOW + 7200}, hot=True)
        self.assertIs(r, True); self.assertNotIn(('view',), told); self.assertEqual(state['hot_pause']['until'], NOW + 7200)
        r, state, told, _ = self.go(dict(hp, until=NOW), hot=True, view={'hot': False, 'ratio': None, 'bad': False})
        self.assertIn(('view',), told); self.assertIs(r, False)       # window over, signal clear for 60 min

    def test_a_pool_switch_inside_the_window_waits_on_the_new_pool(self):
        r, state, _, _ = self.go(dict(self.P, pool='OLD'))
        self.assertIs(r, True); self.assertEqual(state['hot_pause']['pool'], 'P')
        self.assertEqual(self.swaps, 1)                                # 50/50 in the new pool's tokens
        r, state, told, _ = self.go(dict(self.P, pool='OLD', until=NOW))
        self.assertIs(r, False); self.assertEqual(told[0][0], 'HOT_RESUME')   # window over: reopen there
        self.assertIn('pool changed', told[0][1]['reason']); self.assertEqual(self.swaps, 0)
        r, state, told, _ = self.go(dict(self.P, pool='OLD'), macro=False)
        self.assertIs(r, False); self.assertIn('pool changed', told[0][1]['reason']); self.assertEqual(self.swaps, 0)
        r, state, told, _ = self.go(dict(self.P, pool='OLD', kind='hot', until=None), hot=True)
        self.assertIs(r, False); self.assertEqual(self.swaps, 0)       # a HOT pause has no window to keep

    def test_switch_off_resumes_by_kind(self):
        r, state, told, _ = self.go(self.P, macro=False, hot=True)
        self.assertIs(r, False); self.assertEqual(told[0][0], 'HOT_RESUME')
        self.assertNotIn('hot_pause_resumed', state)
        r, state, _, _ = self.go(dict(self.P, kind='hot', until=None), macro=False, hot=False)
        self.assertEqual(state['hot_pause_resumed'], NOW)
        r, state, told, _ = self.go(dict(self.P, kind='hot', until=None), macro=True, hot=False)
        self.assertIs(r, False)
        r, state, _, _ = self.go(dict(self.P, kind=None, until=None, last_bad=NOW), macro=False, hot=True)
        self.assertIs(r, True)                                         # an old pause without a kind is a HOT one


class MacroCalendar(unittest.TestCase):
    def setUp(self):
        import db
        self.db = db
        self.clear()

    def tearDown(self):
        self.clear()

    def clear(self):
        with self.db.cursor(commit=True) as cur:
            cur.execute("delete from macro_events where kind like 'TEST%'")

    def put(self, secs_from_now, kind='TEST'):
        with self.db.cursor(commit=True) as cur:
            cur.execute("insert into macro_events (ts, kind, source) values (now() + make_interval(secs => %s), %s, 't')",
                        (secs_from_now, kind))

    def test_window_edges(self):
        self.put(900 - 5)                                              # 14m55s ahead: inside a 15-min lead
        m = self.db.macro_event_near(900, 7200)
        self.assertEqual(m['kind'], 'TEST'); self.assertAlmostEqual(m['ts'], time.time() + 895, delta=5)
        self.assertIsNone(self.db.macro_event_near(880, 7200))
        self.clear()
        self.put(-7200 + 5)
        self.assertIsNotNone(self.db.macro_event_near(900, 7200))
        self.assertIsNone(self.db.macro_event_near(900, 7190))

    def test_earliest_of_two_and_next(self):
        self.put(-600, 'TEST1'); self.put(300, 'TEST2')
        self.assertEqual(self.db.macro_event_near(900, 7200)['kind'], 'TEST1')
        self.assertAlmostEqual(self.db.macro_next_ts(), time.time() + 300, delta=5)
        self.clear()
        n = self.db.macro_next_ts()
        self.assertTrue(n is None or n > time.time() + 600)            # the TEST rows are gone


class MacroSeed(unittest.TestCase):
    def test_the_fed_calendar_is_seeded_in_utc(self):
        import db
        with db.cursor() as cur:
            cur.execute("select to_char(ts at time zone 'UTC', 'YYYY-MM-DD HH24:MI') t from macro_events "
                        "where kind = 'FOMC' and source like 'federalreserve.gov%' order by ts")
            got = [r['t'] for r in cur.fetchall()]
        self.assertEqual(got[:3], ['2026-10-28 18:00', '2026-12-09 19:00', '2027-01-27 19:00'])
        self.assertGreaterEqual(len(got), 10)

    def test_config_defaults_and_constraint(self):
        import db
        _fixtures.ensure_profile()
        row = db.load_config('sol-usdc')
        self.assertFalse(row['macro_pause_enabled'])
        self.assertEqual((row['macro_pause_before_minutes'], row['macro_pause_after_minutes']), (15, 120))
        for k, v in (('macro_pause_before_minutes', '241'), ('macro_pause_after_minutes', '14')):
            with self.assertRaises(Exception, msg=k):
                db.set_param('sol-usdc', k, v)
