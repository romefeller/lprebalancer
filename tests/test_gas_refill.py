"""The gas refill on EVM chains (sql/041, lp/capital.py refill_gas), and the
gas check before the swap of an open or an add.

2026-10-14 00:15-00:37Z, poly-wpol-usdt: POL fell under the 2 POL reserve
while the fees arrived as WPOL. Every ~4 minutes the loop swapped the idle
cash (approve + swap, ~0.05 POL) and then the signer refused the add under
the reserve: POL 1.96 -> 1.90 -> 1.85 -> 1.80 for four refused adds. After
the next close the wallet held $435 and opened nothing (gas_short).

Covered: refill_due, refill_kind and refill_target as pure rules, with
properties; refill_gas against a mocked signer and the real payouts table
(booked, said once, the retry wait, a held refusal, a partial send);
gas_ready and native_upkeep; the incident replayed on the fake Polygon chain
(the refill comes before the swap and the add, the same poll); with no
refill a short gas sends nothing at all, poll after poll; an open short of
gas refills first and opens the same poll; the DAILY line and since_start
count a refill as value; the migration; the config clamp to native_keep."""
import contextlib
import os
import pathlib
import subprocess
import sys
import time
import unittest
from unittest import mock

import psycopg2
from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import chains
import config
import db
import lp.books
import lp.capital
import lp.loop
import lp.paths
import lp.signers
import lp.swaps
import test_multi_loop as M
import test_polygon_loop as PL
from test_daily import DAY, T, payout, reset, snap

WPOL, USDT0, POL, POLY_POOL, PRICE = PL.WPOL, PL.USDT0, PL.POL, PL.POLY_POOL, PL.PRICE
ROOT = pathlib.Path(__file__).resolve().parent.parent
finite = st.floats(min_value=0, max_value=1e6, allow_nan=False)


class RefillDue(unittest.TestCase):
    def test_the_table(self):
        f = lp.capital.refill_due
        self.assertTrue(f(1.6466, 1.0, 3.0))                        # 2026-10-14 after the close: under 2
        self.assertTrue(f(1.9999, 1.0, 3.0))
        self.assertFalse(f(2.0, 1.0, 3.0))                          # the low-water mark itself: no
        self.assertFalse(f(2.5, 1.0, 3.0))
        self.assertTrue(f(2.5, 1.0, 3.0, need=2.6))                 # what the next write needs wins
        self.assertFalse(f(2.6, 1.0, 3.0, need=2.6))
        self.assertTrue(f(0.0, 1.0, 3.0))
        for bad in ((1.0, 1.0, 1.0), (0.5, 1.0, 0.9), (None, 1, 3), ('x', 1, 3), (float('nan'), 1, 3),
                    (1, float('inf'), 3), (1, 1, float('inf')), (1, 1, 3, float('nan'))):
            self.assertIs(f(*bad), False, bad)
        self.assertIs(f(1.0, 1.0, 3.0, None), True)                 # no need given: the mark alone
        self.assertIs(f(0.6, 0.5, 0.6, None), False)                # over the mark of 0.55, no need
        self.assertIs(f(0.9, 0.0, 1.5), False)                      # the default need is none: 0.9 over 0.75

    @settings(max_examples=3000, deadline=None)
    @given(finite, finite, finite, finite)
    def test_property_only_under_the_mark_and_only_with_a_target_above_the_reserve(self, native, reserve, target, need):
        due = lp.capital.refill_due(native, reserve, target, need)
        if target <= reserve:
            self.assertFalse(due)
        else:
            self.assertEqual(due, native < max((reserve + target) / 2, need))
            if native < reserve:
                self.assertTrue(due)                               # under the reserve: always
            if native >= target and need <= target:
                self.assertFalse(due)                              # at the target: never


class RefillKind(unittest.TestCase):
    def test_native_in_the_book_is_gas_outside_it_a_refill(self):
        self.assertEqual(lp.capital.refill_kind({'nativeSide': None}), 'gas_refill')     # Uniswap: POL is gas only
        self.assertEqual(lp.capital.refill_kind({}), 'gas_refill')
        self.assertEqual(lp.capital.refill_kind({'nativeSide': 'A'}), 'gas')             # Aerodrome: ETH is WETH
        self.assertEqual(lp.capital.refill_kind({'nativeSide': 'B'}), 'gas')


