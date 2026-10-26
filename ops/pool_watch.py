"""Pool watch (owner, 2026-10-26): six SOL, ETH and POL pools like 8sLb, with R,
fee velocity and depth, against 8sLb over the same hours.

The Solana pools are already sampled by sol-usdc's venue_watch (sql/061). The
EVM pools (Uniswap v3 on Base, Arbitrum and Polygon) are read here over public
RPCs and written to fee_growth in the Solana units: sqrtPriceX96 >> 32 is the
Q64 square-root price, feeGrowthGlobalX128 >> 64 the X64 fee growth. So one
report ranks them all, and fee_growth keeps 14 days as for every other pool.

    python3 ops/pool_watch.py sample          one sample of every EVM pool
    python3 ops/pool_watch.py report [days]   the watch, best R first (default 7)

R = fee yield per dollar of full-range value / (variance / 8): what a band
earns over what its in-band loss costs, the same at every width while in range.
Nothing in the bot reads this; it moves no money.
"""
import json
import math
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db  # noqa: E402

Q64 = 2 ** 64
U128 = 2 ** 128
GAP_S = 3600            # a gap longer than this between samples is not one interval
BAND = 1.01             # the fee velocity is that of a centred +/-1% band
RPC_TIMEOUT_S = 15

RPCS = {
    'base': ('https://mainnet.base.org', 'https://base.llamarpc.com'),
    'arbitrum': ('https://arb1.arbitrum.io/rpc', 'https://arbitrum.drpc.org'),
    'polygon': ('https://polygon.drpc.org', 'https://1rpc.io/matic'),
}
WETH_BASE = '0x4200000000000000000000000000000000000006'
USDC_BASE = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
WETH_ARB = '0x82af49447d8a07e3bd95bd0d56f35241523fbab1'
USDC_ARB = '0xaf88d065e77c8cc2239327c5edb3a432268e5831'
WPOL = '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270'
USDT_POLY = '0xc2132d05d31c914a87c6611c10748aeb04b58e8f'
USDC_POLY = '0x3c499c542cef5e3811e1192ce70d8cc03d5c3359'

# (chain, dex, pool, label, (token0, decimals), (token1, decimals)). Solana rows
# carry no tokens: venue_watch samples them. Chosen 2026-10-26 from 6 days of
# fee counters (Solana) and GeckoTerminal volume / liquidity (EVM); Aerodrome
# left out (its fees go to stakers, not to an unstaked position).
WATCH = (
    ('solana', 'raydium-clmm', 'AQAGYQsdU853WAKhXM79CgNdoyhrRwXvYHX6qrDyC1FS', 'SOL/USD1 0.25%', None, None),
    ('solana', 'raydium-clmm', '9n3dSLrERZQp95dHXywft7xV8D8xnGFLaUHtEhQVaXaC', 'SOL/PYTH 0.25%', None, None),
    ('base', 'uniswap-v3-base', '0x6c561b446416e1a00e8e93e221854d6ea4171372', 'WETH/USDC 0.30%',
     (WETH_BASE, 18), (USDC_BASE, 6)),
    ('arbitrum', 'uniswap-v3-arbitrum', '0xc6962004f452be9203591991d15f6b388e09e8d0', 'WETH/USDC 0.05%',
     (WETH_ARB, 18), (USDC_ARB, 6)),
    ('polygon', 'uniswap-v3-polygon', '0x9b08288c3be4f62bbf8d1c20ac9c5e6f9467d8b7', 'WPOL/USDT 0.05%',
     (WPOL, 18), (USDT_POLY, 6)),
    ('polygon', 'uniswap-v3-polygon', '0xb6e57ed85c4c9dbfef2a68711e9d6f36c56e0fcb', 'WPOL/USDC 0.05%',
     (WPOL, 18), (USDC_POLY, 6)),
)
CONTROL = ('solana', 'raydium-clmm', '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj', 'SOL/USDC 0.01% (ours)', None, None)
# Quote tokens worth $1, for the depth in dollars.
STABLES = {'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB',
           'USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB', USDC_BASE, USDC_ARB, USDT_POLY, USDC_POLY}
SELECTORS = (('slot0', '0x3850c7bd'), ('liquidity', '0x1a686502'), ('g0', '0xf3058399'), ('g1', '0x46141319'))


def evm_state(words, token0, token1):
    """A fee_growth sample from a Uniswap v3 pool's call results (hex words of
    slot0, liquidity, feeGrowthGlobal0X128, feeGrowthGlobal1X128), in the Q64
    units of the Solana samples. Pure."""
    return {'sqrt_price': int(words['slot0'][2:66], 16) >> 32, 'liquidity': int(words['liquidity'], 16),
            'g0': (int(words['g0'], 16) >> 64) % U128, 'g1': (int(words['g1'], 16) >> 64) % U128, 'rewards': [],
            'mint_a': token0[0], 'dec_a': token0[1], 'mint_b': token1[0], 'dec_b': token1[1]}


