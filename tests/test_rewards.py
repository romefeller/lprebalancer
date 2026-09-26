"""Rewards: parsed from every venue, priced into the board, swept after a harvest;
and the pool review that keeps running while calm holds the tight band."""
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures
_fixtures.ensure_profile()

import dexes       # noqa: E402
import engine      # noqa: E402
import fees        # noqa: E402
import rebalancer  # noqa: E402

SOL, USDC = fees.NATIVE_MINT, 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
RAY = '4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'


class Parse(unittest.TestCase):
    def test_orca_counts_only_live_programs(self):
        live = {'rewards': [{'mint': 'M1', 'active': True, 'emissionsPerSecond': '0.5'}],
                'stats': {'24h': {'rewards': '120.5'}}}
        self.assertEqual(dexes.orca_rewards(live), {'reward_usd_day': 120.5, 'reward_mints': ['M1']})
        dead = {'rewards': [{'mint': 'M1', 'active': False, 'emissionsPerSecond': '0'}],
                'stats': {'24h': {'rewards': '5'}}}
        self.assertEqual(dexes.orca_rewards(dead)['reward_usd_day'], 0.0)

    def test_raydium_apr_to_dollars_and_ended_programs(self):
        p = {'tvl': 3_650_000, 'day': {'rewardApr': [10, 0]},
             'rewardDefaultInfos': [{'mint': {'address': RAY}, 'perSecond': '10', 'endTime': time.time() + 999}]}
        r = dexes.raydium_rewards(p)
        self.assertAlmostEqual(r['reward_usd_day'], 1000.0); self.assertEqual(r['reward_mints'], [RAY])
        p['rewardDefaultInfos'][0]['endTime'] = time.time() - 10
        self.assertEqual(dexes.raydium_rewards(p)['reward_usd_day'], 0.0)

    def test_meteora_farm_and_null_mints(self):
        p = {'tvl': 365_000, 'has_farm': True, 'farm_apr': 20.0,
             'reward_mint_x': 'MX', 'reward_mint_y': dexes.NULL_MINT}
        r = dexes.meteora_rewards(p)
        self.assertAlmostEqual(r['reward_usd_day'], 200.0); self.assertEqual(r['reward_mints'], ['MX'])
        self.assertEqual(dexes.meteora_rewards({'has_farm': False, 'farm_apr': 20, 'tvl': 1})['reward_usd_day'], 0.0)


class Board(unittest.TestCase):
    def pool(self, reward):
        n = 24 * 41 + 1
        rng = np.random.default_rng(11)
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.008, n)))
        rec = {'tokenA': {'decimals': 9, 'symbol': 'SOL'}, 'tokenB': {'decimals': 6, 'symbol': 'USDC'},
               'liquidity': str(int(20 * (2e7 / (2 * px[-1] ** 0.5)) * (10 ** 15) ** 0.5)),
               'tvlUsdc': '20000000', 'price': str(px[-1]), 'feeRate': 400}
        rec = engine.as_record(rec); rec['reward_usd_day'] = reward
        return rec, (np.arange(n) * 3600, px, np.full(n, 3e6))

    def test_rewards_add_to_the_score_by_concentration(self):
        rec0, cd = self.pool(0.0)
        rec1, _ = self.pool(20_000.0)
        r0, _ = engine.ladder(rec0, cd, (1.03, 1.12), 190.0, policy={})
        r1, _ = engine.ladder(rec1, cd, (1.03, 1.12), 190.0, policy={})
        for a, b in zip(r0, r1):
            self.assertEqual(a['reward_day_pct'], 0.0)
            self.assertAlmostEqual(b['net_day_pct'] - a['net_day_pct'], b['reward_day_pct'])
        self.assertGreater(r1[0]['reward_day_pct'], r1[1]['reward_day_pct'])     # narrower earns more

    def test_density_includes_rewards_and_is_band_free(self):
        rows = [{'net_day_pct': 0.3, 'band': 1.08, 'c_pool': 20.0, 'tvl_usd': 1e7, 'fees_24h_usd': 1e4,
                 'reward_usd_day': 1e4, 'path': {'fees': 1, 'days': 1}}]
        engine.realised_check(rows)
        self.assertAlmostEqual(rows[0]['density'], 2e4 / 20 / 1e7)


