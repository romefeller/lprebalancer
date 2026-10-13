// The operator's vault tool (chains/solana/vault/cli.mjs) on a fork of
// mainnet: the owner's own set-up of a vault and the bot's finish, then every
// owner request signed the way the page signs it (a fresh blockhash, the
// wallet's signature).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { connectedVault } from './_vault.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { request } from '../chains/solana/vault/cli.mjs';
import { actingAccount, PolicyMismatch } from '../chains/solana/vault/account.mjs';

const require = createRequire(import.meta.url);
const { Keypair, Message, Transaction, PublicKey } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';


// What the signing page does with a request: each message gets a fresh
// blockhash, the owner's wallet signs it, the page sends it.
async function ownerSigns(fork, req, owner) {
  const results = [];
  for (const b64 of req.messages) {
    const tx = Transaction.populate(Message.from(Buffer.from(b64, 'base64')));
    tx.recentBlockhash = fork.svm.latestBlockhash();
    tx.sign(owner);
    results.push(await fork.sendSigned(tx));
  }
  return results;
}

const setup = connectedVault;

test('set-up: the owner alone controls the vault; the bot can act through its policies', { timeout: 300_000 }, async () => {
  const { fork, connection, out, env, owner, payer } = await setup();
  assert.equal(out.sent, true);
  const s = sq.decodeSettings(fork.get(out.settings).data);
  assert.deepEqual(s.signers.map(x => x.key.toBase58()), [owner.publicKey.toBase58()]);
  const acting = await actingAccount({ connection, payer, venue: 'meteora-dlmm', env });
  assert.equal(acting.owner.toBase58(), out.vault);
  assert.ok(fork.get(out.lookup), 'the lookup table exists');
});

test('an owner withdraws SOL with one signature; a stranger cannot even get a request', { timeout: 300_000 }, async () => {
  const { fork, connection, out, env, owner } = await setup();
  fork.fund(out.vault, 0.5);
  const req = await request('withdraw', { owner: owner.publicKey.toBase58(), to: owner.publicKey.toBase58(), mint: 'SOL', amount: 'all' },
    { connection, env });
  assert.match(req.summary, /withdraw 0.5 SOL/);
  const before = fork.balance(owner.publicKey);
  for (const r of await ownerSigns(fork, req, owner)) assert.ok(r.ok, r.logs.join('\n'));
  assert.equal(fork.balance(out.vault), 0);
  assert.equal(fork.balance(owner.publicKey) - before, 500_000_000 - 5000);
  const stranger = Keypair.generate();
  await assert.rejects(request('withdraw', { owner: stranger.publicKey.toBase58(), to: stranger.publicKey.toBase58(), mint: 'SOL', amount: 'all' },
    { connection, env }), /not an owner/);
});

test('revoke stops the bot; approve brings it back; only the owner signs either', { timeout: 300_000 }, async () => {
  const { fork, connection, env, owner, payer, policyKey } = await setup();
  const revoke = await request('revoke', { owner: owner.publicKey.toBase58(), payer: payer.publicKey.toBase58() }, { connection, env });
  for (const r of await ownerSigns(fork, revoke, owner)) assert.ok(r.ok, r.logs.join('\n'));
  await assert.rejects(actingAccount({ connection, payer, venue: 'meteora-dlmm', env }), /revoked the bot/);
  const approve = await request('approve', { owner: owner.publicKey.toBase58(), payer: payer.publicKey.toBase58(),
    'policy-key': policyKey.publicKey.toBase58() }, { connection, env });
  for (const r of await ownerSigns(fork, approve, owner)) assert.ok(r.ok, r.logs.join('\n'));
  await actingAccount({ connection, payer, venue: 'meteora-dlmm', env });
  // the bot's own keys cannot sign an owner's request
  const forged = await request('revoke', { owner: owner.publicKey.toBase58(), payer: payer.publicKey.toBase58() }, { connection, env });
  const tx = Transaction.populate(Message.from(Buffer.from(forged.messages[0], 'base64')));
  tx.recentBlockhash = fork.svm.latestBlockhash();
  assert.throws(() => tx.sign(payer), /unknown signer/);
});

