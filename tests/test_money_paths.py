"""Every branch of the money path, one behaviour per test.

Written from the mutation report of 2026-09-27 (tests/mutate.py): each test
here kills mutants the property and replay tests in test_fee_integrity.py
let through. A test name says the behaviour; the comment above a group names
the function it pins down.
"""
import math
import os
import unittest
from unittest import mock

import numpy as np

import _fixtures
import calm
import db
import fees
import guards
import rebalancer
import lp.books
import lp.harvest
import lp.paths
import lp.regime
import lp.signers
import config
import time
import txfees

SOL = fees.NATIVE_MINT
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
BONK = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'


# --- fees.split -------------------------------------------------------------------

def split(fa, fb, sol_before, reserve=0.05, payout=USDC, pa=120.0, pb=1.0, a=SOL, b=USDC):
    return fees.split([(a, 'A', fa, pa), (b, 'B', fb, pb)], payout, sol_before, reserve)


def kinds(parts):
    return sorted((p['symbol'], round(p['amount'], 12), p['kind']) for p in parts)


class Split(unittest.TestCase):
    def test_sol_exactly_at_the_reserve_is_not_low(self):
        self.assertEqual(kinds(split(0.01, 0.3, 0.05)), [('A', 0.01, 'reinvested'), ('B', 0.3, 'paid')])

    def test_just_under_the_reserve_refills_exactly_the_gap(self):
        self.assertEqual(kinds(split(0.01, 0.3, 0.049)),
                         [('A', 0.001, 'gas'), ('A', 0.009, 'reinvested'), ('B', 0.3, 'reinvested')])

    def test_a_fee_exactly_the_gap_is_all_gas(self):
        self.assertEqual(kinds(split(0.001, 0.3, 0.049)), [('A', 0.001, 'gas'), ('B', 0.3, 'reinvested')])

    def test_unknown_balance_is_not_low(self):
        self.assertEqual(kinds(split(0.01, 0.3, None)), [('A', 0.01, 'reinvested'), ('B', 0.3, 'paid')])

    def test_zero_and_none_and_negative_fees_are_dropped(self):
        self.assertEqual(split(0.0, None, 0.07), [])
        self.assertEqual(split(-1.0, 0.0, 0.03), [])

    def test_usd_is_amount_times_price_and_none_without_a_price(self):
        p = split(0.01, 0.3, 0.07, pa=150.0, pb=None)
        self.assertEqual({x['symbol']: x['usd'] for x in p}, {'A': 1.5, 'B': None})
        g = split(0.01, 0.0, 0.045, pa=150.0)
        self.assertEqual([(x['kind'], round(x['usd'], 9)) for x in g], [('gas', 0.75), ('reinvested', 0.75)])

    def test_native_on_the_b_side_refills_gas(self):
        p = split(0.3, 0.01, 0.049, a=USDC, b=SOL, pa=1.0, pb=120.0)
        self.assertEqual(kinds(p), [('A', 0.3, 'reinvested'), ('B', 0.001, 'gas'), ('B', 0.009, 'reinvested')])

    def test_no_payout_mint_reinvests_everything(self):
        self.assertEqual(kinds(split(0.01, 0.3, 0.07, payout=None)), [('A', 0.01, 'reinvested'), ('B', 0.3, 'reinvested')])

    def test_a_non_native_pool_never_books_gas(self):
        p = split(0.01, 0.3, 0.0, a=BONK)
        self.assertEqual(kinds(p), [('A', 0.01, 'reinvested'), ('B', 0.3, 'reinvested')])

    def test_the_gap_is_shared_across_two_native_rows(self):
        p = fees.split([(SOL, 'S1', 0.0006, 1.0), (SOL, 'S2', 0.0006, 1.0)], USDC, 0.049, 0.05)
        self.assertEqual(kinds(p), [('S1', 0.0006, 'gas'), ('S2', 0.0002, 'reinvested'), ('S2', 0.0004, 'gas')])

    def test_a_negative_fee_does_not_raise_the_gas_need(self):
        p = fees.split([(SOL, 'S1', -1.0, 1.0), (SOL, 'S2', 0.5, 1.0)], USDC, 0.049, 0.05)
        self.assertEqual(kinds(p), [('S2', 0.001, 'gas'), ('S2', 0.499, 'reinvested')])

    def test_no_zero_rows_when_gas_is_full(self):
        self.assertEqual(kinds(split(0.01, 0.3, 0.2)), [('A', 0.01, 'reinvested'), ('B', 0.3, 'paid')])
        self.assertTrue(all(x['amount'] > 0 for x in split(0.001, 0.3, 0.049)))

    def test_string_amounts_are_numbers(self):
        self.assertEqual(kinds(split('0.01', '0.3', 0.07)), [('A', 0.01, 'reinvested'), ('B', 0.3, 'paid')])


# --- guards.fee_read_problem --------------------------------------------------------

def rd(usd, pos=200.0, a=0.0, b=0.0):
    return {'feesAccruedA': a, 'feesAccruedB': b, 'feesAccrued_USD': usd, 'positionUsd': pos}


class FeeReadBounds(unittest.TestCase):
    def test_none_figures_are_allowed(self):
        self.assertIsNone(guards.fee_read_problem({'feesAccruedA': None, 'feesAccruedB': None,
                                                   'feesAccrued_USD': None, 'positionUsd': 200.0}))
        self.assertIsNone(guards.fee_read_problem({'positionUsd': 200.0}))

    def test_a_small_position_is_still_checked(self):
        self.assertIsNotNone(guards.fee_read_problem(rd(0.2, 0.5)))

    def test_rise_is_checked_at_zero_hours(self):
        self.assertIsNotNone(guards.fee_read_problem(rd(1.0, 200.0), 0.1, 0.0))
        self.assertIsNone(guards.fee_read_problem(rd(0.1 + 0.39, 200.0), 0.1, 0.0))

    def test_rise_is_checked_inside_the_first_hour(self):
        self.assertIsNotNone(guards.fee_read_problem(rd(15.0, 200.0), 0.1, 0.5))

    def test_the_djt_burst_passes(self):
        # 2026-10-05: +$0.56 in 2.2 minutes on a $200.33 position, real fees
        self.assertIsNone(guards.fee_read_problem(rd(0.81689, 200.33), 0.252557, 0.037))

    def test_negative_hours_skip_the_rise_check(self):
        self.assertIsNone(guards.fee_read_problem(rd(5.0, 200.0), 0.1, -1.0))

    def test_no_previous_or_no_age_skips_the_rise_check(self):
        self.assertIsNone(guards.fee_read_problem(rd(5.0, 200.0), None, 1.0))
        self.assertIsNone(guards.fee_read_problem(rd(5.0, 200.0), 0.1, None))

    def test_integer_figures_are_numbers(self):
        self.assertIsNone(guards.fee_read_problem({'feesAccruedA': 0, 'feesAccruedB': 1,
                                                   'feesAccrued_USD': 1, 'positionUsd': 200}))

    def test_reason_names_the_problem(self):
        self.assertIn('exceed', guards.fee_read_problem(rd(50.0)))
        self.assertIn('rose', guards.fee_read_problem(rd(10.0), 0.1, 0.1))
        self.assertIn('feesAccruedB', guards.fee_read_problem(rd(0.1, b=-1.0)))


