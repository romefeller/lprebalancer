"""Idle money beside an open band is deployed (owner, 2026-09-28: $22 of SOL
sat idle after two rate-limited swaps). The swap retries patiently; a
leftover triggers a re-centre at the regime's width."""
import datetime as dt
import math
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401
import config
import db
import time
import lp.capital
import lp.swaps
from test_deploy_all import Patched, bal, RES, SOL, USDC

NOW = dt.datetime(2026, 9, 28, 19, 0, tzinfo=dt.timezone.utc)


class Decision(unittest.TestCase):
    def test_thresholds(self):
        f = lp.swaps.idle_to_deploy
        self.assertTrue(f(22.0, 241.0, 1800))
        self.assertFalse(f(4.8, 240.0, 1800))            # exactly 2% of equity: not over
        self.assertTrue(f(4.81, 240.0, 1800))
        self.assertFalse(f(2.0, 50.0, 1800))             # the $2 floor
        self.assertTrue(f(2.01, 50.0, 1800))
        self.assertTrue(f(2.01, None, 1800))

    def test_a_fresh_or_unknown_band_waits(self):
        f = lp.swaps.idle_to_deploy
        self.assertFalse(f(22.0, 241.0, 599)); self.assertTrue(f(22.0, 241.0, 600))
        self.assertFalse(f(22.0, 241.0, None))

    @settings(max_examples=300, deadline=None)
    @given(st.floats(0, 500), st.floats(0, 1000), st.floats(0, 100), st.floats(600, 1e6))
    def test_more_idle_never_turns_it_off(self, idle, eq, more, age):
        if lp.swaps.idle_to_deploy(idle, eq, age):
            self.assertTrue(lp.swaps.idle_to_deploy(idle + more, eq, age))


class Hook(Patched):
    STATUS = {'positionMint': 'M', 'price': 119.5, 'lowerPrice': 116.0, 'upperPrice': 123.0,
              'positionUsd': 212.55, 'rentUsd': 0.0}

    def state(self):
        return {'idle_baseline': {'mint': 'M', 'usd': 0.0}}                  # nothing excused

    def go(self, wbal, rv=None, opened_min=30, budget=5, allowed=True):
        moves, books, events = [], [], []
        class FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        opened = None if opened_min is None else NOW - dt.timedelta(minutes=opened_min)
        with mock.patch.object(lp.swaps, 'datetime', FakeDT), \
                mock.patch.object(db, 'position_opened', lambda m: opened), \
                mock.patch.object(db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: budget), \
                mock.patch.object(lp.regime, 'voluntary_move_allowed', lambda s: allowed), \
                mock.patch.object(lp.books, 'notify_book', lambda ev, **kw: books.append((ev, kw))), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: moves.append((a, k))):
            out = lp.swaps.deploy_idle(self.state(), dict(self.STATUS), wbal, rv, 119.5)
        return out, moves, books, events

    # today's wallet: 0.2462 SOL (0.187 idle above the reserve and headroom) and $0.75 USDC
    WALLET = dict(bal(0.059 + 0.187, 0.75, price=119.5), walletUsd=(0.059 + 0.187) * 119.5 + 0.75)

    def test_the_18_16Z_leftover_is_deployed_at_the_regime_width(self):
        out, moves, books, events = self.go(self.WALLET, rv={'choice': 1.03})
        self.assertTrue(out)
        (a, k), = moves
        self.assertEqual(k, {'band': 1.03, 'calm_move': True})
        self.assertEqual(a[2], f'deploy ${0.187 * 119.5 + 0.75:.2f} idle')
        self.assertEqual(books[0][0], 'DEPLOY_IDLE'); self.assertAlmostEqual(books[0][1]['idle_usd'], 0.187 * 119.5 + 0.75, places=1)
        self.assertEqual(events[0][0], 'DEPLOY_IDLE')

    def test_without_a_regime_view_the_held_width_is_kept(self):
        _, moves, *_ = self.go(self.WALLET, rv=None)
        self.assertAlmostEqual(moves[0][1]['band'], math.sqrt(123.0 / 116.0))

    def test_each_gate(self):
        small = dict(bal(0.059 + 0.01, 0.75, price=119.5), walletUsd=2.0)
        self.assertFalse(self.go(small)[0])                                   # $1.95 idle: under the limit
        self.assertFalse(self.go(self.WALLET, opened_min=5)[0])               # a fresh band
        self.assertFalse(self.go(self.WALLET, opened_min=None)[0])            # unknown age
        self.assertFalse(self.go(self.WALLET, budget=0)[0])
        self.assertFalse(self.go(self.WALLET, allowed=False)[0])
        self.assertFalse(self.go({'walletUsd': 30.0})[0])                     # unreadable balances
        self.assertFalse(self.go(dict(self.WALLET, walletUsd=None))[0])
        for gated in (dict(opened_min=5), dict(budget=0), dict(allowed=False)):
            self.assertEqual(self.go(self.WALLET, **gated)[1], [])            # and nothing moved

    def test_the_equity_share_counts_the_position(self):
        # $5 idle against $5 of wallet + $212.55 in the band: 2.3% of equity, over the limit
        w = dict(bal(0.059, 5.0, price=119.5), walletUsd=5.0)
        self.assertTrue(self.go(w)[0])
        w2 = dict(bal(0.059, 4.0, price=119.5), walletUsd=4.0)                # $4: 1.8%, under
        self.assertFalse(self.go(w2)[0])


