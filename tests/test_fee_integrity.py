"""Fee integrity: the 2026-09-27 incident must not happen again.

A status read claimed 23.16 SOL + 3,403 USDC ($6,237) of fees on a $230
Raydium position. The harvest moved 0.000771773 SOL + 0.086267 USDC. The gas
split booked $2,830 as gas and $3,408 as reinvested. Four layers now stand
between a bad read and the book, and each has tests here:

  1. the signer reads atomically (tests/test_fee_snapshot.mjs);
  2. guards.fee_read_problem rejects an impossible read at every poll;
  3. a harvest is booked from its own transaction (txfees.py);
  4. distribute refuses fees the wallet does not hold.

Property tests (hypothesis) state the invariants; the replay tests drive the
real incident through the real code against the test database.
"""
import json
import math
import os
import pathlib
import unittest
from unittest import mock

import numpy as np
from hypothesis import given, settings, assume, HealthCheck, strategies as st

import _fixtures
import calm
import db
import fees
import guards
import rebalancer
import txfees

SOL = fees.NATIVE_MINT
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'
OWNER = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
MINT = 'AqSDWeZCnBmiQmBrfAbdzgBasZY7wW7r5pGepH6nF9U1'
SIG = '2ZKfDT4ZeK1mGsSpMRYaadJNp66XmwWipwGxBo5JF7WptqpzQziyMLh7ycdBhC1NETbvaeBSoEa6iLJq14ogxRPi'
FIXTURE = json.loads((pathlib.Path(__file__).parent / 'fixtures_harvest_20260927.json').read_text())
REAL_A, REAL_B = 0.000771773, 0.086267
PRICE = 122.41441098387526

BOGUS = {'positionMint': MINT, 'whirlpool': POOL, 'price': PRICE, 'quoteUsd': 1.0,
         'inRange': False, 'liquidity': '51565242328', 'lowerPrice': 122.371355, 'upperPrice': 125.456498,
         'feesAccruedA': 23.156854902, 'feesAccruedB': 3403.609163, 'feesAccrued_USD': 6237.000374,
         'positionUsd': 223.7917}

finite = st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False)
# tests/mutate.py lowers the example count (HYP_EXAMPLES) to run hundreds of mutants.
FAST = settings(max_examples=int(os.environ.get('HYP_EXAMPLES', 400)), deadline=None,
                suppress_health_check=[HealthCheck.too_slow])


# --- layer 4's pure core: the split ---------------------------------------------

class SplitProperties(unittest.TestCase):
    @FAST
    @given(fa=finite, fb=finite, sol_before=st.one_of(st.none(), st.floats(-1, 10, allow_nan=False)),
           reserve=st.floats(0, 1, allow_nan=False), payout=st.sampled_from([USDC, SOL, None]))
    def test_split_conserves_every_amount_and_obeys_the_gas_rule(self, fa, fb, sol_before, reserve, payout):
        parts = fees.split([(SOL, 'SOL', fa, 120.0), (USDC, 'USDC', fb, 1.0)], payout, sol_before, reserve)
        for mint, amt in ((SOL, fa), (USDC, fb)):
            got = sum(p['amount'] for p in parts if p['mint'] == mint)
            self.assertAlmostEqual(got, amt, delta=1e-9 * max(1.0, amt))
        self.assertTrue(all(p['amount'] > 0 for p in parts))
        self.assertTrue(all(p['kind'] in ('paid', 'reinvested', 'gas') for p in parts))
        gas = sum(p['amount'] for p in parts if p['kind'] == 'gas')
        gas_low = sol_before is not None and sol_before < reserve
        need = max(reserve - (sol_before or 0.0), 0.0) if gas_low else 0.0
        self.assertAlmostEqual(gas, min(fa, need), delta=1e-9 * max(1.0, fa))
        self.assertTrue(all(p['mint'] == SOL for p in parts if p['kind'] == 'gas'))
        if gas_low:
            self.assertFalse(any(p['kind'] == 'paid' for p in parts))
        else:
            self.assertEqual(gas, 0.0)
        for p in parts:
            if p['kind'] == 'paid':
                self.assertEqual(p['mint'], payout)

    def test_the_incident_split_was_garbage_in_garbage_out(self):
        # The split is pure: given the bad read it books the bad numbers. The
        # layers above it are what must stop the read (see Replay below).
        parts = fees.split([(SOL, 'SOL', 23.156854902, PRICE), (USDC, 'USDC', 3403.609163, 1.0)],
                           USDC, 0.0893 - 23.156854902, 0.05)
        self.assertIn('gas', {p['kind'] for p in parts})


