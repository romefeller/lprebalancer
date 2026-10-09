// Rebalancer — the Raydium CLMM signing layer. Same commands and the same
// output fields as venues/orca/signer.mjs and venues/meteora_dlmm/signer.mjs, so the loop cannot tell
// which DEX it is on. SIGNER_CONTRACT.md is the specification.
//
// Built on @raydium-io/raydium-sdk-v2 0.2.73-alpha. A Raydium CLMM position is
// a pair of ticks, as on Orca: tick i has price 1.0001^i in raw units, and a
// pool with tickSpacing s only accepts ticks that are multiples of s. The
// position is owned through an NFT; its mint is the ledger id (`positionMint`).
// One position account holds the whole band, so an open is one transaction.
// The wallet can still hold several positions on one pool (a failed close, an
// open retried by hand); as venues/meteora_dlmm/signer.mjs does, `status` reports their union,
// `harvest` claims them all, `close` empties and closes them all, and the
// first (lowest tick) position's mint is the id of the lot.
//
// Fees: the SDK's PositionUtils.GetPositionFees recomputes accrued fees from
// the pool's fee growth and the two boundary ticks, so `feesAccruedA/B` are the
// live figures, not the stale `tokenFeesOwed` the program settles only when
// the position is touched. The pool, positions and tick arrays come from one
// read (shared/fee_snapshot.mjs), so all inputs share a slot. If that read is
// unreadable or fails its invariants, the stale figure is reported and
// `feesSource` says so.
//
// Token-2022 base tokens (MSFTx): every human amount and dollar figure here
// is in UI units (shared/token2022.mjs); `price`, `lowerPrice`, `upperPrice` and the
// tick prices stay pool-native, `uiPrice` is the price in UI units. A paused
// mint or one with a transfer hook refuses open, harvest and close.
//
// The key is read from WALLET_SECRET_PATH inside this process, handed to the
// SDK as the owner, and never printed. The pool comes from --pool <address> or
// LPBOT_POOL for every command that needs one.
//
// Commands:
//   node venues/raydium_clmm/signer.mjs balance [pool]
//   node venues/raydium_clmm/signer.mjs positions
//   node venues/raydium_clmm/signer.mjs status [position]
//   node venues/raydium_clmm/signer.mjs pool [pool]
//   node venues/raydium_clmm/signer.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node venues/raydium_clmm/signer.mjs harvest <position> [--execute]
//   node venues/raydium_clmm/signer.mjs close <position> [--execute]
//   node venues/raydium_clmm/signer.mjs increase <position> <maxA> <maxB> [--execute]   (add to an open position in range)
import fs from 'node:fs';
import { positionRent } from '../../shared/position_rent.mjs';
import { PRICE_SLIPPAGE_BPS, SLIPPAGE_REFUSAL, openToleranceBps, safeBase } from '../../shared/slippage.mjs';
import { executeBuilt, isProgramFailure, signerError } from '../../shared/signer_errors.mjs';
import { consistentFees } from '../../shared/fee_snapshot.mjs';
import { endpoints, overEndpoints, isEntry } from '../../shared/rpc_policy.mjs';
import { readMints, rawToUi, uiToNative, uiPrice, assertWritable, mintFields } from '../../shared/token2022.mjs';
import path from 'node:path';
import { assertNotHalted } from '../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../bot_root.mjs';
import { createRequire } from 'node:module';
import { waitTurn } from '../jupiter/gate.mjs';
import { NeverLanded, priorityCuPrice, sendUntilLanded } from '../../shared/tx_send.mjs';
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };

// The SDK's ESM build loads under Node 24, but the CommonJS build is used so
// that this file, the SDK and web3.js share one copy of PublicKey and BN.
const require = createRequire(import.meta.url);
const {
  Raydium, TxVersion, TickUtil, TickArrayUtil, LiquidityMathUtil, CLMM_PROGRAM_ID,
} = require('@raydium-io/raydium-sdk-v2');
const { Connection, Keypair, PublicKey } = require('@solana/web3.js');
const BN = require('bn.js');
const Decimal = require('decimal.js');

// A keyed endpoint from the environment first. Indexed reads
// (getParsedTokenAccountsByOwner) never go to an endpoint that refuses them.
export const ENDPOINTS = endpoints(process.env, { indexed: true });

const MAX_USD = Number(process.env.LPBOT_MAX_USD ?? 260);
const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.02);

const DEX = 'raydium-clmm';
const PROGRAM_ID = new PublicKey(SOLANA.programs.raydium_clmm);
const NATIVE_MINT = SOLANA.native_mint;
// Stablecoins by MINT (USDC, USDT, PYUSD, USDS). A symbol comes from API or token metadata an
// attacker controls: a fake "USDC" priced at $1 would defeat every dollar cap (review 2026-09-26).
const STABLE_MINTS = new Set(Object.values(SOLANA.stable_mints));
const RAYDIUM_API = 'https://api-v3.raydium.io';
const JUPITER = 'https://lite-api.jup.ag';
const HEADERS = { accept: 'application/json', 'user-agent': 'Mozilla/5.0' };
// Legacy transactions: the SDK confirms them by polling. Its V0 path confirms
// through a websocket subscription with a bare 60 s timeout, which a public
// RPC without websockets never answers.
const TX_VERSION = TxVersion.LEGACY;

// The exit's priority fee. 2026-10-07: a close sent once with the base fee
// only expired unconfirmed ("block height exceeded") in a 2% drop, and the
// band sat out of range six more minutes. A harvest or close is one
// decreaseLiquidity per transaction (70k-79k units measured on mainnet);
// EXIT_CU_LIMIT leaves room. Price: the 75th percentile of the recent fees on
// the pool account, at least EXIT_CU_PRICE_FLOOR, and never more than
// EXIT_PRIORITY_MAX_LAMPORTS in all (100k lamports, about $0.012).
export const EXIT_CU_LIMIT = 200_000;
export const EXIT_CU_PRICE_FLOOR = 10_000;                  // micro-lamports per unit: 2,000 lamports in all
export const EXIT_PRIORITY_MAX_LAMPORTS = Number(process.env.LPBOT_EXIT_PRIORITY_MAX_LAMPORTS ?? 100_000);
// Builds of a close or open in all, after a slippage refusal or a send that
// provably never landed.
const REBUILD_ATTEMPTS = 3;

