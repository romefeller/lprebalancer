"""The hot pause on Uniswap v3 venues (Polygon, Unichain): the pool's own fee
counters sampled into fee_growth (2026-10-08: Polygon had none, so the pause
read "no data" and could never fire).

Covered: the Q128 -> Q64 conversion keeps dexes.fee_yield exact against the
native v3 formula, through a wrap of the 256-bit counters, and the price;
the live-read wrapper asks the pool and its tokens on the dex's chain; the
sampler runs every VENUE_SAMPLE_S under the profile's pool key, says a failed
read once, and only on a v3 venue of a chain without the Solana venue
counters; with real fee_growth and tape rows in the test database,
hot_pause_view reads a ratio for the Polygon pool and calls a bad moment bad
and a good one good."""
import datetime as dt
import random
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import chains
import config
import db
import dexes
import rebalancer

POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7'
WPOL = '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270'
USDT0 = '0xc2132d05d31c914a87c6611c10748aeb04b58e8f'
DEX = 'uniswap-v3-polygon'
Q96, Q128, U256 = 2 ** 96, 2 ** 128, 2 ** 256


def raw(g0, g1, sp96, dec_a=18, dec_b=6):
    return {'g0_x128': g0 % U256, 'g1_x128': g1 % U256, 'sqrt_price_x96': sp96, 'dec_a': dec_a, 'dec_b': dec_b,
            'mint_a': WPOL, 'mint_b': USDT0}


def native_yield(a, b, usd_a, usd_b):
    """The v3 fee per full-range dollar, straight from the Q128 / Q96 counters."""
    d0 = ((b['g0_x128'] - a['g0_x128']) % U256) / Q128 / 10 ** b['dec_a'] * usd_a
    d1 = ((b['g1_x128'] - a['g1_x128']) % U256) / Q128 / 10 ** b['dec_b'] * usd_b
    return (d0 + d1) / (2 * b['sqrt_price_x96'] / Q96 / 10 ** b['dec_b'] * usd_b)


class Conversion(unittest.TestCase):
    SP = int((0.0971 * 10 ** (6 - 18)) ** 0.5 * Q96)                      # $0.0971 per WPOL

    def test_the_price_and_the_fields(self):
        st = dexes.v3_fee_state(raw(5 * 2 ** 100, 7 * 2 ** 90, self.SP))
        self.assertAlmostEqual((st['sqrt_price'] / 2 ** 64) ** 2 * 1e12, 0.0971, places=9)
        self.assertEqual((st['g0'], st['g1']), (5 * 2 ** 36, 7 * 2 ** 26))
        self.assertEqual((st['dec_a'], st['dec_b'], st['mint_a'], st['mint_b'], st['rewards']), (18, 6, WPOL, USDT0, []))

    def test_property_fee_yield_matches_the_native_v3_formula(self):
        rng = random.Random(10)
        for _ in range(500):
            g0, g1 = rng.randrange(0, U256), rng.randrange(0, U256)
            d0, d1 = rng.randrange(2 ** 90, 2 ** 150), rng.randrange(2 ** 80, 2 ** 140)
            a, b = raw(g0, g1, self.SP), raw(g0 + d0, g1 + d1, self.SP)
            got = dexes.fee_yield(dexes.v3_fee_state(a), dexes.v3_fee_state(b), 0.0971, 1.0)
            want = native_yield(a, b, 0.0971, 1.0)
            self.assertAlmostEqual(got / want, 1.0, places=9, msg=(g0, g1, d0, d1))

    def test_a_wrap_of_the_256_bit_counters_is_a_small_difference(self):
        a = raw(U256 - 2 ** 100, U256 - 2 ** 95, self.SP)
        b = raw(2 ** 101, 2 ** 96, self.SP)                                  # wrapped past 2^256
        got = dexes.fee_yield(dexes.v3_fee_state(a), dexes.v3_fee_state(b), 0.0971, 1.0)
        self.assertGreater(got, 0)
        self.assertAlmostEqual(got / native_yield(a, b, 0.0971, 1.0), 1.0, places=9)


def word(n):
    return f'{n % U256:064x}'


