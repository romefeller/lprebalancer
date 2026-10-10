// The vault's rules, policies and planner, without a chain: what the fork
// tests (test_vault_fork.mjs, test_vault_dlmm_fork.mjs) cannot cover by
// example — every size limit, every refusal path, every ordering.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'module';
import fc from 'fast-check';
import * as sq from '../chains/solana/vault/squads.mjs';
import { RULES, compile, match, plan, needsVault, NotAllowed, POLICY_BYTES } from '../chains/solana/vault/policy.mjs';
import { vaultConfig, wrap, vaultFundsItsAccounts } from '../chains/solana/vault/account.mjs';
import { connectPlan, connectProblems, lookupPlan, staticAccounts } from '../chains/solana/vault/admin.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Transaction, TransactionInstruction, ComputeBudgetProgram, SystemProgram } = require('@solana/web3.js');

const key = () => Keypair.generate().publicKey;
const DLMM = new PublicKey(RULES.venues['meteora-dlmm'].program);
const BLOCKHASH = '11111111111111111111111111111111';

// The serialized size of a transaction of `ixs` signed by `signers`.
function txSize(ixs, signers) {
  const tx = new Transaction().add(...ixs);
  tx.feePayer = signers[0].publicKey;
  tx.recentBlockhash = BLOCKHASH;
  tx.sign(...signers);
  return tx.serialize().length;
}

// The largest vault the config allows in practice: several approved pools,
// two payout accounts (profit wallet and fee wallet).
const worst = () => ({ vault: key(), payer: key(), pools: [key(), key(), key()], payouts: [key(), key()] });

test('rules: every Anchor discriminator is the hash of its instruction name', () => {
  for (const [venue, v] of Object.entries(RULES.venues)) {
    for (const r of v.instructions) {
      const want = createHash('sha256').update(`global:${r.name}`).digest().subarray(0, 8).toString('hex');
      assert.equal(r.discriminator, want, `${venue} ${r.name}`);
    }
  }
});

test('rules: every pin names a known role and no account twice', () => {
  const roles = new Set(['pool', 'vault', 'vaultToken', 'vaultWsol', 'vaultOrPayer', 'payout', 'none']);
  const all = [...RULES.common, ...RULES.payout, ...Object.values(RULES.venues).flatMap(v => v.instructions)];
  for (const r of all) {
    assert.ok(r.pins.length > 0, `${r.name} pins nothing: it could send money anywhere`);
    for (const p of r.pins) assert.ok(roles.has(p.role), `${r.name}: role ${p.role}`);
    assert.equal(new Set(r.pins.map(p => p.index)).size, r.pins.length, r.name);
  }
});

test('policies: each packed policy fits one create and one update transaction', () => {
  const ctx = worst(), payer = Keypair.generate(), owner = Keypair.generate();
  const settings = key();
  const { policies } = compile('meteora-dlmm', ctx);
  assert.ok(policies.length >= 2, 'the DLMM rules do not fit one policy; packing must have split them');
  for (const [i, p] of policies.entries()) {
    assert.ok(sq.encodeProgramInteraction(p.policy).length <= POLICY_BYTES);
    const address = sq.policyPda(settings, i + 1);
    const create = sq.settingsSyncIx({ settings, signers: [payer.publicKey], rentPayer: payer.publicKey, extra: [address],
      actions: [{ kind: 'policyCreate', seed: i + 1, policy: p.policy, signers: [{ key: key() }] }] });
    assert.ok(txSize([create], [payer]) <= 1232, `create policy ${i}: ${txSize([create], [payer])} bytes`);
    const update = sq.settingsSyncIx({ settings, signers: [owner.publicKey], rentPayer: owner.publicKey, extra: [address],
      actions: [{ kind: 'policyUpdate', address, policy: p.policy, signers: [{ key: key() }] }] });
    assert.ok(txSize([update], [owner]) <= 1232, `update policy ${i}: ${txSize([update], [owner])} bytes`);
  }
});

