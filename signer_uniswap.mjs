// Rebalancer — the Uniswap v3 signing layer on Unichain and Polygon (LPBOT_CHAIN picks the chain
// module in evm/chains.mjs; unset means Unichain). Same commands and output fields as
// the other signers (SIGNER_CONTRACT.md), plus the EVM additions of MULTI_DESIGN.md:
// `rebalance` (the swap_jupiter.mjs shape) and `send` / `balance <token>` (the payout.mjs
// shape, to the pinned profit wallet only).
//
// A position is an ERC-721 of the NonfungiblePositionManager (NPM). The signer reads, opens,
// harvests and closes only NFTs of THIS wallet on THIS pool (same token0, token1 and fee).
// The pool must be a pool of the UniswapV3Factory in the chain module (its factory() and
// getPool() agree), and the NPM, router and quoter must each name that factory.
//
// Neither pool token is native: ETH (POL on Polygon) pays gas only and never goes below
// LPBOT_GAS_RESERVE_NATIVE. Approvals are exact (the amount the next call pulls).
//
// A swap takes the better of two quotes: the held v3 pool through SwapRouter02, or a
// hookless v4 pool of the same pair (the chain module's V4_POOLS; none on Polygon) through the Universal
// Router and Permit2. The 0.05% v4 pool costs a sixth of the held pool's 0.3% fee.
//
// Every write: HALT check, pool genuineness, the price reference check (marketRefusals), a
// refusal list, the whole sequence simulated in one eth_simulateV1 call, then per
// transaction: HALT check, eth_call, estimateGas, sign, send, wait for the receipt, check
// its status. A pending transaction from this wallet refuses the command. After the first
// send nothing is retried: a failure part-way is printed with `sent: true, partial: true`
// and every hash.
//
// The key is read from WALLET_SECRET_PATH inside this process (evm/keyfile.mjs) and never
// printed. Commands:
//   node signer_uniswap.mjs balance [pool]          | balance <token>   (payout.mjs shape)
//   node signer_uniswap.mjs positions
//   node signer_uniswap.mjs status [tokenId]
//   node signer_uniswap.mjs pool [pool]
//   node signer_uniswap.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node signer_uniswap.mjs increase <tokenId> <maxA> <maxB> [--execute]   (add to the open position, in range)
//   node signer_uniswap.mjs harvest <tokenId> [--execute]
//   node signer_uniswap.mjs close <tokenId> [--execute]
//   node signer_uniswap.mjs rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute]
//   node signer_uniswap.mjs send <token> <amount> <to> [--execute]
//   node signer_uniswap.mjs wrap <amount> [--execute]                (native coin -> its wrapped token)
import path from 'node:path';
import { assertNotHalted } from './halt_guard.mjs';
import { createPublicClient, http, getAddress, isAddress, encodeFunctionData, encodeAbiParameters, decodeEventLog, keccak256 } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { isEntry } from './rpc_policy.mjs';
import { planRebalance, TARGET_TOLERANCE } from './rebalance_plan.mjs';
import { readKey } from './evm/keyfile.mjs';
import { overBase, evmErrorKind } from './evm/rpc.mjs';
import { chainModule } from './evm/chains.mjs';
import * as M from './evm/clmath.mjs';

const DIR = path.dirname(new URL(import.meta.url).pathname);
// The chain module: Unichain unless LPBOT_CHAIN names another (evm/chains.mjs).
let U = chainModule(process.env.LPBOT_CHAIN);
// Tests switch the chain without a new process. Returns the module.
export function useChain(name) {
  U = chainModule(name);
  return U;
}
const DEADLINE_S = 300;
const RECEIPT_TIMEOUT_MS = 120_000;
const MAX_POSITIONS_SCANNED = 200;
const PERMIT_EXPIRY_S = 900;            // a Permit2 allowance lives 15 minutes
const REFERENCE_TIMEOUT_MS = 5_000;
const ZERO = '0x0000000000000000000000000000000000000000';

// Read at call time, not import time, so a test can set them per case.
export function settings(env = process.env) {
  const num = (k, d) => {
    const v = env[k] == null || env[k] === '' ? d : Number(env[k]);
    if (!Number.isFinite(v) || v < 0) throw new Error(`${k}=${env[k]} is not a non-negative number`);
    return v;
  };
  return {
    maxUsd: num('LPBOT_MAX_USD', 260),
    slippageBps: num('LPBOT_SLIPPAGE_BPS', 100),
    gasReserve: M.toRaw(num('LPBOT_GAS_RESERVE_NATIVE', 0.0005), 18),
    maxImpact: num('LPBOT_MAX_IMPACT', 0.01),
    maxOracleDev: num('LPBOT_MAX_ORACLE_DEV', 0.02),
    maxFeeWei: M.toRaw(num('LPBOT_EVM_MAX_GWEI', U.MAX_GWEI_DEFAULT), 9),
    pin: env.LPBOT_EVM_PROFIT_WALLET_PIN ?? '',
    profit: env.LPBOT_PROFIT_WALLET ?? '',
    sleeve: env.LPBOT_SLEEVE ?? '',
  };
}

// HALT in this script's directory halts every profile; LPBOT_RUN_DIR/HALT halts one.
export function guard(env = process.env) {
  assertNotHalted(DIR, env);
}

function poolArg(explicit) {
  const i = process.argv.indexOf('--pool');
  const p = explicit ?? (i >= 0 ? process.argv[i + 1] : undefined) ?? process.env.LPBOT_POOL;
  if (!p) throw new Error('no pool: pass --pool <address> or set LPBOT_POOL');
  if (!isAddress(p, { strict: false })) throw new Error(`pool ${p} is not an address`);
  return getAddress(p);
}

const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
const r6 = (x, d = 6) => (x == null ? null : Number(Number(x).toFixed(d)));

// --- connection -----------------------------------------------------------------------
export async function connect(url) {
  guard();
  const pub = createPublicClient({ chain: U.VIEM_CHAIN, transport: http(url, { retryCount: 1, timeout: 20_000 }) });
  const chainId = await pub.getChainId();
  if (chainId !== U.CHAIN_ID) throw new Error(`RPC ${new URL(url).host} serves chain ${chainId}, not ${U.NAME} (${U.CHAIN_ID}); refusing`);
  const account = privateKeyToAccount(readKey(process.env.WALLET_SECRET_PATH));
  return { pub, account, me: account.address, url };
}

export async function withRpc(fn, deps = {}) {
  const { urls = U.endpoints(), connectFn = connect, sleep } = deps;
  return overBase(urls, async url => fn(await connectFn(url)), { sleep });
}

// --- the pool describes itself --------------------------------------------------------
// Refuses a pool that is not a genuine pool of the known factory: the pool's factory() is
// the factory, the factory maps (token0, token1, fee) back to the pool, and the NPM, router
// and quoter each name the factory.
export async function describe(pub, pool, refs = referencePrices) {
  const c = (functionName) => ({ address: pool, abi: U.POOL_ABI, functionName });
  const [factory, t0, t1, fee, ts, liquidity, slot0] = await pub.multicall({
    allowFailure: false,
    contracts: [c('factory'), c('token0'), c('token1'), c('fee'), c('tickSpacing'), c('liquidity'), c('slot0')],
  });
  if (!same(factory, U.V3.factory)) throw new Error(`refused: pool ${pool} belongs to factory ${factory}, not ${U.V3.factory}`);
  const e = (address, functionName) => ({ address, abi: U.ERC20_ABI, functionName });
  const [real, npmFactory, routerFactory, quoterFactory, d0, d1, s0, s1] = await pub.multicall({
    allowFailure: false,
    contracts: [
      { address: U.V3.factory, abi: U.FACTORY_ABI, functionName: 'getPool', args: [t0, t1, fee] },
      { address: U.V3.npm, abi: U.NPM_ABI, functionName: 'factory' },
      { address: U.V3.router, abi: U.ROUTER_ABI, functionName: 'factory' },
      { address: U.V3.quoter, abi: U.QUOTER_ABI, functionName: 'factory' },
      e(t0, 'decimals'), e(t1, 'decimals'), e(t0, 'symbol'), e(t1, 'symbol'),
    ],
  });
  if (!same(real, pool)) throw new Error(`refused: factory ${U.V3.factory} maps (${t0}, ${t1}, ${fee}) to ${real}, not ${pool}`);
  for (const [what, addr, f] of [['position manager', U.V3.npm, npmFactory], ['router', U.V3.router, routerFactory], ['quoter', U.V3.quoter, quoterFactory]]) {
    if (!same(f, U.V3.factory)) throw new Error(`refused: ${what} ${addr} names factory ${f}, not ${U.V3.factory}`);
  }
  const decA = Number(d0), decB = Number(d1);
  const price = M.priceFromSqrtX96(slot0[0], decA, decB);
  const stableA = U.STABLES.has(t0.toLowerCase()), stableB = U.STABLES.has(t1.toLowerCase());
  // USD per unit of token B: 1 for a stable B; 1/price (USD per B, A at $1) for a stable A.
  const quoteUsd = stableB ? 1 : stableA && price > 0 ? 1 / price : null;
  const volatile = stableA ? t1 : stableB ? t0 : null;
  const poolUsd = stableB ? price : stableA && price > 0 ? 1 / price : null;   // USD per volatile token, by the pool
  const ref = volatile ? await refs(volatile) : { usd: null, sources: [] };
  return {
    pool, dex: U.DEX, chain: U.CHAIN, factory, npm: U.V3.npm, router: U.V3.router, quoter: U.V3.quoter,
    mintA: t0, mintB: t1, symbolA: s0, symbolB: s1, decimalsA: decA, decimalsB: decB,
    tickSpacing: Number(ts), tick: Number(slot0[1]), sqrtPriceX96: slot0[0].toString(), price,
    // SIGNER_CONTRACT's Token-2022 fields: ERC-20 amounts carry no UI multiplier
    uiPrice: price, multiplierA: 1, multiplierB: 1, paused: false,
    quoteUsd, quoteUsdSource: stableB ? 'stable' : stableA ? 'stable A: 1/price' : 'unknown',
    nativeSide: null, feePips: Number(fee), fee: Number(fee) / 1e6,
    liquidity: liquidity.toString(),
    volatile, poolUsd, referenceUsd: ref.usd, referenceSources: ref.sources,
    referenceDeviation: poolUsd != null && ref.usd > 0 ? Math.abs(poolUsd / ref.usd - 1) : null,
  };
}

