"""Jupiter: USD prices by mint and token facts. No pools."""

import json
import os
import pathlib
import time

from venues import api as venue_api
from venues.api import _f


VENUE = json.loads((pathlib.Path(__file__).parent / 'venue.json').read_text())
# The keyed endpoint when the service environment holds a key, else the free one.
JUPITER = VENUE['keyed_api'] if os.environ.get(VENUE['key_env']) else VENUE['free_api']


def get(url, timeout=40):
    """venue_api._get for a Jupiter URL, with the key header when there is a key."""
    key = os.environ.get(VENUE['key_env'])
    return venue_api._get(url, timeout=timeout, secret_headers=(f"{VENUE['key_header']}: {key}",) if key else ())


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
            d = get(f'{JUPITER}/price/v3?ids={",".join(chunk)}')
        except Exception:
            continue
        for m in chunk:
            p = _f((d.get(m) or {}).get('usdPrice'))
            if p > 0:
                out[m] = p
    return out


_TOKEN_FACTS = {}                   # mint -> (fetched_at, facts): token facts change slowly


TOKEN_FACTS_TTL_S = 6 * 3600


TOKEN_FACTS_MAX = 512               # memory-lean: the oldest entries leave first


def jupiter_token(mint, now=None):
    """What Jupiter knows about a token: name, verification, organic score,
    holder count, audit flags. What the token screen decides on, so it rests on
    more than a ticker. A found token is cached TOKEN_FACTS_TTL_S (the sweep
    asked every ten minutes for the same mints, 2026-10-01); a miss is not."""
    t0 = time.time() if now is None else now
    hit = _TOKEN_FACTS.get(mint)
    if hit and t0 - hit[0] < TOKEN_FACTS_TTL_S:
        return hit[1]
    facts = _jupiter_token(mint)
    if facts is not None:
        _TOKEN_FACTS[mint] = (t0, facts)
        while len(_TOKEN_FACTS) > TOKEN_FACTS_MAX:
            _TOKEN_FACTS.pop(min(_TOKEN_FACTS, key=lambda k: _TOKEN_FACTS[k][0]))
    return facts


def _jupiter_token(mint):
    """One token-search request; the facts, or None."""
    try:
        d = get(f'{JUPITER}/tokens/v2/search?query={mint}')
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
