// One HALT rule for every signer (halt_guard.mjs): the script directory's HALT
// stops every profile, LPBOT_RUN_DIR/HALT stops one. The helper once, then
// every write script, run for real with a HALT in a run directory only.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { haltFiles, assertNotHalted } from '../halt_guard.mjs';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const tmp = (p) => fs.mkdtempSync(path.join(os.tmpdir(), p));

test('the helper: the directory HALT, then the run directory HALT', () => {
  const dir = tmp('halt-dir-'), run = tmp('halt-run-');
  assert.deepEqual(haltFiles(dir, {}), [path.join(dir, 'HALT')]);
  assert.deepEqual(haltFiles(dir, { LPBOT_RUN_DIR: run }), [path.join(dir, 'HALT'), path.join(run, 'HALT')]);
  assert.doesNotThrow(() => assertNotHalted(dir, { LPBOT_RUN_DIR: run }));
  fs.writeFileSync(path.join(run, 'HALT'), 'mu only\n');
  assert.throws(() => assertNotHalted(dir, { LPBOT_RUN_DIR: run }), /HALT present: mu only/);
  assert.doesNotThrow(() => assertNotHalted(dir, {}));                       // another profile runs on
  fs.writeFileSync(path.join(dir, 'HALT'), '');
  assert.throws(() => assertNotHalted(dir, {}), /HALT present: .*HALT/);       // empty: named by its path
});

test('the helper: a run directory that is not a plain absolute path refuses', () => {
  const dir = tmp('halt-dir-');
  for (const bad of ['run/mu-usdc', '/x/../etc', 'relative', '/a\0b']) {
    assert.throws(() => assertNotHalted(dir, { LPBOT_RUN_DIR: bad }), /not an absolute path/, bad);
  }
  assert.doesNotThrow(() => assertNotHalted(dir, { LPBOT_RUN_DIR: '' }));
});

const SOL = 'So11111111111111111111111111111111111111112';
const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
const POOL = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE';
const EVM = '0x' + 'ab'.repeat(20);
const WRITES = {
  'signer2.mjs': ['open', POOL, '1', '2', '1', '1', '--execute'],
  'signer_dlmm.mjs': ['open', POOL, '1', '2', '1', '1', '--execute'],
  'signer_raydium.mjs': ['open', POOL, '1', '2', '1', '1', '--execute'],
  'signer_byreal.mjs': ['open', POOL, '1', '2', '1', '1', '--execute'],
  'signer_pancake.mjs': ['open', POOL, '1', '2', '1', '1', '--execute'],
  'swap_jupiter.mjs': ['rebalance', SOL, USDC, '1', '1', '--execute'],
  'swap_orca.mjs': ['rebalance', SOL, USDC, '1', '1', '--execute'],
  'payout.mjs': ['send', USDC, '1', USDC, '--execute'],
  'janitor.mjs': ['close-empty', '--execute'],
  'signer_aerodrome.mjs': ['open', EVM, '1', '2', '1', '1', '--execute'],
};

for (const [script, args] of Object.entries(WRITES)) {
  test(`${script}: a HALT in this profile's run directory refuses the write`, () => {
    const run = tmp('halt-run-');
    fs.writeFileSync(path.join(run, 'HALT'), 'operator: this profile only');
    const r = spawnSync('node', [path.join(ROOT, script), ...args], {
      encoding: 'utf8', timeout: 60_000,
      env: { PATH: process.env.PATH, LPBOT_RUN_DIR: run, WALLET_SECRET_PATH: '/nonexistent/key',
             SOLANA_RPC_URL: 'http://127.0.0.1:9', LPBOT_RPC: 'http://127.0.0.1:9', LPBOT_POOL: POOL,
             LPBOT_PROFIT_WALLET: USDC, LPBOT_PROFIT_WALLET_PIN: USDC },
    });
    assert.notEqual(r.status, 0, r.stdout);
    assert.match(r.stderr, /HALT present: operator: this profile only/, r.stderr);
  });
}

// The global HALT: the bot root's HALT stops every write script, wherever the
// script lives in the tree. Run on a copy of the tree, never on the live one:
// a HALT written there would stop the real bot.
const WRITES_ALL = { ...WRITES, 'signer_uniswap.mjs': ['open', EVM, '1', '2', '1', '1', '--execute'] };
const SKIP_IN_COPY = new Set(['node_modules', '.git', '.claude', 'run', 'tests', '__pycache__']);

function treeCopy() {
  const dest = tmp('halt-tree-');
  for (const entry of fs.readdirSync(ROOT)) {
    if (SKIP_IN_COPY.has(entry) || entry === 'HALT') continue;
    fs.cpSync(path.join(ROOT, entry), path.join(dest, entry), { recursive: true });
  }
  fs.symlinkSync(path.join(ROOT, 'node_modules'), path.join(dest, 'node_modules'));
  return dest;
}

test('the bot root HALT refuses every write script', () => {
  const copy = treeCopy();
  fs.writeFileSync(path.join(copy, 'HALT'), 'operator: every profile');
  for (const [script, args] of Object.entries(WRITES_ALL)) {
    const r = spawnSync('node', [path.join(copy, script), ...args], {
      encoding: 'utf8', timeout: 60_000,
      env: { PATH: process.env.PATH, WALLET_SECRET_PATH: '/nonexistent/key', LPBOT_CHAIN: 'polygon',
             SOLANA_RPC_URL: 'http://127.0.0.1:9', LPBOT_RPC: 'http://127.0.0.1:9', LPBOT_POOL: POOL,
             LPBOT_PROFIT_WALLET: USDC, LPBOT_PROFIT_WALLET_PIN: USDC },
    });
    assert.notEqual(r.status, 0, `${script}: ${r.stdout}`);
    assert.match(r.stderr, /HALT present: operator: every profile/, `${script}: ${r.stderr}`);
  }
});
