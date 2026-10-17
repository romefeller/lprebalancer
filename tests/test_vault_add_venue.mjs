// One more venue on a vault that exists, and the WSOL pin, without a chain:
// what tests/test_vault_add_venue_fork.mjs cannot cover by example — every
// order the owner signs in, every refusal, every vault and venue.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createRequire } from 'module';
import fc from 'fast-check';
import * as sq from '../chains/solana/vault/squads.mjs';
import { RULES, compile, match, NotAllowed, vaultWsolAccount } from '../chains/solana/vault/policy.mjs';
import { approvePlan, stagedPolicies } from '../chains/solana/vault/admin.mjs';
import { AWAITS_OWNERS, PolicyMismatch, verifyPolicy } from '../chains/solana/vault/account.mjs';
import { affordsRules, ownerTxs, POLICY_ACCOUNT_BYTES, POLICY_GROWTH_BYTES, status as solStatus } from '../onboard/solana.mjs';
import { rulesRequest, stagePool, stageVenue } from '../chains/solana/vault/cli.mjs';

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, SystemProgram, Transaction } = require('@solana/web3.js');

const key = () => Keypair.generate().publicKey;
const VENUES = Object.keys(RULES.venues);
const WRAP = 'wrap SOL (transfer into a WSOL account of the vault)';
const NATIVE = new PublicKey('So11111111111111111111111111111111111111112');
const TOKEN = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
const ATA_PROGRAM = new PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');
const ataOf = (owner) => PublicKey.findProgramAddressSync([owner.toBuffer(), TOKEN.toBuffer(), NATIVE.toBuffer()], ATA_PROGRAM)[0];
const worst = (vault = key()) => ({ vault, payer: key(), pools: [key(), key(), key()], payouts: [key(), key()] });

// A transaction's size on the wire: its signatures and its message.
function txSize(ixs, feePayer) {
  const tx = new Transaction().add(...ixs);
  tx.feePayer = feePayer;
  tx.recentBlockhash = '11111111111111111111111111111111';
  const message = tx.compileMessage();
  return 1 + 64 * message.header.numRequiredSignatures + message.serialize().length;
}

// --- the WSOL pin (security review, 2026-10-12) ---------------------------------------------------

test('wrap SOL: every venue pins the destination by key to the vault\'s WSOL account, nothing looser', () => {
  fc.assert(fc.property(fc.uint8Array({ minLength: 32, maxLength: 32 }), (bytes) => {
    const vault = new PublicKey(bytes);
    assert.ok(vaultWsolAccount(vault).equals(ataOf(vault)));
    for (const venue of VENUES) {
      const wrap = compile(venue, worst(vault)).policies.flatMap(p => p.index).filter(e => e.name === WRAP);
      assert.equal(wrap.length, 1, venue);
      assert.deepEqual(wrap[0].accounts.map(a => ({ index: a.index, keys: a.keys?.map(String), data: a.data, owner: a.owner })),
        [{ index: 1, keys: [ataOf(vault).toBase58()], data: undefined, owner: undefined }], venue);
    }
  }), { numRuns: 25 });
});

test('wrap SOL: the bot\'s own check passes the vault\'s WSOL account only', () => {
  const ctx = worst(), compiled = compile('meteora-dlmm', ctx);
  const send = (to) => () => match(SystemProgram.transfer({ fromPubkey: ctx.vault, toPubkey: to, lamports: 1 }), compiled);
  const m = send(vaultWsolAccount(ctx.vault))();
  assert.equal(compiled.policies[m.policy].index[m.constraint].name, WRAP);
  fc.assert(fc.property(fc.uint8Array({ minLength: 32, maxLength: 32 }), (bytes) => {
    const to = new PublicKey(bytes);
    fc.pre(!to.equals(vaultWsolAccount(ctx.vault)));
    assert.throws(send(to), NotAllowed);
  }), { numRuns: 50 });
  assert.throws(send(ctx.vault), NotAllowed);
  assert.throws(send(ataOf(key())), NotAllowed, 'another wallet\'s WSOL account');
});

// --- one more venue on a vault that exists: the owners' approval ------------------------------------