# --- rebalancer.read_status, fee_problem, sanitised --------------------------------

BAD = {'positionMint': 'M', 'whirlpool': POOL, 'price': 100.0, 'quoteUsd': 1.0, 'positionUsd': 200.0,
       'feesAccruedA': 1.0, 'feesAccruedB': 900.0, 'feesAccrued_USD': 1000.0}


class Status(unittest.TestCase):
    def run_it(self, answers, last=None, args=()):
        seen, calls = [], []
        it = iter(answers)
        def chain(*a, **k):
            calls.append(a)
            return next(it)
        with mock.patch.object(lp.signers, 'chain', chain), \
                mock.patch.object(time, 'sleep', lambda s: None), \
                mock.patch.object(db, 'last_fees', lambda m: last), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: seen.append(ev)):
            out = lp.signers.read_status(*args)
        return out, seen, calls

    def test_an_unreadable_answer_is_passed_on(self):
        self.assertEqual(self.run_it([(None, 'rpc down')])[0], (None, 'rpc down'))

    def test_a_mint_filter_is_passed_to_both_reads(self):
        _, _, calls = self.run_it([(BAD, None), (BAD, None)], args=('M',))
        self.assertEqual(calls, [('status', 'M'), ('status', 'M')])

    def test_an_unreadable_second_read_keeps_the_sanitised_first(self):
        (out, _), seen, _ = self.run_it([(BAD, None), (None, 'down')])
        self.assertEqual(out['feesAccrued_USD'], 0.0); self.assertIn('fee_read_rejected', seen)

    def test_the_second_read_must_itself_pass(self):
        (out, _), seen, _ = self.run_it([(BAD, None), (dict(BAD), None)])
        self.assertEqual(out['feesAccrued_USD'], 0.0); self.assertIn('fee_read_rejected', seen)

    def test_the_rise_since_the_last_snapshot_is_checked(self):
        # a level under 10% of the position, but 1000x the last snapshot in 2 minutes
        risen = dict(BAD, feesAccruedA=0.0, feesAccruedB=15.0, feesAccrued_USD=15.0)
        last = {'accrued_a': 0.0, 'accrued_b': 0.015, 'accrued_usd': 0.015, 'hours': 0.03}
        (out, _), seen, _ = self.run_it([(risen, None), (risen, None)], last)
        self.assertEqual(out['feesAccrued_USD'], 0.015); self.assertIn('fee_read_rejected', seen)

    def test_an_unreadable_ledger_still_checks_the_level(self):
        with mock.patch.object(db, 'last_fees', side_effect=RuntimeError('db')):
            self.assertIsNotNone(lp.signers.fee_problem(BAD))
            out = lp.signers.sanitised(BAD, 'why')
        self.assertEqual(out['feesAccrued_USD'], 0.0)

    def test_sanitised_values_quote_units_by_the_quote_price(self):
        last = {'accrued_a': 0.001, 'accrued_b': 0.2, 'accrued_usd': 0.5, 'hours': 0.1}
        with mock.patch.object(db, 'last_fees', lambda m: last), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: None):
            out = lp.signers.sanitised(dict(BAD, quoteUsd=2.0), 'why')
            self.assertEqual((out['feesAccruedA'], out['feesAccruedB'], out['feesAccrued_USD']), (0.001, 0.2, 0.5))
            self.assertEqual(out['feesAccrued_quote'], 0.25)
            out = lp.signers.sanitised(dict(BAD, quoteUsd=None), 'why')
            self.assertEqual(out['feesAccrued_quote'], 0.5)
        with mock.patch.object(db, 'last_fees', lambda m: {'accrued_a': None, 'accrued_b': None,
                                                                    'accrued_usd': None, 'hours': 1.0}), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: None):
            out = lp.signers.sanitised(BAD, 'why')
        self.assertEqual((out['feesAccruedA'], out['feesAccruedB'], out['feesAccrued_USD']), (0.0, 0.0, 0.0))
        self.assertEqual(BAD['feesAccrued_USD'], 1000.0)          # the input is not modified


# --- rebalancer.measured_fees -------------------------------------------------------

def vault_tx(pool, a_raw, b_raw, mint_a=SOL, mint_b=USDC):
    bal = lambda i, m, amt, dec: {'accountIndex': i, 'owner': pool, 'mint': m,
                                  'uiTokenAmount': {'amount': str(amt), 'decimals': dec}}
    return {'meta': {'err': None,
                     'preTokenBalances': [bal(0, mint_a, 10**12, 9), bal(1, mint_b, 10**12, 6)],
                     'postTokenBalances': [bal(0, mint_a, 10**12 - a_raw, 9), bal(1, mint_b, 10**12 - b_raw, 6)]},
            'transaction': {'message': {'accountKeys': []}}}


