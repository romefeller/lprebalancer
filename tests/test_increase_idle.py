"""Idle cash goes into the open position (signer `increase`) on a venue that
has it, instead of a close, swap and reopen (2026-10-03 audit: ~$0.04 a
re-centre, twice a day): rebalancer.add_idle, deploy_idle's routing,
db.add_deposit."""
import datetime as dt
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import config
import db
import rebalancer
from test_deploy_all import Patched, bal
from test_deploy_idle import Hook, NOW

STATUS = {'positionMint': 'M', 'whirlpool': 'P', 'price': 119.5, 'lowerPrice': 118.0, 'upperPrice': 121.0,
          'liquidity': '60000000000'}
GREW = {'positionMint': 'M', 'liquidity': '61000000000', 'positionUsd': 230.0, 'rentUsd': 0.0}


class AddIdle(Patched):
    def go(self, answer=({'signature': 'S', 'depositUsd': 21.5}, None), wb=None, caps=(0.09, 11.0), after=None, status=None,
           reread=GREW, unbalanced=False):
        calls, deps, seen, books, events = [], [], [], [], []
        state = {'open_unbalanced': True} if unbalanced else {}
        self.read = []
        wb = wb or dict(bal(0.3, 12.0, price=119.5), walletUsd=0.3 * 119.5 + 12.0)
        after = bal(0.06, 0.4, price=119.5) if after is None else after
        with mock.patch.object(rebalancer, 'balance_wallet', lambda s, b, r, share_a=None: (calls.append(('swap', share_a)) or b)), \
             mock.patch.object(rebalancer, 'pool_record', return_value={'token_a': {}, 'token_b': {}}), \
             mock.patch.object(rebalancer, 'quote_known', return_value=True), \
             mock.patch.object(rebalancer, 'deposit_caps', lambda b, share_a=None: (calls.append(('caps', share_a)) or caps)), \
             mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(('chain',) + a + (k,)) or answer)), \
             mock.patch.object(rebalancer, 'read_status', lambda m=None: (calls.append(('read', m)) or (next(reread) if hasattr(reread, '__next__') else reread, None))), \
             mock.patch.object(rebalancer, 'wallet', lambda p: (self.read.append(p) or after)), \
             mock.patch.object(config, 'POOL', 'CFGPOOL'), \
             mock.patch.object(rebalancer.db, 'add_deposit', lambda m, u: deps.append((m, u))), \
             mock.patch.object(rebalancer.db, 'event', lambda *a: events.append(a)), \
             mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append((ev, kw))), \
             mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: books.append((ev, kw))), \
             mock.patch.object(rebalancer, 'save', lambda s: None), \
             mock.patch.object(rebalancer.time, 'sleep', lambda x: calls.append(('sleep', x))):
            out = rebalancer.add_idle(state, dict(STATUS if status is None else status), wb, 25.0, 119.5)
        return out, calls, deps, seen, books, events, state

    def test_the_idle_cash_is_swapped_to_the_band_share_and_added(self):
        out, calls, deps, seen, books, events, state = self.go()
        self.assertIs(out, True)
        self.assertEqual(self.read, ['P'])
        share = calm.band_share_a(119.5, 118.0, 121.0)
        self.assertEqual(calls[0], ('swap', share))
        self.assertEqual(calls[1], ('caps', share))
        self.assertEqual(calls[2], ('chain', 'increase', 'M', '0.090000000', '11.000000000', '--execute', {'record': False}))
        self.assertEqual(calls[3], ('read', 'M'))
        want = 230.0 * 1e9 / 61e9                                         # the new liquidity's share of the mark
        self.assertAlmostEqual(deps[0][1], want); self.assertEqual(deps[0][0], 'M')
        self.assertEqual(books[0][0], 'INCREASE'); self.assertEqual(books[0][1]['added_usd'], round(want, 4))
        self.assertEqual(books[0][1]['signature'], 'S')
        self.assertEqual(events[0][0], 'INCREASE')
        left = rebalancer.deployable_usd(bal(0.06, 0.4, price=119.5))
        self.assertEqual(state['idle_baseline'], {'mint': 'M', 'usd': round(left, 4)})
        self.assertEqual(books[0][1]['left_usd'], round(left, 2))

    def test_a_failed_read_after_a_clean_send_books_the_signer_s_chain_amounts(self):
        _, _, deps, *_ = self.go(reread=None)
        self.assertEqual(deps, [('M', 21.5)])
        _, _, deps, *_ = self.go(reread={'positionMint': 'M', 'liquidity': '60000000000', 'positionUsd': 229.0, 'rentUsd': 0.0},
                                 answer=({'signature': 'S'}, None))
        self.assertEqual(deps, [('M', 0.0)])

    def test_a_late_landing_after_an_error_is_found_by_the_rechecks(self):
        flat = {'positionMint': 'M', 'liquidity': '60000000000', 'positionUsd': 229.0, 'rentUsd': 0.0}
        seq = iter([flat, flat, GREW])
        out, calls, deps, seen, *_ = self.go(answer=(None, 'ERROR: confirmation timed out'), reread=seq)
        self.assertIs(out, True)
        self.assertEqual([c for c in calls if c[0] in ('read', 'sleep')],
                         [('read', 'M'), ('sleep', 15), ('read', 'M'), ('sleep', 15), ('read', 'M')])
        self.assertEqual(seen[0][0], 'increase_recovered')

    def test_an_error_with_no_growth_rechecks_for_90_s_then_fails(self):
        flat = {'positionMint': 'M', 'liquidity': '60000000000', 'positionUsd': 229.0, 'rentUsd': 0.0}
        out, calls, deps, seen, *_ = self.go(answer=(None, 'ERROR: 6017'), reread=flat)
        self.assertIs(out, False)
        self.assertEqual(sum(1 for c in calls if c[0] == 'read'), 7)
        self.assertEqual(sum(c[1] for c in calls if c[0] == 'sleep'), 90)
        self.assertEqual(seen[-1][0], 'increase_failed')

    def test_a_clean_send_does_not_recheck(self):
        flat = {'positionMint': 'M', 'liquidity': '60000000000', 'positionUsd': 229.0, 'rentUsd': 0.0}
        out, calls, *_ = self.go(reread=flat)
        self.assertEqual(sum(1 for c in calls if c[0] == 'read'), 1)
        self.assertFalse(any(c[0] == 'sleep' for c in calls))

    def test_an_add_that_landed_despite_an_error_is_booked(self):
        out, calls, deps, seen, books, events, state = self.go(answer=(None, 'ERROR: confirmation timed out'))
        self.assertIs(out, True)
        self.assertFalse(any(c[0] == 'sleep' for c in calls))            # the first read saw it: no waiting
        self.assertAlmostEqual(deps[0][1], 230.0 * 1e9 / 61e9)
        self.assertEqual(seen[0][0], 'increase_recovered')
        self.assertIsNone(books[0][1]['signature'])

    def test_without_a_pool_in_the_status_the_profile_s_pool_is_read(self):
        st = {k: v for k, v in STATUS.items() if k != 'whirlpool'}
        self.go(status=st)
        self.assertEqual(self.read, ['CFGPOOL'])

    def test_one_side_alone_is_not_sent(self):
        for caps in ((0.0, 0.5), (0.004, 0.0), (0.0, 0.0)):
            out, calls, *_ = self.go(caps=caps)
            self.assertIs(out, False, caps)
            self.assertFalse(any(c[0] == 'chain' for c in calls), caps)
        out, calls, *_ = self.go(caps=(0.004, 0.5))
        self.assertIs(out, True)

    def test_a_leftover_after_a_failed_swap_is_not_excused(self):
        *_, state = self.go(unbalanced=True)
        self.assertEqual(state['idle_baseline'], {'mint': 'M', 'usd': 0.0})

    def test_a_failure_adds_nothing_and_says_so(self):
        flat = {'positionMint': 'M', 'liquidity': '60000000000', 'positionUsd': 229.0, 'rentUsd': 0.0}
        for answer in ((None, 'ERROR: 6017'), ({'sent': False}, None), ({'signature': 'S'}, 'ERROR: after send'), (None, None)):
            out, calls, deps, seen, books, events, state = self.go(answer=answer, reread=flat)
            self.assertIs(out, False)
            self.assertEqual(deps, []); self.assertEqual(books, [])
            self.assertEqual(seen[0][0], 'increase_failed')
            self.assertEqual(events[0][0], 'increase_failed')
            self.assertNotIn('idle_baseline', state)

    def test_a_held_write_says_nothing(self):
        out, calls, deps, seen, books, *_ = self.go(answer=(None, 'refused: halted (HALT)'))
        self.assertIs(out, False); self.assertEqual((deps, seen, books), ([], [], []))

    def test_nothing_to_add_sends_nothing(self):
        out, calls, *_ = self.go(caps=(0.0, 0.0))
        self.assertIs(out, False)
        self.assertFalse(any(c[0] in ('chain', 'read') for c in calls))

    def test_a_price_outside_the_band_is_left_to_the_exit(self):
        wb = dict(bal(0.3, 12.0, price=125.0), walletUsd=50.0)
        out, calls, *_ = self.go(wb=wb)
        self.assertIs(out, False); self.assertEqual(calls, [])

    def test_a_failed_swap_or_unknown_quote_stops_before_the_add(self):
        for bw, qk in ((lambda s, b, r, share_a=None: None, True), (lambda s, b, r, share_a=None: b, False)):
            sent = []
            with mock.patch.object(rebalancer, 'balance_wallet', bw), \
                 mock.patch.object(rebalancer, 'pool_record', return_value={}), \
                 mock.patch.object(rebalancer, 'quote_known', return_value=qk), \
                 mock.patch.object(rebalancer, 'chain', lambda *a, **k: sent.append(a)):
                self.assertIs(rebalancer.add_idle({}, dict(STATUS), dict(bal(0.3, 12.0, price=119.5), walletUsd=50.0), 25.0, 119.5), False)
            self.assertEqual(sent, [])

    def test_an_unreadable_wallet_after_leaves_no_excuse(self):
        out, *_, state = self.go(after={})
        self.assertTrue(out)
        self.assertEqual(state['idle_baseline'], {'mint': 'M', 'usd': 0.0})


