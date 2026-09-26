"""On-chain fee counters: decoding both layouts, the +/-1% income, wrap-around,
the sampling cadence and the candidate set."""
import math
import time
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import dexes       # noqa: E402
import rebalancer  # noqa: E402

SOL, USDC = 'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
B58 = dexes.b58


def b58decode(s):
    import base64
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    n = 0
    for ch in s:
        n = n * 58 + alphabet.index(ch)
    raw = n.to_bytes(32, 'big')
    return raw


def raydium_account(sqrt_q64, g0, g1, dec_a=9, dec_b=6):
    o = 8 + 1 + 32 * 7
    raw = bytearray(dexes.REWARD_INFOS_OFFSET + 3 * dexes.REWARD_INFO_LEN)
    raw[73:105] = b58decode(SOL); raw[105:137] = b58decode(USDC)
    raw[o] = dec_a; raw[o + 1] = dec_b
    raw[o + 20:o + 36] = sqrt_q64.to_bytes(16, 'little')
    raw[o + 44:o + 60] = g0.to_bytes(16, 'little'); raw[o + 60:o + 76] = g1.to_bytes(16, 'little')
    return bytes(raw)


def orca_account(sqrt_q64, g0, g1):
    raw = bytearray(700)
    raw[65:81] = sqrt_q64.to_bytes(16, 'little')
    raw[101:133] = b58decode(SOL); raw[181:213] = b58decode(USDC)
    raw[165:181] = g0.to_bytes(16, 'little'); raw[245:261] = g1.to_bytes(16, 'little')
    return bytes(raw)


SQRT = int(math.sqrt(121.0 * 1e-3) * 2 ** 64)       # SOL at $121 in raw units (9 vs 6 decimals)


class Decode(unittest.TestCase):
    def test_both_layouts(self):
        r = dexes.decode_fee_state(raydium_account(SQRT, 5, 7), 'raydium')
        self.assertEqual((r['g0'], r['g1'], r['dec_a'], r['dec_b']), (5, 7, 9, 6))
        self.assertEqual((r['mint_a'], r['mint_b']), (SOL, USDC))
        o = dexes.decode_fee_state(orca_account(SQRT, 11, 13), 'orca')
        self.assertEqual((o['g0'], o['g1'], o['mint_a'], o['mint_b']), (11, 13, SOL, USDC))
        self.assertIsNone(dexes.decode_fee_state(b'short', 'raydium'))
        self.assertIsNone(dexes.decode_fee_state(b'short', 'orca'))

    def test_orca_decimals_come_from_the_mints(self):
        with mock.patch.object(dexes, 'pool_accounts', lambda addrs: {
                'P': orca_account(SQRT, 1, 1), SOL: bytes(44) + bytes([9]), USDC: bytes(44) + bytes([6])}
                if 'P' in addrs else {SOL: bytes(44) + bytes([9]), USDC: bytes(44) + bytes([6])}):
            dexes._MINT_DECIMALS.clear()
            st = dexes.fee_states([('orca', 'P'), ('meteora-dlmm', 'M')])
        self.assertEqual((st['P']['dec_a'], st['P']['dec_b']), (9, 6)); self.assertNotIn('M', st)


class Income(unittest.TestCase):
    def state(self, g0, g1):
        return {'sqrt_price': SQRT, 'g0': g0, 'g1': g1, 'dec_a': 9, 'dec_b': 6, 'rewards': []}

    def test_matches_a_direct_calculation(self):
        # a unit of raw liquidity earning 1 raw USDC per day: compare to its +/-1% value
        g1 = 2 ** 64                                         # +1 raw USDC per unit L
        inc = dexes.band_income(self.state(0, 0), self.state(0, g1), 86400, 1.01, 121.0, 1.0)
        sp = SQRT / 2 ** 64; sa, sb = sp / math.sqrt(1.01), sp * math.sqrt(1.01)
        value = (1 / sp - 1 / sb) / 1e9 * 121.0 + (sp - sa) / 1e6
        self.assertAlmostEqual(inc['fee_pct_day'], (1e-6 / value) * 100, places=9)

    def test_wrap_around_and_narrower_bands_earn_more(self):
        near = 2 ** 128 - 2 ** 60
        wrapped = dexes.band_income(self.state(near, 0), self.state(2 ** 60, 0), 3600, 1.01, 121.0, 1.0)
        plain = dexes.band_income(self.state(0, 0), self.state(2 ** 61, 0), 3600, 1.01, 121.0, 1.0)
        self.assertAlmostEqual(wrapped['fee_pct_day'], plain['fee_pct_day'])
        wide = dexes.band_income(self.state(0, 0), self.state(2 ** 61, 0), 3600, 1.05, 121.0, 1.0)
        self.assertGreater(plain['fee_pct_day'], wide['fee_pct_day'] * 4)
        self.assertIsNone(dexes.band_income(self.state(0, 0), self.state(1, 1), 0, 1.01, 121.0, 1.0))


