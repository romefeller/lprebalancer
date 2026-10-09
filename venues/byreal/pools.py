"""Byreal (a Raydium CLMM fork): the API record, liquidity from the chain."""

import base64

from venues import api as venue_api
from venues.api import _f, _rewards, _token, effective_fee
from venues.solana_state import attach_chain_state, decode_clmm_state


BYREAL = 'https://api2.byreal.io/byreal/api/dex/v2'


def byreal_rewards(p):
    """Byreal: rewards[] with an apr in percent of TVL when a program runs."""
    rs = p.get('rewards') or []
    apr = sum(_f(r.get('apr') or r.get('rewardApr')) for r in rs)
    mints = [(r.get('mint') or {}).get('address') if isinstance(r.get('mint'), dict) else r.get('mint')
             or r.get('rewardMint') for r in rs]
    return _rewards(apr / 100 / 365 * _f(p.get('tvl')) if apr > 0 else 0.0, mints)


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
        **byreal_rewards(p),
    }


def byreal(limit=50):
    d = venue_api._get(f'{BYREAL}/pools/info/list?page=1&pageSize={min(limit, 200)}'
                       '&sortField=volumeUsd24h&sortType=desc')
    rows = (((d.get('result') or {}).get('data') or {}).get('records')) or []
    recs = [from_byreal(p) for p in rows if (p.get('mintA') or {}).get('mintInfo')]
    return attach_chain_state(recs)


def byreal_pool(address):
    d = venue_api._get(f'{BYREAL}/pools/details?poolAddress={address}')
    p = (d.get('result') or {}).get('data')
    if not p or not (p.get('mintA') or {}).get('mintInfo'):
        return None
    rec = from_byreal(p)
    st = decode_clmm_state(base64.b64decode(p.get('accountBase64') or ''))
    if st:
        rec['liquidity'] = st['liquidity']; rec['tick_spacing'] = st['tick_spacing']
    return rec
