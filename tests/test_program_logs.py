"""A signer's program failure, decoded and with its log tail, reaches the
loop's error, the events table and the feed (2026-10-12: test0's open said
only "Instruction 7: custom program error: 0x178b").

The signer side is shared/program_errors.mjs (tests/test_program_errors.mjs);
here the loop's side, and one run of the real JS to hold both to one format."""
import json
import pathlib
import random
import string
import subprocess
import time
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import db  # noqa: E402
import guards  # noqa: E402
import lp.books  # noqa: E402
import lp.capital  # noqa: E402
import lp.harvest  # noqa: E402
import lp.moves  # noqa: E402
import lp.paths  # noqa: E402
import lp.signers  # noqa: E402

ROOT = lp.paths.ROOT
DLMM = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'
LINE = ('Simulation failed. Message: Transaction simulation failed: Error processing Instruction 7: custom program error: 0x178b. '
        '| program error: Meteora DLMM InvalidBinArray (6027 = 0x178b) at instruction 7 '
        f'| logs: Program {DLMM} invoke [2] ; Program log: AnchorError occurred. Error Code: InvalidBinArray. '
        f'Error Number: 6027. Error Message: Invalid bin array. ; Program {DLMM} failed: custom program error: 0x178b')


class ProgramLogs(unittest.TestCase):
    def test_the_log_part_is_split_back_into_lines(self):
        self.assertEqual(lp.books.program_logs(LINE), [
            f'Program {DLMM} invoke [2]',
            'Program log: AnchorError occurred. Error Code: InvalidBinArray. Error Number: 6027. Error Message: Invalid bin array.',
            f'Program {DLMM} failed: custom program error: 0x178b'])
        self.assertEqual(lp.books.program_logs('ERROR: 429 Too Many Requests'), [])
        self.assertEqual(lp.books.program_logs(None), [])
        self.assertEqual(lp.books.program_logs('x | logs: '), [])

    def test_bounded_and_redacted_whatever_the_text(self):
        secret = 'k' * 24
        rnd = random.Random(7)
        for _ in range(300):
            lines = [''.join(rnd.choice(string.printable.replace('\n', '').replace('|', ''))
                             for _ in range(rnd.randrange(0, 400))) for _ in range(rnd.randrange(0, 40))]
            if lines and rnd.random() < 0.5:
                lines[rnd.randrange(len(lines))] += f' https://rpc.x/?api-key={secret}'
            text = 'ERROR: x | logs: ' + ' ; '.join(lines) + ('\nnext line' if rnd.random() < 0.5 else '')
            got = lp.books.program_logs(text)
            self.assertLessEqual(len(got), lp.books.LOG_LINES)
            for ln in got:
                self.assertLessEqual(len(ln), lp.books.LOG_LINE_CHARS)
                self.assertNotIn(secret, ln)
                self.assertNotIn('\n', ln)
            want = [ln.strip() for ln in ' ; '.join(lines).split(' ; ') if ln.strip()][-lp.books.LOG_LINES:]
            self.assertEqual(len(got), len(want))

    def test_exact_bounds(self):
        exact, long = 'L' * lp.books.LOG_LINE_CHARS, 'M' * 300
        self.assertEqual(lp.books.program_logs(f'x | logs: {exact} ; {long}'),
                         [exact, 'M' * (lp.books.LOG_LINE_CHARS - 1) + '…'])

    def test_tidy_says_the_decoded_error_first(self):
        self.assertEqual(lp.books.tidy(LINE), 'program error: Meteora DLMM InvalidBinArray (6027 = 0x178b) at instruction 7')
        slip = 'PriceSlippageCheck (6017): price moved beyond the slippage limit | program error: Raydium CLMM PriceSlippageCheck (6017 = 0x1781) | logs: a'
        self.assertEqual(lp.books.tidy(slip), 'PriceSlippageCheck (6017): price moved beyond the slippage limit')
        long = 'x | program error: ' + 'N' * 400 + ' | logs: a'
        self.assertEqual(len(lp.books.tidy(long)), 140)
        # a refusal on chain with no custom code in the text: its JSON, not a crash
        self.assertEqual(lp.books.tidy('transaction failed on chain: {"InstructionError":[2,{"Custom":1}]}'),
                         'failed on chain: {"InstructionError":[2,{"Custom":1}]}')
        # without the decoded part, as before
        self.assertEqual(lp.books.tidy('Simulation failed: Error processing Instruction 2: custom program error: 0x1771'),
                         'Error processing Instruction 2: custom program error: 0x1771')

    def test_a_decoded_program_error_is_never_retried_as_a_transport_failure(self):
        import re
        t = lp.books.tidy(LINE)
        self.assertNotRegex(t, '(?i)' + lp.moves.CLOSE_RETRY_ERRORS)


