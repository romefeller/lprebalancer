// orca_fees.mjs: the Orca fee read, from one consistent snapshot.
//
// A bigint simulator of the whirlpool's fee-growth bookkeeping gives the true
// fee of any history. A consistent snapshot must reproduce it exactly, and the
// SDK's collectFeesQuote must agree with the module's own arithmetic. The
// 2026-09-27 shape (pool state after a crossing, tick state before it) is
// replayed, and the orchestration is driven through an injected reader.
import test from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';
import { address } from '@solana/kit';
import { getWhirlpoolDecoder, getWhirlpoolSize, getPositionDecoder, getPositionSize, getTickDecoder } from '@orca-so/whirlpools-client';
import { getTickArrayStartTickIndex, getTickIndexInArray } from '@orca-so/whirlpools-core';
import {
  U128, HALF_U128, U64, growthInside, ownFees, checkOrca, transferFeeOf, feesFromOrcaSnapshot,
  consistentOrcaFees, snapshotAddresses,
} from '../orca_fees.mjs';

const POOL = address('Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE');
const OTHER = address('4QU2NpRaqmKMvPSwVKQDeW4V6JFEKJdkzbzdauumD9qN');
const MINT = address('HmN4Mgx9avZhaQwuH2MLQrzGUWzE73fLa1n5JTxhb2wH');
const SPACING = 4;
const mod = (x) => ((x % U128) + U128) % U128;

function simulate({ lo, hi, L, start, steps, gA0, gB0 }) {
  let t = start, gA = gA0, gB = gB0;
  const init = (tick) => (t >= tick ? { a: gA, b: gB } : { a: 0n, b: 0n });
  const out = { lo: init(lo), hi: init(hi) };
  const inside = () => ({
    a: growthInside(t, gA, out.lo.a, out.hi.a, lo, hi), b: growthInside(t, gB, out.lo.b, out.hi.b, lo, hi),
  });
  const last = inside();
  let eA = 0n, eB = 0n;
  const history = [];
  const snapshot = () => ({
    whirlpool: { tickCurrentIndex: t, feeGrowthGlobalA: gA, feeGrowthGlobalB: gB, tickSpacing: SPACING },
    lower: { index: lo, initialized: true, liquidityGross: BigInt(L), feeGrowthOutsideA: out.lo.a, feeGrowthOutsideB: out.lo.b },
    upper: { index: hi, initialized: true, liquidityGross: BigInt(L), feeGrowthOutsideA: out.hi.a, feeGrowthOutsideB: out.hi.b },
  });
  for (const s of steps) {
    if (s.kind === 'fee') {
      gA = mod(gA + s.a); gB = mod(gB + s.b);
      if (lo <= t && t < hi) { eA += s.a; eB += s.b; }
    } else {
      const before = snapshot();
      for (const [k, tick] of [['lo', lo], ['hi', hi]]) {
        if ((t < tick && s.to >= tick) || (t >= tick && s.to < tick)) out[k] = { a: mod(gA - out[k].a), b: mod(gB - out[k].b) };
      }
      t = s.to;
      history.push({ before, after: snapshot() });
    }
  }
  const position = { whirlpool: POOL, tickLowerIndex: lo, tickUpperIndex: hi, liquidity: BigInt(L),
                     feeGrowthCheckpointA: last.a, feeGrowthCheckpointB: last.b, feeOwedA: 0n, feeOwedB: 0n };
  return { now: snapshot(), position, history, trueA: (eA * BigInt(L)) >> 64n, trueB: (eB * BigInt(L)) >> 64n };
}

const u128 = fc.bigInt({ min: 0n, max: U128 - 1n });
const growth = fc.bigInt({ min: 0n, max: 1n << 70n });
const scenario = fc.record({
  lo: fc.integer({ min: -40, max: 0 }).map(x => x * SPACING), width: fc.integer({ min: 1, max: 20 }),
  L: fc.bigInt({ min: 1n, max: 1n << 40n }), off: fc.integer({ min: -200, max: 300 }), gA0: u128, gB0: u128,
  steps: fc.array(fc.oneof(fc.record({ kind: fc.constant('fee'), a: growth, b: growth }),
                           fc.record({ kind: fc.constant('move'), to: fc.integer({ min: -400, max: 400 }) })), { maxLength: 30 }),
}).map(s => ({ lo: s.lo, hi: s.lo + s.width * SPACING, L: s.L, start: s.lo + s.off, steps: s.steps, gA0: s.gA0, gB0: s.gB0 }));

