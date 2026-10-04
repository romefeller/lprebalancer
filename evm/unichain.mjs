// Unichain (chain id 130): the Uniswap contracts the signer may call, the RPC endpoints,
// and the price references for the pool's volatile token.
//
// Every address below was checked on chain on 2026-10-04 (evm/UNICHAIN_ADDRESSES.md): the
// NonfungiblePositionManager, SwapRouter02 and QuoterV2 each return factory() =
// UniswapV3Factory, and the factory's getPool(USDC, HYPE, 3000) is the held pool. The v4
// pool id is keccak256(abi.encode(USDC, HYPE, 500, 10, address(0))): no hooks.
import { parseAbi, getAddress } from 'viem';
import { isLoopback } from './rpc.mjs';

// Every address in its EIP-55 form, computed from lower case: a hand-typed checksum can be
// wrong, and viem refuses a wrongly cased address (2026-10-04: a v4 quote failed so).
const A = s => getAddress(s.toLowerCase());

export const CHAIN_ID = 130;

export const V3 = Object.freeze({
  factory: A('0x1F98400000000000000000000000000000000003'),
  npm: A('0x943e6e07a7E8E791dAFC44083e54041D743C46E9'),
  router: A('0x73855d06DE49d0fe4A9c42636Ba96c62da12FF9C'),     // SwapRouter02: exactInputSingle has no deadline
  quoter: A('0x385A5cf5F83e99f7BB2852b6A19C3538b9FA7658'),     // QuoterV2
});

export const V4 = Object.freeze({
  poolManager: A('0x1F98400000000000000000000000000000000004'),
  universalRouter: A('0xEf740bf23aCaE26f6492B10de645D6B98dC8Eaf3'),
  permit2: A('0x000000000022D473030F116dDEE9F6B43aC78BA3'),
  quoter: A('0x333E3C607B141b18fF6de9f258db6e77fE7491E0'),     // V4Quoter
  stateView: A('0x86e8631A016F9068C3f085fAF484Ee3F5fDee8f2'),
});

export const USDC = A('0x078D782b760474a361dDA0AF3839290b0EF57AD6');
export const HYPE = A('0x15d0e0c55a3E7eE67152aD7E89acf164253Ff68d');
export const WETH = A('0x4200000000000000000000000000000000000006');
export const STABLES = new Set([USDC.toLowerCase()]);

// The v4 pools a swap may use instead of the held v3 pool, keyed by the sorted token pair.
// Only hookless pools: a hook is code the router would run with our tokens.
export const V4_POOLS = Object.freeze([
  Object.freeze({ currency0: USDC, currency1: HYPE, fee: 500, tickSpacing: 10, hooks: '0x0000000000000000000000000000000000000000',
    id: '0xc4f393785b36430779a93eedd52dd20857a46142bbe48c88d4c655303a53279c' }),
]);

// USD price references for a volatile pool token, by lower-case address: public spot
// tickers of two venues. A write needs at least one of them to answer and the pool's price
// within LPBOT_MAX_ORACLE_DEV of their median.
export const REFERENCES = Object.freeze({
  [HYPE.toLowerCase()]: Object.freeze([
    Object.freeze({ name: 'binance HYPEUSDC', url: 'https://data-api.binance.vision/api/v3/ticker/price?symbol=HYPEUSDC', pick: j => Number(j?.price) }),
    Object.freeze({ name: 'bybit HYPEUSDT', url: 'https://api.bybit.com/v5/market/tickers?category=spot&symbol=HYPEUSDT', pick: j => Number(j?.result?.list?.[0]?.lastPrice) }),
  ]),
});

export const UNICHAIN_PUBLIC = 'https://mainnet.unichain.org';
export const UNICHAIN_PUBLICNODE = 'https://unichain-rpc.publicnode.com';
export const UNICHAIN_DRPC = 'https://unichain.drpc.org';

// LPBOT_RPC (the loop sets it from config.RPC), then LPBOT_UNICHAIN_RPC, then the public
// endpoints. A loopback LPBOT_RPC (an anvil fork in tests) is the ONLY endpoint: falling
// back from a fork to mainnet would sign the fork's transactions for the real chain.
export function unichainEndpoints(env = process.env) {
  if (env.LPBOT_RPC && isLoopback(env.LPBOT_RPC)) return [env.LPBOT_RPC];
  return [env.LPBOT_RPC, env.LPBOT_UNICHAIN_RPC, UNICHAIN_PUBLIC, UNICHAIN_PUBLICNODE, UNICHAIN_DRPC]
    .filter((v, i, a) => v && a.indexOf(v) === i);
}

