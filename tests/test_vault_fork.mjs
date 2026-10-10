// The vault codec against the deployed Squads Smart Account program, on a
// local fork (tests/_svm.mjs). Every builder in chains/solana/vault/squads.mjs
// is proven here by the program itself: a wrong byte is a failed test.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';

const require = createRequire(import.meta.url);
const { PublicKey, SystemProgram, Keypair } = require('@solana/web3.js');
const { getAssociatedTokenAddressSync, createAssociatedTokenAccountIdempotentInstruction,
  createSyncNativeInstruction, NATIVE_MINT, TOKEN_PROGRAM_ID } = require('@solana/spl-token');

// The bot may move vault SOL only into a token account the vault owns: the
// wrap step before a deposit. Byte 32 of a token account is its owner.
function wrapPolicy(vault) {
  return { accountIndex: 0, constraints: [
    { programId: SystemProgram.programId,
      accounts: [{ index: 0, keys: [vault] },
        { index: 1, owner: TOKEN_PROGRAM_ID, data: [{ offset: 32, type: 'bytes', value: vault.toBuffer(), op: 'eq' }] }],
      data: [{ offset: 0, type: 'u32', value: 2, op: 'eq' }] },          // SystemInstruction::Transfer
    { programId: TOKEN_PROGRAM_ID,
      accounts: [{ index: 0, owner: TOKEN_PROGRAM_ID, data: [{ offset: 32, type: 'bytes', value: vault.toBuffer(), op: 'eq' }] }],
      data: [{ offset: 0, type: 'u8', value: 17, op: 'eq' }] },          // SyncNative
  ] };
}

async function setup() {
  const fork = new Fork();
  await fork.program(sq.PROGRAM_ID);
  await fork.clone([sq.programConfigPda()]);
  const config = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data);
  await fork.clone([config.treasury]);
  const owner = fork.wallet(1), bot = fork.wallet(1), policyKey = Keypair.generate();
  const seed = config.smartAccountIndex + 1n;
  // One transaction: create with the bot as a temporary signer, add the
  // policy, remove the bot. After it lands the bot is no owner.
  const { settings, ix: create } = sq.createSmartAccountIx({ creator: bot.publicKey, treasury: config.treasury, seed, threshold: 1,
    signers: [{ key: owner.publicKey }, { key: bot.publicKey }] });
  const vault = sq.vaultPda(settings), policy = sq.policyPda(settings, 1);
  const settle = sq.settingsSyncIx({ settings, signers: [bot.publicKey], rentPayer: bot.publicKey, extra: [policy], actions: [
    { kind: 'policyCreate', seed: 1, policy: wrapPolicy(vault), signers: [{ key: policyKey.publicKey }] },
    { kind: 'removeSigner', key: bot.publicKey },
  ] });
  const r = await fork.send([create, settle], bot);
  assert.ok(r.ok, r.logs.join('\n'));
  return { fork, owner, bot, policyKey, settings, vault, policy };
}

test('onboarding: one transaction leaves the owner alone in control and the policy in place', async () => {
  const { fork, owner, bot, policyKey, settings, policy } = await setup();
  const s = sq.decodeSettings(fork.get(settings).data);
  assert.deepEqual(s.signers.map(x => x.key.toBase58()), [owner.publicKey.toBase58()]);
  assert.equal(s.threshold, 1);
  assert.equal(s.policySeed, 1n);
  const p = sq.decodePolicyHead(fork.get(policy).data);
  assert.ok(p.settings.equals(settings));
  assert.deepEqual(p.signers.map(x => x.key.toBase58()), [policyKey.publicKey.toBase58()]);
  assert.ok(!p.signers.some(x => x.key.equals(bot.publicKey)));
});

test('the policy key wraps vault SOL into the vault\'s own WSOL account', async () => {
  const { fork, bot, policyKey, vault, policy } = await setup();
  fork.fund(vault, 0.5);
  const ata = getAssociatedTokenAddressSync(NATIVE_MINT, vault, true);
  // the token account exists first (created by the payer, owned by the vault)
  assert.ok((await fork.send([createAssociatedTokenAccountIdempotentInstruction(bot.publicKey, ata, vault, NATIVE_MINT)], bot)).ok);
  const ixs = [SystemProgram.transfer({ fromPubkey: vault, toPubkey: ata, lamports: 100_000_000 }), createSyncNativeInstruction(ata)];
  const r = await fork.send([sq.policyExecuteIx({ policy, signer: policyKey.publicKey, vault, ixs, indices: [0, 1] })], bot, [policyKey]);
  assert.ok(r.ok, r.logs.join('\n'));
  assert.equal(fork.get(ata).data.readBigUInt64LE(64), 100_000_000n);
});

