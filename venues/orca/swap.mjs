// Rebalancer — the Orca fallback swap. One job: the same `rebalance` as
// venues/jupiter/swap.mjs, through one Orca Whirlpool and no aggregator API. When
// Jupiter's free API refuses (429, outage), the loop can call this script with
// the same arguments and read the same JSON. Same conventions as the signers
// (SIGNER_CONTRACT.md): HALT guard, key read from WALLET_SECRET_PATH and never
// printed, one JSON object on stdout, `ERROR: <message>` on stderr with exit 1,
// dry run by default.
//
// Command:
//   node venues/orca/swap.mjs rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute] [--pool <whirlpool>]
//
// Without --pool the pair must be in DEFAULT_POOLS. The pool must be owned by
// the Whirlpool program and hold exactly mintA and mintB, else nothing runs.
//
// Environment (as venues/jupiter/swap.mjs gets it from rebalancer.chain):
//   WALLET_SECRET_PATH, SOLANA_RPC_URL, LPBOT_SLIPPAGE_BPS, LPBOT_GAS_RESERVE_SOL,
//   LPBOT_TOKEN_HINTS {"<mint>": {usd, decimals, symbol}}, LPBOT_MAX_VALUE_LOSS,
//   LPBOT_MAX_IMPACT, LPBOT_PRIORITY_MAX_LAMPORTS.
//   LPBOT_OWNER (dry run only): a public key. Without WALLET_SECRET_PATH, a dry
//   run quotes and simulates for this address and reads no key at all.
//
// Order of work, all before any signature:
//   HALT -> pool owner and mints -> decimals from chain -> balances (gas reserve
//   kept) -> planRebalance (shared/rebalance_plan.mjs) -> SDK exact-in
//   quote at LPBOT_SLIPPAGE_BPS -> quote check, value check at the hint prices,
//   price impact -> instructions -> instruction allow-list and swap-data check
//   -> priority fee cap -> simulation with balance deltas -> (dry run: report).
// With --execute: HALT again, quote age, sign, send and re-send the same
// signed bytes until they confirm or lastValidBlockHeight passes. Never a
// second transaction.
//
// SOL: the SDK's 'ata' wrapping strategy. The wSOL ATA is created if missing,
// funded with the SOL to sell (transfer + SyncNative) and closed at the end, so
// bought SOL arrives as native SOL. The SDK closes the ATA only if it created
// it; this script also closes a wSOL ATA that already existed, so no SOL stays
// wrapped. Its balance counts as SOL for the same reason.
//
// Reads: getAccountInfo / getMultipleAccounts / getEpochInfo / simulate /
// getRecentPrioritizationFees / getSignatureStatuses / getBlockHeight only. No indexed read (getTokenAccountsByOwner),
// so every endpoint from rpc_policy.endpoints() can serve it.
import fs from 'node:fs';
import path from 'node:path';
import { assertNotHalted } from '../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../bot_root.mjs';
import { createRequire } from 'node:module';

import { endpoints, overEndpoints, JupiterError, AfterSignError, isEntry } from '../../shared/rpc_policy.mjs';
import { priorityFeeLamports, verifyPriorityFee, PRIORITY_MAX_LAMPORTS, parseSleeve, sleeveCap } from '../jupiter/swap.mjs';
import { planRebalance, TARGET_TOLERANCE } from '../../shared/rebalance_plan.mjs';
import { NeverLanded, sendUntilLanded } from '../../shared/tx_send.mjs';
import SOLANA from '../../chains/solana/solana.json' with { type: 'json' };
import VENUE from './venue.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Connection, Keypair, PublicKey, VersionedTransaction, TransactionMessage, TransactionInstruction,
  ComputeBudgetProgram } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

export { priorityFeeLamports, verifyPriorityFee, PRIORITY_MAX_LAMPORTS };

export const HALT = path.join(BOT_ROOT, 'HALT');
// No indexed read here (see the header): every endpoint is eligible.
const ENDPOINTS = endpoints(process.env, { indexed: false });

export const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.05);
export const MAX_IMPACT = Number(process.env.LPBOT_MAX_IMPACT ?? 0.01);          // ratio: 0.01 = 1%
export const MAX_VALUE_LOSS = Number(process.env.LPBOT_MAX_VALUE_LOSS ?? 0.02);  // 2% below fair value
const QUOTE_MAX_AGE_MS = 20_000;
const SOL_OVERHEAD_LAMPORTS = 10_000_000n;     // fees + temporary account rent, 0.01 SOL at most
const CU_DRAFT_LIMIT = 1_400_000;
const CU_PRICE_FLOOR = 20_000;                 // micro-lamports per unit
const CU_PRICE_DEFAULT = 50_000;               // when the node reports no recent fees

