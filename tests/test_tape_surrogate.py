"""The surrogate tape (2026-09-29): GeckoTerminal first; a slot it lacks is
filled from another venue's klines (Binance). No failure of either source may
crash the loop, store foreign bars, flip the band, or narrow on a tape that
does not describe the market. Offline: every network call is mocked."""
import math
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures
_fixtures.ensure_profile()

import calm        # noqa: E402
import engine      # noqa: E402
import rebalancer  # noqa: E402
import lp.regime  # noqa: E402
import lp.tape  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402

W = calm.WIDTHS
S = calm.BAR_SECONDS


def slot_now():
    """One slot past the newest slot that counts as missing (past the
    GeckoTerminal grace), so the tests do not depend on the wall clock."""
    return int((time.time() - S - calm.GECKO_GRACE_S) // S) * S + S


def tape(n=3000, sigma=0.0004, end=None, seed=3, price=120.0, drop=()):
    """An aligned, closed five-minute tape ending at the newest closed slot,
    with the slots in `drop` removed."""
    end = slot_now() if end is None else end
    ts = np.arange(end - n * S, end, S, dtype=float)
    rng = np.random.default_rng(seed)
    c = price * np.exp(np.cumsum(rng.normal(0, sigma, n))); c *= price / c[-1]
    o = np.concatenate([[c[0]], c[:-1]])
    h, l = np.maximum(o, c) * 1.0004, np.minimum(o, c) * 0.9996
    keep = np.array([int(t) not in set(drop) for t in ts], dtype=bool)
    return tuple(x[keep] for x in (ts, o, h, l, c, np.full(n, 1e4)))


def rows_of(bars):
    return [list(r) for r in zip(*bars)]


class Pure(unittest.TestCase):
    def test_pair_tokens(self):
        self.assertEqual(calm.pair_tokens('SOL/USDC'), ('SOL', 'USDC'))
        self.assertEqual(calm.pair_tokens(' wsol / usdc '), ('SOL', 'USDC'))
        self.assertEqual(calm.pair_tokens('SOL-USDT'), ('SOL', 'USDT'))
        for junk in (None, '', 'SOL', 'A/B/C', 'SO L/USDC', 'SOL/$', 42, 'SOL/'):
            self.assertIsNone(calm.pair_tokens(junk), junk)

    def test_clean_bars_takes_any_junk_and_keeps_only_valid_bars(self):
        now = slot_now()
        rng = np.random.default_rng(7)
        pool = [None, 'x', float('nan'), float('inf'), -1.0, 0.0, 1.0, 120.0, 121.0, 119.0, '120.5', [], {}]
        for trial in range(300):
            rows = []
            for _ in range(rng.integers(0, 12)):
                kind = rng.integers(0, 4)
                if kind == 0:
                    rows.append([pool[i] for i in rng.integers(0, len(pool), rng.integers(0, 8))])
                elif kind == 1:
                    t = now - S * int(rng.integers(-3, 40)) + int(rng.choice([0, 0, 0, 17]))
                    rows.append([t] + [pool[i] for i in rng.integers(0, len(pool), 5)])
                elif kind == 2:
                    t = now - S * int(rng.integers(1, 40)); c = 120 * (1 + rng.normal(0, 0.002))
                    rows.append([t, c, c * 1.001, c * 0.999, c, 5.0])
                else:
                    rows.append(None)
            try:
                b = calm.clean_bars(rows, now)
            except Exception as e:                       # noqa: BLE001
                self.fail(f'raised on trial {trial}: {e!r} for {rows!r}')
            if b is None:
                continue
            ts, o, h, l, c, v = b
            self.assertTrue(np.all(np.diff(ts) > 0))                               # sorted, one per slot
            self.assertTrue(np.all(ts % S == 0) and np.all(ts + S <= now))         # aligned and closed
            for x in (o, h, l, c):
                self.assertTrue(np.all(np.isfinite(x)) and np.all(x > 0))
            self.assertTrue(np.all(l <= np.minimum(o, c)) and np.all(np.maximum(o, c) <= h))
            self.assertTrue(np.all(v >= 0))

    def test_the_forming_bar_and_a_misaligned_bar_are_refused(self):
        now = slot_now() + 30
        rows = [[slot_now(), 1, 1, 1, 1, 1],                   # still forming
                [slot_now() - S + 60, 1, 1, 1, 1, 1],          # not a slot
                [slot_now() - S, 1, 1.1, 0.9, 1, 1]]
        b = calm.clean_bars(rows, now)
        self.assertEqual(list(b[0]), [slot_now() - S])

    def test_fit_matches_the_level_or_refuses(self):
        b = tape(n=50)
        self.assertIsNotNone(calm.fit_surrogate(b, 120.0))
        self.assertIsNotNone(calm.fit_surrogate(b, 120.0 * 1.004))
        self.assertIsNone(calm.fit_surrogate(b, 120.0 * 1.01))                     # another market
        for bad in (0, -1, None, float('nan'), float('inf')):
            self.assertIsNone(calm.fit_surrogate(b, bad), bad)
        self.assertIsNone(calm.fit_surrogate(None, 120.0))

    def test_fit_inverts_a_pair_listed_the_other_way_up(self):
        b = tape(n=50)
        inv = (b[0], 1 / b[1], 1 / b[3], 1 / b[2], 1 / b[4], b[5])
        f = calm.fit_surrogate(inv, 120.0)
        np.testing.assert_allclose(f[4], b[4]); np.testing.assert_allclose(f[2], b[2]); np.testing.assert_allclose(f[3], b[3])
        self.assertTrue(np.all(f[2] >= f[3]))

    def test_the_join_refuses_a_surrogate_that_disagrees_with_gecko(self):
        g = tape(n=50)
        near = tuple(x.copy() for x in g); near[4][:] *= 1.0001
        far = tuple(x.copy() for x in g)
        for i in (1, 2, 3, 4):
            far[i][:-1] *= 1.004                                      # 40 bp off where both have bars
        self.assertIsNotNone(calm.fit_surrogate(near, 120.0, ref=g))
        self.assertIsNone(calm.fit_surrogate(far, 120.0, ref=g))
        few = tuple(x[-2:] for x in g)                                # below SURROGATE_JOIN_MIN: level check only
        self.assertIsNotNone(calm.fit_surrogate(far, 120.0, ref=few))

    def test_range_scale_widens_and_never_narrows(self):
        b = tape(n=50)
        wide = calm.fit_surrogate(b, 120.0, range_scale=1.15)
        np.testing.assert_allclose(np.log(wide[2] / wide[3]), 1.15 * np.log(b[2] / b[3]))
        np.testing.assert_allclose(wide[4], b[4])
        same = calm.fit_surrogate(b, 120.0, range_scale=0.5)
        np.testing.assert_allclose(same[2], b[2]); np.testing.assert_allclose(same[3], b[3])

    def test_missing_slots_and_freshness(self):
        now = slot_now() + 200
        b = tape(n=300, end=slot_now())
        self.assertEqual(calm.missing_slots(b[0], now, 6 * 3600), [])
        self.assertTrue(calm.tape_fresh(b[0], now))
        gap = tape(n=300, end=slot_now(), drop={slot_now() - 3 * S})
        self.assertEqual(calm.missing_slots(gap[0], now, 3600), [slot_now() - 3 * S])
        self.assertFalse(calm.tape_fresh(gap[0], now))
        self.assertFalse(calm.tape_fresh(b[0], now + 3600))                         # old
        self.assertFalse(calm.tape_fresh(None, now)); self.assertFalse(calm.tape_fresh(b[0][:3], now))

    def test_a_lone_bar_after_a_gap_is_not_fresh(self):
        # 2026-09-29: bars to 10:55, nothing, one bar at 11:20, checked at 11:26
        end = slot_now()
        lone = tape(n=300, end=end, drop={end - k * S for k in range(2, 7)})
        self.assertLessEqual(time.time() - lone[0][-1], 900)                        # the old age check passed it
        self.assertFalse(calm.tape_fresh(lone[0], time.time()))


class Sources(unittest.TestCase):
    def test_a_source_that_raises_or_returns_junk_is_skipped(self):
        good = rows_of(tape(n=40))
        calls = []

        def boom(sym, start):
            calls.append(('boom', sym)); raise RuntimeError('down')

        def junk(sym, start):
            calls.append(('junk', sym)); return [['x'] * 6, None, [1, 2]]

        def ok(sym, start):
            calls.append(('ok', sym)); return good if sym == 'SOLUSDC' else []
        srcs = (('A', calm.binance_symbols, boom, 1.0), ('B', calm.binance_symbols, junk, 1.0),
                ('C', calm.binance_symbols, ok, 1.0))
        name, b = calm.surrogate_5m('SOL/USDC', 0, 120.0, sources=srcs)
        self.assertEqual(name, 'C'); self.assertIsNotNone(b)
        self.assertEqual(calm.surrogate_5m('SOL/USDC', 0, 120.0, sources=srcs[:2]), (None, None))

    def test_an_unknown_pair_asks_nobody(self):
        f = mock.Mock(side_effect=AssertionError('asked'))
        self.assertEqual(calm.surrogate_5m(None, 0, 120.0, sources=(('X', calm.binance_symbols, f, 1.0),)), (None, None))
        f.assert_not_called()

    def test_the_other_way_up_symbol_is_tried(self):
        b = tape(n=40)
        inv = rows_of((b[0], 1 / b[1], 1 / b[3], 1 / b[2], 1 / b[4], b[5]))
        f = lambda sym, start: inv if sym == 'USDCSOL' else []                      # noqa: E731
        name, got = calm.surrogate_5m('SOL/USDC', 0, 120.0, sources=(('X', calm.binance_symbols, f, 1.0),))
        np.testing.assert_allclose(got[4], b[4])

    def test_binance_fetch_survives_every_answer(self):
        for answer in (None, {'code': -1121, 'msg': 'Invalid symbol.'}, {'code': -1003}, [], 'x',
                       [[1, 2]], [None], RuntimeError('curl')):
            side = answer if isinstance(answer, Exception) else None
            with mock.patch.object(engine, 'curl', side_effect=side, return_value=answer):
                rows = calm.binance_5m('SOLUSDC', 0)
            self.assertIsInstance(rows, list)
            self.assertIsNone(calm.clean_bars(rows, time.time()))

    def test_binance_fetch_is_bounded(self):
        seen = []

        def curl(url, **kw):
            seen.append(kw); return [[int(i * S * 1000), '1', '1', '1', '1', '0', 0, '5'] for i in range(1000)]
        with mock.patch.object(engine, 'curl', curl):
            calm.binance_5m('SOLUSDC', 0, pages=2)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(k['retries'] == 0 and k['max_time'] <= 10 for k in seen))

    def test_binance_rows_parse(self):
        b = tape(n=30)
        k = [[int(t) * 1000, str(o), str(h), str(l), str(c), '1', 0, str(v)] for t, o, h, l, c, v in zip(*b)]
        with mock.patch.object(engine, 'curl', return_value=k):
            got = calm.clean_bars(calm.binance_5m('SOLUSDC', 0), time.time())
        np.testing.assert_allclose(got[4], b[4]); np.testing.assert_allclose(got[5], b[5])


