# Multi-wallet, multi-pool: the contract (2026-10-01)

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

## Hard rules for every agent

- Work ONLY in `/home/ubuntu/agent-fin/lp_bot-new-tokens` (git worktree, branch `new-tokens`; the manager commits). NEVER write to `/home/ubuntu/agent-fin/lp_bot` (the
  live bot runs from there and spawns its `.mjs` files fresh on every call), NEVER to
  `/home/ubuntu/agent-fin/node_modules` (shared with the live bot), NEVER `systemctl stop|restart|start`.
- Databases: tests use `LPBOT_DSN=dbname=rebalancer_test` (migration 020 is applied). The live database
  `rebalancer` is read-only for you: SELECT only.
- Never send a transaction. Signers have dry runs (no `--execute`): use them against mainnet for reads and
  simulation only. Never print, log or return a private key or a key path's contents.
- Edit only the files you own (table below). If you need a change in a file you do not own, write the exact
  request in your final report; the manager applies or delegates it. Do not copy code to dodge ownership.
- Match the surrounding code: docstrings that state why, the same naming, no dead code
  (`tests/test_no_dead_code.py` must pass; extend its ALLOW only with a reason).
- Tests: every money-path change gets unit, property (hypothesis 6.168 is installed), integration and
  failure-path tests; `tests/mutate.py` targets for new money paths (it runs in a scratch copy).
  Register new test modules in `tests/run.sh`. Run the whole suite before you report.
- Report: files changed, tests added, test results (pasted counts), open risks. No claims you did not verify.

## Ownership

| agent | owns |
|---|---|
| CORE | `rebalancer.py`, `wallets.py` (new), `engine.py`, `calm.py`, `guards.py`, `health.py`, `fees.py`, `audit.py`, `txfees.py`, `scanner.py`, `swap_jupiter.mjs`, `payout.mjs`, `janitor.mjs`, `ops/`, `deploy.sh` (new), `config.py`, `chains.py`, `sql/` (021+) |
| STOCKS | `token2022.mjs` (new), `signer2.mjs`, `signer_dlmm.mjs`, `signer_raydium.mjs`, `signer_byreal.mjs`, `signer_pancake.mjs`, `signer_errors.mjs`, `rpc_policy.mjs`, `slippage.mjs`, `orca_fees.mjs`, `fee_snapshot.mjs`, `position_rent.mjs`, `dlmm_probe.mjs` |
| EVM | `signer_aerodrome.mjs` (new), `evm_wallet.mjs` (new), `evm/` (new, helpers), `dexes.py`, `package.json` + lockfile + `node_modules/` inside lp_bot_next only |
| STATS | `db.py` (report/read functions; keep the 020 context layer), `stats.py` (new), `telegram_bridge.mjs`, `book_format.mjs`, `event_emoji.json` |

Tests: each agent owns the test files it creates, and may edit existing tests only for the modules it owns.

## Already in place (manager, phase 0)

- `sql/020_multi_wallet.sql`: `wallets(id, chain, address, secret_env, label)`; `config` gains
  `wallet_id, enabled, deposit_mint, residual_owner, min_deploy_usd`; `wallet_claims(wallet_id, profile,
  mint, amount)`; `rate_gate(name, next_at)`; `profile`/`wallet_id` on `events, audits, capital_flows`;
  `capital_flows.amounts jsonb`; payout check accepts 0x addresses; one baseline per (wallet, profile).
- `db.py`: `CONTEXT`, `set_context`, `scoped`/`unscoped` (health keys are per profile, audit_state keys per
  wallet), `wallet_row`, `wallets`, `profiles(wallet_id, enabled_only)`, `add_wallet`; `event`,
  `record_audit`, `record_flow(..., amounts=)` stamp profile and wallet.
- `config.py`: `LPBOT_PROFILE` picks the profile; `WALLET_ID, CHAIN, CAPS (chains.caps), WALLET_ADDRESS,
  WALLET_SECRET_ENV, ENABLED, DEPOSIT_MINT, RESIDUAL_OWNER, MIN_DEPLOY_USD, ROOT, RUN_DIR`; `WALLET` is
  the key PATH from the env var the wallet row names; `RPC` per chain (`LPBOT_BASE_RPC` on Base).
- `chains.py`: capability rows (`sweep, janitor, audit, scanner, payout, gecko_network, native_symbol`).

## Interfaces between agents

