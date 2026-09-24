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
import {
  setRpc, setPayerFromBytes, setNativeMintWrappingStrategy,
  openConcentratedPosition, fetchPositionsForOwner,
  closePosition, harvestPosition, closePositionInstructions,
} from '@orca-so/whirlpools';
import { createSolanaRpc, address } from '@solana/kit';

const DIR = path.dirname(new URL(import.meta.url).pathname);
const HALT = path.join(DIR, 'HALT');
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
  if (fs.existsSync(HALT)) {
    throw new Error(`HALT present: ${fs.readFileSync(HALT, 'utf8').trim()}`);
  }
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

// Dollar value of one unit of the pool's quote token. On a USDC-quoted pool
// this is 1; on SOL/xSOL it is not, and pretending otherwise turns every
// dollar figure the bot reports into nonsense.
const STABLES = new Set(['USDC', 'USDT', 'PYUSD', 'USDS', 'DAI', 'FDUSD', 'USDE']);

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
  if (STABLES.has(info.symbolB)) return { usd: 1, source: 'stable' };
  try {
    const p = await tokenUsd(info.mintB);
    if (p) return { usd: p, source: 'geckoterminal:mint' };
  } catch { /* fall through to the honest answer below */ }
  return { usd: null, source: 'unknown' };
}

// Dollar price of native SOL, for valuing the gas balance when SOL is not one
// of the pool's tokens. When it is, the pool's own price is the answer.
async function solUsd(info, qUsd) {
  if (info.mintA === NATIVE_MINT && qUsd != null) return info.price * qUsd;
  if (info.mintB === NATIVE_MINT && qUsd != null) return qUsd;
  try { return await tokenUsd(NATIVE_MINT); } catch { return null; }
}

// Human-unit balance of one SPL mint. The native mint is the lamport balance:
// the wrapping strategy wraps it into an ATA on demand at open time.
async function tokenBalance(rpc, owner, mint, decimals, lamports) {
  if (mint === NATIVE_MINT) return lamports / 1e9;
  const r = await rpc.getTokenAccountsByOwner(owner, { mint: address(mint) },
    { encoding: 'jsonParsed' }).send();
  let raw = 0n;
  for (const a of r.value ?? []) {
    raw += BigInt(a.account?.data?.parsed?.info?.tokenAmount?.amount ?? 0);
  }
  return Number(raw) / 10 ** decimals;
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
const ENDPOINTS = [RPC, 'https://api.mainnet-beta.solana.com',
                   'https://solana-rpc.publicnode.com']
  .filter((v, i, a) => v && a.indexOf(v) === i);

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
async function withRpc(fn) {
  let lastErr = null;
  for (const url of ENDPOINTS) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        return await fn(await connectTo(url));
      } catch (e) {
        lastErr = e;
        const msg = String(e?.message ?? e);
        if (!/429|Too Many Requests|rate/i.test(msg)) break;   // not transient
        await new Promise(r => setTimeout(r, 2500 * (attempt + 1)));
      }
    }
  }
  throw new Error(`all RPC endpoints failed: ${String(lastErr?.message ?? lastErr).slice(0, 160)}`);
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
      const { usd: qUsd } = await quoteUsd(info);
      const sUsd = await solUsd(info, qUsd);
      out.pool = pool;
      out.tokenA = info.symbolA; out.tokenB = info.symbolB;
      out.price = info.price; out.quoteUsd = qUsd;
      out.balanceA = await tokenBalance(rpc, signer.address, info.mintA, info.decimalsA, lamports);
      out.balanceB = await tokenBalance(rpc, signer.address, info.mintB, info.decimalsB, lamports);
      out.nativeSide = info.mintA === NATIVE_MINT ? 'A' : info.mintB === NATIVE_MINT ? 'B' : null;
      // The pool's two tokens in quote units, then dollars; plus the gas SOL
      // when it is not already one of them.
      const inQuote = out.balanceA * info.price + out.balanceB;
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

