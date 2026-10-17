// The vault's rules, policies and planner, without a chain: what the fork
// tests (test_vault_fork.mjs, test_vault_dlmm_fork.mjs) cannot cover by
// example — every size limit, every refusal path, every ordering.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'module';
import fc from 'fast-check';
import * as sq from '../chains/solana/vault/squads.mjs';
import { RULES, compile, match, plan, needsVault, NotAllowed, POLICY_BYTES, MIN_POLICY_BYTES, policyBytesOf, instructionsOf, poolPda } from '../chains/solana/vault/policy.mjs';
import { vaultConfig, wrap, vaultFundsItsAccounts, withVaultBudget, VAULT_CALL_UNITS } from '../chains/solana/vault/account.mjs';
import { connectProblems, lookupPlan, otherSigners, ownerSetupPlan, packSteps, setupDone, staticAccounts } from '../chains/solana/vault/admin.mjs';
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

const VENUES = Object.keys(RULES.venues);

test('rules: every Anchor discriminator is the hash of its instruction name', () => {
  for (const venue of VENUES) {
    for (const r of instructionsOf(venue)) {
      const want = createHash('sha256').update(`global:${r.name}`).digest().subarray(0, 8).toString('hex');
      assert.equal(r.discriminator, want, `${venue} ${r.name}`);
    }
  }
});

test('rules: every pin names a known role and no account twice', () => {
  const roles = new Set(['pool', 'pda', 'vault', 'vaultToken', 'vaultWsol', 'vaultOrPayer', 'payout', 'none']);
  const all = [...RULES.common, ...RULES.payout, ...VENUES.flatMap(v => instructionsOf(v))];
  for (const r of all) {
    assert.ok(r.pins.length > 0, `${r.name} pins nothing: it could send money anywhere`);
    for (const p of r.pins) assert.ok(roles.has(p.role), `${r.name}: role ${p.role}`);
    assert.equal(new Set(r.pins.map(p => p.index)).size, r.pins.length, r.name);
  }
});

