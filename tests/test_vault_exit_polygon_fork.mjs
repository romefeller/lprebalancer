// The owner's way out of a Polygon vault on an anvil fork, on the real Safe 1.4.1, Zodiac
// Roles v2 and Uniswap v3 contracts: the owner deposits (checkDeposit reads it back), the
// bot opens a position through its role, the owner revokes the bot, closes the position
// from the Safe (the tokens stay in the Safe), withdraws everything, and approves the bot
// again. Every owner transaction goes through the page's decoder first; a tampered copy is
// refused. Skipped when anvil is missing or cannot fork.
//
// Safety: every client points at the local anvil, and the test checks the node is anvil
// before anything is sent. Keys are fresh for each run.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { createPublicClient, createWalletClient, decodeFunctionData, encodeFunctionData, http, parseAbi } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import * as U from '../chains/evm/polygon.mjs';
import { ownerCreatesVault } from './_evm_vault.mjs';
import { SAFE_ABI, SENTINEL } from '../chains/evm/vault/safe.mjs';
import { ownerCloseCalls, vaultPositions } from '../chains/evm/vault/exit.mjs';
import * as P from '../onboard/polygon.mjs';
import { decodeEvm } from '../onboard/decode_evm.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const signer = path.join(dir, '..', 'venues/uniswap_v3/signer.mjs');
const POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7';                 // WPOL/USDT0 0.05%
const VENUE = 'uniswap-v3-polygon';
const FORK_URLS = process.env.LPBOT_POLYGON_FORK_URL ? [process.env.LPBOT_POLYGON_FORK_URL]
  : ['https://polygon.drpc.org', 'https://polygon-bor-rpc.publicnode.com'];   // dRPC first: publicnode answers a receipt anvil lacks with 403 -32602 (2026-10-13)
// Fresh keys: anvil's well-known test addresses carry code on Polygon mainnet (EIP-7702
// sweepers) that forwards any POL they receive.
const KEYS = Object.fromEntries(['bot', 'owner', 'stranger', 'profit'].map(k => [k, `0x${crypto.randomBytes(32).toString('hex')}`]));
const acct = Object.fromEntries(Object.entries(KEYS).map(([k, v]) => [k, privateKeyToAccount(v)]));
const ERC20 = parseAbi(['function transfer(address to, uint256 amount) returns (bool)', 'function balanceOf(address) view returns (uint256)']);
const NPM = parseAbi(['function balanceOf(address) view returns (uint256)']);
const C = P.resolve(JSON.parse(fs.readFileSync(path.join(dir, '..', 'onboard/onboard.json'), 'utf8')).chains.polygon);
const PINS = { chainId: U.CHAIN_ID, wrapped: { WPOL: U.WPOL }, botKey: acct.bot.address, npm: U.V3.npm,
  tokens: Object.fromEntries(Object.entries(C.tokens).map(([s, t]) => [s, { address: t.address, decimals: t.decimals }])) };
const E18 = 10n ** 18n;

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'poly-exit-'));
fs.chmodSync(scratch, 0o700);
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
const balanceOf = (token, who) => pub.readContract({ address: token, abi: ERC20, functionName: 'balanceOf', args: [who] });
const mine = (n) => rpc('anvil_mine', [`0x${n.toString(16)}`]);

// The owner's wallet sends one {to, data, value}, after the page's decoder passed it.
async function send(name, tx, expect) {
  const d = decodeEvm(tx, expect, PINS);
  assert.equal(d.ok, true, `${expect.kind}: ${d.problems.join('; ')}`);
  const hash = await walletOf(name).sendTransaction({ to: tx.to, data: tx.data, value: BigInt(tx.value ?? 0) });
  const r = await pub.waitForTransactionReceipt({ hash });
  assert.equal(r.status, 'success', `${expect.kind} ${d.lines.join('; ')}`);
  return { hash, lines: d.lines };
}

