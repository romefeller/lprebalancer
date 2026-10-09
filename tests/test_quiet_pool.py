"""A quiet pool's five-minute tape: slots without a swap are flat bars.

2026-10-02: GeckoTerminal emits no bar for a slot without a swap, and the
MU/USDC pool had none in 54% of its slots. tape_fresh (the last six bars
consecutive) read that as an outage: mu-usdc was STALE 91.6% of the time,
parked at +/-5%. calm.quiet_fill fills a missing slot flat at the last close
only where the pool was quiet: an old slot, or one the canary pool (SOL/USDC,
which trades every slot) has a bar in; and, after the last bar, only while
the live price has not moved (calm.quiet_tail_ok) and for at most 6 h.
The 2026-09-29 outage (every Solana pool unindexed) must still read STALE.

Covered: the two pure functions as properties and by case, the live tape of
2026-10-02 replayed, the outage replayed, and rebalancer.with_surrogate with
the canary read from the database."""
import json
import pathlib
import time
import unittest
from unittest import mock

import numpy as np
from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import config
import db
import rebalancer
import lp.tape

B = calm.BAR_SECONDS
FIX = json.loads((pathlib.Path(__file__).parent / 'fixtures_quiet_20261002.json').read_text())
WINDOW = 30 * 86400


def tape(ts, closes=None):
    """Six arrays for bars at `ts` (closes 100, 101, ... unless given)."""
    ts = np.array(sorted(ts), dtype=float)
    c = np.array(closes if closes is not None else 100.0 + np.arange(len(ts)), dtype=float)
    return ts, c, c, c, c, np.ones(len(ts))