export const NATIVE_MINT = SOLANA.native_mint;
export const USDC_MINT = SOLANA.usdc_mint;
export const WHIRLPOOL_PROGRAM = SOLANA.programs.orca_whirlpool;
const COMPUTE_BUDGET = SOLANA.programs.compute_budget;
const SYSTEM = SOLANA.programs.system;
const TOKEN = SOLANA.programs.token;
const TOKEN_2022 = SOLANA.programs.token_2022;
const ATA = SOLANA.programs.associated_token;
export const ALLOWED_PROGRAMS = new Set([COMPUTE_BUDGET, SYSTEM, TOKEN, TOKEN_2022, ATA, WHIRLPOOL_PROGRAM]);
export const SWAP_V2_DISCRIMINATOR = Buffer.from([43, 4, 237, 11, 26, 201, 30, 98]);

// Pair -> whirlpool, keyed "mintA/mintB" as venue.json lists it (defaultPool
// also tries the other order).
export const DEFAULT_POOLS = Object.fromEntries(VENUE.fallback_pools.map(p => [`${p.mint_a}/${p.mint_b}`, p.pool]));
export function defaultPool(mintA, mintB) {
  return DEFAULT_POOLS[`${mintA}/${mintB}`] ?? DEFAULT_POOLS[`${mintB}/${mintA}`] ?? null;
}

// USDC and USDT only: the venue signers also count PYUSD and USDS as stable.
const STABLE_MINTS = new Set(VENUE.fallback_stable_mints);

// A refusal or an Orca/SDK answer is the same on every endpoint. JupiterError
// is rpc_policy's class for "an API answer, not an RPC problem": always fatal
// in the endpoint loop, whatever the text (a "$500.12" in a refusal must not
// read as an HTTP 500 and rotate).
export class Refused extends JupiterError {}
// A transaction left this process: never retried.
export class SentError extends AfterSignError {}

export function guard(haltPath = HALT, env = process.env) {
  if (fs.existsSync(haltPath)) throw new Refused(`HALT present: ${fs.readFileSync(haltPath, 'utf8').trim()}`);
  try { assertNotHalted(path.dirname(haltPath), env); } catch (e) { throw new Refused(e.message); }   // this profile's HALT
}

// --- pure helpers ---------------------------------------------------------------
export function toRaw(human, decimals) {
  const s = typeof human === 'number' ? human.toFixed(decimals) : String(human);
  if (!/^\d*\.?\d*$/.test(s) || s === '' || s === '.') throw new Refused(`bad amount: ${human}`);
  const [ip = '0', fp = ''] = s.split('.');
  return BigInt((ip || '0') + fp.slice(0, decimals).padEnd(decimals, '0'));
}
export const toHuman = (raw, decimals) => Number(raw) / 10 ** decimals;

export function parseHints(json) {
  const out = new Map();
  try {
    for (const [m, h] of Object.entries(JSON.parse(json || '{}'))) {
      if (h && Number(h.usd) > 0 && Number.isInteger(h.decimals)) {
        out.set(m, { mint: m, symbol: h.symbol || m.slice(0, 6), decimals: h.decimals, usdPrice: Number(h.usd) });
      }
    }
  } catch { /* a bad hint is ignored; the price is looked up instead */ }
  return out;
}

// The pool must be a Whirlpool and hold exactly the two requested mints.
export function checkPool(pool, mintA, mintB) {
  if (pool.programAddress !== WHIRLPOOL_PROGRAM) throw new Refused(`pool ${pool.address} is owned by ${pool.programAddress}, not the Whirlpool program; refusing`);
  const held = [String(pool.tokenMintA), String(pool.tokenMintB)].sort();
  const asked = [mintA, mintB].sort();
  if (mintA === mintB || held[0] !== asked[0] || held[1] !== asked[1]) {
    throw new Refused(`pool ${pool.address} holds ${held.join('/')}, not ${asked.join('/')}; refusing`);
  }
  return true;
}

// Which way the swap goes in the pool: selling token A of the pool is a->b.
export function direction(sellMint, buyMint, poolMintA, poolMintB) {
  if (sellMint === poolMintA && buyMint === poolMintB) return { aToB: true };
  if (sellMint === poolMintB && buyMint === poolMintA) return { aToB: false };
  throw new Refused(`swap ${sellMint} -> ${buyMint} is not this pool's pair; refusing`);
}

// The plan in planRebalance's terms -> the sell side, its amount (human, floored
// to decimals, never above what is sellable) and the buy side.
export function planSwap(plan, infoA, infoB, sellableA, sellableB) {
  const sellInfo = plan.sellSide === 'A' ? infoA : infoB;
  const buyInfo = plan.sellSide === 'A' ? infoB : infoA;
  const avail = plan.sellSide === 'A' ? sellableA : sellableB;
  const amount = Math.min(plan.sellUsd / sellInfo.usdPrice, avail);
  const raw = amount > 0 ? BigInt(Math.floor(amount * 10 ** sellInfo.decimals)) : 0n;
  return { sellInfo, buyInfo, rawIn: raw, amount: toHuman(raw, sellInfo.decimals) };
}

