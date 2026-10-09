"""Orca whirlpools: the API record."""

import math

from venues import api as venue_api
from venues.api import _f, _rewards, _token, effective_fee


ORCA = 'https://api.orca.so/v2/solana'


def orca_rewards(p):
    """Orca: stats.24h.rewards is the pool's reward value paid in the last day,
    in dollars; rewards[] names the mints, `active` marks live programs."""
    live = [r for r in (p.get('rewards') or []) if r.get('active') and _f(r.get('emissionsPerSecond')) > 0]
    usd = _f(((p.get('stats') or {}).get('24h') or {}).get('rewards')) if live else 0.0
    return _rewards(usd, [r.get('mint') for r in live])


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
        **orca_rewards(p),
    }


def orca(limit=50):
    d = venue_api._get(f'{ORCA}/pools?limit={min(limit, 100)}&sortBy=volume24h')
    return [from_orca(p) for p in d.get('data', []) if p.get('tokenA')]


def orca_pool(address):
    d = venue_api._get(f'{ORCA}/pools/{address}')
    p = d.get('data') or d
    if not isinstance(p, dict) or not p.get('tokenA'):
        return None
    return from_orca(p)
