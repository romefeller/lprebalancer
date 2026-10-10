// Rebalancer — the Meteora DLMM signing layer. Same commands and the same
// output fields as venues/orca/signer.mjs, so the loop cannot tell which DEX it is on.
//
// Built on @meteora-ag/dlmm 1.9. A DLMM position is a range of bins, not a
// pair of ticks: bin i covers the price (1 + step/10000)^i in lamport units.
// One position account holds about 70 bins at creation and the program
// refuses to allocate a wider one in a single instruction (InvalidRealloc, seen
// live on a 387-bin request), so a band the engine likes is usually several
// position accounts side by side. This signer treats every position the
// wallet holds on a pool as ONE logical position: `status` reports their
// union, `harvest` claims them all, `close` empties and closes them all, and
// the ledger's id for the lot is the lowest bin's account. The loop holds one
// pool at a time, so that is the same thing.
//
// The whole band still may not exceed POSITION_MAX_LENGTH bins; the engine's
// `feasible_bands` keeps such a band from being chosen, and `open` refuses it.
//
// Token-2022 base tokens (MU): every human amount and dollar figure here is
// in UI units (shared/token2022.mjs); `price`, `lowerPrice`, `upperPrice` and the bin
// prices stay pool-native, `uiPrice` is the price in UI units. A paused mint
// or one with a transfer hook refuses open, harvest and close.
//
// The key is read from WALLET_SECRET_PATH inside this process and never
// printed. The pool comes from --pool <address> or LPBOT_POOL: a position on
// DLMM is only readable through its pool.
//
// Vault mode (LPBOT_VAULT_SETTINGS set; chains/solana/vault/account.mjs): the
// money and positions belong to a Squads vault the owners control, the key
// above only pays fees and rent, and every instruction that needs the vault
// runs through the vault's policy. `owner` below is whoever owns the money.
//
// Commands:
//   node venues/meteora_dlmm/signer.mjs balance [pool]
//   node venues/meteora_dlmm/signer.mjs positions
//   node venues/meteora_dlmm/signer.mjs status [position]
//   node venues/meteora_dlmm/signer.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node venues/meteora_dlmm/signer.mjs harvest <position> [--execute]
//   node venues/meteora_dlmm/signer.mjs close <position> [--execute]
//   node venues/meteora_dlmm/signer.mjs rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute]
import fs from 'node:fs';
import path from 'node:path';
import { assertNotHalted } from '../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../bot_root.mjs';
import { createRequire } from 'node:module';
import { waitTurn } from '../jupiter/gate.mjs';
import { endpoints, overEndpoints, isEntry, AfterSignError } from '../../shared/rpc_policy.mjs';
import { readMints, rawToUi, uiToNative, uiPrice, assertWritable, mintFields } from '../../shared/token2022.mjs';
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };
import { actingAccount } from '../../chains/solana/vault/account.mjs';
import { planRebalance, TARGET_TOLERANCE } from '../../shared/rebalance_plan.mjs';
import { planSwap, valueLossOk, checkImpact, parseHints, noopReport, Refused, MAX_VALUE_LOSS } from '../orca/swap.mjs';
import { parseSleeve, sleeveCap } from '../jupiter/swap.mjs';
import { JUPITER, jupiterHeaders } from '../jupiter/api.mjs';

// The package's ESM build imports a directory and fails to load under Node 24;
// the CommonJS build resolves cleanly.
const require = createRequire(import.meta.url);
const dlmmPkg = require('@meteora-ag/dlmm');
const DLMM = dlmmPkg.default ?? dlmmPkg;
const { StrategyType } = dlmmPkg;
const { Connection, Keypair, PublicKey } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const { BN } = require('@coral-xyz/anchor');

// A keyed endpoint from the environment first. Indexed reads
// (getParsedTokenAccountsByOwner) never go to an endpoint that refuses them.
export const ENDPOINTS = endpoints(process.env, { indexed: true });

const MAX_USD = Number(process.env.LPBOT_MAX_USD ?? 260);
const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.02);
const POSITION_MAX_LENGTH = 1400;      // bins per position, the program's limit
// SOL a vault keeps for itself: its own rent-exempt minimum and the rent of
// a WSOL account the SDK opens for a swap. Never deposited.
const VAULT_SOL_FLOOR = 0.005;

const NATIVE_MINT = SOLANA.native_mint;
// Stablecoins by MINT (USDC, USDT, PYUSD, USDS). A symbol comes from API or token metadata an
// attacker controls: a fake "USDC" priced at $1 would defeat every dollar cap (review 2026-09-26).
const STABLE_MINTS = new Set(Object.values(SOLANA.stable_mints));
const METEORA = 'https://dlmm.datapi.meteora.ag';
const HEADERS = { accept: 'application/json', 'user-agent': 'Mozilla/5.0' };

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

