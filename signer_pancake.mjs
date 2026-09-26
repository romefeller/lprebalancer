// Rebalancer — the PancakeSwap V3 (Solana) signing layer. Same commands and
// the same output fields as signer2.mjs and signer_dlmm.mjs, so the loop
// cannot tell which DEX it is on.
//
// PancakeSwap's Solana CLMM (program HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq)
// is a fork of Raydium's amm_v3. Its on-chain Anchor IDL (account
// CAD7TZySgk4RkqyAkGu3bJFvf8itWsfTtpViRK8TJrKf, program name "amm_v3") lists
// the same instruction names, discriminators (sha256("global:<name>")[0..8]),
// argument layouts and account orders as Raydium's for open_position_with_
// token22_nft, increase_liquidity_v2, decrease_liquidity_v2 and close_position;
// live transactions on pool DJNt... confirmed the same (20-account open, 59-byte
// data; 16+3-account decrease, 40-byte data). The account layouts (PoolState
// 1544 bytes, PersonalPositionState 281, TickArrayState 10240, AmmConfig 117,
// TickArrayBitmapExtension 1832) match too. There is no PancakeSwap SDK, so
// this file uses @raydium-io/raydium-sdk-v2 as an INSTRUCTION LIBRARY pointed
// at the PancakeSwap program id: layouts, PDA helpers, tick/liquidity math and
// the low-level ClmmInstrument builders. Nothing here calls Raydium's API.
//
// Two places where SDK 0.2.73 (written for Raydium's CURRENT program) differs
// from the fork, both found by simulation and confirmed against live accounts:
//   1. protocol_position PDA seeds use big-endian tick bytes on the fork
//      (the SDK's getPdaProtocolPositionAddress is little-endian);
//   2. protocol_position must be writable (the SDK marks it read-only).
// So the CLMM instructions are built with the low-level per-instruction
// builders and PDAs derived here, not the SDK's high-level *Instructions().
//
// Position NFTs are Token-2022 mints (the fork's front end uses
// open_position_with_token22_nft without metadata). The personal position PDA
// is ["position", nftMint]. Several positions on one pool are reported as ONE
// logical position (union view), as signer_dlmm.mjs does; `harvest` and
// `close` act on all of them.
//
// Fees: decrease_liquidity_v2 with liquidity 0 collects fees and rewards
// (Raydium's harvest). The pool's initialized rewards MUST be passed as
// remaining accounts (vault, owner ATA, mint) or the program rejects the call.
//
// The key is read from WALLET_SECRET_PATH inside this process and never
// printed. The pool comes from --pool <address> or LPBOT_POOL.
//
// Commands:
//   node signer_pancake.mjs balance [pool]
//   node signer_pancake.mjs positions
//   node signer_pancake.mjs status [position]
//   node signer_pancake.mjs pool [pool]
//   node signer_pancake.mjs open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]
//   node signer_pancake.mjs harvest <position> [--execute]
//   node signer_pancake.mjs close <position> [--execute]
//
// Without --execute the real instructions are built AND simulated with
// connection.simulateTransaction; the report carries the simulation logs.
// Extra env: LPBOT_PRIORITY_MICROLAMPORTS (compute unit price, default 20000).
import fs from 'node:fs';
import { positionRent } from './position_rent.mjs';
import { SLIPPAGE_REFUSAL } from './slippage.mjs';
import path from 'node:path';
import { createRequire } from 'node:module';

// The project is "type": "commonjs"; the SDK's CommonJS build resolves cleanly.
const require = createRequire(import.meta.url);
const sdk = require('@raydium-io/raydium-sdk-v2');
const {
  ClmmInstrument, PoolInfoLayout, PersonalPositionLayout, ClmmConfigLayout, TickArrayLayout,
  TickUtil, TickArrayUtil, LiquidityMathUtil, PositionUtils, PoolUtil,
  getPdaPersonalPositionAddress, getPdaTickArrayAddress, getPdaExBitmapAccount,
} = sdk;
const {
  Connection, Keypair, PublicKey, SystemProgram, TransactionMessage, VersionedTransaction,
  ComputeBudgetProgram,
} = require('@solana/web3.js');
const {
  TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, getAssociatedTokenAddressSync,
  createAssociatedTokenAccountIdempotentInstruction, createSyncNativeInstruction,
  createCloseAccountInstruction,
} = require('@solana/spl-token');
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
const PRIORITY_MICROLAMPORTS = Number(process.env.LPBOT_PRIORITY_MICROLAMPORTS ?? 20000);
const CU_LIMIT = 600_000;

const PROGRAM = new PublicKey('HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq');
const DEX = 'pancakeswap-v3-solana';
const NATIVE_MINT = 'So11111111111111111111111111111111111111112';
// Stablecoins by MINT (USDC, USDT, PYUSD, USDS). A symbol comes from API or token metadata an
// attacker controls: a fake "USDC" priced at $1 would defeat every dollar cap (review 2026-09-26).
const STABLE_MINTS = new Set(['EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB', '2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo', 'USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA']);
const GECKO = 'https://api.geckoterminal.com/api/v2/networks/solana/pools';
const JUPITER = 'https://lite-api.jup.ag';
const HEADERS = { accept: 'application/json', 'user-agent': 'Mozilla/5.0' };
const GECKO_HEADERS = { accept: 'application/json;version=20230203', 'user-agent': 'Mozilla/5.0' };

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