# --- layer 2: the plausibility guard --------------------------------------------

class FeeReadGuard(unittest.TestCase):
    def rd(self, usd, pos=200.0, a=0.0, b=0.0):
        return {'feesAccruedA': a, 'feesAccruedB': b, 'feesAccrued_USD': usd, 'positionUsd': pos}

    def test_the_incident_is_rejected(self):
        self.assertIsNotNone(guards.fee_read_problem(BOGUS))
        self.assertIsNotNone(guards.fee_read_problem(BOGUS, 0.165, 0.035))

    def test_real_reads_pass(self):
        # every snapshot of the incident position before the bad read
        prev = None
        for usd in (0.146315, 0.150642, 0.158768, 0.165123):
            self.assertIsNone(guards.fee_read_problem(self.rd(usd, 224.9), prev, 0.035))
            prev = usd
        self.assertIsNone(guards.fee_read_problem(self.rd(0.0), None, None))

    def test_each_bound_and_its_edge(self):
        self.assertIsNone(guards.fee_read_problem(self.rd(20.0, 200.0)))          # exactly 10%
        self.assertIsNotNone(guards.fee_read_problem(self.rd(20.0001, 200.0)))
        # rise: 1% an hour + 0.2% slack, of the position
        allowed = 200.0 * (0.01 * 2.0 + 0.002)
        self.assertIsNone(guards.fee_read_problem(self.rd(1.0 + allowed, 200.0), 1.0, 2.0))
        self.assertIsNotNone(guards.fee_read_problem(self.rd(1.0 + allowed + 1e-6, 200.0), 1.0, 2.0))

    def test_malformed_figures_are_rejected(self):
        for bad in (-1e-9, float('nan'), float('inf'), True, '1', [1]):
            for k in ('feesAccruedA', 'feesAccruedB', 'feesAccrued_USD'):
                s = self.rd(0.1); s[k] = bad
                self.assertIsNotNone(guards.fee_read_problem(s), f'{k}={bad!r}')

    def test_no_position_value_means_only_the_sign_checks(self):
        for pos in (None, 0.0, -5.0, float('nan')):
            self.assertIsNone(guards.fee_read_problem(self.rd(1e9, pos)))

    @FAST
    @given(usd=finite, pos=st.floats(1e-3, 1e6), prev=st.one_of(st.none(), finite),
           hours=st.one_of(st.none(), st.floats(0, 1000)), k=st.floats(1e-3, 1e3))
    def test_the_verdict_does_not_depend_on_the_unit_of_money(self, usd, pos, prev, hours, k):
        a = guards.fee_read_problem(self.rd(usd, pos), prev, hours) is None
        b = guards.fee_read_problem(self.rd(usd * k, pos * k), None if prev is None else prev * k, hours) is None
        # scaling can move a value across a bound only by rounding
        if a != b:
            lim = min(0.10 * pos, float('inf') if prev is None or hours is None
                      else prev + pos * (0.01 * hours + 0.002))
            self.assertLess(abs(usd - lim), 1e-6 * max(1.0, lim))

    @FAST
    @given(usd=finite, more=st.floats(0, 1e6), pos=st.floats(1e-3, 1e6), prev=st.one_of(st.none(), finite),
           hours=st.one_of(st.none(), st.floats(0, 1000)))
    def test_rejection_is_monotone_in_the_fee(self, usd, more, pos, prev, hours):
        if guards.fee_read_problem(self.rd(usd, pos), prev, hours) is not None:
            self.assertIsNotNone(guards.fee_read_problem(self.rd(usd + more, pos), prev, hours))

    @FAST
    @given(pos=st.floats(1e-3, 1e6), prev=finite, drop=st.floats(0, 1), hours=st.floats(0, 1000))
    def test_a_fall_is_always_possible(self, pos, prev, drop, hours):
        usd = min(prev * drop, 0.10 * pos)
        self.assertIsNone(guards.fee_read_problem(self.rd(usd, pos), prev, hours))


