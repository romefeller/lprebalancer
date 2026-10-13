// The owner creates its own Polygon vault on an anvil fork, on the real Safe 1.4.1, Zodiac
// Roles v2 and Uniswap v3 contracts (onboard/polygon.mjs setupTxs, chains/evm/vault/
// owner_setup.mjs): the addresses are the predicted ones, the page's decoder passes each
// transaction, the owner's wallet sends them and pays their gas, the bot's key sends nothing
// until it runs the vault through its role. The owner funds the vault and the bot's gas.
// Vaults set up any other way (another owner, threshold, bot key, an extra grant) are refused
// by the decoder and by verifySetup. Skipped when anvil is missing or cannot fork.
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
import { createPublicClient, createWalletClient, encodeFunctionData, http, parseAbi } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import * as U from '../chains/evm/polygon.mjs';
import { compile, ROLES_ABI } from '../chains/evm/vault/roles.mjs';
import { multiSend, preValidated, SAFE_ABI, SAFE_FACTORY_ABI } from '../chains/evm/vault/safe.mjs';
import { configureCalls, predictRoles, predictSafe, SAFE_PROXY_CODE, saltOf } from '../chains/evm/vault/owner_setup.mjs';
import * as P from '../onboard/polygon.mjs';
import { decodeEvm } from '../onboard/decode_evm.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const signer = path.join(dir, '..', 'venues/uniswap_v3/signer.mjs');
const POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7';                 // WPOL/USDT0 0.05%
const FORK_URLS = process.env.LPBOT_POLYGON_FORK_URL ? [process.env.LPBOT_POLYGON_FORK_URL]
  : ['https://polygon-bor-rpc.publicnode.com', 'https://polygon.drpc.org'];
// Fresh keys: anvil's well-known test addresses carry code on Polygon mainnet (EIP-7702
// sweepers) that forwards any POL they receive.
const KEYS = Object.fromEntries(['bot', 'owner', 'stranger', 'profit', 'fee'].map(k => [k, `0x${crypto.randomBytes(32).toString('hex')}`]));
const acct = Object.fromEntries(Object.entries(KEYS).map(([k, v]) => [k, privateKeyToAccount(v)]));
const ERC20 = parseAbi(['function transfer(address to, uint256 amount) returns (bool)', 'function balanceOf(address) view returns (uint256)']);
const C = P.resolve(JSON.parse(fs.readFileSync(path.join(dir, '..', 'onboard/onboard.json'), 'utf8')).chains.polygon);
const PINS = { chainId: U.CHAIN_ID, wrapped: { WPOL: U.WPOL }, botKey: acct.bot.address, npm: U.V3.npm, router: U.V3.router,
  stables: [...U.STABLES], venue: U.DEX, vault: { ...U.VAULT }, pool: { token0: U.WPOL, token1: U.USDT0, fee: 500 },
  tokens: Object.fromEntries(Object.entries(C.tokens).map(([s, t]) => [s, { address: t.address, decimals: t.decimals }])) };
const ZERO = '0x0000000000000000000000000000000000000000';
const E18 = 10n ** 18n;

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'poly-owner-'));
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

