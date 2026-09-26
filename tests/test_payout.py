"""The fee split: the owner's rule, the transfer wiring, the sizing base."""
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import fees        # noqa: E402
import rebalancer  # noqa: E402

SOL = fees.NATIVE_MINT
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'


def kinds(parts):
    return {(p['symbol'], p['kind']): round(p['amount'], 9) for p in parts}


class Split(unittest.TestCase):
    F = [(SOL, 'SOL', 0.002, 120.0), (USDC, 'USDC', 0.3, 1.0)]

    def test_normal_usdc_paid_sol_reinvested(self):
        self.assertEqual(kinds(fees.split(self.F, USDC, 0.07, 0.05)),
                         {('SOL', 'reinvested'): 0.002, ('USDC', 'paid'): 0.3})

    def test_gas_low_sol_refills_only_to_the_reserve_and_usdc_is_reinvested(self):
        # 0.0495 SOL: needs 0.0005, the other 0.0015 is reinvested
        self.assertEqual(kinds(fees.split(self.F, USDC, 0.0495, 0.05)),
                         {('SOL', 'gas'): 0.0005, ('SOL', 'reinvested'): 0.0015, ('USDC', 'reinvested'): 0.3})
        # far below: every SOL fee is gas
        self.assertEqual(kinds(fees.split(self.F, USDC, 0.01, 0.05)),
                         {('SOL', 'gas'): 0.002, ('USDC', 'reinvested'): 0.3})

    def test_a_pool_without_the_payout_token_reinvests_everything(self):
        f = [(SOL, 'SOL', 0.002, 120.0), ('JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN', 'JUP', 5.0, 0.5)]
        self.assertEqual({k for _, k in kinds(fees.split(f, USDC, 0.07, 0.05))}, {'reinvested'})

    def test_payout_token_may_be_any_mint_and_zero_fees_are_dropped(self):
        f = [(SOL, 'SOL', 0.0, 120.0), (USDC, 'USDC', 0.3, 1.0)]
        self.assertEqual(kinds(fees.split(f, SOL, 0.07, 0.05)), {('USDC', 'reinvested'): 0.3})
        self.assertEqual(kinds(fees.split([(SOL, 'SOL', 0.01, 120.0)], SOL, 0.07, 0.05)), {('SOL', 'paid'): 0.01})
        self.assertEqual(fees.split(self.F, None, 0.07, 0.05)[1]['kind'], 'reinvested')

    def test_usd_values(self):
        p = fees.split(self.F, USDC, 0.07, 0.05)
        self.assertAlmostEqual(sum(x['usd'] for x in p), 0.002 * 120 + 0.3)


class Distribute(unittest.TestCase):
    def run_it(self, sol, chain_result=({'signature': 'sig'}, None), state=None):
        rows, calls, sent = [], [], []
        bal = {'balanceA': sol, 'balanceB': 40.0, 'sol': sol, 'price': 120.0, 'quoteUsd': 1.0}
        state = state if state is not None else {}
        with mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(rebalancer.config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: bal), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append((a, k)) or chain_result)), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer.db, 'record_payout', lambda *a, **k: rows.append(a[6])):
            rebalancer.distribute(state, 'M', 0.002, 0.3)
        return rows, calls, sent, state

    def test_pays_usdc_to_the_profit_wallet_and_records_the_rest(self):
        rows, calls, sent, _ = self.run_it(sol=0.07)
        self.assertEqual(len(calls), 1)
        a, k = calls[0]
        self.assertEqual((a[0], a[1], a[3], k['dex']), ('send', USDC, PROFIT, 'payout'))
        self.assertIn('--execute', a)
        self.assertEqual(sorted(rows), ['paid', 'reinvested'])
        self.assertIn('PAYOUT', sent)

    def test_gas_low_sends_nothing(self):
        rows, calls, _, _ = self.run_it(sol=0.03)
        self.assertEqual(calls, [])
        self.assertEqual(sorted(rows), ['gas', 'reinvested'])

    def test_failed_transfer_is_owed_and_retried_next_time(self):
        rows, _, sent, state = self.run_it(sol=0.07, chain_result=(None, 'rpc down'))
        self.assertIn('owed', rows); self.assertIn('payout_failed', sent)
        self.assertAlmostEqual(state['payout_owed'][USDC], 0.3)
        _, calls, _, state = self.run_it(sol=0.07, state=state)
        self.assertAlmostEqual(float(calls[0][0][2]), 0.6)          # owed + new
        self.assertNotIn(USDC, state['payout_owed'])

    def test_off_does_nothing(self):
        with mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', False), \
                mock.patch.object(rebalancer, 'chain', side_effect=AssertionError('sent')):
            self.assertIsNone(rebalancer.distribute({}, 'M', 0.002, 0.3))


