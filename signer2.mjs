// Orca position signer, rebuilt on @orca-so/whirlpools v8.
//
// The legacy @orca-so/whirlpools-sdk@0.22.0 encodes swaps correctly against the
// deployed program but not the liquidity instructions: every open attempt was
// rejected as custom error 6069 after ~1390 compute units, on both an
// adaptive-fee pool (ZEC/USDC) and a plain one (SOL/USDC). 0.22.0 is the last
// release of that line, so the fix is the current package rather than a version
// bump.
//
// The key is read from WALLET_SECRET_PATH inside this process, handed straight
// to setPayerFromBytes, and never printed or returned. Only the public address
// is ever shown.
//
// Commands:
//   node signer2.mjs balance
//   node signer2.mjs open <pool> <lowerPrice> <upperPrice> <maxSolUi> <maxUsdcUi> [--execute]
//   node signer2.mjs positions
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
const MAX_USD = 260;
const SLIPPAGE_BPS = 100;          // 1%

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

async function balance() {
  return withRpc(async ({ signer, rpc }) => {
  const lamports = await rpc.getBalance(signer.address).send();
  console.log(JSON.stringify({
    owner: signer.address, sol: Number(lamports.value) / 1e9,
  }, null, 1));
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

async function open(pool, lower, upper, maxSolUi, maxUsdcUi, execute) {
  return withRpc(async ({ signer, rpc }) => {
  const lamports = await rpc.getBalance(signer.address).send();
  if (Number(lamports.value) / 1e9 < 0.02) {
    throw new Error('SOL below the 0.02 reserve; a wallet that cannot pay fees '
      + 'cannot close its own position');
  }
  const approxUsd = Number(maxSolUi) * 118 + Number(maxUsdcUi);
  if (approxUsd > MAX_USD) {
    throw new Error(`position about $${approxUsd.toFixed(0)} exceeds cap $${MAX_USD}`);
  }
  const param = {
    tokenMaxA: BigInt(Math.floor(Number(maxSolUi) * 1e9)),
    tokenMaxB: BigInt(Math.floor(Number(maxUsdcUi) * 1e6)),
  };
  const result = await openConcentratedPosition(
    address(pool), param, Number(lower), Number(upper),
    { slippageToleranceBps: SLIPPAGE_BPS, funder: signer });

  const report = {
    pool, lowerPrice: Number(lower), upperPrice: Number(upper),
    tokenMaxA_SOL: Number(maxSolUi), tokenMaxB_USDC: Number(maxUsdcUi),
    positionMint: result.positionMint ?? null,
    quote: result.quote ? {
      liquidity: result.quote.liquidityDelta?.toString(),
      tokenEstA: result.quote.tokenEstA?.toString(),
      tokenEstB: result.quote.tokenEstB?.toString(),
    } : null,
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
    // Explicit, parseable: the read worked and there is genuinely nothing.
    // The caller must be able to tell this apart from a failed read.
    console.log(JSON.stringify({ positions: 0, positionMint: null }, null, 1));
    return null;
  }
  const d = chosen.data;
  // Pool price from Orca's API: the SDK's pool fetcher lives in a separate
  // package and this is already the source the scanner trusts.
  const r = await fetch(`https://api.orca.so/v2/solana/pools/${d.whirlpool}`,
    { headers: { accept: 'application/json', 'user-agent': 'Mozilla/5.0' } });
  const pj = await r.json();
  const price = Number((pj.data ?? pj).price);
  const decDiff = 10 ** (9 - 6);      // SOL 9 decimals against USDC 6
  const lower = 1.0001 ** d.tickLowerIndex * decDiff;
  const upper = 1.0001 ** d.tickUpperIndex * decDiff;
  const out = {
    positionMint: d.positionMint,
    whirlpool: d.whirlpool,
    liquidity: d.liquidity.toString(),
    tickLower: d.tickLowerIndex, tickUpper: d.tickUpperIndex,
    lowerPrice: Number(lower.toFixed(4)), upperPrice: Number(upper.toFixed(4)),
    price: Number(price.toFixed(4)),
    inRange: price >= lower && price <= upper,
    feeOwedA_SOL: Number(d.feeOwedA) / 1e9,
    feeOwedB_USDC: Number(d.feeOwedB) / 1e6,
  };
  // The position's own feeOwed fields are only settled when the position is
  // touched, so they read zero on a live position that is in fact earning.
  // The close quote recomputes them from current fee growth, which is the
  // real accrued figure. Reported separately so the stale field stays visible.
  try {
    const cq = await closePositionInstructions(rpc, address(d.positionMint),
      { slippageToleranceBps: SLIPPAGE_BPS, authority: signer });
    if (cq?.quote) {
      // What a close would return right now: the position's mark.
      out.closeEstA_SOL = Number(cq.quote.tokenEstA) / 1e9;
      out.closeEstB_USDC = Number(cq.quote.tokenEstB) / 1e6;
    }
    if (cq?.feesQuote) {
      out.feesAccruedA_SOL = Number(cq.feesQuote.feeOwedA) / 1e9;
      out.feesAccruedB_USDC = Number(cq.feesQuote.feeOwedB) / 1e6;
      out.feesAccrued_USD = Number(
        (out.feesAccruedA_SOL * price + out.feesAccruedB_USDC).toFixed(4));
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
  if (cmd === 'balance') return balance();
  if (cmd === 'positions') return positions();
  if (cmd === 'status') return status(a[0]);
  if (cmd === 'harvest') return harvest(a[0], execute);
  if (cmd === 'close') return close(a[0], execute);
  if (cmd === 'open') return open(a[0], a[1], a[2], a[3], a[4], execute);
  console.log('commands: balance | positions | status [mint] | open <pool> <lo> <hi> <maxSol> <maxUsdc> [--execute] | harvest <mint> [--execute] | close <mint> [--execute]');
}

main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