const disc = (name) => createHash('sha256').update(`account:${name}`).digest().subarray(0, 8);
const u64le = (n) => { const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt(n)); return b; };
const signersOf = (ks) => [Buffer.from([ks.length, 0, 0, 0]), ...ks.map(k => Buffer.concat([k.toBuffer(), Buffer.from([7])]))];

// A Settings account as the program stores it, with its last policy seed.
function settingsData(policySeed) {
  return Buffer.concat([disc('Settings'), Buffer.alloc(16), Buffer.alloc(32), Buffer.from([1, 0]), Buffer.alloc(4), u64le(0), u64le(0),
    Buffer.from([0]), u64le(0), Buffer.from([255]), ...signersOf([key()]), Buffer.from([0]),
    policySeed == null ? Buffer.from([0]) : Buffer.concat([Buffer.from([1]), u64le(policySeed)])]);
}
// A Policy account of `settings` at `seed`, signed by `signers`, holding `policy`.
function policyAccount(settings, seed, signers, policy) {
  return { owner: sq.PROGRAM_ID, data: Buffer.concat([disc('Policy'), settings.toBuffer(), u64le(seed), Buffer.from([255]), u64le(0), u64le(0),
    ...signersOf(signers), Buffer.from([1, 0]), Buffer.alloc(4), Buffer.from([3]), sq.encodeProgramInteraction(policy), Buffer.alloc(64)]) };
}
const ctxOf = (vault) => ({ vault, payer: key(), pools: [key()], payouts: [key()] });

// A vault with `have` venues' policies on chain at seeds 1.., and `staged` venues' not created yet.
function vaultWith(have, staged) {
  const settings = key(), vault = sq.vaultPda(settings), signer = key(), owner = key();
  let seed = 0n;
  const venues = [];
  for (const venue of have) {
    const compiled = compile(venue, ctxOf(vault));
    const seeds = compiled.policies.map(() => ++seed);
    const addresses = seeds.map(s => sq.policyPda(settings, s));
    venues.push({ venue, compiled, addresses, infos: compiled.policies.map((p, i) => policyAccount(settings, seeds[i], [signer], p.policy)) });
  }
  const last = seed;
  for (const venue of staged) {
    const compiled = compile(venue, ctxOf(vault));
    const addresses = stagedPolicies(settings, last, compiled.policies.length, Number(seed - last));
    seed += BigInt(addresses.length);
    venues.push({ venue, compiled, addresses, infos: addresses.map(() => null) });
  }
  return { settings, vault, signer, owner, venues, settingsData: settingsData(last) };
}

test('approve: staged policies created at the next seeds, every existing one updated, in seed order', () => {
  const v = vaultWith(['meteora-dlmm'], ['raydium-clmm']);
  const steps = approvePlan({ ...v });
  const nM = v.venues[0].addresses.length, nR = v.venues[1].addresses.length;
  assert.deepEqual(steps.map(s => s.kind), [...Array(nM).fill('update'), ...Array(nR).fill('create')]);
  assert.deepEqual(steps.map(s => s.seed), Array.from({ length: nM + nR }, (_, i) => BigInt(i + 1)));
  assert.deepEqual(steps.map(s => String(s.address)), [...v.venues[0].addresses, ...v.venues[1].addresses].map(String));
  assert.deepEqual(steps.map(s => s.venue), [...Array(nM).fill('meteora-dlmm'), ...Array(nR).fill('raydium-clmm')]);
  assert.ok(steps.filter(s => s.kind === 'update').every(s => s.current), 'the policies already hold these rules');
  for (const s of steps) {
    assert.equal(s.ixs.length, 1);
    const want = sq.settingsSyncIx({ settings: v.settings, signers: [v.owner], rentPayer: v.owner, extra: [s.address], actions: [s.action] });
    assert.deepEqual(s.ixs[0].keys, want.keys);
    assert.deepEqual(s.ixs[0].data, want.data);
    assert.deepEqual(s.action.signers.map(x => String(x.key)), [String(v.signer)]);
    if (s.kind === 'create') assert.equal(s.action.seed, s.seed);
    else assert.ok(s.action.address.equals(s.address));
    assert.ok(txSize(s.ixs, v.owner) <= 1232, 'the owner\'s wallet pays and signs one transaction per policy');
  }
  const create = steps.find(s => s.kind === 'create');
  assert.equal(create.action.kind, 'policyCreate');
  assert.equal(create.action.policy, v.venues[1].compiled.policies[0].policy);
  assert.equal(steps[0].action.kind, 'policyUpdate');
  assert.equal(steps[0].action.policy, v.venues[0].compiled.policies[0].policy);
});

