"""CALM on the swing's open pool keeps it on the closed pool (owner,
2026-10-09: "DJT in CALM sucks, must never happen; if things are CALM,
sol-swing must be on SOL").

The bot writes the open pool's mode each poll (rebalancer.publish_swing_regime,
run/<profile>/swing_regime.json); swing.py reads it: CALM, or CALM less than
CALM_CLEAR_S ago, means the closed pool even in market hours."""
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401
import config
import db
import rebalancer
import swing
from test_swing import CLOSED, DJT_POOL, OPEN, ROW, Tick, ny

NOW = 1_800_000_000.0


def view(mode, at=NOW, last_calm=None, pool=DJT_POOL):
    return {'pool': pool, 'mode': mode, 'at': at, 'last_calm_at': last_calm}


class OpenCalm(unittest.TestCase):
    def test_calm_now_is_calm(self):
        self.assertTrue(swing.open_calm(NOW, view('CALM', last_calm=NOW), DJT_POOL))

    def test_warm_or_hot_never_calm_is_not(self):
        for m in ('WARM', 'HOT', 'STALE'):
            self.assertFalse(swing.open_calm(NOW, view(m), DJT_POOL), m)

    def test_it_waits_calm_clear_s_before_returning(self):
        just = view('WARM', last_calm=NOW - swing.CALM_CLEAR_S + 1)
        self.assertTrue(swing.open_calm(NOW, just, DJT_POOL))
        done = view('WARM', last_calm=NOW - swing.CALM_CLEAR_S)
        self.assertFalse(swing.open_calm(NOW, done, DJT_POOL))

    def test_a_view_without_a_time_is_no_view(self):
        self.assertFalse(swing.open_calm(NOW, {'pool': DJT_POOL, 'mode': 'CALM'}, DJT_POOL))
        self.assertFalse(swing.open_calm(NOW, {'pool': DJT_POOL, 'mode': 'CALM', 'at': None}, DJT_POOL))

    def test_an_old_view_or_another_pool_or_none_is_no_view(self):
        self.assertFalse(swing.open_calm(NOW, view('CALM', at=NOW - swing.REGIME_MAX_AGE_S - 1), DJT_POOL))
        self.assertTrue(swing.open_calm(NOW, view('CALM', at=NOW - swing.REGIME_MAX_AGE_S), DJT_POOL))
        self.assertFalse(swing.open_calm(NOW, view('CALM', pool='other'), DJT_POOL))
        self.assertFalse(swing.open_calm(NOW, None, DJT_POOL))
        self.assertFalse(swing.open_calm(NOW, {}, DJT_POOL))

    @settings(max_examples=300, deadline=None)
    @given(st.sampled_from(['CALM', 'WARM', 'HOT', 'STALE']), st.floats(0, 5000), st.one_of(st.none(), st.floats(0, 10_000)))
    def test_property_calm_iff_fresh_and_calm_recently(self, mode, age, since):
        v = view(mode, at=NOW - age, last_calm=None if since is None else NOW - since)
        want = age <= swing.REGIME_MAX_AGE_S and (mode == 'CALM' or (since is not None and since < swing.CALM_CLEAR_S))
        self.assertEqual(swing.open_calm(NOW, v, DJT_POOL), want)


class Wanted(unittest.TestCase):
    def test_market_hours_and_calm_is_the_closed_pool(self):
        t = ny(2026, 10, 9, 11, 0)
        self.assertEqual(swing.wanted(t, dict(ROW, open_calm=True)), CLOSED)
        self.assertEqual(swing.wanted(t, dict(ROW, open_calm=False)), OPEN)
        self.assertEqual(swing.wanted(t, ROW), OPEN)                       # no view: the calendar alone

    def test_market_closed_is_the_closed_pool_whatever_the_mode(self):
        t = ny(2026, 10, 9, 20, 0)
        for c in (True, False):
            self.assertEqual(swing.wanted(t, dict(ROW, open_calm=c)), CLOSED)