// Give `to` some of the pool's own tokens: impersonate the pool on the fork.
async function fund(token, to, raw) {
  await rpc('anvil_impersonateAccount', [POOL]);
  await rpc('anvil_setBalance', [POOL, '0xDE0B6B3A7640000']);
  const hash = await rpc('eth_sendTransaction', [{ from: POOL, to: token, data: encodeFunctionData({ abi: ERC20, functionName: 'transfer', args: [to, raw] }) }]);
  assert.equal((await pub.waitForTransactionReceipt({ hash })).status, 'success');
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

test('fork (Polygon vault exit): deposit checked, revoke, owner close, withdraw all, approve again', { timeout: 900_000 }, async (t) => {
  if (!anvilPath) return t.skip('anvil not installed');
  if (!anvil) return t.skip('anvil could not fork Polygon');
  assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never send to anything but anvil');
  for (const a of Object.values(acct)) await rpc('anvil_setBalance', [a.address, '0x56BC75E2D63100000']);      // 100 POL each
  const botKey = path.join(scratch, 'bot');
  fs.writeFileSync(botKey, `${KEYS.bot}\n`, { mode: 0o600 });
  const owner = acct.owner.address;

  // 1. The owner creates its own vault; it is the vault's only owner.
  const c = await ownerCreatesVault({ U, pub, ownerWallet: walletOf('owner'), bot: acct.bot.address, pools: POOL, payouts: [acct.profit.address] });
  const safe = c.safe, roles = c.roles;
  const keys = P.botKeys(C, {}, { U, account: acct.bot, pub, wallet: walletOf('bot') });
  const row = { settings: safe.toLowerCase(), venues: { [VENUE]: { policies: [roles.toLowerCase()], pools: [POOL.toLowerCase()], payouts: [acct.profit.address] } } };
  const env = { LPBOT_VAULT_SAFE: safe, LPBOT_VAULT_ROLES: roles, LPBOT_VAULT_VENUE: c.venue, LPBOT_VAULT_POOLS: POOL,
    LPBOT_VAULT_PAYOUTS: acct.profit.address, LPBOT_PROFIT_WALLET: acct.profit.address };

  // 2. The owner deposits USDT0 and wrapped POL; checkDeposit reads each back from its receipt.
  await fund(U.USDT0, owner, 500_000_000n);
  const usdt = await P.depositTxs(keys, C, { owner, vault: safe, token: 'USDT0', amount: '200' });
  const [dUsdt] = [await send('owner', usdt.transactions[0], { kind: 'deposit', owner, safe, token: 'USDT0', raw: '200000000' })];
  assert.deepEqual(dUsdt.lines, [`Deposit 200 USDT0 from your wallet ${owner} into your vault ${safe}`]);
  assert.equal((await P.checkDeposit(keys, C, dUsdt.hash, { owner, safe })).state, 'pending', 'one block: not confirmed yet');
  const pol = await P.depositTxs(keys, C, { owner, vault: safe, token: 'POL', amount: '50' });
  const wrap = await send('owner', pol.transactions[0], { kind: 'deposit', owner, safe, token: 'POL', raw: String(50n * E18) });
  const dWpol = await send('owner', pol.transactions[1], { kind: 'deposit', owner, safe, token: 'WPOL', raw: String(50n * E18) });
  const elsewhere = await walletOf('owner').writeContract({ address: U.USDT0, abi: ERC20, functionName: 'transfer', args: [acct.stranger.address, 1_000_000n] });
  await pub.waitForTransactionReceipt({ hash: elsewhere });
  await mine(5);
  assert.deepEqual(await P.checkDeposit(keys, C, dUsdt.hash, { owner, safe }), { state: 'ok', deposits: [{ token: 'USDT0', amount: '200' }] });
  assert.deepEqual(await P.checkDeposit(keys, C, dWpol.hash, { owner, safe }), { state: 'ok', deposits: [{ token: 'WPOL', amount: '50' }] });
  assert.equal((await P.checkDeposit(keys, C, wrap.hash, { owner, safe })).state, 'bad', 'a wrap moves nothing to the vault');
  assert.deepEqual(await P.checkDeposit(keys, C, elsewhere, { owner, safe }),
    { state: 'bad', reason: `the transaction moves no token from ${owner} to the vault ${safe}` });
  assert.equal((await P.checkDeposit(keys, C, dUsdt.hash, { owner: acct.stranger.address, safe })).state, 'bad');
  assert.equal((await P.checkDeposit(keys, C, `0x${'cd'.repeat(32)}`, { owner, safe })).state, 'pending');

  // 3. The bot opens a position through its role: the position NFT is the Safe's.
  const pr = run(['pool'], botKey, env).json.price;
  const w1 = run(['balance'], botKey, env).json;
  const op = run(['open', POOL, String(pr / 1.02), String(pr * 1.02), String(w1.balanceA), String(w1.balanceB), '--execute'], botKey, env);
  assert.equal(op.status, 0, op.stderr + op.stdout);
  const held = await P.positions(keys, C, row);
  assert.equal(held.length, 1);
  assert.equal(held[0].tokenId, String(op.json.positionMint));
  assert.ok(BigInt(held[0].liquidity) > 0n);
  assert.equal((await P.status(keys, C, row)).positions, 1);

  // 4. Revoke: the bot stops; a close needs no bot.
  const rv = await P.ownerTxs(keys, C, row, { action: 'revoke', owner });
  const rvSent = await send('owner', rv.transactions[0], { kind: 'revoke', owner, safe, roles });
  assert.deepEqual(await P.confirm(keys, rvSent.hash), { state: 'confirmed' });
  assert.match(run(['balance'], botKey, env).stderr, /has not enabled the Roles module/);
  assert.equal((await P.status(keys, C, row)).botActive, false);

  // 5. Close from the Safe by the owner: burned, the tokens in the Safe.
  const before = { wpol: await balanceOf(U.WPOL, safe), usdt: await balanceOf(U.USDT0, safe) };
  const cl = await P.ownerTxs(keys, C, row, { action: 'close', owner });
  assert.equal(cl.transactions.length, 1);
  // the same calls with no bot's key at all: only a public client
  assert.deepEqual(await ownerCloseCalls(pub, { U, safe, owner, pool: POOL }), cl.transactions);
  const closeExpect = { kind: 'close', owner, safe };
  // a tampered copy: the collect pays a stranger
  const outer = decodeFunctionData({ abi: SAFE_ABI, data: cl.transactions[0].data }).args;
  const steps = decodeFunctionData({ abi: U.NPM_ABI, data: outer[2] }).args[0].map(s => {
    const d = decodeFunctionData({ abi: U.NPM_ABI, data: s });
    return d.functionName === 'collect' ? encodeFunctionData({ abi: U.NPM_ABI, functionName: 'collect', args: [{ ...d.args[0], recipient: acct.stranger.address }] }) : s;
  });
  const bad = { ...cl.transactions[0], data: encodeFunctionData({ abi: SAFE_ABI, functionName: 'execTransaction',
    args: [outer[0], outer[1], encodeFunctionData({ abi: U.NPM_ABI, functionName: 'multicall', args: [steps] }), ...outer.slice(3)] }) };
  assert.equal(decodeEvm(bad, closeExpect, PINS).ok, false);
  const sent = await send('owner', cl.transactions[0], closeExpect);
  t.diagnostic(sent.lines.join(' | '));
  assert.equal(await pub.readContract({ address: U.V3.npm, abi: NPM, functionName: 'balanceOf', args: [safe] }), 0n, 'the position is burned');
  assert.deepEqual(await vaultPositions(pub, { U, safe, pool: POOL }), []);
  const after = { wpol: await balanceOf(U.WPOL, safe), usdt: await balanceOf(U.USDT0, safe) };
  assert.ok(after.wpol > before.wpol || after.usdt > before.usdt, 'the position\'s tokens came back to the Safe');
  assert.equal(await balanceOf(U.WPOL, acct.bot.address) + await balanceOf(U.USDT0, acct.bot.address), 0n, 'nothing to the bot');
  const none = await P.ownerTxs(keys, C, row, { action: 'close', owner });
  assert.deepEqual(none.transactions, []);
  assert.match(none.summary, /nothing to close/);

  // 6. Withdraw ALL: every token and the Safe's POL to the owner; the Safe is empty.
  await walletOf('stranger').sendTransaction({ to: safe, value: 2n * E18 }).then(h => pub.waitForTransactionReceipt({ hash: h }));
  const own0 = { wpol: await balanceOf(U.WPOL, owner), usdt: await balanceOf(U.USDT0, owner) };
  const all = await P.ownerTxs(keys, C, row, { action: 'withdraw', owner, token: 'ALL', amount: 'ALL' });
  assert.equal(all.transactions.length, 3);
  const expectAll = { kind: 'withdraw', owner, safe, token: 'ALL', raw: 'all' };
  const tampered = { ...all.transactions[0], data: all.transactions[0].data.replace(owner.slice(2).toLowerCase(), acct.stranger.address.slice(2).toLowerCase()) };
  assert.notEqual(tampered.data, all.transactions[0].data);
  assert.equal(decodeEvm(tampered, expectAll, PINS).ok, false, 'a withdrawal to a stranger');
  const polBefore = await pub.getBalance({ address: owner });
  let gasPaid = 0n;
  for (const tx of all.transactions) {
    const { hash } = await send('owner', tx, expectAll);
    const r = await pub.getTransactionReceipt({ hash });
    gasPaid += r.gasUsed * r.effectiveGasPrice;
  }
  assert.equal(await balanceOf(U.WPOL, safe), 0n);
  assert.equal(await balanceOf(U.USDT0, safe), 0n);
  assert.equal(await pub.getBalance({ address: safe }), 0n);
  assert.equal(await balanceOf(U.WPOL, owner) - own0.wpol, after.wpol);
  assert.equal(await balanceOf(U.USDT0, owner) - own0.usdt, after.usdt);
  assert.equal(await pub.getBalance({ address: owner }) - polBefore + gasPaid, 2n * E18, 'the Safe\'s POL, to the wei');
  assert.deepEqual((await P.ownerTxs(keys, C, row, { action: 'withdraw', owner, token: 'ALL', amount: 'all' })).transactions, []);

  // 7. Approve again: the module is back and the bot acts again; a second approve is refused.
  const ap = await P.ownerTxs(keys, C, row, { action: 'approve', owner });
  await send('owner', ap.transactions[0], { kind: 'approve', owner, safe, roles });
  const [modules] = await pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getModulesPaginated', args: [SENTINEL, 10n] });
  assert.deepEqual(modules, [roles]);
  const back = run(['balance'], botKey, env);
  assert.equal(back.status, 0, back.stderr);
  assert.equal(back.json.owner, safe);
  assert.equal((await P.status(keys, C, row)).botActive, true);
  await assert.rejects(P.ownerTxs(keys, C, row, { action: 'approve', owner }), /not revoked/);
  // a key that is not the bot's has no role: an approve for it is refused
  const other = { ...keys, account: acct.stranger };
  await P.ownerTxs(keys, C, row, { action: 'revoke', owner }).then(r => send('owner', r.transactions[0], { kind: 'revoke', owner, safe, roles }));
  await assert.rejects(P.ownerTxs(other, C, row, { action: 'approve', owner }), /no longer gives the bot's key/);
  assert.equal(await P.botHasRole(keys, C, { roles, venue: VENUE, pools: [POOL] }), true, 'the revoked module still holds the bot\'s role');
});
