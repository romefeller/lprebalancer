// The signers' endpoint loops follow rpc_policy.mjs. 2026-09-30: a 403
// "Indexed requests require a personal token" from solana-rpc.publicnode.com
// failed every swap that reached it. The same list was hard-coded in each
// signer, with its own retry regexes.
//
// The rules proved here, for every signer:
//   - a refusal (403 / indexed requests), a rate limit or a transport failure
//     moves to the next endpoint;
//   - a Jupiter error, an error after a send, a program failure, a HALT or an
//     answer from the chain is thrown at once, as itself;
//   - a failed send is never rotated or retried.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { spawnSync } from 'node:child_process';
import { JupiterError, AfterSignError, CAPS, PUBLICNODE } from '../rpc_policy.mjs';
import * as raydium from '../signer_raydium.mjs';
import * as dlmm from '../signer_dlmm.mjs';
import * as pancake from '../signer_pancake.mjs';
import * as byreal from '../signer_byreal.mjs';
import * as orca from '../signer2.mjs';

const INDEXED_403 = '403 Forbidden: {"jsonrpc":"2.0","error":{"code":-32602,"message":"Indexed requests require a personal token. Get one at: https://www.allnodes.com/publicnode"}}';
const A = 'https://a.example', B = 'https://b.example';

// name: module, how to call its withRpc with test deps, tries and pause.
const SIGNERS = {
  'signer_raydium.mjs': { mod: raydium, run: (fn, deps) => raydium.withRpc(fn, undefined, deps), tries: 2, pauseMs: 2500 },
  'signer_dlmm.mjs': { mod: dlmm, run: (fn, deps) => dlmm.withRpc(fn, undefined, deps), tries: 2, pauseMs: 2500 },
  'signer_pancake.mjs': { mod: pancake, run: (fn, deps) => pancake.withRpc(fn, true, deps), tries: 3, pauseMs: 3000 },
  'signer_byreal.mjs': { mod: byreal, run: (fn, deps) => byreal.withRpc(fn, undefined, deps), tries: 2, pauseMs: 2500 },
  'signer2.mjs': { mod: orca, run: (fn, deps) => orca.withRpc(fn, deps), tries: 2, pauseMs: 2500 },
};

// A run over [A, B] that records every endpoint reached and every pause.
function harness(s, fn, { connectFn } = {}) {
  const calls = [], pauses = [];
  const deps = {
    urls: [A, B],
    connectFn: connectFn ?? (async url => ({ url })),
    sleep: async ms => { pauses.push(ms); },
  };
  const p = s.run(async c => { calls.push(c.url); return fn(c.url, calls.length); }, deps);
  return { p, calls, pauses };
}

class SentError extends Error {}   // the name is what rpc_policy checks

const FATAL = {
  'a Jupiter error': () => new JupiterError('Jupiter 429 on /swap/v1/quote: Rate limit exceeded'),
  'an error marked sent': () => Object.assign(new Error('429 Too Many Requests while confirming'), { sent: true }),
  'a SentError': () => new SentError('sent X but could not confirm it: fetch failed'),
  'an AfterSignError': () => new AfterSignError('send failed after signing (not retried): fetch failed'),
  'a program failure': () => new Error('Earlier 429; transaction failed: {"InstructionError":[2,{"Custom":6017}]}'),
  'a simulation failure': () => new Error('simulation failed at clmm[2]: 503; nothing sent'),
  'a HALT': () => new Error('HALT present: 429 test'),
  'an answer from the chain': () => new Error('position X not found for this wallet on this pool'),
};

