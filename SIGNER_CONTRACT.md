# Signer contract

Every DEX the bot can open on has one signer script, `signer_<dex>.mjs`, that
the loop drives through `rebalancer.chain()`. The loop cannot tell which DEX it
is on, so every signer speaks the same language. `signer2.mjs` (Orca) and
`signer_dlmm.mjs` (Meteora DLMM) are the references; the DLMM one is the more
recent and the closer model.

## Environment

| variable | meaning |
|---|---|
| `WALLET_SECRET_PATH` | path to the keypair (JSON array or base58). Read inside the process, handed to the SDK, never printed or returned. |
| `SOLANA_RPC_URL` / `LPBOT_RPC` | RPC endpoint. Fall back to `https://api.mainnet-beta.solana.com`, then `https://solana-rpc.publicnode.com`. |
| `LPBOT_POOL` | the pool the loop is on. Every command that needs a pool takes it from `--pool <addr>`, else from here. |
| `LPBOT_MAX_USD` | refuse an open whose approximate value exceeds this. Default 260. |
| `LPBOT_SLIPPAGE_BPS` | default 100. |
| `LPBOT_GAS_RESERVE_SOL` | refuse an open when native SOL is below this. Default 0.02. |

A file named `HALT` in the script's directory means: refuse to build or send
anything. Check it before every connection.

## Commands

```
node signer_<dex>.mjs balance [pool]
node signer_<dex>.mjs positions
node signer_<dex>.mjs status [position]
node signer_<dex>.mjs pool [pool]
node signer_<dex>.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
node signer_<dex>.mjs harvest <position> [--execute]
node signer_<dex>.mjs close <position> [--execute]
```

Without `--execute`, `open`, `harvest` and `close` BUILD the real instructions
(and simulate them when the SDK does), print the report with `"sent": false`
and a `DRY RUN` line, and send nothing. A dry run that returns early proves
nothing.

Errors go to stderr as `ERROR: <message>` with exit code 1. Anything the loop
must parse goes to stdout as one JSON object. The loop decodes stdout separately
from stderr and treats a nonzero exit, `error`, or `partial` as a failure.
Never print multiple result objects. After a submission may have occurred,
the signer must not retry the whole operation on another RPC endpoint.

## Output fields the loop reads

`balance`:
```
owner, sol, pool, dex, tokenA, tokenB (symbols), price (B per A, human units),
quoteUsd (USD per unit of token B, 1 for a stablecoin, null if unknown),
balanceA, balanceB (wallet balances in human units; native SOL counts as its token),
nativeSide ('A' | 'B' | null), walletUsd
```

`status` with a position:
```
positionMint (the ledger id of the position; for a multi-account position the
  first account), whirlpool AND pool (the pool address, both keys), dex, pair,
tokenA, tokenB, decimalsA, decimalsB, quoteUsd, quoteUsdSource,
liquidity (string; any monotone measure of size is fine),
lowerPrice, upperPrice, price, inRange (bool),
closeEstA, closeEstB (what a close would return now, human units),
positionUsd, feesAccruedA, feesAccruedB, feesAccrued_quote, feesAccrued_USD,
rentSol, rentUsd (lamports held by the position accounts, their NFT token
  accounts, and refundable Token-2022 NFT mints, counted once per address;
  the loop adds rentUsd to the principal mark, so a wide
  DLMM position does not read as a loss the size of its 0.2 SOL of rent)
```
Equity is wallet value + position principal + refundable rent + uncollected
fees. `positionUsd` excludes both rent and fees; they are separate fields.
`status` with no position: exactly `{"positions": 0, "positionMint": null, "pool": <pool>}`.
The loop must be able to tell "read worked, nothing there" from "read failed".

`open`: `positionMint, signature (when sent), depositEstA, depositEstB,
depositUsd, approxUsd, lowerPrice, upperPrice, tokenA, tokenB, pair, pool, dex,
sent`. Refuse before sending when: HALT present; SOL below the gas reserve;
approxUsd over LPBOT_MAX_USD; the price is outside [lower, upper].

`harvest`: `{harvested: <position>, signature}`. `close`: `{closed: <position>, signature}`.

## Sizing

`maxA`, `maxB` are CEILINGS in human units. Deposit as much as the band allows
under both caps: compute the liquidity each cap alone would fund at the
current price and take the smaller. `signer2.mjs:depositQuote` is the formula
for tick pools.

## Retries and partial sends

Retry a READ across endpoints on a rate limit (429). NEVER retry a whole
`open`/`harvest`/`close` after a transaction has been sent: a retried open is a
second position. After the first send, catch errors, print the report with
`sent: true`, `partial: true`, the signatures so far and the error, and exit 1.
The loop re-reads the chain and believes what it finds.

## Prices are B per A

Every price the signer speaks is token B per token A in human units, where A
and B are the pool's own token order (mint 0 and mint 1 on a Raydium-layout
pool; tokenX and tokenY on DLMM). GeckoTerminal and DEX front ends may show the
inverse; the loop and the engine never do.
