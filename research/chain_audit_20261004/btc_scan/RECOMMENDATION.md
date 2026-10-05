Pool expansion recommendation, 2026-10-04 UTC.

**Recommended order: research BTC/stable pools on the existing Solana and Base integrations; build Sui collection and paper execution; investigate Stacks/Bitflow; defer a production Bitcoin-layer integration.** This is a prioritization of experiments and engineering work, not a finding that the newly discovered pools are profitable. No positions, services or live configuration were changed.

BTC's calendar behavior does not require a Bitcoin-specific execution chain. A cbBTC/USDC pool on Solana or Base can express that hypothesis. Custody and redemption of each BTC representation still differ. Base is an Ethereum L2; Stacks, Rootstock and other Bitcoin-connected networks have different security and execution designs and are not interchangeable.

**Existing-chain shortlist.** The figures below are the saved GeckoTerminal API discovery snapshot, taken around 18:55–18:57 UTC. Rolling API data and cached website pages gave different values, so the table consistently uses the saved API responses. TVL and volume are discovery filters; neither measures earnings per unit of active liquidity.

| Priority | Pool | TVL | 24-hour volume | Reason to investigate |
|---|---|---:|---:|---|
| 1 | Orca cbBTC/USDC, Solana | $6.10m | $3.24m | Existing chain and venue integration; test BTC weekends without building a chain stack |
| 2 | Aerodrome Slipstream 3 cbBTC/USDC, Base, displayed 5 bp | $5.22m | $14.13m | Active BTC/stable market; Base signer already exists; distinguish unstaked fees from staked rewards |
| 3 | Meteora cbBTC/USDC, Solana | $1.41m | $0.484m | Existing DLMM venue support; compare actual per-bin fees and rebalancing costs |
| Later | PancakeSwap V3 BTCB/USDT, BSC, 5 bp | $23.92m | $7.14m | Deep alternative BTC/stable venue, but BSC is not currently a configured chain |
| Later | Uniswap V3 WBTC/USDC, Arbitrum, 5 bp | $8.07m | $1.90m | Comparison venue; requires another chain integration |

Pool links and exact addresses:

