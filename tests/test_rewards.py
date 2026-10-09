"""Rewards: parsed from every venue, priced into the board, swept after a harvest;
and the pool review that keeps running while calm holds the tight band."""
import json
import os
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures
_fixtures.ensure_profile()

from venues import api as venue_api       # noqa: E402
from venues import solana_state       # noqa: E402
from venues.jupiter import prices as jupiter_api       # noqa: E402
from venues.meteora_dlmm import pools as meteora_pools       # noqa: E402
from venues.orca import pools as orca_pools       # noqa: E402
from venues.raydium_clmm import pools as raydium_pools       # noqa: E402
import engine      # noqa: E402
import fees        # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import txfees  # noqa: E402
import lp.board  # noqa: E402
import lp.harvest  # noqa: E402
import lp.moves  # noqa: E402

SOL, USDC = fees.NATIVE_MINT, 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
RAY = '4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'


class Parse(unittest.TestCase):
    def test_orca_counts_only_live_programs(self):
        live = {'rewards': [{'mint': 'M1', 'active': True, 'emissionsPerSecond': '0.5'}],
                'stats': {'24h': {'rewards': '120.5'}}}
        self.assertEqual(orca_pools.orca_rewards(live), {'reward_usd_day': 120.5, 'reward_mints': ['M1']})
        dead = {'rewards': [{'mint': 'M1', 'active': False, 'emissionsPerSecond': '0'}],
                'stats': {'24h': {'rewards': '5'}}}
        self.assertEqual(orca_pools.orca_rewards(dead)['reward_usd_day'], 0.0)

    def test_raydium_apr_to_dollars_and_ended_programs(self):
        p = {'tvl': 3_650_000, 'day': {'rewardApr': [10, 0]},
             'rewardDefaultInfos': [{'mint': {'address': RAY}, 'perSecond': '10', 'endTime': time.time() + 999}]}
        r = raydium_pools.raydium_rewards(p)
        self.assertAlmostEqual(r['reward_usd_day'], 1000.0); self.assertEqual(r['reward_mints'], [RAY])
        p['rewardDefaultInfos'][0]['endTime'] = time.time() - 10
        self.assertEqual(raydium_pools.raydium_rewards(p)['reward_usd_day'], 0.0)

    def test_meteora_farm_and_null_mints(self):
        p = {'tvl': 365_000, 'has_farm': True, 'farm_apr': 20.0,
             'reward_mint_x': 'MX', 'reward_mint_y': venue_api.NULL_MINT}
        r = meteora_pools.meteora_rewards(p)
        self.assertAlmostEqual(r['reward_usd_day'], 200.0); self.assertEqual(r['reward_mints'], ['MX'])
        self.assertEqual(meteora_pools.meteora_rewards({'has_farm': False, 'farm_apr': 20, 'tvl': 1})['reward_usd_day'], 0.0)


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
    """The venue review ranks on on-chain income and needs evidence."""
    def row(self, addr, dex):
        return {'address': addr, 'dex': dex, 'pair': 'SOL/USDC', 'screen_ok': True, 'skipped': None,
                'token_a': {'address': SOL, 'symbol': 'SOL'}, 'token_b': {'address': USDC, 'symbol': 'USDC'}}

    def go(self, venues):
        moved, sent = [], []
        with mock.patch.object(lp.board, 'venue_view', lambda price, q=1.0: venues), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: moved.append(k)), \
                mock.patch.object(lp.regime, 'regime_choice_now', lambda pool, price, pair=None: 1.0125), \
                mock.patch.object(config, 'POOL', 'HELD'), \
                mock.patch.object(config, 'POOL_PINNED', False), \
                mock.patch.object(config, 'MIGRATE_MIN_GAIN', 0.25), \
                mock.patch.object(config, 'VENUE_MIN_HOURS', 6), \
                mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.object(config, 'EXECUTE_DEXES', ('orca', 'raydium-clmm')):
            r = lp.board.calm_board_check({}, {'positionMint': 'M', 'price': 121.0, 'quoteUsd': 1.0})
        return r, moved, sent

    def v(self, addr, dex, pct, hours, held=False):
        return {'address': addr, 'dex': dex, 'held': held, 'pair': 'SOL/USDC', 'total_pct_day': pct,
                'fee_pct_day': pct, 'reward_pct_day': 0.0, 'hours': hours, 'row': None if held else self.row(addr, dex)}

    def test_moves_to_a_pool_that_earns_25pct_more_on_chain(self):
        r, moved, _ = self.go([self.v('O', 'orca', 1.4, 8), self.v('HELD', 'raydium-clmm', 1.0, 8, True)])
        self.assertTrue(r); self.assertEqual(moved[0]['target']['address'], 'O'); self.assertEqual(moved[0]['band'], 1.0125)

    def test_stays_without_enough_evidence_gain_or_signer(self):
        self.assertFalse(self.go([self.v('O', 'orca', 1.4, 3), self.v('HELD', 'raydium-clmm', 1.0, 8, True)])[0])
        self.assertFalse(self.go([self.v('O', 'orca', 1.4, 8), self.v('HELD', 'raydium-clmm', 1.0, 2, True)])[0])
        self.assertFalse(self.go([self.v('O', 'orca', 1.2, 8), self.v('HELD', 'raydium-clmm', 1.0, 8, True)])[0])
        self.assertFalse(self.go([self.v('P', 'pancakeswap-v3-solana', 3.0, 8), self.v('HELD', 'raydium-clmm', 1.0, 8, True)])[0])
        r, _, sent = self.go([self.v('O', 'orca', 1.4, 8)])                       # no held-pool evidence
        self.assertFalse(r); self.assertIn('held pool', sent[-1][1]['verdict'])

