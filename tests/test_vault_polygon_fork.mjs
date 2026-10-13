// A Polygon vault on an anvil fork, on the real deployed contracts (Safe 1.4.1, Zodiac Roles v2,
// Uniswap v3): the owner's wallet creates it (chains/evm/vault/cli.mjs request create; the
// bot's key is never an owner and pays nothing), the Uniswap signer runs the vault through its role (swap, open, harvest, close, pay out), the role
// refuses every call that would send money elsewhere, and the owner withdraws and revokes
// with the Safe while the bot cannot. Skipped when anvil is not installed or cannot fork.
//
// Safety: LPBOT_RPC is a loopback URL, which polygonEndpoints() makes the ONLY endpoint, and
// the test asserts the node is anvil before anything is sent. Keys are anvil's public ones.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { createPublicClient, createWalletClient, encodeFunctionData, http, parseAbi, toFunctionSelector } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import * as U from '../chains/evm/polygon.mjs';
import { ownerCreatesVault } from './_evm_vault.mjs';
import { ROLES_ABI } from '../chains/evm/vault/roles.mjs';
import { SAFE_ABI, SENTINEL, preValidated } from '../chains/evm/vault/safe.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const signer = path.join(dir, '..', 'venues/uniswap_v3/signer.mjs');
const POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7';                 // WPOL/USDT0 0.05%
const FORK_URLS = process.env.LPBOT_POLYGON_FORK_URL ? [process.env.LPBOT_POLYGON_FORK_URL]
  : ['https://polygon.drpc.org', 'https://polygon-bor-rpc.publicnode.com'];   // dRPC first: publicnode answers a receipt anvil lacks with 403 -32602 (2026-10-13)
const KEYS = {
  bot: '0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80',
  owner: '0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d',
  trader: '0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a',
  stranger: '0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6',
};
const acct = Object.fromEntries(Object.entries(KEYS).map(([k, v]) => [k, privateKeyToAccount(v)]));
const PROFIT = '0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65';
const ERC20 = parseAbi(['function transfer(address to, uint256 amount) returns (bool)', 'function balanceOf(address) view returns (uint256)']);

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'poly-vault-'));
fs.chmodSync(scratch, 0o700);
const keyFile = (name) => { const f = path.join(scratch, name); fs.writeFileSync(f, `${KEYS[name]}\n`, { mode: 0o600 }); return f; };
let anvil = null, url = null, pub = null;

async function freePort() {
  return new Promise(r => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
}
async function rpc(method, params = []) {
  const res = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
  const j = await res.json();
  if (j.error) throw new Error(`${method}: ${j.error.message}`);
  return j.result;
}
const walletOf = (name) => createWalletClient({ chain: polygon, transport: http(url), account: acct[name] });
async function mined(hash) { const r = await pub.waitForTransactionReceipt({ hash }); return r; }
const balanceOf = (token, who) => pub.readContract({ address: token, abi: ERC20, functionName: 'balanceOf', args: [who] });

// Give `to` some of the pool's own tokens: impersonate the pool on the fork.
async function fund(token, to, raw) {
  await rpc('anvil_impersonateAccount', [POOL]);
  await rpc('anvil_setBalance', [POOL, '0xDE0B6B3A7640000']);
  const hash = await rpc('eth_sendTransaction', [{ from: POOL, to: token, data: encodeFunctionData({ abi: ERC20, functionName: 'transfer', args: [to, raw] }) }]);
  assert.equal((await mined(hash)).status, 'success');
  await rpc('anvil_stopImpersonatingAccount', [POOL]);
}

function run(args, key, env = {}) {
  const r = spawnSync('node', [signer, ...args], {
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: key, LPBOT_RPC: url, LPBOT_POOL: POOL, LPBOT_CHAIN: 'polygon', ...env },
    encoding: 'utf8', timeout: 180_000,
  });
  let json = null;
  try { json = JSON.parse(r.stdout.slice(r.stdout.indexOf('{'), r.stdout.lastIndexOf('}') + 1)); } catch { /* none */ }
  return { ...r, json };
}

