"""PancakeSwap v3 on Solana: GeckoTerminal for volume, the chain for the rest."""

import math

from venues import api as venue_api
from venues import solana_state
from venues.api import _f, _token
from venues.solana_state import attach_chain_rewards, decode_amm_config, decode_clmm_state


GECKO = 'https://api.geckoterminal.com/api/v2/networks/solana'


# --- PancakeSwap V3 on Solana -------------------------------------------------
# No pool API of its own. GeckoTerminal lists the pools with TVL and volume;
# the pool account gives liquidity, decimals and the mints in program order;
# the AmmConfig account gives the fee. Gecko's base/quote order is its own,
# so symbols are matched to the on-chain mints, never taken by position.

def _gecko_dex_pools(dex_id, pages):
    rows = []
    for page in range(1, pages + 1):
        d = venue_api._get(f'{GECKO}/dexes/{dex_id}/pools?sort=h24_volume_usd_desc&page={page}',
                           accept='application/json;version=20230203')
        data = d.get('data') if isinstance(d, dict) else None
        if not data:
            break
        rows.extend(data)
        if len(data) < 20:
            break
    return rows


def pancakeswap(limit=50, rows=None):
    """The PancakeSwap pools GeckoTerminal lists, or `rows` (GeckoTerminal
    pool objects) when the caller has them already."""
    if rows is None:
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
            'tick_spacing': None, 'reward_usd_day': 0.0, 'reward_mints': [],
            '_gecko': {base: (names[0] if len(names) > 0 else '?', _f(at.get('base_token_price_usd'))),
                       quote: (names[1] if len(names) > 1 else '?', _f(at.get('quote_token_price_usd')))},
        })
    accounts = solana_state.pool_accounts([r['address'] for r in recs])
    states = {r['address']: decode_clmm_state(accounts.get(r['address'], b'')) for r in recs}
    cfg_accounts = solana_state.pool_accounts(sorted({st['amm_config'] for st in states.values() if st}))
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
    try:
        attach_chain_rewards(out, accounts)
    except Exception:
        pass
    return out


def pancakeswap_pool(address):
    d = venue_api._get(f'{GECKO}/pools/{address}', accept='application/json;version=20230203')
    data = d.get('data') if isinstance(d, dict) else None
    if not data:
        return None
    # The list path on a one-element list: same decoding, same checks.
    recs = pancakeswap(limit=1, rows=[data])
    return recs[0] if recs else None