@contextlib.contextmanager
def profile(caps='polygon', reserve=1.0, target=3.0, keep=10.0, vault=None, tokens=((WPOL, 'WPOL'), (USDT0, 'USDT0')),
            wallet_id=None):
    reads = []

    def read_tokens():
        reads.append('tokens')
        if isinstance(tokens, Exception):
            raise tokens
        return tokens
    with mock.patch.object(config, 'CAPS', chains.caps(caps) if isinstance(caps, str) else caps), \
            mock.patch.object(config, 'GAS_RESERVE_SOL', reserve), \
            mock.patch.object(config, 'GAS_REFILL_TO', target), \
            mock.patch.object(config, 'NATIVE_KEEP', keep), \
            mock.patch.object(config, 'VAULT', vault), \
            mock.patch.object(config, 'WALLET_ID', wallet_id), \
            mock.patch.object(config, 'DEX', 'uniswap-v3-polygon'), \
            mock.patch.object(config, 'PROFILE', 'gas-test'), \
            mock.patch.object(lp.capital, 'pool_tokens', read_tokens):
        yield reads


class RefillTarget(unittest.TestCase):
    def test_only_where_the_chain_refills_and_the_pool_holds_the_wrapped_native(self):
        with profile():
            self.assertEqual(lp.capital.refill_target(), 3.0)
        for kw in ({'caps': 'solana'}, {'vault': {'kind': 'safe'}}, {'target': 1.0}, {'target': 0.5}, {'target': 0.0},
                   {'tokens': (('0x' + 'ab' * 20, 'X'), (USDT0, 'USDT0'))}, {'tokens': RuntimeError('rpc')}):
            with profile(**kw):
                self.assertEqual(lp.capital.refill_target(), 0.0, kw)
        # a chain whose signer has no `refill`, though it names a wrapped native
        with profile(caps=dict(chains.caps('polygon'), gas_refill=False)):
            self.assertEqual(lp.capital.refill_target(), 0.0)
        for caps in ('base', 'unichain', 'polygon'):
            self.assertTrue(chains.caps(caps)['gas_refill'], caps)
        self.assertNotIn('gas_refill', chains.caps('solana'))

    def test_wrapped_in_pool(self):
        with profile():
            self.assertIs(lp.capital.wrapped_in_pool(), True)
        for kw in ({'caps': 'solana'}, {'tokens': RuntimeError('rpc')}, {'tokens': (('0x' + 'ab' * 20, 'X'), (USDT0, 'USDT0'))}):
            with profile(**kw):
                self.assertIs(lp.capital.wrapped_in_pool(), False, kw)
        with profile(tokens=((USDT0, 'USDT0'), ('0x' + WPOL[2:].upper(), 'WPOL'))):
            self.assertIs(lp.capital.wrapped_in_pool(), True)       # token B, any case

    def test_no_target_reads_nothing(self):
        # every non-refilling profile asks each poll: the cheap exit reads no token
        for kw in ({'target': 0.0}, {'target': 1.0}, {'caps': 'solana', 'target': 3.0}):
            with profile(**kw) as reads:
                self.assertEqual(lp.capital.refill_target(), 0.0)
                self.assertEqual(reads, [], kw)