// USD price of `token` from its public references (the chain module's REFERENCES): the
// median of the ones that answer, with each source's figure. No reference: usd null.
export async function referencePrices(token, fetchFn = fetch) {
  const list = U.REFERENCES[String(token).toLowerCase()] ?? [];
  const got = await Promise.all(list.map(async r => {
    try {
      const res = await fetchFn(r.url, { signal: AbortSignal.timeout(REFERENCE_TIMEOUT_MS) });
      if (!res.ok) return { name: r.name, usd: null };
      const v = r.pick(await res.json());
      return { name: r.name, usd: Number.isFinite(v) && v > 0 ? v : null };
    } catch {
      return { name: r.name, usd: null };
    }
  }));
  const ok = got.map(g => g.usd).filter(v => v != null).sort((a, b) => a - b);
  const usd = !ok.length ? null : ok.length % 2 ? ok[(ok.length - 1) / 2] : (ok[ok.length / 2 - 1] + ok[ok.length / 2]) / 2;
  return { usd, sources: got };
}

// Refusals every write shares: the pool's price must agree with the references before
// money moves at it. No reference answering is a refusal too (fail closed).
export function marketRefusals(info, cfg) {
  const out = [];
  if (!(info.quoteUsd > 0)) out.push(`cannot price ${info.symbolB} in USD`);
  if (info.volatile) {
    if (!(info.referenceUsd > 0)) out.push(`no price reference answered for ${info.volatile}`);
    else if (!(info.referenceDeviation <= cfg.maxOracleDev)) {
      out.push(`pool price $${Number(info.poolUsd).toFixed(4)} is ${(info.referenceDeviation * 100).toFixed(2)}% from the reference $${info.referenceUsd.toFixed(4)} (limit ${(cfg.maxOracleDev * 100).toFixed(2)}%)`);
    }
  }
  return out;
}

// --- wallet reads ---------------------------------------------------------------------
async function holdings(pub, me, info) {
  const [eth, [b0, b1]] = await Promise.all([
    pub.getBalance({ address: me }),
    pub.multicall({ allowFailure: false, contracts: [info.mintA, info.mintB].map(t => ({ address: t, abi: U.ERC20_ABI, functionName: 'balanceOf', args: [me] })) }),
  ]);
  return { eth, rawA: b0, rawB: b1 };
}

// What a profile may spend of side A / B in raw units, capped by the sleeve.
export function spendable(h, info, sleeve) {
  return {
    a: M.capped(h.rawA, M.sleeveCap(sleeve, info.mintA, info.decimalsA)),
    b: M.capped(h.rawB, M.sleeveCap(sleeve, info.mintB, info.decimalsB)),
  };
}