test('approve: seed order whatever order the venues come in', () => {
  const v = vaultWith(['meteora-dlmm', 'orca'], ['raydium-clmm']);
  const want = approvePlan({ ...v }).map(s => s.seed);
  assert.deepEqual(want, [...want].sort((a, b) => (a < b ? -1 : 1)));
  for (const venues of [[v.venues[2], v.venues[0], v.venues[1]], [v.venues[1], v.venues[2], v.venues[0]], [v.venues[2], v.venues[1], v.venues[0]]]) {
    assert.deepEqual(approvePlan({ ...v, venues }).map(s => s.seed), want);
  }
});

test('stage: a venue and its pools, or nothing is read', async () => {
  const env = { LPBOT_VAULT_SETTINGS: key().toBase58(), LPBOT_VAULT_POLICIES: key().toBase58(), LPBOT_VAULT_POLICY_KEY: '/nowhere',
    LPBOT_VAULT_POOLS: key().toBase58(), LPBOT_VAULT_VENUE: 'meteora-dlmm' };
  const quiet = { connection: {}, env, payer: { publicKey: key() }, log: () => {} };
  await assert.rejects(stageVenue({ pools: key().toBase58() }, quiet), /add-venue needs --venue and --pools/);
  await assert.rejects(stageVenue({ venue: 'raydium-clmm' }, quiet), /add-venue needs --venue and --pools/);
  await assert.rejects(stageVenue({ venue: 'meteora-dlmm', pools: key().toBase58() }, quiet), /has it already/);
  await assert.rejects(stageVenue({ venue: 'raydium-clmm', pools: key().toBase58() }, { ...quiet, env: {} }), /LPBOT_VAULT_SETTINGS is not set/);
});

// --- one more pool on a venue the vault has (2026-10-17: tang0 into 3ucNos) -------------------------

test('add-pool: refused before anything is read', async () => {
  const held = key();
  const env = { LPBOT_VAULT_SETTINGS: key().toBase58(), LPBOT_VAULT_POLICIES: key().toBase58(), LPBOT_VAULT_POLICY_KEY: '/nowhere',
    LPBOT_VAULT_POOLS: held.toBase58(), LPBOT_VAULT_VENUE: 'raydium-clmm' };
  const quiet = { connection: {}, env, payer: { publicKey: key() }, log: () => {} };
  await assert.rejects(stagePool({ pools: key().toBase58() }, quiet), /add-pool needs --venue and --pools/);
  await assert.rejects(stagePool({ venue: 'raydium-clmm' }, quiet), /add-pool needs --venue and --pools/);
  await assert.rejects(stagePool({ venue: 'orca', pools: key().toBase58() }, quiet), /not the venue the environment names/);
  await assert.rejects(stagePool({ venue: 'raydium-clmm', pools: held.toBase58() }, quiet), /has every one of these pools already/);
  await assert.rejects(stagePool({ venue: 'raydium-clmm', pools: key().toBase58() }, { ...quiet, env: {} }), /LPBOT_VAULT_SETTINGS is not set/);
});

