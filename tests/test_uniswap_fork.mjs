// signer_uniswap.mjs end to end on an anvil fork of Unichain: rebalance (best of the v3
// pool and the hookless v4 pool) -> open -> fees from another trader -> harvest -> close
// -> pinned send -> run-dir HALT, with --execute, against a LOCAL fork only. The wallets are
// anvil's public test accounts, funded on the fork from the pool's own token balances
// (impersonated); the LP wallet's key is never read. Skipped when anvil is not installed or
// the fork cannot start.
//
// Safety: LPBOT_RPC is a loopback URL, which unichainEndpoints() makes the ONLY endpoint,
// and the test asserts the node answers web3_clientVersion "anvil/..." before any --execute.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { encodeFunctionData, parseAbi } from 'viem';
import { V3, V4, USDC, HYPE } from '../evm/unichain.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const signer = path.join(dir, '..', 'signer_uniswap.mjs');
const POOL = '0x5d3e7f5dA38FBf476E8B36E3b90D02FC4C1A08C3';
const FORK_FROM = process.env.LPBOT_UNICHAIN_FORK_URL ?? 'https://unichain-rpc.publicnode.com';
const LP = { key: '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80', addr: '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266' };
const TRADER = { key: '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d', addr: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8' };
const PROFIT = '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC';
const OTHER = '0x90F79bf6EB2c4f870365E785982E1f101E93b906';
const ERC20 = parseAbi(['function transfer(address to, uint256 amount) returns (bool)']);

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'uni-fork-'));
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
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: key, LPBOT_RPC: url, LPBOT_POOL: POOL, LPBOT_EVM_PROFIT_WALLET_PIN: PROFIT, LPBOT_EVM_MAX_GWEI: '5', ...env },
    encoding: 'utf8', timeout: 180_000,
  });
  const at = r.stdout.indexOf('{');
  let json = null;
  try { json = JSON.parse(r.stdout.slice(at, r.stdout.lastIndexOf('}') + 1)); } catch { /* none */ }
  return { ...r, json };
}

// Give `to` some of the pool's own token balance: impersonate the pool on the fork.
async function fund(token, to, raw) {
  await rpc('anvil_impersonateAccount', [POOL]);
  await rpc('anvil_setBalance', [POOL, '0xDE0B6B3A7640000']);
  const hash = await rpc('eth_sendTransaction', [{ from: POOL, to: token, data: encodeFunctionData({ abi: ERC20, functionName: 'transfer', args: [to, raw] }) }]);
  let rc = null;
  for (let i = 0; i < 40 && !rc; i++) { rc = await rpc('eth_getTransactionReceipt', [hash]); if (!rc) await new Promise(r => setTimeout(r, 250)); }
  assert.strictEqual(rc?.status, '0x1', 'funding transfer');
  await rpc('anvil_stopImpersonatingAccount', [POOL]);
}