async function balance(poolExplicit) {
  const pool = poolArg(poolExplicit);
  const cfg = settings();
  return withRpc(async ({ pub, me }) => {
    const info = await describe(pub, pool);
    const h = await holdings(pub, me, info);
    const ua = M.toHuman(h.rawA, info.decimalsA), ub = M.toHuman(h.rawB, info.decimalsB);
    const ethHuman = M.toHuman(h.eth, 18);
    const out = {
      owner: me, chain: U.CHAIN, sol: ethHuman, eth: ethHuman, pool, dex: U.DEX,
      tokenA: info.symbolA, tokenB: info.symbolB, mintA: info.mintA, mintB: info.mintB,
      price: info.price, uiPrice: info.uiPrice, multiplierA: 1, multiplierB: 1,
      quoteUsd: info.quoteUsd, nativeSide: null, balanceA: ua, balanceB: ub,
      gasReserveNative: M.toHuman(cfg.gasReserve, 18),
    };
    // ETH is gas, outside the pool: not part of walletUsd (the loop counts the native token separately)
    out.walletUsd = info.quoteUsd == null ? null : Number(((ua * info.price + ub) * info.quoteUsd).toFixed(4));
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// --- positions ------------------------------------------------------------------------
// This wallet's NFTs on this pool: same tokens and fee (the NPM keys pools by exactly that
// triple). NFTs with no liquidity and nothing owed are spent; they are left out.
export async function ownPositions(pub, me, info) {
  const n = Number(await pub.readContract({ address: info.npm, abi: U.NPM_ABI, functionName: 'balanceOf', args: [me] }));
  if (n > MAX_POSITIONS_SCANNED) throw new Error(`wallet holds ${n} position NFTs; refusing to scan more than ${MAX_POSITIONS_SCANNED}`);
  if (!n) return [];
  const ids = await pub.multicall({ allowFailure: false, contracts: Array.from({ length: n }, (_, i) => ({ address: info.npm, abi: U.NPM_ABI, functionName: 'tokenOfOwnerByIndex', args: [me, BigInt(i)] })) });
  const ps = await pub.multicall({ allowFailure: false, contracts: ids.map(id => ({ address: info.npm, abi: U.NPM_ABI, functionName: 'positions', args: [id] })) });
  return ids.map((id, i) => ({ id, p: ps[i] }))
    .filter(({ p }) => same(p[2], info.mintA) && same(p[3], info.mintB) && Number(p[4]) === info.feePips)
    .filter(({ p }) => p[7] > 0n || p[10] > 0n || p[11] > 0n)
    .map(({ id, p }) => ({ tokenId: id, tickLower: Number(p[5]), tickUpper: Number(p[6]), liquidity: p[7], owed0: p[10], owed1: p[11] }))
    .sort((x, y) => (x.tokenId < y.tokenId ? -1 : 1));
}

async function positions() {
  const pool = poolArg();
  return withRpc(async ({ pub, me }) => {
    const info = await describe(pub, pool);
    const list = await ownPositions(pub, me, info);
    const out = list.map(x => ({
      address: x.tokenId.toString(), tokenId: x.tokenId.toString(), pool,
      tickLower: x.tickLower, tickUpper: x.tickUpper,
      lowerPrice: r6(M.priceAtTick(x.tickLower, info.decimalsA, info.decimalsB), PX_DECIMALS),
      upperPrice: r6(M.priceAtTick(x.tickUpper, info.decimalsA, info.decimalsB), PX_DECIMALS),
      liquidity: x.liquidity.toString(),
    }));
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// Fees a collect would pay now: an eth_call of collect from the owner. The NPM pokes the
// pool (burn of 0) first when the position has liquidity, so this is exact, not an estimate.
async function feesOwed(pub, me, info, tokenId) {
  const { result } = await pub.simulateContract({
    account: me, address: info.npm, abi: U.NPM_ABI, functionName: 'collect',
    args: [{ tokenId, recipient: me, amount0Max: M.MAX_UINT128, amount1Max: M.MAX_UINT128 }],
  });
  return result;
}

// Prices of a HYPE-per-USDC pool are ~0.011: twelve decimals keep ten significant figures.
const PX_DECIMALS = 12;

export function positionView(x, info, fees) {
  const sa = M.sqrtRatioAtTick(x.tickLower), sb = M.sqrtRatioAtTick(x.tickUpper);
  const [e0, e1] = M.amountsForLiquidity(BigInt(info.sqrtPriceX96), sa, sb, x.liquidity, false);
  const estA = M.toHuman(e0, info.decimalsA), estB = M.toHuman(e1, info.decimalsB);
  const feeA = M.toHuman(fees[0], info.decimalsA), feeB = M.toHuman(fees[1], info.decimalsB);
  const id = x.tokenId.toString();
  const out = {
    positionMint: id, tokenId: id, whirlpool: info.pool, pool: info.pool, dex: U.DEX, chain: U.CHAIN,
    pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
    decimalsA: info.decimalsA, decimalsB: info.decimalsB,
    quoteUsd: info.quoteUsd, quoteUsdSource: info.quoteUsdSource,
    liquidity: x.liquidity.toString(), tickLower: x.tickLower, tickUpper: x.tickUpper,
    lowerPrice: r6(M.priceAtTick(x.tickLower, info.decimalsA, info.decimalsB), PX_DECIMALS),
    upperPrice: r6(M.priceAtTick(x.tickUpper, info.decimalsA, info.decimalsB), PX_DECIMALS),
    price: r6(info.price, PX_DECIMALS), uiPrice: r6(info.price, PX_DECIMALS), multiplierA: 1, multiplierB: 1,
    // a tick pool is in range for tickLower <= tick < tickUpper
    inRange: info.tick >= x.tickLower && info.tick < x.tickUpper,
    closeEstA: estA, closeEstB: estB,
    feesAccruedA: feeA, feesAccruedB: feeB,
    feesAccrued_quote: Number((feeA * info.price + feeB).toFixed(12)),
    // no rent on an EVM chain: the NFT holds nothing refundable
    rentSol: 0, rentUsd: 0, feePips: info.feePips,
  };
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((estA * info.price + estB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number(((feeA * info.price + feeB) * info.quoteUsd).toFixed(6));
  }
  return out;
}

async function status(positionArg) {
  const pool = poolArg();
  return withRpc(async ({ pub, me }) => {
    const info = await describe(pub, pool);
    const list = await ownPositions(pub, me, info);
    const pick = positionArg ? list.find(x => x.tokenId.toString() === String(positionArg)) : list[0];
    if (!pick) {
      const none = { positions: 0, positionMint: null, pool };
      console.log(JSON.stringify(none, null, 1));
      return none;
    }
    const out = positionView(pick, info, await feesOwed(pub, me, info, pick.tokenId));
    if (list.length > 1) out.positions = list.map(x => x.tokenId.toString());
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// The position named, provided this wallet owns it and it sits on this pool.
async function ownedPosition(pub, me, info, tokenArg) {
  if (!/^\d+$/.test(String(tokenArg ?? ''))) throw new Error(`position must be an NFT token id, got ${tokenArg}`);
  const list = await ownPositions(pub, me, info);
  const x = list.find(p => p.tokenId.toString() === String(tokenArg));
  if (!x) throw new Error(`position ${tokenArg} not found for this wallet on this pool`);
  return x;
}

// --- sending --------------------------------------------------------------------------
// The whole sequence in one eth_simulateV1 call (each call sees the state the previous one
// left), on the first simulation endpoint that answers; nothing is sent unless every call
// succeeded. With no endpoint answering, each call is eth_call'ed alone (`sequence: false`):
// a call that needs an earlier approval may then show a revert it would not have.
export async function simulateSequence(pub, me, steps, deps = {}) {
  const clients = deps.clients ?? [pub, ...U.simulationEndpoints().map(u => createPublicClient({ chain: U.VIEM_CHAIN, transport: http(u, { retryCount: 0, timeout: 15_000 }) }))];
  let batchError = null;
  for (const client of clients) {
    try {
      const [block] = await client.simulateBlocks({
        blocks: [{ calls: steps.map(s => ({ from: me, to: s.to, data: s.data, value: s.value ?? 0n })) }],
      });
      return block.calls.map((c, i) => ({
        label: steps[i].label, ok: c.status === 'success', gasUsed: c.gasUsed?.toString() ?? null,
        revert: c.status === 'success' ? null : revertText(c.error), sequence: true,
      }));
    } catch (e) {
      batchError = revertText(e);
    }
  }
  const out = [];
  for (const s of steps) {
    try {
      await pub.call({ account: me, to: s.to, data: s.data, value: s.value ?? 0n });
      out.push({ label: s.label, ok: true, gasUsed: null, revert: null, sequence: false });
    } catch (err) {
      if (evmErrorKind(err) === 'rotate') throw err;
      out.push({ label: s.label, ok: false, gasUsed: null, revert: revertText(err), sequence: false, batchError });
    }
  }
  return out;
}

// The revert reason, as specific as the error chain allows.
export function revertText(e) {
  for (let x = e, d = 0; x && d < 8; x = x.cause, d++) {
    if (x.reason) return String(x.reason);
    if (x.data?.errorName) return `${x.data.errorName}(${(x.data.args ?? []).join(', ')})`;
  }
  return String(e?.shortMessage ?? e?.message ?? e).split('\n')[0].slice(0, 200);
}

// A per-call simulation (no eth_simulateV1) cannot see an approval earlier in the same
// sequence. Execute then requires every step that does not follow an approval to pass; a
// step after an approval is checked again by signStep's eth_call once the approval landed.
export function blockingFailure(simulation) {
  const seq = simulation.every(s => s.sequence);
  let approved = false;
  for (const s of simulation) {
    if (!s.ok && (seq || !approved)) return s;
    if (/^approve|^permit2/.test(s.label)) approved = true;
  }
  return null;
}

async function nonceFor(pub, me) {
  const [latest, pending] = await Promise.all([
    pub.getTransactionCount({ address: me, blockTag: 'latest' }),
    pub.getTransactionCount({ address: me, blockTag: 'pending' }),
  ]);
  if (pending > latest) throw new Error(`refused: ${pending - latest} transaction(s) from this wallet are pending; not stacking another`);
  return latest;
}

// Simulate, estimate, sign one step. Nothing leaves the process.
async function signStep(ctx, step, cfg) {
  guard();
  const { pub, account, me } = ctx;
  const req = { account: me, to: step.to, data: step.data, value: step.value ?? 0n };
  await pub.call(req);
  const gas = (await pub.estimateGas(req)) * 13n / 10n;
  const fees = await pub.estimateFeesPerGas();
  if (fees.maxFeePerGas > cfg.maxFeeWei) throw new Error(`refused: max fee ${fees.maxFeePerGas} wei/gas exceeds LPBOT_EVM_MAX_GWEI`);
  const eth = await pub.getBalance({ address: me });
  if (eth < req.value + gas * fees.maxFeePerGas) throw new Error(`refused: ${M.toHuman(eth, 18)} ${U.NATIVE_SYMBOL} cannot pay ${step.label}`);
  const serialized = await account.signTransaction({
    chainId: U.CHAIN_ID, type: 'eip1559', nonce: ctx.nonce, to: step.to, data: step.data, value: req.value,
    gas, maxFeePerGas: fees.maxFeePerGas, maxPriorityFeePerGas: fees.maxPriorityFeePerGas,
  });
  return { serialized, hash: keccak256(serialized) };
}

// Send the steps in order, each after the previous one's receipt. Throws only if NOTHING
// was sent; after the first send a failure comes back as {sent, error} so the caller
// prints a partial report instead of letting the endpoint loop retry the whole operation.
export async function runSteps(ctx, steps, cfg, deps = {}) {
  const sign = deps.signStep ?? signStep;
  const sent = [];
  for (const step of steps) {
    let hash = null;
    try {
      const s = await sign(ctx, step, cfg);
      hash = s.hash;
      await ctx.pub.sendRawTransaction({ serializedTransaction: s.serialized });
      ctx.nonce += 1;
      const receipt = await ctx.pub.waitForTransactionReceipt({ hash, timeout: RECEIPT_TIMEOUT_MS, pollingInterval: 1000 });
      if (receipt.status !== 'success') throw new Error(`transaction ${hash} (${step.label}) reverted on chain`);
      sent.push({ label: step.label, hash, receipt });
    } catch (e) {
      // A signed transaction whose send threw may still have reached a node: it counts.
      if (hash) sent.push({ label: step.label, hash, receipt: null });
      if (!sent.length) throw e;
      return { sent, error: String(e?.shortMessage ?? e?.message ?? e).split('\n')[0].slice(0, 300) };
    }
  }
  return { sent, error: null };
}

function feeEth(sent) {
  let wei = 0n;
  for (const s of sent) if (s.receipt) wei += s.receipt.gasUsed * s.receipt.effectiveGasPrice + (s.receipt.l1Fee ?? 0n);
  return M.toHuman(wei, 18);
}

// Print the report of a write that sent something; partial when it stopped part-way.
function reportSent(base, r, extra = {}) {
  const out = { ...base, ...extra, signature: r.sent[r.sent.length - 1]?.hash ?? null,
    signatures: r.sent.map(s => s.hash), steps: r.sent.map(s => s.label), feeEth: feeEth(r.sent), sent: true };
  if (r.error) { out.partial = true; out.error = r.error; process.exitCode = 1; }
  console.log(JSON.stringify(out, null, 1));
  return out;
}

// Dry run, or refusals: print the report once, with the simulation, and send nothing.
// Refusals make it a failure (error, exit 1) even without --execute.
function reportUnsent(report, refusals, simulation, note) {
  const out = { ...report, simulation, sent: false, signature: null };
  if (refusals.length) { out.refused = refusals; out.error = `refused: ${refusals.join('; ')}`; process.exitCode = 1; }
  console.log(JSON.stringify(out, null, 1));
  if (!refusals.length) console.log(note);
  return out;
}

async function deadline(pub) {
  const b = await pub.getBlock({ blockTag: 'latest' });
  return b.timestamp + BigInt(DEADLINE_S);
}

const erc20 = (token, functionName, args) => encodeFunctionData({ abi: U.ERC20_ABI, functionName, args });

// The step that lets `spender` pull exactly `need` of `token`, when the allowance is short.
async function approveSteps(pub, me, token, need, spender) {
  if (need <= 0n) return [];
  const allowance = await pub.readContract({ address: token, abi: U.ERC20_ABI, functionName: 'allowance', args: [me, spender] });
  return allowance < need ? [{ label: `approve ${token}`, to: token, data: erc20(token, 'approve', [spender, need]) }] : [];
}

// --- open -----------------------------------------------------------------------------
// The open as data: ticks, deposit, refusals. Pure given the chain reads, so the money
// arithmetic is testable without a node.
export function planOpen(info, h, cfg, sleeve, lower, upper, maxA, maxB) {
  const refusals = [...marketRefusals(info, cfg)];
  const { tickLower, tickUpper } = M.bandTicks(lower, upper, info.tickSpacing, info.decimalsA, info.decimalsB);
  if (!(info.tick >= tickLower && info.tick < tickUpper)) {
    refusals.push(`price ${info.price} (tick ${info.tick}) is outside the band ticks ${tickLower}..${tickUpper}`);
  }
  if (h.eth < cfg.gasReserve) refusals.push(`${U.NATIVE_SYMBOL} ${M.toHuman(h.eth, 18)} is below the ${M.toHuman(cfg.gasReserve, 18)} gas reserve`);
  const capA = M.capped(M.toRaw(maxA, info.decimalsA), M.sleeveCap(sleeve, info.mintA, info.decimalsA));
  const capB = M.capped(M.toRaw(maxB, info.decimalsB), M.sleeveCap(sleeve, info.mintB, info.decimalsB));
  const sa = M.sqrtRatioAtTick(tickLower), sb = M.sqrtRatioAtTick(tickUpper);
  const dep = M.depositFor(BigInt(info.sqrtPriceX96), sa, sb, capA, capB);
  const amtA = M.toHuman(dep.amountA, info.decimalsA), amtB = M.toHuman(dep.amountB, info.decimalsB);
  const approxUsd = (amtA * info.price + amtB) * (info.quoteUsd > 0 ? info.quoteUsd : NaN);
  if (dep.liquidity === 0n) refusals.push('nothing to deposit: the caps fund no liquidity');
  if (!(approxUsd <= cfg.maxUsd)) refusals.push(`position about $${Number.isFinite(approxUsd) ? approxUsd.toFixed(2) : '?'} exceeds cap $${cfg.maxUsd}`);
  if (h.rawA < dep.amountA) refusals.push(`wallet lacks ${amtA} ${info.symbolA}`);
  if (h.rawB < dep.amountB) refusals.push(`wallet lacks ${amtB} ${info.symbolB}`);
  const minA = M.minWithSlippage(dep.amountA, cfg.slippageBps), minB = M.minWithSlippage(dep.amountB, cfg.slippageBps);
  return { refusals, tickLower, tickUpper, capA, capB, dep, amtA, amtB, approxUsd, minA, minB };
}

async function open(poolIn, lower, upper, maxA, maxB, execute) {
  guard();
  const pool = poolArg(poolIn);
  const cfg = settings();
  const sleeve = M.parseSleeve(cfg.sleeve);
  return withRpc(async (ctx) => {
    const { pub, me } = ctx;
    const info = await describe(pub, pool);
    const h = await holdings(pub, me, info);
    const plan = planOpen(info, h, cfg, sleeve, lower, upper, maxA, maxB);
    const dl = await deadline(pub);
    const steps = [
      ...(await approveSteps(pub, me, info.mintA, plan.dep.amountA, info.npm)),
      ...(await approveSteps(pub, me, info.mintB, plan.dep.amountB, info.npm)),
      { label: 'mint', to: info.npm, data: encodeFunctionData({ abi: U.NPM_ABI, functionName: 'mint', args: [{
        token0: info.mintA, token1: info.mintB, fee: info.feePips,
        tickLower: plan.tickLower, tickUpper: plan.tickUpper,
        amount0Desired: plan.dep.amountA, amount1Desired: plan.dep.amountB,
        amount0Min: plan.minA, amount1Min: plan.minB, recipient: me, deadline: dl,
      }] }) },
    ];
    const report = {
      pool, dex: U.DEX, chain: U.CHAIN, pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
      requestedLower: Number(lower), requestedUpper: Number(upper),
      lowerPrice: r6(M.priceAtTick(plan.tickLower, info.decimalsA, info.decimalsB), PX_DECIMALS),
      upperPrice: r6(M.priceAtTick(plan.tickUpper, info.decimalsA, info.decimalsB), PX_DECIMALS),
      tickLower: plan.tickLower, tickUpper: plan.tickUpper, tick: info.tick, price: r6(info.price, PX_DECIMALS),
      tokenMaxA: Number(maxA), tokenMaxB: Number(maxB),
      depositEstA: plan.amtA, depositEstB: plan.amtB, liquidity: plan.dep.liquidity.toString(),
      amountMinA: M.toHuman(plan.minA, info.decimalsA), amountMinB: M.toHuman(plan.minB, info.decimalsB),
      approxUsd: Number.isFinite(plan.approxUsd) ? Number(plan.approxUsd.toFixed(2)) : null,
      depositUsd: Number.isFinite(plan.approxUsd) ? Number(plan.approxUsd.toFixed(4)) : null,
      slippageBps: cfg.slippageBps, referenceUsd: info.referenceUsd, referenceDeviation: info.referenceDeviation,
      positionMint: null, transactions: steps.length, plannedSteps: steps.map(s => s.label),
    };
    const simulation = await simulateSequence(pub, me, steps);
    if (!execute || plan.refusals.length) {
      return reportUnsent(report, plan.refusals, simulation, 'DRY RUN — open built and simulated. Pass --execute to sign and send.');
    }
    const bad = blockingFailure(simulation);
    if (bad) throw new Error(`refused: simulation of ${bad.label} reverts: ${bad.revert}`);
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    const minted = r.sent.find(s => s.label === 'mint' && s.receipt);
    const extra = {};
    if (minted) {
      for (const log of minted.receipt.logs) {
        if (!same(log.address, info.npm)) continue;
        let ev;
        try { ev = decodeEventLog({ abi: U.NPM_ABI, data: log.data, topics: log.topics }); } catch { continue; }
        if (ev.eventName === 'Transfer' && same(ev.args.to, me) && BigInt(ev.args.from) === 0n) extra.positionMint = ev.args.tokenId.toString();
        if (ev.eventName === 'IncreaseLiquidity') {
          extra.depositA = M.toHuman(ev.args.amount0, info.decimalsA); extra.depositB = M.toHuman(ev.args.amount1, info.decimalsB);
          extra.liquidity = ev.args.liquidity.toString();
        }
      }
      if (!extra.positionMint) { r.error = r.error ?? 'mint receipt carries no position NFT transfer'; }
    }
    return reportSent(report, r, extra);
  });
}

// --- increase: idle cash into the open position ----------------------------------------
// Idle cash beside the band goes into the position at its own ticks, instead of a close,
// a swap and a reopen (owner, 2026-10-08: "cost efficient on WPOL as well"; on Polygon a
// re-centre is ~1.0M gas plus a 0.05% swap of half the book, an increase ~200k gas plus
// a swap of the idle cash only). Pure given the chain reads. Refused, before anything is
// signed: a position out of range, gas under the reserve, a position that would pass
// LPBOT_MAX_USD, caps that fund no liquidity, a wallet short of the deposit.
export function planIncrease(info, x, h, cfg, sleeve, maxA, maxB) {
  const refusals = [...marketRefusals(info, cfg)];
  if (!(info.tick >= x.tickLower && info.tick < x.tickUpper)) {
    refusals.push(`price ${info.price} (tick ${info.tick}) is outside the position ticks ${x.tickLower}..${x.tickUpper}: nothing to add`);
  }
  if (h.eth < cfg.gasReserve) refusals.push(`${U.NATIVE_SYMBOL} ${M.toHuman(h.eth, 18)} is below the ${M.toHuman(cfg.gasReserve, 18)} gas reserve`);
  const capA = M.capped(M.toRaw(maxA, info.decimalsA), M.sleeveCap(sleeve, info.mintA, info.decimalsA));
  const capB = M.capped(M.toRaw(maxB, info.decimalsB), M.sleeveCap(sleeve, info.mintB, info.decimalsB));
  const sp = BigInt(info.sqrtPriceX96);
  const sa = M.sqrtRatioAtTick(x.tickLower), sb = M.sqrtRatioAtTick(x.tickUpper);
  const dep = M.depositFor(sp, sa, sb, capA, capB);
  const amtA = M.toHuman(dep.amountA, info.decimalsA), amtB = M.toHuman(dep.amountB, info.decimalsB);
  const q = info.quoteUsd > 0 ? info.quoteUsd : NaN;
  const addUsd = (amtA * info.price + amtB) * q;
  const [h0, h1] = M.amountsForLiquidity(sp, sa, sb, x.liquidity, false);
  const heldUsd = (M.toHuman(h0, info.decimalsA) * info.price + M.toHuman(h1, info.decimalsB)) * q;
  if (dep.liquidity === 0n) refusals.push('nothing to add: the caps fund no liquidity');
  if (!(heldUsd + addUsd <= cfg.maxUsd)) {
    refusals.push(`position about $${Number.isFinite(heldUsd + addUsd) ? (heldUsd + addUsd).toFixed(2) : '?'} after the add exceeds cap $${cfg.maxUsd}`);
  }
  if (h.rawA < dep.amountA) refusals.push(`wallet lacks ${amtA} ${info.symbolA}`);
  if (h.rawB < dep.amountB) refusals.push(`wallet lacks ${amtB} ${info.symbolB}`);
  const minA = M.minWithSlippage(dep.amountA, cfg.slippageBps), minB = M.minWithSlippage(dep.amountB, cfg.slippageBps);
  return { refusals, capA, capB, dep, amtA, amtB, addUsd, heldUsd, minA, minB };
}

async function increase(tokenArg, maxA, maxB, execute) {
  guard();
  const [a, b] = [Number(maxA), Number(maxB)];
  if (![a, b].every(v => Number.isFinite(v) && v >= 0) || !(a > 0 || b > 0)) {
    throw new Error('increase needs <tokenId> <maxA> <maxB>, both >= 0 and one > 0');
  }
  const pool = poolArg();
  const cfg = settings();
  const sleeve = M.parseSleeve(cfg.sleeve);
  return withRpc(async (ctx) => {
    const { pub, me } = ctx;
    const info = await describe(pub, pool);
    const x = await ownedPosition(pub, me, info, tokenArg);
    const h = await holdings(pub, me, info);
    const plan = planIncrease(info, x, h, cfg, sleeve, maxA, maxB);
    const dl = await deadline(pub);
    const steps = [
      ...(await approveSteps(pub, me, info.mintA, plan.dep.amountA, info.npm)),
      ...(await approveSteps(pub, me, info.mintB, plan.dep.amountB, info.npm)),
      { label: 'increase', to: info.npm, data: encodeFunctionData({ abi: U.NPM_ABI, functionName: 'increaseLiquidity', args: [{
        tokenId: x.tokenId, amount0Desired: plan.dep.amountA, amount1Desired: plan.dep.amountB,
        amount0Min: plan.minA, amount1Min: plan.minB, deadline: dl,
      }] }) },
    ];
    const report = {
      positionMint: x.tokenId.toString(), pool, dex: U.DEX, chain: U.CHAIN, pair: `${info.symbolA}/${info.symbolB}`,
      tokenA: info.symbolA, tokenB: info.symbolB, tokenMaxA: a, tokenMaxB: b,
      lowerPrice: r6(M.priceAtTick(x.tickLower, info.decimalsA, info.decimalsB), PX_DECIMALS),
      upperPrice: r6(M.priceAtTick(x.tickUpper, info.decimalsA, info.decimalsB), PX_DECIMALS),
      price: r6(info.price, PX_DECIMALS), depositEstA: plan.amtA, depositEstB: plan.amtB,
      liquidityBefore: x.liquidity.toString(), liquidityAdded: plan.dep.liquidity.toString(),
      heldUsd: Number.isFinite(plan.heldUsd) ? Number(plan.heldUsd.toFixed(4)) : null,
      depositUsd: Number.isFinite(plan.addUsd) ? Number(plan.addUsd.toFixed(4)) : null,
      transactions: steps.length, plannedSteps: steps.map(st => st.label),
    };
    const simulation = await simulateSequence(pub, me, steps);
    if (!execute || plan.refusals.length) {
      return reportUnsent(report, plan.refusals, simulation, 'DRY RUN — increase built and simulated. Pass --execute to sign and send.');
    }
    const bad = blockingFailure(simulation);
    if (bad) throw new Error(`refused: simulation of ${bad.label} reverts: ${bad.revert}`);
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    // what went in, from the receipt's IncreaseLiquidity event: the program's amounts, not the plan's
    const extra = {};
    for (const log of r.sent.find(st => st.label === 'increase')?.receipt?.logs ?? []) {
      if (!same(log.address, info.npm)) continue;
      try {
        const ev = decodeEventLog({ abi: U.NPM_ABI, data: log.data, topics: log.topics });
        if (ev.eventName === 'IncreaseLiquidity') {
          extra.depositA = M.toHuman(ev.args.amount0, info.decimalsA); extra.depositB = M.toHuman(ev.args.amount1, info.decimalsB);
          extra.liquidityAdded = ev.args.liquidity.toString();
          if (info.quoteUsd > 0) extra.depositUsd = Number(((extra.depositA * info.price + extra.depositB) * info.quoteUsd).toFixed(4));
        }
      } catch { /* another event */ }
    }
    return reportSent(report, r, extra);
  });
}

// --- harvest and close ----------------------------------------------------------------
function collectData(tokenId, me) {
  return encodeFunctionData({ abi: U.NPM_ABI, functionName: 'collect', args: [{ tokenId, recipient: me, amount0Max: M.MAX_UINT128, amount1Max: M.MAX_UINT128 }] });
}

// What a receipt's NPM Collect event paid, in human units; {} when it carries none.
export function collected(receipt, info) {
  for (const log of receipt?.logs ?? []) {
    if (!same(log.address, info.npm)) continue;
    try {
      const ev = decodeEventLog({ abi: U.NPM_ABI, data: log.data, topics: log.topics });
      if (ev.eventName === 'Collect') return { amountA: M.toHuman(ev.args.amount0, info.decimalsA), amountB: M.toHuman(ev.args.amount1, info.decimalsB) };
    } catch { /* another event */ }
  }
  return {};
}

async function harvest(tokenArg, execute) {
  guard();
  const pool = poolArg();
  const cfg = settings();
  return withRpc(async (ctx) => {
    const { pub, me } = ctx;
    const info = await describe(pub, pool);
    const x = await ownedPosition(pub, me, info, tokenArg);
    const [f0, f1] = await feesOwed(pub, me, info, x.tokenId);
    const steps = [{ label: 'collect', to: info.npm, data: collectData(x.tokenId, me) }];
    const report = { mint: x.tokenId.toString(), pool, dex: U.DEX,
      feesA: M.toHuman(f0, info.decimalsA), feesB: M.toHuman(f1, info.decimalsB), transactions: 1 };
    if (!execute) return reportUnsent(report, [], await simulateSequence(pub, me, steps), 'DRY RUN — pass --execute to collect fees.');
    if (f0 === 0n && f1 === 0n) {
      const out = { harvested: x.tokenId.toString(), signature: null, note: 'nothing to claim' };
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    return reportSent({ harvested: x.tokenId.toString(), pool, dex: U.DEX }, r, collected(r.sent[0]?.receipt, info));
  });
}

// decreaseLiquidity (all of it, amountMin at slippage) + collect + burn, in ONE NPM
// multicall: one transaction, so a close is never left half done.
export function closeCalls(x, info, me, cfg, dl) {
  const sa = M.sqrtRatioAtTick(x.tickLower), sb = M.sqrtRatioAtTick(x.tickUpper);
  const [e0, e1] = M.amountsForLiquidity(BigInt(info.sqrtPriceX96), sa, sb, x.liquidity, false);
  const calls = [];
  if (x.liquidity > 0n) {
    calls.push(encodeFunctionData({ abi: U.NPM_ABI, functionName: 'decreaseLiquidity', args: [{
      tokenId: x.tokenId, liquidity: x.liquidity,
      amount0Min: M.minWithSlippage(e0, cfg.slippageBps), amount1Min: M.minWithSlippage(e1, cfg.slippageBps), deadline: dl,
    }] }));
  }
  calls.push(collectData(x.tokenId, me));
  calls.push(encodeFunctionData({ abi: U.NPM_ABI, functionName: 'burn', args: [x.tokenId] }));
  return { calls, estA: e0, estB: e1 };
}

async function close(tokenArg, execute) {
  guard();
  const pool = poolArg();
  const cfg = settings();
  return withRpc(async (ctx) => {
    const { pub, me } = ctx;
    const info = await describe(pub, pool);
    const x = await ownedPosition(pub, me, info, tokenArg);
    const [f0, f1] = await feesOwed(pub, me, info, x.tokenId);
    const { calls, estA, estB } = closeCalls(x, info, me, cfg, await deadline(pub));
    const steps = [{ label: 'decrease+collect+burn', to: info.npm, data: encodeFunctionData({ abi: U.NPM_ABI, functionName: 'multicall', args: [calls] }) }];
    const report = { mint: x.tokenId.toString(), pool, dex: U.DEX, transactions: 1, instructions: calls.length,
      quote: { tokenEstA: estA.toString(), tokenEstB: estB.toString() },
      feesQuote: { feeOwedA: f0.toString(), feeOwedB: f1.toString() }, slippageBps: cfg.slippageBps };
    if (!execute) return reportUnsent(report, [], await simulateSequence(pub, me, steps), 'DRY RUN — close built and simulated. Pass --execute to send.');
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    return reportSent({ closed: x.tokenId.toString(), pool, dex: U.DEX }, r, collected(r.sent[0]?.receipt, info));
  });
}

// --- swaps: the held v3 pool or a hookless v4 pool, whichever quotes more ----------------
// The v4 pools of the chain module that trade exactly this pair. Pure.
export function v4PoolsFor(tokA, tokB, registry = U.V4_POOLS) {
  return registry.filter(p => (same(p.currency0, tokA) && same(p.currency1, tokB)) || (same(p.currency0, tokB) && same(p.currency1, tokA)))
    .filter(p => same(p.hooks, ZERO));
}

// Universal Router calldata for one exact-input swap in a v4 pool: V4_SWAP with the
// actions SWAP_EXACT_IN_SINGLE, SETTLE_ALL (pay tokIn, at most amountIn), TAKE_ALL
// (receive tokOut, at least minOut). Pure.
export function v4SwapCalldata(pool, tokIn, amountIn, minOut, dl) {
  const zeroForOne = same(tokIn, pool.currency0);
  const tokOut = zeroForOne ? pool.currency1 : pool.currency0;
  const key = { currency0: pool.currency0, currency1: pool.currency1, fee: pool.fee, tickSpacing: pool.tickSpacing, hooks: pool.hooks };
  const swap = encodeAbiParameters(
    [{ type: 'tuple', components: [
      { name: 'poolKey', type: 'tuple', components: [{ name: 'currency0', type: 'address' }, { name: 'currency1', type: 'address' }, { name: 'fee', type: 'uint24' }, { name: 'tickSpacing', type: 'int24' }, { name: 'hooks', type: 'address' }] },
      { name: 'zeroForOne', type: 'bool' }, { name: 'amountIn', type: 'uint128' }, { name: 'amountOutMinimum', type: 'uint128' }, { name: 'hookData', type: 'bytes' }] }],
    [{ poolKey: key, zeroForOne, amountIn, amountOutMinimum: minOut, hookData: '0x' }]);
  const settle = encodeAbiParameters([{ type: 'address' }, { type: 'uint256' }], [tokIn, amountIn]);
  const take = encodeAbiParameters([{ type: 'address' }, { type: 'uint256' }], [tokOut, minOut]);
  const actions = `0x${[U.ACT_SWAP_EXACT_IN_SINGLE, U.ACT_SETTLE_ALL, U.ACT_TAKE_ALL].map(x => x.toString(16).padStart(2, '0')).join('')}`;
  const input = encodeAbiParameters([{ type: 'bytes' }, { type: 'bytes[]' }], [actions, [swap, settle, take]]);
  return encodeFunctionData({ abi: U.UNIVERSAL_ROUTER_ABI, functionName: 'execute', args: [`0x${U.CMD_V4_SWAP.toString(16).padStart(2, '0')}`, [input], dl] });
}

// Quotes for selling `rawIn` of tokIn: the held v3 pool, and each v4 pool of the pair.
// A route whose quote fails is left out. Best (most out) first.
async function quotes(pub, me, info, tokIn, tokOut, rawIn) {
  const out = [];
  try {
    const { result } = await pub.simulateContract({ account: me, address: info.quoter, abi: U.QUOTER_ABI, functionName: 'quoteExactInputSingle',
      args: [{ tokenIn: tokIn, tokenOut: tokOut, amountIn: rawIn, fee: info.feePips, sqrtPriceLimitX96: 0n }] });
    out.push({ kind: 'v3', label: `uniswap-v3 ${info.feePips / 1e4}%`, pool: info.pool, feePips: info.feePips, amountOut: result[0] });
  } catch (e) { if (evmErrorKind(e) === 'rotate') throw e; }
  for (const p of v4PoolsFor(tokIn, tokOut)) {
    try {
      const { result } = await pub.simulateContract({ account: me, address: U.V4.quoter, abi: U.V4_QUOTER_ABI, functionName: 'quoteExactInputSingle',
        args: [{ poolKey: { currency0: p.currency0, currency1: p.currency1, fee: p.fee, tickSpacing: p.tickSpacing, hooks: p.hooks },
          zeroForOne: same(tokIn, p.currency0), exactAmount: rawIn, hookData: '0x' }] });
      out.push({ kind: 'v4', label: `uniswap-v4 ${p.fee / 1e4}%`, pool: p.id, feePips: p.fee, amountOut: result[0], v4: p });
    } catch (e) { if (evmErrorKind(e) === 'rotate') throw e; }
  }
  return out.sort((a, b) => (a.amountOut > b.amountOut ? -1 : a.amountOut < b.amountOut ? 1 : 0));
}

// The transactions of one swap on `route`: exact approvals, then the swap.
async function swapSteps(pub, me, route, tokIn, tokOut, rawIn, minOut) {
  if (route.kind === 'v3') {
    return [
      ...(await approveSteps(pub, me, tokIn, rawIn, U.V3.router)),
      { label: 'swap', to: U.V3.router, data: encodeFunctionData({ abi: U.ROUTER_ABI, functionName: 'exactInputSingle', args: [{
        tokenIn: tokIn, tokenOut: tokOut, fee: route.feePips, recipient: me, amountIn: rawIn, amountOutMinimum: minOut, sqrtPriceLimitX96: 0n,
      }] }) },
    ];
  }
  const steps = [...(await approveSteps(pub, me, tokIn, rawIn, U.V4.permit2))];
  const [amount, expiration] = await pub.readContract({ address: U.V4.permit2, abi: U.PERMIT2_ABI, functionName: 'allowance', args: [me, tokIn, U.V4.universalRouter] });
  const now = Math.floor(Date.now() / 1000);
  if (amount < rawIn || Number(expiration) < now + 120) {
    steps.push({ label: `permit2 ${tokIn}`, to: U.V4.permit2, data: encodeFunctionData({ abi: U.PERMIT2_ABI, functionName: 'approve',
      args: [tokIn, U.V4.universalRouter, rawIn, now + PERMIT_EXPIRY_S] }) });
  }
  steps.push({ label: 'swap', to: U.V4.universalRouter, data: v4SwapCalldata(route.v4, tokIn, rawIn, minOut, await deadline(pub)) });
  return steps;
}

// What arrived of tokOut in a receipt: the Transfer logs of tokOut to this wallet.
function received(receipt, tokOut, me) {
  let got = 0n;
  for (const log of receipt?.logs ?? []) {
    if (!same(log.address, tokOut)) continue;
    try {
      const ev = decodeEventLog({ abi: U.ERC20_ABI, data: log.data, topics: log.topics });
      if (ev.eventName === 'Transfer' && same(ev.args.to, me)) got += ev.args.value;
    } catch { /* another event */ }
  }
  return got;
}

// --- rebalance: one swap toward the targets --------------------------------------------
async function rebalance(mintA, mintB, targetA, targetB, execute) {
  guard();
  const pool = poolArg();
  const cfg = settings();
  const sleeve = M.parseSleeve(cfg.sleeve);
  const tA = Number(targetA), tB = Number(targetB);
  if (!(tA >= 0 && tB >= 0)) throw new Error(`targets must be non-negative dollars, got ${targetA} ${targetB}`);
  return withRpc(async (ctx) => {
    const { pub, me } = ctx;
    const info = await describe(pub, pool);
    if (!isAddress(String(mintA), { strict: false }) || !isAddress(String(mintB), { strict: false })
        || !same(mintA, info.mintA) || !same(mintB, info.mintB)) {
      throw new Error(`refused: rebalance mints must be the pool's ${info.mintA} ${info.mintB}`);
    }
    const h = await holdings(pub, me, info);
    const can = spendable(h, info, sleeve);
    const usdPriceA = info.price * info.quoteUsd, usdPriceB = info.quoteUsd;
    const humA = M.toHuman(can.a, info.decimalsA), humB = M.toHuman(can.b, info.decimalsB);
    const before = {
      owner: me, gasReserveNative: M.toHuman(cfg.gasReserve, 18),
      A: { mint: info.mintA, symbol: info.symbolA, amount: M.toHuman(h.rawA, info.decimalsA), sellable: humA, usd: Number((humA * usdPriceA).toFixed(4)), usdPrice: usdPriceA },
      B: { mint: info.mintB, symbol: info.symbolB, amount: M.toHuman(h.rawB, info.decimalsB), sellable: humB, usd: Number((humB * usdPriceB).toFixed(4)), usdPrice: usdPriceB },
      targetUsdA: tA, targetUsdB: tB,
    };
    const refusals = marketRefusals(info, cfg);
    if (refusals.length) throw new Error(`refused: ${refusals.join('; ')}`);
    const plan = planRebalance(humA * usdPriceA, humB * usdPriceB, tA, tB);
    if (!plan) {
      const out = { noop: true, reason: `both sides within ${TARGET_TOLERANCE * 100}% of target or nothing to sell`, ...before };
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    const sellA = plan.sellSide === 'A';
    const [tokIn, tokOut] = sellA ? [info.mintA, info.mintB] : [info.mintB, info.mintA];
    const [decIn, decOut] = sellA ? [info.decimalsA, info.decimalsB] : [info.decimalsB, info.decimalsA];
    const [symIn, symOut] = sellA ? [info.symbolA, info.symbolB] : [info.symbolB, info.symbolA];
    const [pxIn, pxOut] = sellA ? [usdPriceA, usdPriceB] : [usdPriceB, usdPriceA];
    let rawIn = M.rawFromFloat(plan.sellUsd / pxIn, decIn);
    rawIn = M.capped(rawIn, sellA ? can.a : can.b);
    if (rawIn === 0n) {
      const out = { noop: true, reason: 'the amount to sell rounds to zero', ...before };
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    const amountIn = M.toHuman(rawIn, decIn);
    const sellUsd = amountIn * pxIn;
    if (sellUsd > cfg.maxUsd) throw new Error(`refused: swap of $${sellUsd.toFixed(2)} exceeds cap $${cfg.maxUsd}`);
    const routes = await quotes(pub, me, info, tokIn, tokOut, rawIn);
    if (!routes.length) throw new Error('refused: no route quotes this swap');
    const route = routes[0];
    const quoteOut = M.toHuman(route.amountOut, decOut);
    // Impact against the pool's spot price, fee included: what the swap costs beyond mid.
    const impact = 1 - (quoteOut * pxOut) / sellUsd;
    if (impact > cfg.maxImpact) throw new Error(`refused: price impact ${(impact * 100).toFixed(3)}% exceeds ${(cfg.maxImpact * 100).toFixed(2)}%`);
    const minOut = M.minWithSlippage(route.amountOut, cfg.slippageBps);
    const steps = await swapSteps(pub, me, route, tokIn, tokOut, rawIn, minOut);
    const report = {
      mode: plan.mode, sellSide: plan.sellSide, buySide: plan.buySide,
      sellUsdPlanned: Number(plan.sellUsd.toFixed(4)),
      desiredUsdA: Number(plan.desiredA.toFixed(4)), desiredUsdB: Number(plan.desiredB.toFixed(4)),
      before,
      sold: { mint: tokIn, symbol: symIn, amount: amountIn, usd: Number(sellUsd.toFixed(4)) },
      bought: { mint: tokOut, symbol: symOut, amount: quoteOut, usd: Number((quoteOut * pxOut).toFixed(4)) },
      quoteOutAmount: quoteOut, minOutAmount: M.toHuman(minOut, decOut),
      priceImpactPct: Number(impact.toFixed(6)), priceImpactPercent: Number((impact * 100).toFixed(4)),
      slippageBps: cfg.slippageBps,
      routePlan: [{ label: route.label, pool: route.pool, feePips: route.feePips, percent: 100 }],
      routesQuoted: routes.map(r => ({ label: r.label, amountOut: M.toHuman(r.amountOut, decOut) })),
      swapUsdValue: Number(sellUsd.toFixed(4)),
      transaction: { steps: steps.map(s => s.label) },
    };
    const swapRefusals = (sellA ? h.rawA : h.rawB) >= rawIn ? [] : [`wallet lacks ${amountIn} ${symIn} to sell`];
    const simulation = await simulateSequence(pub, me, steps);
    if (!execute || swapRefusals.length) {
      return reportUnsent(report, swapRefusals, simulation, 'DRY RUN — quote taken and swap built and simulated. Pass --execute to sign and send.');
    }
    const bad = blockingFailure(simulation);
    if (bad) throw new Error(`refused: simulation of ${bad.label} reverts: ${bad.revert}`);
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    const swapped = r.sent.find(s => s.label === 'swap' && s.receipt);
    if (swapped) {
      const got = M.toHuman(received(swapped.receipt, tokOut, me), decOut);
      report.bought = { ...report.bought, amount: got, usd: Number((got * pxOut).toFixed(4)) };
    }
    return reportSent(report, r);
  });
}

// --- payouts: payout.mjs's shape, to the pinned profit wallet only --------------------
// The recipient must equal LPBOT_EVM_PROFIT_WALLET_PIN (service environment, not the
// database) and, when set, LPBOT_PROFIT_WALLET. Pure, so every refusal is testable.
export function checkRecipient(to, cfg, me) {
  if (!cfg.pin) throw new Error('refused: LPBOT_EVM_PROFIT_WALLET_PIN is not set; refusing to send');
  if (!isAddress(cfg.pin, { strict: false })) throw new Error('refused: LPBOT_EVM_PROFIT_WALLET_PIN is not an address');
  if (!isAddress(String(to ?? ''), { strict: false })) throw new Error(`refused: destination ${to} is not an address`);
  if (!same(to, cfg.pin) || (cfg.profit && !same(to, cfg.profit))) {
    throw new Error('refused: destination is not the pinned profit wallet; refusing to send');
  }
  if (me && same(to, me)) throw new Error('refused: profit wallet equals the LP wallet; nothing to do');
  return getAddress(to);
}

const NATIVE = '0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE';
export function isNative(token) {
  return String(token).toUpperCase() === U.NATIVE_SYMBOL || same(token, NATIVE);
}

async function tokenFacts(pub, token) {
  if (isNative(token)) return { mint: NATIVE, symbol: U.NATIVE_SYMBOL, decimals: 18 };
  if (!isAddress(String(token), { strict: false })) throw new Error(`token ${token} is not an address`);
  const t = getAddress(token);
  const [decimals, symbol] = await pub.multicall({ allowFailure: false, contracts: [
    { address: t, abi: U.ERC20_ABI, functionName: 'decimals' }, { address: t, abi: U.ERC20_ABI, functionName: 'symbol' }] });
  return { mint: t, symbol, decimals: Number(decimals) };
}

async function rawOf(pub, me, t) {
  return t.mint === NATIVE ? pub.getBalance({ address: me })
    : pub.readContract({ address: t.mint, abi: U.ERC20_ABI, functionName: 'balanceOf', args: [me] });
}

async function send(token, amountArg, toArg, execute) {
  guard();
  const cfg = settings();
  const sleeve = M.parseSleeve(cfg.sleeve);
  // The destination is checked before any key or network: a bad argument goes nowhere.
  const to = checkRecipient(toArg, cfg);
  const amount = Number(amountArg);
  if (!Number.isFinite(amount) || amount <= 0) throw new Error(`amount must be a positive number, got ${amountArg}`);
  return withRpc(async (ctx) => {
    const { pub, me } = ctx;
    checkRecipient(to, cfg, me);
    const t = await tokenFacts(pub, token);
    const raw = M.toRaw(String(amountArg), t.decimals);
    if (raw <= 0n) throw new Error('amount rounds to zero');
    const cap = M.sleeveCap(sleeve, t.mint === NATIVE ? U.WRAPPED_NATIVE : t.mint, t.decimals);
    if (cap != null && raw > cap) throw new Error(`refused: ${amount} ${t.symbol} exceeds this profile's sleeve of ${M.toHuman(cap, t.decimals)}`);
    const have = await rawOf(pub, me, t);
    if (t.mint === NATIVE) {
      if (have < raw + cfg.gasReserve) throw new Error(`refused: sending ${amount} ${U.NATIVE_SYMBOL} would leave ${M.toHuman(have - raw, 18)} ${U.NATIVE_SYMBOL}, below the ${M.toHuman(cfg.gasReserve, 18)} gas reserve`);
    } else if (have < raw) {
      throw new Error(`LP wallet holds ${M.toHuman(have, t.decimals)}, less than ${amount}`);
    }
    const step = t.mint === NATIVE
      ? { label: `send ${U.NATIVE_SYMBOL}`, to, data: '0x', value: raw }
      : { label: `send ${t.symbol}`, to: t.mint, data: erc20(t.mint, 'transfer', [to, raw]) };
    const report = { mint: t.mint, symbol: t.symbol, amount: M.toHuman(raw, t.decimals), raw: raw.toString(), decimals: t.decimals, from: me, to };
    if (!execute) {
      const [sim] = await simulateSequence(pub, me, [step]);
      const out = { ...report, simulation: { ok: sim.ok, err: sim.revert }, signature: null, sent: false };
      console.log(JSON.stringify(out, null, 1));
      console.log('DRY RUN — transfer built and simulated. Pass --execute to sign and send.');
      return out;
    }
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, [step], cfg);
    const out = { ...report, signature: r.sent[0]?.hash ?? null, sent: true };
    if (r.error) { out.partial = true; out.error = r.error; process.exitCode = 1; }
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// Native coin into its wrapped token (POL -> WPOL on Polygon), so a deposit of native coin
// can be deployed (owner, 2026-10-08: "let 10 POL for gas and the rest swap to pool"). Only
// where the wrapped token is one of the pool's: the loop's wrap_native checks it; here the
// refusal is the gas reserve: the native left after the wrap must cover it. One transaction,
// the wrapped token's own deposit().
export function planWrap(native, amountArg, cfg) {
  const amount = Number(amountArg);
  if (!Number.isFinite(amount) || amount <= 0) throw new Error(`amount must be a positive number, got ${amountArg}`);
  const raw = M.toRaw(String(amountArg), 18);
  if (raw <= 0n) throw new Error('amount rounds to zero');
  const refusals = [];
  if (native - raw < cfg.gasReserve) {
    refusals.push(`wrapping ${amount} ${U.NATIVE_SYMBOL} would leave ${M.toHuman(native - raw, 18)}, below the ${M.toHuman(cfg.gasReserve, 18)} gas reserve`);
  }
  return { raw, refusals };
}

async function wrap(amountArg, execute) {
  guard();
  const cfg = settings();
  if (!U.WRAPPED_NATIVE) throw new Error(`${U.NAME} has no wrapped native token`);
  return withRpc(async (ctx) => {
    const { pub, me } = ctx;
    const native = await pub.getBalance({ address: me });
    const plan = planWrap(native, amountArg, cfg);
    const step = { label: `wrap ${U.NATIVE_SYMBOL}`, to: U.WRAPPED_NATIVE,
      data: encodeFunctionData({ abi: U.WRAPPED_ABI, functionName: 'deposit' }), value: plan.raw };
    const report = { wrapped: M.toHuman(plan.raw, 18), symbol: U.NATIVE_SYMBOL, to: U.WRAPPED_NATIVE, chain: U.CHAIN,
      nativeBefore: M.toHuman(native, 18), nativeAfter: M.toHuman(native - plan.raw, 18), transactions: 1 };
    const simulation = await simulateSequence(pub, me, [step]);
    if (!execute || plan.refusals.length) {
      return reportUnsent(report, plan.refusals, simulation, 'DRY RUN — wrap built and simulated. Pass --execute to sign and send.');
    }
    const bad = blockingFailure(simulation);
    if (bad) throw new Error(`refused: simulation of ${bad.label} reverts: ${bad.revert}`);
    ctx.nonce = await nonceFor(pub, me);
    return reportSent(report, await runSteps(ctx, [step], cfg));
  });
}

// `balance <token>` in payout.mjs's shape. Read-only.
async function tokenBalance(token) {
  return withRpc(async ({ pub, me }) => {
    const t = await tokenFacts(pub, token);
    const raw = await rawOf(pub, me, t);
    const out = { mint: t.mint, symbol: t.symbol, amount: M.toHuman(raw, t.decimals), raw: raw.toString(), decimals: t.decimals };
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// `balance X`: the pool view when X is a pool (or absent), the token view otherwise.
async function balanceCmd(arg) {
  if (arg == null) return balance();
  if (isNative(arg)) return tokenBalance(arg);
  if (!isAddress(String(arg), { strict: false })) throw new Error(`balance: ${arg} is not an address`);
  const pin = process.env.LPBOT_POOL;
  if (pin && same(pin, arg)) return balance(arg);
  const isPool = await withRpc(async ({ pub }) => {
    try { await pub.readContract({ address: getAddress(arg), abi: U.POOL_ABI, functionName: 'fee' }); return true; }
    catch (e) { if (e?.name === 'ContractFunctionExecutionError' && /revert|returned no data/i.test(String(e.message))) return false; throw e; }
  });
  return isPool ? balance(arg) : tokenBalance(arg);
}

async function poolCmd(arg) {
  const pool = poolArg(arg);
  return withRpc(async ({ pub }) => {
    const out = await describe(pub, pool);
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

export function parseArgs(argv) {
  const execute = argv.includes('--execute');
  const pi = argv.indexOf('--pool');
  const rest = argv.filter((x, i) => x !== '--execute' && x !== '--pool' && (pi < 0 || i !== pi + 1));
  return { execute, cmd: rest[0], args: rest.slice(1) };
}

async function main() {
  const { execute, cmd, args: r } = parseArgs(process.argv.slice(2));
  if (cmd === 'balance') return balanceCmd(r[0]);
  if (cmd === 'positions') return positions();
  if (cmd === 'status') return status(r[0]);
  if (cmd === 'pool') return poolCmd(r[0]);
  if (cmd === 'open') {
    if (r.length < 5) throw new Error('usage: open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]');
    return open(r[0], r[1], r[2], r[3], r[4], execute);
  }
  if (cmd === 'increase') {
    if (r.length < 3) throw new Error('usage: increase <tokenId> <maxA> <maxB> [--execute]');
    return increase(r[0], r[1], r[2], execute);
  }
  if (cmd === 'harvest') return harvest(r[0], execute);
  if (cmd === 'close') return close(r[0], execute);
  if (cmd === 'rebalance') {
    if (r.length < 4) throw new Error('usage: rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute]');
    return rebalance(r[0], r[1], r[2], r[3], execute);
  }
  if (cmd === 'wrap') {
    if (r.length < 1) throw new Error('usage: wrap <amount> [--execute]');
    return wrap(r[0], execute);
  }
  if (cmd === 'send') {
    if (r.length < 3) throw new Error('usage: send <token> <amount> <to> [--execute]');
    return send(r[0], r[1], r[2], execute);
  }
  console.log('commands: balance [pool|token] | positions | status [tokenId] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | increase <tokenId> <maxA> <maxB> [--execute] | harvest <tokenId> [--execute] | close <tokenId> [--execute] | '
    + 'rebalance <mintA> <mintB> <usdA> <usdB> [--execute] | send <token> <amount> <to> [--execute] | wrap <amount> [--execute]   (pool via --pool or LPBOT_POOL)');
}

if (isEntry(import.meta.url)) {
  main().catch(e => {
    // viem messages run to many lines (request bodies, docs links): the first line only.
    console.error('ERROR:', String(e?.shortMessage ?? e?.message ?? e).split('\n')[0].replace(/[{}]/g, ' '));
    process.exitCode = 1;
  });
}