class RefillGas(unittest.TestCase):
    BAL = {'sol': 1.6466, 'balanceA': 900.0, 'balanceB': 7.0, 'nativeSide': None, 'price': PRICE, 'quoteUsd': 1.0}
    OK = ({'signature': '0xU', 'signatures': ['0xU'], 'unwrapped': 1.3534, 'unwrappedUsd': 0.13534, 'sold': None,
           'sent': True}, None)

    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from payouts where config_name = 'gas-test'")
            cur.execute("delete from events where profile = 'gas-test'")

    def go(self, answers=None, bal=None, state=None, need=0.0, now=None, **kw):
        answers = list(answers or [self.OK])
        calls, seen, events = [], [], []

        def chain(*a, **k):
            calls.append((a, k))
            return answers.pop(0) if answers else self.OK
        state = {} if state is None else state
        with profile(**kw), \
                mock.patch.object(lp.signers, 'chain', chain), \
                mock.patch.object(lp.capital, 'wallet', lambda p: dict(self.BAL)), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda e, **k: seen.append((e, k))), \
                mock.patch.object(db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(time, 'time', lambda: now or 1_000_000.0):
            out = lp.capital.refill_gas(state, dict(self.BAL if bal is None else bal), need=need, position='POS1')
        return out, calls, seen, events, state

    def rows(self):
        with db.cursor() as cur:
            cur.execute("select position, token_mint, symbol, amount, usd, kind, signature, detail from payouts "
                        "where config_name = 'gas-test' order by id")
            return cur.fetchall()

    def test_the_14_october_wallet_refills_once_and_books_a_gas_refill(self):
        out, calls, seen, events, state = self.go()
        self.assertIs(out, True)
        self.assertEqual(calls, [(('refill', '--execute'), {'record': False, 'extra_env': None})])   # never the venue breaker
        r, = self.rows()
        self.assertEqual((r['position'], r['token_mint'], r['symbol'], float(r['amount']), float(r['usd']), r['kind'],
                          r['signature']), ('POS1', WPOL, 'POL', 1.3534, 0.13534, 'gas_refill', '0xU'))
        self.assertIn('1.353400 POL unwrapped for gas from 1.6466 toward 3', r['detail'])
        (e, k), = seen
        self.assertEqual(e, 'GAS_REFILL')
        self.assertEqual((k['unwrapped'], k['symbol'], k['usd'], k['native_before'], k['target'], k['kind']),
                         (1.3534, 'POL', 0.13534, 1.6466, 3.0, 'gas_refill'))
        self.assertNotIn('error', k)
        self.assertEqual(events[0][0], 'GAS_REFILL')
        self.assertNotIn('gas_refill_failed', state)
        self.assertEqual(lp.books.emoji_for('GAS_REFILL', k), '⛽')

    def test_a_native_side_is_booked_as_gas_and_a_swap_is_named(self):
        sold = {'mint': USDT0, 'symbol': 'USDT0', 'amount': 0.23, 'usd': 0.23}
        out, *_ = self.go(answers=[(dict(self.OK[0], sold=sold, unwrappedUsd=None), None)], bal=dict(self.BAL, nativeSide='A'))
        self.assertIs(out, True)
        r, = self.rows()
        self.assertEqual((r['kind'], r['usd']), ('gas', None))
        self.assertIn('bought with 0.23 USDT0', r['detail'])

    def test_not_due_sends_nothing(self):
        for bal, need in ((dict(BAL, sol=2.0), 0.0), (dict(BAL, sol=2.9), 0.0), (dict(BAL, sol=5.0), 3.5),
                          ({'sol': 0.1}, 0.0)):
            out, calls, seen, *_ = self.go(bal=bal, need=need)
            self.assertEqual((out, calls, seen), (False, [], []), (bal, need))
        out, calls, *_ = self.go(bal=dict(BAL, sol=2.5), need=2.6)                # the next write's need forces it
        self.assertIs(out, True); self.assertEqual(len(calls), 1)

    def test_no_target_no_read_no_call(self):
        for kw in ({'target': 0.0}, {'caps': 'solana'}, {'vault': {'kind': 'safe'}}):
            out, calls, *_ = self.go(**kw)
            self.assertEqual((out, calls), (False, []), kw)

    def test_no_target_reads_no_wallet(self):
        with profile(target=0.0), mock.patch.object(lp.capital, 'wallet', side_effect=AssertionError('read')), \
                mock.patch.object(lp.signers, 'chain', side_effect=AssertionError('call')):
            self.assertIs(lp.capital.refill_gas({}), False)

    def test_the_default_need_is_none(self):
        # Base: reserve 0.002 ETH, target 0.006, mark 0.004: 0.005 ETH is not due
        with profile(reserve=0.002, target=0.006, keep=0.0), mock.patch.object(lp.signers, 'chain', side_effect=AssertionError('call')):
            self.assertIs(lp.capital.refill_gas({}, dict(self.BAL, sol=0.005)), False)

    def test_a_noop_with_an_error_is_a_failure_and_no_reason_is_named(self):
        out, _, seen, *_ = self.go(answers=[({'noop': True}, 'all RPC endpoints failed')])
        self.assertIs(out, False); self.assertEqual(seen[0][1]['reason'], 'all RPC endpoints failed')
        out, _, seen, *_ = self.go(answers=[(None, None)])
        self.assertEqual(seen[0][1]['reason'], 'no unwrap landed')

    def test_unknown_balances_and_an_unknown_time_wait_as_none_and_zero(self):
        state = {}
        self.go(answers=[(None, 'x')], bal=dict(self.BAL, balanceA=None, balanceB=None), state=state)
        self.assertEqual(state['gas_refill_failed']['held'], [0.0, 0.0])
        # a failure without its time: the wait counts from 0
        state = {'gas_refill_failed': {'held': [900.0, 7.0], 'why': 'x'}}
        out, calls, *_ = self.go(state=state, now=lp.capital.REFILL_RETRY_S + 0.5)
        self.assertEqual(len(calls), 1)
        state = {'gas_refill_failed': {'held': [900.0, 7.0], 'why': 'x'}}
        out, calls, *_ = self.go(state=state, now=lp.capital.REFILL_RETRY_S - 0.5)
        self.assertEqual(calls, [])

    def test_a_noop_answer_and_a_held_refusal_are_quiet(self):
        out, calls, seen, _, state = self.go(answers=[({'noop': True}, None)], state={'gas_refill_failed': {'at': 0, 'held': [], 'why': 'x'}})
        self.assertEqual((out, seen), (False, [])); self.assertNotIn('gas_refill_failed', state)
        for err in ('refused: halted (HALT)', 'refused: wallet w lock busy', 'refused: max fee 9 wei/gas exceeds LPBOT_EVM_MAX_GWEI'):
            out, calls, seen, events, state = self.go(answers=[(None, err)])
            self.assertEqual((out, seen, events, state), (False, [], [], {}), err)
        self.assertEqual(self.rows(), [])

    def test_a_failure_is_said_once_and_waits_unless_the_wallet_changed(self):
        state = {}
        fail = (None, 'refused: nothing to unwrap: the wallet holds no WPOL and no USDT0 to buy it')
        out, calls, seen, events, _ = self.go(answers=[fail], state=state, now=1000.0)
        self.assertIs(out, False)
        self.assertEqual([e for e, _ in seen], ['gas_refill_failed'])
        self.assertEqual(seen[0][1]['reason'], fail[1]); self.assertEqual(events[0][0], 'gas_refill_failed')
        self.assertEqual(state['gas_refill_failed']['held'], [900.0, 7.0])
        # the same wallet within the wait: not asked again
        out, calls, seen, *_ = self.go(answers=[fail], state=state, now=1000.0 + lp.capital.REFILL_RETRY_S - 1)
        self.assertEqual((out, calls, seen), (False, [], []))
        # after the wait: asked again, the same reason not said again
        out, calls, seen, *_ = self.go(answers=[fail], state=state, now=1000.0 + lp.capital.REFILL_RETRY_S)
        self.assertEqual((len(calls), seen), (1, []))
        # the wallet changed (a harvest brought fees): asked at once, and a new reason is said
        out, calls, seen, *_ = self.go(answers=[(None, 'all RPC endpoints failed')], state=state,
                                       bal=dict(self.BAL, balanceA=905.0), now=1000.0 + lp.capital.REFILL_RETRY_S + 1)
        self.assertEqual(len(calls), 1); self.assertEqual([e for e, _ in seen], ['gas_refill_failed'])
        # a success clears the wait
        out, *_ = self.go(state=state, bal=dict(self.BAL, balanceA=910.0), now=1000.0 + lp.capital.REFILL_RETRY_S + 2)
        self.assertIs(out, True); self.assertNotIn('gas_refill_failed', state)

    def test_a_swap_without_its_unwrap_is_a_failure_and_an_unwrap_with_an_error_is_booked(self):
        partial = ({'signature': '0xS', 'signatures': ['0xA', '0xS'], 'partial': True, 'sent': True}, 'reverted')
        out, _, seen, _, state = self.go(answers=[partial])
        self.assertIs(out, False)
        self.assertEqual(seen[0][1]['signatures'], ['0xA', '0xS'])
        self.assertEqual(self.rows(), [])
        landed = (dict(self.OK[0], partial=True), 'receipt read failed after the unwrap')
        out, _, seen, *_ = self.go(answers=[landed])
        self.assertIs(out, True)
        self.assertEqual(seen[0][1]['error'], 'receipt read failed after the unwrap')
        self.assertEqual(len(self.rows()), 1)
        for nothing in (({'unwrapped': 0}, None), ({}, None), (None, None), ({'unwrapped': -1, 'signature': 'x'}, None)):
            out, *_ = self.go(answers=[nothing])
            self.assertIs(out, False, nothing)
        self.assertEqual(len(self.rows()), 1)

    def test_a_shared_wallet_refills_from_this_profile_s_sleeve_only(self):
        out, calls, *_ = self.go(wallet_id='w1')
        self.assertIs(out, True)
        self.assertEqual(calls[0][1]['extra_env'], {'LPBOT_SLEEVE': '{"%s": 900.0, "%s": 7.0}' % (WPOL, USDT0)})
        out, calls, *_ = self.go(wallet_id='w1', tokens=RuntimeError('rpc'))
        self.assertEqual((out, calls), (False, []))                  # the pool unknown: nothing sent
        # the tokens read once for the target, then failing for the sleeve: nothing sent either
        reads = iter([((WPOL, 'WPOL'), (USDT0, 'USDT0'))])

        def once():
            try:
                return next(reads)
            except StopIteration:
                raise RuntimeError('rpc') from None
        calls = []
        with profile(wallet_id='w1'), mock.patch.object(lp.capital, 'pool_tokens', once), \
                mock.patch.object(lp.signers, 'chain', lambda *a, **k: calls.append(a)), \
                mock.patch.object(lp.paths, 'save', lambda s: None):
            self.assertIs(lp.capital.refill_gas({}, dict(self.BAL)), False)
        self.assertEqual(calls, [])

    def test_an_unreadable_wallet_sends_nothing(self):
        out, calls, *_ = self.go(bal={})
        self.assertEqual((out, calls), (False, []))


BAL = RefillGas.BAL


class GasReady(unittest.TestCase):
    def go(self, bal, refilled=True, fresh=None):
        calls, seen = [], []

        def refill(state, b, need=0.0, position=None):
            calls.append(('refill', need, position))
            return refilled
        with profile(), \
                mock.patch.object(lp.capital, 'refill_gas', refill), \
                mock.patch.object(lp.capital, 'wallet', lambda p: (calls.append(('read', p)) or dict(fresh or {}))), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda e, **k: seen.append(e)), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(config, 'POOL', 'P'):
            out = lp.capital.gas_ready({}, dict(bal), position='POS')
        return out, calls, seen

    def test_enough_gas_goes_on_with_the_same_read(self):
        out, calls, seen = self.go(dict(BAL, sol=1.3))                # reserve 1 + the swap's 0.3
        self.assertEqual((out['sol'], calls, seen), (1.3, [], []))

    def test_short_refills_first_and_goes_on_with_the_wallet_after_it(self):
        out, calls, seen = self.go(dict(BAL, sol=1.29), fresh=dict(BAL, sol=3.0))
        self.assertEqual(calls, [('refill', 1.3, 'POS'), ('read', 'P')])
        self.assertEqual((out['sol'], seen), (3.0, []))

    def test_short_without_a_refill_holds_and_says_so_once(self):
        out, calls, seen = self.go(dict(BAL, sol=1.29), refilled=False)
        self.assertIsNone(out); self.assertEqual(seen, ['gas_short']); self.assertEqual(len(calls), 1)
        # a refill that landed but whose read failed: the old read decides
        out, calls, seen = self.go(dict(BAL, sol=1.29), fresh={})
        self.assertIsNone(out)