class ReadStatus(unittest.TestCase):
    """read_status: a bad read is read again, then replaced by the last snapshot."""

    def run_it(self, answers, last=None):
        seen, events = [], []
        it = iter(answers)
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (next(it), None)), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None), \
                mock.patch.object(rebalancer.db, 'last_fees', lambda m: last), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
            out, _ = rebalancer.read_status()
        return out, seen, events

    def test_good_read_passes_untouched(self):
        good = dict(BOGUS, feesAccruedA=0.0007, feesAccruedB=0.08, feesAccrued_USD=0.165)
        out, seen, _ = self.run_it([good])
        self.assertEqual(out, good); self.assertEqual(seen, [])

    def test_bad_then_good_takes_the_good_one(self):
        good = dict(BOGUS, feesAccruedA=0.0007, feesAccruedB=0.08, feesAccrued_USD=0.165)
        out, seen, _ = self.run_it([BOGUS, good])
        self.assertEqual(out, good); self.assertEqual(seen, [])

    def test_bad_twice_gives_the_last_snapshot_and_says_so(self):
        last = {'accrued_a': 0.000695184, 'accrued_b': 0.079703, 'accrued_usd': 0.165123, 'hours': 0.03}
        out, seen, events = self.run_it([BOGUS, BOGUS], last)
        self.assertEqual((out['feesAccruedA'], out['feesAccruedB'], out['feesAccrued_USD']),
                         (0.000695184, 0.079703, 0.165123))
        self.assertIn('fee_read_rejected', seen)
        self.assertEqual(events[0][0], 'fee_read_rejected')
        self.assertEqual(out['feesRejected']['feesAccrued_USD'], 6237.000374)
        self.assertTrue(out['feesSuspect'])
        self.assertEqual(out['positionMint'], MINT)         # the rest of the read stands

    def test_no_snapshot_gives_zero_not_the_bad_figure(self):
        out, _, _ = self.run_it([BOGUS, BOGUS], None)
        self.assertEqual(out['feesAccrued_USD'], 0.0)

    def test_second_read_of_another_position_is_not_taken(self):
        other = dict(BOGUS, positionMint='X', feesAccrued_USD=0.1, feesAccruedA=0.0, feesAccruedB=0.1)
        out, seen, _ = self.run_it([BOGUS, other], None)
        self.assertEqual(out['positionMint'], MINT); self.assertIn('fee_read_rejected', seen)

    def test_no_position_is_not_checked(self):
        out, seen, _ = self.run_it([{'positions': 0, 'positionMint': None}])
        self.assertIsNone(out['positionMint']); self.assertEqual(seen, [])


# --- layer 3: the harvest transaction ---------------------------------------------

def tbal(i, owner, mint, amount, dec=6):
    return {'accountIndex': i, 'owner': owner, 'mint': mint,
            'uiTokenAmount': {'amount': str(amount), 'decimals': dec}}


def tx_from(pre, post, err=None):
    return {'meta': {'err': err, 'preTokenBalances': pre, 'postTokenBalances': post,
                     'preBalances': [], 'postBalances': [], 'fee': 5000},
            'transaction': {'message': {'accountKeys': []}}}