class LiveRead(unittest.TestCase):
    def test_one_batch_of_the_pool_then_the_token_decimals_on_polygon(self):
        asked = []
        sel = dexes._SEL

        def calls(pairs, urls=None, timeout=20, chain='Base'):
            asked.append((tuple(pairs), chain, tuple(urls or ())))
            ans = {sel['feeGrowthGlobal0X128']: word(3 * 2 ** 100), sel['feeGrowthGlobal1X128']: word(2 ** 90),
                   sel['slot0']: word(Conversion.SP) + word(-299000) + word(0) * 5,
                   sel['token0']: word(int(WPOL, 16)), sel['token1']: word(int(USDT0, 16))}
            dec = {WPOL: 18, USDT0: 6}
            return ['0x' + (ans[d] if to == POOL else word(dec[to.lower()])) for to, d in pairs]
        with mock.patch.object(dexes, 'evm_calls', calls):
            st = dexes.uniswap_v3_fee_state(POOL, dex=DEX, urls=('http://127.0.0.1:9',))
        self.assertEqual([c[1] for c in asked], ['Polygon', 'Polygon'])
        self.assertEqual({d for _, d in asked[0][0]}, {sel['feeGrowthGlobal0X128'], sel['feeGrowthGlobal1X128'],
                                                       sel['slot0'], sel['token0'], sel['token1']})
        self.assertEqual(st['g0'], 3 * 2 ** 36)
        self.assertEqual(st['sqrt_price'], Conversion.SP >> 32)              # slot0's first word, not the tick
        self.assertEqual({u for _, _, us in asked for u in us}, {'http://127.0.0.1:9'})   # explicit endpoints only
        self.assertEqual((st['mint_a'], st['mint_b'], st['dec_a'], st['dec_b']), (WPOL, USDT0, 18, 6))

    def test_without_endpoints_it_asks_the_dex_chain_with_the_override_first(self):
        asked = []

        def calls(pairs, urls=None, timeout=20, chain='Base'):
            asked.append((chain, tuple(urls)))
            raise RuntimeError('stop here')
        with mock.patch.object(dexes, 'evm_calls', calls), \
                mock.patch.dict('os.environ', {'LPBOT_POLYGON_RPC': 'https://own.example', 'LPBOT_UNICHAIN_RPC': 'https://uni.example'}):
            with self.assertRaises(RuntimeError):
                dexes.uniswap_v3_fee_state(POOL, dex=DEX)
        chain, urls = asked[0]
        self.assertEqual(chain, 'Polygon')
        self.assertEqual(urls[0], 'https://own.example')
        self.assertIn('https://polygon-bor-rpc.publicnode.com', urls)
        self.assertNotIn('https://uni.example', urls)

    def test_the_selectors(self):
        self.assertEqual(dexes._SEL['feeGrowthGlobal0X128'], '0x' + dexes.keccak256(b'feeGrowthGlobal0X128()')[:4].hex())
        self.assertEqual(dexes._SEL['feeGrowthGlobal1X128'], '0x' + dexes.keccak256(b'feeGrowthGlobal1X128()')[:4].hex())