class Measured(unittest.TestCase):
    def run_it(self, out, status, a, b, usd, fetch, tokens=((SOL, 'SOL'), (USDC, 'USDC')), last=None):
        seen = []
        with mock.patch.object(lp.capital, 'pool_tokens', lambda: tokens), \
                mock.patch.object(txfees, 'fetch', fetch), \
                mock.patch.object(db, 'last_fees', lambda m: last), \
                mock.patch.object(db, 'event', lambda *x: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: seen.append((ev, kw))):
            got = lp.harvest.measured_fees(out, status, a, b, usd)
        return got, [e for e, _ in seen]

    ST = {'positionMint': 'M', 'whirlpool': POOL, 'price': 100.0, 'quoteUsd': 2.0, 'positionUsd': 200.0}

    def test_the_dollar_value_uses_price_and_quote(self):
        (a, b, usd), _ = self.run_it({'signature': 's'}, self.ST, 0.0011, 0.21, 0.64,
                                     lambda rpc, s, **k: vault_tx(POOL, 10**6, 200000))
        self.assertEqual((a, b), (0.001, 0.2))
        self.assertAlmostEqual(usd, (0.001 * 100.0 + 0.2) * 2.0)

    def test_a_missing_quote_price_counts_the_quote_as_a_dollar(self):
        (_, _, usd), _ = self.run_it({'signature': 's'}, dict(self.ST, quoteUsd=None), 0.0011, 0.21, 0.32,
                                     lambda rpc, s, **k: vault_tx(POOL, 10**6, 200000))
        self.assertAlmostEqual(usd, 0.3)

    def test_every_signature_is_read_and_the_pool_is_the_status_pool(self):
        seen = []
        other = '4QU2NpRaqmKMvPSwVKQDeW4V6JFEKJdkzbzdauumD9qN'
        (a, b, _), _ = self.run_it({'signature': 's2', 'signatures': ['s1', 's2']}, dict(self.ST, whirlpool=other),
                                   0.0, 0.0, 0.0, lambda rpc, s, **k: seen.append(s) or vault_tx(other, 10**6, 10**5))
        self.assertEqual(seen, ['s1', 's2']); self.assertEqual((a, b), (0.002, 0.2))

    def test_without_the_status_pool_the_profile_pool_is_used(self):
        with mock.patch.object(config, 'POOL', POOL):
            (a, _, _), _ = self.run_it({'signature': 's'}, {k: v for k, v in self.ST.items() if k != 'whirlpool'},
                                       0.0, 0.0, 0.0, lambda rpc, s, **k: vault_tx(POOL, 10**6, 0))
        self.assertEqual(a, 0.001)

    def test_a_disagreement_is_reported_and_an_agreement_is_not(self):
        f = lambda rpc, s, **k: vault_tx(POOL, 10**6, 200000)            # $0.6 at price 100, quote 2
        _, ev = self.run_it({'signature': 's'}, self.ST, 0.001, 0.2, 0.6, f)
        self.assertNotIn('harvest_measured', ev)
        _, ev = self.run_it({'signature': 's'}, self.ST, 0.001, 0.2, 0.605, f)     # within 1 cent
        self.assertNotIn('harvest_measured', ev)
        _, ev = self.run_it({'signature': 's'}, self.ST, 0.002, 0.2, 0.8, f)
        self.assertIn('harvest_measured', ev)
        big = lambda rpc, s, **k: vault_tx(POOL, 10**9, 100 * 10**6)       # $400 measured
        _, ev = self.run_it({'signature': 's'}, self.ST, 1.0, 100.0, 390.0, big)   # 2.5% off: fine
        self.assertNotIn('harvest_measured', ev)
        _, ev = self.run_it({'signature': 's'}, self.ST, 1.0, 100.0, 370.0, big)   # 7.5% off: reported
        self.assertIn('harvest_measured', ev)

    def test_unreadable_pool_tokens_fall_back_to_a_sane_read(self):
        with mock.patch.object(lp.capital, 'pool_tokens', side_effect=RuntimeError('api')), \
                mock.patch.object(db, 'last_fees', lambda m: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: None):
            got = lp.harvest.measured_fees({'signature': 's'}, self.ST, 0.001, 0.2, 0.6)
        self.assertEqual(got, (0.001, 0.2, 0.6))

    def test_no_output_falls_back(self):
        got, ev = self.run_it(None, self.ST, 0.001, 0.2, 0.6, lambda rpc, s, **k: vault_tx(POOL, 1, 1))
        self.assertEqual(got, (0.001, 0.2, 0.6)); self.assertIn('harvest_unmeasured', ev)


# --- rebalancer.distribute: every branch ---------------------------------------------

