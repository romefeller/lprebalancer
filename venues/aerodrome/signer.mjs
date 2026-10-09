// Rebalancer — the Aerodrome Slipstream signing layer on Base. Same commands and output
// fields as the Solana signers (SIGNER_CONTRACT.md), so the loop cannot tell which chain it
// is on, plus the EVM additions of MULTI_DESIGN.md: `rebalance` (the venues/jupiter/swap.mjs
// shape, swapping through the pool's own router) and `send` / `balance <token>` (the
// chains/solana/payout.mjs shape, to the pinned profit wallet only).
//
// A Slipstream position is an ERC-721 from the NonfungiblePositionManager (NPM). Aerodrome
// runs several Slipstream deployments; the pool names its own (factory, NPM), and every
// call goes to the NPM, router and quoter of that deployment in chains/evm/base_addresses.mjs
// (DEPLOYMENTS). A pool of an unknown deployment is refused. The signer reads, opens,
// harvests and closes only NFTs of THIS wallet on THIS pool, and only unstaked ones: a
// position staked in the gauge earns AERO instead of fees and is out of scope. Unstaked
// liquidity pays the pool's `unstakedFee` (50000 pips = 5% of its swap fees on 2026-10-01)
// to the gauge; `pool` and `status` report it.
//
// Native ETH counts as WETH (`nativeSide`). An open or a swap wraps exactly the WETH it
// lacks, never taking ETH below LPBOT_GAS_RESERVE_NATIVE. Approvals are exact (the amount
// the next call pulls), never infinite.
//
// Every write: HALT check, pool genuineness (a known deployment's factory and NPM, getPool,
// its NPM, router and quoter naming the same factory), the Chainlink ETH/USD
// cross-check of the pool price, a refusal list, the whole sequence simulated in one
// eth_simulateV1 call, then per transaction: HALT check, eth_call, estimateGas, sign, send,
// wait for the receipt, check its status. A nonce comes from the chain once per command; a
// pending transaction from this wallet refuses the command. After the first send nothing
// is retried: a failure part-way is printed with `sent: true, partial: true` and every hash.
//
// The key is read from WALLET_SECRET_PATH inside this process (chains/evm/keyfile.mjs) and never
// printed. Commands:
//   node venues/aerodrome/signer.mjs balance [pool]          | balance <token>   (chains/solana/payout.mjs shape)
//   node venues/aerodrome/signer.mjs positions
//   node venues/aerodrome/signer.mjs status [tokenId]
//   node venues/aerodrome/signer.mjs pool [pool]
//   node venues/aerodrome/signer.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node venues/aerodrome/signer.mjs harvest <tokenId> [--execute]
//   node venues/aerodrome/signer.mjs close <tokenId> [--execute]
//   node venues/aerodrome/signer.mjs rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute]
//   node venues/aerodrome/signer.mjs send <token> <amount> <to> [--execute]
import fs from 'node:fs';
import path from 'node:path';
import { assertNotHalted } from '../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../bot_root.mjs';
import { createPublicClient, http, getAddress, isAddress, encodeFunctionData, decodeEventLog, keccak256 } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { base } from 'viem/chains';
import { isEntry, AfterSignError } from '../../shared/rpc_policy.mjs';
import { planRebalance } from '../../shared/rebalance_plan.mjs';
import { readKey } from '../../chains/evm/keyfile.mjs';
import { baseEndpoints, overBase, evmErrorKind } from '../../chains/evm/rpc.mjs';
import * as A from '../../chains/evm/base_addresses.mjs';
import * as M from '../../chains/evm/clmath.mjs';

const DEX = 'aerodrome-slipstream';
const DEADLINE_S = 300;                 // past the latest block's timestamp
const RECEIPT_TIMEOUT_MS = 120_000;
const ORACLE_MAX_AGE_S = 3600;          // the feed's heartbeat is 1200 s
const TARGET_TOLERANCE = 0.02;          // as venues/jupiter/swap.mjs: "at target" = within 2%
const MAX_POSITIONS_SCANNED = 200;

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
    gasReserve: M.toRaw(num('LPBOT_GAS_RESERVE_NATIVE', 0.002), 18),
    maxImpact: num('LPBOT_MAX_IMPACT', 0.01),
    maxOracleDev: num('LPBOT_MAX_ORACLE_DEV', 0.02),
    maxFeeWei: M.toRaw(num('LPBOT_EVM_MAX_GWEI', 2), 9),
    pin: env.LPBOT_EVM_PROFIT_WALLET_PIN ?? '',
    profit: env.LPBOT_PROFIT_WALLET ?? '',
    sleeve: env.LPBOT_SLEEVE ?? '',
  };
}

