"""The Jupiter gate and one breaker outcome per operation (2026-10-01).

At 20:33Z one pre-open swap got Jupiter 429 on all three attempts. Each attempt
counted as a failure, so one operation tripped the swap breaker. Every process
on the host shares Jupiter's per-IP limit; the gate gives out one request slot
at a time across all of them, Python and Node alike."""
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import config      # noqa: E402
import db          # noqa: E402
import health      # noqa: E402
import jupgate     # noqa: E402
import rebalancer  # noqa: E402
from test_deploy_all import bal, SOL, USDC  # noqa: E402

ROOT = pathlib.Path(rebalancer.__file__).resolve().parent


class Clock:
    """A fake clock whose sleep advances time."""

    def __init__(self, t=1000.0):
        self.t = t
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Gate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.gate = os.path.join(self.dir, 'jup.gate')

    def test_slots_are_spaced(self):
        c = Clock()
        slots = [jupgate.reserve(self.gate, 1.1, c.now, c.sleep) for _ in range(5)]
        self.assertEqual(slots[0], 1000.0)
        for a, b in zip(slots, slots[1:]):
            self.assertAlmostEqual(b - a, 1.1, places=6)
        self.assertFalse(os.path.exists(self.gate + '.lock'))          # the lock is never left behind

    def test_wait_turn_sleeps_until_the_slot(self):
        c = Clock()
        jupgate.wait_turn(self.gate, 1.1, c.now, c.sleep)
        jupgate.wait_turn(self.gate, 1.1, c.now, c.sleep)
        self.assertEqual(len(c.slept), 1); self.assertAlmostEqual(c.slept[0], 1.1, places=6)
        c.t += 10
        jupgate.wait_turn(self.gate, 1.1, c.now, c.sleep)
        self.assertEqual(len(c.slept), 1)                              # a free slot: no wait

    def test_a_garbage_or_far_future_gate_file_is_ignored(self):
        c = Clock()
        for junk in ('', 'abc', '-5', '99999999999'):
            pathlib.Path(self.gate).write_text(junk)
            self.assertEqual(jupgate.reserve(self.gate, 1.1, c.now, c.sleep), c.t, junk)

    def test_a_stale_lock_is_cleared_and_a_live_one_times_out(self):
        c = Clock(time.time())
        lock = self.gate + '.lock'
        pathlib.Path(lock).write_text('')
        os.utime(lock, (c.t - 10, c.t - 10))                           # a crashed holder
        self.assertEqual(jupgate.reserve(self.gate, 1.1, c.now, c.sleep), c.t)
        pathlib.Path(lock).write_text('')
        os.utime(lock, (c.t + 3600, c.t + 3600))                       # a live holder that never lets go
        t = jupgate.reserve(self.gate, 1.1, c.now, c.sleep)
        self.assertGreater(c.t - 1000, 0); self.assertAlmostEqual(t, c.t)  # went on without a slot, no raise
        self.assertGreaterEqual(sum(c.slept), jupgate.WAIT_S)

    def test_an_unwritable_place_never_raises(self):
        c = Clock()
        self.assertEqual(jupgate.reserve('/nonexistent-dir/x.gate', 1.1, c.now, c.sleep), c.t)
        jupgate.wait_turn('/nonexistent-dir/x.gate', 1.1, c.now, c.sleep)

    def test_python_and_node_share_the_gate(self):
        """Five processes, two languages: every slot at least the spacing apart."""
        env = dict(os.environ, LPBOT_JUP_GATE=self.gate, LPBOT_JUP_SPACING_MS='40')
        py = ("import jupgate, json; print(json.dumps([jupgate.reserve() for _ in range(4)]))")
        js = ("import { reserve } from './jupiter_gate.mjs'; const o = [];"
              "for (let i = 0; i < 4; i++) o.push((await reserve()) / 1000); console.log(JSON.stringify(o));")
        procs = [subprocess.Popen([sys.executable, '-c', py], cwd=ROOT, env=env, stdout=subprocess.PIPE, text=True)
                 for _ in range(3)]
        procs += [subprocess.Popen(['node', '--input-type=module', '-e', js], cwd=ROOT, env=env,
                                   stdout=subprocess.PIPE, text=True) for _ in range(2)]
        slots = []
        for p in procs:
            out, _ = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
            slots += json.loads(out.strip().splitlines()[-1])
        slots.sort()
        self.assertEqual(len(slots), 20)
        gaps = [b - a for a, b in zip(slots, slots[1:])]
        self.assertGreaterEqual(min(gaps), 0.040 - 1e-6, gaps)

    def test_only_jupiter_requests_take_a_slot(self):
        seen = []
        import dexes
        with mock.patch.object(dexes.jupgate, 'wait_turn', lambda *a, **k: seen.append(1)), \
                mock.patch.object(dexes.subprocess, 'run', lambda *a, **k: mock.Mock(stdout='{}')):
            dexes._get('https://lite-api.jup.ag/price/v3?ids=x')
            dexes._get('https://api.geckoterminal.com/x')
            dexes._get('https://api.jup.ag/swap/v1/quote')
        self.assertEqual(len(seen), 2)

    # --- exact edges of the lock and the slot (mutation gaps, 2026-10-01) ---

    def script(self, *times):
        """A now() that answers `times` in order and fails the test past the end."""
        it = iter(times)
        def now():
            try:
                return next(it)
            except StopIteration:
                raise AssertionError('now() called more often than scripted') from None
        return now

    def held(self, mtime=1000.0):
        lock = self.gate + '.lock'
        pathlib.Path(lock).write_text('')
        os.utime(lock, (mtime, mtime))
        return lock

    def test_the_lock_is_private_to_the_owner(self):
        lock = self.gate + '.lock'
        self.assertIs(jupgate._take(lock, time.time, lambda s: None), True)
        self.assertEqual(stat.S_IMODE(os.stat(lock).st_mode), 0o600)

    def test_a_wait_of_exactly_wait_s_tries_once_more(self):
        lock = self.held(mtime=2000.0)                                  # a live holder: never stale here
        slept = []
        t0 = 1000.0
        now = self.script(t0, t0, t0 + jupgate.WAIT_S, t0, t0 + jupgate.WAIT_S + 0.01)
        self.assertIs(jupgate._take(lock, now, slept.append), False)
        self.assertEqual(slept, [0.02])                                # one more try at exactly WAIT_S
        self.assertTrue(os.path.exists(lock))                          # the holder's lock is left alone

    def test_a_lock_exactly_stale_s_old_is_still_held(self):
        lock = self.held(mtime=1000.0)
        t = 1000.0 + jupgate.STALE_S
        now = self.script(t, t, t + jupgate.WAIT_S + 1)
        self.assertIs(jupgate._take(lock, now, lambda s: None), False)
        self.assertTrue(os.path.exists(lock))

    def test_a_lock_just_past_stale_s_is_removed_and_taken(self):
        lock = self.held(mtime=1000.0)
        t = 1000.0 + jupgate.STALE_S + 0.001
        self.assertIs(jupgate._take(lock, self.script(t, t), lambda s: None), True)
        self.assertGreater(os.path.getmtime(lock), 1000.0)             # a new lock, ours

    def test_no_writable_directory_is_false_not_none(self):
        self.assertIs(jupgate._take('/nonexistent-dir/x.lock', time.time, lambda s: None), False)

    def test_a_lock_not_taken_leaves_the_gate_and_the_lock_alone(self):
        pathlib.Path(self.gate).write_text('1000.500000')
        lock = self.held()
        c = Clock(1000.0)
        with mock.patch.object(jupgate, '_take', lambda *a: False):
            self.assertEqual(jupgate.reserve(self.gate, 1.1, c.now, c.sleep), 1000.0)
        self.assertEqual(pathlib.Path(self.gate).read_text(), '1000.500000')
        self.assertTrue(os.path.exists(lock))                          # another holder's lock: never removed

    def test_a_slot_exactly_an_hour_ahead_is_kept(self):
        c = Clock(1000.0)
        pathlib.Path(self.gate).write_text(f'{1000.0 + 3600:.6f}')
        self.assertAlmostEqual(jupgate.reserve(self.gate, 1.1, c.now, c.sleep), 4601.1, places=6)
        pathlib.Path(self.gate).write_text(f'{1000.0 + 3600.5:.6f}')
        self.assertEqual(jupgate.reserve(self.gate, 1.1, c.now, c.sleep), 1000.0)   # past the hour: garbage

    def test_a_missing_or_empty_gate_file_is_slot_zero(self):
        for content in (None, ''):
            if content is None:
                if os.path.exists(self.gate):
                    os.unlink(self.gate)
            else:
                pathlib.Path(self.gate).write_text(content)
            c = Clock(0.0)
            self.assertAlmostEqual(jupgate.reserve(self.gate, 1.1, c.now, c.sleep), 1.1, places=9, msg=repr(content))

    def test_a_delay_under_a_second_is_slept_and_zero_is_not(self):
        c = Clock(1000.0)
        pathlib.Path(self.gate).write_text('999.500000')
        jupgate.wait_turn(self.gate, 1.0, c.now, c.sleep)
        self.assertEqual(len(c.slept), 1); self.assertAlmostEqual(c.slept[0], 0.5, places=6)
        c = Clock(1000.0)
        pathlib.Path(self.gate).write_text('999.000000')
        jupgate.wait_turn(self.gate, 1.0, c.now, c.sleep)
        self.assertEqual(c.slept, [])                                  # a delay of exactly 0

    def test_get_parses_curl_with_a_40_s_limit(self):
        import dexes
        seen = []
        def run(argv, **k):
            seen.append((argv, k)); return mock.Mock(stdout='{"a": 1}')
        with mock.patch.object(dexes.subprocess, 'run', run):
            self.assertEqual(dexes._get('https://api.geckoterminal.com/x'), {'a': 1})
        argv, k = seen[0]
        self.assertEqual(argv[argv.index('--max-time') + 1], '40')
        self.assertEqual(k, {'capture_output': True, 'text': True})

    def test_every_jupiter_call_site_takes_a_slot(self):
        for f in ('signer_pancake.mjs', 'signer_dlmm.mjs', 'signer_byreal.mjs', 'signer_raydium.mjs'):
            src = (ROOT / f).read_text()
            self.assertIn("import { waitTurn } from './jupiter_gate.mjs';", src, f)
            body = src[src.index('async function tokenUsd(mint) {'):]
            self.assertLess(body.index('await waitTurn()'), body.index('JUPITER}/price'), f)
        sw = (ROOT / 'swap_jupiter.mjs').read_text()
        jf = sw[sw.index('async function jfetch'):]
        self.assertLess(jf.index('await waitTurn()'), jf.index('await fetch(url, init)'))
        for f in ROOT.glob('*.mjs'):
            src = f.read_text()
            if 'lite-api.jup.ag' in src and f.name not in ('jupiter_gate.mjs',):
                self.assertIn('waitTurn', src, f'{f.name} talks to Jupiter without the gate')


