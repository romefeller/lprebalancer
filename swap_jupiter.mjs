// Rebalancer — the Jupiter swap layer. Jupiter has no LP product; its one job
// here is to move the wallet from one token pair to another before the loop
// opens on a new pool (config `allow_swap`). Same conventions as the signers
// (SIGNER_CONTRACT.md): HALT guard, key read from WALLET_SECRET_PATH and never
// printed, one JSON object on stdout, `ERROR: <message>` on stderr with exit 1,
// dry run by default.
//
// API: Jupiter Metis Swap V1 on lite-api.jup.ag (no key). The docs mark V1 as
// superseded by Swap V2 at api.jup.ag/swap/v2, which needs an API key and is
// not served by lite-api; V1 still answers today (2026-09-24).
//   GET  /swap/v1/quote?inputMint&outputMint&amount=<raw>&slippageBps&restrictIntermediateTokens=true
//        -> { inAmount, outAmount, otherAmountThreshold, priceImpactPct (string, RATIO: "0.001" = 0.1%),
//             routePlan[{swapInfo{label,...},percent}], contextSlot, swapUsdValue }
//   POST /swap/v1/swap  { quoteResponse, userPublicKey, wrapAndUnwrapSol, dynamicComputeUnitLimit,
//                         prioritizationFeeLamports:'auto' }
//        -> { swapTransaction (base64 v0 tx), lastValidBlockHeight, prioritizationFeeLamports,
//             computeUnitLimit, simulationError|null }
//   GET  /price/v3?ids=<mint>,<mint>   -> { <mint>: { usdPrice, decimals } }
//   GET  /tokens/v2/search?query=<mint> -> [{ id, symbol, decimals, usdPrice }]
//
// Commands:
//   node swap_jupiter.mjs quote <inMint> <outMint> <amountIn>              (amountIn in human units of inMint)
//   node swap_jupiter.mjs swap  <inMint> <outMint> <amountIn> [--execute]
//   node swap_jupiter.mjs rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute]
//
// `rebalance` is what the loop calls: it reads the wallet's balances of the two
// mints (native SOL counts as So111...112, less LPBOT_GAS_RESERVE_SOL), prices
// both, and makes AT MOST ONE swap from the side above its target into the side
// below it. With enough value both sides end at or above target; with too
// little, the wallet is split in the targets' proportion. Within 2% -> noop.
//
// Refusals before any send: HALT present; priceImpactPct above LPBOT_MAX_IMPACT
// (ratio, default 0.01 = 1%); Jupiter's own simulation of the built transaction
// failed; the quote is older than QUOTE_MAX_AGE_MS at send time; the wallet
// holds less than the amount to sell.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { Connection, Keypair, PublicKey, VersionedTransaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const DIR = path.dirname(new URL(import.meta.url).pathname);
const HALT = path.join(DIR, 'HALT');
const RPC = process.env.SOLANA_RPC_URL ?? process.env.LPBOT_RPC ?? 'https://api.mainnet-beta.solana.com';
const ENDPOINTS = [RPC, 'https://api.mainnet-beta.solana.com', 'https://solana-rpc.publicnode.com']
  .filter((v, i, a) => v && a.indexOf(v) === i);

const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.05);
const MAX_IMPACT = Number(process.env.LPBOT_MAX_IMPACT ?? 0.01);   // ratio: 0.01 = 1%
const QUOTE_MAX_AGE_MS = 20_000;
const TARGET_TOLERANCE = 0.02;                                       // "at target" = within 2%

const NATIVE_MINT = 'So11111111111111111111111111111111111111112';
const JUPITER = 'https://lite-api.jup.ag';
const HEADERS = { accept: 'application/json', 'user-agent': 'Mozilla/5.0' };

function guard() {
  if (fs.existsSync(HALT)) throw new Error(`HALT present: ${fs.readFileSync(HALT, 'utf8').trim()}`);
}

async function secretBytes() {
  const p = process.env.WALLET_SECRET_PATH;
  if (!p) throw new Error('WALLET_SECRET_PATH is not set; refusing to guess a key location');
  const raw = fs.readFileSync(p, 'utf8').trim();
  if (raw.startsWith('[')) return Uint8Array.from(JSON.parse(raw));
  const bs58 = (await import('bs58')).default;
  return bs58.decode(raw);
}