test('add-pool: the owners update the policies the vault has and create the staged ones, in seed order', () => {
  fc.assert(fc.property(fc.constantFrom(...VENUES), fc.integer({ min: 1, max: 2 }), fc.integer({ min: 1, max: 2 }), (venue, nOld, nNew) => {
    const settings = key(), vault = sq.vaultPda(settings), signer = key(), owner = key(), payer = key(), payouts = [key()];
    const oldPools = Array.from({ length: nOld }, key), newPools = Array.from({ length: nNew }, key);
    const before = compile(venue, { vault, payer, pools: oldPools, payouts });
    const after = compile(venue, { vault, payer, pools: [...oldPools, ...newPools], payouts });
    fc.pre(after.policies.length >= before.policies.length);
    const have = before.policies.map((_, i) => sq.policyPda(settings, BigInt(i + 1)));
    const staged = stagedPolicies(settings, BigInt(have.length), after.policies.length - have.length);
    const infos = [...before.policies.map((p, i) => policyAccount(settings, BigInt(i + 1), [signer], p.policy)), ...staged.map(() => null)];
    const steps = approvePlan({ settings, settingsData: settingsData(BigInt(have.length)), owner, signer,
      venues: [{ venue, compiled: after, addresses: [...have, ...staged], infos }] });
    assert.deepEqual(steps.map(s => s.kind), [...have.map(() => 'update'), ...staged.map(() => 'create')]);
    assert.deepEqual(steps.map(s => s.seed), Array.from({ length: after.policies.length }, (_, i) => BigInt(i + 1)));
    // the old rules differ wherever the new pools are named: those updates are not current, the owners approve them
    assert.ok(steps.some(s => !s.current));
    for (const s of steps) assert.ok(txSize(s.ixs, owner) <= 1232, 'one transaction per policy');
    // after they land, the vault holds exactly the new rules: nothing more to approve
    const landed = after.policies.map((p, i) => policyAccount(settings, BigInt(i + 1), [signer], p.policy));
    assert.ok(approvePlan({ settings, settingsData: settingsData(BigInt(after.policies.length)), owner, signer,
      venues: [{ venue, compiled: after, addresses: [...have, ...staged], infos: landed }] }).every(s => s.kind === 'update' && s.current));
  }), { numRuns: 30 });
});

// tang0, 2026-10-17: the owner's card approved two policy updates; the third (a create) expired. The
// next request sends only what is not on chain yet, one transaction, not the two again.
test('approve: a policy already holding its rules is left out; nothing left to sign is said', async () => {
  const settings = key(), vault = sq.vaultPda(settings), owner = key(), payer = key(), policyKey = key();
  const pools = [key(), key()], payouts = [key()];
  const compiled = compile('raydium-clmm', { vault, payer, pools, payouts, policyBytes: 600 });
  const addresses = compiled.policies.map((_, i) => sq.policyPda(settings, BigInt(i + 1)));
  const landed = compiled.policies.map((p, i) => policyAccount(settings, BigInt(i + 1), [policyKey], p.policy));
  const conn = (infos, seed) => ({ getAccountInfo: async () => ({ data: settingsData(seed) }), getMultipleAccountsInfo: async () => infos });
  const venues = [{ venue: 'raydium-clmm', policies: addresses.map(String), pools: pools.map(String), payouts: payouts.map(String), policyBytes: 600 }];
  const n = compiled.policies.length;
  // all but the last landed: only the create is asked
  let r = await rulesRequest('approve', { connection: conn([...landed.slice(0, -1), null], BigInt(n - 1)), settings, owner, payer, policyKey, venues });
  assert.deepEqual(r.steps.map(s => `${s.kind} ${s.seed}`), [`create ${n}`]);
  assert.equal(r.groups.length, 1);
  // one update did not land either: it is asked too, in seed order
  const older = policyAccount(settings, 1n, [policyKey], compile('raydium-clmm', { vault, payer, pools: pools.slice(0, 1), payouts }).policies[0].policy);
  r = await rulesRequest('approve', { connection: conn([older, ...landed.slice(1, -1), null], BigInt(n - 1)), settings, owner, payer, policyKey, venues });
  assert.deepEqual(r.steps.map(s => `${s.kind} ${s.seed}`), ['update 1', `create ${n}`]);
  // everything landed: nothing to sign, said
  await assert.rejects(rulesRequest('approve', { connection: conn(landed, BigInt(n)), settings, owner, payer, policyKey, venues }), /nothing to approve/);
  // a revoke: every policy still names the bot, so every one is asked
  r = await rulesRequest('revoke', { connection: conn(landed, BigInt(n)), settings, owner, payer, policyKey, venues });
  assert.equal(r.steps.length, n);
  assert.ok(r.steps.every(s => s.kind === 'update'));
});