if (!CLMM_PROGRAM_ID.equals(PROGRAM_ID)) {
  throw new Error(`SDK program id ${CLMM_PROGRAM_ID.toBase58()} differs from the expected CLMM program`);
}

function guard() {
  assertNotHalted(BOT_ROOT);                // the global HALT and this profile's (shared/halt_guard.mjs)
}

async function secretBytes() {
  const p = process.env.WALLET_SECRET_PATH;
  if (!p) throw new Error('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  const raw = fs.readFileSync(p, 'utf8').trim();
  if (raw.startsWith('[')) return Uint8Array.from(JSON.parse(raw));
  const bs58 = (await import('bs58')).default;
  return bs58.decode(raw);
}

function poolArg(explicit) {
  const i = process.argv.indexOf('--pool');
  const p = explicit ?? (i >= 0 ? process.argv[i + 1] : undefined) ?? process.env.LPBOT_POOL;
  if (!p) throw new Error('no pool: pass --pool <address> or set LPBOT_POOL');
  return p;
}

// --- the pool describes itself ------------------------------------------------
const symbolCache = new Map();

async function fetchJson(url) {
  const r = await fetch(url, { headers: HEADERS, signal: AbortSignal.timeout(8000) });
  return r.json();
}

async function symbols(pool, poolInfo) {
  // Raydium's API knows the symbols; the on-chain pool does not. A miss falls
  // back to the mint prefix, which is honest and still unique. WSOL is reported
  // as SOL: the signer deposits native SOL and the loop knows it by that name.
  if (!symbolCache.has(pool)) {
    let a = null, b = null;
    try {
      const j = await fetchJson(`${RAYDIUM_API}/pools/info/ids?ids=${pool}`);
      const d = j?.data?.[0];
      if (d?.mintA?.address === poolInfo.mintA.address) a = d.mintA.symbol ?? null;
      if (d?.mintB?.address === poolInfo.mintB.address) b = d.mintB.symbol ?? null;
    } catch { /* fall through */ }
    const fix = (s, mint) => (s === 'WSOL' ? 'SOL' : s) ?? mint.slice(0, 6);
    symbolCache.set(pool, { a: fix(a, poolInfo.mintA.address), b: fix(b, poolInfo.mintB.address) });
  }
  return symbolCache.get(pool);
}

async function tokenUsd(mint) {
  try {
    await waitTurn();                                 // one Jupiter slot (venues/jupiter/gate.mjs)
    const j = await fetchJson(`${JUPITER}/price/v3?ids=${mint}`);
    const p = Number(j?.[mint]?.usdPrice);
    return p > 0 ? p : null;
  } catch { return null; }
}

async function quoteUsd(symB, mintB) {
  if (STABLE_MINTS.has(String(mintB))) return { usd: 1, source: 'stable' };
  const p = await tokenUsd(mintB);
  return p ? { usd: p, source: 'jupiter:mint' } : { usd: null, source: 'unknown' };
}

// Human price (B per A) of a tick.
function tickPrice(tick, da, db) {
  return Number(TickUtil.tickToPrice(tick, da, db).toString());
}

// Everything the pool says about itself, from one RPC read. `rpc` is the
// decoded pool account; `poolInfo` and `poolKeys` are what the SDK's builders
// want.
async function loadPool(raydium, pool) {
  const r = await raydium.clmm.getPoolInfoFromRpc(pool);
  if (!r?.poolInfo || !r?.rpcPoolInfo) throw new Error(`could not read pool ${pool} from the chain`);
  if (r.poolInfo.programId !== PROGRAM_ID.toBase58()) {
    throw new Error(`pool ${pool} belongs to program ${r.poolInfo.programId}, not the Raydium CLMM`);
  }
  return r;
}

// The pool's two mints, read fresh on every command: a pause or a new
// multiplier must not wait behind a cache.
export async function poolMints(connection, mints) {
  return readMints(async ms => (await connection.getMultipleParsedAccounts(ms.map(m => new PublicKey(m)))).value, mints);
}

async function describe(pool, r, connection) {
  const pi = r.poolInfo, rpc = r.rpcPoolInfo;
  const da = pi.mintA.decimals, db = pi.mintB.decimals;
  const [fa, fb] = await poolMints(connection, [pi.mintA.address, pi.mintB.address]);
  if (fa.decimals !== da || fb.decimals !== db) {
    throw new Error(`mint decimals ${fa.decimals}/${fb.decimals} disagree with the pool's ${da}/${db}`);
  }
  const sym = await symbols(pool, pi);
  const q = await quoteUsd(sym.b, pi.mintB.address);
  // the pool's own sqrt price, so the tick boundaries and the price agree
  const price = Number(TickUtil.sqrtPriceX64ToPrice(rpc.sqrtPriceX64, da, db).toString());
  const info = {
    pool, dex: DEX, programId: pi.programId,
    tickSpacing: pi.config.tickSpacing, tickCurrent: rpc.tickCurrent,
    price, uiPrice: uiPrice(price, fa.multiplier, fb.multiplier),
    feeRate: Number(pi.feeRate) / 1e6,
    liquidity: rpc.liquidity.toString(),
    symbolA: sym.a, symbolB: sym.b, decimalsA: da, decimalsB: db,
    mintA: pi.mintA.address, mintB: pi.mintB.address,
    quoteUsd: q.usd, quoteUsdSource: q.source,
    nativeSide: pi.mintA.address === NATIVE_MINT ? 'A' : pi.mintB.address === NATIVE_MINT ? 'B' : null,
    ...mintFields(fa, fb),
  };
  // The facts ride along for the write checks but stay out of the JSON.
  Object.defineProperty(info, 'mints', { value: [fa, fb], enumerable: false });
  return info;
}

async function connect(url, { withKey = true } = {}) {
  guard();
  const connection = new Connection(url, 'confirmed');
  const payer = withKey ? Keypair.fromSecretKey(await secretBytes()) : null;
  const raydium = await Raydium.load({
    connection, owner: payer ?? undefined, cluster: 'mainnet',
    disableFeatureCheck: true, disableLoadToken: true,
  });
  return { connection, payer, raydium };
}

// Run the whole operation over the endpoints (shared/rpc_policy.mjs). A rate limit,
// a refusal or a transport failure moves on, BEFORE anything is sent. An
// error after a send (`sent`), a program failure or an answer from the chain
// is thrown at once, as itself. `deps` replaces the endpoints, connect and
// sleep in tests.
export async function withRpc(fn, opts, deps = {}) {
  const { urls = ENDPOINTS, connectFn = connect, sleep } = deps;
  return overEndpoints(urls, async url => fn(await connectFn(url, opts)), { tries: 2, pauseMs: 2500, sleep });
}

// UI units: raw / 10^decimals × the mint's multiplier.
async function splBalance(connection, owner, mint, decimals, multiplier, lamports) {
  if (mint === NATIVE_MINT) return lamports / 1e9;
  const r = await connection.getParsedTokenAccountsByOwner(owner, { mint: new PublicKey(mint) });
  let raw = 0n;
  for (const a of r.value ?? []) raw += BigInt(a.account.data.parsed.info.tokenAmount.amount ?? 0);
  return rawToUi(raw, decimals, multiplier);
}

async function balance(poolExplicit) {
  const pool = poolArg(poolExplicit);
  return withRpc(async ({ connection, payer, raydium }) => {
    const info = await describe(pool, await loadPool(raydium, pool), connection);
    const lamports = await connection.getBalance(payer.publicKey);
    const out = { owner: payer.publicKey.toBase58(), sol: lamports / 1e9, pool, dex: DEX,
      tokenA: info.symbolA, tokenB: info.symbolB, price: info.price, uiPrice: info.uiPrice,
      quoteUsd: info.quoteUsd, nativeSide: info.nativeSide, ...mintFields(...info.mints) };
    out.balanceA = await splBalance(connection, payer.publicKey, info.mintA, info.decimalsA, info.multiplierA, lamports);
    out.balanceB = await splBalance(connection, payer.publicKey, info.mintB, info.decimalsB, info.multiplierB, lamports);
    const inQuote = out.balanceA * info.uiPrice + out.balanceB;
    const solUsd = info.nativeSide ? null : await tokenUsd(NATIVE_MINT);
    out.walletUsd = info.quoteUsd == null ? null
      : Number((inQuote * info.quoteUsd + (info.nativeSide ? 0 : out.sol * (solUsd ?? 0))).toFixed(4));
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// --- positions ------------------------------------------------------------------
// Every position the wallet holds on this pool, lowest tick first.
async function positionsOnPool(raydium, pool) {
  const all = await raydium.clmm.getOwnerPositionInfo({ programId: PROGRAM_ID });
  return all.filter(p => p.poolId.toBase58() === pool).sort((x, y) => x.tickLower - y.tickLower);
}

// The accrued fees of every position, from ONE consistent read of the pool,
// the positions and their tick arrays (shared/fee_snapshot.mjs). Three separate
// reads booked $6,237 of fees on a $230 position on 2026-09-27, when the
// price crossed a boundary tick between them. A read that fails the
// invariants reports the settled tokenFeesOwed instead: stale, but never more
// than the position earned.
async function tickStates(connection, pool, list, spacing) {
  const snap = list.length
    ? await consistentFees(connection, PROGRAM_ID, new PublicKey(pool), list, spacing)
    : { ok: true, fees: [] };
  const byMint = new Map(list.map((p, i) => [p.nftMint.toBase58(), snap.fees[i]]));
  return (p) => {
    const f = byMint.get(p.nftMint.toBase58());
    if (snap.ok && f) return { feeA: f.feeA, feeB: f.feeB, feesSource: 'feeGrowth' };
    return { feeA: p.tokenFeesOwedA, feeB: p.tokenFeesOwedB,
             feesSource: `tokenFeesOwed (stale: ${snap.reason || 'no snapshot'})` };
  };
}

// What the position holds now and what it has earned, in raw units.
function positionAmounts(p, r, feeOf) {
  const rpc = r.rpcPoolInfo;
  const { amountA, amountB } = LiquidityMathUtil.getAmountsForLiquidity(
    rpc.sqrtPriceX64, TickUtil.getSqrtPriceAtTick(p.tickLower), TickUtil.getSqrtPriceAtTick(p.tickUpper),
    p.liquidity, false);
  const { feeA, feeB, feesSource } = feeOf(p);
  return { amountA, amountB, feeA, feeB, feesSource };
}

// Amounts in UI units, valued at the UI price; band and price pool-native.
export function positionView(p, r, info, tickOf) {
  const ua = (x) => rawToUi(x, info.decimalsA, info.multiplierA);
  const ub = (x) => rawToUi(x, info.decimalsB, info.multiplierB);
  const am = positionAmounts(p, r, tickOf);
  const lower = tickPrice(p.tickLower, info.decimalsA, info.decimalsB);
  const upper = tickPrice(p.tickUpper, info.decimalsA, info.decimalsB);
  const estA = ua(am.amountA), estB = ub(am.amountB), feeA = ua(am.feeA), feeB = ub(am.feeB);
  const out = {
    positionMint: p.nftMint.toBase58(), whirlpool: info.pool, pool: info.pool, dex: DEX,
    pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
    decimalsA: info.decimalsA, decimalsB: info.decimalsB,
    quoteUsd: info.quoteUsd, quoteUsdSource: info.quoteUsdSource,
    tickLower: p.tickLower, tickUpper: p.tickUpper,
    liquidity: p.liquidity.toString(),
    lowerPrice: Number(lower.toFixed(6)), upperPrice: Number(upper.toFixed(6)),
    price: Number(info.price.toFixed(6)), uiPrice: info.uiPrice,
    multiplierA: info.multiplierA, multiplierB: info.multiplierB,
    paused: info.paused, transferHookA: info.transferHookA, transferHookB: info.transferHookB,
    inRange: info.tickCurrent >= p.tickLower && info.tickCurrent < p.tickUpper,
    closeEstA: estA, closeEstB: estB,
    feesAccruedA: feeA, feesAccruedB: feeB, feesSource: am.feesSource,
    feesAccrued_quote: Number((feeA * info.uiPrice + feeB).toFixed(9)),
  };
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((estA * info.uiPrice + estB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number((out.feesAccrued_quote * info.quoteUsd).toFixed(6));
  }
  return out;
}

// The union of every position the wallet holds on the pool, as one.
export function unionView(list, r, info, tickOf) {
  const views = list.map(p => positionView(p, r, info, tickOf));
  if (views.length === 1) return views[0];
  const sum = (k) => views.reduce((n, v) => n + (v[k] ?? 0), 0);
  const out = {
    ...views[0],
    positionMint: views[0].positionMint,
    positions: views.map(v => v.positionMint),
    tickLower: views[0].tickLower, tickUpper: Math.max(...views.map(v => v.tickUpper)),
    liquidity: list.reduce((n, p) => n.add(p.liquidity), new BN(0)).toString(),
    lowerPrice: views[0].lowerPrice, upperPrice: Math.max(...views.map(v => v.upperPrice)),
    inRange: views.some(v => v.inRange),
    closeEstA: sum('closeEstA'), closeEstB: sum('closeEstB'),
    feesAccruedA: sum('feesAccruedA'), feesAccruedB: sum('feesAccruedB'),
    feesSource: views.every(v => v.feesSource === 'feeGrowth') ? 'feeGrowth' : 'mixed',
  };
  out.feesAccrued_quote = Number((out.feesAccruedA * info.uiPrice + out.feesAccruedB).toFixed(9));
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((out.closeEstA * info.uiPrice + out.closeEstB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number((out.feesAccrued_quote * info.quoteUsd).toFixed(6));
  }
  return out;
}

async function status(positionArg) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r, connection);
    const list = await positionsOnPool(raydium, pool);
    if (!list.length || (positionArg && !list.some(p => p.nftMint.toBase58() === positionArg))) {
      console.log(JSON.stringify({ positions: 0, positionMint: null, pool }, null, 1));
      return null;
    }
    const tickOf = await tickStates(connection, pool, list, info.tickSpacing);
    const out = unionView(list, r, info, tickOf);
    try {
      out.rentSol = Number((await rentOf(connection, payer.publicKey, list.map(x => x.nftMint), PROGRAM_ID)).toFixed(9));
      out.rentUsd = await rentUsdOf(info, out.rentSol);
    } catch { /* reporting only */ }
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

// Rent held by the position accounts and the NFT token accounts, refunded on
// close. The loop adds it to the mark so an open does not read as a loss.
// The personal position PDA is ["position", nftMint] on every Raydium-layout
// program; the NFT sits in one of the owner's token accounts.
async function rentOf(connection, owner, nftMints, programId) {
  return positionRent(connection, owner, nftMints, programId, { refundMint: true });
}

async function rentUsdOf(info, rentSol) {
  const su = info.nativeSide === 'A' && info.quoteUsd != null ? info.uiPrice * info.quoteUsd
    : info.nativeSide === 'B' && info.quoteUsd != null ? info.quoteUsd
    : await tokenUsd(NATIVE_MINT);
  return su != null ? Number((rentSol * su).toFixed(4)) : null;
}

// Every CLMM position the wallet owns, on any pool. No pool argument needed.
async function positions() {
  return withRpc(async ({ raydium }) => {
    const all = await raydium.clmm.getOwnerPositionInfo({ programId: PROGRAM_ID });
    console.log(JSON.stringify(all.map(p => ({
      positionMint: p.nftMint.toBase58(), pool: p.poolId.toBase58(),
      tickLower: p.tickLower, tickUpper: p.tickUpper, liquidity: p.liquidity.toString(),
      tokenFeesOwedA: p.tokenFeesOwedA.toString(), tokenFeesOwedB: p.tokenFeesOwedB.toString(),
    })), null, 1));
  });
}

// --- sizing -------------------------------------------------------------------
// Deposit for a band [pa, pb] at price p with per-token caps: the liquidity
// each cap alone would fund, the smaller of the two, and the amounts that
// liquidity takes. From venues/orca/signer.mjs; human units.
function depositQuote(p, pa, pb, capA, capB) {
  if (!(p > 0 && pa > 0 && pb > pa)) return null;
  const sp = Math.sqrt(p), sa = Math.sqrt(pa), sb = Math.sqrt(pb);
  let L;
  if (p <= pa) L = capA / (1 / sa - 1 / sb);
  else if (p >= pb) L = capB / (sb - sa);
  else L = Math.min(capA / (1 / sp - 1 / sb), capB / (sp - sa));
  if (!(L > 0)) return null;
  const estA = p <= pa ? L * (1 / sa - 1 / sb) : p >= pb ? 0 : L * (1 / sp - 1 / sb);
  const estB = p >= pb ? L * (sb - sa) : p <= pa ? 0 : L * (sp - sa);
  const bound = p <= pa ? 'A' : p >= pb ? 'B'
    : (capA / (1 / sp - 1 / sb) <= capB / (sp - sa) ? 'A' : 'B');
  return { liquidity: L, estA, estB, binding: bound };
}

// The valid ticks that enclose [lower, upper]: lower rounded down, upper
// rounded up, both multiples of the pool's tickSpacing.
function bandTicks(lower, upper, info) {
  const { decimalsA: da, decimalsB: db, tickSpacing: s } = info;
  const at = (price) => TickUtil.getPriceAndTick({
    price: new Decimal(price), mintADecimals: da, mintBDecimals: db, zeroForOne: true, tickSpacing: s,
  }).tick;                                    // floors to a multiple of s
  let lo = at(lower), hi = at(upper);
  while (tickPrice(hi, da, db) < upper) hi += s;
  lo = Math.max(lo, TickArrayUtil.getMinTick(s));
  hi = Math.min(hi, TickArrayUtil.getMaxTick(s));
  if (!(lo < hi)) throw new Error(`band ${lower}-${upper} collapses to a single tick at spacing ${s}`);
  return { tickLower: lo, tickUpper: hi, tickLowerPrice: tickPrice(lo, da, db), tickUpperPrice: tickPrice(hi, da, db) };
}

function toRaw(human, decimals) {
  return new BN(new Decimal(human).mul(new Decimal(10).pow(decimals)).floor().toFixed(0));
}

// Simulate a built transaction without sending it. Signing here is local; the
// dry run still sends nothing. Reports the program's verdict so a dry run
// proves the instructions, not just the builder.
async function simulate(connection, built) {
  const tx = built.transaction;
  try {
    tx.recentBlockhash = (await connection.getLatestBlockhash('confirmed')).blockhash;
    const res = await connection.simulateTransaction(tx, built.signers);
    const logs = res.value.logs ?? [];
    return {
      ok: res.value.err == null,
      err: res.value.err == null ? null : JSON.stringify(res.value.err),
      unitsConsumed: res.value.unitsConsumed ?? null,
      logTail: logs.slice(-4),
    };
  } catch (e) {
    return { ok: false, err: String(e?.message ?? e).slice(0, 200), unitsConsumed: null, logTail: [] };
  }
}

// Sign and send one built transaction. The SDK prints the transaction to
// stdout on its way out; that line is swallowed so stdout stays one JSON.
async function sendBuilt(built) {
  const log = console.log;
  console.log = () => {};
  try {
    return await executeBuilt(built);
  } finally { console.log = log; }
}

// Send several built transactions in order, each through `send`. After the
// first one lands, a failure is reported as a partial send and never retried.
export async function sendAll(builts, report, send = sendBuilt) {
  const sigs = [];
  for (const b of builts) {
    try {
      sigs.push(await send(b));
    } catch (e) {
      if (!sigs.length) throw e;
      console.log(JSON.stringify({ ...report, sent: true, partial: true, signature: sigs[sigs.length - 1],
        signatures: sigs, error: String(e?.message ?? e).slice(0, 300) }, null, 1));
      throw Object.assign(new Error(`partial send: ${sigs.length}/${builts.length} sent; ${e?.message ?? e}`), { sent: true });
    }
  }
  return sigs;
}

// maxA and maxB are UI amounts (what the wallet shows); the deposit is sized
// in pool-native units at the pool price and reported back in UI units.
async function open(pool, lower, upper, uiMaxA, uiMaxB, execute) {
  [lower, upper, uiMaxA, uiMaxB] = [lower, upper, uiMaxA, uiMaxB].map(Number);
  if (![lower, upper, uiMaxA, uiMaxB].every(Number.isFinite)) throw new Error('open needs numeric <lower> <upper> <maxA> <maxB>');
  return withRpc(async ({ connection, payer, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r, connection);
    assertWritable(info.mints);
    const maxA = uiToNative(uiMaxA, info.multiplierA), maxB = uiToNative(uiMaxB, info.multiplierB);
    const lamports = await connection.getBalance(payer.publicKey);
    const sol = lamports / 1e9;
    if (sol < GAS_RESERVE_SOL) {
      throw new Error(`SOL below the ${GAS_RESERVE_SOL} gas reserve; a wallet that cannot pay fees cannot close its own position`);
    }
    const band = bandTicks(lower, upper, info);
    const price = info.price;
    if (price < band.tickLowerPrice || price > band.tickUpperPrice) {
      throw new Error(`price ${price.toFixed(6)} is outside ${band.tickLowerPrice.toFixed(6)}-${band.tickUpperPrice.toFixed(6)}; the price moved`);
    }
    // Size from the actual tick prices, not the requested ones: the program
    // computes the second amount from the ticks it is given.
    const quote = depositQuote(price, band.tickLowerPrice, band.tickUpperPrice, maxA, maxB);
    if (!quote) throw new Error('nothing to deposit: the band or the caps are empty');
    const estA = quote.estA * info.multiplierA, estB = quote.estB * info.multiplierB;
    const approxUsd = (estA * info.uiPrice + estB) * (info.quoteUsd ?? 1);
    if (approxUsd > MAX_USD) throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
    if (!(estA > 0 || estB > 0)) throw new Error('nothing to deposit: both sides are zero');
    const nativeIn = info.nativeSide === 'A' ? estA : info.nativeSide === 'B' ? estB : 0;
    if (sol - nativeIn < GAS_RESERVE_SOL) {
      throw new Error(`depositing ${nativeIn.toFixed(4)} SOL would leave ${(sol - nativeIn).toFixed(4)}, below the ${GAS_RESERVE_SOL} gas reserve`);
    }

    // One side goes in exactly, the other up to its cap. The fixed side is
    // sized so the other fits under its cap anywhere in the price range
    // (shared/slippage.mjs): with the binding side fixed at its cap, a small price
    // move asked for more than the cap and opens failed with 6017.
    const sb_ = safeBase(price, band.tickLowerPrice, band.tickUpperPrice, maxA, maxB,
                         openToleranceBps(band.tickLowerPrice, band.tickUpperPrice));
    if (!sb_) throw new Error('nothing to deposit inside the price range');
    const base = sb_.base === 'A' ? 'MintA' : 'MintB';
    const baseAmount = base === 'MintA' ? toRaw(sb_.amount, info.decimalsA) : toRaw(sb_.amount, info.decimalsB);
    const otherAmountMax = base === 'MintA' ? toRaw(maxB, info.decimalsB) : toRaw(maxA, info.decimalsA);
    if (baseAmount.isZero()) throw new Error('nothing to deposit: the binding side rounds to zero');

    // What the program will compute from the same inputs, in raw units.
    const sqrtP = r.rpcPoolInfo.sqrtPriceX64;
    const sqrtLo = TickUtil.getSqrtPriceAtTick(band.tickLower), sqrtHi = TickUtil.getSqrtPriceAtTick(band.tickUpper);
    const liquidity = base === 'MintA'
      ? LiquidityMathUtil.getLiquidityFromAmountA(sqrtP.gt(sqrtLo) ? sqrtP : sqrtLo, sqrtHi, baseAmount)
      : LiquidityMathUtil.getLiquidityFromAmountB(sqrtLo, sqrtP.lt(sqrtHi) ? sqrtP : sqrtHi, baseAmount);
    const chain = LiquidityMathUtil.getAmountsForLiquidity(sqrtP, sqrtLo, sqrtHi, liquidity, true);

    const built = await raydium.clmm.openPositionFromBase({
      poolInfo: r.poolInfo, poolKeys: r.poolKeys,
      ownerInfo: { useSOLBalance: true },
      tickLower: band.tickLower, tickUpper: band.tickUpper,
      base, baseAmount, otherAmountMax,
      // LPBOT_RAYDIUM_LEAN=1: no Metaplex metadata and a Token-2022 position
      // NFT. The legacy path leaves about 0.0148 SOL per open on chain that
      // close never refunds (metadata account and fee, legacy mint), $1.80 a
      // cycle. Open simulates green both ways; close of a Token-2022 position
      // has not run live yet, so the lean path stays off until it has.
      ...(process.env.LPBOT_RAYDIUM_LEAN === '1' ? { withMetadata: 'no-create', nft2022: true } : {}),
      txVersion: TX_VERSION,
    }).catch(e => {
      // The SDK creates a token account only for a side it deposits nothing
      // of; a side it must take from an account that does not exist is this
      // error, with the missing account left out of its text.
      if (/cannot found target token accounts/.test(String(e?.message ?? e))) {
        throw new Error(`the wallet has no token account for ${info.symbolA} or ${info.symbolB}, and the open `
          + 'deposits both: fund the wallet with both tokens first');
      }
      throw e;
    });
    const report = {
      pool, dex: DEX, pair: `${info.symbolA}/${info.symbolB}`,
      requestedLower: lower, requestedUpper: upper,
      lowerPrice: Number(band.tickLowerPrice.toFixed(6)), upperPrice: Number(band.tickUpperPrice.toFixed(6)),
      tickLower: band.tickLower, tickUpper: band.tickUpper, tickSpacing: info.tickSpacing,
      price: Number(price.toFixed(6)), uiPrice: info.uiPrice,
      multiplierA: info.multiplierA, multiplierB: info.multiplierB,
      tokenMaxA: uiMaxA, tokenMaxB: uiMaxB, tokenA: info.symbolA, tokenB: info.symbolB,
      depositEstA: estA, depositEstB: estB, binding: quote.binding,
      chainEstA: rawToUi(chain.amountA, info.decimalsA, info.multiplierA),
      chainEstB: rawToUi(chain.amountB, info.decimalsB, info.multiplierB),
      liquidity: liquidity.toString(),
      approxUsd: Number(approxUsd.toFixed(2)),
      depositUsd: info.quoteUsd != null ? Number(approxUsd.toFixed(4)) : null,
      positionMint: built.extInfo?.nftMint?.toBase58() ?? null,
      instructions: built.transaction.instructions.length,
      transactions: 1,
    };
    if (!execute) {
      const simulation = await simulate(connection, built);
      console.log(JSON.stringify({ ...report, simulation, sent: false }, null, 1));
      console.log('DRY RUN — instructions built. Pass --execute to sign and send.');
      return;
    }
    const sig = await sendBuilt(built);
    console.log(JSON.stringify({ ...report, sent: true, signature: sig, signatures: [sig] }, null, 1));
  });
}

// All of the wallet's positions on the pool, provided the one named is among
// them: the name is a check that the caller and the chain agree on which pool.
async function findPositions(raydium, pool, address) {
  const list = await positionsOnPool(raydium, pool);
  if (!list.some(p => p.nftMint.toBase58() === address)) {
    throw new Error(`position ${address} not found for this wallet on this pool`);
  }
  return list;
}

// decreaseLiquidity with liquidity 0 collects fees and rewards only; with the
// whole liquidity and closePosition it empties the position and closes it.
function closeMinimumsRaw(sqrtPriceX64, p) {
  const f = Math.sqrt(1 + PRICE_SLIPPAGE_BPS / 1e4);
  const S = 1_000_000_000_000;
  const up = sqrtPriceX64.mul(new BN(Math.round(f * S))).div(new BN(S));
  const dn = sqrtPriceX64.mul(new BN(S)).div(new BN(Math.round(f * S)));
  const lo = TickUtil.getSqrtPriceAtTick(p.tickLower), hi = TickUtil.getSqrtPriceAtTick(p.tickUpper);
  const atUp = LiquidityMathUtil.getAmountsForLiquidity(up, lo, hi, p.liquidity, false);
  const atDn = LiquidityMathUtil.getAmountsForLiquidity(dn, lo, hi, p.liquidity, false);
  return { minA: atUp.amountA, minB: atDn.amountB };
}

async function buildDecrease(raydium, r, p, liquidity, minA, minB, closePosition, computeBudgetConfig) {
  return raydium.clmm.decreaseLiquidity({
    poolInfo: r.poolInfo, poolKeys: r.poolKeys, ownerPosition: p,
    ownerInfo: { useSOLBalance: true, closePosition },
    liquidity, amountMinA: minA, amountMinB: minB,
    computeBudgetConfig, txVersion: TX_VERSION,
  });
}

// The compute budget of a harvest or close on `pool`. An unreadable fee
// history prices at the floor.
async function exitBudget(connection, pool) {
  let recent = [];
  try { recent = await connection.getRecentPrioritizationFees({ lockedWritableAccounts: [new PublicKey(pool)] }); } catch { recent = []; }
  return { units: EXIT_CU_LIMIT,
           microLamports: priorityCuPrice(recent, EXIT_CU_LIMIT, EXIT_PRIORITY_MAX_LAMPORTS, EXIT_CU_PRICE_FLOOR) };
}

// Sign and send one built legacy transaction, re-sent until it lands or its
// blockhash expires (tx_send.sendUntilLanded). NeverLanded is thrown as
// itself: nothing went out, and the operation may be rebuilt. Any other
// error is marked sent, as executeBuilt does: the controller reads the
// outcome and never executes the operation again on another endpoint.
export async function sendLanded(connection, payer, built, deps = {}) {
  try {
    const tx = built.transaction;
    const bh = await connection.getLatestBlockhash('confirmed');
    tx.recentBlockhash = bh.blockhash;
    tx.feePayer = payer.publicKey;
    tx.sign(payer, ...(built.signers ?? []));
    return await sendUntilLanded(connection, tx.serialize(), bh.lastValidBlockHeight, deps);
  } catch (error) {
    if (error instanceof NeverLanded) throw error;
    throw Object.assign(new Error(signerError(error)), { sent: true, cause: error });
  }
}

async function harvest(address, execute) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r, connection);
    assertWritable(info.mints);
    const ps = await findPositions(raydium, pool, address);
    const tickOf = await tickStates(connection, pool, ps, info.tickSpacing);
    const builts = [];
    const budget = await exitBudget(connection, pool);
    for (const p of ps) builts.push(await buildDecrease(raydium, r, p, new BN(0), new BN(0), new BN(0), false, budget));
    const fees = ps.map(p => positionAmounts(p, r, tickOf));
    const sum = (k) => fees.reduce((n, f) => n.add(f[k]), new BN(0));
    const report = {
      mint: address, pool, positions: ps.length, transactions: builts.length,
      instructions: builts.reduce((n, b) => n + b.transaction.instructions.length, 0),
      feesQuote: { feeOwedA: sum('feeA').toString(), feeOwedB: sum('feeB').toString(),
        feesAccruedA: rawToUi(sum('feeA'), info.decimalsA, info.multiplierA),
        feesAccruedB: rawToUi(sum('feeB'), info.decimalsB, info.multiplierB) },
    };
    if (!execute) {
      const simulation = [];
      for (const b of builts) simulation.push(await simulate(connection, b));
      console.log(JSON.stringify({ ...report, simulation, sent: false }, null, 1));
      console.log('DRY RUN — pass --execute to collect fees.');
      return;
    }
    const sigs = await sendAll(builts, report, b => sendLanded(connection, payer, b));
    console.log(JSON.stringify({ harvested: address, signature: sigs[sigs.length - 1], signatures: sigs }, null, 1));
  });
}

async function close(address, execute) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r, connection);
    assertWritable(info.mints);
    const ps = await findPositions(raydium, pool, address);
    const tickOf = await tickStates(connection, pool, ps, info.tickSpacing);
    const builts = [], ests = [];
    const budget = await exitBudget(connection, pool);
    for (const p of ps) {
      const am = positionAmounts(p, r, tickOf);
      ests.push(am);
      // All liquidity out, fees and rewards collected, NFT burnt, accounts
      // closed. Slippage is a PRICE range (shared/slippage.mjs): each minimum is
      // what the position holds if the price moves PRICE_SLIPPAGE_BPS against
      // that token. A 1% cut of each amount was a ~0.01% price tolerance on a
      // +/-1% band, and closes failed with PriceSlippageCheck (6017).
      const { minA, minB } = closeMinimumsRaw(r.rpcPoolInfo.sqrtPriceX64, p);
      builts.push(await buildDecrease(raydium, r, p, p.liquidity, minA, minB, true, budget));
    }
    const sum = (k) => ests.reduce((n, e) => n.add(e[k]), new BN(0));
    const report = {
      mint: address, pool, positions: ps.length, transactions: builts.length,
      instructions: builts.reduce((n, b) => n + b.transaction.instructions.length, 0),
      quote: { liquidity: ps.reduce((n, p) => n.add(p.liquidity), new BN(0)).toString(),
        tokenEstA: sum('amountA').toString(), tokenEstB: sum('amountB').toString(),
        closeEstA: rawToUi(sum('amountA'), info.decimalsA, info.multiplierA),
        closeEstB: rawToUi(sum('amountB'), info.decimalsB, info.multiplierB) },
      feesQuote: { feeOwedA: sum('feeA').toString(), feeOwedB: sum('feeB').toString() },
    };
    if (!execute) {
      const simulation = [];
      for (const b of builts) simulation.push(await simulate(connection, b));
      console.log(JSON.stringify({ ...report, simulation, sent: false }, null, 1));
      console.log('DRY RUN — close instructions built. Pass --execute to send.');
      return;
    }
    const sigs = await sendAll(builts, report, b => sendLanded(connection, payer, b));
    console.log(JSON.stringify({ closed: address, signature: sigs[sigs.length - 1], signatures: sigs }, null, 1));
  });
}

