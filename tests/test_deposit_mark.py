"""The deposit an open books: the chain's mark of the new position, never the
signer's estimate when the chain can be read.

2026-10-07: an Orca read right after a DJT open missed the new position, and
the ledger kept the signer's estimate on the requested band ($192.50). The
tick-rounded band took $179.18 and $13 stayed in the wallet; the per-pool
P&L showed DJT at -$19.66 for a -$5.73 session.
"""
import contextlib
import pathlib
import tempfile
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as hs

import _fixtures
import config      # noqa: E402
import db          # noqa: E402
import lp.moves  # noqa: E402
import lp.paths  # noqa: E402
import time  # noqa: E402


class OpenedMark(unittest.TestCase):
    def go(self, answers):
        reads, sleeps = [], []
        seq = iter(answers)

        def read(mint=None):
            reads.append(mint)
            return next(seq), None
        with mock.patch.object(lp.signers, 'read_status', read), \
                mock.patch.object(time, 'sleep', lambda s: sleeps.append(s)):
            return lp.moves.opened_mark('NEW'), reads, sleeps

    def test_the_first_read_that_shows_the_position(self):
        usd, reads, sleeps = self.go([{'positionMint': 'NEW', 'positionUsd': 179.18}])
        self.assertEqual((usd, reads, sleeps), (179.18, ['NEW'], []))

    def test_a_node_behind_is_asked_again(self):
        miss = [None, {'error': 'position not found'}, {'positionMint': 'OLD', 'positionUsd': 192.5}]
        for first in miss:
            usd, reads, sleeps = self.go([first, {'positionMint': 'NEW', 'positionUsd': 179.18}])
            self.assertEqual(usd, 179.18, first)
            self.assertEqual(reads, ['NEW', 'NEW']); self.assertEqual(sleeps, [lp.moves.OPEN_MARK_PAUSE_S])

    def test_unpriced_is_asked_again(self):
        usd, reads, _ = self.go([{'positionMint': 'NEW'}, {'positionMint': 'NEW', 'positionUsd': 5.0}])
        self.assertEqual((usd, len(reads)), (5.0, 2))

    def test_never_shown_is_none_after_the_tries(self):
        usd, reads, sleeps = self.go([None] * 10)
        self.assertIsNone(usd)
        self.assertEqual(len(reads), lp.moves.OPEN_MARK_TRIES)
        self.assertEqual(sleeps, [lp.moves.OPEN_MARK_PAUSE_S] * (lp.moves.OPEN_MARK_TRIES - 1))

    @settings(max_examples=60, deadline=None)
    @given(hs.integers(min_value=0, max_value=5), hs.floats(min_value=0, max_value=1e4, allow_nan=False))
    def test_property_the_mark_after_k_misses(self, k, usd):
        got, reads, _ = self.go([None] * k + [{'positionMint': 'NEW', 'positionUsd': usd}])
        if k < lp.moves.OPEN_MARK_TRIES:
            self.assertEqual((got, len(reads)), (usd, k + 1))
        else:
            self.assertEqual((got, len(reads)), (None, lp.moves.OPEN_MARK_TRIES))


class SettleDeposit(unittest.TestCase):
    def go(self, state, status):
        set_, told, saved = [], [], []
        with mock.patch.object(db, 'set_deposit', lambda m, u: set_.append((m, u))), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: told.append(ev)), \
                mock.patch.object(lp.paths, 'save', lambda s: saved.append(dict(s))):
            r = lp.moves.settle_deposit(state, status)
        return r, set_, told, saved

    def test_books_the_first_mark_once(self):
        state = {'deposit_estimate': 'NEW'}
        r, set_, told, saved = self.go(state, {'positionMint': 'NEW', 'positionUsd': 179.18})
        self.assertIs(r, True); self.assertEqual(set_, [('NEW', 179.18)]); self.assertEqual(told, ['deposit_settled'])
        self.assertNotIn('deposit_estimate', state); self.assertNotIn('deposit_estimate', saved[-1])
        r, set_, _, _ = self.go(state, {'positionMint': 'NEW', 'positionUsd': 170.0})
        self.assertIs(r, False); self.assertEqual(set_, [])

    def test_the_rent_counts_as_in_every_mark(self):
        with mock.patch.object(lp.capital, 'rent_usd', lambda st: 0.25):
            _, set_, _, _ = self.go({'deposit_estimate': 'NEW'}, {'positionMint': 'NEW', 'positionUsd': 179.0})
        self.assertEqual(set_, [('NEW', 179.25)])

    def test_another_position_or_none_drops_the_estimate(self):
        for st in ({'positionMint': 'OTHER', 'positionUsd': 1.0}, {}):
            state = {'deposit_estimate': 'NEW'}
            r, set_, _, _ = self.go(state, st)
            self.assertIs(r, False, st); self.assertEqual(set_, [], st); self.assertNotIn('deposit_estimate', state, st)

    def test_unpriced_waits_for_the_next_poll(self):
        state = {'deposit_estimate': 'NEW'}
        r, set_, _, _ = self.go(state, {'positionMint': 'NEW'})
        self.assertIs(r, False); self.assertEqual(set_, []); self.assertEqual(state['deposit_estimate'], 'NEW')

    def test_nothing_estimated_does_nothing(self):
        r, set_, told, saved = self.go({}, {'positionMint': 'NEW', 'positionUsd': 1.0})
        self.assertEqual((r, set_, told, saved), (False, [], [], []))