test('policies: packing keeps every rule, in order, and is deterministic', () => {
  const ctx = worst();
  const a = compile('meteora-dlmm', ctx), b = compile('meteora-dlmm', ctx);
  const names = a.policies.flatMap(p => p.index.map(e => e.name));
  const want = [...RULES.venues['meteora-dlmm'].instructions.map(r => r.name), ...RULES.common.map(r => r.name), ...RULES.payout.map(r => r.name)];
  assert.deepEqual(names, want);
  assert.deepEqual(a.policies.map(p => sq.encodeProgramInteraction(p.policy).toString('hex')),
    b.policies.map(p => sq.encodeProgramInteraction(p.policy).toString('hex')));
});

test('policies: no payout rule without payout accounts', () => {
  const ctx = { ...worst(), payouts: [] };
  const names = compile('meteora-dlmm', ctx).policies.flatMap(p => p.index.map(e => e.name));
  assert.ok(!names.some(n => /pay out/.test(n)));
});

test('policies: a venue without rules, a role without accounts, are refused', () => {
  assert.throws(() => compile('nowhere', worst()), /no vault rules/);
  assert.throws(() => compile('meteora-dlmm', { ...worst(), pools: [] }), /approved pool/);
});

test('onboarding: every transaction fits, the bot leaves, the threshold is set last', () => {
  const creator = Keypair.generate(), owners = [key(), key(), key()];
  const compiled = compile('meteora-dlmm', worst());
  const p = connectPlan({ creator: creator.publicKey, treasury: key(), seed: 672_335n, owners, threshold: 2, policyKey: key(), compiled });
  assert.equal(p.txs.length, 1 + compiled.policies.length + 1);
  for (const ixs of p.txs) assert.ok(txSize(ixs, [creator]) <= 1232);
  assert.ok(p.vault.equals(sq.vaultPda(p.settings)));
  assert.deepEqual(p.policies.map(String), compiled.policies.map((_, i) => String(sq.policyPda(p.settings, i + 1))));
  // threshold 1: the bot only leaves; the program keeps the threshold the vault was created with
  const one = connectPlan({ creator: creator.publicKey, treasury: key(), seed: 1n, owners, threshold: 1, policyKey: key(), compiled });
  const leaveOnly = sq.settingsSyncIx({ settings: one.settings, signers: [creator.publicKey], rentPayer: creator.publicKey,
    actions: [{ kind: 'removeSigner', key: creator.publicKey }], extra: [] });
  assert.deepEqual(one.txs.at(-1)[0].data, leaveOnly.data);
  assert.ok(p.txs.at(-1)[0].data.length > leaveOnly.data.length);
});

test('lookup table addresses: each once, all of them', () => {
  const vault = key(), mint = key(), shared = key(), pool = key();
  const venue = { shared: () => [shared, shared] };
  const pools = [{ accounts: [pool, shared], mints: [mint, mint] }, { accounts: [pool], mints: [mint] }];
  const out = staticAccounts({ vault, policies: [key()], venue, pools }).map(String);
  assert.equal(new Set(out).size, out.length);
  for (const k of [vault, shared, pool, sq.PROGRAM_ID]) assert.ok(out.includes(String(k)));
});

test('onboarding: the bot\'s keys cannot be owners; a threshold must be reachable', () => {
  const creator = key(), policyKey = key(), compiled = compile('meteora-dlmm', worst());
  const plan = (o) => connectPlan({ creator, treasury: key(), seed: 1n, owners: [key()], threshold: 1, policyKey, compiled, ...o });
  assert.throws(() => plan({ owners: [creator] }), /cannot be an owner/);
  assert.throws(() => plan({ owners: [policyKey] }), /cannot be an owner/);
  assert.throws(() => plan({ owners: [] }), /at least one owner/);
  assert.throws(() => plan({ threshold: 2 }), /threshold/);
  assert.throws(() => plan({ threshold: 0 }), /threshold/);
});

// A Settings account with these owners, encoded as the program stores it.
function settingsData({ signers, threshold, authority = PublicKey.default, timeLock = 0 }) {
  const disc = createHash('sha256').update('account:Settings').digest().subarray(0, 8);
  const parts = [disc, Buffer.alloc(16), authority.toBuffer()];
  const n = Buffer.alloc(2 + 4 + 8 + 8); n.writeUInt16LE(threshold, 0); n.writeUInt32LE(timeLock, 2); parts.push(n);
  parts.push(Buffer.from([1]), Buffer.alloc(32), Buffer.alloc(8), Buffer.from([255]));
  const len = Buffer.alloc(4); len.writeUInt32LE(signers.length); parts.push(len);
  for (const s of signers) parts.push(s.toBuffer(), Buffer.from([7]));
  parts.push(Buffer.from([0]), Buffer.from([1]), Buffer.alloc(8), Buffer.from([0]));
  return Buffer.concat(parts);
}