class NativeUpkeep(unittest.TestCase):
    def go(self, **kw):
        calls = []
        with profile(**{k: v for k, v in kw.items() if k in ('target', 'keep', 'caps')}), \
                mock.patch.object(lp.capital, 'wallet', lambda p: (calls.append('read') or dict(BAL))), \
                mock.patch.object(lp.capital, 'refill_gas', lambda s, b, position=None: (calls.append(('refill', b is not None, position)) or kw.get('refilled', False))), \
                mock.patch.object(lp.capital, 'wrap_native', lambda s, b: calls.append(('wrap', b is not None))), \
                mock.patch.object(config, 'POOL', 'P'):
            lp.capital.native_upkeep({}, position='POS')
        return calls

    def test_one_read_for_both_and_none_for_neither(self):
        self.assertEqual(self.go(), ['read', ('refill', True, 'POS'), ('wrap', True)])
        self.assertEqual(self.go(refilled=True), ['read', ('refill', True, 'POS'), ('wrap', False)])   # wrap reads again
        self.assertEqual(self.go(keep=0.0), ['read', ('refill', True, 'POS')])
        self.assertEqual(self.go(target=0.0), ['read', ('wrap', True)])
        self.assertEqual(self.go(target=0.0, keep=0.0), [])
        self.assertEqual(self.go(caps='solana'), [])
        self.assertEqual(self.go(target=0.0, keep=0.5), ['read', ('wrap', True)])          # any keep above 0 wraps


