"""HTTP JSON reads of the venue APIs, and the record helpers every venue
shares: numbers, tokens, the fee actually charged, reward fields."""

import json
import math
import subprocess

import jupgate


UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')


def _get(url, accept='application/json', timeout=40, secret_headers=()):
    """GET `url` as JSON. `secret_headers` ('name: value' lines, an API key)
    go to curl on stdin, never in its argv, which every `ps` shows."""
    if 'jup.ag' in url:
        jupgate.wait_turn()                       # one Jupiter slot across every process (jupgate.py)
    r = subprocess.run(['curl', '-s', '--max-time', str(timeout),
                        '-H', f'accept: {accept}', '-H', f'user-agent: {UA}', '-H', '@-', url],
                       input=''.join(f'{h}\n' for h in secret_headers), capture_output=True, text=True)
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


# --- Orca --------------------------------------------------------------------

# --- rewards ------------------------------------------------------------------
#
# Every record carries `reward_usd_day`: what the pool's reward programs pay
# ALL its liquidity per day, in dollars, and `reward_mints`, the tokens they pay
# in. The board prices a position's share of it exactly like fees (engine), and
# the bot splits harvested rewards like income (rebalancer.distribute_rewards).
# Each API reports rewards its own way; an ended or zero program counts as none.
NULL_MINT = '11111111111111111111111111111111'


def _rewards(usd_day, mints):
    mints = [m for m in dict.fromkeys(mints or []) if m and m != NULL_MINT]
    return {'reward_usd_day': max(float(usd_day or 0.0), 0.0), 'reward_mints': mints}