async function connect(url) {
  guard();
  const connection = new Connection(url, 'confirmed');
  const payer = Keypair.fromSecretKey(await secretBytes());
  return { connection, payer };
}

// Retry READS across endpoints on a rate limit. A `SentError` means a
// transaction left this process: never retried, whatever the cause.
class SentError extends Error {}

async function withRpc(fn) {
  let lastErr = null;
  for (const url of ENDPOINTS) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        return await fn(await connect(url));
      } catch (e) {
        if (e instanceof SentError) throw e;
        // Only a rate limit moves to the next endpoint; any other error is
        // the caller's answer (a refusal, a bad quote) and is passed through.
        if (!/429|Too Many Requests|rate/i.test(String(e?.message ?? e))) throw e;
        lastErr = e;
        await new Promise(r => setTimeout(r, 2500 * (attempt + 1)));
      }
    }
  }
  throw new Error(`all RPC endpoints failed: ${String(lastErr?.message ?? lastErr).slice(0, 160)}`);
}

// --- Jupiter HTTP -----------------------------------------------------------------
// Jupiter's free API rate-limits by IP, and the scanner shares it. A 429 on a
// read or on building a transaction is retried with backoff: neither sends
// anything. Sending is never retried (see SentError).
const JUP_RETRY_MS = [1500, 4000, 9000];
async function jfetch(url, init) {
  for (let attempt = 0; ; attempt++) {
    const r = await fetch(url, init);
    const text = await r.text();
    let j; try { j = JSON.parse(text); } catch { j = null; }
    if (r.status === 429 && attempt < JUP_RETRY_MS.length) {
      await new Promise(res => setTimeout(res, JUP_RETRY_MS[attempt]));
      continue;
    }
    if (!r.ok) throw new Error(`Jupiter ${r.status} on ${new URL(url).pathname}: ${(j?.error ?? text).toString().slice(0, 200)}`);
    return j;
  }
}

async function jget(url) {
  return jfetch(url, { headers: HEADERS });
}

async function jpost(url, body) {
  return jfetch(url, { method: 'POST', headers: { ...HEADERS, 'content-type': 'application/json' }, body: JSON.stringify(body) });
}

function assertMint(m, what) {
  try { new PublicKey(m); } catch { throw new Error(`${what} is not a valid mint address: ${m}`); }
  return m;
}

// One call for prices and decimals, one per mint for the symbol.
const infoCache = new Map();

// LPBOT_TOKEN_HINTS: {"<mint>": {"usd": price, "decimals": n, "symbol": "X"}} from
// the loop, which already knows the pool's tokens. A hinted mint needs no
// Jupiter call: the free price API rate-limits by IP, and on 2026-09-26 its
// 429s left the capital idle through two reopen attempts.
try {
  for (const [m, h] of Object.entries(JSON.parse(process.env.LPBOT_TOKEN_HINTS || '{}'))) {
    if (h && Number(h.usd) > 0 && Number.isInteger(h.decimals)) {
      infoCache.set(m, { mint: m, symbol: h.symbol || m.slice(0, 6), decimals: h.decimals, usdPrice: Number(h.usd) });
    }
  }
} catch { /* a bad hint is ignored; Jupiter is asked instead */ }

async function tokenInfos(mints) {
  const need = mints.filter(m => !infoCache.has(m));
  if (need.length) {
    const prices = await jget(`${JUPITER}/price/v3?ids=${need.join(',')}`);
    for (const m of need) {
      let sym = null, dec = prices?.[m]?.decimals ?? null, usd = Number(prices?.[m]?.usdPrice) || null;
      try {
        const hits = await jget(`${JUPITER}/tokens/v2/search?query=${m}`);
        const hit = (hits ?? []).find(t => t.id === m);
        sym = hit?.symbol ?? null;
        dec = dec ?? hit?.decimals ?? null;
        usd = usd ?? (Number(hit?.usdPrice) || null);
      } catch { /* symbol is cosmetic; decimals must still come from somewhere */ }
      if (dec == null) throw new Error(`no decimals for mint ${m}: Jupiter does not know this token`);
      infoCache.set(m, { mint: m, symbol: sym ?? m.slice(0, 6), decimals: dec, usdPrice: usd });
    }
  }
  return mints.map(m => infoCache.get(m));
}