def clear():
    with db.cursor(commit=True) as cur:
        cur.execute('truncate health')


class OneOutcomePerSwap(unittest.TestCase):
    REC = {'token_a': {'address': SOL, 'decimals': 9, 'symbol': 'SOL'}, 'token_b': {'address': USDC, 'decimals': 6, 'symbol': 'USDC'}}
    J429 = (None, 'Jupiter rate limited: ERROR: Jupiter 429 on /swap/v1/quote: Rate limit exceeded')
    FALLBACK = ''                                                   # Jupiter alone, unless a subclass says otherwise

    def setUp(self):
        clear()
        p = mock.patch.object(rebalancer, 'SWAP_FALLBACK', self.FALLBACK); p.start(); self.addCleanup(p.stop)
        for n, v in (('DEPLOY_ALL', True), ('MAX_USD', 300.0), ('SIDE_CAP_FRACTION', 0.55), ('GAS_RESERVE_SOL', 0.05),
                     ('CAPITAL_USD', 190.0), ('PAYOUT_ENABLED', False), ('REBALANCE_SWAP', True)):
            p = mock.patch.object(config, n, v); p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        clear()

    def go(self, answers):
        calls, sleeps = [], []
        it = iter(answers)
        b = bal(0.2, 200.0)

        def fake(*a, **k):
            calls.append(k); out, err = next(it)
            if k.get('record', True):                                  # what the real chain would do
                rebalancer.record_health(rebalancer.health_key(a, k.get('dex')), out, err)
            return out, err
        with mock.patch.object(rebalancer, 'chain', fake), \
                mock.patch.object(rebalancer, 'wallet', lambda p: b), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch('builtins.print'), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: sleeps.append(s)):
            rebalancer.balance_wallet({'failures': 0}, dict(b), self.REC)
        return calls, sleeps

    def test_replay_2026_10_01_three_refusals_are_one_failure(self):
        calls, sleeps = self.go([self.J429] * 3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(k.get('record') is False for k in calls))
        self.assertEqual(db.health_get('jupiter')['fails'], 1)
        self.assertEqual(db.health_get('swap')['fails'], 1)
        self.assertEqual(health.verdict(db.health_get('swap'), time.time())[0], health.BACKOFF)   # yellow, not red
        self.assertEqual(sleeps, list(rebalancer.SWAP_RATE_LIMIT_PAUSES))

    def test_a_late_success_is_a_success(self):
        health.record_failure('swap', 'earlier', now=time.time() - 3600)
        calls, _ = self.go([self.J429, self.J429, ({'sent': True, 'signature': 'S'}, None)])
        self.assertEqual(db.health_get('swap')['fails'], 0)

    def test_other_transport_errors_keep_the_short_pauses(self):
        _, sleeps = self.go([(None, 'RPC timeout')] * 3)
        self.assertEqual(sleeps, list(rebalancer.SWAP_RETRY_PAUSES))
        self.assertEqual(db.health_get('swap')['fails'], 1)

    def test_a_refusal_after_a_rate_limit_still_counts_once(self):
        self.go([self.J429, (None, 'custom program error: 0x1')])
        self.assertEqual(db.health_get('swap')['fails'], 1)

    def test_the_pauses_wait_out_a_minute_window(self):
        self.assertGreaterEqual(sum(rebalancer.SWAP_RATE_LIMIT_PAUSES), 60)
        self.assertGreater(sum(rebalancer.SWAP_RATE_LIMIT_PAUSES), sum(rebalancer.SWAP_RETRY_PAUSES))