// An error thrown after a transaction was sent. withRpc must not retry it.
class SentError extends Error {
  constructor(message, signatures) { super(message); this.sent = true; this.signatures = signatures; }
}

// The fork derives the protocol position with BIG-endian tick bytes
// (["position", pool, i32be(lower), i32be(upper)]), as its IDL and live
// accounts show; SDK 0.2.73's getPdaProtocolPositionAddress uses little-endian
// and the program rejects it (ConstraintSeeds, seen in simulation). So the
// CLMM instructions are built through the low-level ClmmInstrument methods
// with PDAs derived here.
function i32be(n) { const b = Buffer.alloc(4); b.writeInt32BE(n); return b; }
// The fork still creates/updates the protocol position (init_if_needed), so the
// account must be writable; SDK 0.2.73 marks it read-only for Raydium's newer
// program, and the CPI then fails with PrivilegeEscalation (seen in simulation).
function protocolPositionWritable(ix, protocolPosition) {
  for (const k of ix.keys) if (k.pubkey.equals(protocolPosition)) k.isWritable = true;
  return ix;
}
function protocolPositionPda(poolId, tickLower, tickUpper) {
  return PublicKey.findProgramAddressSync([Buffer.from('position'), poolId.toBuffer(), i32be(tickLower), i32be(tickUpper)], PROGRAM)[0];
}
// Tick arrays and, when a tick array lies outside the pool's built-in bitmap,
// the bitmap extension account.
function tickArrayKeys(p, tickLower, tickUpper) {
  const ts = p.state.tickSpacing;
  const lowerStart = TickArrayUtil.getTickArrayStartIndex(tickLower, ts);
  const upperStart = TickArrayUtil.getTickArrayStartIndex(tickUpper, ts);
  return {
    lowerStart, upperStart,
    tickArrayLower: getPdaTickArrayAddress(PROGRAM, p.id, lowerStart).publicKey,
    tickArrayUpper: getPdaTickArrayAddress(PROGRAM, p.id, upperStart).publicKey,
    exBitmap: PoolUtil.isOverflowDefaultTickarrayBitmap({ tickSpacing: ts, tickIndexs: [lowerStart, upperStart] })
      ? getPdaExBitmapAccount(PROGRAM, p.id).publicKey : undefined,
  };
}

// --- chain state -------------------------------------------------------------
// The pool, its config, the token programs of every mint it touches, and the
// initialized rewards. All decoded with the Raydium layouts; the owner and the
// account size are checked so a wrong address fails loudly.
async function loadPool(connection, pool) {
  const id = new PublicKey(pool);
  const acc = await connection.getAccountInfo(id);
  if (!acc) throw new Error(`pool ${pool} does not exist`);
  if (!acc.owner.equals(PROGRAM)) throw new Error(`pool ${pool} is owned by ${acc.owner.toBase58()}, not the PancakeSwap CLMM program`);
  if (acc.data.length !== PoolInfoLayout.span) throw new Error(`pool ${pool} is ${acc.data.length} bytes, expected ${PoolInfoLayout.span}`);
  const state = PoolInfoLayout.decode(acc.data);
  const cfgAcc = await connection.getAccountInfo(state.configId);
  if (!cfgAcc || cfgAcc.data.length < ClmmConfigLayout.span) throw new Error(`amm config ${state.configId.toBase58()} unreadable`);
  const config = ClmmConfigLayout.decode(cfgAcc.data);
  const rewards = state.rewardInfos.filter(r => r.state !== 0 && !r.mint.equals(PublicKey.default));
  const mints = [state.mintA, state.mintB, ...rewards.map(r => r.mint)];
  const mintAccs = await connection.getMultipleAccountsInfo(mints);
  const programOf = new Map();
  mints.forEach((m, i) => {
    if (!mintAccs[i]) throw new Error(`mint ${m.toBase58()} unreadable`);
    programOf.set(m.toBase58(), mintAccs[i].owner);
  });
  return { id, state, config, rewards, programOf };
}

// --- the pool describes itself ------------------------------------------------
const symbolCache = new Map();

// GeckoTerminal knows the symbols; the chain does not. Symbols are mapped to
// mints through relationships.base_token / quote_token ("solana_<mint>"),
// never by position, because Gecko's order is not the pool's order. A miss
// falls back to the mint prefix, which is honest and still unique.
async function symbols(pool, mintA, mintB) {
  if (!symbolCache.has(pool)) {
    const byMint = new Map();
    try {
      const j = await (await fetch(`${GECKO}/${pool}`, { headers: GECKO_HEADERS })).json();
      const d = j?.data;
      const [baseSym, quoteSym] = String(d?.attributes?.name ?? '').split(' / ').map(s => s.trim());
      const baseMint = String(d?.relationships?.base_token?.data?.id ?? '').replace(/^solana_/, '');
      const quoteMint = String(d?.relationships?.quote_token?.data?.id ?? '').replace(/^solana_/, '');
      if (baseSym && baseMint) byMint.set(baseMint, baseSym);
      if (quoteSym && quoteMint) byMint.set(quoteMint, quoteSym);
    } catch { /* fall through */ }
    symbolCache.set(pool, {
      a: byMint.get(mintA) ?? mintA.slice(0, 6),
      b: byMint.get(mintB) ?? mintB.slice(0, 6),
    });
  }
  return symbolCache.get(pool);
}