test('a request takes the whole balance or less, never nothing or more, and says what it does', { timeout: 300_000 }, async () => {
  const { fork, connection, out, env, owner, payer, policyKey } = await setup();
  const ask = (action, f) => request(action, { owner: owner.publicKey.toBase58(), ...f }, { connection, env });
  const me = owner.publicKey.toBase58();
  fork.fund(out.vault, 0.5);
  for (const amount of ['0', '0.6', 'x']) await assert.rejects(ask('withdraw', { to: me, mint: 'SOL', amount }), /the vault holds 0.5 SOL/, amount);
  assert.match((await ask('withdraw', { to: me, mint: 'SOL', amount: '0.5' })).summary, /withdraw 0.5 SOL/);
  assert.match((await ask('withdraw', { to: me, amount: '0.25' })).summary, /withdraw 0.25 SOL/);
  // an empty --mint is not read as SOL
  await assert.rejects(ask('withdraw', { to: me, mint: '', amount: '0.25' }), /public key/i);

  await fork.clone([new PublicKey(USDC)]);
  const src = spl.getAssociatedTokenAddressSync(new PublicKey(USDC), new PublicKey(out.vault), true);
  fork.tokenAccount(src, USDC, out.vault, 2_000_000);
  for (const amount of ['0', '2.000001']) await assert.rejects(ask('withdraw', { to: me, mint: USDC, amount }), /the vault holds 2 of/, amount);
  assert.match((await ask('withdraw', { to: me, mint: USDC, amount: '2' })).summary, /withdraw 2 of/);
  assert.match((await ask('withdraw', { to: me, mint: USDC, amount: '1.5' })).summary, /withdraw 1.5 of/);
  assert.match((await ask('withdraw', { to: me, mint: USDC, amount: 'all' })).summary, /withdraw 2 of/);

  await assert.rejects(request('withdraw', { to: me, amount: 'all' }, { connection, env }), /--owner/);
  await assert.rejects(ask('withdraw', { amount: 'all' }), /withdraw needs --to/);
  await assert.rejects(ask('revoke', {}), /needs LPBOT_VAULT_VENUE and --payer/);
  await assert.rejects(request('revoke', { owner: me, payer: payer.publicKey.toBase58() },
    { connection, env: { ...env, LPBOT_VAULT_VENUE: '' } }), /needs LPBOT_VAULT_VENUE/);
  await assert.rejects(ask('approve', { payer: payer.publicKey.toBase58() }), /approve needs --policy-key/);
  await assert.rejects(ask('pause', {}), /unknown request pause/);
  assert.match((await ask('revoke', { payer: payer.publicKey.toBase58() })).summary, /^revoke the bot: \d+ policies/);
  const approve = await ask('approve', { payer: payer.publicKey.toBase58(), 'policy-key': policyKey.publicKey.toBase58() });
  assert.match(approve.summary, new RegExp(`^approve for the bot \\(${policyKey.publicKey.toBase58()}\\)`));
  assert.ok(approve.summary.endsWith(`payouts ${out.payouts.join(', ')}`), approve.summary);
});

