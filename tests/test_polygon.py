"""Polygon support below the loop: the chain row, the wallet readers, the
Uniswap v3 pool lookup, the surrogate tape symbols, the stable mints,
add_profile, the loop's signer table and sql/034.

Covered, failure paths first: dexes refuses a pool of another factory (Unichain's
v3 factory is not Polygon's) and names Polygon when every endpoint fails; the
record is the usual shape with WPOL as token A and USDT0 as a stable token B;
the wallet reader never adds gas POL to WPOL (the native mint is Polygon's
native-token address, not WPOL); the surrogate tape reads WPOL/USDT0 as
POLUSDT; add_profile pays in USDT0 and keeps the profit wallet; sql/034 lets a
'polygon' wallet in and keeps a bad address out. No live network: the chain
reads are stubbed."""
import argparse
import importlib.util
import math
import os
import pathlib
import unittest
from unittest import mock

import psycopg2

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import chains
import db
import dexes
import engine
import rebalancer
import wallets

DEX = 'uniswap-v3-polygon'
POOL = '0x9b08288c3be4f62bbf8d1c20ac9c5e6f9467d8b7'
WPOL = '0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270'
USDT0 = '0xc2132D05D31c914a87C6611C10748AEb04B58e8F'
USDC = '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359'
FACTORY = '0x1f98431c8ad98523631ae4a59f267346ea31f984'
UNICHAIN_FACTORY = '0x1f98400000000000000000000000000000000003'
NATIVE = '0x0000000000000000000000000000000000001010'
# about $0.10 a WPOL: USDT0 (6 decimals) per WPOL (18 decimals)
SQRT_P = int(math.sqrt(0.1 * 10 ** (6 - 18)) * 2 ** 96)


def word(n, bits=256):
    return f'{n % (1 << bits):064x}'