OWNER = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'


def harvest_tx(mint, amount):
    """A harvest transaction that brought `amount` of `mint` to OWNER."""
    tb = lambda a: {'accountIndex': 3, 'owner': OWNER, 'mint': mint,
                    'uiTokenAmount': {'amount': str(int(a * 1e6)), 'decimals': 6, 'uiAmountString': str(a)}}
    return {'meta': {'err': None, 'preTokenBalances': [tb(0.0)], 'postTokenBalances': [tb(amount)]}}


class Sweep(unittest.TestCase):
    def go(self, sol, ray_amount, price=2.0, swap=({'signature': 's', 'bought': {'amount': 3.9}}, None),
           brought=None, sigs=('H',), theirs=()):
        calls, rows, state = [], [], {}
        brought = ray_amount if brought is None else brought
        target_bal = iter([10.0, 13.9])                  # the target mint before and after the swap
        def chain(*a, **k):
            calls.append((a, k.get('dex')))
            if a[0] == 'balance' and a[1] == RAY:
                return {'amount': ray_amount}, None
            if a[0] == 'balance':
                return {'amount': next(target_bal, 13.9)}, None
            if a[0] == 'swap':
                calls[-1] = (a, k.get('dex'), k.get('extra_env'))
                return swap
            if a[0] == 'send':
                return {'signature': 't'}, None
        rec = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}, 'reward_mints': [RAY]}
        with mock.patch.dict(os.environ, {'LPBOT_PROFIT_WALLET_PIN': PROFIT}), \
                mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(config, 'REWARD_POLICY', 'payout'), \
                mock.patch.object(config, 'REWARD_MIN_USD', 1.0), \
                mock.patch.object(config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(lp.capital, 'pool_record', lambda: rec), \
                mock.patch.object(lp.capital, 'wallet', lambda p: {'sol': sol, 'owner': OWNER}), \
                mock.patch.object(lp.swaps, 'wallet_mints', lambda: set(theirs)), \
                mock.patch.object(txfees, 'fetch', lambda rpc, s, **k: harvest_tx(RAY, brought)), \
                mock.patch.object(jupiter_api, 'jupiter_prices', lambda m: {RAY: price}), \
                mock.patch.object(lp.signers, 'chain', chain), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(db, 'record_payout', lambda *a, **k: rows.append(a[6])):
            lp.harvest.distribute_rewards(state, 'M', list(sigs))
        return calls, rows, state

    def test_reward_swapped_to_usdc_and_paid(self):
        calls, rows, _ = self.go(sol=0.3, ray_amount=2.0)
        ops = [c[0][0] for c in calls]
        self.assertEqual(ops, ['balance', 'balance', 'swap', 'balance', 'send'])
        self.assertEqual(calls[2][0][2], USDC); self.assertEqual(calls[4][0][3], PROFIT)
        self.assertAlmostEqual(float(calls[4][0][2]), 3.9)             # what arrived
        self.assertEqual(rows, ['paid'])

    def test_gas_low_swaps_to_sol_and_keeps_it(self):
        calls, rows, _ = self.go(sol=0.01, ray_amount=2.0)
        self.assertEqual([c[0][0] for c in calls], ['balance', 'balance', 'swap', 'balance'])
        self.assertEqual(calls[2][0][2], SOL); self.assertEqual(rows, ['gas'])

    def test_dust_waits_and_a_failed_swap_sends_nothing(self):
        calls, rows, _ = self.go(sol=0.3, ray_amount=0.2)                 # $0.40 < $1
        self.assertEqual([c[0][0] for c in calls], ['balance']); self.assertEqual(rows, [])
        calls, rows, _ = self.go(sol=0.3, ray_amount=2.0, swap=(None, 'impact'))
        self.assertEqual([c[0][0] for c in calls], ['balance', 'balance', 'swap', 'balance']); self.assertEqual(rows, [])

    def test_only_what_the_harvest_brought_is_sold_and_the_swap_is_told(self):
        # the wallet holds 50 RAY, this harvest brought 2: another profile's RAY stays
        calls, rows, state = self.go(sol=0.3, ray_amount=50.0, brought=2.0)
        swap = [c for c in calls if c[0][0] == 'swap'][0]
        self.assertEqual(swap[0][3], '2.000000000')
        self.assertEqual(json.loads(swap[2]['LPBOT_SLEEVE']), {RAY: 2.0})
        self.assertEqual(state['reward_due'], {RAY: 0.0})

    def test_an_unmeasured_harvest_pays_no_reward(self):
        calls, rows, state = self.go(sol=0.3, ray_amount=50.0, sigs=())
        self.assertEqual((calls, rows), ([], []))
        calls, rows, state = self.go(sol=0.3, ray_amount=2.0, brought=0.2)       # $0.40 waits, carried
        self.assertEqual(rows, []); self.assertAlmostEqual(state['reward_due'][RAY], 0.2)

    def test_another_profiles_mint_is_never_a_reward_here(self):
        calls, rows, _ = self.go(sol=0.3, ray_amount=2.0, theirs=(RAY,))
        self.assertEqual((calls, rows), ([], []))

    def test_pool_tokens_are_never_swept(self):
        rec_calls = []
        with mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(config, 'REWARD_POLICY', 'payout'), \
                mock.patch.object(lp.capital, 'pool_record',
                                  lambda: {'token_a': {'address': SOL}, 'token_b': {'address': USDC},
                                           'reward_mints': [SOL]}), \
                mock.patch.object(lp.signers, 'chain', lambda *a, **k: rec_calls.append(a)), \
                mock.patch.object(lp.paths, 'save', lambda s: None):
            self.assertIsNone(lp.harvest.distribute_rewards({}, 'M'))
        self.assertEqual(rec_calls, [])


class ChainRewards(unittest.TestCase):
    def slot(self, state, open_t, end_t, eps, mint_bytes):
        b = bytes([state]) + open_t.to_bytes(8, 'little') + end_t.to_bytes(8, 'little') + bytes(8)
        b += int(eps * 2 ** 64).to_bytes(16, 'little') + bytes(16) + mint_bytes + bytes(32 * 2 + 16)
        assert len(b) == solana_state.REWARD_INFO_LEN
        return b

    def test_decode_live_and_ended_slots(self):
        now = 1_790_000_000
        mint = bytes(range(1, 33))
        raw = bytes(solana_state.REWARD_INFOS_OFFSET) + self.slot(2, now - 10, now + 10, 1000.5, mint) \
            + self.slot(3, now - 100, now - 1, 50.0, mint) + bytes(solana_state.REWARD_INFO_LEN)
        out = solana_state.decode_rewards(raw, now=now)
        self.assertEqual(len(out), 1); self.assertAlmostEqual(out[0][1], 1000.5, places=3)
        self.assertEqual(out[0][0], solana_state.b58(mint))
        self.assertEqual(solana_state.decode_rewards(b'', now=now), [])

    def test_chain_rewards_priced_by_mint_decimals(self):
        now_mint = bytes(range(1, 33))
        import time as _t
        raw = bytes(solana_state.REWARD_INFOS_OFFSET) + self.slot(2, 0, int(_t.time()) + 999, 1e6, now_mint) \
            + bytes(solana_state.REWARD_INFO_LEN * 2)
        m = solana_state.b58(now_mint)
        mint_acct = bytes(44) + bytes([6]) + bytes(40)
        recs = [{'address': 'P', 'reward_usd_day': 0.0, 'reward_mints': []}]
        with mock.patch.object(jupiter_api, 'jupiter_prices', lambda ms: {m: 2.0}), \
                mock.patch.object(solana_state, 'pool_accounts', lambda addrs: {m: mint_acct}):
            solana_state.attach_chain_rewards(recs, {'P': raw})
        # 1e6 raw/s at 6 decimals = 1 token/s -> 86400/day at $2
        self.assertAlmostEqual(recs[0]['reward_usd_day'], 172800.0)
        self.assertEqual(recs[0]['reward_mints'], [m])


class CloseRetry(unittest.TestCase):
    # 2026-10-07: the real error of a close sent once and expired, and the
    # Raydium signer's NeverLanded after its rebuilds.
    EXPIRED = ('ERROR: Signature 5kPK has expired: block height exceeded',
               'expired: block height passed and the chain has no record of 5kPK; nothing was sent')

    def test_a_rate_limited_close_is_retried_once_when_the_position_is_still_there(self):
        for err in ('RPC rate limited', *self.EXPIRED):
            with self.subTest(err=err):
                calls, sent, state = self.close_after(err)
                self.assertEqual(calls, ['harvest', 'close', 'close'])
                self.assertIn('close_retry', sent); self.assertIn('REOPEN', sent)
                self.assertEqual(state['failures'], 0)

    def test_a_program_refusal_is_not_retried(self):
        calls, sent, state = self.close_after('PriceSlippageCheck (6017): price moved beyond the slippage limit')
        self.assertEqual(calls, ['harvest', 'close'])
        self.assertNotIn('close_retry', sent); self.assertIn('close_failed', sent)
        self.assertEqual(state['failures'], 1)

    def test_the_retry_pattern(self):
        for err in ('429', 'request timed out', 'ECONNRESET', 'Blockhash not found', *self.EXPIRED):
            self.assertRegex(err, '(?i)' + lp.moves.CLOSE_RETRY_ERRORS)
        for err in ('PriceSlippageCheck (6017)', 'transaction failed on chain: {"InstructionError":[2,{"Custom":1}]}',
                    'position M not found for this wallet on this pool', 'partial send: 1/2 sent'):
            self.assertNotRegex(err, '(?i)' + lp.moves.CLOSE_RETRY_ERRORS)

    def close_after(self, err):
        """A rebalance whose first close fails with `err` and whose second
        lands, the position still there in between: (signer calls, notices,
        state)."""
        calls, sent = [], []
        results = iter([({'signature': 'h'}, None), (None, err), ({'closed': 'M', 'signature': 'c'}, None)])
        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [], 'failures': 0}
        with mock.patch.object(lp.signers, 'chain', lambda *a, **k: (calls.append(a[0]) or next(results))), \
                mock.patch.object(lp.signers, 'read_status', lambda *a: ({'positionMint': 'M'}, None)), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(lp.books, 'notify_book', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(lp.capital, 'wallet', lambda p: {}), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.moves, 'reopen', lambda *a, **k: sent.append('REOPEN')), \
                mock.patch.object(lp.harvest, 'distribute', lambda *a, **k: None), \
                mock.patch.object(lp.harvest, 'distribute_rewards', lambda *a, **k: None), \
                mock.patch.object(time, 'sleep', lambda s: None), \
                mock.patch.object(db, 'record_harvest', lambda *a: None), \
                mock.patch.object(db, 'snapshot', lambda *a, **k: None), \
                mock.patch.object(db, 'close_position', lambda *a: None), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.signers, 'record_health', lambda *a, **k: None):
            lp.moves.rebalance(state, {'positionMint': 'M', 'price': 100, 'whirlpool': 'P'}, 'x')
        return calls, sent, state
