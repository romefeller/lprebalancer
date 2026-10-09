# Base addresses for the Aerodrome Slipstream venue

Aerodrome runs several Slipstream deployments on Base. `evm/addresses.mjs` `DEPLOYMENTS`
holds the two the signer trusts: "initial" (first section below) and "gauges-v3" (second
section). The signer refuses a pool of any other deployment.

## Deployment "initial"

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
refuses a pool whose `factory()` and `nft()` are not one entry of `DEPLOYMENTS`.

## Pool facts, deployment "initial" (2026-10-01)

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

## Deployment "gauges-v3"

Verified on 2026-10-02 (Base block ~52,073,755) with Foundry `cast` against
`https://base-mainnet.public.blastapi.io`. Source list: the "Gauges V3 Deployment" table of
the Slipstream README (github.com/aerodrome-finance/slipstream, `main` at commit
`f8717faaae6e6717db3c8e3850149c01a79c0603`, 2026-04-10). Every row below was also
confirmed by a call on chain; the README alone does not count.

| name | address | evidence |
|---|---|---|
| Pool WETH/USDC | `0x3FE04A59Ebd38cF06080a6F60a98D124eb59392A` | Given by the owner. `factory.getPool(WETH, USDC, 50)` and `getPool(USDC, WETH, 50)` returned it. `token0()` = WETH, `token1()` = USDC, `tickSpacing()` = 50. |
| CLFactory (PoolFactory) | `0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef` | `pool.factory()` returned it. README "Gauges V3" PoolFactory. `factory.voter()` = `0x16613524…C480A5`, the same Voter as the initial factory. `poolImplementation()` = `0xc770898522D2A9c8Da7A10D63989b6b58305B665` (README PoolImplementation); the pool is an EIP-1167 clone of it. `allPoolsLength()` = 3021. |
| NonfungiblePositionManager | `0xe1f8cd9AC4e4A65F54f38a5CdAfCA44f6dD68b53` | `pool.nft()` returned it. README NonfungiblePositionManager. `npm.factory()` = this factory. `npm.WETH9()` = WETH. `name()` = "Slipstream Position NFT v1", `symbol()` = AERO-CL-POS. `tokenDescriptor()` = `0xc85C1264…8e3fE` (README NonfungibleTokenPositionDescriptor). |
| CLGauge | `0xA0B61fdB9f1FB9b917Fe38b49427Fd4D87472D28` | `pool.gauge()` returned it. `gauge.pool()` = the pool. `gauge.nft()` = this NPM. `gauge.rewardToken()` = AERO. `gauge.isPool()` = true. Not used: the bot does not stake. |
| SwapRouter | `0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F` | README "Gauges V3" SwapRouter. `router.factory()` = this factory. `router.WETH9()` = WETH. Its bytecode holds `exactInputSingle` (a026383e, the Slipstream struct with `int24 tickSpacing`) and `exactInput` (c04b8d59). The anvil fork test swaps through it (`tests/test_aerodrome_fork.mjs`). |
| Quoter | `0x514c8B5f54112481E28028F1166Bd78501089259` | README "Gauges V3" Quoter. `quoter.factory()` = this factory. `quoter.WETH9()` = WETH. Its bytecode holds `quoteExactInputSingle((address,address,uint256,int24,uint160))` (9e7defe6), the same struct as the initial QuoterV2. `quoteExactInputSingle(0.01 WETH -> USDC, tickSpacing 50)` returned 27.413776 USDC (initial QuoterV2 on its tickSpacing-100 pool: 27.413131). |
| DynamicSwapFeeModule | `0x87D8f999BBa9343E8099552426775B51C338E8CB` | `factory.swapFeeModule()` returned it (README "Gauges V3" DynamicSwapFeeModule). Its `factory()` = this factory. `dynamicFeeConfig(pool)` returned (300, 5000, 25000000). Read only. |
| UnstakedFeeModule | `0xc2cc3256434AfbC36Bb5e815e1Bb2151310a1a0b` | `factory.unstakedFeeModule()` returned it (README UnstakedFeeModule). Its `factory()` = this factory. Read only. |

### ABI: the same as deployment "initial"

Proved from the deployed bytecode (`eth_getCode`), not from memory:

- NPM: both runtime codes are 24,542 bytes. They differ in 172 bytes only: seven 20-byte
  runs that hold the factory immutable (`f8f2eb49…` against `5e7bb104…`) and the last
  32 bytes (the compiler metadata hash). Same code, so the same `positions()`, `mint`,
  `decreaseLiquidity`, `collect`, `burn`, `multicall` and events. The selectors b5007d1f,
  0c49ccbe, fc6f7865, 42966c68, 99fbab88, 2f745c59 and ac9650d8 are in both.
  `positions(67315)` (a gauge-held NFT of this pool) returned 12 words that decode as
  (nonce, operator, token0 WETH, token1 USDC, tickSpacing 50, tickLower −887250,
  tickUpper 887250, liquidity 1, …), the layout of `NPM_ABI`.
- Pool implementation: both are 24,279 bytes and differ only in the last 32 bytes
  (metadata). Same 54 PUSH4 selectors. `slot0()` returns 6 words on both pools
  (sqrtPriceX96, tick, observationIndex, observationCardinality,
  observationCardinalityNext, unlocked).
- SwapRouter and Quoter: each is the same size as its initial counterpart (9,908 and
  6,934 bytes) and differs in 92 bytes: the factory immutable three times (3 × 20) and
  the metadata hash (32). The new codes hold no copy of the initial factory.

### Why the router must come from the pool's own deployment

The initial factory ALSO holds a WETH/USDC tickSpacing-50 pool:
`getPool(WETH, USDC, 50)` on `0x5e7BB104…` = `0xAaD23a67F2AC693ABBe543489aeB3F24F561D517`.
The initial QuoterV2 asked for tickSpacing 50 quoted that pool (27.378935 USDC for 0.01
WETH), not `0x3fe04a59…`. A router or quoter computes the pool from its own factory, so
the initial router with tickSpacing 50 swaps through `0xAaD23a67…`. The signer takes the
NPM, router and quoter from the registry entry of the pool's own factory, and refuses
when any of them names another factory.

The third deployment ("Gauge Caps", factory `0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a`,
NPM `0xa990C6a7…`, router `0xcbBb8035…`) holds WETH/USDC tickSpacing 50 at
`0xc758d81B9b81A6FCDAd075bD471874A2c46B54e0`. It is NOT in the registry; the signer and
`dexes.py` refuse that pool (`tests/test_aerodrome_live.mjs`).

## Pool facts, deployment "gauges-v3" (2026-10-02)

- `tickSpacing()` = 50. `tickSpacingToFee(50)` = 500 pips.
- `fee()` is dynamic: reads gave 450 and 525 pips.
- `unstakedFee()` = 50000 pips = 5%, as on the initial pool.
- `stakedLiquidity / liquidity` was 67% to 93% (active liquidity moves with the tick).
- GeckoTerminal (network `base`): TVL $11.25M, 24h volume $64.8M (initial pool at the same
  time: TVL $8.42M, volume $80.4M).