// A snapshot in the shape readSnapshot returns: tick arrays with the ticks at
// the SDK's own indices, the rest of each array empty.
// Full account shapes, from the client's own decoders over zero bytes, so
// the SDK's wasm quote sees every field it expects.
const ZERO_WHIRLPOOL = getWhirlpoolDecoder().decode(new Uint8Array(getWhirlpoolSize()));
const ZERO_POSITION = getPositionDecoder().decode(new Uint8Array(getPositionSize()));
const ZERO_TICK = getTickDecoder().decode(new Uint8Array(200));

function arrayWith(ticks) {
  const start = getTickArrayStartTickIndex(ticks[0].index, SPACING);
  const arr = Array.from({ length: 88 }, () => ({ ...ZERO_TICK }));
  for (const tk of ticks) {
    const { index, ...rest } = tk;
    arr[getTickIndexInArray(index, start, SPACING)] = { ...ZERO_TICK, ...rest };
  }
  return { startTickIndex: start, ticks: arr };
}

function snapOf(state, position, extra = {}) {
  const w = { ...ZERO_WHIRLPOOL, ...state.whirlpool, liquidity: 1n, sqrtPrice: 1n << 64n, feeRate: 400 };
  const sameArray = getTickArrayStartTickIndex(state.lower.index, SPACING) === getTickArrayStartTickIndex(state.upper.index, SPACING);
  const lowerArray = sameArray ? arrayWith([state.lower, state.upper]) : arrayWith([state.lower]);
  const upperArray = sameArray ? lowerArray : arrayWith([state.upper]);
  return { position: { ...ZERO_POSITION, ...position }, whirlpool: w, lowerArray, upperArray, mintA: null, mintB: null, ...extra };
}

// --- the arithmetic -------------------------------------------------------------------

test('a consistent snapshot gives the true fee, and the SDK quote agrees with it', () => {
  fc.assert(fc.property(scenario, (sc) => {
    const sim = simulate(sc);
    const r = feesFromOrcaSnapshot(snapOf(sim.now, sim.position), POOL, 0n);
    assert.equal(r.reason, null);
    assert.equal(r.feeA, sim.trueA); assert.equal(r.feeB, sim.trueB);
  }), { numRuns: 400 });
});

test('replay: pool after a crossing with ticks from before it is refused', () => {
  const G = 1n << 100n;
  const sim = simulate({ lo: 0, hi: 40, L: 50_000_000_000n, start: 20, gA0: G, gB0: 3n * G,
                         steps: [{ kind: 'fee', a: 1n << 40n, b: 1n << 42n }, { kind: 'move', to: -4 }] });
  const { before, after } = sim.history[0];
  const good = feesFromOrcaSnapshot(snapOf(after, sim.position), POOL, 0n);
  assert.ok(good.ok); assert.equal(good.feeA, sim.trueA);
  const mixed = feesFromOrcaSnapshot(snapOf({ whirlpool: after.whirlpool, lower: before.lower, upper: before.upper }, sim.position), POOL, 0n);
  assert.equal(mixed.ok, false);
  assert.equal(mixed.feeA, 0n);                    // the settled feeOwed, not the mixed figure
});

test('property: a mixed read is refused, or passes with a bounded fee (the one-call read is what excludes it)', () => {
  fc.assert(fc.property(scenario, (sc) => {
    const sim = simulate(sc);
    const exact = feesFromOrcaSnapshot(snapOf(sim.now, sim.position), POOL, 0n);
    for (const { before, after } of sim.history) {
      assert.ok(feesFromOrcaSnapshot(snapOf(after, sim.position), POOL, 0n).ok);
      for (const st of [{ whirlpool: after.whirlpool, lower: before.lower, upper: before.upper },
                        { whirlpool: before.whirlpool, lower: after.lower, upper: after.upper }]) {
        const r = feesFromOrcaSnapshot(snapOf(st, sim.position), POOL, 0n);
        // A refused read falls back to the settled fee. An accepted mixed read
        // is NOT always exact: fast-check found one passing every invariant
        // with fee 0 instead of 1 (lo 0, hi 4, L 1, one 2^64 growth, cross
        // to 4). Only the one-call read excludes mixing; the Python guard and
        // the transaction-measured harvest stand behind it.
        if (!r.ok) { assert.equal(r.feeA, 0n); assert.equal(r.feeB, 0n); }
        assert.ok(r.feeA >= 0n && r.feeB >= 0n && r.feeA < U64 && r.feeB < U64);
      }
    }
    assert.ok(exact.ok);
  }), { numRuns: 200 });
});

