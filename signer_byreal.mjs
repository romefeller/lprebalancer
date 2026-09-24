// Rebalancer — the Byreal signing layer. Same commands and the same output
// fields as signer2.mjs and signer_dlmm.mjs, so the loop cannot tell which
// DEX it is on.
//
// Byreal is Bybit's Solana DEX, a Raydium CLMM fork (program
// REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2). A position is a Token-2022
// NFT whose PersonalPosition PDA holds [tickLower, tickUpper) and liquidity,
// exactly as on Raydium. Built on @byreal-io/byreal-clmm-sdk 0.2.2: its
// `Chain` client reads pool and position state, computes uncollected fees
// from the tick arrays, and builds versioned transactions for open, collect
// and close. Every transaction is simulated before it is reported; a dry run
// prints the simulation result, an --execute refuses to send one that failed.
//
// Several positions on one pool are ONE logical position: `status` reports
// the union (positionMint = the lowest tick's NFT, `positions` lists all),
// `harvest` collects them all, `close` empties and closes them all. The loop
// holds one pool at a time, so that is the same thing.
//
// The key is read from WALLET_SECRET_PATH inside this process and never
// printed. The pool comes from --pool <address> or LPBOT_POOL; a position
// names its pool on chain, so harvest/close/status <position> can find it
// without one.
//
// Commands:
//   node signer_byreal.mjs balance [pool]
//   node signer_byreal.mjs positions
//   node signer_byreal.mjs status [position]
//   node signer_byreal.mjs pool [pool]
//   node signer_byreal.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node signer_byreal.mjs harvest <position> [--execute]
//   node signer_byreal.mjs close <position> [--execute]
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
// The SDK's ESM entry loads cleanly under Node 24. BN and Decimal come from
// the SDK's own nested copies so the objects handed to it are its own classes.
const sdk = await import('@byreal-io/byreal-clmm-sdk');
const { Chain, BYREAL_CLMM_PROGRAM_ID, SqrtPriceMath, TickMath, LiquidityMath } = sdk;
const sdkRequire = createRequire(require.resolve('@byreal-io/byreal-clmm-sdk/package.json'));
const BN = sdkRequire('bn.js');
const { Connection, Keypair, PublicKey } = require('@solana/web3.js');

// The SDK prints "Compute unit limit: N" and "Transaction sent: ..." with
// console.info. The loop parses stdout, so route those lines to stderr.
console.info = (...a) => console.error(...a);

const DIR = path.dirname(new URL(import.meta.url).pathname);
const HALT = path.join(DIR, 'HALT');
const RPC = process.env.SOLANA_RPC_URL ?? process.env.LPBOT_RPC ?? 'https://api.mainnet-beta.solana.com';
const ENDPOINTS = [RPC, 'https://api.mainnet-beta.solana.com', 'https://solana-rpc.publicnode.com']
  .filter((v, i, a) => v && a.indexOf(v) === i);

const DEX = 'byreal';
const MAX_USD = Number(process.env.LPBOT_MAX_USD ?? 260);
const SLIPPAGE_BPS = Number(process.env.LPBOT_SLIPPAGE_BPS ?? 100);
const GAS_RESERVE_SOL = Number(process.env.LPBOT_GAS_RESERVE_SOL ?? 0.02);

const NATIVE_MINT = 'So11111111111111111111111111111111111111112';
const STABLES = new Set(['USDC', 'USDT', 'PYUSD', 'USDS', 'DAI', 'FDUSD', 'USDE']);
const BYREAL_API = 'https://api2.byreal.io/byreal/api/dex/v2';
const JUPITER = 'https://lite-api.jup.ag';
const HEADERS = { accept: 'application/json', 'user-agent': 'Mozilla/5.0' };
const PROGRAM_NAMES = {
  ComputeBudget111111111111111111111111111111: 'ComputeBudget',
  '11111111111111111111111111111111': 'System',
  TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA: 'Token',
  TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb: 'Token-2022',
  ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL: 'AssociatedToken',
  MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr: 'Memo',
  [BYREAL_CLMM_PROGRAM_ID.toBase58()]: 'ByrealClmm',
};

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

