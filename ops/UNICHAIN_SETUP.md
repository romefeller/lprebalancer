# Unichain: first profile `uni-hype-usdc` (operator steps)

Pool: Uniswap v3 USDC/HYPE 0.3% on Unichain (chain id 130),
`0x5d3e7f5da38fbf476e8b36e3b90d02fc4c1a08c3`. Token A is USDC
(`0x078D782b760474a361dDA0AF3839290b0EF57AD6`, 6 decimals), token B is HYPE
(`0x15d0e0c55a3e7ee67152ad7e89acf164253ff68d`, 18 decimals). The price is HYPE
per USDC; a HYPE is worth 1/price dollars. Gas is native ETH, outside the pool.

The profile starts dormant: it opens nothing until its wallet holds at least
`min_deploy_usd` of USDC and HYPE. Every step below is safe on a running bot.

## 1. Schema (once, with the deploy)

    psql -d rebalancer -v ON_ERROR_STOP=1 -f sql/031_unichain.sql

Additive and idempotent: it lets a `wallets` row name chain `unichain`.

## 2. The key (once)

    node chains/evm/wallet.mjs create --path /home/ubuntu/.kamino-keys/unichain-wallet.secret

It prints the new address and nothing else. The file is mode 0600 and is never
overwritten. A new key is necessary: `wallets.address` is unique, so the Base
key (`base-lp`) cannot be a second wallet row.

The service unit already names the path:
`Environment=LPBOT_UNICHAIN_KEY_PATH=/home/ubuntu/.kamino-keys/unichain-wallet.secret`
(ops/lp-bot@.service; install it again after the merge:
`sudo install -m 644 ops/lp-bot@.service /etc/systemd/system/ && sudo systemctl daemon-reload`).

## 3. The wallet row and the profile row

The tuning comes from `base-weth-usdc` (the other EVM profile). It is on
another chain, so `--cross-chain` is necessary: the tool then sets the payout
token to this pool's USDC and checks that the template's profit wallet
(`0x2b35948898e1b4897E7FC5a70e39b213dcfd0142`) is a valid address here.
A dry run first; it writes nothing:

    LPBOT_DSN=dbname=rebalancer python3 ops/add_profile.py \
        --profile uni-hype-usdc --template base-weth-usdc --cross-chain \
        --wallet uni-lp --chain unichain --address <address from step 2> \
        --secret-env LPBOT_UNICHAIN_KEY_PATH --label 'Unichain LP wallet' \
        --dex uniswap-v3-unichain --pool 0x5d3e7f5da38fbf476e8b36e3b90d02fc4c1a08c3 \
        --max-usd 260

Check the printed rows: `mints` = [USDC, HYPE] in lower case, `deposit_mint` =
HYPE (the side that is not stable), `payout_mint` = Unichain USDC,
`max_usd` = 260, `pool_pinned`, `regime_enabled` and `rebalance_swap` true,
`residual_owner` true (the profile is alone on its wallet). Then add `--apply`.

## 4. Start it

    sudo systemctl enable --now lp-bot@uni-hype-usdc

The book says `dormant` once. Nothing is signed while the wallet is empty.

## 5. Fund it

Send to the address from step 2, on Unichain:

- ETH for gas: at least the reserve (`gas_reserve_sol`, 0.003 ETH from the
  template) plus a margin, e.g. 0.005 ETH. A re-centre costs well under a cent.
- HYPE and/or USDC on Unichain, about $230 in all. The profile swaps toward
  50/50 itself and opens.

USDC fees go to the EVM profit wallet on each harvest (the pin
`LPBOT_EVM_PROFIT_WALLET_PIN` in the unit; the same address serves Base and
Unichain). HYPE fees are reinvested. While the ETH is under the reserve, the
USDC fees of a harvest are reinvested instead of paid (fees.py): top up the
ETH, the bot cannot buy gas from HYPE on this chain.

## Stop it

    touch /home/ubuntu/agent-fin/lp_bot/run/uni-hype-usdc/HALT

## Endpoint

`https://mainnet.unichain.org` by default (chains.py `public_rpc`). Set
`LPBOT_UNICHAIN_RPC` in the unit to use another endpoint.
