// Base mainnet addresses and ABIs for the Aerodrome Slipstream signer.
//
// Every address here was verified on chain (evidence in chains/evm/BASE_ADDRESSES.md). Aerodrome runs
// more than one Slipstream deployment on Base. Each deployment has its own factory,
// NonfungiblePositionManager (NPM), SwapRouter and quoter, and they do not mix: an NPM
// mints only for pools of its own factory, and a router or quoter computes the pool
// address from its own factory. Two factories can both hold a WETH/USDC pool of the same
// tick spacing, so a router of the wrong deployment swaps through the wrong pool.
//
// The signer accepts a pool only when pool.factory() and pool.nft() name the factory and
// NPM of ONE entry below, factory.getPool(token0, token1, tickSpacing) returns the pool,
// and the entry's NPM, router and quoter each name that factory. Everything it then calls
// comes from that entry. A pool of any other deployment is refused.
import { parseAbi } from 'viem';

export const CHAIN_ID = 8453;

// One entry per deployment: its factory, and the NPM, router and quoter that belong to it.
// Names follow the sections of the Slipstream README (github.com/aerodrome-finance/slipstream).
export const DEPLOYMENTS = Object.freeze([
  // "Initial Deployment": verified 2026-10-01. WETH/USDC tickSpacing 100 = 0xb2cc224c…
  Object.freeze({
    name: 'initial',
    factory: '0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A',
    npm: '0x827922686190790b37229fd06084350E74485b72',
    router: '0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5',
    quoter: '0x254cF9E1E6e233aa1AC962CB9B05b2cfeAaE15b0',
  }),
  // "Gauges V3 Deployment": verified 2026-10-02. WETH/USDC tickSpacing 50 = 0x3fe04a59…
  Object.freeze({
    name: 'gauges-v3',
    factory: '0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef',
    npm: '0xe1f8cd9AC4e4A65F54f38a5CdAfCA44f6dD68b53',
    router: '0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F',
    quoter: '0x514c8B5f54112481E28028F1166Bd78501089259',
  }),
]);

export const WETH = '0x4200000000000000000000000000000000000006';
export const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
// Chainlink ETH/USD on Base ('eth-usd-svr', 8 decimals, heartbeat 1200 s, deviation 0.15%).
export const ETH_USD_FEED = '0xa4250cE1aA15Ff4cb5E5a8655293b65694e436Ed';
// Stablecoins by ADDRESS: a symbol is whatever the token contract says.
export const STABLES = new Set([USDC.toLowerCase()]);
// The sentinel for native ETH in `send`/`balance <token>` (also accepted: 'ETH').
export const NATIVE = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE';

export const POOL_ABI = parseAbi([
  'function factory() view returns (address)',
  'function token0() view returns (address)',
  'function token1() view returns (address)',
  'function tickSpacing() view returns (int24)',
  'function fee() view returns (uint24)',
  'function unstakedFee() view returns (uint24)',
  'function liquidity() view returns (uint128)',
  'function stakedLiquidity() view returns (uint128)',
  'function gauge() view returns (address)',
  'function nft() view returns (address)',
  'function slot0() view returns (uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 observationCardinality, uint16 observationCardinalityNext, bool unlocked)',
]);

export const FACTORY_ABI = parseAbi([
  'function getPool(address tokenA, address tokenB, int24 tickSpacing) view returns (address)',
  'function swapFeeModule() view returns (address)',
  'function tickSpacingToFee(int24 tickSpacing) view returns (uint24)',
]);

export const ERC20_ABI = parseAbi([
  'function balanceOf(address) view returns (uint256)',
  'function allowance(address owner, address spender) view returns (uint256)',
  'function approve(address spender, uint256 amount) returns (bool)',
  'function transfer(address to, uint256 amount) returns (bool)',
  'function decimals() view returns (uint8)',
  'function symbol() view returns (string)',
  'event Transfer(address indexed from, address indexed to, uint256 value)',
]);

export const WETH_ABI = parseAbi(['function deposit() payable']);

export const NPM_ABI = parseAbi([
  'function factory() view returns (address)',
  'function balanceOf(address owner) view returns (uint256)',
  'function ownerOf(uint256 tokenId) view returns (address)',
  'function tokenOfOwnerByIndex(address owner, uint256 index) view returns (uint256)',
  'function positions(uint256 tokenId) view returns (uint96 nonce, address operator, address token0, address token1, int24 tickSpacing, int24 tickLower, int24 tickUpper, uint128 liquidity, uint256 feeGrowthInside0LastX128, uint256 feeGrowthInside1LastX128, uint128 tokensOwed0, uint128 tokensOwed1)',
  'struct MintParams { address token0; address token1; int24 tickSpacing; int24 tickLower; int24 tickUpper; uint256 amount0Desired; uint256 amount1Desired; uint256 amount0Min; uint256 amount1Min; address recipient; uint256 deadline; uint160 sqrtPriceX96; }',
  'function mint(MintParams params) payable returns (uint256 tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)',
  'struct DecreaseLiquidityParams { uint256 tokenId; uint128 liquidity; uint256 amount0Min; uint256 amount1Min; uint256 deadline; }',
  'function decreaseLiquidity(DecreaseLiquidityParams params) payable returns (uint256 amount0, uint256 amount1)',
  'struct CollectParams { uint256 tokenId; address recipient; uint128 amount0Max; uint128 amount1Max; }',
  'function collect(CollectParams params) payable returns (uint256 amount0, uint256 amount1)',
  'function burn(uint256 tokenId) payable',
  'function multicall(bytes[] data) payable returns (bytes[] results)',
  'event Transfer(address indexed from, address indexed to, uint256 indexed tokenId)',
  'event IncreaseLiquidity(uint256 indexed tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)',
  'event Collect(uint256 indexed tokenId, address recipient, uint256 amount0, uint256 amount1)',
]);

export const ROUTER_ABI = parseAbi([
  'function factory() view returns (address)',
  'struct ExactInputSingleParams { address tokenIn; address tokenOut; int24 tickSpacing; address recipient; uint256 deadline; uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96; }',
  'function exactInputSingle(ExactInputSingleParams params) payable returns (uint256 amountOut)',
]);

export const QUOTER_ABI = parseAbi([
  'function factory() view returns (address)',
  'struct QuoteExactInputSingleParams { address tokenIn; address tokenOut; uint256 amountIn; int24 tickSpacing; uint160 sqrtPriceLimitX96; }',
  'function quoteExactInputSingle(QuoteExactInputSingleParams params) returns (uint256 amountOut, uint160 sqrtPriceX96After, uint32 initializedTicksCrossed, uint256 gasEstimate)',
]);

export const FEED_ABI = parseAbi([
  'function decimals() view returns (uint8)',
  'function latestRoundData() view returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound)',
]);
