"""Unichain support below the loop: the chain row, the wallet readers, the
Uniswap v3 pool lookup, the config endpoint, add_profile and sql/031.

Covered, failure paths first: dexes refuses a pool of another factory and a
pool the factory does not map back, survives GeckoTerminal being down, and
names Unichain when every endpoint fails; the record is the usual shape with
the stable as token A (USDC/HYPE) and the price math is right; the wallet
readers pin every EVM read to one block and book a write at its receipt's
block; config takes the endpoint from the chain row; add_profile refuses a
template of another chain unless asked, then pays in this pool's stablecoin;
sql/031 lets a 'unichain' wallet in and keeps a bad address out. No live
network: the chain reads are stubbed."""
import argparse
import importlib.util
import math
import os
import pathlib
import unittest
from unittest import mock

import psycopg2

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import chains
import db
import dexes
from venues import api as venue_api
from venues import evm
from venues.uniswap_v3 import pools as uniswap_pools
import wallets

POOL = '0x5d3e7f5da38fbf476e8b36e3b90d02fc4c1a08c3'
USDC = '0x078D782b760474a361dDA0AF3839290b0EF57AD6'
HYPE = '0x15d0e0c55a3e7ee67152ad7e89acf164253ff68d'
FACTORY = '0x1f98400000000000000000000000000000000003'
OTHER_FACTORY = '0x33128a8fc17869897dce68ed026d694621f6fdfd'     # Uniswap v3 on Base: not Unichain's
WETH = '0x4200000000000000000000000000000000000006'
# slot0 of the pool on 2026-10-04: HYPE per USDC ~0.011035
SQRT_P = int(math.sqrt(0.011035 * 10 ** 12) * 2 ** 96)


def word(n, bits=256):
    return f'{n % (1 << bits):064x}'


