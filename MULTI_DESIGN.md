# Multi-wallet, multi-pool: the design (2026-10-01, deployed 2026-10-02)

Owner's goals, in order:
1. MU/USDC first (Meteora DLMM `13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5`), same rationale as SOL/USDC.
2. One wallet, many pools, routed by the token that arrives: SOL → SOL/USDC, MU → MU/USDC,
   DJT → DJT/USDC (Orca `7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG`),
   MSFTx → MSFTx/USDC (Raydium CLMM `D6bRhQUcR9B7bPbbqgxpE17MjyUjBtr8hHQCcJoHrrv1`).
   A deposit is split ~50/50 against USDC and deployed.
3. Many wallets. Database and code carry wallet and profile everywhere. Generalise always.
4. Base chain: WETH/USDC on Aerodrome Slipstream (`0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59`), new EVM
   wallet, key on disk outside the repo (mode 0600), never printed. ETH deposit → keep gas, split, open.
   EVM profit wallet `0x2b35948898e1b4897E7FC5a70e39b213dcfd0142`. Solana profit wallet unchanged.
5. Stats per wallet and per pool, plus one shared listing with the sums. Active pools only.
6. The live bot never stops for this work. One restart at the end. Nothing that works today may break.
7. End-to-end tests, failure paths first. No slop. Security first.

## Design

**Processes.** One process per profile: systemd template `lp-bot@<profile>.service` with
`LPBOT_PROFILE=<profile>`. Profiles: `sol-usdc` (existing; wallet `sol-lp`, residual owner),
`mu-usdc`, `djt-usdc`, `msftx-usdc` (wallet `sol-lp`), `base-weth-usdc` (wallet `base-lp`). New ones are
`pool_pinned`, `regime_enabled`, `rebalance_swap`, payout to the chain's profit wallet in USDC.

**Run directory.** `config.RUN_DIR` = `run/<profile>/` holds `runtime.json`, `events.jsonl`, `HALT`,
`REBALANCE`, `REOPT`, `MIGRATE`. `ROOT/HALT` halts every profile. Every feed row carries `profile`,
`wallet_id`, `chain`, `pair` (`notify` adds them). The bridge tails `run/*/events.jsonl`
and the legacy `ROOT/events.jsonl`, one cursor per file.

**Shared tokens in one wallet (`wallets.py`).** A mint used by exactly one profile of a wallet
belongs wholly to it (native SOL belongs to the profile whose pool holds SOL; the gas reserve stays out).
A mint several profiles use (USDC) is split: profile P sees `claim[P]`; the residual owner sees
`wallet − Σ other claims`. Every chain() call with `--execute` runs under `pg_advisory_lock` of the wallet
(own connection, bounded wait) and, for each shared mint, adds `after − before` to the caller's claim
(floored at 0; an overdraw is an event). Balances are read at `confirmed` (what the signers confirm at)
and the after-read waits until its slot reaches the write's. The write is recorded as pending before it
is sent; a write whose before-read fails is refused, not sent, and every write of the wallet waits while
an earlier one is unbooked. Claims and mints of disabled profiles still count (fail closed). `wallet()` returns the sleeve view, so capital, caps, deploy-all
and the swap planner see only the profile's own money. The swap scripts honour
`LPBOT_SLEEVE={"<mint>": <max human amount>}` (`swap_jupiter.mjs`, the `swap_orca.mjs` fallback, the venue signer on Base).

**Dormant profiles.** No position and sleeve deployable < `min_deploy_usd`: no open attempt, a light poll
(every ≥300 s), one `dormant` event on entry, one `deposit_seen` on exit. A deposit of the profile's
deposit mint wakes it: swap toward 50/50 (existing `balance_wallet`), open.

**Wallet-wide housekeeping** (sweep, janitor, audits, the board scanner) runs in the wallet's residual owner
only, and only where `config.CAPS` allows. Sweep and janitor keep every mint of every profile of the
wallet, disabled ones included. Audits reconcile the wallet against the sum of its profiles.

**Signers.** SIGNER_CONTRACT.md holds for every signer. Additions:
- Token-2022 scaled UI amounts (MU, DJT, MSFTx): every human amount and every USD value the signer
  reports is in UI units (raw / 10^decimals × current multiplier, honouring `newMultiplier` once its
  timestamp passes); `price` stays pool-native (B per A, raw-decimal adjusted) AND the signer adds
  `uiPrice` (B per A in UI units) and `multiplierA/B`. Paused mints: refuse writes with `refused: mint
  paused`. Transfer hooks with a program set: refuse writes with `refused: transfer hook`.
- EVM signer (`signer_aerodrome.mjs`, chain `base`): same commands and fields (`sol` = native ETH balance;
  native ETH counts as WETH, `nativeSide`), plus `rebalance <mintA> <mintB> <usdA> <usdB> [--execute]` with
  the `swap_jupiter.mjs` output shape, plus `send <token> <amount> <to> [--execute]` and
  `balance <token>` in the `payout.mjs` shape. Env: `WALLET_SECRET_PATH` (key file path), `LPBOT_RPC`,
  `LPBOT_POOL`, `LPBOT_MAX_USD`, `LPBOT_SLIPPAGE_BPS`, `LPBOT_GAS_RESERVE_NATIVE`, `LPBOT_SLEEVE`,
  `LPBOT_EVM_PROFIT_WALLET_PIN` (send refuses any other recipient). Every signer refuses writes on
  `HALT` in its directory and on `LPBOT_RUN_DIR/HALT` (`halt_guard.mjs`).
- `dexes.pool('aerodrome-slipstream', addr)` returns the usual record shape.
- Routing: `SIGNERS['aerodrome-slipstream']`; on Base the swap goes to the venue signer instead of
  Jupiter, and payout calls go to the venue signer instead of `payout.mjs`.

**Stats.** Every report function takes `profile=None, wallet_id=None` filters; `None` = all
enabled. Per-profile money: harvests/snapshots/band_profile through `positions.config_name`; payouts by
`config_name`; flows by `profile`/`wallet_id` (pre-020 NULL rows belong to `sol-usdc`/`sol-lp`; the deploy
backfills them). The book a process attaches to its events (`notify_book` → `db.stats()`) is scoped to its
own profile. `stats.py`: per wallet, per pool, and the sum; only active pools (enabled and holding a
position now), with a count of dormant ones. Snapshot `wallet_usd` is the profile's sleeve, so sums
never double count. The residual owner of the first wallet by id emits a daily `PORTFOLIO` event
(`stats.portfolio()`), which the bridge renders.

**Deploy (`deploy.sh`, dry run by default).** Run right after the branch is merged into `main` of the
live repository (never a directory swap: other sessions commit to that repo). Everything that can fail is
checked before the stop; then one restart: stop `lp-bot.service`, backfill pre-020 rows to `sol-usdc` /
`sol-lp`, prefix the old `audit_state` keys with `sol-lp|` and the health keys with `sol-usdc|`, rename
`runtime.json`/`events.jsonl` into `run/sol-usdc/`, start every `lp-bot@<profile>`, restart the bridge. A
trap brings a bot back if any step after the stop fails. Idempotent. Rollback: `git revert` the merge, then
`./deploy.sh rollback --apply`, which refuses while another profile holds a position or a claim.