// Pool spot price as raw output per raw input. sqrtPrice is Q64.64 of B per A.
export function spotOutPerIn(sqrtPriceX64, aToB) {
  const p = (Number(BigInt(sqrtPriceX64)) / 2 ** 64) ** 2;
  return aToB ? p : 1 / p;
}

// Price impact as a ratio (0.001 = 0.1%), net of the trading fee, like
// Jupiter's priceImpactPct: how far the estimated output falls below the spot
// price applied to the input after the fee.
export function priceImpact(quote, sqrtPriceX64, aToB) {
  const net = Number(BigInt(quote.tokenIn) - BigInt(quote.tradeFee ?? 0n));
  const ideal = net * spotOutPerIn(sqrtPriceX64, aToB);
  if (!(ideal > 0)) return 1;
  return Math.max(0, 1 - Number(BigInt(quote.tokenEstOut)) / ideal);
}

// Output value at least (1 - maxLoss) of the input value, both at hint prices.
export function valueLossOk(inUsd, outUsd, maxLoss = MAX_VALUE_LOSS) {
  return Number.isFinite(inUsd) && Number.isFinite(outUsd) && inUsd >= 0 && outUsd >= inUsd * (1 - maxLoss);
}

// The SDK's quote must be the exact-in swap asked for, with our slippage, and
// worth what goes in: the estimate within MAX_VALUE_LOSS of fair value, the
// guaranteed minimum within MAX_VALUE_LOSS plus the slippage.
export function verifyQuote(quote, inInfo, outInfo, rawIn, { slippageBps = SLIPPAGE_BPS, maxLoss = MAX_VALUE_LOSS } = {}) {
  const tokenIn = BigInt(quote.tokenIn), est = BigInt(quote.tokenEstOut), min = BigInt(quote.tokenMinOut);
  if (tokenIn !== BigInt(rawIn)) throw new Refused(`quote tokenIn ${tokenIn} differs from the request ${rawIn}; refusing`);
  if (!(est > 0n)) throw new Refused('quote pays nothing; refusing');
  if (min > est) throw new Refused('quote minimum exceeds its own estimate; refusing');
  const floor = est * BigInt(10_000 - slippageBps) / 10_000n;
  if (min + 1n < floor) throw new Refused(`quote minimum ${min} is below ${slippageBps} bps slippage of ${est}; refusing`);
  if (inInfo.usdPrice == null || outInfo.usdPrice == null) throw new Refused('no USD price for a side; cannot check the value; refusing');
  const inUsd = toHuman(tokenIn, inInfo.decimals) * inInfo.usdPrice;
  const estUsd = toHuman(est, outInfo.decimals) * outInfo.usdPrice;
  const minUsd = toHuman(min, outInfo.decimals) * outInfo.usdPrice;
  if (!valueLossOk(inUsd, estUsd, maxLoss)) {
    throw new Refused(`quote pays $${estUsd.toFixed(4)} for $${inUsd.toFixed(4)}; more than ${maxLoss * 100}% below fair value; refusing`);
  }
  if (!valueLossOk(inUsd, minUsd, maxLoss + slippageBps / 1e4)) {
    throw new Refused(`quote pays $${minUsd.toFixed(4)} at worst for $${inUsd.toFixed(4)}; refusing`);
  }
  return { inUsd, estUsd, minUsd };
}

export function checkImpact(impact, max = MAX_IMPACT) {
  if (!(impact <= max)) throw new Refused(`price impact ${(impact * 100).toFixed(4)}% exceeds the ${max * 100}% limit; refusing`);
  return true;
}

// Compute-unit price: 75th percentile of the pool's recent non-zero fees,
// floored, and clamped so that limit x price stays inside the cap.
export function chooseCuPrice(recent, units, cap = PRIORITY_MAX_LAMPORTS) {
  const fees = (recent ?? []).map(r => Number(r.prioritizationFee ?? r)).filter(f => f > 0).sort((a, b) => a - b);
  let price = fees.length ? fees[Math.min(fees.length - 1, Math.floor(fees.length * 0.75))] : CU_PRICE_DEFAULT;
  price = Math.max(price, CU_PRICE_FLOOR);
  const ceiling = Math.floor(Number(cap) * 1_000_000 / Math.max(1, units));
  return Math.max(0, Math.min(price, ceiling));
}

export function cuLimit(unitsConsumed) {
  return Math.min(CU_DRAFT_LIMIT, Math.ceil(Number(unitsConsumed) * 1.25) + 5_000);
}