async function symbols(pool, dlmm) {
  // Meteora's API knows the symbols; the chain does not. A miss falls back to
  // the mint prefix, which is honest and still unique.
  if (!symbolCache.has(pool)) {
    let x = null, y = null;
    try {
      const j = await (await fetch(`${METEORA}/pools/${pool}`, { headers: HEADERS })).json();
      x = j?.token_x?.symbol ?? null; y = j?.token_y?.symbol ?? null;
    } catch { /* fall through */ }
    symbolCache.set(pool, {
      x: x ?? dlmm.tokenX.publicKey.toBase58().slice(0, 6),
      y: y ?? dlmm.tokenY.publicKey.toBase58().slice(0, 6),
    });
  }
  return symbolCache.get(pool);
}

async function tokenUsd(mint) {
  try {
    await waitTurn();                                 // one Jupiter slot (venues/jupiter/gate.mjs)
    const j = await (await fetch(`${JUPITER}/price/v3?ids=${mint}`, { headers: jupiterHeaders(HEADERS) })).json();
    const p = Number(j?.[mint]?.usdPrice);
    return p > 0 ? p : null;
  } catch { return null; }
}

async function quoteUsd(symY, mintY) {
  if (STABLE_MINTS.has(String(mintY))) return { usd: 1, source: 'stable' };
  const p = await tokenUsd(mintY);
  return p ? { usd: p, source: 'jupiter:mint' } : { usd: null, source: 'unknown' };
}

// price of the LOWER edge of bin `id`, in human units (Y per X)
function binPrice(id, step, dx, dy) {
  return (1 + step / 10000) ** id * 10 ** (dx - dy);
}

// The pool's two mints, read fresh on every command: a pause or a new
// multiplier must not wait behind a cache.
export async function poolMints(connection, mints) {
  return readMints(async ms => (await connection.getMultipleParsedAccounts(ms.map(m => new PublicKey(m)))).value, mints);
}

async function describe(pool, dlmm, connection) {
  const dx = dlmm.tokenX.mint.decimals, dy = dlmm.tokenY.mint.decimals;
  const active = await dlmm.getActiveBin();
  const sym = await symbols(pool, dlmm);
  const mintX = dlmm.tokenX.publicKey.toBase58(), mintY = dlmm.tokenY.publicKey.toBase58();
  const [fx, fy] = await poolMints(connection, [mintX, mintY]);
  if (fx.decimals !== dx || fy.decimals !== dy) {
    throw new Error(`mint decimals ${fx.decimals}/${fy.decimals} disagree with the pool's ${dx}/${dy}`);
  }
  const q = await quoteUsd(sym.y, mintY);
  const price = Number(active.pricePerToken);
  const info = {
    pool, step: dlmm.lbPair.binStep, activeBin: active.binId,
    price, uiPrice: uiPrice(price, fx.multiplier, fy.multiplier),
    symbolA: sym.x, symbolB: sym.y, decimalsA: dx, decimalsB: dy, mintA: mintX, mintB: mintY,
    quoteUsd: q.usd, quoteUsdSource: q.source,
    nativeSide: mintX === NATIVE_MINT ? 'A' : mintY === NATIVE_MINT ? 'B' : null,
    ...mintFields(fx, fy),
  };
  // The facts ride along for the write checks but stay out of the JSON.
  Object.defineProperty(info, 'mints', { value: [fx, fy], enumerable: false });
  return info;
}

