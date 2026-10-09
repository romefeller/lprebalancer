"""The loop's pure pieces: sizing, error tidying, signer output parsing."""
import pathlib
import subprocess
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import config      # noqa: E402
import db  # noqa: E402
import dexes  # noqa: E402
import engine  # noqa: E402
import guards  # noqa: E402
import lp.board  # noqa: E402
import lp.books  # noqa: E402
import lp.capital  # noqa: E402
import lp.harvest  # noqa: E402
import lp.moves  # noqa: E402
import lp.signers  # noqa: E402
import lp.tape  # noqa: E402


class DepositCaps(unittest.TestCase):
    # the configured-capital sizing (deploy_all off); tests/test_deploy_all.py covers the default
    def setUp(self):
        p = mock.patch.object(config, 'DEPLOY_ALL', False); p.start(); self.addCleanup(p.stop)

    def bal(self, **kw):
        base = dict(price=100.0, quoteUsd=1.0, balanceA=10.0, balanceB=1000.0,
                    nativeSide='A', tokenA='SOL', tokenB='USDC')
        base.update(kw)
        return base

    def test_flush_wallet_caps_each_side_at_the_fraction(self):
        a, b = lp.capital.deposit_caps(self.bal())
        want = config.CAPITAL_USD * config.SIDE_CAP_FRACTION
        self.assertAlmostEqual(b, want)
        self.assertAlmostEqual(a * 100.0, want)

    def test_gas_reserve_comes_off_the_native_side_only(self):
        a, _ = lp.capital.deposit_caps(self.bal(balanceA=0.6))
        self.assertAlmostEqual(a, 0.6 - config.GAS_RESERVE_SOL - lp.capital.OPEN_RENT_HEADROOM_SOL)
        # native on the B side: reserve leaves A alone
        a2, b2 = lp.capital.deposit_caps(self.bal(price=0.01, quoteUsd=100.0, balanceA=5000.0,
                                                  balanceB=0.6, nativeSide='B',
                                                  tokenA='WIF', tokenB='SOL'))
        self.assertAlmostEqual(b2, 0.6 - config.GAS_RESERVE_SOL - lp.capital.OPEN_RENT_HEADROOM_SOL)
        self.assertAlmostEqual(a2, min(5000.0, config.CAPITAL_USD / 100.0
                                       * config.SIDE_CAP_FRACTION / 0.01))

    def test_non_dollar_quote_is_converted_to_quote_units(self):
        # SOL/cbBTC: price is BTC per SOL, quote worth $100k. Cap B is in BTC.
        _, b = lp.capital.deposit_caps(self.bal(price=0.001, quoteUsd=100_000.0,
                                                balanceA=10.0, balanceB=1.0, nativeSide='A'))
        self.assertAlmostEqual(b, config.CAPITAL_USD / 100_000.0 * config.SIDE_CAP_FRACTION)

    def test_short_wallet_never_goes_negative(self):
        a, b = lp.capital.deposit_caps(self.bal(balanceA=0.01, balanceB=0.0))
        self.assertEqual(a, 0.0); self.assertEqual(b, 0.0)

    def test_no_native_side(self):
        a, b = lp.capital.deposit_caps(self.bal(nativeSide=None, balanceA=2.0, balanceB=50.0))
        self.assertAlmostEqual(a, min(2.0, config.CAPITAL_USD * config.SIDE_CAP_FRACTION / 100))
        self.assertAlmostEqual(b, 50.0)


class Nearest(unittest.TestCase):
    def test_finds_the_closest_rung_within_tolerance(self):
        runs = {3: 'a', 5: 'b', 8: 'c', 12: 'd'}
        self.assertEqual(lp.books.nearest(runs, 6), 'b')
        self.assertEqual(lp.books.nearest(runs, 10), 'c')
        self.assertIsNone(lp.books.nearest(runs, 20))
        self.assertIsNone(lp.books.nearest({}, 5))

    def test_held_band_is_a_property_of_the_band(self):
        # +/-5% opened at 100, price now 118: half-width is still 5%.
        lower, upper = 100 / 1.05, 100 * 1.05
        held = (upper / lower) ** 0.5
        self.assertEqual(round((held - 1) * 100), 5)


