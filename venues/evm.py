"""EVM reads without a library: keccak, checksum addresses, one eth_call batch."""

import json
import urllib.request

from venues.api import UA


_SEL = {'token0': '0x0dfe1681', 'token1': '0xd21220a7', 'tickSpacing': '0xd0c93a7c',
        'fee': '0xddca3f43', 'unstakedFee': '0xb64cc67b', 'liquidity': '0x1a686502',
        'stakedLiquidity': '0x3ab04b20', 'slot0': '0x3850c7bd', 'factory': '0xc45a0155',
        'nft': '0x47ccca02', 'decimals': '0x313ce567', 'symbol': '0x95d89b41',
        'getPool': '0x28af8d0b'}


_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
       0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
       0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
       0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]


_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]


_M64 = (1 << 64) - 1


def keccak256(data):
    """Keccak-256 as Ethereum uses it (NOT hashlib's sha3_256: different
    padding). Only for EIP-55 checksums of a few addresses; pure Python."""
    rate = 136
    msg = bytearray(data) + b'\x01'
    msg += b'\x00' * (-len(msg) % rate)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        for i in range(rate // 8):
            a[i % 5][i // 5] ^= int.from_bytes(msg[off + 8 * i:off + 8 * i + 8], 'little')
        for rc in _RC:
            c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & _M64) for x in range(5)]
            a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    r = _ROT[x][y]
                    b[y][(2 * x + 3 * y) % 5] = ((a[x][y] << r) | (a[x][y] >> (64 - r))) & _M64 if r else a[x][y]
            a = [[b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            a[0][0] ^= rc
    return b''.join(a[i % 5][i // 5].to_bytes(8, 'little') for i in range(4))


def checksum_address(addr):
    """EIP-55 mixed-case form of a 0x address, as the EVM signer prints it, so
    the loop's string comparisons agree with the signer's output."""
    h = addr.lower().removeprefix('0x')
    if len(h) != 40 or any(ch not in '0123456789abcdef' for ch in h):
        raise ValueError(f'not an address: {addr!r}')
    digest = keccak256(h.encode()).hex()
    return '0x' + ''.join(ch.upper() if int(digest[i], 16) >= 8 else ch for i, ch in enumerate(h))


def evm_calls(calls, urls, timeout=20, chain='Base'):
    """eth_call each (to, data) at 'latest' in ONE JSON-RPC batch. Returns the
    hex results in order. Any error, missing id or failed call moves to the
    next endpoint; all failing raises. In-process: the URL may carry a key."""
    body = json.dumps([{'jsonrpc': '2.0', 'id': i, 'method': 'eth_call',
                        'params': [{'to': to, 'data': data}, 'latest']}
                       for i, (to, data) in enumerate(calls)]).encode()
    errors = []
    for url in urls:
        try:
            req = urllib.request.Request(url, data=body, headers={'content-type': 'application/json',
                                                                  'user-agent': UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                rows = json.loads(resp.read())
            by_id = {r.get('id'): r for r in rows} if isinstance(rows, list) else {}
            out = []
            for i in range(len(calls)):
                r = by_id.get(i) or {}
                if 'result' not in r or r['result'] in (None, '0x'):
                    raise RuntimeError(f"call {i}: {(r.get('error') or {}).get('message', 'no result')}")
                out.append(r['result'])
            return out
        except Exception as e:         # next endpoint; the last error is reported
            errors.append(f'{url.split("/")[2]}: {str(e)[:120]}')
    raise RuntimeError(f'all {chain} RPC endpoints failed: ' + ' | '.join(errors))


def _words(hexstr):
    h = hexstr.removeprefix('0x')
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def _signed(word, bits):
    word &= (1 << bits) - 1
    return word - (1 << bits) if word >> (bits - 1) else word


def _abi_string(hexstr):
    """An ABI-encoded `string` return (dynamic), or a bytes32 one (old tokens)."""
    w = _words(hexstr)
    h = hexstr.removeprefix('0x')
    if len(w) >= 3 and w[0] == 32:
        return bytes.fromhex(h[128:128 + 2 * w[1]]).decode('utf-8', 'replace')
    return bytes.fromhex(h[:64]).rstrip(b'\0').decode('utf-8', 'replace')


def _evm_addr(word):
    return checksum_address(f'0x{word & ((1 << 160) - 1):040x}')