// Human -> raw without float drift: "0.1" with 9 decimals -> 100000000n.
function toRaw(human, decimals) {
  const s = String(human);
  if (!/^\d*\.?\d*$/.test(s) || s === '' || s === '.') throw new Error(`bad amount: ${human}`);
  const [ip = '0', fp = ''] = s.split('.');
  if (fp.length > decimals) return BigInt(ip + fp.slice(0, decimals));
  return BigInt(ip + fp.padEnd(decimals, '0'));
}
const toHuman = (raw, decimals) => Number(raw) / 10 ** decimals;

async function getQuote(inMint, outMint, rawIn) {
  const q = new URLSearchParams({
    inputMint: inMint, outputMint: outMint, amount: rawIn.toString(),
    slippageBps: String(SLIPPAGE_BPS), restrictIntermediateTokens: 'true',
  });
  const quote = await jget(`${JUPITER}/swap/v1/quote?${q}`);
  if (!quote?.outAmount) throw new Error(`quote has no outAmount: ${JSON.stringify(quote).slice(0, 200)}`);
  quote.__quotedAt = Date.now();
  return quote;
}

function quoteView(quote, inInfo, outInfo) {
  const impact = Number(quote.priceImpactPct);
  const outHuman = toHuman(quote.outAmount, outInfo.decimals);
  const inHuman = toHuman(quote.inAmount, inInfo.decimals);
  return {
    inMint: inInfo.mint, inSymbol: inInfo.symbol, amountIn: inHuman,
    outMint: outInfo.mint, outSymbol: outInfo.symbol,
    quoteOutAmount: outHuman,
    minOutAmount: toHuman(quote.otherAmountThreshold, outInfo.decimals),
    priceOutPerIn: inHuman > 0 ? outHuman / inHuman : null,
    priceImpactPct: impact,                                   // ratio, as Jupiter returns it
    priceImpactPercent: Number((impact * 100).toFixed(6)),    // the same number in percent
    slippageBps: quote.slippageBps,
    routePlan: (quote.routePlan ?? []).map(s => s.swapInfo?.label ?? '?'),
    swapUsdValue: quote.swapUsdValue != null ? Number(quote.swapUsdValue) : null,
    contextSlot: quote.contextSlot,
  };
}

function checkImpact(view) {
  if (!(view.priceImpactPct <= MAX_IMPACT)) {
    throw new Error(`price impact ${view.priceImpactPercent}% exceeds the ${MAX_IMPACT * 100}% limit; refusing`);
  }
}

async function buildSwapTx(quote, payer) {
  const built = await jpost(`${JUPITER}/swap/v1/swap`, {
    quoteResponse: stripInternal(quote), userPublicKey: payer.publicKey.toBase58(),
    wrapAndUnwrapSol: true, dynamicComputeUnitLimit: true, prioritizationFeeLamports: 'auto',
  });
  if (!built?.swapTransaction) throw new Error(`swap build returned no transaction: ${JSON.stringify(built).slice(0, 200)}`);
  const tx = VersionedTransaction.deserialize(Buffer.from(built.swapTransaction, 'base64'));
  return { built, tx };
}

function stripInternal(quote) { const { __quotedAt, ...q } = quote; return q; }

// --- verification: nothing Jupiter returns is signed on trust ----------------------
// Security review, 2026-09-26: the bot signed the transaction the API built
// without checking what it does. A compromised or intercepted API could have
// drained the wallet. Every check below runs before `sign`, dry run included.
export const ALLOWED_PROGRAMS = new Set([
  'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4',   // Jupiter aggregator v6
  '11111111111111111111111111111111',              // System
  'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',   // Token
  'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',   // Token-2022
  'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',  // Associated Token Account
  'ComputeBudget111111111111111111111111111111',   // Compute budget
]);
export const MAX_VALUE_LOSS = Number(process.env.LPBOT_MAX_VALUE_LOSS ?? 0.02);   // 2% below fair value
const SOL_OVERHEAD_LAMPORTS = 10_000_000;           // fees + temporary account rent, 0.01 SOL at most