test('onboarding check: catches the bot as owner, a wrong threshold, an authority, a time lock', () => {
  const a = key(), b = key(), bot = key();
  const ok = { owners: [a, b], threshold: 1, botKeys: [bot] };
  assert.deepEqual(connectProblems(settingsData({ signers: [a, b], threshold: 1 }), ok), []);
  assert.match(connectProblems(settingsData({ signers: [a, b, bot], threshold: 1 }), ok).join(), /is an owner/);
  assert.match(connectProblems(settingsData({ signers: [a, b], threshold: 2 }), ok).join(), /threshold is 2/);
  assert.match(connectProblems(settingsData({ signers: [a, b], threshold: 1, authority: bot }), ok).join(), /settings authority/);
  assert.match(connectProblems(settingsData({ signers: [a, b], threshold: 1, timeLock: 60 }), ok).join(), /time lock/);
  assert.match(connectProblems(settingsData({ signers: [a], threshold: 1 }), ok).join(), /owners are/);
});

test('lookup table: created once, extended only with what it lacks, every transaction fits', () => {
  const payer = Keypair.generate(), addresses = Array.from({ length: 70 }, key);
  const first = lookupPlan({ payer: payer.publicKey, recentSlot: 1, addresses });
  for (const ixs of first.txs) assert.ok(txSize(ixs, [payer]) <= 1232);
  const again = lookupPlan({ payer: payer.publicKey, recentSlot: 2, table: first.table, have: addresses.slice(0, 50), addresses });
  assert.ok(again.table.equals(first.table));
  assert.equal(again.txs.length, 1);
  assert.equal(lookupPlan({ payer: payer.publicKey, recentSlot: 2, table: first.table, have: addresses, addresses }).txs.length, 0);
  assert.throws(() => lookupPlan({ payer: payer.publicKey, recentSlot: 1, addresses: Array.from({ length: 257 }, key) }), /256/);
  assert.ok(lookupPlan({ payer: payer.publicKey, recentSlot: 1, addresses: Array.from({ length: 256 }, key) }).txs.length);
});

test('a key wallet signs its own transactions, with only the extra signers it needs', async () => {
  const payer = Keypair.generate(), position = Keypair.generate(), bystander = Keypair.generate();
  const acting = await actingAccount({ connection: null, payer, venue: 'meteora-dlmm', env: {} });
  assert.equal(acting.mode, 'key');
  assert.ok(acting.owner.equals(payer.publicKey));
  const ix = new TransactionInstruction({ programId: DLMM, data: Buffer.alloc(1), keys: [
    { pubkey: position.publicKey, isSigner: true, isWritable: true }, { pubkey: bystander.publicKey, isSigner: false, isWritable: true }] });
  const tx = acting.transaction([ix], BLOCKHASH, [position, bystander]);
  assert.deepEqual(tx.signatures.map(s => String(s.publicKey)), [payer, position].map(k => String(k.publicKey)));
  assert.ok(tx.verifySignatures());
});

test('config: unset is no vault; half set refuses', () => {
  assert.equal(vaultConfig({}), null);
  assert.throws(() => vaultConfig({ LPBOT_VAULT_SETTINGS: String(key()) }), /LPBOT_VAULT_POLICIES/);
  const c = vaultConfig({ LPBOT_VAULT_SETTINGS: String(key()), LPBOT_VAULT_POLICIES: `${key()}, ${key()}`,
    LPBOT_VAULT_POLICY_KEY: '/k', LPBOT_VAULT_POOLS: String(key()), LPBOT_VAULT_PAYOUTS: '' });
  assert.equal(c.policies.length, 2);
  assert.deepEqual(c.payouts, []);
  assert.equal(c.lookup, null);
});

// --- the planner -------------------------------------------------------------

