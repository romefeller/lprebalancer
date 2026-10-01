// signer_aerodrome.mjs against Base mainnet: reads and dry runs only (no --execute
// anywhere in this file). The wallet is a throwaway key made in a scratch directory, so
// it is empty: the dry-run open must refuse and its simulation must revert in the token
// transfer (STF), which proves the calldata decoded and reached the pool's money path.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

const dir = path.dirname(new URL(import.meta.url).pathname);
const signer = path.join(dir, '..', 'signer_aerodrome.mjs');
const POOL = '0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59';
const WETH = '0x4200000000000000000000000000000000000006';
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const PIN = '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142';

const d = fs.mkdtempSync(path.join(os.tmpdir(), 'aero-live-'));
fs.chmodSync(d, 0o700);
const KEY = path.join(d, 'k.secret');
spawnSync('node', [path.join(dir, '..', 'evm_wallet.mjs'), 'create', '--path', KEY]);
test.after(() => fs.rmSync(d, { recursive: true, force: true }));

function run(args, env = {}) {
  assert.ok(!args.includes('--execute'), 'live tests never send');
  const r = spawnSync('node', [signer, ...args], {
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: KEY, LPBOT_POOL: POOL, ...env }, encoding: 'utf8', timeout: 120_000,
  });
  const at = r.stdout.indexOf('{') >= 0 ? r.stdout.indexOf('{') : r.stdout.indexOf('[');
  let json = null;
  try { json = JSON.parse(r.stdout.slice(at, r.stdout.lastIndexOf(r.stdout[at] === '[' ? ']' : '}') + 1)); } catch { /* none */ }
  return { ...r, json };
}

test('pool: the verified factory, tokens, spacing; fee dynamic; Chainlink agrees', () => {
  const r = run(['pool']);
  assert.strictEqual(r.status, 0, r.stderr);
  const p = r.json;
  assert.strictEqual(p.factory, '0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A');
  assert.strictEqual(p.nft, '0x827922686190790b37229fd06084350E74485b72');
  assert.strictEqual(p.mintA, WETH); assert.strictEqual(p.mintB, USDC);
  assert.strictEqual(p.tickSpacing, 100); assert.strictEqual(p.nativeSide, 'A');
  assert.strictEqual(p.dynamicFee, true);
  assert.ok(p.unstakedFee > 0 && p.unstakedFee < 0.5, `unstakedFee ${p.unstakedFee}`);
  assert.ok(p.oracleDeviation < 0.02, `pool vs Chainlink ${p.oracleDeviation}`);
  assert.ok(p.price > 100 && p.price < 100000);
});

test('balance, status, positions: an empty wallet reads as empty, not as a failure', () => {
  const b = run(['balance']);
  assert.strictEqual(b.status, 0, b.stderr);
  assert.strictEqual(b.json.balanceA, 0); assert.strictEqual(b.json.walletUsd, 0); assert.strictEqual(b.json.sol, 0);
  const s = run(['status']);
  assert.strictEqual(s.status, 0, s.stderr);
  assert.deepStrictEqual(s.json, { positions: 0, positionMint: null, pool: POOL });
  const p = run(['positions']);
  assert.strictEqual(p.status, 0, p.stderr);
  assert.deepStrictEqual(p.json, []);
  const t = run(['balance', USDC]);
  assert.strictEqual(t.status, 0, t.stderr);
  assert.deepStrictEqual([t.json.symbol, t.json.decimals, t.json.amount], ['USDC', 6, 0]);
});

test('dry-run open: refused for the empty wallet, simulated, the mint reverts in the transfer (STF)', () => {
  const pr = run(['pool']).json.price;
  const r = run(['open', POOL, String(pr * 0.97), String(pr * 1.03), '0.02', '50']);
  assert.strictEqual(r.status, 1);
  assert.strictEqual(r.json.sent, false);
  assert.ok(r.json.refused.some(x => /gas reserve/.test(x)), r.json.refused.join('; '));
  assert.ok(r.json.refused.some(x => /wallet lacks .* USDC/.test(x)));
  const mint = r.json.simulation.find(s => s.label === 'mint');
  assert.strictEqual(mint.ok, false);
  assert.match(mint.revert, /STF/, 'an encoding error would not reach the token transfer');
});

test('dry-run rebalance with nothing to sell is a noop; dry-run send refuses an empty wallet', () => {
  const r = run(['rebalance', WETH, USDC, '50', '50']);
  assert.strictEqual(r.status, 0, r.stderr);
  assert.strictEqual(r.json.noop, true);
  const s = run(['send', USDC, '1', PIN], { LPBOT_EVM_PROFIT_WALLET_PIN: PIN });
  assert.strictEqual(s.status, 1);
  assert.match(s.stderr, /holds 0, less than 1/);
});