// The quote must be the swap that was asked for, and worth what went in.
export function verifyQuote(quote, inInfo, outInfo, rawIn) {
  if (quote.inputMint !== inInfo.mint || quote.outputMint !== outInfo.mint) throw new Error('quote mints differ from the request; refusing');
  if (BigInt(quote.inAmount) !== BigInt(rawIn)) throw new Error('quote inAmount differs from the request; refusing');
  if ((quote.swapMode ?? 'ExactIn') !== 'ExactIn') throw new Error(`quote swapMode ${quote.swapMode}; refusing`);
  if (Number(quote.slippageBps) > SLIPPAGE_BPS) throw new Error(`quote slippage ${quote.slippageBps} bps above ${SLIPPAGE_BPS}; refusing`);
  if (BigInt(quote.otherAmountThreshold) > BigInt(quote.outAmount)) throw new Error('quote minimum exceeds its own output; refusing');
  if (inInfo.usdPrice != null && outInfo.usdPrice != null) {
    const inUsd = toHuman(quote.inAmount, inInfo.decimals) * inInfo.usdPrice;
    const outUsd = toHuman(quote.otherAmountThreshold, outInfo.decimals) * outInfo.usdPrice;
    if (outUsd < inUsd * (1 - MAX_VALUE_LOSS - Number(quote.slippageBps) / 1e4)) {
      throw new Error(`quote pays $${outUsd.toFixed(4)} at worst for $${inUsd.toFixed(4)}; more than ${MAX_VALUE_LOSS * 100}% below fair value; refusing`);
    }
  }
  return true;
}

// The transaction may pay from our wallet only and call allowed programs only.
// Programs cannot be loaded from lookup tables, so the static keys are complete.
export function verifyTxShape(tx, payerKey) {
  const keys = tx.message.staticAccountKeys.map(k => k.toBase58());
  if (keys[0] !== payerKey) throw new Error('transaction fee payer is not the wallet; refusing');
  if (tx.message.header.numRequiredSignatures !== 1) throw new Error('transaction needs signers other than the wallet; refusing');
  for (const ix of tx.message.compiledInstructions) {
    const pid = keys[ix.programIdIndex];
    if (pid === undefined || !ALLOWED_PROGRAMS.has(pid)) throw new Error(`transaction calls ${pid ?? 'a program from a lookup table'}; refusing`);
  }
  return true;
}

function splAmount(b64) {
  const buf = Buffer.from(b64, 'base64');
  return buf.length >= 72 ? buf.readBigUInt64LE(64) : 0n;
}

// Simulate the unsigned transaction and compare the wallet's balances before and after.
async function verifyBalances(connection, payer, tx, inInfo, outInfo, rawIn, quote) {
  const owner = payer.publicKey;
  const acct = async (info) => {
    if (info.mint === NATIVE_MINT) return { addr: owner, native: true };
    const mi = await connection.getAccountInfo(new PublicKey(info.mint));
    return { addr: spl.getAssociatedTokenAddressSync(new PublicKey(info.mint), owner, false, mi.owner), native: false };
  };
  const a = await acct(inInfo), b = await acct(outInfo);
  const addrs = [a.addr, b.addr, owner].map(k => k.toBase58());
  const uniq = [...new Set(addrs)];
  const pre = await connection.getMultipleAccountsInfo(uniq.map(k => new PublicKey(k)));
  const sim = await connection.simulateTransaction(tx, { sigVerify: false, replaceRecentBlockhash: true,
    accounts: { encoding: 'base64', addresses: uniq } });
  if (sim.value.err) throw new Error(`simulation failed: ${JSON.stringify(sim.value.err).slice(0, 160)}`);
  const post = sim.value.accounts;
  const val = (list, i, native) => {
    const x = list[i];
    if (!x) return 0n;
    if (native) return BigInt(x.lamports);
    const data = Array.isArray(x.data) ? x.data[0] : (x.data?.toString?.('base64') ?? x.data);
    return splAmount(typeof data === 'string' ? data : Buffer.from(x.data).toString('base64'));
  };
  const ix = (k) => uniq.indexOf(k.toBase58());
  const inPre = val(pre, ix(a.addr), a.native), inPost = val(post, ix(a.addr), a.native);
  const outPre = val(pre, ix(b.addr), b.native), outPost = val(post, ix(b.addr), b.native);
  const solPre = val(pre, ix(owner), true), solPost = val(post, ix(owner), true);
  const inDrop = inPre - inPost, outGain = outPost - outPre, solDrop = solPre - solPost;
  if (inDrop > BigInt(rawIn) + (a.native ? BigInt(SOL_OVERHEAD_LAMPORTS) : 0n)) throw new Error(`simulation takes ${inDrop} of ${inInfo.symbol}, more than ${rawIn}; refusing`);
  if (outGain < BigInt(quote.otherAmountThreshold) - (b.native ? BigInt(SOL_OVERHEAD_LAMPORTS) : 0n)) throw new Error(`simulation pays ${outGain} of ${outInfo.symbol}, under the minimum ${quote.otherAmountThreshold}; refusing`);
  if (!a.native && !b.native && solDrop > BigInt(SOL_OVERHEAD_LAMPORTS)) throw new Error(`simulation spends ${solDrop} lamports of SOL; refusing`);
  return { inDrop: inDrop.toString(), outGain: outGain.toString(), solDrop: solDrop.toString() };
}

