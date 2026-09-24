"""The loop's pure pieces: sizing, error tidying, signer output parsing."""
import pathlib
import subprocess
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import config      # noqa: E402
import rebalancer  # noqa: E402


class DepositCaps(unittest.TestCase):
    def bal(self, **kw):
        base = dict(price=100.0, quoteUsd=1.0, balanceA=10.0, balanceB=1000.0,
                    nativeSide='A', tokenA='SOL', tokenB='USDC')
        base.update(kw)
        return base

    def test_flush_wallet_caps_each_side_at_the_fraction(self):
        a, b = rebalancer.deposit_caps(self.bal())
        want = config.CAPITAL_USD * config.SIDE_CAP_FRACTION
        self.assertAlmostEqual(b, want)
        self.assertAlmostEqual(a * 100.0, want)

    def test_gas_reserve_comes_off_the_native_side_only(self):
        a, _ = rebalancer.deposit_caps(self.bal(balanceA=0.6))
        self.assertAlmostEqual(a, 0.6 - config.GAS_RESERVE_SOL)
        # native on the B side: reserve leaves A alone
        a2, b2 = rebalancer.deposit_caps(self.bal(price=0.01, quoteUsd=100.0, balanceA=5000.0,
                                                  balanceB=0.6, nativeSide='B',
                                                  tokenA='WIF', tokenB='SOL'))
        self.assertAlmostEqual(b2, 0.6 - config.GAS_RESERVE_SOL)
        self.assertAlmostEqual(a2, min(5000.0, config.CAPITAL_USD / 100.0
                                       * config.SIDE_CAP_FRACTION / 0.01))

    def test_non_dollar_quote_is_converted_to_quote_units(self):
        # SOL/cbBTC: price is BTC per SOL, quote worth $100k. Cap B is in BTC.
        _, b = rebalancer.deposit_caps(self.bal(price=0.001, quoteUsd=100_000.0,
                                                balanceA=10.0, balanceB=1.0, nativeSide='A'))
        self.assertAlmostEqual(b, config.CAPITAL_USD / 100_000.0 * config.SIDE_CAP_FRACTION)

    def test_short_wallet_never_goes_negative(self):
        a, b = rebalancer.deposit_caps(self.bal(balanceA=0.01, balanceB=0.0))
        self.assertEqual(a, 0.0); self.assertEqual(b, 0.0)

    def test_no_native_side(self):
        a, b = rebalancer.deposit_caps(self.bal(nativeSide=None, balanceA=2.0, balanceB=50.0))
        self.assertAlmostEqual(a, min(2.0, config.CAPITAL_USD * config.SIDE_CAP_FRACTION / 100))
        self.assertAlmostEqual(b, 50.0)


class Nearest(unittest.TestCase):
    def test_finds_the_closest_rung_within_tolerance(self):
        runs = {3: 'a', 5: 'b', 8: 'c', 12: 'd'}
        self.assertEqual(rebalancer.nearest(runs, 6), 'b')
        self.assertEqual(rebalancer.nearest(runs, 10), 'c')
        self.assertIsNone(rebalancer.nearest(runs, 20))
        self.assertIsNone(rebalancer.nearest({}, 5))

    def test_held_band_is_a_property_of_the_band(self):
        # +/-5% opened at 100, price now 118: half-width is still 5%.
        lower, upper = 100 / 1.05, 100 * 1.05
        held = (upper / lower) ** 0.5
        self.assertEqual(round((held - 1) * 100), 5)


class Tidy(unittest.TestCase):
    def test_rate_limit_dumps_become_one_line(self):
        wall = 'Error: {"status":429,"headers":{"set-cookie":"x"},"body":"Too Many Requests"} ' * 20
        self.assertEqual(rebalancer.tidy(wall), 'RPC rate limited')
        self.assertEqual(rebalancer.tidy('socket ECONNRESET'), 'RPC connection reset')
        self.assertEqual(rebalancer.tidy('Blockhash not found'), 'blockhash expired')
        self.assertIsNone(rebalancer.tidy(''))
        self.assertLessEqual(len(rebalancer.tidy('x' * 500)), 140)


class Chain(unittest.TestCase):
    def fake(self, stdout='', stderr='', raise_timeout=False):
        def run(*a, **kw):
            if raise_timeout:
                raise subprocess.TimeoutExpired(a, 1)
            return subprocess.CompletedProcess(a, 0, stdout, stderr)
        return run

    def test_extracts_json_from_noisy_output(self):
        out = 'bigint: Failed to load bindings\n{"positionMint": "M", "n": 1}\nDRY RUN'
        with mock.patch('subprocess.run', self.fake(stdout=out)):
            j, err = rebalancer.chain('status')
        self.assertEqual(j, {'positionMint': 'M', 'n': 1}); self.assertIsNone(err)

    def test_error_text_without_json_is_tidied(self):
        with mock.patch('subprocess.run', self.fake(stderr='ERROR: 429 Too Many Requests')):
            j, err = rebalancer.chain('status')
        self.assertIsNone(j); self.assertEqual(err, 'RPC rate limited')

    def test_timeout_is_an_error_not_a_crash(self):
        with mock.patch('subprocess.run', self.fake(raise_timeout=True)):
            j, err = rebalancer.chain('status')
        self.assertIsNone(j); self.assertEqual(err, 'signer timed out')

    def test_explicit_empty_position_is_not_an_error(self):
        with mock.patch('subprocess.run', self.fake(stdout='{"positions": 0, "positionMint": null}')):
            j, err = rebalancer.chain('status')
        self.assertIsNotNone(j); self.assertIsNone(j['positionMint'])