// Endpoints that answer eth_simulateV1 (mainnet.unichain.org does not, 2026-10-04). On a
// loopback fork, the fork itself.
export function simulationEndpoints(env = process.env) {
  if (env.LPBOT_RPC && isLoopback(env.LPBOT_RPC)) return [env.LPBOT_RPC];
  return [UNICHAIN_PUBLICNODE, UNICHAIN_DRPC];
}

export const POOL_ABI = parseAbi([
  'function factory() view returns (address)',
  'function token0() view returns (address)',
  'function token1() view returns (address)',
  'function fee() view returns (uint24)',
  'function tickSpacing() view returns (int24)',
  'function liquidity() view returns (uint128)',
  'function slot0() view returns (uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 observationCardinality, uint16 observationCardinalityNext, uint8 feeProtocol, bool unlocked)',
]);

export const FACTORY_ABI = parseAbi([
  'function getPool(address tokenA, address tokenB, uint24 fee) view returns (address)',
]);

export const NPM_ABI = parseAbi([
  'function factory() view returns (address)',
  'function balanceOf(address owner) view returns (uint256)',
  'function tokenOfOwnerByIndex(address owner, uint256 index) view returns (uint256)',
  'function positions(uint256 tokenId) view returns (uint96 nonce, address operator, address token0, address token1, uint24 fee, int24 tickLower, int24 tickUpper, uint128 liquidity, uint256 feeGrowthInside0LastX128, uint256 feeGrowthInside1LastX128, uint128 tokensOwed0, uint128 tokensOwed1)',
  'struct MintParams { address token0; address token1; uint24 fee; int24 tickLower; int24 tickUpper; uint256 amount0Desired; uint256 amount1Desired; uint256 amount0Min; uint256 amount1Min; address recipient; uint256 deadline; }',
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
  'struct ExactInputSingleParams { address tokenIn; address tokenOut; uint24 fee; address recipient; uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96; }',
  'function exactInputSingle(ExactInputSingleParams params) payable returns (uint256 amountOut)',
]);

export const QUOTER_ABI = parseAbi([
  'function factory() view returns (address)',
  'struct QuoteExactInputSingleParams { address tokenIn; address tokenOut; uint256 amountIn; uint24 fee; uint160 sqrtPriceLimitX96; }',
  'function quoteExactInputSingle(QuoteExactInputSingleParams params) returns (uint256 amountOut, uint160 sqrtPriceX96After, uint32 initializedTicksCrossed, uint256 gasEstimate)',
]);

export const V4_QUOTER_ABI = parseAbi([
  'struct PoolKey { address currency0; address currency1; uint24 fee; int24 tickSpacing; address hooks; }',
  'struct QuoteExactSingleParams { PoolKey poolKey; bool zeroForOne; uint128 exactAmount; bytes hookData; }',
  'function quoteExactInputSingle(QuoteExactSingleParams params) returns (uint256 amountOut, uint256 gasEstimate)',
]);

export const STATE_VIEW_ABI = parseAbi([
  'function getSlot0(bytes32 poolId) view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)',
  'function getLiquidity(bytes32 poolId) view returns (uint128 liquidity)',
]);

export const UNIVERSAL_ROUTER_ABI = parseAbi([
  'function execute(bytes commands, bytes[] inputs, uint256 deadline) payable',
]);

export const PERMIT2_ABI = parseAbi([
  'function allowance(address owner, address token, address spender) view returns (uint160 amount, uint48 expiration, uint48 nonce)',
  'function approve(address token, address spender, uint160 amount, uint48 expiration)',
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

// Universal Router command and v4 router actions (v4-periphery Actions.sol).
export const CMD_V4_SWAP = 0x10;
export const ACT_SWAP_EXACT_IN_SINGLE = 0x06;
export const ACT_SETTLE_ALL = 0x0c;
export const ACT_TAKE_ALL = 0x0f;