class TxFees(unittest.TestCase):
    def test_the_incident_transaction(self):
        self.assertEqual(txfees.parse(FIXTURE, POOL, SOL, USDC), (REAL_A, REAL_B))
        self.assertEqual(txfees.harvested('rpc', [SIG], POOL, SOL, USDC, fetcher=lambda r, s: FIXTURE),
                         (REAL_A, REAL_B))

    def test_measured_at_the_pool_not_the_wallet(self):
        # the wallet also paid the network fee: its SOL rose less than the fee
        a, _ = txfees.parse(FIXTURE, OWNER, SOL, USDC)
        self.assertNotEqual(a, REAL_A)

    @FAST
    @given(st.lists(st.tuples(st.sampled_from(['pool', 'other']), st.sampled_from([SOL, USDC, 'X']),
                              st.integers(0, 10**15), st.integers(0, 10**15)), max_size=8),
           st.randoms())
    def test_outflow_is_the_pools_net_decrease_in_any_order(self, rows, rnd):
        owners = {'pool': POOL, 'other': OWNER}
        pre = [tbal(i, owners[o], m, a, 9 if m == SOL else 6) for i, (o, m, a, _b) in enumerate(rows)]
        post = [tbal(i, owners[o], m, b, 9 if m == SOL else 6) for i, (o, m, _a, b) in enumerate(rows)]
        exp_sol = sum(a - b for o, m, a, b in rows if o == 'pool' and m == SOL) / 1e9
        exp_usdc = sum(a - b for o, m, a, b in rows if o == 'pool' and m == USDC) / 1e6
        rnd.shuffle(pre); rnd.shuffle(post)
        got = txfees.parse(tx_from(pre, post), POOL, SOL, USDC)
        self.assertAlmostEqual(got[0], exp_sol, places=9); self.assertAlmostEqual(got[1], exp_usdc, places=6)

    def test_a_failed_transaction_moved_nothing(self):
        t = tx_from([tbal(0, POOL, USDC, 100)], [tbal(0, POOL, USDC, 0)], err={'InstructionError': [0, 'x']})
        self.assertEqual(txfees.parse(t, POOL, SOL, USDC), (0.0, 0.0))

    def test_an_account_opened_or_closed_in_the_transaction_counts(self):
        t = tx_from([tbal(0, POOL, USDC, 500)], [tbal(1, POOL, USDC, 200)])
        self.assertAlmostEqual(txfees.parse(t, POOL, SOL, USDC)[1], 300 / 1e6)

    def test_unreadable_or_wrong_way_gives_none(self):
        self.assertIsNone(txfees.harvested('rpc', [SIG], POOL, SOL, USDC, fetcher=lambda r, s: None))
        self.assertIsNone(txfees.harvested('rpc', [], POOL, SOL, USDC, fetcher=lambda r, s: FIXTURE))
        self.assertIsNone(txfees.harvested('rpc', [None], POOL, SOL, USDC, fetcher=lambda r, s: FIXTURE))
        self.assertIsNone(txfees.harvested('rpc', [SIG], None, SOL, USDC, fetcher=lambda r, s: FIXTURE))
        deposit = tx_from([tbal(0, POOL, USDC, 0)], [tbal(0, POOL, USDC, 100)])
        self.assertIsNone(txfees.harvested('rpc', ['s'], POOL, SOL, USDC, fetcher=lambda r, s: deposit))
        self.assertIsNone(txfees.harvested('rpc', ['s'], POOL, SOL, USDC, fetcher=lambda r, s: {'meta': None}))
        broken = {'meta': {'err': None, 'preTokenBalances': [{'owner': POOL, 'mint': USDC}]}}
        self.assertIsNone(txfees.harvested('rpc', ['s'], POOL, SOL, USDC, fetcher=lambda r, s: broken))

    def test_every_signature_of_a_harvest_is_summed(self):
        self.assertEqual(txfees.harvested('rpc', [SIG, SIG], POOL, SOL, USDC, fetcher=lambda r, s: FIXTURE),
                         (2 * REAL_A, 2 * REAL_B))
        calls = []
        txfees.harvested('rpc', ['a', 'b', 'c'], POOL, SOL, USDC,
                         fetcher=lambda r, s: calls.append(s) or FIXTURE)
        self.assertEqual(calls, ['a', 'b', 'c'])

    def test_fetch_retries_then_gives_up(self):
        calls = []
        def boom(*a, **k):
            calls.append(1); raise OSError('down')
        with mock.patch.object(txfees.urllib.request, 'urlopen', boom), \
                mock.patch.object(txfees.time, 'sleep', lambda s: None):
            self.assertIsNone(txfees.network_fetch('http://rpc.test', SIG, tries=3))
        self.assertEqual(len(calls), 3)

    def test_fetch_returns_the_first_result(self):
        class R:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, *a): return self.body
        answers = iter([b'{"result": null}', json.dumps({'result': FIXTURE}).encode()])
        with mock.patch.object(txfees.urllib.request, 'urlopen', lambda req, timeout: R(next(answers))), \
                mock.patch.object(txfees.time, 'sleep', lambda s: None):
            self.assertEqual(txfees.network_fetch('http://rpc.test', SIG, tries=3), FIXTURE)


