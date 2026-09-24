# Swap hook: how the loop calls `swap_jupiter.mjs`

Where: `rebalancer.reopen()`, after `repoint(target)` has run (so `config` is the
new pool's) and before `chain('open', ...)`. `rebalancer.py` is not changed yet;
this file is the spec. Add `SIGNERS['jupiter'] = str(ROOT / 'swap_jupiter.mjs')`
so `chain(..., dex='jupiter')` drives it with the same env (WALLET_SECRET_PATH,
SOLANA_RPC_URL, LPBOT_SLIPPAGE_BPS, LPBOT_GAS_RESERVE_SOL).

## Gate

1. `config.ALLOW_SWAP` is true. If false, do nothing here (`board_pick` already
   limits candidates to the held pair).
2. `bal = wallet(pool)`; `C = config.CAPITAL_USD`; `q = bal['quoteUsd'] or 1`.
   `usdA = max(balanceA - reserve_if_nativeSide_A, 0) * price * q`;
   `usdB = max(balanceB - reserve_if_nativeSide_B, 0) * q`.
3. Swap when `min(usdA, usdB) < 0.4 * C` (one side is more than 10% of capital
   below 50/50; a wallet that still holds the OLD pair has one side near 0).
   Otherwise `notify('swap_skipped', reason='split within 10% of 50/50', usdA=, usdB=)` and open.

## Call

`out, err = chain('rebalance', mintA, mintB, f'{C*0.55:.2f}', f'{C*0.55:.2f}', '--execute', dex='jupiter')`
`mintA`, `mintB` are the target pool's mints (`target['token_a']['address']`,
`target['token_b']['address']`; store them at `repoint` or read them from the
signer's `pool` command). 0.55 = the 0.50 side cap plus a cushion so slippage
and fees do not make `deposit_caps` the binding side. One call per reopen, never two.

## Outcomes

- `out['noop']` -> `notify('swap_skipped', reason='already at target', **balances)`; open.
- `out['sent']` and no `err` -> `db.event('SWAP', ...)`; `notify('SWAP', sold=, bought=,
  signature=, priceImpactPct=, routePlan=)`; then RE-READ: `bal = wallet(pool)`,
  `cap_a, cap_b = deposit_caps(bal)`; open with the new caps.
- `err`, or `out.get('partial')` -> a failed swap opens nothing and counts as a failure:
  `state['failures'] += 1; save(state); db.event('swap_failed', err);
  notify('swap_failed', reason=err, failures=state['failures'], signature=(out or {}).get('signature'))`;
  `halt()` at `MAX_CONSECUTIVE_FAILURES`; `return False`. The next pass re-reads the
  wallet, so a partial send (tokens moved, confirm failed) is seen, not repeated.

Signer refusals arrive as `swap_failed`: HALT present; price impact > 1%
(`LPBOT_MAX_IMPACT`, ratio 0.01); Jupiter simulation error; quote older than 20 s
at send; wallet holds less than the sell amount after the gas reserve.