class OpenBooksTheMark(unittest.TestCase):
    """The whole open: the deposit is the chain's mark; the estimate stands in
    only when the chain never shows the position, and is then settled."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.patch(lp.paths, 'STATE', pathlib.Path(tmp) / 'runtime.json')
        self.patch(time, 'sleep', lambda _: None)
        self.events = []
        self.patch(lp.books, 'notify', lambda ev, **kw: self.events.append((ev, kw)))
        self.patch(lp.books, 'notify_book', lambda ev, **kw: self.events.append((ev, kw)))
        self.opened = mock.Mock()
        self.patch(db, 'open_position', self.opened)
        for name in ['close_position', 'snapshot', 'record_harvest', 'event']:
            self.patch(db, name, mock.Mock())
        self.patch(config, 'CALM_ENABLED', True)
        self.patch(config, 'CALM_MAX_MOVES', 12)
        self.patch(config, 'DEX', 'orca')
        self.patch(config, 'EXECUTE_DEXES', ('orca',))
        self.patch(config, 'CAPITAL_USD', 190)
        self.patch(config, 'MAX_USD', 260)
        self.bal = dict(price=100, quoteUsd=1, balanceA=1.2, balanceB=120, nativeSide='A', tokenA='SOL', tokenB='USDC')
        self.patch(lp.capital, 'wallet', lambda _: dict(self.bal))
        self.patch(lp.swaps, 'balance_wallet', lambda *a, **k: dict(self.bal))
        self.patch(lp.board, 'best_band_for', lambda _: dict(band=1.08, price=100, record={}, net_day_pct=.2,
                                                               rebal_per_day=.1))
        self.patch(lp.regime, 'calm_view', lambda *a: dict(calm=True, bar_age_s=420, p_touch_fresh=.06))
        self.patch(lp.signers, 'chain', lambda *a, **k: (dict(positionMint='new', signature='s', depositUsd=192.5), None)
                   if a[0] == 'open' else ({'signature': a[0]}, None))
        self.state = dict(last_rebalance=0, rebalance_times=[], calm_times=[], failures=0)

    def patch(self, obj, name, value):
        return self.stack.enter_context(mock.patch.object(obj, name, value))

    def open_with(self, reads):
        seq = iter(reads)
        self.patch(lp.signers, 'read_status', lambda *a: (next(seq, None), None))
        self.assertTrue(lp.moves.reopen(self.state, 'x', band=1.01))
        return self.opened.call_args.args[7]                         # deposit_usd

    def test_the_mark_not_the_estimate(self):
        dep = self.open_with([None, {'positionMint': 'new', 'positionUsd': 179.18}])
        self.assertEqual(dep, 179.18)
        self.assertNotIn('deposit_estimate', lp.paths.load())
        self.assertNotIn('deposit_estimated', [e for e, _ in self.events])
        self.assertEqual(next(kw for e, kw in self.events if e == 'OPEN')['deposit_usd'], 179.18)

    def test_never_shown_books_the_estimate_and_settles_later(self):
        dep = self.open_with([])
        self.assertEqual(dep, 192.5)
        self.assertEqual(lp.paths.load()['deposit_estimate'], 'new')
        self.assertIn('deposit_estimated', [e for e, _ in self.events])


class SetDeposit(unittest.TestCase):
    """db.set_deposit against the test database: open positions only."""

    def test_open_only(self):
        _fixtures.ensure_profile()
        db.open_position('DM-open', 'P', 'A/B', 1, 2, 1, 's', 192.5, 'x', config_name='sol-usdc')
        db.open_position('DM-shut', 'P', 'A/B', 1, 2, 1, 's', 192.5, 'x', config_name='sol-usdc')
        db.close_position('DM-shut', 'c', 190.0)
        self.assertEqual(db.set_deposit('DM-open', 179.18), 1)
        self.assertEqual(db.set_deposit('DM-shut', 179.18), 0)
        with db.cursor() as cur:
            cur.execute("select mint, deposit_usd from positions where mint like 'DM-%%' order by mint")
            got = {r['mint']: float(r['deposit_usd']) for r in cur.fetchall()}
        self.assertEqual(got, {'DM-open': 179.18, 'DM-shut': 192.5})


if __name__ == '__main__':
    unittest.main()