class Added(unittest.TestCase):
    def test_the_new_share_of_the_mark(self):
        self.assertAlmostEqual(rebalancer.added_usd('100', {'liquidity': '125', 'positionUsd': 250.0, 'rentUsd': 0.0}), 50.0)
        # the rent in the mark is left out of the add
        self.assertAlmostEqual(rebalancer.added_usd('100', {'liquidity': '125', 'positionUsd': 250.0, 'rentUsd': 0.63}),
                               (250.63 - 0.63) * 25 / 125)

    def test_no_growth_no_read_or_bad_figures_is_none(self):
        for l0, st in (('100', {'liquidity': '100', 'positionUsd': 250.0}), ('100', {'liquidity': '90', 'positionUsd': 250.0}),
                       ('100', None), ('100', {}), ('x', {'liquidity': '125', 'positionUsd': 250.0}),
                       (None, {'liquidity': '125', 'positionUsd': 250.0}), ('-5', {'liquidity': '125', 'positionUsd': 250.0}),
                       ('100', {'liquidity': '125'})):
            self.assertIsNone(rebalancer.added_usd(l0, st), (l0, st))

    def test_zero_before_is_the_whole_mark(self):
        self.assertAlmostEqual(rebalancer.added_usd('0', {'liquidity': '10', 'positionUsd': 30.0, 'rentUsd': 0.0}), 30.0)


