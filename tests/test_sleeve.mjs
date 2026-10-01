// LPBOT_SLEEVE in swap_jupiter.mjs: the swap plans from one profile's share
// of a shared wallet, and a sleeve that cannot be read refuses the call.
// And Token-2022 scaled UI amounts: the sleeve, the plan and Jupiter's
// usdPrice are UI units, the quote raw.
import test from 'node:test';
import assert from 'node:assert/strict';
import { parseSleeve, sleeveCap, uiOf, amountToRaw, verifyQuote } from '../swap_jupiter.mjs';
import { planRebalance } from '../rebalance_plan.mjs';

const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
const MU = 'MUmint1111111111111111111111111111111111111';

test('no sleeve: the wallet as it is', () => {
  assert.equal(parseSleeve(undefined), null);
  assert.equal(parseSleeve(''), null);
  assert.equal(sleeveCap(123.4, USDC, null), 123.4);
});

test('a sleeve caps each mint at its share, and a mint it does not name at 0', () => {
  const s = parseSleeve(JSON.stringify({ [USDC]: 40, [MU]: 2.5 }));
  assert.equal(sleeveCap(500, USDC, s), 40);           // the wallet holds more: the share binds
  assert.equal(sleeveCap(10, USDC, s), 10);            // the wallet holds less: the wallet binds
  assert.equal(sleeveCap(9, MU, s), 2.5);
  assert.equal(sleeveCap(7, 'OtherMint11111111111111111111111111111111', s), 0);
  assert.equal(sleeveCap(0, USDC, parseSleeve(JSON.stringify({ [USDC]: 0 }))), 0);
});

test('a sleeve that cannot be read refuses, never means the whole wallet', () => {
  for (const bad of ['{', '[1]', '"x"', 'null', JSON.stringify({ [USDC]: -1 }), JSON.stringify({ [USDC]: '5' }),
                     JSON.stringify({ [USDC]: null }), '{"a": 1e999}']) {
    assert.throws(() => parseSleeve(bad), /LPBOT_SLEEVE/, bad);
  }
});

test('property: the capped balance is never above the sleeve or the wallet, never negative', () => {
  let seed = 7;
  const rnd = () => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed / 2 ** 31; };
  for (let i = 0; i < 2000; i++) {
    const total = rnd() * 1000, share = rnd() < 0.1 ? 0 : rnd() * 1000;
    const s = parseSleeve(JSON.stringify({ [USDC]: share }));
    const c = sleeveCap(total, USDC, s);
    assert.ok(c <= share && c <= total && c >= 0, `${total} ${share} -> ${c}`);
  }
});

test('a plan from capped balances sells only from the sleeve', () => {
  // The wallet holds $500 of USDC, the profile's sleeve $40 of it and $0 of MU:
  // the plan buys MU with at most the sleeve's USDC.
  const s = parseSleeve(JSON.stringify({ [USDC]: 40, [MU]: 0 }));
  const usdA = sleeveCap(0, MU, s), usdB = sleeveCap(500, USDC, s);
  const plan = planRebalance(usdA, usdB, 20, 20);
  assert.equal(plan.sellSide, 'B');
  assert.ok(plan.sellUsd <= 40 + 1e-9);
  assert.ok(Math.abs(plan.sellUsd - 20) < 1e-9);
});

test('a plain mint converts exactly as before', () => {
  const usdc = { mint: USDC, decimals: 6 };
  assert.equal(amountToRaw('0.1', { decimals: 9 }), 100000000n);
  assert.equal(amountToRaw('12.345678', usdc), 12345678n);
  assert.equal(uiOf(12345678n, usdc), 12.345678);
  assert.equal(uiOf('100000000', { decimals: 9, multiplier: 1 }), 0.1);
  // the decimal string, not a float product: 0.29 x 100 is 28.999999999999996 in floating point
  assert.equal(amountToRaw('0.29', { decimals: 2 }), 29n);
  assert.equal(amountToRaw('0.29', { decimals: 2, multiplier: 1 }), 29n);
});

test('a scaled mint: UI amounts are raw times the multiplier, and back, never above the UI amount', () => {
  const msftx = { mint: MU, decimals: 8, multiplier: 1.0059 };
  assert.ok(Math.abs(uiOf(100000000n, msftx) - 1.0059) < 1e-12);
  const raw = amountToRaw(1.0059, msftx);
  assert.ok(raw === 100000000n || raw === 99999999n, String(raw));      // floored: a cap stays a ceiling
  assert.ok(uiOf(raw, msftx) <= 1.0059 + 1e-12);
  // the 0.59% the old conversion lost on every MSFTx amount
  assert.ok(Math.abs(Number(amountToRaw(100, msftx)) / 1e8 - 100 / 1.0059) < 1e-8);
});

test('the quote value check prices UI amounts: a fair scaled swap passes, a short one is refused', () => {
  const inInfo = { mint: MU, symbol: 'MSFTx', decimals: 8, multiplier: 1.0059, usdPrice: 500 };
  const outInfo = { mint: USDC, symbol: 'USDC', decimals: 6, usdPrice: 1 };
  const rawIn = amountToRaw(1, inInfo);                         // 1 UI MSFTx, worth $500
  const q = (outUsd) => ({ inputMint: MU, outputMint: USDC, inAmount: rawIn.toString(), swapMode: 'ExactIn',
    slippageBps: 50, outAmount: String(Math.round(outUsd * 1e6)), otherAmountThreshold: String(Math.round(outUsd * 1e6)) });
  assert.ok(verifyQuote(q(499), inInfo, outInfo, rawIn));
  assert.throws(() => verifyQuote(q(480), inInfo, outInfo, rawIn), /below fair value/);
});
