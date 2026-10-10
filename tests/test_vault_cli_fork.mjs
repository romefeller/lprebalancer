// The operator's vault tool (chains/solana/vault/cli.mjs) on a fork of
// mainnet: connect a vault for an owner, then every owner request signed the
// way owner/index.html signs it (a fresh blockhash, the wallet's signature).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { connectedVault } from './_vault.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { request } from '../chains/solana/vault/cli.mjs';
import { actingAccount, PolicyMismatch } from '../chains/solana/vault/account.mjs';

const require = createRequire(import.meta.url);
const { Keypair, Message, Transaction } = require('@solana/web3.js');


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

test('connect: the owner alone controls the vault; the bot can act through its policies', { timeout: 300_000 }, async () => {
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