// HALT in this script's directory halts every profile; LPBOT_RUN_DIR/HALT halts one.
export function guard(env = process.env) {
  assertNotHalted(BOT_ROOT, env);           // shared/halt_guard.mjs: one rule for every signer
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
  const pub = createPublicClient({ chain: base, transport: http(url, { retryCount: 1, timeout: 20_000 }) });
  const chainId = await pub.getChainId();
  if (chainId !== A.CHAIN_ID) throw new Error(`RPC ${new URL(url).host} serves chain ${chainId}, not Base (${A.CHAIN_ID}); refusing`);
  const account = privateKeyToAccount(readKey(process.env.WALLET_SECRET_PATH));
  return { pub, account, me: account.address, url };
}

export async function withRpc(fn, deps = {}) {
  const { urls = baseEndpoints(), connectFn = connect, sleep } = deps;
  return overBase(urls, async url => fn(await connectFn(url)), { sleep });
}

// --- the pool describes itself --------------------------------------------------------
// The deployment a pool belongs to: the registry entry whose factory is the pool's
// factory() AND whose NPM is the pool's nft(). Anything else is refused.
export function deploymentOf(pool, factory, nft, registry = A.DEPLOYMENTS) {
  const d = registry.find(x => same(x.factory, factory));
  if (!d) throw new Error(`refused: pool ${pool} belongs to factory ${factory}, not a known Slipstream deployment (${registry.map(x => x.factory).join(', ')})`);
  if (!same(nft, d.npm)) throw new Error(`refused: pool ${pool} names position manager ${nft}, not ${d.npm} of factory ${d.factory}`);
  return d;
}

// Refuses a pool that is not a genuine Slipstream pool of a known deployment: the factory
// must map (token0, token1, tickSpacing) back to the pool, and the deployment's NPM, router
// and quoter must each name that factory.
export async function describe(pub, pool) {
  const c = (functionName, args = []) => ({ address: pool, abi: A.POOL_ABI, functionName, args });
  const [factory, t0, t1, ts, fee, unstakedFee, liquidity, staked, gauge, nft, slot0] = await pub.multicall({
    allowFailure: false,
    contracts: [c('factory'), c('token0'), c('token1'), c('tickSpacing'), c('fee'), c('unstakedFee'),
      c('liquidity'), c('stakedLiquidity'), c('gauge'), c('nft'), c('slot0')],
  });
  const dep = deploymentOf(pool, factory, nft);
  const e = (address, functionName, args = []) => ({ address, abi: A.ERC20_ABI, functionName, args });
  const [real, npmFactory, routerFactory, quoterFactory, d0, d1, s0, s1, feeModule, baseFee, round, feedDec] = await pub.multicall({
    allowFailure: false,
    contracts: [
      { address: dep.factory, abi: A.FACTORY_ABI, functionName: 'getPool', args: [t0, t1, ts] },
      { address: dep.npm, abi: A.NPM_ABI, functionName: 'factory' },
      { address: dep.router, abi: A.ROUTER_ABI, functionName: 'factory' },
      { address: dep.quoter, abi: A.QUOTER_ABI, functionName: 'factory' },
      e(t0, 'decimals'), e(t1, 'decimals'), e(t0, 'symbol'), e(t1, 'symbol'),
      { address: dep.factory, abi: A.FACTORY_ABI, functionName: 'swapFeeModule' },
      { address: dep.factory, abi: A.FACTORY_ABI, functionName: 'tickSpacingToFee', args: [ts] },
      { address: A.ETH_USD_FEED, abi: A.FEED_ABI, functionName: 'latestRoundData' },
      { address: A.ETH_USD_FEED, abi: A.FEED_ABI, functionName: 'decimals' },
    ],
  });
  if (!same(real, pool)) throw new Error(`refused: factory ${dep.factory} maps (${t0}, ${t1}, ${ts}) to ${real}, not ${pool}`);
  for (const [what, addr, f] of [['position manager', dep.npm, npmFactory], ['router', dep.router, routerFactory], ['quoter', dep.quoter, quoterFactory]]) {
    if (!same(f, dep.factory)) throw new Error(`refused: ${what} ${addr} names factory ${f}, not ${dep.factory}`);
  }
  const decA = Number(d0), decB = Number(d1);
  const price = M.priceFromSqrtX96(slot0[0], decA, decB);
  const ethUsd = Number(round[1]) / 10 ** Number(feedDec);
  const oracleAgeS = Math.floor(Date.now() / 1000) - Number(round[3]);
  const stableB = A.STABLES.has(t1.toLowerCase()), stableA = A.STABLES.has(t0.toLowerCase());
  const quote = stableB ? { usd: 1, source: 'stable' } : same(t1, A.WETH) ? { usd: ethUsd, source: 'chainlink' } : { usd: null, source: 'unknown' };
  // The pool's own ETH price, when it is a WETH/stable pool: what the oracle checks.
  const poolEthUsd = same(t0, A.WETH) && stableB ? price : same(t1, A.WETH) && stableA ? 1 / price : null;
  return {
    pool, dex: DEX, chain: 'base', factory, nft, gauge,
    deployment: dep.name, npm: dep.npm, router: dep.router, quoter: dep.quoter,
    mintA: t0, mintB: t1, symbolA: s0, symbolB: s1, decimalsA: decA, decimalsB: decB,
    tickSpacing: Number(ts), tick: Number(slot0[1]), sqrtPriceX96: slot0[0].toString(), price,
    // SIGNER_CONTRACT's Token-2022 fields: ERC-20 amounts carry no UI multiplier
    uiPrice: price, multiplierA: 1, multiplierB: 1, paused: false,
    quoteUsd: quote.usd, quoteUsdSource: quote.source,
    nativeSide: same(t0, A.WETH) ? 'A' : same(t1, A.WETH) ? 'B' : null,
    feePips: Number(fee), fee: Number(fee) / 1e6, baseFeePips: Number(baseFee),
    dynamicFee: !same(feeModule, '0x0000000000000000000000000000000000000000'), swapFeeModule: feeModule,
    unstakedFeePips: Number(unstakedFee), unstakedFee: Number(unstakedFee) / 1e6,
    // what an unstaked LP keeps of each swap: fee × (1 − unstakedFee)
    lpFeeUnstaked: Number(fee) / 1e6 * (1 - Number(unstakedFee) / 1e6),
    liquidity: liquidity.toString(), stakedLiquidity: staked.toString(),
    stakedShare: liquidity > 0n ? Number(staked * 10000n / liquidity) / 10000 : null,
    ethUsd, oracleAgeS, poolEthUsd,
    oracleDeviation: poolEthUsd != null && ethUsd > 0 ? Math.abs(poolEthUsd / ethUsd - 1) : null,
  };
}

