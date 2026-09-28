import test from 'node:test';
import assert from 'node:assert/strict';
import { equityLine, lpLine, sinceStartLine } from '../book_format.mjs';

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
  assert.equal(lpLine({ ...R, lp_usd: 0 }), 'in LP       $0.00 (92.2%) · wallet $18.73');
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
