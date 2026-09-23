"""The loop's pure pieces: sizing, error tidying, signer output parsing."""
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
