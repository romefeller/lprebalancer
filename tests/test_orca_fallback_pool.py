"""The DJT halt of 2026-10-09 and its three fixes.

At 13:26Z the swing moved from SOL/USDC to the Orca DJT/USDC pool while
Jupiter answered 429. The Orca fallback swap knew SOL/USDC only, so it refused
DJT/USDC; the 1.6 SOL left behind stayed unsold; the open went ahead with 0 DJT
and Orca refused it (0x177c) three times, which wrote HALT at 13:47Z.

A: the fallback swaps on the held pool when it is an Orca pool of that pair,
   and the left-behind sale falls back to Orca too.
B: a failed swap that leaves a side the band needs (almost) empty holds: no
   open, no failure counted, no HALT.
C (swap_orca.sendLanded, tests/test_swap_orca.mjs): the swap is re-sent until
   it lands or expires.
"""
import json
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures
_fixtures.ensure_profile()

import config      # noqa: E402
import rebalancer  # noqa: E402
from test_deploy_all import bal, SOL, USDC  # noqa: E402

DJT = 'DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF'
DJT_POOL = '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG'
REC = {'token_a': {'address': DJT, 'decimals': 6, 'symbol': 'DJT'},
       'token_b': {'address': USDC, 'decimals': 6, 'symbol': 'USDC'}}
J429 = (None, 'Jupiter rate limited: ERROR: Jupiter 429 on /swap/v1/quote: Rate limit exceeded')
NO_POOL = (None, f'ERROR: no default Orca pool for {DJT}/{USDC}; pass --pool <whirlpool>')
ORCA_OK = ({'sent': True, 'signature': 'ORCA', 'routePlan': ['Orca Whirlpool']}, None)


def on_pool(dex, pool, tokens):
    """Patches that put the profile on `pool` of `dex` holding `tokens`."""
    return [mock.patch.object(config, 'DEX', dex), mock.patch.object(config, 'POOL', pool),
            mock.patch.object(rebalancer, 'pool_tokens', lambda: tokens)]


class Patched(unittest.TestCase):
    def setUp(self):
        with rebalancer.db.cursor(commit=True) as cur:
            cur.execute('truncate health')
        for name, v in (('DEPLOY_ALL', True), ('MAX_USD', 300.0), ('SIDE_CAP_FRACTION', 0.55),
                        ('GAS_RESERVE_SOL', 0.05), ('CAPITAL_USD', 190.0), ('PAYOUT_ENABLED', False),
                        ('REBALANCE_SWAP', True), ('MAX_CONSECUTIVE_FAILURES', 3)):
            p = mock.patch.object(config, name, v); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(rebalancer, 'SWAP_FALLBACK', 'orca-swap'); p.start(); self.addCleanup(p.stop)
        for p in on_pool('orca', DJT_POOL, ((DJT, 'DJT'), (USDC, 'USDC'))):
            p.start(); self.addCleanup(p.stop)
        self.notes = []

    def tearDown(self):
        with rebalancer.db.cursor(commit=True) as cur:
            cur.execute('truncate health')


# --- A: the fallback's pool -------------------------------------------------------------
class FallbackPoolArgs(Patched):
    def test_the_held_orca_pool_of_the_pair_is_passed(self):
        self.assertEqual(rebalancer.fallback_pool_args(DJT, USDC), ['--pool', DJT_POOL])
        self.assertEqual(rebalancer.fallback_pool_args(USDC, DJT), ['--pool', DJT_POOL])      # either order

    def test_another_pair_gets_the_default_pools(self):
        self.assertEqual(rebalancer.fallback_pool_args(SOL, USDC), [])     # the SOL left behind after the switch
        self.assertEqual(rebalancer.fallback_pool_args(DJT, SOL), [])

    def test_a_pool_that_is_not_orca_is_never_passed(self):
        for dex in ('raydium-clmm', 'meteora', 'uniswap-v3-polygon'):
            with mock.patch.object(config, 'DEX', dex):
                self.assertEqual(rebalancer.fallback_pool_args(DJT, USDC), [], dex)

    def test_an_unreadable_pool_record_passes_nothing(self):
        def boom():
            raise RuntimeError('pool record unavailable')
        with mock.patch.object(rebalancer, 'pool_tokens', boom):
            self.assertEqual(rebalancer.fallback_pool_args(DJT, USDC), [])

    def test_the_orca_script_knows_djt_usdc_without_the_flag(self):
        src = (_fixtures.ROOT / 'swap_orca.mjs').read_text()
        self.assertIn("[`${DJT_MINT}/${USDC_MINT}`]: '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG'", src)
        self.assertIn(f"export const DJT_MINT = '{DJT}'", src)