// @solana/kit instruction -> web3.js instruction. Role bit 1 = writable, 2 = signer.
export function kitToWeb3(ix) {
  return new TransactionInstruction({
    programId: new PublicKey(String(ix.programAddress)),
    keys: (ix.accounts ?? []).map(a => ({ pubkey: new PublicKey(String(a.address)), isSigner: (a.role & 2) !== 0, isWritable: (a.role & 1) !== 0 })),
    data: Buffer.from(ix.data ?? []),
  });
}

// Close the wSOL ATA at the end if the SDK did not (it closes only an ATA it created).
export function withWsolClose(ixs, wsolAta, owner) {
  const closes = ixs.some(ix => ix.programId.toBase58() === TOKEN && ix.data[0] === 9 && ix.keys[0]?.pubkey.toBase58() === wsolAta);
  if (closes) return ixs;
  return [...ixs, spl.createCloseAccountInstruction(new PublicKey(wsolAta), new PublicKey(owner), new PublicKey(owner))];
}

export function decodeSwapV2(data) {
  const d = Buffer.from(data);
  if (d.length < 42 || !d.subarray(0, 8).equals(SWAP_V2_DISCRIMINATOR)) return null;
  return {
    amount: d.readBigUInt64LE(8), otherAmountThreshold: d.readBigUInt64LE(16),
    sqrtPriceLimit: d.readBigUInt64LE(24) + (d.readBigUInt64LE(32) << 64n),
    amountSpecifiedIsInput: d[40] === 1, aToB: d[41] === 1,
  };
}

// Every top-level instruction is checked: program allow-list, and per program
// only the instructions a swap needs. Programs cannot come from lookup tables
// (and this script uses none), so the static keys are complete.
//   want: { payer, pool, rawIn, minOut, aToB, ownerA, ownerB, vaultA, vaultB, wsolAta }
export function verifyTxShape(tx, want) {
  const msg = tx.message;
  const keys = msg.staticAccountKeys.map(k => k.toBase58());
  if (keys[0] !== want.payer) throw new Refused('transaction fee payer is not the wallet; refusing');
  if (msg.header.numRequiredSignatures !== 1) throw new Refused('transaction needs signers other than the wallet; refusing');
  if ((msg.addressTableLookups ?? []).length) throw new Refused('transaction uses lookup tables; refusing');
  let swaps = 0;
  for (const ix of msg.compiledInstructions) {
    const pid = keys[ix.programIdIndex];
    if (pid === undefined || !ALLOWED_PROGRAMS.has(pid)) throw new Refused(`transaction calls ${pid ?? 'a program from a lookup table'}; refusing`);
    const d = Buffer.from(ix.data);
    const acc = (i) => keys[ix.accountKeyIndexes[i]];
    if (pid === COMPUTE_BUDGET) {
      if (d[0] !== 2 && d[0] !== 3) throw new Refused(`compute-budget instruction ${d[0]} not allowed; refusing`);
    } else if (pid === SYSTEM) {
      // Only the SOL -> wSOL ATA transfer of the wrapping.
      if (!(d.length >= 12 && d.readUInt32LE(0) === 2 && acc(0) === want.payer && acc(1) === want.wsolAta)) throw new Refused('system instruction other than the wSOL funding transfer; refusing');
    } else if (pid === TOKEN || pid === TOKEN_2022) {
      const sync = d.length === 1 && d[0] === 17 && acc(0) === want.wsolAta;
      const close = d.length === 1 && d[0] === 9 && acc(0) === want.wsolAta && acc(1) === want.payer && acc(2) === want.payer;
      if (!sync && !close) throw new Refused(`token instruction ${d[0]} not allowed (only SyncNative / CloseAccount of the wSOL ATA to the wallet); refusing`);
    } else if (pid === ATA) {
      if (!(d.length === 0 || d[0] === 0 || d[0] === 1) || acc(0) !== want.payer || acc(2) !== want.payer) throw new Refused('ATA instruction other than create-for-the-wallet; refusing');
    } else if (pid === WHIRLPOOL_PROGRAM) {
      const s = decodeSwapV2(d);
      if (!s) throw new Refused('Whirlpool instruction other than swapV2; refusing');
      swaps += 1;
      // Accounts: 3 tokenAuthority, 4 whirlpool, 7 ownerA, 8 vaultA, 9 ownerB, 10 vaultB.
      if (acc(3) !== want.payer || acc(4) !== want.pool) throw new Refused('swap authority or pool differs; refusing');
      if (acc(7) !== want.ownerA || acc(9) !== want.ownerB) throw new Refused('swap pays to accounts not the wallet\'s; refusing');
      if (acc(8) !== want.vaultA || acc(10) !== want.vaultB) throw new Refused('swap vaults are not the pool\'s; refusing');
      if (!s.amountSpecifiedIsInput || s.amount !== BigInt(want.rawIn) || s.otherAmountThreshold !== BigInt(want.minOut) || s.aToB !== want.aToB) {
        throw new Refused('swap data differs from the verified quote; refusing');
      }
    }
  }
  if (swaps !== 1) throw new Refused(`transaction has ${swaps} swaps, not one; refusing`);
  return true;
}

