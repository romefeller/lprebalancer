"""Edges of the moves: a swing switch whose open fails, a Jupiter outage at
the switch, the hot pause's swap in a Jupiter outage, a HALT in the middle of
a move, and a payout that timed out after it was sent.

The first two and the HALT run the real loop over the fake chain of
test_multi_loop (test_swing.Loop adds the DJT/USDC Orca pool); the hot pause
and the payout run their own function with the signer call stubbed."""
import contextlib
import json
import os
import time
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import db
import health
import lp.books
import lp.capital
import lp.harvest
import lp.moves
import lp.pauses
import lp.regime
import lp.paths
import lp.signers
import lp.swaps
import test_multi_loop as tml
import test_swing as tsw
from test_deploy_all import bal

DJT, DJT_POOL = tsw.DJT, tsw.DJT_POOL
J429 = 'Jupiter rate limited: ERROR: Jupiter 429 on /swap/v1/quote: Rate limit exceeded'
ORCA_NO = 'ERROR: Orca quote: no route for this pair'
OPEN_REFUSED = 'Error processing Instruction 2: custom program error: 0x177c'


def wrap_answer(chain, rule):
    """Put `rule(args, dex)` in front of the fake chain: a (out, err) answer
    replaces the fake's, None lets the fake answer."""
    real = chain.answer

    def answer(*args, dex=None, extra_env=None):
        got = rule(args, dex)
        if got is not None:
            chain.calls.append({'profile': config.PROFILE, 'dex': dex, 'args': args, 'env': dict(extra_env or {})})
            return got
        return real(*args, dex=dex, extra_env=extra_env)
    chain.answer = answer


class SwingFixture(tsw.Loop):
    def setUp(self):
        super().setUp()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate health')
        self.addCleanup(self.truncate_health)
        # Jupiter's 429 pauses are 30 s and 60 s; the fixture's sleep ends a
        # poll at 60 s, which would cut the swap off before its fallback.
        for p in (mock.patch.object(lp.swaps, 'SWAP_RATE_LIMIT_PAUSES', (1, 2)),
                  mock.patch.object(lp.swaps, 'SWAP_FALLBACK', 'orca-swap')):
            p.start(); self.addCleanup(p.stop)

    @staticmethod
    def truncate_health():
        with db.cursor(commit=True) as cur:
            cur.execute('truncate health')

    def state(self):
        with self.as_profile('e2e-swing'):
            return json.loads(lp.paths.STATE.read_text())

    def halted(self):
        with self.as_profile('e2e-swing'):
            return lp.paths.halted()

    def opens(self, pool=None):
        return [c for c in self.chain.of('e2e-swing', 'open') if pool is None or c['args'][1] == pool]


# The fixture only: test_swing.Loop's own tests run in test_swing.
for _name in [n for n in dir(tsw.Loop) if n.startswith('test_')]:
    setattr(SwingFixture, _name, None)


# --- 1 ----------------------------------------------------------------------------------
class SwitchCloseLandsOpenFails(SwingFixture):
    """The close on the old pool lands, the open on the new one fails."""

    def test_the_next_poll_opens_on_the_new_pool_without_a_halt(self):
        self.chain.wallet.update({tml.USDC: 200.0, tml.SOL: 0.1})
        with self.pins():
            self.poll('e2e-swing')
        self.assertIn(tml.SOL_POOL, self.chain.positions)
        refused = []

        def rule(args, dex):
            if args[0] == 'open' and args[1] == DJT_POOL and not refused:
                refused.append(args)
                return None, OPEN_REFUSED                      # nothing opened: the chain shows no position
            return None
        wrap_answer(self.chain, rule)
        self.move('orca', DJT_POOL)
        self.assertEqual(len(refused), 1)
        self.assertEqual(self.chain.positions, {})               # the close landed, the open did not
        self.assertEqual(self.state()['failures'], 1)
        self.assertIsNone(self.halted())                         # one failure is no HALT
        with db.cursor() as cur:
            cur.execute("select pool from config where name = 'e2e-swing'")
            self.assertEqual(cur.fetchone()['pool'], DJT_POOL)  # the profile stays on the new pool
        with self.pins():
            self.poll('e2e-swing')                               # no MIGRATE: the no-position path
        self.assertIn(DJT_POOL, self.chain.positions)
        self.assertNotIn(tml.SOL_POOL, self.chain.positions)
        self.assertEqual(len(self.opens(DJT_POOL)), 2)           # the refused one and this one
        self.assertEqual(self.state()['failures'], 0)            # an open that lands clears the count
        self.assertIsNone(self.halted())