class PositionUsd(unittest.TestCase):
    def test_prefers_the_signers_dollar_figure(self):
        self.assertEqual(rebalancer.position_usd({'positionUsd': 161.9, 'closeEstA': 1, 'closeEstB': 1, 'price': 100}), 161.9)

    def test_falls_back_to_quote_units_times_quote_price(self):
        s = {'closeEstA': 1.0, 'closeEstB': 50.0, 'price': 100.0, 'quoteUsd': 2.0}
        self.assertAlmostEqual(rebalancer.position_usd(s), 300.0)
        self.assertIsNone(rebalancer.position_usd({'price': 100.0}))

    def test_rent_locked_in_the_position_accounts_is_part_of_the_mark(self):
        # 0.2391 SOL of rent at $115 on the first Meteora position: the close
        # refunds it, so the mark must carry it or the open reads as a loss.
        s = {'positionUsd': 194.2, 'rentUsd': 27.5}
        self.assertAlmostEqual(rebalancer.position_usd(s), 221.7)
        s = {'closeEstA': 1.0, 'closeEstB': 50.0, 'price': 100.0, 'quoteUsd': 1.0, 'rentUsd': 2.5}
        self.assertAlmostEqual(rebalancer.position_usd(s), 152.5)




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
        with mock.patch.object(rebalancer.db, 'latest_scan', lambda max_age_seconds=None: ({'id': 7}, rows)):
            return fn()

    def test_best_other_pool_and_its_gain(self):
        rows = [self.row('M1', 'meteora-dlmm', 0.58), self.row('O1', 'orca', 0.386), self.row('R1', 'raydium-clmm', 0.40)]
        run, best, gain, why = self.with_board(rows, lambda: rebalancer.board_pick(self.held(), 'O1'))
        self.assertEqual(best['address'], 'M1')
        self.assertAlmostEqual(gain, (0.58 - 0.386) / 0.386)
        self.assertIsNone(why)

    def test_unscreened_skipped_and_other_pairs_are_not_candidates(self):
        rows = [self.row('X1', 'orca', 0.9, ok=False), self.row('X2', 'orca', 0.8, skipped='thin'),
                self.row('X3', 'orca', 0.7, a='zzz'), self.row('R1', 'raydium-clmm', 0.40)]
        with mock.patch.object(rebalancer.config, 'ALLOW_SWAP', False):
            run, best, gain, why = self.with_board(rows, lambda: rebalancer.board_pick(self.held(), 'O1'))
        self.assertEqual(best['address'], 'R1')
        # swaps allowed: the other pair is eligible
        with mock.patch.object(rebalancer.config, 'ALLOW_SWAP', True):
            run, best, gain, why = self.with_board(rows, lambda: rebalancer.board_pick(self.held(), 'O1'))
        self.assertEqual(best['address'], 'X3')

    def test_no_board_and_no_candidates(self):
        run, best, gain, why = self.with_board([], lambda: rebalancer.board_pick(self.held(), 'O1'))
        self.assertIsNone(best); self.assertEqual(why, 'no fresh board')
        run, best, gain, why = self.with_board([self.row('O1', 'orca', 0.4)],
                                               lambda: rebalancer.board_pick(self.held(), 'O1'))
        self.assertIsNone(best); self.assertIn('no eligible', why)

    def test_unscorable_held_pool_still_yields_a_candidate(self):
        rows = [self.row('M1', 'meteora-dlmm', 0.58)]
        run, best, gain, why = self.with_board(rows, lambda: rebalancer.board_pick(None, 'O1'))
        self.assertEqual(best['address'], 'M1'); self.assertIsNone(gain)

    def test_consider_migration_recommends_without_a_signer_and_moves_with_one(self):
        rows = [self.row('M1', 'meteora-dlmm', 0.90)]
        sent = []
        moved = []
        with mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch.object(rebalancer, 'rebalance', lambda *a, **kw: moved.append(kw.get('target'))), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.config, 'POOL_PINNED', False), \
                mock.patch.object(rebalancer.config, 'MIGRATE_MIN_GAIN', 0.5):
            with mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca',)):
                acted = self.with_board(rows, lambda: rebalancer.consider_migration({}, {}, self.held()))
            self.assertFalse(acted)
            self.assertEqual(sent[-1][0], 'MIGRATE_RECOMMENDED')
            self.assertIn('repoint', sent[-1][1]['command'])
            self.assertEqual(moved, [])
            with mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca', 'meteora-dlmm')):
                acted = self.with_board(rows, lambda: rebalancer.consider_migration({}, {}, self.held()))
            self.assertTrue(acted)
            self.assertEqual(sent[-1][0], 'MIGRATE')
            self.assertEqual(moved[-1]['address'], 'M1')
            # under the threshold: checked, not moved
            small = [self.row('M1', 'meteora-dlmm', 0.40)]
            with mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca', 'meteora-dlmm')):
                acted = self.with_board(small, lambda: rebalancer.consider_migration({}, {}, self.held()))
            self.assertFalse(acted); self.assertEqual(sent[-1][0], 'board_checked')
            # pinned: nothing at all
            with mock.patch.object(rebalancer.config, 'POOL_PINNED', True):
                self.assertFalse(self.with_board(rows, lambda: rebalancer.consider_migration({}, {}, self.held())))

    def test_chain_dispatches_by_dex(self):
        j, err = rebalancer.chain('status', dex='no-such-dex')
        self.assertIsNone(j); self.assertIn('no signer', err)