class Tidy(unittest.TestCase):
    def test_program_failure_wins_over_earlier_rpc_noise(self):
        for err in ['429 Too Many Requests\nPriceSlippageCheck: price slippage check',
                    '429 earlier; failed on chain: {"InstructionError":[2,{"Custom":6017}]}']:
            self.assertIn('PriceSlippageCheck (6017)', lp.books.tidy(err))
        self.assertNotEqual(lp.books.tidy('transaction abc429xyz failed'), 'RPC rate limited')

    def test_rate_limit_dumps_become_one_line(self):
        wall = 'Error: {"status":429,"headers":{"set-cookie":"x"},"body":"Too Many Requests"} ' * 20
        self.assertEqual(lp.books.tidy(wall), 'RPC rate limited')
        self.assertEqual(lp.books.tidy('socket ECONNRESET'), 'RPC connection reset')
        self.assertEqual(lp.books.tidy('Blockhash not found'), 'blockhash expired')
        self.assertIsNone(lp.books.tidy(''))
        self.assertLessEqual(len(lp.books.tidy('x' * 500)), 140)


class Chain(unittest.TestCase):
    def fake(self, stdout='', stderr='', raise_timeout=False, returncode=0):
        def run(*a, **kw):
            if raise_timeout:
                raise subprocess.TimeoutExpired(a, 1)
            return subprocess.CompletedProcess(a, returncode, stdout, stderr)
        return run

    def test_extracts_json_from_noisy_output(self):
        out = 'bigint: Failed to load bindings\n{"positionMint": "M", "n": 1}\nDRY RUN'
        with mock.patch('subprocess.run', self.fake(stdout=out)):
            j, err = lp.signers.chain('status')
        self.assertEqual(j, {'positionMint': 'M', 'n': 1}); self.assertIsNone(err)

    def test_error_text_without_json_is_tidied(self):
        with mock.patch('subprocess.run', self.fake(stderr='ERROR: 429 Too Many Requests')):
            j, err = lp.signers.chain('status')
        self.assertIsNone(j); self.assertEqual(err, 'RPC rate limited')

    def test_timeout_is_an_error_not_a_crash(self):
        with mock.patch('subprocess.run', self.fake(raise_timeout=True)):
            j, err = lp.signers.chain('status')
        self.assertIsNone(j); self.assertEqual(err, 'signer timed out')

    def test_explicit_empty_position_is_not_an_error(self):
        with mock.patch('subprocess.run', self.fake(stdout='{"positions": 0, "positionMint": null}')):
            j, err = lp.signers.chain('status')
        self.assertIsNotNone(j); self.assertIsNone(j['positionMint'])

    def test_partial_json_result_and_nonzero_exit_are_not_success(self):
        with mock.patch('subprocess.run', self.fake(
                stdout='{"signature":"s", "partial":true, "error":"PriceSlippageCheck"}',
                stderr='earlier RPC 429', returncode=1)):
            out, err = lp.signers.chain('close', 'M', '--execute')
        self.assertEqual(out['signature'], 's')
        self.assertIn('PriceSlippageCheck', err)

    def test_error_json_on_stderr_is_not_a_status(self):
        with mock.patch('subprocess.run', self.fake(stderr='ERROR: {"status":429}', returncode=1)):
            out, err = lp.signers.chain('status')
        self.assertIsNone(out)
        self.assertEqual(err, 'RPC rate limited')


class PositionUsd(unittest.TestCase):
    def test_prefers_the_signers_dollar_figure(self):
        self.assertEqual(lp.capital.position_usd({'positionUsd': 161.9, 'closeEstA': 1, 'closeEstB': 1, 'price': 100}), 161.9)

    def test_falls_back_to_quote_units_times_quote_price(self):
        s = {'closeEstA': 1.0, 'closeEstB': 50.0, 'price': 100.0, 'quoteUsd': 2.0}
        self.assertAlmostEqual(lp.capital.position_usd(s), 300.0)
        self.assertIsNone(lp.capital.position_usd({'price': 100.0}))

    def test_rent_locked_in_the_position_accounts_is_part_of_the_mark(self):
        # 0.2391 SOL of rent at $115 on the first Meteora position: the close
        # refunds it, so the mark must carry it or the open reads as a loss.
        s = {'positionUsd': 194.2, 'rentUsd': 27.5}
        self.assertAlmostEqual(lp.capital.position_usd(s), 221.7)
        s = {'closeEstA': 1.0, 'closeEstB': 50.0, 'price': 100.0, 'quoteUsd': 1.0, 'rentUsd': 2.5}
        self.assertAlmostEqual(lp.capital.position_usd(s), 152.5)




