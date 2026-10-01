// signer_aerodrome.mjs end to end on an anvil fork of Base: rebalance (wrap + approve +
// swap) -> open -> fees from another trader -> harvest -> close, with --execute, against a
// LOCAL fork only. The keys are anvil's public test accounts; the LP wallet's key is never
// read. Skipped when anvil is not installed or the fork cannot start.
//
// Safety: LPBOT_RPC is a loopback URL, which baseEndpoints() makes the ONLY endpoint, and
// the test asserts the node answers web3_clientVersion "anvil/..." before any --execute.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';

const dir = path.dirname(new URL(import.meta.url).pathname);
const signer = path.join(dir, '..', 'signer_aerodrome.mjs');
const POOL = '0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59';
const WETH = '0x4200000000000000000000000000000000000006';
const USDC = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
const FORK_FROM = process.env.LPBOT_FORK_URL ?? 'https://base-rpc.publicnode.com';
// anvil's well-known test accounts 0, 1 and 2 (public keys of a public mnemonic)
const LP = { key: '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80', addr: '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266' };
const TRADER = { key: '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d', addr: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8' };
const PROFIT = '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC';

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'aero-fork-'));
fs.chmodSync(scratch, 0o700);
const keyFile = (k, name) => { const f = path.join(scratch, name); fs.writeFileSync(f, `${k}\n`, { mode: 0o600 }); return f; };
let anvil = null, url = null;

async function freePort() {
  return new Promise(r => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
}

async function rpc(method, params = []) {
  const res = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
  const j = await res.json();
  if (j.error) throw new Error(`${method}: ${j.error.message}`);
  return j.result;
}

function run(args, key, env = {}) {
  const r = spawnSync('node', [signer, ...args], {
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: key, LPBOT_RPC: url, LPBOT_POOL: POOL, ...env }, encoding: 'utf8', timeout: 180_000,
  });
  const at = r.stdout.indexOf('{');
  let json = null;
  try { json = JSON.parse(r.stdout.slice(at, r.stdout.lastIndexOf('}') + 1)); } catch { /* none */ }
  return { ...r, json };
}