# --- B: the side the band needs ---------------------------------------------------------
class OneSideShort(unittest.TestCase):
    def test_replay_2026_10_09_no_djt_is_short(self):
        self.assertTrue(rebalancer.one_side_short(0.0, 13.70, 0.5, 0.5, 13.70))

    def test_both_sides_held_is_not_short(self):
        self.assertFalse(rebalancer.one_side_short(40.0, 150.0, 0.5, 0.5, 190.0))

    def test_exactly_at_the_floor_is_not_short(self):
        self.assertFalse(rebalancer.one_side_short(2.0, 98.0, 0.5, 0.5, 100.0))
        self.assertTrue(rebalancer.one_side_short(1.999, 98.0, 0.5, 0.5, 100.0))

    def test_a_side_the_band_does_not_want_may_be_empty(self):
        self.assertFalse(rebalancer.one_side_short(0.0, 100.0, 0.0, 1.0, 100.0))
        self.assertFalse(rebalancer.one_side_short(100.0, 0.0, 1.0, 0.0, 100.0))

    @settings(max_examples=300, deadline=None)
    @given(st.floats(0, 1e6), st.floats(0, 1e6), st.floats(0.01, 0.99))
    def test_property_short_iff_a_wanted_side_is_under_the_floor(self, a, b, fa):
        c = a + b
        want = a < rebalancer.OPEN_SIDE_MIN * c or b < rebalancer.OPEN_SIDE_MIN * c
        self.assertEqual(rebalancer.one_side_short(a, b, fa, 1 - fa, c), want)

    @settings(max_examples=200, deadline=None)
    @given(st.floats(0, 1e6), st.floats(0, 1e6), st.floats(1e-3, 0.99), st.floats(1.0, 1e3))
    def test_property_more_of_a_side_never_makes_it_short(self, a, b, fa, k):
        c = a + b
        if not rebalancer.one_side_short(a, b, fa, 1 - fa, c):
            self.assertFalse(rebalancer.one_side_short(a * k, b * k, fa, 1 - fa, c))

    def test_a_negative_capital_has_no_floor(self):
        self.assertFalse(rebalancer.one_side_short(0.0, 0.0, 0.5, 0.5, -5.0))


class BalanceWallet(Patched):
    """balance_wallet on the DJT/USDC wallet of 2026-10-09 13:30Z."""

    def go(self, answers, b=None):
        calls = []
        it = iter(answers)
        b = b or bal(0.0, 13.70, price=8.24, q=1.0, native=None)
        state = {'failures': 0}
        halts = []

        def fake(*a, **k):
            calls.append((a, k))
            return next(it)
        with mock.patch.object(rebalancer, 'chain', fake), \
                mock.patch.object(rebalancer, 'wallet', lambda p: b), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: self.notes.append((a, k))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'halt', lambda r: halts.append(r)), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch('builtins.print'), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            out = rebalancer.balance_wallet(state, dict(b), REC)
        return out, calls, state, halts

    def test_replay_the_fallback_swaps_on_the_djt_pool(self):
        out, calls, state, halts = self.go([J429] * 3 + [ORCA_OK])
        a, k = calls[-1]
        self.assertEqual(k['dex'], 'orca-swap')
        self.assertEqual(a[a.index('--pool') + 1], DJT_POOL)
        self.assertEqual(a[:3], ('rebalance', DJT, USDC))
        self.assertIn('--execute', a)
        self.assertIsNotNone(out)
        self.assertEqual((state['failures'], halts), (0, []))

    def test_jupiter_calls_never_get_the_pool_flag(self):
        _, calls, _, _ = self.go([J429] * 3 + [ORCA_OK])
        for a, k in calls[:-1]:
            self.assertEqual(k['dex'], 'jupiter')
            self.assertNotIn('--pool', a)

    def test_replay_both_failing_with_no_djt_holds_without_a_failure(self):
        out, calls, state, halts = self.go([J429] * 3 + [NO_POOL])
        self.assertIsNone(out)                                        # the caller opens nothing
        self.assertEqual(state['failures'], 0)
        self.assertEqual(halts, [])
        self.assertNotIn('open_unbalanced', {k for k, v in state.items() if v})
        reasons = [k.get('reason', '') for a, k in self.notes if a and a[0] == 'swap_skipped']
        self.assertTrue(any('holding' in r for r in reasons), reasons)

    def test_three_polls_of_it_never_halt(self):
        for _ in range(3):
            out, _, state, halts = self.go([J429] * 3 + [NO_POOL])
            self.assertIsNone(out)
            self.assertEqual(halts, [])

    def test_both_sides_held_still_opens_with_the_wallet(self):
        b = bal(10.0, 13.70, price=8.24, q=1.0, native=None)          # $82 of DJT, $14 of USDC: lopsided, both present
        out, _, state, halts = self.go([J429] * 3 + [NO_POOL], b=b)
        self.assertEqual(out, b)
        self.assertTrue(state['open_unbalanced'])
        self.assertEqual(halts, [])

    def test_a_sent_but_unconfirmed_fallback_still_counts(self):
        sent = ({'signature': 'ORCA', 'partial': True}, 'sent ORCA but could not confirm it: x')
        out, _, state, _ = self.go([J429] * 3 + [sent])
        self.assertIsNone(out)
        self.assertEqual(state['failures'], 1)                       # it may be on chain: a real failure