class Distribute(unittest.TestCase):
    def run_it(self, fa, fb, bal=None, chain_result=({'signature': 'sig'}, None), state=None,
               tokens=((SOL, 'SOL'), (USDC, 'USDC')), pin=PROFIT, enabled=True, payout=USDC):
        rows, calls, seen = [], [], []
        bal = bal if bal is not None else {'balanceA': 0.07, 'balanceB': 40.0, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 1.0}
        state = state if state is not None else {}
        results = iter(chain_result) if isinstance(chain_result, list) else None
        def chain(*a, **k):
            calls.append(a)
            return next(results) if results else chain_result
        def rec(*a, **k):
            rows.append({'mint': a[2], 'symbol': a[3], 'amount': a[4], 'usd': a[5], 'kind': a[6], **k})
        with mock.patch.dict(os.environ, {'LPBOT_PROFIT_WALLET_PIN': pin}), \
                mock.patch.object(config, 'PAYOUT_ENABLED', enabled), \
                mock.patch.object(config, 'PAYOUT_MINT', payout), \
                mock.patch.object(config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(lp.capital, 'pool_tokens', tokens if callable(tokens) else (lambda: tokens)), \
                mock.patch.object(lp.capital, 'wallet', lambda p: bal), \
                mock.patch.object(lp.signers, 'chain', chain), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: seen.append((ev, kw))), \
                mock.patch.object(db, 'record_payout', rec):
            ret = lp.harvest.distribute(state, 'M', fa, fb)
        return ret, rows, calls, [e for e, _ in seen], state, seen

    def test_disabled_or_no_fees_does_nothing(self):
        self.assertEqual(self.run_it(0.001, 0.3, enabled=False)[:3], (None, [], []))
        self.assertEqual(self.run_it(0.0, 0.0)[:3], (None, [], []))
        self.assertEqual(self.run_it(None, None)[:3], (None, [], []))

    def test_one_side_alone_is_split(self):
        ret, rows, *_ = self.run_it(0.0, 0.3)
        self.assertEqual([(r['symbol'], r['kind']) for r in rows], [('USDC', 'paid')])
        ret, rows, *_ = self.run_it(0.002, None)
        self.assertEqual([(r['symbol'], r['kind']) for r in rows], [('SOL', 'reinvested')])

    def test_a_none_fee_beside_a_real_one(self):
        _, rows, *_ = self.run_it(None, 0.3)
        self.assertEqual([(r['symbol'], r['kind']) for r in rows], [('USDC', 'paid')])
        _, rows, *_ = self.run_it(0.002, None)
        self.assertEqual([(r['symbol'], r['kind']) for r in rows], [('SOL', 'reinvested')])

    def test_native_sol_short_of_the_native_fee_refuses_even_if_token_a_covers_it(self):
        # token A can include wrapped SOL; native SOL below the fee still means a bad read
        bal = {'balanceA': 0.07, 'balanceB': 40.0, 'sol': 0.001, 'price': 120.0, 'quoteUsd': 1.0}
        self.assertEqual(self.run_it(0.002, 0.3, bal=bal)[1], [])

    def test_an_unreadable_sol_balance_reads_as_no_gas(self):
        bal = {'balanceA': 0.07, 'balanceB': 40.0, 'sol': None, 'price': 120.0, 'quoteUsd': 1.0}
        _, rows, calls, *_ = self.run_it(0.0, 0.3, bal=bal)
        self.assertEqual([(r['symbol'], r['kind']) for r in rows], [('USDC', 'reinvested')])
        self.assertEqual(calls, [])

    def test_a_none_fee_is_checked_as_zero_against_a_small_wallet(self):
        bal = {'balanceA': 0.5, 'balanceB': 0.5, 'sol': 0.5, 'price': 120.0, 'quoteUsd': 1.0}
        self.assertTrue(self.run_it(0.002, None, bal=bal)[1])
        self.assertTrue(self.run_it(None, 0.3, bal=dict(bal, sol=0.5, balanceA=0.5))[1])

    def test_a_none_balance_on_one_side_refuses_that_fee(self):
        base = {'balanceA': 0.07, 'balanceB': 40.0, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 1.0}
        self.assertEqual(self.run_it(0.002, 0.3, bal=dict(base, balanceA=None))[1], [])
        self.assertEqual(self.run_it(0.002, 0.3, bal=dict(base, balanceB=None))[1], [])

    def test_the_slack_is_one_nano_on_each_side(self):
        base = {'balanceA': 0.07, 'balanceB': 0.3, 'sol': 0.08, 'price': 120.0, 'quoteUsd': 1.0}
        for fa, fb, ok in ((0.07 + 7e-10, 0.3, True), (0.07 + 1.2e-9, 0.3, False),
                           (0.07, 0.3 + 7e-10, True), (0.07, 0.3 + 1.2e-9, False),
                           (0.07 + 1e-9, 0.3, True)):
            self.assertEqual(bool(self.run_it(fa, fb, bal=base)[1]), ok, (fa, fb))

    def test_unreadable_pool_tokens_or_wallet_book_nothing(self):
        def boom():
            raise RuntimeError('api down')
        ret, rows, calls, ev, *_ = self.run_it(0.002, 0.3, tokens=boom)
        self.assertEqual((ret, rows, calls), (None, [], [])); self.assertIn('payout_skipped', ev)
        ret, rows, calls, ev, *_ = self.run_it(0.002, 0.3, bal={'sol': 0.07})
        self.assertEqual((ret, rows, calls), (None, [], [])); self.assertIn('payout_skipped', ev)

    def test_a_quote_token_without_a_dollar_price_books_nothing(self):
        # quote_price None: no dollar figure for the split, so nothing is paid, booked or sent
        with mock.patch.object(lp.capital, 'quote_price', lambda bal: None):
            ret, rows, calls, ev, *_, seen = self.run_it(0.002, 0.3)
        self.assertEqual((ret, rows, calls), (None, [], []))
        self.assertEqual(ev, ['payout_skipped'])
        self.assertIn('no USD price', seen[0][1]['reason'])

    def test_returns_the_parts(self):
        ret, rows, *_ = self.run_it(0.002, 0.3)
        self.assertEqual(sorted((p['symbol'], p['kind']) for p in ret), [('SOL', 'reinvested'), ('USDC', 'paid')])

    def test_dollars_are_valued_by_price_and_quote(self):
        bal = {'balanceA': 0.07, 'balanceB': 40.0, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 2.0}
        _, rows, *_ = self.run_it(0.002, 0.3, bal=bal, payout=None)
        self.assertEqual({r['symbol']: round(r['usd'], 9) for r in rows}, {'SOL': 0.48, 'USDC': 0.6})
        bal = dict(bal, quoteUsd=None)
        _, rows, *_ = self.run_it(0.002, 0.3, bal=bal, payout=None)
        self.assertEqual({r['symbol']: round(r['usd'], 9) for r in rows}, {'SOL': 0.24, 'USDC': 0.3})

    def test_native_on_the_b_side_is_found(self):
        bal = {'balanceA': 40.0, 'balanceB': 0.03, 'sol': 0.03, 'price': 1 / 120.0, 'quoteUsd': 120.0}
        _, rows, *_ = self.run_it(0.3, 0.002, bal=bal, tokens=((USDC, 'USDC'), (SOL, 'SOL')))
        self.assertIn(('SOL', 'gas'), [(r['symbol'], r['kind']) for r in rows])
        self.assertNotIn('paid', [r['kind'] for r in rows])             # gas low: nothing paid

    def test_a_non_native_pool_uses_the_whole_wallet_sol(self):
        bal = {'balanceA': 5.0, 'balanceB': 40.0, 'sol': 0.03, 'price': 2.0, 'quoteUsd': 1.0}
        _, rows, *_ = self.run_it(1.0, 0.3, bal=bal, tokens=((BONK, 'BONK'), (USDC, 'USDC')))
        self.assertEqual(sorted((r['symbol'], r['kind']) for r in rows), [('BONK', 'reinvested'), ('USDC', 'reinvested')])

    def test_a_fee_equal_to_the_wallet_is_possible_and_a_hair_over_is_not(self):
        bal = {'balanceA': 0.07, 'balanceB': 0.3, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 1.0}
        self.assertTrue(self.run_it(0.07, 0.3, bal=bal)[1])
        self.assertTrue(self.run_it(0.07, 0.3 + 5e-10, bal=bal)[1])          # inside the 1e-9 slack
        self.assertTrue(self.run_it(0.07 + 5e-10, 0.3, bal=dict(bal, sol=0.0700000006))[1])
        self.assertEqual(self.run_it(0.07, 0.3 + 2e-9, bal=bal)[1], [])
        self.assertEqual(self.run_it(0.07 + 2e-9, 0.3, bal=dict(bal, sol=0.071))[1], [])
        edge = 0.3 + 1e-9
        self.assertTrue(self.run_it(0.07, edge, bal=bal)[1])                 # exactly at the slack

    def test_none_wallet_balances_read_as_zero(self):
        bal = {'balanceA': None, 'balanceB': None, 'sol': None, 'price': 120.0, 'quoteUsd': 1.0}
        self.assertEqual(self.run_it(0.002, 0.3, bal=bal)[1], [])

    def test_gas_rows_say_why(self):
        bal = {'balanceA': 0.03, 'balanceB': 40.0, 'sol': 0.03, 'price': 120.0, 'quoteUsd': 1.0}
        _, rows, *_ = self.run_it(0.002, 0.3, bal=bal)
        self.assertEqual({r['kind']: r.get('detail') for r in rows}, {'gas': 'gas under the reserve', 'reinvested': None})

    def test_a_held_payout_names_what_stayed_and_the_reserve(self):
        # 2026-10-07 DJT close: gas 0.049209 < 0.05 held a 0.694 USDC payout; the message must say so
        bal = {'balanceA': 0.03, 'balanceB': 40.0, 'sol': 0.03, 'price': 120.0, 'quoteUsd': 1.0}
        *_, calls, _, _, seen = self.run_it(0.002, 0.3, bal=bal)
        pay = [kw for e, kw in seen if e == 'PAYOUT'][0]
        self.assertTrue(pay['gas_low']); self.assertEqual(pay['sent'], []); self.assertEqual(calls, [])
        self.assertEqual(pay['gas_reserve'], config.GAS_RESERVE_SOL)
        self.assertEqual(pay['held'], [{'symbol': 'USDC', 'amount': 0.3, 'usd': 0.3}])

    def test_a_paid_harvest_holds_nothing(self):
        *_, seen = self.run_it(0.002, 0.3)
        pay = [kw for e, kw in seen if e == 'PAYOUT'][0]
        self.assertFalse(pay['gas_low']); self.assertEqual(pay['held'], [])

    def test_paid_row_records_amount_dollars_destination_and_signature(self):
        _, rows, calls, *_ = self.run_it(0.002, 0.3)
        paid = [r for r in rows if r['kind'] == 'paid'][0]
        self.assertEqual((paid['amount'], paid['usd'], paid['to_address'], paid['signature']), (0.3, 0.3, PROFIT, 'sig'))
        self.assertEqual(calls, [('send', USDC, '0.300000000', PROFIT, '--execute')])

    def test_paid_owed_and_uncertain_rows_are_valued_at_the_quote_price(self):
        bal = {'balanceA': 0.07, 'balanceB': 40.0, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 2.0}
        for result, kind in ((({'signature': 's'}, None), 'paid'), ((None, 'insufficient funds'), 'owed'),
                             ((None, 'timed out'), 'uncertain')):
            _, rows, *_, seen = self.run_it(0.002, 0.3, bal=bal, chain_result=result)
            row = [r for r in rows if r['kind'] == kind][0]
            self.assertAlmostEqual(row['usd'], 0.6, msg=kind)
        pay = [kw for e, kw in seen if e == 'PAYOUT'][0]
        _, _, *_, seen = self.run_it(0.002, 0.3, bal=bal)
        pay = [kw for e, kw in seen if e == 'PAYOUT'][0]
        self.assertEqual(pay['sent'][0]['usd'], 0.6)

    def test_an_answer_without_a_signature_is_owed_not_paid(self):
        _, rows, _, _, state, _ = self.run_it(0.002, 0.3, chain_result=({'note': 'nothing sent'}, None))
        self.assertIn('owed', [r['kind'] for r in rows]); self.assertAlmostEqual(state['payout_owed'][USDC], 0.3)

    def test_a_signature_with_any_error_is_uncertain(self):
        _, rows, _, _, state, _ = self.run_it(0.002, 0.3, chain_result=({'signature': 'x'}, 'blockhash expired'))
        self.assertIn('uncertain', [r['kind'] for r in rows]); self.assertEqual(state['payout_owed'], {})

    def test_the_summary_counts_a_part_without_dollars_as_zero(self):
        with mock.patch.object(fees, 'split', lambda *a: [
                {'mint': USDC, 'symbol': 'USDC', 'amount': 0.3, 'usd': None, 'kind': 'reinvested'}]):
            *_, seen = self.run_it(0.002, 0.3)
        self.assertEqual([kw for e, kw in seen if e == 'PAYOUT'][0]['split']['reinvested'], 0)

    def test_owed_is_added_and_cleared(self):
        _, rows, calls, _, state, _ = self.run_it(0.002, 0.3, state={'payout_owed': {USDC: 0.086267}})
        self.assertEqual(calls[0][2], f'{0.386267:.9f}')
        self.assertEqual(state['payout_owed'], {})
        self.assertAlmostEqual([r['amount'] for r in rows if r['kind'] == 'paid'][0], 0.386267, places=12)

    def test_a_wallet_short_of_the_due_pays_what_it_holds_and_owes_the_rest(self):
        bal = {'balanceA': 0.07, 'balanceB': 0.35, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 1.0}
        _, rows, calls, _, state, _ = self.run_it(0.002, 0.3, bal=bal, state={'payout_owed': {USDC: 0.2}})
        self.assertEqual(calls[0][2], '0.350000000')
        self.assertAlmostEqual(state['payout_owed'][USDC], 0.15)
        paid = [r for r in rows if r['kind'] == 'paid'][0]
        self.assertAlmostEqual(paid['usd'], 0.35)

    def test_a_remainder_under_a_nano_is_not_owed(self):
        bal = {'balanceA': 0.07, 'balanceB': 0.3, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 1.0}
        for owed, kept in ((7e-10, False), (1.2e-9, True)):
            _, _, _, _, state, _ = self.run_it(0.002, 0.3, bal=bal, state={'payout_owed': {USDC: owed}})
            self.assertEqual(USDC in state['payout_owed'], kept, owed)

    def test_nothing_to_send_sends_nothing(self):
        bal = {'balanceA': 0.07, 'balanceB': 0.0, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 1.0}
        # a paid part while the wallet holds none of it: the guard stops it first
        self.assertEqual(self.run_it(0.002, 0.3, bal=bal)[2], [])

    def test_held_none_sends_nothing(self):
        bal = {'balanceA': 0.07, 'balanceB': 40.0, 'sol': 0.07, 'price': 120.0, 'quoteUsd': 1.0}
        with mock.patch.object(fees, 'split', lambda *a: [
                {'mint': USDC, 'symbol': 'USDC', 'amount': 0.3, 'usd': 0.3, 'kind': 'paid'},
                {'mint': BONK, 'symbol': 'BONK', 'amount': 1.0, 'usd': 1.0, 'kind': 'paid'}]):
            _, rows, calls, *_ = self.run_it(0.002, 0.3, bal=bal)
        self.assertEqual([c[1] for c in calls], [USDC])          # BONK is not in the wallet map

    def test_a_part_without_dollars_is_paid_without_dollars(self):
        with mock.patch.object(fees, 'split', lambda *a: [
                {'mint': USDC, 'symbol': 'USDC', 'amount': 0.3, 'usd': None, 'kind': 'paid'}]):
            _, rows, *_ = self.run_it(0.002, 0.3)
        self.assertEqual([(r['kind'], r['usd']) for r in rows], [('paid', None)])

    def test_a_wrong_pin_pays_nothing_and_owes_it(self):
        _, rows, calls, ev, state, _ = self.run_it(0.002, 0.3, pin='CHANGEME')
        self.assertEqual(calls, [])
        self.assertIn(('USDC', 'owed'), [(r['symbol'], r['kind']) for r in rows])
        self.assertEqual(state['payout_owed'], {USDC: 0.3})
        self.assertIn('payout_refused', ev)
        _, _, calls, *_ = self.run_it(0.002, 0.3, pin='')
        self.assertEqual(calls, [])

    def test_a_signature_with_an_error_is_uncertain_and_not_owed(self):
        _, rows, _, ev, state, _ = self.run_it(0.002, 0.3, chain_result=({'signature': 'maybe'}, 'confirm failed'),
                                              state={'payout_owed': {USDC: 0.1}})
        u = [r for r in rows if r['kind'] == 'uncertain'][0]
        self.assertEqual((u['signature'], u['amount'], round(u['usd'], 9), u['detail']), ('maybe', 0.4, 0.4, 'confirm failed'))
        self.assertEqual(state['payout_owed'], {}); self.assertIn('payout_uncertain', ev)

    def test_a_partial_send_is_uncertain(self):
        _, rows, _, _, state, _ = self.run_it(0.002, 0.3, chain_result=({'partial': True}, 'boom'))
        self.assertIn('uncertain', [r['kind'] for r in rows]); self.assertEqual(state['payout_owed'], {})

    def test_a_timeout_is_uncertain(self):
        for err in ('request timed out', 'Timeout', 'could not confirm'):
            _, rows, _, _, state, _ = self.run_it(0.002, 0.3, chain_result=(None, err))
            self.assertIn('uncertain', [r['kind'] for r in rows], err); self.assertEqual(state['payout_owed'], {})

    def test_a_clean_failure_is_owed(self):
        _, rows, _, ev, state, _ = self.run_it(0.002, 0.3, chain_result=(None, 'insufficient funds'),
                                              state={'payout_owed': {USDC: 0.1}})
        o = [r for r in rows if r['kind'] == 'owed'][0]
        self.assertEqual((o['amount'], round(o['usd'], 9), o['detail'], o['to_address']), (0.4, 0.4, 'insufficient funds', PROFIT))
        self.assertAlmostEqual(state['payout_owed'][USDC], 0.4); self.assertIn('payout_failed', ev)

    def test_no_signature_and_no_error_is_owed(self):
        _, rows, _, _, state, _ = self.run_it(0.002, 0.3, chain_result=({}, None))
        o = [r for r in rows if r['kind'] == 'owed'][0]
        self.assertEqual(o['detail'], 'no signature'); self.assertAlmostEqual(state['payout_owed'][USDC], 0.3)

    def test_a_signature_is_not_enough_when_there_is_an_error(self):
        _, rows, *_ = self.run_it(0.002, 0.3, chain_result=({'signature': 's'}, 'late error'))
        self.assertNotIn('paid', [r['kind'] for r in rows])

    def test_summary_totals_by_kind(self):
        bal = {'balanceA': 0.03, 'balanceB': 40.0, 'sol': 0.03, 'price': 120.0, 'quoteUsd': 1.0}
        *_, seen = self.run_it(0.03, 0.3, bal=bal)
        pay = [kw for e, kw in seen if e == 'PAYOUT'][0]
        # sol before = 0.0: 0.03 SOL all gas (needs 0.05); USDC reinvested while gas is low
        self.assertEqual(pay['split'], {'paid': 0, 'reinvested': 0.3, 'gas': 3.6})
        self.assertTrue(pay['gas_low'])