test('the owner\'s set-up resumes: cut after its first transaction, the rest finishes the same vault; the bot only after all of it', { timeout: 300_000 }, async () => {
  const { Fork } = await import('./_svm.mjs');
  const { finishSetup, setupRequest } = await import('../chains/solana/vault/cli.mjs');
  const { packSteps, lookupPlan } = await import('../chains/solana/vault/admin.mjs');
  const { DLMM_PROGRAM, POOL, freeSeeds } = await import('./_vault.mjs');
  const fork = new Fork();
  await Promise.all([fork.program(sq.PROGRAM_ID), fork.program(DLMM_PROGRAM)]);
  await fork.syncClock();
  await fork.clone([sq.programConfigPda()]);
  const index0 = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).smartAccountIndex;
  await fork.clone([sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).treasury]);
  freeSeeds(fork);
  const payer = Keypair.generate(), policyKey = Keypair.generate(), owner = fork.wallet(1), profit = Keypair.generate();
  const connection = fork.connection();
  const f = { owner: owner.publicKey.toBase58(), venue: 'meteora-dlmm', pools: POOL, payout: profit.publicKey.toBase58() };
  const keys = { connection, payer: payer.publicKey, policyKey: policyKey.publicKey };
  const first = await setupRequest(f, keys);
  assert.equal(first.seed, String(index0 + 1n));
  assert.deepEqual(first.done, []);
  const groups = packSteps(first.steps, owner.publicKey);
  assert.ok(groups.length >= 2);
  // the first transaction lands; the owner's wallet is closed before the rest
  const r0 = await fork.send(groups[0], owner);
  assert.ok(r0.ok, r0.logs.join('\n'));
  const half = await setupRequest({ ...f, seed: first.seed }, keys);
  assert.equal(half.settings, first.settings, 'the same vault');
  assert.equal(half.steps.length, first.steps.length - groups[0].length, 'only what is missing');
  assert.deepEqual(half.steps.map(ix => ix.data), first.steps.slice(groups[0].length).map(ix => ix.data));
  // the bot spends nothing while any of it is missing
  await assert.rejects(finishSetup(half, { connection, payer, policyKey: policyKey.publicKey, log: () => {} }), /not on chain yet/);
  assert.equal(fork.balance(payer.publicKey), 0, 'the bot holds nothing and paid nothing');
  for (const ixs of packSteps(half.steps, owner.publicKey)) {
    const r = await fork.send(ixs, owner);
    assert.ok(r.ok, r.logs.join('\n'));
  }
  const whole = await setupRequest({ ...f, seed: first.seed }, keys);
  assert.deepEqual(whole.steps, []);
  assert.equal(sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).smartAccountIndex, index0 + 1n, 'one vault, not two');
  // the owner's gas reaches the bot. A first try cut short after its table's create: 3 addresses of 20
  fork.fund(payer.publicKey, 0.05);
  const partial = lookupPlan({ payer: payer.publicKey, recentSlot: fork.recentSlot(), addresses: whole.lookupAddresses.slice(0, 3) });
  for (const ixs of partial.txs) assert.ok((await fork.send(ixs, payer)).ok);
  fork.nextSlot();
  // the retry with the saved table: only its missing addresses, the table saved again before anything is sent
  let saved = null;
  const out = await finishSetup(whole, { connection, payer, policyKey: policyKey.publicKey, lookup: partial.table.toBase58(), log: () => {},
    onLookup: (t) => { saved = t; } });
  assert.equal(out.lookup, partial.table.toBase58());
  assert.equal(saved, out.lookup);
  assert.deepEqual(out.owners, [owner.publicKey.toBase58()]);
  assert.equal(out.threshold, 1);
  assert.ok(out.signatures.length >= 1);
  fork.nextSlot();
  const { AddressLookupTableAccount } = require('@solana/web3.js');
  const table = AddressLookupTableAccount.deserialize(fork.get(partial.table).data);
  assert.equal(table.addresses.length, whole.lookupAddresses.length, 'each address once');
  // a retry with the whole table sends nothing more
  const again = await finishSetup(whole, { connection, payer, policyKey: policyKey.publicKey, lookup: out.lookup, log: () => {} });
  assert.equal(again.lookup, out.lookup);
  assert.deepEqual(again.signatures, []);
});

test('a seed another account took is planned again at the next free one', { timeout: 300_000 }, async () => {
  const { Fork } = await import('./_svm.mjs');
  const { setupRequest } = await import('../chains/solana/vault/cli.mjs');
  const { DLMM_PROGRAM, POOL, freeSeeds } = await import('./_vault.mjs');
  const fork = new Fork();
  await Promise.all([fork.program(sq.PROGRAM_ID), fork.program(DLMM_PROGRAM)]);
  await fork.syncClock();
  await fork.clone([sq.programConfigPda()]);
  const cfg = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data);
  await fork.clone([cfg.treasury]);
  freeSeeds(fork);
  const owner = fork.wallet(1), stranger = fork.wallet(2);
  const connection = fork.connection();
  const f = { owner: owner.publicKey.toBase58(), venue: 'meteora-dlmm', pools: POOL, payout: owner.publicKey.toBase58() };
  const keys = { connection, payer: Keypair.generate().publicKey, policyKey: Keypair.generate().publicKey };
  const planned = await setupRequest(f, keys);
  // another account is created first, at the planned seed
  const { ix } = sq.createSmartAccountIx({ creator: stranger.publicKey, treasury: cfg.treasury, seed: BigInt(planned.seed), threshold: 1,
    signers: [{ key: stranger.publicKey }] });
  assert.ok((await fork.send([ix], stranger)).ok);
  const again = await setupRequest({ ...f, seed: planned.seed }, keys);
  assert.equal(again.seed, String(BigInt(planned.seed) + 1n));
  assert.notEqual(again.settings, planned.settings);
  assert.deepEqual(again.done, []);
  // the owner's stale set-up fails on chain, whole: its creation names the taken settings
  const stale = await fork.send(planned.steps.slice(0, 1), owner);
  assert.equal(stale.ok, false);
});
