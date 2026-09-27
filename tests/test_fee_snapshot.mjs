// fee_snapshot.mjs: the fee read of Raydium-layout CLMM positions.
//
//   * a simulator of the program's fee-growth bookkeeping (global growth,
//     the boundary ticks' "outside" growth that flips on every crossing)
//     gives the true fee of any history; property tests check that a
//     CONSISTENT snapshot reproduces it exactly and passes the invariants;
//   * the 2026-09-27 failure is replayed: pool state after a crossing, tick
//     state before it, which is what three separate RPC reads produced;
//   * consistentFees is driven through a fake RPC with byte-exact accounts:
//     one call per try, retried on a bad read, `ok: false` when it stays bad.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fc from 'fast-check';
import {
  wrappingSubU128, checkFees, feesFromSnapshot, consistentFees, snapshotKeys, U128, HALF_U128,
} from '../fee_snapshot.mjs';

const require = createRequire(import.meta.url);
const BN = require('bn.js');
const { PublicKey } = require('@solana/web3.js');
const {
  PoolInfoLayout, PersonalPositionLayout, TickArrayLayout, TickArrayUtil,
} = require('@raydium-io/raydium-sdk-v2');

const Q64 = new BN(1).shln(64);
const PROGRAM = new PublicKey('CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK');
const POOL = new PublicKey('8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj');
const NFT = new PublicKey('AqSDWeZCnBmiQmBrfAbdzgBasZY7wW7r5pGepH6nF9U1');
const SPACING = 1;

// --- the simulator ----------------------------------------------------------------
// One position [lo, hi) with liquidity L. Only its two boundary ticks matter.
// Growth is per unit of liquidity, X64, and wraps at 2^128 like the program's.
function simulate({ lo, hi, L, start, steps, globalA0, globalB0 }) {
  const mod = (x) => x.umod(U128);
  let t = start, gA = new BN(globalA0), gB = new BN(globalB0);
  // A tick initialised while the price is at or above it starts with
  // outside = global (all growth so far counted "below" it).
  const init = (tick) => (t >= tick ? { a: gA.clone(), b: gB.clone() } : { a: new BN(0), b: new BN(0) });
  const out = { lo: init(lo), hi: init(hi) };
  const inside = () => {
    const below = t >= lo ? out.lo : { a: mod(gA.sub(out.lo.a)), b: mod(gB.sub(out.lo.b)) };
    const above = t < hi ? out.hi : { a: mod(gA.sub(out.hi.a)), b: mod(gB.sub(out.hi.b)) };
    return { a: mod(gA.sub(below.a).sub(above.a)), b: mod(gB.sub(below.b).sub(above.b)) };
  };
  const last = inside();
  let earnedA = new BN(0), earnedB = new BN(0);     // growth accrued while in range
  const history = [];
  for (const s of steps) {
    if (s.kind === 'fee') {
      gA = mod(gA.add(new BN(s.a))); gB = mod(gB.add(new BN(s.b)));
      if (lo <= t && t < hi) { earnedA = earnedA.add(new BN(s.a)); earnedB = earnedB.add(new BN(s.b)); }
    } else {
      const before = snapshot();
      // crossing a tick flips its outside growth: outside = global - outside
      for (const [k, tick] of [['lo', lo], ['hi', hi]]) {
        const crosses = (t < tick && s.to >= tick) || (t >= tick && s.to < tick);
        if (crosses) out[k] = { a: mod(gA.sub(out[k].a)), b: mod(gB.sub(out[k].b)) };
      }
      t = s.to;
      history.push({ before, after: snapshot() });
    }
  }
  function snapshot() {
    return {
      pool: { tickCurrent: t, feeGrowthGlobalX64A: gA.clone(), feeGrowthGlobalX64B: gB.clone() },
      lower: { tick: lo, liquidityGross: new BN(L), feeGrowthOutsideX64A: out.lo.a.clone(), feeGrowthOutsideX64B: out.lo.b.clone() },
      upper: { tick: hi, liquidityGross: new BN(L), feeGrowthOutsideX64A: out.hi.a.clone(), feeGrowthOutsideX64B: out.hi.b.clone() },
    };
  }
  const position = {
    poolId: POOL, tickLower: lo, tickUpper: hi, liquidity: new BN(L),
    feeGrowthInsideLastX64A: last.a, feeGrowthInsideLastX64B: last.b,
    tokenFeesOwedA: new BN(0), tokenFeesOwedB: new BN(0),
  };
  return {
    now: snapshot(), position, history,
    trueFeeA: earnedA.mul(new BN(L)).div(Q64), trueFeeB: earnedB.mul(new BN(L)).div(Q64),
  };
}

