// The three stock pools against mainnet, read-only. Nothing here sends a
// transaction: `open` runs without --execute, and the refusal tests stop it
// before anything is built.
//
//   - `pool` on each signer reports the chain's multiplier, uiPrice =
//     price × multiplierB / multiplierA, and the Token-2022 program;
//   - `balance` (needs WALLET_SECRET_PATH) reports UI amounts;
//   - a local JSON-RPC proxy forwards to mainnet and marks the stock mint
//     paused, or gives it a transfer hook: each signer's `open` must refuse
//     with exactly that reason. This is the whole CLI path, not the helper.
//
// Skipped without network; the key-needing parts skipped without a key.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import { execFile } from 'node:child_process';
import { readMints } from '../token2022.mjs';

const ROOT = new URL('..', import.meta.url).pathname;
const MAINNET = 'https://api.mainnet-beta.solana.com';
const KEY = process.env.WALLET_SECRET_PATH;
const HAVE_KEY = Boolean(KEY && fs.existsSync(KEY));
const POOLS = [
  { signer: 'signer_dlmm.mjs', pool: '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5', mint: 'MUxEsUKSMACyw5fZf68wxf5FLnZVhtU9CwH8uNNGay1', sym: 'MU', dec: 6 },
  { signer: 'signer2.mjs', pool: '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG', mint: 'DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF', sym: 'DJT', dec: 6 },
  { signer: 'signer_raydium.mjs', pool: 'D6bRhQUcR9B7bPbbqgxpE17MjyUjBtr8hHQCcJoHrrv1', mint: 'XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX', sym: 'MSFTx', dec: 8 },
];

async function rpc(method, params, url = MAINNET) {
  const r = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }), signal: AbortSignal.timeout(15000) });
  return (await r.json()).result;
}

const ONLINE = await rpc('getHealth', []).then(r => r === 'ok', () => false);
const live = { skip: ONLINE ? false : 'no network' };
const keyed = { skip: !ONLINE ? 'no network' : !HAVE_KEY ? 'WALLET_SECRET_PATH not set' : false };

// Run a signer; the result is parsed from stdout's first JSON object.
function run(signer, args, env = {}) {
  return new Promise((resolve) => {
    execFile('node', [signer, ...args], { cwd: ROOT, timeout: 240000, maxBuffer: 1 << 24,
      env: { ...process.env, LPBOT_POOL: args.find(a => POOLS.some(p => p.pool === a)) ?? '', ...env } },
    (err, stdout, stderr) => {
      let json = null;
      const i = stdout.indexOf('{');
      if (i >= 0) { try { json = JSON.parse(stdout.slice(i, stdout.lastIndexOf('}') + 1)); } catch { /* not JSON */ } }
      resolve({ code: err ? (err.code ?? 1) : 0, json, stdout, stderr });
    });
  });
}