for (const [name, s] of Object.entries(SIGNERS)) {
  test(`${name}: static: uses rpc_policy, no hard-coded list, no own regexes`, () => {
    const src = fs.readFileSync(new URL(`../${name}`, import.meta.url), 'utf8');
    assert.ok(src.includes("from './rpc_policy.mjs'"), 'imports rpc_policy');
    assert.ok(!src.includes('solana-rpc.publicnode.com'), 'no hard-coded publicnode');
    assert.ok(/export const ENDPOINTS = endpoints\([^)]*\{ indexed: true \}\);/.test(src), 'indexed endpoint list');
    assert.ok(/overEndpoints\(urls,/.test(src), 'withRpc runs overEndpoints');
    assert.ok(!/const (TRANSIENT|RETRYABLE|RATE_LIMITED|TRANSPORT) =/.test(src), 'old regexes gone');
    assert.ok(!/429\|Too Many Requests\|rate/.test(src), 'no local rate-limit regex');
    assert.ok(/^if \(isEntry\(import\.meta\.url\)\) main\(\)/m.test(src), 'CLI guarded');
    assert.ok(!/^main\(\)/m.test(src), 'no bare CLI call');
  });

  test(`${name}: indexed reads never land on an endpoint that refuses them`, () => {
    assert.ok(s.mod.ENDPOINTS.length >= 1);
    assert.ok(!s.mod.ENDPOINTS.includes(PUBLICNODE));
    for (const u of s.mod.ENDPOINTS) assert.notEqual(CAPS[u]?.indexed, false, u);
  });

  test(`${name}: a 403 "Indexed requests" moves to the next endpoint`, async () => {
    const h = harness(s, async url => { if (url === A) throw new Error(INDEXED_403); return 'ok'; });
    assert.equal(await h.p, 'ok');
    assert.deepEqual(h.calls, [...Array(s.tries).fill(A), B]);
  });

  test(`${name}: a refusal while connecting moves on too`, async () => {
    let n = 0;
    const h = harness(s, async () => 'ok', { connectFn: async url => { n++; if (url === A) throw new Error(INDEXED_403); return { url }; } });
    assert.equal(await h.p, 'ok');
    assert.deepEqual(h.calls, [B]); assert.equal(n, s.tries + 1);
  });

  test(`${name}: rate limits and transport failures rotate, bounded`, async () => {
    for (const m of ['429 Too Many Requests', 'fetch failed', 'ECONNRESET', '503 Service Unavailable', 'Request blocked', 'timed out']) {
      const h = harness(s, async () => { throw new Error(m); });
      await assert.rejects(h.p, e => /all RPC endpoints failed/.test(e.message) && e.message.includes('a.example') && e.message.includes('b.example'), m);
      assert.equal(h.calls.length, 2 * s.tries, m);
      const one = Array.from({ length: s.tries - 1 }, (_, i) => s.pauseMs * (i + 1));
      assert.deepEqual(h.pauses, [...one, ...one], m);
    }
  });

  for (const [what, make] of Object.entries(FATAL)) {
    test(`${name}: ${what} is thrown at once, as itself`, async () => {
      const err = make();
      const h = harness(s, async () => { throw err; });
      await assert.rejects(h.p, e => e === err);
      assert.deepEqual(h.calls, [A]); assert.deepEqual(h.pauses, []);
    });
  }

  test(`${name}: a read may rotate, but nothing rotates after the send`, async () => {
    const sends = [];
    const h = harness(s, async (url, n) => {
      if (n === 1) throw new Error('429 Too Many Requests');                 // a read, before the send
      sends.push(url);
      throw Object.assign(new Error('fetch failed'), { sent: true });          // the send
    });
    await assert.rejects(h.p, /fetch failed/);
    assert.equal(sends.length, 1); assert.equal(h.calls.length, 2);
  });

  test(`${name}: the CLI still runs as a script`, () => {
    const r = spawnSync('node', [name], { cwd: new URL('..', import.meta.url).pathname, encoding: 'utf8', timeout: 60000 });
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /^commands: /m);
  });
}

// The send wrappers: a failure at or after the send is marked so that the
// loop above never rotates it.
async function oneAttempt(s, body) {
  let n = 0;
  const p = s.run(async () => { n++; return body(); }, { urls: [A, B], connectFn: async url => ({ url }), sleep: async () => {} });
  return { p, n: () => n };
}

test('signer2: a failed SDK callback is never rotated or retried', async () => {
  const r = await oneAttempt(SIGNERS['signer2.mjs'], () => orca.sendOnce({ callback: async () => { throw new Error('fetch failed'); } }));
  await assert.rejects(r.p, e => e instanceof AfterSignError && /\(not retried\): fetch failed/.test(e.message));
  assert.equal(r.n(), 1);
  assert.equal(await orca.sendOnce({ callback: async () => 'SIG' }), 'SIG');
});