class OrcaFallback(OneOutcomePerSwap):
    """When Jupiter fails without sending, the swap is tried once on Orca."""
    FALLBACK = 'orca-swap'
    OK = ({'sent': True, 'signature': 'ORCA', 'routePlan': ['Orca Whirlpool']}, None)
    # the Jupiter-only tests run in the base class, with the fallback off
    test_replay_2026_10_01_three_refusals_are_one_failure = None
    test_a_late_success_is_a_success = None
    test_other_transport_errors_keep_the_short_pauses = None
    test_a_refusal_after_a_rate_limit_still_counts_once = None
    test_the_pauses_wait_out_a_minute_window = None

    def go(self, answers):
        self.seen = []
        calls, sleeps = super().go(answers)
        return calls, sleeps

    def dexes(self, calls):
        return [k.get('dex') for k in calls]

    def test_replay_2026_10_01_jupiter_refused_orca_swaps(self):
        calls, _ = self.go([self.J429] * 3 + [self.OK])
        self.assertEqual(self.dexes(calls), ['jupiter'] * 3 + ['orca-swap'])
        self.assertIs(calls[-1].get('record'), False)
        self.assertEqual(db.health_get('jupiter')['fails'], 1)                    # Jupiter's own health: yellow
        self.assertEqual(db.health_get('swap')['fails'], 0)                       # the bot could swap: green

    def test_never_after_jupiter_sent_anything(self):
        for sent in (({'sent': True, 'signature': 'J'}, None), ({'signature': 'J', 'partial': True}, 'partial transaction execution'),
                     ({'signature': 'J'}, 'could not confirm')):
            clear()
            calls, _ = self.go([sent])
            self.assertEqual(self.dexes(calls), ['jupiter'], sent)               # a second swap could double the trade

    def test_never_after_our_own_refusal_or_halt(self):
        for err in ('refused: bad mint', 'HALT present: stop'):
            clear()
            calls, _ = self.go([(None, err)])
            self.assertEqual(self.dexes(calls), ['jupiter'], err)

    def test_never_when_jupiter_says_noop(self):
        calls, _ = self.go([({'noop': True}, None)])
        self.assertEqual(self.dexes(calls), ['jupiter'])

    def test_both_failing_is_one_swap_failure(self):
        calls, _ = self.go([self.J429] * 3 + [(None, 'Orca quote: no liquidity')])
        self.assertEqual(self.dexes(calls), ['jupiter'] * 3 + ['orca-swap'])
        self.assertEqual(db.health_get('swap')['fails'], 1)
        self.assertEqual(db.health_get('swap')['last_error'], 'Orca quote: no liquidity')

    def test_a_route_error_falls_back_at_once(self):
        calls, sleeps = self.go([(None, 'simulation failed: {"InstructionError":[5,{"Custom":11}]}'), self.OK])
        self.assertEqual(self.dexes(calls), ['jupiter', 'orca-swap'])
        self.assertFalse(set(sleeps) & set(rebalancer.SWAP_RETRY_PAUSES + rebalancer.SWAP_RATE_LIMIT_PAUSES))   # no retry wait

    def test_off_means_jupiter_alone(self):
        with mock.patch.object(rebalancer, 'SWAP_FALLBACK', ''):
            calls, _ = self.go([self.J429] * 3)
        self.assertEqual(self.dexes(calls), ['jupiter'] * 3)

    def test_a_fallback_that_is_no_signer_is_never_called(self):
        with mock.patch.object(rebalancer, 'SWAP_FALLBACK', 'no-such-signer'):
            calls, _ = self.go([self.J429] * 3)
        self.assertEqual(self.dexes(calls), ['jupiter'] * 3)

    def test_no_answer_and_no_error_falls_back(self):
        calls, _ = self.go([(None, None), self.OK])
        self.assertEqual(self.dexes(calls), ['jupiter', 'orca-swap'])
        self.assertEqual(db.health_get('swap')['fails'], 0)

    def test_an_answer_with_an_error_and_nothing_sent_falls_back(self):
        calls, _ = self.go([({'quoted': True}, 'route not found'), self.OK])
        self.assertEqual(self.dexes(calls), ['jupiter', 'orca-swap'])

    def test_never_after_a_partial_without_a_signature(self):
        calls, _ = self.go([({'partial': True}, 'partial transaction execution')])
        self.assertEqual(self.dexes(calls), ['jupiter'])                       # part of it may have landed

    def test_the_fallback_script_is_a_registered_signer(self):
        self.assertTrue(rebalancer.SIGNERS['orca-swap'].endswith('swap_orca.mjs'))
        self.assertNotIn('orca-swap', rebalancer.config.EXECUTE_DEXES)                # never a venue