// A call as a key sends it through the role: 'passed' when the role lets it through, else
// the revert data. The inner call may still fail (a dummy amount): the role passed it when
// the revert is the module's own ModuleTransactionFailed.
const PASSED_ROLE = toFunctionSelector('ModuleTransactionFailed()');
const CONDITION = toFunctionSelector('ConditionViolation(uint8,bytes32)');
const NOT_A_MODULE = toFunctionSelector('NotAuthorized(address)');      // the key is no module of the Roles mod
async function roleVerdict(vault, to, data, { from = acct.bot, value = 0n, operation = 0 } = {}) {
  const call = encodeFunctionData({ abi: ROLES_ABI, functionName: 'execTransactionWithRole', args: [to, value, data, operation, vault.role, true] });
  const res = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'eth_call', params: [{ from: from.address, to: vault.roles, data: call }, 'latest'] }) });
  const j = await res.json();
  const revert = String(j.error?.data ?? '');
  return !j.error || revert.startsWith(PASSED_ROLE) ? 'passed' : revert || String(j.error.message);
}
const roleAllows = async (...a) => (await roleVerdict(...a)) === 'passed';

test.before(async () => {
  if (!anvilPath) return;
  for (const from of FORK_URLS) {
    const port = await freePort();
    url = `http://127.0.0.1:${port}`;
    anvil = spawn(anvilPath, ['--fork-url', from, '--port', String(port), '--silent', '--no-storage-caching'], { stdio: 'ignore' });
    for (let i = 0; i < 60; i++) {
      try { if (/^anvil\//.test(await rpc('web3_clientVersion'))) { pub = createPublicClient({ chain: polygon, transport: http(url) }); return; } } catch { /* not up yet */ }
      await new Promise(r => setTimeout(r, 500));
    }
    anvil.kill(); anvil = null;
  }
});

test.after(() => {
  anvil?.kill();
  fs.rmSync(scratch, { recursive: true, force: true });
});

test('fork (Polygon vault): connect, run through the role, refusals, owner withdraw and revoke', { timeout: 900_000 }, async (t) => {
  if (!anvilPath) return t.skip('anvil not installed');
  if (!anvil) return t.skip('anvil could not fork Polygon');
  assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never send to anything but anvil');
  const bot = keyFile('bot'), trader = keyFile('trader');
  for (const a of Object.values(acct)) await rpc('anvil_setBalance', [a.address, '0x56BC75E2D63100000']);      // 100 POL each

  // 1. The owner creates its own vault; the bot's key sends nothing.
  const botNonce = await pub.getTransactionCount({ address: acct.bot.address });
  const c = await ownerCreatesVault({ U, pub, ownerWallet: walletOf('owner'), bot: acct.bot.address, pools: POOL, payouts: [PROFIT] });
  assert.equal(await pub.getTransactionCount({ address: acct.bot.address }), botNonce, 'the bot sent nothing');
  t.diagnostic(`owner set-up gas ${JSON.stringify(c.gas)}`);
  const safe = c.safe, vault = { safe, roles: c.roles, role: c.role };
  assert.deepEqual(await pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getOwners' }), [acct.owner.address]);
  const env = { LPBOT_VAULT_SAFE: safe, LPBOT_VAULT_ROLES: c.roles, LPBOT_VAULT_VENUE: c.venue, LPBOT_VAULT_POOLS: POOL,
    LPBOT_VAULT_PAYOUTS: PROFIT, LPBOT_PROFIT_WALLET: PROFIT };

  // 2. The owner deposits USDT0 into the Safe; the bot's key holds only gas.
  await fund(U.USDT0, safe, 200_000_000n);
  const w0 = run(['balance'], bot, env);
  assert.equal(w0.status, 0, w0.stderr);
  assert.equal(w0.json.owner, safe);
  assert.equal(w0.json.custody, 'vault');
  assert.equal(w0.json.balanceB, 200);
  assert.ok(w0.json.gasSol > 99, 'the gas is the key\'s');

  // 3. Swap toward 50/50 on the approved pool, through the role.
  const sw = run(['rebalance', U.WPOL, U.USDT0, '100', '100', '--execute'], bot, env);
  assert.equal(sw.status, 0, sw.stderr + sw.stdout);
  assert.ok(sw.json.bought.amount >= sw.json.minOutAmount);
  assert.ok(await balanceOf(U.WPOL, safe) > 0n, 'the WPOL is the Safe\'s');
  assert.equal(await balanceOf(U.WPOL, acct.bot.address), 0n);

  // 4. Open: the position NFT is the Safe's.
  const pr = run(['pool'], bot, env).json.price;
  const w1 = run(['balance'], bot, env).json;
  const op = run(['open', POOL, String(pr / 1.02), String(pr * 1.02), String(w1.balanceA), String(w1.balanceB), '--execute'], bot, env);
  assert.equal(op.status, 0, op.stderr + op.stdout);
  const id = op.json.positionMint;
  assert.match(String(id), /^\d+$/);
  const NPM = parseAbi(['function ownerOf(uint256) view returns (address)']);
  assert.equal(await pub.readContract({ address: U.V3.npm, abi: NPM, functionName: 'ownerOf', args: [BigInt(id)] }), safe);
  assert.equal(run(['increase', id, '1', '1'], bot, env).status, 1, 'a vault never adds to a position');

  // 5. A trader swaps through the pool; the harvest collects to the Safe.
  await fund(U.USDT0, acct.trader.address, 3_000_000_000n);
  const tr = run(['rebalance', U.WPOL, U.USDT0, '2000', '0', '--execute'], trader, { LPBOT_MAX_USD: '100000', LPBOT_MAX_IMPACT: '0.05' });
  assert.equal(tr.status, 0, tr.stderr + tr.stdout);
  const usdtBefore = await balanceOf(U.USDT0, safe);
  const hv = run(['harvest', id, '--execute'], bot, env);
  assert.equal(hv.status, 0, hv.stderr + hv.stdout);
  assert.ok(hv.json.amountA > 0 || hv.json.amountB > 0, JSON.stringify(hv.json));
  assert.ok(await balanceOf(U.USDT0, safe) >= usdtBefore);

  // 6. Close: decrease, collect, burn, each through the role; the tokens are the Safe's.
  const cl = run(['close', id, '--execute'], bot, env);
  assert.equal(cl.status, 0, cl.stderr + cl.stdout);
  assert.deepEqual(cl.json.steps, ['decreaseLiquidity', 'collect', 'burn']);
  assert.deepEqual(run(['status'], bot, env).json, { positions: 0, positionMint: null, pool: POOL });

  // 7. Pay out: the approved profit wallet only; no POL.
  const pay = run(['send', U.USDT0, '1', PROFIT, '--execute'], bot, env);
  assert.equal(pay.status, 0, pay.stderr);
  assert.equal(await balanceOf(U.USDT0, PROFIT), 1_000_000n);
  const stray = run(['send', U.USDT0, '1', acct.stranger.address, '--execute'], bot, { ...env, LPBOT_PROFIT_WALLET: acct.stranger.address });
  assert.match(stray.stderr, /not a payout wallet the vault's owners approved/);
  assert.match(run(['send', 'POL', '1', PROFIT, '--execute'], bot, env).stderr, /tokens only/);

  // 8. The role itself refuses every call that sends money elsewhere (the chain, not the signer).
  const S = acct.stranger.address, dl = 2n ** 40n;
  const npm = (functionName, args) => encodeFunctionData({ abi: U.NPM_ABI, functionName, args });
  const mint = (o) => npm('mint', [{ token0: U.WPOL, token1: U.USDT0, fee: 500, tickLower: -10, tickUpper: 10, amount0Desired: 1n,
    amount1Desired: 1n, amount0Min: 0n, amount1Min: 0n, recipient: safe, deadline: dl, ...o }]);
  const swap = (o) => encodeFunctionData({ abi: U.ROUTER_ABI, functionName: 'exactInputSingle', args: [{ tokenIn: U.USDT0, tokenOut: U.WPOL,
    fee: 500, recipient: safe, amountIn: 1n, amountOutMinimum: 0n, sqrtPriceLimitX96: 0n, ...o }] });
  const erc = (functionName, args) => encodeFunctionData({ abi: U.ERC20_ABI, functionName, args });
  // what the role allows: these must pass, or the refusals below prove nothing
  assert.ok(await roleAllows(vault, U.USDT0, erc('approve', [U.V3.npm, 0n])));
  assert.ok(await roleAllows(vault, U.USDT0, erc('approve', [U.V3.router, 0n])));
  assert.ok(await roleAllows(vault, U.USDT0, erc('transfer', [PROFIT, 1n])));
  assert.ok(await roleAllows(vault, U.V3.router, swap({})));
  const refused = {
    'mint to a stranger': [U.V3.npm, mint({ recipient: S })],
    'mint on another fee tier': [U.V3.npm, mint({ fee: 3000 })],
    'mint on another pair': [U.V3.npm, mint({ token1: U.USDC })],
    'collect to a stranger': [U.V3.npm, npm('collect', [{ tokenId: 1n, recipient: S, amount0Max: 1n, amount1Max: 1n }])],
    'increase any position': [U.V3.npm, npm('increaseLiquidity', [{ tokenId: 1n, amount0Desired: 1n, amount1Desired: 1n, amount0Min: 0n, amount1Min: 0n, deadline: dl }])],
    'multicall': [U.V3.npm, npm('multicall', [[mint({ recipient: S })]])],
    'swap to a stranger': [U.V3.router, swap({ recipient: S })],
    'swap another token': [U.V3.router, swap({ tokenOut: U.USDC })],
    'swap on another fee tier': [U.V3.router, swap({ fee: 3000 })],
    'transfer to a stranger': [U.USDT0, erc('transfer', [S, 1n])],
    'approve a stranger': [U.USDT0, erc('approve', [S, 1n])],
    'transfer a token that is no payout token': [U.WPOL, erc('transfer', [PROFIT, 1n])],
    'a token of no approved pool': [U.USDC, erc('approve', [U.V3.npm, 1n])],
  };
  // each refusal is the role's own condition check, not some other revert
  for (const [what, [to, data]] of Object.entries(refused)) assert.ok((await roleVerdict(vault, to, data)).startsWith(CONDITION), what);
  assert.ok((await roleVerdict(vault, U.USDT0, erc('approve', [U.V3.npm, 0n]), { value: 1n })).startsWith(CONDITION), 'a call with value');
  assert.ok((await roleVerdict(vault, U.USDT0, erc('approve', [U.V3.npm, 0n]), { operation: 1 })).startsWith(CONDITION), 'a delegatecall');
  assert.ok((await roleVerdict(vault, U.USDT0, erc('approve', [U.V3.npm, 0n]), { from: acct.stranger })).startsWith(NOT_A_MODULE), 'a key without the role');

  // 9. The owner withdraws with the Safe; the bot's key cannot use the Safe itself.
  const exec = (signerName, to, data) => encodeFunctionData({ abi: SAFE_ABI, functionName: 'execTransaction',
    args: [to, 0n, data, 0, 0n, 0n, 0n, '0x0000000000000000000000000000000000000000', '0x0000000000000000000000000000000000000000', preValidated(acct[signerName].address)] });
  const all = await balanceOf(U.USDT0, safe);
  await assert.rejects(pub.call({ account: acct.bot, to: safe, data: exec('bot', U.USDT0, erc('transfer', [acct.bot.address, all])) }));
  assert.equal((await mined(await walletOf('owner').sendTransaction({ to: safe, data: exec('owner', U.USDT0, erc('transfer', [acct.owner.address, all])) }))).status, 'success');
  assert.equal(await balanceOf(U.USDT0, safe), 0n);

  // 10. Revoke: the owner removes the module; the bot stops before signing anything.
  const disable = encodeFunctionData({ abi: SAFE_ABI, functionName: 'disableModule', args: [SENTINEL, c.roles] });
  assert.equal((await mined(await walletOf('owner').sendTransaction({ to: safe, data: exec('owner', safe, disable) }))).status, 'success');
  assert.equal(await roleAllows(vault, U.USDT0, erc('approve', [U.V3.npm, 0n])), false, 'the module can no longer act');
  const after = run(['balance'], bot, env);
  assert.notEqual(after.status, 0);
  assert.match(after.stderr, /has not enabled the Roles module/);
});
