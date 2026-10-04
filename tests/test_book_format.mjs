import test from 'node:test';
import assert from 'node:assert/strict';
import { equityLine, lpLine, sinceStartLine, shareAgrees } from '../book_format.mjs';

const R = { equity_usd: 240.0, pnl_usd: 3.52, last_price: 118.0905, lp_usd: 221.24, deployed_pct: 92.2, wallet_usd: 18.73,
  since_start: { since: '2026-09-22T19:56:24+00:00', start_usd: 248.096, start_sol: 2.102865, value_usd: 242.4247,
                 profit_usd: -5.6713, vs_hold_start_assets_usd: -5.9883 } };

test('equity carries the P&L since the start, not since the first snapshot', () => {
  assert.equal(equityLine(R), 'equity      $240.00 · SOL $118.09   P&L -5.67 since start · includes pending fees');
  assert.equal(equityLine({ ...R, since_start: null }), 'equity      $240.00 · SOL $118.09   P&L +3.52 · includes pending fees');
  assert.equal(equityLine({ ...R, since_start: { profit_usd: null } }), 'equity      $240.00 · SOL $118.09   P&L +3.52 · includes pending fees');
  assert.equal(equityLine({ ...R, since_start: { profit_usd: 0 } }), 'equity      $240.00 · SOL $118.09   P&L +0.00 since start · includes pending fees');
});

test('the SOL price sits next to equity, and is left out when unknown', () => {
  assert.equal(equityLine({ ...R, last_price: null }), 'equity      $240.00   P&L -5.67 since start · includes pending fees');
  assert.equal(equityLine({ ...R, last_price: undefined, since_start: null }), 'equity      $240.00   P&L +3.52 · includes pending fees');
  assert.equal(equityLine({ ...R, last_price: 0 }), 'equity      $240.00 · SOL $0.00   P&L -5.67 since start · includes pending fees');
});

test('the line after equity shows what is in the LP', () => {
  assert.equal(lpLine(R), 'in LP       $221.24 (92.2%) · wallet $18.73');
  assert.equal(lpLine({ ...R, deployed_pct: null }), 'in LP       $221.24 · wallet $18.73');
  assert.equal(lpLine({ ...R, wallet_usd: null }), 'in LP       $221.24 (92.2%)');
  assert.equal(lpLine({ ...R, lp_usd: 0 }), 'in LP       $0.00 · wallet $18.73');     // disagrees with equity: no share
  assert.equal(lpLine({ ...R, lp_usd: null }), null);
  assert.equal(lpLine({}), null);
});

test('since start: the capital then, the value now, against holding it', () => {
  assert.equal(sinceStartLine(R), 'start       $248.10 (2.1029 SOL, 2026-09-22) · now $242.42 · vs holding it -5.99');
  assert.equal(sinceStartLine({}), null);
  assert.equal(sinceStartLine({ since_start: { start_usd: null } }), null);
  assert.equal(sinceStartLine({ since_start: { ...R.since_start, vs_hold_start_assets_usd: null } }),
    'start       $248.10 (2.1029 SOL, 2026-09-22) · now $242.42 · vs holding it —');
});

import { splitMessage } from '../book_format.mjs';
import fs from 'node:fs';

test('long messages are split on line breaks under the limit', () => {
  assert.deepEqual(splitMessage('short'), ['short']);
  const lines = Array.from({ length: 300 }, (_, i) => `line ${i} ` + 'x'.repeat(20));
  const parts = splitMessage(lines.join('\n'));
  assert.ok(parts.length > 1);
  for (const p of parts) assert.ok(p.length <= 4000);
  assert.equal(parts.join('\n'), lines.join('\n'));                    // nothing lost, nothing added
  const huge = 'y'.repeat(9000);
  const hp = splitMessage(huge);
  assert.deepEqual(hp.map(p => p.length), [4000, 4000, 1000]);
  assert.equal(hp.join(''), huge);
  assert.deepEqual(splitMessage('a\nb\nc', 3), ['a\nb', 'c']);
  assert.deepEqual(splitMessage('abcd', 4), ['abcd']);
});

test('the bridge formats every new event', () => {
  const src = fs.readFileSync(new URL('../telegram_bridge.mjs', import.meta.url), 'utf8');
  for (const ev of ['DEPLOY_IDLE', 'SWEEP', 'sweep_failed', 'JANITOR', 'AUDIT', 'DAILY', 'TAPE_SOURCE']) {
    assert.ok(src.includes(`case '${ev}':`), ev);
  }
  assert.ok(src.includes('splitMessage(text)'));
});

