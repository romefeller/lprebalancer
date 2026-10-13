// The whole Solana onboarding, for real: the page (onboard/index.html and
// bundle.js) in Chromium, served by onboard/server.mjs, a Wallet Standard
// wallet holding the owner's key, the owner's own set-up of its vault, the
// bot's finish (its lookup table) and the Raydium pool on a LiteSVM fork of
// mainnet (tests/_svm.mjs), and the rows in the test database
// (onboard/store.py). Every transaction the page asks the wallet to sign is
// decoded and checked in the page first (decode_sol.mjs).
//
// The owner's own test, in its order: create with a small amount (ONE wallet
// approval: the vault, the bot's rules, the deposit and the bot's gas; the
// bot finishes and opens), revoke (the bot closes its position into the vault
// when asked, then the owner revokes), add capital (the bot's rules back and
// a deposit, one approval; the bot opens again), withdraw everything (the bot
// closes, the owner takes all). Then a vault whose bot does not answer:
// revoke first, then the owner closes the position. Then the server's
// refusals: a set-up not all on chain, too little gas, another owner's
// payment, a finish that stops and resumes. The bot spends nothing before
// the owner's set-up is on chain. Here the test plays the bot: it opens and
// closes positions with the Raydium signer through the vault's policies, as
// the loop does.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import { DSN, clean, json, psql, template } from './_onboard.mjs';
import { browser, testBundle, openPage, solanaWallet, shot, flowDone, PHONE } from './_browser.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';
import { vaultPositions } from '../chains/solana/vault/exit.mjs';
import { loadConfig, makeServer, pythonStore, solanaAdapter } from '../onboard/server.mjs';
import { requestEnv } from '../onboard/solana.mjs';
import { gasTotal, toRaw } from '../onboard/intent.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, SystemProgram, Transaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const bs58 = require('bs58').default;
const { Raydium } = require('@raydium-io/raydium-sdk-v2');

const INVITE = 'fork-invite-code';
const NAME = 'ob-tg-one', NAME2 = 'ob-tg-two', NAME3 = 'ob-tg-down';
const TEMPLATE = 'ob-tg-tpl';
const SYSTEM = SystemProgram.programId.toBase58();
const USDC = new PublicKey(SOLANA.usdc_mint);
const POOL = SOLANA.sol_usdc_pools['raydium-clmm'];

// What the fork landed, as an RPC node reports it: statuses (none for a
// signature it never saw) and jsonParsed System transfers.
class Ledger {
  constructor() { this.txs = new Map(); this.sent = new Set(); }
  add(tx) { const sig = bs58.encode(tx.signature); this.txs.set(sig, { tx, blockTime: Math.floor(Date.now() / 1000) }); return sig; }
  connection(base) {
    return {
      ...base,
      sendRawTransaction: async (raw, opts) => {
        const sig = await base.sendRawTransaction(raw, opts);
        this.sent.add(sig);
        // a legacy transaction the page sent (the owner's, signed by its wallet) is read back like the wallet's own
        try { this.add(Transaction.from(raw)); } catch { /* the bot's version 0 transactions */ }
        return sig;
      },
      // newest first, as a node lists them: the transactions that name the address
      getSignaturesForAddress: async (address, { limit = 1000 } = {}) => [...this.txs.entries()].reverse()
        .filter(([, t]) => t.tx.compileMessage().accountKeys.some(k => k.equals(new PublicKey(address))))
        .slice(0, limit).map(([signature]) => ({ signature, err: null })),
      getSignatureStatuses: async (sigs) => ({ value: sigs.map(s => this.txs.has(s) || this.sent.has(s) ? { err: null, confirmationStatus: 'confirmed' } : null) }),
      getParsedTransaction: async (sig) => {
        const t = this.txs.get(sig);
        if (!t) return null;
        const instructions = t.tx.instructions.map(ix => {
          const program = ix.programId.toBase58();
          if (program !== SYSTEM || ix.data.readUInt32LE(0) !== 2) return { programId: program };
          return { program: 'system', parsed: { type: 'transfer', info: { source: ix.keys[0].pubkey.toBase58(),
            destination: ix.keys[1].pubkey.toBase58(), lamports: Number(ix.data.readBigUInt64LE(4)) } } };
        });
        return { blockTime: t.blockTime, meta: { err: null }, transaction: { message: { instructions } } };
      },
    };
  }
}


