// The RPC endpoint policy (rpc_policy.mjs). 2026-09-30: a Jupiter 429 moved
// the swap to an endpoint that refuses indexed reads (403), and every swap
// that reached it failed.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { endpoints, errorKind, overEndpoints, JupiterError, AfterSignError, MAINNET, PUBLICNODE, CAPS, isEntry } from '../rpc_policy.mjs';

const noSleep = { sleep: async () => {} };
const INDEXED_403 = '403 Forbidden: {"jsonrpc":"2.0","error":{"code":-32602,"message":"Indexed requests require a personal token. Get one at: https://www.allnodes.com/publicnode"}}';

test('indexed reads never go to an endpoint that refuses them', () => {
  assert.deepEqual(endpoints({}, { indexed: true }), [MAINNET]);
  assert.deepEqual(endpoints({}), [MAINNET, PUBLICNODE]);
  assert.deepEqual(endpoints({ SOLANA_RPC_URL: 'https://keyed.example/rpc' }, { indexed: true }), ['https://keyed.example/rpc', MAINNET]);
  assert.deepEqual(endpoints({ SOLANA_RPC_URL: MAINNET }), [MAINNET, PUBLICNODE]);            // no duplicates
  for (const env of [{}, { SOLANA_RPC_URL: PUBLICNODE }, { LPBOT_RPC: 'https://x.example' }]) {
    for (const u of endpoints(env, { indexed: true })) assert.notEqual(CAPS[u]?.indexed, false, u);
  }
});

test('error kinds', () => {
  assert.equal(errorKind(new JupiterError('Jupiter 429 on /swap/v1/quote: Rate limit exceeded')), 'fatal');
  assert.equal(errorKind(new Error('Jupiter 429 on /swap/v1/quote: Rate limit exceeded')), 'fatal');   // by message too
  assert.equal(errorKind(new AfterSignError('send failed after signing: fetch failed')), 'fatal');
  class SentError extends Error {}
  assert.equal(errorKind(new SentError('sent X but could not confirm it: 429')), 'fatal');
  assert.equal(errorKind(new Error(INDEXED_403)), 'rotate');
  assert.equal(errorKind(new Error('Server responded with 429 Too Many Requests')), 'rotate');
  for (const m of ['fetch failed', 'ECONNRESET', 'ETIMEDOUT', 'socket hang up', '503 Service Unavailable', '502 Bad Gateway'])
    assert.equal(errorKind(new Error(m)), 'rotate', m);
  for (const m of ['quote is 21.0s old at send time', 'Jupiter simulated the swap and it failed', 'custom program error: 0x1', 'HALT present'])
    assert.equal(errorKind(new Error(m)), 'fatal', m);
  assert.equal(errorKind(null), 'fatal'); assert.equal(errorKind(undefined), 'fatal'); assert.equal(errorKind('429'), 'rotate');
});

test('replay 2026-09-30: a Jupiter 429 is not an RPC problem and a 403 moves on', async () => {
  const seen = [];
  await assert.rejects(overEndpoints([MAINNET, PUBLICNODE], async u => { seen.push(u); throw new JupiterError('Jupiter 429 on /swap/v1/quote: Rate limit exceeded'); }, noSleep),
    /Jupiter 429/);
  assert.deepEqual(seen, [MAINNET]);                                          // no move, no retry
  const seen2 = [];
  const out = await overEndpoints(['https://a.example', MAINNET], async u => { seen2.push(u); if (u !== MAINNET) throw new Error(INDEXED_403); return 'ok'; }, noSleep);
  assert.equal(out, 'ok'); assert.deepEqual(seen2, ['https://a.example', 'https://a.example', MAINNET]);
});

test('nothing is retried after signing', async () => {
  let n = 0;
  await assert.rejects(overEndpoints([MAINNET, PUBLICNODE], async () => { n++; throw new AfterSignError('send failed after signing (not retried): fetch failed'); }, noSleep), /after signing/);
  assert.equal(n, 1);
});

test('the final error names every endpoint', async () => {
  await assert.rejects(overEndpoints(['https://a.example', 'https://b.example'], async () => { throw new Error('429'); }, noSleep),
    e => /a\.example: 429/.test(e.message) && /b\.example: 429/.test(e.message));
});

test('tries and pauses are bounded', async () => {
  const pauses = []; let n = 0;
  await assert.rejects(overEndpoints(['https://a.example', 'https://b.example'], async () => { n++; throw new Error('fetch failed'); },
    { tries: 2, pauseMs: 100, sleep: async ms => pauses.push(ms) }));
  assert.equal(n, 4); assert.deepEqual(pauses, [100, 100]);
});

test('the swap script uses the policy and never the old list', () => {
  const src = fs.readFileSync(new URL('../swap_jupiter.mjs', import.meta.url), 'utf8');
  assert.ok(src.includes("from './rpc_policy.mjs'"));
  assert.ok(/endpoints\(process\.env, \{ indexed: true \}\)/.test(src));
  assert.ok(!src.includes('solana-rpc.publicnode.com'), 'no hard-coded endpoint list');
  assert.ok(/throw new JupiterError\(/.test(src));
  assert.ok(/throw new AfterSignError\(/.test(src));
  assert.ok(!/test\(String\(e\?\.message \?\? e\)\)\) throw e/.test(src), 'old /rate/ loop gone');
});

test('signer rules: sent, program failures and HALT are fatal whatever the text', () => {
  assert.equal(errorKind(Object.assign(new Error('429 Too Many Requests'), { sent: true })), 'fatal');
  assert.equal(errorKind(Object.assign(new Error('429 Too Many Requests'), { sent: false })), 'rotate');
  assert.equal(errorKind(Object.assign(new Error('fetch failed'), { sent: 1 })), 'rotate');   // only a true flag counts
  assert.equal(errorKind(new Error('Earlier 429; transaction failed: {"InstructionError":[2,{"Custom":6017}]}')), 'fatal');
  assert.equal(errorKind(new Error('simulation failed at clmm[2]: 503; nothing sent')), 'fatal');
  assert.equal(errorKind(new Error('HALT present: 429 test')), 'fatal');
  for (const m of ['getaddrinfo ENOTFOUND x', 'Request blocked', 'request timed out', 'timeout', '500 Internal Server Error', '501'])
    assert.equal(errorKind(new Error(m)), 'rotate', m);
  assert.equal(errorKind(new Error('505 HTTP Version Not Supported')), 'fatal');
});

test('isEntry: true only for the script node was started with', () => {
  const me = new URL(import.meta.url).pathname;
  assert.equal(isEntry(import.meta.url, ['node', me]), true);
  assert.equal(isEntry(import.meta.url, ['node', new URL('../rpc_policy.mjs', import.meta.url).pathname]), false);
  assert.equal(isEntry(import.meta.url, ['node']), false);
  assert.equal(isEntry(import.meta.url, ['node', '/no/such/file.mjs']), false);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'isentry-'));
  try {                                                               // a symlinked start path is the same file
    fs.symlinkSync(me, path.join(dir, 'link.mjs'));
    assert.equal(isEntry(import.meta.url, ['node', path.join(dir, 'link.mjs')]), true);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