class Routing(Hook):
    def go_dex(self, dex):
        added = []
        with mock.patch.object(config, 'DEX', dex), \
             mock.patch.object(rebalancer, 'add_idle', lambda *a: (added.append(a) or 'added')):
            out, moves, books, events = self.go(self.WALLET, rv={'choice': 1.03})
        return out, moves, added

    def test_raydium_adds_instead_of_recentring(self):
        out, moves, added = self.go_dex('raydium-clmm')
        self.assertEqual(out, 'added'); self.assertEqual(moves, [])
        self.assertEqual(added[0][4], 119.5)
        self.assertAlmostEqual(added[0][3], 0.187 * 119.5 + 0.75, places=1)

    def test_other_venues_keep_the_recentre(self):
        for dex in ('orca', 'meteora-dlmm', 'byreal'):
            out, moves, added = self.go_dex(dex)
            self.assertTrue(out); self.assertEqual(len(moves), 1); self.assertEqual(added, [])

    def test_the_venue_breaker_covers_the_add(self):
        self.assertIn('increase', rebalancer.WRITE_COMMANDS)
        self.assertEqual(rebalancer.INCREASE_DEXES, {'raydium-clmm'})


class AddDeposit(unittest.TestCase):
    def setUp(self):
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at, deposit_usd) values ('O', 'P', now(), 200)")
            cur.execute("insert into positions (mint, pool, opened_at, deposit_usd) values ('N', 'P', now(), null)")
            cur.execute("insert into positions (mint, pool, opened_at, closed_at, deposit_usd) values ('C', 'P', now(), now(), 100)")

    def dep(self, m):
        with db.cursor() as cur:
            cur.execute('select deposit_usd from positions where mint = %s', (m,))
            return float(cur.fetchone()['deposit_usd'] or 0)

    def test_an_open_position_grows_a_closed_one_does_not(self):
        self.assertEqual(db.add_deposit('O', 21.5), 1); self.assertAlmostEqual(self.dep('O'), 221.5)
        self.assertEqual(db.add_deposit('N', 3.0), 1); self.assertEqual(self.dep('N'), 0.0)   # unpriced stays unpriced
        with db.cursor() as cur:
            cur.execute("select deposit_usd from positions where mint = 'N'"); self.assertIsNone(cur.fetchone()['deposit_usd'])
        self.assertEqual(db.add_deposit('C', 5.0), 0); self.assertAlmostEqual(self.dep('C'), 100.0)
        self.assertEqual(db.add_deposit('missing', 5.0), 0)


if __name__ == '__main__':
    unittest.main()