# --- 2 ----------------------------------------------------------------------------------
class JupiterOutageAtTheSwitch(SwingFixture):
    """2026-10-09 13:26Z: the swing moved to DJT/USDC while Jupiter answered
    429. The wallet held SOL, a little USDC and no DJT."""

    def outage(self, orca_ok):
        def rule(args, dex):
            if args[0] == 'rebalance' and dex == 'jupiter':
                return None, J429
            if args[0] == 'rebalance' and dex == 'orca-swap' and not orca_ok:
                return None, ORCA_NO                            # refused before sending
            return None
        wrap_answer(self.chain, rule)
        self.chain.wallet.update({tml.SOL: 1.7, tml.USDC: 13.70, DJT: 0.0})

    def orca_calls(self):
        return [c for c in self.chain.of('e2e-swing', 'rebalance') if c['dex'] == 'orca-swap']

    def test_the_orca_fallback_gets_the_held_pool_and_the_open_runs(self):
        self.outage(orca_ok=True)
        self.move('orca', DJT_POOL)
        djt = [c for c in self.orca_calls() if DJT in c['args'][1:3]]
        self.assertTrue(djt)
        for c in djt:
            a = c['args']
            self.assertEqual(a[a.index('--pool') + 1], DJT_POOL)
        self.assertIn(DJT_POOL, self.chain.positions)
        self.assertEqual(self.state()['failures'], 0)
        self.assertIsNone(self.halted())

    def sleep(self, s):
        # Polls inside one main(), as the live process runs them: a failure
        # counted only in memory would add up here and write HALT.
        self.sleeps.append(s)
        if s >= 60:
            self.polls_left = getattr(self, 'polls_left', 1) - 1
            if self.polls_left <= 0:
                raise tml.StopPoll()

    def test_a_fallback_that_sends_nothing_opens_nothing_and_three_polls_never_halt(self):
        self.outage(orca_ok=False)
        self.polls_left = 3
        self.move('orca', DJT_POOL)
        self.assertEqual(len([s for s in self.sleeps if s >= 60]), 3)
        self.assertEqual(self.opens(), [])                       # an open with 0 DJT is refused on chain
        self.assertEqual(self.chain.positions, {})
        self.assertTrue(self.orca_calls())                       # the fallback was tried
        self.assertEqual(self.state().get('failures', 0), 0)
        self.assertIsNone(self.halted())