**Processes.** One process per profile: systemd template `lp-bot@<profile>.service` with
`LPBOT_PROFILE=<profile>`. Profiles: `sol-usdc` (existing; wallet `sol-lp`, residual owner),
`mu-usdc`, `djt-usdc`, `msftx-usdc` (wallet `sol-lp`), `base-weth-usdc` (wallet `base-lp`). New ones are
`pool_pinned`, `regime_enabled`, `rebalance_swap`, payout to the chain's profit wallet in USDC.

**Run directory.** `config.RUN_DIR` = `run/<profile>/` holds `runtime.json`, `events.jsonl`, `HALT`,
`REBALANCE`, `REOPT`, `MIGRATE`. `ROOT/HALT` halts every profile. Every feed row carries `profile`,
`wallet_id`, `chain`, `pair` (CORE adds them in `notify`). The bridge (STATS) tails `run/*/events.jsonl`
and the legacy `ROOT/events.jsonl`, one cursor per file.

**Shared tokens in one wallet (CORE, `wallets.py`).** A mint used by exactly one enabled profile of a wallet
belongs wholly to it (native SOL belongs to the profile whose pool holds SOL; the gas reserve stays out).
A mint several profiles use (USDC) is split: profile P sees `claim[P]`; the residual owner sees
`wallet − Σ other claims`. Every chain() call with `--execute` runs under `pg_advisory_lock` of the wallet
(own connection, bounded wait) and, for each shared mint, adds `after − before` to the caller's claim
(floored at 0; an overdraw is an event). `wallet()` returns the sleeve view, so capital, caps, deploy-all
and the swap planner see only the profile's own money. The swap scripts honour
`LPBOT_SLEEVE={"<mint>": <max human amount>}` (CORE in `swap_jupiter.mjs`; EVM in its signer).

**Dormant profiles.** No position and sleeve deployable < `min_deploy_usd`: no open attempt, a light poll
(every ≥300 s), one `dormant` event on entry, one `deposit_seen` on exit. A deposit of the profile's
deposit mint wakes it: swap toward 50/50 (existing `balance_wallet`), open.

**Wallet-wide housekeeping** (sweep, janitor, audits, the board scanner) runs in the wallet's residual owner
only, and only where `config.CAPS` allows. Sweep and janitor keep every mint of every enabled profile of
the wallet. Audits reconcile the wallet against the sum of its profiles.

**Signers (STOCKS, EVM).** SIGNER_CONTRACT.md holds for every signer. Additions:
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
  `LPBOT_EVM_PROFIT_WALLET_PIN` (send refuses any other recipient). HALT as in the contract.
- `dexes.pool('aerodrome-slipstream', addr)` returns the usual record shape (EVM).
- CORE routes: `SIGNERS['aerodrome-slipstream']`; on Base the swap goes to the venue signer instead of
  Jupiter, and payout calls go to the venue signer instead of `payout.mjs`.

**Stats (STATS).** Every report function takes `profile=None, wallet_id=None` filters; `None` = all
enabled. Per-profile money: harvests/snapshots/band_profile through `positions.config_name`; payouts by
`config_name`; flows by `profile`/`wallet_id` (pre-020 NULL rows belong to `sol-usdc`/`sol-lp`; the deploy
backfills them). The book a process attaches to its events (`notify_book` → `db.stats()`) is scoped to its
own profile. `stats.py`: per wallet, per pool, and the sum; only active pools (enabled and holding a
position now), with a count of dormant ones. Snapshot `wallet_usd` is the profile's sleeve (CORE), so sums
never double count. The residual owner of the first wallet by id emits a daily `PORTFOLIO` event
(`stats.portfolio()`), which the bridge renders.

**Deploy (CORE, `deploy.sh`, run by the manager only).** Apply 020; register wallets; backfill
`sol-usdc` (wallet `sol-lp`, enabled, residual owner, deposit mint SOL) and pre-020 NULL rows; prefix
the old `audit_state` keys with `sol-lp|` and the health keys with `sol-usdc|`; create the new profiles;
move `runtime.json`/`events.jsonl` into `run/sol-usdc/`; (the code reaches `lp_bot` as a git merge into
its `main` by the manager — never a directory swap: another session commits to that repo);
switch `lp-bot.service` to `lp-bot@sol-usdc`; start the others; restart the bridge. Idempotent, with a dry
mode; rollback = git revert of the merge + the old unit + restart (020 is additive).