function sqrtP(info) { return BigInt(info.sqrtPriceX96); }

// Refusals every write shares: the price must agree with Chainlink before money moves at it.
export function marketRefusals(info, cfg) {
  const out = [];
  if (!(info.quoteUsd > 0)) out.push(`cannot price ${info.symbolB} in USD`);
  if (info.poolEthUsd != null) {
    if (info.oracleAgeS > ORACLE_MAX_AGE_S) out.push(`Chainlink ETH/USD is ${info.oracleAgeS}s old (limit ${ORACLE_MAX_AGE_S}s)`);
    else if (info.oracleDeviation > cfg.maxOracleDev) {
      out.push(`pool ETH price ${info.poolEthUsd.toFixed(2)} is ${(info.oracleDeviation * 100).toFixed(2)}% from Chainlink ${info.ethUsd.toFixed(2)} (limit ${(cfg.maxOracleDev * 100).toFixed(2)}%)`);
    }
  }
  return out;
}

// --- wallet reads ---------------------------------------------------------------------
async function holdings(pub, me, info) {
  const [eth, [b0, b1]] = await Promise.all([
    pub.getBalance({ address: me }),
    pub.multicall({ allowFailure: false, contracts: [info.mintA, info.mintB].map(t => ({ address: t, abi: A.ERC20_ABI, functionName: 'balanceOf', args: [me] })) }),
  ]);
  return { eth, rawA: b0, rawB: b1 };
}

// What a profile may spend of side A / B in raw units: WETH plus ETH above the reserve
// on the native side, capped by the sleeve.
export function spendable(h, info, cfg, sleeve) {
  const above = h.eth > cfg.gasReserve ? h.eth - cfg.gasReserve : 0n;
  const a = info.nativeSide === 'A' ? h.rawA + above : h.rawA;
  const b = info.nativeSide === 'B' ? h.rawB + above : h.rawB;
  return {
    a: M.capped(a, M.sleeveCap(sleeve, info.mintA, info.decimalsA)),
    b: M.capped(b, M.sleeveCap(sleeve, info.mintB, info.decimalsB)),
  };
}