class Overlay(unittest.TestCase):
    """rebalancer.with_surrogate: GeckoTerminal wins; the surrogate fills."""

    def setUp(self):
        lp.tape._SURR.clear(); lp.tape.LAST_SURROGATE.clear()

    def tearDown(self):
        lp.tape._SURR.clear(); lp.tape.LAST_SURROGATE.clear()

    def run_it(self, gecko, surr=None, calls=None, pool='P'):
        def fake(pair, start, price, ref=None):
            if calls is not None:
                calls.append(start)
            if isinstance(surr, Exception):
                raise surr
            return ('Binance', surr) if surr is not None else (None, None)
        with mock.patch.object(calm, 'surrogate_5m', fake):
            return lp.tape.with_surrogate(pool, gecko, 120.0, 'SOL/USDC')

    def test_a_complete_gecko_tape_never_asks_the_surrogate(self):
        g = tape()
        with mock.patch.object(calm, 'surrogate_5m', side_effect=AssertionError('asked')):
            out = lp.tape.with_surrogate('P', g, 120.0, 'SOL/USDC')
        self.assertIs(out, g); self.assertEqual(lp.tape.LAST_SURROGATE['P']['source'], 'Gecko')

    def test_gaps_are_filled_and_gecko_wins_where_it_has_a_bar(self):
        end = slot_now(); holes = {end - k * S for k in range(1, 6)}
        g = tape(end=end, drop=holes); full = tape(end=end)
        other = tuple(x.copy() for x in full); other[4][:] *= 1.0002           # the surrogate's own values
        out = self.run_it(g, other)
        self.assertEqual(len(out[0]), len(full[0]))
        at = dict(zip(out[0].astype(int), out[4]))
        for t, c in zip(g[0].astype(int), g[4]):
            self.assertEqual(at[t], c)                                         # GeckoTerminal untouched
        for t in holes:
            self.assertAlmostEqual(at[t], dict(zip(full[0].astype(int), full[4]))[t] * 1.0002)
        src = lp.tape.LAST_SURROGATE['P']
        self.assertEqual(src['source'], 'Gecko+Binance'); self.assertEqual(src['filled_1h'], 5)
        self.assertTrue(calm.tape_fresh(out[0], time.time()))

    def test_a_whole_hour_from_the_surrogate_says_so(self):
        end = slot_now()
        g = tape(end=end, drop={end - k * S for k in range(1, 20)})
        self.run_it(g, tape(end=end))
        self.assertEqual(lp.tape.LAST_SURROGATE['P']['source'], 'Binance')

    def test_every_surrogate_failure_returns_gecko_as_it_is(self):
        end = slot_now(); g = tape(end=end, drop={end - S})
        for surr in (None, RuntimeError('down')):
            lp.tape._SURR.clear()
            out = self.run_it(g, surr)
            self.assertIs(out, g)
        with mock.patch.object(calm, 'missing_slots', side_effect=ValueError('bug')):
            self.assertIs(lp.tape.with_surrogate('P', g, 120.0), g)
        self.assertIsNone(lp.tape.with_surrogate('P', None, 120.0))

    def test_asks_at_most_once_per_refresh_and_keeps_bars_a_failed_ask_gave(self):
        end = slot_now(); g = tape(end=end, drop={end - S, end - 2 * S}); calls = []
        full = tape(end=end, drop={end - 3 * S})                          # the surrogate lacks one slot too
        self.run_it(g, full, calls)
        self.assertEqual(len(calls), 1)
        g2 = tape(end=end, drop={end - S, end - 2 * S, end - 3 * S})         # a new hole, inside the refresh
        self.run_it(g2, full, calls)
        self.assertEqual(len(calls), 1)
        t, name, s = lp.tape._SURR['P']
        lp.tape._SURR['P'] = (t - 10 * lp.tape.SURROGATE_REFRESH, name, s)
        out = self.run_it(g2, None, calls)                                   # the next ask fails
        self.assertEqual(len(calls), 2)
        self.assertIn(end - S, set(out[0].astype(int)))                      # the earlier fill stays

    def test_holes_already_filled_are_not_asked_again(self):
        end = slot_now(); g = tape(end=end, drop={end - 50 * S}); calls = []
        self.run_it(g, tape(end=end), calls)
        t, name, s = lp.tape._SURR['P']
        lp.tape._SURR['P'] = (t - 10 * lp.tape.SURROGATE_REFRESH, name, s)
        self.run_it(g, tape(end=end), calls)
        self.assertEqual(len(calls), 1)

    def test_back_to_gecko_when_it_is_complete(self):
        end = slot_now(); g = tape(end=end, drop={end - S})
        self.run_it(g, tape(end=end))
        self.assertEqual(lp.tape.LAST_SURROGATE['P']['source'], 'Gecko+Binance')
        full = tape(end=end)
        with mock.patch.object(calm, 'surrogate_5m', side_effect=AssertionError('asked')):
            out = lp.tape.with_surrogate('P', full, 120.0, 'SOL/USDC')
        self.assertIs(out, full)
        self.assertEqual(lp.tape.LAST_SURROGATE['P']['source'], 'Gecko'); self.assertNotIn('P', lp.tape._SURR)

    def test_the_cache_holds_one_pool_and_one_day(self):
        end = slot_now()
        for p in ('P1', 'P2', 'P3'):
            self.run_it(tape(end=end, drop={end - S}), tape(n=8000, end=end), pool=p)
        self.assertEqual(list(lp.tape._SURR), ['P3'])
        s = lp.tape._SURR['P3'][2]
        self.assertGreaterEqual(s[0][0], time.time() - lp.tape.SURROGATE_LOOKBACK_S - 3600 - S)