test('pool: chain multiplier, uiPrice and token program on all three stock pools', live, async () => {
  const facts = await readMints(async ms => (await rpc('getMultipleAccounts', [ms, { encoding: 'jsonParsed' }])).value,
    POOLS.map(p => p.mint));
  for (const [i, p] of POOLS.entries()) {
    const r = await run(p.signer, ['pool', p.pool]);
    assert.equal(r.code, 0, `${p.signer}: ${r.stderr.slice(-300)}`);
    const j = r.json;
    assert.equal(j.mintA, p.mint, p.signer);
    assert.equal(j.decimalsA, p.dec, p.signer);
    assert.equal(j.multiplierA, facts[i].multiplier, p.signer);
    assert.equal(j.multiplierB, 1, p.signer);
    assert.ok(Math.abs(j.uiPrice - j.price * j.multiplierB / j.multiplierA) <= 1e-9 * j.price, p.signer);
    assert.equal(j.tokenProgramA, 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb', p.signer);
    assert.equal(j.paused, false, p.signer);
    assert.equal(j.transferHookA, null, p.signer);
  }
});

test('balance: UI amounts and a dollar total built from them', keyed, async () => {
  for (const p of POOLS) {
    const r = await run(p.signer, ['balance', p.pool]);
    assert.equal(r.code, 0, `${p.signer}: ${r.stderr.slice(-300)}`);
    const j = r.json;
    assert.equal(j.tokenA, p.sym, p.signer);
    assert.ok(j.balanceA >= 0 && j.balanceB >= 0, p.signer);
    // walletUsd = (A at the UI price + B) in dollars, plus gas SOL when it is priced
    assert.ok(j.walletUsd + 1e-6 >= (j.balanceA * j.uiPrice + j.balanceB) * j.quoteUsd, p.signer);
  }
});

// A JSON-RPC proxy to mainnet that rewrites one mint's jsonParsed extension state.
function proxy(mint, mutate) {
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const c of req) body += c;
    try {
      const reqs = JSON.parse(body);
      const up = await fetch(MAINNET, { method: 'POST', headers: { 'content-type': 'application/json' }, body,
        signal: AbortSignal.timeout(30000) });
      const text = await up.text();
      let out;
      try { out = JSON.parse(text); } catch { res.writeHead(up.status); res.end(text); return; }
      const pairs = Array.isArray(reqs) ? reqs.map((q, i) => [q, out[i]]) : [[reqs, out]];
      for (const [q, a] of pairs) {
        const keys = q?.method === 'getMultipleAccounts' ? q.params[0] : q?.method === 'getAccountInfo' ? [q.params[0]] : [];
        const vals = q?.method === 'getMultipleAccounts' ? a?.result?.value : [a?.result?.value];
        keys.forEach((k, i) => {
          const exts = vals?.[i]?.data?.parsed?.info?.extensions;
          if (k === mint && Array.isArray(exts)) mutate(exts);
        });
      }
      res.writeHead(up.status, { 'content-type': 'application/json' });
      res.end(JSON.stringify(out));
    } catch (e) {
      res.writeHead(502); res.end(String(e));
    }
  });
  return new Promise(ok => server.listen(0, '127.0.0.1', () => ok(server)));
}

const VARIANTS = [
  ['refused: mint paused', exts => { exts.find(e => e.extension === 'pausableConfig').state.paused = true; }],
  ['refused: transfer hook', exts => { exts.find(e => e.extension === 'transferHook').state.programId = 'HookProgram1111111111111111111111111111111'; }],
];

for (const [reason, mutate] of VARIANTS) {
  test(`open refuses with "${reason}" on every stock pool (dry run, through a rewriting RPC)`, keyed, async () => {
    for (const p of POOLS) {
      const server = await proxy(p.mint, mutate);
      try {
        const url = `http://127.0.0.1:${server.address().port}`;
        // a band around the live price, small caps; the refusal must come first
        const pr = await run(p.signer, ['pool', p.pool], { SOLANA_RPC_URL: url });
        assert.equal(pr.code, 0, `${p.signer} pool via proxy: ${pr.stderr.slice(-300)}`);
        assert.equal(reason.includes('paused') ? pr.json.paused : pr.json.transferHookA !== null, true, p.signer);
        const lo = (pr.json.price * 0.98).toFixed(6), hi = (pr.json.price * 1.02).toFixed(6);
        const r = await run(p.signer, ['open', p.pool, lo, hi, '0.001', '1'],
          { SOLANA_RPC_URL: url, LPBOT_ORCA_ADAPTIVE: '1' });
        assert.notEqual(r.code, 0, `${p.signer} must refuse: ${r.stdout.slice(0, 300)}`);
        assert.match(r.stderr, new RegExp(`ERROR: ${reason}`), p.signer);
        assert.equal(r.json, null, `${p.signer} printed a result: ${r.stdout.slice(0, 300)}`);
        // harvest and close name a position; DLMM and Raydium check the pool's
        // mints before they look for it, so any address shows the refusal.
        // (Orca reads the pool from the position: it needs a real one.)
        if (p.signer !== 'signer2.mjs') {
          for (const cmd of ['harvest', 'close']) {
            const w = await run(p.signer, [cmd, '11111111111111111111111111111111', '--pool', p.pool], { SOLANA_RPC_URL: url });
            assert.notEqual(w.code, 0, `${p.signer} ${cmd}`);
            assert.match(w.stderr, new RegExp(`ERROR: ${reason}`), `${p.signer} ${cmd}: ${w.stderr.slice(-200)}`);
          }
        }
      } finally {
        server.close();
      }
    }
  });
}
