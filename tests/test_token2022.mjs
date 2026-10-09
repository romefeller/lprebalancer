// shared/token2022.mjs: scaled UI amounts, pause and transfer hook (2026-10-01).
//
// The bot LPs tokenized stocks (MU, DJT, MSFTx), Token-2022 mints whose UI
// amount is raw / 10^decimals × a multiplier the issuer changes. Proven here:
//   - raw <-> UI round trips lose at most one raw unit, and a UI cap never
//     converts to more raw than it is worth;
//   - both conversions are monotone; multiplier 1 is the identity;
//   - newMultiplier takes over exactly at its timestamp (inclusive);
//   - paused and hooked mints refuse writes; unreadable extension data fails
//     loudly instead of reading as multiplier 1.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fc from 'fast-check';
import {
  effectiveMultiplier, mintFacts, readMints, rawToUi, uiToRaw, uiToNative, uiPrice,
  writeRefusal, assertWritable, mintFields, REFUSE_PAUSED, REFUSE_HOOK, TOKEN_2022_PROGRAM, TOKEN_PROGRAM,
} from '../shared/token2022.mjs';

const FIX = JSON.parse(fs.readFileSync(new URL('./fixtures_stocks_20261001.json', import.meta.url)));
const NOW = 1790885369;                   // 2026-10-01, after MSFTx's switch at 1787185800
const clone = (x) => JSON.parse(JSON.stringify(x));
const ext = (acct, name) => acct.data.parsed.info.extensions.find(e => e.extension === name);

const multiplier = fc.double({ min: 0.01, max: 100, noNaN: true });
const decimals = fc.integer({ min: 0, max: 9 });

test('recorded mints decode to the facts the chain shows', () => {
  const mu = mintFacts('MU', FIX.mints.MU, NOW);
  assert.deepEqual(mu, { mint: 'MU', programId: TOKEN_2022_PROGRAM, decimals: 6,
    multiplier: 1.0001069314314899, paused: false, transferHook: null });
  assert.equal(mintFacts('DJT', FIX.mints.DJT, NOW).multiplier, 1);
  const ms = mintFacts('MSFTx', FIX.mints.MSFTx, NOW);
  assert.equal(ms.decimals, 8);
  assert.equal(ms.multiplier, 1.0059033904787456);     // newMultiplier: its timestamp has passed
  const usdc = mintFacts('USDC', FIX.mints.USDC, NOW);
  assert.deepEqual([usdc.programId, usdc.multiplier, usdc.paused, usdc.transferHook], [TOKEN_PROGRAM, 1, false, null]);
});

test('newMultiplier takes over exactly at its timestamp', () => {
  const cfg = ext(FIX.mints.MSFTx, 'scaledUiAmountConfig').state;
  const at = cfg.newMultiplierEffectiveTimestamp;
  assert.equal(effectiveMultiplier(cfg, at - 1), 1.0045820905025638);
  assert.equal(effectiveMultiplier(cfg, at), 1.0059033904787456);
  assert.equal(effectiveMultiplier(cfg, at + 1), 1.0059033904787456);
  assert.equal(mintFacts('MSFTx', FIX.mints.MSFTx, at - 0.5).multiplier, 1.0045820905025638);
  fc.assert(fc.property(multiplier, multiplier, fc.integer({ min: 0, max: 4e9 }), fc.integer({ min: -1e6, max: 1e6 }),
    (m, next, ts, dt) => {
      const got = effectiveMultiplier({ multiplier: String(m), newMultiplier: String(next), newMultiplierEffectiveTimestamp: ts }, ts + dt);
      return got === (dt >= 0 ? next : m);
    }));
});

test('multiplier 1 is the identity', () => {
  fc.assert(fc.property(fc.integer({ min: 0, max: 2 ** 52 }), decimals, (raw, d) =>
    rawToUi(raw, d, 1) === raw / 10 ** d));
  fc.assert(fc.property(fc.double({ min: 0, max: 1e6, noNaN: true }), decimals, (ui, d) =>
    uiToRaw(ui, d, 1) === BigInt(Math.floor(ui * 10 ** d)) && uiToNative(ui, 1) === ui));
  fc.assert(fc.property(fc.double({ min: 1e-9, max: 1e9, noNaN: true }), p => uiPrice(p, 1, 1) === p));
});