# --- the incident on the fake Polygon chain --------------------------------------


class RefillChain(PL.PolyChain):
    """The Polygon fake chain with the signer's `refill` and `increase`, its
    refusals under the gas reserve (swap, add, open), and a gas cost per
    transaction sent."""

    gas = 0.0

    def __call__(self, *args, dex=None, timeout=420, extra_env=None):
        out, err = super().__call__(*args, dex=dex, timeout=timeout, extra_env=extra_env)
        if out and out.get('signature') and '--execute' in args and args[0] != 'wrap':
            self.wallet[POL] -= self.gas
        elif out and out.get('signature') and args[0] in ('rebalance', 'open'):
            self.wallet[POL] -= self.gas
        return out, err

    def answer(self, *args, dex=None, extra_env=None):
        cmd = args[0]
        under = self.wallet[POL] < config.GAS_RESERVE_SOL
        if cmd in ('rebalance', 'open', 'increase') and under:
            self.calls.append({'args': args, 'dex': dex, 'env': extra_env or {}, 'profile': config.PROFILE})
            return {'sent': False, 'error': 'refused: below the gas reserve'}, 'refused: POL is below the gas reserve'
        if cmd == 'refill':
            self.calls.append({'args': args, 'dex': dex, 'env': extra_env or {}, 'profile': config.PROFILE})
            need = config.GAS_REFILL_TO - self.wallet[POL]
            if need <= 0:
                return {'noop': True}, None
            from_w = min(need, self.wallet[WPOL])
            short = need - from_w
            sold = None
            if short > 0:
                cost = short * PRICE * 1.0105
                if self.wallet[USDT0] < cost:
                    return None, 'refused: nothing to unwrap: the wallet holds no WPOL and no USDT0 to buy it'
                self.wallet[USDT0] -= cost
                sold = {'mint': USDT0, 'symbol': 'USDT0', 'amount': cost, 'usd': cost}
            self.wallet[WPOL] -= from_w
            self.wallet[POL] += need
            self.n += 1
            return {'signature': f'refill{self.n}', 'signatures': [f'refill{self.n}'], 'unwrapped': need,
                    'unwrappedUsd': need * PRICE, 'sold': sold, 'sent': True}, None
        if cmd == 'increase':
            self.calls.append({'args': args, 'dex': dex, 'env': extra_env or {}, 'profile': config.PROFILE})
            pos = self.positions[POLY_POOL]
            a, b = min(float(args[2]), self.wallet[WPOL]), min(float(args[3]), self.wallet[USDT0])
            self.wallet[WPOL] -= a; self.wallet[USDT0] -= b
            pos['a'] += a; pos['b'] += b
            self.n += 1
            return {'signature': f'inc{self.n}', 'sent': True, 'depositUsd': a * PRICE + b}, None
        return super().answer(*args, dex=dex, extra_env=extra_env)