class OneOutcomePerClose(unittest.TestCase):
    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def close(self, answers, after_close):
        calls = []
        it = iter(answers)
        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [], 'failures': 0}

        def fake(*a, **k):
            calls.append((a[0], k)); out, err = next(it)
            if k.get('record', True):
                rebalancer.record_health(rebalancer.health_key(a, k.get('dex') or config.DEX), out, err)
            return out, err
        with mock.patch.object(rebalancer, 'chain', fake), \
                mock.patch.object(rebalancer, 'read_status', lambda *a: (after_close, None)), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'notify_book', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'reopen', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'halt', lambda r: None), \
                mock.patch.object(rebalancer, 'distribute', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'record_harvest', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'close_position', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'snapshot', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'wallet', lambda p: {'walletUsd': 10.0}), \
                mock.patch.object(rebalancer, 'band_profile', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'measured_fees', lambda out, st, a, b, u: (a, b, u)), \
                mock.patch.object(rebalancer, 'distribute_rewards', lambda *a, **k: None), \
                mock.patch('builtins.print'), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            rebalancer.rebalance(state, {'positionMint': 'M', 'whirlpool': 'P', 'price': 100, 'feesAccruedA': 0.0,
                                         'feesAccruedB': 0.0, 'feesAccrued_USD': 0.0}, 'price went below')
        return [k for c, k in calls if c == 'close']

    def venue(self):
        return db.health_get(f'venue:{config.DEX}') or {}

    def test_a_retried_close_that_fails_is_one_failure(self):
        rl = (None, 'RPC rate limited')
        closes = self.close([({'signature': 'h'}, None), rl, rl], after_close={'positionMint': 'M'})
        self.assertEqual(len(closes), 2); self.assertTrue(all(k.get('record') is False for k in closes))
        self.assertEqual(self.venue()['fails'], 1)

    def test_a_close_that_landed_despite_the_error_is_a_success(self):
        health.record_failure(f'venue:{config.DEX}', 'earlier', now=time.time() - 3600)
        self.close([({'signature': 'h'}, None), (None, 'custom program error: 0x1')], after_close={'positionMint': None})
        self.assertEqual(self.venue()['fails'], 0)

    def test_a_clean_close_is_a_success(self):
        health.record_failure(f'venue:{config.DEX}', 'earlier', now=time.time() - 3600)
        self.close([({'signature': 'h'}, None), ({'closed': 'M', 'signature': 'c'}, None)], after_close=None)
        self.assertEqual(self.venue()['fails'], 0)