test('add-pool: tang0, raydium 8sLb + 3ucNos compiles to one more policy than the vault holds', () => {
  const settings = new PublicKey('7yw9FuFRB7mKhJsdcqyrZGq2iEnQjxpL9e8jB2ZV4CGz'), vault = sq.vaultPda(settings);
  const payer = new PublicKey('Hce75RqXeuH4mBxZhRK8iojUMkx89961RUPLHb283iJJ'), payouts = [new PublicKey('CvkYX7aRberphM2wx2Mf2f7BomxtikEqPvsM4USGYG9z')];
  const S8 = new PublicKey('8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'), S3 = new PublicKey('3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv');
  assert.equal(compile('raydium-clmm', { vault, payer, pools: [S8], payouts }).policies.length, 2);
  const both = compile('raydium-clmm', { vault, payer, pools: [S8, S3], payouts });
  assert.equal(both.policies.length, 3);
  // every rule that names a pool names both
  const named = both.policies.flatMap(p => p.index).flatMap(e => e.accounts).filter(a => a.keys?.some(k => k.equals(S8)));
  assert.ok(named.length > 0);
  assert.ok(named.every(a => a.keys.some(k => k.equals(S3))));
});

test('withdraw at /bot: a positive amount or all, refused before anything is read', async () => {
  const c = { tokens: { SOL: { native: true, decimals: 9 } } };
  for (const amount of ['0', '0.0000000001']) {
    await assert.rejects(ownerTxs(null, c, { venues: {} }, {}, { action: 'withdraw', owner: key().toBase58(), token: 'SOL', amount }),
      /amount: a number or all/, amount);
  }
});

test('approve: the list does not shift as the owner signs; each step lands at its place', () => {
  fc.assert(fc.property(fc.subarray(VENUES, { minLength: 1 }), fc.subarray(VENUES, { minLength: 1 }), (have, add) => {
    const staged = add.filter(x => !have.includes(x));
    fc.pre(staged.length > 0);
    const v = vaultWith(have, staged);
    const order = approvePlan({ ...v }).map(s => String(s.address));
    let policySeed = BigInt(v.venues.filter(x => x.infos.every(Boolean)).reduce((n, x) => n + x.addresses.length, 0));
    for (let i = 0; i < order.length; i++) {
      const steps = approvePlan({ ...v, settingsData: settingsData(policySeed) });
      assert.deepEqual(steps.map(s => String(s.address)), order);
      const s = steps[i];
      if (s.kind === 'create') {
        assert.equal(s.seed, policySeed + 1n);
        const at = v.venues.find(x => x.venue === s.venue);
        at.infos[at.addresses.findIndex(a => String(a) === String(s.address))] = policyAccount(v.settings, s.seed, [v.signer], s.action.policy);
        policySeed += 1n;
      }
    }
    assert.ok(approvePlan({ ...v, settingsData: settingsData(policySeed) }).every(s => s.kind === 'update' && s.current));
  }), { numRuns: 30 });
});

test('approve: other rules on chain, another signer, a revoke are updates that are not current', () => {
  const v = vaultWith(['meteora-dlmm'], []);
  // an older release's WSOL pin: the owner field and the program, not the key
  const wsol = vaultWsolAccount(v.vault);
  const older = v.venues[0].compiled.policies.map(({ policy }) => ({ ...policy, constraints: policy.constraints.map(c => ({ ...c,
    accounts: c.accounts.map(a => (a.keys?.length === 1 && a.keys[0].equals(wsol)
      ? { index: 1, owner: TOKEN, data: [{ offset: 32, type: 'bytes', value: v.vault.toBuffer(), op: 'eq' }] } : a)) })) }));
  v.venues[0].infos = older.map((p, i) => policyAccount(v.settings, BigInt(i + 1), [v.signer], p));
  const steps = approvePlan({ ...v });
  assert.deepEqual(steps.map(s => s.current), older.map((p, i) => sq.encodeProgramInteraction(p).equals(
    sq.encodeProgramInteraction(v.venues[0].compiled.policies[i].policy))));
  assert.ok(steps.some(s => !s.current), 'the WSOL pin changed: the owners approve it');
  const signedBy = (signers) => {
    const w = vaultWith(['raydium-clmm'], []);
    w.venues[0].infos = w.venues[0].compiled.policies.map((p, i) => policyAccount(w.settings, BigInt(i + 1), signers(w), p.policy));
    return approvePlan({ ...w });
  };
  assert.ok(signedBy(w => [w.owner]).every(s => s.kind === 'update' && !s.current), 'revoked: the owner signs, not the bot');
  assert.ok(signedBy(w => [w.signer, key()]).every(s => !s.current), 'a second signer is not the bot\'s policy');
  assert.ok(signedBy(w => [w.signer]).every(s => s.current));
});