async function tokenUsd(mint) {
  try {
    const j = await (await fetch(`${JUPITER}/price/v3?ids=${mint}`, { headers: HEADERS })).json();
    const p = Number(j?.[mint]?.usdPrice);
    return p > 0 ? p : null;
  } catch { return null; }
}

async function quoteUsd(symB, mintB) {
  if (STABLE_MINTS.has(String(mintB))) return { usd: 1, source: 'stable' };
  const p = await tokenUsd(mintB);
  return p ? { usd: p, source: 'jupiter:mint' } : { usd: null, source: 'unknown' };
}

// B per A in human units at a tick (the lower edge of the tick).
function tickPrice(tick, dA, dB) {
  return Number(TickUtil.tickToPrice(tick, dA, dB).toString());
}

async function describe(pool, p) {
  const s = p.state;
  const mintA = s.mintA.toBase58(), mintB = s.mintB.toBase58();
  const dA = s.mintDecimalsA, dB = s.mintDecimalsB;
  const sym = await symbols(pool, mintA, mintB);
  const q = await quoteUsd(sym.b, mintB);
  return {
    pool, dex: DEX, program: PROGRAM.toBase58(),
    tickSpacing: s.tickSpacing, tickCurrent: s.tickCurrent,
    feeRate: p.config.tradeFeeRate / 1e6,          // 100 = 0.01%
    liquidity: s.liquidity.toString(),
    price: Number(TickUtil.sqrtPriceX64ToPrice(s.sqrtPriceX64, dA, dB).toString()),
    symbolA: sym.a, symbolB: sym.b, decimalsA: dA, decimalsB: dB, mintA, mintB,
    vaultA: s.vaultA.toBase58(), vaultB: s.vaultB.toBase58(),
    rewards: p.rewards.map(r => r.mint.toBase58()),
    quoteUsd: q.usd, quoteUsdSource: q.source,
    nativeSide: mintA === NATIVE_MINT ? 'A' : mintB === NATIVE_MINT ? 'B' : null,
  };
}

async function connect(url, withKey = true) {
  guard();
  const connection = new Connection(url, 'confirmed');
  const payer = withKey ? Keypair.fromSecretKey(await secretBytes()) : null;
  return { connection, payer };
}