class Sampling(unittest.TestCase):
    def test_cadence_and_candidates(self):
        calls = []
        rows = [{'address': 'R2', 'dex': 'raydium-clmm', 'screen_ok': True, 'skipped': None, 'pair': 'SOL/USDC',
                 'token_a': {'address': SOL}, 'token_b': {'address': USDC}},
                {'address': 'M1', 'dex': 'meteora-dlmm', 'screen_ok': True, 'skipped': None,
                 'token_a': {'address': SOL}, 'token_b': {'address': USDC}},
                {'address': 'X1', 'dex': 'orca', 'screen_ok': True, 'skipped': None,
                 'token_a': {'address': SOL}, 'token_b': {'address': 'JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'}},
                {'address': 'U1', 'dex': 'orca', 'screen_ok': False, 'skipped': None,
                 'token_a': {'address': SOL}, 'token_b': {'address': USDC}}]
        with mock.patch.object(rebalancer.db, 'latest_scan', lambda max_age_seconds=None: ({'id': 1}, rows)), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer.config, 'POOL', 'HELD'), \
                mock.patch.object(rebalancer.config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(rebalancer.config, 'ALLOW_SWAP', False):
            got = {a for _, a, _ in rebalancer.venue_candidates()}
            self.assertEqual(got, {'HELD', 'R2'})           # Meteora: no counters; X1: other pair; U1: unscreened
            with mock.patch.object(rebalancer.dexes, 'fee_states', lambda pools: calls.append(pools) or {}), \
                    mock.patch.object(rebalancer, 'save', lambda s: None), \
                    mock.patch.object(rebalancer.config, 'VENUE_SAMPLE_S', 600):
                st = {'last_fee_sample': time.time() - 30}
                rebalancer.sample_fee_growth(st); self.assertEqual(calls, [])
                st['last_fee_sample'] = time.time() - 700
                rebalancer.sample_fee_growth(st); self.assertEqual(len(calls), 1)


class Ledger(unittest.TestCase):
    def test_samples_span_and_two_day_window(self):
        with rebalancer.db.cursor(commit=True) as cur:
            cur.execute('truncate fee_growth')
            cur.execute("insert into fee_growth (ts, dex, pool, sqrt_price, g0, g1, dec_a, dec_b, mint_a, mint_b) "
                        "values (now() - interval '3 days', 'orca', 'P', 1, 0, 0, 9, 6, 'a', 'b')")
        st = {'sqrt_price': SQRT, 'g0': 1, 'g1': 2, 'dec_a': 9, 'dec_b': 6, 'mint_a': SOL, 'mint_b': USDC, 'rewards': []}
        rebalancer.db.record_fee_state('orca', 'P', st)
        self.assertIsNone(rebalancer.db.fee_state_span('P'))            # the old row is gone, one sample left
        rebalancer.db.record_fee_state('orca', 'P', dict(st, g0=5))
        first, last, secs = rebalancer.db.fee_state_span('P')
        self.assertEqual((int(first['g0']), int(last['g0'])), (1, 5)); self.assertGreaterEqual(secs, 0)


class RewardPrices(unittest.TestCase):
    def test_a_failed_fetch_uses_the_last_good_price_for_six_hours(self):
        rebalancer._REWARD_PX.clear()
        with mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda m: {'CAKE': 2.7}):
            self.assertEqual(rebalancer.reward_prices(['CAKE']), {'CAKE': 2.7})
        with mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda m: {}):
            self.assertEqual(rebalancer.reward_prices(['CAKE']), {'CAKE': 2.7})
        rebalancer._REWARD_PX['CAKE'] = (time.time() - 7 * 3600, 2.7)
        with mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda m: {}):
            self.assertEqual(rebalancer.reward_prices(['CAKE']), {})
        rebalancer._REWARD_PX.clear()