test('dlmm: a first send that fails is never rotated; a later one is a partial result', async () => {
  const conn = { sendTransaction: async () => { throw new Error('429 Too Many Requests'); } };
  const r = await oneAttempt(SIGNERS['signer_dlmm.mjs'], () => dlmm.sendAll(conn, [{}], []));
  await assert.rejects(r.p, e => e instanceof AfterSignError && /\(not retried\): 429/.test(e.message));
  assert.equal(r.n(), 1);
  let k = 0;
  const conn2 = {
    sendTransaction: async () => { if (k++) throw new Error('fetch failed'); return 'SIG1'; },
    confirmTransaction: async () => ({ value: { err: null } }),
    getSignatureStatus: async () => ({ value: { err: null, confirmationStatus: 'confirmed' } }),
    getLatestBlockhash: async () => ({ blockhash: 'x', lastValidBlockHeight: 1 }),
  };
  const out = await dlmm.sendAll(conn2, [{ recentBlockhash: 'x', lastValidBlockHeight: 1 }, {}], []);
  assert.deepEqual(out.sigs, ['SIG1']); assert.match(out.error, /fetch failed/);
});

function fakeTx() { return { message: {}, sign() {}, serialize: () => Buffer.alloc(1) }; }

test('pancake: a send or confirm failure is never rotated', async () => {
  const base = { getLatestBlockhash: async () => ({ blockhash: 'x', lastValidBlockHeight: 1 }) };
  const noSend = { ...base, sendRawTransaction: async () => { throw new Error('fetch failed'); } };
  let r = await oneAttempt(SIGNERS['signer_pancake.mjs'], () => pancake.sendOne(noSend, fakeTx(), [], []));
  await assert.rejects(r.p, e => e instanceof AfterSignError && !e.sent && /\(not retried\): fetch failed/.test(e.message));
  assert.equal(r.n(), 1);
  const noConfirm = { ...base, sendRawTransaction: async () => 'SIG', confirmTransaction: async () => { throw new Error('429 Too Many Requests'); } };
  r = await oneAttempt(SIGNERS['signer_pancake.mjs'], () => pancake.sendOne(noConfirm, fakeTx(), [], []));
  await assert.rejects(r.p, e => e instanceof pancake.SentError && e.sent && e.signatures[0] === 'SIG');
  assert.equal(r.n(), 1);
});

test('byreal: sendAll never throws, so the loop never sees a send', async () => {
  const conn = { sendRawTransaction: async () => { throw new Error('fetch failed'); } };
  const out = await byreal.sendAll(conn, [fakeTx()], {});
  assert.deepEqual(out.sigs, []); assert.match(out.error, /fetch failed/);
  const conn2 = { sendRawTransaction: async () => 'SIG', confirmTransaction: async () => { throw new Error('timed out'); }, getSignatureStatuses: async () => ({ value: [null] }) };
  const out2 = await byreal.sendAll(conn2, [fakeTx()], {});
  assert.deepEqual(out2.sigs, ['SIG']); assert.match(out2.error, /not confirmed/);
  const conn3 = { ...conn2, getSignatureStatuses: async () => ({ value: [{ err: null, confirmationStatus: 'confirmed' }] }) };
  const out3 = await byreal.sendAll(conn3, [fakeTx()], {});                 // a late confirmation counts
  assert.deepEqual(out3, { sigs: ['SIG'], error: null });
});

test('raydium: a failed execute is marked sent and never rotated', async () => {
  const log = console.log; console.log = () => {};
  try {
    const built = { execute: async () => { throw new Error('fetch failed'); } };
    const r = await oneAttempt(SIGNERS['signer_raydium.mjs'], () => raydium.sendAll([built], {}));
    await assert.rejects(r.p, e => e.sent === true && /fetch failed/.test(e.message));
    assert.equal(r.n(), 1);
    let k = 0;
    const two = { execute: async () => { if (k++) throw new Error('429 Too Many Requests'); return { txId: 'SIG1' }; } };
    const r2 = await oneAttempt(SIGNERS['signer_raydium.mjs'], () => raydium.sendAll([two, two], {}));
    await assert.rejects(r2.p, e => e.sent === true && /partial send: 1\/2/.test(e.message));
    assert.equal(r2.n(), 1);
  } finally { console.log = log; }
});