// Decimals from the mint account itself; a hint that disagrees is refused.
async function chainDecimals(connection, info) {
  if (info.mint === NATIVE_MINT) return 9;
  const mi = await connection.getAccountInfo(new PublicKey(info.mint));
  if (!mi || mi.data.length < 45) throw new Error(`mint ${info.mint} not readable on chain`);
  return mi.data[44];
}

// --- wallet reads -------------------------------------------------------------------
async function rawBalance(connection, owner, mint) {
  if (mint === NATIVE_MINT) return BigInt(await connection.getBalance(owner));
  const r = await connection.getParsedTokenAccountsByOwner(owner, { mint: new PublicKey(mint) });
  let raw = 0n;
  for (const a of r.value ?? []) raw += BigInt(a.account.data.parsed.info.tokenAmount.amount ?? 0);
  return raw;
}

// What the wallet may sell of a mint: the native balance keeps the gas reserve.
async function sellable(connection, owner, info) {
  const raw = await rawBalance(connection, owner, info.mint);
  const total = toHuman(raw, info.decimals);
  const avail = info.mint === NATIVE_MINT ? Math.max(0, total - GAS_RESERVE_SOL) : total;
  return { total, avail, usd: info.usdPrice != null ? avail * info.usdPrice : null };
}

// --- the swap itself -------------------------------------------------------------
// Quote, build, (dry run: report) or (execute: sign, send once, confirm).
async function performSwap({ connection, payer }, inInfo, outInfo, amountHuman, execute, extra = {}) {
  for (const info of [inInfo, outInfo]) {
    const d = await chainDecimals(connection, info);
    if (info.decimals !== d) throw new Error(`${info.symbol} decimals ${info.decimals} disagree with the chain's ${d}; refusing`);
  }
  const rawIn = toRaw(amountHuman, inInfo.decimals);
  if (rawIn <= 0n) throw new Error(`amount ${amountHuman} ${inInfo.symbol} rounds to zero`);
  const have = await sellable(connection, payer.publicKey, inInfo);
  if (toHuman(rawIn, inInfo.decimals) > have.avail + 1e-12) {
    throw new Error(`wallet can sell ${have.avail} ${inInfo.symbol}`
      + (inInfo.mint === NATIVE_MINT ? ` (after the ${GAS_RESERVE_SOL} SOL gas reserve)` : '')
      + `, asked ${amountHuman}`);
  }
  const quote = await getQuote(inInfo.mint, outInfo.mint, rawIn);
  verifyQuote(quote, inInfo, outInfo, rawIn);
  const view = quoteView(quote, inInfo, outInfo);
  checkImpact(view);
  const { built, tx } = await buildSwapTx(quote, payer);
  verifyTxShape(tx, payer.publicKey.toBase58());
  const verified = await verifyBalances(connection, payer, tx, inInfo, outInfo, rawIn, quote);
  const report = {
    verified,
    ...extra,
    sold: { mint: inInfo.mint, symbol: inInfo.symbol, amount: view.amountIn,
      usd: inInfo.usdPrice != null ? Number((view.amountIn * inInfo.usdPrice).toFixed(4)) : null },
    bought: { mint: outInfo.mint, symbol: outInfo.symbol, amount: view.quoteOutAmount,
      usd: outInfo.usdPrice != null ? Number((view.quoteOutAmount * outInfo.usdPrice).toFixed(4)) : null },
    quoteOutAmount: view.quoteOutAmount, minOutAmount: view.minOutAmount,
    priceImpactPct: view.priceImpactPct, priceImpactPercent: view.priceImpactPercent,
    slippageBps: view.slippageBps, routePlan: view.routePlan, swapUsdValue: view.swapUsdValue,
    transaction: {
      version: tx.version, bytes: tx.serialize().length,
      signaturesRequired: tx.message.header.numRequiredSignatures,
      instructions: tx.message.compiledInstructions.length,
      lookupTables: tx.message.addressTableLookups.length,
      lastValidBlockHeight: built.lastValidBlockHeight,
      prioritizationFeeLamports: built.prioritizationFeeLamports ?? null,
      computeUnitLimit: built.computeUnitLimit ?? null,
      simulationError: built.simulationError ?? null,
    },
    signature: null, sent: false,
  };
  if (!execute) {
    console.log(JSON.stringify(report, null, 1));
    console.log('DRY RUN — quote taken and swap transaction built. Pass --execute to sign and send.');
    return report;
  }
  if (built.simulationError) {
    throw new Error(`Jupiter simulated the swap and it failed: ${JSON.stringify(built.simulationError).slice(0, 200)}`);
  }
  const age = Date.now() - quote.__quotedAt;
  if (age > QUOTE_MAX_AGE_MS) throw new Error(`quote is ${(age / 1000).toFixed(1)}s old at send time (limit ${QUOTE_MAX_AGE_MS / 1000}s); refusing`);
  tx.sign([payer]);
  const raw = tx.serialize();
  // From here on the transaction may be on chain: no retry, report what we know.
  let signature = null;
  try {
    signature = await connection.sendRawTransaction(raw, { skipPreflight: false, maxRetries: 2 });
    const conf = await connection.confirmTransaction({
      signature, blockhash: tx.message.recentBlockhash, lastValidBlockHeight: built.lastValidBlockHeight,
    }, 'confirmed');
    if (conf.value?.err) throw new Error(`transaction ${signature} failed on chain: ${JSON.stringify(conf.value.err)}`);
  } catch (e) {
    if (!signature) throw e;                     // nothing left this process; a plain error
    console.log(JSON.stringify({ ...report, signature, sent: true, partial: true, error: String(e.message ?? e) }, null, 1));
    throw new SentError(`sent ${signature} but could not confirm it: ${e.message}`);
  }
  const out = { ...report, signature, sent: true };
  console.log(JSON.stringify(out, null, 1));
  return out;
}

