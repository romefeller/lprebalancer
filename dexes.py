"""Pool discovery across Solana concentrated-liquidity DEXes, in one shape.

Each DEX has its own API, its own field names and its own fee units, and two of
them do not publish the one number the fee model needs — the pool's active
liquidity — so it is read from the pool account on chain instead. Everything
below turns that into one record the engine can score without knowing where the
pool lives:

    dex             'orca' | 'raydium-clmm' | 'byreal' | 'pancakeswap-v3-solana' | 'meteora-dlmm'
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
import base64
import concurrent.futures as cf
import json
import math
import os
import subprocess

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
ORCA = 'https://api.orca.so/v2/solana'
RAYDIUM = 'https://api-v3.raydium.io'
BYREAL = 'https://api2.byreal.io/byreal/api/dex/v2'
METEORA_DLMM = 'https://dlmm.datapi.meteora.ag'
GECKO = 'https://api.geckoterminal.com/api/v2/networks/solana'
JUPITER = 'https://lite-api.jup.ag'

RAYDIUM_CLMM_PROGRAM = 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK'
BYREAL_CLMM_PROGRAM = 'REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2'
PANCAKE_CLMM_PROGRAM = 'HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq'
METEORA_DLMM_PROGRAM = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'

# Every DEX this module knows, with the adapter that lists it. The config's
# `dexes` column names entries of this table.
KNOWN = ('orca', 'raydium-clmm', 'byreal', 'pancakeswap-v3-solana', 'meteora-dlmm')


def _get(url, accept='application/json', timeout=40):
    r = subprocess.run(['curl', '-s', '--max-time', str(timeout),
                        '-H', f'accept: {accept}', '-H', f'user-agent: {UA}', url],
                       capture_output=True, text=True)
    return json.loads(r.stdout)


def _f(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _token(address, symbol, name, decimals):
    return {'address': address, 'symbol': symbol or '?', 'name': name or symbol or '?',
            'decimals': int(decimals)}


def effective_fee(nominal, fees_24h, volume_24h):
    """The fee the pool actually charged, when the nominal figure is a placeholder.

    Dynamic-fee pools publish a nominal rate that is not what swaps pay
    (Byreal's SOL/USDC says 1 ppm and earns ~1 bp). The realised ratio is the
    honest number there; everywhere else the nominal rate is exact and the
    ratio is noisy, so it is only used when the nominal rate is missing or
    below one basis point.
    """
    realised = fees_24h / volume_24h if volume_24h > 0 and fees_24h > 0 else None
    if nominal and nominal >= 1e-4:
        return nominal, 'nominal'
    if realised and 1e-6 <= realised <= 0.05:
        return realised, 'realised'
    return nominal or 0.0, 'nominal'


# --- on-chain CLMM state (Raydium layout, also Byreal's) ----------------------

def _rpc_url():
    return (os.environ.get('LPBOT_RPC') or os.environ.get('SOLANA_RPC_URL')
            or 'https://api.mainnet-beta.solana.com')


def pool_accounts(addresses):
    """Raw account bytes for up to 100 pools in one RPC call."""
    out = {}
    for i in range(0, len(addresses), 100):
        chunk = addresses[i:i + 100]
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getMultipleAccounts',
                           'params': [chunk, {'encoding': 'base64'}]})
        r = subprocess.run(['curl', '-s', '--max-time', '40', '-H', 'content-type: application/json',
                            '-d', body, _rpc_url()], capture_output=True, text=True)
        try:
            vals = json.loads(r.stdout)['result']['value']
        except Exception:
            continue
        for addr, v in zip(chunk, vals):
            if v and v.get('data'):
                out[addr] = base64.b64decode(v['data'][0])
    return out


_B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b58(b):
    n = int.from_bytes(b, 'big')
    out = ''
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return '1' * (len(b) - len(b.lstrip(b'\0'))) + out


def decode_clmm_state(raw):
    """Raydium's PoolState, which Byreal and PancakeSwap share: after the
    8-byte discriminator come a bump, then amm_config, owner, the two mints,
    the two vaults and the observation key, then the fields the fee model
    needs. Verified against the API price on all three DEXes."""
    o = 8 + 1 + 32 * 7
    if len(raw) < o + 40:
        return None
    dec0, dec1 = raw[o], raw[o + 1]
    tick_spacing = int.from_bytes(raw[o + 2:o + 4], 'little')
    liquidity = int.from_bytes(raw[o + 4:o + 20], 'little')
    sqrt_price = int.from_bytes(raw[o + 20:o + 36], 'little')
    tick = int.from_bytes(raw[o + 36:o + 40], 'little', signed=True)
    price = (sqrt_price / 2 ** 64) ** 2 * 10 ** (dec0 - dec1)
    return {'decimals_a': dec0, 'decimals_b': dec1, 'tick_spacing': tick_spacing,
            'liquidity_raw': liquidity, 'tick': tick, 'price': price,
            'liquidity': liquidity / math.sqrt(10 ** dec0 * 10 ** dec1),
            'amm_config': b58(raw[9:41]),
            'mint_a': b58(raw[73:105]), 'mint_b': b58(raw[105:137])}


def decode_amm_config(raw):
    """Raydium's AmmConfig: bump, index, owner, then the fee rates in ppm."""
    o = 8
    if len(raw) < o + 45:
        return None
    return {'protocol_fee_rate': int.from_bytes(raw[o + 35:o + 39], 'little'),
            'trade_fee_rate': int.from_bytes(raw[o + 39:o + 43], 'little'),
            'tick_spacing': int.from_bytes(raw[o + 43:o + 45], 'little')}


def attach_chain_state(records):
    """Fill `liquidity` (and check the price) from the pool accounts themselves.
    A record whose account cannot be read or decoded keeps liquidity=None and
    the engine declines to score it rather than guess."""
    accounts = pool_accounts([r['address'] for r in records])
    for r in records:
        st = decode_clmm_state(accounts.get(r['address'], b''))
        if not st:
            continue
        r['liquidity'] = st['liquidity']
        r['tick_spacing'] = st['tick_spacing']
        r['chain_price'] = st['price']
        # The API price and the chain price must agree, or the pool the API
        # describes is not the account that was read.
        if r.get('price') and abs(st['price'] / r['price'] - 1) > 0.05:
            r['liquidity'] = None
            r['note'] = f'chain price {st["price"]:.6g} disagrees with API price {r["price"]:.6g}'
    return records


# --- Orca --------------------------------------------------------------------

def from_orca(p):
    a, b = p['tokenA'], p['tokenB']
    stats = (p.get('stats') or {}).get('24h') or {}
    nominal = int(p.get('feeRate') or 0) / 1e6
    fee, src = effective_fee(nominal, _f(stats.get('fees')), _f(stats.get('volume')))
    da, db_ = int(a.get('decimals', 9)), int(b.get('decimals', 6))
    return {
        'dex': 'orca', 'kind': 'clmm', 'address': p.get('address'),
        'pair': f"{a.get('symbol', '?')}/{b.get('symbol', '?')}",
        'token_a': _token(a.get('address'), a.get('symbol'), a.get('name'), da),
        'token_b': _token(b.get('address'), b.get('symbol'), b.get('name'), db_),
        'price': _f(p.get('price')), 'fee': fee, 'fee_source': src,
        'tvl_usd': _f(p.get('tvlUsdc')), 'volume_24h_usd': _f(stats.get('volume')),
        'fees_24h_usd': _f(stats.get('fees')),
        'liquidity': _f(p.get('liquidity')) / math.sqrt(10 ** da * 10 ** db_) or None,
        'adaptive_fee': bool(p.get('adaptiveFeeEnabled')),
        'tick_spacing': p.get('tickSpacing'),
    }


def orca(limit=50):
    d = _get(f'{ORCA}/pools?limit={min(limit, 100)}&sortBy=volume24h')
    return [from_orca(p) for p in d.get('data', []) if p.get('tokenA')]


def orca_pool(address):
    d = _get(f'{ORCA}/pools/{address}')
    p = d.get('data') or d
    if not isinstance(p, dict) or not p.get('tokenA'):
        return None
    return from_orca(p)


# --- Raydium CLMM ------------------------------------------------------------

def from_raydium(p):
    a, b = p['mintA'], p['mintB']
    day = p.get('day') or {}
    fee, src = effective_fee(_f(p.get('feeRate')), _f(day.get('volumeFee')), _f(day.get('volume')))
    return {
        'dex': 'raydium-clmm', 'kind': 'clmm', 'address': p['id'],
        'pair': f"{a.get('symbol', '?')}/{b.get('symbol', '?')}".replace('WSOL', 'SOL'),
        'token_a': _token(a.get('address'), (a.get('symbol') or '?').replace('WSOL', 'SOL'),
                          a.get('name'), a.get('decimals', 9)),
        'token_b': _token(b.get('address'), (b.get('symbol') or '?').replace('WSOL', 'SOL'),
                          b.get('name'), b.get('decimals', 6)),
        'price': _f(p.get('price')), 'fee': fee, 'fee_source': src,
        'tvl_usd': _f(p.get('tvl')), 'volume_24h_usd': _f(day.get('volume')),
        'fees_24h_usd': _f(day.get('volumeFee')),
        'liquidity': None,                       # read from chain below
        'adaptive_fee': bool(p.get('hasDynamicFee')),
        'tick_spacing': (p.get('config') or {}).get('tickSpacing'),
    }


def raydium(limit=50):
    d = _get(f'{RAYDIUM}/pools/info/list?poolType=concentrated&poolSortField=volume24h'
             f'&sortType=desc&pageSize={min(limit, 100)}&page=1')
    rows = ((d.get('data') or {}).get('data')) or []
    recs = [from_raydium(p) for p in rows if p.get('mintA')]
    return attach_chain_state(recs)


def raydium_pool(address):
    d = _get(f'{RAYDIUM}/pools/info/ids?ids={address}')
    rows = [p for p in (d.get('data') or []) if p and p.get('mintA')]
    return attach_chain_state([from_raydium(rows[0])])[0] if rows else None


# --- Byreal ------------------------------------------------------------------

def from_byreal(p):
    a, b = p['mintA']['mintInfo'], p['mintB']['mintInfo']
    nominal = _f((p.get('feeRate') or {}).get('fixFeeRate')) / 1e6
    fee, src = effective_fee(nominal, _f(p.get('feeUsd24h')), _f(p.get('volumeUsd24h')))
    # The API's price is in display order, which may be B/A. Recompute A->B
    # from the two token prices, which are unambiguous.
    pa, pb = _f(p['mintA'].get('price')), _f(p['mintB'].get('price'))
    price = pa / pb if pa > 0 and pb > 0 else _f(p.get('price'))
    return {
        'dex': 'byreal', 'kind': 'clmm', 'address': p['poolAddress'],
        'pair': f"{a.get('symbol', '?')}/{b.get('symbol', '?')}",
        'token_a': _token(a.get('address'), a.get('symbol'), a.get('name'), a.get('decimals', 9)),
        'token_b': _token(b.get('address'), b.get('symbol'), b.get('name'), b.get('decimals', 6)),
        'price': price, 'fee': fee, 'fee_source': src,
        'tvl_usd': _f(p.get('tvl')), 'volume_24h_usd': _f(p.get('volumeUsd24h')),
        'fees_24h_usd': _f(p.get('feeUsd24h')),
        'liquidity': None,
        'adaptive_fee': nominal < 1e-5 or bool(p.get('decayFeeFlag')),
        'tick_spacing': None,
    }


def byreal(limit=50):
    d = _get(f'{BYREAL}/pools/info/list?page=1&pageSize={min(limit, 200)}'
             '&sortField=volumeUsd24h&sortType=desc')
    rows = (((d.get('result') or {}).get('data') or {}).get('records')) or []
    recs = [from_byreal(p) for p in rows if (p.get('mintA') or {}).get('mintInfo')]
    return attach_chain_state(recs)


def byreal_pool(address):
    d = _get(f'{BYREAL}/pools/details?poolAddress={address}')
    p = (d.get('result') or {}).get('data')
    if not p or not (p.get('mintA') or {}).get('mintInfo'):
        return None
    rec = from_byreal(p)
    st = decode_clmm_state(base64.b64decode(p.get('accountBase64') or ''))
    if st:
        rec['liquidity'] = st['liquidity']; rec['tick_spacing'] = st['tick_spacing']
    return rec


# --- PancakeSwap V3 on Solana -------------------------------------------------
# No pool API of its own. GeckoTerminal lists the pools with TVL and volume;
# the pool account gives liquidity, decimals and the mints in program order;
# the AmmConfig account gives the fee. Gecko's base/quote order is its own,
# so symbols are matched to the on-chain mints, never taken by position.

def _gecko_dex_pools(dex_id, pages):
    rows = []
    for page in range(1, pages + 1):
        d = _get(f'{GECKO}/dexes/{dex_id}/pools?sort=h24_volume_usd_desc&page={page}',
                 accept='application/json;version=20230203')
        data = d.get('data') if isinstance(d, dict) else None
        if not data:
            break
        rows.extend(data)
        if len(data) < 20:
            break
    return rows


def pancakeswap(limit=50):
    rows = _gecko_dex_pools('pancakeswap-v3-solana', pages=max(1, math.ceil(limit / 20)))
    recs = []
    for r in rows[:limit]:
        at = r.get('attributes') or {}
        rel = r.get('relationships') or {}
        names = [x.strip() for x in (at.get('name') or '?/?').split('/')]
        base = ((rel.get('base_token') or {}).get('data') or {}).get('id', '')[len('solana_'):]
        quote = ((rel.get('quote_token') or {}).get('data') or {}).get('id', '')[len('solana_'):]
        recs.append({
            'dex': 'pancakeswap-v3-solana', 'kind': 'clmm', 'address': at.get('address'),
            'pair': '?', 'token_a': None, 'token_b': None, 'price': None,
            'fee': 0.0, 'fee_source': 'nominal',
            'tvl_usd': _f(at.get('reserve_in_usd')),
            'volume_24h_usd': _f((at.get('volume_usd') or {}).get('h24')),
            'fees_24h_usd': 0.0, 'liquidity': None, 'adaptive_fee': False,
            'tick_spacing': None,
            '_gecko': {base: (names[0] if len(names) > 0 else '?', _f(at.get('base_token_price_usd'))),
                       quote: (names[1] if len(names) > 1 else '?', _f(at.get('quote_token_price_usd')))},
        })
    accounts = pool_accounts([r['address'] for r in recs])
    states = {r['address']: decode_clmm_state(accounts.get(r['address'], b'')) for r in recs}
    cfg_accounts = pool_accounts(sorted({st['amm_config'] for st in states.values() if st}))
    out = []
    for r in recs:
        st = states.get(r['address'])
        if not st:
            continue
        ga = r.pop('_gecko')
        sym_a, usd_a = ga.get(st['mint_a'], ('?', 0.0))
        sym_b, usd_b = ga.get(st['mint_b'], ('?', 0.0))
        cfg = decode_amm_config(cfg_accounts.get(st['amm_config'], b''))
        r.update({
            'pair': f'{sym_a}/{sym_b}',
            'token_a': _token(st['mint_a'], sym_a, sym_a, st['decimals_a']),
            'token_b': _token(st['mint_b'], sym_b, sym_b, st['decimals_b']),
            'price': st['price'], 'liquidity': st['liquidity'],
            'tick_spacing': st['tick_spacing'], 'chain_price': st['price'],
            'fee': (cfg['trade_fee_rate'] / 1e6) if cfg else 0.0,
        })
        r['fees_24h_usd'] = r['volume_24h_usd'] * r['fee']
        if r['fee'] > 0:
            out.append(r)
    return out


def pancakeswap_pool(address):
    d = _get(f'{GECKO}/pools/{address}', accept='application/json;version=20230203')
    data = d.get('data') if isinstance(d, dict) else None
    if not data:
        return None
    # Reuse the list path on a one-element list: same decoding, same checks.
    saved = _gecko_dex_pools
    try:
        globals()['_gecko_dex_pools'] = lambda dex_id, pages: [data]
        recs = pancakeswap(limit=1)
    finally:
        globals()['_gecko_dex_pools'] = saved
    return recs[0] if recs else None


# --- Meteora DLMM ------------------------------------------------------------
# The list API has everything but the liquidity around the active bin, which
# the fee model needs and which only the chain knows. dlmm_probe.mjs reads it
# for every listed pool in one pass through the SDK.

DLMM_MIN_TVL = 100_000


def _dlmm_probe(addresses, timeout=180):
    if not addresses:
        return {}
    r = subprocess.run(['node', str(ROOT / 'dlmm_probe.mjs'), *addresses],
                       capture_output=True, text=True, timeout=timeout,
                       env=dict(os.environ, LPBOT_RPC=_rpc_url()))
    text = r.stdout or ''
    i = text.find('{')
    if i < 0:
        return {}
    try:
        return json.loads(text[i:text.rindex('}') + 1])
    except Exception:
        return {}


def from_meteora_dlmm(p):
    x, y = p['token_x'], p['token_y']
    cfg = p.get('pool_config') or {}
    base = _f(cfg.get('base_fee_pct')) / 100
    vol, fees = _f((p.get('volume') or {}).get('24h')), _f((p.get('fees') or {}).get('24h'))
    # DLMM fees have a variable part that rises with volatility; the realised
    # ratio over 24h is what swaps actually paid, and it is preferred to the
    # base rate whenever it is sane.
    realised = fees / vol if vol > 0 and fees > 0 else None
    if realised and base * 0.5 <= realised <= max(base * 10, 0.05):
        fee, src = realised, 'realised'
    else:
        fee, src = base, 'nominal'
    return {
        'dex': 'meteora-dlmm', 'kind': 'dlmm', 'address': p['address'],
        'pair': f"{x.get('symbol', '?')}/{y.get('symbol', '?')}",
        'token_a': _token(x.get('address'), x.get('symbol'), x.get('name'), x.get('decimals', 9)),
        'token_b': _token(y.get('address'), y.get('symbol'), y.get('name'), y.get('decimals', 6)),
        'price': _f(p.get('current_price')), 'fee': fee, 'fee_source': src,
        'base_fee': base,
        'tvl_usd': _f(p.get('tvl')), 'volume_24h_usd': vol, 'fees_24h_usd': fees,
        'bin_step': int(cfg.get('bin_step') or 0), 'active_bin_usd': None,
        'adaptive_fee': _f(cfg.get('max_fee_pct')) > 0 or bool(_f(p.get('dynamic_fee_pct')) > base * 100 * 0.5),
        'token_usd': (_f(x.get('price')), _f(y.get('price'))),
        'blacklisted': bool(p.get('is_blacklisted')),
    }


def attach_bins(records):
    """Mean dollar liquidity per bin around the active bin, over a span of
    about +/-1% either side. One bin alone is too noisy: the active bin is
    partly consumed, and a single empty bin next to it says nothing."""
    probe = _dlmm_probe([r['address'] for r in records])
    for r in records:
        pr = probe.get(r['address']) or {}
        bins = pr.get('bins') or []
        step = r.get('bin_step') or pr.get('binStep') or 0
        if not bins or not step:
            r['note'] = pr.get('error', 'no bin data')
            continue
        usd_x, usd_y = r.get('token_usd') or (0.0, 0.0)
        span = max(1, min(len(bins) // 2, int(round(100 / step))))   # about 1%
        act = pr.get('activeBin')
        near = [b for b in bins if act is not None and abs(b['id'] - act) <= span]
        vals = [b['x'] * usd_x + b['y'] * usd_y for b in near] or \
               [b['x'] * usd_x + b['y'] * usd_y for b in bins]
        r['active_bin_usd'] = sum(vals) / len(vals) if vals else None
        r['bins_sampled'] = len(vals)
        r['bin_step'] = step
        if pr.get('baseFeePct') is not None:
            r['base_fee'] = pr['baseFeePct'] / 100
    return records


def meteora_dlmm(limit=50):
    flt = f'tvl>={DLMM_MIN_TVL} && is_blacklisted=false'
    d = _get(f'{METEORA_DLMM}/pools?page=1&page_size={min(limit, 100)}'
             f'&sort_by=volume_24h:desc&filter_by={flt.replace(" ", "%20").replace(">=", "%3E%3D").replace("&&", "%26%26")}')
    rows = d.get('data') or []
    recs = [from_meteora_dlmm(p) for p in rows if p.get('token_x')]
    return attach_bins(recs)


def meteora_dlmm_pool(address):
    d = _get(f'{METEORA_DLMM}/pools/{address}')
    if not isinstance(d, dict) or not d.get('token_x'):
        return None
    return attach_bins([from_meteora_dlmm(d)])[0]


# A DLMM position spans at most this many bins. On a 4 bp pool that is about
# +/-32%; on a 1 bp pool about +/-7%. A band that does not fit cannot be opened.
DLMM_POSITION_MAX_BINS = 1400


def feasible_bands(rec, bands):
    """The rungs of the ladder a position on this pool can actually hold."""
    if rec.get('kind') != 'dlmm' or not rec.get('bin_step'):
        return tuple(bands)
    per_bin = math.log(1 + rec['bin_step'] / 1e4)
    ok = tuple(k for k in bands if 2 * math.log(k) / per_bin + 2 <= DLMM_POSITION_MAX_BINS)
    return ok or tuple(bands[:1])


# --- Jupiter: prices and token facts, no pools ---------------------------------

def jupiter_prices(mints):
    """USD price per mint from Jupiter's price API. No key, no rate-limit
    trouble at this volume, and it prices by mint so it cannot confuse the
    two sides of a pair."""
    out = {}
    mints = [m for m in dict.fromkeys(mints) if m]
    for i in range(0, len(mints), 50):
        chunk = mints[i:i + 50]
        try:
            d = _get(f'{JUPITER}/price/v3?ids={",".join(chunk)}')
        except Exception:
            continue
        for m in chunk:
            p = _f((d.get(m) or {}).get('usdPrice'))
            if p > 0:
                out[m] = p
    return out


def jupiter_token(mint):
    """What Jupiter knows about a token: name, verification, organic score,
    holder count, audit flags. What the token screen decides on, so it rests on
    more than a ticker."""
    try:
        d = _get(f'{JUPITER}/tokens/v2/search?query={mint}')
    except Exception:
        return None
    for t in d if isinstance(d, list) else []:
        if t.get('id') == mint:
            audit = t.get('audit') or {}
            return {'name': t.get('name'), 'symbol': t.get('symbol'),
                    'verified': bool(t.get('isVerified')),
                    'organic_score': t.get('organicScore'),
                    'organic_score_label': t.get('organicScoreLabel'),
                    'holders': t.get('holderCount'), 'tags': t.get('tags'),
                    'market_cap_usd': t.get('mcap'),
                    'first_pool_at': (t.get('firstPool') or {}).get('createdAt'),
                    'mint_authority_disabled': audit.get('mintAuthorityDisabled'),
                    'freeze_authority_disabled': audit.get('freezeAuthorityDisabled'),
                    'top_holders_pct': audit.get('topHoldersPercentage')}
    return None


ADAPTERS = {'orca': orca, 'raydium-clmm': raydium, 'byreal': byreal,
            'pancakeswap-v3-solana': pancakeswap, 'meteora-dlmm': meteora_dlmm}
SINGLE = {'orca': orca_pool, 'raydium-clmm': raydium_pool, 'byreal': byreal_pool,
          'pancakeswap-v3-solana': pancakeswap_pool, 'meteora-dlmm': meteora_dlmm_pool}


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