# --- layer 4: distribute, against random wallets ------------------------------------

class DistributeProperties(unittest.TestCase):
    def run_it(self, fa, fb, sol, usdc):
        rows, calls = [], []
        bal = {'balanceA': sol, 'balanceB': usdc, 'sol': sol, 'price': 120.0, 'quoteUsd': 1.0}
        with mock.patch.dict(os.environ, {'LPBOT_PROFIT_WALLET_PIN': PROFIT}), \
                mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(rebalancer.config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: bal), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or ({'signature': 's'}, None))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None), \
                mock.patch.object(rebalancer.db, 'record_payout',
                                  lambda *a, **k: rows.append({'mint': a[2], 'amount': a[4], 'kind': a[6]})):
            rebalancer.distribute({}, 'M', fa, fb)
        return rows, calls

    @FAST
    @given(fa=st.floats(0, 50), fb=st.floats(0, 5000), sol=st.floats(0, 50), usdc=st.floats(0, 5000))
    def test_nothing_is_booked_beyond_the_wallet_and_every_fee_is_booked_once(self, fa, fb, sol, usdc):
        assume(fa > 0 or fb > 0)
        rows, calls = self.run_it(fa, fb, sol, usdc)
        impossible = fa > sol + 1e-9 or fb > usdc + 1e-9 or sol - fa < 0
        if impossible:
            self.assertEqual((rows, calls), ([], []))
            return
        for mint, amt in ((SOL, fa), (USDC, fb)):
            booked = sum(r['amount'] for r in rows if r['mint'] == mint)
            self.assertAlmostEqual(booked, amt, delta=1e-9 * max(1, amt))
        gas = sum(r['amount'] for r in rows if r['kind'] == 'gas')
        self.assertLessEqual(gas, max(0.05 - (sol - fa), 0.0) + 1e-12)
        if sol - fa >= 0.05:
            self.assertEqual(gas, 0.0)
        for r in rows:
            self.assertLessEqual(r['amount'], (sol if r['mint'] == SOL else usdc) + 1e-9)

    def test_the_incident_books_nothing(self):
        rows, calls = self.run_it(23.156854902, 3403.609163, 0.0893, 15.07)
        self.assertEqual((rows, calls), ([], []))


# --- the replay, end to end, on the test database -------------------------------------

