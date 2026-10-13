// The whole Polygon onboarding on an anvil fork of Polygon, on the real Safe,
// Zodiac Roles and Uniswap v3 contracts: the page in Chromium with an injected
// EIP-1193 wallet (the owner's key), onboard/server.mjs, and the test
// database. The owner's wallet deploys its own Safe, sets up the bot's Roles
// module and role, wraps POL, deposits the WPOL and pays the bot's key its
// gas, last: with EIP-5792 (wallet_sendCalls) in one approval, else one by
// one. No message is signed; the bot sends nothing for the vault. The server
// checks the vault and the gas payment on chain before it registers; the
// profile starts. Bad payments are refused; the owner withdraws, revokes,
// gives the rules back and withdraws all from the page. A second owner, its
// wallet without wallet_sendCalls, presses Max and goes with what it filled:
// its POL less the fees kept back for its own set-up. Skipped when anvil or
// Chromium is missing, or anvil cannot fork.
//
// Safety: every client points at the local anvil, and the test checks the
// node is anvil before anything is sent. Keys are fresh for each run.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import net from 'node:net';
import { spawn, spawnSync } from 'node:child_process';
import { createPublicClient, createWalletClient, http, parseAbi } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { polygon } from 'viem/chains';
import * as U from '../chains/evm/polygon.mjs';
import { SAFE_ABI } from '../chains/evm/vault/safe.mjs';
import { DSN, clean, json, psql, template } from './_onboard.mjs';
import { browser, testBundle, openPage, evmWallet, shot, flowDone, PHONE } from './_browser.mjs';
import { loadConfig, makeServer, polygonAdapter, pythonStore } from '../onboard/server.mjs';

// Fresh keys: anvil's well-known test addresses carry code on Polygon
// mainnet (EIP-7702 sweepers) that forwards any POL they receive.
const KEYS = Object.fromEntries(['bot', 'owner', 'owner2', 'stranger', 'maxer'].map(k => [k, `0x${crypto.randomBytes(32).toString('hex')}`]));
const acct = Object.fromEntries(Object.entries(KEYS).map(([k, v]) => [k, privateKeyToAccount(v)]));
const FORK_URLS = process.env.LPBOT_POLYGON_FORK_URL ? [process.env.LPBOT_POLYGON_FORK_URL]
  : ['https://polygon-bor-rpc.publicnode.com', 'https://polygon.drpc.org'];
const INVITE = 'polygon-fork-invite';
const NAME = 'ob-tgp-fork', NAME2 = 'ob-tgp-two', NAME3 = 'ob-tgp-max', TEMPLATE = 'ob-tgp-tpl';
const ERC20 = parseAbi(['function balanceOf(address) view returns (uint256)']);
const E18 = 10n ** 18n;

const anvilPath = spawnSync('bash', ['-c', 'command -v anvil'], { encoding: 'utf8' }).stdout.trim();
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

