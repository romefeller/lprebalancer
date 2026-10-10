// owner/index.html, the owners' signing page, run for real: the page in
// jsdom, a Wallet Standard wallet holding the owner's key, and a local
// JSON-RPC server backed by a fork of mainnet (tests/_svm.mjs). The owner
// pastes a request, the page shows it, the wallet signs, the page sends.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'module';
import { fileURLToPath } from 'node:url';
import { connectedVault } from './_vault.mjs';
import { request } from '../chains/solana/vault/cli.mjs';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');
const { Keypair, Transaction, VersionedTransaction } = require('@solana/web3.js');
const bs58 = require('bs58').default;

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PAGE = fs.readFileSync(path.join(ROOT, 'owner/index.html'), 'utf8');
const WEB3 = fs.readFileSync(require.resolve('@solana/web3.js/lib/index.iife.min.js'), 'utf8');

// The few JSON-RPC calls the page makes, answered by the fork.
function rpcServer(fork) {
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const { id, method, params } = JSON.parse(body);
    let result;
    if (method === 'getLatestBlockhash') {
      result = { context: { slot: 1 }, value: { blockhash: fork.svm.latestBlockhash(), lastValidBlockHeight: 1e9 } };
    } else if (method === 'sendTransaction') {
      const raw = params[1]?.encoding === 'base64' ? Buffer.from(params[0], 'base64') : Buffer.from(bs58.decode(params[0]));
      const tx = VersionedTransaction.deserialize(raw);
      const r = await fork.sendSigned(tx);
      if (!r.ok) {
        res.end(JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32002, message: 'Transaction simulation failed', data: { logs: r.logs } } }));
        return;
      }
      result = bs58.encode(tx.signatures[0]);
    } else if (method === 'getSignatureStatuses') {
      result = { context: { slot: 1 }, value: params[0].map(() => ({ slot: 1, confirmations: 1, err: null, confirmationStatus: 'confirmed' })) };
    } else if (method === 'getBlockHeight') {
      result = 1;
    } else {
      res.end(JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32601, message: `fork RPC: ${method} not served` } }));
      return;
    }
    res.end(JSON.stringify({ jsonrpc: '2.0', id, result }));
  });
  return new Promise(r => server.listen(0, '127.0.0.1', () => r(server)));
}

// A wallet as the Wallet Standard describes one, holding `key`.
function standardWallet(key, name = 'Test Wallet') {
  const account = { address: key.publicKey.toBase58(), publicKey: key.publicKey.toBytes(), chains: ['solana:mainnet'], features: [] };
  return {
    version: '1.0.0', name, icon: 'data:image/svg+xml,<svg/>', chains: ['solana:mainnet'], accounts: [account],
    features: {
      'standard:connect': { version: '1.0.0', connect: async () => ({ accounts: [account] }) },
      'solana:signTransaction': { version: '1.0.0', supportedTransactionVersions: ['legacy'],
        signTransaction: async ({ transaction }) => {
          const tx = Transaction.from(transaction);
          tx.partialSign(key);
          return [{ signedTransaction: tx.serialize({ requireAllSignatures: false }) }];
        } },
    },
  };
}

async function openPage(rpcUrl) {
  const html = PAGE.replace(/<script src="https:\/\/unpkg\.com\/@solana\/web3\.js@[^"]+"><\/script>/, `<script>${WEB3}</script>`);
  assert.notEqual(html, PAGE, 'the page loads web3.js from its pinned URL');
  const dom = new JSDOM(html, { runScripts: 'dangerously', url: 'http://localhost:8765/', pretendToBeVisual: true,
    beforeParse(w) { w.fetch = globalThis.fetch; w.crypto = globalThis.crypto; w.TextEncoder = TextEncoder; w.TextDecoder = TextDecoder; } });
  const w = dom.window, doc = w.document;
  doc.getElementById('rpc').value = rpcUrl;
  return { w, doc };
}

function register(w, wallet) {
  w.dispatchEvent(new w.CustomEvent('wallet-standard:register-wallet', { detail: ({ register: reg }) => reg(wallet) }));
}

const wait = async (fn, ms = 20_000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { const v = fn(); if (v) return v; await new Promise(r => setTimeout(r, 50)); }
  throw new Error('timed out waiting');
};

function paste(w, doc, text) {
  const t = doc.getElementById('request');
  t.value = text;
  t.dispatchEvent(new w.Event('input'));
}

test('the owner withdraws through the page: shown, signed by the wallet, sent', { timeout: 300_000 }, async () => {
  const { fork, connection, out, env, owner } = await connectedVault();
  fork.fund(out.vault, 0.25);
  const server = await rpcServer(fork);
  try {
    const { w, doc } = await openPage(`http://127.0.0.1:${server.address().port}`);
    register(w, standardWallet(owner));
    const connectButton = await wait(() => [...doc.querySelectorAll('#wallets button')].find(b => /Test Wallet/.test(b.textContent)));
    connectButton.click();
    await wait(() => doc.getElementById('connected').textContent.includes(owner.publicKey.toBase58()));
    const req = await request('withdraw', { owner: owner.publicKey.toBase58(), to: owner.publicKey.toBase58(), mint: 'SOL', amount: 'all' }, { connection, env });
    paste(w, doc, JSON.stringify(req));
    const shown = doc.getElementById('decoded').textContent;
    assert.match(shown, /withdraw 0.25 SOL/);
    assert.match(shown, /Squads smart account \(your vault\)/);
    assert.equal(doc.getElementById('send').disabled, false);
    const before = fork.balance(owner.publicKey);
    doc.getElementById('send').click();
    await wait(() => /Done\./.test(doc.getElementById('result').textContent) || /warn/.test(doc.getElementById('result').innerHTML));
    assert.match(doc.getElementById('result').textContent, /Done\./, doc.getElementById('result').textContent);
    assert.equal(fork.balance(out.vault), 0);
    assert.equal(fork.balance(owner.publicKey) - before, 250_000_000 - 5000);
    w.close();
  } finally { server.close(); }
});

test('the page refuses a request for another owner, one paid by someone else, and anything not a request', { timeout: 300_000 }, async () => {
  const { fork, connection, out, env, owner } = await connectedVault();
  fork.fund(out.vault, 0.1);
  const server = await rpcServer(fork);
  try {
    const { w, doc } = await openPage(`http://127.0.0.1:${server.address().port}`);
    const other = Keypair.generate();
    register(w, standardWallet(other, 'Other Wallet'));
    (await wait(() => [...doc.querySelectorAll('#wallets button')].find(b => /Other Wallet/.test(b.textContent)))).click();
    await wait(() => doc.getElementById('connected').textContent.includes(other.publicKey.toBase58()));
    const req = await request('withdraw', { owner: owner.publicKey.toBase58(), to: owner.publicKey.toBase58(), mint: 'SOL', amount: 'all' }, { connection, env });
    paste(w, doc, JSON.stringify(req));
    assert.equal(doc.getElementById('send').disabled, true);
    assert.match(doc.getElementById('result').textContent, /not the owner this request names/);
    // a request edited to be paid by someone else is not shown at all
    paste(w, doc, JSON.stringify({ ...req, owner: other.publicKey.toBase58() }));
    assert.match(doc.getElementById('decoded').textContent, /paid by someone other than the owner/);
    assert.equal(doc.getElementById('send').disabled, true);
    paste(w, doc, '{"kind": "something else"}');
    assert.match(doc.getElementById('decoded').textContent, /not a vault request/);
    w.close();
  } finally { server.close(); }
});
