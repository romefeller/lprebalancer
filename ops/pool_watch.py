"""Pool watch (owner, 2026-10-26): SOL, ETH and POL pools like 8sLb, with R,
fee velocity and depth, against 8sLb over the same hours. Since 2026-10-28 every
sampled pool (the SOL/USDC pools of venue_watch included) is rolled up per hour
into pool_hourly (sql/064), kept a year, so R stays judged after fee_growth's 14
days: the shadow stats that name a second-best pool.

The Solana pools are already sampled by sol-usdc's venue_watch (sql/061). The
EVM pools (Uniswap v3 on Base, Arbitrum and Polygon) are read here over public
RPCs and written to fee_growth in the Solana units: sqrtPriceX96 >> 32 is the
Q64 square-root price, feeGrowthGlobalX128 >> 64 the X64 fee growth. So one
report ranks them all, and fee_growth keeps 14 days as for every other pool.

    python3 ops/pool_watch.py sample          one sample of every EVM pool, then the hourly roll-up
    python3 ops/pool_watch.py report [days]   every pool by R over `days` (default 7), SOL/stable first

R = fee yield per dollar of full-range value / (variance / 8): what a band
earns over what its in-band loss costs, the same at every width while in range.
Nothing in the bot reads this; it moves no money.
"""
import json
import math
import pathlib
import sys
import urllib.request
from datetime import timedelta

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
    'ethereum': ('https://ethereum-rpc.publicnode.com', 'https://eth.drpc.org'),
}
WETH_BASE = '0x4200000000000000000000000000000000000006'
USDC_BASE = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
WETH_ARB = '0x82af49447d8a07e3bd95bd0d56f35241523fbab1'
USDC_ARB = '0xaf88d065e77c8cc2239327c5edb3a432268e5831'
WPOL = '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270'
USDT_POLY = '0xc2132d05d31c914a87c6611c10748aeb04b58e8f'
USDC_POLY = '0x3c499c542cef5e3811e1192ce70d8cc03d5c3359'
WETH_ETH = '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2'
USDC_ETH = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'

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
    # 2026-10-28: the deepest ETH pools (token0/token1/fee read on chain)
    ('ethereum', 'uniswap-v3-ethereum', '0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640', 'USDC/WETH 0.05%',
     (USDC_ETH, 6), (WETH_ETH, 18)),
    ('base', 'uniswap-v3-base', '0xd0b53d9277642d899df5c87a3966a349a798f224', 'WETH/USDC 0.05%',
     (WETH_BASE, 18), (USDC_BASE, 6)),
)
CONTROL = ('solana', 'raydium-clmm', '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj', 'SOL/USDC 0.01% (ours)', None, None)
# Quote tokens worth $1, for the depth in dollars.
STABLES = {'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB',
           'USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB', '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH',
           USDC_BASE, USDC_ARB, USDT_POLY, USDC_POLY, USDC_ETH}
SOL = 'So11111111111111111111111111111111111111112'
SYMBOLS = {SOL: 'SOL', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v': 'USDC',
           'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB': 'USDT', 'USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB': 'USD1',
           '2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH': 'USDG', 'J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn': 'JitoSOL',
           WETH_BASE: 'WETH', WETH_ARB: 'WETH', WETH_ETH: 'WETH', WPOL: 'WPOL', USDC_BASE: 'USDC', USDC_ARB: 'USDC',
           USDC_ETH: 'USDC', USDC_POLY: 'USDC', USDT_POLY: 'USDT'}
HOURLY_KEEP_DAYS = 365
dt_hour = timedelta(hours=1)
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