class Incident(PL.Polygon):
    def setUp(self):
        super().setUp()
        self.chain = RefillChain()
        p = mock.patch.object(lp.signers, '_chain', self.chain); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(lp.swaps, 'IDLE_MIN_AGE_S', 0); p.start(); self.addCleanup(p.stop)

    def run_polls(self, n=1, reserve=1.0, target=3.0, keep=10.0):
        seen = []
        real = lp.books.notify
        with self.as_profile('e2e-poly'), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', reserve), \
                mock.patch.object(config, 'GAS_REFILL_TO', target), \
                mock.patch.object(config, 'NATIVE_KEEP', keep), \
                mock.patch.object(lp.books, 'notify', lambda e, **kw: (seen.append((e, kw)), real(e, **kw))):
            for _ in range(n):
                try:
                    lp.loop.main()
                except M.StopPoll:
                    pass
        return seen

    def writes(self):
        return [c['args'][0] for c in self.chain.calls if '--execute' in c['args'] or c['args'][0] == 'rebalance']

    def refills(self):
        with db.cursor() as cur:
            cur.execute("select kind, round(amount::numeric, 6) amount, round(usd::numeric, 6) usd from payouts "
                        "where config_name = 'e2e-poly' and kind in ('gas', 'gas_refill') order by id")
            return [(r['kind'], float(r['amount']), float(r['usd'])) for r in cur.fetchall()]

    def incident_wallet(self, pol):
        """The band open, then $12.93 idle beside it (WPOL fees and USDT0), POL at `pol`."""
        self.opened()
        self.chain.calls.clear()
        self.chain.wallet.update({WPOL: self.chain.wallet[WPOL] + 64.65, USDT0: self.chain.wallet[USDT0] + 6.465, POL: pol})

    def test_the_14_october_replay_refills_first_then_swaps_and_adds_in_the_same_poll(self):
        self.incident_wallet(1.9626)
        self.chain.gas = 0.05
        seen = self.run_polls(1)
        w = self.writes()
        self.assertEqual(w[0], 'refill')
        self.assertIn('increase', w)
        self.assertLess(w.index('refill'), w.index('rebalance') if 'rebalance' in w else w.index('increase'))
        self.assertNotIn('gas_short', [e for e, _ in seen])
        self.assertNotIn('increase_failed', [e for e, _ in seen])
        # 3 - 1.9626 = 1.0374 POL unwrapped from the WPOL fees, $0.10374 booked
        self.assertEqual(self.refills(), [('gas_refill', 1.0374, 0.10374)])
        self.assertGreaterEqual(self.chain.wallet[POL], 1.0 + 0.3 - 1e-9)

    def test_without_a_refill_a_short_gas_sends_nothing_poll_after_poll(self):
        # the live config before the fix: reserve 2 POL, no refill; every attempt used to cost ~0.05 POL
        self.incident_wallet(1.9626)
        self.chain.gas = 0.05
        seen = self.run_polls(4, reserve=2.0, target=0.0)
        self.assertEqual(self.writes(), [])                          # no swap, no add, no refill: nothing sent
        self.assertEqual(self.chain.wallet[POL], 1.9626)             # not one wei of gas spent
        self.assertEqual([e for e, _ in seen].count('gas_short'), 1)  # said once
        self.assertNotIn('increase_failed', [e for e, _ in seen])

    def test_an_open_short_of_gas_refills_and_opens_in_the_same_poll(self):
        # the 00:32Z state: no position, money in the wallet, POL 1.27 over the low-water mark of a
        # 1 / 1.5 band but under what the open's swap needs (1.3): the open itself refills
        self.chain.wallet.update({USDT0: 200.0, WPOL: 0.0, POL: 1.27})
        seen = self.run_polls(1, reserve=1.0, target=1.5)
        self.assertIn(POLY_POOL, self.chain.positions)
        w = self.writes()
        self.assertEqual(w[0], 'refill'); self.assertIn('open', w)
        self.assertNotIn('gas_short', [e for e, _ in seen])
        kind, amount, usd = self.refills()[0]
        self.assertEqual((kind, amount), ('gas_refill', 0.23))
        self.assertAlmostEqual(usd, 0.023, places=6)

    def test_a_refill_with_nothing_to_unwrap_holds_and_sends_nothing(self):
        self.chain.wallet.update({USDT0: 0.0, WPOL: 0.0, POL: 0.5})
        seen = self.run_polls(2, reserve=1.0, target=3.0)
        self.assertEqual([w for w in self.writes() if w != 'refill'], [])
        self.assertLessEqual(len([c for c in self.chain.calls if c['args'][0] == 'refill']), 1)   # the wait holds the second
        self.assertEqual(self.chain.wallet[POL], 0.5)
        self.assertNotIn(POLY_POOL, self.chain.positions)

    def test_the_low_water_mark_refills_with_the_position_open_and_books_it_once(self):
        self.incident_wallet(1.9)
        self.run_polls(1)
        self.run_polls(1)                                            # at the target now: no second refill
        self.assertEqual(len(self.refills()), 1)
        self.assertAlmostEqual(self.chain.wallet[POL], 3.0, places=9)