const u128 = fc.bigInt({ min: 0n, max: (1n << 128n) - 1n }).map(x => new BN(x.toString()));
const growth = fc.bigInt({ min: 0n, max: 1n << 70n }).map(x => new BN(x.toString()));
const scenario = fc.record({
  lo: fc.integer({ min: -50, max: 0 }),
  width: fc.integer({ min: 1, max: 40 }),
  L: fc.bigInt({ min: 1n, max: 1n << 40n }).map(String),
  startOff: fc.integer({ min: -60, max: 100 }),
  globalA0: u128, globalB0: u128,
  steps: fc.array(fc.oneof(
    fc.record({ kind: fc.constant('fee'), a: growth, b: growth }),
    fc.record({ kind: fc.constant('move'), to: fc.integer({ min: -120, max: 120 }) })), { maxLength: 40 }),
}).map(s => ({ lo: s.lo, hi: s.lo + s.width, L: s.L, start: s.lo + s.startOff,
               steps: s.steps, globalA0: s.globalA0, globalB0: s.globalB0 }));

// --- wrapping arithmetic ------------------------------------------------------------

test('wrappingSubU128 is subtraction modulo 2^128', () => {
  fc.assert(fc.property(u128, u128, (a, b) => {
    const d = wrappingSubU128(a, b);
    assert.ok(!d.isNeg() && d.lt(U128));
    assert.ok(d.add(b).umod(U128).eq(a));            // (a - b) + b == a  (mod 2^128)
  }), { numRuns: 500 });
  assert.ok(wrappingSubU128(new BN(0), new BN(1)).eq(U128.subn(1)));
  assert.ok(wrappingSubU128(new BN(5), new BN(5)).isZero());
});

// --- consistent snapshots -----------------------------------------------------------

test('a consistent snapshot gives the true fee, exactly, and passes every invariant', () => {
  fc.assert(fc.property(scenario, (sc) => {
    const sim = simulate(sc);
    const r = feesFromSnapshot(POOL.toBase58(), sim.now.pool, sim.position, sim.now.lower, sim.now.upper);
    assert.equal(r.reason, null, `rejected a consistent read: ${r.reason}`);
    assert.ok(r.ok);
    assert.equal(r.feeA.toString(), sim.trueFeeA.toString());
    assert.equal(r.feeB.toString(), sim.trueFeeB.toString());
  }), { numRuns: 1000 });
});

test('fees never fall as fee growth accrues, in range or out', () => {
  fc.assert(fc.property(scenario, growth, growth, (sc, a, b) => {
    const s0 = simulate(sc);
    const s1 = simulate({ ...sc, steps: [...sc.steps, { kind: 'fee', a, b }] });
    const f0 = feesFromSnapshot(null, s0.now.pool, s0.position, s0.now.lower, s0.now.upper);
    const f1 = feesFromSnapshot(null, s1.now.pool, s1.position, s1.now.lower, s1.now.upper);
    assert.ok(f1.feeA.gte(f0.feeA) && f1.feeB.gte(f0.feeB));
  }), { numRuns: 500 });
});

// --- the 2026-09-27 failure ---------------------------------------------------------

test('replay: pool read after a crossing with tick arrays read before it inflates the fee', () => {
  // The position is in range and earning; then the price drops through the
  // lower tick. The pool account is read AFTER the crossing, the tick array
  // BEFORE it: the shape of the three separate reads.
  const G = new BN(1).shln(100);                    // a pool with a long fee history
  const sim = simulate({
    lo: 0, hi: 10, L: String(5e10), start: 5, globalA0: G, globalB0: G.muln(3),
    steps: [{ kind: 'fee', a: new BN(1).shln(40), b: new BN(1).shln(42) }, { kind: 'move', to: -1 }],
  });
  const { before, after } = sim.history[0];
  const truth = feesFromSnapshot(POOL.toBase58(), after.pool, sim.position, after.lower, after.upper);
  assert.ok(truth.ok);
  assert.equal(truth.feeA.toString(), sim.trueFeeA.toString());
  const mixed = feesFromSnapshot(POOL.toBase58(), after.pool, sim.position, before.lower, before.upper);
  // The mixed read is wrong, and its error is the whole pool's history.
  assert.ok(!mixed.feeA.eq(sim.trueFeeA));
  assert.ok(!mixed.ok, 'the invariants must reject this mixed read');
});

