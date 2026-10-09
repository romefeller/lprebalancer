// Rebalancer — the signing layer. All chain I/O lives here.
//
// Built on @orca-so/whirlpools v8. The legacy @orca-so/whirlpools-sdk@0.22.0
// encodes swaps correctly against the deployed program but not the liquidity
// instructions: every open attempt was rejected as custom error 6069 after
// ~1390 compute units, on an adaptive-fee pool (ZEC/USDC) and on a plain one
// (SOL/USDC) alike. 0.22.0 is the last release of that line, so the fix was the
// current package, not a version bump.
//
// Nothing here is written for one pair. Token symbols, decimals and the pool
// price are read from the pool itself, so `open` and `status` behave the same
// on SOL/USDC, on WIF/USDC and on a pool whose quote token is not a dollar.
//
// Token-2022 base tokens (DJT): every human amount and dollar figure here is
// in UI units (token2022.mjs); `price`, `lowerPrice` and `upperPrice` stay
// pool-native (from the pool's own sqrt price), `uiPrice` is the price in UI
// units. A paused mint or one with a transfer hook refuses open, harvest and
// close.
//
// The key is read from WALLET_SECRET_PATH inside this process, handed straight
// to setPayerFromBytes, and never printed or returned. Only the public address
// is ever shown.
//
// Commands:
//   node signer2.mjs balance [pool]
//   node signer2.mjs positions
//   node signer2.mjs status [mint]
//   node signer2.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node signer2.mjs harvest <mint> [--execute]
//   node signer2.mjs close <mint> [--execute]
import fs from 'node:fs';
import path from 'node:path';
import { assertNotHalted } from './halt_guard.mjs';
import { BOT_ROOT } from './bot_root.mjs';
import {
  setRpc, setPayerFromBytes, setNativeMintWrappingStrategy,
  openConcentratedPosition, fetchPositionsForOwner,
  closePosition, harvestPosition, closePositionInstructions, harvestPositionInstructions,
} from '@orca-so/whirlpools';
import {
  createSolanaRpc, address, pipe, createTransactionMessage, setTransactionMessageFeePayerSigner,
  setTransactionMessageLifetimeUsingBlockhash, appendTransactionMessageInstructions,
  signTransactionMessageWithSigners, getBase64EncodedWireTransaction,
} from '@solana/kit';
import { fetchWhirlpool, fetchPosition, getPositionAddress } from '@orca-so/whirlpools-client';
import { sqrtPriceToPrice } from '@orca-so/whirlpools-core';
import { consistentOrcaFees } from './orca_fees.mjs';
import { endpoints, overEndpoints, isEntry, AfterSignError } from './rpc_policy.mjs';
import { readMints, rawToUi, uiToRaw, uiToNative, uiPrice, assertWritable, mintFields } from './token2022.mjs';

const RPC = process.env.SOLANA_RPC_URL
  ?? (process.env.KAMINO_RPC_KEY
    ? `https://mainnet.helius-rpc.com/?api-key=${process.env.KAMINO_RPC_KEY}`
    : 'https://api.mainnet-beta.solana.com');

// Both are parameters of the deployment, not of the code. The bot passes the
// values from its active profile; the defaults only matter to a bare CLI run.
const MAX_USD = Number(process.env.LPBOT_MAX_USD ?? 260);
const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
// Native SOL kept back for transaction fees. Not a property of the pool: a
// WIF/USDC position still needs SOL to close itself.
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.02);

const NATIVE_MINT = 'So11111111111111111111111111111111111111112';
const HEADERS = { accept: 'application/json', 'user-agent': 'Mozilla/5.0' };

function guard() {
  assertNotHalted(BOT_ROOT);                // the global HALT and this profile's (halt_guard.mjs)
}