# --- txfees -----------------------------------------------------------------------

def tb(i, owner, mint, amount, dec=6):
    return {'accountIndex': i, 'owner': owner, 'mint': mint, 'uiTokenAmount': {'amount': str(amount), 'decimals': dec}}


class TxParse(unittest.TestCase):
    def tx(self, pre, post):
        return {'meta': {'err': None, 'preTokenBalances': pre, 'postTokenBalances': post}}

    def test_other_mints_of_the_pool_are_not_counted(self):
        t = self.tx([tb(0, POOL, USDC, 500), tb(1, POOL, BONK, 900, 5)], [tb(0, POOL, USDC, 200), tb(1, POOL, BONK, 0, 5)])
        self.assertEqual(txfees.parse(t, POOL, SOL, USDC), (0.0, 300 / 1e6))

    def test_other_holders_of_the_mint_are_not_counted(self):
        t = self.tx([tb(0, POOL, USDC, 500), tb(1, 'X', USDC, 900)], [tb(0, POOL, USDC, 200), tb(1, 'X', USDC, 0)])
        self.assertEqual(txfees.parse(t, POOL, SOL, USDC), (0.0, 300 / 1e6))

    def test_decimals_come_from_the_balances(self):
        t = self.tx([tb(0, POOL, SOL, 2_000_000_000, 9)], [tb(0, POOL, SOL, 1_000_000_000, 9)])
        self.assertEqual(txfees.parse(t, POOL, SOL, USDC), (1.0, 0.0))
        t = self.tx([], [tb(0, POOL, SOL, 0, 9)])
        self.assertEqual(txfees.parse(t, POOL, SOL, USDC), (0.0, 0.0))

    def test_a_vault_created_in_the_transaction_is_a_gain(self):
        t = self.tx([], [tb(0, POOL, USDC, 500)])
        self.assertEqual(txfees.parse(t, POOL, SOL, USDC), (0.0, -500 / 1e6))
        t = self.tx([tb(0, POOL, USDC, 500)], [])
        self.assertEqual(txfees.parse(t, POOL, SOL, USDC), (0.0, 500 / 1e6))

    def test_missing_balance_lists_read_as_empty(self):
        self.assertEqual(txfees.parse({'meta': {'err': None}}, POOL, SOL, USDC), (0.0, 0.0))

    def test_parse_refuses_what_is_not_a_transaction(self):
        with self.assertRaises(ValueError):
            txfees.parse(None, POOL, SOL, USDC)
        with self.assertRaises(ValueError):
            txfees.parse({'meta': None}, POOL, SOL, USDC)

    def test_a_negative_native_side_alone_is_refused(self):
        t = self.tx([tb(0, POOL, SOL, 0, 9), tb(1, POOL, USDC, 500)], [tb(0, POOL, SOL, 1000, 9), tb(1, POOL, USDC, 400)])
        self.assertIsNone(txfees.harvested('r', ['s'], POOL, SOL, USDC, fetcher=lambda r, s: t))

    def test_zero_outflow_is_a_harvest_of_nothing(self):
        t = self.tx([tb(0, POOL, USDC, 500)], [tb(0, POOL, USDC, 500)])
        self.assertEqual(txfees.harvested('r', ['s'], POOL, SOL, USDC, fetcher=lambda r, s: t), (0.0, 0.0))

    def test_a_none_signature_list_is_nothing(self):
        self.assertIsNone(txfees.harvested('r', None, POOL, SOL, USDC, fetcher=lambda r, s: {}))

    def test_one_unreadable_transaction_voids_the_sum(self):
        t = self.tx([tb(0, POOL, USDC, 500)], [tb(0, POOL, USDC, 400)])
        answers = iter([t, None])
        self.assertIsNone(txfees.harvested('r', ['a', 'b'], POOL, SOL, USDC, fetcher=lambda r, s: next(answers)))

    def test_the_default_fetcher_is_looked_up_at_call_time(self):
        t = self.tx([tb(0, POOL, USDC, 500)], [tb(0, POOL, USDC, 400)])
        with mock.patch.object(txfees, 'fetch', lambda r, s: t):
            self.assertEqual(txfees.harvested('r', ['a'], POOL, SOL, USDC), (0.0, 100 / 1e6))