// Add to an open position in range, instead of a close and a reopen (owner,
// 2026-10-03: idle cash beside the band cost a full close, swap and open,
// ~$0.04 each, twice a day). maxA and maxB are UI amounts; the deposit is
// sized like an open's (safeBase: one side exact, the other up to its cap
// anywhere in the price tolerance) against the position's own ticks. Refused,
// before anything is signed: a position out of range, gas under the reserve,
// a deposit that would leave gas under it, a position that would pass
// LPBOT_MAX_USD, nothing to add.
export async function increase(address, uiMaxA, uiMaxB, execute) {
  [uiMaxA, uiMaxB] = [uiMaxA, uiMaxB].map(Number);
  if (![uiMaxA, uiMaxB].every(x => Number.isFinite(x) && x >= 0) || !(uiMaxA > 0 || uiMaxB > 0)) {
    throw new Error('increase needs <positionMint> <maxA> <maxB>, both >= 0 and one > 0');
  }
  const pool = poolArg();
  return withRpc(async ({ connection, payer, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r, connection);
    assertWritable(info.mints);
    const ps = await findPositions(raydium, pool, address);
    const p = ps.find(x => x.nftMint.toBase58() === address);
    const { decimalsA: da, decimalsB: db } = info;
    const lowerP = tickPrice(p.tickLower, da, db), upperP = tickPrice(p.tickUpper, da, db);
    const price = info.price;
    if (!(price > lowerP && price < upperP)) {
      throw new Error(`price ${price.toFixed(6)} is outside the position ${lowerP.toFixed(6)}-${upperP.toFixed(6)}: nothing to add`);
    }
    const lamports = await connection.getBalance(payer.publicKey);
    const sol = lamports / 1e9;
    if (sol < GAS_RESERVE_SOL) {
      throw new Error(`SOL below the ${GAS_RESERVE_SOL} gas reserve; a wallet that cannot pay fees cannot close its own position`);
    }
    const maxA = uiToNative(uiMaxA, info.multiplierA), maxB = uiToNative(uiMaxB, info.multiplierB);
    const quote = depositQuote(price, lowerP, upperP, maxA, maxB);
    if (!quote) throw new Error('nothing to add: the caps are empty');
    const estA = quote.estA * info.multiplierA, estB = quote.estB * info.multiplierB;
    const addUsd = (estA * info.uiPrice + estB) * (info.quoteUsd ?? 1);
    const sqrtP = r.rpcPoolInfo.sqrtPriceX64;
    const sqrtLo = TickUtil.getSqrtPriceAtTick(p.tickLower), sqrtHi = TickUtil.getSqrtPriceAtTick(p.tickUpper);
    const held = LiquidityMathUtil.getAmountsForLiquidity(sqrtP, sqrtLo, sqrtHi, p.liquidity, false);
    const heldUsd = (rawToUi(held.amountA, da, info.multiplierA) * info.uiPrice
                     + rawToUi(held.amountB, db, info.multiplierB)) * (info.quoteUsd ?? 1);
    if (heldUsd + addUsd > MAX_USD) {
      throw new Error(`position about $${(heldUsd + addUsd).toFixed(0)} after the add exceeds cap $${MAX_USD}`);
    }
    // The SDK wraps the whole cap of the other side, not the estimate: the
    // reserve is checked against the native side's cap (audit 2026-10-03).
    const nativeIn = info.nativeSide === 'A' ? uiMaxA : info.nativeSide === 'B' ? uiMaxB : 0;
    if (sol - nativeIn < GAS_RESERVE_SOL) {
      throw new Error(`adding up to ${nativeIn.toFixed(4)} SOL would leave ${(sol - nativeIn).toFixed(4)}, below the ${GAS_RESERVE_SOL} gas reserve`);
    }
    const sb_ = safeBase(price, lowerP, upperP, maxA, maxB, openToleranceBps(lowerP, upperP));
    if (!sb_) throw new Error('nothing to add inside the price range');
    const base = sb_.base === 'A' ? 'MintA' : 'MintB';
    const baseAmount = base === 'MintA' ? toRaw(sb_.amount, da) : toRaw(sb_.amount, db);
    const otherAmountMax = base === 'MintA' ? toRaw(maxB, db) : toRaw(maxA, da);
    if (baseAmount.isZero()) throw new Error('nothing to add: the binding side rounds to zero');
    const liquidity = base === 'MintA'
      ? LiquidityMathUtil.getLiquidityFromAmountA(sqrtP.gt(sqrtLo) ? sqrtP : sqrtLo, sqrtHi, baseAmount)
      : LiquidityMathUtil.getLiquidityFromAmountB(sqrtLo, sqrtP.lt(sqrtHi) ? sqrtP : sqrtHi, baseAmount);
    const chain = LiquidityMathUtil.getAmountsForLiquidity(sqrtP, sqrtLo, sqrtHi, liquidity, true);
    const chainA = rawToUi(chain.amountA, da, info.multiplierA), chainB = rawToUi(chain.amountB, db, info.multiplierB);
    // what the program takes for this liquidity, not the quote before the price tolerance (audit: 4.4% high)
    const chainUsd = (chainA * info.uiPrice + chainB) * (info.quoteUsd ?? 1);
    const built = await raydium.clmm.increasePositionFromBase({
      poolInfo: r.poolInfo, ownerPosition: p, ownerInfo: { useSOLBalance: true },
      base, baseAmount, otherAmountMax, txVersion: TX_VERSION,
    });
    const report = {
      positionMint: address, pool, dex: DEX, pair: `${info.symbolA}/${info.symbolB}`,
      lowerPrice: Number(lowerP.toFixed(6)), upperPrice: Number(upperP.toFixed(6)),
      price: Number(price.toFixed(6)), uiPrice: info.uiPrice,
      tokenMaxA: uiMaxA, tokenMaxB: uiMaxB, tokenA: info.symbolA, tokenB: info.symbolB,
      depositEstA: estA, depositEstB: estB, binding: quote.binding,
      chainEstA: chainA, chainEstB: chainB,
      liquidityAdded: liquidity.toString(), liquidityBefore: p.liquidity.toString(),
      heldUsd: Number(heldUsd.toFixed(2)), quoteUsdEstimate: Number(addUsd.toFixed(4)),
      depositUsd: info.quoteUsd != null ? Number(chainUsd.toFixed(4)) : null,
      instructions: built.transaction.instructions.length, transactions: 1,
    };
    if (!execute) {
      const simulation = await simulate(connection, built);
      console.log(JSON.stringify({ ...report, simulation, sent: false }, null, 1));
      console.log('DRY RUN — increase built. Pass --execute to sign and send.');
      return;
    }
    const sig = await sendBuilt(built);
    console.log(JSON.stringify({ ...report, sent: true, signature: sig, signatures: [sig] }, null, 1));
  });
}

