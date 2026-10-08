// signer_uniswap.mjs with LPBOT_CHAIN=polygon end to end on an anvil fork of Polygon PoS:
// rebalance (the held v3 pool only: Polygon has no v4 route) -> open -> fees from another
// trader -> harvest -> close -> pinned send -> run-dir HALT, with --execute, against a LOCAL
// fork only. Gas is native POL, outside the pool; WPOL is an ordinary ERC-20. The wallets are
// anvil's public test accounts, funded on the fork from the pool's own token balances
// (impersonated); the LP wallet's key is never read. Skipped when anvil is not installed or
// the fork cannot start.
//
// Safety: LPBOT_RPC is a loopback URL, which polygonEndpoints() makes the ONLY endpoint,
// and the test asserts the node answers web3_clientVersion "anvil/..." before any --execute.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { encodeFunctionData, parseAbi } from 'viem';
import { V3, WPOL, USDT0 } from '../evm/polygon.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const signer = path.join(dir, '..', 'signer_uniswap.mjs');
const POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7';
// publicnode first, then dRPC: a busy public endpoint must not turn the test into a skip.
const FORK_URLS = process.env.LPBOT_POLYGON_FORK_URL ? [process.env.LPBOT_POLYGON_FORK_URL]
  : ['https://polygon-bor-rpc.publicnode.com', 'https://polygon.drpc.org'];
let FORK_FROM = FORK_URLS.join(' or ');
const LP = { key: '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80', addr: '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266' };
const TRADER = { key: '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d', addr: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8' };
const PROFIT = '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC';
const OTHER = '0x90F79bf6EB2c4f870365E785982E1f101E93b906';
const ERC20 = parseAbi(['function transfer(address to, uint256 amount) returns (bool)']);

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'poly-fork-'));
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
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: key, LPBOT_RPC: url, LPBOT_POOL: POOL, LPBOT_EVM_PROFIT_WALLET_PIN: PROFIT, LPBOT_CHAIN: 'polygon', ...env },
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

// Gas used by each transaction, from its receipt.
async function gasOf(hashes) {
  return Promise.all(hashes.map(async h => Number(BigInt((await rpc('eth_getTransactionReceipt', [h])).gasUsed))));
}
const sum = xs => xs.reduce((a, b) => a + b, 0);
// One re-centre (close + swap + open) may cost at most this much gas: measured 0.95-1.01M
// (a mint on fresh ticks pays their initialisation), about $0.03 at 281 gwei and POL $0.10.
// A change that adds a transaction to every cycle fails here.
const RECENTRE_GAS_BUDGET = 1_100_000;

async function targets(hashes) {
  return Promise.all(hashes.map(async h => (await rpc('eth_getTransactionByHash', [h])).to.toLowerCase()));
}