async function open(pool, lower, upper, maxA, maxB, execute) {
  const info = await poolInfo(pool);
  // The liquidity instructions this package builds are rejected by adaptive-fee
  // pools with custom error 6069, which reads like slippage and is not. Refuse
  // before any token is bought for it; the first time this cost a round trip.
  if (info.adaptiveFee) {
    throw new Error(`${info.symbolA}/${info.symbolB} is an adaptive-fee pool; this `
      + 'signer cannot open positions on it (Whirlpool error 6069). Choose another pool.');
  }
  return withRpc(async ({ signer, rpc }) => {
    const lamports = await rpc.getBalance(signer.address).send();
    if (Number(lamports.value) / 1e9 < GAS_RESERVE_SOL) {
      throw new Error(`SOL below the ${GAS_RESERVE_SOL} gas reserve; a wallet that cannot `
        + 'pay fees cannot close its own position');
    }
    // Size the cap at the pool's own price rather than a constant. The old
    // constant was the SOL price on the day it was written.
    const { usd: qUsd } = await quoteUsd(info);
    const approxUsd = (Number(maxA) * info.price + Number(maxB)) * (qUsd ?? 1);
    if (approxUsd > MAX_USD) {
      throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
    }
    const param = {
      tokenMaxA: BigInt(Math.floor(Number(maxA) * 10 ** info.decimalsA)),
      tokenMaxB: BigInt(Math.floor(Number(maxB) * 10 ** info.decimalsB)),
    };
    // Nothing in 8.0.1 returns the deposit quote, so it is computed here from
    // the CLMM amount formulas: the liquidity both caps allow, and what that
    // liquidity takes of each token at the current price. The caps are
    // ceilings; the ledger must record what actually goes in, which on a
    // wallet short of one side is less than the capital. After a live open
    // the bot replaces this estimate with the chain's own mark.
    const quote = depositQuote(info.price, Number(lower), Number(upper), Number(maxA), Number(maxB));
    const estA = quote?.estA ?? null;
    const estB = quote?.estB ?? null;

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
        ? Number(((estA * info.price + estB) * qUsd).toFixed(4)) : null,
      positionMint: result.positionMint ?? null,
      quote,
      initializationCost: result.initializationCost?.toString() ?? null,
      instructions: result.instructions?.length ?? 0,
    };
    if (!execute) {
      console.log(JSON.stringify({ ...report, sent: false }, null, 1));
      console.log('DRY RUN — instructions built. Pass --execute to sign and send.');
      return;
    }
    const sig = await result.callback();
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
    const chosen = mintArg
      ? hydrated.find(p => p.data.positionMint === mintArg)
      : hydrated[0];
    if (!chosen) {
      // Explicit and parseable: the read worked and there is genuinely nothing.
      // The caller must be able to tell this apart from a failed read.
      console.log(JSON.stringify({ positions: 0, positionMint: null }, null, 1));
      return null;
    }
    const d = chosen.data;
    const info = await poolInfo(d.whirlpool);
    const price = info.price;
    const q = await quoteUsd(info);
    // Ticks are in raw-amount space; the decimal difference converts them to
    // the human price the pool quotes.
    const scale = 10 ** (info.decimalsA - info.decimalsB);
    const lower = 1.0001 ** d.tickLowerIndex * scale;
    const upper = 1.0001 ** d.tickUpperIndex * scale;
    const ua = (x) => Number(x) / 10 ** info.decimalsA;
    const ub = (x) => Number(x) / 10 ** info.decimalsB;
    const out = {
      positionMint: d.positionMint,
      whirlpool: d.whirlpool,
      pair: `${info.symbolA}/${info.symbolB}`,
      tokenA: info.symbolA, tokenB: info.symbolB,
      decimalsA: info.decimalsA, decimalsB: info.decimalsB,
      quoteUsd: q.usd, quoteUsdSource: q.source,
      liquidity: d.liquidity.toString(),
      tickLower: d.tickLowerIndex, tickUpper: d.tickUpperIndex,
      lowerPrice: Number(lower.toFixed(6)), upperPrice: Number(upper.toFixed(6)),
      price: Number(price.toFixed(6)),
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
            ((out.closeEstA * price + out.closeEstB) * q.usd).toFixed(4));
        }
      }
      if (cq?.feesQuote) {
        out.feesAccruedA = ua(cq.feesQuote.feeOwedA);
        out.feesAccruedB = ub(cq.feesQuote.feeOwedB);
        // Value the fees in quote units first, then in dollars. Collapsing
        // straight to dollars assumes token B is a dollar, which is true of
        // USDC pools and of nothing else.
        const inQuote = out.feesAccruedA * price + out.feesAccruedB;
        out.feesAccrued_quote = Number(inQuote.toFixed(9));
        if (q.usd != null) out.feesAccrued_USD = Number((inQuote * q.usd).toFixed(6));
      }
    } catch { /* reporting only: never fail a status read over fee accounting */ }
    console.log(JSON.stringify(out, null, 1));
    return out;
  });
}

async function harvest(mint, execute) {
  if (!execute) { console.log('DRY RUN — pass --execute to collect fees.'); return; }
  // Writes retry across endpoints too. A 429 here lands while the SDK is
  // FETCHING accounts to build the instruction, before anything is signed or
  // sent, so rotating endpoints is safe. The caller still re-reads chain state
  // after any failure rather than trusting the error alone.
  return withRpc(async ({ signer }) => {
    // authority, not funder: harvest and close act on a position you own, and
    // both return an ActionResult that still needs its callback invoked.
    const result = await harvestPosition(address(mint), { authority: signer });
    const sig = await result.callback();
    console.log(JSON.stringify({ harvested: mint, signature: sig }, null, 1));
  });
}

async function close(mint, execute) {
  return withRpc(async ({ signer, rpc }) => {
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
        sent: false }, null, 1));
      console.log('DRY RUN — close instructions built. Pass --execute to send.');
      return;
    }
    const result = await closePosition(address(mint),
      { slippageToleranceBps: SLIPPAGE_BPS, authority: signer });
    const sig = await result.callback();
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
  if (cmd === 'pool') return poolInfo(a[0]).then(i => console.log(JSON.stringify(i, null, 1)));
  console.log('commands: balance [pool] | positions | status [mint] | pool <pool> | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <mint> [--execute] '
    + '| close <mint> [--execute]');
}

main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