test('growthInside below, inside and above the band', () => {
  // outside growth 10 at the lower tick, 3 at the upper, global 100
  assert.equal(growthInside(5, 100n, 10n, 3n, 0, 10), 87n);          // inside: 100 - 10 - 3
  assert.equal(growthInside(-1, 100n, 10n, 3n, 0, 10), mod(100n - 90n - 3n));
  assert.equal(growthInside(10, 100n, 10n, 3n, 0, 10), mod(100n - 10n - 97n));
  assert.equal(growthInside(0, 100n, 10n, 3n, 0, 10), 87n);          // the lower tick is inside
});

// --- the invariants, one at a time -------------------------------------------------

function good() {
  const sim = simulate({ lo: 0, hi: 40, L: 1_000_000n, start: 20, gA0: 7n, gB0: 9n,
                         steps: [{ kind: 'fee', a: 1n << 64n, b: 1n << 65n }] });
  const own = ownFees(sim.now.whirlpool, sim.position, sim.now.lower, sim.now.upper);
  return { whirlpoolAddress: POOL, positionWhirlpool: POOL, position: sim.position, lower: sim.now.lower,
           upper: sim.now.upper, own, quote: { feeOwedA: own.feeA, feeOwedB: own.feeB }, transferFees: false };
}

test('checkOrca accepts a good read and refuses each broken invariant', () => {
  assert.equal(checkOrca(good()), null);
  const g = good;
  assert.match(checkOrca({ ...g(), positionWhirlpool: OTHER }), /belongs to whirlpool/);
  assert.equal(checkOrca({ ...g(), whirlpoolAddress: null, positionWhirlpool: OTHER }), null);
  assert.match(checkOrca({ ...g(), lower: null }), /unreadable/);
  assert.match(checkOrca({ ...g(), upper: null }), /unreadable/);
  assert.match(checkOrca({ ...g(), lower: { ...g().lower, initialized: false } }), /not initialised/);
  assert.match(checkOrca({ ...g(), upper: { ...g().upper, initialized: false } }), /not initialised/);
  assert.match(checkOrca({ ...g(), lower: { ...g().lower, liquidityGross: 0n } }), /not initialised/);
  assert.match(checkOrca({ ...g(), upper: { ...g().upper, liquidityGross: 0n } }), /not initialised/);
  assert.equal(checkOrca({ ...g(), position: { ...g().position, liquidity: 0n }, lower: { ...g().lower, initialized: false } }), null);
  assert.match(checkOrca({ ...g(), own: { ...g().own, deltaA: HALF_U128 } }), /backwards/);
  assert.match(checkOrca({ ...g(), own: { ...g().own, deltaB: HALF_U128 } }), /backwards/);
  assert.equal(checkOrca({ ...g(), own: { ...g().own, deltaA: HALF_U128 - 1n, deltaB: HALF_U128 - 1n } }), null);
  const big = { ...g(), own: { ...g().own, feeA: U64 }, quote: { feeOwedA: U64, feeOwedB: g().own.feeB } };
  assert.match(checkOrca(big), /u64/);
  assert.match(checkOrca({ ...g(), own: { ...g().own, feeB: U64 }, quote: { feeOwedA: g().own.feeA, feeOwedB: U64 } }), /u64/);
  assert.match(checkOrca({ ...g(), quote: { feeOwedA: g().own.feeA + 1n, feeOwedB: g().own.feeB } }), /disagrees/);
  assert.match(checkOrca({ ...g(), quote: { feeOwedA: g().own.feeA, feeOwedB: g().own.feeB - 1n } }), /disagrees/);
  // with a transfer fee the quote may be lower, never higher
  assert.equal(checkOrca({ ...g(), transferFees: true, quote: { feeOwedA: g().own.feeA - 5n, feeOwedB: g().own.feeB } }), null);
  assert.match(checkOrca({ ...g(), transferFees: true, quote: { feeOwedA: g().own.feeA + 1n, feeOwedB: g().own.feeB } }), /exceeds/);
  assert.match(checkOrca({ ...g(), transferFees: true, quote: { feeOwedA: g().own.feeA, feeOwedB: g().own.feeB + 1n } }), /exceeds/);
  assert.match(checkOrca({ ...g(), transferFees: true, quote: { feeOwedA: -1n, feeOwedB: 0n } }), /exceeds/);
  assert.match(checkOrca({ ...g(), transferFees: true, quote: { feeOwedA: 0n, feeOwedB: -1n } }), /exceeds/);
});

