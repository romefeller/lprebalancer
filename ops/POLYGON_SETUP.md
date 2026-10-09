# Polygon: first profile `poly-wpol-usdt` (operator steps)

Pool: Uniswap v3 WPOL/USDT0 0.05% on Polygon PoS (chain id 137),
`0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7`. Token A is WPOL
(`0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270`, 18 decimals), token B is USDT0
(`0xc2132D05D31c914a87C6611C10748AEb04B58e8F`, 6 decimals). The price is USDT
per WPOL. Gas is native POL, outside the pool: the signer never wraps or
unwraps, so gas POL is never counted as WPOL.

The signer is `signer_uniswap.mjs` with `LPBOT_CHAIN=polygon` (the loop sets
it from the wallet's chain). Swaps use the held pool; Polygon has no v4 route.
Writes are simulated with `eth_simulateV1` on publicnode or dRPC.
`polygon-rpc.com` answers 403 (2026-10-08) and is not used.

The profile starts dormant: it opens nothing until its wallet holds at least
`min_deploy_usd` of WPOL and USDT0. Every step below is safe on a running bot.

## 1. Schema (once, with the deploy)

    psql -d rebalancer -v ON_ERROR_STOP=1 -f sql/034_polygon.sql
    psql -d rebalancer -v ON_ERROR_STOP=1 -f sql/035_native_keep.sql

Additive and idempotent: they let a `wallets` row name chain `polygon`, and
add `config.native_keep`.

## 2. The key (once)

    node chains/evm/wallet.mjs create --path /home/ubuntu/.kamino-keys/polygon-wallet.secret

It prints the new address and nothing else. The file is mode 0600 and is never
overwritten. The unit names the path:
`Environment=LPBOT_POLYGON_KEY_PATH=/home/ubuntu/.kamino-keys/polygon-wallet.secret`
(install it again after the merge:
`sudo install -m 644 ops/lp-bot@.service /etc/systemd/system/ && sudo systemctl daemon-reload`).

## 3. The wallet row and the profile row

The tuning comes from `uni-hype-usdc` (the same signer). A dry run first; it
writes nothing:

    LPBOT_DSN=dbname=rebalancer python3 ops/add_profile.py \
        --profile poly-wpol-usdt --template uni-hype-usdc --cross-chain \
        --wallet poly-lp --chain polygon --address <address from step 2> \
        --secret-env LPBOT_POLYGON_KEY_PATH --label 'Polygon LP wallet' \
        --dex uniswap-v3-polygon --pool 0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7 \
        --max-usd 260

Check the printed rows: `mints` = [WPOL, USDT0] in lower case, `deposit_mint`
= WPOL, `payout_mint` = USDT0, `max_usd` = 260. Then add `--apply`, and set
the values that are in POL, are Polygon's, or are the SOL profile's cost cuts
(the Unichain template predates them):

    psql -d rebalancer -c "update rebalancer.config
        set gas_reserve_sol = 2, swap_cost_bps = 10, regime_steps = 3, regime_threshold = 0.25,
            hot_pause_enabled = true, macro_pause_enabled = true
        where name = 'poly-wpol-usdt'"

- `gas_reserve_sol` is in the chain's native token: 2 POL (about $0.20, about
  7 re-centres). The template's 0.003 is ETH.
- `swap_cost_bps` 10, as on SOL/USDC: the pool's 0.05% fee and ~0.2 bp of
  impact on $115 one way, plus the gas of a cycle.
- `regime_steps` 3 and `regime_threshold` 0.25: SOL/USDC's settings since the
  2026-10-03 cost cut (fewer voluntary re-centres). Hot and FOMC pauses on:
  the same strategy as SOL/USDC.

## Costs (measured on an anvil fork, 2026-10-08: 281 gwei, POL $0.10)

| Step | Gas | Dollars |
|---|---|---|
| Re-centre: close + swap (approve, swap) + open (2 approves, mint) | 0.95–1.01M | ≈$0.028 |
| Increase (idle cash into the open position) | 261k | ≈$0.007 |
| Harvest | 120–137k | ≈$0.004 |
| Payout transfer | 59k | ≈$0.002 |

The re-centre swap also pays the pool's 0.05% on about half the book (≈$0.06
on $115): the held pool is the cheapest route (a $115 round trip loses 10.4 bp
there, 91 bp on the 0.3% tier, and the 0.01% tier is empty). Idle cash
(reinvested WPOL fees, a deposit) goes in with `increase`, never with a
re-centre (rebalancer.INCREASE_DEXES). The fork test fails if a re-centre
passes 1.1M gas.

## 4. Start it

    sudo systemctl enable --now lp-bot@poly-wpol-usdt

The book says `dormant` once. Nothing is signed while the wallet is empty.

## 5. Fund it

Send to the address from step 2, on Polygon PoS. The simplest is native POL
only, e.g. about $400 of POL:

- The profile keeps `native_keep` POL for gas (10, sql/035) and wraps the rest
  into WPOL on its next poll (rebalancer.wrap_native, the signer's `wrap`).
  Telegram says 🪙 WRAP. The WPOL is then a deposit like any other: deposit
  seen, the swap toward 50/50 in the pool, the open. A top-up while a position
  is open is wrapped too and goes in with `increase`, without a re-centre.
- WPOL and/or USDT0 work as well, plus at least 10 POL for gas.

    psql -d rebalancer -c "update rebalancer.config set native_keep = 10 where name = 'poly-wpol-usdt'"

`max_usd` is 600 (as on SOL/USDC): an open is at most max_usd / 1.1, so up to
~$545 deploys in one position.

The bot spends about 0.9 POL a day on gas. It never buys POL back: once the
10 POL run under the 2 POL reserve, the USDT0 fees of each harvest are
reinvested instead of paid (Telegram ⛽ PAYOUT HELD). Send POL again then;
anything above 10 POL is wrapped into the pool again.

USDT0 fees go to the EVM profit wallet on each harvest (the pin
`LPBOT_EVM_PROFIT_WALLET_PIN`; the same address serves Base, Unichain and
Polygon, a plain account on all three). WPOL fees are reinvested.

## Gas cap

`LPBOT_EVM_MAX_GWEI` defaults to 1500 on Polygon (0.5 on Unichain): a write
whose max fee is above it is refused. 1500 gwei caps a re-centre near $0.13.

## Stop it

    touch /home/ubuntu/agent-fin/lp_bot/run/poly-wpol-usdt/HALT

## Endpoint

`https://polygon-bor-rpc.publicnode.com` by default (chains.py `public_rpc`),
then dRPC. Set `LPBOT_POLYGON_RPC` in the unit to put another endpoint first.