# --- the book: a refill is value, not a loss --------------------------------------


class Book(unittest.TestCase):
    def setUp(self):
        reset()
        snap(T(0.1), 240.0, 120.0)
        snap(T(23.9), 238.0, 120.0)                                  # $2 of WPOL left the book as POL
        payout(T(3), 0.3)
        payout(T(5), 2.0, 'gas_refill')
        payout(T(6), 9.0, 'gas')                                     # native in the book: never counted twice
        self.addCleanup(reset)

    def test_the_daily_line_adds_the_refill_to_value_and_shows_it(self):
        l = db.daily_line(DAY)
        self.assertAlmostEqual(l['paid_out_usd'], 0.3)
        self.assertAlmostEqual(l['gas_refill_usd'], 2.0)
        self.assertAlmostEqual(l['value_change_usd'], 238.0 + 0.3 + 2.0 - 240.0)
        both = db.combine_days([l, dict(l)])
        self.assertAlmostEqual(both['gas_refill_usd'], 4.0)

    def test_since_start_adds_the_refill_to_value_and_profit(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd) values ('sol-usdc', 'P', 'X', 1, 1) "
                        "on conflict (name) do nothing")
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, profile) "
                        "values (%s, 'baseline', 1, 120, 240, 120, 'sol-usdc')", (T(0),))
            cur.execute("insert into positions (mint, config_name, pool) values ('M', 'sol-usdc', 'P') on conflict do nothing")
        s = db.since_start(0.0, 'sol-usdc')
        self.assertAlmostEqual(s['gas_refill_usd'], 2.0)
        self.assertAlmostEqual(s['paid_out_usd'], 0.3)
        self.assertAlmostEqual(s['value_usd'], 238.0 + 0.3 + 2.0)
        self.assertAlmostEqual(s['profit_usd'], 238.0 + 0.3 + 2.0 - 240.0)
        self.assertAlmostEqual(db.combine_since([s, dict(s)])['gas_refill_usd'], 4.0)

    def test_payout_totals_show_a_refill_as_gas(self):
        with mock.patch.object(db, 'book_scope', lambda p=None, w=None: ['sol-usdc']):
            t = db.payout_totals('sol-usdc')
        self.assertAlmostEqual(t['gas_usd'], 11.0)
        self.assertAlmostEqual(t['paid_usd'], 0.3)