def abi_string(s):
    b = s.encode()
    return '0x' + word(32) + word(len(b)) + b.hex().ljust(64 * ((len(b) + 31) // 32 or 1), '0')


def fake_calls(factory=FACTORY, maps_to=None, fee=500, asked=None, liquidity=10 ** 15, tick=-299_000):
    """A stand-in for evm_calls answering the pool's, tokens' and factory's selectors."""
    answers = {
        dexes._SEL['factory']: '0x' + word(int(factory, 16)),
        dexes._SEL['token0']: '0x' + word(int(WPOL, 16)),
        dexes._SEL['token1']: '0x' + word(int(USDT0, 16)),
        dexes._SEL['fee']: '0x' + word(fee),
        dexes._SEL['tickSpacing']: '0x' + word(10),
        dexes._SEL['liquidity']: '0x' + word(liquidity),
        dexes._SEL['slot0']: '0x' + word(SQRT_P) + word(tick) + word(1) * 5,
    }
    tokens = {(WPOL.lower(), dexes._SEL['decimals']): '0x' + word(18),
              (USDT0.lower(), dexes._SEL['decimals']): '0x' + word(6),
              (WPOL.lower(), dexes._SEL['symbol']): abi_string('WPOL'),
              (USDT0.lower(), dexes._SEL['symbol']): abi_string('USDT0')}
    get_pool = dexes._SEL['getPool_v3'] + word(int(WPOL, 16)) + word(int(USDT0, 16)) + word(fee)

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
    def test_a_pool_of_the_unichain_factory_is_refused_on_polygon(self):
        with mock.patch.object(dexes, 'evm_calls', fake_calls(factory=UNICHAIN_FACTORY)):
            with self.assertRaisesRegex(ValueError, 'not a pool of the Uniswap v3 factory on Polygon'):
                dexes.uniswap_v3_state(POOL, dex=DEX)
            self.assertIsNone(dexes.pool(DEX, POOL))

    def test_a_polygon_pool_is_refused_as_a_unichain_pool(self):
        with mock.patch.object(dexes, 'evm_calls', fake_calls()):
            with self.assertRaisesRegex(ValueError, 'on Unichain'):
                dexes.uniswap_v3_state(POOL)

    def test_a_pool_the_factory_does_not_map_back_is_refused(self):
        with mock.patch.object(dexes, 'evm_calls', fake_calls(maps_to='0x' + '12' * 20)):
            with self.assertRaisesRegex(ValueError, 'does not map'):
                dexes.uniswap_v3_state(POOL, dex=DEX)

    def test_every_endpoint_failing_names_polygon(self):
        with mock.patch.object(dexes.urllib.request, 'urlopen', side_effect=OSError('refused')):
            with self.assertRaisesRegex(RuntimeError, 'all Polygon RPC endpoints failed'):
                dexes.uniswap_v3_state(POOL, urls=('http://127.0.0.1:9',), dex=DEX)

    def test_reads_go_to_polygon_endpoints_and_the_override_comes_first(self):
        asked = []
        with mock.patch.object(dexes, 'evm_calls', fake_calls(asked=asked)), \
                mock.patch.dict(os.environ, {'LPBOT_POLYGON_RPC': 'https://own.example',
                                             'LPBOT_UNICHAIN_RPC': 'https://uni.example'}):
            dexes.uniswap_v3_state(POOL, dex=DEX)
        self.assertTrue(asked)
        for urls, chain in asked:
            self.assertEqual(chain, 'Polygon')
            self.assertEqual(urls[0], 'https://own.example')
            self.assertIn('https://polygon-bor-rpc.publicnode.com', urls)
            self.assertNotIn('https://uni.example', urls)
            self.assertNotIn('https://polygon-rpc.com', urls)              # 403 since 2026-10
            self.assertFalse(any('unichain' in u for u in urls))

    def test_gecko_down_keeps_the_record_and_gecko_is_asked_on_polygon_pos(self):
        seen = []

        def get(url, accept=None):
            seen.append(url)
            raise RuntimeError('gecko down')
        with mock.patch.object(dexes, 'evm_calls', fake_calls()), mock.patch.object(dexes, '_get', get):
            rec = dexes.pool(DEX, POOL)
        self.assertEqual((rec['tvl_usd'], rec['volume_24h_usd']), (0.0, 0.0))
        self.assertEqual(seen, [f'https://api.geckoterminal.com/api/v2/networks/polygon_pos/pools/{POOL}'])


class PoolRecord(unittest.TestCase):
    def test_the_record_shape_and_price(self):
        gecko = {'attributes': {'reserve_in_usd': '1290000', 'volume_usd': {'h24': '3000000'}}}
        with mock.patch.object(dexes, 'evm_calls', fake_calls()), \
                mock.patch.object(dexes, '_get', return_value={'data': gecko}):
            rec = dexes.pool(DEX, POOL)
        self.assertEqual((rec['dex'], rec['kind'], rec['chain'], rec['pair']), (DEX, 'clmm', 'polygon', 'WPOL/USDT0'))
        self.assertEqual(rec['address'], dexes.checksum_address(POOL))
        self.assertEqual((rec['token_a']['symbol'], rec['token_a']['decimals']), ('WPOL', 18))
        self.assertEqual((rec['token_b']['symbol'], rec['token_b']['decimals']), ('USDT0', 6))
        self.assertAlmostEqual(rec['price'], 0.1, places=9)                 # USDT0 per WPOL
        self.assertEqual((rec['fee'], rec['tick_spacing']), (0.0005, 10))
        self.assertAlmostEqual(rec['fees_24h_usd'], 3_000_000 * 0.0005)
        self.assertTrue(engine.is_stable(rec['token_b']))                   # USDT0 by mint
        self.assertFalse(engine.is_stable(rec['token_a']))
        self.assertEqual(engine.stable_quote(rec), 1.0)

    def test_dexes_names_both_venues_once(self):
        self.assertIs(dexes.SINGLE['uniswap-v3-unichain'], dexes.uniswap_v3_pool)
        self.assertIs(dexes.SINGLE[DEX], dexes.uniswap_v3_polygon_pool)
        self.assertNotIn(DEX, dexes.ADAPTERS)                              # never on the Solana board
        self.assertEqual(dexes.UNISWAP_V3[DEX]['chain'], 'polygon')


class ChainRow(unittest.TestCase):
    def test_the_polygon_row(self):
        c = chains.caps('polygon')
        self.assertEqual((c['native_symbol'], c['native_decimals'], c['gecko_network'], c['native_mint']),
                         ('POL', 18, 'polygon_pos', NATIVE))
        self.assertNotEqual(c['native_mint'].lower(), WPOL.lower())          # WPOL is a pool token
        self.assertEqual((c['swap_via'], c['payout_via'], c['pin_env']), ('venue', 'venue', 'LPBOT_EVM_PROFIT_WALLET_PIN'))
        self.assertFalse(any(c[k] for k in ('sweep', 'janitor', 'audit', 'scanner', 'rewards', 'txfees', 'venues')))
        self.assertTrue(c['payout'])
        self.assertEqual((c['probe'], c['min_poll_seconds']), ('eth_blockNumber', 60))
        self.assertEqual((c['public_rpc'], c['rpc_env']), ('https://polygon-bor-rpc.publicnode.com', 'LPBOT_POLYGON_RPC'))

    def test_addresses(self):
        self.assertTrue(chains.is_address('polygon', POOL))
        self.assertTrue(chains.is_address('polygon', WPOL))
        for bad in (POOL + '\n', POOL[:-1], '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f', None, ''):
            self.assertFalse(chains.is_address('polygon', bad), bad)

    def test_the_public_endpoint_and_its_override(self):
        import config
        caps = chains.caps('polygon')
        env = {'LPBOT_RPC': 'https://solana.example', 'LPBOT_UNICHAIN_RPC': 'https://uni.example'}
        self.assertEqual(config.public_rpc('polygon', caps, env), 'https://polygon-bor-rpc.publicnode.com')
        self.assertEqual(config.public_rpc('polygon', caps, dict(env, LPBOT_POLYGON_RPC='https://own.example')),
                         'https://own.example')


class Stables(unittest.TestCase):
    def test_polygon_usdt0_and_usdc_are_stable_and_wpol_is_not(self):
        for m in (USDT0, USDC):
            self.assertIn(m.lower(), engine.STABLE_MINTS)
            self.assertTrue(engine.is_stable({'address': m}))
        self.assertFalse(engine.is_stable({'address': WPOL}))


class Tape(unittest.TestCase):
    """The surrogate tape for WPOL/USDT0 is Binance POLUSDT, the pool's way up."""

    def test_the_symbols(self):
        self.assertEqual(calm.pair_tokens('WPOL/USDT0'), ('POL', 'USDT'))
        self.assertEqual(calm.pair_tokens('WPOL/USDT'), ('POL', 'USDT'))
        self.assertEqual(calm.binance_symbols(*calm.pair_tokens('WPOL/USDT0')), ['POLUSDT', 'USDTPOL'])
        self.assertEqual(calm.pair_tokens('SOL/USDC'), ('SOL', 'USDC'))       # untouched


class Readers(unittest.TestCase):
    OWNER = '0x' + 'cd' * 20

    def rpc(self, native_wei=5 * 10 ** 18, wpol_raw=7 * 10 ** 18):
        def call(url, method, params, timeout):
            if method == 'eth_blockNumber':
                return hex(900)
            assert params[1] == hex(900)
            if method == 'eth_getBalance':
                return hex(native_wei)
            to, data = params[0]['to'].lower(), params[0]['data']
            if data == '0x313ce567':
                return hex(18 if to == WPOL.lower() else 6)
            return hex(wpol_raw if to == WPOL.lower() else 12_345_678)
        return call

    def test_gas_pol_is_never_added_to_wpol(self):
        with mock.patch.object(wallets, '_rpc', self.rpc()):
            got = wallets.read_balances('polygon', 'u', self.OWNER, [WPOL.lower(), USDT0.lower()],
                                        chains.caps('polygon')['native_mint'])
        self.assertEqual(got, ({WPOL.lower(): 7.0, USDT0.lower(): 12.345678}, 900))

    def test_what_wpol_as_the_native_mint_would_have_done(self):
        # the trap the native mint avoids: 5 POL of gas would read as 12 WPOL
        with mock.patch.object(wallets, '_rpc', self.rpc()):
            got = wallets.read_balances('polygon', 'u', self.OWNER, [WPOL.lower()], WPOL.lower())
        self.assertEqual(got[0][WPOL.lower()], 12.0)

    def test_a_write_books_at_its_receipt_block(self):
        def rpc(url, method, params, timeout):
            return {'blockNumber': hex(800 if params[0] == '0xa' else 801)} if params[0] != '0xc' else None
        with mock.patch.object(wallets, '_rpc', rpc):
            self.assertEqual(wallets.write_slot('polygon', 'u', ['0xa', '0xb']), 801)
            self.assertIsNone(wallets.write_slot('polygon', 'u', ['0xa', '0xc']))


class Loop(unittest.TestCase):
    def test_the_polygon_dex_runs_the_uniswap_signer(self):
        self.assertEqual(rebalancer.SIGNERS[DEX], rebalancer.SIGNERS['uniswap-v3-unichain'])
        self.assertTrue(rebalancer.SIGNERS[DEX].endswith('signer_uniswap.mjs'))
        self.assertEqual(rebalancer.OPEN_RENT_HEADROOM[DEX], 0.0)

    def test_the_signer_is_told_its_chain(self):
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, '{"ok": true}', ''

        def run(cmd, **kw):
            seen.update(kw['env'])
            return Done()
        with mock.patch.object(rebalancer.config, 'CHAIN', 'polygon'), \
                mock.patch.object(rebalancer.config, 'DEX', DEX), \
                mock.patch.object(rebalancer.subprocess, 'run', run):
            rebalancer._chain('pool', dex=DEX)
        self.assertEqual(seen.get('LPBOT_CHAIN'), 'polygon')


spec = importlib.util.spec_from_file_location('add_profile', pathlib.Path(__file__).parent.parent / 'ops' / 'add_profile.py')
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)
UNI_USDC = '0x078d782b760474a361dda0af3839290b0ef57ad6'
TEMPLATE = {'id': 7, 'name': 'uni-hype-usdc', 'chain': 'unichain', 'pool': 'X', 'capital_usd': 216, 'max_usd': 260,
            'bands': [1.03], 'regime_threshold': 0.2, 'profit_wallet': '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142',
            'payout_mint': UNI_USDC, 'payout_enabled': True, 'gas_reserve_sol': 0.003, 'allow_swap': True,
            'wallet_id': 'uni-lp', 'residual_owner': True, 'enabled': True, 'signer_env': None}
REC = {'token_a': {'address': WPOL, 'symbol': 'WPOL'}, 'token_b': {'address': USDT0, 'symbol': 'USDT0'}}


def args(**kw):
    a = dict(profile='poly-wpol-usdt', template='uni-hype-usdc', wallet='poly-lp', chain='polygon',
             address='0x' + 'ef' * 20, secret_env='LPBOT_POLYGON_KEY_PATH', label='Polygon LP wallet',
             dex=DEX, pool=POOL, execute_dexes=None, signer_env=None, deposit_mint=None,
             swing_open=None, swing_closed=None, calendar='nyse', lead_s=300, apply=False,
             cross_chain=True, payout_mint=None, max_usd=260.0)
    a.update(kw)
    return argparse.Namespace(**a)


class AddProfile(unittest.TestCase):
    def test_the_polygon_profile(self):
        w, row, swing = ap.build(args(), TEMPLATE, REC, None, 0)
        self.assertEqual(w, ('poly-lp', 'polygon', '0x' + 'ef' * 20, 'LPBOT_POLYGON_KEY_PATH', 'Polygon LP wallet'))
        self.assertIsNone(swing)
        self.assertEqual(row['mints'], [WPOL.lower(), USDT0.lower()])
        self.assertEqual(row['deposit_mint'], WPOL.lower())                 # the side that is not stable
        self.assertEqual(row['payout_mint'], USDT0.lower())                 # this pool's stable, not Unichain's USDC
        self.assertEqual((row['dex'], row['execute_dexes']), (DEX, [DEX]))
        self.assertEqual(row['profit_wallet'], TEMPLATE['profit_wallet'])

    def test_a_template_of_another_chain_needs_the_flag_and_a_bad_address_is_refused(self):
        with self.assertRaisesRegex(ap.Refused, 'cross-chain'):
            ap.build(args(cross_chain=False), TEMPLATE, REC, None, 0)
        with self.assertRaises(ap.Refused):
            ap.build(args(address='83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'), TEMPLATE, REC, None, 0)

    def test_the_cli_takes_polygon(self):
        a = ap.parse(['--profile', 'p', '--template', 't', '--wallet', 'w', '--chain', 'polygon', '--cross-chain',
                      '--dex', DEX, '--pool', POOL, '--max-usd', '260'])
        self.assertEqual((a.chain, a.dex), ('polygon', DEX))
        self.assertIn(DEX, ap.VENUES)


class Migration034(unittest.TestCase):
    """sql/034 on the test database (applied here: test_unichain runs 031 again, which narrows the check)."""

    SQL = (pathlib.Path(__file__).parent.parent / 'sql' / '034_polygon.sql').read_text()

    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute(self.SQL.replace('begin;', '').replace('commit;', ''))

    def insert(self, chain, address):
        with db.cursor(commit=True) as cur:
            cur.execute('insert into wallets (id, chain, address, secret_env) values (%s, %s, %s, %s)',
                        ('t034', chain, address, 'LPBOT_POLYGON_KEY_PATH'))

    def tearDown(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from wallets where id = 't034'")

    def test_a_polygon_wallet_is_allowed(self):
        self.insert('polygon', '0x' + 'Ab' * 20)

    def test_every_earlier_chain_is_still_allowed(self):
        for chain, addr in (('unichain', '0x' + 'ab' * 20), ('base', '0x' + 'ac' * 20)):
            self.insert(chain, addr)
            self.tearDown()

    def test_a_bad_address_or_chain_is_refused(self):
        for chain, addr in (('polygon', '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'), ('polygon', '0x' + 'ab' * 19),
                            ('optimism', '0x' + 'ab' * 20)):
            with self.subTest(chain=chain, addr=addr), self.assertRaises(psycopg2.errors.CheckViolation):
                self.insert(chain, addr)

    def test_the_migration_runs_twice(self):
        with db.cursor(commit=True) as cur:
            cur.execute(self.SQL.replace('begin;', '').replace('commit;', ''))
        self.insert('polygon', '0x' + 'ab' * 20)


if __name__ == '__main__':
    unittest.main()
