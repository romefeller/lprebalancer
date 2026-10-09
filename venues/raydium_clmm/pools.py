"""Raydium CLMM: the API record, liquidity from the chain."""

from venues import api as venue_api
from venues.api import _f, _rewards, _token, effective_fee
from venues.solana_state import attach_chain_state


RAYDIUM = 'https://api-v3.raydium.io'


def raydium_rewards(p):
    """Raydium: day.rewardApr is a list of APRs in percent of TVL, one per
    program; rewardDefaultInfos names the mints and their end times."""
    import time as _t
    apr = sum(_f(x) for x in ((p.get('day') or {}).get('rewardApr') or []))
    now = _t.time()
    live = [((r.get('mint') or {}).get('address')) for r in (p.get('rewardDefaultInfos') or [])
            if _f(r.get('perSecond')) > 0 and _f(r.get('endTime')) > now]
    return _rewards(apr / 100 / 365 * _f(p.get('tvl')) if live else 0.0, live)


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
        **raydium_rewards(p),
    }


def raydium(limit=50):
    d = venue_api._get(f'{RAYDIUM}/pools/info/list?poolType=concentrated&poolSortField=volume24h'
                       f'&sortType=desc&pageSize={min(limit, 100)}&page=1')
    rows = ((d.get('data') or {}).get('data')) or []
    recs = [from_raydium(p) for p in rows if p.get('mintA')]
    return attach_chain_state(recs)


def raydium_pool(address):
    d = venue_api._get(f'{RAYDIUM}/pools/info/ids?ids={address}')
    rows = [p for p in (d.get('data') or []) if p and p.get('mintA')]
    return attach_chain_state([from_raydium(rows[0])])[0] if rows else None