class BoardPick(unittest.TestCase):
    """Which pool to be in, from a board and the held pool's own best."""

    def row(self, address, dex, net, ok=True, skipped=None, a='a', b='b'):
        return {'address': address, 'dex': dex, 'pair': 'SOL/USDC', 'band': 1.08, 'band_pct': 8.0,
                'net_day_pct': net, 'rebal_per_day': 0.1, 'screen_ok': ok, 'skipped': skipped,
                'token_a': {'address': a, 'symbol': 'SOL'}, 'token_b': {'address': b, 'symbol': 'USDC'}}

    def held(self, net=0.386):
        return {'band': 1.18, 'net_day_pct': net,
                'record': {'token_a': {'address': 'a'}, 'token_b': {'address': 'b'}}}

    def with_board(self, rows, fn):
        with mock.patch.object(db, 'latest_scan', lambda max_age_seconds=None: ({'id': 7}, rows)):
            return fn()

    def test_best_other_pool_and_its_gain(self):
        rows = [self.row('M1', 'meteora-dlmm', 0.58), self.row('O1', 'orca', 0.386), self.row('R1', 'raydium-clmm', 0.40)]
        run, best, gain, why = self.with_board(rows, lambda: lp.board.board_pick(self.held(), 'O1'))
        self.assertEqual(best['address'], 'M1')
        self.assertAlmostEqual(gain, (0.58 - 0.386) / 0.386)
        self.assertIsNone(why)

    def test_unscreened_skipped_and_other_pairs_are_not_candidates(self):
        rows = [self.row('X1', 'orca', 0.9, ok=False), self.row('X2', 'orca', 0.8, skipped='thin'),
                self.row('X3', 'orca', 0.7, a='zzz'), self.row('R1', 'raydium-clmm', 0.40)]
        with mock.patch.object(config, 'ALLOW_SWAP', False):
            run, best, gain, why = self.with_board(rows, lambda: lp.board.board_pick(self.held(), 'O1'))
        self.assertEqual(best['address'], 'R1')
        # swaps allowed: the other pair is eligible
        with mock.patch.object(config, 'ALLOW_SWAP', True):
            run, best, gain, why = self.with_board(rows, lambda: lp.board.board_pick(self.held(), 'O1'))
        self.assertEqual(best['address'], 'X3')

    def test_no_board_and_no_candidates(self):
        run, best, gain, why = self.with_board([], lambda: lp.board.board_pick(self.held(), 'O1'))
        self.assertIsNone(best); self.assertEqual(why, 'no fresh board')
        run, best, gain, why = self.with_board([self.row('O1', 'orca', 0.4)],
                                               lambda: lp.board.board_pick(self.held(), 'O1'))
        self.assertIsNone(best); self.assertIn('no eligible', why)

    def test_unscorable_held_pool_still_yields_a_candidate(self):
        rows = [self.row('M1', 'meteora-dlmm', 0.58)]
        run, best, gain, why = self.with_board(rows, lambda: lp.board.board_pick(None, 'O1'))
        self.assertEqual(best['address'], 'M1'); self.assertIsNone(gain)

    def test_consider_migration_recommends_without_a_signer_and_moves_with_one(self):
        rows = [self.row('M1', 'meteora-dlmm', 0.90)]
        sent = []
        moved = []
        with mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **kw: moved.append(kw.get('target'))), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(config, 'POOL_PINNED', False), \
                mock.patch.object(config, 'MIGRATE_MIN_GAIN', 0.5):
            with mock.patch.object(config, 'EXECUTE_DEXES', ('orca',)):
                acted = self.with_board(rows, lambda: lp.board.consider_migration({}, {}, self.held()))
            self.assertFalse(acted)
            self.assertEqual(sent[-1][0], 'MIGRATE_RECOMMENDED')
            self.assertIn('repoint', sent[-1][1]['command'])
            self.assertEqual(moved, [])
            with mock.patch.object(config, 'EXECUTE_DEXES', ('orca', 'meteora-dlmm')):
                acted = self.with_board(rows, lambda: lp.board.consider_migration({}, {}, self.held()))
            self.assertTrue(acted)
            self.assertEqual(sent[-1][0], 'MIGRATE')
            self.assertEqual(moved[-1]['address'], 'M1')
            # under the threshold: checked, not moved
            small = [self.row('M1', 'meteora-dlmm', 0.40)]
            with mock.patch.object(config, 'EXECUTE_DEXES', ('orca', 'meteora-dlmm')):
                acted = self.with_board(small, lambda: lp.board.consider_migration({}, {}, self.held()))
            self.assertFalse(acted); self.assertEqual(sent[-1][0], 'board_checked')
            # pinned: nothing at all
            with mock.patch.object(config, 'POOL_PINNED', True):
                self.assertFalse(self.with_board(rows, lambda: lp.board.consider_migration({}, {}, self.held())))

    def test_chain_dispatches_by_dex(self):
        j, err = lp.signers.chain('status', dex='no-such-dex')
        self.assertIsNone(j); self.assertIn('no signer', err)