class Replay(unittest.TestCase):
    """The incident through dividend() and rebalance(), with the real ledger."""

    def setUp(self):
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate payouts')
        db.snapshot(MINT, PRICE, True, '1', 0.000695184, 0.079703, 0.165123, 21.96, 223.79)
        self.notes, self.sends = [], []

    def tearDown(self):
        # leave nothing behind: the ledger tests count paid dividends in P&L
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate payouts')

    def patches(self, fetch=lambda rpc, s, **k: FIXTURE):
        bal = {'balanceA': 0.0893, 'balanceB': 15.07, 'sol': 0.0893, 'price': PRICE, 'quoteUsd': 1.0,
               'walletUsd': 22.14, 'owner': OWNER}
        def chain(cmd, *a, **k):
            if cmd == 'harvest':
                return {'harvested': MINT, 'signature': SIG}, None
            if cmd == 'send':
                self.sends.append(a); return {'signature': 'PAYSIG'}, None
            if cmd == 'close':
                return None, 'stop here'
            if cmd == 'status':
                return dict(BOGUS), None
            return None, f'unexpected {cmd}'
        return [mock.patch.dict(os.environ, {'LPBOT_PROFIT_WALLET_PIN': PROFIT}),
                mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True),
                mock.patch.object(rebalancer.config, 'PAYOUT_MINT', USDC),
                mock.patch.object(rebalancer.config, 'PROFIT_WALLET', PROFIT),
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05),
                mock.patch.object(rebalancer.config, 'REWARD_POLICY', 'hold'),
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))),
                mock.patch.object(rebalancer, 'wallet', lambda p: bal),
                mock.patch.object(rebalancer, 'chain', chain),
                mock.patch.object(rebalancer, 'save', lambda s: None),
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None),
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: self.notes.append((ev, kw))),
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: self.notes.append((ev, kw))),
                mock.patch.object(txfees, 'fetch', fetch)]

    def run_with(self, fn, **kw):
        ps = self.patches(**kw)
        for p in ps:
            p.start()
        try:
            return fn()
        finally:
            for p in reversed(ps):
                p.stop()

    def book(self):
        with db.cursor() as cur:
            cur.execute('select fee_a, fee_b, fee_usd from harvests')
            h = [tuple(float(x) for x in r.values()) for r in cur.fetchall()]
            cur.execute('select symbol, amount, kind from payouts order by id')
            p = [(r['symbol'], float(r['amount']), r['kind']) for r in cur.fetchall()]
        return h, p

    def assert_true_book(self):
        h, p = self.book()
        self.assertEqual(len(h), 1)
        self.assertAlmostEqual(h[0][0], REAL_A, places=12)
        self.assertAlmostEqual(h[0][1], REAL_B, places=12)
        self.assertAlmostEqual(h[0][2], REAL_A * PRICE + REAL_B, places=6)     # usd is numeric(_, 6)
        self.assertNotIn('gas', [k for _, _, k in p])
        self.assertIn(('USDC', REAL_B, 'paid'), p)
        self.assertIn(('SOL', REAL_A, 'reinvested'), p)
        self.assertEqual(self.sends, [(USDC, f'{REAL_B:.9f}', PROFIT, '--execute')])
        t = db.payout_totals(None)
        self.assertEqual(t['gas_usd'], 0.0)
        self.assertLess(t['reinvested_usd'] + t['paid_usd'], 1.0)

    def test_dividend_books_the_transaction_not_the_read(self):
        self.run_with(lambda: rebalancer.dividend({}, dict(BOGUS)))
        self.assert_true_book()
        self.assertIn('harvest_measured', [e for e, _ in self.notes])

    def test_rebalance_harvest_books_the_transaction_not_the_read(self):
        state = dict(rebalancer.STATE_DEFAULTS)
        self.run_with(lambda: rebalancer.rebalance(state, dict(BOGUS), 'price went below', band=1.015,
                                                   calm_move=True, exit_move=True))
        self.assert_true_book()

    def test_main_loop_read_never_lets_the_bogus_figure_through(self):
        out, _ = self.run_with(rebalancer.read_status)
        self.assertEqual(out['feesAccrued_USD'], 0.165123)
        with db.cursor() as cur:
            cur.execute("select count(*) n from events where kind = 'fee_read_rejected'")
            self.assertEqual(cur.fetchone()['n'], 1)

    def test_unreadable_transaction_and_bogus_read_books_the_last_snapshot(self):
        self.run_with(lambda: rebalancer.dividend({}, dict(BOGUS)), fetch=lambda rpc, s, **k: None)
        h, p = self.book()
        self.assertEqual([round(x, 9) for x in h[0]], [0.000695184, 0.079703, 0.165123])
        self.assertNotIn('gas', [k for _, _, k in p])
        self.assertLess(db.payout_totals(None)['reinvested_usd'], 1.0)

    def test_unreadable_transaction_and_sane_read_books_the_read(self):
        sane = dict(BOGUS, feesAccruedA=0.0007, feesAccruedB=0.08, feesAccrued_USD=0.1657)
        self.run_with(lambda: rebalancer.dividend({}, sane), fetch=lambda rpc, s, **k: None)
        h, _ = self.book()
        self.assertEqual([round(x, 6) for x in h[0]], [0.0007, 0.08, 0.1657])
        self.assertIn('harvest_unmeasured', [e for e, _ in self.notes])


# --- the risk profile ----------------------------------------------------------------

def tape(r, level=100.0, spread=1.001):
    c = level * np.exp(np.concatenate([[0.0], np.cumsum(r)]))
    return (np.arange(len(c)) * 300, c, c * spread, c / spread, c, np.ones(len(c)))


def garch(n, seed, a=0.25, b=0.7, w=1e-7):
    rng = np.random.default_rng(seed)
    r, h = np.zeros(n), w / (1 - a - b)
    for i in range(n):
        r[i] = math.sqrt(h) * rng.standard_normal()
        h = w + a * r[i] ** 2 + b * h
    return r


