// chains/evm/wallet.mjs and chains/evm/keyfile.mjs: a new key is written once, mode 0600, never over an
// existing file, and the only thing printed is the address. Every case runs in a scratch
// directory; the real key at /home/ubuntu/.kamino-keys is never opened.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { readKey, writeNewKey, validKey } from '../chains/evm/keyfile.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const script = path.join(dir, '..', 'chains/evm/wallet.mjs');
const ADDR = /^0x[0-9a-fA-F]{40}\n$/;

const made = [];
test.after(() => { for (const x of made) fs.rmSync(x, { recursive: true, force: true }); });

function scratch(mode = 0o700) {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'evmw-'));
  made.push(d);
  fs.chmodSync(d, mode);
  return d;
}
const run = (args, umask) => spawnSync('bash', ['-c', `umask ${umask ?? '022'}; exec node "$0" "$@"`, script, ...args], { encoding: 'utf8' });

test('create refuses to overwrite an existing key, and leaves it unchanged', () => {
  const d = scratch(), f = path.join(d, 'k.secret');
  assert.strictEqual(run(['create', '--path', f]).status, 0);
  const before = fs.readFileSync(f, 'utf8');
  const r = run(['create', '--path', f]);
  assert.strictEqual(r.status, 1);
  assert.match(r.stderr, /exists; refusing to overwrite a key/);
  assert.strictEqual(fs.readFileSync(f, 'utf8'), before);
  assert.deepStrictEqual(fs.readdirSync(d), ['k.secret'], 'no temporary file left behind');
});

test('create refuses a dangling symlink at the key path', () => {
  const d = scratch(), f = path.join(d, 'k.secret');
  fs.symlinkSync(path.join(d, 'elsewhere'), f);
  const r = run(['create', '--path', f]);
  assert.strictEqual(r.status, 1);
  assert.match(r.stderr, /exists/);
  assert.ok(!fs.existsSync(path.join(d, 'elsewhere')));
});

test('create refuses a key directory that group or others can read', () => {
  const d = scratch(0o755);
  const r = run(['create', '--path', path.join(d, 'k.secret')]);
  assert.strictEqual(r.status, 1);
  assert.match(r.stderr, /readable by group or others/);
});

test('reading refuses a key file group or others can read, and malformed content', () => {
  const d = scratch(), f = path.join(d, 'k.secret');
  fs.writeFileSync(f, `0x${'11'.repeat(32)}\n`, { mode: 0o644 });
  fs.chmodSync(f, 0o644);
  assert.throws(() => readKey(f), /must be 0600/);
  fs.chmodSync(f, 0o600);
  assert.match(readKey(f), /^0x(11){32}$/);
  for (const bad of ['0x' + '00'.repeat(32), '0x' + 'ff'.repeat(32), 'deadbeef', '0x' + '11'.repeat(31), '[1,2,3]']) {
    fs.writeFileSync(f, bad);
    assert.throws(() => readKey(f), /does not hold one 0x-prefixed 32-byte secp256k1 key/, bad);
  }
  assert.throws(() => readKey(undefined), /WALLET_SECRET_PATH is not set/);
  assert.throws(() => readKey(d), /not a regular file/);
});

test('an error message names the path, never the content', () => {
  const d = scratch(), f = path.join(d, 'k.secret');
  const secret = '0x' + 'ab'.repeat(32);
  fs.writeFileSync(f, secret, { mode: 0o644 }); fs.chmodSync(f, 0o644);
  try { readKey(f); assert.fail('must throw'); } catch (e) { assert.ok(!e.message.includes('abab'), e.message); }
  const r = run(['address', '--path', f]);
  assert.ok(!(r.stdout + r.stderr).includes('abab'));
});

test('writeNewKey refuses an invalid key before touching the disk', () => {
  const d = scratch();
  assert.throws(() => writeNewKey(path.join(d, 'k'), '0x00'), /invalid key/);
  assert.deepStrictEqual(fs.readdirSync(d), []);
  assert.ok(!validKey(undefined) && !validKey(123) && validKey('0x' + '01'.repeat(32)));
  // the group order n itself is not a key; n - 1 is
  const n = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;
  assert.ok(!validKey('0x' + n.toString(16)) && validKey('0x' + (n - 1n).toString(16)));
});

test('a key file appearing between the check and the link is refused; other link errors pass through', () => {
  const d = scratch(), key = '0x' + '01'.repeat(32);
  const link = fs.linkSync;
  try {
    fs.linkSync = () => { throw Object.assign(new Error('raced'), { code: 'EEXIST' }); };
    assert.throws(() => writeNewKey(path.join(d, 'k'), key), /exists; refusing to overwrite a key/);
    fs.linkSync = () => { throw Object.assign(new Error('EACCES: denied'), { code: 'EACCES' }); };
    assert.throws(() => writeNewKey(path.join(d, 'k'), key), /EACCES: denied/);
  } finally { fs.linkSync = link; }
  assert.deepStrictEqual(fs.readdirSync(d), [], 'the temporary file is removed on every path');
});

test('create: mode 0600 under a permissive umask; stdout is the checksummed address only', () => {
  const d = scratch(), f = path.join(d, 'k.secret');
  const r = run(['create', '--path', f], '000');
  assert.strictEqual(r.status, 0, r.stderr);
  assert.match(r.stdout, ADDR);
  assert.strictEqual(r.stderr, '');
  assert.strictEqual(fs.statSync(f).mode & 0o777, 0o600);
  const key = fs.readFileSync(f, 'utf8').trim();
  assert.ok(!r.stdout.includes(key.slice(2, 20)), 'key material in stdout');
  const a = run(['address', '--path', f]);
  assert.strictEqual(a.stdout, r.stdout, 'address reads back the same wallet');
  assert.notStrictEqual(r.stdout, run(['create', '--path', path.join(d, 'k2.secret')]).stdout, 'two keys, two addresses');
});

test('unknown command prints usage and nothing else', () => {
  const r = run([]);
  assert.strictEqual(r.status, 0);
  assert.match(r.stdout, /^commands: create/);
});