class NeverStored(unittest.TestCase):
    """Surrogate bars never reach the database: a late GeckoTerminal bar replaces them."""

    def setUp(self):
        lp.tape._TAPE5.clear(); lp.tape._SURR.clear()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate tape5')

    def tearDown(self):
        lp.tape._TAPE5.clear(); lp.tape._SURR.clear()

    def test_tape5_stores_gecko_only(self):
        end = slot_now(); holes = {end - k * S for k in range(1, 4)}
        g = tape(n=1000, end=end, drop=holes)
        with mock.patch.object(calm, 'tape_5m', lambda pool, live_price=None, before=None: None if before else g), \
                mock.patch.object(config, 'REGIME_TAPE_DAYS', 1), \
                mock.patch.object(calm, 'surrogate_5m', lambda *a, **k: ('Binance', tape(n=1000, end=end))):
            b = lp.tape.tape5('SURRPOOL', 120.0, 'SOL/USDC')
        self.assertTrue(holes <= set(b[0].astype(int)))
        with db.cursor() as cur:
            cur.execute("select ts from tape5 where pool = 'SURRPOOL'")
            stored = {int(r['ts']) for r in cur.fetchall()}
        self.assertTrue(stored); self.assertFalse(holes & stored)


class RegimeDefence(unittest.TestCase):
    """regime_view: STALE only when no source has the market; no flapping."""

    def view(self, state, bars, p=120.0, half=1.02):
        with mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.object(lp.tape, 'tape5', lambda pool, price, pair=None: bars), \
                mock.patch.object(lp.tape, 'liquidity_view', lambda *a: {'factor': 1.0}), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(db, 'event', lambda *a: None):
            lp.tape.LAST_SURROGATE.clear()
            return lp.regime.regime_view(state, {'price': p, 'lowerPrice': p / half, 'upperPrice': p * half})

    def test_replay_2026_09_29_both_sources_down_is_stale(self):
        end = slot_now()
        lone = tape(end=end, drop={end - k * S for k in range(2, 7)})
        v = self.view({'regime_mode': 'WARM'}, lone)
        self.assertEqual(v['mode'], 'STALE'); self.assertEqual(v['choice'], W[-1])
        self.assertNotEqual(calm.regime_decide(v, widths=W, steps=2), 'narrow')
        self.assertEqual(v['data']['source'], 'none')

    def test_leaving_stale_waits_for_a_complete_tape(self):
        state = {'regime_mode': 'STALE'}; b = tape()
        v = self.view(state, b)
        self.assertEqual(v['mode'], 'STALE'); self.assertGreater(v['unstale_in_s'], 0)
        state['tape_fresh_since'] -= lp.regime.REGIME_UNSTALE_S + 1
        v = self.view(state, b)
        self.assertNotEqual(v['mode'], 'STALE'); self.assertFalse(v.get('stale'))

    def test_a_flickering_tape_never_leaves_stale(self):
        end = slot_now()
        good, bad = tape(end=end), tape(end=end, drop={end - 2 * S})
        state = {'regime_mode': 'WARM'}; modes = []
        for k in range(40):
            modes.append(self.view(state, good if k % 3 else bad)['mode'])
            if state.get('tape_fresh_since') is not None:
                state['tape_fresh_since'] -= 200                          # polls a few minutes apart
        self.assertEqual(set(modes), {'STALE'})

    def test_a_stale_view_never_narrows(self):
        end = slot_now()
        v = self.view({'regime_mode': 'WARM'}, tape(end=end - 3600), half=W[-1])
        self.assertTrue(v['stale'])
        ract = calm.regime_decide(v, widths=W, steps=2)
        self.assertNotEqual(ract, 'narrow')

    def test_a_one_step_source_difference_does_not_move_the_band(self):
        for i in range(1, len(W) - 1):
            for d in (-1, 1):
                v = {'held': W[i], 'choice': W[i + d], 'inside': True}
                self.assertIsNone(calm.regime_decide(v, widths=W, steps=2))


