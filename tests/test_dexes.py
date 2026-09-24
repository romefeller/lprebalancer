"""Pool normalisation: every DEX's record lands in one shape, fee units come
out as fractions, and the on-chain decoders read the fields they claim to."""
import base64
import math
import unittest

import _fixtures  # noqa: F401
import dexes


def clmm_account(dec0=9, dec1=6, tick_spacing=4, liquidity=10 ** 15, price=100.0, tick=-1234):
    """A synthetic Raydium-layout PoolState with known values."""
    raw = bytearray(1544)
    raw[9:41] = bytes([7] * 32)                     # amm_config
    raw[73:105] = bytes([1] * 32); raw[105:137] = bytes([2] * 32)
    o = 233
    raw[o], raw[o + 1] = dec0, dec1
    raw[o + 2:o + 4] = tick_spacing.to_bytes(2, 'little')
    raw[o + 4:o + 20] = liquidity.to_bytes(16, 'little')
    sqrt_price = int(math.sqrt(price / 10 ** (dec0 - dec1)) * 2 ** 64)
    raw[o + 20:o + 36] = sqrt_price.to_bytes(16, 'little')
    raw[o + 36:o + 40] = tick.to_bytes(4, 'little', signed=True)
    return bytes(raw)


class Decoders(unittest.TestCase):
    def test_clmm_state_round_trips(self):
        st = dexes.decode_clmm_state(clmm_account())
        self.assertEqual((st['decimals_a'], st['decimals_b'], st['tick_spacing']), (9, 6, 4))
        self.assertAlmostEqual(st['price'], 100.0, places=6)
        self.assertEqual(st['tick'], -1234)
        self.assertAlmostEqual(st['liquidity'], 10 ** 15 / math.sqrt(10 ** 15))
        self.assertIn(len(st['amm_config']), (43, 44))
        self.assertNotEqual(st['mint_a'], st['mint_b'])

    def test_short_account_is_none(self):
        self.assertIsNone(dexes.decode_clmm_state(b''))
        self.assertIsNone(dexes.decode_clmm_state(b'\0' * 100))

    def test_amm_config(self):
        raw = bytearray(117)
        o = 8
        raw[o + 35:o + 39] = (120000).to_bytes(4, 'little')
        raw[o + 39:o + 43] = (400).to_bytes(4, 'little')
        raw[o + 43:o + 45] = (1).to_bytes(2, 'little')
        c = dexes.decode_amm_config(bytes(raw))
        self.assertEqual(c, {'protocol_fee_rate': 120000, 'trade_fee_rate': 400, 'tick_spacing': 1})

    def test_b58(self):
        self.assertEqual(dexes.b58(b'\0' * 32), '1' * 32)
        # round trip against a reference decoder
        alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        def dec(s):
            n = 0
            for ch in s:
                n = n * 58 + alphabet.index(ch)
            return n.to_bytes(32, 'big')
        for b in (bytes(range(32)), bytes([255] * 32), bytes([0, 0, 7] + [9] * 29)):
            self.assertEqual(dec(dexes.b58(b)), b)


class Fees(unittest.TestCase):
    def test_nominal_wins_when_it_is_a_real_rate(self):
        self.assertEqual(dexes.effective_fee(0.0004, 100.0, 1e6), (0.0004, 'nominal'))

    def test_placeholder_nominal_yields_to_the_realised_ratio(self):
        fee, src = dexes.effective_fee(1e-6, 116.0, 1.2e6)
        self.assertEqual(src, 'realised')
        self.assertAlmostEqual(fee, 116.0 / 1.2e6)

    def test_no_volume_keeps_the_nominal(self):
        self.assertEqual(dexes.effective_fee(1e-6, 0.0, 0.0), (1e-6, 'nominal'))