test('ownFees adds the settled feeOwed and floors at 2^64', () => {
  const w = { tickCurrentIndex: 5, feeGrowthGlobalA: 3n << 64n, feeGrowthGlobalB: 0n };
  const t = { feeGrowthOutsideA: 0n, feeGrowthOutsideB: 0n };
  const p = { tickLowerIndex: 0, tickUpperIndex: 10, liquidity: 2n, feeGrowthCheckpointA: 1n << 64n,
              feeGrowthCheckpointB: 0n, feeOwedA: 7n, feeOwedB: 1n };
  const f = ownFees(w, p, t, t);
  assert.equal(f.feeA, 7n + 4n); assert.equal(f.feeB, 1n); assert.equal(f.deltaA, 2n << 64n);
  assert.equal(ownFees(w, { ...p, liquidity: 1n, feeGrowthCheckpointA: (3n << 64n) - 1n }, t, t).feeA, 7n);
});

test('transferFeeOf picks the fee of the epoch and nothing for plain mints', () => {
  const cfg = { __kind: 'TransferFeeConfig', olderTransferFee: { epoch: 0n, transferFeeBasisPoints: 10, maximumFee: 5n },
                newerTransferFee: { epoch: 100n, transferFeeBasisPoints: 20, maximumFee: 9n } };
  const mint = { data: { extensions: { __option: 'Some', value: [cfg] } } };
  assert.deepEqual(transferFeeOf(mint, 99n), { feeBps: 10, maxFee: 5n });
  assert.deepEqual(transferFeeOf(mint, 100n), { feeBps: 20, maxFee: 9n });
  assert.equal(transferFeeOf(null, 1n), undefined);
  assert.equal(transferFeeOf({ data: { extensions: { __option: 'None' } } }, 1n), undefined);
  assert.equal(transferFeeOf({ data: { extensions: { __option: 'Some', value: [{ __kind: 'Other' }] } } }, 1n), undefined);
});

test('a tick array that starts elsewhere or is missing is unreadable', () => {
  const sim = simulate({ lo: 0, hi: 40, L: 1n, start: 20, gA0: 0n, gB0: 0n, steps: [] });
  const s = snapOf(sim.now, sim.position);
  assert.equal(feesFromOrcaSnapshot({ ...s, lowerArray: null }, POOL, 0n).ok, false);
  assert.equal(feesFromOrcaSnapshot({ ...s, upperArray: null }, POOL, 0n).ok, false);
  assert.equal(feesFromOrcaSnapshot({ ...s, lowerArray: { ...s.lowerArray, startTickIndex: 352 } }, POOL, 0n).ok, false);
  assert.equal(feesFromOrcaSnapshot({ ...s, upperArray: { ...s.upperArray, ticks: [] } }, POOL, 0n).ok, false);
});

// --- orchestration --------------------------------------------------------------------

function reader(snaps) {
  const calls = [];
  const read = async (_rpc, addrs) => {
    calls.push(addrs);
    if (!addrs.lowerArray) return snaps[0];                 // the address read
    return snaps[Math.min(calls.length - 1, snaps.length - 1)];
  };
  return { read, calls };
}

const replayOrca = () => simulate({ lo: 0, hi: 40, L: 50_000_000_000n, start: 20, gA0: 1n << 100n, gB0: 3n << 100n,
                                    steps: [{ kind: 'fee', a: 1n << 40n, b: 1n << 42n }, { kind: 'move', to: -4 }] });

test('one snapshot read with every account, and the true fee', async () => {
  const sim = replayOrca();
  const s = snapOf(sim.history[0].after, sim.position);
  const { read, calls } = reader([s, s]);
  const r = await consistentOrcaFees({}, MINT, POOL, { read, epoch: 0n, pauseMs: 0 });
  assert.ok(r.ok); assert.equal(r.feeA, sim.trueA); assert.equal(r.feeB, sim.trueB);
  assert.equal(calls.length, 2);                            // the address read, then ONE snapshot
  const snapRead = calls[1];
  assert.deepEqual(Object.keys(snapRead).sort(), ['lowerArray', 'mintA', 'mintB', 'position', 'upperArray', 'whirlpool']);
  assert.equal(snapRead.whirlpool, POOL);
  const want = await snapshotAddresses(s.position, s.whirlpool, POOL);
  assert.equal(snapRead.lowerArray, want.lowerArray); assert.equal(snapRead.upperArray, want.upperArray);
});