class SourceMessages(unittest.TestCase):
    def run_seq(self, labels):
        sent, state = [], {}
        with mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append(kw['source'])):
            for lab in labels:
                n = 0 if lab in ('Gecko', 'none') else 4
                lp.regime.track_tape_source(state, {'source': lab, 'surrogate': 'Binance' if n else None,
                                                     'filled_1h': n, 'bars_1h': 12, 'filled_24h': n})
        return sent

    def test_one_message_per_change_and_none_at_a_normal_start(self):
        self.assertEqual(self.run_seq(['Gecko', 'Gecko']), [])
        self.assertEqual(self.run_seq(['Gecko', 'Gecko+Binance', 'Binance', 'Gecko+Binance', 'Binance', 'Gecko']),
                         ['Gecko+Binance', 'Gecko'])
        self.assertEqual(self.run_seq(['Gecko', 'none', 'none', 'Binance', 'Gecko']), ['none', 'Binance', 'Gecko'])

    def test_tape_source_labels(self):
        now = time.time(); b = tape(end=slot_now())
        f = lp.tape.tape_source
        self.assertEqual(f(b[0], [], now, None, True)['source'], 'Gecko')
        self.assertEqual(f(b[0], b[0][-3:], now, 'Binance', True)['source'], 'Gecko+Binance')
        self.assertEqual(f(b[0], b[0][-20:], now, 'Binance', True)['source'], 'Binance')
        self.assertEqual(f(b[0], b[0][-3:], now, 'Binance', False)['source'], 'none')
        self.assertEqual(f(b[0], b[0][-500:-400], now, 'Binance', True)['source'], 'Gecko')   # old holes only



