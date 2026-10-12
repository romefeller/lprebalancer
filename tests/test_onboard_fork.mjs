// The whole Solana onboarding, for real: the page (onboard/index.html and
// app.js) in jsdom, served by onboard/server.mjs, a Wallet Standard wallet
// holding the owner's key, the bot's connect on a LiteSVM fork of mainnet
// (tests/_svm.mjs), and the rows in the test database (onboard/store.py).
//
// The owner signs one message and pays the bot's key the gas; the server
// finds that payment on chain before it spends anything; the bot creates
// the vault, registers it and the profile; the wallet deposits the rest; the
// profile starts. Then the owner withdraws and revokes the bot. A second
// owner's bad payments are refused without a lamport spent, a payment is
// never used twice, and a setup that stops resumes from the page with the
// payment already made. A third owner presses Max and signs with what it
// filled: the most the wallet holds, less the fees kept back for its own two
// transactions, and the wallet stays rent-exempt.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import { DSN, cardAction, clean, json, openPage, template, wait } from './_onboard.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { loadConfig, makeServer, pythonStore, solanaAdapter } from '../onboard/server.mjs';
import { fromRaw, gasTotal, toRaw } from '../onboard/intent.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, SystemProgram, Transaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const bs58 = require('bs58').default;

const INVITE = 'fork-invite-code';
const NAME = 'ob-fork', NAME2 = 'ob-fork-two', NAME3 = 'ob-fork-max';
const TEMPLATE = 'ob-fork-tpl';
const SYSTEM = SystemProgram.programId.toBase58();

