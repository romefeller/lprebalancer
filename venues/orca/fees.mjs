// Accrued fees of an Orca whirlpool position, from ONE consistent read.
//
// Why (2026-09-27): a fee is feeGrowthInside(now) - checkpoint, and
// feeGrowthInside is built from the whirlpool's global growth, its current
// tick and the two boundary ticks' "outside" growth. The SDK's
// closePositionInstructions reads the position, the whirlpool and the tick
// arrays in separate calls. A tick crossed between them mixes two states: on
// Raydium the same shape booked $6,237 of fees on a $230 position.
//
// Here the position, the whirlpool, both tick arrays and both mints come from
// one getMultipleAccounts call (one slot). The fee is computed twice, by the
// SDK's collectFeesQuote and by the arithmetic below, and must pass the
// invariants in checkOrca; the two must agree before transfer fees. A read
// that fails is retried; if it never passes the caller gets ok: false.
import { fetchEncodedAccounts } from '@solana/kit';
import {
  decodeWhirlpool, decodePosition, decodeTickArray, getTickArrayAddress, getPositionAddress,
} from '@orca-so/whirlpools-client';
import { collectFeesQuote, getTickArrayStartTickIndex, getTickIndexInArray } from '@orca-so/whirlpools-core';
import { decodeMint } from '@solana-program/token-2022';

export const U128 = 1n << 128n;
export const HALF_U128 = 1n << 127n;
export const U64 = 1n << 64n;

const wsub = (a, b) => ((BigInt(a) - BigInt(b)) % U128 + U128) % U128;

// Fee growth inside [lower, upper), as the program computes it.
export function growthInside(tickCurrent, globalG, lowerOutside, upperOutside, lowerIndex, upperIndex) {
  const below = tickCurrent < lowerIndex ? wsub(globalG, lowerOutside) : BigInt(lowerOutside);
  const above = tickCurrent < upperIndex ? BigInt(upperOutside) : wsub(globalG, upperOutside);
  return wsub(wsub(globalG, below), above);
}

// Fees of one position from one snapshot, before transfer fees, with the
// growth deltas the invariants need.
export function ownFees(whirlpool, position, lower, upper) {
  const t = whirlpool.tickCurrentIndex, lo = position.tickLowerIndex, hi = position.tickUpperIndex;
  const inA = growthInside(t, whirlpool.feeGrowthGlobalA, lower.feeGrowthOutsideA, upper.feeGrowthOutsideA, lo, hi);
  const inB = growthInside(t, whirlpool.feeGrowthGlobalB, lower.feeGrowthOutsideB, upper.feeGrowthOutsideB, lo, hi);
  const dA = wsub(inA, position.feeGrowthCheckpointA), dB = wsub(inB, position.feeGrowthCheckpointB);
  const L = BigInt(position.liquidity);
  return {
    feeA: BigInt(position.feeOwedA) + ((dA * L) >> 64n),
    feeB: BigInt(position.feeOwedB) + ((dB * L) >> 64n),
    deltaA: dA, deltaB: dB,
  };
}

// The invariants one read must satisfy; null when it passes, else why not.
export function checkOrca({ whirlpoolAddress, positionWhirlpool, position, lower, upper, own, quote, transferFees }) {
  if (whirlpoolAddress && positionWhirlpool !== whirlpoolAddress) {
    return `position belongs to whirlpool ${positionWhirlpool}, not ${whirlpoolAddress}`;
  }
  if (!lower || !upper) return 'boundary tick unreadable';
  if (BigInt(position.liquidity) > 0n && (!lower.initialized || !upper.initialized ||
      BigInt(lower.liquidityGross) === 0n || BigInt(upper.liquidityGross) === 0n)) {
    return 'a boundary tick of a live position is not initialised';
  }
  if (own.deltaA >= HALF_U128 || own.deltaB >= HALF_U128) {
    return 'fee growth inside went backwards (inconsistent read)';
  }
  if (own.feeA >= U64 || own.feeB >= U64) return 'fee outside the u64 range';
  const qa = BigInt(quote.feeOwedA), qb = BigInt(quote.feeOwedB);
  if (!transferFees && (qa !== own.feeA || qb !== own.feeB)) {
    return `the SDK quote ${qa}/${qb} disagrees with the fee arithmetic ${own.feeA}/${own.feeB}`;
  }
  if (qa > own.feeA || qb > own.feeB || qa < 0n || qb < 0n) {
    return 'the SDK quote exceeds the fee before transfer fees';
  }
  return null;
}

// The transfer fee of a Token-2022 mint at `epoch`, in the shape
// collectFeesQuote takes; undefined for a mint without one.
export function transferFeeOf(mint, epoch) {
  const ext = mint?.data?.extensions;
  if (!ext || ext.__option === 'None') return undefined;
  const cfg = ext.value.find(x => x.__kind === 'TransferFeeConfig');
  if (!cfg) return undefined;
  const f = BigInt(epoch) >= BigInt(cfg.newerTransferFee.epoch) ? cfg.newerTransferFee : cfg.olderTransferFee;
  return { feeBps: f.transferFeeBasisPoints, maxFee: f.maximumFee };
}