class Boundaries(unittest.TestCase):
    """Edges the mutation run found untested."""

    def test_tape_fresh_edges(self):
        end = slot_now(); ts = np.arange(end - 6 * S, end, S, dtype=float)       # exactly FRESH_BARS bars
        self.assertIs(calm.tape_fresh(ts, ts[-1] + 900), True)                     # age exactly the limit
        self.assertIs(calm.tape_fresh(ts, ts[-1] + 901), False)
        self.assertIs(calm.tape_fresh(ts[-3:], ts[-1] + 60), False)                # recent but too short
        self.assertIs(calm.tape_fresh(None, 0), False)
        off = ts.copy(); off[-1] += 1                                              # 301 s apart: not a slot
        self.assertIs(calm.tape_fresh(off, off[-1] + 60), False)
        off[-1] -= 0.5                                                             # float noise is fine
        self.assertIs(calm.tape_fresh(off, off[-1] + 60), True)

    def test_tape_source_hour_edge(self):
        end = slot_now(); ts = np.arange(end - 30 * S, end, S, dtype=float)
        now = float(end)                                                           # a bar sits at now - 3900
        self.assertEqual(lp.tape.tape_source(ts, [], now, None, True)['bars_1h'], 13)
        self.assertEqual(lp.tape.tape_source(ts, [], now + 1, None, True)['bars_1h'], 12)

    def test_binance_parses_every_column_and_pages_from_the_last_bar(self):
        b = tape(n=1500)
        k = [[int(t) * 1000, str(o), str(h), str(l), str(c), '1', 0, str(v)] for t, o, h, l, c, v in zip(*b)]
        urls = []

        def curl(url, **kw):
            urls.append(url)
            start = int(url.split('startTime=')[1])
            return [r for r in k if r[0] >= start][:1000]
        with mock.patch.object(engine, 'curl', curl):
            got = calm.clean_bars(calm.binance_5m('SOLUSDC', b[0][0]), time.time())
        for i in range(6):
            np.testing.assert_allclose(got[i], b[i])
        self.assertEqual(len(urls), 2)
        self.assertEqual(int(urls[1].split('startTime=')[1]), k[999][0] + 1)