// The wallet a page sees: EIP-1193 over the owner's key, on the fork. Each
// send is followed by `blocks` more blocks, as a chain moves on.
function provider(name, blocks = 5, { sendCalls = false } = {}) {
  const w = walletOf(name);
  const batches = new Map();
  return {
    sent: [], approvals: 0,
    async request({ method, params }) {
      if (method === 'wallet_sendCalls' && !sendCalls) throw Object.assign(new Error('the method wallet_sendCalls does not exist / is not available'), { code: -32601 });
      if (method === 'wallet_sendCalls') {
        // one approval, the calls in order, each mined before the next (the set-up needs the Safe the first makes)
        this.approvals++;
        const hashes = [];
        for (const t of params[0].calls) {
          const hash = await w.sendTransaction({ to: t.to, data: t.data, value: BigInt(t.value ?? 0) });
          await pub.waitForTransactionReceipt({ hash });
          hashes.push(hash);
        }
        await mine(blocks);
        this.sent.push(...hashes);
        const id = `0x${crypto.randomBytes(32).toString('hex')}`;
        batches.set(id, hashes);
        return { id };
      }
      if (method === 'wallet_getCallsStatus') {
        return { version: '2.0.0', status: 200, atomic: false, receipts: batches.get(params[0]).map(h => ({ transactionHash: h, status: '0x1' })) };
      }
      if (method === 'eth_requestAccounts') return [acct[name].address];
      if (method === 'eth_chainId') return '0x89';
      if (method === 'wallet_switchEthereumChain') return null;
      if (method === 'personal_sign') return acct[name].signMessage({ message: { raw: params[0] } });
      if (method === 'eth_sendTransaction') {
        this.approvals++;
        const [t] = params;
        const hash = await w.sendTransaction({ to: t.to, data: t.data, value: BigInt(t.value ?? 0) });
        await pub.waitForTransactionReceipt({ hash });
        await mine(blocks);
        this.sent.push(hash);
        return hash;
      }
      if (method === 'eth_getTransactionReceipt') {
        const r = await pub.getTransactionReceipt({ hash: params[0] }).catch(() => null);
        return r && { status: r.status === 'success' ? '0x1' : '0x0' };
      }
      throw new Error(`stub wallet: ${method}`);
    },
  };
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

test.after(() => { anvil?.kill(); return B?.close(); });

const B = await browser();

test('fork (Polygon): the owner\'s own Safe and the bot\'s role, the deposit and the gas, one approval; bad payments; the page: add, withdraw, revoke, add again, withdraw all', { timeout: 900_000 }, async (t) => {
  if (!anvilPath) return t.skip('anvil not installed');
  if (!B) return t.skip('Chromium is not installed');
  if (!anvil) return t.skip('anvil could not fork Polygon');
  assert.match(await rpc('web3_clientVersion'), /^anvil\//, 'never send to anything but anvil');
  assert.match(DSN, /_test\s*$/);
  for (const a of Object.values(acct)) await rpc('anvil_setBalance', [a.address, '0x56BC75E2D63100000']);      // 100 POL each

  const cfg = loadConfig();
  const C = cfg.chains.polygon;
  C.template = TEMPLATE;
  cfg.limits.creates = { ...cfg.limits.creates, max: 4 };       // four new vaults from this one address
  clean(NAME, TEMPLATE); clean(NAME2, TEMPLATE); clean(NAME3, TEMPLATE);
  template(TEMPLATE, { chain: 'polygon', address: acct.bot.address, payoutMint: U.USDT0.toLowerCase(), profit: C.operatorWallet });
  const started = [], logs = [];
  const adapter = polygonAdapter(C, { deps: { U, account: acct.bot, pub, wallet: walletOf('bot') }, log: (o) => logs.push(o) });
  const bundle = await testBundle({ polygon: { botKey: acct.bot.address } });
  const server = makeServer(cfg, { chains: { polygon: adapter }, store: pythonStore(), log: (o) => logs.push(o), bundle,
    walletConnect: { projectId: '', relayUrl: 'wss://relay.walletconnect.org', verify: [] },
    inviteHashes: crypto.createHash('sha256').update(INVITE).digest('hex'),
    start: async (cmd) => { started.push(cmd); return { code: 0, out: '', err: '' }; },
    status: async () => ({ code: 0, out: 'active\n', err: '' }) });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`, base = `${origin}/bot/`;
  const post = async (p, b) => {
    const r = await fetch(`${base}api/${p}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ invite: INVITE, ...b }) });
    return { status: r.status, json: await r.json() };
  };
  let page, page3;
  try {
    // --- 1. over the API (owner2): a set-up the owner sends itself, then refused payments ---
    const short = await post('intent', { chain: 'polygon', owner: acct.owner2.address, token: 'USDT0', amount: '10',
      profitWallet: acct.owner2.address, name: NAME2 });
    assert.equal(short.status, 400, 'the owner holds no USDT0 for the deposit: no name is held, nothing is built');
    assert.equal(json(`select to_json(count(*)) from rebalancer.onboard where name = '${NAME2}'`), 0);
    const it2 = (await post('intent', { chain: 'polygon', owner: acct.owner2.address, token: 'POL', amount: '30',
      profitWallet: acct.owner2.address, name: NAME2 })).json;
    assert.deepEqual([it2.terms.gas, it2.terms.deposit, it2.chainId], ['3', '27', 137]);
    assert.equal(it2.calls.setup.length, 2, 'the Safe, then its Roles set-up');
    assert.equal(it2.calls.fund.length, 3, 'the wrap, the deposit, the bot\'s gas last');
    assert.equal(it2.calls.fund[2].to, acct.bot.address);
    const sendTx = async (from, t, blocks = 5) => {
      const hash = await walletOf(from).sendTransaction({ to: t.to, data: t.data ?? '0x', value: BigInt(t.value ?? 0) });
      await pub.waitForTransactionReceipt({ hash });
      if (blocks) await mine(blocks);
      return hash;
    };
    const send = (from, to, value, blocks = 5) => sendTx(from, { to, value }, blocks);
    const botBefore = await pub.getBalance({ address: acct.bot.address });
    const botNonce = await pub.getTransactionCount({ address: acct.bot.address });
    const pay = (payment, deposit) => post('pay', { name: NAME2, payment, deposit });
    const none = await pay(null);
    assert.equal(none.status, 409, 'nothing on chain yet');
    await sendTx('owner2', it2.calls.setup[0]);
    const half = await pay(null);
    assert.equal(half.status, 409, 'the Safe without its Roles set-up');
    assert.match(half.json.error, /not all on chain yet/);
    await sendTx('owner2', it2.calls.setup[1]);
    assert.equal((await pay(null)).status, 409, 'the vault is whole; its gas is not paid');
    await sendTx('owner2', it2.calls.fund[0]);
    const deposit2 = await sendTx('owner2', it2.calls.fund[1]);
    const fresh = await sendTx('owner2', it2.calls.fund[2], 0);
    assert.equal((await pay(fresh, deposit2)).status, 409, 'one block: not confirmed yet');
    assert.equal((await pay(`0x${'ab'.repeat(32)}`)).status, 409, `not on chain ${JSON.stringify(logs.filter(l => l.event === 'error'))}`);
    for (const [hash, re] of [[await send('owner2', acct.bot.address, 3n * E18 - 1n), /less than 3/],
      [await send('stranger', acct.bot.address, 3n * E18), /not from the owner/],
      [await send('owner2', acct.stranger.address, 3n * E18), /does not go to the bot's key/],
      ['0x1234', /not a transaction hash/]]) {
      const r = await pay(hash);
      assert.equal(r.status, 400, `${hash} ${JSON.stringify(r.json)}`);
      assert.match(r.json.error, re);
    }
    await mine(5);
    const paid = await pay(fresh, deposit2);
    assert.equal(paid.status, 200, JSON.stringify(paid.json));
    // the bot only received (the payment, the short one and the stranger's) and sent nothing
    assert.equal(await pub.getBalance({ address: acct.bot.address }), botBefore + 9n * E18 - 1n, 'the bot spent nothing');
    assert.equal(await pub.getTransactionCount({ address: acct.bot.address }), botNonce, 'the bot sent no transaction');
    const o2 = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME2}'`);
    assert.deepEqual([o2.state, o2.signature, o2.payment], ['registered', '', fresh]);
    assert.deepEqual(await pub.readContract({ address: o2.vault, abi: SAFE_ABI, functionName: 'getOwners' }), [acct.owner2.address]);
    const dep2 = json(`select coalesce(json_agg(d), '[]') from rebalancer.onboard_deposits d where onboard_id = ${o2.id}`);
    assert.deepEqual(dep2.map(d => [d.token, Number(d.amount)]), [['WPOL', 27]], 'the deposit read back from the chain');

    // --- 2. the page, end to end, in Chromium ------------------------------------------------
    // one pool per wallet: owner2 has the vault from part 1 and gets no second
    const second = await post('intent', { chain: 'polygon', owner: acct.owner2.address, token: 'POL', amount: '30',
      profitWallet: acct.owner2.address, name: 'ob-tgp-three' });
    assert.deepEqual([second.status, second.json.error], [403, 'this wallet has its vault already: put more capital in with Add capital under Your vaults']);
    // owner2's payment, replayed for another wallet's request, pays for nothing
    const it3 = (await post('intent', { chain: 'polygon', owner: acct.owner.address, token: 'POL', amount: '30',
      profitWallet: acct.owner.address, name: 'ob-tgp-three' })).json;
    for (const t of it3.calls.setup) await sendTx('owner', t);
    const replay = await post('pay', { name: 'ob-tgp-three', payment: fresh });
    assert.equal(replay.status, 400);
    assert.equal(replay.json.error, `the payment is not from the owner ${acct.owner.address}`);
    clean('ob-tgp-three', 'none');

    const wallet = provider('owner', 5, { sendCalls: true });
    ({ page } = await openPage(B, origin, INVITE, { device: PHONE, setup: (ctx) => evmWallet(ctx, wallet) }));
    const click = (sel) => page.locator(sel).click();
    const view = (id) => page.waitForSelector(`#${id}:not([hidden])`, { timeout: 120_000 });
    await page.locator('#chains .tab', { hasText: 'Polygon' }).click();
    await click('#w-injected');
    await view('v-create');
    assert.match(await page.textContent('#c-bounds'), /From 23 to 100000 POL; 3 POL of it pays the bot's gas/);
    await page.selectOption('#c-token', 'POL');
    await page.fill('#c-amount', '30');
    await page.fill('#c-profit', C.operatorWallet.toLowerCase());
    await page.fill('#c-name', NAME);
    const ownerBefore = await pub.getBalance({ address: acct.owner.address });
    await click('#c-go');
    let f = await flowDone(page, 600_000);
    // the bot opens nothing here (no loop runs): the last step says so, it does not fail
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f) + JSON.stringify(logs.filter(l => l.event !== 'connect_report').slice(-5)));
    await shot(page, 'poly-01-created');
    assert.equal(wallet.approvals, 1, 'ONE approval: wallet_sendCalls');
    assert.equal(wallet.sent.length, 5, 'the Safe, its Roles set-up, the wrap, the deposit, the gas');
    assert.match(f.review.join('\n'), /Create your vault .*its only owner/);
    assert.match(f.review.join('\n'), /Pay 3 POL from your wallet .* to the bot's key/);
    const o = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME}'`);
    assert.deepEqual([o.state, o.payment, o.gas, o.deposit, o.signature], ['started', wallet.sent[4], 3, 27, '']);
    const safe = o.vault;
    assert.deepEqual(await pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getOwners' }), [acct.owner.address]);
    assert.equal(await balanceOf(U.WPOL, safe), 27n * E18);
    assert.ok(ownerBefore - await pub.getBalance({ address: acct.owner.address }) >= 30n * E18);
    assert.deepEqual(started.at(-1), ['sudo', '-n', '/usr/bin/systemctl', 'enable', '--now', `lp-bot@${NAME}.service`]);
    const c = json(`select row_to_json(c) from rebalancer.config c where name = '${NAME}'`);
    assert.deepEqual([c.dex, c.pool, c.payout_mint, c.deposit_mint], ['uniswap-v3-polygon', C.pool, U.USDT0.toLowerCase(), U.WPOL.toLowerCase()]);
    assert.deepEqual([c.special, c.fee_bps, c.fee_wallet, c.profit_wallet], [true, 0, null, C.operatorWallet], 'the operator\'s own wallet');
    const deposits = json(`select coalesce(json_agg(d), '[]') from rebalancer.onboard_deposits d join rebalancer.onboard o on o.id = d.onboard_id where o.name = '${NAME}'`);
    assert.deepEqual(deposits.map(d => [d.token, Number(d.amount)]), [['WPOL', 27]], 'the deposit read back from the chain, once');

    // --- 3. add capital, withdraw part, revoke, add capital again, withdraw all ----------------
    await click('#f-done');
    await view('v-active');
    await click('#a-add');
    await page.selectOption('#sh-body select', 'POL');
    await page.fill('#sh-body input', '2');
    await click('#sh-go');
    f = await flowDone(page, 600_000);
    assert.ok(f.steps.every(s => s.state === 'done'), JSON.stringify(f));
    assert.equal(await balanceOf(U.WPOL, safe), 29n * E18, 'into the same vault');
    assert.equal(wallet.sent.length, 7, 'the wrap and the transfer, no message');
    await click('#f-done');
    await view('v-active');
    const owned = await balanceOf(U.WPOL, acct.owner.address);
    await click('#a-withdraw');
    await page.selectOption('#sh-body select', 'WPOL');
    await page.fill('#sh-body input', '7');
    await click('#sh-go');
    f = await flowDone(page, 600_000);
    assert.deepEqual(f.steps.map(s => [s.label, s.state]), [['Withdraw 7 WPOL', 'done']], JSON.stringify(f));
    assert.match(f.review.join('\n'), /Withdraw 7 WPOL from your vault .* to your wallet/);
    assert.equal(await balanceOf(U.WPOL, safe), 22n * E18);
    assert.equal(await balanceOf(U.WPOL, acct.owner.address) - owned, 7n * E18);
    await click('#f-done');
    await view('v-active');
    await click('#a-revoke');
    await click('#sh-go');
    f = await flowDone(page, 600_000);
    assert.deepEqual(f.steps.map(s => [s.label, s.state]), [['Remove the bot\'s key from your vault', 'done']], JSON.stringify(f));
    const modules = async () => (await pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getModulesPaginated',
      args: ['0x0000000000000000000000000000000000000001', 10n] }))[0];
    assert.deepEqual(await modules(), [], 'the Roles module is disabled');
    await click('#f-done');
    await view('v-revoked');
    await shot(page, 'poly-02-revoked');
    await click('#r-add');
    await page.selectOption('#sh-body select', 'WPOL');
    await page.fill('#sh-body input', '1');
    await rpc('anvil_setBalance', [acct.owner.address, '0x56BC75E2D63100000']);
    const approvalsBefore = wallet.approvals;
    await click('#sh-go');
    f = await flowDone(page, 600_000);
    assert.deepEqual(f.steps.map(s => s.state), ['done', 'done'], JSON.stringify(f));
    assert.equal(wallet.approvals, approvalsBefore + 1, 'the module back and the deposit: one approval');
    assert.equal((await modules()).length, 1, 'the Roles module is back');
    assert.equal(await balanceOf(U.WPOL, safe), 23n * E18);
    await click('#f-done');
    await view('v-active');
    await click('#a-withdraw');
    await click('#sh-go');
    f = await flowDone(page, 600_000);
    assert.ok(f.steps.every(s => s.state === 'done'), JSON.stringify(f));
    assert.equal(await balanceOf(U.WPOL, safe), 0n, 'everything out');
    await shot(page, 'poly-03-withdrawn');

    // --- 4. Max fills the box with what the wallet can put in; a Sign with it goes through ---
    await rpc('anvil_setBalance', [acct.maxer.address, `0x${(30n * E18).toString(16)}`]);
    const w3 = provider('maxer');
    ({ page: page3 } = await openPage(B, origin, INVITE, { device: PHONE, setup: (ctx) => evmWallet(ctx, w3) }));
    await page3.locator('#chains .tab', { hasText: 'Polygon' }).click();
    await page3.locator('#w-injected').click();
    await page3.waitForSelector('#v-create:not([hidden])');
    await page3.selectOption('#c-token', 'WPOL');
    await page3.locator('#c-max').click();
    await page3.waitForFunction(() => !document.getElementById('alert').hidden);
    assert.equal(await page3.textContent('#alert'), 'not enough: you have 0 WPOL, the minimum is 20 WPOL');
    assert.equal(await page3.inputValue('#c-amount'), '', 'the box is left as it was');
    await page3.selectOption('#c-token', 'POL');
    await page3.locator('#c-max').click();
    await page3.waitForFunction(() => document.getElementById('c-amount').value);
    assert.equal(await page3.inputValue('#c-amount'), '29', '30 POL less 1 kept back for the fees');
    await page3.fill('#c-name', NAME3);
    await page3.locator('#c-go').click();
    f = await flowDone(page3, 600_000);
    assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f));
    assert.equal(w3.approvals, 5, 'no wallet_sendCalls: the Safe, the set-up, the wrap, the deposit, the gas, one by one');
    const o3 = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME3}'`);
    assert.deepEqual([o3.state, o3.amount, o3.deposit], ['started', 29, 26]);
    assert.equal(await balanceOf(U.WPOL, o3.vault), 26n * E18);
    const left = await pub.getBalance({ address: acct.maxer.address });
    t.diagnostic(`after Max: ${Number(left) / 1e18} POL left of the 1 kept back for the owner's set-up`);
    assert.ok(left > 0n && left < E18, 'the fees came out of what Max kept back');
  } finally {
    await page?.context().close().catch(() => {});
    await page3?.context().close().catch(() => {});
    server.close();
    for (const n of [NAME, NAME2, 'ob-tgp-three', NAME3]) {
      psql(`delete from rebalancer.onboard_deposits where onboard_id in (select id from rebalancer.onboard where name = '${n}');
        delete from rebalancer.owner_requests where profile = '${n}';`);
      clean(n, TEMPLATE);
    }
  }
});