function fixture() {
  const vault = key(), payer = key(), pool = key();
  const compiled = compile('meteora-dlmm', { vault, payer, pools: [pool], payouts: [key()] });
  const rule = (name) => RULES.venues['meteora-dlmm'].instructions.find(r => r.name === name);
  const dlmm = (name, at = {}) => {
    const r = rule(name);
    const keys = Array.from({ length: 16 }, (_, i) => ({ pubkey: at[i] ?? key(), isSigner: false, isWritable: true }));
    for (const p of r.pins) if (p.role === 'pool') keys[p.index].pubkey = at[p.index] ?? pool;
    for (const p of r.pins) if (p.role === 'none') keys[p.index].pubkey = DLMM;
    for (const p of r.pins) if (p.role === 'vaultOrPayer') keys[p.index].pubkey = vault;
    keys[15] = { pubkey: vault, isSigner: true, isWritable: false };
    return new TransactionInstruction({ programId: DLMM, keys, data: Buffer.concat([Buffer.from(r.discriminator, 'hex'), Buffer.alloc(8)]) });
  };
  const ata = () => new TransactionInstruction({ programId: new PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'),
    keys: [{ pubkey: vault, isSigner: true, isWritable: true }, { pubkey: key(), isSigner: false, isWritable: true },
      { pubkey: vault, isSigner: false, isWritable: false }], data: Buffer.from([1]) });
  const plain = () => SystemProgram.transfer({ fromPubkey: payer, toPubkey: key(), lamports: 1 });
  const budget = () => ComputeBudgetProgram.setComputeUnitLimit({ units: 400_000 });
  return { vault, payer, pool, compiled, dlmm, ata, plain, budget };
}

// Rules of one venue with `n` instructions and no pins: u8 discriminators 0..n-1.
const tiny = (n, discriminator = (i) => ({ type: 'u8', value: i })) => ({ common: [], payout: [], venues: { x: {
  program: DLMM.toBase58(), instructions: Array.from({ length: n }, (_, i) => ({ name: `r${i}`, discriminator: discriminator(i), pins: [] })) } } });
const encoded = (constraints) => sq.encodeProgramInteraction({ accountIndex: 0, constraints }).length;

test('policies: packing fills each policy up to the byte limit exactly, and to 20 rules', () => {
  const ctx = worst();
  const all = compile('meteora-dlmm', ctx, RULES, 1e6).policies[0].policy.constraints;
  const largest = Math.max(...all.map(c => encoded([c])));
  const fits = all.map((_, k) => k + 1).filter(k => k < all.length && encoded(all.slice(0, k)) >= largest);
  assert.ok(fits.length >= 3);
  for (const k of fits) {
    const limit = encoded(all.slice(0, k));
    const { policies } = compile('meteora-dlmm', ctx, RULES, limit);
    assert.equal(policies[0].policy.constraints.length, k, `limit ${limit}`);
    assert.ok(policies.every(p => encoded(p.policy.constraints) <= limit));
  }
  assert.ok(compile('meteora-dlmm', ctx, RULES, largest));
  assert.throws(() => compile('meteora-dlmm', ctx, RULES, largest - 1), /one rule alone is over/);
  assert.deepEqual(compile('x', ctx, tiny(41), 1e6).policies.map(p => p.policy.constraints.length), [20, 20, 1]);
});

test('policies: a discriminator must be 8 bytes of lower-case hex', () => {
  for (const bad of ['abcd', '0011223344556677ff', 'ABCDEF0123456789', 'zz11223344556677']) {
    assert.throws(() => compile('x', worst(), tiny(1, () => bad)), /not 8 bytes of hex/, bad);
  }
  assert.equal(compile('x', worst(), tiny(1, () => 'abcdef0123456789')).policies.length, 1);
});

test('needsVault: the vault signing, nothing else', () => {
  const vault = key(), other = key();
  const ix = (keys) => new TransactionInstruction({ programId: DLMM, data: Buffer.alloc(1), keys });
  assert.ok(needsVault(ix([{ pubkey: vault, isSigner: true, isWritable: false }]), vault));
  assert.ok(!needsVault(ix([{ pubkey: vault, isSigner: false, isWritable: true }, { pubkey: other, isSigner: true, isWritable: true }]), vault));
});

