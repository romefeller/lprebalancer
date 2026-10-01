"""Deploy all the capital (owner, 2026-09-28): every open is sized from what
the wallet holds, less only the gas reserve and the open's rent, under the
max_usd ceiling. Nothing else may stay idle, and nothing new may cost."""
import json
import unittest
from unittest import mock

from hypothesis import given, settings, assume, strategies as st

import _fixtures  # noqa: F401
import config
import rebalancer

RES = lambda: config.GAS_RESERVE_SOL + rebalancer.OPEN_RENT_HEADROOM_SOL
SOL, USDC = 'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def bal(a=1.0, b=100.0, price=120.0, q=1.0, native='A'):
    return {'balanceA': a, 'balanceB': b, 'price': price, 'quoteUsd': q, 'nativeSide': native}


class Patched(unittest.TestCase):
    def setUp(self):
        # balance_wallet records the swap outcome itself: no breaker state may leak between tests
        # These tests pin Jupiter's own retries; the Orca fallback has its own (test_jupiter_gate.OrcaFallback).
        p = mock.patch.object(rebalancer, 'SWAP_FALLBACK', ''); p.start(); self.addCleanup(p.stop)
        with rebalancer.db.cursor(commit=True) as cur:
            cur.execute('truncate health')
        for name, v in (('DEPLOY_ALL', True), ('MAX_USD', 300.0), ('SIDE_CAP_FRACTION', 0.55),
                        ('GAS_RESERVE_SOL', 0.05), ('CAPITAL_USD', 190.0), ('PAYOUT_ENABLED', False),
                        ('REBALANCE_SWAP', True)):
            p = mock.patch.object(config, name, v); p.start(); self.addCleanup(p.stop)


class Deployable(Patched):
    def test_everything_but_the_reserve_on_the_native_side(self):
        self.assertAlmostEqual(rebalancer.deployable_usd(bal(1.0, 100.0)), (1.0 - RES()) * 120.0 + 100.0)
        self.assertAlmostEqual(rebalancer.deployable_usd(bal(1000.0, 1.0, price=0.01, q=120.0, native='B')),
                               (1000.0 * 0.01 + (1.0 - RES())) * 120.0)
        self.assertAlmostEqual(rebalancer.deployable_usd(bal(1.0, 100.0, native=None)), 220.0)

    def test_a_wallet_under_the_reserve_deploys_nothing_of_it(self):
        self.assertEqual(rebalancer.deployable_usd(bal(0.03, 0.0)), 0.0)
        self.assertEqual(rebalancer.deployable_usd(bal(0.03, 5.0)), 5.0)
        self.assertEqual(rebalancer.deployable_usd({'balanceA': None, 'balanceB': None, 'price': 120.0}), 0.0)

    def test_capital_is_the_wallet_under_the_ceiling(self):
        self.assertAlmostEqual(rebalancer.capital(bal(1.0, 100.0)), (1.0 - RES()) * 120.0 + 100.0)
        ceiling = 300.0 / (2 * 0.55)
        self.assertAlmostEqual(rebalancer.capital(bal(10.0, 1000.0)), ceiling)

    def test_without_a_wallet_read_or_with_deploy_all_off_the_configured_capital_stands(self):
        self.assertEqual(rebalancer.capital(), 190.0)
        self.assertEqual(rebalancer.capital({'price': 120.0}), 190.0)
        self.assertEqual(rebalancer.capital(dict(bal(), price=0)), 190.0)
        with mock.patch.object(config, 'DEPLOY_ALL', False):
            self.assertEqual(rebalancer.capital(bal(1.0, 100.0)), 190.0)
        with mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.db, 'reinvested_usd', lambda p: 5.0):
            self.assertEqual(rebalancer.capital(), 195.0)
        with mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.db, 'reinvested_usd', side_effect=RuntimeError('db')):
            self.assertEqual(rebalancer.capital(), 190.0)

    def test_the_target_share_is_half_when_the_whole_wallet_is_the_capital(self):
        self.assertEqual(rebalancer.side_target_fraction(), 0.5)
        with mock.patch.object(config, 'DEPLOY_ALL', False):
            self.assertEqual(rebalancer.side_target_fraction(), 0.55)