// Up to REBUILD_ATTEMPTS builds on fresh pool state when the chain refuses
// on slippage, or when the first transaction provably never landed
// (NeverLanded: its blockhash expired and the chain has no record of it).
// Only the first transaction's failure is rebuilt: a refused transaction
// reverted whole. A partial multi-transaction send is never retried.
export async function rebuildOnRefusal(fn, execute, { attempts = REBUILD_ATTEMPTS, sleep = ms => new Promise(res => setTimeout(res, ms)) } = {}) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (e) {
      const m = signerError(e);
      const expired = e instanceof NeverLanded;
      if (!execute || i >= attempts || /partial send/.test(m) || !(expired || SLIPPAGE_REFUSAL.test(m))) throw e;
      console.error(`${expired ? 'expired unsent' : 'slippage refusal'}; rebuilding on fresh pool state (attempt ${i + 1}/${attempts})`);
      await sleep(1500);
    }
  }
}

async function main() {
  const args = process.argv.slice(2);
  const execute = args.includes('--execute');
  const pi = args.indexOf('--pool');
  const a = args.filter((x, i) => x !== '--execute' && x !== '--pool' && (pi < 0 || i !== pi + 1));
  const [cmd, ...rest] = a;
  if (cmd === 'balance') return balance(rest[0]);
  if (cmd === 'positions') return positions();
  if (cmd === 'status') return status(rest[0]);
  if (cmd === 'harvest') return rebuildOnRefusal(() => harvest(rest[0], execute), execute);
  if (cmd === 'close') return rebuildOnRefusal(() => close(rest[0], execute), execute);
  if (cmd === 'increase') return rebuildOnRefusal(() => increase(rest[0], rest[1], rest[2], execute), execute);
  if (cmd === 'open') return rebuildOnRefusal(() => open(rest[0], rest[1], rest[2], rest[3], rest[4], execute), execute);
  if (cmd === 'pool') {
    // Read-only: no key is loaded.
    const pool = poolArg(rest[0]);
    return withRpc(async ({ connection, raydium }) => {
      console.log(JSON.stringify(await describe(pool, await loadPool(raydium, pool), connection), null, 1));
    }, { withKey: false });
  }
  console.log('commands: balance [pool] | positions | status [position] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <position> [--execute] '
    + '| close <position> [--execute]   (pool via --pool or LPBOT_POOL)');
}

// Braces in an error text would look like JSON to the loop; flatten them.
// The CLI runs only when node starts this file; a test import runs nothing.
if (isEntry(import.meta.url)) main().catch(e => {
  console.error('ERROR:', signerError(e).replace(/[{}]/g, ' '));
  process.exitCode = 1;
});
