"""Pool discovery across Solana concentrated-liquidity DEXes, in one shape.

Each DEX has its own API, its own field names and its own fee units, and two of
them do not publish the one number the fee model needs — the pool's active
liquidity — so it is read from the pool account on chain instead. Everything
below turns that into one record the engine can score without knowing where the
pool lives:

    dex             'orca' | 'raydium-clmm' | 'byreal' | 'pancakeswap-v3-solana' | 'meteora-dlmm'
                    | 'aerodrome-slipstream' (Base; single-pool lookup only, never on the board)
                    | 'uniswap-v3-unichain' (Unichain; single-pool lookup only, never on the board)
                    | 'uniswap-v3-polygon' (Polygon; single-pool lookup only, never on the board)
    kind            'clmm' (ticks) | 'dlmm' (bins)
    address, pair
    token_a/token_b {address, symbol, name, decimals}
    price           token B per token A, human units
    fee             fraction actually charged per swap (0.0004 = 4 bps)
    fee_source      'nominal' | 'realised' (24h fees / 24h volume, for dynamic-fee pools)
    tvl_usd, volume_24h_usd, fees_24h_usd
    liquidity       clmm: active L in human units (Orca's raw L / sqrt(10^da 10^db))
    active_bin_usd  dlmm: dollar liquidity in the active bin
    bin_step        dlmm: bin width in basis points
    adaptive_fee    True when the fee moves with volatility; Orca's cannot be opened
    tick_spacing    clmm only, informational

`fetch_all` runs every DEX in parallel and returns what each produced, along
with the error for any that failed, so one API being down costs one DEX, not
the scan.

Not here, and why: Meteora DAMM v2 sets one price range per pool that every
position shares, so there is no band to choose and nothing for the optimiser
to do. Jupiter runs no range-liquidity product a third party can deposit into;
its price and token APIs are used below as a pricer instead. Saros DLMM and
DeFiTuna's own pools carry a few thousand dollars a day. HumidiFi, ZeroFi,
SolFi and Lifinity take no outside liquidity.
"""
import concurrent.futures as cf

from venues.aerodrome import pools as aerodrome_pools
from venues.byreal import pools as byreal_pools
from venues.meteora_dlmm import pools as meteora_pools
from venues.orca import pools as orca_pools
from venues.pancakeswap_v3 import pools as pancake_pools
from venues.raydium_clmm import pools as raydium_pools
from venues.uniswap_v3 import pools as uniswap_pools

# Every DEX the board scans, with the adapter that lists it. The config's
# `dexes` column names entries of this table.
KNOWN = ('orca', 'raydium-clmm', 'byreal', 'pancakeswap-v3-solana', 'meteora-dlmm')

ADAPTERS = {'orca': orca_pools.orca, 'raydium-clmm': raydium_pools.raydium, 'byreal': byreal_pools.byreal,
            'pancakeswap-v3-solana': pancake_pools.pancakeswap, 'meteora-dlmm': meteora_pools.meteora_dlmm}
# One pool by address. The EVM venues are here only: never on the Solana board.
SINGLE = {'orca': orca_pools.orca_pool, 'raydium-clmm': raydium_pools.raydium_pool,
          'byreal': byreal_pools.byreal_pool, 'pancakeswap-v3-solana': pancake_pools.pancakeswap_pool,
          'meteora-dlmm': meteora_pools.meteora_dlmm_pool,
          'aerodrome-slipstream': aerodrome_pools.slipstream_pool,
          'uniswap-v3-unichain': uniswap_pools.uniswap_v3_pool,
          'uniswap-v3-polygon': uniswap_pools.uniswap_v3_polygon_pool}


def fetch_all(dexes=KNOWN, limit=50, timeout=120):
    """Every DEX in parallel. Returns ({dex: [records]}, {dex: error})."""
    out, errors = {}, {}
    with cf.ThreadPoolExecutor(max_workers=len(dexes) or 1) as ex:
        futs = {ex.submit(ADAPTERS[d], limit): d for d in dexes if d in ADAPTERS}
        for d in dexes:
            if d not in ADAPTERS:
                errors[d] = 'unknown dex'
        for fut, d in futs.items():
            try:
                out[d] = fut.result(timeout=timeout)
            except Exception as e:      # one DEX down is one DEX, not the scan
                errors[d] = f'{type(e).__name__}: {str(e)[:120]}'
                out[d] = []
    return out, errors


def pool(dex, address):
    """One pool, by DEX and address, in the same shape."""
    fn = SINGLE.get(dex)
    return fn(address) if fn else None


if __name__ == '__main__':
    import sys
    which = tuple(sys.argv[1].split(',')) if len(sys.argv) > 1 else KNOWN
    recs, errs = fetch_all(which, limit=int(sys.argv[2]) if len(sys.argv) > 2 else 20)
    for dex, rows in recs.items():
        print(f'== {dex}: {len(rows)} pools' + (f'  ERROR {errs[dex]}' if dex in errs else ''))
        for r in rows[:15]:
            L = r.get('liquidity') if r['kind'] == 'clmm' else r.get('active_bin_usd')
            print(f"  {r['pair'][:18]:<19} fee {r['fee'] * 100:>6.3f}% {r['fee_source'][:4]}  "
                  f"tvl ${r['tvl_usd'] / 1e6:>7.2f}M  vol24 ${r['volume_24h_usd'] / 1e6:>7.2f}M  "
                  f"{'L' if r['kind'] == 'clmm' else '$/bin'} {'-' if L is None else f'{L:.3g}'}  "
                  f"{r['address']}" + (f"  [{r['note']}]" if r.get('note') else ''))
    for dex, e in errs.items():
        if dex not in recs or not recs[dex]:
            print(f'== {dex}: ERROR {e}')