// Returns the 64-byte secret key. Never logged, never returned to a caller
// other than setPayerFromBytes.
async function secretBytes() {
  const p = process.env.WALLET_SECRET_PATH;
  if (!p) throw new Error('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  const raw = fs.readFileSync(p, 'utf8').trim();
  if (raw.startsWith('[')) return Uint8Array.from(JSON.parse(raw));
  const bs58 = (await import('bs58')).default;
  return bs58.decode(raw);
}

// --- the pool describes itself ----------------------------------------------
// Decimals, symbols and price all come from the pool. Hardcoding 9 and 6 is how
// a rebalancer silently misprices every pair that is not SOL/USDC.
const poolCache = new Map();

async function poolInfo(pool) {
  if (poolCache.has(pool)) return poolCache.get(pool);
  const r = await fetch(`https://api.orca.so/v2/solana/pools/${pool}`, { headers: HEADERS });
  // Orca answers an unknown address with plain text, not JSON.
  const j = await r.json().catch(() => null);
  const d = j?.data ?? j;
  if (!d?.tokenA) throw new Error(`could not read pool ${pool}: Orca returned ${r.status}`);
  const info = {
    address: pool,
    price: Number(d.price),
    feeRate: Number(d.feeRate ?? 0) / 1e6,
    tvlUsd: Number(d.tvlUsdc ?? 0),
    adaptiveFee: Boolean(d.adaptiveFeeEnabled),
    symbolA: d.tokenA.symbol, symbolB: d.tokenB.symbol,
    decimalsA: Number(d.tokenA.decimals), decimalsB: Number(d.tokenB.decimals),
    mintA: d.tokenA.address, mintB: d.tokenB.address,
  };
  poolCache.set(pool, info);
  return info;
}

// The pool's two mints, read fresh on every command: a pause or a new
// multiplier must not wait behind a cache.
export async function poolMints(rpc, mints) {
  return readMints(async ms => (await rpc.getMultipleAccounts(ms.map(m => address(m)),
    { encoding: 'jsonParsed' }).send()).value, mints);
}

// The pool as the chain has it now: the price from its own sqrt price, which
// is pool-native by construction (Orca's API figure is not documented to be,
// and on a scaled-UI mint the two differ), and the facts of both mints.
// Throws when the chain's mints or decimals disagree with Orca's API.
export async function chainView(rpc, info) {
  const wp = (await fetchWhirlpool(rpc, address(info.address))).data;
  if (String(wp.tokenMintA) !== info.mintA || String(wp.tokenMintB) !== info.mintB) {
    throw new Error(`pool ${info.address}: the chain's mints differ from Orca's API`);
  }
  const [fa, fb] = await poolMints(rpc, [info.mintA, info.mintB]);
  if (fa.decimals !== info.decimalsA || fb.decimals !== info.decimalsB) {
    throw new Error(`mint decimals ${fa.decimals}/${fb.decimals} disagree with Orca's ${info.decimalsA}/${info.decimalsB}`);
  }
  const price = sqrtPriceToPrice(BigInt(wp.sqrtPrice), info.decimalsA, info.decimalsB);
  const view = { ...info, price, uiPrice: uiPrice(price, fa.multiplier, fb.multiplier), ...mintFields(fa, fb) };
  // The facts ride along for the write checks but stay out of the JSON.
  Object.defineProperty(view, 'mints', { value: [fa, fb], enumerable: false });
  return view;
}

// The facts of the mints of the pool a position is on, for the write checks
// of harvest and close, which are given only the position.
async function positionMints(rpc, positionMint) {
  const [pda] = await getPositionAddress(address(positionMint));
  const pos = (await fetchPosition(rpc, pda)).data;
  const wp = (await fetchWhirlpool(rpc, pos.whirlpool)).data;
  return poolMints(rpc, [String(wp.tokenMintA), String(wp.tokenMintB)]);
}

// Simulate built instructions without sending them. Signing is local; the
// report carries the program's verdict, so a dry run proves the instructions.
// A wallet without the tokens fails here on the token transfer ("insufficient
// funds"), after every account and instruction has been checked.
async function simulate(rpc, signer, instructions) {
  try {
    const { value: blockhash } = await rpc.getLatestBlockhash().send();
    const msg = pipe(createTransactionMessage({ version: 0 }),
      m => setTransactionMessageFeePayerSigner(signer, m),
      m => setTransactionMessageLifetimeUsingBlockhash(blockhash, m),
      m => appendTransactionMessageInstructions(instructions, m));
    const tx = getBase64EncodedWireTransaction(await signTransactionMessageWithSigners(msg));
    const { value } = await rpc.simulateTransaction(tx,
      { encoding: 'base64', sigVerify: false, replaceRecentBlockhash: true }).send();
    const logs = value.logs ?? [];
    return {
      ok: value.err == null,
      err: value.err == null ? null : JSON.stringify(value.err, (k, x) => (typeof x === 'bigint' ? String(x) : x)),
      logError: logs.find(l => /Program log: (Error|AnchorError)/.test(l)) ?? null,
      unitsConsumed: value.unitsConsumed == null ? null : Number(value.unitsConsumed),
      logTail: logs.slice(-4),
    };
  } catch (e) {
    return { ok: false, err: String(e?.message ?? e).slice(0, 200), unitsConsumed: null, logTail: [] };
  }
}

// Dollar value of one unit of the pool's quote token. On a USDC-quoted pool
// this is 1; on SOL/xSOL it is not, and pretending otherwise turns every
// dollar figure the bot reports into nonsense.
// Stablecoins by MINT (USDC, USDT, PYUSD, USDS). A symbol comes from API or token metadata an
// attacker controls: a fake "USDC" priced at $1 would defeat every dollar cap (review 2026-09-26).
const STABLE_MINTS = new Set(['EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', '2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo', 'USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA']);

// Priced by MINT, never by GeckoTerminal's idea of which token is the quote.
// Gecko orders a pair by its own convention, so its quote_token_price_usd on
// SOL/cbBTC is the price of SOL. Read that as the price of a bitcoin and the
// wallet reads $0.10 and every position is mispriced by a factor of a thousand.
async function tokenUsd(mint) {
  // GeckoTerminal's free tier is ~30 requests a minute; one retry covers the
  // 429 a burst of reads produces, and a second failure is reported as null.
  for (let attempt = 0; attempt < 2; attempt++) {
    const r = await fetch('https://api.geckoterminal.com/api/v2/simple/networks/solana/'
      + `token_price/${mint}`, { headers: { accept: 'application/json;version=20230203' } });
    if (r.status === 429 && attempt === 0) { await new Promise(s => setTimeout(s, 2500)); continue; }
    const p = Number((await r.json().catch(() => null))?.data?.attributes?.token_prices?.[mint]);
    return p > 0 ? p : null;
  }
  return null;
}

async function quoteUsd(info) {
  if (STABLE_MINTS.has(String(info.mintB))) return { usd: 1, source: 'stable' };
  try {
    const p = await tokenUsd(info.mintB);
    if (p) return { usd: p, source: 'geckoterminal:mint' };
  } catch { /* fall through to the honest answer below */ }
  return { usd: null, source: 'unknown' };
}

// Dollar price of native SOL, for valuing the gas balance when SOL is not one
// of the pool's tokens. When it is, the pool's own price is the answer.
async function solUsd(info, qUsd) {
  if (info.mintA === NATIVE_MINT && qUsd != null) return info.uiPrice * qUsd;
  if (info.mintB === NATIVE_MINT && qUsd != null) return qUsd;
  try { return await tokenUsd(NATIVE_MINT); } catch { return null; }
}

// UI-unit balance of one SPL mint (raw / 10^decimals × the mint's multiplier).
// The native mint is the lamport balance: the wrapping strategy wraps it into
// an ATA on demand at open time.
async function tokenBalance(rpc, owner, mint, decimals, multiplier, lamports) {
  if (mint === NATIVE_MINT) return lamports / 1e9;
  const r = await rpc.getTokenAccountsByOwner(owner, { mint: address(mint) },
    { encoding: 'jsonParsed' }).send();
  let raw = 0n;
  for (const a of r.value ?? []) {
    raw += BigInt(a.account?.data?.parsed?.info?.tokenAmount?.amount ?? 0);
  }
  return rawToUi(raw, decimals, multiplier);
}

// Deposit for a band [pa, pb] at price p with per-token caps: the liquidity
// each cap alone would fund, the smaller of the two, and the amounts that
// liquidity takes. Standard concentrated-liquidity arithmetic, in human units.
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

// Try each endpoint in turn. A single rate-limited RPC made the bot read
// "no position" and try to open a second one; the read must be hard to fail,
// and when it does fail it must fail loudly rather than return an empty answer.
// RPC first (a keyed endpoint when there is one). Indexed reads
// (fetchPositionsForOwner) never go to an endpoint that refuses them.
export const ENDPOINTS = endpoints({ SOLANA_RPC_URL: RPC }, { indexed: true });

async function connectTo(url) {
  guard();
  await setRpc(url);
  setNativeMintWrappingStrategy('ata');   // wrap/unwrap SOL through an ATA
  const signer = await setPayerFromBytes(await secretBytes());
  return { signer, rpc: createSolanaRpc(url), endpoint: url };
}

// Retry the WHOLE operation across endpoints, not just the handshake.
// Validating the connection alone is not enough: the rate limit lands on the
// account fetch that comes afterwards, which is what made the first armed run
// report an unreadable position on every poll.
// A rate limit, a refusal or a transport failure moves on (rpc_policy.mjs);
// an answer from the chain or an error after a send is thrown at once.
// `deps` replaces the endpoints, connect and sleep in tests.
export async function withRpc(fn, deps = {}) {
  const { urls = ENDPOINTS, connectFn = connectTo, sleep } = deps;
  return overEndpoints(urls, async url => fn(await connectFn(url)), { tries: 2, pauseMs: 2500, sleep });
}

// The SDK callback signs, sends and confirms. A failure in it may follow a
// send that reached a node: it is never rotated or retried (AfterSignError).
export async function sendOnce(result) {
  try {
    return await result.callback();
  } catch (e) {
    throw new AfterSignError(`send failed after signing (not retried): ${e?.message ?? e}`);
  }
}

async function connect() {
  return withRpc(async (c) => c);
}

// What the wallet holds. With a pool: both of its tokens, what each is worth,
// and the whole wallet in dollars. Without one: native SOL only.
//
// The pool-aware form is what the bot sizes an open from and what it adds to
// the position's mark for equity. Counting SOL alone made every idle USDC
// balance invisible, so equity would have dropped by the withdrawn amount on
// every close and reported a loss that never happened.
async function balance(pool) {
  const info = pool ? await poolInfo(pool) : null;
  return withRpc(async ({ signer, rpc }) => {
    const lamports = Number((await rpc.getBalance(signer.address).send()).value);
    const out = { owner: signer.address, sol: lamports / 1e9 };
    if (info) {
      const v = await chainView(rpc, info);
      const { usd: qUsd } = await quoteUsd(info);
      const sUsd = await solUsd(v, qUsd);
      out.pool = pool;
      out.tokenA = info.symbolA; out.tokenB = info.symbolB;
      out.price = v.price; out.uiPrice = v.uiPrice; out.quoteUsd = qUsd;
      Object.assign(out, mintFields(...v.mints));
      out.balanceA = await tokenBalance(rpc, signer.address, info.mintA, info.decimalsA, v.multiplierA, lamports);
      out.balanceB = await tokenBalance(rpc, signer.address, info.mintB, info.decimalsB, v.multiplierB, lamports);
      out.nativeSide = info.mintA === NATIVE_MINT ? 'A' : info.mintB === NATIVE_MINT ? 'B' : null;
      // The pool's two tokens in quote units, then dollars; plus the gas SOL
      // when it is not already one of them. UI amounts at the UI price.
      const inQuote = out.balanceA * v.uiPrice + out.balanceB;
      out.walletUsd = qUsd == null ? null : Number((inQuote * qUsd
        + (out.nativeSide ? 0 : out.sol * (sUsd ?? 0))).toFixed(4));
    }
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

async function positions() {
  const { signer, rpc } = await connect();
  const list = await fetchPositionsForOwner(rpc, signer.address);
  console.log(JSON.stringify(list.map(p => ({
    address: p.address,
    whirlpool: p.data?.whirlpool,
    liquidity: p.data?.liquidity?.toString(),
    tickLower: p.data?.tickLowerIndex,
    tickUpper: p.data?.tickUpperIndex,
  })), null, 1));
}

// maxA and maxB are UI amounts (what the wallet shows); the deposit is sized
// in pool-native units at the pool price and reported back in UI units.
async function open(pool, lower, upper, maxA, maxB, execute) {
  const info = await poolInfo(pool);
  // Opens on adaptive-fee pools were rejected with custom error 6069
  // (PriceSlippageOutOfBounds). On 2026-10-01 the instructions of
  // @orca-so/whirlpools 8.0.1 simulated green on two adaptive pools (an
  // in-range SOL/USDC open on Esvfxt3j…, a USDC-only open on DJT/USDC
  // 7gkB2D1S…), but none has run live. So the refusal stays the default and a
  // profile that needs such a pool opts in with LPBOT_ORCA_ADAPTIVE=1.
  if (info.adaptiveFee && process.env.LPBOT_ORCA_ADAPTIVE !== '1') {
    throw new Error(`${info.symbolA}/${info.symbolB} is an adaptive-fee pool; this `
      + 'signer does not open on it unless LPBOT_ORCA_ADAPTIVE=1 (Whirlpool error 6069 seen). Choose another pool.');
  }
  return withRpc(async ({ signer, rpc }) => {
    const v = await chainView(rpc, info);
    assertWritable(v.mints);
    const lamports = await rpc.getBalance(signer.address).send();
    if (Number(lamports.value) / 1e9 < GAS_RESERVE_SOL) {
      throw new Error(`SOL below the ${GAS_RESERVE_SOL} gas reserve; a wallet that cannot `
        + 'pay fees cannot close its own position');
    }
    // Size the cap at the pool's own price rather than a constant. The old
    // constant was the SOL price on the day it was written.
    const { usd: qUsd } = await quoteUsd(info);
    const approxUsd = (Number(maxA) * v.uiPrice + Number(maxB)) * (qUsd ?? 1);
    if (approxUsd > MAX_USD) {
      throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
    }
    const param = {
      tokenMaxA: uiToRaw(maxA, info.decimalsA, v.multiplierA),
      tokenMaxB: uiToRaw(maxB, info.decimalsB, v.multiplierB),
    };
    // Nothing in 8.0.1 returns the deposit quote, so it is computed here from
    // the CLMM amount formulas: the liquidity both caps allow, and what that
    // liquidity takes of each token at the current price. The caps are
    // ceilings; the ledger must record what actually goes in, which on a
    // wallet short of one side is less than the capital. After a live open
    // the bot replaces this estimate with the chain's own mark.
    const quote = depositQuote(v.price, Number(lower), Number(upper),
      uiToNative(Number(maxA), v.multiplierA), uiToNative(Number(maxB), v.multiplierB));
    const estA = quote ? quote.estA * v.multiplierA : null;
    const estB = quote ? quote.estB * v.multiplierB : null;

    const result = await openConcentratedPosition(
      address(pool), param, Number(lower), Number(upper),
      { slippageToleranceBps: SLIPPAGE_BPS, funder: signer });
    const report = {
      pool, pair: `${info.symbolA}/${info.symbolB}`,
      lowerPrice: Number(lower), upperPrice: Number(upper),
      tokenMaxA: Number(maxA), tokenMaxB: Number(maxB),
      tokenA: info.symbolA, tokenB: info.symbolB,
      approxUsd: Number(approxUsd.toFixed(2)),
      depositEstA: estA, depositEstB: estB,
      depositUsd: (estA != null && qUsd != null)
        ? Number(((estA * v.uiPrice + estB) * qUsd).toFixed(4)) : null,
      price: v.price, uiPrice: v.uiPrice, multiplierA: v.multiplierA, multiplierB: v.multiplierB,
      positionMint: result.positionMint ?? null,
      quote,
      initializationCost: result.initializationCost?.toString() ?? null,
      instructions: result.instructions?.length ?? 0,
    };
    if (!execute) {
      const simulation = await simulate(rpc, signer, result.instructions);
      console.log(JSON.stringify({ ...report, simulation, sent: false }, null, 1));
      console.log('DRY RUN — instructions built. Pass --execute to sign and send.');
      return;
    }
    const sig = await sendOnce(result);
    console.log(JSON.stringify({ ...report, sent: true, signature: sig }, null, 1));
  });
}

// On-chain truth for one position: its band, its liquidity, and the fees it has
// accrued but not yet collected. The bot reports these rather than simulated
// numbers, so what reaches Telegram is what the chain says.
async function status(mintArg) {
  return withRpc(async ({ signer, rpc }) => {
    const list = await fetchPositionsForOwner(rpc, signer.address);
    const hydrated = list.filter(p => !p.isPositionBundle && p.data);
    // Without a position named, only the loop's own pool (LPBOT_POOL) counts:
    // one wallet holds positions for several profiles (DJT/USDC beside
    // SOL/USDC), and the first position of another pool is not this one's.
    const pool = process.env.LPBOT_POOL;
    const chosen = mintArg
      ? hydrated.find(p => p.data.positionMint === mintArg)
      : hydrated.find(p => !pool || String(p.data.whirlpool) === pool);
    if (!chosen) {
      // Explicit and parseable: the read worked and there is genuinely nothing.
      // The caller must be able to tell this apart from a failed read.
      console.log(JSON.stringify({ positions: 0, positionMint: null, ...(pool ? { pool } : {}) }, null, 1));
      return null;
    }
    const d = chosen.data;
    const info = await chainView(rpc, await poolInfo(d.whirlpool));
    const price = info.price;
    const q = await quoteUsd(info);
    // Ticks are in raw-amount space; the decimal difference converts them to
    // the human price the pool quotes.
    const scale = 10 ** (info.decimalsA - info.decimalsB);
    const lower = 1.0001 ** d.tickLowerIndex * scale;
    const upper = 1.0001 ** d.tickUpperIndex * scale;
    // Amounts in UI units, valued at the UI price; band and price pool-native.
    const ua = (x) => rawToUi(x, info.decimalsA, info.multiplierA);
    const ub = (x) => rawToUi(x, info.decimalsB, info.multiplierB);
    const up = info.uiPrice;
    const out = {
      positionMint: d.positionMint,
      whirlpool: d.whirlpool, pool: d.whirlpool,
      pair: `${info.symbolA}/${info.symbolB}`,
      tokenA: info.symbolA, tokenB: info.symbolB,
      decimalsA: info.decimalsA, decimalsB: info.decimalsB,
      quoteUsd: q.usd, quoteUsdSource: q.source,
      liquidity: d.liquidity.toString(),
      tickLower: d.tickLowerIndex, tickUpper: d.tickUpperIndex,
      lowerPrice: Number(lower.toFixed(6)), upperPrice: Number(upper.toFixed(6)),
      price: Number(price.toFixed(6)), uiPrice: up,
      ...mintFields(...info.mints),
      inRange: price >= lower && price <= upper,
      feeOwedA: ua(d.feeOwedA), feeOwedB: ub(d.feeOwedB),
    };
    // Rent in the position account (and the NFT's token account), refunded on
    // close. Small on Orca, 0.2 SOL on a wide DLMM position; counted the same
    // way everywhere so equity does not fall by the rent on every open.
    try {
      const acct = await rpc.getAccountInfo(chosen.address, { encoding: 'base64' }).send();
      const rentLamports = Number(acct?.value?.lamports ?? 0);
      out.rentSol = rentLamports / 1e9;
      const sUsd = await solUsd(info, q.usd);
      out.rentUsd = sUsd != null ? Number((out.rentSol * sUsd).toFixed(4)) : null;
    } catch { /* reporting only */ }
    // The position's own feeOwed fields are only settled when the position is
    // touched, so they read zero on a live position that is in fact earning.
    // The close quote recomputes them from current fee growth, which is the
    // real accrued figure. Reported separately so the stale field stays visible.
    try {
      const cq = await closePositionInstructions(rpc, address(d.positionMint),
        { slippageToleranceBps: SLIPPAGE_BPS, authority: signer });
      if (cq?.quote) {
        // What a close would return right now: the position's mark.
        out.closeEstA = ua(cq.quote.tokenEstA);
        out.closeEstB = ub(cq.quote.tokenEstB);
        if (q.usd != null) {
          out.positionUsd = Number(
            ((out.closeEstA * up + out.closeEstB) * q.usd).toFixed(4));
        }
      }
    } catch { /* reporting only: never fail a status read over the close quote */ }
    try {
      // Fees from ONE read of position, whirlpool, tick arrays and mints
      // (orca_fees.mjs), not from the quote above: the SDK builds it from
      // separate reads, and a tick crossed between them mixes two states
      // (on Raydium: $6,237 of fees on a $230 position, 2026-09-27). A read
      // that fails the invariants reports the settled feeOwed: stale, never
      // more than was earned.
      const snap = await consistentOrcaFees(rpc, address(d.positionMint), address(d.whirlpool));
      out.feesSource = snap.ok ? 'feeGrowth' : `feeOwed (stale: ${snap.reason})`;
      out.feesAccruedA = ua(snap.feeA);
      out.feesAccruedB = ub(snap.feeB);
      // Value the fees in quote units first, then in dollars. Collapsing
      // straight to dollars assumes token B is a dollar, which is true of
      // USDC pools and of nothing else.
      const inQuote = out.feesAccruedA * up + out.feesAccruedB;
      out.feesAccrued_quote = Number(inQuote.toFixed(9));
      if (q.usd != null) out.feesAccrued_USD = Number((inQuote * q.usd).toFixed(6));
    } catch { /* reporting only: never fail a status read over fee accounting */ }
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

async function harvest(mint, execute) {
  // Writes retry across endpoints too. A 429 here lands while the SDK is
  // FETCHING accounts to build the instruction, before anything is signed or
  // sent, so rotating endpoints is safe. The send itself is never retried
  // (sendOnce). The caller still re-reads chain state after any failure
  // rather than trusting the error alone.
  return withRpc(async ({ signer, rpc }) => {
    assertWritable(await positionMints(rpc, mint));
    if (!execute) {
      // Build and simulate: a dry run that returns early proves nothing.
      const ix = await harvestPositionInstructions(rpc, address(mint), { authority: signer });
      const simulation = await simulate(rpc, signer, ix.instructions);
      console.log(JSON.stringify({ mint, instructions: ix.instructions.length,
        feesQuote: { feeOwedA: ix.feesQuote?.feeOwedA?.toString(), feeOwedB: ix.feesQuote?.feeOwedB?.toString() },
        simulation, sent: false }, null, 1));
      console.log('DRY RUN — pass --execute to collect fees.');
      return;
    }
    // authority, not funder: harvest and close act on a position you own, and
    // both return an ActionResult that still needs its callback invoked.
    const result = await harvestPosition(address(mint), { authority: signer });
    const sig = await sendOnce(result);
    console.log(JSON.stringify({ harvested: mint, signature: sig }, null, 1));
  });
}

async function close(mint, execute) {
  return withRpc(async ({ signer, rpc }) => {
    assertWritable(await positionMints(rpc, mint));
    if (!execute) {
      // Actually build the instructions. A dry run that returns early proves
      // nothing, and the close path is the one the bot depends on at 3am.
      const ix = await closePositionInstructions(rpc, address(mint),
        { slippageToleranceBps: SLIPPAGE_BPS, authority: signer });
      console.log(JSON.stringify({
        mint, instructions: ix.instructions?.length ?? 0,
        quote: ix.quote ? {
          liquidity: ix.quote.liquidityDelta?.toString(),
          tokenEstA: ix.quote.tokenEstA?.toString(),
          tokenEstB: ix.quote.tokenEstB?.toString(),
        } : null,
        feesQuote: ix.feesQuote ? {
          feeOwedA: ix.feesQuote.feeOwedA?.toString(),
          feeOwedB: ix.feesQuote.feeOwedB?.toString(),
        } : null,
        simulation: await simulate(rpc, signer, ix.instructions),
        sent: false }, null, 1));
      console.log('DRY RUN — close instructions built. Pass --execute to send.');
      return;
    }
    const result = await closePosition(address(mint),
      { slippageToleranceBps: SLIPPAGE_BPS, authority: signer });
    const sig = await sendOnce(result);
    console.log(JSON.stringify({ closed: mint, signature: sig }, null, 1));
  });
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const execute = rest.includes('--execute');
  const a = rest.filter(x => x !== '--execute');
  if (cmd === 'balance') return balance(a[0]);
  if (cmd === 'positions') return positions();
  if (cmd === 'status') return status(a[0]);
  if (cmd === 'harvest') return harvest(a[0], execute);
  if (cmd === 'close') return close(a[0], execute);
  if (cmd === 'open') return open(a[0], a[1], a[2], a[3], a[4], execute);
  if (cmd === 'pool') {
    // Read-only: no key is loaded. The API's record plus the chain's price and mint facts.
    const info = await poolInfo(a[0]);
    const v = await overEndpoints(ENDPOINTS, async url => chainView(createSolanaRpc(url), info), { tries: 2, pauseMs: 2500 });
    return console.log(JSON.stringify(v, null, 1));
  }
  console.log('commands: balance [pool] | positions | status [mint] | pool <pool> | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <mint> [--execute] '
    + '| close <mint> [--execute]');
}

// The CLI runs only when node starts this file; a test import runs nothing.
if (isEntry(import.meta.url)) main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