class Caps(Patched):
    def test_a_balanced_wallet_deploys_both_sides_whole(self):
        b = bal(1.0 + RES(), 120.0)
        a_cap, b_cap = rebalancer.deposit_caps(b)
        self.assertAlmostEqual(a_cap, 1.0); self.assertAlmostEqual(b_cap, 120.0)

    @settings(max_examples=300, deadline=None)
    @given(a=st.floats(0, 5), b=st.floats(0, 600), price=st.floats(20, 400))
    def test_caps_never_touch_the_reserve_nor_exceed_the_wallet(self, a, b, price):
        a_cap, b_cap = rebalancer.deposit_caps(bal(a, b, price))
        self.assertLessEqual(a_cap, max(a - RES(), 0.0) + 1e-12)
        self.assertLessEqual(b_cap, b + 1e-9)
        self.assertGreaterEqual(a_cap, 0.0); self.assertGreaterEqual(b_cap, 0.0)


def simulate_swap(b, target_a, target_b):
    """What swap_jupiter's planRebalance 'fill' does, without fees: move value
    from the long side to the short one until the short side reaches its
    target. The script sells only above the 0.05 SOL gas reserve."""
    price, q = b['price'], b.get('quoteUsd') or 1.0
    usd_a = max(b['balanceA'] - config.GAS_RESERVE_SOL, 0) * price * q
    usd_b = b['balanceB'] * q
    out = dict(b)
    if usd_a < target_a:
        move = min(target_a - usd_a, max(usd_b - target_b, 0))
        out['balanceA'] += move / (price * q); out['balanceB'] -= move / q
    elif usd_b < target_b:
        move = min(target_b - usd_b, max(usd_a - target_a, 0))
        out['balanceA'] -= move / (price * q); out['balanceB'] += move / q
    return out


class Swap(Patched):
    REC = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}}

    def run_it(self, b):
        calls = []
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or ({'sent': True, 'signature': 's'}, None))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: b), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            rebalancer.balance_wallet({'failures': 0}, dict(b), self.REC)
        return calls

    def test_a_lopsided_wallet_swaps_to_half_each_with_the_headroom_on_sol(self):
        b = bal(0.2, 200.0, price=120.0)
        calls = self.run_it(b)
        self.assertEqual(len(calls), 1)
        C = rebalancer.deployable_usd(b)
        _, _, _, ta, tb, *_ = calls[0]
        self.assertAlmostEqual(float(ta), C / 2 + rebalancer.OPEN_RENT_HEADROOM_SOL * 120.0, places=2)
        self.assertAlmostEqual(float(tb), C / 2, places=2)

    def test_native_on_the_b_side_gets_the_headroom_there(self):
        b = bal(20000.0, 0.2, price=0.0001, q=120.0, native='B')
        calls = self.run_it(b)
        C = rebalancer.deployable_usd(b)
        _, _, _, ta, tb, *_ = calls[0]
        self.assertAlmostEqual(float(ta), C / 2, places=2)
        self.assertAlmostEqual(float(tb), C / 2 + rebalancer.OPEN_RENT_HEADROOM_SOL * 120.0, places=2)

    def test_no_native_side_has_no_headroom(self):
        b = bal(0.2, 200.0, native=None)
        _, _, _, ta, tb, *_ = self.run_it(b)[0]
        self.assertEqual(ta, tb)

    def test_a_wallet_near_half_each_does_not_swap(self):
        C_half = 110.0
        self.assertEqual(self.run_it(bal(C_half / 120.0 + RES(), C_half)), [])
        self.assertEqual(self.run_it(bal(C_half / 120.0 + RES(), C_half * 1.03)), [])   # within 4%

    @settings(max_examples=300, deadline=None)
    @given(a=st.floats(0.06, 4), b=st.floats(0, 500), price=st.floats(50, 300))
    def test_after_the_swap_only_the_reserve_stays_out(self, a, b, price):
        w = bal(a, b, price)
        C = rebalancer.deployable_usd(w)
        assume(C > 20 and C < 300 / 1.1)
        calls = self.run_it(w)
        if calls:
            _, _, _, ta, tb, *_ = calls[0]
            w = simulate_swap(w, float(ta), float(tb))
        a_cap, b_cap = rebalancer.deposit_caps(w)
        deposit = 2 * min(a_cap * price, b_cap)            # a centred band takes half in value of each
        self.assertGreaterEqual(deposit, 0.955 * C)         # 4% no-swap tolerance, plus rounding
        self.assertLessEqual(a_cap, w['balanceA'] - RES() + 1e-9)


if __name__ == '__main__':
    unittest.main()


