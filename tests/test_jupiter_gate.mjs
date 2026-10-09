// The Node side of the Jupiter gate (venues/jupiter/gate.mjs; protocol in jupgate.py).
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { reserve, waitTurn, STALE_MS, WAIT_MS } from '../venues/jupiter/gate.mjs';

function clock(t = 1_000_000) {
  const c = { t, slept: [] };
  c.now = () => c.t;
  c.sleep = async ms => { c.slept.push(ms); c.t += ms; };
  return c;
}
const tmp = () => path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'jg-')), 'g');

test('slots are spaced and the lock is never left behind', async () => {
  const gate = tmp(), c = clock();
  const s = [];
  for (let i = 0; i < 5; i++) s.push(await reserve({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep }));
  assert.equal(s[0], c.t);
  for (let i = 1; i < 5; i++) assert.ok(Math.abs(s[i] - s[i - 1] - 1100) < 1e-3);
  assert.equal(fs.existsSync(`${gate}.lock`), false);
});

test('waitTurn sleeps only when the slot is ahead', async () => {
  const gate = tmp(), c = clock();
  await waitTurn({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep });
  await waitTurn({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep });
  assert.equal(c.slept.length, 1); assert.ok(Math.abs(c.slept[0] - 1100) < 1e-3);
});

test('garbage and far-future gate files are ignored', async () => {
  const gate = tmp(), c = clock();
  for (const junk of ['', 'abc', '99999999999']) {
    fs.writeFileSync(gate, junk);
    assert.equal(await reserve({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep }), c.t, junk);
  }
});

test('a stale lock is cleared; a live one times out without throwing', async () => {
  const gate = tmp(), c = clock(Date.now());
  fs.writeFileSync(`${gate}.lock`, '');
  fs.utimesSync(`${gate}.lock`, (c.t - STALE_MS - 5000) / 1000, (c.t - STALE_MS - 5000) / 1000);
  assert.equal(await reserve({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep }), c.t);
  fs.writeFileSync(`${gate}.lock`, '');
  fs.utimesSync(`${gate}.lock`, (c.t + 3_600_000) / 1000, (c.t + 3_600_000) / 1000);
  const t = await reserve({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep });
  assert.equal(t, c.t);
  assert.ok(c.slept.reduce((a, b) => a + b, 0) >= WAIT_MS);
});

test('an unwritable place never throws', async () => {
  const c = clock();
  assert.equal(await reserve({ gate: '/nonexistent-dir/x.gate', now: c.now, sleep: c.sleep }), c.t);
  await waitTurn({ gate: '/nonexistent-dir/x.gate', now: c.now, sleep: c.sleep });
});

// A lock file with an exact mtime (whole seconds, so mtimeMs is exact).
function lockAt(gate, ms) {
  fs.writeFileSync(`${gate}.lock`, '');
  fs.utimesSync(`${gate}.lock`, ms / 1000, ms / 1000);
  assert.equal(fs.statSync(`${gate}.lock`).mtimeMs, ms);
}

test('a lock exactly STALE_MS old is still live; one ms older is stale', async () => {
  const T = 1_700_000_000_000;
  let gate = tmp(), c = clock(T + STALE_MS);
  lockAt(gate, T);
  assert.equal(await reserve({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep }), c.t);
  assert.deepEqual(c.slept, [20]);              // waited once, then the lock was stale
  gate = tmp(); c = clock(T + STALE_MS + 1);
  lockAt(gate, T);
  await reserve({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep });
  assert.deepEqual(c.slept, []);
});

test('the wait gives up only after more than WAIT_MS', async () => {
  const T = 1_700_000_000_000, gate = tmp();
  const c = { t: T, slept: [] };
  c.now = () => c.t;
  c.sleep = async ms => { c.slept.push(ms); c.t += WAIT_MS / 2; };
  lockAt(gate, T);
  // waits at 0, WAIT_MS/2 and exactly WAIT_MS; then the lock is stale and is taken
  const t = await reserve({ gate, spacingMs: 1100, now: c.now, sleep: c.sleep });
  assert.equal(c.slept.length, 3);
  assert.equal(t, c.t);
  assert.equal(Number(fs.readFileSync(gate, 'utf8')) * 1000, c.t);   // the slot was reserved: the lock was taken
});
