# Base addresses for the Aerodrome Slipstream venue

Verified on 2026-10-01 (Base block ~52,048,060) with viem 2.56.9 against
`https://base-rpc.publicnode.com` (`https://mainnet.base.org` answered
`-32016 over rate limit` after about five calls in a burst). `evm/addresses.mjs`
holds these values. The signer re-checks the pool on every call (factory, NPM,
`getPool`).

Rule: an address goes into code only after a call on chain returned it or
confirmed it. Memory does not count.

| name | address | evidence |
|---|---|---|
| Pool WETH/USDC | `0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59` | Given by the owner. `CLFactory.getPool(WETH, USDC, 100)` returned this address. |
| CLFactory (PoolFactory) | `0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A` | `pool.factory()` returned it. The Slipstream README lists it as "Initial Deployment / PoolFactory". |
| token0 = WETH | `0x4200000000000000000000000000000000000006` | `pool.token0()`. `symbol()` = WETH, `decimals()` = 18. |
| token1 = USDC | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | `pool.token1()`. `symbol()` = USDC, `decimals()` = 6. |
| NonfungiblePositionManager | `0x827922686190790b37229fd06084350E74485b72` | `pool.nft()` returned it. `npm.factory()` = the CLFactory. `npm.WETH9()` = WETH. `name()` = "Slipstream Position NFT v1", `symbol()` = AERO-CL-POS. Its bytecode holds the selectors of `mint` (b5007d1f), `decreaseLiquidity` (0c49ccbe), `collect` (fc6f7865), `burn` (42966c68), `positions` (99fbab88), `tokenOfOwnerByIndex` (2f745c59) and `multicall` (ac9650d8). |
| CLGauge | `0xF33a96b5932D9E9B9A0eDA447AbD8C9d48d2e0c8` | `pool.gauge()` returned it. `gauge.pool()` = the pool. `gauge.nft()` = the NPM. `gauge.rewardToken()` = AERO `0x940181a94A35A4569E4529A3CDfB74e38FD98631`. Not used: the bot does not stake. |
| SwapRouter | `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5` | The Slipstream README lists it next to this factory ("Initial Deployment"). `router.factory()` = the CLFactory. `router.WETH9()` = WETH. Its bytecode holds `exactInputSingle` (a026383e, the Slipstream struct with `int24 tickSpacing`). An anvil fork swap through it filled. |
| QuoterV2 | `0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0` | In the README next to this factory. `quoter.factory()` = the CLFactory. `quoter.WETH9()` = WETH. `quoteExactInputSingle(0.01 WETH -> USDC, tickSpacing 100)` returned 26.969153 USDC. |
| DynamicSwapFeeModule | `0x090b2A6bb475c00e2256e2095A60887cD710803b` | `factory.swapFeeModule()` returned it (9,270 bytes of code). Read only. |
| UnstakedFeeModule | `0x0AD08370c76Ff426F534bb2AFFD9b5555338ee68` | `factory.unstakedFeeModule()` returned it. Read only. |
| Chainlink ETH/USD | `0xa4250cE1aA15Ff4cb5E5a8655293b65694e436Ed` | Chainlink's feed directory for Base (`feeds-ethereum-mainnet-base-1.json`, path `eth-usd-svr`: heartbeat 1200 s, deviation 0.15%, 8 decimals). `description()` = "ETH / USD". `latestRoundData()` = 2702.35 when the pool showed 2702.4. |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` | viem's `base` chain definition. All batched reads went through it. |

Newer Slipstream deployments (factories `0xaDe65c38…`, `0xf8f2eB49…`, with their own NPM and
router) do NOT serve this pool. Their NPMs key pools by their own factory. The signer
refuses a pool whose `factory()` or `nft()` is not in the table above.

## Pool facts (2026-10-01)

- `tickSpacing()` = 100. `tickSpacingToFee(100)` = 500 pips (the base fee for that spacing).
- `fee()` is DYNAMIC. Reads gave 535 to 728 pips (0.054% to 0.073%). The fee module's
  `dynamicFeeConfig(pool)` = baseFee 535, feeCap 2000, scalingFactor 14,900,000,
  `initialFeeEnabled` = true, initialFee 150. The FIRST swap of each block pays 150 pips
  (0.015%). Later swaps in the same block pay 535 + k·|tick − TWAP tick| (cap 2000). On an
  anvil fork, a 1 WETH swap in a fresh block paid exactly 0.015% (0.000149927 WETH). The
  realised fee is therefore below `fee()`.
- `unstakedFee()` = 50000 pips = 5%. The pool takes 5% of the swap fees that unstaked
  liquidity earns and sends it to the gauge. An unstaked LP keeps fee × 0.95 per unit of
  liquidity. `stakedLiquidity / liquidity` was 82% to 89%. Staked LPs earn AERO, not fees.
  This bot does not stake.
- GeckoTerminal (network `base`): TVL $8.87M, 24h volume $81.3M.