// Retry the whole operation across endpoints on a rate limit or an endpoint
// fault, as the other signers do. Never after a send. `pool` reads without the
// key.
const RETRYABLE = /429|Too Many Requests|rate limit|fetch failed|ECONN|ETIMEDOUT|EAI_AGAIN|socket|timed? ?out|50[0-4]\b|Service Unavailable|Gateway|Forbidden|Indexed requests|Blockhash not found|failed to get/i;
async function withRpc(fn, withKey = true) {
  const errs = [];
  for (const url of ENDPOINTS) {
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        return await fn(await connect(url, withKey));
      } catch (e) {
        if (e?.sent) throw e;
        // A deterministic error (bad position, price outside the band, a
        // simulation failure) is the answer; only endpoint trouble moves on.
        if (!RETRYABLE.test(String(e?.message ?? e))) throw e;
        errs.push(String(e?.message ?? e).replace(/\s+/g, ' ').slice(0, 140));
        if (!/429|Too Many Requests|rate/i.test(String(e?.message ?? e))) break;
        await new Promise(r => setTimeout(r, 3000 * (attempt + 1)));
      }
    }
  }
  const uniq = errs.filter((v, i, a) => a.indexOf(v) === i);
  throw new Error(`all RPC endpoints failed: ${uniq.join(' || ')}`);
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
  return withRpc(async ({ connection, payer }) => {
    const p = await loadPool(connection, pool);
    const info = await describe(pool, p);
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

// --- positions -----------------------------------------------------------------
// A position is an NFT (amount 1, 0 decimals) in the wallet, under either token
// program; its personal position PDA is ["position", mint] on the PancakeSwap
// program. Only PDAs the program owns, of the right size, on this pool count.
async function ownerPositions(connection, owner, p) {
  const nfts = [];
  for (const prog of [TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID]) {
    const r = await connection.getParsedTokenAccountsByOwner(owner, { programId: prog });
    for (const a of r.value ?? []) {
      const info = a.account.data.parsed.info;
      if (info.tokenAmount.amount === '1' && info.tokenAmount.decimals === 0) {
        nfts.push({ nftMint: new PublicKey(info.mint), nftAccount: a.pubkey, nft2022: prog.equals(TOKEN_2022_PROGRAM_ID) });
      }
    }
  }
  const out = [];
  for (let i = 0; i < nfts.length; i += 100) {
    const chunk = nfts.slice(i, i + 100);
    const pdas = chunk.map(n => getPdaPersonalPositionAddress(PROGRAM, n.nftMint).publicKey);
    const accs = await connection.getMultipleAccountsInfo(pdas);
    accs.forEach((acc, k) => {
      if (!acc || !acc.owner.equals(PROGRAM) || acc.data.length !== PersonalPositionLayout.span) return;
      const d = PersonalPositionLayout.decode(acc.data);
      if (!d.poolId.equals(p.id)) return;
      out.push({ ...chunk[k], address: pdas[k], data: d });
    });
  }
  return out.sort((x, y) => x.data.tickLower - y.data.tickLower);
}

// Fees accrued but not collected: position vs pool fee growth, through the
// two boundary ticks (PositionUtils.GetPositionFees). When a tick array is
// unreadable the report falls back to tokenFeesOwed and says so.
async function positionFees(connection, p, d) {
  const ts = p.state.tickSpacing;
  const starts = [TickArrayUtil.getTickArrayStartIndex(d.tickLower, ts), TickArrayUtil.getTickArrayStartIndex(d.tickUpper, ts)];
  const addrs = starts.map(s => getPdaTickArrayAddress(PROGRAM, p.id, s).publicKey);
  const accs = await connection.getMultipleAccountsInfo(addrs);
  const ticks = accs.map((acc, i) => {
    if (!acc || acc.data.length !== TickArrayLayout.span) return null;
    const ta = TickArrayLayout.decode(acc.data);
    const tick = ta.ticks[TickArrayUtil.getTickOffsetInArray(i === 0 ? d.tickLower : d.tickUpper, ts)];
    return tick;
  });
  if (ticks.every(Boolean)) {
    const f = PositionUtils.GetPositionFees(p.state, d, ticks[0], ticks[1]);
    return { feeA: f.tokenFeeAmountA, feeB: f.tokenFeeAmountB, source: 'feeGrowth' };
  }
  return { feeA: d.tokenFeesOwedA, feeB: d.tokenFeesOwedB, source: 'tokenFeesOwed-only (tick array unreadable)' };
}

async function positionView(connection, pos, p, info) {
  const d = pos.data;
  const ua = (x) => Number(x.toString()) / 10 ** info.decimalsA;
  const ub = (x) => Number(x.toString()) / 10 ** info.decimalsB;
  const sqrtL = TickUtil.getSqrtPriceAtTick(d.tickLower), sqrtU = TickUtil.getSqrtPriceAtTick(d.tickUpper);
  const amounts = LiquidityMathUtil.getAmountsForLiquidity(p.state.sqrtPriceX64, sqrtL, sqrtU, d.liquidity, false);
  const fees = await positionFees(connection, p, d);
  const estA = ua(amounts.amountA), estB = ub(amounts.amountB);
  const feeA = ua(fees.feeA), feeB = ub(fees.feeB);
  const out = {
    positionMint: pos.nftMint.toBase58(), positionAddress: pos.address.toBase58(), nft2022: pos.nft2022,
    whirlpool: info.pool, pool: info.pool, dex: DEX,
    pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
    decimalsA: info.decimalsA, decimalsB: info.decimalsB,
    quoteUsd: info.quoteUsd, quoteUsdSource: info.quoteUsdSource,
    tickLower: d.tickLower, tickUpper: d.tickUpper, tickCurrent: p.state.tickCurrent,
    liquidity: d.liquidity.toString(),
    lowerPrice: Number(tickPrice(d.tickLower, info.decimalsA, info.decimalsB).toFixed(6)),
    upperPrice: Number(tickPrice(d.tickUpper, info.decimalsA, info.decimalsB).toFixed(6)),
    price: Number(info.price.toFixed(6)),
    inRange: p.state.tickCurrent >= d.tickLower && p.state.tickCurrent < d.tickUpper,
    closeEstA: estA, closeEstB: estB,
    feesAccruedA: feeA, feesAccruedB: feeB, feesSource: fees.source,
    feesOwedA: ua(d.tokenFeesOwedA), feesOwedB: ub(d.tokenFeesOwedB),
    feesAccrued_quote: Number((feeA * info.price + feeB).toFixed(9)),
  };
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((estA * info.price + estB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number(((feeA * info.price + feeB) * info.quoteUsd).toFixed(6));
  }
  return out;
}

// The union of every position the wallet holds on the pool, as one.
async function unionView(connection, list, p, info) {
  const views = [];
  for (const pos of list) views.push(await positionView(connection, pos, p, info));
  const sum = (k) => views.reduce((n, v) => n + (v[k] ?? 0), 0);
  const sumBN = (k) => list.reduce((n, x) => n.add(x.data[k]), new BN(0));
  const lo = views[0];
  const upper = Math.max(...views.map(v => v.tickUpper));
  const out = {
    ...lo,
    positionMint: lo.positionMint,
    positions: views.map(v => v.positionMint),
    tickLower: lo.tickLower, tickUpper: upper,
    liquidity: sumBN('liquidity').toString(),
    lowerPrice: lo.lowerPrice, upperPrice: Math.max(...views.map(v => v.upperPrice)),
    inRange: p.state.tickCurrent >= lo.tickLower && p.state.tickCurrent < upper,
    closeEstA: sum('closeEstA'), closeEstB: sum('closeEstB'),
    feesAccruedA: sum('feesAccruedA'), feesAccruedB: sum('feesAccruedB'),
    feesOwedA: sum('feesOwedA'), feesOwedB: sum('feesOwedB'),
    feesSource: views.every(v => v.feesSource === 'feeGrowth') ? 'feeGrowth' : views.map(v => v.feesSource).join('; '),
  };
  out.feesAccrued_quote = Number((out.feesAccruedA * info.price + out.feesAccruedB).toFixed(9));
  if (info.quoteUsd != null) {
    out.positionUsd = Number(((out.closeEstA * info.price + out.closeEstB) * info.quoteUsd).toFixed(4));
    out.feesAccrued_USD = Number((out.feesAccrued_quote * info.quoteUsd).toFixed(6));
  }
  return out;
}

const isPosition = (pos, id) => pos.nftMint.toBase58() === id || pos.address.toBase58() === id;

async function status(positionArg) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer }) => {
    const p = await loadPool(connection, pool);
    const info = await describe(pool, p);
    const list = await ownerPositions(connection, payer.publicKey, p);
    if (!list.length || (positionArg && !list.some(x => isPosition(x, positionArg)))) {
      console.log(JSON.stringify({ positions: 0, positionMint: null, pool }, null, 1));
      return null;
    }
    const out = await unionView(connection, list, p, info);
    try {
      out.rentSol = Number((await rentOf(connection, payer.publicKey, list.map(x => x.nftMint), PROGRAM)).toFixed(9));
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
  // The close burns the Token-2022 position mint and refunds its rent
  // (seen on chain, 2026-09-26: mint DAaooK… returned 0.0016764 SOL).
  return positionRent(connection, owner, nftMints, programId, { refundMint: true });
}

async function rentUsdOf(info, rentSol) {
  const su = info.nativeSide === 'A' && info.quoteUsd != null ? info.price * info.quoteUsd
    : info.nativeSide === 'B' && info.quoteUsd != null ? info.quoteUsd
    : await tokenUsd(NATIVE_MINT);
  return su != null ? Number((rentSol * su).toFixed(4)) : null;
}

async function positions() {
  const pool = poolArg();
  return withRpc(async ({ connection, payer }) => {
    const p = await loadPool(connection, pool);
    const list = await ownerPositions(connection, payer.publicKey, p);
    console.log(JSON.stringify(list.map(x => ({
      positionMint: x.nftMint.toBase58(), address: x.address.toBase58(), pool, nft2022: x.nft2022,
      tickLower: x.data.tickLower, tickUpper: x.data.tickUpper, liquidity: x.data.liquidity.toString(),
      feesOwedA: x.data.tokenFeesOwedA.toString(), feesOwedB: x.data.tokenFeesOwedB.toString(),
    })), null, 1));
  });
}

// --- transactions ------------------------------------------------------------------
function ata(owner, mint, program) {
  return getAssociatedTokenAddressSync(mint, owner, true, program);
}

// The owner's token accounts for both pool mints and every reward mint, created
// idempotently. When one side is native SOL its account is the WSOL ATA:
// `wrapLamports` lamports are moved in and synced before the CLMM instruction,
// and the account is closed at the end so the balance returns to native SOL.
function tokenAccountIxs(p, owner, wrapLamports) {
  const pre = [], post = [];
  const acct = {};
  for (const [side, mint] of [['A', p.state.mintA], ['B', p.state.mintB]]) {
    const prog = p.programOf.get(mint.toBase58());
    const a = ata(owner, mint, prog);
    acct[side] = a;
    pre.push(createAssociatedTokenAccountIdempotentInstruction(owner, a, owner, mint, prog));
    if (mint.toBase58() === NATIVE_MINT) {
      if (wrapLamports && wrapLamports.gtn(0)) {
        pre.push(SystemProgram.transfer({ fromPubkey: owner, toPubkey: a, lamports: BigInt(wrapLamports.toString()) }));
        pre.push(createSyncNativeInstruction(a, prog));
      }
      post.push(createCloseAccountInstruction(a, owner, owner, [], prog));
    }
  }
  acct.rewards = p.rewards.map(r => {
    const prog = p.programOf.get(r.mint.toBase58());
    const a = ata(owner, r.mint, prog);
    pre.push(createAssociatedTokenAccountIdempotentInstruction(owner, a, owner, r.mint, prog));
    return a;
  });
  return { pre, post, acct };
}

function budgetIxs() {
  return [
    ComputeBudgetProgram.setComputeUnitLimit({ units: CU_LIMIT }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: PRIORITY_MICROLAMPORTS }),
  ];
}

async function buildTx(connection, payer, instructions) {
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
  const msg = new TransactionMessage({ payerKey: payer, recentBlockhash: blockhash, instructions }).compileToV0Message();
  return { tx: new VersionedTransaction(msg), blockhash, lastValidBlockHeight };
}

async function simulate(connection, tx) {
  const r = await connection.simulateTransaction(tx, { sigVerify: false, replaceRecentBlockhash: true, commitment: 'confirmed' });
  return { err: r.value.err ?? null, unitsConsumed: r.value.unitsConsumed ?? null, logs: r.value.logs ?? [] };
}

// Send one signed transaction and confirm it. Any error after the send is a
// SentError carrying the signature, so the caller reports partial state.
async function sendOne(connection, tx, signers, sigsSoFar) {
  const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
  tx.message.recentBlockhash = blockhash;
  tx.sign(signers);
  const sig = await connection.sendRawTransaction(tx.serialize(), { skipPreflight: false, maxRetries: 3 });
  sigsSoFar.push(sig);
  try {
    const c = await connection.confirmTransaction({ signature: sig, blockhash, lastValidBlockHeight }, 'confirmed');
    if (c.value.err) throw new Error(`transaction ${sig} failed: ${JSON.stringify(c.value.err)}`);
  } catch (e) {
    throw new SentError(e.message, sigsSoFar.slice());
  }
  return sig;
}

function partialReport(base, e) {
  console.log(JSON.stringify({ ...base, sent: true, partial: true, signatures: e.signatures,
    signature: e.signatures[e.signatures.length - 1] ?? null, error: e.message }, null, 1));
  process.exitCode = 1;
}

// --- open ---------------------------------------------------------------------------
// Price -> tick, then round the lower edge down and the upper edge up to the
// tick spacing, so the band the chain gets contains the band asked for.
function bandTicks(lower, upper, dA, dB, ts) {
  const ln = (x) => new Decimal(x).div(new Decimal(10).pow(dA - dB)).ln().div(new Decimal(1.0001).ln());
  let lo = Math.floor(ln(lower).toNumber() / ts) * ts;
  let hi = Math.ceil(ln(upper).toNumber() / ts) * ts;
  lo = Math.max(lo, TickArrayUtil.getMinTick(ts));
  hi = Math.min(hi, TickArrayUtil.getMaxTick(ts));
  if (hi <= lo) hi = lo + ts;
  return { lo, hi };
}

// signer2.mjs:depositQuote — the liquidity each cap alone would fund, the
// smaller one wins. Reported alongside the on-chain integer math as a check.
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

const toRaw = (human, decimals) => new BN(new Decimal(human).mul(new Decimal(10).pow(decimals)).floor().toFixed(0));
const withSlip = (bn) => new BN(new Decimal(bn.toString()).mul(1 + SLIPPAGE_BPS / 10000).ceil().toFixed(0));
const lessSlip = (bn) => new BN(new Decimal(bn.toString()).mul(1 - SLIPPAGE_BPS / 10000).floor().toFixed(0));
const minBN = (a, b) => (a.lt(b) ? a : b);

async function open(pool, lower, upper, maxA, maxB, execute) {
  if (!pool || !(Number(lower) > 0) || !(Number(upper) > Number(lower)) || !(Number(maxA) >= 0) || !(Number(maxB) >= 0)) {
    throw new Error('usage: open <pool> <lowerPrice> <upperPrice> <maxA> <maxB> [--execute]');
  }
  return withRpc(async ({ connection, payer }) => {
    const p = await loadPool(connection, pool);
    const info = await describe(pool, p);
    const lamports = await connection.getBalance(payer.publicKey);
    if (lamports / 1e9 < GAS_RESERVE_SOL) {
      throw new Error(`SOL below the ${GAS_RESERVE_SOL} gas reserve; a wallet that cannot pay fees cannot close its own position`);
    }
    if (!(info.price >= Number(lower) && info.price <= Number(upper))) {
      throw new Error(`price ${info.price} is outside [${lower}, ${upper}]; the band must contain the price`);
    }
    const { lo, hi } = bandTicks(lower, upper, info.decimalsA, info.decimalsB, p.state.tickSpacing);
    if (!(p.state.tickCurrent >= lo && p.state.tickCurrent < hi)) {
      throw new Error(`current tick ${p.state.tickCurrent} is outside ticks ${lo}..${hi}; the price moved`);
    }
    const tickLower = tickPrice(lo, info.decimalsA, info.decimalsB);
    const tickUpper = tickPrice(hi, info.decimalsA, info.decimalsB);

    // Size: the liquidity each cap alone funds at the current price, the
    // smaller wins — in the program's own integer math.
    const sqrtL = TickUtil.getSqrtPriceAtTick(lo), sqrtU = TickUtil.getSqrtPriceAtTick(hi);
    const capA = toRaw(maxA, info.decimalsA), capB = toRaw(maxB, info.decimalsB);
    const liquidity = LiquidityMathUtil.getLiquidityFromAmounts(p.state.sqrtPriceX64, sqrtL, sqrtU, capA, capB);
    if (liquidity.isZero()) throw new Error('nothing to deposit: the caps fund zero liquidity in this band');
    const need = LiquidityMathUtil.getAmountsForLiquidity(p.state.sqrtPriceX64, sqrtL, sqrtU, liquidity, true);
    const amountMaxA = minBN(withSlip(need.amountA), capA), amountMaxB = minBN(withSlip(need.amountB), capB);
    const estA = Number(need.amountA.toString()) / 10 ** info.decimalsA;
    const estB = Number(need.amountB.toString()) / 10 ** info.decimalsB;
    const approxUsd = (estA * info.price + estB) * (info.quoteUsd ?? 1);
    if (approxUsd > MAX_USD) throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
    const quote = depositQuote(info.price, tickLower, tickUpper, Number(maxA), Number(maxB));

    // The wallet's token accounts; native SOL is wrapped for the deposit.
    // A dry run still builds and simulates when the wallet is short (the
    // simulation then fails at the SOL transfer, which is the expected place);
    // --execute refuses.
    const wrap = info.nativeSide === 'A' ? amountMaxA : info.nativeSide === 'B' ? amountMaxB : null;
    const solShort = wrap ? Math.max(0, (Number(wrap.toString()) + GAS_RESERVE_SOL * 1e9 - lamports) / 1e9) : 0;
    if (execute && solShort > 0) {
      throw new Error(`deposit needs ${Number(wrap.toString()) / 1e9} SOL plus the ${GAS_RESERVE_SOL} gas reserve; wallet has ${lamports / 1e9}`);
    }
    const { pre, post, acct } = tokenAccountIxs(p, payer.publicKey, wrap);

    // open_position_with_token22_nft: the fork's own open path (no metadata).
    const nftMint = Keypair.generate();
    const positionNftAccount = ata(payer.publicKey, nftMint.publicKey, TOKEN_2022_PROGRAM_ID);
    const personalPosition = getPdaPersonalPositionAddress(PROGRAM, nftMint.publicKey).publicKey;
    const protocolPosition = protocolPositionPda(p.id, lo, hi);
    const ta = tickArrayKeys(p, lo, hi);
    const openIx = ClmmInstrument.openPositionWithToken22NftInstruction(
      PROGRAM, payer.publicKey, p.id, payer.publicKey, nftMint.publicKey, positionNftAccount,
      protocolPosition, ta.tickArrayLower, ta.tickArrayUpper, personalPosition,
      acct.A, acct.B, p.state.vaultA, p.state.vaultB, p.state.mintA, p.state.mintB,
      lo, hi, ta.lowerStart, ta.upperStart, liquidity, amountMaxA, amountMaxB,
      false /* withMetadata */, null /* baseFlag: use `liquidity` */, ta.exBitmap);
    protocolPositionWritable(openIx, protocolPosition);
    const instructions = [...budgetIxs(), ...pre, openIx, ...post];
    const { tx } = await buildTx(connection, payer.publicKey, instructions);
    const sim = await simulate(connection, tx);

    const report = {
      pool, dex: DEX, pair: `${info.symbolA}/${info.symbolB}`, tokenA: info.symbolA, tokenB: info.symbolB,
      lowerPrice: Number(lower), upperPrice: Number(upper),
      tickLower: lo, tickUpper: hi, tickLowerPrice: tickLower, tickUpperPrice: tickUpper,
      price: info.price, tickCurrent: p.state.tickCurrent, tickSpacing: p.state.tickSpacing,
      tokenMaxA: Number(maxA), tokenMaxB: Number(maxB),
      liquidity: liquidity.toString(),
      depositEstA: estA, depositEstB: estB,
      amountMaxA: Number(amountMaxA.toString()) / 10 ** info.decimalsA,
      amountMaxB: Number(amountMaxB.toString()) / 10 ** info.decimalsB,
      approxUsd: Number(approxUsd.toFixed(2)),
      depositUsd: info.quoteUsd != null ? Number(approxUsd.toFixed(4)) : null,
      walletSol: lamports / 1e9, solShort: Number(solShort.toFixed(9)),
      quote,
      positionMint: nftMint.publicKey.toBase58(),
      positionAddress: personalPosition.toBase58(), protocolPosition: protocolPosition.toBase58(),
      tickArrays: [ta.tickArrayLower.toBase58(), ta.tickArrayUpper.toBase58()], exBitmap: ta.exBitmap?.toBase58() ?? null,
      instructions: instructions.length, transactions: 1,
      simulation: sim,
    };
    if (!execute) {
      console.log(JSON.stringify({ ...report, sent: false }, null, 1));
      console.log('DRY RUN — instructions built and simulated. Pass --execute to sign and send.');
      return;
    }
    if (sim.err) throw new Error(`simulation failed, not sending: ${JSON.stringify(sim.err)} ${sim.logs.slice(-3).join(' | ')}`);
    const sigs = [];
    try {
      const sig = await sendOne(connection, tx, [payer, nftMint], sigs);
      console.log(JSON.stringify({ ...report, sent: true, signature: sig, signatures: sigs }, null, 1));
    } catch (e) {
      if (e.sent) return partialReport(report, e);
      throw e;
    }
  });
}

// --- harvest / close ----------------------------------------------------------------
// All of the wallet's positions on the pool, provided the one named is among
// them: the name is a check that the caller and the chain agree on which pool.
async function findPositions(connection, owner, p, id) {
  const list = await ownerPositions(connection, owner, p);
  if (!list.some(x => isPosition(x, id))) throw new Error(`position ${id} not found for this wallet on this pool`);
  return list;
}

// decrease_liquidity_v2 for one position: liquidity 0 harvests (fees and
// rewards); the full liquidity plus close_position empties and closes it.
function decreaseIxs(p, payer, pos, liquidity, amountMinA, amountMinB, acct, close) {
  const d = pos.data;
  const ta = tickArrayKeys(p, d.tickLower, d.tickUpper);
  const rewardAccounts = p.rewards.map((r, i) => ({ poolRewardVault: r.vault, ownerRewardVault: acct.rewards[i], rewardMint: r.mint }));
  const protocolPosition = protocolPositionPda(p.id, d.tickLower, d.tickUpper);
  const ixs = [protocolPositionWritable(ClmmInstrument.decreaseLiquidityV2Instruction(
    PROGRAM, payer, pos.nftAccount, pos.address, p.id, protocolPosition,
    ta.tickArrayLower, ta.tickArrayUpper, acct.A, acct.B, p.state.vaultA, p.state.vaultB, p.state.mintA, p.state.mintB,
    rewardAccounts, liquidity, amountMinA, amountMinB, ta.exBitmap), protocolPosition)];
  if (close) {
    // The fork's close_position takes six accounts; no trailing pool account.
    ixs.push(ClmmInstrument.closePositionInstruction(PROGRAM, payer, pos.nftMint, pos.nftAccount, pos.address, pos.nft2022));
  }
  return ixs;
}

async function harvestOrClose(kind, id, execute) {
  const pool = poolArg();
  return withRpc(async ({ connection, payer }) => {
    const p = await loadPool(connection, pool);
    const info = await describe(pool, p);
    const ps = await findPositions(connection, payer.publicKey, p, id);
    const { pre, post, acct } = tokenAccountIxs(p, payer.publicKey, null);
    const txs = [];
    let totA = new BN(0), totB = new BN(0), feeA = new BN(0), feeB = new BN(0);
    for (const pos of ps) {
      const d = pos.data;
      const sqrtL = TickUtil.getSqrtPriceAtTick(d.tickLower), sqrtU = TickUtil.getSqrtPriceAtTick(d.tickUpper);
      const amounts = LiquidityMathUtil.getAmountsForLiquidity(p.state.sqrtPriceX64, sqrtL, sqrtU, d.liquidity, false);
      const fees = await positionFees(connection, p, d);
      totA = totA.add(amounts.amountA); totB = totB.add(amounts.amountB);
      feeA = feeA.add(fees.feeA); feeB = feeB.add(fees.feeB);
      const body = kind === 'close'
        ? decreaseIxs(p, payer.publicKey, pos, d.liquidity, lessSlip(amounts.amountA), lessSlip(amounts.amountB), acct, true)
        : decreaseIxs(p, payer.publicKey, pos, new BN(0), new BN(0), new BN(0), acct, false);
      const instructions = [...budgetIxs(), ...pre, ...body, ...post];
      const { tx } = await buildTx(connection, payer.publicKey, instructions);
      txs.push({ tx, instructions: instructions.length, position: pos.nftMint.toBase58(), simulation: await simulate(connection, tx) });
    }
    const ua = (x) => Number(x.toString()) / 10 ** info.decimalsA, ub = (x) => Number(x.toString()) / 10 ** info.decimalsB;
    const report = {
      mint: id, pool, dex: DEX, positions: ps.map(x => x.nftMint.toBase58()), transactions: txs.length,
      instructions: txs.reduce((n, t) => n + t.instructions, 0),
      quote: { tokenEstA: ua(totA), tokenEstB: ub(totB) },
      feesQuote: { feeA: ua(feeA), feeB: ub(feeB) },
      simulations: txs.map(t => ({ position: t.position, ...t.simulation })),
    };
    if (!execute) {
      console.log(JSON.stringify({ ...report, sent: false }, null, 1));
      console.log(`DRY RUN — ${kind} instructions built and simulated. Pass --execute to send.`);
      return;
    }
    const bad = txs.find(t => t.simulation.err);
    if (bad) throw new Error(`simulation failed for ${bad.position}, not sending: ${JSON.stringify(bad.simulation.err)} ${bad.simulation.logs.slice(-3).join(' | ')}`);
    const sigs = [];
    try {
      for (const t of txs) await sendOne(connection, t.tx, [payer], sigs);
    } catch (e) {
      if (e.sent) return partialReport(report, e);
      throw e;
    }
    const key = kind === 'close' ? 'closed' : 'harvested';
    console.log(JSON.stringify({ [key]: id, signature: sigs[sigs.length - 1], signatures: sigs }, null, 1));
  });
}

// Up to three builds on fresh pool state when the chain refuses on slippage
// (a refused transaction reverted whole; nothing else is retried here).
async function rebuildOnSlippage(fn, execute, attempts = 3) {
  for (let i = 1; ; i++) {
    try {
      return await fn();
    } catch (e) {
      const m = String(e?.message ?? e) + ' ' + (e?.logs ?? []).join(' ');
      if (!execute || i >= attempts || !SLIPPAGE_REFUSAL.test(m) || /partial send/.test(m) || e?.sent) throw e;
      console.error(`slippage refusal; rebuilding on fresh pool state (attempt ${i + 1}/${attempts})`);
      await new Promise(res => setTimeout(res, 1500));
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
  if (cmd === 'harvest') { if (!rest[0]) throw new Error('usage: harvest <position> [--execute]'); return harvestOrClose('harvest', rest[0], execute); }
  if (cmd === 'close') { if (!rest[0]) throw new Error('usage: close <position> [--execute]'); return rebuildOnSlippage(() => harvestOrClose('close', rest[0], execute), execute); }
  if (cmd === 'open') return rebuildOnSlippage(() => open(rest[0], rest[1], rest[2], rest[3], rest[4], execute), execute);
  if (cmd === 'pool') {
    const pool = poolArg(rest[0]);
    return withRpc(async ({ connection }) => {
      const p = await loadPool(connection, pool);
      console.log(JSON.stringify(await describe(pool, p), null, 1));
    }, false);
  }
  console.log('commands: balance [pool] | positions | status [position] | pool [pool] | '
    + 'open <pool> <lo> <hi> <maxA> <maxB> [--execute] | harvest <position> [--execute] '
    + '| close <position> [--execute]   (pool via --pool or LPBOT_POOL)');
}

main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
