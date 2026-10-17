// onboard/verify.mjs: the owner's check outside the public server. The rules
// and pins come from a git commit; the transaction from the page's copy (or
// the server); the vault from the chain. The same decoder as the page.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'module';
import * as sq from '../chains/solana/vault/squads.mjs';
import { compile } from '../chains/solana/vault/policy.mjs';
import * as POLYGON from '../chains/evm/polygon.mjs';
import { ata } from '../onboard/decode_sol.mjs';
import { atCommit, chainProblems, check, flags, main, pinsOf } from '../onboard/verify.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Transaction } = require('@solana/web3.js');

const read = (f) => fs.readFileSync(new URL(`../${f}`, import.meta.url), 'utf8');
const ONBOARD = JSON.parse(read('onboard/onboard.json'));
const PINS = pinsOf(read('chains/solana/vault/rules.json'), read('chains/solana/solana.json'), read('onboard/onboard.json'), POLYGON);
const OWNER = Keypair.generate().publicKey, SETTINGS = Keypair.generate().publicKey, VAULT = sq.vaultPda(SETTINGS);
const VENUE = 'raydium-clmm', POOL = SOLANA.sol_usdc_pools[VENUE];
const USDC = new PublicKey(SOLANA.usdc_mint), PAYER = new PublicKey(ONBOARD.chains.solana.botKeys[0]);
const payoutWallets = [OWNER.toBase58(), SOLANA.operator_profit_wallet];
const compiled = compile(VENUE, { vault: VAULT, payer: PAYER, pools: [new PublicKey(POOL)], payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies;
const POLICIES = compiled.map(() => Keypair.generate().publicKey.toBase58());
const expect = { kind: 'approve', owner: OWNER.toBase58(), settings: SETTINGS.toBase58(), vault: VAULT.toBase58(), policies: { [VENUE]: POLICIES },
  pools: { [VENUE]: [POOL] }, payoutWallets };

function approveTx(i, signer = new PublicKey(ONBOARD.chains.solana.botKeys[1]), policy = compiled[i].policy) {
  const tx = new Transaction().add(sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER], rentPayer: OWNER, extra: [new PublicKey(POLICIES[i])],
    actions: [{ kind: 'policyUpdate', address: new PublicKey(POLICIES[i]), policy, signers: [{ key: signer }] }] }));
  tx.feePayer = OWNER; tx.recentBlockhash = PublicKey.default.toBase58();
  return tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');
}

test('the pins of a commit: its rules (hashed), pools, the bot\'s keys and the operator', () => {
  assert.match(PINS.rulesSha256, /^[0-9a-f]{64}$/);
  assert.equal(PINS.solana.payer, ONBOARD.chains.solana.botKeys[0]);
  assert.equal(PINS.solana.policyKey, ONBOARD.chains.solana.botKeys[1]);
  assert.deepEqual(PINS.solana.pools[VENUE], [POOL, ...(SOLANA.vault_extra_pools?.[VENUE] ?? [])]);   // the home pool, then the extra ones
  assert.equal(PINS.polygon.botKey, ONBOARD.chains.polygon.botKeys[0]);
  assert.equal(PINS.polygon.npm, POLYGON.V3.npm);
  // git show, for a commit id only
  assert.equal(atCommit('abc1234', 'x', (args) => args.join(' ')), 'show abc1234:x');
  for (const bad of ['HEAD', 'main', '--output=x', 'abc', '']) assert.throws(() => atCommit(bad, 'x', () => ''), /commit id/);
  assert.deepEqual(flags(['--a', '1', '--b', '2']), { a: '1', b: '2' });
  assert.throws(() => flags(['--a']), /pairs/);
});