# --- 3 ----------------------------------------------------------------------------------
class HotPauseSwapInAJupiterOutage(unittest.TestCase):
    """hot_pause_close: the band is closed, then the 50/50 swap meets 429."""

    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate health')
        self.addCleanup(SwingFixture.truncate_health)
        for name, v in (('DEPLOY_ALL', True), ('MAX_USD', 300.0), ('SIDE_CAP_FRACTION', 0.55),
                        ('GAS_RESERVE_SOL', 0.05), ('CAPITAL_USD', 190.0), ('PAYOUT_ENABLED', False),
                        ('REBALANCE_SWAP', True), ('MAX_CONSECUTIVE_FAILURES', 3), ('DEX', 'raydium-clmm'),
                        ('POOL', tml.SOL_POOL), ('WALLET_ID', None), ('CHAIN', 'solana')):
            p = mock.patch.object(config, name, v); p.start(); self.addCleanup(p.stop)
        for p in (mock.patch.object(lp.swaps, 'SWAP_FALLBACK', 'orca-swap'),
                  mock.patch.object(lp.capital, 'pool_record', lambda: tml.pool_record_for(tml.SOL_POOL))):
            p.start(); self.addCleanup(p.stop)

    def go(self, answers, wallet=None):
        calls, halts = [], []
        it = iter(answers)
        before = wallet or bal(0.06, 200.0, price=150.0)            # the closed band left USDC: SOL short
        after = bal(0.7, 110.0, price=150.0)
        reads = iter([before, after])
        state = dict(lp.paths.STATE_DEFAULTS)
        status = {'positionMint': 'M', 'whirlpool': tml.SOL_POOL, 'price': 150.0}

        def fake(*a, **k):
            calls.append((a, k))
            return next(it)
        with mock.patch.object(lp.signers, 'chain', fake), \
                mock.patch.object(lp.capital, 'wallet', lambda p: next(reads)), \
                mock.patch.object(lp.capital, 'position_usd', lambda s: 210.0), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: None), \
                mock.patch.object(db, 'position_closed', lambda m: True), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(lp.books, 'halt', lambda r: halts.append(r)), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(time, 'sleep', lambda s: None), \
                mock.patch('builtins.print'):
            r = lp.pauses.hot_pause_close(state, status, 'hot pause: test', time.time())
        return r, calls, state, halts

    def test_429_falls_back_to_orca_and_counts_no_failure(self):
        ok = ({'sent': True, 'signature': 'ORCA', 'routePlan': ['Orca Whirlpool']}, None)
        r, calls, state, halts = self.go([(None, J429)] * 3 + [ok])
        self.assertTrue(r)
        self.assertEqual([k['dex'] for _, k in calls], ['jupiter'] * 3 + ['orca-swap'])
        self.assertEqual(calls[-1][0][:3], ('rebalance', tml.SOL, tml.USDC))
        self.assertTrue(state['hot_pause']['swapped'])
        self.assertEqual((state['failures'], halts), (0, []))
        self.assertEqual(health.load('swap')['fails'], 0)            # the bot could swap
        # Jupiter's own breaker: one outcome for one swap, whatever the three attempts.
        self.assertEqual(health.load('jupiter')['fails'], 1)

    def test_both_failing_with_sol_short_holds_without_a_failure_and_once_per_pause(self):
        r, calls, state, halts = self.go([(None, J429)] * 3 + [(None, ORCA_NO)])
        self.assertTrue(r)
        self.assertEqual(len(calls), 4)
        self.assertTrue(state['hot_pause']['swapped'])               # hot_paused does not try again
        self.assertEqual((state['failures'], halts), (0, []))


