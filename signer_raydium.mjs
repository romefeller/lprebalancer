// Rebalancer — the Raydium CLMM signing layer. Same commands and the same
// output fields as signer2.mjs and signer_dlmm.mjs, so the loop cannot tell
// which DEX it is on. SIGNER_CONTRACT.md is the specification.
//
// Built on @raydium-io/raydium-sdk-v2 0.2.73-alpha. A Raydium CLMM position is
// a pair of ticks, as on Orca: tick i has price 1.0001^i in raw units, and a
// pool with tickSpacing s only accepts ticks that are multiples of s. The
// position is owned through an NFT; its mint is the ledger id (`positionMint`).
// One position account holds the whole band, so an open is one transaction.
// The wallet can still hold several positions on one pool (a failed close, an
// open retried by hand); as signer_dlmm.mjs does, `status` reports their union,
// `harvest` claims them all, `close` empties and closes them all, and the
// first (lowest tick) position's mint is the id of the lot.
//
// Fees: the SDK's PositionUtils.GetPositionFees recomputes accrued fees from
// the pool's fee growth and the two boundary ticks, so `feesAccruedA/B` are the
// live figures, not the stale `tokenFeesOwed` the program settles only when
// the position is touched. If the tick accounts cannot be read, the stale
// figure is reported and `feesSource` says so.
//
// The key is read from WALLET_SECRET_PATH inside this process, handed to the
// SDK as the owner, and never printed. The pool comes from --pool <address> or
// LPBOT_POOL for every command that needs one.
//
// Commands:
//   node signer_raydium.mjs balance [pool]
//   node signer_raydium.mjs positions
//   node signer_raydium.mjs status [position]
//   node signer_raydium.mjs pool [pool]
//   node signer_raydium.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node signer_raydium.mjs harvest <position> [--execute]
//   node signer_raydium.mjs close <position> [--execute]
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

// The SDK's ESM build loads under Node 24, but the CommonJS build is used so
// that this file, the SDK and web3.js share one copy of PublicKey and BN.
const require = createRequire(import.meta.url);
const {
  Raydium, TxVersion, TickUtil, TickArrayUtil, PositionUtils, LiquidityMathUtil,
  TickArrayLayout, getPdaTickArrayAddress, CLMM_PROGRAM_ID,
} = require('@raydium-io/raydium-sdk-v2');
const { Connection, Keypair, PublicKey } = require('@solana/web3.js');
const BN = require('bn.js');
const Decimal = require('decimal.js');

const DIR = path.dirname(new URL(import.meta.url).pathname);
const HALT = path.join(DIR, 'HALT');
const RPC = process.env.SOLANA_RPC_URL ?? process.env.LPBOT_RPC ?? 'https://api.mainnet-beta.solana.com';
const ENDPOINTS = [RPC, 'https://api.mainnet-beta.solana.com', 'https://solana-rpc.publicnode.com']
  .filter((v, i, a) => v && a.indexOf(v) === i);

const MAX_USD = Number(process.env.LPBOT_MAX_USD ?? 260);
const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.02);

const DEX = 'raydium-clmm';
const PROGRAM_ID = new PublicKey('CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK');
const NATIVE_MINT = 'So11111111111111111111111111111111111111112';
const STABLES = new Set(['USDC', 'USDT', 'PYUSD', 'USDS', 'DAI', 'FDUSD', 'USDE']);
const RAYDIUM_API = 'https://api-v3.raydium.io';
const JUPITER = 'https://lite-api.jup.ag';
const HEADERS = { accept: 'application/json', 'user-agent': 'Mozilla/5.0' };
// Legacy transactions: the SDK confirms them by polling. Its V0 path confirms
// through a websocket subscription with a bare 60 s timeout, which a public
// RPC without websockets never answers.
const TX_VERSION = TxVersion.LEGACY;

if (!CLMM_PROGRAM_ID.equals(PROGRAM_ID)) {
  throw new Error(`SDK program id ${CLMM_PROGRAM_ID.toBase58()} differs from the expected CLMM program`);
}

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
    const j = await fetchJson(`${JUPITER}/price/v3?ids=${mint}`);
    const p = Number(j?.[mint]?.usdPrice);
    return p > 0 ? p : null;
  } catch { return null; }
}