// What the fork landed, as an RPC node reports it: statuses (none for a
// signature it never saw) and jsonParsed System transfers. The bot's own
// sends (the connect) land too: a send waits for their status (tx_send.mjs).
class Ledger {
  constructor() { this.txs = new Map(); this.sent = new Set(); }
  add(tx) { const sig = bs58.encode(tx.signature); this.txs.set(sig, { tx, blockTime: Math.floor(Date.now() / 1000) }); return sig; }
  connection(base) {
    return {
      ...base,
      sendRawTransaction: async (raw, opts) => { const sig = await base.sendRawTransaction(raw, opts); this.sent.add(sig); return sig; },
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

function sign(key, bytes) {
  const sk = crypto.createPrivateKey({ format: 'jwk', key: { kty: 'OKP', crv: 'Ed25519',
    d: Buffer.from(key.secretKey.subarray(0, 32)).toString('base64url'), x: Buffer.from(key.publicKey.toBytes()).toString('base64url') } });
  return crypto.sign(null, bytes, sk);
}

// A Wallet Standard wallet with the owner's key: it signs messages, and signs
// and sends transactions to the fork.
function standardWallet(fork, ledger, key) {
  const account = { address: key.publicKey.toBase58(), publicKey: key.publicKey.toBytes(), chains: ['solana:mainnet'], features: [] };
  const signed = [];
  return {
    signed,
    wallet: {
      version: '1.0.0', name: 'Fork Wallet', icon: 'data:image/svg+xml,<svg/>', chains: ['solana:mainnet'], accounts: [account],
      features: {
        'standard:connect': { version: '1.0.0', connect: async () => ({ accounts: [account] }) },
        'solana:signMessage': { version: '1.0.0', signMessage: async ({ message }) => {
          signed.push(Buffer.from(message).toString('utf8'));
          return [{ signedMessage: message, signature: new Uint8Array(sign(key, Buffer.from(message))) }];
        } },
        'solana:signAndSendTransaction': { version: '1.0.0', supportedTransactionVersions: ['legacy'],
          signAndSendTransaction: async ({ transaction }) => {
            const tx = Transaction.from(Buffer.from(transaction));
            tx.partialSign(key);
            const r = await fork.sendSigned(tx);
            if (!r.ok) throw new Error(`the fork refused: ${r.logs.slice(-3).join(' | ')}`);
            ledger.add(tx);
            return [{ signature: new Uint8Array(tx.signature) }];
          } },
      },
    },
  };
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

// The fork's mainnet snapshot may be older than mainnet: the next smart
// account seeds, their policies and vaults can exist there already (mainnet
// makes new ones while the test runs). Keep them out of the fork.
function freeSeeds(fork, n = 8) {
  const next = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).smartAccountIndex;
  for (let i = 1n; i <= BigInt(n); i++) {
    const settings = sq.settingsPda(next + i);
    fork.absent.add(settings.toBase58());
    fork.absent.add(sq.vaultPda(settings).toBase58());
    for (let p = 1; p <= 8; p++) fork.absent.add(sq.policyPda(settings, p).toBase58());
  }
}

test('fork (Solana): pay the gas, sign once, vault and profile, deposit, start; bad payments, resume, withdraw, revoke', { timeout: 900_000 }, async () => {
  assert.match(DSN, /_test\s*$/, 'the test database only');
  const fork = new Fork();
  await Promise.all([fork.program(sq.PROGRAM_ID), fork.program(SOLANA.programs.meteora_dlmm)]);
  await fork.syncClock();
  await fork.clone([sq.programConfigPda()]);
  await fork.clone([sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).treasury]);
  freeSeeds(fork);
  const ledger = new Ledger();
  const connection = ledger.connection(fork.connection());

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'onboard-fork-'));
  const payer = Keypair.generate(), policyKey = Keypair.generate(), owner = fork.wallet(5), owner2 = fork.wallet(5), stranger = fork.wallet(5);
  fs.writeFileSync(path.join(dir, 'payer.json'), JSON.stringify([...payer.secretKey]), { mode: 0o600 });
  fs.writeFileSync(path.join(dir, 'policy.json'), JSON.stringify([...policyKey.secretKey]), { mode: 0o600 });
  const cfg = loadConfig();
  const C = cfg.chains.solana;
  C.template = TEMPLATE;
  cfg.limits.creates = { ...cfg.limits.creates, max: 4 };       // four new vaults from this one address
  const env = { [C.payerKeyEnv]: path.join(dir, 'payer.json'), [C.policyKeyEnv]: path.join(dir, 'policy.json') };
  clean(NAME, TEMPLATE); clean(NAME2, TEMPLATE); clean('ob-fork-three', TEMPLATE); clean(NAME3, TEMPLATE);
  template(TEMPLATE, { chain: 'solana', address: payer.publicKey.toBase58(), payoutMint: SOLANA.usdc_mint, profit: SOLANA.operator_profit_wallet });

  const started = [], logs = [];
  const log = (o) => logs.push(o);
  const adapter = await solanaAdapter(C, { connection, env, log });
  let breakRecord = false;
  const record = adapter.record;
  adapter.record = async (...a) => { if (breakRecord) { breakRecord = false; throw new Error('pool read timed out'); } return record(...a); };
  const server = makeServer(cfg, { chains: { solana: adapter }, store: pythonStore(), log,
    inviteHashes: crypto.createHash('sha256').update(INVITE).digest('hex'),
    start: async (cmd) => { started.push(cmd); return { code: 0, out: '', err: '' }; },
    status: async () => ({ code: 0, out: started.length ? 'active\n' : 'inactive\n', err: '' }) });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}/bot/`;
  const post = async (p, b) => {
    const r = await fetch(`${base}api/${p}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ invite: INVITE, ...b }) });
    return { status: r.status, json: await r.json() };
  };
  const gas = gasTotal(C);
  const doms = [];
  try {
    // --- 1. the page, end to end -----------------------------------------------------
    const { dom, w, $ } = await openPage(base, INVITE);
    doms.push(dom);
    assert.equal($('invite-box').hidden, true, 'the invite came in the fragment');
    const { wallet, signed } = standardWallet(fork, ledger, owner);
    w.dispatchEvent(new w.CustomEvent('wallet-standard:register-wallet', { detail: ({ register }) => register(wallet) }));
    $('chain').value = 'solana';
    $('connect').click();
    await wait(() => $('wallet').textContent.includes(owner.publicKey.toBase58()));
    assert.equal($('profit').value, owner.publicKey.toBase58(), 'the profit wallet defaults to the connected one');
    assert.match($('bounds').textContent, /From 0.165 to 50 SOL, of which 0.145 SOL pays the bot's gas/);
    $('amount').value = '1.5';
    $('token').value = 'SOL';
    $('name').value = NAME;
    const payerBefore = fork.balance(payer.publicKey), ownerBefore = fork.balance(owner.publicKey);
    $('sign').click();
    await wait(() => /The bot runs/.test($('progress').textContent) || $('error').textContent);
    assert.equal($('error').textContent, '', $('error').textContent + JSON.stringify(logs.filter(l => l.event !== 'connect_report')));

    // what the owner signed: every value, once; the gas to the bot's key, named
    assert.equal(signed.length, 1);
    assert.match(signed[0], new RegExp(`Amount: 1.5 SOL\\nGas to the bot: 0.145 SOL, paid now to the bot's key ${payer.publicKey.toBase58()}`));
    assert.match(signed[0], new RegExp(`Deposit into the vault: 1.355 SOL\\nProfit wallet: ${owner.publicKey.toBase58()}\\nName: ${NAME}`));
    assert.match(signed[0], /Fee share: 10% of the USDC fees/);

    // the money: the owner paid the gas and the deposit; the setup cost less than its budget
    const o = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME}'`);
    assert.equal(o.state, 'started');
    assert.ok(ledger.txs.has(o.payment), 'the payment the server took is the one the wallet sent');
    const vault = o.vault;
    assert.equal(fork.balance(vault), 1_355_000_000);
    const spentBySetup = payerBefore + Number(gas) - fork.balance(payer.publicKey);
    assert.ok(spentBySetup > 0 && BigInt(spentBySetup) <= toRaw(C.gas.setup, 9) + toRaw(C.gas.margin, 9), `setup spent ${spentBySetup}`);
    assert.equal(ownerBefore - fork.balance(owner.publicKey), 1_500_000_000 + 2 * 5000, 'gas + deposit + two fees');
    assert.deepEqual(started, [['sudo', '-n', '/usr/bin/systemctl', 'enable', '--now', `lp-bot@${NAME}.service`]]);

    // the vault: the owner alone, threshold 1, the bot's policies
    const s = sq.decodeSettings(fork.get(o.connected.settings).data);
    assert.deepEqual(s.signers.map(x => x.key.toBase58()), [owner.publicKey.toBase58()]);
    assert.equal(s.threshold, 1);

    // the rows: the profile is the template's with this vault, pool, profit wallet and fee share
    const c = json(`select row_to_json(c) from rebalancer.config c where name = '${NAME}'`);
    const t = json(`select row_to_json(c) from rebalancer.config c where name = '${TEMPLATE}'`);
    assert.equal(c.wallet_id, `${NAME}-vault`);
    assert.deepEqual([c.dex, c.pool, c.execute_dexes], ['raydium-clmm', SOLANA.sol_usdc_pools['raydium-clmm'], ['raydium-clmm']]);
    assert.equal(c.profit_wallet, owner.publicKey.toBase58());
    assert.deepEqual([c.special, c.fee_bps, c.fee_wallet], [false, 1000, SOLANA.operator_profit_wallet]);
    assert.deepEqual([c.payout_mint, c.deposit_mint, c.pair_label], [SOLANA.usdc_mint, SOLANA.native_mint, 'SOL/USDC']);
    for (const k of ['bands', 'regime_widths', 'max_usd', 'poll_seconds', 'harvest_interval_hours', 'payout_enabled']) assert.deepEqual(c[k], t[k], k);
    assert.equal(Number(c.min_deploy_usd), 2, 'onboard.json settings over the template');
    const wrow = json(`select row_to_json(w) from rebalancer.wallets w where id = '${NAME}-vault'`);
    assert.deepEqual([wrow.address, wrow.secret_env], [vault, C.payerKeyEnv]);
    assert.equal(json(`select row_to_json(v) from rebalancer.vaults v where wallet_id = '${NAME}-vault'`).policy_key_env, C.policyKeyEnv);
    const usdc = new PublicKey(SOLANA.usdc_mint);
    assert.deepEqual(json(`select to_json(payouts) from rebalancer.vault_policies where wallet_id = '${NAME}-vault'`),
      [owner.publicKey, new PublicKey(SOLANA.operator_profit_wallet)].map(k => spl.getAssociatedTokenAddressSync(usdc, k, true).toBase58()),
      'the profit wallet\'s and the fee wallet\'s USDC accounts');
    await wait(() => /running/.test($('vaults').textContent));
    assert.match($('vaults').textContent, /1\.355 SOL/);

    // --- 2. bad payments for a second owner: refused, nothing spent ---------------------
    const it = (await post('intent', { chain: 'solana', owner: owner2.publicKey.toBase58(), token: 'SOL', amount: '1',
      profitWallet: SOLANA.operator_profit_wallet, name: NAME2 })).json;
    assert.match(it.message, /Fee share: none/, 'the operator\'s own wallet is special');
    const sg = await post('sign', { nonce: it.nonce, message: it.message, signature: bs58.encode(sign(owner2, Buffer.from(it.message))) });
    assert.equal(sg.status, 200, JSON.stringify(sg.json));
    const payerMid = fork.balance(payer.publicKey);
    const pay = (payment) => post('pay', { name: NAME2, payment });
    const missing = await pay(bs58.encode(crypto.randomBytes(64)));
    assert.deepEqual([missing.status, missing.json.error], [409, 'the payment is not on chain (yet); the page asks again']);
    const cases = [
      [await transfer(fork, ledger, owner2, payer.publicKey, Number(gas) - 1), /less than 0.145/],
      [await transfer(fork, ledger, stranger, payer.publicKey, Number(gas)), /not from the owner/],
      [await transfer(fork, ledger, owner2, stranger.publicKey, Number(gas)), /does not go to the bot's key/],
      [o.payment, /not from the owner/],                                                 // owner 1's payment
      ['not-a-signature', /not a transaction signature/],
    ];
    for (const [payment, re] of cases) {
      const r = await pay(payment);
      assert.equal(r.status, 400, payment);
      assert.match(r.json.error, re);
    }
    assert.equal(fork.balance(payer.publicKey), payerMid + 2 * Number(gas) - 1, 'the bot spent nothing (it only received)');
    assert.equal(json(`select to_json(state) from rebalancer.onboard where name = '${NAME2}'`), 'signed');

    // --- 3. a good payment, a setup that stops, a resume from the page --------------------
    const good = await transfer(fork, ledger, owner2, payer.publicKey, Number(gas));
    breakRecord = true;
    const stopped = await pay(good);
    assert.equal(stopped.status, 502);
    assert.match(stopped.json.error, /the setup stopped: pool read timed out\. Your gas payment is kept: press Resume/);
    assert.match(stopped.json.error, /The vault \w+ exists; do not deposit/);
    const s2 = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME2}'`);
    assert.deepEqual([s2.state, s2.payment, s2.connected.sent], ['stuck', good, true]);
    const p2 = await openPage(base, INVITE);
    doms.push(p2.dom);
    const w2 = standardWallet(fork, ledger, owner2);
    p2.w.dispatchEvent(new p2.w.CustomEvent('wallet-standard:register-wallet', { detail: ({ register }) => register(w2.wallet) }));
    p2.$('connect').click();
    await wait(() => /setup stopped: pool read timed out/.test(p2.$('vaults').textContent));
    const afterStop = fork.balance(payer.publicKey);
    await cardAction(p2.$, 'Resume');
    assert.match(p2.$('progress').textContent, /The bot runs ob-fork-two/);
    assert.equal(fork.balance(payer.publicKey), afterStop, 'the resume connected nothing again');
    assert.equal(w2.signed.length, 0, 'and asked the owner to sign no message, only the deposit');
    const r2 = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME2}'`);
    assert.deepEqual([r2.state, r2.vault], ['started', s2.vault]);
    assert.equal(fork.balance(r2.vault), 1_000_000_000 - Number(gas));
    const c2 = json(`select row_to_json(c) from rebalancer.config c where name = '${NAME2}'`);
    assert.deepEqual([c2.special, c2.fee_bps, c2.fee_wallet, c2.profit_wallet], [true, 0, null, SOLANA.operator_profit_wallet]);
    assert.equal((await pay(good)).json.state, 'started', 'a done request spends nothing again');
    // one pool per wallet: owner2 adds capital to its vault, never signs for a second
    const second = await post('intent', { chain: 'solana', owner: owner2.publicKey.toBase58(), token: 'SOL', amount: '1',
      profitWallet: owner2.publicKey.toBase58(), name: 'ob-fork-three' });
    assert.deepEqual([second.status, second.json.error], [403, 'this wallet has its vault already: put more capital in with Add capital under Your vaults']);
    // the same payment, replayed for another wallet's request (a used payment of the
    // same owner is refused by the store: test_onboard.mjs, test_onboard_store.py)
    const owner4 = fork.wallet(5);
    const it3 = (await post('intent', { chain: 'solana', owner: owner4.publicKey.toBase58(), token: 'SOL', amount: '1',
      profitWallet: owner4.publicKey.toBase58(), name: 'ob-fork-three' })).json;
    assert.equal((await post('sign', { nonce: it3.nonce, message: it3.message, signature: bs58.encode(sign(owner4, Buffer.from(it3.message))) })).status, 200);
    const replay = await post('pay', { name: 'ob-fork-three', payment: good });
    assert.equal(replay.status, 400);
    assert.equal(replay.json.error, `the payment is not from the owner ${owner4.publicKey.toBase58()}`, 'another wallet\'s payment pays for nothing');

    // --- 4. add capital, withdraw and revoke, from the first owner's page ----------------------
    assert.equal($('form').hidden, true, 'a wallet with a vault gets no new-vault form');
    const add = $('vaults').querySelector('.vault .add');
    add.querySelector('select').value = 'SOL';
    const maxAdd = [...add.querySelectorAll('button')].find(b => b.textContent === 'Max');
    maxAdd.click();
    await wait(() => add.querySelector('input').value || $('error').textContent);
    assert.equal(add.querySelector('input').value, fromRaw(BigInt(fork.balance(owner.publicKey)) - 1_000_000n, 9),
      'Max: the whole wallet less the fees kept back, no gas and no minimum');
    add.querySelector('input').value = '0,2';
    const beforeAdd = fork.balance(owner.publicKey);
    [...add.querySelectorAll('button')].find(b => b.textContent === 'Add capital').click();
    await wait(() => /Add capital: done/.test($('progress').textContent) || $('error').textContent);
    assert.equal($('error').textContent, '');
    assert.equal(fork.balance(vault), 1_555_000_000, 'into the same vault');
    assert.equal(beforeAdd - fork.balance(owner.publicKey), 200_000_000 + 5000);
    assert.equal(signed.length, 1, 'adding capital asks for no message');
    const before = fork.balance(owner.publicKey);
    await cardAction($, 'Withdraw', (card) => { card.querySelector(':scope > .row input').value = '0.7'; card.querySelector(':scope > .row select').value = 'SOL'; });
    assert.equal(fork.balance(vault), 855_000_000);
    assert.equal(fork.balance(owner.publicKey) - before, 700_000_000 - 5000);
    await wait(() => [...$('vaults').querySelectorAll('button')].some(b => b.textContent === 'Revoke bot'));
    await cardAction($, 'Revoke bot');
    for (const p of json(`select to_json(policies) from rebalancer.vault_policies where wallet_id = '${NAME}-vault'`)) {
      assert.deepEqual(sq.decodePolicyHead(fork.get(p).data).signers.map(x => x.key.toBase58()), [owner.publicKey.toBase58()]);
    }
    await wait(() => /bot revoked/.test($('vaults').textContent));
    const again = await post('intent', { chain: 'solana', owner: owner.publicKey.toBase58(), token: 'SOL', amount: '1',
      profitWallet: owner.publicKey.toBase58(), name: NAME });
    assert.equal(again.status, 409, 'the name is taken');

    // --- 5. Max fills the box with what the wallet can put in; a Sign with it goes through ---
    const owner3 = fork.wallet(0.5);
    const p3 = await openPage(base, INVITE);
    doms.push(p3.dom);
    const w3 = standardWallet(fork, ledger, owner3);
    p3.w.dispatchEvent(new p3.w.CustomEvent('wallet-standard:register-wallet', { detail: ({ register }) => register(w3.wallet) }));
    p3.$('connect').click();
    await wait(() => p3.$('wallet').textContent.includes(owner3.publicKey.toBase58()));
    const max = p3.$('max');
    assert.deepEqual([max.tagName, max.type, max.parentElement, max.previousElementSibling], ['BUTTON', 'button', p3.$('amount').parentElement, p3.$('amount')],
      'a real button, beside the amount box');
    const pressMax = async () => {
      p3.$('error').textContent = '';
      max.click();
      await wait(() => !max.disabled);
    };
    p3.$('token').value = 'USDC';
    p3.$('amount').value = '12';
    await pressMax();
    assert.equal(p3.$('error').textContent, 'not enough: you have 0 USDC, the minimum is 10 USDC');
    assert.equal(p3.$('amount').value, '12', 'the box is left as it was');
    fork.tokenAccount(spl.getAssociatedTokenAddressSync(usdc, owner3.publicKey), usdc, owner3.publicKey, 25_250_001n);
    await pressMax();
    assert.deepEqual([p3.$('amount').value, p3.$('error').textContent], ['25.250001', ''], 'the whole USDC account, read on chain');
    p3.$('token').value = 'SOL';
    await pressMax();
    assert.deepEqual([p3.$('amount').value, p3.$('error').textContent], ['0.499', ''], '0.5 SOL less 0.001 kept back for the fees');
    p3.$('name').value = NAME3;
    p3.$('sign').click();
    await wait(() => /The bot runs/.test(p3.$('progress').textContent) || p3.$('error').textContent);
    assert.equal(p3.$('error').textContent, '', JSON.stringify(logs.filter(l => l.event !== 'connect_report').slice(-5)));
    assert.match(w3.signed[0], /Amount: 0.499 SOL\nGas to the bot: 0.145 SOL/);
    const o3 = json(`select row_to_json(o) from rebalancer.onboard o where name = '${NAME3}'`);
    assert.deepEqual([o3.state, o3.amount, o3.deposit], ['started', 0.499, 0.354]);
    assert.equal(fork.balance(o3.vault), 354_000_000);
    assert.equal(fork.balance(owner3.publicKey), 1_000_000 - 2 * 5000, 'the fees kept back, less the two it paid: still above the rent floor');
  } finally {
    for (const d of doms) d.window.close();
    server.close();
    clean(NAME, TEMPLATE); clean(NAME2, TEMPLATE); clean('ob-fork-three', TEMPLATE); clean(NAME3, TEMPLATE);
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
