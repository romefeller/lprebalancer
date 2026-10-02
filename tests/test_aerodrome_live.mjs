// signer_aerodrome.mjs against Base mainnet: reads and dry runs only (no --execute
// anywhere in this file), on a WETH/USDC pool of each known Slipstream deployment, and a
// refusal of a pool of a deployment the registry does not hold. The wallet is a throwaway key made in a scratch directory, so
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
const CASES = [
  { name: 'initial', pool: '0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59', spacing: 100,
    factory: '0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A', nft: '0x827922686190790b37229fd06084350E74485b72' },
  { name: 'gauges-v3', pool: '0x3FE04A59Ebd38cF06080a6F60a98D124eb59392A', spacing: 50,
    factory: '0xf8f2eB4940CFE7d13603DDDD87f123820Fc061Ef', nft: '0xe1f8cd9AC4e4A65F54f38a5CdAfCA44f6dD68b53' },
];
// WETH/USDC tickSpacing 50 of the "Gauge Caps" deployment (factory 0xaDe65c38…): not in the registry.
const CAPS_POOL = '0xc758d81B9b81A6FCDAd075bD471874A2c46B54e0';
const WETH = '0x4200000000000000000000000000000000000006';
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const PIN = '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142';

const d = fs.mkdtempSync(path.join(os.tmpdir(), 'aero-live-'));
fs.chmodSync(d, 0o700);
const KEY = path.join(d, 'k.secret');
spawnSync('node', [path.join(dir, '..', 'evm_wallet.mjs'), 'create', '--path', KEY]);
test.after(() => fs.rmSync(d, { recursive: true, force: true }));

function runOn(pool, args, env = {}) {
  assert.ok(!args.includes('--execute'), 'live tests never send');
  const r = spawnSync('node', [signer, ...args], {
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: KEY, LPBOT_POOL: pool, ...env }, encoding: 'utf8', timeout: 120_000,
  });
  const at = r.stdout.indexOf('{') >= 0 ? r.stdout.indexOf('{') : r.stdout.indexOf('[');
  let json = null;
  try { json = JSON.parse(r.stdout.slice(at, r.stdout.lastIndexOf(r.stdout[at] === '[' ? ']' : '}') + 1)); } catch { /* none */ }
  return { ...r, json };
}

test('a pool of a deployment outside the registry is refused, even for a read', () => {
  const r = runOn(CAPS_POOL, ['pool']);
  assert.strictEqual(r.status, 1);
  assert.match(r.stderr, /belongs to factory 0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a, not a known Slipstream deployment/);
});

for (const C of CASES) {
  const run = (args, env) => runOn(C.pool, args, env);

  test(`${C.name}: pool: the verified factory, tokens, spacing; fee dynamic; Chainlink agrees`, () => {
    const r = run(['pool']);
    assert.strictEqual(r.status, 0, r.stderr);
    const p = r.json;
    assert.strictEqual(p.factory, C.factory);
    assert.strictEqual(p.nft, C.nft); assert.strictEqual(p.npm, C.nft); assert.strictEqual(p.deployment, C.name);
    assert.strictEqual(p.mintA, WETH); assert.strictEqual(p.mintB, USDC);
    assert.strictEqual(p.tickSpacing, C.spacing); assert.strictEqual(p.nativeSide, 'A');
    assert.strictEqual(p.dynamicFee, true);
    assert.ok(p.unstakedFee > 0 && p.unstakedFee < 0.5, `unstakedFee ${p.unstakedFee}`);
    assert.ok(p.oracleDeviation < 0.02, `pool vs Chainlink ${p.oracleDeviation}`);
    assert.ok(p.price > 100 && p.price < 100000);
  });

  test(`${C.name}: balance, status, positions: an empty wallet reads as empty, not as a failure`, () => {
    const b = run(['balance']);
    assert.strictEqual(b.status, 0, b.stderr);
    assert.strictEqual(b.json.balanceA, 0); assert.strictEqual(b.json.walletUsd, 0); assert.strictEqual(b.json.sol, 0);
    const s = run(['status']);
    assert.strictEqual(s.status, 0, s.stderr);
    assert.deepStrictEqual(s.json, { positions: 0, positionMint: null, pool: C.pool });
    const p = run(['positions']);
    assert.strictEqual(p.status, 0, p.stderr);
    assert.deepStrictEqual(p.json, []);
    const t = run(['balance', USDC]);
    assert.strictEqual(t.status, 0, t.stderr);
    assert.deepStrictEqual([t.json.symbol, t.json.decimals, t.json.amount], ['USDC', 6, 0]);
  });

  test(`${C.name}: dry-run open: refused for the empty wallet, simulated, the mint reverts in the transfer (STF)`, () => {
    const pr = run(['pool']).json.price;
    const r = run(['open', C.pool, String(pr * 0.97), String(pr * 1.03), '0.02', '50']);
    assert.strictEqual(r.status, 1);
    assert.strictEqual(r.json.sent, false);
    assert.ok(r.json.refused.some(x => /gas reserve/.test(x)), r.json.refused.join('; '));
    assert.ok(r.json.refused.some(x => /wallet lacks .* USDC/.test(x)));
    const mint = r.json.simulation.find(s => s.label === 'mint');
    assert.strictEqual(mint.ok, false);
    assert.match(mint.revert, /STF/, 'an encoding error would not reach the token transfer');
  });

  test(`${C.name}: dry-run rebalance with nothing to sell is a noop; dry-run send refuses an empty wallet`, () => {
    const r = run(['rebalance', WETH, USDC, '50', '50']);
    assert.strictEqual(r.status, 0, r.stderr);
    assert.strictEqual(r.json.noop, true);
    const s = run(['send', USDC, '1', PIN], { LPBOT_EVM_PROFIT_WALLET_PIN: PIN });
    assert.strictEqual(s.status, 1);
    assert.match(s.stderr, /holds 0, less than 1/);
  });
}
