// Accrued fees of Raydium-layout CLMM positions (Raydium CLMM, PancakeSwap v3
// on Solana, and the Byreal fork through its own adapter), from ONE
// consistent read of the chain.
//
// Why (2026-09-27): the fee is feeGrowthInside(now) - feeGrowthInsideLast,
// and feeGrowthInside is built from the pool's global growth, its current
// tick and the two boundary ticks' "outside" growth. The Raydium signer read
// the pool, the position and the tick arrays in three separate RPC calls.
// When the price crossed the lower tick between those reads, the tick's
// outside growth had flipped in one read and not in the other: the position
// reported 23.16 SOL + 3,403 USDC of fees ($6,237) on a $230 position. The
// real harvest was 0.00077 SOL + 0.086 USDC.
//
// Here every account comes from one getMultipleAccountsInfo call, which the
// RPC answers from a single slot, and the result must pass the invariants in
// checkFees before it is reported. A read that fails them is retried; if it
// never passes, the caller gets `ok: false` and must not report the figure.
import { createRequire } from 'node:module';
const require_ = createRequire(import.meta.url);
const {
  PoolInfoLayout, PersonalPositionLayout, TickArrayLayout, TickArrayUtil, PositionUtils,
  getPdaPersonalPositionAddress, getPdaTickArrayAddress,
} = require_('@raydium-io/raydium-sdk-v2');
const BN = require_('bn.js');
const byreal = require_('@byreal-io/byreal-clmm-sdk');

export const U128 = new BN(1).shln(128);
export const HALF_U128 = new BN(1).shln(127);

// a - b modulo 2^128, as the program computes it.
export function wrappingSubU128(a, b) {
  const d = new BN(a).sub(new BN(b));
  return d.isNeg() ? d.add(U128) : d;            // inputs are u128, so |d| < 2^128
}

// The invariants one position's fee read must satisfy. Returns null when it
// passes, or the reason it fails.
//   * the position belongs to the pool, and the boundary ticks are the ones
//     the position names;
//   * a position with liquidity has both boundary ticks initialised;
//   * fee growth inside never goes backwards: growth - last, modulo 2^128, is
//     below 2^127 (a "negative" growth wraps to the top half);
//   * the per-token fee fits in a u64, as the program's own field does.
export function checkFees({ poolId, position, lower, upper, feeA, feeB, growthDeltaA, growthDeltaB }) {
  if (poolId && position.poolId && position.poolId.toBase58 && position.poolId.toBase58() !== poolId) {
    return `position belongs to pool ${position.poolId.toBase58()}, not ${poolId}`;
  }
  if (!lower || !upper) return 'boundary tick unreadable';
  if (lower.tick !== position.tickLower || upper.tick !== position.tickUpper) {
    return `boundary ticks read as ${lower.tick}/${upper.tick}, position names ${position.tickLower}/${position.tickUpper}`;
  }
  const liq = new BN(position.liquidity);
  if (!liq.isZero() && (new BN(lower.liquidityGross).isZero() || new BN(upper.liquidityGross).isZero())) {
    return 'a boundary tick of a live position is not initialised';
  }
  if (growthDeltaA.gte(HALF_U128) || growthDeltaB.gte(HALF_U128)) {
    return 'fee growth inside went backwards (inconsistent read)';
  }
  const U64 = new BN(1).shln(64);
  if (new BN(feeA).gte(U64) || new BN(feeB).gte(U64) || new BN(feeA).isNeg() || new BN(feeB).isNeg()) {
    return 'fee outside the u64 range';
  }
  return null;
}

// Fees of one position from one consistent snapshot: the decoded pool, the
// decoded personal position, and the two boundary tick states.
export function feesFromSnapshot(poolId, pool, position, lower, upper) {
  if (!lower || !upper) {
    return { ok: false, reason: 'boundary tick unreadable',
             feeA: new BN(position.tokenFeesOwedA), feeB: new BN(position.tokenFeesOwedB) };
  }
  const g = PositionUtils.getfeeGrowthInside(pool, lower, upper);
  const growthDeltaA = wrappingSubU128(g.feeGrowthInsideX64A, position.feeGrowthInsideLastX64A);
  const growthDeltaB = wrappingSubU128(g.feeGrowthInsideBX64, position.feeGrowthInsideLastX64B);
  const f = PositionUtils.GetPositionFees(pool, position, lower, upper);
  const reason = checkFees({ poolId, position, lower, upper, feeA: f.tokenFeeAmountA, feeB: f.tokenFeeAmountB,
                             growthDeltaA, growthDeltaB });
  return { ok: !reason, reason, feeA: f.tokenFeeAmountA, feeB: f.tokenFeeAmountB };
}