class CalmReview(unittest.TestCase):
    def row(self, addr, dex, fees24, tvl=1e7, c=20.0, ok=True):
        return {'address': addr, 'dex': dex, 'pair': 'SOL/USDC', 'screen_ok': ok, 'skipped': None,
                'fees_24h_usd': fees24, 'reward_usd_day': 0.0, 'c_pool': c, 'tvl_usd': tvl,
                'token_a': {'address': SOL, 'symbol': 'SOL'}, 'token_b': {'address': USDC, 'symbol': 'USDC'}}

    def go(self, rows):
        moved, sent = [], []
        with mock.patch.object(rebalancer.db, 'latest_scan', lambda max_age_seconds=None: ({'id': 1}, rows)), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'rebalance', lambda *a, **k: moved.append(k)), \
                mock.patch.object(rebalancer.config, 'POOL', 'HELD'), \
                mock.patch.object(rebalancer.config, 'POOL_PINNED', False), \
                mock.patch.object(rebalancer.config, 'MIGRATE_MIN_GAIN', 0.25), \
                mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca', 'raydium-clmm')), \
                mock.patch.object(rebalancer.config, 'CALM_BAND', 1.01):
            r = rebalancer.calm_board_check({}, {'positionMint': 'M'})
        return r, moved, sent

    def test_moves_tight_to_a_denser_pool(self):
        r, moved, _ = self.go([self.row('HELD', 'raydium-clmm', 1e4), self.row('O', 'orca', 2e4)])
        self.assertTrue(r); self.assertEqual(moved[0]['band'], 1.01); self.assertTrue(moved[0]['calm_move'])
        self.assertEqual(moved[0]['target']['address'], 'O')

    def test_stays_under_the_gain_or_on_an_unarmed_or_unscreened_venue(self):
        self.assertFalse(self.go([self.row('HELD', 'raydium-clmm', 1e4), self.row('O', 'orca', 1.1e4)])[0])
        self.assertFalse(self.go([self.row('HELD', 'raydium-clmm', 1e4), self.row('P', 'byreal', 9e4)])[0])
        self.assertFalse(self.go([self.row('HELD', 'raydium-clmm', 1e4), self.row('O', 'orca', 9e4, ok=False)])[0])
        r, _, sent = self.go([self.row('O', 'orca', 9e4)])                      # held pool unscored
        self.assertFalse(r); self.assertIn('not scored', sent[-1][1]['verdict'])


class Sweep(unittest.TestCase):
    def go(self, sol, ray_amount, price=2.0, swap=({'signature': 's', 'bought': {'amount': 3.9}}, None)):
        calls, rows, state = [], [], {}
        def chain(*a, **k):
            calls.append((a, k.get('dex')))
            if a[0] == 'balance':
                return {'amount': ray_amount}, None
            if a[0] == 'swap':
                return swap
            if a[0] == 'send':
                return {'signature': 't'}, None
        rec = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}, 'reward_mints': [RAY]}
        with mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'REWARD_POLICY', 'payout'), \
                mock.patch.object(rebalancer.config, 'REWARD_MIN_USD', 1.0), \
                mock.patch.object(rebalancer.config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(rebalancer.config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(rebalancer, 'pool_record', lambda: rec), \
                mock.patch.object(rebalancer, 'wallet', lambda p: {'sol': sol}), \
                mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda m: {RAY: price}), \
                mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'record_payout', lambda *a, **k: rows.append(a[6])):
            rebalancer.distribute_rewards(state, 'M')
        return calls, rows, state

    def test_reward_swapped_to_usdc_and_paid(self):
        calls, rows, _ = self.go(sol=0.3, ray_amount=2.0)
        ops = [c[0][0] for c in calls]
        self.assertEqual(ops, ['balance', 'swap', 'send'])
        self.assertEqual(calls[1][0][2], USDC); self.assertEqual(calls[2][0][3], PROFIT)
        self.assertEqual(rows, ['paid'])

    def test_gas_low_swaps_to_sol_and_keeps_it(self):
        calls, rows, _ = self.go(sol=0.01, ray_amount=2.0)
        self.assertEqual([c[0][0] for c in calls], ['balance', 'swap'])
        self.assertEqual(calls[1][0][2], SOL); self.assertEqual(rows, ['gas'])

    def test_dust_waits_and_a_failed_swap_sends_nothing(self):
        calls, rows, _ = self.go(sol=0.3, ray_amount=0.2)                 # $0.40 < $1
        self.assertEqual([c[0][0] for c in calls], ['balance']); self.assertEqual(rows, [])
        calls, rows, _ = self.go(sol=0.3, ray_amount=2.0, swap=(None, 'impact'))
        self.assertEqual([c[0][0] for c in calls], ['balance', 'swap']); self.assertEqual(rows, [])

    def test_pool_tokens_are_never_swept(self):
        rec_calls = []
        with mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'REWARD_POLICY', 'payout'), \
                mock.patch.object(rebalancer, 'pool_record',
                                  lambda: {'token_a': {'address': SOL}, 'token_b': {'address': USDC},
                                           'reward_mints': [SOL]}), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: rec_calls.append(a)), \
                mock.patch.object(rebalancer, 'save', lambda s: None):
            self.assertIsNone(rebalancer.distribute_rewards({}, 'M'))
        self.assertEqual(rec_calls, [])