// The owner's wallet sends each {to, data, value} in order: the receipts.
async function sendAll(txs, name = 'owner') {
  const out = [];
  for (const tx of txs) {
    const hash = await walletOf(name).sendTransaction({ to: tx.to, data: tx.data, value: BigInt(tx.value ?? 0) });
    const r = await pub.waitForTransactionReceipt({ hash });
    assert.equal(r.status, 'success');
    out.push(r);
  }
  return out;
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

// A set-up built some other way: other owners or threshold, another bot key, more calls.
function tampered({ owners = [acct.owner.address], threshold = 1n, bot = acct.bot.address, extra = () => [], saltNonce, payouts }) {
  const initializer = encodeFunctionData({ abi: SAFE_ABI, functionName: 'setup', args: [owners, threshold, ZERO, '0x', U.VAULT.fallbackHandler, ZERO, 0n, ZERO] });
  const safe = predictSafe({ U, initializer, saltNonce }), roles = predictRoles({ U, safe, saltNonce });
  const compiled = compile(U.DEX, { npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools: [{ token0: U.WPOL, token1: U.USDT0, fee: 500 }], payouts });
  const calls = [...configureCalls({ U, safe, roles, compiled, bot, saltNonce }), ...extra(roles, compiled)];
  return { safe, roles, txs: [
    { to: U.VAULT.safeFactory, value: '0x0', data: encodeFunctionData({ abi: SAFE_FACTORY_ABI, functionName: 'createProxyWithNonce', args: [U.VAULT.safeSingleton, initializer, saltNonce] }) },
    { to: safe, value: '0x0', data: encodeFunctionData({ abi: SAFE_ABI, functionName: 'execTransaction',
      args: [U.VAULT.multiSendCallOnly, 0n, multiSend(calls), 1, 0n, 0n, 0n, ZERO, ZERO, preValidated(acct.owner.address)] }) }] };
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

test('fork (owner\'s vault): the owner creates and funds it, the bot runs it; a vault set up any other way is refused', { timeout: 900_000 }, async (t) => {
  if (!anvilPath) return t.skip('anvil not installed');
  if (!anvil) return t.skip('anvil could not fork Polygon');
  assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never send to anything but anvil');
  for (const a of Object.values(acct)) await rpc('anvil_setBalance', [a.address, '0x3635C9ADC5DEA00000']);      // 1000 POL each
  const botKey = path.join(scratch, 'bot');
  fs.writeFileSync(botKey, `${KEYS.bot}\n`, { mode: 0o600 });
  const owner = acct.owner.address, profitWallet = acct.profit.address, feeWallet = acct.fee.address;
  const keys = P.botKeys(C, {}, { U, account: acct.bot, pub, wallet: walletOf('bot') });
  assert.equal(await pub.readContract({ address: U.VAULT.safeFactory, abi: parseAbi(['function proxyCreationCode() view returns (bytes)']), functionName: 'proxyCreationCode' }),
    SAFE_PROXY_CODE, 'the pinned proxy code is the factory\'s');

  // 1. The owner's set-up: two transactions from the owner's wallet, decoded first.
  await fund(U.USDT0, owner, 500_000_000n);
  const req = { owner, profitWallet, feeWallet, token: 'USDT0', deposit: '200', gas: '4', saltNonce: String(saltOf(owner, 0)) };
  const s = await P.setupTxs(keys, C, req);
  assert.equal(s.setup.length, 2);
  const expect = { kind: 'createVault', owner, safe: s.safe, saltNonce: s.saltNonce, profitWallet, feeWallet };
  const d = decodeEvm(s.setup, expect, PINS);
  assert.deepEqual(d.problems, []);
  const botNonce = await pub.getTransactionCount({ address: acct.bot.address });
  const botPol = await pub.getBalance({ address: acct.bot.address });
  const receipts = await sendAll(s.setup);
  const gas = receipts.map(r => Number(r.gasUsed));
  t.diagnostic(`owner set-up gas: create ${gas[0]}, set-up ${gas[1]}, total ${gas[0] + gas[1]} (${((gas[0] + gas[1]) * 185e-9).toFixed(4)} POL at 185 gwei); `
    + `calldata ${(s.setup[1].data.length - 2) / 2} bytes`);
  assert.notEqual(await pub.getCode({ address: s.safe }), undefined, 'the Safe is at the predicted address');
  assert.equal((await pub.getCode({ address: s.roles }))?.length > 2, true, 'the Roles module is at the predicted address');
  assert.deepEqual(await P.verifySetup(keys, C, { owner, safe: s.safe, roles: s.roles, profitWallet, feeWallet, fromBlock: s.fromBlock }), []);
  assert.deepEqual(await pub.readContract({ address: s.safe, abi: SAFE_ABI, functionName: 'getOwners' }), [owner]);
  assert.equal(await pub.getTransactionCount({ address: acct.bot.address }), botNonce, 'the bot sent nothing');
  assert.equal(await pub.getBalance({ address: acct.bot.address }), botPol, 'the bot paid nothing');
  // a resume: the same addresses, nothing left to send
  const again = await P.setupTxs(keys, C, req);
  assert.deepEqual([again.safe, again.roles, again.setup], [s.safe, s.roles, []]);
  // a later request number is another vault
  assert.notEqual((await P.setupTxs(keys, C, { ...req, saltNonce: String(saltOf(owner, 1)) })).safe, s.safe);

  // 2. The owner funds it: the deposit, then the bot's gas last.
  const fundExpect = { kind: 'fund', owner, safe: s.safe, token: 'USDT0', raw: '200000000', gasWei: String(4n * E18) };
  assert.deepEqual(decodeEvm(s.fund, fundExpect, PINS).problems, []);
  const [dep, pay] = await sendAll(s.fund);
  await rpc('anvil_mine', ['0x5']);
  assert.equal(await balanceOf(U.USDT0, s.safe), 200_000_000n);
  assert.equal(await pub.getBalance({ address: acct.bot.address }), botPol + 4n * E18);
  assert.deepEqual(await P.checkDeposit(keys, C, dep.transactionHash, { owner, safe: s.safe }), { state: 'ok', deposits: [{ token: 'USDT0', amount: '200' }] });
  assert.ok(pay.blockNumber >= dep.blockNumber, 'the gas lands after the deposit');

  // 3. The bot's key acts through the role: swap toward 50/50, open; the position is the Safe's.
  const env = { LPBOT_VAULT_SAFE: s.safe, LPBOT_VAULT_ROLES: s.roles, LPBOT_VAULT_VENUE: U.DEX, LPBOT_VAULT_POOLS: POOL,
    LPBOT_VAULT_PAYOUTS: `${profitWallet},${feeWallet}`, LPBOT_PROFIT_WALLET: profitWallet, LPBOT_FEE_WALLET: feeWallet };
  const sw = run(['rebalance', U.WPOL, U.USDT0, '100', '100', '--execute'], botKey, env);
  assert.equal(sw.status, 0, sw.stderr + sw.stdout);
  const pays = run(['send', U.USDT0, '1', feeWallet, '--execute'], botKey, env);
  assert.equal(pays.status, 0, pays.stderr);
  assert.equal(await balanceOf(U.USDT0, feeWallet), 1_000_000n, 'the fee wallet is pinned in the role');
  const pr = run(['pool'], botKey, env).json.price;
  const w = run(['balance'], botKey, env).json;
  const op = run(['open', POOL, String(pr / 1.02), String(pr * 1.02), String(w.balanceA), String(w.balanceB), '--execute'], botKey, env);
  assert.equal(op.status, 0, op.stderr + op.stdout);
  const NPM = parseAbi(['function ownerOf(uint256) view returns (address)']);
  assert.equal(await pub.readContract({ address: U.V3.npm, abi: NPM, functionName: 'ownerOf', args: [BigInt(op.json.positionMint)] }), s.safe);

  // 4. Vaults set up any other way: the page's decoder refuses each, and so does verifySetup.
  const payouts = [profitWallet, feeWallet];
  const cases = [
    ['another owner too', { owners: [owner, acct.stranger.address] }, /not your wallet alone/, /owners are/],
    ['another bot key', { bot: acct.stranger.address }, /not what the pinned rules give/, /role members are/],
    ['an extra grant', { extra: (roles, compiled) => [{ to: roles, data: encodeFunctionData({ abi: ROLES_ABI, functionName: 'assignRoles',
      args: [acct.stranger.address, [compiled.role], [true]] }) }] }, /makes \d+ calls, not \d+/, /role members are/],
    ['no fee wallet in the role', { payouts: [profitWallet] }, /not what the pinned rules give/, /lacks the function|not as compiled/],
  ];
  let n = 10;
  for (const [what, o, decodeRe, verifyRe] of cases) {
    const saltNonce = saltOf(owner, n++);
    const from = await pub.getBlockNumber();
    const bad = tampered({ payouts, ...o, saltNonce });
    const honestSafe = predictSafe({ U, owner, saltNonce });
    const r = decodeEvm(bad.txs, { ...expect, safe: honestSafe, saltNonce: String(saltNonce) }, PINS);
    assert.equal(r.ok, false, what);
    assert.match(r.problems.join('; '), decodeRe, what);
    await sendAll(bad.txs);
    const v = await P.verifySetup(keys, C, { owner, safe: bad.safe, roles: bad.roles, profitWallet, feeWallet, fromBlock: from });
    assert.match(v.join('; '), verifyRe, what);
  }
  // threshold 2: the decoder refuses it; one owner's signature cannot set it up, and the check finds no module
  const saltNonce = saltOf(owner, n);
  const two = tampered({ owners: [owner, acct.stranger.address], threshold: 2n, payouts, saltNonce });
  assert.match(decodeEvm(two.txs, { ...expect, safe: predictSafe({ U, owner, saltNonce }), saltNonce: String(saltNonce) }, PINS).problems.join(), /needs 2 signatures, not 1/);
  assert.match(decodeEvm(two.txs, { ...expect, safe: two.safe, saltNonce: String(saltNonce) }, PINS).problems.join(), /your vault would be/);
  await sendAll(two.txs.slice(0, 1));
  await assert.rejects(walletOf('owner').sendTransaction({ to: two.txs[1].to, data: two.txs[1].data }), /GS020|revert/);
  assert.deepEqual(await P.verifySetup(keys, C, { owner, safe: two.safe, roles: two.roles, profitWallet, feeWallet, fromBlock: s.fromBlock }),
    [`no Roles module at ${two.roles}`]);
});