class RiskMetrics(unittest.TestCase):
    def test_chi2_matches_scipy(self):
        from scipy import stats
        for k in (2, 4, 6, 8, 12):
            for x in (0.0, 0.1, 1.0, 5.0, 12.5916, 30.0, 200.0):
                self.assertAlmostEqual(calm.chi2_sf_even(x, k), float(stats.chi2.sf(x, k)), places=9)
        for bad in (0, 3, -2):
            with self.assertRaises(ValueError):
                calm.chi2_sf_even(1.0, bad)

    @FAST
    @given(x=st.floats(0, 500), k=st.sampled_from([2, 4, 6, 8, 10]))
    def test_chi2_is_a_survival_function(self, x, k):
        p = calm.chi2_sf_even(x, k)
        self.assertTrue(0.0 <= p <= 1.0)
        self.assertLessEqual(calm.chi2_sf_even(x + 1.0, k), p + 1e-15)

    def test_arch_lm_finds_clustering_and_not_where_there_is_none(self):
        found = sum(calm.arch_lm(garch(288, s))[1] < 0.05 for s in range(40))
        self.assertGreaterEqual(found, 28)                        # power
        rng = np.random.default_rng(7)
        false = sum(calm.arch_lm(rng.normal(0, 0.002, 288))[1] < 0.05 for _ in range(200))
        self.assertLessEqual(false, 22)                           # size near 5%
        self.assertIsNone(calm.arch_lm(np.zeros(288)))
        self.assertIsNone(calm.arch_lm(np.ones(20)))

    def test_known_values(self):
        r = np.full(300, 0.001); r[::2] = -0.001                  # |r| constant
        m = calm.risk_metrics(tape(r))
        self.assertAlmostEqual(m['rms_1h_pct'], 0.1, places=6)
        self.assertAlmostEqual(m['rms_24h_pct'], 0.1, places=6)
        self.assertAlmostEqual(m['vol_ratio_1h_24h'], 1.0, places=6)
        self.assertAlmostEqual(m['kurtosis_24h'], -2.0, places=3)  # two-point distribution
        self.assertAlmostEqual(m['park_1h_pct'], 100 * 2 * math.log(1.001) / math.sqrt(4 * math.log(2)), places=4)
        self.assertEqual(m['n_bars'], 301)

    def test_short_tapes_give_none_not_garbage(self):
        self.assertTrue(all(v is None for v in calm.risk_metrics(None).values()))
        m = calm.risk_metrics(tape(np.full(20, 0.001)))
        self.assertIsNotNone(m['rms_1h_pct']); self.assertIsNone(m['rms_6h_pct']); self.assertIsNone(m['arch_lm_24h'])
        m = calm.risk_metrics(tape(np.full(5, 0.001)))
        self.assertIsNone(m['rms_1h_pct'])

    @settings(max_examples=60, deadline=None)
    @given(seed=st.integers(0, 10**6), level=st.floats(1e-3, 1e5))
    def test_metrics_do_not_depend_on_the_price_level(self, seed, level):
        r = garch(300, seed)
        a, b = calm.risk_metrics(tape(r, 100.0)), calm.risk_metrics(tape(r, level))
        for k, v in a.items():
            if v is None:
                self.assertIsNone(b[k])
            else:
                self.assertAlmostEqual(v, b[k], delta=1e-3 * max(1.0, abs(v)), msg=k)

    @settings(max_examples=60, deadline=None)
    @given(seed=st.integers(0, 10**6))
    def test_every_figure_is_finite_and_in_its_range(self, seed):
        m = calm.risk_metrics(tape(garch(400, seed)))
        for k, v in m.items():
            self.assertTrue(v is None or math.isfinite(v), k)
        self.assertGreaterEqual(m['rms_24h_pct'], 0); self.assertGreaterEqual(m['park_1h_pct'], 0)
        self.assertTrue(-1 <= m['acf_r2_lag1_24h'] <= 1)
        self.assertTrue(0 <= m['arch_lm_p_24h'] <= 1)
        self.assertGreaterEqual(m['arch_lm_24h'], 0)
        self.assertGreaterEqual(m['kurtosis_24h'], -2.0 - 1e-9)

    def test_a_vol_spike_shows_in_every_figure(self):
        rng = np.random.default_rng(3)
        calm_r = rng.normal(0, 0.001, 300)
        hot = calm_r.copy(); hot[-12:] *= 8
        a, b = calm.risk_metrics(tape(calm_r)), calm.risk_metrics(tape(hot))
        self.assertGreater(b['rms_1h_pct'], 4 * a['rms_1h_pct'])
        self.assertGreater(b['vol_ratio_1h_24h'], 2 * a['vol_ratio_1h_24h'])
        self.assertGreater(b['kurtosis_24h'], a['kurtosis_24h'])