test('match: an approved instruction is found; a wrong pool, an unknown one, a short one are refused', () => {
  const f = fixture();
  const m = match(f.dlmm('claim_fee2'), f.compiled);
  assert.equal(f.compiled.policies[m.policy].index[m.constraint].name, 'claim_fee2');
  assert.throws(() => match(f.dlmm('claim_fee2', { 0: key() }), f.compiled), NotAllowed);
  const unknown = new TransactionInstruction({ programId: DLMM, keys: [{ pubkey: f.vault, isSigner: true, isWritable: false }], data: Buffer.alloc(8, 7) });
  assert.throws(() => match(unknown, f.compiled), /not in the meteora-dlmm vault policy/);
  const short = f.dlmm('claim_fee2'); short.keys = short.keys.slice(0, 3);
  assert.throws(() => match(short, f.compiled), /account 5 is missing/);
});

test('plan: order and content are kept; only vault instructions are wrapped; budgets stay outside', () => {
  const f = fixture();
  const kinds = ['claim_fee2', 'swap2', 'remove_liquidity_by_range2', 'close_position2', 'ata', 'plain', 'budget'];
  fc.assert(fc.property(fc.array(fc.constantFrom(...kinds), { maxLength: 12 }), (seq) => {
    const ixs = seq.map(k => k === 'ata' ? f.ata() : k === 'plain' ? f.plain() : k === 'budget' ? f.budget() : f.dlmm(k));
    const steps = plan(ixs, f.vault, f.compiled);
    assert.deepEqual(steps.flatMap(s => s.ixs), ixs);
    for (const s of steps) {
      if (s.policy === null) {
        assert.equal(s.ixs.length, 1);
        assert.ok(!needsVault(s.ixs[0], f.vault) || s.ixs[0].programId.equals(ComputeBudgetProgram.programId));
      } else {
        assert.equal(s.indices.length, s.ixs.length);
        s.ixs.forEach(ix => assert.ok(needsVault(ix, f.vault), 'only vault instructions are wrapped'));
        s.ixs.forEach((ix, i) => assert.ok(ix.data.subarray(0, f.compiled.policies[s.policy].index[s.indices[i]].prefix.length)
          .equals(f.compiled.policies[s.policy].index[s.indices[i]].prefix)));
        // an instruction that opens accounts is always last in its call
        s.ixs.slice(0, -1).forEach(ix => assert.ok(!ix.programId.equals(f.ata().programId)));
      }
    }
  }));
});

test('wrap: one policy call per step, every vault instruction inside one', () => {
  const f = fixture();
  const cfg = { vault: f.vault, payer: f.payer, policies: f.compiled.policies.map(() => key()), policyKey: key() };
  const ixs = [f.budget(), f.ata(), f.dlmm('swap2'), f.plain(), f.dlmm('claim_fee2'), f.dlmm('close_position2')];
  const out = wrap(ixs, cfg, f.compiled);
  const calls = out.filter(ix => ix.programId.equals(sq.PROGRAM_ID));
  assert.equal(calls.length, plan(ixs, f.vault, f.compiled).filter(s => s.policy !== null).length);
  assert.ok(out[0].programId.equals(ComputeBudgetProgram.programId));
  assert.ok(!out.some(ix => !ix.programId.equals(sq.PROGRAM_ID) && needsVault(ix, f.vault)), 'a vault instruction escaped the policy');
  // the policy key signs each call; the vault never appears as a signer
  for (const c of calls) {
    assert.ok(c.keys.some(k => k.isSigner && k.pubkey.equals(cfg.policyKey)));
    assert.ok(!c.keys.some(k => k.isSigner && k.pubkey.equals(f.vault)));
  }
});

test('codec: inner accounts are listed once, writable if any use writes, the vault never a signer', () => {
  const vault = key(), a = key(), b = key();
  const ix1 = new TransactionInstruction({ programId: DLMM, data: Buffer.from([1]), keys: [
    { pubkey: a, isSigner: false, isWritable: false }, { pubkey: vault, isSigner: true, isWritable: true }] });
  const ix2 = new TransactionInstruction({ programId: DLMM, data: Buffer.from([2]), keys: [
    { pubkey: a, isSigner: false, isWritable: true }, { pubkey: b, isSigner: true, isWritable: false }] });
  const { metas } = sq.compileInner([ix1, ix2], vault);
  assert.equal(metas.length, 4);
  assert.ok(metas.find(m => m.pubkey.equals(a)).isWritable);
  assert.ok(!metas.find(m => m.pubkey.equals(vault)).isSigner);
  assert.ok(metas.find(m => m.pubkey.equals(b)).isSigner);
  const many = new TransactionInstruction({ programId: DLMM, data: Buffer.alloc(1), keys: Array.from({ length: 256 }, () => ({ pubkey: key(), isSigner: false, isWritable: false })) });
  assert.throws(() => sq.compileInner([many], vault), /255/);
});