class ReadStatus(unittest.TestCase):
    def test_status_never_passes_the_pool_positionally(self):
        calls = []
        with mock.patch.object(lp.signers, 'chain', lambda *a, **kw: calls.append(a) or ({}, None)):
            with mock.patch.object(config, 'DEX', 'orca'):
                lp.signers.read_status()
            with mock.patch.object(config, 'DEX', 'meteora-dlmm'), \
                    mock.patch.object(config, 'POOL', 'M1'):
                lp.signers.read_status()
            lp.signers.read_status('mintX')
        # the pool travels in LPBOT_POOL, never as an argument: a positional
        # argument is a position filter and the pool address matches none.
        self.assertEqual(calls, [('status',), ('status',), ('status', 'mintX')])




class Guarded(unittest.TestCase):
    """The loop refuses before it spawns a signer or repoints the profile."""

    def test_chain_refuses_bad_arguments_and_foreign_scripts(self):
        j, err = lp.signers.chain('status', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE\n')
        self.assertIsNone(j); self.assertIn('refused', err)
        j, err = lp.signers.chain('open', 'a b c')
        self.assertIsNone(j); self.assertIn('refused', err)
        with mock.patch.dict(lp.signers.SIGNERS, {'evil': '/tmp/evil.mjs'}):
            pathlib.Path('/tmp/evil.mjs').write_text('')
            j, err = lp.signers.chain('status', dex='evil')
            self.assertIsNone(j); self.assertIn('outside', err)

    def test_repoint_refuses_an_unarmed_or_malformed_target(self):
        bad = {'dex': 'raydium-clmm', 'address': '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv', 'pair': 'SOL/USDC',
               'token_a': {'address': 'So11111111111111111111111111111111111111112', 'symbol': 'SOL'},
               'token_b': {'address': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'symbol': 'USDC'}}
        with mock.patch.object(config, 'EXECUTE_DEXES', ('orca',)), \
                mock.patch.object(db, 'repoint', lambda *a: self.fail('repointed')):
            with self.assertRaises(guards.Refused):
                lp.board.repoint(bad)
            with self.assertRaises(guards.Refused):
                lp.board.repoint(dict(bad, dex='orca', address='junk'))

    def test_operator_target_rejects_junk_before_touching_the_network(self):
        sent = []
        with mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch.object(dexes, 'pool', lambda *a: self.fail('network')):
            self.assertIsNone(lp.board.operator_target(['orca']))
            self.assertIsNone(lp.board.operator_target(['orca', 'not-an-address']))
            self.assertIsNone(lp.board.operator_target(['jupiter', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE']))
            with mock.patch.object(config, 'EXECUTE_DEXES', ('orca',)):
                self.assertIsNone(lp.board.operator_target(['byreal', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE']))
        self.assertTrue(all(ev == 'migrate_refused' for ev, _ in sent))
        self.assertEqual(len(sent), 4)

    def test_reopen_refuses_a_band_that_does_not_contain_the_price(self):
        calls = []
        best = {'band': 1.08, 'price': 200.0, 'net_day_pct': 0.5, 'rebal_per_day': 0.1, 'all_runs': [], 'fee': 0.0004}
        bal = {'price': 115.0, 'quoteUsd': 1.0, 'balanceA': 1.0, 'balanceB': 100.0, 'nativeSide': 'A',
               'tokenA': 'SOL', 'tokenB': 'USDC'}
        sent = []
        state = {'failures': 0}
        with mock.patch.object(lp.board, 'best_band_for', lambda *a, **k: best), \
                mock.patch.object(lp.capital, 'wallet', lambda p: bal), \
                mock.patch.object(lp.signers, 'chain', lambda *a, **k: calls.append(a) or ({}, None)), \
                mock.patch.object(lp.books, 'notify', lambda ev, **k: sent.append(ev)), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(config, 'DEX', 'orca'), \
                mock.patch.object(config, 'EXECUTE_DEXES', ('orca',)):
            # the band is centred on the modelled price 200 but the chain says 115:
            # nothing may be signed, and it counts as a failed open
            self.assertFalse(lp.moves.reopen(state, 'test'))
        self.assertFalse(any(a[0] == 'open' for a in calls))
        self.assertIn('open_refused', sent)
        self.assertEqual(state['failures'], 1)




class QuietHours(unittest.TestCase):
    def test_a_voluntary_move_waits_for_a_quiet_hour(self):
        busy = [0.7] * 12 + [1.4] * 12
        sent, moved = [], []
        rows = [{'address': 'M1', 'dex': 'meteora-dlmm', 'pair': 'SOL/USDC', 'band': 1.08, 'band_pct': 8.0,
                 'net_day_pct': 0.9, 'decision_day_pct': 0.9, 'rebal_per_day': 0.1, 'screen_ok': True, 'skipped': None,
                 'token_a': {'address': 'a', 'symbol': 'SOL'}, 'token_b': {'address': 'b', 'symbol': 'USDC'}}]
        held = {'band': 1.18, 'net_day_pct': 0.4, 'record': {'token_a': {'address': 'a'}, 'token_b': {'address': 'b'}}}
        with mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **kw: moved.append(1)), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(db, 'latest_scan', lambda max_age_seconds=None: ({'id': 1}, rows)), \
                mock.patch.object(db, 'season', lambda: busy), \
                mock.patch.object(config, 'POOL_PINNED', False), \
                mock.patch.object(config, 'MIGRATE_MIN_GAIN', 0.25), \
                mock.patch.object(config, 'EXECUTE_DEXES', ('orca', 'meteora-dlmm')):
            with mock.patch.object(lp.board, 'utc_hour', lambda: 15):        # busy afternoon
                self.assertFalse(lp.board.consider_migration({}, {}, held))
            self.assertEqual(sent[-1], 'move_deferred'); self.assertEqual(moved, [])
            with mock.patch.object(lp.board, 'utc_hour', lambda: 4):         # quiet night
                self.assertTrue(lp.board.consider_migration({}, {}, held))
            self.assertEqual(sent[-1], 'MIGRATE'); self.assertEqual(moved, [1])
            # the switch off: busy hour, still moves
            with mock.patch.object(config, 'DEFER_MOVES_TO_QUIET_HOURS', False), \
                    mock.patch.object(lp.board, 'utc_hour', lambda: 15):
                self.assertTrue(lp.board.consider_migration({}, {}, held))
            self.assertEqual(len(moved), 2)
            # no profile yet: nothing is busy
            with mock.patch.object(db, 'season', lambda: None), \
                    mock.patch.object(lp.board, 'utc_hour', lambda: 15):
                self.assertTrue(lp.board.consider_migration({}, {}, held))
            self.assertEqual(len(moved), 3)

    def test_board_pick_prefers_the_decision_figure(self):
        rows = [{'address': 'A', 'dex': 'orca', 'pair': 'SOL/USDC', 'net_day_pct': 0.9, 'decision_day_pct': 0.3,
                 'screen_ok': True, 'skipped': None, 'band_pct': 8.0, 'band': 1.08, 'rebal_per_day': 0.1,
                 'token_a': {'address': 'a', 'symbol': 'SOL'}, 'token_b': {'address': 'b', 'symbol': 'USDC'}},
                {'address': 'B', 'dex': 'orca', 'pair': 'SOL/USDC', 'net_day_pct': 0.6, 'decision_day_pct': 0.6,
                 'screen_ok': True, 'skipped': None, 'band_pct': 8.0, 'band': 1.08, 'rebal_per_day': 0.1,
                 'token_a': {'address': 'a', 'symbol': 'SOL'}, 'token_b': {'address': 'b', 'symbol': 'USDC'}}]
        held = {'band': 1.18, 'net_day_pct': 0.4, 'record': {'token_a': {'address': 'a'}, 'token_b': {'address': 'b'}}}
        with mock.patch.object(db, 'latest_scan', lambda max_age_seconds=None: ({'id': 1}, rows)):
            run, best, gain, why = lp.board.board_pick(held, 'X')
        self.assertEqual(best['address'], 'B')                 # A's tape says its model is stale
        self.assertAlmostEqual(gain, (0.6 - 0.4) / 0.4)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class Proactive(unittest.TestCase):
    """The loop acts on the forecast, and the dividend is gated."""

    def test_harvest_ready_gates_on_interval_and_amount(self):
        day = 24 * 3600
        self.assertTrue(lp.harvest.harvest_ready(0.5, 1e9, day, 0.25))
        self.assertFalse(lp.harvest.harvest_ready(0.1, 1e9, day, 0.25))
        self.assertFalse(lp.harvest.harvest_ready(5, 3600, day, 0.25))
        self.assertFalse(lp.harvest.harvest_ready(5, 1e9, 0, 0.25))
        self.assertTrue(lp.harvest.harvest_ready(0.25, day, day, 0.25))         # both edges count
        self.assertFalse(lp.harvest.harvest_ready(None, 1e9, day, 0.25))
        self.assertTrue(lp.harvest.harvest_ready(None, 1e9, day, 0))

    def test_dividend_records_once_and_zeroes_the_counter(self):
        sent, snaps, harvests = [], [], []
        status = {'positionMint': 'M', 'whirlpool': 'P', 'price': 100.0, 'inRange': True, 'liquidity': '1',
                  'feesAccruedA': 0.001, 'feesAccruedB': 0.2, 'feesAccrued_USD': 0.3, 'positionUsd': 190.0}
        state = {'last_harvest': 0}
        with mock.patch.object(lp.signers, 'chain', lambda *a, **k: ({'signature': 'sig'}, None)), \
                mock.patch.object(lp.capital, 'wallet', lambda p: {'walletUsd': 50.0}), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify_book', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(db, 'record_harvest', lambda *a: harvests.append(a)), \
                mock.patch.object(db, 'snapshot', lambda *a, **k: snaps.append(a)), \
                mock.patch.object(lp.capital, 'pool_tokens', lambda: (('A', 'SOL'), ('B', 'USDC'))), \
                mock.patch.object(db, 'event', lambda *a: None):
            self.assertTrue(lp.harvest.dividend(state, status))
        self.assertEqual(sent, ['DIVIDEND']); self.assertEqual(len(harvests), 1)
        self.assertEqual(snaps[0][4:7], (0.0, 0.0, 0.0))        # accrual recorded at zero
        self.assertGreater(state['last_harvest'], 0)

    def test_forecast_for_uses_the_tape_and_the_ledger(self):
        import numpy as np
        rng = np.random.default_rng(1)
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 1200)))
        status = {'positionMint': 'M', 'whirlpool': 'P', 'price': 104.5, 'lowerPrice': 95.0, 'upperPrice': 105.0}
        with mock.patch.object(lp.tape, 'tape', lambda pool: (np.arange(1200), px, np.ones(1200))), \
                mock.patch.object(db, 'position_opened', lambda m: db.now()), \
                mock.patch.object(db, 'position_open_price', lambda m: 100.0), \
                mock.patch.object(config, 'PROACTIVE_HORIZON', 6), \
                mock.patch.object(config, 'PROACTIVE_THRESHOLD', 0.5):
            f = lp.tape.forecast_for(status)
        self.assertTrue(f['act']); self.assertGreater(f['p_exit_6h'], 0.5)
        with mock.patch.object(lp.tape, 'tape', lambda pool: None):
            self.assertIsNone(lp.tape.forecast_for(status))

    def test_tape_is_cached_for_an_hour_and_survives_a_failed_fetch(self):
        calls = []
        lp.tape._TAPE.clear()
        with mock.patch.object(engine, 'candles', lambda p: calls.append(p) or ('t', 'p', 'v')):
            self.assertEqual(lp.tape.tape('X'), ('t', 'p', 'v'))
            self.assertEqual(lp.tape.tape('X'), ('t', 'p', 'v'))
        self.assertEqual(calls, ['X'])
        lp.tape._TAPE['X'] = (0, ('t', 'p', 'v'))          # stale
        with mock.patch.object(engine, 'candles', lambda p: None):
            self.assertEqual(lp.tape.tape('X'), ('t', 'p', 'v'))
        lp.tape._TAPE.clear()