test('an approval is checked against the commit\'s rules: the right one passes, another key or rule does not', () => {
  for (let i = 0; i < POLICIES.length; i++) assert.equal(check('solana', [approveTx(i)], () => expect, PINS)[0].ok, true, `policy ${i}`);
  const r = check('solana', [approveTx(0, Keypair.generate().publicKey)], () => expect, PINS)[0];
  assert.equal(r.ok, false);
  assert.match(r.problems.join(), /not the bot's rules key/);
  const last = POLICIES.length - 1;
  const other = { ...PINS, solana: { ...PINS.solana, rules: { ...PINS.solana.rules, common: PINS.solana.rules.common.slice(1) } } };
  assert.equal(check('solana', [approveTx(last)], () => expect, other)[0].ok, false, 'the rules of another commit differ');
});

test('the vault on chain: the owner alone, the policies its own', async () => {
  // a connection that serves the settings and policy accounts a real vault has, built by the program's own layout
  const head = (s) => {
    const parts = [Buffer.from(require('node:crypto').createHash('sha256').update('account:Policy').digest().subarray(0, 8)), s.toBuffer(),
      Buffer.alloc(8), Buffer.from([255]), Buffer.alloc(16), Buffer.from([0, 0, 0, 0]), Buffer.alloc(6)];
    return Buffer.concat(parts);
  };
  const settingsData = (owners, threshold) => {
    const w = [Buffer.from(require('node:crypto').createHash('sha256').update('account:Settings').digest().subarray(0, 8)), Buffer.alloc(16), PublicKey.default.toBuffer()];
    const t = Buffer.alloc(2); t.writeUInt16LE(threshold);
    const n = Buffer.alloc(4); n.writeUInt32LE(owners.length);
    w.push(t, Buffer.alloc(4), Buffer.alloc(8), Buffer.alloc(8), Buffer.from([0]), Buffer.alloc(8), Buffer.from([255]), n,
      ...owners.map(o => Buffer.concat([o.toBuffer(), Buffer.from([7])])), Buffer.from([0]), Buffer.from([0]));
    return Buffer.concat(w);
  };
  const conn = (owners, threshold = 1, policyOf = SETTINGS) => ({
    getAccountInfo: async () => ({ data: settingsData(owners, threshold) }),
    getMultipleAccountsInfo: async (ks) => ks.map(() => ({ data: head(policyOf) })),
  });
  assert.deepEqual(await chainProblems(conn([OWNER]), expect), []);
  assert.match((await chainProblems(conn([OWNER, Keypair.generate().publicKey]), expect)).join(), /not your wallet alone/);
  assert.match((await chainProblems(conn([OWNER], 2), expect)).join(), /not your wallet alone/);
  assert.match((await chainProblems(conn([OWNER], 1, Keypair.generate().publicKey), expect)).join(), /belongs to another vault/);
  assert.match((await chainProblems(conn([OWNER]), { ...expect, vault: Keypair.generate().publicKey.toBase58() })).join(), /not the account of these settings/);
  assert.match((await chainProblems({ getAccountInfo: async () => null }, expect)).join(), /does not exist/);
});

test('the command: a pasted transaction at HEAD, OK; a tampered one, refused', async () => {
  const log = console.log, out = [];
  console.log = (x) => out.push(String(x));
  try {
    // the files of a commit, here this checkout's (git show stubbed)
    const git = (args) => read(args[1].split(':')[1]);
    const head = 'abcdef1';
    const paste = (tx) => JSON.stringify({ chain: 'solana', tx, expect });
    assert.equal(await main(['--commit', head, '--rpc', 'none', '--paste', paste(approveTx(0))], { git }), 0, out.join('\n'));
    assert.match(out.join('\n'), /transaction 1 of 1: OK/);
    out.length = 0;
    assert.equal(await main(['--commit', head, '--rpc', 'none', '--paste', paste(approveTx(0, OWNER))], { git }), 1);
    assert.match(out.join('\n'), /REFUSED[\s\S]*do not sign/);
  } finally { console.log = log; }
});

test('the command: the chain is read for a Solana vault, never for an EVM one; what it needs, it asks for', async () => {
  const log = console.log, out = [];
  console.log = (x) => out.push(String(x));
  const git = (args) => read(args[1].split(':')[1]);
  try {
    const paste = JSON.stringify({ chain: 'solana', tx: approveTx(0), expect });
    let read0 = 0;
    const wrongOwner = { getAccountInfo: async () => { read0++; return null; }, getMultipleAccountsInfo: async () => [] };
    assert.equal(await main(['--commit', 'abcdef1', '--paste', paste], { git, connection: wrongOwner }), 1, 'the vault is not on chain: a problem');
    assert.equal(read0, 1);
    assert.match(out.join('\n'), /CHAIN: the settings account .* does not exist/);
    const never = { getAccountInfo: async () => { throw new Error('read'); } };
    assert.equal(await main(['--commit', 'abcdef1', '--rpc', 'none', '--paste', paste], { git, connection: never }), 0, '--rpc none reads nothing');
    const evm = JSON.stringify({ chain: 'polygon', tx: { to: ONBOARD.chains.polygon.botKeys[0], data: '0x', value: '0x1' },
      expect: { kind: 'payment', owner: '0x70997970C51812dc3A010C7d01b50e0d17dc79C8', lamports: '1' } });
    assert.equal(await main(['--commit', 'abcdef1', '--paste', evm], { git, connection: never }), 0, 'a Safe: no Solana read');
  } finally { console.log = log; }
  await assert.rejects(main(['--paste', '{}'], { git }), /^Error: --commit <the git commit whose rules you trust>$/);
  for (const missing of ['from', 'invite', 'name', 'owner', 'action']) {
    const f = { from: 'https://x.test/bot', invite: 'i', name: 'n', owner: 'o', action: 'approve' };
    delete f[missing];
    await assert.rejects(main(['--commit', 'abcdef1', ...Object.entries(f).flat().map((v, i) => (i % 2 ? v : `--${v}`))], { git }), new RegExp(`--${missing} is needed`));
  }
  assert.equal(PINS.polygon.tokens.POL.address, null, 'the native coin has no token address');
  assert.equal(PINS.polygon.tokens.WPOL.address, POLYGON.WPOL);
});

test('a set-up the wallet approves at once: pasted as parts, or read from the server (--action setup), checked as a whole', async () => {
  const { ownerSetupPlan, packSteps } = await import('../chains/solana/vault/admin.mjs');
  const { setupExpectOf } = await import('../onboard/verify.mjs');
  const http = await import('node:http');
  const { SystemProgram } = require('@solana/web3.js');
  const seed = 4321n, settings = sq.settingsPda(seed), vault = sq.vaultPda(settings);
  const policyKey = new PublicKey(ONBOARD.chains.solana.botKeys[1]);
  const venueCompiled = compile(VENUE, { vault, payer: PAYER, pools: [new PublicKey(POOL)], payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) });
  // the page's one-time set-up key signs the policies (too long for a hardware wallet); the server builds with the key it is sent
  const KEY = Keypair.generate().publicKey;
  let key = KEY;
  const plan = () => ownerSetupPlan({ owner: OWNER, treasury: Keypair.generate().publicKey, seed, policyKey, venues: [venueCompiled], setupKey: key });
  const wire = (ixs, payer = OWNER) => {
    const tx = new Transaction().add(...ixs); tx.feePayer = payer; tx.recentBlockhash = PublicKey.default.toBase58();
    return tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');
  };
  const parts = (gasTo = PAYER) => {
    const p = plan();
    return { owner: [wire([p.steps[0], SystemProgram.transfer({ fromPubkey: OWNER, toPubkey: key, lamports: 20_000_000 })])],
      key: p.steps.slice(1).map(ix => wire([ix], key)),
      fund: [wire([SystemProgram.transfer({ fromPubkey: OWNER, toPubkey: vault, lamports: 400_000_000 }), SystemProgram.transfer({ fromPubkey: OWNER, toPubkey: gasTo, lamports: 118_600_000 })])] };
  };
  const build = (gasTo) => { const p = parts(gasTo); return [...p.owner, ...p.key, ...p.fund]; };
  const it = { name: 'my-pool', done: false, ...parts(), terms: { token: 'SOL', amount: '0.5186', deposit: '0.4', gas: '0.1186',
    profitWallet: OWNER.toBase58(), feeWallet: SOLANA.operator_profit_wallet },
  context: { seed: String(seed), settings: settings.toBase58(), vault: vault.toBase58(), report: { venues: { [VENUE]: { pools: [POOL] } } } } };
  const e = { ...setupExpectOf(it, OWNER.toBase58(), PINS), setupKey: KEY.toBase58() };
  assert.deepEqual([e.kind, e.venues, e.raw, e.gasRaw, e.payoutWallets], ['setup', [VENUE], '400000000', '118600000', payoutWallets]);
  const show = (args) => read(args[1].split(':')[1]);
  const log = console.log, out = [];
  console.log = (x) => out.push(String(x));
  let srv;
  try {
    const paste = (txs) => JSON.stringify({ chain: 'solana', parts: [{ tx: txs, expect: e }] });
    assert.equal(await main(['--commit', 'abcdef1', '--rpc', 'none', '--paste', paste(build())], { git: show }), 0, out.join('\n'));
    assert.match(out.join('\n'), /transaction 1 of 1: OK[\s\S]*Transaction 1 of 4:[\s\S]*Create your vault[\s\S]*signed and paid by the set-up key/);
    out.length = 0;
    assert.equal(await main(['--commit', 'abcdef1', '--rpc', 'none', '--paste', paste(build(Keypair.generate().publicKey))], { git: show }), 1);
    assert.match(out.join('\n'), /0 payments to the bot's gas key[\s\S]*do not sign/);
    // the vault not created yet is no chain problem for a set-up
    assert.deepEqual(await chainProblems({ getAccountInfo: async () => null }, e), []);
    // created, the page's set-up key beside the owner: a set-up under way; any other beside it is not
    const settingsOf = (owners) => {
      const w = [Buffer.from(require('node:crypto').createHash('sha256').update('account:Settings').digest().subarray(0, 8)), Buffer.alloc(16), PublicKey.default.toBuffer()];
      const t = Buffer.alloc(2); t.writeUInt16LE(1);
      const n = Buffer.alloc(4); n.writeUInt32LE(owners.length);
      w.push(t, Buffer.alloc(4), Buffer.alloc(8), Buffer.alloc(8), Buffer.from([0]), Buffer.alloc(8), Buffer.from([255]), n,
        ...owners.map(o => Buffer.concat([o.toBuffer(), Buffer.from([7])])), Buffer.from([0]), Buffer.from([0]));
      return Buffer.concat(w);
    };
    const at = (owners) => ({ getAccountInfo: async () => ({ data: settingsOf(owners) }), getMultipleAccountsInfo: async () => [] });
    assert.deepEqual(await chainProblems(at([OWNER, KEY]), e), []);
    assert.deepEqual(await chainProblems(at([OWNER]), e), []);
    assert.match((await chainProblems(at([OWNER, Keypair.generate().publicKey]), e)).join(), /not your wallet alone/);
    assert.match((await chainProblems(at([OWNER, KEY]), { ...e, kind: 'approve' })).join(), /not your wallet alone/, 'outside a set-up: the owner alone');
    // from the server: its set-up of the request
    let tamper = false;
    const sent = [];
    srv = http.createServer((req, res) => {
      let body = '';
      req.on('data', (d) => { body += d; });
      req.on('end', () => {
        assert.equal(req.url, '/bot/api/setup');
        const b = JSON.parse(body);
        assert.equal(b.name, 'my-pool');
        // the server builds with the key it is sent
        key = new PublicKey(b.setupKey);
        sent.push(b.setupKey);
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ ...it, ...parts(tamper ? Keypair.generate().publicKey : PAYER) }));
      });
    });
    await new Promise(r => srv.listen(0, '127.0.0.1', r));
    const from = ['--commit', 'abcdef1', '--rpc', 'none', '--from', `http://127.0.0.1:${srv.address().port}/bot`, '--invite', 'i', '--name', 'my-pool',
      '--owner', OWNER.toBase58(), '--action', 'setup'];
    out.length = 0;
    assert.equal(await main(from, { git: show }), 0, out.join('\n'));
    assert.equal(await main([...from, '--setup-key', KEY.toBase58()], { git: show }), 0, out.join('\n'));
    assert.equal(sent.at(-1), KEY.toBase58(), 'the page\'s key, when given');
    assert.notEqual(sent[0], KEY.toBase58(), 'else a throwaway key stands for it');
    tamper = true;
    assert.equal(await main(from, { git: show }), 1);
    await assert.rejects(main([...from, '--chain', 'polygon'], { git: show }), /a Polygon set-up: paste what the page copied/);
  } finally { console.log = log; srv?.close(); }
});