# --- 4 ----------------------------------------------------------------------------------
class HaltInTheMiddleOfAMove(tml.Fixture):
    """HALT written after the close landed and before the reopen."""

    def setUp(self):
        super().setUp()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate health')
        self.addCleanup(SwingFixture.truncate_health)
        self.chain.wallet[tml.SOL] = 2.0
        self.poll('e2e-sol')                                     # SOL: swap to 50/50, open
        self.assertIn(tml.SOL_POOL, self.chain.positions)
        self.swaps0 = len(self.sent('rebalance'))
        self.opens0 = len(self.sent('open'))

    def sent(self, cmd):
        return [c for c in self.chain.of('e2e-sol', cmd) if c.get('sent')]

    def halting_after(self, cmd):
        """The fake chain writes HALT when `cmd` lands, and from then on
        refuses every write as signers._chain does (the fake replaces it)."""
        real = self.chain.answer

        def answer(*args, dex=None, extra_env=None):
            stop = lp.paths.halted() if '--execute' in args else None
            if stop:
                self.chain.calls.append({'profile': config.PROFILE, 'dex': dex, 'args': args, 'env': {}})
                return None, f'refused: halted ({stop})'
            out, err = real(*args, dex=dex, extra_env=extra_env)
            self.chain.calls[-1]['sent'] = bool(out and out.get('signature'))
            if args[0] == cmd and out and out.get('signature'):
                lp.paths.HALT.write_text('operator halt mid-move')
            return out, err
        self.chain.answer = answer

    def move_then_resume(self, halt_after):
        # the band went all to SOL: the reopen needs a swap
        pos = self.chain.positions[tml.SOL_POOL]
        pos['a'], pos['b'] = pos['a'] + pos['b'] / 150.0, 0.0
        self.halting_after(halt_after)
        with self.as_profile('e2e-sol'):
            state = lp.paths.load()
            lp.moves.rebalance(state, self.chain.status(tml.SOL_POOL), 'calm: tight band', band=1.02, calm_move=True)
            self.assertIsNotNone(lp.paths.halted())
            saved = lp.paths.load()
        self.assertEqual(self.chain.positions, {})
        self.assertTrue(saved['pending_reopen']['closed'])       # the intent survives the HALT
        self.assertEqual(saved['failures'], 0)                   # a held write is no failure
        self.assertEqual(len(self.sent('open')), self.opens0)
        self.assertEqual(self.poll('e2e-sol'), 2)                # halted: the loop stops at once
        with self.as_profile('e2e-sol'):
            lp.paths.HALT.unlink()
        self.poll('e2e-sol')                                     # resume_reopen
        self.assertIn(tml.SOL_POOL, self.chain.positions)
        with self.as_profile('e2e-sol'):
            state = lp.paths.load()
            self.assertNotIn('pending_reopen', state)            # consumed: no second reopen
            self.assertFalse(lp.regime.resume_reopen(state))

    def test_halt_after_the_close_resumes_with_one_swap_and_one_open(self):
        self.move_then_resume('close')
        self.assertEqual(len(self.sent('open')) - self.opens0, 1)
        self.assertEqual(len(self.sent('rebalance')) - self.swaps0, 1)

    def test_halt_after_the_swap_resumes_with_one_open_and_no_second_swap(self):
        self.move_then_resume('rebalance')
        self.assertEqual(len(self.sent('open')) - self.opens0, 1)
        self.assertEqual(len(self.sent('rebalance')) - self.swaps0, 1)


# --- 5 ----------------------------------------------------------------------------------
# By case: test_money_paths.Distribute.test_a_timeout_is_uncertain and
# test_hardening.Payouts.test_an_unconfirmed_send_is_never_owed. Here: the
# next harvest's payout, over any fee, any earlier debt and any "may have landed" answer.
SOL, USDC = tml.SOL, tml.USDC
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
MAYBE_SENT = [(None, 'signer timed out'), (None, 'request timed out'), (None, 'RPC timeout'),
              (None, 'sent X but could not confirm it'), ({'signature': 'S1'}, 'blockhash expired'),
              ({'signature': 'S1', 'partial': True}, 'partial transaction execution'), ({'partial': True}, 'boom')]


class PayoutTimedOutAfterSending(unittest.TestCase):
    def harvest(self, state, fee_b, answer, sends):
        b = {'balanceA': 0.3, 'balanceB': 1000.0, 'sol': 0.3, 'price': 120.0, 'quoteUsd': 1.0}
        env = dict(os.environ, LPBOT_PROFIT_WALLET_PIN=PROFIT)

        def chain(*a, **k):
            sends.append(a)
            return answer
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(config, 'CHAIN', 'solana'), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(lp.capital, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(lp.capital, 'wallet', lambda p: b), \
                mock.patch.object(lp.signers, 'chain', chain), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(db, 'record_payout', lambda *a, **k: None):
            lp.harvest.distribute(state, 'M', 0.0, fee_b)

    @settings(max_examples=150, deadline=None)
    @given(st.floats(0.01, 50.0), st.floats(0.01, 50.0), st.floats(0.0, 5.0), st.sampled_from(MAYBE_SENT))
    def test_property_the_next_payout_sends_only_the_next_fee(self, first, second, owed, maybe):
        state = {'payout_owed': {USDC: owed}} if owed else {}
        sends = []
        self.harvest(state, first, maybe, sends)
        self.assertEqual(len(sends), 1)
        self.assertNotIn(USDC, state.get('payout_owed', {}))         # never owed: it may have landed
        self.harvest(state, second, ({'signature': 'S2'}, None), sends)
        self.assertEqual(len(sends), 2)
        self.assertAlmostEqual(float(sends[1][2]), second, places=8)  # not first + second
        self.assertEqual(state['payout_owed'], {})


if __name__ == '__main__':
    unittest.main()