class TickCalm(Tick):
    def regime(self, mode, at, last_calm=None):
        f = self.tmp / 'run' / 'tk-swing' / swing.REGIME_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(view(mode, at=at, last_calm=last_calm)))

    def test_calm_at_the_open_stays_on_sol(self):
        t = ny(2026, 10, 9, 9, 40)
        self.regime('CALM', t.timestamp(), t.timestamp())
        swing.tick_all(t)
        self.assertIsNone(self.migrate())                                  # holds SOL/USDC
        self.assertEqual(self.feed(), [])

    def test_calm_on_djt_moves_to_sol_with_calm_in_the_event(self):
        t = ny(2026, 10, 9, 11, 0)
        with db.cursor(commit=True) as cur:
            cur.execute("update config set pool = %s where name = 'tk-swing'", (DJT_POOL,))
        self.regime('CALM', t.timestamp(), t.timestamp())
        swing.tick_all(t)
        self.assertEqual(self.migrate(), f'{CLOSED[0]} {CLOSED[1]}')
        ev = [json.loads(x) for x in swing.FEED.read_text().splitlines()][-1]
        self.assertEqual((ev['event'], ev['market'], ev['calm']), ('SWING', 'open', True))

    def test_back_to_djt_only_after_calm_clear(self):
        t = ny(2026, 10, 9, 11, 0)
        self.regime('WARM', t.timestamp(), t.timestamp() - 600)
        swing.tick_all(t)
        self.assertIsNone(self.migrate())
        later = t.timestamp() + swing.CALM_CLEAR_S
        self.regime('WARM', later, t.timestamp() - 600)
        swing.tick_all(ny(2026, 10, 9, 11, 30))
        self.assertEqual(self.migrate(), f'orca {DJT_POOL}')

    def test_an_unreadable_file_is_the_calendar_alone(self):
        f = self.tmp / 'run' / 'tk-swing' / swing.REGIME_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('{not json')
        swing.tick_all(ny(2026, 10, 9, 11, 0))
        self.assertEqual(self.migrate(), f'orca {DJT_POOL}')


class Record(unittest.TestCase):
    def test_calm_sets_last_calm_at(self):
        self.assertEqual(rebalancer.swing_regime_record(None, 'CALM', 'P', 5.0),
                         {'pool': 'P', 'mode': 'CALM', 'at': 5.0, 'last_calm_at': 5.0})

    def test_not_calm_keeps_the_last_for_the_same_pool_only(self):
        prev = {'pool': 'P', 'mode': 'CALM', 'at': 1.0, 'last_calm_at': 1.0}
        self.assertEqual(rebalancer.swing_regime_record(prev, 'WARM', 'P', 9.0)['last_calm_at'], 1.0)
        self.assertIsNone(rebalancer.swing_regime_record(prev, 'WARM', 'Q', 9.0)['last_calm_at'])
        self.assertIsNone(rebalancer.swing_regime_record(None, 'HOT', 'P', 9.0)['last_calm_at'])