async function targets(hashes) {
  return Promise.all(hashes.map(async h => (await rpc('eth_getTransactionByHash', [h])).to.toLowerCase()));
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

test('fork: rebalance -> open -> trade -> harvest -> close, pinned send, HALT', { timeout: 900_000 }, async (t) => {
  if (!anvilPath) return t.skip('anvil not installed');
  if (!anvil) return t.skip(`anvil could not fork ${FORK_FROM}`);
  assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never --execute against anything but anvil');
  assert.strictEqual(await rpc('eth_chainId'), '0x82');
  const lp = keyFile(LP.key, 'lp.secret'), trader = keyFile(TRADER.key, 'trader.secret');
  const pl = run(['pool'], lp);
  assert.strictEqual(pl.status, 0, pl.stderr);
  assert.deepStrictEqual([pl.json.symbolA, pl.json.symbolB, pl.json.feePips, pl.json.tickSpacing], ['USDC', 'HYPE', 3000, 60]);
  assert.ok(pl.json.referenceDeviation < 0.02, `fork price far from the reference: ${pl.json.referenceDeviation}`);
  await rpc('anvil_setBalance', [LP.addr, '0x2386F26FC10000']);        // 0.01 ETH: gas only
  await rpc('anvil_setBalance', [TRADER.addr, '0x2386F26FC10000']);
  await fund(USDC, LP.addr, 200_000_000n);                              // 200 USDC, no HYPE

  // 1. USDC -> HYPE toward 50/50: exact approvals, the better-quoting route.
  const sw = run(['rebalance', USDC, HYPE, '100', '100', '--execute'], lp);
  assert.strictEqual(sw.status, 0, sw.stderr + sw.stdout);
  assert.ok(sw.json.bought.amount >= sw.json.minOutAmount, 'received at least amountOutMinimum');
  assert.strictEqual(sw.json.routesQuoted.length, 2, 'both the v3 and the v4 pool quoted');
  const best = Math.max(...sw.json.routesQuoted.map(r => r.amountOut));
  assert.strictEqual(sw.json.quoteOutAmount, best, 'took the best quote');
  const to = await targets(sw.json.signatures);
  const viaV4 = sw.json.routePlan[0].label.startsWith('uniswap-v4');
  t.diagnostic(`route ${sw.json.routePlan[0].label}; quotes ${JSON.stringify(sw.json.routesQuoted)}; impact ${sw.json.priceImpactPercent}%`);
  assert.strictEqual(to.at(-1), (viaV4 ? V4.universalRouter : V3.router).toLowerCase(), 'the swap went to the route\'s router');
  if (viaV4) assert.deepStrictEqual(to.slice(0, -1), [USDC.toLowerCase(), V4.permit2.toLowerCase()], 'approve Permit2, then Permit2 -> router');
  const again = run(['rebalance', USDC, HYPE, '100', '100'], lp);
  assert.strictEqual(again.json.noop, true, 'at target: no second swap');

  // 2. A sleeve on USDC caps the open below the wallet.
  const pr = run(['pool'], lp).json.price;
  const lo = String(pr / 1.02), hi = String(pr * 1.02);
  const w = run(['balance'], lp).json;
  const capA = String(w.balanceA), capB = String(w.balanceB);
  const capped = run(['open', POOL, lo, hi, capA, capB], lp, { LPBOT_SLEEVE: JSON.stringify({ [USDC]: 20 }) });
  assert.ok(capped.json.depositEstA <= 20, `sleeve breached: ${capped.json.depositEstA}`);

  // 3. Open: mint consumes exactly the planned amounts.
  const op = run(['open', POOL, lo, hi, capA, capB, '--execute'], lp);
  assert.strictEqual(op.status, 0, op.stderr + op.stdout);
  assert.ok(/^\d+$/.test(op.json.positionMint));
  assert.strictEqual(op.json.depositA, op.json.depositEstA, 'on-chain deposit A equals the BigInt plan');
  assert.strictEqual(op.json.depositB, op.json.depositEstB, 'on-chain deposit B equals the BigInt plan');
  assert.ok(op.json.tickLower % 60 === 0 && op.json.tickUpper % 60 === 0);
  assert.strictEqual((await targets(op.json.signatures)).at(-1), V3.npm.toLowerCase(), 'minted on the NPM');
  const id = op.json.positionMint;
  const st = run(['status'], lp);
  assert.strictEqual(st.json.positionMint, id); assert.strictEqual(st.json.inRange, true);
  assert.ok(Math.abs(st.json.positionUsd - op.json.depositUsd) < op.json.depositUsd * 0.01, 'marked near the deposit');

  // 4. Another trader swaps through the v3 pool both ways: fees accrue in A and B.
  await fund(USDC, TRADER.addr, 3_000_000_000n);
  // Straight through the v3 router into the held pool: the signer's own swap takes the best
  // quote, and once the v4 pool quoted better (2026-10-08) the trade skipped our pool.
  await rpc('anvil_impersonateAccount', [TRADER.addr]);
  const send = async (to, data) => {
    const h = await rpc('eth_sendTransaction', [{ from: TRADER.addr, to, data }]);
    let rc = null;
    for (let i = 0; i < 40 && !rc; i++) { rc = await rpc('eth_getTransactionReceipt', [h]); if (!rc) await new Promise(r => setTimeout(r, 250)); }
    assert.strictEqual(rc?.status, '0x1', `trader tx to ${to}`);
  };
  const TRADE = 2_000_000_000n;                                           // 2000 USDC
  await send(USDC, encodeFunctionData({ abi: parseAbi(['function approve(address,uint256) returns (bool)']), functionName: 'approve', args: [V3.router, TRADE] }));
  await send(V3.router, encodeFunctionData({ abi: parseAbi(['struct P { address tokenIn; address tokenOut; uint24 fee; address recipient; uint256 amountIn; uint256 amountOutMinimum; uint160 sqrtPriceLimitX96; }', 'function exactInputSingle(P params) payable returns (uint256)']),
    functionName: 'exactInputSingle', args: [{ tokenIn: USDC, tokenOut: HYPE, fee: 3000, recipient: TRADER.addr, amountIn: TRADE, amountOutMinimum: 0n, sqrtPriceLimitX96: 0n }] }));
  await rpc('anvil_stopImpersonatingAccount', [TRADER.addr]);
  const fees = run(['status'], lp).json;
  t.diagnostic(`fees after a $2000 trade: A ${fees.feesAccruedA} B ${fees.feesAccruedB} ($${fees.feesAccrued_USD})`);
  assert.ok(fees.feesAccruedA > 0 || fees.feesAccruedB > 0, `no fees after a trade: ${JSON.stringify(fees)}`);

  // 5. Harvest pays what status said.
  const hv = run(['harvest', id, '--execute'], lp);
  assert.strictEqual(hv.status, 0, hv.stderr);
  assert.deepStrictEqual(await targets(hv.json.signatures), [V3.npm.toLowerCase()]);
  assert.ok(Math.abs(hv.json.amountA - fees.feesAccruedA) <= fees.feesAccruedA * 0.01 + 1e-12);
  assert.ok(Math.abs(hv.json.amountB - fees.feesAccruedB) <= fees.feesAccruedB * 0.01 + 1e-18);
  assert.strictEqual(run(['harvest', id, '--execute'], lp).json.note, 'nothing to claim');

  // 6. Close: one transaction, the NFT burned, the read is empty.
  const cl = run(['close', id, '--execute'], lp);
  assert.strictEqual(cl.status, 0, cl.stderr + cl.stdout);
  assert.strictEqual(cl.json.closed, id); assert.strictEqual(cl.json.signatures.length, 1);
  assert.deepStrictEqual(run(['status'], lp).json, { positions: 0, positionMint: null, pool: POOL });

  // 7. Payout: the pin only.
  const bad = run(['send', USDC, '1', OTHER, '--execute'], lp);
  assert.notStrictEqual(bad.status, 0); assert.match(bad.stderr, /not the pinned profit wallet/);
  const pay = run(['send', USDC, '1', PROFIT, '--execute'], lp);
  assert.strictEqual(pay.status, 0, pay.stderr);
  assert.strictEqual(run(['balance', USDC], keyFile(TRADER.key, 'x.secret')).json.symbol, 'USDC');

  // 8. A HALT in the run directory refuses every write.
  const runDir = fs.mkdtempSync(path.join(scratch, 'run-'));
  fs.writeFileSync(path.join(runDir, 'HALT'), 'test halt');
  const halted = run(['send', USDC, '1', PROFIT, '--execute'], lp, { LPBOT_RUN_DIR: runDir });
  assert.notStrictEqual(halted.status, 0); assert.match(halted.stderr, /HALT present/);
});