async function connect(url) {
  guard();
  const connection = new Connection(url, 'confirmed');
  const payer = Keypair.fromSecretKey(await secretBytes());
  const acting = await actingAccount({ connection, payer, venue: 'meteora-dlmm' });
  return { connection, payer, acting };
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

export async function balance(poolExplicit, deps) {
  const pool = poolArg(poolExplicit);
  return withRpc(async ({ connection, payer, acting }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const info = await describe(pool, dlmm, connection);
    const lamports = await connection.getBalance(acting.owner);
    // gasSol: what pays the fees (the owner itself unless a vault holds the money)
    const gasSol = acting.mode === 'vault' ? (await connection.getBalance(payer.publicKey)) / 1e9 : lamports / 1e9;
    const out = { owner: acting.owner.toBase58(), custody: acting.mode, sol: lamports / 1e9, gasSol, pool, dex: 'meteora-dlmm',
      // a vault keeps only its floor: the payer key pays every rent
      ...(acting.mode === 'vault' ? { nativeReserve: VAULT_SOL_FLOOR } : {}),
      tokenA: info.symbolA, tokenB: info.symbolB, price: info.price, uiPrice: info.uiPrice,
      quoteUsd: info.quoteUsd, nativeSide: info.nativeSide, ...mintFields(...info.mints) };
    out.balanceA = await splBalance(connection, acting.owner, info.mintA, info.decimalsA, info.multiplierA, lamports);
    out.balanceB = await splBalance(connection, acting.owner, info.mintB, info.decimalsB, info.multiplierB, lamports);
    const inQuote = out.balanceA * info.uiPrice + out.balanceB;
    const solUsd = info.nativeSide ? null : await tokenUsd(NATIVE_MINT);
    out.walletUsd = info.quoteUsd == null ? null
      : Number((inQuote * info.quoteUsd + (info.nativeSide ? 0 : out.sol * (solUsd ?? 0))).toFixed(4));
    console.log(JSON.stringify(out, null, 1));
    return out;
  }, undefined, deps);
}

// Amounts in UI units, valued at the UI price; band and price pool-native.
export function positionView(p, info) {
  const d = p.positionData;
  const ua = (x) => rawToUi(x, info.decimalsA, info.multiplierA);
  const ub = (x) => rawToUi(x, info.decimalsB, info.multiplierB);
  const lower = binPrice(d.lowerBinId, info.step, info.decimalsA, info.decimalsB);
  const upper = binPrice(d.upperBinId + 1, info.step, info.decimalsA, info.decimalsB);
  const estA = ua(new BN(d.totalXAmount.split('.')[0])), estB = ub(new BN(d.totalYAmount.split('.')[0]));
  const feeA = ua(d.feeX), feeB = ub(d.feeY);
  const out = {
    positionMint: p.publicKey.toBase58(), whirlpool: info.pool, pool: info.pool, dex: 'meteora-dlmm',
    pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
    decimalsA: info.decimalsA, decimalsB: info.decimalsB,
    quoteUsd: info.quoteUsd, quoteUsdSource: info.quoteUsdSource,
    lowerBinId: d.lowerBinId, upperBinId: d.upperBinId, bins: d.upperBinId - d.lowerBinId + 1,
    // no single L on DLMM; the bin count stands in for the snapshot column
    liquidity: String(d.upperBinId - d.lowerBinId + 1),
    lowerPrice: Number(lower.toFixed(6)), upperPrice: Number(upper.toFixed(6)),
    price: Number(info.price.toFixed(6)), uiPrice: info.uiPrice,
    multiplierA: info.multiplierA, multiplierB: info.multiplierB,
    paused: info.paused, transferHookA: info.transferHookA, transferHookB: info.transferHookB,
    inRange: info.activeBin >= d.lowerBinId && info.activeBin <= d.upperBinId,
    closeEstA: estA, closeEstB: estB,
    feesAccruedA: feeA, feesAccruedB: feeB,
    feesAccrued_quote: Number((feeA * info.uiPrice + feeB).toFixed(9)),
  };
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((estA * info.uiPrice + estB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number(((feeA * info.uiPrice + feeB) * info.quoteUsd).toFixed(6));
  }
  return out;
}

// The union of every position the wallet holds on the pool, as one.
export function unionView(list, info) {
  const views = list.map(p => positionView(p, info)).sort((a, b) => a.lowerBinId - b.lowerBinId);
  const sum = (k) => views.reduce((n, v) => n + (v[k] ?? 0), 0);
  const out = {
    ...views[0],
    positionMint: views[0].positionMint,
    positions: views.map(v => v.positionMint),
    lowerBinId: views[0].lowerBinId, upperBinId: views[views.length - 1].upperBinId,
    bins: sum('bins'), liquidity: String(sum('bins')),
    lowerPrice: views[0].lowerPrice, upperPrice: views[views.length - 1].upperPrice,
    inRange: info.activeBin >= views[0].lowerBinId && info.activeBin <= views[views.length - 1].upperBinId,
    closeEstA: sum('closeEstA'), closeEstB: sum('closeEstB'),
    feesAccruedA: sum('feesAccruedA'), feesAccruedB: sum('feesAccruedB'),
  };
  out.feesAccrued_quote = Number((out.feesAccruedA * info.uiPrice + out.feesAccruedB).toFixed(9));
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((out.closeEstA * info.uiPrice + out.closeEstB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number((out.feesAccrued_quote * info.quoteUsd).toFixed(6));
  }
  return out;
}

// Rent held in the position accounts. A DLMM position stores per-bin data,
// so a 387-bin band is a 38 KB account holding 0.2 SOL of rent: refundable
// on close, invisible to a book that counts only tokens. Equity must count
// it, or every wide DLMM open reads as a loss the size of the deposit.
async function rentOf(connection, pubkeys) {
  const infos = await connection.getMultipleAccountsInfo(pubkeys);
  return infos.reduce((n, a) => n + (a?.lamports ?? 0), 0) / 1e9;
}

async function solUsd(info) {
  if (info.nativeSide === 'A' && info.quoteUsd != null) return info.uiPrice * info.quoteUsd;
  if (info.nativeSide === 'B' && info.quoteUsd != null) return info.quoteUsd;
  return tokenUsd(NATIVE_MINT);
}

export async function status(positionArg, deps) {
  const pool = poolArg();
  return withRpc(async ({ connection, acting }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const info = await describe(pool, dlmm, connection);
    const { userPositions } = await dlmm.getPositionsByUserAndLbPair(acting.owner);
    if (!userPositions.length
        || (positionArg && !userPositions.some(p => p.publicKey.toBase58() === positionArg))) {
      console.log(JSON.stringify({ positions: 0, positionMint: null, pool }, null, 1));
      return null;
    }
    const out = unionView(userPositions, info);
    out.rentSol = Number((await rentOf(connection, userPositions.map(p => p.publicKey))).toFixed(9));
    const su = await solUsd(info);
    out.rentUsd = su != null ? Number((out.rentSol * su).toFixed(4)) : null;
    console.log(JSON.stringify(out, null, 1));
    return out;
  }, undefined, deps);
}

async function positions() {
  const pool = poolArg();
  return withRpc(async ({ connection, acting }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const { userPositions } = await dlmm.getPositionsByUserAndLbPair(acting.owner);
    console.log(JSON.stringify(userPositions.map(p => ({
      address: p.publicKey.toBase58(), pool,
      lowerBinId: p.positionData.lowerBinId, upperBinId: p.positionData.upperBinId,
      x: p.positionData.totalXAmount, y: p.positionData.totalYAmount,
    })), null, 1));
  });
}

// Sign `ixs` for `acting` on a fresh blockhash, send, and wait for
// `confirmed`. Returns the signature; throws if it failed on chain.
export async function sendIxs(connection, acting, ixs, extra = []) {
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
  const tx = acting.transaction(ixs, blockhash, extra);
  const signature = await connection.sendRawTransaction(tx.serialize());
  const r = await connection.confirmTransaction({ signature, blockhash, lastValidBlockHeight }, 'confirmed');
  if (r?.value?.err) throw new Error(`transaction ${signature} failed: ${JSON.stringify(r.value.err)}`);
  return signature;
}

// Sends in order. Throws only if NOTHING was sent; after the first send a
// failure comes back as {sigs, error} so the caller reports a partial result
// instead of letting withRpc retry the whole operation.
export async function sendAll(connection, txs, acting) {
  const sigs = [];
  for (const tx of Array.isArray(txs) ? txs : [txs]) {
    try {
      sigs.push(await sendIxs(connection, acting, tx.instructions));
    } catch (e) {
      // Nothing confirmed, but the send may still reach a node: never
      // rotated, never retried (AfterSignError).
      if (!sigs.length) throw new AfterSignError(`send failed after signing (not retried): ${e?.message ?? e}`);
      return { sigs, error: String(e?.message ?? e).slice(0, 300) };
    }
  }
  return { sigs, error: null };
}

function reportSent(base, r) {
  const out = { ...base, signature: r.sigs[r.sigs.length - 1] ?? null, signatures: r.sigs, sent: true };
  if (r.error) { out.partial = true; out.error = r.error; process.exitCode = 1; }
  console.log(JSON.stringify(out, null, 1));
}

// Simulate one built transaction without sending it. Signing is local; the
// report carries the program's verdict, so a dry run proves the instructions
// (in vault mode, the policy's verdict too). A wallet without the tokens
// fails here on the token transfer ("insufficient funds"), after every
// account and instruction has been checked.
async function simulate(connection, acting, ixs, extra) {
  try {
    const tx = acting.transaction(ixs, (await connection.getLatestBlockhash('confirmed')).blockhash, extra);
    const res = await connection.simulateTransaction(tx);
    const logs = res.value.logs ?? [];
    return { ok: res.value.err == null, err: res.value.err == null ? null : JSON.stringify(res.value.err),
      // the program's own words for a failure ("Error: insufficient funds")
      logError: logs.find(l => /Program log: (Error|AnchorError)/.test(l)) ?? null,
      unitsConsumed: res.value.unitsConsumed ?? null, logTail: logs.slice(-4) };
  } catch (e) {
    return { ok: false, err: String(e?.message ?? e).slice(0, 200), unitsConsumed: null, logTail: [] };
  }
}

// maxA and maxB are UI amounts (what the wallet shows); the deposit is sized
// in pool-native units at the pool price and reported back in UI units.
export async function open(pool, lower, upper, maxA, maxB, execute, deps) {
  return withRpc(async ({ connection, payer, acting }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const info = await describe(pool, dlmm, connection);
    assertWritable(info.mints);
    const lamports = await connection.getBalance(payer.publicKey);
    if (lamports / 1e9 < GAS_RESERVE_SOL) {
      throw new Error(`SOL below the ${GAS_RESERVE_SOL} gas reserve; a wallet that cannot pay fees cannot close its own position`);
    }
    const minBinId = dlmm.getBinIdFromPrice(Number(dlmm.toPricePerLamport(Number(lower))), true);
    const maxBinId = dlmm.getBinIdFromPrice(Number(dlmm.toPricePerLamport(Number(upper))), false);
    const width = maxBinId - minBinId + 1;
    if (width > POSITION_MAX_LENGTH) {
      throw new Error(`band ${lower}-${upper} spans ${width} bins at step ${info.step}; `
        + `a position holds at most ${POSITION_MAX_LENGTH}. Choose a narrower band.`);
    }
    if (info.activeBin < minBinId || info.activeBin > maxBinId) {
      throw new Error(`active bin ${info.activeBin} is outside ${minBinId}..${maxBinId}; the price moved`);
    }
    // A spot position spreads X over the bins above the active one and Y over
    // the bins below, so the deposit is whatever amounts are handed in. Take a
    // 50/50 split in quote terms, bounded by both caps.
    const price = info.price;
    let capA = uiToNative(Number(maxA), info.multiplierA), capB = uiToNative(Number(maxB), info.multiplierB);
    if (acting.mode === 'vault' && info.nativeSide) {
      const spare = Math.max(0, (await connection.getBalance(acting.owner)) / 1e9 - VAULT_SOL_FLOOR);
      if (info.nativeSide === 'A') capA = Math.min(capA, spare); else capB = Math.min(capB, spare);
    }
    let amtA = Math.min(capA, capB / price);
    let amtB = Math.min(capB, amtA * price);
    amtA = Math.min(amtA, amtB / price);
    const estA = amtA * info.multiplierA, estB = amtB * info.multiplierB;
    const approxUsd = (estA * info.uiPrice + estB) * (info.quoteUsd ?? 1);
    if (approxUsd > MAX_USD) throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
    if (!(amtA > 0 && amtB > 0)) throw new Error('nothing to deposit: one side is zero');

    const built = await dlmm.initializeMultiplePositionAndAddLiquidityByStrategy2(
      async (count) => Array.from({ length: count }, () => Keypair.generate()),
      new BN(Math.floor(amtA * 10 ** info.decimalsA).toString()),
      new BN(Math.floor(amtB * 10 ** info.decimalsB).toString()),
      { minBinId, maxBinId, strategyType: StrategyType.Spot },
      acting.owner, payer.publicKey, SLIPPAGE_BPS / 100);
    const parts = built.instructionsByPositions;
    const txCount = parts.reduce((n, p) => n + p.transactionInstructions.length, 0);
    const report = {
      pool, dex: 'meteora-dlmm', pair: `${info.symbolA}/${info.symbolB}`,
      lowerPrice: Number(lower), upperPrice: Number(upper), minBinId, maxBinId, bins: width,
      binLower: binPrice(minBinId, info.step, info.decimalsA, info.decimalsB),
      binUpper: binPrice(maxBinId + 1, info.step, info.decimalsA, info.decimalsB),
      tokenMaxA: Number(maxA), tokenMaxB: Number(maxB), tokenA: info.symbolA, tokenB: info.symbolB,
      depositEstA: estA, depositEstB: estB, price: info.price, uiPrice: info.uiPrice,
      multiplierA: info.multiplierA, multiplierB: info.multiplierB,
      approxUsd: Number(approxUsd.toFixed(2)),
      depositUsd: info.quoteUsd != null ? Number(approxUsd.toFixed(4)) : null,
      positions: parts.map(p => p.positionKeypair.publicKey.toBase58()),
      positionMint: parts[0]?.positionKeypair.publicKey.toBase58() ?? null,
      transactions: txCount,
      instructions: parts.reduce((n, p) => n + p.transactionInstructions.reduce((m, t) => m + t.length, 0), 0),
    };
    if (!execute) {
      // The first transaction only: the later ones act on the position it creates.
      const first = parts[0]?.transactionInstructions[0];
      const simulation = first ? await simulate(connection, acting, first, [parts[0].positionKeypair]) : null;
      console.log(JSON.stringify({ ...report, simulation, simulated: first ? `1 of ${txCount}` : 'none', sent: false }, null, 1));
      console.log('DRY RUN — instructions built. Pass --execute to sign and send.');
      return;
    }
    // Every transaction in order: each position's init, resizes, deposits.
    // Once the first has been sent this operation is never retried whole (a
    // retried open is a second position): a failure part-way is reported with
    // what landed, and `status` will show the loop what exists on chain.
    const sigs = [];
    try {
      for (const part of parts) {
        for (const ixs of part.transactionInstructions) {
          sigs.push(await sendIxs(connection, acting, ixs, [part.positionKeypair]));
        }
      }
    } catch (e) {
      console.log(JSON.stringify({ ...report, sent: sigs.length > 0, partial: true, signatures: sigs,
        signature: sigs[sigs.length - 1] ?? null, error: String(e?.message ?? e).slice(0, 300) }, null, 1));
      process.exitCode = 1;
      return;
    }
    console.log(JSON.stringify({ ...report, sent: true, signature: sigs[sigs.length - 1], signatures: sigs }, null, 1));
  }, undefined, deps);
}

// A position's rent goes back to the key that paid it. The payer pays it at
// open (initializeMultiplePosition...'s payer), but the SDK's close refunds
// the position's owner, which for a vault is the vault: each cycle would move
// the rent from the bot's payer key into the vault. With a key wallet the
// two are one key and nothing changes. close_position_if_empty and
// close_position2 name the rent receiver at account 2 (the vault policy
// pins it to vault or payer).
const CLOSE_DISCRIMINATORS = ['3b7cd4765b986e9d', 'ae5a2373ba2893e2'];
export function refundRentTo(ixs, payer) {
  for (const ix of ixs) {
    if (ix.programId.toBase58() === SOLANA.programs.meteora_dlmm
        && CLOSE_DISCRIMINATORS.includes(Buffer.from(ix.data.subarray(0, 8)).toString('hex'))) {
      ix.keys[2] = { ...ix.keys[2], pubkey: payer };
    }
  }
  return ixs;
}

// All of the wallet's positions on the pool, provided the one named is among
// them: the name is a check that the caller and the chain agree on which pool.
async function findPositions(dlmm, owner, address) {
  const { userPositions } = await dlmm.getPositionsByUserAndLbPair(owner);
  if (!userPositions.some(x => x.publicKey.toBase58() === address)) {
    throw new Error(`position ${address} not found for this wallet on this pool`);
  }
  return userPositions;
}

export async function harvest(address, execute, deps) {
  const pool = poolArg();
  return withRpc(async ({ connection, acting }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    assertWritable(await poolMints(connection, [dlmm.tokenX.publicKey.toBase58(), dlmm.tokenY.publicKey.toBase58()]));
    const ps = await findPositions(dlmm, acting.owner, address);
    const txs = await dlmm.claimAllSwapFee({ owner: acting.owner, positions: ps });
    if (!execute) {
      console.log(JSON.stringify({ mint: address, transactions: txs.length, sent: false }, null, 1));
      console.log('DRY RUN — pass --execute to collect fees.');
      return;
    }
    if (!txs.length) { console.log(JSON.stringify({ harvested: address, signature: null, note: 'nothing to claim' }, null, 1)); return; }
    reportSent({ harvested: address }, await sendAll(connection, txs, acting));
  }, undefined, deps);
}

export async function close(address, execute, deps) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer, acting }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    assertWritable(await poolMints(connection, [dlmm.tokenX.publicKey.toBase58(), dlmm.tokenY.publicKey.toBase58()]));
    const ps = await findPositions(dlmm, acting.owner, address);
    // All liquidity out, fees claimed, accounts closed: one batch per position.
    const txs = [];
    for (const p of ps) {
      const d = p.positionData;
      txs.push(...await dlmm.removeLiquidity({
        user: acting.owner, position: p.publicKey,
        fromBinId: d.lowerBinId, toBinId: d.upperBinId,
        bps: new BN(10000), shouldClaimAndClose: true,
      }));
    }
    for (const tx of txs) refundRentTo(tx.instructions, payer.publicKey);
    const tot = (k) => ps.reduce((n, p) => n + Number(p.positionData[k].toString()), 0);
    if (!execute) {
      console.log(JSON.stringify({ mint: address, positions: ps.length, transactions: txs.length,
        instructions: txs.reduce((n, t) => n + t.instructions.length, 0),
        quote: { tokenEstA: String(tot('totalXAmount')), tokenEstB: String(tot('totalYAmount')) },
        feesQuote: { feeOwedA: String(tot('feeX')), feeOwedB: String(tot('feeY')) },
        sent: false }, null, 1));
      console.log('DRY RUN — close instructions built. Pass --execute to send.');
      return;
    }
    reportSent({ closed: address }, await sendAll(connection, txs, acting));
  }, undefined, deps);
}