// Simulated balance changes against the quote. sol = wallet lamports + wSOL ATA lamports.
export function checkDeltas({ inNative, outNative, bothSpl }, d, rawIn, minOut) {
  if (d.inDrop > BigInt(rawIn) + (inNative ? SOL_OVERHEAD_LAMPORTS : 0n)) throw new Refused(`simulation takes ${d.inDrop}, more than ${rawIn}; refusing`);
  if (d.outGain < BigInt(minOut) - (outNative ? SOL_OVERHEAD_LAMPORTS : 0n)) throw new Refused(`simulation pays ${d.outGain}, under the minimum ${minOut}; refusing`);
  if (bothSpl && d.solDrop > SOL_OVERHEAD_LAMPORTS) throw new Refused(`simulation spends ${d.solDrop} lamports of SOL; refusing`);
  return true;
}

export function noopReport(reason, balances) {
  return { noop: true, reason, ...balances };
}

export const USAGE = 'commands: rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute] [--pool <whirlpool>]   (targets in USD)';

export function parseArgs(argv) {
  const execute = argv.includes('--execute');
  let pool = null;
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--execute') continue;
    if (argv[i] === '--pool') { pool = argv[++i] ?? null; continue; }
    rest.push(argv[i]);
  }
  const [cmd, ...args] = rest;
  return { cmd, args, execute, pool };
}

// --- chain I/O ------------------------------------------------------------------
async function secretBytes() {
  const p = process.env.WALLET_SECRET_PATH;
  const raw = fs.readFileSync(p, 'utf8').trim();
  if (raw.startsWith('[')) return Uint8Array.from(JSON.parse(raw));
  const bs58 = (await import('bs58')).default;
  return bs58.decode(raw);
}