def abi_string(s):
    b = s.encode()
    return '0x' + word(32) + word(len(b)) + b.hex().ljust(64 * ((len(b) + 31) // 32 or 1), '0')


def fake_calls(factory=FACTORY, maps_to=None, fee=3000, asked=None, liquidity=8219674644672126879, tick=231254):
    """A stand-in for evm_calls answering the pool's, tokens' and factory's selectors."""
    answers = {
        evm._SEL['factory']: '0x' + word(int(factory, 16)),
        evm._SEL['token0']: '0x' + word(int(USDC, 16)),
        evm._SEL['token1']: '0x' + word(int(HYPE, 16)),
        evm._SEL['fee']: '0x' + word(fee),
        evm._SEL['tickSpacing']: '0x' + word(60),
        evm._SEL['liquidity']: '0x' + word(liquidity),
        evm._SEL['slot0']: '0x' + word(SQRT_P) + word(tick) + word(1) * 5,
    }
    tokens = {(USDC.lower(), evm._SEL['decimals']): '0x' + word(6),
              (HYPE.lower(), evm._SEL['decimals']): '0x' + word(18),
              (USDC.lower(), evm._SEL['symbol']): abi_string('USDC'),
              (HYPE.lower(), evm._SEL['symbol']): abi_string('HYPE')}
    get_pool = evm._SEL['getPool_v3'] + word(int(USDC, 16)) + word(int(HYPE, 16)) + word(fee)

    def calls(pairs, urls=None, timeout=20, chain='Base'):
        if asked is not None:
            asked.append((tuple(urls or ()), chain))
        out = []
        for to, data in pairs:
            if to.lower() == POOL:
                out.append(answers[data])
            elif to.lower() == factory.lower() and data == get_pool:
                out.append('0x' + word(int(maps_to or POOL, 16)))
            else:
                out.append(tokens[(to.lower(), data)])
        return out
    return calls


class PoolFailures(unittest.TestCase):
    def test_a_pool_of_another_factory_is_refused(self):
        with mock.patch.object(evm, 'evm_calls', fake_calls(factory=OTHER_FACTORY)):
            with self.assertRaisesRegex(ValueError, 'not a pool of the Uniswap v3 factory on Unichain'):
                uniswap_pools.uniswap_v3_state(POOL)
            self.assertIsNone(dexes.pool('uniswap-v3-unichain', POOL))

    def test_a_pool_the_factory_does_not_map_back_is_refused(self):
        with mock.patch.object(evm, 'evm_calls', fake_calls(maps_to='0x' + '12' * 20)):
            with self.assertRaisesRegex(ValueError, 'does not map'):
                uniswap_pools.uniswap_v3_state(POOL)

    def test_the_factory_is_asked_with_the_pools_own_fee(self):
        # a pool that reports fee 500 is looked up under 500; the factory maps 3000 only
        with mock.patch.object(evm, 'evm_calls', fake_calls(fee=500)):
            st = uniswap_pools.uniswap_v3_state(POOL)
        self.assertEqual(st['fee_pips'], 500)

    def test_gecko_down_keeps_the_record_with_zero_volume(self):
        with mock.patch.object(evm, 'evm_calls', fake_calls()), \
                mock.patch.object(venue_api, '_get', side_effect=RuntimeError('gecko down')):
            rec = dexes.pool('uniswap-v3-unichain', POOL)
        self.assertEqual((rec['tvl_usd'], rec['volume_24h_usd'], rec['fees_24h_usd']), (0.0, 0.0, 0.0))

    def test_every_endpoint_failing_names_unichain(self):
        with mock.patch.object(evm.urllib.request, 'urlopen', side_effect=OSError('refused')):
            with self.assertRaisesRegex(RuntimeError, 'all Unichain RPC endpoints failed'):
                uniswap_pools.uniswap_v3_state(POOL, urls=('http://127.0.0.1:9',))

    def test_explicit_endpoints_are_the_only_ones_asked(self):
        asked = []
        with mock.patch.object(evm, 'evm_calls', fake_calls(asked=asked)):
            uniswap_pools.uniswap_v3_state(POOL, urls=('http://127.0.0.1:9',))
        self.assertEqual({urls for urls, _ in asked}, {('http://127.0.0.1:9',)})

    def test_reads_go_to_unichain_endpoints_and_the_override_comes_first(self):
        asked = []
        with mock.patch.object(evm, 'evm_calls', fake_calls(asked=asked)), \
                mock.patch.dict(os.environ, {'LPBOT_UNICHAIN_RPC': 'https://own.example'}):
            uniswap_pools.uniswap_v3_state(POOL)
        self.assertTrue(asked)
        for urls, chain in asked:
            self.assertEqual(chain, 'Unichain')
            self.assertEqual(urls[0], 'https://own.example')
            self.assertIn('https://mainnet.unichain.org', urls)
            self.assertNotIn('https://mainnet.base.org', urls)


class PoolRecord(unittest.TestCase):
    def test_the_record_shape_and_price(self):
        gecko = {'attributes': {'reserve_in_usd': '7500000', 'volume_usd': {'h24': '900000'}}}
        with mock.patch.object(evm, 'evm_calls', fake_calls()), \
                mock.patch.object(venue_api, '_get', return_value={'data': gecko}):
            rec = dexes.pool('uniswap-v3-unichain', POOL)
        self.assertEqual((rec['dex'], rec['kind'], rec['chain'], rec['pair']),
                         ('uniswap-v3-unichain', 'clmm', 'unichain', 'USDC/HYPE'))
        self.assertEqual(rec['address'], evm.checksum_address(POOL))
        self.assertEqual(rec['token_a'], {'address': evm.checksum_address(USDC), 'symbol': 'USDC',
                                          'name': 'USDC', 'decimals': 6})
        self.assertEqual(rec['token_b']['decimals'], 18)
        # (sqrtP / 2^96)^2 * 10^(6 - 18): HYPE per USDC
        self.assertAlmostEqual(rec['price'], (SQRT_P / 2 ** 96) ** 2 * 1e-12, places=15)
        self.assertAlmostEqual(1 / rec['price'], 90.62, delta=0.5)
        self.assertEqual((rec['fee'], rec['fee_nominal'], rec['adaptive_fee']), (0.003, 0.003, False))
        self.assertAlmostEqual(rec['fees_24h_usd'], 900000 * 0.003)
        self.assertAlmostEqual(rec['liquidity'], 8219674644672126879 / 1e12)
        self.assertEqual((rec['tick_spacing'], rec['tick'], rec['reward_mints'], rec['reward_usd_day']),
                         (60, 231254, [], 0.0))

    def test_a_negative_tick_and_an_empty_pool(self):
        with mock.patch.object(evm, 'evm_calls', fake_calls(liquidity=0, tick=-887220)), \
                mock.patch.object(venue_api, '_get', side_effect=RuntimeError('gecko down')):
            rec = dexes.pool('uniswap-v3-unichain', POOL)
        self.assertEqual(rec['tick'], -887220)                             # int24, sign-extended by the ABI
        self.assertIsNone(rec['liquidity'])                                # no active liquidity: unknown, not 0

    def test_dexes_names_the_venue_once(self):
        self.assertIs(dexes.SINGLE['uniswap-v3-unichain'], uniswap_pools.uniswap_v3_pool)
        self.assertNotIn('uniswap-v3-unichain', dexes.ADAPTERS)          # never on the Solana board


class ChainRow(unittest.TestCase):
    def test_the_unichain_row(self):
        c = chains.caps('unichain')
        self.assertEqual((c['native_symbol'], c['native_decimals'], c['gecko_network'], c['native_mint']),
                         ('ETH', 18, 'unichain', WETH))
        self.assertEqual((c['swap_via'], c['payout_via'], c['pin_env']), ('venue', 'venue', 'LPBOT_EVM_PROFIT_WALLET_PIN'))
        self.assertFalse(any(c[k] for k in ('sweep', 'janitor', 'audit', 'scanner', 'rewards', 'txfees', 'venues')))
        self.assertEqual((c['probe'], c['min_poll_seconds']), ('eth_blockNumber', 60))
        self.assertEqual((c['public_rpc'], c['rpc_env']), ('https://mainnet.unichain.org', 'LPBOT_UNICHAIN_RPC'))
        self.assertEqual((chains.caps('base')['public_rpc'], chains.caps('base')['rpc_env']),
                         ('https://mainnet.base.org', 'LPBOT_BASE_RPC'))

    def test_addresses(self):
        self.assertTrue(chains.is_address('unichain', POOL))
        self.assertTrue(chains.is_address('unichain', evm.checksum_address(POOL)))
        for bad in (POOL + '\n', POOL[:-1], '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f', None, ''):
            self.assertFalse(chains.is_address('unichain', bad), bad)


class Tape(unittest.TestCase):
    """The surrogate tape for USDC/HYPE: Binance lists HYPEUSDC (~$90), the pool
    prices HYPE per USDC (~0.011). The bars are turned the pool's way up."""

    def test_the_symbols_and_the_inversion(self):
        import calm
        import numpy as np
        self.assertEqual(calm.pair_tokens('USDC/HYPE'), ('USDC', 'HYPE'))
        self.assertEqual(calm.binance_symbols('USDC', 'HYPE'), ['USDCHYPE', 'HYPEUSDC'])
        ts = np.array([0.0, 300.0]); o = np.array([90.0, 91.0]); h = np.array([92.0, 93.0])
        l = np.array([89.0, 90.0]); c = np.array([91.0, 90.909]); v = np.zeros(2)
        fit = calm.fit_surrogate((ts, o, h, l, c, v), 0.011)
        self.assertIsNotNone(fit)
        _, fo, fh, fl, fc, _ = fit
        self.assertAlmostEqual(fc[-1], 1 / 90.909)
        self.assertAlmostEqual(fh[0], 1 / 89.0)                    # the high is the inverted low
        self.assertAlmostEqual(fl[0], 1 / 92.0)
        self.assertTrue(np.all(fl <= np.minimum(fo, fc)) and np.all(np.maximum(fo, fc) <= fh))
        self.assertIsNone(calm.fit_surrogate((ts, o, h, l, c, v), 0.02))     # neither way up matches


class Readers(unittest.TestCase):
    def test_the_unichain_reader_pins_every_read_to_one_block(self):
        owner = '0x' + 'cd' * 20

        def rpc(url, method, params, timeout):
            if method == 'eth_blockNumber':
                return hex(700)
            assert params[1] == hex(700)
            if method == 'eth_getBalance':
                return hex(10 ** 16)
            return hex(6) if params[0]['data'] == '0x313ce567' else hex(12_345_678)
        with mock.patch.object(wallets, '_rpc', rpc):
            got = wallets.read_balances('unichain', 'u', owner, [USDC.lower()], WETH)
        self.assertEqual(got, ({USDC.lower(): 12.345678}, 700))           # USDC is not native: no ETH added

    def test_a_failed_read_is_none_and_a_write_books_at_its_receipt_block(self):
        with mock.patch.object(wallets, '_rpc', side_effect=OSError('down')), \
                mock.patch.object(wallets.time, 'sleep', lambda s: None):
            self.assertIsNone(wallets.read_balances('unichain', 'u', '0x' + 'cd' * 20, [USDC], WETH))

        def rpc(url, method, params, timeout):
            return {'blockNumber': hex(800 if params[0] == '0xa' else 801)} if params[0] != '0xc' else None
        with mock.patch.object(wallets, '_rpc', rpc):
            self.assertEqual(wallets.write_slot('unichain', 'u', ['0xa', '0xb']), 801)
            self.assertIsNone(wallets.write_slot('unichain', 'u', ['0xa', '0xc']))


class ConfigEndpoint(unittest.TestCase):
    """config.public_rpc: the endpoint from the chain row."""

    def test_the_public_endpoint_and_its_override(self):
        import config
        caps = chains.caps('unichain')
        env = {'LPBOT_RPC': 'https://solana.example', 'SOLANA_RPC_URL': 'https://solana.example',
               'LPBOT_BASE_RPC': 'https://base.example'}
        self.assertEqual(config.public_rpc('unichain', caps, env), 'https://mainnet.unichain.org')
        self.assertEqual(config.public_rpc('unichain', caps, dict(env, LPBOT_UNICHAIN_RPC='https://own.example')),
                         'https://own.example')
        self.assertEqual(config.public_rpc('base', chains.caps('base'), env), 'https://base.example')
        self.assertEqual(config.public_rpc('base', chains.caps('base'), {}), 'https://mainnet.base.org')
        self.assertEqual(config.public_rpc('solana', chains.caps('solana'), {}), 'https://api.mainnet-beta.solana.com')
        self.assertEqual(config.public_rpc('solana', chains.caps('solana'), env), 'https://solana.example')


spec = importlib.util.spec_from_file_location('add_profile', pathlib.Path(__file__).parent.parent / 'ops' / 'add_profile.py')
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)
BASE_USDC = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
TEMPLATE = {'id': 5, 'name': 'base-weth-usdc', 'chain': 'base', 'pool': 'X', 'capital_usd': 216, 'max_usd': 600,
            'bands': [1.03], 'regime_threshold': 0.2, 'profit_wallet': '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142',
            'payout_mint': BASE_USDC, 'payout_enabled': True, 'gas_reserve_sol': 0.003, 'allow_swap': False,
            'wallet_id': 'base-lp', 'residual_owner': True, 'enabled': True, 'signer_env': None}
REC = {'token_a': {'address': USDC, 'symbol': 'USDC'}, 'token_b': {'address': HYPE, 'symbol': 'HYPE'}}


def args(**kw):
    a = dict(profile='uni-hype-usdc', template='base-weth-usdc', wallet='uni-lp', chain='unichain',
             address='0x' + 'cd' * 20, secret_env='LPBOT_UNICHAIN_KEY_PATH', label='Unichain LP wallet',
             dex='uniswap-v3-unichain', pool=POOL, execute_dexes=None, signer_env=None, deposit_mint=None,
             swing_open=None, swing_closed=None, calendar='nyse', lead_s=300, apply=False,
             cross_chain=True, payout_mint=None, max_usd=260.0)
    a.update(kw)
    return argparse.Namespace(**a)


class AddProfile(unittest.TestCase):
    def test_an_old_namespace_without_the_flag_is_refused_across_chains(self):
        a = args()
        del a.cross_chain
        with self.assertRaisesRegex(ap.Refused, 'cross-chain'):
            ap.build(a, TEMPLATE, REC, None, 0)

    def test_a_payout_mint_by_address_in_any_case_and_a_small_max(self):
        _, row, _ = ap.build(args(payout_mint=USDC, max_usd=0.5), TEMPLATE, REC, None, 0)
        self.assertEqual((row['payout_mint'], row['max_usd']), (USDC.lower(), 0.5))

    def test_a_template_of_another_chain_needs_the_flag(self):
        with self.assertRaisesRegex(ap.Refused, 'cross-chain'):
            ap.build(args(cross_chain=False), TEMPLATE, REC, None, 0)

    def test_a_cross_chain_template_needs_a_profit_wallet_valid_here(self):
        sol = dict(TEMPLATE, chain='solana', profit_wallet='8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h')
        with self.assertRaisesRegex(ap.Refused, 'not a unichain address'):
            ap.build(args(), sol, REC, None, 0)

    def test_the_unichain_profile(self):
        w, row, swing = ap.build(args(), TEMPLATE, REC, None, 0)
        self.assertEqual(w, ('uni-lp', 'unichain', '0x' + 'cd' * 20, 'LPBOT_UNICHAIN_KEY_PATH', 'Unichain LP wallet'))
        self.assertIsNone(swing)
        self.assertEqual(row['mints'], [USDC.lower(), HYPE])                # lower case, A then B
        self.assertEqual(row['deposit_mint'], HYPE)                         # the side that is not stable
        self.assertEqual(row['payout_mint'], USDC.lower())                  # this chain's USDC, not Base's
        self.assertEqual(row['max_usd'], 260.0)
        self.assertEqual((row['dex'], row['execute_dexes'], row['pair_label']),
                         ('uniswap-v3-unichain', ['uniswap-v3-unichain'], 'USDC/HYPE'))
        self.assertTrue(row['pool_pinned'] and row['regime_enabled'] and row['rebalance_swap'])
        self.assertEqual(row['profit_wallet'], TEMPLATE['profit_wallet'])
        self.assertEqual(row['gas_reserve_sol'], 0.003)

    def test_payout_mint_and_max_usd_overrides(self):
        _, row, _ = ap.build(args(payout_mint='hype', max_usd=None), TEMPLATE, REC, None, 0)
        self.assertEqual((row['payout_mint'], row['max_usd']), (HYPE, 600))
        with self.assertRaisesRegex(ap.Refused, 'not one of the pool'):
            ap.build(args(payout_mint=BASE_USDC), TEMPLATE, REC, None, 0)
        with self.assertRaisesRegex(ap.Refused, 'positive'):
            ap.build(args(max_usd=0.0), TEMPLATE, REC, None, 0)

    def test_the_cli_takes_unichain(self):
        a = ap.parse(['--profile', 'p', '--template', 't', '--wallet', 'w', '--chain', 'unichain', '--cross-chain',
                      '--dex', 'uniswap-v3-unichain', '--pool', POOL, '--max-usd', '260', '--payout-mint', 'USDC'])
        self.assertEqual((a.chain, a.cross_chain, a.max_usd, a.payout_mint), ('unichain', True, 260.0, 'USDC'))
        self.assertIn('uniswap-v3-unichain', ap.VENUES)


class Migration031(unittest.TestCase):
    """sql/031 on the test database (tests/run.sh applies every migration)."""

    def insert(self, chain, address):
        with db.cursor(commit=True) as cur:
            cur.execute('insert into wallets (id, chain, address, secret_env) values (%s, %s, %s, %s)',
                        ('t031', chain, address, 'LPBOT_UNICHAIN_KEY_PATH'))

    def tearDown(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from wallets where id = 't031'")

    def test_a_unichain_wallet_is_allowed(self):
        self.insert('unichain', '0x' + 'Ab' * 20)

    def test_a_bad_address_or_chain_is_refused(self):
        for chain, addr in (('unichain', '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'), ('unichain', '0x' + 'ab' * 19),
                            ('optimism', '0x' + 'ab' * 20)):
            with self.subTest(chain=chain, addr=addr), self.assertRaises(psycopg2.errors.CheckViolation):
                self.insert(chain, addr)

    def test_the_migration_runs_twice(self):
        sql = (pathlib.Path(__file__).parent.parent / 'sql' / '031_unichain.sql').read_text()
        with db.cursor(commit=True) as cur:
            cur.execute(sql.replace('begin;', '').replace('commit;', ''))
            cur.execute(sql.replace('begin;', '').replace('commit;', ''))
        self.insert('unichain', '0x' + 'ab' * 20)


if __name__ == '__main__':
    unittest.main()