class OverlayWiring(unittest.TestCase):
    def setUp(self):
        lp.tape._SURR.clear(); lp.tape.LAST_SURROGATE.clear()

    tearDown = setUp

    def test_the_held_pool_defaults_to_its_pair_and_another_pool_to_none(self):
        end = slot_now(); g = tape(end=end, drop={end - S}); asked = []

        def fake(pair, start, price, ref=None):
            asked.append(pair); return None, None
        with mock.patch.object(calm, 'surrogate_5m', fake):
            lp.tape.with_surrogate(config.POOL, g, 120.0)
            lp.tape._SURR.clear()
            lp.tape.with_surrogate('SOMEOTHERPOOL', g, 120.0)
            lp.tape._SURR.clear()
            lp.tape.with_surrogate('SOMEOTHERPOOL', g, 120.0, 'SOL/USDT')
        self.assertEqual(asked, [config.PAIR_LABEL, None, 'SOL/USDT'])

    def test_a_complete_tape_reports_its_hour(self):
        g = tape()
        lp.tape.with_surrogate('P', g, 120.0, 'SOL/USDC')
        src = lp.tape.LAST_SURROGATE['P']
        self.assertGreaterEqual(src['bars_1h'], 11); self.assertEqual(src['filled_1h'], 0)
        self.assertIsNone(src['surrogate'])

    def test_the_cache_keeps_exactly_the_last_day(self):
        end = slot_now()
        with mock.patch.object(calm, 'surrogate_5m', lambda *a, **k: ('Binance', tape(n=8000, end=end))):
            lp.tape.with_surrogate('P', tape(end=end, drop={end - S}), 120.0, 'SOL/USDC')
        s = lp.tape._SURR['P'][2]
        edge = time.time() - lp.tape.SURROGATE_LOOKBACK_S - 3600
        self.assertGreaterEqual(s[0][0], edge); self.assertLessEqual(s[0][0], edge + S)


