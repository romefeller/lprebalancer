// No signer confirms a transaction over a websocket (2026-10-12 20:09:12Z): the
// Alchemy RPC serves no signatureSubscribe. A sol-swing Raydium open landed,
// the SDK's execute() failed on the subscription, and the venue's breaker
// counted a failure (20 min backoff). Every send polls (shared/tx_send.mjs).
//
// Two checks: the code names no websocket confirmation (stripped of comments),
// and a connection whose subscription methods throw carries every send path
// that the unit tests can drive.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { sendUntilLanded } from '../shared/tx_send.mjs';

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Transaction, TransactionInstruction } = require('@solana/web3.js');
const ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');

// Every script the loop spawns or the onboarding page runs.
function sources() {
  const out = [];
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (/\.(mjs|js)$/.test(e.name)) out.push(p);
    }
  };
  for (const d of ['venues', 'chains', 'shared', 'onboard']) walk(path.join(ROOT, d));
  return out.concat([path.join(ROOT, 'telegram_bridge.mjs')]);
}

// The code without // and /* */ comments (strings are kept: none of these
// names is ever a string the code sends).
const code = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').split('\n').map(l => l.replace(/(^|[^:'"`])\/\/.*$/, '$1')).join('\n');

const WEBSOCKET = [
  [/sendAndConfirmTransaction|sendAndConfirmRawTransaction/, 'web3.js sendAndConfirm* confirms over a websocket'],
  [/\bconfirmTransaction\s*\(/, 'Connection.confirmTransaction subscribes to the signature'],
  [/signatureSubscribe|onSignature\s*\(|signatureNotifications|rpcSubscriptions/, 'a signature subscription'],
  [/sendAndConfirm\s*:\s*true/, "an SDK's execute({ sendAndConfirm: true }) confirms over a websocket"],
  [/\.execute\s*\(\s*\{/, "an SDK's execute() sends and confirms itself"],
];

test('no signer, chain script or page names a websocket confirmation', () => {
  const found = [];
  for (const f of sources()) {
    const src = code(fs.readFileSync(f, 'utf8'));
    for (const [re, why] of WEBSOCKET) if (re.test(src)) found.push(`${path.relative(ROOT, f)}: ${why}`);
  }
  assert.deepEqual(found, []);
});

test('the check finds what it looks for (it would fail on the old Raydium send)', () => {
  const old = "const { txId } = await built.execute({ sendAndConfirm: true, skipPreflight: false });\n"
    + "await sendAndConfirmTransaction(connection, tx, [payer]);\nawait connection.confirmTransaction(sig);";
  const hits = WEBSOCKET.filter(([re]) => re.test(code(old))).length;
  assert.equal(hits, 4);
  assert.equal(WEBSOCKET.filter(([re]) => re.test(code('// built.execute({ sendAndConfirm: true }) was the old way'))).length, 0);
});

// A connection that answers a poll and throws on any subscription.
function pollOnly(statuses) {
  const calls = [];
  const no = (name) => () => { calls.push(name); throw new Error(`${name}: websocket confirmation used`); };
  return { calls, conn: {
    getLatestBlockhash: async () => ({ blockhash: Keypair.generate().publicKey.toBase58(), lastValidBlockHeight: 100 }),
    sendRawTransaction: async () => 'SIG',
    getSignatureStatuses: async () => ({ value: [statuses.shift() ?? { err: null, confirmationStatus: 'confirmed' }] }),
    getBlockHeight: async () => 0,
    confirmTransaction: no('confirmTransaction'), onSignature: no('onSignature'), onSignatureWithOptions: no('onSignatureWithOptions'),
    _rpcWebSocket: { call: no('signatureSubscribe') },
  } };
}

test('the polled senders land a transaction on a connection without subscriptions', async () => {
  const { calls, conn } = pollOnly([null, { err: null, confirmationStatus: 'processed' }]);
  assert.equal(await sendUntilLanded(conn, Buffer.alloc(1), 100, { sleep: async () => {} }), 'SIG');
  const raydium = await import('../venues/raydium_clmm/signer.mjs');
  const payer = Keypair.generate();
  const tx = new Transaction().add(new TransactionInstruction({ programId: new PublicKey('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'), keys: [], data: Buffer.from('x') }));
  assert.equal(await raydium.sendLanded(conn, payer, { transaction: tx }, { sleep: async () => {} }), 'SIG');
  const dlmm = await import('../venues/meteora_dlmm/signer.mjs');
  assert.equal(await dlmm.sendIxs(conn, { transaction: () => ({ serialize: () => Buffer.alloc(1) }) }, []), 'SIG');
  const byreal = await import('../venues/byreal/signer.mjs');
  assert.deepEqual(await byreal.sendAll(conn, [{ message: {}, sign() {}, serialize: () => Buffer.alloc(1) }], payer), { sigs: ['SIG'], error: null });
  assert.deepEqual(calls, []);
});