// Fees of one snapshot. Pure.
export function feesFromOrcaSnapshot(snap, whirlpoolAddress, epoch, quoteFn = collectFeesQuote) {
  const { position, whirlpool, lowerArray, upperArray, mintA, mintB } = snap;
  const s = whirlpool.tickSpacing;
  const pick = (arr, tick) => {
    if (!arr) return null;
    const start = getTickArrayStartTickIndex(tick, s);
    if (arr.startTickIndex !== start) return null;
    return arr.ticks[getTickIndexInArray(tick, start, s)] ?? null;
  };
  const lower = pick(lowerArray, position.tickLowerIndex), upper = pick(upperArray, position.tickUpperIndex);
  const fallback = { feeA: BigInt(position.feeOwedA), feeB: BigInt(position.feeOwedB) };
  if (!lower || !upper) return { ok: false, reason: 'boundary tick unreadable', ...fallback };
  const tA = transferFeeOf(mintA, epoch), tB = transferFeeOf(mintB, epoch);
  const own = ownFees(whirlpool, position, lower, upper);
  const args = { whirlpoolAddress, positionWhirlpool: position.whirlpool, position, lower, upper, own,
                 transferFees: Boolean(tA || tB) };
  // Our own arithmetic and invariants first: on a mixed read the SDK's quote
  // throws ("Amount exceeds max u64") instead of answering, and a throw here
  // would skip the retry.
  const pre = checkOrca({ ...args, quote: { feeOwedA: own.feeA, feeOwedB: own.feeB }, transferFees: true });
  if (pre) return { ok: false, reason: pre, ...fallback };
  let quote;
  try {
    quote = quoteFn(whirlpool, position, lower, upper, tA, tB);
  } catch (e) {
    return { ok: false, reason: `the SDK quote failed: ${e?.message ?? e}`, ...fallback };
  }
  const reason = checkOrca({ ...args, quote });
  if (reason) return { ok: false, reason, ...fallback };
  return { ok: true, reason: null, feeA: BigInt(quote.feeOwedA), feeB: BigInt(quote.feeOwedB) };
}

// The default reader: one getMultipleAccounts call, decoded.
export async function readSnapshot(rpc, addrs) {
  const [p, w, lo, hi, ma, mb] = await fetchEncodedAccounts(rpc, [addrs.position, addrs.whirlpool,
    addrs.lowerArray, addrs.upperArray, addrs.mintA, addrs.mintB]);
  if (!p.exists || !w.exists) throw new Error('position or whirlpool unreadable in the snapshot');
  return {
    position: decodePosition(p).data, whirlpool: decodeWhirlpool(w).data,
    lowerArray: lo.exists ? decodeTickArray(lo).data : null,
    upperArray: hi.exists ? decodeTickArray(hi).data : null,
    mintA: ma.exists ? decodeMint(ma) : null, mintB: mb.exists ? decodeMint(mb) : null,
  };
}

// The addresses a snapshot needs. The position's ticks, its whirlpool, the
// whirlpool's tick spacing and mints never change, so one earlier read that
// names them is safe; only the snapshot itself must be consistent.
export async function snapshotAddresses(position, whirlpool, whirlpoolAddress) {
  const s = whirlpool.tickSpacing;
  const lo = getTickArrayStartTickIndex(position.tickLowerIndex, s);
  const hi = getTickArrayStartTickIndex(position.tickUpperIndex, s);
  return {
    whirlpool: whirlpoolAddress,
    lowerArray: (await getTickArrayAddress(whirlpoolAddress, lo))[0],
    upperArray: (await getTickArrayAddress(whirlpoolAddress, hi))[0],
    mintA: whirlpool.tokenMintA, mintB: whirlpool.tokenMintB,
  };
}

// Fees of the position with mint `positionMint` on `whirlpoolAddress`.
// `read` and `epoch` are injectable for tests.
export async function consistentOrcaFees(rpc, positionMint, whirlpoolAddress,
                                         { tries = 3, pauseMs = 400, read = readSnapshot, epoch = null } = {}) {
  const positionAddress = (await getPositionAddress(positionMint))[0];
  const ep = epoch ?? (await rpc.getEpochInfo().send()).epoch;
  // one read to learn the fixed addresses (tick arrays, mints)
  const [p0, w0] = await fetchEncodedAccountsFor(rpc, read, positionAddress, whirlpoolAddress);
  const addrs = { position: positionAddress, ...(await snapshotAddresses(p0, w0, whirlpoolAddress)) };
  let last = null;
  for (let i = 0; i < tries; i++) {
    const snap = await read(rpc, addrs);
    const r = feesFromOrcaSnapshot(snap, whirlpoolAddress, ep);
    if (r.ok) return r;
    last = r;
    if (i + 1 < tries) await new Promise(res => setTimeout(res, pauseMs));
  }
  return last;
}

async function fetchEncodedAccountsFor(rpc, read, positionAddress, whirlpoolAddress) {
  if (read !== readSnapshot) {
    const s = await read(rpc, { position: positionAddress, whirlpool: whirlpoolAddress });
    return [s.position, s.whirlpool];
  }
  const [p, w] = await fetchEncodedAccounts(rpc, [positionAddress, whirlpoolAddress]);
  if (!p.exists || !w.exists) throw new Error('position or whirlpool unreadable');
  return [decodePosition(p).data, decodeWhirlpool(w).data];
}