async function quoteUsd(symB, mintB) {
  if (STABLES.has(symB)) return { usd: 1, source: 'stable' };
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

async function describe(pool, r) {
  const pi = r.poolInfo, rpc = r.rpcPoolInfo;
  const da = pi.mintA.decimals, db = pi.mintB.decimals;
  const sym = await symbols(pool, pi);
  const q = await quoteUsd(sym.b, pi.mintB.address);
  return {
    pool, dex: DEX, programId: pi.programId,
    tickSpacing: pi.config.tickSpacing, tickCurrent: rpc.tickCurrent,
    // the pool's own sqrt price, so the tick boundaries and the price agree
    price: Number(TickUtil.sqrtPriceX64ToPrice(rpc.sqrtPriceX64, da, db).toString()),
    feeRate: Number(pi.feeRate) / 1e6,
    liquidity: rpc.liquidity.toString(),
    symbolA: sym.a, symbolB: sym.b, decimalsA: da, decimalsB: db,
    mintA: pi.mintA.address, mintB: pi.mintB.address,
    quoteUsd: q.usd, quoteUsdSource: q.source,
    nativeSide: pi.mintA.address === NATIVE_MINT ? 'A' : pi.mintB.address === NATIVE_MINT ? 'B' : null,
  };
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

// Retry the whole operation across endpoints on a rate limit, as the other
// signers do. Only a transport failure moves to the next endpoint: an answer
// the chain gave ("position not found", "price moved") is final and is
// reported as itself, not masked by whatever the last fallback says. An error
// that carries `sent` happened after a transaction went out; it is never
// retried, whatever its text says.
const RATE_LIMITED = /429|Too Many Requests|rate limit/i;
const TRANSPORT = /429|Too Many Requests|rate limit|403|Request blocked|fetch failed|ECONN|ETIMEDOUT|ENOTFOUND|socket|timed? ?out|50\d\b|Service Unavailable|Bad Gateway/i;

async function withRpc(fn, opts) {
  let lastErr = null;
  for (const url of ENDPOINTS) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        return await fn(await connect(url, opts));
      } catch (e) {
        if (e?.sent) throw e;
        const msg = String(e?.message ?? e);
        if (!TRANSPORT.test(msg)) throw e;                    // the chain answered; that is the answer
        lastErr = e;
        if (!RATE_LIMITED.test(msg)) break;                   // endpoint down or blocked: next one
        await new Promise(r => setTimeout(r, 2500 * (attempt + 1)));
      }
    }
  }
  throw new Error(`all RPC endpoints failed: ${String(lastErr?.message ?? lastErr).slice(0, 160)}`);
}

async function splBalance(connection, owner, mint, decimals, lamports) {
  if (mint === NATIVE_MINT) return lamports / 1e9;
  const r = await connection.getParsedTokenAccountsByOwner(owner, { mint: new PublicKey(mint) });
  let raw = 0n;
  for (const a of r.value ?? []) raw += BigInt(a.account.data.parsed.info.tokenAmount.amount ?? 0);
  return Number(raw) / 10 ** decimals;
}