class TxFetch(unittest.TestCase):
    def test_retries_five_times_by_default_and_sleeps_between_only(self):
        calls, sleeps, bodies = [], [], []
        def boom(req, timeout):
            calls.append(timeout); bodies.append(req.data); raise OSError('down')
        with mock.patch.object(txfees.urllib.request, 'urlopen', boom), \
                mock.patch.object(txfees.time, 'sleep', lambda s: sleeps.append(s)):
            self.assertIsNone(txfees.network_fetch('http://rpc.test', 'SIG'))
        self.assertEqual(calls, [20] * 5); self.assertEqual(sleeps, [2.0] * 4)
        import json as _j
        body = _j.loads(bodies[0])
        self.assertEqual(body['method'], 'getTransaction')
        self.assertEqual(body['params'][0], 'SIG')
        self.assertEqual(body['params'][1], {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                             'maxSupportedTransactionVersion': 0})

    def test_one_try_does_not_sleep(self):
        sleeps = []
        with mock.patch.object(txfees.urllib.request, 'urlopen', mock.Mock(side_effect=OSError('x'))), \
                mock.patch.object(txfees.time, 'sleep', lambda s: sleeps.append(s)):
            txfees.network_fetch('http://rpc.test', 'SIG', tries=1)
        self.assertEqual(sleeps, [])