// --pool <addr>, else LPBOT_POOL. `required` false returns undefined instead
// of throwing, for commands that can read the pool off the position.
function poolArg(explicit, required = true) {
  const i = process.argv.indexOf('--pool');
  const p = explicit ?? (i >= 0 ? process.argv[i + 1] : undefined) ?? process.env.LPBOT_POOL;
  if (!p && required) throw new Error('no pool: pass --pool <address> or set LPBOT_POOL');
  return p;
}

// --- the pool describes itself ------------------------------------------------
const metaCache = new Map();

// Byreal's API knows the symbols and the posted fee rate; the chain does not
// know symbols. A miss falls back to the mint prefix, which is honest and
// still unique.
async function meta(pool) {
  if (!metaCache.has(pool)) {
    let m = { symbolA: null, symbolB: null, feeRatePpm: null };
    try {
      const j = await (await fetch(`${BYREAL_API}/pools/details?poolAddress=${pool}`, { headers: HEADERS })).json();
      const d = j?.result?.data;
      m = {
        symbolA: d?.mintA?.mintInfo?.symbol ?? null,
        symbolB: d?.mintB?.mintInfo?.symbol ?? null,
        feeRatePpm: d?.feeRate?.fixFeeRate != null ? Number(d.feeRate.fixFeeRate) : null,
      };
    } catch { /* fall through */ }
    metaCache.set(pool, m);
  }
  return metaCache.get(pool);
}

async function tokenUsd(mint) {
  try {
    const j = await (await fetch(`${JUPITER}/price/v3?ids=${mint}`, { headers: HEADERS })).json();
    const p = Number(j?.[mint]?.usdPrice);
    return p > 0 ? p : null;
  } catch { return null; }
}

async function quoteUsd(symB, mintB) {
  if (STABLES.has(symB)) return { usd: 1, source: 'stable' };
  const p = await tokenUsd(mintB);
  return p ? { usd: p, source: 'jupiter:mint' } : { usd: null, source: 'unknown' };
}

// price (B per A, human units) of a tick
function tickPrice(tick, dA, dB) {
  return Number(TickMath.getPriceFromTick({ tick, decimalsA: dA, decimalsB: dB }).toString());
}

async function describe(pool, chain) {
  const poolInfo = await chain.getRawPoolInfoByPoolId(pool);
  if (!poolInfo.programId.equals(BYREAL_CLMM_PROGRAM_ID)) {
    throw new Error(`${pool} is owned by ${poolInfo.programId.toBase58()}, not the Byreal CLMM program`);
  }
  const dA = poolInfo.mintDecimalsA, dB = poolInfo.mintDecimalsB;
  const mintA = poolInfo.mintA.toBase58(), mintB = poolInfo.mintB.toBase58();
  const m = await meta(pool);
  let config = null;
  try {
    config = await sdk.RawDataUtils.getRawAmmConfigByConfigId({ connection: chain.connection, configId: poolInfo.ammConfig });
  } catch { /* the posted rate is enough */ }
  const symbolA = m.symbolA ?? mintA.slice(0, 6), symbolB = m.symbolB ?? mintB.slice(0, 6);
  const q = await quoteUsd(symbolB, mintB);
  const info = {
    pool, dex: DEX, programId: poolInfo.programId.toBase58(),
    tickSpacing: poolInfo.tickSpacing, tickCurrent: poolInfo.tickCurrent,
    price: poolInfo.currentPrice,
    symbolA, symbolB, decimalsA: dA, decimalsB: dB, mintA, mintB,
    liquidity: poolInfo.liquidity.toString(),
    // Byreal posts a fixed rate per pool (ppm); the AMM config carries the
    // base trade fee; decayFeeFlag != 0 means the fee is dynamic.
    feeRatePpm: m.feeRatePpm ?? config?.tradeFeeRate ?? null,
    configFeeRatePpm: config?.tradeFeeRate ?? null,
    dynamicFee: poolInfo.decayFeeFlag !== 0,
    status: poolInfo.status,
    quoteUsd: q.usd, quoteUsdSource: q.source,
    nativeSide: mintA === NATIVE_MINT ? 'A' : mintB === NATIVE_MINT ? 'B' : null,
  };
  return { info, poolInfo };
}

