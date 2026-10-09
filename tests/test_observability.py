"""Observability (2026-09-30): the log line and the Telegram message of an
event carry the same emoji (event_emoji.json), every event the loop emits has
one, and the book carries the breakers."""
import ast
import json
import re
import pathlib
import subprocess
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import db  # noqa: E402
import guards  # noqa: E402
import health  # noqa: E402
import lp.books  # noqa: E402
import lp.paths  # noqa: E402
import lp.signers  # noqa: E402

ROOT = lp.paths.ROOT


def emitted_events():
    """Every event name the loop passes to notify / notify_book as a literal."""
    out = set()
    for f in sorted((ROOT / 'lp').glob('*.py')):
        for node in ast.walk(ast.parse(f.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
            if name in ('notify', 'notify_book') \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                out.add(node.args[0].value)
    return out


class Emoji(unittest.TestCase):
    def test_the_map_is_valid_json_of_strings(self):
        m = json.loads((ROOT / 'event_emoji.json').read_text())
        for k, v in m.items():
            self.assertIsInstance(v, str, k)
            self.assertTrue(v.strip(), k)

    def test_python_and_node_agree_on_every_emitted_event(self):
        events = sorted(emitted_events() | {'some_new_failed', 'x_deferred', 'unknown_thing'})
        self.assertGreater(len(events), 40)
        js = ("import fs from 'fs'; import { emojiFor } from './book_format.mjs';"
              "const m = JSON.parse(fs.readFileSync('./event_emoji.json', 'utf8'));"
              f"console.log(JSON.stringify({json.dumps(events)}.map(e => emojiFor(e, m))));")
        r = subprocess.run(['node', '--input-type=module', '-e', js], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        node = json.loads(r.stdout.strip().splitlines()[-1])
        py = [lp.books.emoji_for(e) for e in events]
        self.assertEqual(dict(zip(events, py)), dict(zip(events, node)))

    def test_a_payout_held_for_gas_shows_the_pump_in_python_and_node(self):
        rows = [('PAYOUT', {'gas_low': True}), ('REWARD_PAYOUT', {'gas_low': True}),
                ('PAYOUT', {'gas_low': False}), ('PAYOUT', {})]
        js = ("import fs from 'fs'; import { emojiFor } from './book_format.mjs';"
              "const m = JSON.parse(fs.readFileSync('./event_emoji.json', 'utf8'));"
              f"console.log(JSON.stringify({json.dumps(rows)}.map(([e, r]) => emojiFor(e, m, r))));")
        r = subprocess.run(['node', '--input-type=module', '-e', js], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        py = [lp.books.emoji_for(e, p) for e, p in rows]
        self.assertEqual(py, ['⛽', '⛽', '💸', '💸'])
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), py)

    def test_the_log_line_of_a_held_payout_carries_the_pump(self):
        seen = []
        with mock.patch('builtins.print', lambda *a, **k: seen.append(a[0])), \
                mock.patch.object(lp.paths, 'FEED', pathlib.Path('/dev/null')):
            lp.books.notify('PAYOUT', gas_low=True)
            lp.books.notify('PAYOUT', gas_low=False)
        self.assertIn('] ⛽ PAYOUT: ', seen[0]); self.assertIn('] 💸 PAYOUT: ', seen[1])

    def test_failures_are_marked_as_failures(self):
        for e in emitted_events():
            if re.search(r'fail(?!over)', e, re.I) or e in ('failover_failed', 'status_unreadable', 'BREAKER', 'halted'):
                self.assertIn(lp.books.emoji_for(e), ('❌', '🛑', '📵'), e)

    def test_the_log_line_carries_the_emoji(self):
        seen = []
        with mock.patch('builtins.print', lambda *a, **k: seen.append(a[0])), \
                mock.patch.object(lp.paths, 'FEED', pathlib.Path('/dev/null')):
            lp.books.notify('OPEN', x=1)
            lp.books.notify('something_failed')
        self.assertIn('] 🟩 OPEN: ', seen[0]); self.assertIn('] ❌ something_failed: ', seen[1])

    def test_a_missing_map_falls_back_to_the_rule(self):
        with mock.patch.object(lp.paths, 'ROOT', pathlib.Path('/nonexistent')):
            self.assertEqual(lp.books._load_emoji(), {})


class BookCarriesHealth(unittest.TestCase):
    def test_every_book_has_the_breakers(self):
        sent = []
        with mock.patch.object(db, 'stats', lambda: {}), \
                mock.patch.object(health, 'summary', lambda: [{'key': 'swap', 'state': 'backoff'}]), \
                mock.patch.object(lp.books, 'notify', lambda ev, **p: sent.append(p)):
            lp.books.notify_book('in_band')
        self.assertEqual(sent[0]['health'], [{'key': 'swap', 'state': 'backoff'}])



BIGINT = 'bigint: Failed to load bindings, pure JS will be used (try npm run rebuild?)'
FORBIDDEN = ('ERROR: 403 Forbidden: {"jsonrpc":"2.0","error":{"code":-32602,"message":"Indexed requests require a personal '
             'token. Get one at: https://www.allnodes.com/publicnode"}}')


class TidyKeepsTheCause(unittest.TestCase):
    """2026-09-30: the bigint warning took 76 of 140 characters and the 403's
    cause was cut off."""

    def test_the_403_is_named(self):
        self.assertEqual(lp.books.tidy(BIGINT + '\n' + FORBIDDEN), 'RPC endpoint refuses indexed reads (403: needs a personal token)')
        self.assertEqual(lp.books.tidy('needs a personal token'), 'RPC endpoint refuses indexed reads (403: needs a personal token)')

    def test_a_jupiter_limit_is_jupiter_s(self):
        t = lp.books.tidy(BIGINT + '\nJupiter 429 on /swap/v1/quote: Rate limit exceeded')
        self.assertTrue(t.startswith('Jupiter rate limited: Jupiter 429'), t)
        self.assertTrue(re.search(r'rate limit', t, re.I))           # the swap retry still matches
        t2 = lp.books.tidy('Jupiter 400 on /swap/v1/swap: bad route')
        self.assertEqual(t2, 'Jupiter 400 on /swap/v1/swap: bad route')

    def test_noise_lines_are_dropped_and_the_rest_kept(self):
        self.assertIsNone(lp.books.tidy(BIGINT))
        self.assertIsNone(lp.books.tidy(BIGINT + '\n  ' + BIGINT))
        self.assertEqual(lp.books.tidy(BIGINT + '\nsomething else broke'), 'something else broke')
        self.assertEqual(lp.books.tidy('(node:123) [DEP0040] DeprecationWarning: punycode\nreal error'), 'real error')
        self.assertEqual(lp.books.tidy('(Use `node --trace-deprecation ...`)\nreal error'), 'real error')
        self.assertEqual(lp.books.tidy('not bigint: Failed to load bindings here'), 'not bigint: Failed to load bindings here')

    def test_the_full_error_is_kept_for_the_events_table(self):
        class R:
            returncode, stdout, stderr = 1, '', BIGINT + '\n' + FORBIDDEN
        with mock.patch.object(subprocess, 'run', lambda *a, **k: R()), \
                mock.patch.object(guards, 'signer_args', lambda a: None), \
                mock.patch.object(guards, 'inside', lambda *a: None), \
                mock.patch.dict(lp.signers.SIGNERS, {'jupiter': str(ROOT / 'venues/jupiter/swap.mjs')}):
            out, err = lp.signers._chain('rebalance', 'A', 'B', dex='jupiter')
        self.assertIsNone(out)
        self.assertIn('Indexed requests require a personal token', lp.signers.LAST_CHAIN_ERROR['text'])
        self.assertEqual(lp.signers.LAST_CHAIN_ERROR['args'], 'rebalance')
        self.assertLessEqual(len(lp.signers.LAST_CHAIN_ERROR['text']), 2000)



class TidyProgramErrors(unittest.TestCase):
    def test_anchor_error_message(self):
        e = ('Simulation failed. Message: Transaction simulation failed: Error processing Instruction 3: custom program error: 0x1786. '
             'Logs: ["Program log: AnchorError occurred. Error Code: InvalidTickArray. Error Number: 6022. Error Message: Invalid tick array.", '
             '"Program data: 7XCU..."]')
        self.assertEqual(lp.books.tidy(e), 'program error InvalidTickArray (6022): Invalid tick array')

    def test_the_custom_code_and_the_instruction(self):
        e = ('Simulation failed. Message: Transaction simulation failed: Error processing Instruction 2: custom program error: 0x1771. '
             'Logs: ["Program data: 7XCUabcdefghijklmnopqrstuvwxyz0123456789", "' + 'x' * 400 + '"]')
        self.assertEqual(lp.books.tidy(e), 'Error processing Instruction 2: custom program error: 0x1771')
        self.assertEqual(lp.books.tidy('transaction abc failed on chain: custom program error: 0x1'), 'custom program error: 0x1')

    def test_slippage_still_wins(self):
        e = 'Simulation failed: Error processing Instruction 2: custom program error: 0x1781'
        self.assertEqual(lp.books.tidy(e), 'PriceSlippageCheck (6017): price moved beyond the slippage limit')

    def test_a_json_rpc_message(self):
        self.assertEqual(lp.books.tidy('ERROR: 400 Bad Request: {"jsonrpc":"2.0","error":{"code":-32602,"message":"invalid param: WrongSize"}}'),
                         'invalid param: WrongSize')

    def test_a_bare_code_without_a_failure_is_not_rewritten(self):
        self.assertEqual(lp.books.tidy('custom program error: 0x1 seen in a log'), 'custom program error: 0x1 seen in a log')


if __name__ == '__main__':
    unittest.main()