class RecordHealth(unittest.TestCase):
    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def test_no_key_records_nothing(self):
        rebalancer.record_health(None, None, 'boom')
        self.assertEqual(db.health_all(), [])

    def test_chain_records_unless_told_not_to(self):
        with mock.patch.object(rebalancer, '_chain', lambda *a, **k: (None, 'boom')), mock.patch('builtins.print'):
            rebalancer.chain('open', 'M', dex='orca')
            rebalancer.chain('open', 'M', dex='orca', record=False)
        self.assertEqual(db.health_get('venue:orca')['fails'], 1)


    def test_an_empty_key_feeds_no_breaker(self):
        seen = []
        with mock.patch.object(rebalancer.health, 'record_failure', lambda *a, **k: seen.append('f')), \
                mock.patch.object(rebalancer.health, 'record_success', lambda *a, **k: seen.append('s')):
            for key in (None, ''):
                rebalancer.record_health(key, None, 'boom')
                rebalancer.record_health(key, {'signature': 'S'}, None)
        self.assertEqual(seen, [])

    def earlier(self, key='k'):
        health.record_failure(key, 'earlier', now=time.time() - 3600)

    def outcome(self, out, err, key='k'):
        with mock.patch('builtins.print'):
            rebalancer.record_health(key, out, err)
        return db.health_get(key) or {}

    def test_no_answer_and_no_error_is_a_failure_with_no_result(self):
        rec = self.outcome(None, None)
        self.assertEqual((rec['fails'], rec['last_error']), (1, 'no result'))

    def test_an_answer_without_an_error_is_a_success(self):
        self.earlier()
        self.assertEqual(self.outcome({'answer': 1}, None)['fails'], 0)

    def test_an_answer_with_an_error_and_no_signature_is_a_failure(self):
        rec = self.outcome({'answer': 1}, 'route not found')
        self.assertEqual((rec['fails'], rec['last_error']), (1, 'route not found'))

    def test_a_signature_or_a_noop_beside_an_error_is_a_success(self):
        for out in ({'signature': 'S'}, {'noop': True}):
            self.earlier()
            self.assertEqual(self.outcome(out, 'could not confirm')['fails'], 0, out)

    def test_our_own_refusal_feeds_nothing(self):
        self.earlier()
        rec = self.outcome(None, 'refused: bad mint')
        self.assertEqual((rec['fails'], rec['last_error']), (1, 'earlier'))   # neither a failure nor a success

    def test_chain_defaults_the_timeout_and_the_dex(self):
        seen = []
        def fake(*a, **k):
            seen.append(k); return {'signature': 'S'}, None
        with mock.patch.object(rebalancer, '_chain', fake), mock.patch.object(config, 'DEX', 'raydium-clmm'):
            rebalancer.chain('open', 'M')
            rebalancer.chain('open', 'M', dex='orca', timeout=7)
        self.assertEqual((seen[0]['dex'], seen[0]['timeout']), ('raydium-clmm', 420))
        self.assertEqual((seen[1]['dex'], seen[1]['timeout']), ('orca', 7))
        self.assertIsNotNone(db.health_get('venue:raydium-clmm'))


