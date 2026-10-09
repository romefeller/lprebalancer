// Polygon PoS (chain id 137): the Uniswap v3 contracts the signer may call, the RPC endpoints,
// and the price references for WPOL.
//
// Every address below was checked on chain on 2026-10-08: the NonfungiblePositionManager,
// SwapRouter02 and QuoterV2 each return factory() = UniswapV3Factory, and the factory's
// getPool(WPOL, USDT0, 500) is the held pool 0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7.
// No v4 pool: a swap uses the held v3 pool (0.05%), the deepest WPOL/USDT pool.
//
// Native POL pays gas only. WPOL is an ordinary ERC-20 here: the signer never wraps or
// unwraps, so the gas POL is never part of the pool's side A.
import { getAddress } from 'viem';
import { polygon } from 'viem/chains';
import { isLoopback } from './rpc.mjs';

const A = s => getAddress(s.toLowerCase());

export const CHAIN_ID = 137;

export const V3 = Object.freeze({
  factory: A('0x1F98431c8aD98523631AE4a59f267346ea31F984'),
  npm: A('0xC36442b4a4522E871399CD717aBDD847Ab11FE88'),
  router: A('0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45'),     // SwapRouter02: exactInputSingle has no deadline
  quoter: A('0x61fFE014bA17989E743c5F6cB21bF9697530B21e'),     // QuoterV2
});

// No v4 route on Polygon: an empty registry, so every swap takes the v3 quote.
export const V4 = null;
export const V4_POOLS = Object.freeze([]);

export const WPOL = A('0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270');
export const USDT0 = A('0xc2132D05D31c914a87C6611C10748AEb04B58e8F');   // symbol() is 'USDT0'
export const USDC = A('0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359');    // native USDC
export const STABLES = new Set([USDT0.toLowerCase(), USDC.toLowerCase()]);

// USD price references for WPOL: public POL spot tickers of two venues. A write needs at
// least one to answer and the pool's price within LPBOT_MAX_ORACLE_DEV of their median.
export const REFERENCES = Object.freeze({
  [WPOL.toLowerCase()]: Object.freeze([
    Object.freeze({ name: 'binance POLUSDT', url: 'https://data-api.binance.vision/api/v3/ticker/price?symbol=POLUSDT', pick: j => Number(j?.price) }),
    Object.freeze({ name: 'bybit POLUSDT', url: 'https://api.bybit.com/v5/market/tickers?category=spot&symbol=POLUSDT', pick: j => Number(j?.result?.list?.[0]?.lastPrice) }),
  ]),
});

// polygon-rpc.com answers 403 since 2026-10 (its key is disabled); not listed.
export const POLYGON_PUBLICNODE = 'https://polygon-bor-rpc.publicnode.com';
export const POLYGON_DRPC = 'https://polygon.drpc.org';

// LPBOT_RPC (the loop sets it from config.RPC), then LPBOT_POLYGON_RPC, then the public
// endpoints. A loopback LPBOT_RPC (an anvil fork in tests) is the ONLY endpoint.
export function polygonEndpoints(env = process.env) {
  if (env.LPBOT_RPC && isLoopback(env.LPBOT_RPC)) return [env.LPBOT_RPC];
  return [env.LPBOT_RPC, env.LPBOT_POLYGON_RPC, POLYGON_PUBLICNODE, POLYGON_DRPC]
    .filter((v, i, a) => v && a.indexOf(v) === i);
}

// Endpoints that answer eth_simulateV1 (both public ones do, 2026-10-08). On a loopback
// fork, the fork itself.
export function simulationEndpoints(env = process.env) {
  if (env.LPBOT_RPC && isLoopback(env.LPBOT_RPC)) return [env.LPBOT_RPC];
  return [POLYGON_PUBLICNODE, POLYGON_DRPC];
}

export const NAME = 'Polygon';
export const CHAIN = 'polygon';
export const DEX = 'uniswap-v3-polygon';
export const VIEM_CHAIN = polygon;
export const NATIVE_SYMBOL = 'POL';
export const WRAPPED_NATIVE = WPOL;
// Polygon gas is priced in hundreds of gwei (281 gwei on 2026-10-08; a re-centre is about
// 880k gas = 0.25 POL, about $0.03). 1500 gwei caps a re-centre near $0.13.
export const MAX_GWEI_DEFAULT = 1500;
export const endpoints = polygonEndpoints;

export * from './uniswap_abi.mjs';