test('codec: every one-byte bound takes 255 and refuses 256', () => {
  const vault = key();
  const ix = (keys, data = Buffer.alloc(1)) => new TransactionInstruction({ programId: DLMM, data, keys });
  const meta = (pubkey) => ({ pubkey, isSigner: false, isWritable: false });
  const same = key();
  // 255 instructions; 255 accounts of one instruction; 254 distinct accounts + the program = 255 entries
  assert.equal(sq.compileInner(Array.from({ length: 255 }, () => ix([])), vault).metas.length, 1);
  assert.throws(() => sq.compileInner(Array.from({ length: 256 }, () => ix([])), vault), /256 instructions/);
  assert.equal(sq.compileInner([ix(Array(255).fill(meta(same)))], vault).metas.length, 2);
  assert.throws(() => sq.compileInner([ix(Array(256).fill(meta(same)))], vault), /more than 255 accounts/);
  assert.equal(sq.compileInner([ix(Array.from({ length: 254 }, () => meta(key())))], vault).metas.length, 255);
  assert.throws(() => sq.compileInner([ix(Array.from({ length: 255 }, () => meta(key())))], vault), /256 accounts/);
  assert.ok(sq.compileInner([ix([], Buffer.alloc(0xffff))], vault));
  assert.throws(() => sq.compileInner([ix([], Buffer.alloc(0x10000))], vault), /65535/);
});

test('codec: byte constraints take eq and ne only; numbers take every operator', () => {
  const c = (type, value, op) => ({ programId: DLMM, accounts: [], data: [{ offset: 0, type, value, op }] });
  const bytes = Buffer.alloc(8, 1);
  for (const op of ['eq', 'ne']) assert.ok(encoded([c('bytes', bytes, op)]));
  for (const op of ['gt', 'gte', 'lt', 'lte']) assert.throws(() => encoded([c('bytes', bytes, op)]), /eq or ne/);
  for (const op of sq.DATA_OPERATOR) assert.ok(encoded([c('u8', 1, op)]));
  assert.throws(() => encoded([c('u8', 1, 'about')]), /not one of/);
});

test('codec: a policy with no constraints, or more than 20, is refused before encoding', () => {
  assert.throws(() => sq.encodeProgramInteraction({ accountIndex: 0, constraints: [] }), /allow anything/);
  const c = compile('meteora-dlmm', worst()).policies[0].policy.constraints[0];
  assert.throws(() => sq.encodeProgramInteraction({ accountIndex: 0, constraints: Array(21).fill(c) }), /20/);
  assert.ok(sq.encodeProgramInteraction({ accountIndex: 0, constraints: Array(20).fill(c) }));
  assert.throws(() => sq.encodeProgramInteraction({ constraints: [c] }), /accountIndex/);
});

test('the vault funds its own token accounts; anyone else\'s stay as built', () => {
  const vault = key(), payer = key(), other = key(), mint = key();
  const ATA = new PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
  const create = (funder, wallet, data = Buffer.from([1])) => new TransactionInstruction({ programId: ATA, data, keys: [
    { pubkey: funder, isSigner: true, isWritable: true }, { pubkey: key(), isSigner: false, isWritable: true },
    { pubkey: wallet, isSigner: false, isWritable: false }, { pubkey: mint, isSigner: false, isWritable: false }] });
  const [mine, plain, theirs, byOther] = vaultFundsItsAccounts(
    [create(payer, vault), create(payer, vault, Buffer.alloc(0)), create(payer, other), create(other, vault)], vault, payer);
  for (const ix of [mine, plain]) {
    assert.ok(ix.keys[0].pubkey.equals(vault) && ix.keys[0].isSigner);
    assert.deepEqual([...ix.data], [1], 'always the idempotent create');
  }
  assert.ok(theirs.keys[0].pubkey.equals(payer), 'a payout wallet\'s account stays funded by the payer');
  assert.ok(byOther.keys[0].pubkey.equals(other));
});