# --- the migration and the config ---------------------------------------------------


class Migration041(unittest.TestCase):
    SQL = (ROOT / 'sql' / '041_gas_refill.sql').read_text()

    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name in ('poly-wpol-usdt', 'm41')")
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, gas_reserve_sol, native_keep) "
                        "values ('poly-wpol-usdt', 'P', 'WPOL/USDT0', 1, 1, 2, 10), ('m41', 'P', 'X', 1, 1, 0.05, null)")
        self.addCleanup(self._drop)

    def _drop(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from payouts where config_name = 'm41'")
            cur.execute("delete from config where name in ('poly-wpol-usdt', 'm41')")

    def apply(self):
        with db.cursor(commit=True) as cur:
            cur.execute(self.SQL.replace('begin;', '').replace('commit;', ''))

    def test_runs_twice_and_sets_the_polygon_profile(self):
        self.apply(); self.apply()
        with db.cursor() as cur:
            cur.execute("select gas_reserve_sol, gas_refill_to, native_keep from config where name = 'poly-wpol-usdt'")
            r = cur.fetchone()
        self.assertEqual((float(r['gas_reserve_sol']), float(r['gas_refill_to']), float(r['native_keep'])), (1.0, 3.0, 10.0))

    def test_the_checks(self):
        self.apply()
        for bad in ("update config set gas_refill_to = 0.05 where name = 'm41'",              # not above the reserve
                    "update config set gas_refill_to = 0.01 where name = 'm41'",
                    "update config set gas_refill_to = 11 where name = 'poly-wpol-usdt'",     # above native_keep
                    "update config set gas_reserve_sol = 3 where name = 'poly-wpol-usdt'",    # the reserve passes it
                    "insert into payouts (token_mint, amount, kind) values ('x', 1, 'bogus')"):
            with self.subTest(bad), self.assertRaises(psycopg2.errors.CheckViolation), db.cursor(commit=True) as cur:
                cur.execute(bad)
        with db.cursor(commit=True) as cur:
            cur.execute("update config set gas_refill_to = 0.06 where name = 'm41'")             # no native_keep: any target
            cur.execute("update config set gas_refill_to = 10 where name = 'poly-wpol-usdt'")    # at native_keep
            cur.execute("update config set gas_refill_to = 0 where name = 'poly-wpol-usdt'")     # off
            cur.execute("update config set gas_refill_to = null where name = 'poly-wpol-usdt'")
            cur.execute("insert into payouts (config_name, token_mint, amount, kind) values ('m41', 'x', 1, 'gas_refill') returning id")
            cur.execute("delete from payouts where config_name = 'm41'")


class ConfigClamp(unittest.TestCase):
    def target(self, **env):
        out = subprocess.run([sys.executable, '-c', 'import config; print(config.GAS_REFILL_TO)'], cwd=ROOT,
                             env=dict(os.environ, LPBOT_PROFILE='m41c', **env), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return float(out.stdout.strip().splitlines()[-1])

    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name = 'm41c'")
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, gas_reserve_sol) "
                        "values ('m41c', 'P', 'X', 1, 1, 1)")
        self.addCleanup(self._drop)

    def _drop(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name = 'm41c'")

    def test_never_above_native_keep_when_keep_is_on(self):
        self.assertEqual(self.target(), 0.0)
        self.assertEqual(self.target(LPBOT_GAS_REFILL_TO='5'), 5.0)
        self.assertEqual(self.target(LPBOT_GAS_REFILL_TO='5', LPBOT_NATIVE_KEEP='3'), 3.0)
        self.assertEqual(self.target(LPBOT_GAS_REFILL_TO='2', LPBOT_NATIVE_KEEP='3'), 2.0)
        self.assertEqual(self.target(LPBOT_GAS_REFILL_TO='5', LPBOT_NATIVE_KEEP='0'), 5.0)
        with db.cursor(commit=True) as cur:
            cur.execute("update config set gas_refill_to = 4, native_keep = 10 where name = 'm41c'")
        self.assertEqual(self.target(), 4.0)

    def test_the_signer_is_told_the_target(self):
        src = (ROOT / 'lp' / 'signers.py').read_text()
        self.assertIn('LPBOT_GAS_REFILL_TO=str(config.GAS_REFILL_TO)', src)


if __name__ == '__main__':
    unittest.main()