// --- commands ---------------------------------------------------------------------------
async function quoteCmd(inMint, outMint, amount) {
  guard();
  const [inInfo, outInfo] = await tokenInfos([assertMint(inMint, 'inMint'), assertMint(outMint, 'outMint')]);
  const rawIn = toRaw(amount, inInfo.decimals);
  if (rawIn <= 0n) throw new Error(`amount ${amount} ${inInfo.symbol} rounds to zero`);
  const quote = await getQuote(inInfo.mint, outInfo.mint, rawIn);
  const view = quoteView(quote, inInfo, outInfo);
  view.inUsd = inInfo.usdPrice != null ? Number((view.amountIn * inInfo.usdPrice).toFixed(4)) : null;
  view.outUsd = outInfo.usdPrice != null ? Number((view.quoteOutAmount * outInfo.usdPrice).toFixed(4)) : null;
  view.withinImpactLimit = view.priceImpactPct <= MAX_IMPACT;
  console.log(JSON.stringify(view, null, 1));
  return view;
}

async function swapCmd(inMint, outMint, amount, execute) {
  guard();
  const [inInfo, outInfo] = await tokenInfos([assertMint(inMint, 'inMint'), assertMint(outMint, 'outMint')]);
  return withRpc(ctx => performSwap(ctx, inInfo, outInfo, amount, execute));
}

// Decide the one swap that brings the wallet's split of A and B to the targets.
// Pure so it can be reasoned about: returns null for noop.
export function planRebalance(usdA, usdB, targetA, targetB) {
  const total = usdA + usdB, want = targetA + targetB;
  if (!(want > 0)) return null;
  // Enough value: fill the short side up to its target from the other's surplus.
  // Too little: split what there is in the targets' proportion.
  const mode = total >= want ? 'fill' : 'proportional';
  const desiredA = mode === 'fill' ? targetA : total * targetA / want;
  const desiredB = mode === 'fill' ? targetB : total * targetB / want;
  let side, sellUsd, deficit;
  if (usdA < desiredA) { side = 'A'; deficit = desiredA - usdA; sellUsd = Math.min(deficit, Math.max(0, usdB - desiredB)); }
  else if (usdB < desiredB) { side = 'B'; deficit = desiredB - usdB; sellUsd = Math.min(deficit, Math.max(0, usdA - desiredA)); }
  else return null;
  const ref = side === 'A' ? desiredA : desiredB;
  if (deficit <= TARGET_TOLERANCE * ref || !(sellUsd > 0)) return null;
  return { mode, buySide: side, sellSide: side === 'A' ? 'B' : 'A', sellUsd, desiredA, desiredB };
}