test('the LP share is shown only when it is in range and agrees with the book', () => {
  let seed = 7;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let i = 0; i < 2000; i++) {
    const eq = rnd() < 0.1 ? [null, 0, -5, NaN][Math.floor(rnd() * 4)] : rnd() * 1000;
    const lp = rnd() < 0.1 ? [0, null, NaN][Math.floor(rnd() * 3)] : rnd() * 1000;
    const pct = rnd() < 0.2 ? [null, -1, 101, NaN, 0][Math.floor(rnd() * 5)] : rnd() * 100;
    const line = lpLine({ equity_usd: eq, lp_usd: lp, deployed_pct: pct, wallet_usd: 1 });
    if (lp == null) { assert.equal(line, null); continue; }
    const m = /\((-?[\d.]+)%\)/.exec(line);
    if (!m) continue;
    const shown = Number(m[1]);
    assert.ok(shown >= 0 && shown <= 100, line);
    if (Number.isFinite(eq) && eq > 0) assert.ok(Math.abs(lp / eq * 100 - pct) <= 0.2, line);
  }
  assert.equal(shareAgrees({ equity_usd: 240, lp_usd: 150.13, deployed_pct: 62.6 }), true);
  assert.equal(shareAgrees({ equity_usd: 240, lp_usd: 0, deployed_pct: 62.6 }), false);
  assert.equal(shareAgrees({ equity_usd: 240, lp_usd: 0, deployed_pct: 0 }), true);
  assert.equal(shareAgrees({ lp_usd: 5, deployed_pct: 50 }), true);          // no equity: nothing to check against
  assert.equal(shareAgrees({ equity_usd: 240, lp_usd: 5, deployed_pct: 101 }), false);
});

import { emojiFor, healthLine } from '../book_format.mjs';
test('emoji rule and health line', () => {
  assert.equal(emojiFor('OPEN', { OPEN: '🟩' }), '🟩');
  assert.equal(emojiFor('open_failed', {}), '❌');
  assert.equal(emojiFor('move_deferred', {}), '⏳');
  assert.equal(emojiFor('whatever', {}), '▫️');
  assert.equal(emojiFor('_comment', { _comment: 'x' }), '▫️');
  assert.equal(healthLine(null), null);
  assert.equal(healthLine([]), '🩺 health   🟢 all systems');
  assert.equal(healthLine([{ key: 'swap', state: 'closed' }]), '🩺 health   🟢 all systems (1 watched)');
  assert.equal(healthLine([{ key: 'venue:orca', state: 'tripped', emoji: '🔴', fails: 3, wait_s: 2400 },
                           { key: 'swap', state: 'backoff', emoji: '🟡', fails: 1, wait_s: 0 }]),
    '🩺 health   🔴 venue:orca 3x retry 40m · 🟡 swap 1x probing');
});

test('share edges: each invalid field alone refuses, the limits are inclusive', () => {
  const ok = { equity_usd: 200, lp_usd: 100, deployed_pct: 50 };
  assert.equal(shareAgrees(ok), true);
  for (const bad of [{ deployed_pct: null }, { deployed_pct: NaN }, { deployed_pct: -0.1 }, { deployed_pct: 100.1 }, { lp_usd: NaN }])
    assert.equal(shareAgrees({ ...ok, ...bad }), false, JSON.stringify(bad));
  assert.equal(shareAgrees({ equity_usd: 200, lp_usd: 200, deployed_pct: 100 }), true);
  assert.equal(shareAgrees({ equity_usd: 200, lp_usd: 0, deployed_pct: 0 }), true);
  for (const eq of [null, NaN, 0, -1]) assert.equal(shareAgrees({ ...ok, equity_usd: eq }), true, String(eq));   // nothing to check against
  assert.equal(shareAgrees({ equity_usd: 100, lp_usd: 50.19, deployed_pct: 50 }), true);
  assert.equal(shareAgrees({ equity_usd: 100, lp_usd: 50.21, deployed_pct: 50 }), false);
});