// A SOL transfer signed by `from`, landed on the fork and in the ledger.
async function transfer(fork, ledger, from, to, lamports) {
  const tx = new Transaction().add(SystemProgram.transfer({ fromPubkey: from.publicKey, toPubkey: new PublicKey(to), lamports }));
  tx.feePayer = from.publicKey;
  tx.recentBlockhash = fork.svm.latestBlockhash();
  tx.sign(from);
  assert.ok((await fork.sendSigned(tx)).ok);
  return ledger.add(tx);
}

// The fork's mainnet snapshot may be older than mainnet: keep the next smart
// account seeds, their policies and vaults out of the fork.
function freeSeeds(fork, n = 8) {
  const next = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).smartAccountIndex;
  for (let i = 1n; i <= BigInt(n); i++) {
    const settings = sq.settingsPda(next + i);
    fork.absent.add(settings.toBase58());
    fork.absent.add(sq.vaultPda(settings).toBase58());
    for (let p = 1; p <= 8; p++) fork.absent.add(sq.policyPda(settings, p).toBase58());
  }
}

// What a signer command printed: its one JSON object.
async function said(fn) {
  const lines = [], log = console.log;
  console.log = (x) => lines.push(x);
  try { await fn(); } finally { console.log = log; }
  return JSON.parse(lines.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null');
}

// The bot of profile `name`, as the loop runs it: the Raydium signer through
// the vault's policies. It swaps nothing here: the test gives the vault the
// USDC side an open needs.
async function botOf(fork, connection, keys, name) {
  const row = json(`select row_to_json(o) from rebalancer.onboard o where name = '${name}'`);
  const r = (await pythonStore()('get', { name })).request;
  const env = { ...requestEnv(r.vault_row, 'raydium-clmm', keys), LPBOT_VAULT_POLICY_KEY: keys.policyPath };
  const vault = new PublicKey(row.vault);
  const signer = await import('../venues/raydium_clmm/signer.mjs');
  const deps = async () => {
    const acting = await actingAccount({ connection, payer: keys.payer, venue: 'raydium-clmm', env });
    const raydium = await Raydium.load({ connection, owner: vault, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
    return { urls: ['http://fork.test'], connectFn: async () => ({ connection, payer: keys.payer, raydium, acting }), sleep: async () => {} };
  };
  process.env.LPBOT_POOL = POOL;
  process.env.LPBOT_MAX_USD = '100000';
  process.env.LPBOT_RAYDIUM_LEAN = '1';
  delete process.env.LPBOT_TOKEN_HINTS;
  return {
    vault,
    positions: () => vaultPositions(connection, { venue: 'raydium-clmm', pools: [POOL], vault: vault.toBase58() }),
    async open() {
      const d = await deps();
      const b = await said(() => signer.balance(undefined, d));
      const usdc = spl.getAssociatedTokenAddressSync(USDC, vault, true);
      fork.tokenAccount(usdc, USDC, vault, (fork.tokenAmount(usdc) ?? 0n) + 30_000_000n);
      fork.nextSlot();                                    // the vault's lookup table, made at the connect, becomes usable
      const out = await said(() => signer.open(POOL, String(b.price * 0.99), String(b.price * 1.01), '0.1', '20', true, d));
      assert.equal(out?.sent, true, JSON.stringify(out));
      return out;
    },
    async close(mint) {
      const out = await said(async () => signer.close(mint, true, await deps()));
      assert.ok(out?.signature || out?.sent, JSON.stringify(out));
    },
  };
}

const B = await browser();

test('fork (Solana): the owner\'s test in a browser: create, revoke, add capital, withdraw; an owner close; the server\'s refusals',
  { skip: B ? false : 'Chromium is not installed', timeout: 1_500_000 }, async () => {
    assert.match(DSN, /_test\s*$/, 'the test database only');
    const fork = new Fork();
    await Promise.all([fork.program(sq.PROGRAM_ID), fork.program(SOLANA.programs.raydium_clmm), fork.program(SOLANA.programs.metaplex_metadata)]);
    await fork.syncClock();
    await fork.clone([sq.programConfigPda()]);
    await fork.clone([sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).treasury]);
    await fork.clone([USDC]);
    freeSeeds(fork);
    fork.keepBlockhash = true;                               // the owner's set-up: transactions signed at once, one blockhash
    const ledger = new Ledger();
    const connection = ledger.connection(fork.connection());

    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'onboard-fork-'));
    const payer = Keypair.generate(), policyKey = Keypair.generate();
    const owner = fork.wallet(5), owner2 = fork.wallet(5), owner3 = fork.wallet(5), stranger = fork.wallet(5);
    fs.writeFileSync(path.join(dir, 'payer.json'), JSON.stringify([...payer.secretKey]), { mode: 0o600 });
    fs.writeFileSync(path.join(dir, 'policy.json'), JSON.stringify([...policyKey.secretKey]), { mode: 0o600 });
    const cfg = loadConfig();
    const C = cfg.chains.solana;
    C.template = TEMPLATE;
    cfg.limits.creates = { ...cfg.limits.creates, max: 6 };
    const env = { [C.payerKeyEnv]: path.join(dir, 'payer.json'), [C.policyKeyEnv]: path.join(dir, 'policy.json') };
    for (const n of [NAME, NAME2, NAME3, 'ob-tg-four']) clean(n, TEMPLATE);
    template(TEMPLATE, { chain: 'solana', address: payer.publicKey.toBase58(), payoutMint: SOLANA.usdc_mint, profit: SOLANA.operator_profit_wallet });

    const services = new Map(), logs = [];
    const log = (o) => logs.push(o);
    const adapter = await solanaAdapter(C, { connection, env, log });
    let breakRecord = false;
    const record = adapter.record;
    adapter.record = async (...a) => { if (breakRecord) { breakRecord = false; throw new Error('pool read timed out'); } return record(...a); };
    const bundle = await testBundle({ solana: { payer: payer.publicKey.toBase58(), policyKey: policyKey.publicKey.toBase58() } });
    const server = makeServer(cfg, { chains: { solana: adapter }, store: pythonStore(), log, bundle,
      walletConnect: { projectId: '', relayUrl: 'wss://relay.walletconnect.org', verify: [] },
      inviteHashes: crypto.createHash('sha256').update(INVITE).digest('hex'),
      start: async (cmd) => { services.set(cmd.at(-1).replace(/^lp-bot@|\.service$/g, ''), 'active'); return { code: 0, out: '', err: '' }; },
      status: async (cmd) => ({ code: 0, out: `${services.get(cmd.at(-1).replace(/^lp-bot@|\.service$/g, '')) ?? 'inactive'}\n`, err: '' }) });
    await new Promise(r => server.listen(0, '127.0.0.1', r));
    const origin = `http://127.0.0.1:${server.address().port}`, base = `${origin}/bot/`;
    const post = async (p, b) => {
      const r = await fetch(`${base}api/${p}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ invite: INVITE, ...b }) });
      return { status: r.status, json: await r.json() };
    };
    const gas = gasTotal(C);
    const keys = { payer, policyKey, policyPath: env[C.policyKeyEnv] };
    // the bot's answer to an owner's exit: close the position into the vault, mark it done, stop
    const exitWatch = (name, bot) => {
      let stop = false;
      (async () => {
        while (!stop) {
          const id = psql(`select id from rebalancer.owner_requests where profile = '${name}' and done_at is null`);
          if (id) {
            for (const p of await bot.positions()) await bot.close(p.nftMint ?? p.position);
            psql(`update rebalancer.owner_requests set done_at = now(), outcome = 'closed' where id = ${id}`);
            services.set(name, 'inactive');
          }
          await new Promise(r => setTimeout(r, 500));
        }
      })();
      return () => { stop = true; };
    };
    const pages = [];
    // the profile's service runs (and `more`), or the page's flow failed: then what it says, and the server's last words
    const runs = async (page, name, more = () => true) => {
      const steps = () => page.evaluate(() => [...document.querySelectorAll('#f-steps .step')].map(s => ({ label: s.querySelector('.label').textContent,
        state: s.dataset.state, detail: s.querySelector('.detail').textContent })));
      const end = Date.now() + 600_000;
      while (Date.now() < end) {
        if (services.get(name) === 'active' && more()) return;
        const now = await steps();
        if (now.some(x => x.state === 'failed')) {
          throw new Error(`the flow failed: ${JSON.stringify(now)} ${await page.textContent('#alert')} ${JSON.stringify(logs.filter(l => l.event !== 'connect_report').slice(-6))}`);
        }
        await new Promise(r => setTimeout(r, 500));
      }
      throw new Error(`${name} never started: ${JSON.stringify(await steps())} ${JSON.stringify(logs.slice(-6))}`);
    };
    const click = (page, sel) => page.locator(sel).click();
    const view = (page, id) => page.waitForSelector(`#${id}:not([hidden])`, { timeout: 120_000 });
    try {
      // --- 1. create: one message, the gas, the deposit; the bot opens ---------------------
      const { page, errors, wallet: wallet1 } = await openPage(B, origin, INVITE, { device: PHONE,
        setup: (ctx) => solanaWallet(ctx, owner, async (tx) => { const r = await fork.sendSigned(tx); if (!r.ok) throw new Error(r.logs.slice(-3).join(' | ')); return ledger.add(tx); }) });
      pages.push(page);
      await click(page, '#w-injected');
      await view(page, 'v-create');
      assert.equal(await page.inputValue('#c-profit'), owner.publicKey.toBase58(), 'the profit wallet defaults to the connected one');
      assert.match(await page.textContent('#c-bounds'), /From 0\.1386 to 50 SOL; 0\.1186 SOL of it pays the bot's gas/);
      await page.fill('#c-amount', '0.5');
      await page.fill('#c-name', NAME);
      const payerBefore = fork.balance(payer.publicKey), ownerBefore = fork.balance(owner.publicKey);
      assert.equal(payerBefore, 0, 'the bot holds nothing: it can front nothing');
      await click(page, '#c-go');
      // the bot opens once the service runs
      let bot = null;
      await runs(page, NAME);
      bot = await botOf(fork, connection, keys, NAME);
      await bot.open();
      let f = await flowDone(page, 600_000);
      assert.deepEqual(f.steps.map(s => s.state), Array(5).fill('done'), JSON.stringify(f) + JSON.stringify(logs.filter(l => l.event !== 'connect_report').slice(-5)));
      assert.equal(f.steps[2].detail, 'In your vault', 'the set-up carried the deposit');
      await shot(page, 'fork-01-created');
      // ONE approval in the wallet: the whole set-up, two transactions (Raydium)
      const approvals = wallet1.filter(x => x.kind === 'signAll');
      assert.equal(approvals.length, 1, 'one approval for the whole set-up');
      assert.equal(approvals[0].txs.length, 2);
      assert.equal(wallet1.filter(x => x.kind === 'tx' || x.kind === 'message').length, 0, 'no message, no other transaction');
      assert.match(f.review.join('\n'), /Create your vault .*you its only owner, threshold 1/);
      const o = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME}'`);
      assert.equal(o.state, 'started');
      assert.equal(o.signature, '', 'no message signed');
      assert.ok(ledger.txs.has(o.payment), 'the payment the server took is the set-up\'s last transaction');
      assert.equal(o.payment, bs58.encode(approvals[0].txs[1].signature));
      const vault = o.vault;
      const spentBySetup = payerBefore + Number(gas) - fork.balance(payer.publicKey);
      assert.ok(spentBySetup > 0 && BigInt(spentBySetup) <= toRaw(C.gas.setup, 9) + toRaw(C.gas.margin, 9), `the bot's finish spent ${spentBySetup}`);
      const s = sq.decodeSettings(fork.get(o.connected.settings).data);
      assert.deepEqual([s.signers.map(x => x.key.toBase58()), s.threshold, s.timeLock, s.settingsAuthority.toBase58()],
        [[owner.publicKey.toBase58()], 1, 0, PublicKey.default.toBase58()]);
      // the owner paid its own vault: the rent of the settings and the policies, two fees, the gas and the deposit (0.5 in all)
      const rent = Number(fork.get(o.connected.settings).lamports)
        + o.connected.venues['raydium-clmm'].policies.reduce((a, p) => a + Number(fork.get(p).lamports), 0);
      assert.equal(ownerBefore - fork.balance(owner.publicKey), 500_000_000 + rent + 2 * 5000, 'the amount, the vault\'s rent and two fees');
      assert.ok(rent < 25_000_000, `the vault's rent ${rent}`);
      const c = json(`select row_to_json(c) from rebalancer.config c where name = '${NAME}'`);
      assert.deepEqual([c.dex, c.pool, c.profit_wallet, c.fee_bps], ['raydium-clmm', POOL, owner.publicKey.toBase58(), 1000]);
      // the cap: from the deposit, never the template's 600 (the price of SOL in this database decides the figure)
      const deposits = json(`select coalesce(json_agg(d), '[]') from rebalancer.onboard_deposits d join rebalancer.onboard o on o.id = d.onboard_id where o.name = '${NAME}'`);
      assert.deepEqual(deposits.map(d => [d.token, Number(d.amount)]), [['SOL', 0.3814]], 'the deposit, read from the chain');
      assert.equal((await bot.positions()).length, 1);

      // --- 2. revoke: the bot closes into the vault when asked, then the owner revokes -----
      await click(page, '#f-done');
      await view(page, 'v-active');
      const stopWatch = exitWatch(NAME, bot);
      await click(page, '#a-revoke');
      await click(page, '#sh-go');
      f = await flowDone(page, 600_000);
      assert.deepEqual(f.steps.map(x => [x.label, x.state]), [['The bot closes its position into your vault', 'done'],
        ['Remove the bot\'s key from your vault', 'done']], JSON.stringify(f));
      await shot(page, 'fork-02-revoked');
      assert.deepEqual(await bot.positions(), []);
      for (const p of json(`select to_json(policies) from rebalancer.vault_policies where wallet_id = '${NAME}-vault'`)) {
        assert.deepEqual(sq.decodePolicyHead(fork.get(p).data).signers.map(x => x.key.toBase58()), [owner.publicKey.toBase58()]);
      }
      await assert.rejects(bot.open(), /revoked the bot/, 'the bot can no longer act');
      await click(page, '#f-done');                         // keep it in the vault
      await view(page, 'v-revoked');
      assert.deepEqual(await page.$$eval('#v-revoked button', bs => bs.filter(b => !b.hidden).map(b => b.textContent)), ['Add capital']);

      // --- 3. add capital: the rules back, a deposit, the bot starts and opens again --------
      await click(page, '#r-add');
      await page.fill('#sh-body input', '0.1');
      await click(page, '#sh-go');
      await runs(page, NAME, () => json(`select to_json(count(*)) from rebalancer.onboard_deposits d join rebalancer.onboard o on o.id = d.onboard_id where o.name = '${NAME}'`) === 2);
      f = await flowDone(page, 600_000);
      assert.deepEqual(f.steps.map(x => x.state), ['done', 'done'], JSON.stringify(f));
      assert.match(f.review.join('\n'), /Deposit: Send 0\.1 SOL from your wallet .* to your vault/);
      assert.match(f.review.join('\n'), /exactly the rules in git/);
      for (const p of json(`select to_json(policies) from rebalancer.vault_policies where wallet_id = '${NAME}-vault'`)) {
        assert.deepEqual(sq.decodePolicyHead(fork.get(p).data).signers.map(x => x.key.toBase58()), [policyKey.publicKey.toBase58()]);
      }
      await bot.open();
      await click(page, '#f-done');
      await view(page, 'v-active');

      // --- 4. withdraw everything: the bot closes, the owner takes all ------------------------
      const before = fork.balance(owner.publicKey);
      await click(page, '#a-withdraw');
      await click(page, '#sh-go');
      f = await flowDone(page, 600_000);
      stopWatch();
      assert.deepEqual(f.steps.map(x => [x.label, x.state]), [['The bot closes its position into your vault', 'done'],
        ['Withdraw everything to your wallet', 'done']], JSON.stringify(f));
      await shot(page, 'fork-03-withdrawn');
      assert.equal(fork.balance(vault), 0, 'the vault holds no SOL');
      assert.equal(fork.get(spl.getAssociatedTokenAddressSync(USDC, new PublicKey(vault), true)), null, 'its USDC account is closed');
      assert.ok(fork.balance(owner.publicKey) - before > 0.4e9, `the owner got its SOL back: ${(fork.balance(owner.publicKey) - before) / 1e9}`);
      assert.deepEqual(errors.filter(e => !/favicon/.test(e)), []);

      // --- 5. a bot that does not answer: revoke first, the owner closes the position ---------
      const { page: p3 } = await openPage(B, origin, INVITE, { device: PHONE,
        setup: (ctx) => solanaWallet(ctx, owner3, async (tx) => { const r = await fork.sendSigned(tx); if (!r.ok) throw new Error(r.logs.slice(-3).join(' | ')); return ledger.add(tx); }) });
      pages.push(p3);
      await click(p3, '#w-injected');
      await view(p3, 'v-create');
      await p3.fill('#c-amount', '0.5');
      await p3.fill('#c-name', NAME3);
      await click(p3, '#c-go');
      await runs(p3, NAME3);
      const bot3 = await botOf(fork, connection, keys, NAME3);
      await bot3.open();
      assert.deepEqual((await flowDone(p3, 600_000)).steps.map(x => x.state), Array(5).fill('done'));
      services.set(NAME3, 'failed');                          // the bot is down
      await click(p3, '#f-done');
      await view(p3, 'v-active');
      const vault3 = json(`select to_json(vault) from rebalancer.onboard where name = '${NAME3}'`);
      const v3Before = fork.balance(vault3);
      await click(p3, '#a-revoke');
      await click(p3, '#sh-go');
      f = await flowDone(p3, 600_000);
      assert.deepEqual(f.steps.map(x => [x.label, x.state]), [['Remove the bot\'s key from your vault', 'done'], ['Close the position yourself', 'done']],
        JSON.stringify(f));
      assert.match(f.review.join('\n'), /Raydium: (decrease_liquidity_v2|close_position)/);
      assert.deepEqual(await bot3.positions(), []);
      assert.ok(fork.balance(vault3) > v3Before, 'the position came back into the vault');
      await shot(p3, 'fork-04-owner-closed');

      // --- 6. the server's refusals: a set-up not all on chain, too little gas, another's payment, a stop and a resume ---
      const it = (await post('intent', { chain: 'solana', owner: owner2.publicKey.toBase58(), token: 'SOL', amount: '1',
        profitWallet: SOLANA.operator_profit_wallet, name: NAME2 })).json;
      assert.match(it.terms_text, /Fee share: none/, 'the operator\'s own wallet is special');
      assert.equal(it.terms.feeWallet, null);
      const payerMid = fork.balance(payer.publicKey);
      const pay = (payment) => post('pay', { name: NAME2, payment });
      const nothing = await pay(null);
      assert.equal(nothing.status, 409);
      assert.match(nothing.json.error, /not all on chain yet/);
      // the owner's wallet signs a set-up that pays the bot too little (built here, as a tampered page would)
      const planned = json(`select to_json(connected) from rebalancer.onboard where name = '${NAME2}'`).planned;
      const short = await adapter.setup({ owner: owner2.publicKey.toBase58(), profitWallet: SOLANA.operator_profit_wallet, feeWallet: null,
        token: 'SOL', deposit: '0.8814', gas: '0.1', planned });
      const signedSet = short.transactions.map(t => { const tx = Transaction.from(Buffer.from(t, 'base64')); tx.partialSign(owner2); return tx; });
      // only its first transaction lands: the vault exists without all its rules and holds nothing
      assert.ok((await fork.sendSigned(signedSet[0])).ok);
      ledger.add(signedSet[0]);
      const half = await pay(bs58.encode(signedSet[0].signature));
      assert.deepEqual([half.status, half.json.error], [409, 'your set-up is not all on chain yet (1 steps to come); the page asks again']);
      assert.equal(fork.balance(planned.vault), 0, 'no money in a vault without all its rules');
      assert.ok((await fork.sendSigned(signedSet[1])).ok);
      const under = ledger.add(signedSet[1]);
      for (const [payment, re] of [[under, /less than 0\.1186/], [o.payment, /not from the owner/], ['not-a-signature', /not a transaction signature/]]) {
        const r = await pay(payment);
        assert.equal(r.status, 400, payment);
        assert.match(r.json.error, re);
      }
      assert.equal(fork.balance(payer.publicKey), payerMid + 100_000_000, 'the bot spent nothing (it only received)');
      // the owner pays the rest of the gas: a payment the server reads (from the owner, to the bot's key, all of the gas)
      const good = await transfer(fork, ledger, owner2, payer.publicKey, Number(gas));
      breakRecord = true;
      const stopped = await pay(good);
      assert.equal(stopped.status, 502);
      assert.match(stopped.json.error, /the setup stopped: pool read timed out\. Your vault and your money are yours and safe/);
      // the page resumes it: no second payment, no second table, the deposit already in
      const { page: p2, wallet: wallet2 } = await openPage(B, origin, INVITE, { device: PHONE,
        setup: (ctx) => solanaWallet(ctx, owner2, async (tx) => { const r = await fork.sendSigned(tx); if (!r.ok) throw new Error(r.logs.slice(-3).join(' | ')); return ledger.add(tx); }) });
      pages.push(p2);
      await click(p2, '#w-injected');
      await view(p2, 'v-setup');
      assert.match(await p2.textContent('#s-text'), /The bot's finish stopped: pool read timed out/);
      const afterStop = fork.balance(payer.publicKey);
      await click(p2, '#s-go');
      await runs(p2, NAME2);
      const bot2 = await botOf(fork, connection, keys, NAME2);
      await bot2.open();
      f = await flowDone(p2, 600_000);
      assert.deepEqual(f.steps.map(x => x.state), Array(5).fill('done'), JSON.stringify(f));
      assert.equal(wallet2.length, 0, 'the resume asked the wallet for nothing');
      assert.equal(fork.balance(payer.publicKey) < afterStop, true, 'only the open the test made as the bot');
      const r2 = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME2}'`);
      assert.equal(r2.state, 'started');
      assert.equal((await pay(good)).json.state, 'started', 'a done request spends nothing again');
      const second = await post('intent', { chain: 'solana', owner: owner2.publicKey.toBase58(), token: 'SOL', amount: '1',
        profitWallet: owner2.publicKey.toBase58(), name: 'ob-tg-four' });
      assert.equal(second.status, 403, 'one vault per wallet');
    } finally {
      for (const p of pages) await p.context().close().catch(() => {});
      server.close();
      for (const n of [NAME, NAME2, NAME3, 'ob-tg-four']) {
        psql(`delete from rebalancer.onboard_deposits where onboard_id in (select id from rebalancer.onboard where name = '${n}');
          delete from rebalancer.owner_requests where profile = '${n}'; delete from rebalancer.feed_latest where profile = '${n}';`);
        clean(n, TEMPLATE);
      }
      fs.rmSync(dir, { recursive: true, force: true });
      await B.close();
    }
  });