test('property: a mixed read is refused, or passes with a non-negative fee (the one-call read is what excludes it)', () => {
  // Pool state and tick state from the two sides of one crossing, both ways.
  fc.assert(fc.property(scenario, (sc) => {
    const sim = simulate(sc);
    for (const { before, after } of sim.history) {
      for (const [pool, ticks] of [[after.pool, before], [before.pool, after]]) {
        const mixed = feesFromSnapshot(POOL.toBase58(), pool, sim.position, ticks.lower, ticks.upper);
        const exact = feesFromSnapshot(POOL.toBase58(), after.pool, sim.position, after.lower, after.upper);
        // Not always exact when accepted: fast-check found a mixed read that
        // passes every invariant with fee 0 instead of 1. Only the one-call
        // read excludes mixing; the Python layers stand behind it.
        assert.ok(!mixed.feeA.isNeg() && !mixed.feeB.isNeg());
        if (!mixed.ok) assert.ok(mixed.reason);
      }
    }
  }), { numRuns: 300 });
});

// --- checkFees, one invariant at a time ----------------------------------------------

function goodCase() {
  const sim = simulate({ lo: 0, hi: 10, L: '1000000', start: 5, globalA0: new BN(7), globalB0: new BN(9),
                         steps: [{ kind: 'fee', a: new BN(1).shln(64), b: new BN(1).shln(65) }] });
  return { sim, args: { poolId: POOL.toBase58(), position: sim.position, lower: sim.now.lower, upper: sim.now.upper,
                        feeA: sim.trueFeeA, feeB: sim.trueFeeB, growthDeltaA: new BN(1).shln(64),
                        growthDeltaB: new BN(1).shln(65) } };
}

test('checkFees accepts a good read', () => {
  assert.equal(checkFees(goodCase().args), null);
});

test('checkFees rejects each broken invariant', () => {
  const g = () => goodCase().args;
  const other = new PublicKey('4QU2NpRaqmKMvPSwVKQDeW4V6JFEKJdkzbzdauumD9qN');
  assert.match(checkFees({ ...g(), position: { ...g().position, poolId: other } }), /belongs to pool/);
  assert.match(checkFees({ ...g(), lower: null }), /unreadable/);
  assert.match(checkFees({ ...g(), upper: null }), /unreadable/);
  assert.match(checkFees({ ...g(), lower: { ...g().lower, tick: 1 } }), /boundary ticks read as/);
  assert.match(checkFees({ ...g(), upper: { ...g().upper, tick: 11 } }), /boundary ticks read as/);
  assert.match(checkFees({ ...g(), lower: { ...g().lower, liquidityGross: new BN(0) } }), /not initialised/);
  assert.match(checkFees({ ...g(), upper: { ...g().upper, liquidityGross: new BN(0) } }), /not initialised/);
  assert.match(checkFees({ ...g(), growthDeltaA: HALF_U128 }), /went backwards/);
  assert.match(checkFees({ ...g(), growthDeltaB: HALF_U128 }), /went backwards/);
  assert.match(checkFees({ ...g(), feeA: Q64 }), /u64/);
  assert.match(checkFees({ ...g(), feeB: Q64 }), /u64/);
  assert.match(checkFees({ ...g(), feeA: new BN(-1) }), /u64/);
  // the boundary just below each limit passes
  assert.equal(checkFees({ ...g(), growthDeltaA: HALF_U128.subn(1), growthDeltaB: HALF_U128.subn(1),
                           feeA: Q64.subn(1), feeB: Q64.subn(1) }), null);
  // a position with no liquidity may sit on uninitialised ticks
  assert.equal(checkFees({ ...g(), position: { ...g().position, liquidity: new BN(0) },
                           lower: { ...g().lower, liquidityGross: new BN(0) } }), null);
});

