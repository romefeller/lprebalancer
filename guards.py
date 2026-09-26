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
                 quote_usd, execute_dexes, signers, model_price=None, max_drift=0.03):
    """Everything an open must satisfy before a signer is spawned. `price` is
    the LIVE price the wallet read from the chain; `model_price` the one the
    band was centred on, from the DEX's API. They must agree, or the band is
    centred on a stale number."""
    if not is_address(pool):
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
    value_usd = (cap_a * price + cap_b) * quote_usd
    # Each side is capped at side_cap_fraction (< 1) of the capital, so the two
    # caps together cannot exceed 2x capital, and never the hard ceiling.
    if value_usd > 2.0 * capital_usd + 1e-6:
        raise Refused(f'caps worth ${value_usd:.2f} exceed twice the capital ${capital_usd:.2f}')
    if min(cap_a * price, cap_b) * quote_usd > max_usd:
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
