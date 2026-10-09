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


def split(fees, payout_mint, sol_before, gas_reserve):
    """Where each harvested amount goes.

    `fees` is a list of (mint, symbol, amount, usd_per_unit). `sol_before` is
    the LP wallet's native SOL before this harvest landed. Returns a list of
    dicts {mint, symbol, amount, usd, kind} with kind 'paid', 'reinvested' or
    'gas'; amounts of zero are dropped."""
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
        add(mint, sym, amt, px, 'paid' if mint == payout_mint and not gas_low else 'reinvested')
    return out