# --- calm: chi-square, ARCH-LM, risk_metrics -----------------------------------------

def tape(r, level=100.0, spread=1.001):
    c = level * np.exp(np.concatenate([[0.0], np.cumsum(r)]))
    return (np.arange(len(c)) * 300, c, c * spread, c / spread, c, np.ones(len(c)))


def arch_reference(r, lags):
    """An independent ARCH-LM: explicit lag matrix, normal equations."""
    e = np.asarray(r, float) ** 2
    rows = [[1.0] + [e[t - j] for j in range(1, lags + 1)] for t in range(lags, len(e))]
    X, y = np.array(rows), e[lags:]
    beta = np.linalg.solve(X.T @ X, X.T @ y)
    resid = y - X @ beta
    r2 = 1 - (resid @ resid) / ((y - y.mean()) @ (y - y.mean()))
    return len(y) * r2


class RiskMath(unittest.TestCase):
    def test_chi2_below_zero_is_one(self):
        for k in (2, 4, 6):
            self.assertEqual(calm.chi2_sf_even(-3.0, k), 1.0)

    def test_arch_lm_matches_an_independent_implementation(self):
        rng = np.random.default_rng(11)
        for lags in (2, 4, 6):
            r = rng.standard_t(4, 300) * 0.002
            lm, p = calm.arch_lm(r, lags)
            self.assertAlmostEqual(lm, arch_reference(r, lags), places=6)
            self.assertEqual(p, calm.chi2_sf_even(lm, lags))
        r = rng.normal(0, 0.002, 288)
        self.assertEqual(calm.arch_lm(r), calm.arch_lm(r, calm.ARCH_LAGS))    # six lags by default

    def test_arch_lm_needs_five_rows_per_parameter(self):
        rng = np.random.default_rng(2)
        self.assertIsNone(calm.arch_lm(rng.normal(0, 1, 6 + 34), 6))    # n = 34 < 35
        self.assertIsNotNone(calm.arch_lm(rng.normal(0, 1, 6 + 35), 6))  # n = 35

    def test_rms_of_nothing_is_none(self):
        self.assertIsNone(calm._rms(np.array([])))
        self.assertAlmostEqual(calm._rms(np.array([3.0, -4.0])), math.sqrt(12.5))

    def test_tape_length_thresholds(self):
        r = np.full(400, 0.001); r[::2] = -0.001
        self.assertIsNone(calm.risk_metrics(tape(r[:11]))['rms_1h_pct'])        # 12 closes
        self.assertIsNotNone(calm.risk_metrics(tape(r[:12]))['rms_1h_pct'])     # 13 closes
        self.assertIsNone(calm.risk_metrics(tape(r[:71]))['rms_6h_pct'])
        self.assertIsNotNone(calm.risk_metrics(tape(r[:72]))['rms_6h_pct'])
        self.assertIsNone(calm.risk_metrics(tape(r[:287]))['rms_24h_pct'])
        self.assertIsNotNone(calm.risk_metrics(tape(r[:288]))['rms_24h_pct'])

    def test_windows_are_the_last_hour_six_hours_and_day(self):
        r = np.concatenate([np.full(300, 0.001), np.full(60, 0.002), np.full(12, 0.004)])
        r[::2] *= -1
        m = calm.risk_metrics(tape(r))
        self.assertAlmostEqual(m['rms_1h_pct'], 0.4, places=6)
        self.assertAlmostEqual(m['rms_6h_pct'], 100 * math.sqrt((60 * 0.002 ** 2 + 12 * 0.004 ** 2) / 72), places=4)
        d = r[-288:]
        self.assertAlmostEqual(m['rms_24h_pct'], 100 * math.sqrt(np.mean(d * d)), places=4)
        self.assertAlmostEqual(m['vol_ratio_1h_24h'], round(0.004 / math.sqrt(np.mean(d * d)), 4), places=4)

    def test_parkinson_uses_the_last_hour_of_ranges(self):
        c = np.full(300, 100.0)
        hi, lo = c * 1.001, c / 1.001
        hi[-12:] = c[-12:] * 1.01; lo[-12:] = c[-12:] / 1.01
        m = calm.risk_metrics((np.arange(300), c, hi, lo, c, np.ones(300)))
        self.assertAlmostEqual(m['park_1h_pct'], 100 * 2 * math.log(1.01) / math.sqrt(4 * math.log(2)), places=4)

    def test_clustering_and_kurtosis_match_numpy(self):
        rng = np.random.default_rng(5)
        r = rng.standard_t(3, 400) * 0.002
        m = calm.risk_metrics(tape(r))
        d = r[-288:]; e = d * d
        self.assertAlmostEqual(m['acf_r2_lag1_24h'], round(float(np.corrcoef(e[1:], e[:-1])[0, 1]), 4))
        z = (d - d.mean()) / d.std()
        self.assertAlmostEqual(m['kurtosis_24h'], round(float(np.mean(z ** 4) - 3), 4))
        s = calm.ewma_sigma(tape(r)[4])[-289:]
        self.assertAlmostEqual(m['vol_of_vol_24h'], round(float(np.std(np.diff(np.log(s)))), 5))
        lm, p = calm.arch_lm(d)
        self.assertEqual((m['arch_lm_24h'], m['arch_lm_p_24h']), (round(lm, 3), round(p, 6)))

    def test_a_flat_tape_gives_none_where_a_figure_is_undefined(self):
        m = calm.risk_metrics(tape(np.zeros(300)))
        self.assertEqual(m['rms_24h_pct'], 0.0)
        for k in ('vol_ratio_1h_24h', 'acf_r2_lag1_24h', 'arch_lm_24h', 'kurtosis_24h'):
            self.assertIsNone(m[k], k)

    def test_float_noise_is_not_variance(self):
        r = np.full(300, 0.001); r[::2] = -0.001                 # |r| constant, up to float noise
        self.assertIsNone(calm.arch_lm(r[-288:] * (1 + 1e-15 * np.arange(288))))
        self.assertIsNone(calm.risk_metrics(tape(np.full(300, 0.001)))['kurtosis_24h'])   # a constant drift

    def test_squares_constant_but_for_one_end_have_no_autocorrelation(self):
        for i in (0, -1):
            r = np.full(300, 0.001); r[::2] = -0.001
            r[-288:][i] = 0.003
            self.assertIsNone(calm.risk_metrics(tape(r))['acf_r2_lag1_24h'], i)
            r[-288:][i + 1 if i == 0 else i - 1] = 0.002
            self.assertIsNotNone(calm.risk_metrics(tape(r))['acf_r2_lag1_24h'], i)

    def test_mildly_varying_squares_have_an_autocorrelation(self):
        rng = np.random.default_rng(9)
        r = (0.001 + 0.0001 * rng.random(300)) * np.where(np.arange(300) % 2, 1, -1)
        self.assertIsNotNone(calm.risk_metrics(tape(r))['acf_r2_lag1_24h'])

    def test_constant_squares_have_no_autocorrelation_figure(self):
        r = np.full(300, 0.001); r[::2] = -0.001
        m = calm.risk_metrics(tape(r))
        self.assertIsNone(m['acf_r2_lag1_24h'])


