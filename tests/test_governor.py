"""The payout governor (sql/053): the loss of a band, the share it allows,
the fee split it drives, and the measurement from the ledger."""
import math
import unittest

import _fixtures
_fixtures.ensure_profile()

import db  # noqa: E402
import fees  # noqa: E402
from lp import governor  # noqa: E402

SOL = fees.NATIVE_MINT
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'


class Loss(unittest.TestCase):
    def test_no_move_no_loss(self):
        self.assertAlmostEqual(governor.band_loss(99, 101, 100, 100, [(100, 10 ** 9)], 1.0), 0.0, places=9)

    def test_any_move_inside_loses_the_exact_lvr(self):
        # hold minus LP for an in-range step is L (sqrt(P1) - sqrt(P0))^2 / sqrt(P0)
        for p1 in (99.2, 99.9, 100.4, 100.9):
            got = governor.band_loss(99, 101, 100, p1, [(100, 1000)], 1.0)
            self.assertAlmostEqual(got, -1000 * (math.sqrt(p1) - 10) ** 2 / 10, places=9)

    def test_a_move_out_of_the_band_loses_and_the_scale_applies(self):
        a = governor.band_loss(99, 101, 100, 98, [(100, 1000)], 1.0)
        b = governor.band_loss(99, 101, 100, 98, [(100, 1000)], 10 ** -7.5)
        self.assertLess(a, 0)
        self.assertAlmostEqual(b, a * 10 ** -7.5, places=15)

    def test_an_increase_is_held_from_its_own_price(self):
        one = governor.band_loss(99, 101, 100, 100.5, [(100, 1000)], 1.0)
        two = governor.band_loss(99, 101, 100, 100.5, [(100, 1000), (100.5, 2000)], 1.0)
        self.assertAlmostEqual(two, one * 1.0 + 0.0, places=9)  # the add at the close price loses nothing

    def test_a_partial_removal_scales_the_hold(self):
        full = governor.band_loss(99, 101, 100, 100.5, [(100, 1000)], 1.0)
        half = governor.band_loss(99, 101, 100, 100.5, [(100, 1000), (100.2, 500)], 1.0)
        self.assertAlmostEqual(half, full / 2, places=9)

    def test_no_liquidity_is_none(self):
        self.assertIsNone(governor.band_loss(99, 101, 100, 100, [(100, 0)], 1.0))
        self.assertIsNone(governor.band_loss(99, 101, 100, 100, [], 1.0))

    def test_value_scale_sol_usdc(self):
        self.assertAlmostEqual(governor.value_scale(9, 6), 10 ** -7.5)


class Share(unittest.TestCase):
    def test_the_audited_weeks(self):
        # W40: fees 25.9, loss -14.8; W42: fees 115.3, loss -115.0
        self.assertAlmostEqual(governor.payout_share(25.9, -14.8, 0.2, 0.5), 0.8 * 11.1 / 25.9, places=9)
        self.assertAlmostEqual(governor.payout_share(115.3, -115.0, 0.2, 0.5), 0.8 * 0.3 / 115.3, places=9)

    def test_a_losing_window_pays_nothing_and_the_cap_holds(self):
        self.assertEqual(governor.payout_share(10, -20, 0.2, 0.5), 0.0)
        self.assertEqual(governor.payout_share(0, 0, 0.2, 0.5), 0.0)
        self.assertEqual(governor.payout_share(10, 0, 0.0, 0.5), 0.5)
        self.assertEqual(governor.payout_share(10, 5, 0.0, 1.0), 1.0)    # a positive "loss" counts as none