class Capital(unittest.TestCase):
    def test_reinvested_fees_raise_the_base_under_the_ceiling(self):
        with mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'CAPITAL_USD', 190.0), \
                mock.patch.object(rebalancer.config, 'MAX_USD', 260.0), \
                mock.patch.object(rebalancer.config, 'SIDE_CAP_FRACTION', 0.55):
            with mock.patch.object(rebalancer.db, 'reinvested_usd', lambda n: 5.0):
                self.assertAlmostEqual(rebalancer.capital(), 195.0)
            with mock.patch.object(rebalancer.db, 'reinvested_usd', lambda n: 500.0):
                self.assertAlmostEqual(rebalancer.capital(), 260 / 1.1)
        with mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', False), \
                mock.patch.object(rebalancer.config, 'CAPITAL_USD', 190.0), \
                mock.patch.object(rebalancer.db, 'reinvested_usd', side_effect=AssertionError('read')):
            self.assertEqual(rebalancer.capital(), 190.0)


class SwapRetry(unittest.TestCase):
    BAL = {'price': 100.0, 'quoteUsd': 1.0, 'balanceA': 0.06, 'balanceB': 300.0, 'nativeSide': 'A'}
    REC = {'token_a': {'address': SOL, 'decimals': 9, 'symbol': 'SOL'},
           'token_b': {'address': USDC, 'decimals': 6, 'symbol': 'USDC'}}

    def go(self, results):
        global calls_env
        calls, state, calls_env = [], {'failures': 0}, []
        it = iter(results)
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or calls_env.append(k.get('extra_env')) or next(it))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: dict(self.BAL, balanceA=1.0, balanceB=100.0)), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None), \
                mock.patch.object(rebalancer.config, 'REBALANCE_SWAP', True), \
                mock.patch.object(rebalancer.config, 'CAPITAL_USD', 190.0), \
                mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', False):
            out = rebalancer.balance_wallet(state, dict(self.BAL), self.REC)
        return out, calls, state

    def test_a_rate_limited_swap_is_retried_once(self):
        out, calls, state = self.go([(None, 'RPC rate limited'), ({'sent': True, 'signature': 's'}, None)])
        self.assertEqual(len(calls), 2); self.assertIsNotNone(out); self.assertEqual(state['failures'], 0)
        import json as _j
        hints = _j.loads(calls_env[-1]['LPBOT_TOKEN_HINTS']) if calls_env and calls_env[-1] else {}
        self.assertIn(SOL, hints); self.assertEqual(hints[USDC]['usd'], 1.0)

    def test_a_program_failure_or_a_sent_swap_is_not_retried(self):
        out, calls, state = self.go([(None, 'PriceSlippageCheck (6017): price moved beyond the slippage limit')])
        self.assertEqual(len(calls), 1); self.assertEqual(state['failures'], 0); self.assertIsNotNone(out)
        _, calls, state = self.go([({'signature': 's', 'partial': True}, 'confirm timed out')])
        self.assertEqual(len(calls), 1); self.assertEqual(state['failures'], 1)


class SwapGate(unittest.TestCase):
    """The swap tops a side up to its deposit cap, not merely to half."""
    def go(self, a_sol, b_usdc):
        calls = []
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or ({'sent': True, 'signature': 's'}, None))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: {'balanceA': 1.0, 'balanceB': 100.0, 'price': 100.0}), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None), \
                mock.patch.object(rebalancer.config, 'REBALANCE_SWAP', True), \
                mock.patch.object(rebalancer.config, 'CAPITAL_USD', 190.0), \
                mock.patch.object(rebalancer.config, 'SIDE_CAP_FRACTION', 0.55), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', False):
            rebalancer.balance_wallet({'failures': 0}, {'price': 100.0, 'quoteUsd': 1.0, 'balanceA': a_sol,
                                                        'balanceB': b_usdc, 'nativeSide': 'A'},
                                      {'token_a': {'address': SOL}, 'token_b': {'address': USDC}})
        return len(calls)

    def test_the_2026_09_26_case_now_swaps(self):
        # SOL side worth $97.6 after reserve and headroom, USDC $143: deposit would cap at ~$195
        self.assertEqual(self.go(1.035, 143.0), 1)

    def test_both_sides_at_the_cap_or_balanced_do_not_swap(self):
        self.assertEqual(self.go(1.2, 110.0), 0)             # both above 0.55 * 190 * 0.97
        self.assertEqual(self.go(0.559, 50.0), 0)            # small but balanced: nothing to gain