test('policies: each packed policy of every venue fits one create and one update transaction', () => {
  const ctx = worst(), payer = Keypair.generate(), owner = Keypair.generate();
  const settings = key();
  assert.ok(compile('meteora-dlmm', ctx).policies.length >= 2, 'the DLMM rules do not fit one policy; packing must have split them');
  for (const [i, p] of VENUES.flatMap(v => compile(v, ctx).policies).entries()) {
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
  for (const venue of VENUES) {
    const a = compile(venue, ctx), b = compile(venue, ctx);
    const names = a.policies.flatMap(p => p.index.map(e => e.name));
    const want = [...instructionsOf(venue).map(r => r.name), ...RULES.common.map(r => r.name), ...RULES.payout.map(r => r.name)];
    assert.deepEqual(names, want, venue);
    assert.deepEqual(a.policies.map(p => sq.encodeProgramInteraction(p.policy).toString('hex')),
      b.policies.map(p => sq.encodeProgramInteraction(p.policy).toString('hex')));
    assert.ok(a.policies.every(p => p.policy.constraints.length <= 20 && sq.encodeProgramInteraction(p.policy).length <= POLICY_BYTES), venue);
  }
});

// --- a vault's own policy size (sql/047): a card-signing owner's packs smaller -----------------------

test('policy bytes: the default, a vault\'s own, and nothing outside the bounds', () => {
  assert.equal(policyBytesOf({}), POLICY_BYTES);
  assert.equal(policyBytesOf({ policyBytes: null }), POLICY_BYTES);
  assert.equal(policyBytesOf({ policyBytes: 600 }), 600);
  for (const bad of [MIN_POLICY_BYTES - 1, POLICY_BYTES + 1, 600.5, '600', NaN, 0, -1]) {
    assert.throws(() => policyBytesOf({ policyBytes: bad }), /policy bytes/, String(bad));
    assert.throws(() => compile('raydium-clmm', { ...worst(), policyBytes: bad }), /policy bytes/, String(bad));
  }
});

test('policy bytes: any size packs the same rules, in order, each policy under it; only the split changes', () => {
  fc.assert(fc.property(fc.constantFrom(...VENUES), fc.integer({ min: MIN_POLICY_BYTES, max: POLICY_BYTES }), (venue, size) => {
    const ctx = worst();
    const a = compile(venue, ctx);
    let b;
    try { b = compile(venue, { ...ctx, policyBytes: size }); } catch (e) {
      // a size under one single rule is refused, never packed past it
      assert.match(e.message, /one rule alone is over/); return;
    }
    const flat = (c) => c.policies.flatMap(p => p.policy.constraints.map(x => sq.encodeProgramInteraction({ accountIndex: 0, constraints: [x] }).toString('hex')));
    assert.deepEqual(flat(b), flat(a), 'the same constraints, in the same order');
    assert.ok(b.policies.every(p => sq.encodeProgramInteraction(p.policy).length <= size), 'each policy under the size');
    assert.ok(b.policies.length >= a.policies.length, 'a smaller size never packs into fewer policies');
    assert.deepEqual(compile(venue, { ...ctx, policyBytes: POLICY_BYTES }).policies.map(p => sq.encodeProgramInteraction(p.policy).toString('hex')),
      a.policies.map(p => sq.encodeProgramInteraction(p.policy).toString('hex')), 'the default size is the default');
  }), { numRuns: 60 });
});

// tang0, 2026-10-17: a Tangem card signs at most 930 bytes; at 760 its Raydium update was 1,043-1,050.
test('policy bytes 600: every update of a two-pool Raydium vault fits a Tangem card (930 bytes)', () => {
  const owner = Keypair.generate(), settings = key();
  for (let run = 0; run < 20; run++) {
    const ctx = { vault: key(), payer: key(), pools: [key(), key()], payouts: [key()] };
    const sizes = (c) => c.policies.map((p, i) => {
      const address = sq.policyPda(settings, i + 1);
      const ix = sq.settingsSyncIx({ settings, signers: [owner.publicKey], rentPayer: owner.publicKey, extra: [address],
        actions: [{ kind: 'policyUpdate', address, policy: p.policy, signers: [{ key: key() }] }] });
      const tx = new Transaction().add(ix); tx.feePayer = owner.publicKey; tx.recentBlockhash = BLOCKHASH;
      return tx.serializeMessage().length;
    });
    assert.ok(Math.max(...sizes(compile('raydium-clmm', ctx))) > 930, 'the default does not fit a card (why sql/047 exists)');
    const small = compile('raydium-clmm', { ...ctx, policyBytes: 600 });
    assert.equal(small.policies.length, 3, 'three policies: the two the vault has and one staged');
    assert.ok(Math.max(...sizes(small)) <= 930, `card: ${sizes(small)}`);
  }
});

test('policy bytes: the environment carries a vault\'s size to the signer, and nothing invalid', () => {
  const base = { LPBOT_VAULT_SETTINGS: key().toBase58(), LPBOT_VAULT_POLICIES: key().toBase58(), LPBOT_VAULT_POLICY_KEY: '/x',
    LPBOT_VAULT_POOLS: key().toBase58() };
  assert.equal(vaultConfig(base).policyBytes, undefined);
  assert.equal(vaultConfig({ ...base, LPBOT_VAULT_POLICY_BYTES: '600' }).policyBytes, 600);
  for (const bad of ['10', '900', 'abc', '600.5']) assert.throws(() => vaultConfig({ ...base, LPBOT_VAULT_POLICY_BYTES: bad }), /policy bytes/, bad);
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

test('owner set-up of every venue at once: packed transactions fit, seeds run on, each venue names its own, the owner alone signs', () => {
  const owner = Keypair.generate(), ctx = worst(), policyKey = key();
  const venues = VENUES.map(v => compile(v, ctx));
  const p = ownerSetupPlan({ owner: owner.publicKey, treasury: key(), seed: 9n, policyKey, venues });
  const n = venues.reduce((k, v) => k + v.policies.length, 0);
  assert.equal(p.steps.length, 1 + n);
  const groups = packSteps(p.steps, owner.publicKey);
  assert.deepEqual(groups.flat().map(ix => ix.data), p.steps.map(ix => ix.data), 'every step once, in order');
  assert.ok(groups.length <= p.steps.length);
  for (const ixs of groups) assert.ok(txSize(ixs, [owner]) <= 1232, `${txSize(ixs, [owner])} bytes`);
  for (const ix of p.steps) {
    assert.deepEqual([...new Set(ix.keys.filter(k => k.isSigner).map(k => String(k.pubkey)))], [String(owner.publicKey)], 'the owner signs alone');
  }
  assert.deepEqual(p.policies.map(String), Array.from({ length: n }, (_, i) => String(sq.policyPda(p.settings, i + 1))));
  assert.deepEqual(VENUES.flatMap(v => p.byVenue[v]).map(String), p.policies.map(String));
  VENUES.forEach((v, i) => assert.equal(p.byVenue[v].length, venues[i].policies.length));
  assert.ok(p.vault.equals(sq.vaultPda(p.settings)));
  assert.ok(p.settings.equals(sq.settingsPda(9n)));
  assert.throws(() => ownerSetupPlan({ owner: owner.publicKey, treasury: key(), seed: 9n, policyKey, venues: [venues[0], venues[0]] }), /listed twice/);
  assert.throws(() => ownerSetupPlan({ owner: owner.publicKey, treasury: key(), seed: 9n, policyKey, venues: [] }), /at least one venue/);
  assert.throws(() => ownerSetupPlan({ owner: policyKey, treasury: key(), seed: 9n, policyKey, venues }), /cannot own the vault/);
});

test('owner set-up: the creation names the owner alone at threshold 1; each policy names the bot\'s key; the owner pays the rent', () => {
  const owner = key(), policyKey = key(), treasury = key(), compiled = compile('meteora-dlmm', worst());
  const p = ownerSetupPlan({ owner, treasury, seed: 5n, policyKey, venues: [compiled], memo: 'm' });
  const want = sq.createSmartAccountIx({ creator: owner, treasury, seed: 5n, threshold: 1, memo: 'm', signers: [{ key: owner }] }).ix;
  assert.deepEqual(p.steps[0].data, want.data);
  assert.deepEqual(p.steps[0].keys.map(k => String(k.pubkey)), want.keys.map(k => String(k.pubkey)));
  compiled.policies.forEach((pol, i) => {
    const ix = sq.settingsSyncIx({ settings: p.settings, signers: [owner], rentPayer: owner, extra: [p.policies[i]],
      actions: [{ kind: 'policyCreate', seed: i + 1, policy: pol.policy, signers: [{ key: policyKey }] }] });
    assert.deepEqual(p.steps[i + 1].data, ix.data, `policy ${i + 1}`);
    assert.equal(String(p.steps[i + 1].keys[1].pubkey), String(owner), 'rent from the owner');
  });
});

test('owner set-up with a set-up key: the creation names the owner and the key; the key signs and pays each policy, then takes itself off', () => {
  const owner = key(), policyKey = key(), treasury = key(), setupKey = key(), compiled = compile('meteora-dlmm', worst());
  const p = ownerSetupPlan({ owner, treasury, seed: 5n, policyKey, venues: [compiled], memo: 'm', setupKey });
  const want = sq.createSmartAccountIx({ creator: owner, treasury, seed: 5n, threshold: 1, memo: 'm', signers: [{ key: owner }, { key: setupKey }] }).ix;
  assert.deepEqual(p.steps[0].data, want.data);
  assert.deepEqual(p.steps[0].keys.filter(k => k.isSigner).map(k => String(k.pubkey)), [String(owner)], 'the owner alone signs the creation');
  compiled.policies.forEach((pol, i) => {
    const ix = sq.settingsSyncIx({ settings: p.settings, signers: [setupKey], rentPayer: setupKey, extra: [p.policies[i]],
      actions: [{ kind: 'policyCreate', seed: i + 1, policy: pol.policy, signers: [{ key: policyKey }] }] });
    assert.deepEqual(p.steps[i + 1].data, ix.data, `policy ${i + 1}`);
    assert.deepEqual([...new Set(p.steps[i + 1].keys.filter(k => k.isSigner).map(k => String(k.pubkey)))], [String(setupKey)], 'the key alone signs');
    assert.equal(String(p.steps[i + 1].keys[1].pubkey), String(setupKey), 'rent from the key');
  });
  const release = sq.settingsSyncIx({ settings: p.settings, signers: [setupKey], rentPayer: setupKey, actions: [{ kind: 'removeSigner', key: setupKey }] });
  assert.deepEqual(p.release.data, release.data);
  assert.deepEqual(p.release.keys.map(k => String(k.pubkey)), release.keys.map(k => String(k.pubkey)));
  assert.equal(ownerSetupPlan({ owner, treasury, seed: 5n, policyKey, venues: [compiled] }).release, null, 'no key, nothing to take off');
  for (const k of [owner, policyKey]) {
    assert.throws(() => ownerSetupPlan({ owner, treasury, seed: 5n, policyKey, venues: [compiled], setupKey: k }), /the set-up key is the owner's or the bot's key/);
  }
});

test('packSteps: order kept, a unit stays in one transaction, a step too big alone is refused', () => {
  const owner = key(), big = (n) => new TransactionInstruction({ programId: sq.PROGRAM_ID, keys: [], data: Buffer.alloc(n, 1) });
  const g = packSteps([big(500), big(500), [big(300), big(300)], big(100)], owner);
  assert.deepEqual(g.map(x => x.map(ix => ix.data.length)), [[500, 500], [300, 300, 100]]);
  const tight = packSteps([big(600), [big(400), big(400)]], owner);
  assert.deepEqual(tight.map(x => x.map(ix => ix.data.length)), [[600], [400, 400]], 'the unit is not split to fill the first');
  assert.throws(() => packSteps([[big(700), big(700)]], owner), /over a transaction's size/);
  assert.throws(() => packSteps([big(1300)], owner), /over a transaction's size/);
  assert.deepEqual(packSteps([[], big(10)], owner).map(x => x.length), [1], 'an empty unit is nothing');
  // the limit is exact: the largest data that fits, and one byte more
  const fit = (n) => { try { packSteps([big(n)], owner); return true; } catch { return false; } };
  let lo = 1, hi = 1300;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (fit(m)) lo = m; else hi = m - 1; }
  assert.equal(txSize([big(lo)], [Keypair.generate()]), 1232);
  assert.ok(!fit(lo + 1));
});

test('setupDone: what of an owner\'s set-up landed; a seed another account took is null', () => {
  const owner = key();
  assert.deepEqual([...setupDone(null, [false, false], { owner })], []);
  assert.throws(() => setupDone(null, [true, false], { owner }), /policies exist for a vault that does not/);
  const mine = settingsData({ signers: [owner], threshold: 1 });
  assert.deepEqual([...setupDone(mine, [false, false], { owner })], [0]);
  assert.deepEqual([...setupDone(mine, [true, false], { owner })].sort(), [0, 1]);
  assert.deepEqual([...setupDone(mine, [true, true], { owner })].sort(), [0, 1, 2]);
  assert.equal(setupDone(settingsData({ signers: [key()], threshold: 1 }), [false], { owner }), null, 'another owner');
  assert.equal(setupDone(settingsData({ signers: [owner, key()], threshold: 1 }), [false], { owner }), null, 'a second owner');
  // a set-up key this request made beside the owner: still the owner's vault; a stranger's, or the owner gone, is not
  const k1 = key(), k2 = key();
  assert.deepEqual([...setupDone(settingsData({ signers: [owner, k1], threshold: 1 }), [true, false], { owner, known: [k2, k1] })].sort(), [0, 1]);
  assert.equal(setupDone(settingsData({ signers: [owner, k1, key()], threshold: 1 }), [false], { owner, known: [k1] }), null, 'a stranger beside the key');
  assert.equal(setupDone(settingsData({ signers: [k1], threshold: 1 }), [false], { owner, known: [k1] }), null, 'the key without the owner');
  // a policy beyond the plan's (made while another signer was on the account): someone else's
  assert.deepEqual([...setupDone(settingsData({ signers: [owner], threshold: 1, policySeed: 2n }), [true, true], { owner })].sort(), [0, 1, 2]);
  assert.equal(setupDone(settingsData({ signers: [owner], threshold: 1, policySeed: 3n }), [true, true], { owner }), null, 'a third policy of two');
  assert.deepEqual(otherSigners(settingsData({ signers: [owner, k1, k2], threshold: 1 }), owner), [String(k1), String(k2)]);
  assert.deepEqual(otherSigners(settingsData({ signers: [owner], threshold: 1 }), owner), []);
  assert.deepEqual(otherSigners(null, owner), []);
});

test('lookup table addresses: each once, all of them, every venue\'s', () => {
  const vault = key(), mint = key(), shared = key(), other = key(), pool = key();
  const venues = [{ shared: () => [shared, shared] }, { shared: () => [other] }];
  const pools = [{ accounts: [pool, shared], mints: [mint, mint] }, { accounts: [pool], mints: [mint] }];
  const out = staticAccounts({ vault, policies: [key()], venues, pools }).map(String);
  assert.equal(new Set(out).size, out.length);
  for (const k of [vault, shared, other, pool, sq.PROGRAM_ID]) assert.ok(out.includes(String(k)));
});

// A Settings account with these owners, encoded as the program stores it.
function settingsData({ signers, threshold, authority = PublicKey.default, timeLock = 0, policySeed = 0n }) {
  const disc = createHash('sha256').update('account:Settings').digest().subarray(0, 8);
  const parts = [disc, Buffer.alloc(16), authority.toBuffer()];
  const n = Buffer.alloc(2 + 4 + 8 + 8); n.writeUInt16LE(threshold, 0); n.writeUInt32LE(timeLock, 2); parts.push(n);
  parts.push(Buffer.from([1]), Buffer.alloc(32), Buffer.alloc(8), Buffer.from([255]));
  const len = Buffer.alloc(4); len.writeUInt32LE(signers.length); parts.push(len);
  for (const s of signers) parts.push(s.toBuffer(), Buffer.from([7]));
  const seed = Buffer.alloc(8); seed.writeBigUInt64LE(BigInt(policySeed));
  parts.push(Buffer.from([0]), Buffer.from([1]), seed, Buffer.from([0]));
  return Buffer.concat(parts);
}

test('onboarding check: catches the bot as owner, a wrong threshold, an authority, a time lock', () => {
  const a = key(), b = key(), bot = key();
  const ok = { owners: [a, b], threshold: 1, botKeys: [bot] };
  assert.deepEqual(connectProblems(settingsData({ signers: [a, b], threshold: 1 }), ok), []);
  assert.match(connectProblems(settingsData({ signers: [a, b, bot], threshold: 1 }), ok).join(), /is an owner/);
  assert.match(connectProblems(settingsData({ signers: [a, b], threshold: 2 }), ok).join(), /threshold is 2/);
  // a new vault: no policy beyond the bot's
  assert.deepEqual(connectProblems(settingsData({ signers: [a, b], threshold: 1, policySeed: 3n }), { ...ok, policies: 3 }), []);
  assert.match(connectProblems(settingsData({ signers: [a, b], threshold: 1, policySeed: 4n }), { ...ok, policies: 3 }).join(), /a policy at seed 4: the bot's rules make 3/);
  assert.deepEqual(connectProblems(settingsData({ signers: [a, b], threshold: 1, policySeed: 9n }), ok), [], 'a vault with venues added later: not counted');
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

test('withVaultBudget: an SDK compute limit gains the vault cost of each policy call (2026-10-11 swap ran out)', () => {
  const limit = (u) => ComputeBudgetProgram.setComputeUnitLimit({ units: u });
  const price = ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 5000 });
  const unitsOf = (ix) => Buffer.from(ix.data).readUInt32LE(1);
  const other = new TransactionInstruction({ programId: DLMM, data: Buffer.from([2, 0, 0, 0, 0]), keys: [] });
  const [l, p, o] = withVaultBudget([limit(100_000), price, other], 2);
  assert.equal(unitsOf(l), 100_000 + 2 * VAULT_CALL_UNITS);
  assert.deepEqual([p, o], [price, other], 'a price and a non-budget instruction stay as built');
  assert.equal(unitsOf(withVaultBudget([limit(1_390_000)], 1)[0]), 1_400_000, 'never above the transaction maximum');
  const none = [limit(100_000)];
  assert.equal(withVaultBudget(none, 0), none, 'no policy call: nothing changes');
  // through wrap: a swap the SDK sized at 100k leaves with room for its call
  const f = fixture();
  const cfg = { vault: f.vault, payer: f.payer, policies: f.compiled.policies.map(() => key()), policyKey: key() };
  const out = wrap([limit(100_000), f.dlmm('swap2')], cfg, f.compiled);
  assert.equal(unitsOf(out[0]), 100_000 + VAULT_CALL_UNITS);
  // instructions that need no vault are no policy call: one call, however many run beside it
  const beside = wrap([limit(100_000), price, other, f.dlmm('swap2')], cfg, f.compiled);
  assert.equal(unitsOf(beside[0]), 100_000 + VAULT_CALL_UNITS);
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

// --- forks of a venue, reward pins, derived accounts ---------------------------

const forked = (venue, extra = {}) => ({ common: [], payout: [], venues: {
  base: { program: DLMM.toBase58(), rewards: 0, instructions: [
    { name: 'a', discriminator: { type: 'u8', value: 1 }, pins: [{ index: 0, role: 'pool' }] },
    { name: 'b', discriminator: { type: 'u8', value: 2 }, pins: [{ index: 1, role: 'pool' }], rewardRecipients: { first: 18, stride: 3 } }] },
  fork: { program: key().toBase58(), like: 'base', ...venue },
  ...extra } });

test('rules: a fork takes its base\'s rules against its own program, less those it leaves out', () => {
  const rules = forked({ rewards: 2, without: ['a'] });
  assert.deepEqual(instructionsOf('fork', rules).map(r => r.name), ['b']);
  const c = compile('fork', worst(), rules);
  assert.ok(c.policies[0].index.every(e => e.programId.equals(new PublicKey(rules.venues.fork.program))));
  assert.throws(() => instructionsOf('fork', forked({ rewards: 0, without: ['nope'] })), /no rule nope to leave out/);
  assert.throws(() => instructionsOf('fork', forked({ like: 'ghost' })), /like ghost, which has no rules/);
  // the base itself keeps every rule
  assert.deepEqual(instructionsOf('base', forked({ rewards: 0 })).map(r => r.name).sort(), ['a', 'b']);
});

test('rules: one recipient pin per reward, at first + i * stride, never a pin for a reward the pools lack', () => {
  for (const rewards of [0, 1, 2, 3]) {
    const b = instructionsOf('fork', forked({ rewards })).find(r => r.name === 'b');
    assert.deepEqual(b.pins, [{ index: 1, role: 'pool' }, ...[18, 21, 24].slice(0, rewards).map(index => ({ index, role: 'vaultToken' }))]);
    assert.ok(!('rewardRecipients' in b));
  }
  for (const rewards of [-1, 4, 1.5, undefined]) assert.throws(() => instructionsOf('fork', forked({ rewards })), /rewards must be 0 to 3/, String(rewards));
  // the live rules: Raydium's pools carry one reward, Byreal's none
  const recipients = (v) => instructionsOf(v).find(r => r.name === 'decrease_liquidity_v2').pins.filter(p => p.index >= 17).map(p => p.index);
  assert.deepEqual(recipients('raydium-clmm'), [18]);
  assert.deepEqual(recipients('byreal'), []);
  assert.ok(!instructionsOf('byreal').some(r => r.name === 'swap_v2'), 'Byreal leaves out swap_v2');
});

test('a pda pin names, for each approved pool, the address its venue program derives', () => {
  const pools = [key(), key()], program = new PublicKey(RULES.venues['raydium-clmm'].program);
  const c = compile('raydium-clmm', { ...worst(), pools });
  const decrease = c.policies.flatMap(p => p.index).find(e => e.name === 'decrease_liquidity_v2');
  const bitmap = decrease.accounts.find(a => a.index === 16);
  const want = pools.map(p => PublicKey.findProgramAddressSync([Buffer.from('pool_tick_array_bitmap_extension'), p.toBuffer()], program)[0]);
  assert.deepEqual(bitmap.keys.map(String), want.map(String));
  assert.ok(poolPda(['x', 'pool'], pools[0], program).equals(PublicKey.findProgramAddressSync([Buffer.from('x'), pools[0].toBuffer()], program)[0]));
  assert.throws(() => poolPda([], pools[0], program), /needs its seeds/);
  assert.throws(() => compile('raydium-clmm', { ...worst(), pools: [] }), /approved pool/);
});
