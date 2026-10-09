"""Meteora DLMM: the API record, bin liquidity from probe.mjs."""

import json
import math
import os
import pathlib
import subprocess

from venues import api as venue_api
from venues.api import _f, _rewards, _token
from venues.solana_state import _rpc_url


METEORA_DLMM = 'https://dlmm.datapi.meteora.ag'


def meteora_rewards(p):
    """Meteora DLMM: farm_apr in percent of TVL while has_farm; the reward mints
    are reward_mint_x/y, the null mint when unused."""
    apr = _f(p.get('farm_apr')) if p.get('has_farm') else 0.0
    return _rewards(apr / 100 / 365 * _f(p.get('tvl')) if apr > 0 else 0.0,
                    [p.get('reward_mint_x'), p.get('reward_mint_y')] if apr > 0 else [])


# --- Meteora DLMM ------------------------------------------------------------
# The list API has everything but the liquidity around the active bin, which
# the fee model needs and which only the chain knows. venues/meteora_dlmm/probe.mjs reads it
# for every listed pool in one pass through the SDK.

PROBE = pathlib.Path(__file__).parent / 'probe.mjs'    # the bin reader, through the SDK
DLMM_MIN_TVL = 100_000


def _dlmm_probe(addresses, timeout=180):
    if not addresses:
        return {}
    r = subprocess.run(['node', str(PROBE), *addresses],
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
        **meteora_rewards(p),
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
    d = venue_api._get(f'{METEORA_DLMM}/pools?page=1&page_size={min(limit, 100)}'
                       f'&sort_by=volume_24h:desc&filter_by={flt.replace(" ", "%20").replace(">=", "%3E%3D").replace("&&", "%26%26")}')
    rows = d.get('data') or []
    recs = [from_meteora_dlmm(p) for p in rows if p.get('token_x')]
    return attach_bins(recs)


def meteora_dlmm_pool(address):
    d = venue_api._get(f'{METEORA_DLMM}/pools/{address}')
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