class ReadStatus(unittest.TestCase):
    def test_status_never_passes_the_pool_positionally(self):
        calls = []
        with mock.patch.object(rebalancer, 'chain', lambda *a, **kw: calls.append(a) or ({}, None)):
            with mock.patch.object(rebalancer.config, 'DEX', 'orca'):
                rebalancer.read_status()
            with mock.patch.object(rebalancer.config, 'DEX', 'meteora-dlmm'), \
                    mock.patch.object(rebalancer.config, 'POOL', 'M1'):
                rebalancer.read_status()
            rebalancer.read_status('mintX')
        # the pool travels in LPBOT_POOL, never as an argument: a positional
        # argument is a position filter and the pool address matches none.
        self.assertEqual(calls, [('status',), ('status',), ('status', 'mintX')])




class Guarded(unittest.TestCase):
    """The loop refuses before it spawns a signer or repoints the profile."""

    def test_chain_refuses_bad_arguments_and_foreign_scripts(self):
        j, err = rebalancer.chain('status', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE\n')
        self.assertIsNone(j); self.assertIn('refused', err)
        j, err = rebalancer.chain('open', 'a b c')
        self.assertIsNone(j); self.assertIn('refused', err)
        with mock.patch.dict(rebalancer.SIGNERS, {'evil': '/tmp/evil.mjs'}):
            pathlib.Path('/tmp/evil.mjs').write_text('')
            j, err = rebalancer.chain('status', dex='evil')
            self.assertIsNone(j); self.assertIn('outside', err)

    def test_repoint_refuses_an_unarmed_or_malformed_target(self):
        bad = {'dex': 'raydium-clmm', 'address': '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv', 'pair': 'SOL/USDC',
               'token_a': {'address': 'So11111111111111111111111111111111111111112', 'symbol': 'SOL'},
               'token_b': {'address': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'symbol': 'USDC'}}
        with mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca',)), \
                mock.patch.object(rebalancer.db, 'repoint', lambda *a: self.fail('repointed')):
            with self.assertRaises(rebalancer.guards.Refused):
                rebalancer.repoint(bad)
            with self.assertRaises(rebalancer.guards.Refused):
                rebalancer.repoint(dict(bad, dex='orca', address='junk'))

    def test_operator_target_rejects_junk_before_touching_the_network(self):
        sent = []
        with mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch.object(rebalancer.dexes, 'pool', lambda *a: self.fail('network')):
            self.assertIsNone(rebalancer.operator_target(['orca']))
            self.assertIsNone(rebalancer.operator_target(['orca', 'not-an-address']))
            self.assertIsNone(rebalancer.operator_target(['jupiter', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE']))
            with mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca',)):
                self.assertIsNone(rebalancer.operator_target(['byreal', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE']))
        self.assertTrue(all(ev == 'migrate_refused' for ev, _ in sent))
        self.assertEqual(len(sent), 4)

    def test_reopen_refuses_a_band_that_does_not_contain_the_price(self):
        calls = []
        best = {'band': 1.08, 'price': 200.0, 'net_day_pct': 0.5, 'rebal_per_day': 0.1, 'all_runs': [], 'fee': 0.0004}
        bal = {'price': 115.0, 'quoteUsd': 1.0, 'balanceA': 1.0, 'balanceB': 100.0, 'nativeSide': 'A',
               'tokenA': 'SOL', 'tokenB': 'USDC'}
        sent = []
        state = {'failures': 0}
        with mock.patch.object(rebalancer, 'best_band_for', lambda *a, **k: best), \
                mock.patch.object(rebalancer, 'wallet', lambda p: bal), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: calls.append(a) or ({}, None)), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **k: sent.append(ev)), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.config, 'DEX', 'orca'), \
                mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca',)):
            # the band is centred on the modelled price 200 but the chain says 115:
            # nothing may be signed, and it counts as a failed open
            self.assertFalse(rebalancer.reopen(state, 'test'))
        self.assertFalse(any(a[0] == 'open' for a in calls))
        self.assertIn('open_refused', sent)
        self.assertEqual(state['failures'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