test('the program refuses the same transfer to an account the vault does not own', async () => {
  const { fork, bot, policyKey, vault, policy } = await setup();
  fork.fund(vault, 0.5);
  const thief = Keypair.generate().publicKey;
  const theirs = getAssociatedTokenAddressSync(NATIVE_MINT, thief);
  assert.ok((await fork.send([createAssociatedTokenAccountIdempotentInstruction(bot.publicKey, theirs, thief, NATIVE_MINT)], bot)).ok);
  // a wallet fails the owner-program check, a token account the owner-field check
  for (const to of [thief, theirs]) {
    const ixs = [SystemProgram.transfer({ fromPubkey: vault, toPubkey: to, lamports: 100_000_000 })];
    const r = await fork.send([sq.policyExecuteIx({ policy, signer: policyKey.publicKey, vault, ixs, indices: [0] })], bot, [policyKey]);
    assert.ok(!r.ok, `transfer to ${to.toBase58()} went through`);
    assert.match(r.logs.join('\n'), /IllegalAccountOwner|ProgramInteractionInvalidNumericValue/);
  }
  assert.equal(fork.balance(vault), 500_000_000);
});

test('the bot key alone can neither use the policy nor act as an owner', async () => {
  const { fork, bot, vault, policy, settings } = await setup();
  fork.fund(vault, 0.5);
  const ixs = [SystemProgram.transfer({ fromPubkey: vault, toPubkey: bot.publicKey, lamports: 100_000_000 })];
  assert.ok(!(await fork.send([sq.policyExecuteIx({ policy, signer: bot.publicKey, vault, ixs, indices: [0] })], bot)).ok);
  assert.ok(!(await fork.send([sq.ownerExecuteIx({ settings, signers: [bot.publicKey], vault, ixs })], bot)).ok);
  assert.equal(fork.balance(vault), 500_000_000);
});

test('the owner withdraws alone, with no policy and no bot', async () => {
  const { fork, owner, vault, settings } = await setup();
  fork.fund(vault, 0.5);
  const before = fork.balance(owner.publicKey);
  const ixs = [SystemProgram.transfer({ fromPubkey: vault, toPubkey: owner.publicKey, lamports: 400_000_000 })];
  const r = await fork.send([sq.ownerExecuteIx({ settings, signers: [owner.publicKey], vault, ixs })], owner);
  assert.ok(r.ok, r.logs.join('\n'));
  assert.equal(fork.balance(vault), 100_000_000);
  assert.equal(fork.balance(owner.publicKey) - before, 400_000_000 - 5000);
});

test('the owner revokes the bot alone: the policy keeps only the owner, the bot key is powerless', async () => {
  const { fork, owner, bot, policyKey, vault, policy, settings } = await setup();
  fork.fund(vault, 0.5);
  // Removing the policy refunds its rent to the bot's payer, which would have
  // to sign; an update needs only the owners.
  const r = await fork.send([sq.settingsSyncIx({ settings, signers: [owner.publicKey], rentPayer: owner.publicKey, extra: [policy], actions: [
    { kind: 'policyUpdate', address: policy, policy: wrapPolicy(vault), signers: [{ key: owner.publicKey }] }] })], owner);
  assert.ok(r.ok, r.logs.join('\n'));
  assert.deepEqual(sq.decodePolicyHead(fork.get(policy).data).signers.map(x => x.key.toBase58()), [owner.publicKey.toBase58()]);
  const ata = getAssociatedTokenAddressSync(NATIVE_MINT, vault, true);
  assert.ok((await fork.send([createAssociatedTokenAccountIdempotentInstruction(bot.publicKey, ata, vault, NATIVE_MINT)], bot)).ok);
  const ixs = [SystemProgram.transfer({ fromPubkey: vault, toPubkey: ata, lamports: 1 }), createSyncNativeInstruction(ata)];
  assert.ok(!(await fork.send([sq.policyExecuteIx({ policy, signer: policyKey.publicKey, vault, ixs, indices: [0, 1] })], bot, [policyKey])).ok);
});

test('the bot\'s payer closes a policy and takes its rent back', async () => {
  const { fork, owner, bot, policy, settings } = await setup();
  // A removal is a settings change: the owners sign it, the payer signs for the refund.
  const before = fork.balance(bot.publicKey);
  const r = await fork.send([sq.settingsSyncIx({ settings, signers: [owner.publicKey], rentPayer: bot.publicKey, extra: [policy],
    actions: [{ kind: 'policyRemove', address: policy }] })], bot, [owner]);
  assert.ok(r.ok, r.logs.join('\n'));
  assert.equal(fork.get(policy), null);
  assert.ok(fork.balance(bot.publicKey) > before);
});