# --- A: the left-behind sale ------------------------------------------------------------
class LeftBehind(Patched):
    def go(self, answers, prices=None):
        calls = []
        it = iter(answers)
        state = {'left_behind': [SOL], 'left_behind_at': 0}

        def fake(*a, **k):
            calls.append((a, k))
            return next(it)
        with mock.patch.object(rebalancer, 'chain', fake), \
                mock.patch.object(rebalancer, 'claim_mints', lambda: ({SOL, DJT, USDC}, None, False)), \
                mock.patch.object(rebalancer.wallets, 'read_balances', lambda *a: ({SOL: 1.6676}, None)), \
                mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda m: prices or {}), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: self.notes.append((a, k))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            sold = rebalancer.sell_left_behind(state, force=True)
        return sold, calls, state

    def test_replay_jupiter_429_sells_the_sol_on_orca(self):
        sold, calls, state = self.go([J429, ORCA_OK])
        self.assertTrue(sold)
        self.assertEqual([k['dex'] for _, k in calls], ['jupiter', 'orca-swap'])
        a, k = calls[1]
        self.assertEqual(a[:5], ('rebalance', SOL, USDC, '0', '1000000'))
        self.assertNotIn('--pool', a)                                   # SOL/USDC: swap_orca's default pool
        self.assertEqual(json.loads(k['extra_env']['LPBOT_SLEEVE']), json.loads(calls[0][1]['extra_env']['LPBOT_SLEEVE']))
        self.assertEqual(state['left_behind'], [])

    def test_jupiter_selling_needs_no_fallback(self):
        sold, calls, _ = self.go([({'sent': True, 'signature': 'J'}, None)])
        self.assertTrue(sold)
        self.assertEqual([k['dex'] for _, k in calls], ['jupiter'])

    def test_never_after_jupiter_sent_something(self):
        for sent in (({'signature': 'J', 'partial': True}, 'partial'), ({'signature': 'J'}, 'could not confirm')):
            sold, calls, state = self.go([sent])
            self.assertEqual([k['dex'] for _, k in calls], ['jupiter'], sent)
            self.assertEqual(state['left_behind'], [SOL])

    def test_both_failing_keeps_it_for_the_next_try(self):
        sold, calls, state = self.go([J429, (None, 'Orca quote: no liquidity')])
        self.assertFalse(sold)
        self.assertEqual(state['left_behind'], [SOL])
        self.assertEqual(len(calls), 2)

    def test_off_means_jupiter_alone(self):
        with mock.patch.object(rebalancer, 'SWAP_FALLBACK', ''):
            sold, calls, _ = self.go([J429])
        self.assertFalse(sold)
        self.assertEqual(len(calls), 1)

    def test_a_fallback_that_is_no_signer_is_never_called(self):
        with mock.patch.object(rebalancer, 'SWAP_FALLBACK', 'no-such-signer'):
            sold, calls, _ = self.go([J429])
        self.assertEqual((sold, len(calls)), (False, 1))

    def test_a_partial_without_a_signature_is_never_sold_twice(self):
        sold, calls, state = self.go([({'partial': True}, 'partial transaction execution')])
        self.assertEqual((sold, len(calls), state['left_behind']), (False, 1, [SOL]))

    def test_an_answer_with_an_error_and_nothing_sent_falls_back(self):
        sold, calls, _ = self.go([({'quoted': True}, 'route not found'), ORCA_OK])
        self.assertEqual((sold, [k['dex'] for _, k in calls]), (True, ['jupiter', 'orca-swap']))

    def test_no_answer_and_no_error_falls_back(self):
        sold, calls, _ = self.go([(None, None), ORCA_OK])
        self.assertEqual((sold, [k['dex'] for _, k in calls]), (True, ['jupiter', 'orca-swap']))

    def test_a_signature_with_an_error_is_never_sold_twice(self):
        sold, calls, state = self.go([({'signature': 'J'}, 'late')])
        self.assertEqual((sold, len(calls), state['left_behind']), (False, 1, [SOL]))

    def test_a_noop_answer_is_no_fallback(self):
        sold, calls, state = self.go([({'noop': True}, None)])
        self.assertEqual((sold, len(calls), state['left_behind']), (False, 1, []))


if __name__ == '__main__':
    unittest.main()