class BalanceWallet(Patched):
    """Every branch of the pre-open swap under deploy-all."""
    REC = {'token_a': {'address': SOL, 'decimals': 9, 'symbol': 'SOL'},
           'token_b': {'address': USDC, 'decimals': 6, 'symbol': 'USDC'}}

    def go(self, b, answers=(({'sent': True, 'signature': 's'}, None),), rec=None, state=None, after=None):
        calls, seen, events, halted = [], [], [], []
        it = iter(answers)
        state = state if state is not None else {'failures': 0}
        after = after if after is not None else dict(b, balanceA=b['balanceA'] + 0.0001)
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append((a, k)) or next(it))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: after), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append((ev, kw))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'halt', lambda r: halted.append(r)), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            out = rebalancer.balance_wallet(state, dict(b), self.REC if rec is None else rec)
        return out, calls, [e for e, _ in seen], events, state, halted, seen

    LOP = bal(0.2, 200.0)                                                      # SOL short: needs a swap

    def test_off_or_without_a_record_nothing_happens(self):
        with mock.patch.object(config, 'REBALANCE_SWAP', False):
            out, calls, *_ = self.go(self.LOP)
        self.assertEqual((out, calls), (self.LOP, []))
        out, calls, *_ = self.go(self.LOP, rec={})
        self.assertEqual((out, calls), (self.LOP, []))

    def test_a_missing_quote_price_counts_the_quote_as_a_dollar(self):
        b = dict(self.LOP, quoteUsd=None)
        _, calls, *_ = self.go(b)
        a = calls[0][0]
        C = rebalancer.deployable_usd(b)
        self.assertAlmostEqual(float(a[4]), C / 2, places=2)
        self.assertEqual(json.loads(calls[0][1]['extra_env']['LPBOT_TOKEN_HINTS'])[USDC]['usd'], 1.0)

    def test_the_thresholds(self):
        # Under deploy-all the 4% balance gate is the one that binds: a short side
        # at 48.5% of the total (the need) is already within 3% of the other.
        C = 220.0
        ok = bal(0.481 * C / 120.0 + RES(), 0.519 * C)                         # 3.8% apart: no swap
        self.assertEqual(self.go(ok)[1], [])
        short = bal(0.47 * C / 120.0 + RES(), 0.53 * C)                        # 6% apart: swap
        self.assertEqual(len(self.go(short)[1]), 1)
        # balanced within 4% of the total: no swap even when both are under need
        small = 20.0
        self.assertEqual(self.go(bal(small / 120.0 + RES(), small * 1.04))[1], [])
        self.assertEqual(len(self.go(bal(small / 120.0 + RES(), small * 1.1))[1]), 1)

    def test_the_reserve_is_off_the_native_side_only(self):
        # native on B: A's full balance counts, B's less the reserve
        b = bal(2000.0, 0.2, price=0.01, q=120.0, native='B')
        _, calls, *_ = self.go(b)
        self.assertEqual(len(calls), 1)
        none = bal(0.2, 200.0, native=None)                                    # no native side: nothing reserved
        _, calls, *_ = self.go(none)
        C = rebalancer.deployable_usd(none)
        self.assertAlmostEqual(C, 0.2 * 120 + 200.0)
        self.assertAlmostEqual(float(calls[0][0][3]), C / 2, places=2)

    def test_a_record_without_mints_skips(self):
        out, calls, seen, *_ = self.go(self.LOP, rec={'token_a': {'address': 'bad'}, 'token_b': {'address': USDC}})
        self.assertEqual((out, calls), (self.LOP, [])); self.assertIn('swap_skipped', seen)
        out, calls, seen, *_ = self.go(self.LOP, rec={'token_a': {'address': SOL}})
        self.assertEqual(calls, [])

    def test_hints_need_both_decimals(self):
        _, calls, *_ = self.go(self.LOP)
        h = json.loads(calls[0][1]['extra_env']['LPBOT_TOKEN_HINTS'])
        self.assertEqual(h[SOL], {'usd': 120.0, 'decimals': 9, 'symbol': 'SOL'})
        self.assertEqual(h[USDC], {'usd': 1.0, 'decimals': 6, 'symbol': 'USDC'})
        _, calls, *_ = self.go(self.LOP, rec={'token_a': {'address': SOL}, 'token_b': {'address': USDC, 'decimals': 6}})
        self.assertIsNone(calls[0][1]['extra_env'])
        _, calls, *_ = self.go(self.LOP, rec={'token_a': {'address': SOL, 'decimals': 'x'}, 'token_b': {'address': USDC, 'decimals': 6}})
        self.assertIsNone(calls[0][1]['extra_env'])
        a = calls[0][0]
        self.assertEqual((a[0], a[1], a[2], a[5]), ('rebalance', SOL, USDC, '--execute'))
        self.assertEqual(calls[0][1]['dex'], 'jupiter')

    def test_a_transport_failure_is_retried_once_and_nothing_else_is(self):
        ok = ({'sent': True, 'signature': 's'}, None)
        for err in ('RPC rate limited', '429 Too Many', 'request timed out', 'timeout', 'ECONNRESET', 'blockhash not found'):
            _, calls, *_ = self.go(self.LOP, answers=((None, err), ok))
            self.assertEqual(len(calls), 2, err)
            self.assertEqual(calls[0][0], calls[1][0])
        _, calls, *_ = self.go(self.LOP, answers=((None, 'custom program error'),))
        self.assertEqual(len(calls), 1)
        _, calls, *_ = self.go(self.LOP, answers=(({'signature': 'x'}, 'timeout'),))    # it may have landed
        self.assertEqual(len(calls), 1)
        _, calls, *_ = self.go(self.LOP, answers=(({'partial': True}, 'timeout'),))
        self.assertEqual(len(calls), 1)
        _, calls, *_ = self.go(self.LOP, answers=(({}, 'timeout'), ok))                 # an empty answer with a transport error
        self.assertEqual(len(calls), 2)

    def test_nothing_sent_opens_with_the_wallet_as_it_is(self):
        out, calls, seen, _, state, *_ = self.go(self.LOP, answers=((None, 'custom program error'),))
        self.assertEqual(out, self.LOP); self.assertIn('swap_skipped', seen); self.assertEqual(state['failures'], 0)
        out, *_ = self.go(self.LOP, answers=((None, None),))
        self.assertEqual(out, self.LOP)

    def test_already_at_target(self):
        out, _, seen, events, state, *_ = self.go(self.LOP, answers=(({'noop': True}, None),))
        self.assertEqual(out, self.LOP); self.assertIn('swap_skipped', seen); self.assertEqual(events, [])

    def test_a_partial_or_unsent_swap_is_a_failure_and_halts_at_the_limit(self):
        for ans in (({'partial': True, 'signature': 'p'}, 'confirm'), ({'signature': 's', 'sent': False}, None),
                    ({'signature': 's', 'sent': True}, 'late error')):
            out, _, seen, events, state, halted, _ = self.go(self.LOP, answers=(ans,))
            self.assertIsNone(out); self.assertEqual(state['failures'], 1, ans)
            self.assertIn('swap_failed', seen); self.assertEqual(events[0][0], 'swap_failed'); self.assertEqual(halted, [])
        with mock.patch.object(config, 'MAX_CONSECUTIVE_FAILURES', 2):
            _, _, _, _, state, halted, _ = self.go(self.LOP, answers=(({'partial': True, 'signature': 'p'}, 'x'),),
                                                   state={'failures': 1})
        self.assertEqual(state['failures'], 2); self.assertEqual(len(halted), 1)
        with mock.patch.object(config, 'MAX_CONSECUTIVE_FAILURES', 3):
            _, _, _, _, state, halted, _ = self.go(self.LOP, answers=(({'partial': True, 'signature': 'p'}, 'x'),),
                                                   state={'failures': 1})
        self.assertEqual(halted, [])

    def test_a_sent_swap_is_recorded_and_the_wallet_read_again(self):
        after = dict(self.LOP, balanceA=0.95, balanceB=110.0)
        ans = ({'sent': True, 'signature': 'SIG', 'swapUsdValue': 90.0, 'sold': {}, 'bought': {}}, None)
        out, _, seen, events, state, _, raw = self.go(self.LOP, answers=(ans,), after=after)
        self.assertEqual(out, after); self.assertEqual(state['failures'], 0)
        self.assertEqual(events[0], ('SWAP', 'SIG usd 90.0'))
        sw = [kw for e, kw in raw if e == 'SWAP'][0]
        self.assertEqual(sw['signature'], 'SIG'); self.assertEqual(sw['before_usd_b'], 200.0)
        out, *_ = self.go(self.LOP, answers=(ans,), after={'error': 'unreadable'})
        self.assertIsNone(out)