class Chain(unittest.TestCase):
    def run_chain(self, stdout, stderr, code=1):
        class R:
            returncode = code
        R.stdout, R.stderr = stdout, stderr
        with mock.patch.object(subprocess, 'run', lambda *a, **k: R()), \
                mock.patch.object(guards, 'signer_args', lambda a: None), \
                mock.patch.object(guards, 'inside', lambda *a: None):
            return lp.signers._chain('open', 'P', '1', '2', '1', '1', '--execute', dex='meteora-dlmm')

    def test_an_error_line_gives_the_decoded_error_and_the_logs(self):
        out, err = self.run_chain('', 'bigint: Failed to load bindings, pure JS will be used\nERROR: ' + LINE + '\n')
        self.assertIsNone(out)
        self.assertEqual(err, 'program error: Meteora DLMM InvalidBinArray (6027 = 0x178b) at instruction 7')
        self.assertEqual(len(lp.signers.failure_logs()), 3)
        self.assertIn('Error Code: InvalidBinArray', lp.signers.failure_detail(err))

    def test_a_partial_report_gives_them_too(self):
        out, err = self.run_chain(json.dumps({'sent': False, 'partial': True, 'signatures': [], 'error': LINE}), '')
        self.assertTrue(out['partial'])
        self.assertTrue(err.startswith('program error: Meteora DLMM InvalidBinArray'))
        self.assertEqual(lp.signers.failure_logs()[-1], f'Program {DLMM} failed: custom program error: 0x178b')

    def test_the_next_call_never_speaks_of_an_earlier_failure(self):
        self.run_chain('', 'ERROR: ' + LINE)
        self.assertTrue(lp.signers.failure_logs())
        self.run_chain(json.dumps({'positionMint': 'M'}), '', code=0)
        self.assertEqual(lp.signers.failure_logs(), [])
        self.assertEqual(lp.signers.LAST_CHAIN_ERROR, {})
        self.run_chain('', 'ERROR: ' + LINE)
        with mock.patch.object(lp.paths, 'halted', lambda: 'HALT'):
            _, err = lp.signers._chain('open', '--execute', dex='meteora-dlmm')
        self.assertEqual(err, 'refused: halted (HALT)')
        self.assertEqual(lp.signers.failure_logs(), [])

    def test_failure_detail(self):
        self.assertEqual(lp.signers.failure_detail('e', ['a', 'b']), 'e | logs: a ; b')
        self.assertEqual(lp.signers.failure_detail('e', []), 'e')
        lp.signers.LAST_CHAIN_ERROR.clear()
        self.assertEqual(lp.signers.failure_detail('e'), 'e')


class RealSignerText(unittest.TestCase):
    """The JS formats, the loop parses: one run of shared/program_errors.mjs."""

    def test_the_js_line_parses(self):
        script = (
            "import { describeFailure } from './shared/program_errors.mjs';"
            "const logs = Array.from({ length: 30 }, (_, i) => `Program log: step ${i} | a ; b`);"
            f"logs.push('Program {DLMM} failed: custom program error: 0x178c');"
            "const e = Object.assign(new Error('Transaction simulation failed: Error processing Instruction 3: custom program error: 0x178c'), { programLogs: logs });"
            "console.error('ERROR:', describeFailure(e));")
        r = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = r.stderr
        self.assertEqual(lp.books.tidy(text),
                         'program error: Meteora DLMM NonContinuousBinArrays (6028 = 0x178c) at instruction 3')
        logs = lp.books.program_logs(text)
        self.assertEqual(len(logs), lp.books.LOG_LINES)
        self.assertEqual(logs[0], 'Program log: step 21 / a, b')
        self.assertEqual(logs[-1], f'Program {DLMM} failed: custom program error: 0x178c')


class CloseFailure(unittest.TestCase):
    """The close path keeps the failing close's logs although the status reads
    after it empty LAST_CHAIN_ERROR, and writes them to the events table and
    the feed."""

    def test_close_failed_carries_the_logs(self):
        events, notes = [], []
        results = iter([({'signature': 'h'}, None), (None, 'program error: Meteora DLMM InvalidBinArray (6027 = 0x178b)')])

        def chain(*a, **k):
            lp.signers.LAST_CHAIN_ERROR.clear()
            res = next(results)
            if res[1]:
                lp.signers.LAST_CHAIN_ERROR.update(args=a[0], text=LINE, logs=lp.books.program_logs(LINE))
            return res

        def status(*a):
            lp.signers.LAST_CHAIN_ERROR.clear()               # a status read is a chain() call
            return {'positionMint': 'M'}, None

        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [], 'failures': 0}
        with mock.patch.object(lp.signers, 'chain', chain), \
                mock.patch.object(lp.signers, 'read_status', status), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: notes.append((ev, kw))), \
                mock.patch.object(lp.books, 'notify_book', lambda ev, **kw: notes.append((ev, kw))), \
                mock.patch.object(lp.capital, 'wallet', lambda p: {}), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.harvest, 'distribute', lambda *a, **k: None), \
                mock.patch.object(lp.harvest, 'distribute_rewards', lambda *a, **k: None), \
                mock.patch.object(time, 'sleep', lambda s: None), \
                mock.patch.object(db, 'record_harvest', lambda *a: None), \
                mock.patch.object(db, 'snapshot', lambda *a, **k: None), \
                mock.patch.object(db, 'event', lambda kind, detail='': events.append((kind, detail))), \
                mock.patch.object(lp.signers, 'record_health', lambda *a, **k: None):
            lp.moves.rebalance(state, {'positionMint': 'M', 'price': 100, 'whirlpool': 'P'}, 'x')
        failed = [kw for ev, kw in notes if ev == 'close_failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['logs'], lp.books.program_logs(LINE))
        detail = dict(events)['close_failed']
        self.assertTrue(detail.startswith('program error: Meteora DLMM InvalidBinArray (6027 = 0x178b) | logs: '), detail)
        self.assertIn('Error Code: InvalidBinArray', detail)


if __name__ == '__main__':
    unittest.main()