class Retries(Patched):
    REC = {'token_a': {'address': SOL, 'decimals': 9, 'symbol': 'SOL'}, 'token_b': {'address': USDC, 'decimals': 6, 'symbol': 'USDC'}}

    def go(self, answers):
        calls, sleeps = [], []
        it = iter(answers)
        b = bal(0.2, 200.0)
        with mock.patch.object(lp.signers, 'chain', lambda *a, **k: (calls.append(a) or next(it))), \
                mock.patch.object(lp.capital, 'wallet', lambda p: b), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(time, 'sleep', lambda s: sleeps.append(s)):
            out = lp.swaps.balance_wallet({'failures': 0}, dict(b), self.REC)
        return out, calls, sleeps

    OK = ({'sent': True, 'signature': 's'}, None)
    RL = (None, 'RPC rate limited')

    def test_three_attempts_with_growing_pauses(self):
        out, calls, sleeps = self.go([self.RL, self.RL, self.OK])
        self.assertEqual(len(calls), 3); self.assertEqual(sleeps[:2], [15, 30]); self.assertIsNotNone(out)

    def test_it_stops_at_the_first_success(self):
        _, calls, sleeps = self.go([self.RL, self.OK])
        self.assertEqual(len(calls), 2); self.assertEqual(sleeps[0], 15)
        _, calls, _ = self.go([self.OK])
        self.assertEqual(len(calls), 1)

    def test_three_failures_open_with_the_wallet_as_it_is(self):
        out, calls, _ = self.go([self.RL, self.RL, self.RL])
        self.assertEqual(len(calls), 3); self.assertEqual(out, bal(0.2, 200.0))

    def test_the_pauses(self):
        self.assertEqual(lp.swaps.SWAP_RETRY_PAUSES, (15, 30))


if __name__ == '__main__':
    unittest.main()


class Exact(Hook):
    def test_answers_are_exactly_false_or_true(self):
        self.assertIs(lp.swaps.idle_to_deploy(1.0, 240.0, 1800), False)
        self.assertIs(lp.swaps.idle_to_deploy(1.0, 240.0, None), False)
        self.assertIs(self.go({'walletUsd': 1.0})[0], False)
        self.assertIs(self.go(self.WALLET, allowed=False)[0], False)
        self.assertIs(self.go(self.WALLET)[0], True)

    def test_one_move_left_is_enough(self):
        self.assertTrue(self.go(self.WALLET, budget=1)[0])

    def test_an_unreadable_position_mark_counts_as_zero(self):
        status = {k: v for k, v in self.STATUS.items() if k != 'positionUsd'}
        w = dict(bal(0.059, 4.81, price=119.5), walletUsd=240.0)               # 4.81 idle, 2% of 240 = 4.80
        moves = []
        class FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        with mock.patch.object(lp.swaps, 'datetime', FakeDT), \
                mock.patch.object(db, 'position_opened', lambda m: NOW - dt.timedelta(hours=1)), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: 5), \
                mock.patch.object(lp.regime, 'voluntary_move_allowed', lambda s: True), \
                mock.patch.object(lp.books, 'notify_book', lambda *a, **k: None), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: moves.append(1)):
            self.assertIs(lp.swaps.deploy_idle(self.state(), status, w, None, 119.5), True)