async function connect(url) {
  guard();
  const connection = new Connection(url, 'confirmed');
  const payer = Keypair.fromSecretKey(await secretBytes());
  const chain = new Chain({ connection, programId: BYREAL_CLMM_PROGRAM_ID });
  return { connection, payer, chain };
}

// Retry the whole operation across endpoints on a rate limit or a transport
// failure, as signer2 does. A logic error ("position not found", "exceeds
// cap") is the same on every endpoint, so it is thrown as it is, at once.
const TRANSIENT = /429|Too Many Requests|rate|403|blocked|Indexed requests|fetch failed|ECONN|ETIMEDOUT|timed? ?out|socket hang up|502|503|504/i;

async function withRpc(fn) {
  let lastErr = null;
  for (const url of ENDPOINTS) {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        return await fn(await connect(url));
      } catch (e) {
        const msg = String(e?.message ?? e);
        if (!TRANSIENT.test(msg)) throw e;
        lastErr = e;
        if (!/429|Too Many Requests|rate/i.test(msg)) break;   // blocked here: next endpoint
        await new Promise(r => setTimeout(r, 2500 * (attempt + 1)));
      }
    }
  }
  throw new Error(`all RPC endpoints failed: ${String(lastErr?.message ?? lastErr).slice(0, 160)}`);
}

// Native SOL counts as its token: the SDK funds a fresh WSOL account from
// native SOL on every open and closes it after, so an idle WSOL ATA is not
// what gets deposited.
async function splBalance(connection, owner, mint, decimals, lamports) {
  if (mint === NATIVE_MINT) return lamports / 1e9;
  const r = await connection.getParsedTokenAccountsByOwner(owner, { mint: new PublicKey(mint) });
  let raw = 0n;
  for (const a of r.value ?? []) raw += BigInt(a.account.data.parsed.info.tokenAmount.amount ?? 0);
  return Number(raw) / 10 ** decimals;
}