test('feesFromSnapshot falls back to tokenFeesOwed when a tick is missing', () => {
  const { sim } = goodCase();
  const p = { ...sim.position, tokenFeesOwedA: new BN(11), tokenFeesOwedB: new BN(12) };
  const r = feesFromSnapshot(POOL.toBase58(), sim.now.pool, p, null, sim.now.upper);
  assert.equal(r.ok, false);
  assert.equal(r.feeA.toString(), '11'); assert.equal(r.feeB.toString(), '12');
});

// --- consistentFees through a fake RPC ----------------------------------------------

function encode(layout, fields) {
  const z = layout.decode(Buffer.alloc(layout.span));
  const b = Buffer.alloc(layout.span);
  layout.encode({ ...z, ...fields }, b);
  return b;
}

function accountsFor(snap, position) {
  const pool = encode(PoolInfoLayout, { tickSpacing: SPACING, ...snap.pool });
  const pos = encode(PersonalPositionLayout, { nftMint: NFT, ...position });
  const start = TickArrayUtil.getTickArrayStartIndex(position.tickLower, SPACING);
  const z = TickArrayLayout.decode(Buffer.alloc(TickArrayLayout.span));
  const ticks = z.ticks.map(t => ({ ...t }));
  for (const tk of [snap.lower, snap.upper]) {
    const i = TickArrayUtil.getTickOffsetInArray(tk.tick, SPACING);
    ticks[i] = { ...ticks[i], ...tk };
  }
  const arr = Buffer.alloc(TickArrayLayout.span);
  TickArrayLayout.encode({ ...z, poolId: POOL, startTickIndex: start, ticks }, arr);
  const acct = (data) => ({ owner: PROGRAM, data, lamports: 1, executable: false });
  return [acct(pool), acct(pos), acct(arr)];
}

function fakeConnection(answers) {
  const calls = [];
  return {
    calls,
    async getMultipleAccountsInfoAndContext(keys) {
      calls.push(keys.map(k => k.toBase58()));
      const value = answers[Math.min(calls.length - 1, answers.length - 1)];
      return { context: { slot: 1000 + calls.length }, value };
    },
    // any other read would be a split read: fail loudly
    async getAccountInfo() { throw new Error('split read'); },
    async getMultipleAccountsInfo() { throw new Error('split read'); },
  };
}

const replay = () => simulate({
  lo: 0, hi: 10, L: String(5e10), start: 5, globalA0: new BN(1).shln(100), globalB0: new BN(3).shln(100),
  steps: [{ kind: 'fee', a: new BN(1).shln(40), b: new BN(1).shln(42) }, { kind: 'move', to: -1 }],
});

test('consistentFees reads pool, position and tick arrays in ONE call and returns the true fee', async () => {
  const sim = replay();
  const conn = fakeConnection([accountsFor(sim.history[0].after, sim.position)]);
  const r = await consistentFees(conn, PROGRAM, POOL, [{ nftMint: NFT, tickLower: 0, tickUpper: 10 }], SPACING, { pauseMs: 0 });
  assert.ok(r.ok);
  assert.equal(conn.calls.length, 1);
  const { keys } = snapshotKeys(PROGRAM, POOL, [NFT], [[0, 10]], SPACING);
  assert.deepEqual(conn.calls[0], keys.map(k => k.toBase58()));
  assert.equal(conn.calls[0][0], POOL.toBase58());
  assert.equal(r.fees[0].feeA.toString(), sim.trueFeeA.toString());
  assert.equal(r.fees[0].feeB.toString(), sim.trueFeeB.toString());
  assert.equal(r.slot, 1001);
});

test('consistentFees retries a bad read and takes the next good one', async () => {
  const sim = replay();
  const { before, after } = sim.history[0];
  const bad = accountsFor({ pool: after.pool, lower: before.lower, upper: before.upper }, sim.position);
  const good = accountsFor(after, sim.position);
  const conn = fakeConnection([bad, good]);
  const r = await consistentFees(conn, PROGRAM, POOL, [{ nftMint: NFT, tickLower: 0, tickUpper: 10 }], SPACING, { pauseMs: 0 });
  assert.ok(r.ok);
  assert.equal(conn.calls.length, 2);
  assert.equal(r.fees[0].feeA.toString(), sim.trueFeeA.toString());
});