async function rebalanceCmd(mintA, mintB, targetA, targetB, execute) {
  guard();
  const [infoA, infoB] = await tokenInfos([assertMint(mintA, 'mintA'), assertMint(mintB, 'mintB')]);
  if (infoA.mint === infoB.mint) throw new Error('mintA and mintB are the same token');
  for (const i of [infoA, infoB]) if (i.usdPrice == null) throw new Error(`no USD price for ${i.symbol} (${i.mint}); cannot size a rebalance`);
  const tA = Number(targetA), tB = Number(targetB);
  if (!(tA >= 0 && tB >= 0)) throw new Error(`targets must be non-negative dollars, got ${targetA} ${targetB}`);
  return withRpc(async (ctx) => {
    const { connection, payer } = ctx;
    const balA = await sellable(connection, payer.publicKey, infoA);
    const balB = await sellable(connection, payer.publicKey, infoB);
    const balances = {
      owner: payer.publicKey.toBase58(), gasReserveSol: GAS_RESERVE_SOL,
      A: { mint: infoA.mint, symbol: infoA.symbol, amount: balA.total, sellable: balA.avail, usd: Number(balA.usd.toFixed(4)), usdPrice: infoA.usdPrice },
      B: { mint: infoB.mint, symbol: infoB.symbol, amount: balB.total, sellable: balB.avail, usd: Number(balB.usd.toFixed(4)), usdPrice: infoB.usdPrice },
      targetUsdA: tA, targetUsdB: tB,
    };
    const plan = planRebalance(balA.usd, balB.usd, tA, tB);
    if (!plan) {
      const out = { noop: true, reason: `both sides within ${TARGET_TOLERANCE * 100}% of target or nothing to sell`, ...balances };
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    const sellInfo = plan.sellSide === 'A' ? infoA : infoB;
    const buyInfo = plan.sellSide === 'A' ? infoB : infoA;
    const sellBal = plan.sellSide === 'A' ? balA : balB;
    // Human amount, floored to the token's decimals, never above what is sellable.
    let amount = Math.min(plan.sellUsd / sellInfo.usdPrice, sellBal.avail);
    amount = Number(amount.toFixed(sellInfo.decimals));
    if (!(amount > 0)) {
      const out = { noop: true, reason: 'the amount to sell rounds to zero', ...balances };
      console.log(JSON.stringify(out, null, 1));
      return out;
    }
    return performSwap(ctx, sellInfo, buyInfo, amount, execute, {
      mode: plan.mode, sellSide: plan.sellSide, buySide: plan.buySide,
      sellUsdPlanned: Number(plan.sellUsd.toFixed(4)),
      desiredUsdA: Number(plan.desiredA.toFixed(4)), desiredUsdB: Number(plan.desiredB.toFixed(4)),
      before: balances,
    });
  });
}

async function main() {
  const args = process.argv.slice(2);
  const execute = args.includes('--execute');
  const [cmd, ...rest] = args.filter(x => x !== '--execute');
  if (cmd === 'quote') return quoteCmd(rest[0], rest[1], rest[2]);
  if (cmd === 'swap') return swapCmd(rest[0], rest[1], rest[2], execute);
  if (cmd === 'rebalance') return rebalanceCmd(rest[0], rest[1], rest[2], rest[3], execute);
  console.log('commands: quote <inMint> <outMint> <amountIn> | swap <inMint> <outMint> <amountIn> [--execute] '
    + '| rebalance <mintA> <mintB> <targetUsdA> <targetUsdB> [--execute]   (amounts in human units; targets in USD)');
}

if (process.argv[1] && path.resolve(process.argv[1]) === new URL(import.meta.url).pathname) {
  main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
}