class ViewWiring(unittest.TestCase):
    def view(self, state, bars, status=None, factor=1.0, surr=None, seen=None):
        p = 120.0
        status = status or {'price': p, 'lowerPrice': p / 1.02, 'upperPrice': p * 1.02}

        def t5(pool, price, pair=None):
            if seen is not None:
                seen.append(pool)
            if surr is not None:
                lp.tape.LAST_SURROGATE[pool] = surr
            return bars
        with mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.object(lp.tape, 'tape5', t5), \
                mock.patch.object(lp.tape, 'liquidity_view', lambda *a: {'factor': factor}), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(db, 'event', lambda *a: None):
            lp.tape.LAST_SURROGATE.clear()
            return lp.regime.regime_view(state, status)

    def test_no_tape_is_no_view(self):
        self.assertIsNone(self.view({}, None))

    def test_the_view_reads_the_held_pool(self):
        seen = []; p = 120.0
        self.view({}, tape(), status={'price': p, 'lowerPrice': p / 1.02, 'upperPrice': p * 1.02, 'whirlpool': 'HELD'}, seen=seen)
        self.view({}, tape(), seen=seen)
        self.assertEqual(seen, ['HELD', config.POOL])

    def test_the_liquidity_threshold_is_clamped(self):
        base = config.REGIME_THRESHOLD
        self.assertAlmostEqual(self.view({}, tape(), factor=1.0)['threshold'], min(max(base, 0.05), 0.40))
        self.assertAlmostEqual(self.view({}, tape(), factor=1e6)['threshold'], 0.40)
        self.assertAlmostEqual(self.view({}, tape(), factor=1e-6)['threshold'], 0.05)
        self.assertAlmostEqual(self.view({}, tape(), factor=0.2 / base)['threshold'], 0.2)

    def test_the_stale_width_is_reported_exactly(self):
        v = self.view({'regime_mode': 'WARM'}, tape(end=slot_now() - 7200))
        self.assertEqual(v['choice_pct'], round((W[-1] - 1) * 100, 2)); self.assertIsNone(v['unstale_in_s'])

    def test_the_view_carries_the_overlay_source(self):
        src = {'source': 'Gecko+Binance', 'surrogate': 'Binance', 'filled_1h': 3, 'bars_1h': 12, 'filled_24h': 3}
        v = self.view({}, tape(), surr=src)
        self.assertEqual(v['data']['source'], 'Gecko+Binance'); self.assertEqual(v['data']['filled_1h'], 3)
        stale = self.view({}, tape(end=slot_now() - 7200), surr=dict(src, source='Gecko'))
        self.assertEqual(stale['data']['source'], 'none')


class SourceMessageText(unittest.TestCase):
    def go(self, first, labels):
        sent, state = [], {}
        with mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append(kw)):
            for lab in [first] + labels:
                n = 0 if lab in ('Gecko', 'none') else 4
                lp.regime.track_tape_source(state, {'source': lab, 'surrogate': 'Binance' if n else None,
                                                     'filled_1h': n, 'bars_1h': 12, 'filled_24h': n})
        return sent

    def test_a_start_during_an_outage_says_so(self):
        self.assertEqual([m['kind'] for m in self.go('Binance', [])], ['surrogate'])
        self.assertEqual([m['kind'] for m in self.go('none', [])], ['none'])

    def test_each_message_names_its_state(self):
        m = self.go('Gecko', ['Gecko+Binance', 'none', 'Gecko'])
        self.assertIn('Binance fills 4 of 12', m[0]['detail'])
        self.assertIn('STALE', m[1]['detail'])
        self.assertIn('complete again', m[2]['detail'])



class CleanBarsOracle(unittest.TestCase):
    """Each valid row with one field broken: the exact expected result."""

    def base(self, now):
        t = float(slot_now() - 10 * S)
        return [t, 120.0, 121.0, 119.0, 120.5, 7.0], now

    def keep(self, row, now):
        b = calm.clean_bars([row], now)
        return None if b is None else [float(x[0]) for x in b]

    def test_valid_rows_pass_unchanged(self):
        row, now = self.base(time.time())
        self.assertEqual(self.keep(row, now), row)
        self.assertEqual(self.keep(row + ['extra', None], now), row)                 # extra columns ignored
        edge = [row[0], 120.0, 120.5, 120.0, 120.5, 1.0]                              # low = open, high = close
        self.assertEqual(self.keep(edge, now), edge)
        closes_now = [row[0]] + row[1:]
        self.assertEqual(self.keep(closes_now, row[0] + S), closes_now)              # closed exactly now
        self.assertEqual(self.keep([row[0], 120, 121, 119, 120.5, 0.5], now)[5], 0.5)
        self.assertEqual(self.keep([str(x) for x in row], now), row)                 # numeric strings

    def test_each_broken_field_drops_the_row(self):
        row, now = self.base(time.time())
        bad = {1: [0.0, -1.0, float('nan'), float('inf'), 'x', None],
               2: [0.0, -1.0, float('nan'), float('inf'), 120.4],                    # high below close
               3: [0.0, -1.0, float('nan'), float('-inf'), 120.1],                   # low above open
               4: [0.0, -1.0, float('nan'), float('inf'), 121.5, 118.0],
               0: [row[0] + 1, row[0] + 17, float('nan'), float('inf'), float(slot_now() + S), 'x']}
        for i, values in bad.items():
            for x in values:
                r = list(row); r[i] = x
                self.assertIsNone(self.keep(r, now), (i, x))
        self.assertIsNone(self.keep(row[:5], now))                                   # too short
        self.assertIsNone(self.keep([row[0] + S * 11] + row[1:], row[0] + S * 11 + S - 1))  # still forming

    def test_a_broken_volume_is_zero_not_a_dropped_bar(self):
        row, now = self.base(time.time())
        for v in (float('nan'), float('inf'), -3.0):
            r = list(row); r[5] = v
            self.assertEqual(self.keep(r, now)[5], 0.0, v)

    def test_one_bar_per_slot_sorted(self):
        row, now = self.base(time.time())
        later = [row[0] + S] + row[1:]
        dup = [row[0], 100.0, 101.0, 99.0, 100.0, 1.0]
        b = calm.clean_bars([later, row, dup], now)
        self.assertEqual(list(b[0]), [row[0], row[0] + S]); self.assertEqual(b[4][0], 100.0)   # the last copy wins


