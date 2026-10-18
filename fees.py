"""The fee split, as a pure function.

The owner's rule (2026-09-26):

  * Fees in the payout token go to the profit wallet, on every harvest. On
    SOL/USDC that is USDC, so a payout needs no swap.
  * Every other fee is reinvested: it raises the sizing base, and the next
    open deposits it.
  * When the LP wallet's native SOL is under the gas reserve, native-SOL fees
    refill it, up to the reserve and no further; any SOL beyond that is
    reinvested. While gas is low, the payout-token fees of that harvest are
    reinvested instead of paid.

Nothing here is specific to a pair: the payout mint and the pool's two mints
arrive as arguments, and the native mint is Solana's, not a choice.
"""
import chains

NATIVE_MINT = chains.SOLANA['native_mint']


def split(fees, payout_mint, sol_before, gas_reserve, share=None):
    """Where each harvested amount goes.

    `fees` is a list of (mint, symbol, amount, usd_per_unit). `sol_before` is
    the LP wallet's native SOL before this harvest landed. With `share` (0..1),
    the payout is that share of the harvest's dollar value, taken from the
    payout-token fees only (never more than they are; no swap), and the rest
    of them is reinvested (the payout governor, lp/governor.py). Returns a list of dicts
    {mint, symbol, amount, usd, kind} with kind 'paid', 'reinvested' or 'gas';
    amounts of zero are dropped."""
    if share is not None and not 0 <= share <= 1:
        raise ValueError(f'payout share {share} is outside 0..1')
    total_usd = sum(max(float(a or 0.0), 0.0) * px for _m, _s, a, px in fees if px is not None)
    budget = share * total_usd if share is not None else None
    gas_low = sol_before is not None and sol_before < gas_reserve
    need = max(gas_reserve - sol_before, 0.0) if gas_low else 0.0
    out = []

    def add(mint, sym, amt, px, kind):
        # the one place a zero amount is dropped
        if amt > 0:
            out.append({'mint': mint, 'symbol': sym, 'amount': amt,
                        'usd': (amt * px) if px is not None else None, 'kind': kind})

    for mint, sym, amt, px in fees:
        amt = float(amt or 0.0)
        if amt < 0:
            continue                    # a negative fee is no fee, and must not raise the gas need
        if mint == NATIVE_MINT:
            g = min(amt, need)
            need -= g
            add(mint, sym, g, px, 'gas')
            amt -= g
        if mint != payout_mint or gas_low:
            add(mint, sym, amt, px, 'reinvested')
        elif budget is None:
            add(mint, sym, amt, px, 'paid')
        else:
            pay = min(amt, budget / px) if px else 0.0
            budget -= pay * px if px else 0.0
            add(mint, sym, pay, px, 'paid')
            add(mint, sym, amt - pay, px, 'reinvested')
    return out


def fee_share(parts, bps):
    """Split each 'paid' part of `parts` (split()'s output) into what the
    profit wallet gets and, as kind 'fee', the `bps` share a non-special
    instance pays (owner, 2026-10-10: 10% of the USDC/USDT0 fees). Only the
    payout token is shared: reinvested and gas parts pass as they are. Pure."""
    if not bps:
        return list(parts)
    if not 0 < bps <= 5000:
        raise ValueError(f'fee share of {bps} bps is outside 1..5000')
    out = []
    for p in parts:
        if p['kind'] != 'paid':
            out.append(p)
            continue
        cut = p['amount'] * bps / 10_000
        px = p['usd'] / p['amount'] if p['usd'] is not None and p['amount'] else None
        for amt, kind in ((p['amount'] - cut, 'paid'), (cut, 'fee')):
            if amt > 0:
                out.append(dict(p, amount=amt, usd=amt * px if px is not None else None, kind=kind))
    return out