class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='lp_swreg_'))
        self.told = []
        for p in (mock.patch.object(rebalancer, 'RUN', self.tmp),
                  mock.patch.object(rebalancer, 'notify', lambda e, **k: self.told.append((e, k))),
                  mock.patch.object(rebalancer, 'swing_open_pool', lambda: OPEN),
                  mock.patch.object(config, 'POOL', CLOSED[1]),
                  mock.patch.object(config, 'REGIME_WIDTHS', (1.01, 1.0125, 1.015, 1.02, 1.025, 1.05))):
            p.start(); self.addCleanup(p.stop)

    def run_with(self, choice, held=None, pool_price=8.0):
        seen = {}

        def rc(pool, price, pair=None):
            seen['args'] = (pool, price)
            return choice
        with mock.patch.object(rebalancer, 'regime_choice_now', rc), \
                mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: {'price': pool_price}):
            rec = rebalancer.publish_swing_regime(held)
        return rec, seen

    def file(self):
        return json.loads((self.tmp / rebalancer.SWING_REGIME_F).read_text())

    def test_modes_from_the_width_choice(self):
        for choice, mode in ((1.01, 'CALM'), (1.0125, 'WARM'), (1.02, 'WARM'), (1.025, 'HOT'), (None, 'STALE')):
            rec, _ = self.run_with(choice)
            self.assertEqual((rec['mode'], self.file()['mode']), (mode, mode), choice)

    def test_on_sol_the_open_pool_is_read_at_its_own_price(self):
        _, seen = self.run_with(1.01, held=110.0, pool_price=8.06)
        self.assertEqual(seen['args'], (DJT_POOL, 8.06))

    def test_on_djt_the_held_price_is_used(self):
        with mock.patch.object(config, 'POOL', DJT_POOL):
            _, seen = self.run_with(1.01, held=8.1, pool_price=99.0)
        self.assertEqual(seen['args'], (DJT_POOL, 8.1))

    def test_a_change_of_mode_is_told_once(self):
        self.run_with(1.01); self.run_with(1.01); self.run_with(1.03)
        self.assertEqual([k['mode'] for e, k in self.told if e == 'swing_regime'], ['CALM', 'HOT'])
        self.assertIsNotNone(self.file()['last_calm_at'])                 # HOT now, CALM before: kept

    def test_on_djt_without_a_held_price_the_pool_price_is_used(self):
        with mock.patch.object(config, 'POOL', DJT_POOL):
            _, seen = self.run_with(1.01, held=None, pool_price=8.2)
        self.assertEqual(seen['args'], (DJT_POOL, 8.2))

    def test_an_unknown_pool_writes_nothing(self):
        with mock.patch.object(rebalancer, 'regime_choice_now', lambda *a, **k: 1.01), \
                mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: None):
            self.assertIsNone(rebalancer.publish_swing_regime(110.0))
        self.assertFalse((self.tmp / rebalancer.SWING_REGIME_F).exists())

    def test_no_price_writes_nothing(self):
        rec, _ = self.run_with(1.01, pool_price=None)
        self.assertIsNone(rec)
        self.assertFalse((self.tmp / rebalancer.SWING_REGIME_F).exists())

    def test_not_a_swing_writes_nothing(self):
        with mock.patch.object(rebalancer, 'swing_open_pool', lambda: None):
            self.assertIsNone(rebalancer.publish_swing_regime(100.0))
        self.assertFalse((self.tmp / rebalancer.SWING_REGIME_F).exists())


class OpenPoolRow(Tick):
    def test_only_a_swing_profile_has_an_open_pool(self):
        with mock.patch.object(config, 'SWING_POOLS', ()), mock.patch.object(config, 'PROFILE', 'tk-swing'):
            self.assertIsNone(rebalancer.swing_open_pool())              # a row, but no pinned swing pools

    def test_the_row_of_the_profile_from_the_database(self):
        with mock.patch.object(config, 'SWING_POOLS', (DJT_POOL, CLOSED[1])), \
                mock.patch.object(config, 'PROFILE', 'tk-swing'):
            self.assertEqual(rebalancer.swing_open_pool(), OPEN)
        with mock.patch.object(config, 'SWING_POOLS', (DJT_POOL, CLOSED[1])), \
                mock.patch.object(config, 'PROFILE', 'tk-other'):
            self.assertIsNone(rebalancer.swing_open_pool())               # no row
        with db.cursor(commit=True) as cur:
            cur.execute("update swing set enabled = false where profile = 'tk-swing'")
        with mock.patch.object(config, 'SWING_POOLS', (DJT_POOL, CLOSED[1])), \
                mock.patch.object(config, 'PROFILE', 'tk-swing'):
            self.assertIsNone(rebalancer.swing_open_pool())               # a disabled row


if __name__ == '__main__':
    unittest.main()
