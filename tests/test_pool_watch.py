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


if __name__ == '__main__':
    unittest.main()