test('a bad snapshot is retried; one that stays bad is ok:false with the settled fee', async () => {
  const sim = replayOrca();
  const { before, after } = sim.history[0];
  const good = snapOf(after, sim.position);
  const bad = snapOf({ whirlpool: after.whirlpool, lower: before.lower, upper: before.upper }, sim.position);
  const r1 = await consistentOrcaFees({}, MINT, POOL, { read: reader([good, bad, good]).read, epoch: 0n, pauseMs: 0 });
  assert.ok(r1.ok); assert.equal(r1.feeA, sim.trueA);
  const { read, calls } = reader([good, bad]);
  const r2 = await consistentOrcaFees({}, MINT, POOL, { read, epoch: 0n, pauseMs: 0, tries: 3 });
  assert.equal(r2.ok, false); assert.equal(calls.length, 4); assert.equal(r2.feeA, 0n);
});

test('the epoch is read from the chain when not given', async () => {
  const sim = replayOrca();
  const s = snapOf(sim.history[0].after, sim.position);
  let asked = 0;
  const rpc = { getEpochInfo: () => ({ send: async () => { asked += 1; return { epoch: 5n }; } }) };
  const r = await consistentOrcaFees(rpc, MINT, POOL, { read: reader([s, s]).read, pauseMs: 0 });
  assert.ok(r.ok); assert.equal(asked, 1);
});

test('the counterexample: invariants alone cannot see every mixed read', () => {
  const sim = simulate({ lo: 0, hi: 4, L: 1n, start: 0, gA0: 0n, gB0: 0n,
                         steps: [{ kind: 'fee', a: 0n, b: 1n << 64n }, { kind: 'move', to: 4 }] });
  const { before, after } = sim.history[0];
  const truth = feesFromOrcaSnapshot(snapOf(after, sim.position), POOL, 0n);
  const mixed = feesFromOrcaSnapshot(snapOf({ whirlpool: before.whirlpool, lower: after.lower, upper: after.upper }, sim.position), POOL, 0n);
  assert.equal(truth.feeB, 1n);
  assert.ok(mixed.ok && mixed.feeB !== truth.feeB);     // why the snapshot must come from one call
});

test('each refusal path says so and gives ok:false', () => {
  const sim = replayOrca();
  const { before, after } = sim.history[0];
  // our own invariants refuse first: an uninitialised boundary tick
  const unin = snapOf({ ...after, lower: { ...after.lower, initialized: false } }, sim.position);
  const r1 = feesFromOrcaSnapshot(unin, POOL, 0n);
  assert.equal(r1.ok, false); assert.match(r1.reason, /not initialised/);
  // the incident's mixed read: refused, whichever check sees it first
  const r2 = feesFromOrcaSnapshot(snapOf({ whirlpool: after.whirlpool, lower: before.lower, upper: before.upper }, sim.position), POOL, 0n);
  assert.equal(r2.ok, false); assert.ok(r2.reason);
  // the SDK quote throwing is a refusal, not a crash
  const broken = snapOf(after, sim.position);
  broken.whirlpool = { ...broken.whirlpool, rewardInfos: [] };          // the wasm facade refuses this shape
  const r3 = feesFromOrcaSnapshot(broken, POOL, 0n);
  assert.equal(r3.ok, false); assert.match(r3.reason, /SDK quote failed/);
});

test('a transfer fee on one mint only lowers that side and still passes', () => {
  const sim = replayOrca();
  const cfg = { __kind: 'TransferFeeConfig', olderTransferFee: { epoch: 0n, transferFeeBasisPoints: 100, maximumFee: 1n << 60n },
                newerTransferFee: { epoch: 0n, transferFeeBasisPoints: 100, maximumFee: 1n << 60n } };
  const withFee = { data: { extensions: { __option: 'Some', value: [cfg] } } };
  for (const side of ['mintA', 'mintB']) {
    const r = feesFromOrcaSnapshot(snapOf(sim.history[0].after, sim.position, { [side]: withFee }), POOL, 0n);
    assert.equal(r.reason, null, side); assert.ok(r.ok);
    if (side === 'mintA') { assert.ok(r.feeA < sim.trueA); assert.equal(r.feeB, sim.trueB); }
    else { assert.equal(r.feeA, sim.trueA); assert.ok(r.feeB < sim.trueB); }
  }
});

test('an SDK quote that disagrees with the arithmetic is refused', () => {
  const sim = replayOrca();
  const s = snapOf(sim.history[0].after, sim.position);
  const off = () => ({ feeOwedA: sim.trueA + 1n, feeOwedB: sim.trueB });
  const r = feesFromOrcaSnapshot(s, POOL, 0n, off);
  assert.equal(r.ok, false); assert.match(r.reason, /disagrees/);
  assert.equal(r.feeA, 0n);                                  // the settled fee, not the quote
  const same = () => ({ feeOwedA: sim.trueA, feeOwedB: sim.trueB });
  assert.ok(feesFromOrcaSnapshot(s, POOL, 0n, same).ok);
});