// --- rebalance: a swap on this pool -----------------------------------------

// What `owner` may sell of one token, in human units: the profile's sleeve
// (LPBOT_SLEEVE) of what it holds, less the native reserve. A vault is a PDA,
// so its token accounts are derived off the curve.
async function sellable(connection, acting, info) {
  const ata = spl.getAssociatedTokenAddressSync(new PublicKey(info.mint), acting.owner, true, new PublicKey(info.program));
  const a = await connection.getAccountInfo(ata);
  let raw = a ? a.data.readBigUInt64LE(64) : 0n;
  if (info.mint === NATIVE_MINT) raw += BigInt(await connection.getBalance(acting.owner));
  const total = sleeveCap(Number(raw) / 10 ** info.decimals, info.mint, parseSleeve(process.env.LPBOT_SLEEVE));
  const reserve = info.mint !== NATIVE_MINT ? 0 : acting.mode === 'vault' ? Math.max(GAS_RESERVE_SOL, VAULT_SOL_FLOOR) : GAS_RESERVE_SOL;
  const avail = Math.max(0, total - reserve);
  return { total, avail, usd: avail * info.usdPrice };
}
// The loop's `rebalance` (SWAP_HOOK.md) on the profile's own pool. A vault may
// swap only on pools its owners approved, so a vault profile's swaps come here
// rather than to Jupiter (lp/signers.route); a key wallet may use it too. Same
// plan (shared/rebalance_plan.mjs), the same value and impact checks and the
// same report fields as venues/orca/swap.mjs.
export async function rebalance(mintA, mintB, targetA, targetB, execute, deps) {
  const pool = poolArg();
  const tA = Number(targetA), tB = Number(targetB);
  if (!(tA >= 0 && tB >= 0)) throw new Refused(`targets must be non-negative dollars, got ${targetA} ${targetB}`);
  if (mintA === mintB) throw new Refused('mintA and mintB are the same token');
  const hints = parseHints(process.env.LPBOT_TOKEN_HINTS);
  return withRpc(async ({ connection, acting }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const info = await describe(pool, dlmm, connection);
    assertWritable(info.mints);
    if (new Set([mintA, mintB, info.mintA, info.mintB]).size !== 2) {
      throw new Refused(`swap ${mintA}/${mintB} is not this pool's pair (${info.mintA}/${info.mintB}); refusing`);
    }
    if (info.multiplierA !== 1 || info.multiplierB !== 1) throw new Refused('a scaled Token-2022 mint: no swap here; refusing');
    // Prices: the loop's hints (the prices it sized the targets with), else
    // the pool's own price and the quote token's USD value.
    const side = (mint) => {
      const x = mint === info.mintA;
      const decimals = x ? info.decimalsA : info.decimalsB;
      const h = hints.get(mint);
      if (h && h.decimals !== decimals) throw new Refused(`${h.symbol ?? mint} decimals ${h.decimals} disagree with the chain's ${decimals}; refusing`);
      const own = info.quoteUsd == null ? null : x ? info.uiPrice * info.quoteUsd : info.quoteUsd;
      return { mint, symbol: x ? info.symbolA : info.symbolB, decimals, usdPrice: h?.usdPrice ?? own,
        program: x ? info.tokenProgramA : info.tokenProgramB };
    };
    const infoA = side(mintA), infoB = side(mintB);
    for (const i of [infoA, infoB]) if (i.usdPrice == null) throw new Refused(`no USD price for ${i.symbol}; cannot size a rebalance`);
    const [balA, balB] = await Promise.all([sellable(connection, acting, infoA), sellable(connection, acting, infoB)]);
    const balances = { owner: acting.owner.toBase58(), custody: acting.mode,
      A: { mint: infoA.mint, symbol: infoA.symbol, amount: balA.total, sellable: balA.avail, usd: Number(balA.usd.toFixed(4)), usdPrice: infoA.usdPrice },
      B: { mint: infoB.mint, symbol: infoB.symbol, amount: balB.total, sellable: balB.avail, usd: Number(balB.usd.toFixed(4)), usdPrice: infoB.usdPrice },
      targetUsdA: tA, targetUsdB: tB };
    const plan = planRebalance(balA.usd, balB.usd, tA, tB);
    const s = plan && planSwap(plan, infoA, infoB, balA.avail, balB.avail);
    if (!plan || !(s.rawIn > 0n)) {
      const out = noopReport(plan ? 'the amount to sell rounds to zero' : `both sides within ${TARGET_TOLERANCE * 100}% of target or nothing to sell`, balances);
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    const swapForY = s.sellInfo.mint === info.mintA;
    const quote = dlmm.swapQuote(new BN(s.rawIn.toString()), swapForY, new BN(SLIPPAGE_BPS), await dlmm.getBinArrayForSwap(swapForY));
    if (BigInt(quote.consumedInAmount.toString()) !== s.rawIn) throw new Refused('the pool cannot take the whole amount in its loaded bins; refusing');
    const human = (raw, d) => Number(BigInt(raw.toString())) / 10 ** d;
    const inUsd = human(s.rawIn, s.sellInfo.decimals) * s.sellInfo.usdPrice;
    const estUsd = human(quote.outAmount, s.buyInfo.decimals) * s.buyInfo.usdPrice;
    const minUsd = human(quote.minOutAmount, s.buyInfo.decimals) * s.buyInfo.usdPrice;
    if (!valueLossOk(inUsd, estUsd)) throw new Refused(`the pool pays $${estUsd.toFixed(4)} for $${inUsd.toFixed(4)}; more than ${MAX_VALUE_LOSS * 100}% below fair value; refusing`);
    if (!valueLossOk(inUsd, minUsd, MAX_VALUE_LOSS + SLIPPAGE_BPS / 1e4)) throw new Refused(`the pool pays $${minUsd.toFixed(4)} at worst for $${inUsd.toFixed(4)}; refusing`);
    const impact = Number(quote.priceImpact.toString()) / 100;          // the SDK gives percent
    checkImpact(impact);
    const built = await dlmm.swap({ inToken: new PublicKey(s.sellInfo.mint), outToken: new PublicKey(s.buyInfo.mint),
      inAmount: new BN(s.rawIn.toString()), minOutAmount: quote.minOutAmount, lbPair: new PublicKey(pool),
      user: acting.owner, binArraysPubkey: quote.binArraysPubkey });
    const report = { ...balances, mode: plan.mode, sellSide: plan.sellSide, buySide: plan.buySide,
      pool, dex: 'meteora-dlmm', routePlan: ['Meteora DLMM'],
      sold: { mint: s.sellInfo.mint, symbol: s.sellInfo.symbol, amount: s.amount, usd: Number(inUsd.toFixed(4)) },
      bought: { mint: s.buyInfo.mint, symbol: s.buyInfo.symbol, amount: human(quote.outAmount, s.buyInfo.decimals), usd: Number(estUsd.toFixed(4)) },
      minOutAmount: human(quote.minOutAmount, s.buyInfo.decimals), priceImpactPct: Number(impact.toFixed(8)),
      slippageBps: SLIPPAGE_BPS, swapUsdValue: Number(inUsd.toFixed(4)), signature: null, sent: false };
    if (!execute) {
      const simulation = await simulate(connection, acting, built.instructions, []);
      console.log(JSON.stringify({ ...report, simulation }, null, 1));
      console.log('DRY RUN — swap built and simulated. Pass --execute to sign and send.');
      return report;
    }
    guard();
    const r = await sendAll(connection, [built], acting);
    const out = { ...report, signature: r.sigs[0] ?? null, sent: true };
    if (r.error) { out.partial = true; out.error = r.error; process.exitCode = 1; }
    console.log(JSON.stringify(out, null, 1));
    return out;
  }, undefined, deps);
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
  if (cmd === 'harvest') return harvest(rest[0], execute);
  if (cmd === 'close') return close(rest[0], execute);
  if (cmd === 'open') return open(rest[0], rest[1], rest[2], rest[3], rest[4], execute);
  if (cmd === 'rebalance') return rebalance(rest[0], rest[1], rest[2], rest[3], execute);
  if (cmd === 'pool') {
    const pool = poolArg(rest[0]);
    const connection = new Connection(ENDPOINTS[0], 'confirmed');
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    return console.log(JSON.stringify(await describe(pool, dlmm, connection), null, 1));
  }
  console.log('commands: balance [pool] | positions | status [position] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <position> [--execute] '
    + '| close <position> [--execute] | rebalance <mintA> <mintB> <usdA> <usdB> [--execute]   (pool via --pool or LPBOT_POOL)');
}

// The CLI runs only when node starts this file; a test import runs nothing.
if (isEntry(import.meta.url)) main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