class RiskRecord(unittest.TestCase):
    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate risk_profile')
    tearDown = setUp

    def test_row_without_liquidity_or_probs(self):
        row = db.risk_row({'mode': 'HOT', 'liquidity': None, 'probs': None}, {}, {'p_exit_6h': 0.4})
        self.assertIsNone(row['liquidity_factor']); self.assertIsNone(row['probs']); self.assertEqual(row['p_exit_6h'], 0.4)
        self.assertEqual(db.risk_row({'probs': []}, {}, {})['probs'], '[]')

    def test_raw_and_smoothed_inflow_are_both_stored(self):
        lq = {'factor': 1.064, 'inflow': 0.94, 'inflow_raw': 0.24, 'volume_x': 1.1}
        db.record_risk_profile('P', 'M', 1.0, {'mode': 'WARM', 'liquidity': lq}, {}, {})
        with db.cursor() as cur:
            cur.execute('select liquidity_factor, inflow, inflow_raw, volume_x from risk_profile')
            r = cur.fetchone()
        self.assertEqual((r['liquidity_factor'], r['inflow'], r['inflow_raw'], r['volume_x']), (1.064, 0.94, 0.24, 1.1))

    def test_row_from_nothing(self):
        row = db.risk_row(None, None, None)
        self.assertTrue(all(v is None for k, v in row.items() if k != 'stale')); self.assertFalse(row['stale'])

    def test_the_status_pool_wins_over_the_profile_pool(self):
        with mock.patch.object(lp.books, 'LAST_REGIME', {'risk': None}), \
                mock.patch.object(config, 'POOL', 'PROFILEPOOL'):
            lp.regime.record_risk({'price': 1.0, 'whirlpool': 'STATUSPOOL'}, {'mode': 'CALM'}, None)
        with db.cursor() as cur:
            cur.execute('select pool from risk_profile')
            self.assertEqual([r['pool'] for r in cur.fetchall()], ['STATUSPOOL'])

    def test_last_fees_of_an_unknown_mint_is_none(self):
        _fixtures.reset_ledger()
        self.assertIsNone(db.last_fees('nobody'))
        db.snapshot('MX', 100.0, True, '1', 0.001, 0.2, 0.3, 10.0, 100.0)
        got = db.last_fees('MX')
        self.assertEqual((got['accrued_a'], got['accrued_b'], got['accrued_usd']), (0.001, 0.2, 0.3))
        self.assertLess(got['hours'], 0.1)
        _fixtures.reset_ledger()

    def snap_at(self, minutes_ago, usd, mint='MX'):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into snapshots (ts, mint, accrued_a, accrued_b, accrued_usd) "
                        "values (now() - make_interval(mins => %s), %s, 0, %s, %s)", (minutes_ago, mint, usd, usd))

    def test_a_repeated_figure_ages_from_its_first_snapshot(self):
        # a rejected read writes the last good figure again: its age is that of
        # the first snapshot holding it, not of the rewrite
        _fixtures.reset_ledger()
        self.snap_at(10, 0.1); self.snap_at(8, 0.25); self.snap_at(6, 0.25); self.snap_at(2, 0.25)
        got = db.last_fees('MX')
        self.assertEqual(got['accrued_usd'], 0.25)
        self.assertAlmostEqual(got['hours'], 8 / 60, delta=0.01)
        _fixtures.reset_ledger()

    def test_a_changed_figure_ages_from_its_own_snapshot(self):
        _fixtures.reset_ledger()
        self.snap_at(10, 0.25); self.snap_at(8, 0.25); self.snap_at(2, 0.3)
        self.assertAlmostEqual(db.last_fees('MX')['hours'], 2 / 60, delta=0.01)
        _fixtures.reset_ledger()

    def test_a_long_level_counts_one_hour_at_most(self):
        _fixtures.reset_ledger()
        self.snap_at(300, 0.25); self.snap_at(2, 0.25)
        self.assertAlmostEqual(db.last_fees('MX')['hours'], db.FEE_LEVEL_MAX_HOURS, delta=0.01)
        self.snap_at(200, 0.25, mint='MY')
        self.assertAlmostEqual(db.last_fees('MY')['hours'], 200 / 60, delta=0.01)      # a real gap stays
        _fixtures.reset_ledger()

    def test_other_mints_do_not_count(self):
        _fixtures.reset_ledger()
        self.snap_at(30, 0.25, mint='OTHER'); self.snap_at(9, 0.1); self.snap_at(5, 0.25)
        self.assertAlmostEqual(db.last_fees('MX')['hours'], 5 / 60, delta=0.01)
        _fixtures.reset_ledger()

    def test_the_rejection_does_not_ratchet(self):
        # the read rejected at the first poll passes once the level is old enough
        _fixtures.reset_ledger()
        self.snap_at(40, 0.25); self.snap_at(20, 0.25); self.snap_at(1, 0.25)
        f = db.last_fees('MX')
        self.assertIsNone(guards.fee_read_problem(rd(1.6, 200.0), f['accrued_usd'], f['hours']))
        self.assertIsNotNone(guards.fee_read_problem(rd(1.6, 200.0), 0.25, 1 / 60))
        _fixtures.reset_ledger()

    def test_the_loop_falls_back_to_the_profile_pool(self):
        with mock.patch.object(lp.books, 'LAST_REGIME', {'risk': None}), \
                mock.patch.object(config, 'POOL', 'PROFILEPOOL'):
            lp.regime.record_risk({'price': 1.0, 'positionMint': 'M'}, {'mode': 'CALM'}, None)
            lp.regime.record_risk({'price': 1.0, 'positionMint': 'M', 'whirlpool': None}, {'mode': 'CALM'}, None)
        with db.cursor() as cur:
            cur.execute('select pool from risk_profile')
            self.assertEqual([r['pool'] for r in cur.fetchall()], ['PROFILEPOOL', 'PROFILEPOOL'])


class Isolation(unittest.TestCase):
    def test_tests_never_write_the_live_feed(self):
        live = _fixtures.ROOT / 'events.jsonl'
        self.assertNotEqual(lp.paths.FEED.resolve(), live.resolve())
        before = live.stat().st_size if live.exists() else 0
        lp.books.notify('test_isolation_probe', note='must not reach events.jsonl')
        after = live.stat().st_size if live.exists() else 0
        # the live bot may append meanwhile, but never our probe
        self.assertNotIn('test_isolation_probe', live.read_text()[before:] if live.exists() else '')
        self.assertIn('test_isolation_probe', lp.paths.FEED.read_text())


if __name__ == '__main__':
    unittest.main()