// The signer for --execute; a public key only for a dry run without a key file.
export async function loadOwner(execute, env = process.env, readSecret = secretBytes) {
  const pinned = env.LPBOT_OWNER ? new PublicKey(env.LPBOT_OWNER).toBase58() : null;
  if (env.WALLET_SECRET_PATH) {
    const payer = Keypair.fromSecretKey(await readSecret());
    if (pinned && payer.publicKey.toBase58() !== pinned) throw new Refused('LPBOT_OWNER differs from the key in WALLET_SECRET_PATH; refusing');
    return { owner: payer.publicKey, payer };
  }
  if (execute) throw new Refused('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  if (!pinned) throw new Refused('dry run needs WALLET_SECRET_PATH or LPBOT_OWNER');
  return { owner: new PublicKey(pinned), payer: null };
}

async function geckoUsd(mint) {
  const r = await fetch(`https://api.geckoterminal.com/api/v2/simple/networks/solana/token_price/${mint}`,
    { headers: { accept: 'application/json;version=20230203' } });
  const p = Number((await r.json().catch(() => null))?.data?.attributes?.token_prices?.[mint]);
  return p > 0 ? p : null;
}

// Decimals always from the mint account; a hint that disagrees is refused.
async function tokenInfo(connection, mint, hints) {
  const mi = await connection.getAccountInfo(new PublicKey(mint));
  if (!mi || mi.data.length < 45) throw new Refused(`mint ${mint} not readable on chain`);
  const program = mi.owner.toBase58();
  if (program !== TOKEN && program !== TOKEN_2022) throw new Refused(`mint ${mint} is not a token mint; refusing`);
  const decimals = mi.data[44];
  const h = hints.get(mint);
  if (h && h.decimals !== decimals) throw new Refused(`${h.symbol} decimals ${h.decimals} disagree with the chain's ${decimals}; refusing`);
  let usdPrice = h?.usdPrice ?? (STABLE_MINTS.has(mint) ? 1 : null);
  if (usdPrice == null) usdPrice = await geckoUsd(mint).catch(() => null);
  const symbol = h?.symbol ?? (mint === NATIVE_MINT ? 'SOL' : mint === USDC_MINT ? 'USDC' : mint.slice(0, 6));
  return { mint, symbol, decimals, usdPrice, program };
}

function ataOf(info, owner) {
  return spl.getAssociatedTokenAddressSync(new PublicKey(info.mint), owner, false, new PublicKey(info.program)).toBase58();
}

function splAmount(data) {
  const buf = Buffer.isBuffer(data) ? data : Buffer.from(Array.isArray(data) ? data[0] : data, Array.isArray(data) ? 'base64' : undefined);
  return buf.length >= 72 ? buf.readBigUInt64LE(64) : 0n;
}

// Raw balance the swap can spend: the ATA for an SPL token; lamports plus the
// wSOL ATA for SOL. The gas reserve is kept back from native SOL. Capped at
// LPBOT_SLEEVE, as in venues/jupiter/swap.mjs: a wallet several profiles share must
// not let this fallback sell another profile's tokens.
export async function sellable(connection, owner, info) {
  let raw;
  if (info.mint === NATIVE_MINT) {
    const wsol = await connection.getAccountInfo(new PublicKey(ataOf(info, owner)));
    raw = BigInt(await connection.getBalance(owner)) + (wsol ? splAmount(wsol.data) : 0n);
  } else {
    const a = await connection.getAccountInfo(new PublicKey(ataOf(info, owner)));
    raw = a ? splAmount(a.data) : 0n;
  }
  const total = sleeveCap(toHuman(raw, info.decimals), info.mint, parseSleeve(process.env.LPBOT_SLEEVE));
  const avail = info.mint === NATIVE_MINT ? Math.max(0, total - GAS_RESERVE_SOL) : total;
  return { total, avail, usd: avail * info.usdPrice };
}

// Accounts whose balances the simulation reports, and how to read them.
async function snapshot(connection, list) {
  return connection.getMultipleAccountsInfo(list.map(k => new PublicKey(k)));
}
function deltas(pre, post, idx, side) {
  const lam = (x) => (x ? BigInt(x.lamports) : 0n);
  const tok = (x) => (x ? splAmount(x.data) : 0n);
  const sol = (list) => lam(list[idx.owner]) + lam(list[idx.wsol]);
  const val = (list, s) => (s === 'native' ? sol(list) : tok(list[idx[s]]));
  return {
    inDrop: val(pre, side.in) - val(post, side.in),
    outGain: val(post, side.out) - val(pre, side.out),
    solDrop: sol(pre) - sol(post),
  };
}

function compile(payer, blockhash, ixs) {
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message();
  return new VersionedTransaction(msg);
}

async function simulate(connection, tx, addresses) {
  const sim = await connection.simulateTransaction(tx, { sigVerify: false, replaceRecentBlockhash: true,
    accounts: addresses ? { encoding: 'base64', addresses } : undefined });
  if (sim.value.err) throw new Refused(`simulation failed: ${JSON.stringify(sim.value.err).slice(0, 160)} ${(sim.value.logs ?? []).slice(-3).join(' | ').slice(0, 300)}`);
  return sim.value;
}

// --- the swap -------------------------------------------------------------------
async function performSwap(ctx, pool, sellInfo, buyInfo, rawIn, execute, extra) {
  const { connection, rpc, owner, payer } = ctx;
  const { swapInstructions, setNativeMintWrappingStrategy } = await import('@orca-so/whirlpools');
  const { createNoopSigner, address } = await import('@solana/kit');
  const { aToB } = direction(sellInfo.mint, buyInfo.mint, String(pool.tokenMintA), String(pool.tokenMintB));
  const have = await sellable(connection, owner, sellInfo);
  if (toHuman(rawIn, sellInfo.decimals) > have.avail + 1e-12) {
    throw new Refused(`wallet can sell ${have.avail} ${sellInfo.symbol}`
      + (sellInfo.mint === NATIVE_MINT ? ` (after the ${GAS_RESERVE_SOL} SOL gas reserve)` : '')
      + `, asked ${toHuman(rawIn, sellInfo.decimals)}`);
  }
  setNativeMintWrappingStrategy('ata');           // one signer: no temporary keypair account
  const signer = createNoopSigner(address(owner.toBase58()));   // the SDK never sees the key
  const built = await swapInstructions(rpc, { inputAmount: rawIn, mint: address(sellInfo.mint) },
    address(pool.address), { slippageToleranceBps: SLIPPAGE_BPS, signer });
  const quotedAt = Date.now();
  const quote = built.quote;
  if (built.tradeEnableTimestamp && BigInt(built.tradeEnableTimestamp) > BigInt(Math.floor(quotedAt / 1000))) throw new Refused('pool trading is not enabled yet; refusing');
  const values = verifyQuote(quote, sellInfo, buyInfo, rawIn);
  const impact = priceImpact(quote, pool.sqrtPrice, aToB);
  checkImpact(impact);

  const infoA = aToB ? sellInfo : buyInfo, infoB = aToB ? buyInfo : sellInfo;
  const wsolAta = spl.getAssociatedTokenAddressSync(new PublicKey(NATIVE_MINT), owner, false).toBase58();
  const want = {
    payer: owner.toBase58(), pool: pool.address, rawIn, minOut: BigInt(quote.tokenMinOut), aToB,
    ownerA: ataOf(infoA, owner), ownerB: ataOf(infoB, owner),
    vaultA: String(pool.tokenVaultA), vaultB: String(pool.tokenVaultB), wsolAta,
  };
  let body = built.instructions.map(kitToWeb3);
  if (sellInfo.mint === NATIVE_MINT || buyInfo.mint === NATIVE_MINT) body = withWsolClose(body, wsolAta, want.payer);

  // Draft with the maximum unit limit and no price, to measure the units.
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
  const draft = compile(owner, blockhash, [ComputeBudgetProgram.setComputeUnitLimit({ units: CU_DRAFT_LIMIT }), ...body]);
  verifyTxShape(draft, want);
  const drafted = await simulate(connection, draft, null);
  const units = cuLimit(drafted.unitsConsumed ?? 200_000);
  const recent = await connection.getRecentPrioritizationFees({ lockedWritableAccounts: [new PublicKey(pool.address)] }).catch(() => []);
  const price = chooseCuPrice(recent, units);
  const tx = compile(owner, blockhash, [ComputeBudgetProgram.setComputeUnitLimit({ units }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: price }), ...body]);
  verifyTxShape(tx, want);
  verifyPriorityFee(tx);

  // The final transaction, simulated with balances before and after.
  const list = [...new Set([want.payer, wsolAta, want.ownerA, want.ownerB])];
  const idx = { owner: list.indexOf(want.payer), wsol: list.indexOf(wsolAta), A: list.indexOf(want.ownerA), B: list.indexOf(want.ownerB) };
  const side = {
    in: sellInfo.mint === NATIVE_MINT ? 'native' : (aToB ? 'A' : 'B'),
    out: buyInfo.mint === NATIVE_MINT ? 'native' : (aToB ? 'B' : 'A'),
  };
  const pre = await snapshot(connection, list);
  const sim = await simulate(connection, tx, list);
  const d = deltas(pre, sim.accounts, idx, side);
  checkDeltas({ inNative: side.in === 'native', outNative: side.out === 'native', bothSpl: side.in !== 'native' && side.out !== 'native' }, d, rawIn, quote.tokenMinOut);

  const amountIn = toHuman(rawIn, sellInfo.decimals);
  const estOut = toHuman(quote.tokenEstOut, buyInfo.decimals);
  const report = {
    verified: { inDrop: d.inDrop.toString(), outGain: d.outGain.toString(), solDrop: d.solDrop.toString(), unitsConsumed: sim.unitsConsumed ?? null },
    ...extra,
    pool: pool.address, feeRate: pool.feeRate / 1e6,
    sold: { mint: sellInfo.mint, symbol: sellInfo.symbol, amount: amountIn, usd: Number(values.inUsd.toFixed(4)) },
    bought: { mint: buyInfo.mint, symbol: buyInfo.symbol, amount: estOut, usd: Number(values.estUsd.toFixed(4)) },
    quoteOutAmount: estOut, minOutAmount: toHuman(quote.tokenMinOut, buyInfo.decimals),
    priceImpactPct: Number(impact.toFixed(8)), priceImpactPercent: Number((impact * 100).toFixed(6)),
    slippageBps: SLIPPAGE_BPS, routePlan: ['Orca Whirlpool'], swapUsdValue: Number(values.inUsd.toFixed(4)),
    transaction: {
      version: tx.version, bytes: tx.serialize().length,
      signaturesRequired: tx.message.header.numRequiredSignatures,
      instructions: tx.message.compiledInstructions.length, lookupTables: 0,
      lastValidBlockHeight, computeUnitLimit: units, computeUnitPriceMicroLamports: price,
      prioritizationFeeLamports: Number(priorityFeeLamports(tx)), simulationError: null,
    },
    signature: null, sent: false,
  };
  if (!execute) {
    console.log(JSON.stringify(report, null, 1));
    console.log('DRY RUN — Orca quote taken, swap transaction built and simulated. Pass --execute to sign and send.');
    return report;
  }
  guard();
  if (!payer) throw new Refused('no signing key; refusing');
  const age = Date.now() - quotedAt;
  if (age > QUOTE_MAX_AGE_MS) throw new Refused(`quote is ${(age / 1000).toFixed(1)}s old at send time (limit ${QUOTE_MAX_AGE_MS / 1000}s); refusing`);
  tx.sign([payer]);
  return sendLanded(connection, tx, lastValidBlockHeight, report);
}