class Sampler(unittest.TestCase):
    def go(self, chain='polygon', dex=DEX, last=0.0, boom=None, polls=1):
        recorded, told, read = [], [], []
        state = {'last_fee_sample': last}

        def fee_state(pool, dex=None):
            read.append((pool, dex))
            if boom:
                raise boom
            return {'g0': 1}
        with mock.patch.object(config, 'CAPS', chains.caps(chain)), mock.patch.object(config, 'DEX', dex), \
                mock.patch.object(config, 'POOL', POOL), mock.patch.object(config, 'VENUE_SAMPLE_S', 600), \
                mock.patch.object(rebalancer.dexes, 'uniswap_v3_fee_state', fee_state), \
                mock.patch.object(rebalancer.db, 'record_fee_state', lambda *a: recorded.append(a)), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: told.append(ev)):
            for _ in range(polls):
                rebalancer.sample_fee_growth(state)
                state['last_fee_sample'] -= 601 if polls > 1 else 0
        return recorded, told, read, state

    def test_a_v3_venue_samples_its_pool_under_the_profile_key(self):
        recorded, told, read, state = self.go()
        self.assertEqual(read, [(POOL, DEX)])
        self.assertEqual(recorded, [(DEX, POOL, {'g0': 1})])
        self.assertEqual(told, [])
        self.assertGreater(state['last_fee_sample'], time.time() - 5)

    def test_every_venue_sample_seconds_only(self):
        recorded, _, read, _ = self.go(last=time.time() - 599)
        self.assertEqual((recorded, read), ([], []))
        recorded, _, _, _ = self.go(last=time.time() - 601)
        self.assertEqual(len(recorded), 1)

    def test_exactly_the_interval_samples_one_second_less_does_not(self):
        now = 1_800_000_000.0
        with mock.patch.object(rebalancer.time, 'time', lambda: now):
            self.assertEqual(self.go(last=now - 599)[2], [])
            self.assertEqual(len(self.go(last=now - 600)[2]), 1)

    def test_unichain_too_and_not_aerodrome_nor_solana(self):
        self.assertEqual(len(self.go(chain='unichain', dex='uniswap-v3-unichain')[0]), 1)
        self.assertEqual(self.go(chain='base', dex='aerodrome-slipstream')[2], [])
        with mock.patch.object(rebalancer, 'venue_candidates', lambda: []), \
                mock.patch.object(rebalancer.dexes, 'fee_states', lambda pools: {}):
            self.assertEqual(self.go(chain='solana', dex='raydium-clmm')[2], [])   # the Solana path, not this one

    def test_a_failed_read_is_said_once_records_nothing_and_never_raises(self):
        recorded, told, read, state = self.go(boom=RuntimeError('all Polygon RPC endpoints failed'), polls=3)
        self.assertEqual(len(read), 3)
        self.assertEqual(recorded, [])
        self.assertEqual(told, ['venue_sample_failed'])
        self.assertIn('Polygon', state['fee_sample_failed_told'])

    def test_a_good_read_after_a_failure_clears_the_note(self):
        _, _, _, state = self.go(boom=RuntimeError('down'))
        self.assertIn('fee_sample_failed_told', state)
        state['last_fee_sample'] = 0
        with mock.patch.object(config, 'CAPS', chains.caps('polygon')), mock.patch.object(config, 'DEX', DEX), \
                mock.patch.object(rebalancer.dexes, 'uniswap_v3_fee_state', lambda p, dex=None: {'g0': 1}), \
                mock.patch.object(rebalancer.db, 'record_fee_state', lambda *a: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None):
            rebalancer.sample_fee_growth(state)
        self.assertNotIn('fee_sample_failed_told', state)