def slot(now, k):
    """The k-th closed slot back from `now` (k = 0: the newest that counts)."""
    return int((now - B - calm.GECKO_GRACE_S) // B) * B - k * B


NOW = 1_790_000_000.0 + 37                     # not on a slot boundary


class TailOk(unittest.TestCase):
    def test_an_unmoved_price_is_ok_and_clears_the_timer(self):
        self.assertEqual(calm.quiet_tail_ok(100.29, 100.0, 5.0, NOW), (True, None))
        self.assertEqual(calm.quiet_tail_ok(100.0 * (1 + calm.QUIET_TOL), 100.0, None, NOW), (True, None))

    def test_a_moved_price_waits_for_its_bar_then_is_an_outage(self):
        ok, since = calm.quiet_tail_ok(101.0, 100.0, None, NOW)
        self.assertEqual((ok, since), (True, NOW))
        self.assertEqual(calm.quiet_tail_ok(101.0, 100.0, since, NOW + calm.QUIET_MISMATCH_S), (True, NOW))
        self.assertEqual(calm.quiet_tail_ok(101.0, 100.0, since, NOW + calm.QUIET_MISMATCH_S + 1), (False, NOW))
        self.assertEqual(calm.quiet_tail_ok(99.0, 100.0, None, NOW), (True, NOW))     # down as up

    def test_the_tolerance_is_inclusive_and_small_prices_are_prices(self):
        self.assertEqual(calm.quiet_tail_ok(1.5, 1.0, None, NOW, tol=0.5), (True, None))
        self.assertEqual(calm.quiet_tail_ok(1.5, 1.0, None, NOW, tol=0.25), (True, NOW))
        self.assertEqual(calm.quiet_tail_ok(0.5, 0.5, None, NOW), (True, None))       # under 1 is a price
        self.assertEqual(calm.quiet_tail_ok(0.001, 0.001, None, NOW), (True, None))

    def test_an_unknown_price_is_never_ok(self):
        for price, close in ((None, 100.0), (100.0, None), (0.0, 100.0), (100.0, 0.0), (-1.0, 100.0)):
            self.assertEqual(calm.quiet_tail_ok(price, close, None, NOW), (False, None))

    @settings(max_examples=300, deadline=None)
    @given(move=st.floats(min_value=-0.05, max_value=0.05, allow_nan=False),
           waited=st.floats(min_value=0, max_value=3600, allow_nan=False))
    def test_ok_means_unmoved_or_still_waiting(self, move, waited):
        since = NOW - waited
        ok, out = calm.quiet_tail_ok(100.0 * (1 + move), 100.0, since, NOW)
        unmoved = abs(move) <= calm.QUIET_TOL * (1 - 1e-9)
        if unmoved:
            self.assertEqual((ok, out), (True, None))
        if abs(move) > calm.QUIET_TOL * (1 + 1e-9):
            self.assertEqual((ok, out), (waited <= calm.QUIET_MISMATCH_S, since))


class Fill(unittest.TestCase):
    def fill(self, have, ref, tail_ok=True, now=NOW, closes=None, **kw):
        t = tape(have, closes)
        return calm.quiet_fill(t[0], t[4], now, ref, tail_ok, WINDOW, **kw)

    def test_a_canary_bar_makes_a_recent_gap_quiet(self):
        have = [slot(NOW, k) for k in (5, 4, 1, 0)]
        ref = [slot(NOW, k) for k in range(8)]
        q = self.fill(have, ref)
        self.assertEqual(list(q[0]), [slot(NOW, 3), slot(NOW, 2)])
        self.assertEqual(list(q[4]), [101.0, 101.0])                 # the close before the gap
        self.assertEqual((list(q[1]), list(q[2]), list(q[3])), (list(q[4]),) * 3)
        self.assertEqual(list(q[5]), [0.0, 0.0])
        self.assertTrue(calm.tape_fresh(np.sort(np.r_[q[0], have]), NOW))

    def test_an_interior_gap_needs_no_tail_ok(self):
        have = [slot(NOW, k) for k in (5, 4, 1, 0)]
        ref = [slot(NOW, k) for k in range(8)]
        self.assertEqual(list(self.fill(have, ref, tail_ok=False)[0]), [slot(NOW, 3), slot(NOW, 2)])

    def test_a_slot_exactly_history_old_needs_the_canary(self):
        have = [slot(NOW, 300), slot(NOW, 0)]
        edge = slot(NOW, 290)
        q = self.fill(have, None, now=NOW, history_s=NOW - edge)        # `edge` is exactly that old
        self.assertNotIn(edge, list(q[0]))
        self.assertIn(edge - B, list(q[0]))

    def test_without_the_canary_a_recent_gap_stays(self):
        have = [slot(NOW, k) for k in (5, 4, 1, 0)]
        self.assertIsNone(self.fill(have, None))
        self.assertIsNone(self.fill(have, []))
        ref = [slot(NOW, k) for k in (7, 6, 5, 4, 1, 0)]             # the canary lacks them too: an outage
        self.assertIsNone(self.fill(have, ref))

    def test_an_old_gap_is_quiet_without_the_canary(self):
        old = NOW - calm.QUIET_HISTORY_S - 3600
        have = [slot(old, 3), slot(old, 0)] + [slot(NOW, k) for k in range(6)]
        q = self.fill(have, None)
        self.assertEqual(list(q[0][:2]), [slot(old, 2), slot(old, 1)])
        self.assertEqual(list(q[4][:2]), [100.0, 100.0])
        self.assertTrue(np.all(q[0] < NOW - calm.QUIET_HISTORY_S))      # every recent slot stays a gap
        self.assertTrue(np.all(q[4][2:] == 101.0))                       # flat at slot(old, 0)'s close

    def test_the_tail_needs_tail_ok_and_at_most_six_hours(self):
        have = [slot(NOW, k) for k in (9, 8, 7, 6)]
        ref = [slot(NOW, k) for k in range(80)]
        q = self.fill(have, ref)
        self.assertEqual(list(q[0]), [slot(NOW, k) for k in (5, 4, 3, 2, 1, 0)])
        self.assertIsNone(self.fill(have, ref, tail_ok=False))
        n = calm.QUIET_MAX_S // B
        long_have = [slot(NOW, n + 1)]
        ref = [slot(NOW, k) for k in range(n + 5)]
        self.assertIsNone(self.fill(long_have, ref))                  # past the cap: an outage
        self.assertEqual(len(self.fill([slot(NOW, n)], ref)[0]), n)  # at the cap: filled

    def test_a_tail_newer_than_a_fresh_canary_is_quiet_a_stale_canary_is_not(self):
        have = [slot(NOW, k) for k in (6, 5, 4, 3)]
        ref = [slot(NOW, k) for k in range(2, 10)]                    # refreshed up to slot 2
        self.assertLessEqual(NOW - max(ref), calm.QUIET_REF_FRESH_S)
        self.assertEqual(list(self.fill(have, ref)[0]), [slot(NOW, k) for k in (2, 1, 0)])
        old_ref = [slot(NOW, k) for k in range(2, 12)]
        stale_now = max(old_ref) + calm.QUIET_REF_FRESH_S + 1                # the canary not refreshed since
        late = [t for t in range(max(old_ref) + B, slot(stale_now, 0) + 1, B)]   # newer than the canary
        self.assertTrue(late)
        q = self.fill(have, old_ref, now=stale_now)
        self.assertFalse(set(late) & set(q[0] if q is not None else []))
        self.assertEqual(list(q[0]), [slot(NOW, 2)])                     # the canary's own slot only

    def test_the_canary_fresh_exactly_at_its_limit_still_vouches(self):
        have = [slot(NOW, k) for k in (6, 5, 4, 3)]
        ref = [slot(NOW, k) for k in range(1, 10)]
        tail, age = slot(NOW, 0), NOW - slot(NOW, 1)                      # the canary's newest bar's age
        self.assertIn(tail, list(self.fill(have, ref, fresh_s=age)[0]))
        self.assertNotIn(tail, list(self.fill(have, ref, fresh_s=age - 1e-3)[0]))
        self.assertGreater(calm.QUIET_REF_FRESH_S, calm.FRESH_MAX_AGE_S)

    def test_nothing_before_the_first_bar_or_outside_the_window(self):
        have = [slot(NOW, k) for k in (3, 2, 1, 0)]
        ref = [slot(NOW, k) for k in range(20)]
        self.assertIsNone(self.fill(have, ref))
        t = tape([slot(NOW, 40), slot(NOW, 0)])
        q = calm.quiet_fill(t[0], t[4], NOW, [slot(NOW, k) for k in range(50)], True, 10 * B)
        self.assertEqual(list(q[0]), [slot(NOW, k) for k in range(10, 0, -1)])

    def test_no_tape_no_fill(self):
        self.assertIsNone(calm.quiet_fill(None, None, NOW, None, True, WINDOW))
        self.assertIsNone(calm.quiet_fill(np.array([]), np.array([]), NOW, None, True, WINDOW))

    @settings(max_examples=300, deadline=None)
    @given(have=st.sets(st.integers(min_value=0, max_value=60), min_size=1, max_size=40),
           ref=st.one_of(st.none(), st.sets(st.integers(min_value=0, max_value=60), max_size=60)),
           tail_ok=st.booleans(), old=st.booleans())
    def test_a_fill_is_flat_at_the_last_real_close_and_never_over_a_bar(self, have, ref, tail_ok, old):
        now = NOW + (calm.QUIET_HISTORY_S + 61 * B if old else 0)
        hs = sorted(slot(NOW, k) for k in have)
        closes = [100.0 + 3 * i for i in range(len(hs))]
        rs = None if ref is None else [slot(NOW, k) for k in ref]
        q = self.fill(hs, rs, tail_ok, now=now, closes=closes)
        if q is None:
            return
        self.assertFalse(set(q[0]) & set(hs))
        self.assertTrue(np.all(np.diff(q[0]) > 0))
        for t, c in zip(q[0], q[4]):
            i = max(k for k, h in enumerate(hs) if h < t)
            self.assertEqual(c, closes[i])                           # flat at the previous real close
            self.assertLessEqual(t, slot(now, 0))
            if t > hs[-1]:
                self.assertTrue(tail_ok)
            recent = t >= now - calm.QUIET_HISTORY_S
            if recent:
                self.assertIsNotNone(rs)                             # a recent fill needs the canary
                self.assertTrue(t in rs or t > max(rs))


class Replay(unittest.TestCase):
    """The live tapes of 2026-10-02 (fixtures_quiet_20261002.json)."""

    def setUp(self):
        rows = np.array(FIX['mu'], dtype=float)
        self.mu = tuple(rows[:, i] for i in range(6))
        self.sol = FIX['sol_ts']
        self.now = self.mu[0][-1] + B + calm.GECKO_GRACE_S + 30      # just after the newest MU bar counts

    def test_the_old_rule_called_the_live_mu_tape_stale(self):
        stale = sum(not calm.tape_fresh(self.mu[0][self.mu[0] <= t], t + B + calm.GECKO_GRACE_S + 30)
                    for t in range(int(self.sol[0]), int(self.sol[-1]), B))
        self.assertGreater(stale / len(range(int(self.sol[0]), int(self.sol[-1]), B)), 0.6)

    def test_with_the_canary_the_quiet_mu_tape_is_fresh(self):
        fresh, steps = 0, 0
        for t in range(int(self.sol[0]) + 6 * B, int(self.sol[-1]), B):
            now = t + B + calm.GECKO_GRACE_S + 30
            m = self.mu[0] <= t
            ts, close = self.mu[0][m], self.mu[4][m]
            ref = [x for x in self.sol if x <= t]
            q = calm.quiet_fill(ts, close, now, ref, True, WINDOW)
            full = np.sort(np.r_[ts, q[0]]) if q is not None else ts
            fresh += calm.tape_fresh(full, now)
            steps += 1
        self.assertGreater(fresh / steps, 0.95)

    def test_the_fill_lowers_the_inflated_sigma(self):
        q = calm.quiet_fill(self.mu[0], self.mu[4], self.now, self.sol, True, WINDOW)
        order = np.argsort(np.r_[self.mu[0], q[0]])
        filled_close = np.r_[self.mu[4], q[4]][order]
        raw = calm.ewma_sigma(self.mu[4])
        flat = calm.ewma_sigma(filled_close)
        self.assertLess(np.median(flat[-288:]), np.median(raw[-288:]))

    def test_the_2026_09_29_outage_still_reads_stale(self):
        """Every Solana pool unindexed for 25 min, then a lone bar: the
        canary lacks the same slots, so nothing is filled."""
        t0 = int(self.sol[-1])
        gap = set(range(t0 - 6 * B, t0, B))
        sol = [x for x in self.sol if x not in gap]
        mu_ts = [x for x in self.mu[0] if x not in gap and x <= t0]
        m = np.isin(self.mu[0], mu_ts)
        now = t0 + B + calm.GECKO_GRACE_S + 30
        for ref in (sol, None):                                      # mu-usdc's view, and sol-usdc's own
            for ts, close in ((self.mu[0][m], self.mu[4][m]), (np.array(sol, float), np.full(len(sol), 120.0))):
                q = calm.quiet_fill(ts, close, now, ref if ts is not sol else None, True, WINDOW)
                full = np.sort(np.r_[ts, q[0]]) if q is not None else ts
                if ts[-1] == t0:
                    self.assertFalse(calm.tape_fresh(full, now))


class WithSurrogateMore(unittest.TestCase):
    """with_surrogate: the tail checks the last close, the window trims, a
    surrogate and a quiet fill together."""

    setUp = None                                                        # set below from WithSurrogate

    def test_the_tail_compares_the_last_close_not_another_column(self):
        have = [slot(self.now, k) for k in (8, 7, 6, 5, 4)]
        bars = tape(have, [1000.0, 1000.0, 1000.0, 1000.0, 1080.0])
        out = lp.tape.with_surrogate(self.MU_POOL, bars, 1080.5, 'MU/USDC')
        self.assertEqual(list(out[0]), [slot(self.now, k) for k in range(8, -1, -1)])
        self.assertTrue(np.all(out[4][5:] == 1080.0))                  # flat at the last close
        self.assertTrue(np.all(out[5][5:] == 0.0))
        self.assertIsNone(lp.tape._QUIET_MISMATCH[self.MU_POOL])
        lp.tape.with_surrogate(self.MU_POOL, bars, 1000.0, 'MU/USDC')
        self.assertIsNotNone(lp.tape._QUIET_MISMATCH[self.MU_POOL])  # 1000 is not the last close

    def test_the_filled_tape_is_trimmed_to_the_window(self):
        with mock.patch.object(config, 'REGIME_TAPE_DAYS', 1):         # tape_bars() = 288: one day
            w = lp.tape.tape_bars() * B
            newest = slot(self.now, 0)
            have = [newest - w - B, newest - w, newest - w + B, newest - 3 * B, newest - 2 * B, newest - B, newest]
            out = lp.tape.with_surrogate(self.MU_POOL, tape(have), 102.0 + 4, 'MU/USDC')
        self.assertEqual(out[0][0], newest - w)                          # the edge stays, older goes
        self.assertEqual(out[0][-1], newest)
        self.assertTrue(set(have[1:]) <= set(out[0]))
        self.assertGreater(len(out[0]), len(have) - 1)                   # the canary's slots were filled

    def test_a_failed_surrogate_overlay_still_gets_the_quiet_fill(self):
        have = [slot(self.now, k) for k in (12, 11, 10, 6, 5)]
        with mock.patch.object(calm, 'missing_slots', side_effect=RuntimeError('x')):
            out = lp.tape.with_surrogate(self.MU_POOL, tape(have, [104.0] * 5), 104.0, 'MU/USDC')
        self.assertEqual(list(out[0]), [slot(self.now, k) for k in range(12, -1, -1)])
        src = lp.tape.LAST_SURROGATE[self.MU_POOL]
        self.assertEqual((src['surrogate'], src['filled_1h'], src['quiet_1h']), (None, 0, 8))

    def test_a_surrogate_and_a_quiet_fill_together(self):
        have = [slot(self.now, k) for k in (12, 11, 10, 9, 8)]
        sur = [slot(self.now, k) for k in (3, 2)]
        got = tape(sur, [104.0, 104.0])
        with mock.patch.object(calm, 'surrogate_5m', lambda *a, **k: ('Binance', got)):
            out = lp.tape.with_surrogate(self.MU_POOL, tape(have), 104.0, 'MU/USDC')
        src = lp.tape.LAST_SURROGATE[self.MU_POOL]
        self.assertEqual(list(out[0]), [slot(self.now, k) for k in range(12, -1, -1)])
        self.assertEqual((src['surrogate'], src['filled_1h'], src['quiet_1h']), ('Binance', 2, 6))
        self.assertEqual(src['source'], 'Gecko+Binance')


class WithSurrogate(unittest.TestCase):
    """rebalancer.with_surrogate: the canary from tape5 and config."""

    MU_POOL, SOL_POOL = 'QPmuPOOLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'QPsolPOOLxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    SOL, USDC = 'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'

    def cleanup(self):
        with db.cursor(commit=True) as cur:
            cur.execute('delete from tape5 where pool = any(%s)', ([self.MU_POOL, self.SOL_POOL],))
            cur.execute("delete from config where name in ('qp-sol', 'qp-mu')")

    def setUp(self):
        self.cleanup(); self.addCleanup(self.cleanup)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, mints) values "
                        "('qp-sol', %s, 'SOL/USDC', 100, 1000, %s), ('qp-mu', %s, 'MU/USDC', 100, 1000, %s)",
                        (self.SOL_POOL, [self.SOL, self.USDC], self.MU_POOL, ['MUx', self.USDC]))
        self.now = time.time()
        ref = [slot(self.now, k) for k in range(40)]
        db.tape_store(self.SOL_POOL, tape(ref), ref[-1] - B)
        for p in (mock.patch.dict(lp.tape._QUIET_REF, clear=True),
                  mock.patch.dict(lp.tape._QUIET_MISMATCH, clear=True),
                  mock.patch.dict(lp.tape._SURR, clear=True),
                  mock.patch.dict(lp.tape.LAST_SURROGATE, clear=True),
                  mock.patch.object(calm, 'surrogate_5m', lambda *a, **k: (None, None))):
            p.start(); self.addCleanup(p.stop)

    def test_the_quiet_pool_reads_fresh_and_says_so(self):
        have = [slot(self.now, k) for k in (12, 11, 10, 6, 5)]
        bars = tape(have, [1080.0] * len(have))
        out = lp.tape.with_surrogate(self.MU_POOL, bars, 1080.5, 'MU/USDC')
        self.assertEqual(list(out[0]), [slot(self.now, k) for k in range(12, -1, -1)])
        self.assertTrue(calm.tape_fresh(out[0], time.time()))
        src = lp.tape.LAST_SURROGATE[self.MU_POOL]
        self.assertEqual((src['source'], src['filled_1h']), ('Gecko', 0))
        self.assertEqual(src['quiet_1h'], 8)
        for real in have:
            self.assertIn(real, list(out[0]))

    def test_a_price_that_moved_without_a_bar_ends_the_tail_fill(self):
        have = [slot(self.now, k) for k in (8, 7, 6, 5, 4)]
        bars = tape(have, [1080.0] * len(have))
        self.assertEqual(len(lp.tape.with_surrogate(self.MU_POOL, bars, 1100.0, 'MU/USDC')[0]), 9)  # waits
        lp.tape._QUIET_MISMATCH[self.MU_POOL] = time.time() - calm.QUIET_MISMATCH_S - 1
        out = lp.tape.with_surrogate(self.MU_POOL, bars, 1100.0, 'MU/USDC')
        self.assertEqual(list(out[0]), have)
        self.assertFalse(calm.tape_fresh(out[0], time.time()))

    def test_the_canary_pool_cannot_vouch_for_itself(self):
        have = [slot(self.now, k) for k in (12, 11, 10, 6, 5)]
        out = lp.tape.with_surrogate(self.SOL_POOL, tape(have), 104.0, 'SOL/USDC')
        self.assertEqual(list(out[0]), have)
        self.assertIsNone(lp.tape.quiet_ref_ts(self.SOL_POOL, time.time() + 999))

    def test_a_database_failure_leaves_the_tape_as_it_was(self):
        have = [slot(self.now, k) for k in (12, 11, 10, 6, 5)]
        with mock.patch.object(db, 'tape_ref_pool', side_effect=RuntimeError('db')):
            out = lp.tape.with_surrogate(self.MU_POOL, tape(have), 104.0, 'MU/USDC')
        self.assertEqual(list(out[0]), have)

    def test_a_fill_failure_returns_the_real_tape(self):
        have = [slot(self.now, k) for k in (12, 11, 10, 6, 5)]
        with mock.patch.object(calm, 'quiet_fill', side_effect=ValueError('x')):
            out = lp.tape.with_surrogate(self.MU_POOL, tape(have), 104.0, 'MU/USDC')
        self.assertEqual(list(out[0]), have)

    def test_the_canary_is_read_once_a_minute(self):
        calls = []
        real = db.tape_load
        with mock.patch.object(db, 'tape_load', lambda *a: calls.append(a) or real(*a)):
            for _ in range(3):
                lp.tape.quiet_ref_ts(self.MU_POOL, self.now)
            lp.tape.quiet_ref_ts(self.MU_POOL, self.now + lp.tape.QUIET_REF_REFRESH + 1)
        self.assertEqual(len(calls), 2)

    def test_the_cache_is_per_pool_holds_its_answer_and_expires_on_time(self):
        first = lp.tape.quiet_ref_ts(self.MU_POOL, self.now)
        self.assertEqual(len(first), 40)
        with mock.patch.object(db, 'tape_load', side_effect=AssertionError('read')):
            self.assertIs(lp.tape.quiet_ref_ts(self.MU_POOL, self.now + lp.tape.QUIET_REF_REFRESH), first)
        self.assertIsNone(lp.tape.quiet_ref_ts(self.SOL_POOL, self.now))       # another pool: asked again
        self.assertEqual(len(lp.tape.quiet_ref_ts(self.MU_POOL, self.now)), 40)

    def test_the_canary_is_a_native_stable_pool_not_this_one(self):
        self.assertEqual(db.tape_ref_pool(self.SOL, [self.USDC], self.MU_POOL), self.SOL_POOL)
        self.assertIsNone(db.tape_ref_pool(self.SOL, ['NOTSTABLE'], self.MU_POOL))