- [Orca cbBTC/USDC](https://www.geckoterminal.com/solana/pools/HxA6SKW5qA4o12fjVgTpXdq2YnZ5Zv1s7SB4FFomsyLM)
- [Aerodrome cbBTC/USDC](https://www.geckoterminal.com/base/pools/0x160d7e9d948b16c163332a277b393c288408eb12)
- [Meteora cbBTC/USDC](https://www.geckoterminal.com/solana/pools/7ubS3GccjhQY99AYNKXjNJqnXjaokEdfdV915xnCb96r)
- [PancakeSwap BTCB/USDT](https://www.geckoterminal.com/bsc/pools/0x46cf1cf8c69595804ba91dfdd8d6b960c9b0a7c4)
- [Uniswap WBTC/USDC](https://www.geckoterminal.com/arbitrum/pools/0x0e4831319a50228b9e450861297ab92dee15b44f)

The search also returned tokens named BTC with implausible TVL and little activity. Those are excluded. Before any pool is admitted to a bot, match the exact token contracts/mints to the intended issuer and validate prices; ticker matching alone is insufficient. No search-result token was added to a live allowlist.

**Sui: implement the research path first.** The prior audit already supplies historical hourly data, but a full execution adapter has a larger scope. Local `chains.py` currently supports Solana and Base; Sui needs its own addresses, state reader, transaction lifecycle, gas accounting, payout and signer implementation.

The first Sui work should record current pool/tick state and executable swap quotes, then maintain paper positions using the same causal CALM/WARM/HOT controller and explicit parking policy. Reuse the decision logic while implementing Sui-specific data and accounting separately. The [Cetus CLMM interface](https://github.com/CetusProtocol/cetus-clmm-interface/blob/main/sui/cetus_clmm/README.md) provides the liquidity and reward operations that a later signer would need.

Test these candidates in order:

1. **Cetus ETH/USDC 25 bp**: the previous audit's CALM/WARM fee-only ratio was 1.187, so the candidate does not depend on rewards to clear the variance proxy across that sample. Its first half was below one; it is still unproven after costs. Validate the exact ETH representation and redemption/liquidity route.
2. **Cetus SUI/USDC 5 bp**: CALM/WARM was 1.219 including rewards, but only 0.918 fees-only. Replay rewards sold at realistic bid quotes, delayed claims and lower emissions. Its second-half ratio of 1.017 leaves little pre-cost margin.
3. **Bluefin xBTC/USDC**: test a weekend policy explicitly. The previous audit's weekend fee-only ratio was 1.292, versus 0.888 for generic CALM/WARM selection. This requires a separate Bluefin adapter if promoted beyond research.

These figures are from the prior audit, not new live P&L. Do not rank chains solely by them. Compare each candidate over the same dates and at the same capital, recording spendable fees, ending equity plus payouts, drawdown, time active and turnover. Simulate idle stablecoins and a stablecoin LP as distinct HOT defenses; charge both exit and re-entry, and earn stable-pool fees only while actually deployed there. Include one-off bridging/setup costs in the capital-payback assessment. A new chain is unattractive at $230 if small daily margins cannot recover those costs.

After a historical inventory-and-cost replay, use a forward paper run spanning at least two weekends before choosing a funded pilot. This is an engineering recommendation, not a guarantee that two weekends establish statistical reliability. No funded pilot is authorized or executed by this recommendation.

**Bitcoin-connected chains.** A current DefiLlama API discovery screen gives the following covered DEX volumes, averaged over 30 days. Coverage is not assumed complete, and a chain total is not a pool profit estimate.

| Network | Covered DEX volume, average per day over 30 days | Research decision |
|---|---:|---|
| Stacks | $3.52m | Investigate Bitflow sBTC/USDCx |
| Rootstock | $399k | Monitor individual BTC/stable pools; lower priority |
| BOB | $6.54k | Defer until pool-level activity justifies work |
| Core | $4.64k | Defer on this screen |
| Merlin | $656 | Defer on this screen |
| Bitlayer | $37 | Defer on this screen |

Raw API URLs follow `https://api.llama.fi/overview/dexs/{chain}?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true`; the exact responses are saved as `llama_*.json`. The 24-hour chain headline and sums of protocol entries sometimes differed, so this report uses the chain's reported 30-day totals consistently. The [Stacks dashboard](https://defillama.com/chain/stacks) and [Rootstock dashboard](https://defillama.com/chain/rootstock) provide interactive views.

**Stacks has a real candidate, with an unresolved accounting issue.** [Bitflow](https://www.bitflow.finance/) has a live HODLMM market. Its official app API returned:

- `dlmm_1`, sBTC/USDCx, pool contract `SM1FKXGNZJWSTWDWXQZJNF7B5TV5ZB235JTCXYXKD.dlmm-pool-sbtc-usdcx-v-1-bps-10`.
- About $340,721 TVL, $672,251 one-day volume and $34.00m 30-day volume.
- The quote API reports **50 bp total swap fee**, split into **25 bp provider fee and 25 bp protocol fee**, with zero variable fee in this snapshot. The 10 bp bin step in the contract name is not the trading fee.
- At a 50 bp swap rate, exchanging half a $230 position costs approximately **$0.575 per exchange**, before slippage and gas. Repeated re-centering or stablecoin parking can therefore consume substantial income. The actual aggregator route may charge less and must be quoted.
- Public read endpoints for pools and bins worked without an API key during this check; the [official API documentation](https://docs.bitflow.finance/bitflow-documentation/developers/hodlmm-api-documentation) documents key requirements and beta exceptions. It also documents liquidity-add/move fee calculations, which must be modeled rather than assuming only gas.

The pool summary reported `feesUsd1d = 1046.11` and `feesUsd30d = 42942.57`. Summing all 1,001 bins from the official bin-metrics endpoint instead produced:

| Metric | One day | 30 days |
|---|---:|---:|
| Total bin fees | $120.59 | $3,123.33 |
| Provider bin fees | $60.32 | $1,562.46 |

The bin TVL sum **does** match the pool TVL ($340,720.70), but fees and volume do not reconcile. Possible causes include update timing, different event coverage or accounting definitions; the cause is not established. Neither the headline APR nor the smaller bin sum can yet be treated as a verified history of collectible LP earnings. Resolve this with contract events and position-level claims before writing a funded Stacks bot. Also use HODLMM's bin accounting: the smooth CLMM concentration multiplier from the Sui study cannot simply be applied to it.

Stacks is not constrained to one execution block per Bitcoin block. Its [Nakamoto documentation](https://docs.stacks.co/reference/nakamoto-upgrade/nakamoto-in-10-minutes) describes roughly five-second blocks. BTC withdrawal settlement is separate: [sBTC withdrawal documentation](https://docs.stacks.co/learn/sbtc/sbtc-operations/withdrawal) describes six Bitcoin confirmations. Neither should be confused with the local time required to switch an LP into USDCx.

**Rootstock is technically closer, economically less compelling in this screen.** [Rootstock](https://rootstock.io/) supports EVM applications and Uniswap/Oku. The sampled USD₮0/WRBTC 30 bp pool had about $358,501 TVL but only $7,801 one-day volume. That is about $23.40 total gross swap fees at the displayed tier before any protocol share. It may still have a concentrated niche, but its active-liquidity history and achievable LP share would have to justify the integration. Large capital locked elsewhere in a Bitcoin ecosystem does not establish a fee opportunity in the target pool.

**Concrete next deliverable:** one comparable paper ledger for Orca BTC, Aerodrome BTC and Cetus ETH/SUI, with lagged regime decisions, measured inside fees/rewards, executable swap costs, and separate idle-stable versus stable-LP defense. Keep Bitflow as the additional research candidate pending reconciliation. This directly tests the user's strategy and avoids spending the next development cycle on a new chain merely because it markets itself as Bitcoin L2.

Evidence files are in this directory, including `summary.json`, raw GeckoTerminal and DefiLlama responses, official Bitflow OpenAPI schemas, pool data, all bins and the fee reconciliation totals in `bitflow_fee_check.json`.