class RiskProfileRecord(unittest.TestCase):
    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate risk_profile')

    tearDown = setUp

    def view(self):
        return {'mode': 'WARM', 'choice': 1.015, 'choice_pct': 1.5, 'held_pct': 1.25, 'inside': True,
                'p_held': 0.49, 'threshold': 0.25, 'threshold_base': 0.2, 'horizon_minutes': 120,
                'bar_age_s': 600, 'probs': [[1.0, 0.4], [1.5, 0.18]], 'sigma_5m_pct': 0.1548,
                'velocity': 0.353, 'instability': 0.0607,
                'liquidity': {'factor': 1.25, 'inflow': 0.79, 'volume_x': 9.2}}

    def test_every_column_is_written(self):
        m = calm.risk_metrics(tape(garch(400, 1)))
        fc = {'sigma_24h_pct': 0.609, 'vol_regime_x': 1.05, 'position': -0.53,
              'p_exit_6h': 0.54, 'p_exit_6h_regime': 0.535, 'p_exit_24h': 0.95, 'p_exit_72h': 1.0, 'p_exit_168h': 1.0}
        db.record_risk_profile(POOL, MINT, PRICE, self.view(), m, fc)
        with db.cursor() as cur:
            cur.execute('select * from risk_profile')
            rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r['pool'], r['mint'], r['mode']), (POOL, MINT, 'WARM'))
        self.assertEqual(r['p_exit_6h'], 0.535)                  # the regime figure wins
        self.assertEqual(r['horizon_min'], 120)
        self.assertEqual(r['liquidity_factor'], 1.25)
        self.assertEqual(r['probs'], [[1.0, 0.4], [1.5, 0.18]])
        for k in db.RISK_COLUMNS:
            if k not in ('stale',):
                self.assertIsNotNone(r[k], k)
        for k in m:
            self.assertAlmostEqual(r[k], m[k], places=9, msg=k)
        self.assertFalse(r['stale'])

    def test_missing_inputs_are_null_not_errors(self):
        db.record_risk_profile(POOL, None, PRICE, {'mode': 'STALE', 'stale': True}, None, None)
        with db.cursor() as cur:
            cur.execute('select * from risk_profile')
            r = cur.fetchone()
        self.assertTrue(r['stale']); self.assertIsNone(r['sigma_5m_pct']); self.assertIsNone(r['probs'])

    def test_old_rows_are_pruned(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into risk_profile (ts, pool, price) values (now() - interval '31 days', 'p', 1)")
        db.record_risk_profile(POOL, MINT, PRICE, self.view(), None, None)
        with db.cursor() as cur:
            cur.execute('select count(*) n from risk_profile')
            self.assertEqual(cur.fetchone()['n'], 1)

    def test_the_loop_records_each_poll_and_never_raises(self):
        seen = []
        with mock.patch.object(rebalancer, 'LAST_REGIME', {'risk': {'rms_1h_pct': 0.1}}):
            rebalancer.record_risk({'whirlpool': POOL, 'positionMint': MINT, 'price': PRICE}, self.view(), {})
            with mock.patch.object(rebalancer.db, 'record_risk_profile', side_effect=RuntimeError('db down')), \
                    mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
                rebalancer.record_risk({'whirlpool': POOL, 'positionMint': MINT, 'price': PRICE}, self.view(), {})
            rebalancer.record_risk({'whirlpool': POOL, 'price': PRICE}, None, {})     # no view: nothing
        self.assertEqual(seen, ['risk_record_failed'])
        with db.cursor() as cur:
            cur.execute('select rms_1h_pct from risk_profile')
            self.assertEqual([r['rms_1h_pct'] for r in cur.fetchall()], [0.1])


if __name__ == '__main__':
    unittest.main()