test('the server\'s answer, read as the page reads it: every transaction of an action, one at a time or at once, and what each must be', async () => {
  const { fromServer } = await import('../onboard/verify.mjs');
  const http = await import('node:http');
  let answer = () => ({}), status = 200, asked = [];
  const srv = http.createServer((req, res) => {
    let body = '';
    req.on('data', (d) => { body += d; });
    req.on('end', () => {
      const b = JSON.parse(body);
      asked.push(b);
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(status === 599 ? 'not json' : JSON.stringify(answer(b)));
    });
  });
  await new Promise(r => srv.listen(0, '127.0.0.1', r));
  const from = `http://127.0.0.1:${srv.address().port}/bot`;
  const f = (o) => ({ from, invite: 'i', name: 'my-pool', owner: OWNER.toBase58(), ...o });
  try {
    // Solana: one at a time (a blockhash each), every index asked for, in order
    const ctx = { settings: SETTINGS.toBase58(), vault: VAULT.toBase58(), policies: { [VENUE]: POLICIES } };
    answer = (b) => ({ count: 3, index: b.index, transactions: [`tx${b.index}`], context: ctx });
    let job = await fromServer(f({ action: 'approve' }), PINS);
    assert.deepEqual([job.chain, job.txs], ['solana', ['tx0', 'tx1', 'tx2']]);
    assert.deepEqual(asked.map(b => [b.index, b.action, b.name, b.invite]), [[0, 'approve', 'my-pool', 'i'], [1, 'approve', 'my-pool', 'i'], [2, 'approve', 'my-pool', 'i']]);
    assert.deepEqual(job.expect(0), { kind: 'approve', owner: OWNER.toBase58(), ...ctx, mint: null, raw: 'all' });
    asked = [];
    job = await fromServer(f({ action: 'withdraw', token: 'SOL', amount: '0.5' }), PINS);
    assert.deepEqual([job.expect(1).mint, job.expect(1).raw], [SOLANA.native_mint, '500000000']);
    job = await fromServer(f({ action: 'withdraw', token: 'USDC', amount: '2.5' }), PINS);
    assert.deepEqual([job.expect(0).mint, job.expect(0).raw], [SOLANA.usdc_mint, '2500000']);
    job = await fromServer(f({ action: 'withdraw', token: 'USDC', amount: 'all' }), PINS);
    assert.equal(job.expect(0).raw, 'all');
    job = await fromServer(f({ action: 'withdraw', token: 'ALL', amount: 'all' }), PINS);
    assert.deepEqual([job.expect(0).mint, job.expect(0).raw], [null, 'all']);
    // Polygon: every transaction at once (no index), the Safe and its module from the context
    answer = () => ({ count: 2, transactions: ['a', 'b'], context: { safe: '0xSafe', roles: '0xRoles' } });
    job = await fromServer(f({ action: 'withdraw', token: 'WPOL', amount: '1.5', chain: 'polygon' }), PINS);
    assert.deepEqual([job.chain, job.txs], ['polygon', ['a', 'b']]);
    assert.deepEqual(job.expect(0), { kind: 'withdraw', owner: OWNER.toBase58(), safe: '0xSafe', roles: '0xRoles', token: 'WPOL', raw: '1500000000000000000' });
    // no context: nothing named
    answer = () => ({ count: 1, transactions: ['a'] });
    job = await fromServer(f({ action: 'revoke' }), PINS);
    assert.deepEqual(job.expect(0), { kind: 'revoke', owner: OWNER.toBase58(), mint: null, raw: 'all' });
    // the server's refusal, said as it says it; a body that is no answer
    status = 409;
    answer = () => ({ error: 'the request is signed, not set up' });
    await assert.rejects(fromServer(f({ action: 'approve' }), PINS), /^Error: the request is signed, not set up$/);
    answer = () => ({});
    await assert.rejects(fromServer(f({ action: 'approve' }), PINS), /^Error: the server answered 409$/);
  } finally { srv.close(); }
});