async function balance(poolExplicit) {
  const pool = poolArg(poolExplicit);
  const cfg = settings();
  return withRpc(async ({ pub, me }) => {
    const info = await describe(pub, pool);
    const h = await holdings(pub, me, info);
    const ua = Number(h.rawA + (info.nativeSide === 'A' ? h.eth : 0n)) / 10 ** info.decimalsA;
    const ub = Number(h.rawB + (info.nativeSide === 'B' ? h.eth : 0n)) / 10 ** info.decimalsB;
    const ethHuman = Number(h.eth) / 1e18;
    const out = {
      owner: me, chain: 'base', sol: ethHuman, eth: ethHuman, pool, dex: DEX,
      tokenA: info.symbolA, tokenB: info.symbolB, mintA: info.mintA, mintB: info.mintB,
      price: info.price, uiPrice: info.uiPrice, multiplierA: 1, multiplierB: 1,
      quoteUsd: info.quoteUsd, nativeSide: info.nativeSide,
      balanceA: ua, balanceB: ub,
      wethBalance: Number(info.nativeSide === 'A' ? h.rawA : info.nativeSide === 'B' ? h.rawB : 0n) / 1e18,
      gasReserveNative: Number(cfg.gasReserve) / 1e18,
    };
    out.walletUsd = info.quoteUsd == null ? null
      : Number(((ua * info.price + ub) * info.quoteUsd + (info.nativeSide ? 0 : ethHuman * info.ethUsd)).toFixed(4));
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// --- positions ------------------------------------------------------------------------
// This wallet's NFTs on this pool: same tokens and tick spacing (the NPM keys pools by
// exactly that triple). NFTs with no liquidity and nothing owed are spent; they are left out.
export async function ownPositions(pub, me, info) {
  const n = Number(await pub.readContract({ address: info.npm, abi: A.NPM_ABI, functionName: 'balanceOf', args: [me] }));
  if (n > MAX_POSITIONS_SCANNED) throw new Error(`wallet holds ${n} position NFTs; refusing to scan more than ${MAX_POSITIONS_SCANNED}`);
  if (!n) return [];
  const ids = await pub.multicall({ allowFailure: false, contracts: Array.from({ length: n }, (_, i) => ({ address: info.npm, abi: A.NPM_ABI, functionName: 'tokenOfOwnerByIndex', args: [me, BigInt(i)] })) });
  const ps = await pub.multicall({ allowFailure: false, contracts: ids.map(id => ({ address: info.npm, abi: A.NPM_ABI, functionName: 'positions', args: [id] })) });
  return ids.map((id, i) => ({ id, p: ps[i] }))
    .filter(({ p }) => same(p[2], info.mintA) && same(p[3], info.mintB) && Number(p[4]) === info.tickSpacing)
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
      lowerPrice: r6(M.priceAtTick(x.tickLower, info.decimalsA, info.decimalsB)),
      upperPrice: r6(M.priceAtTick(x.tickUpper, info.decimalsA, info.decimalsB)),
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
    account: me, address: info.npm, abi: A.NPM_ABI, functionName: 'collect',
    args: [{ tokenId, recipient: me, amount0Max: M.MAX_UINT128, amount1Max: M.MAX_UINT128 }],
  });
  return result;
}

export function positionView(x, info, fees) {
  const sa = M.sqrtRatioAtTick(x.tickLower), sb = M.sqrtRatioAtTick(x.tickUpper);
  const [e0, e1] = M.amountsForLiquidity(sqrtP(info), sa, sb, x.liquidity, false);
  const estA = M.toHuman(e0, info.decimalsA), estB = M.toHuman(e1, info.decimalsB);
  const feeA = M.toHuman(fees[0], info.decimalsA), feeB = M.toHuman(fees[1], info.decimalsB);
  const id = x.tokenId.toString();
  const out = {
    positionMint: id, tokenId: id, whirlpool: info.pool, pool: info.pool, dex: DEX, chain: 'base',
    pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
    decimalsA: info.decimalsA, decimalsB: info.decimalsB,
    quoteUsd: info.quoteUsd, quoteUsdSource: info.quoteUsdSource,
    liquidity: x.liquidity.toString(), tickLower: x.tickLower, tickUpper: x.tickUpper,
    lowerPrice: r6(M.priceAtTick(x.tickLower, info.decimalsA, info.decimalsB)),
    upperPrice: r6(M.priceAtTick(x.tickUpper, info.decimalsA, info.decimalsB)),
    price: r6(info.price), uiPrice: r6(info.price), multiplierA: 1, multiplierB: 1,
    // a tick pool is in range for tickLower <= tick < tickUpper
    inRange: info.tick >= x.tickLower && info.tick < x.tickUpper,
    closeEstA: estA, closeEstB: estB,
    feesAccruedA: feeA, feesAccruedB: feeB,
    feesAccrued_quote: Number((feeA * info.price + feeB).toFixed(9)),
    // no rent on an EVM chain: the NFT holds nothing refundable
    rentSol: 0, rentUsd: 0, staked: false,
    unstakedFee: info.unstakedFee, feePips: info.feePips,
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
// left), then nothing is sent unless every call succeeded. Returns per-call results.
// A node refuses the whole batch when a call's value exceeds the balance; then each call
// is eth_call'ed alone (`sequence: false`), so a dry run still shows every revert.
export async function simulateSequence(pub, me, steps) {
  let block;
  try {
    [block] = await pub.simulateBlocks({
      blocks: [{ calls: steps.map(s => ({ from: me, to: s.to, data: s.data, value: s.value ?? 0n })) }],
    });
  } catch (e) {
    if (evmErrorKind(e) === 'rotate') throw e;
    const batch = revertText(e);
    const out = [];
    for (const s of steps) {
      try {
        await pub.call({ account: me, to: s.to, data: s.data, value: s.value ?? 0n });
        out.push({ label: s.label, ok: true, gasUsed: null, revert: null, sequence: false });
      } catch (err) {
        if (evmErrorKind(err) === 'rotate') throw err;
        out.push({ label: s.label, ok: false, gasUsed: null, revert: revertText(err), sequence: false, batchError: batch });
      }
    }
    return out;
  }
  return block.calls.map((c, i) => ({
    label: steps[i].label, ok: c.status === 'success', gasUsed: c.gasUsed?.toString() ?? null,
    revert: c.status === 'success' ? null : revertText(c.error), sequence: true,
  }));
}

// The revert reason, as specific as the error chain allows.
export function revertText(e) {
  for (let x = e, d = 0; x && d < 8; x = x.cause, d++) {
    if (x.reason) return String(x.reason);
    if (x.data?.errorName) return `${x.data.errorName}(${(x.data.args ?? []).join(', ')})`;
  }
  return String(e?.shortMessage ?? e?.message ?? e).split('\n')[0].slice(0, 200);
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
  if (eth < req.value + gas * fees.maxFeePerGas) throw new Error(`refused: ${Number(eth) / 1e18} ETH cannot pay ${step.label}`);
  const serialized = await account.signTransaction({
    chainId: A.CHAIN_ID, type: 'eip1559', nonce: ctx.nonce, to: step.to, data: step.data, value: req.value,
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
      const receipt = await ctx.pub.waitForTransactionReceipt({ hash, timeout: RECEIPT_TIMEOUT_MS, pollingInterval: 2000 });
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
  return Number(wei) / 1e18;
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

const erc20 = (token, functionName, args) => encodeFunctionData({ abi: A.ERC20_ABI, functionName, args });

// The steps that make `need` of `token` spendable by `spender`: wrap the WETH shortfall
// from ETH, approve exactly `need` when the allowance is short.
async function fundSteps(pub, me, token, need, spender, plan) {
  const steps = [];
  if (same(token, A.WETH) && plan.wrap > 0n) {
    steps.push({ label: `wrap ${Number(plan.wrap) / 1e18} ETH`, to: A.WETH, data: encodeFunctionData({ abi: A.WETH_ABI, functionName: 'deposit' }), value: plan.wrap });
  }
  if (need > 0n) {
    const allowance = await pub.readContract({ address: token, abi: A.ERC20_ABI, functionName: 'allowance', args: [me, spender] });
    if (allowance < need) steps.push({ label: `approve ${token}`, to: token, data: erc20(token, 'approve', [spender, need]) });
  }
  return steps;
}

function wrapFor(side, info, need, h, cfg) {
  if (info.nativeSide !== side) return { wrap: 0n, ok: (side === 'A' ? h.rawA : h.rawB) >= need, spendable: 0n };
  return M.wrapPlan(need, side === 'A' ? h.rawA : h.rawB, h.eth, cfg.gasReserve);
}

// --- open -----------------------------------------------------------------------------
// The open as data: ticks, deposit, refusals. Pure given the chain reads, so the money
// arithmetic is testable without a node.
export function planOpen(info, h, cfg, sleeve, lower, upper, maxA, maxB) {
  const refusals = [...marketRefusals(info, cfg)];
  const { tickLower, tickUpper } = M.bandTicks(lower, upper, info.tickSpacing, info.decimalsA, info.decimalsB);
  if (!(info.tick >= tickLower && info.tick < tickUpper)) {
    refusals.push(`price ${info.price.toFixed(6)} (tick ${info.tick}) is outside the band ticks ${tickLower}..${tickUpper}`);
  }
  if (h.eth < cfg.gasReserve) refusals.push(`ETH ${Number(h.eth) / 1e18} is below the ${Number(cfg.gasReserve) / 1e18} gas reserve`);
  const capA = M.capped(M.toRaw(maxA, info.decimalsA), M.sleeveCap(sleeve, info.mintA, info.decimalsA));
  const capB = M.capped(M.toRaw(maxB, info.decimalsB), M.sleeveCap(sleeve, info.mintB, info.decimalsB));
  const sa = M.sqrtRatioAtTick(tickLower), sb = M.sqrtRatioAtTick(tickUpper);
  const dep = M.depositFor(sqrtP(info), sa, sb, capA, capB);
  const amtA = M.toHuman(dep.amountA, info.decimalsA), amtB = M.toHuman(dep.amountB, info.decimalsB);
  const approxUsd = (amtA * info.price + amtB) * (info.quoteUsd > 0 ? info.quoteUsd : NaN);
  // no liquidity is no deposit: a nonzero L in range costs at least a wei of each side
  if (dep.liquidity === 0n) refusals.push('nothing to deposit: the caps fund no liquidity');
  if (!(approxUsd <= cfg.maxUsd)) refusals.push(`position about $${Number.isFinite(approxUsd) ? approxUsd.toFixed(2) : '?'} exceeds cap $${cfg.maxUsd}`);
  const wrapA = wrapFor('A', info, dep.amountA, h, cfg), wrapB = wrapFor('B', info, dep.amountB, h, cfg);
  if (!wrapA.ok) refusals.push(`wallet lacks ${amtA} ${info.symbolA}` + (info.nativeSide === 'A' ? ' (WETH + ETH above the gas reserve)' : ''));
  if (!wrapB.ok) refusals.push(`wallet lacks ${amtB} ${info.symbolB}` + (info.nativeSide === 'B' ? ' (WETH + ETH above the gas reserve)' : ''));
  const minA = M.minWithSlippage(dep.amountA, cfg.slippageBps), minB = M.minWithSlippage(dep.amountB, cfg.slippageBps);
  return { refusals, tickLower, tickUpper, capA, capB, dep, amtA, amtB, approxUsd, wrapA, wrapB, minA, minB };
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
      ...(await fundSteps(pub, me, info.mintA, plan.dep.amountA, info.npm, plan.wrapA)),
      ...(await fundSteps(pub, me, info.mintB, plan.dep.amountB, info.npm, plan.wrapB)),
      { label: 'mint', to: info.npm, data: encodeFunctionData({ abi: A.NPM_ABI, functionName: 'mint', args: [{
        token0: info.mintA, token1: info.mintB, tickSpacing: info.tickSpacing,
        tickLower: plan.tickLower, tickUpper: plan.tickUpper,
        amount0Desired: plan.dep.amountA, amount1Desired: plan.dep.amountB,
        amount0Min: plan.minA, amount1Min: plan.minB, recipient: me, deadline: dl,
        sqrtPriceX96: 0n,                // nonzero would ask the factory to create a pool
      }] }) },
    ];
    const report = {
      pool, dex: DEX, chain: 'base', pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
      requestedLower: Number(lower), requestedUpper: Number(upper),
      lowerPrice: r6(M.priceAtTick(plan.tickLower, info.decimalsA, info.decimalsB)),
      upperPrice: r6(M.priceAtTick(plan.tickUpper, info.decimalsA, info.decimalsB)),
      tickLower: plan.tickLower, tickUpper: plan.tickUpper, tick: info.tick, price: r6(info.price),
      tokenMaxA: Number(maxA), tokenMaxB: Number(maxB),
      depositEstA: plan.amtA, depositEstB: plan.amtB, liquidity: plan.dep.liquidity.toString(),
      amountMinA: M.toHuman(plan.minA, info.decimalsA), amountMinB: M.toHuman(plan.minB, info.decimalsB),
      wrapEth: Number(plan.wrapA.wrap + plan.wrapB.wrap) / 1e18,
      approxUsd: Number.isFinite(plan.approxUsd) ? Number(plan.approxUsd.toFixed(2)) : null,
      depositUsd: Number.isFinite(plan.approxUsd) ? Number(plan.approxUsd.toFixed(4)) : null,
      slippageBps: cfg.slippageBps, unstakedFee: info.unstakedFee, staked: false,
      positionMint: null, transactions: steps.length, plannedSteps: steps.map(s => s.label),
    };
    const simulation = await simulateSequence(pub, me, steps);
    if (!execute || plan.refusals.length) {
      return reportUnsent(report, plan.refusals, simulation, 'DRY RUN — open built and simulated. Pass --execute to sign and send.');
    }
    const bad = simulation.find(s => !s.ok);
    if (bad) throw new Error(`refused: simulation of ${bad.label} reverts: ${bad.revert}`);
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    const minted = r.sent.find(s => s.label === 'mint' && s.receipt);
    const extra = {};
    if (minted) {
      for (const log of minted.receipt.logs) {
        if (!same(log.address, info.npm)) continue;
        let ev;
        try { ev = decodeEventLog({ abi: A.NPM_ABI, data: log.data, topics: log.topics }); } catch { continue; }
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

// --- harvest and close ----------------------------------------------------------------
function collectData(tokenId, me) {
  return encodeFunctionData({ abi: A.NPM_ABI, functionName: 'collect', args: [{ tokenId, recipient: me, amount0Max: M.MAX_UINT128, amount1Max: M.MAX_UINT128 }] });
}

function collected(receipt, info) {
  for (const log of receipt?.logs ?? []) {
    if (!same(log.address, info.npm)) continue;
    try {
      const ev = decodeEventLog({ abi: A.NPM_ABI, data: log.data, topics: log.topics });
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
    const report = { mint: x.tokenId.toString(), pool, dex: DEX,
      feesA: M.toHuman(f0, info.decimalsA), feesB: M.toHuman(f1, info.decimalsB), transactions: 1 };
    if (!execute) return reportUnsent(report, [], await simulateSequence(pub, me, steps), 'DRY RUN — pass --execute to collect fees.');
    if (f0 === 0n && f1 === 0n) {
      const out = { harvested: x.tokenId.toString(), signature: null, note: 'nothing to claim' };
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    return reportSent({ harvested: x.tokenId.toString(), pool, dex: DEX }, r, collected(r.sent[0]?.receipt, info));
  });
}

// decreaseLiquidity (all of it, amountMin at slippage) + collect + burn, in ONE NPM
// multicall: one transaction, so a close is never left half done.
export function closeCalls(x, info, me, cfg, dl) {
  const sa = M.sqrtRatioAtTick(x.tickLower), sb = M.sqrtRatioAtTick(x.tickUpper);
  const [e0, e1] = M.amountsForLiquidity(sqrtP(info), sa, sb, x.liquidity, false);
  const calls = [];
  if (x.liquidity > 0n) {
    calls.push(encodeFunctionData({ abi: A.NPM_ABI, functionName: 'decreaseLiquidity', args: [{
      tokenId: x.tokenId, liquidity: x.liquidity,
      amount0Min: M.minWithSlippage(e0, cfg.slippageBps), amount1Min: M.minWithSlippage(e1, cfg.slippageBps), deadline: dl,
    }] }));
  }
  calls.push(collectData(x.tokenId, me));
  calls.push(encodeFunctionData({ abi: A.NPM_ABI, functionName: 'burn', args: [x.tokenId] }));
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
    const steps = [{ label: 'decrease+collect+burn', to: info.npm, data: encodeFunctionData({ abi: A.NPM_ABI, functionName: 'multicall', args: [calls] }) }];
    const report = { mint: x.tokenId.toString(), pool, dex: DEX, transactions: 1, instructions: calls.length,
      quote: { tokenEstA: estA.toString(), tokenEstB: estB.toString() },
      feesQuote: { feeOwedA: f0.toString(), feeOwedB: f1.toString() }, slippageBps: cfg.slippageBps };
    if (!execute) return reportUnsent(report, [], await simulateSequence(pub, me, steps), 'DRY RUN — close built and simulated. Pass --execute to send.');
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    return reportSent({ closed: x.tokenId.toString(), pool, dex: DEX }, r, collected(r.sent[0]?.receipt, info));
  });
}

// --- rebalance: one swap through the pool's own router --------------------------------
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
    const can = spendable(h, info, cfg, sleeve);
    const usdPriceA = info.price * info.quoteUsd, usdPriceB = info.quoteUsd;
    const humA = M.toHuman(can.a, info.decimalsA), humB = M.toHuman(can.b, info.decimalsB);
    const before = {
      owner: me, gasReserveNative: Number(cfg.gasReserve) / 1e18,
      A: { mint: info.mintA, symbol: info.symbolA, amount: Number(h.rawA + (info.nativeSide === 'A' ? h.eth : 0n)) / 10 ** info.decimalsA, sellable: humA, usd: Number((humA * usdPriceA).toFixed(4)), usdPrice: usdPriceA },
      B: { mint: info.mintB, symbol: info.symbolB, amount: Number(h.rawB + (info.nativeSide === 'B' ? h.eth : 0n)) / 10 ** info.decimalsB, sellable: humB, usd: Number((humB * usdPriceB).toFixed(4)), usdPrice: usdPriceB },
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
    const { result: q } = await pub.simulateContract({ account: me, address: info.quoter, abi: A.QUOTER_ABI, functionName: 'quoteExactInputSingle',
      args: [{ tokenIn: tokIn, tokenOut: tokOut, amountIn: rawIn, tickSpacing: info.tickSpacing, sqrtPriceLimitX96: 0n }] });
    const quoteOut = M.toHuman(q[0], decOut);
    // Impact against the pool's spot price, fee included: what the swap costs beyond mid.
    const impact = 1 - (quoteOut * pxOut) / sellUsd;
    if (impact > cfg.maxImpact) throw new Error(`refused: price impact ${(impact * 100).toFixed(3)}% exceeds ${(cfg.maxImpact * 100).toFixed(2)}%`);
    const minOut = M.minWithSlippage(q[0], cfg.slippageBps);
    const side = sellA ? 'A' : 'B';
    const wrap = wrapFor(side, info, rawIn, h, cfg);
    const steps = [
      ...(await fundSteps(pub, me, tokIn, rawIn, info.router, wrap)),
      { label: 'swap', to: info.router, data: encodeFunctionData({ abi: A.ROUTER_ABI, functionName: 'exactInputSingle', args: [{
        tokenIn: tokIn, tokenOut: tokOut, tickSpacing: info.tickSpacing, recipient: me,
        deadline: await deadline(pub), amountIn: rawIn, amountOutMinimum: minOut, sqrtPriceLimitX96: 0n,
      }] }) },
    ];
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
      routePlan: [{ label: DEX, pool, tickSpacing: info.tickSpacing, feePips: info.feePips, percent: 100 }],
      swapUsdValue: Number(sellUsd.toFixed(4)), wrapEth: Number(wrap.wrap) / 1e18,
      transaction: { steps: steps.map(s => s.label) },
    };
    const swapRefusals = wrap.ok ? [] : [`wallet lacks ${amountIn} ${symIn} to sell`];
    const simulation = await simulateSequence(pub, me, steps);
    if (!execute || swapRefusals.length) {
      return reportUnsent(report, swapRefusals, simulation, 'DRY RUN — quote taken and swap built and simulated. Pass --execute to sign and send.');
    }
    const bad = simulation.find(s => !s.ok);
    if (bad) throw new Error(`refused: simulation of ${bad.label} reverts: ${bad.revert}`);
    ctx.nonce = await nonceFor(pub, me);
    const r = await runSteps(ctx, steps, cfg);
    // What arrived, from the swap receipt's Transfer of tokOut to this wallet.
    const swapped = r.sent.find(s => s.label === 'swap' && s.receipt);
    if (swapped) {
      let got = 0n;
      for (const log of swapped.receipt.logs) {
        if (!same(log.address, tokOut)) continue;
        try {
          const ev = decodeEventLog({ abi: A.ERC20_ABI, data: log.data, topics: log.topics });
          if (ev.eventName === 'Transfer' && same(ev.args.to, me)) got += ev.args.value;
        } catch { /* another event */ }
      }
      report.bought = { ...report.bought, amount: M.toHuman(got, decOut), usd: Number((M.toHuman(got, decOut) * pxOut).toFixed(4)) };
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

export function isNative(token) {
  return String(token).toUpperCase() === 'ETH' || same(token, A.NATIVE);
}

async function tokenFacts(pub, token) {
  if (isNative(token)) return { mint: A.NATIVE, symbol: 'ETH', decimals: 18 };
  if (!isAddress(String(token), { strict: false })) throw new Error(`token ${token} is not an address`);
  const t = getAddress(token);
  const [decimals, symbol] = await pub.multicall({ allowFailure: false, contracts: [
    { address: t, abi: A.ERC20_ABI, functionName: 'decimals' }, { address: t, abi: A.ERC20_ABI, functionName: 'symbol' }] });
  return { mint: t, symbol, decimals: Number(decimals) };
}

async function rawOf(pub, me, t) {
  return t.mint === A.NATIVE ? pub.getBalance({ address: me })
    : pub.readContract({ address: t.mint, abi: A.ERC20_ABI, functionName: 'balanceOf', args: [me] });
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
    const cap = M.sleeveCap(sleeve, t.mint === A.NATIVE ? A.WETH : t.mint, t.decimals);
    if (cap != null && raw > cap) throw new Error(`refused: ${amount} ${t.symbol} exceeds this profile's sleeve of ${M.toHuman(cap, t.decimals)}`);
    const have = await rawOf(pub, me, t);
    if (t.mint === A.NATIVE) {
      if (have < raw + cfg.gasReserve) throw new Error(`refused: sending ${amount} ETH would leave ${M.toHuman(have - raw, 18)} ETH, below the ${M.toHuman(cfg.gasReserve, 18)} gas reserve`);
    } else if (have < raw) {
      throw new Error(`LP wallet holds ${M.toHuman(have, t.decimals)}, less than ${amount}`);
    }
    const step = t.mint === A.NATIVE
      ? { label: 'send ETH', to, data: '0x', value: raw }
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
    try { await pub.readContract({ address: getAddress(arg), abi: A.POOL_ABI, functionName: 'tickSpacing' }); return true; }
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
  if (cmd === 'harvest') return harvest(r[0], execute);
  if (cmd === 'close') return close(r[0], execute);
  if (cmd === 'rebalance') {
    if (r.length < 4) throw new Error('usage: rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute]');
    return rebalance(r[0], r[1], r[2], r[3], execute);
  }
  if (cmd === 'send') {
    if (r.length < 3) throw new Error('usage: send <token> <amount> <to> [--execute]');
    return send(r[0], r[1], r[2], execute);
  }
  console.log('commands: balance [pool|token] | positions | status [tokenId] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <tokenId> [--execute] | close <tokenId> [--execute] | '
    + 'rebalance <mintA> <mintB> <usdA> <usdB> [--execute] | send <token> <amount> <to> [--execute]   (pool via --pool or LPBOT_POOL)');
}

if (isEntry(import.meta.url)) {
  main().catch(e => {
    // viem messages run to many lines (request bodies, docs links): the first line only.
    console.error('ERROR:', String(e?.shortMessage ?? e?.message ?? e).split('\n')[0].replace(/[{}]/g, ' '));
    process.exitCode = 1;
  });
}