class BalanceWalletEdges(BalanceWallet):
    """Exact edges of the swap decision and of the outcome (mutation gaps, 2026-10-01)."""

    def no_swap(self, b):
        out, calls, seen, *_ = self.go(b)
        self.assertEqual((calls, seen), ([], []), b)
        self.assertEqual(out, b)                                               # the wallet as it is, not None

    def swaps(self, b):
        self.assertEqual(len(self.go(b)[1]), 1, b)

    def test_without_a_record_nothing_is_asked_or_said(self):
        out, calls, seen, *_ = self.go(self.LOP, rec={})
        self.assertEqual((out, calls, seen), (self.LOP, [], []))
        with mock.patch.object(rebalancer, 'notify', lambda *a, **k: self.fail('notified')):
            self.assertEqual(rebalancer.balance_wallet({'failures': 0}, dict(self.LOP), None), self.LOP)

    def test_a_record_without_token_a_skips(self):
        out, calls, seen, *_ = self.go(self.LOP, rec={'token_b': {'address': USDC}})
        self.assertEqual((out, calls), (self.LOP, [])); self.assertIn('swap_skipped', seen)

    def test_a_short_side_of_exactly_need_does_not_swap(self):
        # The capital is capped, so a side can reach `need` while the wallet is lopsided.
        C = config.MAX_USD / (2 * config.SIDE_CAP_FRACTION)
        need = C * rebalancer.side_target_fraction() * 0.97
        b = bal(400.0, need, price=1.0, native=None)
        self.assertAlmostEqual(rebalancer.capital(b), C)
        self.no_swap(b)
        self.swaps(bal(400.0, need * 0.999, price=1.0, native=None))

    def test_a_gap_of_exactly_4_percent_is_balanced(self):
        self.assertEqual(abs(52.0 - 48.0), 0.04 * (52.0 + 48.0))
        self.assertLess(48.0, rebalancer.capital(bal(52.0, 48.0, price=1.0, native=None)) * 0.5 * 0.97)   # under need
        self.no_swap(bal(52.0, 48.0, price=1.0, native=None))
        self.no_swap(bal(48.0, 52.0, price=1.0, native=None))                   # nothing reserved off A either
        self.swaps(bal(52.5, 47.5, price=1.0, native=None))

    def test_the_quote_price_scales_both_sides(self):
        self.no_swap(bal(50.0, 50.0, price=1.0, q=2.0, native=None))

    def test_a_side_under_one_unit_counts_as_it_is(self):
        self.swaps(bal(1.0, 0.5, price=1.0, native=None))
        self.swaps(bal(0.03, 100.0, price=120.0, native='A'))                   # under the reserve: nothing of A

    def test_the_reserve_comes_off_a_native_b_side(self):
        self.swaps(bal(0.05, RES() + 0.03, price=1.0, q=1000.0, native='B'))  # $50 of A, $30 of B

    def test_the_quote_price_reaches_the_targets_and_the_hints(self):
        b = bal(0.2, 200.0, price=120.0, q=2.0)
        _, calls, *_ = self.go(b)
        C = rebalancer.capital(b)
        a = calls[0][0]
        self.assertAlmostEqual(float(a[3]), C / 2 + rebalancer.OPEN_RENT_HEADROOM_SOL * 120.0 * 2.0, places=2)
        self.assertAlmostEqual(float(a[4]), C / 2, places=2)
        h = json.loads(calls[0][1]['extra_env']['LPBOT_TOKEN_HINTS'])
        self.assertEqual((h[SOL]['usd'], h[USDC]['usd']), (240.0, 2.0))

    def test_an_answer_with_a_transport_error_is_retried(self):
        ok = ({'sent': True, 'signature': 's'}, None)
        _, calls, *_ = self.go(self.LOP, answers=(({'quoted': True}, 'timeout'), ok))
        self.assertEqual(len(calls), 2)

    def test_an_answer_with_an_error_and_nothing_sent_opens_as_it_is(self):
        out, _, seen, _, state, *_ = self.go(self.LOP, answers=(({'quoted': True}, 'custom program error'),))
        self.assertEqual(out, self.LOP); self.assertIn('swap_skipped', seen); self.assertEqual(state['failures'], 0)

    def test_a_partial_is_a_failure_with_or_without_a_signature_or_an_error(self):
        for ans in (({'partial': True}, 'boom'), ({'partial': True, 'signature': 'p', 'sent': True}, None)):
            out, _, seen, _, state, *_ = self.go(self.LOP, answers=(ans,))
            self.assertIsNone(out, ans); self.assertEqual(state['failures'], 1, ans); self.assertIn('swap_failed', seen)
