"""Hard invariants, checked at the moment money is about to move.

Every check here is the shape of a real loss: a band that does not contain
the price, a cap above the capital, a move to a DEX without an armed signer, an
address that is not one. They raise `Refused`, the loop reports it and opens
nothing. None of them is a substitute for the tests; they are the last line
when the tests were wrong about the world.
"""
import math
import pathlib
import re

import chains

B58 = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
ARG = re.compile(r'^[A-Za-z0-9._:/\-]{1,128}$')


class Refused(Exception):
    pass


def is_address(s):
    # fullmatch, not match: `$` in a Python regex accepts a trailing newline,
    # and an address with a newline in it is not an address.
    return isinstance(s, str) and bool(B58.fullmatch(s))


def signer_args(args):
    """Arguments handed to a signer: printable, short, no whitespace, and no
    option except the literal --execute the loop itself adds. A value from an
    API that starts with '-' (a mint, an amount) would otherwise be parsed as
    a flag such as --pool (security review, 2026-09-26)."""
    for a in args:
        if not isinstance(a, str) or not ARG.fullmatch(a):
            raise Refused(f'bad signer argument {a!r}')
        if a.startswith('-') and a != '--execute':
            raise Refused(f'signer argument {a!r} looks like an option')
    return True


def inside(path, root):
    """A signer script must live inside the bot's own directory."""
    p, r = pathlib.Path(path).resolve(), pathlib.Path(root).resolve()
    if r not in p.parents:
        raise Refused(f'{path} is outside {root}')
    return True


def _finite_pos(x, name):
    if not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
        raise Refused(f'{name} must be a positive finite number, got {x!r}')


def open_request(*, pool, dex, price, lower, upper, cap_a, cap_b, capital_usd, max_usd,
                 quote_usd, execute_dexes, signers, model_price=None, max_drift=0.03, chain='solana',
                 ui_price=None):
    """Everything an open must satisfy before a signer is spawned. `price` is
    the LIVE price the wallet read from the chain; `model_price` the one the
    band was centred on, from the DEX's API. They must agree, or the band is
    centred on a stale number. `pool` must be an address on `chain`. The
    caps are UI amounts (Token-2022 scaled mints): their value is taken at
    `ui_price` when given, the pool-native `price` otherwise."""
    if not chains.is_address(chain, pool):
        raise Refused(f'pool {pool!r} is not an address')
    if model_price is not None:
        _finite_pos(model_price, 'model_price')
        _finite_pos(price, 'price')
        if abs(model_price / price - 1.0) > max_drift:
            raise Refused(f'model price {model_price} is {abs(model_price / price - 1) * 100:.1f}% '
                          f'from the live price {price}; the band would be centred on a stale number')
    if dex not in signers or dex not in execute_dexes:
        raise Refused(f'{dex} has no armed signer')
    for x, n in ((price, 'price'), (lower, 'lower'), (upper, 'upper'), (quote_usd, 'quote_usd')):
        _finite_pos(x, n)
    if not lower < price < upper:
        raise Refused(f'price {price} is not inside the band {lower}..{upper}')
    if upper / lower > 4.0:
        raise Refused(f'band {lower}..{upper} is wider than any rung of the ladder')
    for x, n in ((cap_a, 'cap_a'), (cap_b, 'cap_b')):
        if not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0:
            raise Refused(f'{n} must be a non-negative finite number, got {x!r}')
    px = price
    if ui_price is not None:
        _finite_pos(ui_price, 'ui_price')
        px = ui_price
    value_usd = (cap_a * px + cap_b) * quote_usd
    # Each side is capped at side_cap_fraction (< 1) of the capital, so the two
    # caps together cannot exceed 2x capital, and never the hard ceiling.
    if value_usd > 2.0 * capital_usd + 1e-6:
        raise Refused(f'caps worth ${value_usd:.2f} exceed twice the capital ${capital_usd:.2f}')
    if min(cap_a * px, cap_b) * quote_usd > max_usd:
        raise Refused(f'a side worth more than the ${max_usd} ceiling')
    return True


def migration_target(target, *, execute_dexes, signers, known):
    """A board row or operator target the bot may actually move to."""
    if not isinstance(target, dict):
        raise Refused('target is not a record')
    dex, addr = target.get('dex'), target.get('address')
    if dex not in known:
        raise Refused(f'unknown dex {dex!r}')
    if dex not in signers or dex not in execute_dexes:
        raise Refused(f'{dex} has no armed signer')
    if dex == 'jupiter':
        raise Refused('jupiter is a swap route, not a pool')
    if not is_address(addr):
        raise Refused(f'pool {addr!r} is not an address')
    for side in ('token_a', 'token_b'):
        tok = target.get(side) or {}
        if not is_address(tok.get('address')):
            raise Refused(f'{side} has no mint address')
        if not isinstance(tok.get('symbol'), str) or not (0 < len(tok['symbol']) <= 32):
            raise Refused(f'{side} has no symbol')
    if target['token_a']['address'] == target['token_b']['address']:
        raise Refused('both tokens are the same mint')
    return True


# A fee read the chain cannot have produced. On 2026-09-27 a Raydium status
# read the pool, the position and the tick arrays at different slots while
# the price crossed a boundary tick, and reported $6,237 of fees on a $230
# position. The gas split then booked $2,830 as gas and $3,408 as reinvested.
# The signers now read atomically; this check stands behind them for every
# venue. Bounds, generous on purpose. SOL/USDC earns about 0.03% of the
# position an hour; on 2026-10-05 a DJT/USDC burst ($510k in 15 min through a
# $714k pool, at the band's edge) earned 0.28% in 2 minutes, 7.6% an hour,
# and the old 1% bound rejected real fees for the whole burst:
FEE_MAX_FRACTION = 0.10         # accrued fees above 10% of the position
FEE_MAX_RISE_PER_HOUR = 0.10    # a rise of more than 10% of the position an hour
FEE_RISE_SLACK = 0.002          # plus 0.2% of the position, for short gaps


def _nonneg(x):
    return x is None or (isinstance(x, (int, float)) and not isinstance(x, bool)
                         and math.isfinite(x) and x >= 0)


def fee_read_problem(status, prev_usd=None, hours=None):
    """None when the fee figures of a status read are possible, else why not.

    `status` is a signer's status answer; `prev_usd` the accrued fees in
    dollars at the last snapshot of the same position, `hours` the time since.
    A fall is always possible (a harvest resets the counter); a rise or a
    level no position of this size can earn is not."""
    a, b, usd = status.get('feesAccruedA'), status.get('feesAccruedB'), status.get('feesAccrued_USD')
    for x, n in ((a, 'feesAccruedA'), (b, 'feesAccruedB'), (usd, 'feesAccrued_USD')):
        if not _nonneg(x):
            return f'{n} is {x!r}, not a non-negative finite number'
    pos = status.get('positionUsd')
    if not isinstance(pos, (int, float)) or not math.isfinite(pos) or pos <= 0 or usd is None:
        return None
    if usd > FEE_MAX_FRACTION * pos:
        return f'fees ${usd:.4f} exceed {FEE_MAX_FRACTION:.0%} of the ${pos:.2f} position'
    if prev_usd is not None and hours is not None and hours >= 0:
        rise = usd - float(prev_usd)
        allowed = pos * (FEE_MAX_RISE_PER_HOUR * hours + FEE_RISE_SLACK)
        if rise > allowed:
            return (f'fees rose ${rise:.4f} in {hours:.2f}h on a ${pos:.2f} position '
                    f'(at most ${allowed:.4f} is possible)')
    return None