test('raw -> UI -> raw loses at most one raw unit and never gains', () => {
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 15n }), decimals, multiplier, (raw, d, m) => {
    const back = uiToRaw(rawToUi(raw, d, m), d, m);
    return back <= raw && raw - back <= 1n;
  }), { numRuns: 2000 });
});

test('a UI cap converts to raw worth at most the cap', () => {
  fc.assert(fc.property(fc.double({ min: 0, max: 1e4, noNaN: true }), decimals, multiplier, (ui, d, m) => {
    const raw = uiToRaw(ui, d, m);
    // one more raw unit would exceed the cap; the raw itself does not (float slack 1e-12)
    return rawToUi(raw, d, m) <= ui * (1 + 1e-12) + 1e-12 && rawToUi(raw + 1n, d, m) > ui * (1 - 1e-12);
  }), { numRuns: 2000 });
});

test('both conversions are monotone', () => {
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 14n }), fc.bigInt({ min: 0n, max: 10n ** 6n }), decimals, multiplier,
    (raw, step, d, m) => rawToUi(raw, d, m) <= rawToUi(raw + step, d, m)));
  fc.assert(fc.property(fc.double({ min: 0, max: 1e4, noNaN: true }), fc.double({ min: 0, max: 1e3, noNaN: true }), decimals, multiplier,
    (ui, step, d, m) => uiToRaw(ui, d, m) <= uiToRaw(ui + step, d, m)));
});

test('uiPrice values UI amounts the same as the pool values raw ones', () => {
  // amountA_raw × price + amountB_raw, in B's raw-human units, equals the
  // UI amounts at the UI price, divided back by B's multiplier.
  fc.assert(fc.property(fc.double({ min: 0, max: 1e4, noNaN: true }), fc.double({ min: 0, max: 1e4, noNaN: true }),
    fc.double({ min: 1e-3, max: 1e4, noNaN: true }), multiplier, multiplier, (a, b, p, mA, mB) => {
      const native = a * p + b;
      const ui = (a * mA) * uiPrice(p, mA, mB) + b * mB;
      return Math.abs(ui / mB - native) <= 1e-9 * Math.max(1, native);
    }));
  assert.ok(Math.abs(uiPrice(516.2616982233687, 1.0059033904787456, 1) - 513.2318899707269) < 1e-9);
});

test('a paused mint refuses writes; reads still report it', () => {
  const acct = clone(FIX.mints.MU);
  ext(acct, 'pausableConfig').state.paused = true;
  const mu = mintFacts('MU', acct, NOW), usdc = mintFacts('USDC', FIX.mints.USDC, NOW);
  assert.equal(writeRefusal([mu, usdc]), REFUSE_PAUSED);
  assert.throws(() => assertWritable([usdc, mu]), { message: 'refused: mint paused' });
  assert.equal(mintFields(mu, usdc).paused, true);
  assert.equal(mintFields(mu, usdc).pausedA, true);
});

test('a transfer hook with a program refuses writes; a null program does not', () => {
  const acct = clone(FIX.mints.MSFTx);
  ext(acct, 'transferHook').state.programId = 'HookProgram1111111111111111111111111111111';
  const ms = mintFacts('MSFTx', acct, NOW), usdc = mintFacts('USDC', FIX.mints.USDC, NOW);
  assert.equal(ms.transferHook, 'HookProgram1111111111111111111111111111111');
  assert.throws(() => assertWritable([ms, usdc]), { message: 'refused: transfer hook' });
  assert.equal(writeRefusal([mintFacts('MSFTx', FIX.mints.MSFTx, NOW), usdc]), null);
  // paused wins when both hold: it is the one that fails for certain
  ext(acct, 'pausableConfig').state.paused = true;
  assert.equal(writeRefusal([mintFacts('MSFTx', acct, NOW)]), REFUSE_PAUSED);
  assert.equal(REFUSE_HOOK, 'refused: transfer hook');
});