class LessJupiterTraffic(unittest.TestCase):
    """Token facts are cached; dust gets no token search (2026-10-01)."""

    def setUp(self):
        import dexes
        self.dexes = dexes
        dexes._TOKEN_FACTS.clear()

    tearDown = setUp

    def test_facts_are_cached_for_six_hours_and_misses_are_not(self):
        calls = []
        def fake(m):
            calls.append(m); return {'verified': True} if m != 'MISS' else None
        with mock.patch.object(self.dexes, '_jupiter_token', fake):
            t = 1_000_000.0
            self.assertEqual(self.dexes.jupiter_token('A', now=t), {'verified': True})
            self.assertEqual(self.dexes.jupiter_token('A', now=t + 6 * 3600 - 1), {'verified': True})
            self.assertEqual(calls, ['A'])
            self.dexes.jupiter_token('A', now=t + 6 * 3600)                   # expired: asked again
            self.assertIsNone(self.dexes.jupiter_token('MISS', now=t)); self.dexes.jupiter_token('MISS', now=t)
        self.assertEqual(calls, ['A', 'A', 'MISS', 'MISS'])

    def test_the_cache_is_bounded(self):
        with mock.patch.object(self.dexes, '_jupiter_token', lambda m: {'m': m}), \
                mock.patch.object(self.dexes, 'TOKEN_FACTS_MAX', 3):
            for i in range(5):
                self.dexes.jupiter_token(f'M{i}', now=float(i))
        self.assertEqual(sorted(self.dexes._TOKEN_FACTS), ['M2', 'M3', 'M4'])           # the oldest left first

    def test_the_sweep_asks_only_about_tokens_worth_sweeping(self):
        asked = []
        accounts = [{'mint': 'DUST', 'amount': 5, 'decimals': 6}, {'mint': 'RICH', 'amount': 3_000_000, 'decimals': 6},
                    {'mint': 'NOPRICE', 'amount': 10 ** 9, 'decimals': 6}]
        with mock.patch.object(rebalancer, 'pool_tokens', lambda: (('SOLM', 'SOL'), ('USDCM', 'USDC'))), \
                mock.patch.object(rebalancer.audit, 'token_accounts', lambda url, owner: accounts), \
                mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda ms: {'DUST': 1.0, 'RICH': 1.0}), \
                mock.patch.object(rebalancer.dexes, 'jupiter_token', lambda m: asked.append(m) or {'verified': False}), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None):
            rebalancer.sweep_foreign({'last_sweep': 0}, {'owner': 'OWNER', 'balanceA': 1, 'price': 100})
        self.assertEqual(asked, ['RICH'])