class SolanaSampler(unittest.TestCase):
    """The Solana branch of sample_fee_growth, unchanged by the v3 work and pinned here."""

    def go(self, last, now=1_800_000_000.0, status=None, states=None):
        recorded, viewed, told = [], [], []
        cands = [('raydium-clmm', 'A1', None), ('orca', 'B2', None)]
        with mock.patch.object(config, 'CAPS', chains.caps('solana')), mock.patch.object(config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(config, 'VENUE_SAMPLE_S', 600), mock.patch.object(rebalancer.time, 'time', lambda: now), \
                mock.patch.object(rebalancer, 'venue_candidates', lambda: cands), \
                mock.patch.object(rebalancer.dexes, 'fee_states', lambda pools: {'A1': {'g0': 1}} if states is None else states), \
                mock.patch.object(rebalancer.db, 'record_fee_state', lambda *a: recorded.append(a)), \
                mock.patch.object(rebalancer, 'venue_view', lambda px, q: viewed.append((px, q))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: told.append(ev)):
            state = {'last_fee_sample': last}
            rebalancer.sample_fee_growth(state, status)
        return recorded, viewed, told, state

    def test_exactly_the_interval_samples_one_second_less_does_not(self):
        now = 1_800_000_000.0
        self.assertEqual(self.go(last=now - 599)[0], [])
        self.assertEqual(len(self.go(last=now - 600)[0]), 1)
        self.assertEqual(self.go(last=now - 600)[3]['last_fee_sample'], now)
        self.assertEqual(len(self.go(last=0)[0]), 1)                          # never sampled: samples

    def test_only_the_candidates_with_counters_are_recorded(self):
        recorded, _, _, _ = self.go(last=0)
        self.assertEqual(recorded, [('raydium-clmm', 'A1', {'g0': 1})])
        self.assertEqual(self.go(last=0, states={})[0], [])

    def test_the_ranking_is_refreshed_only_with_a_priced_status(self):
        self.assertEqual(self.go(last=0, status={'price': 120.0, 'quoteUsd': 1.0})[1], [(120.0, 1.0)])
        self.assertEqual(self.go(last=0, status={'price': 120.0, 'quoteUsd': 0.0})[1], [(120.0, 0.0)])
        self.assertEqual(self.go(last=0, status={'price': 120.0, 'quoteUsd': None})[1], [])
        self.assertEqual(self.go(last=0, status=None)[1:3], ([], []))         # no status: no ranking, no failure
        self.assertEqual(self.go(last=0, status={})[1], [])


class ViewOnTheDatabase(unittest.TestCase):
    """hot_pause_view with real fee_growth and tape5 rows (tests/run.sh's database)."""

    KEY = '0xTEST9B08hotpause'

    def setUp(self):
        self.addCleanup(self.clean)
        self.clean()

    def clean(self):
        with db.cursor(commit=True) as cur:
            cur.execute('delete from fee_growth where pool = %s', (self.KEY,))
            cur.execute('delete from tape5 where pool = %s', (self.KEY,))

    def seed(self, fee_x128_per_side, move=0.004):
        now = int(time.time()) // 300 * 300
        t0 = now - 66 * 300                   # 5.5 h: inside the 6 h window, over its 80% minimum
        sp = Conversion.SP
        a = dexes.v3_fee_state(raw(10 ** 40, 10 ** 30, sp))
        b = dexes.v3_fee_state(raw(10 ** 40 + fee_x128_per_side, 10 ** 30 + fee_x128_per_side // 10 ** 12, sp))
        with db.cursor(commit=True) as cur:
            for ts, st in ((t0, a), (now, b)):
                cur.execute('insert into fee_growth (ts, dex, pool, sqrt_price, g0, g1, rewards, dec_a, dec_b, mint_a, mint_b) '
                            'values (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                            (ts, DEX, self.KEY, st['sqrt_price'], st['g0'], st['g1'], '[]', 18, 6, WPOL, USDT0))
            c = 0.0971 * np.exp(np.cumsum(np.tile([move, -move], 33)))
            for i, close in enumerate(float(x) for x in c):
                cur.execute('insert into tape5 (pool, ts, open, high, low, close, volume) values (%s,%s,%s,%s,%s,%s,%s)',
                            (self.KEY, t0 + i * 300, close, close, close, close, 1.0))
        return t0, now

    def view(self):
        with mock.patch.object(config, 'HOT_PAUSE_HOT_PCT', 2.0), mock.patch.object(config, 'HOT_PAUSE_FG_HOURS', 6.0), \
                mock.patch.object(config, 'HOT_PAUSE_FG_THRESHOLD', 0.8):
            return rebalancer.hot_pause_view(self.KEY, 0.0971, 1.0, 1.05)

    def test_a_polygon_pool_with_samples_has_a_ratio_and_a_verdict(self):
        self.seed(fee_x128_per_side=10 ** 33)
        low = self.view()
        self.assertTrue(low['hot'])
        self.assertIsNotNone(low['ratio'], 'with v3 samples the pause reads a ratio, not "no data"')
        self.clean()
        self.seed(fee_x128_per_side=10 ** 33 * int(2 / max(low['ratio'], 1e-9) + 1))
        high = self.view()
        self.assertGreater(high['ratio'], 0.8)
        self.assertFalse(high['bad'])
        self.assertEqual(low['bad'], low['ratio'] < 0.8)

    def test_without_samples_it_is_no_data_and_never_bad(self):
        out = self.view()
        self.assertEqual((out['ratio'], out['bad']), (None, False))


if __name__ == '__main__':
    unittest.main()