class Normalisers(unittest.TestCase):
    def test_orca(self):
        p = {'address': 'X', 'feeRate': 400, 'liquidity': str(10 ** 15), 'price': '100',
             'tvlUsdc': '2000000', 'adaptiveFeeEnabled': False, 'tickSpacing': 4,
             'tokenA': {'address': 'a', 'symbol': 'SOL', 'name': 'Solana', 'decimals': 9},
             'tokenB': {'address': 'b', 'symbol': 'USDC', 'name': 'USD Coin', 'decimals': 6},
             'stats': {'24h': {'volume': '5000000', 'fees': '2000'}}}
        r = dexes.from_orca(p)
        self.assertEqual((r['dex'], r['kind'], r['pair']), ('orca', 'clmm', 'SOL/USDC'))
        self.assertEqual(r['fee'], 0.0004)
        self.assertAlmostEqual(r['liquidity'], 10 ** 15 / math.sqrt(10 ** 15))
        self.assertEqual(r['token_b']['decimals'], 6)

    def test_raydium_renames_wsol_and_reads_ppm_config(self):
        p = {'id': 'R', 'feeRate': 0.0004, 'tvl': 7e6, 'price': 115.0, 'hasDynamicFee': False,
             'mintA': {'address': 'a', 'symbol': 'WSOL', 'name': 'Wrapped SOL', 'decimals': 9},
             'mintB': {'address': 'b', 'symbol': 'USDC', 'name': 'USD Coin', 'decimals': 6},
             'day': {'volume': 3.8e7, 'volumeFee': 15378}, 'config': {'tickSpacing': 1}}
        r = dexes.from_raydium(p)
        self.assertEqual(r['pair'], 'SOL/USDC')
        self.assertEqual(r['fee'], 0.0004)
        self.assertIsNone(r['liquidity'])            # comes from the chain
        self.assertEqual(r['tick_spacing'], 1)

    def test_byreal_prices_a_over_b_from_token_prices(self):
        p = {'poolAddress': 'B', 'tvl': '1000000', 'volumeUsd24h': '500000', 'feeUsd24h': '1000',
             'feeRate': {'fixFeeRate': '2000'}, 'price': '0.0087', 'decayFeeFlag': 0,
             'mintA': {'mintInfo': {'address': 'a', 'symbol': 'SOL', 'name': 'SOL', 'decimals': 9}, 'price': '115'},
             'mintB': {'mintInfo': {'address': 'b', 'symbol': 'USDC', 'name': 'USDC', 'decimals': 6}, 'price': '1'}}
        r = dexes.from_byreal(p)
        self.assertAlmostEqual(r['price'], 115.0)      # not the display-order 0.0087
        self.assertEqual(r['fee'], 0.002)
        self.assertFalse(r['adaptive_fee'])
        p['feeRate']['fixFeeRate'] = '1'
        r = dexes.from_byreal(p)
        self.assertTrue(r['adaptive_fee'])
        self.assertEqual(r['fee_source'], 'realised')

    def test_meteora_dlmm_prefers_the_realised_fee_when_sane(self):
        p = {'address': 'M', 'tvl': 6.9e6, 'current_price': 114.9, 'is_blacklisted': False,
             'token_x': {'address': 'a', 'symbol': 'SOL', 'name': 'Wrapped SOL', 'decimals': 9, 'price': 114.9},
             'token_y': {'address': 'b', 'symbol': 'USDC', 'name': 'USD Coin', 'decimals': 6, 'price': 1.0},
             'pool_config': {'bin_step': 4, 'base_fee_pct': 0.04, 'max_fee_pct': 0.0},
             'dynamic_fee_pct': 5e-7, 'volume': {'24h': 5.58e7}, 'fees': {'24h': 26514.0}}
        r = dexes.from_meteora_dlmm(p)
        self.assertEqual((r['dex'], r['kind'], r['bin_step']), ('meteora-dlmm', 'dlmm', 4))
        self.assertEqual(r['fee_source'], 'realised')
        self.assertAlmostEqual(r['fee'], 26514.0 / 5.58e7)
        self.assertAlmostEqual(r['base_fee'], 0.0004)
        # absurd realised ratio: fall back to the base rate
        p['fees']['24h'] = 5e6
        self.assertEqual(dexes.from_meteora_dlmm(p)['fee_source'], 'nominal')

    def test_attach_bins_averages_dollars_per_bin_near_the_active_bin(self):
        rec = {'address': 'M', 'bin_step': 4, 'token_usd': (100.0, 1.0)}
        probe = {'M': {'activeBin': 0, 'binStep': 4, 'baseFeePct': 0.04,
                       'bins': [{'id': i, 'price': 100, 'x': 0.0 if i <= 0 else 1.0,
                                 'y': 100.0 if i < 0 else (50.0 if i == 0 else 0.0)}
                                for i in range(-30, 31)]}}
        with unittest.mock.patch.object(dexes, '_dlmm_probe', lambda a: probe):
            out = dexes.attach_bins([rec])[0]
        # every bin except the active one is worth $100; the active one $50
        self.assertGreater(out['active_bin_usd'], 95)
        self.assertLess(out['active_bin_usd'], 100)
        self.assertEqual(out['bins_sampled'], 51)       # +/-25 bins = +/-1%
        self.assertAlmostEqual(out['base_fee'], 0.0004)

    def test_feasible_bands_respects_the_position_length(self):
        bands = (1.03, 1.05, 1.08, 1.12, 1.18, 1.25, 1.40)
        self.assertEqual(dexes.feasible_bands({'kind': 'clmm'}, bands), bands)
        self.assertEqual(dexes.feasible_bands({'kind': 'dlmm', 'bin_step': 4}, bands), bands[:-1])
        self.assertEqual(dexes.feasible_bands({'kind': 'dlmm', 'bin_step': 1}, bands), (1.03, 1.05))
        self.assertEqual(dexes.feasible_bands({'kind': 'dlmm', 'bin_step': 100}, bands), bands)


class FetchAll(unittest.TestCase):
    def test_one_dex_failing_is_one_dex_not_the_scan(self):
        def boom(limit):
            raise RuntimeError('down')
        with unittest.mock.patch.dict(dexes.ADAPTERS, {'orca': lambda limit: [{'address': 'o'}],
                                                       'raydium-clmm': boom}):
            recs, errs = dexes.fetch_all(('orca', 'raydium-clmm', 'nope'), limit=1)
        self.assertEqual(recs['orca'], [{'address': 'o'}])
        self.assertEqual(recs['raydium-clmm'], [])
        self.assertIn('RuntimeError', errs['raydium-clmm'])
        self.assertEqual(errs['nope'], 'unknown dex')


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main(verbosity=2)