test('missing or junk extension data fails loudly, never reads as multiplier 1', () => {
  const cases = [
    ['scaledUiAmountConfig state missing', a => { delete ext(a, 'scaledUiAmountConfig').state; }, /scaledUiAmountConfig unreadable/],
    ['multiplier not a number', a => { ext(a, 'scaledUiAmountConfig').state.multiplier = 'abc'; }, /scaledUiAmountConfig unreadable/],
    ['multiplier zero', a => { ext(a, 'scaledUiAmountConfig').state.newMultiplier = '0'; }, /scaledUiAmountConfig unreadable/],
    ['timestamp missing', a => { delete ext(a, 'scaledUiAmountConfig').state.newMultiplierEffectiveTimestamp; }, /scaledUiAmountConfig unreadable/],
    ['paused not a boolean', a => { ext(a, 'pausableConfig').state.paused = 'no'; }, /pausableConfig unreadable/],
    ['hook state missing', a => { delete ext(a, 'transferHook').state; }, /transferHook unreadable/],
    ['hook program not a string', a => { ext(a, 'transferHook').state.programId = 7; }, /transferHook unreadable/],
    ['extensions not a list', a => { a.data.parsed.info.extensions = {}; }, /extensions unreadable/],
    ['extension without a name', a => { a.data.parsed.info.extensions.push({ state: {} }); }, /without a name/],
    ['unparseable extension', a => { a.data.parsed.info.extensions.push({ extension: 'unparseableExtension' }); }, /unsupported extension/],
    ['interest-bearing', a => { a.data.parsed.info.extensions.push({ extension: 'interestBearingConfig', state: {} }); }, /unsupported extension/],
    ['decimals missing', a => { delete a.data.parsed.info.decimals; }, /decimals/],
    ['decimals fractional', a => { a.data.parsed.info.decimals = 6.5; }, /decimals/],
    ['decimals negative', a => { a.data.parsed.info.decimals = -1; }, /decimals/],
    ['decimals above 18', a => { a.data.parsed.info.decimals = 19; }, /decimals/],
    ['hook state without programId', a => { ext(a, 'transferHook').state = {}; }, /transferHook unreadable/],
    ['mint without info', a => { a.data.parsed.info = null; }, /no parsed mint data/],
    ['mint info not an object', a => { a.data.parsed.info = 'x'; }, /no parsed mint data/],
    ['not a mint', a => { a.data.parsed.type = 'account'; }, /no parsed mint data/],
    ['raw bytes instead of parsed', a => { a.data = ['AAAA', 'base64']; }, /no parsed mint data/],
    ['owner not a token program', a => { a.owner = '11111111111111111111111111111111'; }, /owner 1111.* is not a token program/],
    ['owner missing', a => { delete a.owner; }, /owner unknown is not a token program/],
  ];
  for (const [name, mutate, re] of cases) {
    const a = clone(FIX.mints.MSFTx);
    mutate(a);
    assert.throws(() => mintFacts('MSFTx', a, NOW), re, name);
  }
  assert.throws(() => mintFacts('X', null, NOW), /account not found/);
  for (const d of [0, 18]) {                          // the bounds themselves are valid
    const a = clone(FIX.mints.USDC);
    a.data.parsed.info.decimals = d;
    assert.equal(mintFacts('USDC', a, NOW).decimals, d);
  }
  assert.throws(() => effectiveMultiplier(ext(FIX.mints.MSFTx, 'scaledUiAmountConfig').state, NaN), /no clock/);
});

test('readMints rejects an RPC answer that is not one account per mint', async () => {
  const ms = ['MU', 'USDC'];
  await assert.rejects(readMints(async () => null, ms, NOW), /returned object accounts for 2/);
  await assert.rejects(readMints(async () => [FIX.mints.MU], ms, NOW), /returned 1 accounts for 2/);
  await assert.rejects(readMints(async () => [FIX.mints.MU, null], ms, NOW), /mint USDC: account not found/);
  const ok = await readMints(async got => got.map(m => FIX.mints[m]), ms, NOW);
  assert.deepEqual(ok.map(f => f.multiplier), [1.0001069314314899, 1]);
});

test('uiToRaw refuses what it cannot convert exactly', () => {
  assert.throws(() => uiToRaw(-1, 6, 1), /non-negative/);
  assert.throws(() => uiToRaw('x', 6, 1), /non-negative/);
  assert.throws(() => uiToRaw(Infinity, 6, 1), /non-negative/);
  assert.throws(() => uiToRaw(1e12, 8, 1), /too large/);
  assert.equal(uiToRaw(Number.MAX_SAFE_INTEGER, 0, 1), BigInt(Number.MAX_SAFE_INTEGER));   // the bound is exact
  assert.equal(uiToRaw('1.5', 8, 1.0059033904787456), 149119688n);   // 1.5 UI MSFTx in raw (floor of 149119688.25)
  assert.equal(rawToUi('149119688', 8, 1.0059033904787456).toFixed(9), '1.499999997');
});
