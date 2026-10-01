"""dexes.pool('aerodrome-slipstream', addr): the Base adapter lands in the usual
record shape, refuses a pool of another factory, survives GeckoTerminal being
down, and moves to the next RPC endpoint when one fails. No live network: the
RPC endpoints are local HTTP servers and the chain reads are stubbed."""
import http.server
import json
import threading
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import dexes

POOL = '0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59'
WETH = '0x4200000000000000000000000000000000000006'
USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'


def word(n, bits=256):
    return f'{n % (1 << bits):064x}'


def abi_string(s):
    b = s.encode()
    return '0x' + word(32) + word(len(b)) + b.hex().ljust(64 * ((len(b) + 31) // 32 or 1), '0')


def fake_calls(factory=dexes.SLIPSTREAM_FACTORY, tick=-197314, sqrt_p=4116179725402233657475978):
    """A stand-in for evm_calls answering the pool's and tokens' selectors."""
    pool = {
        dexes._SEL['factory']: '0x' + word(int(factory, 16)),
        dexes._SEL['token0']: '0x' + word(int(WETH, 16)),
        dexes._SEL['token1']: '0x' + word(int(USDC, 16)),
        dexes._SEL['tickSpacing']: '0x' + word(100),
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

    def calls(pairs, urls=None):
        return [pool[data] if to.lower() == POOL else tokens[(to.lower(), data)] for to, data in pairs]
    return calls


class Failures(unittest.TestCase):
    def test_a_pool_of_another_factory_is_refused(self):
        other = '0xade65c38cd4849adba595a4323a8c7ddfe89716a'
        with mock.patch.object(dexes, 'evm_calls', fake_calls(factory=other)):
            with self.assertRaisesRegex(ValueError, 'not a pool of the Slipstream factory'):
                dexes.slipstream_state(POOL)
            self.assertIsNone(dexes.pool('aerodrome-slipstream', POOL))

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