def read_evm(chain, pool, token0, token1):
    """One sample of an EVM pool, from the first RPC of its chain that answers."""
    batch = [{'jsonrpc': '2.0', 'id': i, 'method': 'eth_call', 'params': [{'to': pool, 'data': sel}, 'latest']}
             for i, (_, sel) in enumerate(SELECTORS)]
    err = None
    for url in RPCS[chain]:
        try:
            req = urllib.request.Request(url, json.dumps(batch).encode(),
                                         {'Content-Type': 'application/json', 'User-Agent': 'pool-watch'})
            res = {r['id']: r.get('result') for r in json.load(urllib.request.urlopen(req, timeout=RPC_TIMEOUT_S))}
            if any(not res.get(i) or res[i] == '0x' for i in range(len(SELECTORS))):
                raise ValueError(f'empty result from {url}')
            return evm_state({name: res[i] for i, (name, _) in enumerate(SELECTORS)}, token0, token1)
        except Exception as e:      # the next RPC of the chain
            err = e
    raise RuntimeError(f'{chain} {pool}: no RPC answered ({err})')


def sample():
    """One sample of every EVM pool into fee_growth. A pool that fails is
    reported and skipped; the others are still sampled."""
    failed = 0
    for chain, dex, pool, label, t0, t1 in WATCH:
        if chain == 'solana':
            continue
        try:
            db.record_fee_state(dex, pool, read_evm(chain, pool, t0, t1))
        except Exception as e:
            failed += 1
            print(f'{label} {pool}: {e}', file=sys.stderr)
    return failed


def price(row):
    """Token A in token B, human units, of one sample. Pure."""
    return (int(row['sqrt_price']) / Q64) ** 2 * 10 ** (row['dec_a'] - row['dec_b'])


def pool_stats(rows):
    """R, fee velocity (+/-1% band, % per day) and volatility of one pool from
    its samples in time order: dicts with ts, sqrt_price, g0, g1, liquidity,
    dec_a, dec_b. Intervals longer than GAP_S are left out of fees and
    variance alike. None with fewer than two usable intervals. Pure."""
    fees = var = secs = 0.0
    n = 0
    for a, b in zip(rows, rows[1:]):
        dt = (b['ts'] - a['ts']).total_seconds()
        pa, pb = price(a), price(b)
        if dt <= 0 or dt > GAP_S or pa <= 0 or pb <= 0:
            continue
        dg0 = (int(b['g0']) - int(a['g0'])) % U128 / Q64 / 10 ** b['dec_a']
        dg1 = (int(b['g1']) - int(a['g1'])) % U128 / Q64 / 10 ** b['dec_b']
        full = 2 * int(b['sqrt_price']) / Q64 / 10 ** b['dec_b']      # 2 sqrt(p) per unit L, in token B
        fees += (dg0 * pb + dg1) / full
        var += math.log(pb / pa) ** 2
        secs += dt
        n += 1
    if n < 2 or secs <= 0:
        return None
    fy_s, var_s = fees / secs, var / secs
    last = rows[-1]
    sp = int(last['sqrt_price']) / Q64
    depth_b = 2 * int(last['liquidity'] or 0) * sp / 10 ** last['dec_b'] * (1 - 1 / math.sqrt(BAND))
    return {'r': fy_s / (var_s / 8) if var_s > 0 else None,
            'fee_pct_day': fy_s * 86400 / (1 - 1 / math.sqrt(BAND)) * 100,
            'vol_pct_year': math.sqrt(var_s * 86400 * 365) * 100,
            'depth_b': depth_b, 'mint_b': last['mint_b'], 'hours': secs / 3600}


def samples(pool, days):
    """The pool's fee_growth samples of the last `days`, oldest first."""
    with db.cursor() as cur:
        cur.execute('select ts, sqrt_price, g0, g1, liquidity, dec_a, dec_b, mint_b from fee_growth '
                    'where pool = %s and ts > now() - make_interval(secs => %s) order by ts', (pool, days * 86400))
        return [dict(r) for r in cur.fetchall()]


def report(days):
    """One line per watched pool and 8sLb, best R first, with the last day's
    fee velocity beside the window's."""
    out = []
    for chain, dex, pool, label, _, _ in WATCH + (CONTROL,):
        rows = samples(pool, days)
        full, day = pool_stats(rows), pool_stats(samples(pool, 1))
        out.append((chain, label, pool, full, day))
    out.sort(key=lambda x: -((x[3] or {}).get('r') or 0))
    print(f"{'chain':9s} {'pool':22s} {'R':>5} {'fees ±1% %/d':>12} {'last 24h':>8} {'vol %/y':>7} "
          f"{'±1% depth':>11} {'hours':>6}  address")
    for chain, label, pool, s, d in out:
        if not s:
            print(f'{chain:9s} {label:22s}  no data yet  {pool}')
            continue
        depth = f"${s['depth_b']:,.0f}" if s['mint_b'] in STABLES else '—'
        r = f"{s['r']:.2f}" if s['r'] else '—'
        print(f"{chain:9s} {label:22s} {r:>5} {s['fee_pct_day']:12.2f} {(d or {}).get('fee_pct_day', 0):8.2f} "
              f"{s['vol_pct_year']:7.0f} {depth:>11} {s['hours']:6.1f}  {pool}")


def main(argv):
    if argv[:1] == ['sample']:
        return 1 if sample() else 0
    if argv[:1] == ['report']:
        report(float(argv[1]) if len(argv) > 1 else 7)
        return 0
    print(__doc__.strip().split('\n\n')[2], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