async function balance(poolExplicit) {
  const pool = poolArg(poolExplicit);
  return withRpc(async ({ connection, payer, chain }) => {
    const { info } = await describe(pool, chain);
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

// --- positions -------------------------------------------------------------------
// Every Byreal position the wallet holds: the SDK walks the wallet's Token and
// Token-2022 accounts with amount 1 and keeps those whose PersonalPosition PDA
// exists under the Byreal program. Positions on other DEXes fall out because
// their PDAs do not exist under this program.
async function ownerPositions(chain, owner, pool) {
  const all = await chain.getRawPositionInfoListByUserAddress(owner);
  const list = pool ? all.filter(p => p.poolId.toBase58() === pool) : all;
  return list.sort((a, b) => a.tickLower - b.tickLower);
}

// On-chain truth for one position: band, liquidity, what a close returns now,
// and the fees accrued but not yet collected (from the tick arrays).
async function positionView(chain, raw, info) {
  const d = await chain.getPositionInfoByNftMint(raw.nftMint);
  if (!d) throw new Error(`position ${raw.nftMint.toBase58()} vanished between reads`);
  const ua = (x) => Number(x.toString()) / 10 ** info.decimalsA;
  const ub = (x) => Number(x.toString()) / 10 ** info.decimalsB;
  const estA = ua(d.tokenA.amount), estB = ub(d.tokenB.amount);
  const feeA = ua(d.tokenA.feeAmount), feeB = ub(d.tokenB.feeAmount);
  const out = {
    positionMint: raw.nftMint.toBase58(), whirlpool: info.pool, pool: info.pool, dex: DEX,
    pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
    decimalsA: info.decimalsA, decimalsB: info.decimalsB,
    quoteUsd: info.quoteUsd, quoteUsdSource: info.quoteUsdSource,
    tickLower: raw.tickLower, tickUpper: raw.tickUpper, tickCurrent: info.tickCurrent,
    liquidity: raw.liquidity.toString(),
    lowerPrice: Number(tickPrice(raw.tickLower, info.decimalsA, info.decimalsB).toFixed(6)),
    upperPrice: Number(tickPrice(raw.tickUpper, info.decimalsA, info.decimalsB).toFixed(6)),
    price: Number(info.price.toFixed(6)),
    inRange: info.tickCurrent >= raw.tickLower && info.tickCurrent < raw.tickUpper,
    closeEstA: estA, closeEstB: estB,
    feesAccruedA: feeA, feesAccruedB: feeB,
    feesAccrued_quote: Number((feeA * info.price + feeB).toFixed(9)),
    rawFeesA: d.tokenA.feeAmount.toString(), rawFeesB: d.tokenB.feeAmount.toString(),
  };
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((estA * info.price + estB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number(((feeA * info.price + feeB) * info.quoteUsd).toFixed(6));
  }
  return out;
}

// The union of every position the wallet holds on the pool, as one.
function unionView(views, info) {
  views = [...views].sort((a, b) => a.tickLower - b.tickLower);
  const sum = (k) => views.reduce((n, v) => n + (v[k] ?? 0), 0);
  const L = views.reduce((n, v) => n.add(new BN(v.liquidity)), new BN(0));
  const tickLower = Math.min(...views.map(v => v.tickLower));
  const tickUpper = Math.max(...views.map(v => v.tickUpper));
  const out = {
    ...views[0],
    positionMint: views[0].positionMint,
    positions: views.map(v => v.positionMint),
    tickLower, tickUpper, liquidity: L.toString(),
    lowerPrice: Number(tickPrice(tickLower, info.decimalsA, info.decimalsB).toFixed(6)),
    upperPrice: Number(tickPrice(tickUpper, info.decimalsA, info.decimalsB).toFixed(6)),
    inRange: info.tickCurrent >= tickLower && info.tickCurrent < tickUpper,
    closeEstA: sum('closeEstA'), closeEstB: sum('closeEstB'),
    feesAccruedA: sum('feesAccruedA'), feesAccruedB: sum('feesAccruedB'),
  };
  delete out.rawFeesA; delete out.rawFeesB;
  out.feesAccrued_quote = Number((out.feesAccruedA * info.price + out.feesAccruedB).toFixed(9));
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((out.closeEstA * info.price + out.closeEstB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number((out.feesAccrued_quote * info.quoteUsd).toFixed(6));
  }
  return out;
}

// The pool for a command that names a position: --pool/LPBOT_POOL when set,
// else the pool the position itself names on chain.
async function poolFor(chain, positionArg) {
  const p = poolArg(undefined, false);
  if (p) return p;
  if (!positionArg) throw new Error('no pool: pass --pool <address> or set LPBOT_POOL');
  const raw = await chain.getRawPositionInfoByNftMint(new PublicKey(positionArg));
  if (!raw) throw new Error(`position ${positionArg} not found under the Byreal program`);
  return raw.poolId.toBase58();
}

async function status(positionArg) {
  return withRpc(async ({ connection, payer, chain }) => {
    const pool = await poolFor(chain, positionArg);
    const list = await ownerPositions(chain, payer.publicKey, pool);
    if (!list.length
        || (positionArg && !list.some(p => p.nftMint.toBase58() === positionArg))) {
      console.log(JSON.stringify({ positions: 0, positionMint: null, pool }, null, 1));
      return null;
    }
    const { info } = await describe(pool, chain);
    const views = [];
    for (const raw of list) views.push(await positionView(chain, raw, info));
    const out = unionView(views, info);
    try {
      out.rentSol = Number((await rentOf(connection, payer.publicKey, list.map(x => x.nftMint), BYREAL_CLMM_PROGRAM_ID)).toFixed(9));
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
const TOKEN_2022_PROGRAM_ID_RENT = new PublicKey('TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnWqfbrbyy3X22');

async function rentUsdOf(info, rentSol) {
  const su = info.nativeSide === 'A' && info.quoteUsd != null ? info.price * info.quoteUsd
    : info.nativeSide === 'B' && info.quoteUsd != null ? info.quoteUsd
    : await tokenUsd(NATIVE_MINT);
  return su != null ? Number((rentSol * su).toFixed(4)) : null;
}

async function positions() {
  const pool = poolArg(undefined, false);
  return withRpc(async ({ payer, chain }) => {
    const list = await ownerPositions(chain, payer.publicKey, pool);
    console.log(JSON.stringify(list.map(p => ({
      address: p.nftMint.toBase58(), pool: p.poolId.toBase58(),
      tickLower: p.tickLower, tickUpper: p.tickUpper, liquidity: p.liquidity.toString(),
      feesOwedA: p.tokenFeesOwedA.toString(), feesOwedB: p.tokenFeesOwedB.toString(),
    })), null, 1));
  });
}

// --- transactions ------------------------------------------------------------------
function programsOf(tx) {
  const keys = tx.message.staticAccountKeys;
  return tx.message.compiledInstructions.map(ix => {
    const id = keys[ix.programIdIndex].toBase58();
    return PROGRAM_NAMES[id] ?? id;
  });
}

// Simulate the built transaction as the payer would send it and report what
// the chain says: err (null when it passes), units, the failing instruction
// by index and program, and the last log lines.
async function simulate(connection, tx) {
  const r = await connection.simulateTransaction(tx, { sigVerify: false, replaceRecentBlockhash: true });
  const v = r.value;
  const programs = programsOf(tx);
  const out = { ok: v.err == null, err: v.err ?? null, unitsConsumed: v.unitsConsumed ?? null,
    instructions: programs.length, programs };
  const ie = v.err?.InstructionError;
  if (Array.isArray(ie)) {
    out.failedIndex = ie[0];
    out.failedProgram = programs[ie[0]] ?? null;
    out.passedBefore = programs.slice(0, ie[0]);
  }
  out.logs = (v.logs ?? []).slice(-8);
  return out;
}

// Sends versioned transactions in order, signing each with the payer (the
// SDK already signed with the NFT keypair where one exists). Never throws:
// a failure comes back as {sigs, error} so the caller reports what landed
// and withRpc never retries a send on another endpoint. A preflight failure
// is the same everywhere, and a transport timeout on the send may or may not
// have delivered the transaction; either way the loop re-reads the chain.
async function sendAll(connection, txs, payer) {
  const sigs = [];
  for (const tx of Array.isArray(txs) ? txs : [txs]) {
    try {
      tx.sign([payer]);
      const sig = await connection.sendRawTransaction(tx.serialize(),
        { skipPreflight: false, preflightCommitment: 'confirmed', maxRetries: 3 });
      sigs.push(sig);
      await confirm(connection, sig);
    } catch (e) {
      return { sigs, error: String(e?.message ?? e).slice(0, 300) };
    }
  }
  return { sigs, error: null };
}

async function confirm(connection, sig) {
  let res;
  try {
    res = await connection.confirmTransaction(sig, 'confirmed');
  } catch (e) {
    // timed out waiting: ask once more before declaring the outcome unknown
    const st = await connection.getSignatureStatuses([sig]);
    const s = st.value[0];
    if (!s || !s.confirmationStatus) throw new Error(`${sig} not confirmed: ${String(e?.message ?? e).slice(0, 120)}`);
    res = { value: s };
  }
  if (res.value.err) throw new Error(`${sig} failed on chain: ${JSON.stringify(res.value.err)}`);
}

// sent: whether anything reached the chain; partial: something did and then
// a later step failed. Any error exits 1.
function reportSent(base, r) {
  const out = { ...base, signature: r.sigs[r.sigs.length - 1] ?? null, signatures: r.sigs, sent: r.sigs.length > 0 };
  if (r.error) { out.error = r.error; process.exitCode = 1; if (r.sigs.length) out.partial = true; }
  console.log(JSON.stringify(out, null, 1));
}

// --- open ----------------------------------------------------------------------------
// The liquidity each cap alone would fund at the current price, and the
// smaller of the two; the amounts that liquidity needs. (signer2.mjs)
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

// Lower rounds down and upper rounds up to a multiple of tickSpacing, so the
// band on chain contains the band asked for.
function alignTicks(lower, upper, spacing, dA, dB) {
  const exact = (p) => Math.log(p * 10 ** (dB - dA)) / Math.log(1.0001);
  let tickLower = Math.floor(exact(lower) / spacing) * spacing;
  let tickUpper = Math.ceil(exact(upper) / spacing) * spacing;
  if (tickUpper <= tickLower) tickUpper = tickLower + spacing;
  if (tickLower < sdk.MIN_TICK || tickUpper > sdk.MAX_TICK) throw new Error(`ticks ${tickLower}..${tickUpper} out of range`);
  return { tickLower, tickUpper };
}

const toRaw = (human, decimals) => new BN(BigInt(Math.floor(human * 10 ** decimals)).toString());

async function open(pool, lower, upper, maxA, maxB, execute) {
  return withRpc(async ({ connection, payer, chain }) => {
    const { info, poolInfo } = await describe(pool, chain);
    const lamports = await connection.getBalance(payer.publicKey);
    if (lamports / 1e9 < GAS_RESERVE_SOL) {
      throw new Error(`SOL below the ${GAS_RESERVE_SOL} gas reserve; a wallet that cannot pay fees cannot close its own position`);
    }
    const dA = info.decimalsA, dB = info.decimalsB;
    const { tickLower, tickUpper } = alignTicks(Number(lower), Number(upper), info.tickSpacing, dA, dB);
    const pLo = tickPrice(tickLower, dA, dB), pHi = tickPrice(tickUpper, dA, dB);
    if (info.tickCurrent < tickLower || info.tickCurrent >= tickUpper) {
      throw new Error(`price ${info.price} (tick ${info.tickCurrent}) is outside ${pLo}-${pHi} (ticks ${tickLower}..${tickUpper}); the price moved`);
    }
    const price = info.price;
    const quote = depositQuote(price, pLo, pHi, Number(maxA), Number(maxB));
    if (!quote) throw new Error('nothing to deposit: the caps fund no liquidity in this band');
    const approxUsd = (quote.estA * price + quote.estB) * (info.quoteUsd ?? 1);
    if (approxUsd > MAX_USD) throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
    if (!(quote.estA > 0 && quote.estB > 0)) throw new Error('nothing to deposit: one side is zero');

    // The binding side goes in exactly; the other side is what the SDK's own
    // math says that base needs, with slippage, never above its cap.
    const base = quote.binding === 'A' ? 'MintA' : 'MintB';
    const baseAmount = toRaw(quote.binding === 'A' ? quote.estA : quote.estB, quote.binding === 'A' ? dA : dB);
    const sqLo = SqrtPriceMath.getSqrtPriceX64FromTick(tickLower);
    const sqHi = SqrtPriceMath.getSqrtPriceX64FromTick(tickUpper);
    const sdkOther = quote.binding === 'A'
      ? LiquidityMath.getAmountBFromAmountA(sqLo, sqHi, poolInfo.sqrtPriceX64, baseAmount)
      : LiquidityMath.getAmountAFromAmountB(sqLo, sqHi, poolInfo.sqrtPriceX64, baseAmount);
    const otherDec = quote.binding === 'A' ? dB : dA;
    const otherEst = toRaw(quote.binding === 'A' ? quote.estB : quote.estA, otherDec);
    const otherCap = toRaw(quote.binding === 'A' ? Number(maxB) : Number(maxA), otherDec);
    const otherNeed = BN.max(sdkOther, otherEst);
    let otherAmountMax = otherNeed.muln(10000 + SLIPPAGE_BPS).divn(10000);
    if (otherAmountMax.gt(otherCap)) otherAmountMax = otherCap;
    if (otherAmountMax.lt(otherNeed)) {
      throw new Error(`the ${quote.binding === 'A' ? 'B' : 'A'} cap is below what the ${quote.binding} side needs (${otherNeed.toString()} raw); the caps disagree with the price`);
    }

    const built = await chain.createPositionInstructions({
      userAddress: payer.publicKey, poolInfo, tickLower, tickUpper, base, baseAmount, otherAmountMax,
    });
    const sim = await simulate(connection, built.transaction);
    const depositEstA = quote.estA, depositEstB = quote.estB;
    const report = {
      pool, dex: DEX, pair: `${info.symbolA}/${info.symbolB}`,
      requestedLower: Number(lower), requestedUpper: Number(upper),
      lowerPrice: pLo, upperPrice: pHi, tickLower, tickUpper, tickSpacing: info.tickSpacing,
      price, tickCurrent: info.tickCurrent,
      tokenMaxA: Number(maxA), tokenMaxB: Number(maxB), tokenA: info.symbolA, tokenB: info.symbolB,
      depositEstA, depositEstB,
      approxUsd: Number(approxUsd.toFixed(2)),
      depositUsd: info.quoteUsd != null ? Number(((depositEstA * price + depositEstB) * info.quoteUsd).toFixed(4)) : null,
      quote: { liquidity: quote.liquidity, binding: quote.binding },
      base, baseAmount: baseAmount.toString(), otherAmountMax: otherAmountMax.toString(),
      sdkOtherAmount: sdkOther.toString(),
      positionMint: built.nftAddress ?? null,
      instructions: built.transaction.message.compiledInstructions.length,
      simulation: sim,
    };
    if (!execute) {
      console.log(JSON.stringify({ ...report, sent: false }, null, 1));
      console.log('DRY RUN — instructions built and simulated. Pass --execute to sign and send.');
      return;
    }
    if (!sim.ok) throw new Error(`simulation failed at ${sim.failedProgram ?? '?'}[${sim.failedIndex ?? '?'}]: ${JSON.stringify(sim.err)}; nothing sent`);
    // One transaction. Once sent this operation is never retried whole (a
    // retried open is a second position): a confirmation failure is reported
    // as partial with the signature, and `status` shows what exists on chain.
    const r = await sendAll(connection, built.transaction, payer);
    reportSent(report, r);
  });
}

// All of the wallet's positions on the pool, provided the one named is among
// them: the name is a check that the caller and the chain agree on which pool.
async function findPositions(chain, owner, pool, address) {
  const list = await ownerPositions(chain, owner, pool);
  if (!list.some(p => p.nftMint.toBase58() === address)) {
    throw new Error(`position ${address} not found for this wallet on pool ${pool}`);
  }
  return list;
}

function parseKey(s, what) {
  try { return new PublicKey(s); } catch { throw new Error(`${what} ${s} is not a valid address`); }
}

async function harvest(address, execute) {
  if (!address) throw new Error('harvest needs a position');
  parseKey(address, 'position');
  return withRpc(async ({ connection, payer, chain }) => {
    const pool = await poolFor(chain, address);
    const list = await findPositions(chain, payer.publicKey, pool, address);
    const { info } = await describe(pool, chain);
    const views = [];
    for (const raw of list) views.push(await positionView(chain, raw, info));
    // Collect = decrease 0 liquidity. Skip a position with nothing owed.
    const due = views.filter(v => v.rawFeesA !== '0' || v.rawFeesB !== '0');
    const txs = [], sims = [];
    for (const v of due) {
      const built = await chain.collectFeesInstructions({ userAddress: payer.publicKey, nftMint: new PublicKey(v.positionMint) });
      txs.push(built.transaction);
      sims.push({ position: v.positionMint, ...(await simulate(connection, built.transaction)) });
    }
    const feeA = views.reduce((n, v) => n + v.feesAccruedA, 0), feeB = views.reduce((n, v) => n + v.feesAccruedB, 0);
    const report = { mint: address, pool, dex: DEX, positions: list.length, transactions: txs.length,
      feesQuote: { feeOwedA: feeA, feeOwedB: feeB }, simulation: sims };
    if (!execute) {
      console.log(JSON.stringify({ ...report, sent: false }, null, 1));
      console.log('DRY RUN — pass --execute to collect fees.');
      return;
    }
    if (!txs.length) { console.log(JSON.stringify({ harvested: address, signature: null, note: 'nothing to claim' }, null, 1)); return; }
    const bad = sims.find(s => !s.ok);
    if (bad) throw new Error(`simulation failed for ${bad.position} at ${bad.failedProgram ?? '?'}: ${JSON.stringify(bad.err)}; nothing sent`);
    const r = await sendAll(connection, txs, payer);
    reportSent({ harvested: address }, r);
  });
}

async function close(address, execute) {
  if (!address) throw new Error('close needs a position');
  parseKey(address, 'position');
  return withRpc(async ({ connection, payer, chain }) => {
    const pool = await poolFor(chain, address);
    const list = await findPositions(chain, payer.publicKey, pool, address);
    const { info } = await describe(pool, chain);
    const views = [];
    for (const raw of list) views.push(await positionView(chain, raw, info));
    // All liquidity out (with fees, in the same instruction) and the NFT
    // account closed: one transaction per position.
    const txs = [], sims = [];
    for (const v of views) {
      const built = await chain.decreaseFullLiquidityInstructions({
        userAddress: payer.publicKey, nftMint: new PublicKey(v.positionMint),
        closePosition: true, slippage: SLIPPAGE_BPS / 10000,
      });
      txs.push(built.transaction);
      sims.push({ position: v.positionMint, ...(await simulate(connection, built.transaction)) });
    }
    const sum = (k) => views.reduce((n, v) => n + v[k], 0);
    const report = { mint: address, pool, dex: DEX, positions: list.length, transactions: txs.length,
      instructions: txs.reduce((n, t) => n + t.message.compiledInstructions.length, 0),
      quote: { tokenEstA: sum('closeEstA'), tokenEstB: sum('closeEstB') },
      feesQuote: { feeOwedA: sum('feesAccruedA'), feeOwedB: sum('feesAccruedB') },
      simulation: sims };
    if (!execute) {
      console.log(JSON.stringify({ ...report, sent: false }, null, 1));
      console.log('DRY RUN — close instructions built and simulated. Pass --execute to send.');
      return;
    }
    const bad = sims.find(s => !s.ok);
    if (bad) throw new Error(`simulation failed for ${bad.position} at ${bad.failedProgram ?? '?'}: ${JSON.stringify(bad.err)}; nothing sent`);
    const r = await sendAll(connection, txs, payer);
    reportSent({ closed: address }, r);
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
  if (cmd === 'open') {
    if (rest.length < 5) throw new Error('open needs <pool> <lowerPrice> <upperPrice> <maxA> <maxB>');
    return open(rest[0], rest[1], rest[2], rest[3], rest[4], execute);
  }
  if (cmd === 'pool') {
    guard();
    const pool = poolArg(rest[0]);
    const connection = new Connection(ENDPOINTS[0], 'confirmed');
    const chain = new Chain({ connection, programId: BYREAL_CLMM_PROGRAM_ID });
    const { info } = await describe(pool, chain);
    return console.log(JSON.stringify(info, null, 1));
  }
  console.log('commands: balance [pool] | positions | status [position] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <position> [--execute] '
    + '| close <position> [--execute]   (pool via --pool or LPBOT_POOL)');
}

main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