test('consistentFees gives ok:false after every try fails, never a figure marked good', async () => {
  const sim = replay();
  const { before, after } = sim.history[0];
  const bad = accountsFor({ pool: after.pool, lower: before.lower, upper: before.upper }, sim.position);
  const conn = fakeConnection([bad]);
  const r = await consistentFees(conn, PROGRAM, POOL, [{ nftMint: NFT, tickLower: 0, tickUpper: 10 }], SPACING, { tries: 3, pauseMs: 0 });
  assert.equal(r.ok, false);
  assert.ok(r.reason);
  assert.equal(conn.calls.length, 3);
});

test('consistentFees refuses accounts owned by another program', async () => {
  const sim = replay();
  const accs = accountsFor(sim.history[0].after, sim.position);
  const foreign = new PublicKey('4QU2NpRaqmKMvPSwVKQDeW4V6JFEKJdkzbzdauumD9qN');
  await assert.rejects(consistentFees(fakeConnection([[{ ...accs[0], owner: foreign }, accs[1], accs[2]]]),
    PROGRAM, POOL, [{ nftMint: NFT, tickLower: 0, tickUpper: 10 }], SPACING, { pauseMs: 0 }), /pool account/);
  await assert.rejects(consistentFees(fakeConnection([[accs[0], { ...accs[1], owner: foreign }, accs[2]]]),
    PROGRAM, POOL, [{ nftMint: NFT, tickLower: 0, tickUpper: 10 }], SPACING, { pauseMs: 0 }), /position/);
  // a tick array owned elsewhere is not read: the fee falls back, marked not ok
  const r = await consistentFees(fakeConnection([[accs[0], accs[1], { ...accs[2], owner: foreign }]]),
    PROGRAM, POOL, [{ nftMint: NFT, tickLower: 0, tickUpper: 10 }], SPACING, { tries: 1, pauseMs: 0 });
  assert.equal(r.ok, false);
});

// --- the Byreal adapter ---------------------------------------------------------------
import { BYREAL_LAYOUT, RAYDIUM_LAYOUT } from '../fee_snapshot.mjs';
const by = require('@byreal-io/byreal-clmm-sdk');
const BY_PROGRAM = by.BYREAL_CLMM_PROGRAM_ID;
const BY_POOL = new PublicKey('9GTj99g9tbz9U6UYDsX6YeRTgUnkYG6GTnHv3qLa5aXq');

function byAccounts(snap, position, { positionBytes = 281 } = {}) {
  const enc = (layout, fields, size = layout.span) => {
    const z = layout.decode(Buffer.alloc(layout.span));
    const b = Buffer.alloc(layout.span);
    layout.encode({ ...z, ...fields }, b);
    return Buffer.concat([b, Buffer.alloc(Math.max(0, size - layout.span))]).subarray(0, size);   // padding as on chain
  };
  const pool = enc(by.PoolLayout, { tickSpacing: SPACING, ...snap.pool });
  const pos = enc(by.PersonalPositionLayout, { nftMint: NFT, ...position, poolId: BY_POOL }, positionBytes);
  const z = by.TickArrayLayout.decode(Buffer.alloc(by.TickArrayLayout.span));
  const ticks = z.ticks.map(t => ({ ...t }));
  for (const tk of [snap.lower, snap.upper]) {
    const i = by.TickUtils.getTickOffsetInArray(tk.tick, SPACING);
    ticks[i] = { ...ticks[i], ...tk };
  }
  const start = by.TickUtils.getTickArrayStartIndexByTick(position.tickLower, SPACING);
  const arr = Buffer.alloc(by.TickArrayLayout.span);
  by.TickArrayLayout.encode({ ...z, poolId: BY_POOL, startTickIndex: start, ticks }, arr);
  by.TickArrayUtils.FIXED_TICK_ARRAY_DISCRIMINATOR.copy(arr, 0);
  const acct = (data) => ({ owner: BY_PROGRAM, data, lamports: 1, executable: false });
  return [acct(pool), acct(pos), acct(arr)];
}

const BY_LIST = [{ nftMint: NFT, tickLower: 0, tickUpper: 10 }];