class BreakerProbe(unittest.TestCase):
    """A breaker past its cooldown shows yellow and a read-only quote clears it (2026-10-01)."""

    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def trip(self, key, at):
        for _ in range(3):
            health.record_failure(key, 'Jupiter 429', now=at)

    def probe(self, answer, now):
        seen = []
        def fake():
            seen.append(1)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(rebalancer, 'jupiter_answers', fake), mock.patch('builtins.print'):
            out = rebalancer.probe_breakers(now)
        return out, len(seen)

    def test_the_light_follows_the_cooldown(self):
        t = 1_000_000.0
        self.trip('swap', t)
        self.assertEqual(health.verdict(db.health_get('swap'), t + 1)[0], health.TRIPPED)
        self.assertEqual(health.verdict(db.health_get('swap'), t + 2400)[:2], (health.PROBING, True))
        self.assertEqual(health.EMOJI[health.PROBING], '🟡')

    def test_replay_2026_10_01_the_probe_clears_a_cooled_swap_breaker(self):
        t = 1_000_000.0
        self.trip('swap', t)
        self.assertEqual(self.probe((True, None), t + 100), (None, 0))                   # still cooling: no request
        self.assertEqual(self.probe((True, None), t + 2401), (True, 1))
        self.assertEqual(db.health_get('swap')['fails'], 0)
        self.assertEqual(self.probe((True, None), t + 2500), (None, 0))                  # nothing due: no request

    def test_a_failed_probe_counts_for_jupiter_only(self):
        t = 1_000_000.0
        self.trip('swap', t); self.trip('jupiter', t)
        out, n = self.probe((False, 'Jupiter 429'), t + 2401)
        self.assertEqual((out, n), (False, 1))
        self.assertEqual(db.health_get('jupiter')['fails'], 4)                           # backoff grows
        self.assertEqual(db.health_get('swap')['fails'], 3)                              # Orca may still swap
        self.assertEqual(self.probe((True, None), t + 2402), (None, 0))                  # Jupiter cooling: no request

    def test_both_due_and_both_cleared(self):
        t = 1_000_000.0
        self.trip('swap', t); self.trip('jupiter', t)
        self.assertEqual(self.probe((True, None), t + 2401), (True, 1))
        self.assertEqual((db.health_get('swap')['fails'], db.health_get('jupiter')['fails']), (0, 0))

    def test_a_venue_breaker_is_never_probed(self):
        t = 1_000_000.0
        self.trip('venue:orca', t)
        self.assertEqual(self.probe((True, None), t + 2401), (None, 0))
        self.assertEqual(db.health_get('venue:orca')['fails'], 3)

    def test_the_probe_never_raises(self):
        t = 1_000_000.0
        self.trip('swap', t)
        self.assertIsNone(self.probe(RuntimeError('boom'), t + 2401)[0])
        with mock.patch.object(rebalancer.health, 'allowed', side_effect=RuntimeError('db')), mock.patch('builtins.print'):
            self.assertIsNone(rebalancer.probe_breakers(t))

    def test_jupiter_answers_reads_the_quote(self):
        for d, ok in (({'outAmount': '1180000'}, True), ({'outAmount': '0'}, False), ({'error': 'Rate limit'}, False),
                      (None, False), ([], False), ({'outAmount': 'x'}, False)):
            with mock.patch.object(rebalancer.dexes, '_get', lambda url, **k: d):
                self.assertEqual(rebalancer.jupiter_answers()[0], ok, d)
        with mock.patch.object(rebalancer.dexes, '_get', side_effect=ValueError('bad json')):
            self.assertEqual(rebalancer.jupiter_answers()[0], False)
        self.assertIn('/swap/v1/quote?', rebalancer.PROBE_QUOTE); self.assertIn('amount=10000000', rebalancer.PROBE_QUOTE)

    def test_failover_still_follows_the_failure_count(self):
        t = time.time()
        self.trip('venue:raydium-clmm', t - 10 * 3600)                                   # long cooled: yellow
        with mock.patch.object(rebalancer.config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(rebalancer, 'venue_view', lambda p, q=1.0: []), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: self.__dict__.setdefault('ev', []).append(ev)), \
                mock.patch.object(rebalancer, 'save', lambda s: None), mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            rebalancer.venue_failover({}, None, price=120.0, quote=1.0)    # an unknown quote price moves nothing
        self.assertIn('failover_none', self.ev)                                          # it looked for a target

    def test_the_loop_probes_every_poll(self):
        src = (ROOT / 'rebalancer.py').read_text()
        i = src.index('        run_audits(state)\n')
        self.assertEqual(src[i:i + 60].split('\n')[1].strip(), 'probe_breakers()')


if __name__ == '__main__':
    unittest.main()