class ToleranceBaseline(Hook):
    """What a balanced open leaves out is its price tolerance: excused. Only
    new money beyond it is deployed; after a fallback open nothing is excused."""
    def go_state(self, state, wbal, mint='M'):
        moves = []
        class FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        st = dict(self.STATUS, positionMint=mint)
        with mock.patch.object(lp.swaps, 'datetime', FakeDT), \
                mock.patch.object(db, 'position_opened', lambda m: NOW - dt.timedelta(hours=1)), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: 5), \
                mock.patch.object(lp.regime, 'voluntary_move_allowed', lambda s: True), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify_book', lambda *a, **k: None), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: moves.append(k)):
            out = lp.swaps.deploy_idle(state, st, wbal, None, 119.5)
        return out, moves

    TOL = dict(bal(0.059 + 0.06, 6.0, price=119.5), walletUsd=13.2)          # $13.17 left by the open

    def test_the_first_reading_after_a_balanced_open_is_excused(self):
        state = {'open_unbalanced': False}
        out, moves = self.go_state(state, self.TOL)
        self.assertIs(out, False); self.assertEqual(moves, [])
        self.assertEqual(state['idle_baseline']['mint'], 'M')
        self.assertAlmostEqual(state['idle_baseline']['usd'], lp.capital.deployable_usd(self.TOL), places=3)
        self.assertIs(self.go_state(state, self.TOL)[0], False)               # the same leftover: still held

    def test_new_money_beyond_the_leftover_is_deployed(self):
        state = {'idle_baseline': {'mint': 'M', 'usd': lp.capital.deployable_usd(self.TOL)}}
        more = dict(self.TOL, balanceB=6.0 + 4.0, walletUsd=17.2)             # +$4: under max($2, 2% of ~$230)
        self.assertIs(self.go_state(state, more)[0], False)
        more = dict(self.TOL, balanceB=6.0 + 50.0, walletUsd=63.2)            # a $50 deposit
        self.assertIs(self.go_state(state, more)[0], True)

    def test_after_a_fallback_open_nothing_is_excused(self):
        state = {'open_unbalanced': True}
        self.assertIs(self.go_state(state, self.TOL)[0], True)
        self.assertEqual(state['idle_baseline']['usd'], 0.0)

    def test_a_new_band_takes_a_new_reading(self):
        state = {'idle_baseline': {'mint': 'OLD', 'usd': 100.0}, 'open_unbalanced': False}
        self.assertIs(self.go_state(state, self.TOL)[0], False)
        self.assertEqual(state['idle_baseline']['mint'], 'M')

    def test_the_swap_marks_how_the_wallet_was_opened(self):
        b = bal(0.2, 200.0)
        rec = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}}
        for answers, flag in ((((None, 'custom program error'),), True), ((({'sent': True, 'signature': 's'}, None),), False)):
            state = {'failures': 0, 'open_unbalanced': not flag}
            it = iter(answers)
            with mock.patch.object(lp.signers, 'chain', lambda *a, **k: next(it)), \
                    mock.patch.object(lp.capital, 'wallet', lambda p: b), \
                    mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                    mock.patch.object(lp.paths, 'save', lambda s: None), \
                    mock.patch.object(db, 'event', lambda *a: None), \
                    mock.patch.object(time, 'sleep', lambda s: None):
                lp.swaps.balance_wallet(state, dict(b), rec)
            self.assertIs(state['open_unbalanced'], flag)
        state = {'open_unbalanced': True}                                       # balanced already: not unbalanced
        with mock.patch.object(config, 'REBALANCE_SWAP', False):
            lp.swaps.balance_wallet(state, dict(b), rec)
        self.assertIs(state['open_unbalanced'], False)