test('Byreal: one call, Byreal addresses, the true fee from a 281-byte padded position', async () => {
  const sim = replay();
  const conn = fakeConnection([byAccounts(sim.history[0].after, sim.position)]);
  const r = await consistentFees(conn, BY_PROGRAM, BY_POOL, BY_LIST, SPACING, { pauseMs: 0 }, BYREAL_LAYOUT);
  assert.equal(r.reason, null); assert.ok(r.ok);
  assert.equal(conn.calls.length, 1);
  const { keys } = snapshotKeys(BY_PROGRAM, BY_POOL, [NFT], [[0, 10]], SPACING, BYREAL_LAYOUT);
  assert.deepEqual(conn.calls[0], keys.map(k => k.toBase58()));
  assert.equal(conn.calls[0][1], by.getPdaPersonalPositionAddress(BY_PROGRAM, NFT).publicKey.toBase58());
  assert.equal(r.fees[0].feeA.toString(), sim.trueFeeA.toString());
  assert.equal(r.fees[0].feeB.toString(), sim.trueFeeB.toString());
});

test('Byreal: the mixed read of the incident is retried, then refused', async () => {
  const sim = replay();
  const { before, after } = sim.history[0];
  const bad = byAccounts({ pool: after.pool, lower: before.lower, upper: before.upper }, sim.position);
  const good = byAccounts(after, sim.position);
  const r1 = await consistentFees(fakeConnection([bad, good]), BY_PROGRAM, BY_POOL, BY_LIST, SPACING, { pauseMs: 0 }, BYREAL_LAYOUT);
  assert.ok(r1.ok); assert.equal(r1.fees[0].feeA.toString(), sim.trueFeeA.toString());
  const r2 = await consistentFees(fakeConnection([bad]), BY_PROGRAM, BY_POOL, BY_LIST, SPACING, { pauseMs: 0, tries: 2 }, BYREAL_LAYOUT);
  assert.equal(r2.ok, false);
});

test('Byreal: a short position account or an unknown tick array is unreadable, never a figure', async () => {
  const sim = replay();
  const snap = sim.history[0].after;
  await assert.rejects(consistentFees(fakeConnection([byAccounts(snap, sim.position, { positionBytes: 200 })]),
    BY_PROGRAM, BY_POOL, BY_LIST, SPACING, { pauseMs: 0 }, BYREAL_LAYOUT), /position/);
  const accs = byAccounts(snap, sim.position);
  const garbage = Buffer.from(accs[2].data); garbage.fill(7, 0, 8);           // unknown discriminator
  const r = await consistentFees(fakeConnection([[accs[0], accs[1], { ...accs[2], data: garbage }]]),
    BY_PROGRAM, BY_POOL, BY_LIST, SPACING, { pauseMs: 0, tries: 1 }, BYREAL_LAYOUT);
  assert.equal(r.ok, false); assert.match(r.reason, /unreadable/);
  assert.equal(BYREAL_LAYOUT.decodePool(Buffer.alloc(100)), null);
  assert.equal(RAYDIUM_LAYOUT.decodePool(Buffer.alloc(100)), null);
  assert.equal(RAYDIUM_LAYOUT.decodePosition(Buffer.alloc(100)), null);
  assert.equal(RAYDIUM_LAYOUT.tickState(Buffer.alloc(100), null, 0, 1), null);
});

test('property: Byreal and Raydium adapters give the same fee for the same history', async () => {
  await fc.assert(fc.asyncProperty(scenario, async (sc) => {
    const sim = simulate(sc);
    // the Raydium byte layout needs the same tick array for both ticks; keep the band inside one array
    fc.pre(TickArrayUtil.getTickArrayStartIndex(sc.lo, SPACING) === TickArrayUtil.getTickArrayStartIndex(sc.hi, SPACING));
    fc.pre(by.TickUtils.getTickArrayStartIndexByTick(sc.lo, SPACING) === by.TickUtils.getTickArrayStartIndexByTick(sc.hi, SPACING));
    const list = [{ nftMint: NFT, tickLower: sc.lo, tickUpper: sc.hi }];
    const ry = await consistentFees(fakeConnection([accountsFor(sim.now, sim.position)]), PROGRAM, POOL, list, SPACING, { pauseMs: 0 });
    const br = await consistentFees(fakeConnection([byAccounts(sim.now, sim.position)]), BY_PROGRAM, BY_POOL, list, SPACING, { pauseMs: 0 }, BYREAL_LAYOUT);
    assert.ok(ry.ok && br.ok, `${ry.reason} / ${br.reason}`);
    assert.equal(br.fees[0].feeA.toString(), ry.fees[0].feeA.toString());
    assert.equal(br.fees[0].feeB.toString(), sim.trueFeeB.toString());
  }), { numRuns: 60 });
});