async function balance(poolExplicit) {
  const pool = poolArg(poolExplicit);
  return withRpc(async ({ connection, payer, raydium }) => {
    const info = await describe(pool, await loadPool(raydium, pool));
    const lamports = await connection.getBalance(payer.publicKey);
    const out = { owner: payer.publicKey.toBase58(), sol: lamports / 1e9, pool, dex: DEX,
      tokenA: info.symbolA, tokenB: info.symbolB, price: info.price, quoteUsd: info.quoteUsd,
      nativeSide: info.nativeSide };
    out.balanceA = await splBalance(connection, payer.publicKey, info.mintA, info.decimalsA, lamports);
    out.balanceB = await splBalance(connection, payer.publicKey, info.mintB, info.decimalsB, lamports);
    const inQuote = out.balanceA * info.price + out.balanceB;
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

// The boundary tick states of every position, one RPC call. A tick that was
// never initialised has no account and reads as null.
async function tickStates(connection, pool, list, spacing) {
  const starts = [...new Set(list.flatMap(p => [p.tickLower, p.tickUpper])
    .map(t => TickArrayUtil.getTickArrayStartIndex(t, spacing)))];
  const keys = starts.map(s => getPdaTickArrayAddress(PROGRAM_ID, new PublicKey(pool), s).publicKey);
  const accounts = keys.length ? await connection.getMultipleAccountsInfo(keys) : [];
  const arrays = new Map();
  accounts.forEach((a, i) => { if (a) arrays.set(starts[i], TickArrayLayout.decode(a.data)); });
  return (tick) => {
    const arr = arrays.get(TickArrayUtil.getTickArrayStartIndex(tick, spacing));
    return arr ? arr.ticks[TickArrayUtil.getTickOffsetInArray(tick, spacing)] : null;
  };
}

// What the position holds now and what it has earned, in raw units.
function positionAmounts(p, r, tickOf) {
  const rpc = r.rpcPoolInfo;
  const { amountA, amountB } = LiquidityMathUtil.getAmountsForLiquidity(
    rpc.sqrtPriceX64, TickUtil.getSqrtPriceAtTick(p.tickLower), TickUtil.getSqrtPriceAtTick(p.tickUpper),
    p.liquidity, false);
  let feeA = p.tokenFeesOwedA, feeB = p.tokenFeesOwedB, feesSource = 'tokenFeesOwed (stale)';
  const lo = tickOf(p.tickLower), hi = tickOf(p.tickUpper);
  if (lo && hi) {
    try {
      const f = PositionUtils.GetPositionFees(rpc, p, lo, hi);
      feeA = f.tokenFeeAmountA; feeB = f.tokenFeeAmountB; feesSource = 'feeGrowth';
    } catch { /* the stale figure stands */ }
  }
  return { amountA, amountB, feeA, feeB, feesSource };
}

function positionView(p, r, info, tickOf) {
  const ua = (x) => Number(x.toString()) / 10 ** info.decimalsA;
  const ub = (x) => Number(x.toString()) / 10 ** info.decimalsB;
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
    price: Number(info.price.toFixed(6)),
    inRange: info.tickCurrent >= p.tickLower && info.tickCurrent < p.tickUpper,
    closeEstA: estA, closeEstB: estB,
    feesAccruedA: feeA, feesAccruedB: feeB, feesSource: am.feesSource,
    feesAccrued_quote: Number((feeA * info.price + feeB).toFixed(9)),
  };
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((estA * info.price + estB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number((out.feesAccrued_quote * info.quoteUsd).toFixed(6));
  }
  return out;
}

// The union of every position the wallet holds on the pool, as one.
function unionView(list, r, info, tickOf) {
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
  out.feesAccrued_quote = Number((out.feesAccruedA * info.price + out.feesAccruedB).toFixed(9));
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((out.closeEstA * info.price + out.closeEstB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number((out.feesAccrued_quote * info.quoteUsd).toFixed(6));
  }
  return out;
}

async function status(positionArg) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r);
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
  const pdas = nftMints.map(m => PublicKey.findProgramAddressSync(
    [Buffer.from('position'), m.toBuffer()], programId)[0]);
  const infos = await connection.getMultipleAccountsInfo(pdas);
  let lamports = infos.reduce((n, a) => n + (a?.lamports ?? 0), 0);
  for (const m of nftMints) {
    for (const prog of [TOKEN_PROGRAM_ID_RENT, TOKEN_2022_PROGRAM_ID_RENT]) {
      try {
        const r = await connection.getTokenAccountsByOwner(owner, { mint: m }, { programId: prog });
        lamports += (r.value ?? []).reduce((n, a) => n + (a.account?.lamports ?? 0), 0);
      } catch { /* one of the two programs owns it; the other answers empty or errors */ }
    }
  }
  return lamports / 1e9;
}
const TOKEN_PROGRAM_ID_RENT = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const TOKEN_2022_PROGRAM_ID_RENT = new PublicKey('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb');

async function rentUsdOf(info, rentSol) {
  const su = info.nativeSide === 'A' && info.quoteUsd != null ? info.price * info.quoteUsd
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
// liquidity takes. From signer2.mjs; human units.
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
    const { txId } = await built.execute({ sendAndConfirm: true, skipPreflight: false });
    return txId;
  } finally { console.log = log; }
}

// Send several built transactions in order. After the first one lands, a
// failure is reported as a partial send and never retried.
async function sendAll(builts, report) {
  const sigs = [];
  for (const b of builts) {
    try {
      sigs.push(await sendBuilt(b));
    } catch (e) {
      if (!sigs.length) throw e;
      console.log(JSON.stringify({ ...report, sent: true, partial: true, signature: sigs[sigs.length - 1],
        signatures: sigs, error: String(e?.message ?? e).slice(0, 300) }, null, 1));
      throw Object.assign(new Error(`partial send: ${sigs.length}/${builts.length} sent; ${e?.message ?? e}`), { sent: true });
    }
  }
  return sigs;
}

async function open(pool, lower, upper, maxA, maxB, execute) {
  [lower, upper, maxA, maxB] = [lower, upper, maxA, maxB].map(Number);
  if (![lower, upper, maxA, maxB].every(Number.isFinite)) throw new Error('open needs numeric <lower> <upper> <maxA> <maxB>');
  return withRpc(async ({ connection, payer, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r);
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
    const estA = quote.estA, estB = quote.estB;
    const approxUsd = (estA * price + estB) * (info.quoteUsd ?? 1);
    if (approxUsd > MAX_USD) throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
    if (!(estA > 0 || estB > 0)) throw new Error('nothing to deposit: both sides are zero');
    const nativeIn = info.nativeSide === 'A' ? estA : info.nativeSide === 'B' ? estB : 0;
    if (sol - nativeIn < GAS_RESERVE_SOL) {
      throw new Error(`depositing ${nativeIn.toFixed(4)} SOL would leave ${(sol - nativeIn).toFixed(4)}, below the ${GAS_RESERVE_SOL} gas reserve`);
    }

    // The binding side goes in exactly; the other side's ceiling is its cap.
    const base = quote.binding === 'A' ? 'MintA' : 'MintB';
    const baseAmount = base === 'MintA' ? toRaw(estA, info.decimalsA) : toRaw(estB, info.decimalsB);
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
    });
    const report = {
      pool, dex: DEX, pair: `${info.symbolA}/${info.symbolB}`,
      requestedLower: lower, requestedUpper: upper,
      lowerPrice: Number(band.tickLowerPrice.toFixed(6)), upperPrice: Number(band.tickUpperPrice.toFixed(6)),
      tickLower: band.tickLower, tickUpper: band.tickUpper, tickSpacing: info.tickSpacing,
      price: Number(price.toFixed(6)),
      tokenMaxA: maxA, tokenMaxB: maxB, tokenA: info.symbolA, tokenB: info.symbolB,
      depositEstA: estA, depositEstB: estB, binding: quote.binding,
      chainEstA: Number(chain.amountA.toString()) / 10 ** info.decimalsA,
      chainEstB: Number(chain.amountB.toString()) / 10 ** info.decimalsB,
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
async function buildDecrease(raydium, r, p, liquidity, minA, minB, closePosition) {
  return raydium.clmm.decreaseLiquidity({
    poolInfo: r.poolInfo, poolKeys: r.poolKeys, ownerPosition: p,
    ownerInfo: { useSOLBalance: true, closePosition },
    liquidity, amountMinA: minA, amountMinB: minB,
    txVersion: TX_VERSION,
  });
}

async function harvest(address, execute) {
  const pool = poolArg();
  return withRpc(async ({ connection, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r);
    const ps = await findPositions(raydium, pool, address);
    const tickOf = await tickStates(connection, pool, ps, info.tickSpacing);
    const builts = [];
    for (const p of ps) builts.push(await buildDecrease(raydium, r, p, new BN(0), new BN(0), new BN(0), false));
    const fees = ps.map(p => positionAmounts(p, r, tickOf));
    const sum = (k) => fees.reduce((n, f) => n.add(f[k]), new BN(0));
    const report = {
      mint: address, pool, positions: ps.length, transactions: builts.length,
      instructions: builts.reduce((n, b) => n + b.transaction.instructions.length, 0),
      feesQuote: { feeOwedA: sum('feeA').toString(), feeOwedB: sum('feeB').toString(),
        feesAccruedA: Number(sum('feeA').toString()) / 10 ** info.decimalsA,
        feesAccruedB: Number(sum('feeB').toString()) / 10 ** info.decimalsB },
    };
    if (!execute) {
      const simulation = [];
      for (const b of builts) simulation.push(await simulate(connection, b));
      console.log(JSON.stringify({ ...report, simulation, sent: false }, null, 1));
      console.log('DRY RUN — pass --execute to collect fees.');
      return;
    }
    const sigs = await sendAll(builts, report);
    console.log(JSON.stringify({ harvested: address, signature: sigs[sigs.length - 1], signatures: sigs }, null, 1));
  });
}

async function close(address, execute) {
  const pool = poolArg();
  return withRpc(async ({ connection, raydium }) => {
    const r = await loadPool(raydium, pool);
    const info = await describe(pool, r);
    const ps = await findPositions(raydium, pool, address);
    const tickOf = await tickStates(connection, pool, ps, info.tickSpacing);
    const builts = [], ests = [];
    for (const p of ps) {
      const am = positionAmounts(p, r, tickOf);
      ests.push(am);
      // All liquidity out, fees and rewards collected, NFT burnt, accounts
      // closed. The minimums are the current amounts less the slippage.
      const minA = am.amountA.muln(10000 - SLIPPAGE_BPS).divn(10000);
      const minB = am.amountB.muln(10000 - SLIPPAGE_BPS).divn(10000);
      builts.push(await buildDecrease(raydium, r, p, p.liquidity, minA, minB, true));
    }
    const sum = (k) => ests.reduce((n, e) => n.add(e[k]), new BN(0));
    const report = {
      mint: address, pool, positions: ps.length, transactions: builts.length,
      instructions: builts.reduce((n, b) => n + b.transaction.instructions.length, 0),
      quote: { liquidity: ps.reduce((n, p) => n.add(p.liquidity), new BN(0)).toString(),
        tokenEstA: sum('amountA').toString(), tokenEstB: sum('amountB').toString(),
        closeEstA: Number(sum('amountA').toString()) / 10 ** info.decimalsA,
        closeEstB: Number(sum('amountB').toString()) / 10 ** info.decimalsB },
      feesQuote: { feeOwedA: sum('feeA').toString(), feeOwedB: sum('feeB').toString() },
    };
    if (!execute) {
      const simulation = [];
      for (const b of builts) simulation.push(await simulate(connection, b));
      console.log(JSON.stringify({ ...report, simulation, sent: false }, null, 1));
      console.log('DRY RUN — close instructions built. Pass --execute to send.');
      return;
    }
    const sigs = await sendAll(builts, report);
    console.log(JSON.stringify({ closed: address, signature: sigs[sigs.length - 1], signatures: sigs }, null, 1));
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
    // Read-only: no key is loaded.
    const pool = poolArg(rest[0]);
    return withRpc(async ({ raydium }) => {
      console.log(JSON.stringify(await describe(pool, await loadPool(raydium, pool)), null, 1));
    }, { withKey: false });
  }
  console.log('commands: balance [pool] | positions | status [position] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <position> [--execute] '
    + '| close <position> [--execute]   (pool via --pool or LPBOT_POOL)');
}

// Braces in an error text would look like JSON to the loop; flatten them.
main().catch(e => {
  console.error('ERROR:', String(e?.message ?? e).replace(/[{}]/g, ' '));
  process.exitCode = 1;
});