test.before(async () => {
  if (!anvilPath) return;
  for (const from of FORK_URLS) {
    const port = await freePort();
    url = `http://127.0.0.1:${port}`;
    anvil = spawn(anvilPath, ['--fork-url', from, '--port', String(port), '--silent', '--no-storage-caching'], { stdio: 'ignore' });
    for (let i = 0; i < 60; i++) {
      try { if (/^anvil\//.test(await rpc('web3_clientVersion'))) { FORK_FROM = from; return; } } catch { /* not up yet */ }
      await new Promise(r => setTimeout(r, 500));
    }
    anvil.kill(); anvil = null;
  }
});

test.after(() => {
  anvil?.kill();
  fs.rmSync(scratch, { recursive: true, force: true });
});

test('fork (Polygon): rebalance -> open -> trade -> harvest -> close, pinned send, HALT', { timeout: 900_000 }, async (t) => {
  if (!anvilPath) return t.skip('anvil not installed');
  if (!anvil) return t.skip(`anvil could not fork ${FORK_FROM}`);
  assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never --execute against anything but anvil');
  assert.strictEqual(await rpc('eth_chainId'), '0x89');
  const lp = keyFile(LP.key, 'lp.secret'), trader = keyFile(TRADER.key, 'trader.secret');
  const pl = run(['pool'], lp);
  assert.strictEqual(pl.status, 0, pl.stderr);
  assert.deepStrictEqual([pl.json.dex, pl.json.chain, pl.json.symbolA, pl.json.symbolB, pl.json.feePips, pl.json.tickSpacing],
    ['uniswap-v3-polygon', 'polygon', 'WPOL', 'USDT0', 500, 10]);
  assert.strictEqual(pl.json.quoteUsd, 1);
  assert.ok(pl.json.referenceDeviation < 0.02, `fork price far from the reference: ${pl.json.referenceDeviation}`);
  // The chain module of another chain refuses this pool: Unichain's factory is not Polygon's.
  const wrong = run(['pool'], lp, { LPBOT_CHAIN: 'unichain' });
  assert.notStrictEqual(wrong.status, 0);
  await rpc('anvil_setBalance', [LP.addr, '0x8AC7230489E80000']);         // 10 POL: gas only
  await rpc('anvil_setBalance', [TRADER.addr, '0x8AC7230489E80000']);
  await fund(USDT0, LP.addr, 200_000_000n);                               // 200 USDT0, no WPOL

  // 0a. Wrap: native POL above the gas into WPOL (a native POL deposit); the reserve is kept.
  await rpc('anvil_setBalance', [LP.addr, '0x56BC75E2D63100000']);       // 100 POL
  const wr = run(['wrap', '60', '--execute'], lp);
  assert.strictEqual(wr.status, 0, wr.stderr + wr.stdout);
  assert.deepStrictEqual(await targets(wr.json.signatures), [WPOL.toLowerCase()]);
  assert.strictEqual(run(['balance'], lp).json.balanceA, 60, '60 WPOL from 60 POL');
  const over = run(['wrap', '39.9999', '--execute'], lp, { LPBOT_GAS_RESERVE_NATIVE: '1' });
  assert.notStrictEqual(over.status, 0); assert.match(over.stderr + over.stdout, /gas reserve/);
  // send the 60 WPOL away so the rest of the test starts from USDT0 only
  const away = run(['send', WPOL, '60', PROFIT, '--execute'], lp);
  assert.strictEqual(away.status, 0, away.stderr);
  await rpc('anvil_setBalance', [LP.addr, '0x8AC7230489E80000']);         // back to 10 POL

  // 0. Gas POL is not WPOL: the wallet view shows no side A, and POL as the gas.
  const w0 = run(['balance'], lp).json;
  assert.strictEqual(w0.balanceA, 0, 'native POL is never counted as WPOL');
  assert.ok(w0.sol >= 9.9 && w0.eth === w0.sol, `gas POL read: ${w0.sol}`);
  assert.strictEqual(run(['balance', 'POL'], lp).json.symbol, 'POL');

  // 1. USDT0 -> WPOL toward 50/50 through the held pool: exact approval, then the router.
  const sw = run(['rebalance', WPOL, USDT0, '100', '100', '--execute'], lp);
  assert.strictEqual(sw.status, 0, sw.stderr + sw.stdout);
  assert.ok(sw.json.bought.amount >= sw.json.minOutAmount, 'received at least amountOutMinimum');
  assert.strictEqual(sw.json.routesQuoted.length, 1, 'only the v3 pool quoted: no v4 on Polygon');
  assert.match(sw.json.routePlan[0].label, /^uniswap-v3 0.05%/);
  t.diagnostic(`route ${sw.json.routePlan[0].label}; impact ${sw.json.priceImpactPercent}%`);
  const to = await targets(sw.json.signatures);
  assert.deepStrictEqual(to, [USDT0.toLowerCase(), V3.router.toLowerCase()], 'approve USDT0, then swap on SwapRouter02');
  assert.strictEqual(run(['rebalance', WPOL, USDT0, '100', '100'], lp).json.noop, true, 'at target: no second swap');
  assert.ok(run(['balance'], lp).json.sol < w0.sol, 'gas paid in POL');
  const gas = { swap: await gasOf(sw.json.signatures) };

  // 2. A sleeve on USDT0 caps the open below the wallet.
  const pr = run(['pool'], lp).json.price;
  const lo = String(pr / 1.02), hi = String(pr * 1.02);
  const w = run(['balance'], lp).json;
  const capA = String(w.balanceA), capB = String(w.balanceB);
  const capped = run(['open', POOL, lo, hi, capA, capB], lp, { LPBOT_SLEEVE: JSON.stringify({ [USDT0]: 20 }) });
  assert.ok(capped.json.depositEstB <= 20, `sleeve breached: ${capped.json.depositEstB}`);

  // 3. Open: the position manager recomputes the liquidity from amountDesired, so it pulls
  // at most the plan and may pull a few wei less (WPOL has 18 decimals: one double step at
  // ~1e21 wei is ~131k wei). Never more than planned; within 1e-12 of it.
  const op = run(['open', POOL, lo, hi, capA, capB, '--execute'], lp);
  assert.strictEqual(op.status, 0, op.stderr + op.stdout);
  assert.ok(/^\d+$/.test(op.json.positionMint));
  for (const [got, plan, side] of [[op.json.depositA, op.json.depositEstA, 'A'], [op.json.depositB, op.json.depositEstB, 'B']]) {
    assert.ok(got <= plan, `deposit ${side} ${got} above the plan ${plan}`);
    assert.ok(got >= plan * (1 - 1e-12), `deposit ${side} ${got} far below the plan ${plan}`);
  }
  assert.ok(op.json.tickLower % 10 === 0 && op.json.tickUpper % 10 === 0);
  assert.strictEqual((await targets(op.json.signatures)).at(-1), V3.npm.toLowerCase(), 'minted on the NPM');
  const id = op.json.positionMint;
  gas.open = await gasOf(op.json.signatures);
  const st = run(['status'], lp);
  assert.strictEqual(st.json.positionMint, id); assert.strictEqual(st.json.inRange, true);
  assert.ok(Math.abs(st.json.positionUsd - op.json.depositUsd) < op.json.depositUsd * 0.01, 'marked near the deposit');

  // 3b. Increase: idle cash into the open position, at its ticks, no close and no swap.
  await fund(WPOL, LP.addr, 200n * 10n ** 18n);                         // 200 WPOL
  await fund(USDT0, LP.addr, 20_000_000n);                              // 20 USDT0
  const before = run(['status'], lp).json;
  const inc = run(['increase', id, '200', '20', '--execute'], lp);
  assert.strictEqual(inc.status, 0, inc.stderr + inc.stdout);
  assert.strictEqual(inc.json.positionMint, id);
  assert.ok(inc.json.depositA <= 200 && inc.json.depositB <= 20, `increase above its caps: ${inc.json.depositA} ${inc.json.depositB}`);
  assert.ok(inc.json.depositA > 0 && inc.json.depositB > 0, 'in range an increase takes both tokens');
  assert.strictEqual((await targets(inc.json.signatures)).at(-1), V3.npm.toLowerCase(), 'increased on the NPM');
  const grown = run(['status'], lp).json;
  assert.strictEqual(BigInt(grown.liquidity), BigInt(before.liquidity) + BigInt(inc.json.liquidityAdded), 'liquidity grew by the added amount');
  assert.strictEqual(grown.positionMint, id, 'the same NFT: nothing closed or reopened');
  assert.ok(Math.abs(grown.positionUsd - before.positionUsd - inc.json.depositUsd) < 0.05, 'the mark grew by the deposit');
  gas.increase = await gasOf(inc.json.signatures);
  const off = run(['increase', id, '0', '0'], lp);
  assert.notStrictEqual(off.status, 0, 'empty caps are refused');

  // 4. Another trader swaps through the pool: fees accrue.
  await fund(USDT0, TRADER.addr, 3_000_000_000n);
  const tr1 = run(['rebalance', WPOL, USDT0, '2000', '0', '--execute'], trader, { LPBOT_MAX_USD: '100000', LPBOT_MAX_IMPACT: '0.05' });
  assert.strictEqual(tr1.status, 0, tr1.stderr + tr1.stdout);
  const fees = run(['status'], lp).json;
  t.diagnostic(`fees after a $2000 trade: A ${fees.feesAccruedA} B ${fees.feesAccruedB} ($${fees.feesAccrued_USD})`);
  assert.ok(fees.feesAccruedA > 0 || fees.feesAccruedB > 0, `no fees after a trade: ${JSON.stringify(fees)}`);

  // 5. Harvest pays what status said.
  const hv = run(['harvest', id, '--execute'], lp);
  assert.strictEqual(hv.status, 0, hv.stderr);
  assert.deepStrictEqual(await targets(hv.json.signatures), [V3.npm.toLowerCase()]);
  assert.ok(Math.abs(hv.json.amountA - fees.feesAccruedA) <= fees.feesAccruedA * 0.01 + 1e-18);
  assert.ok(Math.abs(hv.json.amountB - fees.feesAccruedB) <= fees.feesAccruedB * 0.01 + 1e-12);
  assert.strictEqual(run(['harvest', id, '--execute'], lp).json.note, 'nothing to claim');
  gas.harvest = await gasOf(hv.json.signatures);

  // 6. Close: one transaction, the NFT burned, the read is empty.
  const cl = run(['close', id, '--execute'], lp);
  assert.strictEqual(cl.status, 0, cl.stderr + cl.stdout);
  assert.strictEqual(cl.json.closed, id); assert.strictEqual(cl.json.signatures.length, 1);
  assert.deepStrictEqual(run(['status'], lp).json, { positions: 0, positionMint: null, pool: POOL });
  gas.close = await gasOf(cl.json.signatures);

  // 7. Payout: the pin only, in USDT0.
  const bad = run(['send', USDT0, '1', OTHER, '--execute'], lp);
  assert.notStrictEqual(bad.status, 0); assert.match(bad.stderr, /not the pinned profit wallet/);
  const pay = run(['send', USDT0, '1', PROFIT, '--execute'], lp);
  assert.strictEqual(pay.status, 0, pay.stderr);
  gas.payout = await gasOf([pay.json.signature]);
  const recentre = sum(gas.close) + sum(gas.swap) + sum(gas.open);
  t.diagnostic(`gas per tx: ${JSON.stringify(gas)}; re-centre (close+swap+open) ${recentre}; increase ${sum(gas.increase)}`);
  assert.ok(sum(gas.increase) * 3 < recentre, `an increase (${sum(gas.increase)}) should cost well under a re-centre (${recentre})`);
  assert.ok(recentre <= RECENTRE_GAS_BUDGET, `a re-centre costs ${recentre} gas, over the ${RECENTRE_GAS_BUDGET} budget`);
  assert.strictEqual(run(['balance', USDT0], keyFile(TRADER.key, 'x.secret')).json.symbol, 'USDT0');
  // Native POL can go to the pin only above the gas reserve.
  const pol = run(['send', 'POL', '100', PROFIT, '--execute'], lp);
  assert.notStrictEqual(pol.status, 0); assert.match(pol.stderr, /POL/);

  // 8. A HALT in the run directory refuses every write.
  const runDir = fs.mkdtempSync(path.join(scratch, 'run-'));
  fs.writeFileSync(path.join(runDir, 'HALT'), 'test halt');
  const halted = run(['send', USDT0, '1', PROFIT, '--execute'], lp, { LPBOT_RUN_DIR: runDir });
  assert.notStrictEqual(halted.status, 0); assert.match(halted.stderr, /HALT present/);
});