test('health line without emoji fields', () => {
  assert.equal(healthLine([{ key: 'a', state: 'tripped', fails: 3, wait_s: 60 }]), '🩺 health   🔴 a 3x retry 1m');
  assert.equal(healthLine([{ key: 'b', state: 'backoff', fails: 1, wait_s: 61 }]), '🩺 health   🟡 b 1x retry 2m');
  assert.equal(healthLine([null, { key: 'c' }]), '🩺 health   🟢 all systems (2 watched)');
});

test('a missing share is refused even when zero would agree', () => {
  assert.equal(shareAgrees({ equity_usd: 200, lp_usd: 0, deployed_pct: null }), false);
});

test('a cooled breaker reads yellow and probing', () => {
  assert.equal(healthLine([{ key: 'swap', state: 'probing', emoji: '🟡', fails: 3, wait_s: 0 }]),
    '🩺 health   🟡 swap 3x probing');
  assert.equal(healthLine([{ key: 'swap', state: 'probing', fails: 3, wait_s: 0 }]), '🩺 health   🟡 swap 3x probing');
});

// A pool whose token A is the stable (Unichain USDC/HYPE): prices are HYPE per USDC; the book
// shows HYPE in dollars, band edges swapped, moves turned the same way. SOL/USDC unchanged.
import { pricedView, shownPrice, shownBand, shownMovePct, shownSide } from '../book_format.mjs';
const H = { ...R, token_a: 'USDC', token_b: 'HYPE', last_price: 1 / 90.5 };

test('stable token A: the book shows the volatile token in dollars', () => {
  assert.deepEqual(pricedView(H), { symbol: 'HYPE', inverted: true });
  assert.deepEqual(pricedView({ pair: 'USDC/HYPE' }), { symbol: 'HYPE', inverted: true });
  assert.deepEqual(pricedView({ position_pair: 'USDC / HYPE', pair: 'SOL/USDC' }), { symbol: 'HYPE', inverted: true }, 'the held pair wins');
  assert.deepEqual(pricedView(R), { symbol: 'SOL', inverted: false });
  assert.deepEqual(pricedView({ token_a: 'MU' }), { symbol: 'MU', inverted: false });
  assert.deepEqual(pricedView({ pair: 'SOL/USDC' }), { symbol: 'SOL', inverted: false });
  assert.deepEqual(pricedView({ pair: 'USDC/USDT' }), { symbol: 'USDC', inverted: false }, 'two stables: nothing to invert');
  assert.equal(equityLine(H), 'equity      $240.00 · HYPE $90.50   P&L -5.67 since start · includes pending fees');
  assert.equal(sinceStartLine(H), 'start       $248.10 (2026-09-22) · now $242.42 · vs holding it -5.99', 'no token count in the stable token');
  assert.ok(sinceStartLine(R).includes('2.1029 SOL, '));
});

test('stable token A: prices invert, band edges swap, moves and sides turn; zero and junk are unknown', () => {
  assert.equal(shownPrice(H, 0.01), 100); assert.equal(shownPrice(H, 0), null); assert.equal(shownPrice(H, null), null);
  assert.equal(shownPrice(H, undefined), null); assert.equal(shownPrice(H, 'x'), null); assert.equal(shownPrice(H, -1), null);
  assert.equal(shownPrice(R, 0), 0, 'a non-inverted zero stays a zero'); assert.equal(shownPrice(R, '120'), 120);
  assert.deepEqual(shownBand(H, 0.01, 0.0125), [80, 100]);
  assert.deepEqual(shownBand(R, 100, 120), [100, 120]);
  assert.ok(Math.abs(shownMovePct(H, 25) - -20) < 1e-12);
  assert.ok(Math.abs(shownMovePct(H, -20) - 25) < 1e-12);
  assert.equal(shownMovePct(R, 25), 25); assert.equal(shownMovePct(R, 0), 0);
  assert.equal(shownMovePct(H, -100), null); assert.equal(shownMovePct(H, -150), null);
  assert.equal(shownMovePct(H, null), null); assert.equal(shownMovePct(H, undefined), null); assert.equal(shownMovePct(H, 'x'), null);
  assert.equal(shownMovePct(R, null), null);
  assert.equal(shownSide(H, 'above'), 'below'); assert.equal(shownSide(H, 'below'), 'above');
  assert.equal(shownSide(H, 'up'), 'down'); assert.equal(shownSide(H, 'sideways'), 'sideways');
  assert.equal(shownSide(R, 'above'), 'above');
});