def intervals(rows):
    """(end ts, seconds, fee yield per full-range unit, squared log price step)
    of each usable interval between consecutive samples. Intervals longer than
    GAP_S are left out. Pure."""
    for a, b in zip(rows, rows[1:]):
        dt = (b['ts'] - a['ts']).total_seconds()
        pa, pb = price(a), price(b)
        if dt <= 0 or dt > GAP_S or pa <= 0 or pb <= 0:
            continue
        dg0 = (int(b['g0']) - int(a['g0'])) % U128 / Q64 / 10 ** b['dec_a']
        dg1 = (int(b['g1']) - int(a['g1'])) % U128 / Q64 / 10 ** b['dec_b']
        full = 2 * int(b['sqrt_price']) / Q64 / 10 ** b['dec_b']      # 2 sqrt(p) per unit L, in token B
        yield b['ts'], dt, (dg0 * pb + dg1) / full, math.log(pb / pa) ** 2


def stats(fees, var, secs):
    """R, fee velocity (+/-1% band, % per day) and volatility from summed fee
    yield, squared log steps and seconds. None without seconds. Pure."""
    if secs <= 0:
        return None
    fy_s, var_s = fees / secs, var / secs
    return {'r': fy_s / (var_s / 8) if var_s > 0 else None,
            'fee_pct_day': fy_s * 86400 / (1 - 1 / math.sqrt(BAND)) * 100,
            'vol_pct_year': math.sqrt(var_s * 86400 * 365) * 100, 'hours': secs / 3600}


def pool_stats(rows):
    """R, fee velocity and volatility of one pool from its samples in time
    order (dicts with ts, sqrt_price, g0, g1, liquidity, dec_a, dec_b), and the
    token B in a +/-1% band at the last price. None with fewer than two usable
    intervals. Pure."""
    iv = list(intervals(rows))
    if len(iv) < 2:
        return None
    out = stats(sum(x[2] for x in iv), sum(x[3] for x in iv), sum(x[1] for x in iv))
    last = rows[-1]
    sp = int(last['sqrt_price']) / Q64
    out.update(depth_b=2 * int(last['liquidity'] or 0) * sp / 10 ** last['dec_b'] * (1 - 1 / math.sqrt(BAND)),
               mint_b=last['mint_b'])
    return out


def hourly(rows):
    """{hour start: [fee yield, squared log steps, seconds]} of the samples,
    each interval counted in the hour it ends in. Pure."""
    out = {}
    for ts, dt, fy, r2 in intervals(rows):
        h = out.setdefault(ts.replace(minute=0, second=0, microsecond=0), [0.0, 0.0, 0.0])
        h[0] += fy; h[1] += r2; h[2] += dt
    return out


def samples(pool, days):
    """The pool's fee_growth samples of the last `days`, oldest first."""
    with db.cursor() as cur:
        cur.execute('select ts, sqrt_price, g0, g1, liquidity, dec_a, dec_b, mint_b from fee_growth '
                    'where pool = %s and ts > now() - make_interval(secs => %s) order by ts', (pool, days * 86400))
        return [dict(r) for r in cur.fetchall()]


def rollup():
    """Every sampled pool's finished hours not yet in pool_hourly, from
    fee_growth; rows older than HOURLY_KEEP_DAYS go. Returns rows written."""
    n = 0
    with db.cursor(commit=True) as cur:
        cur.execute("select date_trunc('hour', now()) h")
        this_hour = cur.fetchone()['h']
        cur.execute('select f.pool, max(f.dex) dex, max(f.mint_a) mint_a, max(f.mint_b) mint_b, '
                    '(select max(hour) from pool_hourly p where p.pool = f.pool) done '
                    'from fee_growth f group by f.pool')
        for p in cur.fetchall():
            frm = p['done'] + dt_hour if p['done'] else None
            cur.execute('select ts, sqrt_price, g0, g1, liquidity, dec_a, dec_b, mint_b from fee_growth '
                        'where pool = %s and ts >= coalesce(%s::timestamptz - interval \'1 hour\', \'-infinity\') '
                        'and ts < %s order by ts', (p['pool'], frm, this_hour))
            for h, (fy, r2, secs) in sorted(hourly([dict(r) for r in cur.fetchall()]).items()):
                if frm and h < frm:
                    continue
                cur.execute('insert into pool_hourly (pool, hour, dex, mint_a, mint_b, fee_yield, var_sum, secs) '
                            'values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (pool, hour) do nothing',
                            (p['pool'], h, p['dex'], p['mint_a'], p['mint_b'], fy, r2, secs))
                n += cur.rowcount
        cur.execute('delete from pool_hourly where hour < now() - make_interval(days => %s)', (HOURLY_KEEP_DAYS,))
    return n


