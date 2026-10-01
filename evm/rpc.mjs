// Base RPC endpoints and what an error means for them.
//
// mainnet.base.org answers JSON-RPC -32016 "over rate limit" after about five calls in a
// burst (seen 2026-10-01), so reads are batched through Multicall3 and a rate limit moves
// to the next endpoint. The rules are rpc_policy.mjs's: a read may move on, BEFORE anything
// is signed; after signing nothing moves and nothing is retried; a revert is the same on
// every endpoint.
import { errorKind as solanaErrorKind, AfterSignError } from '../rpc_policy.mjs';

export const BASE_PUBLIC = 'https://mainnet.base.org';
export const BASE_PUBLICNODE = 'https://base-rpc.publicnode.com';

// LPBOT_RPC (CORE sets it from config.RPC), then LPBOT_BASE_RPC, then the public pair.
// SOLANA_RPC_URL is never read here: on a Base profile it would be a Solana URL.
// A loopback LPBOT_RPC (an anvil fork in tests) is the ONLY endpoint: falling back from a
// fork to mainnet would sign the fork's transactions for the real chain.
export function baseEndpoints(env = process.env) {
  if (env.LPBOT_RPC && isLoopback(env.LPBOT_RPC)) return [env.LPBOT_RPC];
  return [env.LPBOT_RPC, env.LPBOT_BASE_RPC, BASE_PUBLIC, BASE_PUBLICNODE]
    .filter((v, i, a) => v && a.indexOf(v) === i);
}

export function isLoopback(url) {
  try {
    const h = new URL(url).hostname;
    return h === 'localhost' || h === '[::1]' || /^127(\.\d{1,3}){3}$/.test(h);
  } catch {
    return false;
  }
}

const ROTATE_CODES = new Set([-32016, -32005, 429]);

// 'rotate' or 'fatal', for a viem error or anything else. Walks the cause chain: viem
// wraps an HTTP 429 or an RPC rate limit several layers down.
export function evmErrorKind(e) {
  if (e instanceof AfterSignError || e?.sent === true) return 'fatal';
  for (let x = e, depth = 0; x && depth < 8; x = x.cause, depth++) {
    const name = x.name ?? x.constructor?.name;
    if (name === 'ContractFunctionRevertedError' || name === 'ExecutionRevertedError') return 'fatal';
    if (ROTATE_CODES.has(x.code) || x.status === 429 || (x.status >= 500 && x.status <= 504)) return 'rotate';
    if (name === 'TimeoutError' || name === 'HttpRequestError' || name === 'SocketClosedError') return 'rotate';
  }
  return solanaErrorKind(e);
}

// Run fn(url) over the endpoints until one answers; the same contract as
// rpc_policy.overEndpoints with the EVM classifier.
export async function overBase(urls, fn, { tries = 2, pauseMs = 1500, sleep = ms => new Promise(r => setTimeout(r, ms)) } = {}) {
  const seen = [];
  for (const url of urls) {
    for (let attempt = 0; attempt < tries; attempt++) {
      try {
        return await fn(url);
      } catch (e) {
        if (evmErrorKind(e) === 'fatal') throw e;
        seen.push(`${new URL(url).host}: ${String(e?.shortMessage ?? e?.message ?? e).slice(0, 160)}`);
        if (attempt + 1 < tries) await sleep(pauseMs * (attempt + 1));
      }
    }
  }
  throw new Error(`all RPC endpoints failed: ${seen.join(' | ')}`);
}
