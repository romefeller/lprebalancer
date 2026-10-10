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
// Commands:
//   node venues/meteora_dlmm/signer.mjs balance [pool]
//   node venues/meteora_dlmm/signer.mjs positions
//   node venues/meteora_dlmm/signer.mjs status [position]
//   node venues/meteora_dlmm/signer.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node venues/meteora_dlmm/signer.mjs harvest <position> [--execute]
//   node venues/meteora_dlmm/signer.mjs close <position> [--execute]
import fs from 'node:fs';
import path from 'node:path';
import { assertNotHalted } from '../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../bot_root.mjs';
import { createRequire } from 'node:module';
import { waitTurn } from '../jupiter/gate.mjs';
import { endpoints, overEndpoints, isEntry, AfterSignError } from '../../shared/rpc_policy.mjs';
import { readMints, rawToUi, uiToNative, uiPrice, assertWritable, mintFields } from '../../shared/token2022.mjs';
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };
import { JUPITER, jupiterHeaders } from '../jupiter/api.mjs';

// The package's ESM build imports a directory and fails to load under Node 24;
// the CommonJS build resolves cleanly.
const require = createRequire(import.meta.url);
const dlmmPkg = require('@meteora-ag/dlmm');
const DLMM = dlmmPkg.default ?? dlmmPkg;
const { StrategyType } = dlmmPkg;
const { Connection, Keypair, PublicKey, Transaction, sendAndConfirmTransaction } = require('@solana/web3.js');
const { BN } = require('@coral-xyz/anchor');

// A keyed endpoint from the environment first. Indexed reads
// (getParsedTokenAccountsByOwner) never go to an endpoint that refuses them.
export const ENDPOINTS = endpoints(process.env, { indexed: true });

const MAX_USD = Number(process.env.LPBOT_MAX_USD ?? 260);
const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.02);
const POSITION_MAX_LENGTH = 1400;      // bins per position, the program's limit

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
  return { connection, payer };
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
  return withRpc(async ({ connection, payer }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const info = await describe(pool, dlmm, connection);
    const lamports = await connection.getBalance(payer.publicKey);
    const out = { owner: payer.publicKey.toBase58(), sol: lamports / 1e9, pool, dex: 'meteora-dlmm',
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

async function status(positionArg) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const info = await describe(pool, dlmm, connection);
    const { userPositions } = await dlmm.getPositionsByUserAndLbPair(payer.publicKey);
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
  });
}

async function positions() {
  const pool = poolArg();
  return withRpc(async ({ connection, payer }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    const { userPositions } = await dlmm.getPositionsByUserAndLbPair(payer.publicKey);
    console.log(JSON.stringify(userPositions.map(p => ({
      address: p.publicKey.toBase58(), pool,
      lowerBinId: p.positionData.lowerBinId, upperBinId: p.positionData.upperBinId,
      x: p.positionData.totalXAmount, y: p.positionData.totalYAmount,
    })), null, 1));
  });
}

// Sends in order. Throws only if NOTHING was sent; after the first send a
// failure comes back as {sigs, error} so the caller reports a partial result
// instead of letting withRpc retry the whole operation.
export async function sendAll(connection, txs, signers) {
  const sigs = [];
  for (const tx of Array.isArray(txs) ? txs : [txs]) {
    try {
      sigs.push(await sendAndConfirmTransaction(connection, tx, signers, { commitment: 'confirmed' }));
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
// report carries the program's verdict, so a dry run proves the instructions.
// A wallet without the tokens fails here on the token transfer ("insufficient
// funds"), after every account and instruction has been checked.
async function simulate(connection, ixs, signers, feePayer) {
  try {
    const tx = new Transaction().add(...ixs);
    tx.feePayer = feePayer;
    tx.recentBlockhash = (await connection.getLatestBlockhash('confirmed')).blockhash;
    tx.sign(...signers);
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

// The keypairs a transaction needs: the payer, plus the new position's key
// when an instruction expects its signature.
function signersOf(ixs, part, payer) {
  return ixs.some(ix => ix.keys.some(k => k.isSigner && k.pubkey.equals(part.positionKeypair.publicKey)))
    ? [payer, part.positionKeypair] : [payer];
}

// maxA and maxB are UI amounts (what the wallet shows); the deposit is sized
// in pool-native units at the pool price and reported back in UI units.
async function open(pool, lower, upper, maxA, maxB, execute) {
  return withRpc(async ({ connection, payer }) => {
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
    const capA = uiToNative(Number(maxA), info.multiplierA), capB = uiToNative(Number(maxB), info.multiplierB);
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
      payer.publicKey, payer.publicKey, SLIPPAGE_BPS / 100);
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
      const simulation = first ? await simulate(connection, first, signersOf(first, parts[0], payer), payer.publicKey) : null;
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
          const tx = new Transaction().add(...ixs);
          tx.feePayer = payer.publicKey;
          sigs.push(await sendAndConfirmTransaction(connection, tx, signersOf(ixs, part, payer), { commitment: 'confirmed' }));
        }
      }
    } catch (e) {
      console.log(JSON.stringify({ ...report, sent: sigs.length > 0, partial: true, signatures: sigs,
        signature: sigs[sigs.length - 1] ?? null, error: String(e?.message ?? e).slice(0, 300) }, null, 1));
      process.exitCode = 1;
      return;
    }
    console.log(JSON.stringify({ ...report, sent: true, signature: sigs[sigs.length - 1], signatures: sigs }, null, 1));
  });
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

async function harvest(address, execute) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    assertWritable(await poolMints(connection, [dlmm.tokenX.publicKey.toBase58(), dlmm.tokenY.publicKey.toBase58()]));
    const ps = await findPositions(dlmm, payer.publicKey, address);
    const txs = await dlmm.claimAllSwapFee({ owner: payer.publicKey, positions: ps });
    if (!execute) {
      console.log(JSON.stringify({ mint: address, transactions: txs.length, sent: false }, null, 1));
      console.log('DRY RUN — pass --execute to collect fees.');
      return;
    }
    if (!txs.length) { console.log(JSON.stringify({ harvested: address, signature: null, note: 'nothing to claim' }, null, 1)); return; }
    reportSent({ harvested: address }, await sendAll(connection, txs, [payer]));
  });
}

async function close(address, execute) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer }) => {
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    assertWritable(await poolMints(connection, [dlmm.tokenX.publicKey.toBase58(), dlmm.tokenY.publicKey.toBase58()]));
    const ps = await findPositions(dlmm, payer.publicKey, address);
    // All liquidity out, fees claimed, accounts closed: one batch per position.
    const txs = [];
    for (const p of ps) {
      const d = p.positionData;
      txs.push(...await dlmm.removeLiquidity({
        user: payer.publicKey, position: p.publicKey,
        fromBinId: d.lowerBinId, toBinId: d.upperBinId,
        bps: new BN(10000), shouldClaimAndClose: true,
      }));
    }
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
    reportSent({ closed: address }, await sendAll(connection, txs, [payer]));
  });
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
  if (cmd === 'pool') {
    const pool = poolArg(rest[0]);
    const connection = new Connection(ENDPOINTS[0], 'confirmed');
    const dlmm = await DLMM.create(connection, new PublicKey(pool));
    return console.log(JSON.stringify(await describe(pool, dlmm, connection), null, 1));
  }
  console.log('commands: balance [pool] | positions | status [position] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <position> [--execute] '
    + '| close <position> [--execute]   (pool via --pool or LPBOT_POOL)');
}

// The CLI runs only when node starts this file; a test import runs nothing.
if (isEntry(import.meta.url)) main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