test.before(async () => {
  if (!anvilPath) return;
  const port = await freePort();
  url = `http://127.0.0.1:${port}`;
  anvil = spawn(anvilPath, ['--fork-url', FORK_FROM, '--port', String(port), '--silent', '--no-storage-caching'], { stdio: 'ignore' });
  for (let i = 0; i < 60; i++) {
    try { if (/^anvil\//.test(await rpc('web3_clientVersion'))) return; } catch { /* not up yet */ }
    await new Promise(r => setTimeout(r, 500));
  }
  anvil.kill(); anvil = null;
});

test.after(() => {
  anvil?.kill();
  fs.rmSync(scratch, { recursive: true, force: true });
});

test('fork: rebalance -> open -> trade -> harvest -> close, and the pinned send', { timeout: 600_000 }, async (t) => {
  if (!anvilPath) return t.skip('anvil not installed');
  if (!anvil) return t.skip(`anvil could not fork ${FORK_FROM}`);
  assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never --execute against anything but anvil');
  assert.strictEqual(await rpc('eth_chainId'), '0x2105');
  const lp = keyFile(LP.key, 'lp.secret'), trader = keyFile(TRADER.key, 'trader.secret');
  await rpc('anvil_setBalance', [LP.addr, '0xDE0B6B3A7640000']);         // 1 ETH
  await rpc('anvil_setBalance', [TRADER.addr, '0x8AC7230489E80000']);    // 10 ETH

  // 1. ETH -> USDC through the pool's router: wrap exactly, approve exactly, swap.
  const sw = run(['rebalance', WETH, USDC, '100', '100', '--execute'], lp);
  assert.strictEqual(sw.status, 0, sw.stderr);
  assert.deepStrictEqual(sw.json.steps.map(s => s.split(' ')[0]), ['wrap', 'approve', 'swap']);
  assert.ok(sw.json.bought.amount >= sw.json.minOutAmount, 'received at least amountOutMinimum');
  assert.strictEqual(sw.json.wrapEth, sw.json.sold.amount, 'wrapped exactly what the swap sold');
  const again = run(['rebalance', WETH, USDC, '100', '100'], lp);
  assert.strictEqual(again.json.noop, true, 'at target: no second swap');

  // 2. A sleeve on USDC caps the open below the wallet.
  const pr = run(['pool'], lp).json.price;
  const lo = String(pr * 0.97), hi = String(pr * 1.03);
  const capped = run(['open', POOL, lo, hi, '0.05', '99'], lp, { LPBOT_SLEEVE: JSON.stringify({ [USDC]: 20 }) });
  assert.ok(capped.json.depositEstB <= 20, `sleeve breached: ${capped.json.depositEstB}`);

  // 3. Open: mint consumes exactly the planned amounts.
  const op = run(['open', POOL, lo, hi, '0.05', '99', '--execute'], lp);
  assert.strictEqual(op.status, 0, op.stderr);
  assert.strictEqual(op.json.sent, true);
  assert.ok(/^\d+$/.test(op.json.positionMint));
  assert.strictEqual(op.json.depositA, op.json.depositEstA, 'on-chain deposit A equals the BigInt plan');
  assert.strictEqual(op.json.depositB, op.json.depositEstB, 'on-chain deposit B equals the BigInt plan');
  const id = op.json.positionMint;
  const st = run(['status'], lp);
  assert.strictEqual(st.json.positionMint, id); assert.strictEqual(st.json.inRange, true);
  const bal = run(['balance'], lp).json;
  assert.ok(bal.eth >= 0.002, 'the open kept the gas reserve');

  // 4. Another trader swaps through the band: fees accrue.
  const tr = run(['rebalance', WETH, USDC, '0', '3000', '--execute'], trader, { LPBOT_MAX_USD: '100000' });
  assert.strictEqual(tr.status, 0, tr.stderr);
  const fees = run(['status'], lp).json;
  assert.ok(fees.feesAccruedA > 0, `no fees after a trade: ${JSON.stringify(fees)}`);

  // 5. Harvest pays what status said, to the wallet.
  const hv = run(['harvest', id, '--execute'], lp);
  assert.strictEqual(hv.status, 0, hv.stderr);
  assert.strictEqual(hv.json.harvested, id);
  assert.ok(Math.abs(hv.json.amountA - fees.feesAccruedA) <= fees.feesAccruedA * 0.01 + 1e-15);
  const nothing = run(['harvest', id, '--execute'], lp);
  assert.strictEqual(nothing.json.note, 'nothing to claim');

  // 6. Close: one transaction, the NFT burned, the read is empty.
  const cl = run(['close', id, '--execute'], lp);
  assert.strictEqual(cl.status, 0, cl.stderr);
  assert.strictEqual(cl.json.closed, id); assert.strictEqual(cl.json.signatures.length, 1);
  assert.deepStrictEqual(run(['status'], lp).json, { positions: 0, positionMint: null, pool: POOL });
  const gone = run(['close', id, '--execute'], lp);
  assert.strictEqual(gone.status, 1); assert.match(gone.stderr, /not found for this wallet/);

  // 7. Payout: to the pin only; native ETH keeps the reserve.
  const pin = { LPBOT_EVM_PROFIT_WALLET_PIN: PROFIT };
  const wrong = run(['send', USDC, '1', TRADER.addr, '--execute'], lp, pin);
  assert.strictEqual(wrong.status, 1); assert.match(wrong.stderr, /pinned profit wallet/);
  const paid = run(['send', USDC, '1.5', PROFIT, '--execute'], lp, pin);
  assert.strictEqual(paid.status, 0, paid.stderr);
  assert.strictEqual(paid.json.raw, '1500000');
  const got = run(['balance', USDC], keyFile('0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a', 'profit.secret'));
  assert.strictEqual(got.json.amount, 1.5);
  const eth = run(['balance'], lp).json.eth;
  const drain = run(['send', 'ETH', String(eth - 0.001), PROFIT, '--execute'], lp, pin);
  assert.strictEqual(drain.status, 1); assert.match(drain.stderr, /below the 0.002 gas reserve/);

  // 8. A profile's run-dir HALT refuses every write on the fork; nothing is sent.
  const runDir = path.join(scratch, 'run-aero');
  fs.mkdirSync(runDir);
  fs.writeFileSync(path.join(runDir, 'HALT'), 'test halt\n');
  const nonce = await rpc('eth_getTransactionCount', [LP.addr, 'latest']);
  const halted = run(['rebalance', WETH, USDC, '0', '50', '--execute'], lp, { LPBOT_RUN_DIR: runDir, LPBOT_MAX_USD: '100000' });
  assert.notStrictEqual(halted.status, 0); assert.match(halted.stderr, /HALT present/);
  const haltedOpen = run(['open', POOL, lo, hi, '0.05', '99', '--execute'], lp, { LPBOT_RUN_DIR: runDir });
  assert.notStrictEqual(haltedOpen.status, 0); assert.match(haltedOpen.stderr, /HALT present/);
  const relative = run(['send', USDC, '0.1', PROFIT, '--execute'], lp, { ...pin, LPBOT_RUN_DIR: 'run/aero' });
  assert.notStrictEqual(relative.status, 0); assert.match(relative.stderr, /not an absolute path/);
  assert.strictEqual(await rpc('eth_getTransactionCount', [LP.addr, 'latest']), nonce, 'a HALTed write sent a transaction');
  fs.rmSync(path.join(runDir, 'HALT'));
  const free = run(['send', USDC, '0.1', PROFIT, '--execute'], lp, { ...pin, LPBOT_RUN_DIR: runDir });
  assert.strictEqual(free.status, 0, free.stderr);                      // the control: no HALT, the write goes
});