// From the signature on, the transaction may be on chain. The same signed
// bytes are re-sent until they confirm or the blockhash expires
// (tx_send.sendUntilLanded); never a new transaction. 2026-10-09: two Orca
// swaps sent once expired unconfirmed, and one held DJT out of its band.
// The first send failing: AfterSignError, never retried. Expired and unknown
// to the chain: a plain error without a signature, nothing was swapped.
// Anything else after the send: a partial report with the signature, then
// SentError.
export async function sendLanded(connection, tx, lastValidBlockHeight, report,
                                 { log = s => console.log(s), sleep } = {}) {
  const raw = tx.serialize();
  let signature;
  try {
    signature = await sendUntilLanded(connection, raw, lastValidBlockHeight, sleep ? { sleep } : {});
  } catch (e) {
    if (e instanceof NeverLanded) throw new Error(`swap ${e.message}`);
    if (!e.afterSend) throw new AfterSignError(`send failed after signing (not retried): ${e?.message ?? e}`);
    log(JSON.stringify({ ...report, signature: e.signature, sent: true, partial: true, error: String(e.message ?? e) }, null, 1));
    throw new SentError(`sent ${e.signature} but could not confirm it: ${e.message}`);
  }
  const out = { ...report, signature, sent: true };
  log(JSON.stringify(out, null, 1));
  return out;
}