def label(pool, mint_a, mint_b):
    """The watch label of a pool, else its pair from SYMBOLS."""
    for _, _, addr, lab, _, _ in WATCH + (CONTROL,):
        if addr == pool:
            return lab
    return '/'.join(SYMBOLS.get(m, (m or '?')[:4]) for m in (mint_a, mint_b))


def sol_stable(mint_a, mint_b):
    """Whether the pair is SOL against a dollar stablecoin: the bot's own kind."""
    return {mint_a, mint_b} - {SOL} <= STABLES and SOL in (mint_a, mint_b)


def report(days):
    """Every rolled-up pool by R over the last `days`, with 24 h and 30 d R
    beside: the SOL/stable pools first (the bot's own kind: best and
    second-best), then the rest."""
    with db.cursor() as cur:
        cur.execute("""select pool, max(dex) dex, max(mint_a) mint_a, max(mint_b) mint_b,
                       sum(fee_yield) filter (where hour > now() - make_interval(secs => %(w)s)) f,
                       sum(var_sum) filter (where hour > now() - make_interval(secs => %(w)s)) v,
                       sum(secs) filter (where hour > now() - make_interval(secs => %(w)s)) s,
                       sum(fee_yield) filter (where hour > now() - interval '1 day') f1,
                       sum(var_sum) filter (where hour > now() - interval '1 day') v1,
                       sum(secs) filter (where hour > now() - interval '1 day') s1,
                       sum(fee_yield) f30, sum(var_sum) v30, sum(secs) s30
                       from pool_hourly where hour > now() - interval '30 days' group by pool""", {'w': days * 86400})
        rows = cur.fetchall()
    out = []
    for r in rows:
        w = stats(r['f'] or 0, r['v'] or 0, r['s'] or 0)
        if not w:
            continue
        d1, d30 = stats(r['f1'] or 0, r['v1'] or 0, r['s1'] or 0), stats(r['f30'], r['v30'], r['s30'])
        out.append((sol_stable(r['mint_a'], r['mint_b']), r, w, d1, d30))
    out.sort(key=lambda x: (not x[0], -(x[2]['r'] or 0)))
    rf = lambda s: f"{s['r']:.2f}" if s and s['r'] else '—'
    print(f"{'':4} {'dex':20} {'pool':22} {'R':>5} {'fees ±1% %/d':>12} {'R 24h':>6} {'R 30d':>6} {'vol %/y':>7} {'hours':>6}  address")
    rank = 0
    for ss, r, w, d1, d30 in out:
        rank = rank + 1 if ss else 0
        tag = {1: 'BEST', 2: '2ND'}.get(rank, 'SOL$' if ss else '')
        print(f"{tag:4} {r['dex']:20} {label(r['pool'], r['mint_a'], r['mint_b']):22} {rf(w):>5} {w['fee_pct_day']:12.2f} "
              f"{rf(d1):>6} {rf(d30):>6} {w['vol_pct_year']:7.0f} {w['hours']:6.1f}  {r['pool']}")


def main(argv):
    if argv[:1] == ['sample']:
        failed = sample()
        rollup()            # every pool's finished hours, whatever the EVM reads did
        return 1 if failed else 0
    if argv[:1] == ['report']:
        report(float(argv[1]) if len(argv) > 1 else 7)
        return 0
    print(__doc__.strip().split('\n\n')[2], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