class FitOracle(unittest.TestCase):
    def test_empty_and_low_priced_inputs(self):
        e = tuple(np.array([]) for _ in range(6))
        self.assertIsNone(calm.fit_surrogate(e, 120.0))
        b = tape(n=30, price=0.8)                                                     # a pair priced below 1
        self.assertIsNotNone(calm.fit_surrogate(b, 0.8))

    def test_only_the_newest_close_sets_the_level(self):
        b = tape(n=30); c = b[4].copy(); c[:-1] *= 1.05
        moved = (b[0], np.minimum(b[1], c), np.maximum(b[2], c * 1.001), np.minimum(b[3], c * 0.999), c, b[5])
        self.assertIsNotNone(calm.fit_surrogate(moved, 120.0))

    def test_inversion_keeps_every_column(self):
        b = tape(n=30)
        inv = (b[0], 1 / b[1], 1 / b[3], 1 / b[2], 1 / b[4], b[5])
        f = calm.fit_surrogate(inv, 120.0)
        for i in range(6):
            np.testing.assert_allclose(f[i], b[i])

    def test_join_edges(self):
        g = tape(n=30)
        far = tuple(x.copy() for x in g)
        for i in (1, 2, 3, 4):
            far[i][:-1] *= 1.004
        two = tuple(x[-3:-1] for x in g)                                              # two disagreeing bars: too few
        self.assertIsNotNone(calm.fit_surrogate(far, 120.0, ref=two))
        three = tuple(x[-4:-1] for x in g)                                            # exactly the minimum: checked
        self.assertIsNone(calm.fit_surrogate(far, 120.0, ref=three))
        zero = tuple(x.copy() for x in g); zero[4][:5] = 0.0                          # a broken reference bar
        self.assertIsNotNone(calm.fit_surrogate(g, 120.0, ref=zero))
        low = tape(n=30, price=0.8); lowfar = tuple(x.copy() for x in low)
        for i in (1, 2, 3, 4):
            lowfar[i][:-1] *= 1.004
        self.assertIsNone(calm.fit_surrogate(lowfar, 0.8, ref=low))                  # sub-1 references count


class BinanceFetchOracle(unittest.TestCase):
    def test_the_first_ask_starts_at_start_ts_and_a_short_page_ends_it(self):
        urls = []

        def curl(url, **kw):
            urls.append(url); return [[int(S * 1000), '1', '1', '1', '1', '0', 0, '5']]
        with mock.patch.object(engine, 'curl', curl):
            calm.binance_5m('SOLUSDC', 1790000100)
        self.assertEqual(len(urls), 1)
        self.assertEqual(int(urls[0].split('startTime=')[1]), 1790000100 * 1000)
        self.assertIn('symbol=SOLUSDC&interval=5m&limit=1000', urls[0])

    def test_two_pages_by_default(self):
        n = []

        def curl(url, **kw):
            n.append(1); return [[int(i * S * 1000), '1', '1', '1', '1', '0', 0, '5'] for i in range(1000)]
        with mock.patch.object(engine, 'curl', curl):
            calm.binance_5m('SOLUSDC', 0)
        self.assertEqual(len(n), 2)



class LastEdges(unittest.TestCase):
    def test_clean_bars_of_nothing(self):
        self.assertIsNone(calm.clean_bars(None, time.time()))
        self.assertIsNone(calm.clean_bars([], time.time()))

    def test_only_the_newest_close_sets_the_level_the_other_way_up(self):
        b = tape(n=30); c = b[4].copy(); c[:-1] *= 1.05
        moved = (b[0], np.minimum(b[1], c), np.maximum(b[2], c * 1.001), np.minimum(b[3], c * 0.999), c, b[5])
        inv = (moved[0], 1 / moved[1], 1 / moved[3], 1 / moved[2], 1 / moved[4], moved[5])
        f = calm.fit_surrogate(inv, 120.0)
        self.assertIsNotNone(f); np.testing.assert_allclose(f[4], c)

    def test_the_view_counts_its_hour_without_an_overlay(self):
        v = ViewWiring.view(ViewWiring(), {}, tape())
        self.assertGreaterEqual(v['data']['bars_1h'], 11); self.assertEqual(v['data']['source'], 'Gecko')


if __name__ == '__main__':
    unittest.main()
