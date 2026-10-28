"""ops/pool_watch.py: EVM counters in Solana units, and R from samples (2026-10-26)."""
import datetime as dt
import importlib.util
import math
import pathlib
import unittest

spec = importlib.util.spec_from_file_location('pool_watch', pathlib.Path(__file__).resolve().parent.parent / 'ops' / 'pool_watch.py')
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)


def word(x):
    return '0x' + format(x, '064x')


class EvmState(unittest.TestCase):
    def test_x96_and_x128_become_q64(self):
        sp96 = 3821983726727094790890444
        st = pw.evm_state({'slot0': word(sp96) + '0' * 64, 'liquidity': word(5), 'g0': word(7 << 64), 'g1': word(3 << 64)},
                          ('0xa', 18), ('0xb', 6))
        self.assertEqual(st['sqrt_price'], sp96 >> 32)
        self.assertEqual((st['g0'], st['g1'], st['liquidity'], st['rewards']), (7, 3, 5, []))
        # WETH at about $2,327 in USDC on 2026-10-26
        self.assertAlmostEqual(pw.price(dict(st, dec_a=18, dec_b=6)), 2327, delta=5)


class PoolStats(unittest.TestCase):
    def rows(self, fee_per_step, moves):
        t0, sp, g, out = dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc), pw.Q64, 0, []
        for i, m in enumerate([0.0] + moves):
            sp = sp * math.exp(m / 2)
            g += fee_per_step
            out.append({'ts': t0 + dt.timedelta(minutes=10 * i), 'sqrt_price': int(sp), 'g0': 0, 'g1': int(g),
                        'liquidity': 10 ** 12, 'dec_a': 0, 'dec_b': 0, 'mint_b': 'x'})
        return out

    def test_r_is_fee_yield_over_variance_over_8(self):
        # price 1, fee growth 2e-6 of token B per unit L per step: yield 1e-6 per step of 2 sqrt(p) = 2
        moves = [0.001, -0.001] * 20
        s = pw.pool_stats(self.rows(2e-6 * pw.Q64, moves))
        self.assertAlmostEqual(s['r'], (1e-6 / 600) / ((0.001 ** 2) / 600 / 8), delta=0.3)
        self.assertGreater(s['fee_pct_day'], 0)

    def test_gaps_and_too_few_samples(self):
        self.assertIsNone(pw.pool_stats(self.rows(1, [0.001])))
        rows = self.rows(1, [0.001] * 5)
        for r in rows[3:]:
            r['ts'] += dt.timedelta(hours=5)
        s = pw.pool_stats(rows)
        self.assertAlmostEqual(s['hours'], 4 * 10 / 60, places=6)


class Hourly(unittest.TestCase):
    def test_hours_sum_to_the_window_and_split_at_the_hour(self):
        rows = PoolStats.rows(None, 2e-6 * pw.Q64, [0.001, -0.002, 0.0015] * 6)     # 10-min samples over 3 hours
        h = pw.hourly(rows)
        self.assertEqual(sorted(k.hour for k in h), [0, 1, 2, 3])
        # an interval counts in the hour it ends in: 00:10..00:50 in hour 0, 01:00..01:50 in hour 1
        self.assertEqual((h[rows[0]['ts']][2], h[rows[6]['ts']][2]), (3000, 3600))
        tot = pw.stats(*(sum(v[i] for v in h.values()) for i in range(3)))
        s = pw.pool_stats(rows)
        for k in ('r', 'fee_pct_day', 'vol_pct_year', 'hours'):
            self.assertAlmostEqual(tot[k], s[k], places=9)

    def test_stats_edges(self):
        self.assertIsNone(pw.stats(1.0, 0.0, 0))
        self.assertIsNone(pw.stats(1.0, 0.0, 600)['r'])     # no price move: no R

    def test_sol_stable_and_labels(self):
        usdc, usd1 = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB'
        self.assertTrue(pw.sol_stable(pw.SOL, usdc))
        self.assertTrue(pw.sol_stable(usd1, pw.SOL))
        self.assertFalse(pw.sol_stable(pw.SOL, 'J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn'))
        self.assertFalse(pw.sol_stable(usdc, pw.USDC_ETH))
        self.assertEqual(pw.label('x', pw.SOL, usd1), 'SOL/USD1')
        self.assertEqual(pw.label(pw.CONTROL[2], None, None), pw.CONTROL[3])


if __name__ == '__main__':
    unittest.main()