test('revoke: existing policies name the owner only; a staged one stays uncreated', () => {
  const v = vaultWith(['meteora-dlmm'], ['raydium-clmm']);
  const steps = approvePlan({ ...v, signer: v.owner, create: false });
  assert.deepEqual(steps.map(s => String(s.address)), v.venues[0].addresses.map(String));
  assert.ok(steps.every(s => s.kind === 'update' && s.action.signers[0].key.equals(v.owner) && !s.current));
  // a revoke does not care where a staged policy would go
  assert.equal(approvePlan({ ...v, signer: v.owner, create: false, settingsData: settingsData(99n) }).length, steps.length);
});

test('approve refuses a policy that is not the vault\'s next, a count that differs, another vault\'s or program\'s account', () => {
  const v = vaultWith(['meteora-dlmm'], ['raydium-clmm']);
  // a policy created since the staging: its seeds are taken
  assert.throws(() => approvePlan({ ...v, settingsData: settingsData(BigInt(v.venues[0].addresses.length) + 1n) }), /not the vault's next policy/);
  assert.throws(() => approvePlan({ ...v, settingsData: settingsData(null) }), /not the vault's next policy/);
  const [m, r] = v.venues;
  assert.throws(() => approvePlan({ ...v, venues: [{ ...m, addresses: m.addresses.slice(1), infos: m.infos.slice(1) }, r] }),
    /the meteora-dlmm rules compile to \d+ policies; the config lists \d+/);
  assert.throws(() => approvePlan({ ...v, venues: [{ ...m, infos: [policyAccount(key(), 1n, [v.signer], m.compiled.policies[0].policy),
    ...m.infos.slice(1)] }, r] }), /belongs to another vault/);
  assert.throws(() => approvePlan({ ...v, venues: [{ ...m, infos: [{ ...m.infos[0], owner: key() }, ...m.infos.slice(1)] }, r] }),
    /not a Squads account/);
});

test('staged policies: the vault\'s next seeds, after the ones staged before them', () => {
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 2n ** 32n }), fc.integer({ min: 1, max: 5 }), fc.integer({ min: 0, max: 5 }), (seed, count, before) => {
    const settings = key();
    assert.deepEqual(stagedPolicies(settings, seed, count, before).map(String),
      Array.from({ length: count }, (_, i) => String(sq.policyPda(settings, seed + BigInt(before + i + 1)))));
  }), { numRuns: 30 });
  const s = key();
  assert.deepEqual(stagedPolicies(s, null, 1).map(String), [String(sq.policyPda(s, 1n))]);
});

// --- what the bot says when the owners must sign ------------------------------------------------------