class ChainRewards(unittest.TestCase):
    def slot(self, state, open_t, end_t, eps, mint_bytes):
        b = bytes([state]) + open_t.to_bytes(8, 'little') + end_t.to_bytes(8, 'little') + bytes(8)
        b += int(eps * 2 ** 64).to_bytes(16, 'little') + bytes(16) + mint_bytes + bytes(32 * 2 + 16)
        assert len(b) == dexes.REWARD_INFO_LEN
        return b

    def test_decode_live_and_ended_slots(self):
        now = 1_790_000_000
        mint = bytes(range(1, 33))
        raw = bytes(dexes.REWARD_INFOS_OFFSET) + self.slot(2, now - 10, now + 10, 1000.5, mint) \
            + self.slot(3, now - 100, now - 1, 50.0, mint) + bytes(dexes.REWARD_INFO_LEN)
        out = dexes.decode_rewards(raw, now=now)
        self.assertEqual(len(out), 1); self.assertAlmostEqual(out[0][1], 1000.5, places=3)
        self.assertEqual(out[0][0], dexes.b58(mint))
        self.assertEqual(dexes.decode_rewards(b'', now=now), [])

    def test_chain_rewards_priced_by_mint_decimals(self):
        now_mint = bytes(range(1, 33))
        import time as _t
        raw = bytes(dexes.REWARD_INFOS_OFFSET) + self.slot(2, 0, int(_t.time()) + 999, 1e6, now_mint) \
            + bytes(dexes.REWARD_INFO_LEN * 2)
        m = dexes.b58(now_mint)
        mint_acct = bytes(44) + bytes([6]) + bytes(40)
        recs = [{'address': 'P', 'reward_usd_day': 0.0, 'reward_mints': []}]
        with mock.patch.object(dexes, 'jupiter_prices', lambda ms: {m: 2.0}), \
                mock.patch.object(dexes, 'pool_accounts', lambda addrs: {m: mint_acct}):
            dexes.attach_chain_rewards(recs, {'P': raw})
        # 1e6 raw/s at 6 decimals = 1 token/s -> 86400/day at $2
        self.assertAlmostEqual(recs[0]['reward_usd_day'], 172800.0)
        self.assertEqual(recs[0]['reward_mints'], [m])


class CloseRetry(unittest.TestCase):
    def test_a_rate_limited_close_is_retried_once_when_the_position_is_still_there(self):
        calls, sent = [], []
        results = iter([({'signature': 'h'}, None), (None, 'RPC rate limited'), ({'closed': 'M', 'signature': 'c'}, None)])
        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [], 'failures': 0}
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a[0]) or next(results))), \
                mock.patch.object(rebalancer, 'read_status', lambda *a: ({'positionMint': 'M'}, None)), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'wallet', lambda p: {}), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'reopen', lambda *a, **k: sent.append('REOPEN')), \
                mock.patch.object(rebalancer, 'distribute', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'distribute_rewards', lambda *a, **k: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None), \
                mock.patch.object(rebalancer.db, 'record_harvest', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'snapshot', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'close_position', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            rebalancer.rebalance(state, {'positionMint': 'M', 'price': 100, 'whirlpool': 'P'}, 'x')
        self.assertEqual(calls, ['harvest', 'close', 'close'])
        self.assertIn('close_retry', sent); self.assertIn('REOPEN', sent)
        self.assertEqual(state['failures'], 0)