test('the chain for a set-up: a vault not made yet, or policies not made yet, are no problem; for anything else they are', async () => {
  const settingsData = (owners) => {
    const w = [Buffer.from(require('node:crypto').createHash('sha256').update('account:Settings').digest().subarray(0, 8)), Buffer.alloc(16), PublicKey.default.toBuffer()];
    const t = Buffer.alloc(2); t.writeUInt16LE(1);
    const n = Buffer.alloc(4); n.writeUInt32LE(owners.length);
    w.push(t, Buffer.alloc(4), Buffer.alloc(8), Buffer.alloc(8), Buffer.from([0]), Buffer.alloc(8), Buffer.from([255]), n,
      ...owners.map(o => Buffer.concat([o.toBuffer(), Buffer.from([7])])), Buffer.from([0]), Buffer.from([0]));
    return Buffer.concat(w);
  };
  const head = (s) => Buffer.concat([Buffer.from(require('node:crypto').createHash('sha256').update('account:Policy').digest().subarray(0, 8)), s.toBuffer(),
    Buffer.alloc(8), Buffer.from([255]), Buffer.alloc(16), Buffer.from([0, 0, 0, 0]), Buffer.alloc(6)]);
  const conn = (policies) => ({ getAccountInfo: async () => ({ data: settingsData([OWNER]) }), getMultipleAccountsInfo: async () => policies });
  const setup = { ...expect, kind: 'setup' };
  assert.deepEqual(await chainProblems(conn([null, null]), setup), [], 'the set-up makes them');
  assert.deepEqual((await chainProblems(conn([null, null]), expect)).length, 2, 'an approval needs them');
  assert.match((await chainProblems(conn([{ data: head(Keypair.generate().publicKey) }, null]), setup)).join(), /belongs to another vault/,
    'one that exists must be this vault\'s');
});