test('verify: the owners can end a rules change, a staged venue, a revoke; a wrong config stays a mismatch', async () => {
  const v = vaultWith(['meteora-dlmm'], []);
  const compiled = v.venues[0].compiled, ok = v.venues[0].infos;
  const cfg = { settings: v.settings, policies: v.venues[0].addresses };
  const conn = (infos) => ({ getMultipleAccountsInfo: async () => infos });
  await verifyPolicy(conn(ok), cfg, v.signer, compiled);
  const awaiting = (re) => (e) => e instanceof PolicyMismatch && e.message.startsWith(`${AWAITS_OWNERS}: `) && re.test(e.message);
  const mismatch = (re) => (e) => e instanceof PolicyMismatch && !e.message.includes(AWAITS_OWNERS) && re.test(e.message);
  await assert.rejects(verifyPolicy(conn(ok), { ...cfg, policies: cfg.policies.slice(1) }, v.signer, compiled), awaiting(/compile to \d+ policies/));
  await assert.rejects(verifyPolicy(conn([null, ...ok.slice(1)]), cfg, v.signer, compiled), awaiting(/does not exist/));
  await assert.rejects(verifyPolicy(conn(ok), cfg, key(), compiled), awaiting(/revoked the bot/));
  await assert.rejects(verifyPolicy(conn(ok), cfg, v.signer, compile('meteora-dlmm', ctxOf(v.vault))), awaiting(/Approve bot rules/));
  await assert.rejects(verifyPolicy(conn([{ ...ok[0], owner: key() }, ...ok.slice(1)]), cfg, v.signer, compiled), mismatch(/not a Squads account/));
  await assert.rejects(verifyPolicy(conn([policyAccount(key(), 1n, [v.signer], compiled.policies[0].policy), ...ok.slice(1)]), cfg, v.signer, compiled),
    mismatch(/belongs to another vault/));
  assert.equal(AWAITS_OWNERS, 'vault awaits the owners\' approval', 'lp/signers.py AWAITS_OWNERS matches this text');
});

// --- the owner's wallet pays: the check before it signs ---------------------------------------------

test('approval cost: the rent of each new policy, the growth of each update, the fees and the wallet\'s floor', async () => {
  const rent = (n) => 1000 + 7 * n;                               // a rent of 1000 + 7 per byte, for the arithmetic
  const steps = (creates, updates) => [...Array(creates).fill({ kind: 'create' }), ...Array(updates).fill({ kind: 'update' })];
  const conn = (balance) => ({ getMinimumBalanceForRentExemption: async (n) => rent(n), getBalance: async () => balance });
  const owner = key().toBase58();
  fc.assert(await fc.asyncProperty(fc.integer({ min: 0, max: 4 }), fc.integer({ min: 0, max: 6 }), async (c, u) => {
    const want = BigInt(c * rent(POLICY_ACCOUNT_BYTES) + u * 7 * POLICY_GROWTH_BYTES + (c + u) * 10_000 + rent(0));
    assert.equal(await affordsRules(conn(Number(want)), owner, steps(c, u)), want);
    await assert.rejects(affordsRules(conn(Number(want) - 1), owner, steps(c, u)),
      new RegExp(`approving needs ${Number(want) / 1e9}.*\\(${c} new policies' rent, ${c + u} transaction fees`));
  }), { numRuns: 40 });
});

// the vault's positions are read by the venues' SDKs: none here (test_onboard_sol.mjs counts them)
const NO_POSITIONS = { positionsOf: async () => [] };

test('status: a policy staged for a venue the owners have not approved yet is no revoke, but the rules are not current', async () => {
  const v = vaultWith(['meteora-dlmm'], ['raydium-clmm']);
  const policyKey = { publicKey: v.signer };
  const conn = (infos) => ({ getMultipleAccountsInfo: async () => infos, getBalance: async () => 0 });
  const infos = v.venues.flatMap(x => x.infos);
  const keys = { payer: { publicKey: key() }, policyKey };
  const rowOf = () => ({ settings: v.settings.toBase58(), venues: Object.fromEntries(v.venues.map(x => [x.venue,
    { policies: x.addresses.map(String), pools: [key().toBase58()], payouts: [key().toBase58()] }])) });
  const st = await solStatus(conn(infos), { tokens: {} }, rowOf(), keys, NO_POSITIONS);
  assert.equal(st.botActive, true);
  assert.equal(st.rulesCurrent, false);
  assert.equal((await solStatus(conn(infos.map(() => null)), { tokens: {} }, rowOf(), keys, NO_POSITIONS)).botActive, false, 'no policy at all: nothing active');
  const revoked = infos.map(i => i && policyAccount(v.settings, 1n, [key()], v.venues[0].compiled.policies[0].policy));
  assert.equal((await solStatus(conn(revoked), { tokens: {} }, rowOf(), keys, NO_POSITIONS)).botActive, false);
});