async function rebalanceCmd(mintA, mintB, targetA, targetB, execute, poolArg) {
  guard();
  for (const [m, w] of [[mintA, 'mintA'], [mintB, 'mintB']]) {
    try { new PublicKey(m); } catch { throw new Refused(`${w} is not a valid mint address: ${m}`); }
  }
  if (mintA === mintB) throw new Refused('mintA and mintB are the same token');
  const tA = Number(targetA), tB = Number(targetB);
  if (!(tA >= 0 && tB >= 0)) throw new Refused(`targets must be non-negative dollars, got ${targetA} ${targetB}`);
  const poolAddr = poolArg ?? defaultPool(mintA, mintB);
  if (!poolAddr) throw new Refused(`no default Orca pool for ${mintA}/${mintB}; pass --pool <whirlpool>`);
  try { new PublicKey(poolAddr); } catch { throw new Refused(`pool is not a valid address: ${poolAddr}`); }
  const hints = parseHints(process.env.LPBOT_TOKEN_HINTS);
  const { owner, payer } = await loadOwner(execute);
  const { createSolanaRpc, address } = await import('@solana/kit');
  const { fetchWhirlpool } = await import('@orca-so/whirlpools-client');
  return overEndpoints(ENDPOINTS, async (url) => {
    guard();
    const connection = new Connection(url, 'confirmed');
    const rpc = createSolanaRpc(url);
    const w = await fetchWhirlpool(rpc, address(poolAddr));
    const pool = { ...w.data, address: poolAddr, programAddress: String(w.programAddress) };
    checkPool(pool, mintA, mintB);
    const infoA = await tokenInfo(connection, mintA, hints);
    const infoB = await tokenInfo(connection, mintB, hints);
    for (const i of [infoA, infoB]) if (i.usdPrice == null) throw new Refused(`no USD price for ${i.symbol} (${i.mint}); cannot size a rebalance`);
    const balA = await sellable(connection, owner, infoA);
    const balB = await sellable(connection, owner, infoB);
    const balances = {
      owner: owner.toBase58(), gasReserveSol: GAS_RESERVE_SOL,
      A: { mint: infoA.mint, symbol: infoA.symbol, amount: balA.total, sellable: balA.avail, usd: Number(balA.usd.toFixed(4)), usdPrice: infoA.usdPrice },
      B: { mint: infoB.mint, symbol: infoB.symbol, amount: balB.total, sellable: balB.avail, usd: Number(balB.usd.toFixed(4)), usdPrice: infoB.usdPrice },
      targetUsdA: tA, targetUsdB: tB,
    };
    const plan = planRebalance(balA.usd, balB.usd, tA, tB);
    if (!plan) {
      const out = noopReport(`both sides within ${TARGET_TOLERANCE * 100}% of target or nothing to sell`, balances);
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    const s = planSwap(plan, infoA, infoB, balA.avail, balB.avail);
    if (!(s.rawIn > 0n)) {
      const out = noopReport('the amount to sell rounds to zero', balances);
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    return performSwap({ connection, rpc, owner, payer }, pool, s.sellInfo, s.buyInfo, s.rawIn, execute, {
      mode: plan.mode, sellSide: plan.sellSide, buySide: plan.buySide,
      sellUsdPlanned: Number(plan.sellUsd.toFixed(4)),
      desiredUsdA: Number(plan.desiredA.toFixed(4)), desiredUsdB: Number(plan.desiredB.toFixed(4)),
      before: balances,
    });
  });
}

export async function main(argv = process.argv.slice(2)) {
  const { cmd, args, execute, pool } = parseArgs(argv);
  if (cmd === 'rebalance') return rebalanceCmd(args[0], args[1], args[2], args[3], execute, pool);
  console.log(USAGE);
  return null;
}

if (isEntry(import.meta.url)) {
  main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
}
