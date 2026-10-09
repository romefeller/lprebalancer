"""dexes.pool('aerodrome-slipstream', addr): the Base adapter lands in the usual
record shape for a pool of either known Slipstream deployment, refuses a pool of
an unknown factory, a pool whose position manager is not its factory's, and a
pool its factory does not map back to; survives GeckoTerminal being down, and
moves to the next RPC endpoint when one fails. No live network: the
RPC endpoints are local HTTP servers and the chain reads are stubbed."""
import http.server
import json
import threading
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import dexes

POOL = '0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59'
POOL_V3 = '0x3fe04a59ebd38cf06080a6f60a98d124eb59392a'
WETH = '0x4200000000000000000000000000000000000006'
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
FACTORY = '0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a'
NPM = '0x827922686190790b37229fd06084350e74485b72'
FACTORY_V3 = '0xf8f2eb4940cfe7d13603dddd87f123820fc061ef'
NPM_V3 = '0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53'
GAUGE_CAPS_FACTORY = '0xade65c38cd4849adba595a4323a8c7ddfe89716a'   # a real deployment, not in the registry


def word(n, bits=256):
    return f'{n % (1 << bits):064x}'


def abi_string(s):
    b = s.encode()
    return '0x' + word(32) + word(len(b)) + b.hex().ljust(64 * ((len(b) + 31) // 32 or 1), '0')


def fake_calls(factory=FACTORY, nft=NPM, pool=POOL, spacing=100, tick=-197314, sqrt_p=4116179725402233657475978,
               maps_to=None, asked=None):
    """A stand-in for evm_calls answering the pool's, tokens' and factory's selectors.
    The factory maps (WETH, USDC, `spacing`) to `maps_to` (default: the pool)."""
    answers = {
        dexes._SEL['factory']: '0x' + word(int(factory, 16)),
        dexes._SEL['nft']: '0x' + word(int(nft, 16)),
        dexes._SEL['token0']: '0x' + word(int(WETH, 16)),
        dexes._SEL['token1']: '0x' + word(int(USDC, 16)),
        dexes._SEL['tickSpacing']: '0x' + word(spacing),
        dexes._SEL['fee']: '0x' + word(500),
        dexes._SEL['unstakedFee']: '0x' + word(50000),
        dexes._SEL['liquidity']: '0x' + word(4 * 10 ** 18),
        dexes._SEL['stakedLiquidity']: '0x' + word(3 * 10 ** 18),
        dexes._SEL['slot0']: '0x' + word(sqrt_p) + word(tick) + word(1) * 4,
    }
    tokens = {(WETH.lower(), dexes._SEL['decimals']): '0x' + word(18),
              (USDC.lower(), dexes._SEL['decimals']): '0x' + word(6),
              (WETH.lower(), dexes._SEL['symbol']): abi_string('WETH'),
              (USDC.lower(), dexes._SEL['symbol']): abi_string('USDC')}
    get_pool = dexes._SEL['getPool'] + word(int(WETH, 16)) + word(int(USDC, 16)) + word(spacing)

    def calls(pairs, urls=None):
        out = []
        for to, data in pairs:
            if asked is not None:
                asked.append((to.lower(), data))
            if to.lower() == pool:
                out.append(answers[data])
            elif to.lower() == factory and data == get_pool:
                out.append('0x' + word(int(maps_to or pool, 16)))
            else:
                out.append(tokens[(to.lower(), data)])
        return out
    return calls


class Failures(unittest.TestCase):
    def test_a_pool_of_an_unknown_factory_is_refused(self):
        with mock.patch.object(dexes, 'evm_calls', fake_calls(factory=GAUGE_CAPS_FACTORY)):
            with self.assertRaisesRegex(ValueError, 'not a pool of a known Slipstream factory'):
                dexes.slipstream_state(POOL)
            self.assertIsNone(dexes.pool('aerodrome-slipstream', POOL))

    def test_a_position_manager_of_another_deployment_is_refused(self):
        for factory, nft in ((FACTORY, NPM_V3), (FACTORY_V3, NPM), (FACTORY, '0x' + '11' * 20)):
            with self.subTest(factory=factory, nft=nft), \
                    mock.patch.object(dexes, 'evm_calls', fake_calls(factory=factory, nft=nft)):
                with self.assertRaisesRegex(ValueError, 'names position manager'):
                    dexes.slipstream_state(POOL)

    def test_a_pool_the_factory_does_not_map_back_is_refused(self):
        # a contract that answers like a pool and names a real factory, which knows another pool
        for maps_to in (POOL_V3, '0x' + '00' * 20):
            with self.subTest(maps_to=maps_to), mock.patch.object(dexes, 'evm_calls', fake_calls(maps_to=maps_to)):
                with self.assertRaisesRegex(ValueError, 'does not map'):
                    dexes.slipstream_state(POOL)
                self.assertIsNone(dexes.pool('aerodrome-slipstream', POOL))

    def test_get_pool_is_asked_of_the_pools_own_factory_with_its_spacing(self):
        asked = []
        with mock.patch.object(dexes, 'evm_calls', fake_calls(factory=FACTORY_V3, nft=NPM_V3, pool=POOL_V3, spacing=50, asked=asked)):
            dexes.slipstream_state(POOL_V3)
        gets = [(to, data) for to, data in asked if data.startswith(dexes._SEL['getPool'])]
        self.assertEqual(gets, [(FACTORY_V3, dexes._SEL['getPool'] + word(int(WETH, 16)) + word(int(USDC, 16)) + word(50))])

    def test_get_pool_encodes_a_negative_spacing_as_int24_twos_complement(self):
        to, data = dexes._get_pool_call(FACTORY, WETH, USDC, -50)
        self.assertEqual((to, data[-64:]), (FACTORY, 'f' * 62 + 'ce'))

    def test_chain_down_means_no_record_not_a_guess(self):
        with mock.patch.object(dexes, 'evm_calls', side_effect=RuntimeError('all Base RPC endpoints failed')):
            self.assertIsNone(dexes.pool('aerodrome-slipstream', POOL))

    def test_gecko_down_keeps_the_chain_record_with_zero_volume(self):
        with mock.patch.object(dexes, 'evm_calls', fake_calls()), \
                mock.patch.object(dexes, '_get', side_effect=ValueError('no json')):
            rec = dexes.pool('aerodrome-slipstream', POOL)
        self.assertEqual((rec['volume_24h_usd'], rec['tvl_usd']), (0.0, 0.0))
        self.assertAlmostEqual(rec['price'], 2699.1, delta=1)

    def test_rpc_failover_and_all_failing(self):
        bad = _server(lambda body: (429, {'error': 'over rate limit'}))
        good = _server(lambda body: (200, [{'jsonrpc': '2.0', 'id': r['id'], 'result': '0x' + word(7)} for r in body]))
        errs = _server(lambda body: (200, [{'jsonrpc': '2.0', 'id': r['id'], 'error': {'code': 3, 'message': 'execution reverted'}}
                                           for r in body]))
        try:
            out = dexes.evm_calls([(WETH, '0x313ce567'), (USDC, '0x313ce567')], urls=[bad.url, good.url])
            self.assertEqual(out, ['0x' + word(7)] * 2)
            with self.assertRaisesRegex(RuntimeError, 'all Base RPC endpoints failed.*execution reverted'):
                dexes.evm_calls([(WETH, '0x313ce567')], urls=[bad.url, errs.url])
        finally:
            for s in (bad, good, errs):
                s.shutdown()
                s.server_close()


class Record(unittest.TestCase):
    def test_usual_shape_and_unstaked_fee(self):
        gecko = {'attributes': {'reserve_in_usd': '8867502.83', 'volume_usd': {'h24': '81341491.01'}}}
        with mock.patch.object(dexes, 'evm_calls', fake_calls()), \
                mock.patch.object(dexes, '_get', return_value={'data': gecko}):
            rec = dexes.pool('aerodrome-slipstream', POOL)
        self.assertEqual(rec['dex'], 'aerodrome-slipstream')
        self.assertEqual(rec['kind'], 'clmm')
        self.assertEqual(rec['address'], '0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59')
        self.assertEqual(rec['token_a'], {'address': WETH, 'symbol': 'WETH', 'name': 'WETH', 'decimals': 18})
        self.assertEqual(rec['token_b'], {'address': USDC, 'symbol': 'USDC', 'name': 'USDC', 'decimals': 6})
        self.assertEqual(rec['pair'], 'WETH/USDC')
        self.assertEqual(rec['tick_spacing'], 100)
        self.assertEqual(rec['tick'], -197314)
        self.assertAlmostEqual(rec['fee_nominal'], 0.0005)
        self.assertAlmostEqual(rec['fee'], 0.0005 * 0.95)       # what an unstaked LP keeps
        self.assertAlmostEqual(rec['tvl_usd'], 8867502.83)
        self.assertAlmostEqual(rec['volume_24h_usd'], 81341491.01)
        self.assertAlmostEqual(rec['liquidity'], 4e18 / 1e12)  # raw L / sqrt(10^18 * 10^6)
        self.assertAlmostEqual(rec['staked_share'], 0.75)
        self.assertEqual(rec['reward_mints'], [])

    def test_second_deployment_pool_same_shape_spacing_50(self):
        with mock.patch.object(dexes, 'evm_calls', fake_calls(factory=FACTORY_V3, nft=NPM_V3, pool=POOL_V3, spacing=50)), \
                mock.patch.object(dexes, '_get', side_effect=ValueError('no json')):
            rec = dexes.pool('aerodrome-slipstream', POOL_V3)
        with mock.patch.object(dexes, 'evm_calls', fake_calls()), mock.patch.object(dexes, '_get', side_effect=ValueError('no json')):
            old = dexes.pool('aerodrome-slipstream', POOL)
        self.assertEqual(sorted(rec), sorted(old))
        self.assertEqual(rec['address'], '0x3FE04A59Ebd38cF06080a6F60a98D124eb59392A')
        self.assertEqual(rec['tick_spacing'], 50)
        self.assertEqual(rec['pair'], 'WETH/USDC')
        self.assertAlmostEqual(rec['fee'], 0.0005 * 0.95)

    def test_registry_is_the_signers(self):
        # dexes.py and chains/evm/base_addresses.mjs carry the same (factory, NPM) pairs
        import pathlib
        import re
        js = (pathlib.Path(dexes.__file__).parent / 'chains/evm/base_addresses.mjs').read_text()
        pairs = re.findall(r"factory: '(0x[0-9a-fA-F]{40})',\s*npm: '(0x[0-9a-fA-F]{40})'", js)
        self.assertEqual({f.lower(): n.lower() for f, n in pairs}, dexes.SLIPSTREAM_DEPLOYMENTS)
        self.assertEqual(len(pairs), 2)

    def test_never_on_the_solana_board(self):
        self.assertNotIn('aerodrome-slipstream', dexes.KNOWN)
        self.assertNotIn('aerodrome-slipstream', dexes.ADAPTERS)
        self.assertIn('aerodrome-slipstream', dexes.SINGLE)


class Primitives(unittest.TestCase):
    def test_keccak256_vectors(self):
        self.assertEqual(dexes.keccak256(b'').hex(), 'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470')
        self.assertEqual(dexes.keccak256(b'abc').hex(), '4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45')
        # two blocks (more than the 136-byte rate); the value is viem's keccak256 of the same input
        self.assertEqual(dexes.keccak256(b'a' * 200).hex(), '96ea54061def936c4be90b518992fdc6f12f535068a256229aca54267b4d084d')

    def test_checksum_matches_eip55(self):
        # EIP-55's own test vectors
        for a in ('0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed', '0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359',
                  '0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB', '0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb', USDC):
            self.assertEqual(dexes.checksum_address(a.lower()), a)
        for bad in ('0x12', 'zz' * 20, '0x' + 'g' * 40):
            with self.assertRaises(ValueError):
                dexes.checksum_address(bad)

    def test_abi_decoding(self):
        self.assertEqual(dexes._abi_string(abi_string('USDC')), 'USDC')
        self.assertEqual(dexes._abi_string('0x' + b'MKR'.hex().ljust(64, '0')), 'MKR')    # bytes32 symbol
        self.assertEqual(dexes._signed(int(word(-197314), 16), 24), -197314)
        self.assertEqual(dexes._signed(100, 24), 100)


def _server(handler):
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['content-length'])))
            code, payload = handler(body)
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('content-type', 'application/json')
            self.send_header('content-length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f'http://127.0.0.1:{srv.server_address[1]}'
    return srv


if __name__ == '__main__':
    unittest.main()