class Split(unittest.TestCase):
    F = [(SOL, 'SOL', 0.02, 100.0), (USDC, 'USDC', 3.0, 1.0)]        # $5 of fees

    def by_kind(self, parts):
        out = {}
        for p in parts:
            out[(p['symbol'], p['kind'])] = out.get((p['symbol'], p['kind']), 0) + p['amount']
        return out

    def test_share_pays_that_share_of_the_value_from_usdc(self):
        k = self.by_kind(fees.split(self.F, USDC, 1.0, 0.05, share=0.2))
        self.assertAlmostEqual(k[('USDC', 'paid')], 1.0)                 # 20% of $5
        self.assertAlmostEqual(k[('USDC', 'reinvested')], 2.0)
        self.assertAlmostEqual(k[('SOL', 'reinvested')], 0.02)

    def test_share_never_pays_more_usdc_than_was_earned(self):
        k = self.by_kind(fees.split([(SOL, 'SOL', 0.05, 100.0), (USDC, 'USDC', 0.5, 1.0)], USDC, 1.0, 0.05, share=0.5))
        self.assertAlmostEqual(k[('USDC', 'paid')], 0.5)
        self.assertNotIn(('USDC', 'reinvested'), k)

    def test_zero_share_reinvests_everything_and_none_keeps_the_old_rule(self):
        k = self.by_kind(fees.split(self.F, USDC, 1.0, 0.05, share=0.0))
        self.assertNotIn(('USDC', 'paid'), k)
        self.assertEqual(self.by_kind(fees.split(self.F, USDC, 1.0, 0.05))[('USDC', 'paid')], 3.0)

    def test_gas_low_still_reinvests_and_total_is_conserved(self):
        parts = fees.split(self.F, USDC, 0.01, 0.05, share=0.3)
        self.assertNotIn('paid', {p['kind'] for p in parts})
        for share in (0.0, 0.1, 0.33, 0.5, 1.0):
            parts = fees.split(self.F, USDC, 1.0, 0.05, share=share)
            self.assertAlmostEqual(sum(p['usd'] for p in parts), 5.0)

    def test_a_bad_share_raises(self):
        with self.assertRaises(ValueError):
            fees.split(self.F, USDC, 1.0, 0.05, share=1.5)


class Measure(unittest.TestCase):
    """measure() on the test ledger: one band that rode inside and earned."""

    def setUp(self):
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute("delete from fee_growth where pool = %s", (POOL,))
            cur.execute("""insert into fee_growth (ts, dex, pool, sqrt_price, g0, g1, rewards, dec_a, dec_b, mint_a, mint_b)
                           values (now(), 'raydium-clmm', %s, 0, 0, 0, '[]', 9, 6, %s, %s)""", (POOL, SOL, USDC))
            cur.execute("""insert into positions (mint, config_name, pool, dex, opened_at, closed_at, lower_price, upper_price,
                                                  deposit_usd, withdraw_usd)
                           values ('M1', 'sol-usdc', %s, 'raydium-clmm', now() - interval '3 hours', now() - interval '1 hour',
                                   99, 101, 100, 99.9),
                                  ('M2', 'sol-usdc', 'X', 'meteora-dlmm', now() - interval '3 hours', now() - interval '2 hours',
                                   99, 101, 100, 100),
                                  ('M3', 'sol-usdc', %s, 'raydium-clmm', now() - interval '20 days', now() - interval '19 days',
                                   99, 101, 100, 100)""", (POOL, POOL))
            cur.execute("insert into band_profile (mint, last_event, fees_usd) values ('M1', 'rebalance', 0.5), "
                        "('M2', 'rebalance', 9), ('M3', 'rebalance', 9)")
            L = 10 ** 9
            cur.execute("""insert into snapshots (ts, mint, price, liquidity) values
                           (now() - interval '3 hours', 'M1', 100, %s), (now() - interval '1 hour', 'M1', 100.5, %s)""", (L, L))
            self.L = L

    def tearDown(self):
        _fixtures.reset_ledger()

    def test_only_this_window_and_measurable_bands(self):
        m = governor.measure('sol-usdc', 7)
        self.assertEqual((m['bands'], m['left_out']), (1, 1))       # M2 is DLMM; M3 is 19 days old
        self.assertAlmostEqual(m['fees'], 0.5)
        want = governor.band_loss(99, 101, 100, 100.5, [(100, self.L), (100.5, self.L)], 10 ** -7.5)
        self.assertAlmostEqual(m['loss'], want, places=12)
        self.assertLess(m['loss'], 0)


if __name__ == '__main__':
    unittest.main()