// How one program lays out its accounts. The fee math and the invariants are
// the same for every Raydium-layout program; only decoding and addresses
// differ. Each adapter:
//   positionKey(programId, nftMint)          the personal position PDA
//   arrayStart(tick, spacing)                the start index of a tick's array
//   arrayKey(programId, poolKey, start)      that array's PDA
//   decodePool(data), decodePosition(data)   null when the size is wrong
//   tickState(data, key, tick, spacing)      the tick's state, or null
export const RAYDIUM_LAYOUT = {
  positionKey: (programId, m) => getPdaPersonalPositionAddress(programId, m).publicKey,
  arrayStart: (t, spacing) => TickArrayUtil.getTickArrayStartIndex(t, spacing),
  arrayKey: (programId, poolKey, start) => getPdaTickArrayAddress(programId, poolKey, start).publicKey,
  decodePool: (data) => (data.length === PoolInfoLayout.span ? PoolInfoLayout.decode(data) : null),
  decodePosition: (data) => (data.length === PersonalPositionLayout.span ? PersonalPositionLayout.decode(data) : null),
  tickState: (data, _key, t, spacing) => (data.length === TickArrayLayout.span
    ? TickArrayLayout.decode(data).ticks[TickArrayUtil.getTickOffsetInArray(t, spacing)] : null),
};

// Byreal: its own position and tick-array layouts, and tick arrays that are
// either fixed or dynamic; the SDK's container parser reads both.
export const BYREAL_LAYOUT = {
  positionKey: (programId, m) => byreal.getPdaPersonalPositionAddress(programId, m).publicKey,
  arrayStart: (t, spacing) => byreal.TickUtils.getTickArrayStartIndexByTick(t, spacing),
  arrayKey: (programId, poolKey, start) => byreal.getPdaTickArrayAddress(programId, poolKey, start).publicKey,
  decodePool: (data) => (data.length >= byreal.PoolLayout.span ? byreal.PoolLayout.decode(data) : null),
  // the account is 281 bytes, the SDK's layout 217: the rest is padding
  decodePosition: (data) => (data.length >= byreal.PersonalPositionLayout.span
    ? byreal.PersonalPositionLayout.decode(data) : null),
  tickState: (data, key, t, spacing) => {
    try {
      const c = byreal.TickArrayUtils.parseTickArrayContainer(data, key);
      return byreal.TickArrayUtils.getTickStateFromContainer(c, t, spacing) ?? null;
    } catch {
      return null;
    }
  },
};

// The accounts one snapshot needs: the pool, each personal position, and the
// tick arrays holding each position's two boundary ticks.
export function snapshotKeys(programId, poolKey, nftMints, ticks, spacing, layout = RAYDIUM_LAYOUT) {
  const starts = [...new Set(ticks.flatMap(([lo, hi]) => [lo, hi]).map(t => layout.arrayStart(t, spacing)))];
  const arrayKeys = starts.map(s => layout.arrayKey(programId, poolKey, s));
  return {
    starts, arrayKeys,
    keys: [poolKey, ...nftMints.map(m => layout.positionKey(programId, m)), ...arrayKeys],
  };
}

// Decode one snapshot. `accounts` is the answer to getMultipleAccountsInfo
// over snapshotKeys(...).keys, in that order.
export function decodeSnapshot(programId, poolKey, nftMints, accounts, starts, spacing, layout = RAYDIUM_LAYOUT,
                               arrayKeys = null) {
  const [poolAcc, ...rest] = accounts;
  const pool = poolAcc && poolAcc.owner.equals(programId) ? layout.decodePool(poolAcc.data) : null;
  if (!pool) throw new Error('pool account unreadable in the snapshot');
  const posAccs = rest.slice(0, nftMints.length), arrAccs = rest.slice(nftMints.length);
  const arrays = new Map();
  arrAccs.forEach((a, i) => {
    if (a && a.owner.equals(programId)) arrays.set(starts[i], { data: a.data, key: arrayKeys ? arrayKeys[i] : null });
  });
  const tickOf = (t) => {
    const arr = arrays.get(layout.arrayStart(t, spacing));
    return arr ? layout.tickState(arr.data, arr.key, t, spacing) : null;
  };
  const positions = posAccs.map((a, i) => {
    const d = a && a.owner.equals(programId) ? layout.decodePosition(a.data) : null;
    if (!d) throw new Error(`position ${nftMints[i].toBase58()} unreadable in the snapshot`);
    return d;
  });
  return { pool, positions, tickOf };
}

// Fees of every listed position, from one consistent read, retried when the
// invariants fail. `list` items carry nftMint, tickLower, tickUpper. Returns
// { ok, reason, slot, fees: [{feeA, feeB}] } in list order.
export async function consistentFees(connection, programId, poolKey, list, spacing,
                                     { tries = 3, pauseMs = 400 } = {}, layout = RAYDIUM_LAYOUT) {
  const nftMints = list.map(p => p.nftMint);
  const { keys, starts, arrayKeys } = snapshotKeys(programId, poolKey, nftMints,
                                                   list.map(p => [p.tickLower, p.tickUpper]), spacing, layout);
  let last = null;
  for (let i = 0; i < tries; i++) {
    const res = await connection.getMultipleAccountsInfoAndContext(keys);
    const snap = decodeSnapshot(programId, poolKey, nftMints, res.value, starts, spacing, layout, arrayKeys);
    const fees = snap.positions.map(p => feesFromSnapshot(poolKey.toBase58(), snap.pool, p,
                                                          snap.tickOf(p.tickLower), snap.tickOf(p.tickUpper)));
    const bad = fees.find(f => !f.ok);
    if (!bad) return { ok: true, reason: null, slot: res.context.slot, fees };
    last = { ok: false, reason: bad.reason, slot: res.context.slot, fees };
    if (i + 1 < tries) await new Promise(r => setTimeout(r, pauseMs));
  }
  return last;
}