if __name__ == '__main__':
    unittest.main()


class FeeTolerance(unittest.TestCase):
    """The tail's tolerance carries the pool's fee: trade-price closes sit a
    fee away from the pool price (DJT/USDC, 0.30%, 2026-10-06)."""

    setUp = None                                                        # set below from WithSurrogate

    def liq(self, rec):
        p = mock.patch.dict(lp.tape._LIQ, {self.MU_POOL: (time.time(), rec)} if rec is not None else {}, clear=True)
        p.start(); self.addCleanup(p.stop)

    def test_the_fee_is_added(self):
        self.liq({'fee': 0.003})
        self.assertAlmostEqual(lp.tape.quiet_tolerance(self.MU_POOL), calm.QUIET_TOL + 0.003)

    def test_no_record_or_no_usable_fee_is_the_plain_tolerance(self):
        for rec in (None, {}, {'fee': None}, {'fee': 'x'}, {'fee': -0.01}, {'fee': float('nan')},
                    {'fee': float('inf')}, {'fee': [1]}):
            with self.subTest(rec=rec):
                self.liq(rec)
                self.assertEqual(lp.tape.quiet_tolerance(self.MU_POOL), calm.QUIET_TOL)

    def test_a_large_fee_is_capped_and_the_cap_is_inclusive(self):
        self.liq({'fee': 0.25})
        self.assertAlmostEqual(lp.tape.quiet_tolerance(self.MU_POOL), calm.QUIET_TOL + lp.tape.QUIET_FEE_MAX)
        self.liq({'fee': lp.tape.QUIET_FEE_MAX})
        self.assertAlmostEqual(lp.tape.quiet_tolerance(self.MU_POOL), calm.QUIET_TOL + lp.tape.QUIET_FEE_MAX)

    def test_another_pools_record_does_not_count(self):
        p = mock.patch.dict(lp.tape._LIQ, {self.SOL_POOL: (time.time(), {'fee': 0.003})}, clear=True)
        p.start(); self.addCleanup(p.stop)
        self.assertEqual(lp.tape.quiet_tolerance(self.MU_POOL), calm.QUIET_TOL)

    def test_the_djt_case_fills_with_the_fee_and_not_without(self):
        # the live price 0.30% above the last trade-price close, past the mismatch wait
        have = [slot(self.now, k) for k in (8, 7, 6, 5, 4)]
        bars = tape(have, [8.6446] * len(have))
        live = 8.6446 * 1.00301
        self.liq({'fee': 0.003})
        lp.tape._QUIET_MISMATCH[self.MU_POOL] = time.time() - calm.QUIET_MISMATCH_S - 1
        out = lp.tape.with_surrogate(self.MU_POOL, bars, live, 'DJT/USDC')
        self.assertEqual(list(out[0]), [slot(self.now, k) for k in range(8, -1, -1)])
        self.assertTrue(calm.tape_fresh(out[0], time.time()))
        self.assertIsNone(lp.tape._QUIET_MISMATCH[self.MU_POOL])
        self.liq({})
        lp.tape._QUIET_MISMATCH[self.MU_POOL] = time.time() - calm.QUIET_MISMATCH_S - 1
        out = lp.tape.with_surrogate(self.MU_POOL, bars, live, 'DJT/USDC')
        self.assertEqual(list(out[0]), have)                              # the old rule: STALE
        self.assertFalse(calm.tape_fresh(out[0], time.time()))

    def test_a_real_move_beyond_the_fee_still_ends_the_fill(self):
        have = [slot(self.now, k) for k in (8, 7, 6, 5, 4)]
        bars = tape(have, [8.6446] * len(have))
        self.liq({'fee': 0.003})
        lp.tape._QUIET_MISMATCH[self.MU_POOL] = time.time() - calm.QUIET_MISMATCH_S - 1
        out = lp.tape.with_surrogate(self.MU_POOL, bars, 8.6446 * 1.0061, 'DJT/USDC')
        self.assertEqual(list(out[0]), have)


WithSurrogateMore.setUp = WithSurrogate.setUp
WithSurrogateMore.cleanup = WithSurrogate.cleanup
FeeTolerance.setUp = WithSurrogate.setUp
FeeTolerance.cleanup = WithSurrogate.cleanup
for _k in ('MU_POOL', 'SOL_POOL', 'SOL', 'USDC'):
    setattr(FeeTolerance, _k, getattr(WithSurrogate, _k))
for _k in ('MU_POOL', 'SOL_POOL', 'SOL', 'USDC'):
    setattr(WithSurrogateMore, _k, getattr(WithSurrogate, _k))
