// onboard/decode_sol.mjs: the page reads every Solana transaction before the
// wallet signs it. Each kind built as the server builds it passes and reads
// in words; every tampered copy (another amount, recipient, signer, vault,
// rule, pool, program) is refused. The real builders on a fork:
// test_onboard_fork.mjs runs every transaction of the page through it too.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import fc from 'fast-check';
import * as sq from '../chains/solana/vault/squads.mjs';
import { RULES, compile, instructionsOf } from '../chains/solana/vault/policy.mjs';
import { ata, decodeSol, decodeSolSet, expectedPolicies, MAX_PRIORITY_LAMPORTS, OWNER_MESSAGE_BYTES } from '../onboard/decode_sol.mjs';
import { ownerSetupPlan, packSteps } from '../chains/solana/vault/admin.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { ComputeBudgetProgram, Keypair, PublicKey, SystemProgram, Transaction, TransactionInstruction, TransactionMessage, VersionedTransaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const key = () => Keypair.generate().publicKey;
const OWNER = key(), PAYER = key(), POLICY_KEY = key(), PROFIT = key(), STRANGER = key();
const SETTINGS = key(), VAULT = sq.vaultPda(SETTINGS);
const OPERATOR = SOLANA.operator_profit_wallet, USDC = new PublicKey(SOLANA.usdc_mint), WSOL = new PublicKey(SOLANA.native_mint);
const POOLS = Object.fromEntries(Object.entries(SOLANA.sol_usdc_pools).map(([v, p]) => [v, [p]]));
const PINS = { payer: PAYER.toBase58(), policyKey: POLICY_KEY.toBase58(), operator: OPERATOR, pools: POOLS, rules: RULES, solana: SOLANA };
const VENUE = 'raydium-clmm';
const payoutWallets = [PROFIT.toBase58(), OPERATOR];
const POLICIES = compile(VENUE, { vault: VAULT, payer: PAYER, pools: POOLS[VENUE].map(p => new PublicKey(p)),
  payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies.map(() => key().toBase58());
const base = { owner: OWNER.toBase58(), vault: VAULT.toBase58(), settings: SETTINGS.toBase58() };
const vaultExpect = (kind, over = {}) => ({ ...base, kind, policies: { [VENUE]: POLICIES }, pools: { [VENUE]: POOLS[VENUE] }, payoutWallets, ...over });

const wire = (ixs, payer = OWNER) => {
  const tx = new Transaction().add(...ixs);
  tx.feePayer = payer; tx.recentBlockhash = PublicKey.default.toBase58();
  return tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');
};
const ok = (r) => assert.deepEqual(r.problems, [], r.lines.join('\n'));
const refused = (r, re, what) => { assert.equal(r.ok, false, what); if (re) assert.match(r.problems.join(' | '), re, what); };

const transfer = (from, to, lamports) => SystemProgram.transfer({ fromPubkey: from, toPubkey: to, lamports });
const tokenSend = (src, dst, auth, raw, mint = USDC) => spl.createTransferCheckedInstruction(src, mint, dst, auth, raw, 6);
const ownerExec = (ixs, over = {}) => sq.ownerExecuteIx({ settings: SETTINGS, signers: [OWNER], vault: VAULT, ixs, ...over });
const policyTx = (signer, i = 0, over = {}) => {
  const policies = compile(VENUE, { vault: VAULT, payer: PAYER, pools: POOLS[VENUE].map(p => new PublicKey(p)),
    payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies;
  const address = new PublicKey(over.address ?? POLICIES[i]);
  return [sq.settingsSyncIx({ settings: over.settings ?? SETTINGS, signers: [OWNER], rentPayer: OWNER, extra: [address],
    actions: [{ kind: 'policyUpdate', address, policy: over.policy ?? policies[i].policy, signers: [{ key: signer, permissions: over.permissions }] }] })];
};

// --- the owner's set-up: one approval, a set of transactions ----------------------

const SEED = 4242n, SETUP_SETTINGS = sq.settingsPda(SEED), SETUP_VAULT = sq.vaultPda(SETUP_SETTINGS);
const TREASURY = key(), GAS = 118_600_000n;
const setupCompiled = (over = {}) => [compile(VENUE, { vault: SETUP_VAULT, payer: PAYER, pools: POOLS[VENUE].map(p => new PublicKey(p)),
  payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))), ...over })];
const setupExpect = (over = {}) => ({ kind: 'setup', owner: OWNER.toBase58(), seed: SEED.toString(), settings: SETUP_SETTINGS.toBase58(),
  vault: SETUP_VAULT.toBase58(), venues: [VENUE], pools: { [VENUE]: POOLS[VENUE] }, payoutWallets, mint: SOLANA.native_mint,
  raw: '500000000', gasRaw: GAS.toString(), ...over });
const solDeposit = (raw = 500_000_000n, to = SETUP_VAULT) => [transfer(OWNER, to, raw)];
const usdcDeposit = (raw = 25_000_000n) => {
  const dst = spl.getAssociatedTokenAddressSync(USDC, SETUP_VAULT, true);
  return [spl.createAssociatedTokenAccountIdempotentInstruction(OWNER, dst, SETUP_VAULT, USDC),
    tokenSend(spl.getAssociatedTokenAddressSync(USDC, OWNER), dst, OWNER, raw)];
};
// The set as the server builds it: packSteps of the plan, the funding with the last policy.
function setupSet({ plan = ownerSetupPlan({ owner: OWNER, treasury: TREASURY, seed: SEED, policyKey: POLICY_KEY, venues: setupCompiled() }),
  from = 0, fund = [...solDeposit(), transfer(OWNER, PAYER, GAS)], groups = null } = {}) {
  const steps = plan.steps.slice(from);
  const packed = groups ?? packSteps([...steps.slice(0, -1), [steps.at(-1), ...fund]], OWNER);
  return packed.map(ixs => wire(ixs));
}

test('setup: the owner creates its vault, the bot\'s policies exactly as in git, then deposits and pays the gas, one approval', () => {
  const p0 = ownerSetupPlan({ owner: OWNER, treasury: TREASURY, seed: SEED, policyKey: POLICY_KEY, venues: setupCompiled() });
  const set = setupSet();
  assert.equal(set.length, 2, 'Raydium: two transactions');
  const r = decodeSolSet(set, setupExpect(), PINS);
  ok(r);
  const text = r.lines.join('\n');
  assert.match(text, /Transaction 1 of 2:\n  Create your vault .*you its only owner, threshold 1/);
  assert.equal(text.match(/exactly the rules in git/g).length, setupCompiled()[0].policies.length);
  assert.match(text, /Funding: Send 0\.1186 SOL from your wallet .* to the bot's gas key/);
  assert.match(text, /Funding: Send 0\.5 SOL from your wallet .* to your vault/);
  // a USDC deposit: the vault's account opened, the tokens from the wallet's
  const [open, send] = usdcDeposit();
  const usdc = setupSet({ groups: packSteps([...p0.steps.slice(0, -1), open, [p0.steps.at(-1), send, transfer(OWNER, PAYER, GAS)]], OWNER) });
  assert.equal(usdc.length, 3, 'Raydium with USDC: the vault\'s account opened in a transaction of its own');
  ok(decodeSolSet(usdc, setupExpect({ mint: SOLANA.usdc_mint, raw: '25000000' }), PINS));
  // the account opened for another wallet or mint, or paid by another, is refused
  const strangerOpen = spl.createAssociatedTokenAccountIdempotentInstruction(OWNER, spl.getAssociatedTokenAddressSync(USDC, STRANGER), STRANGER, USDC);
  refused(decodeSolSet(setupSet({ groups: packSteps([...p0.steps.slice(0, -1), strangerOpen, [p0.steps.at(-1), send, transfer(OWNER, PAYER, GAS)]], OWNER) }),
    setupExpect({ mint: SOLANA.usdc_mint, raw: '25000000' }), PINS), /not your vault's/);
  // a resume: the vault and the first policy exist; the rest and the funding
  ok(decodeSolSet(setupSet({ from: 2 }), setupExpect(), PINS));
  ok(decodeSolSet(setupSet({ from: 2 }), setupExpect({ created: false }), PINS));
  refused(decodeSolSet(setupSet({ from: 2 }), setupExpect({ created: true }), PINS), /not created/);
  refused(decodeSolSet(setupSet(), setupExpect({ created: false }), PINS), /creates a vault that exists/);
  // each venue: its policies at the seeds after the vault's creation
  for (const venue of Object.keys(POOLS).filter(v => RULES.venues[v])) {
    const compiled = [compile(venue, { vault: SETUP_VAULT, payer: PAYER, pools: POOLS[venue].map(p => new PublicKey(p)),
      payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) })];
    const plan = ownerSetupPlan({ owner: OWNER, treasury: TREASURY, seed: SEED, policyKey: POLICY_KEY, venues: compiled });
    ok(decodeSolSet(setupSet({ plan }), setupExpect({ venues: [venue], pools: { [venue]: POOLS[venue] } }), PINS));
  }
});

test('setup: every tampered set is refused (owners, threshold, rules, order, funding, gas, the vault)', () => {
  const e = setupExpect();
  const plan = (over = {}) => ownerSetupPlan({ owner: OWNER, treasury: TREASURY, seed: SEED, policyKey: POLICY_KEY, venues: setupCompiled(), ...over });
  const p = plan();
  const steps = p.steps, fund = [...solDeposit(), transfer(OWNER, PAYER, GAS)];
  // the creation: another owner beside you, threshold 2, a settings authority, a time lock
  const create = (o) => sq.createSmartAccountIx({ creator: OWNER, treasury: TREASURY, seed: SEED, threshold: 1, signers: [{ key: OWNER }], ...o }).ix;
  const withCreate = (ix) => setupSet({ groups: [[ix], [steps[1]], [steps[2], ...fund]] });
  refused(decodeSolSet(withCreate(create({ signers: [{ key: OWNER }, { key: STRANGER }] })), e, PINS), /not your wallet alone/, 'a second owner');
  refused(decodeSolSet(withCreate(create({ signers: [{ key: STRANGER }] })), e, PINS), /not your wallet alone/, 'another owner');
  refused(decodeSolSet(withCreate(create({ threshold: 2 })), e, PINS), /threshold 2/);
  refused(decodeSolSet(withCreate(create({ seed: SEED + 1n })), e, PINS), /not your vault's/, 'another seed\'s settings');
  refused(decodeSolSet(withCreate(create({ signers: [{ key: OWNER, permissions: 1 }] })), e, PINS), /not your wallet alone/, 'fewer permissions');
  {
    const ix = create();
    const auth = Buffer.concat([ix.data.subarray(0, 8), Buffer.from([1]), STRANGER.toBuffer(), ix.data.subarray(9)]);
    refused(decodeSolSet(withCreate(new TransactionInstruction({ programId: ix.programId, keys: ix.keys, data: auth })), e, PINS), /settings authority/);
    const lock = Buffer.from(ix.data); lock.writeUInt32LE(60, 8 + 1 + 2 + 4 + 33);
    refused(decodeSolSet(withCreate(new TransactionInstruction({ programId: ix.programId, keys: ix.keys, data: lock })), e, PINS), /time lock/);
    const keys = ix.keys.map((k, i) => (i === 2 ? { ...k, pubkey: STRANGER } : k));
    refused(decodeSolSet(withCreate(new TransactionInstruction({ programId: ix.programId, keys, data: ix.data })), e, PINS), /creator is not your wallet|signatures of/);
  }
  // a planned seed whose settings are not those the page shows
  refused(decodeSolSet(setupSet(), setupExpect({ seed: '7' }), PINS), /planned seed/);
  // policies: another key, other rules, out of order, one missing, after the money
  refused(decodeSolSet(setupSet({ plan: plan({ policyKey: STRANGER }) }), e, PINS), /not the bot's rules key/);
  const wrongRules = plan({ venues: setupCompiled({ payouts: [new PublicKey(ata(STRANGER.toBase58(), USDC))] }) });
  refused(decodeSolSet(setupSet({ plan: wrongRules }), e, PINS), /differs from the rules in git/);
  refused(decodeSolSet(setupSet({ groups: [[steps[0]], [steps[2]], [steps[1], ...fund]] }), e, PINS), /out of order/);
  refused(decodeSolSet(setupSet({ groups: [[steps[0], steps[1]], [...fund]] }), e, PINS), /rules would be missing/);
  refused(decodeSolSet(setupSet({ groups: [[steps[0], steps[1]], [...fund, steps[2]]] }), e, PINS), /after the deposit/);
  refused(decodeSolSet(setupSet({ groups: [[steps[0], ...fund], [steps[1]], [steps[2]]] }), e, PINS), /before the last transaction/);
  refused(decodeSolSet(setupSet({ groups: [[steps[0], steps[1]], [steps[2]], fund] }), e, PINS), /same transaction as the last policy/);
  refused(decodeSolSet(setupSet({ groups: [[steps[1]], [steps[0]], [steps[2], ...fund]] }), e, PINS), /not the first step/);
  refused(decodeSolSet(setupSet({ groups: [[steps[0]], [steps[2], ...fund]] }), e, PINS), /without its first policy|out of order/);
  // the funding: the gas to another key, another amount, twice; the deposit elsewhere or another amount; one more transfer
  refused(decodeSolSet(setupSet({ fund: [...solDeposit(), transfer(OWNER, STRANGER, GAS)] }), e, PINS), /0 payments to the bot's gas key/);
  refused(decodeSolSet(setupSet({ fund: [...solDeposit(), transfer(OWNER, PAYER, GAS + 1n)] }), e, PINS), /not the 0\.1186 SOL gas shown/);
  refused(decodeSolSet(setupSet({ fund: [...solDeposit(), transfer(OWNER, PAYER, GAS), transfer(OWNER, PAYER, 1n)] }), e, PINS), /2 payments/);
  refused(decodeSolSet(setupSet({ fund: [...solDeposit(500_000_000n, STRANGER), transfer(OWNER, PAYER, GAS)] }), e, PINS), /not go from your wallet to your vault/);
  refused(decodeSolSet(setupSet({ fund: [...solDeposit(1n), transfer(OWNER, PAYER, GAS)] }), e, PINS), /deposits 0\.000000001 SOL/);
  refused(decodeSolSet(setupSet({ fund: [...solDeposit(), transfer(OWNER, STRANGER, 1n), transfer(OWNER, PAYER, GAS)] }), e, PINS), /one transfer/);
  refused(decodeSolSet(setupSet({ fund: [transfer(OWNER, PAYER, GAS)] }), e, PINS), /one transfer/, 'no deposit');
  // the gas from another wallet: it would need that wallet's signature
  refused(decodeSolSet(setupSet({ fund: [...solDeposit(), transfer(STRANGER, PAYER, GAS)] }), e, PINS), /signatures of|not come from your wallet/);
  refused(decodeSolSet([], e, PINS), /no transaction/);
  refused(decodeSolSet(setupSet(), { ...e, kind: 'teleport' }, PINS), /nothing to check/);
  // the gas: exactly the amount shown, any other refused
  fc.assert(fc.property(fc.bigInt({ min: 1n, max: 10n ** 11n }), (lamports) => {
    const r = decodeSolSet(setupSet({ fund: [...solDeposit(), transfer(OWNER, PAYER, lamports)] }), e, PINS);
    assert.equal(r.ok, lamports === GAS);
  }), { numRuns: 60 });
});

test('approve with a deposit (a re-grant with more capital): the rules first, the deposit last, one approval', () => {
  const dep = { mint: SOLANA.native_mint, raw: '700000000' };
  const e = vaultExpect('approve', { deposit: dep });
  const rules = POLICIES.map((_, i) => policyTx(POLICY_KEY, i)[0]);
  const money = [transfer(OWNER, VAULT, 700_000_000n)];
  const groups = packSteps([...rules.map(ix => [ix]), money], OWNER);
  const r = decodeSolSet(groups.map(g => wire(g)), e, PINS);
  ok(r);
  assert.match(r.lines.join('\n'), /Deposit: Send 0\.7 SOL from your wallet .* to your vault/);
  // without a deposit asked for, money is refused; a set of policies alone passes
  ok(decodeSolSet(rules.map(ix => wire([ix])), vaultExpect('approve'), PINS));
  refused(decodeSolSet(groups.map(g => wire(g)), vaultExpect('approve'), PINS), /an approval moves none/);
  refused(decodeSolSet(rules.map(ix => wire([ix])), e, PINS), /deposit is missing/);
  refused(decodeSolSet([wire(money), ...rules.map(ix => wire([ix]))], e, PINS), /before the last transaction|after the deposit/);
  refused(decodeSolSet(packSteps([...rules.map(ix => [ix]), [transfer(OWNER, STRANGER, 700_000_000n)]], OWNER).map(g => wire(g)), e, PINS), /your vault/);
  refused(decodeSolSet(packSteps([...rules.map(ix => [ix]), [transfer(OWNER, VAULT, 1n)]], OWNER).map(g => wire(g)), e, PINS), /deposits 0\.000000001 SOL/);
  refused(decodeSolSet([wire([...money, rules[0]])], vaultExpect('approve', { deposit: dep, policies: { [VENUE]: [POLICIES[0]] } }), PINS),
    /money moves before the rules/);
  refused(decodeSolSet(rules.map(ix => wire([ix])).map((w, i) => (i === 0 ? wire(policyTx(STRANGER, 0)) : w)), vaultExpect('approve'), PINS), /not the bot's rules key/);
});

test('deposit: SOL to the vault, or USDC from the wallet\'s account to the vault\'s', () => {
  ok(decodeSol(wire([transfer(OWNER, VAULT, 2_000_000_000)]), { ...base, kind: 'deposit', mint: WSOL.toBase58(), raw: '2000000000' }, PINS));
  refused(decodeSol(wire([transfer(OWNER, STRANGER, 2_000_000_000)]), { ...base, kind: 'deposit', mint: WSOL.toBase58(), raw: '2000000000' }, PINS), /your vault/);
  const e = { ...base, kind: 'deposit', mint: USDC.toBase58(), raw: '10000000' };
  const src = spl.getAssociatedTokenAddressSync(USDC, OWNER), dst = spl.getAssociatedTokenAddressSync(USDC, VAULT, true);
  const open = spl.createAssociatedTokenAccountIdempotentInstruction(OWNER, dst, VAULT, USDC);
  const r = decodeSol(wire([open, tokenSend(src, dst, OWNER, 10_000_000)]), e, PINS);
  ok(r);
  assert.match(r.lines.join('\n'), /Send 10 USDC from your wallet's USDC account .* to your vault's USDC account/);
  const strangerAta = spl.getAssociatedTokenAddressSync(USDC, STRANGER);
  refused(decodeSol(wire([open, tokenSend(src, strangerAta, OWNER, 10_000_000)]), e, PINS), /not your vault's account/);
  refused(decodeSol(wire([open, tokenSend(src, dst, OWNER, 9_999_999)]), e, PINS), /deposits 9\.999999/);
  refused(decodeSol(wire([spl.createAssociatedTokenAccountIdempotentInstruction(OWNER, strangerAta, STRANGER, USDC), tokenSend(src, dst, OWNER, 10_000_000)]), e, PINS),
    /not your vault's/);
});

test('deposit signed by Phantom: its read-only Lighthouse guard passes; a writable or signing one, or another program, is refused', () => {
  const LIGHTHOUSE = new PublicKey(SOLANA.programs.lighthouse);
  const e = { ...base, kind: 'deposit', mint: WSOL.toBase58(), raw: '1590000000' };
  // the owner is the fee payer: signer and writable in the message whatever the guard asks
  const guard = (writable, signer = false, who = STRANGER) => new TransactionInstruction({ programId: LIGHTHOUSE, data: Buffer.from([4, 0, 1, 2]),
    keys: [{ pubkey: OWNER, isSigner: true, isWritable: true }, { pubkey: VAULT, isSigner: false, isWritable: false },
      ...(writable || signer ? [{ pubkey: who, isSigner: signer, isWritable: writable }] : [])] });
  const r = decodeSol(wire([transfer(OWNER, VAULT, 1_590_000_000), guard(false)]), e, PINS);
  ok(r);
  assert.match(r.lines.join('\n'), /Wallet guard \(Lighthouse\)/);
  ok(decodeSol(wire([guard(false), transfer(OWNER, VAULT, 1_590_000_000), guard(false)]), e, PINS));
  refused(decodeSol(wire([transfer(OWNER, VAULT, 1_590_000_000), guard(true)]), e, PINS), /could change an account/);
  ok(decodeSol(wire([transfer(OWNER, VAULT, 1_590_000_000), guard(true, false, VAULT)]), e, PINS));   // the vault: written by the transfer already
  refused(decodeSol(wire([transfer(OWNER, VAULT, 1_590_000_000), guard(false, true)]), e, PINS), /could change an account/);
  const other = new TransactionInstruction({ programId: STRANGER, data: Buffer.from([1]), keys: [] });
  refused(decodeSol(wire([transfer(OWNER, VAULT, 1_590_000_000), other]), e, PINS), /2 instruction\(s\)/);
});

test('withdraw: from the vault into the owner\'s wallet only, the asked amount', () => {
  const e = (raw, mint = WSOL) => ({ ...base, kind: 'withdraw', mint: mint.toBase58(), raw });
  ok(decodeSol(wire([ownerExec([transfer(VAULT, OWNER, 500_000_000)])]), e('500000000'), PINS));
  refused(decodeSol(wire([ownerExec([transfer(VAULT, STRANGER, 500_000_000)])]), e('500000000'), PINS), /from your vault to your wallet/);
  refused(decodeSol(wire([ownerExec([transfer(VAULT, OWNER, 500_000_001)])]), e('500000000'), PINS), /not what you asked/);
  refused(decodeSol(wire([ownerExec([transfer(VAULT, OWNER, 1)], { settings: key() })]), e('1'), PINS), /settings/);
  const src = spl.getAssociatedTokenAddressSync(USDC, VAULT, true), dst = spl.getAssociatedTokenAddressSync(USDC, OWNER);
  const usdc = [spl.createAssociatedTokenAccountIdempotentInstruction(OWNER, dst, OWNER, USDC), ownerExec([tokenSend(src, dst, VAULT, 1_500_000)])];
  ok(decodeSol(wire(usdc), e('1500000', USDC), PINS));
  refused(decodeSol(wire(usdc), e('1500001', USDC), PINS), /amount/);
  const away = [ownerExec([tokenSend(src, spl.getAssociatedTokenAddressSync(USDC, STRANGER), VAULT, 1_500_000)])];
  refused(decodeSol(wire(away), e('1500000', USDC), PINS), /not your wallet's account/);
  // all: every balance, the vault's accounts closed to the owner
  const all = [spl.createAssociatedTokenAccountIdempotentInstruction(OWNER, dst, OWNER, USDC),
    ownerExec([tokenSend(src, dst, VAULT, 7), spl.createCloseAccountInstruction(src, OWNER, VAULT), transfer(VAULT, OWNER, 9)])];
  ok(decodeSol(wire(all), e('all'), PINS));
  const closeAway = [ownerExec([spl.createCloseAccountInstruction(src, STRANGER, VAULT), transfer(VAULT, OWNER, 9)])];
  refused(decodeSol(wire(closeAway), e('all'), PINS), /closes to somewhere/);
  refused(decodeSol(wire([ownerExec([spl.createCloseAccountInstruction(src, OWNER, VAULT), transfer(VAULT, OWNER, 9)])]), e('9'), PINS), /does not need/, 'a close only with all');
  // through a bot policy, never
  const viaPolicy = sq.policyExecuteIx({ policy: new PublicKey(POLICIES[0]), signer: OWNER, vault: VAULT, ixs: [transfer(VAULT, OWNER, 1)], indices: [0] });
  refused(decodeSol(wire([viaPolicy]), e('1'), PINS), /does not need|policy/);
});

test('approve: exactly the rules in git, the bot\'s rules key the only signer; revoke names the owner alone', () => {
  for (let i = 0; i < POLICIES.length; i++) {
    const r = decodeSol(wire(policyTx(POLICY_KEY, i)), vaultExpect('approve'), PINS);
    ok(r);
    assert.match(r.lines.join('\n'), /exactly the rules in git/);
    assert.match(r.lines.join('\n'), /Raydium: (open_position_v2|decrease_liquidity_v2|close_position)/);
    ok(decodeSol(wire(policyTx(OWNER, i)), vaultExpect('revoke'), PINS));
  }
  refused(decodeSol(wire(policyTx(STRANGER)), vaultExpect('approve'), PINS), /not the bot's rules key/, 'another key');
  refused(decodeSol(wire(policyTx(POLICY_KEY)), vaultExpect('revoke'), PINS), /not only you/, 'a revoke that leaves the bot');
  refused(decodeSol(wire(policyTx(POLICY_KEY, 0, { permissions: 1 })), vaultExpect('approve'), PINS), /not the bot's rules key/, 'other permissions');
  refused(decodeSol(wire(policyTx(POLICY_KEY, 0, { address: key().toBase58() })), vaultExpect('approve'), PINS), /not one of your vault's/);
  refused(decodeSol(wire(policyTx(POLICY_KEY, 0, { settings: key() })), vaultExpect('approve'), PINS), /another vault/);
  // a policy with another pool, payout or one more rule differs from git
  // each policy that a changed pool, payout or payer touches differs from the pinned rules
  const compiled = (ctx = {}) => compile(VENUE, { vault: VAULT, payer: PAYER, pools: POOLS[VENUE].map(p => new PublicKey(p)),
    payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))), ...ctx }).policies.map(p => p.policy);
  const right = compiled();
  for (const [what, ctx] of [['another pool', { pools: [key()] }], ['another payout', { payouts: [key()] }]]) {
    const wrong = compiled(ctx);
    let changed = 0;
    wrong.forEach((policy, i) => {
      if (sq.encodeProgramInteraction(policy).equals(sq.encodeProgramInteraction(right[i]))) return;
      changed++;
      refused(decodeSol(wire(policyTx(POLICY_KEY, i, { policy })), vaultExpect('approve'), PINS), /differs from the rules in git/, `${what}, policy ${i}`);
    });
    assert.ok(changed > 0, what);
  }
  // one more allowed instruction in the last policy
  const last = right.length - 1;
  const wider = { ...right[last], constraints: [...right[last].constraints, right[last].constraints[0]] };
  refused(decodeSol(wire(policyTx(POLICY_KEY, last, { policy: wider })), vaultExpect('approve'), PINS), /differs/, 'one more rule');
  // a pool the server names that the bundled config does not approve
  refused(decodeSol(wire(policyTx(POLICY_KEY)), vaultExpect('approve', { pools: { [VENUE]: [key().toBase58()] } }), PINS), /not the raydium-clmm pool/);
  // the payouts the page shows are the owner's profit wallet and the operator: another wallet changes the rules
  refused(decodeSol(wire(policyTx(POLICY_KEY, last)), vaultExpect('approve', { payoutWallets: [STRANGER.toBase58(), OPERATOR] }), PINS), /differs/);
  assert.equal(expectedPolicies(VENUE, vaultExpect('approve'), PINS).length, POLICIES.length);
});

test('transactions the page cannot read, or that cost the owner, are refused', () => {
  const e = { ...base, kind: 'deposit', mint: SOLANA.native_mint, raw: '1' };
  refused(decodeSol('not base64 at all', e, PINS), /cannot be read/);
  const v0 = new VersionedTransaction(new TransactionMessage({ payerKey: OWNER, recentBlockhash: PublicKey.default.toBase58(),
    instructions: [transfer(OWNER, VAULT, 1)] }).compileToV0Message()).serialize();
  refused(decodeSol(Buffer.from(v0).toString('base64'), e, PINS), /cannot be read|version 0/);
  const pricey = [ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 10_000_000 }), transfer(OWNER, VAULT, 1)];
  refused(decodeSol(wire(pricey), e, PINS), /priority fee/);
  const cheap = [ComputeBudgetProgram.setComputeUnitLimit({ units: 200_000 }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1000 }), transfer(OWNER, VAULT, 1)];
  ok(decodeSol(wire(cheap), e, PINS));
  assert.ok(MAX_PRIORITY_LAMPORTS <= 1_000_000n);
  const unknown = new TransactionInstruction({ programId: STRANGER, keys: [], data: Buffer.from([1, 2, 3]) });
  refused(decodeSol(wire([unknown]), e, PINS), /transfer/);
  refused(decodeSol(wire([transfer(OWNER, VAULT, 1)]), { ...base, kind: 'teleport' }, PINS), /nothing to check/);
  refused(decodeSol(wire([transfer(OWNER, PAYER, 1)]), { ...base, kind: 'payment', raw: '1' }, PINS), /nothing to check/, 'a gas payment alone is no kind any more');
});

// 2026-10-17: tang0 adds Raydium 3ucNos to its vault (cli.mjs add-pool): the rules over both
// pools, the existing policies updated and one created. The page pins the extra pool from
// solana.json vault_extra_pools; without the pin it refuses, as it did before the pin.
test('approve over two pools: passes with the extra pool pinned, refused without it', () => {
  const extra = SOLANA.vault_extra_pools[VENUE];
  assert.deepEqual(extra, ['3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv']);
  const both = [...POOLS[VENUE], ...extra];
  const pinned = { ...PINS, pools: { ...POOLS, [VENUE]: both } };
  const compiled = compile(VENUE, { vault: VAULT, payer: PAYER, pools: both.map(p => new PublicKey(p)),
    payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies;
  // the policies the vault has stay where they are; a created one is the vault's policy at its next seed
  const addresses = compiled.map((_, i) => (i < POLICIES.length ? new PublicKey(POLICIES[i]) : sq.policyPda(SETTINGS, BigInt(i + 1))));
  const expect = vaultExpect('approve', { policies: { [VENUE]: addresses.map(String) }, pools: { [VENUE]: both } });
  const txs = compiled.map(({ policy }, i) => wire([sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER], rentPayer: OWNER,
    extra: [addresses[i]], actions: [{ kind: i < POLICIES.length ? 'policyUpdate' : 'policyCreate', address: addresses[i],
      ...(i < POLICIES.length ? {} : { seed: BigInt(i + 1) }), policy, signers: [{ key: POLICY_KEY }] }] })]));
  for (const t of txs) ok(decodeSol(t, expect, pinned));
  for (const t of txs) refused(decodeSol(t, expect, PINS), /3ucNos.*not the raydium-clmm pool|not the raydium-clmm pool/);
  // a pool pinned for another venue does not count for this one
  refused(decodeSol(txs[0], expect, { ...PINS, pools: { ...POOLS, orca: [...POOLS.orca, ...extra] } }), /not the raydium-clmm pool/);
});

// sql/047: a card-signing owner's vault packs smaller policies; the page compiles with the size the
// server's context names (bounded by compile), and each transaction fits the card.
test('approve at the vault\'s policy size: passes, fits a Tangem card; another size or an invalid one is refused', () => {
  const both = [...POOLS[VENUE], ...SOLANA.vault_extra_pools[VENUE]];
  const pinned = { ...PINS, pools: { ...POOLS, [VENUE]: both } };
  const payouts = payoutWallets.map(w => new PublicKey(ata(w, USDC)));
  const build = (size) => {
    const compiled = compile(VENUE, { vault: VAULT, payer: PAYER, pools: both.map(p => new PublicKey(p)), payouts, policyBytes: size }).policies;
    const have = compile(VENUE, { vault: VAULT, payer: PAYER, pools: POOLS[VENUE].map(p => new PublicKey(p)), payouts }).policies.length;
    const addresses = compiled.map((_, i) => (i < have ? new PublicKey(POLICIES[i]) : sq.policyPda(SETTINGS, BigInt(i + 1))));
    const txs = compiled.map(({ policy }, i) => {
      const t = new Transaction().add(sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER], rentPayer: OWNER, extra: [addresses[i]],
        actions: [{ kind: i < have ? 'policyUpdate' : 'policyCreate', address: addresses[i], ...(i < have ? {} : { seed: BigInt(i + 1) }),
          policy, signers: [{ key: POLICY_KEY }] }] }));
      t.feePayer = OWNER; t.recentBlockhash = PublicKey.default.toBase58();
      return t;
    });
    return { addresses, txs, wires: txs.map(t => t.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64')) };
  };
  const at600 = build(600);
  const expect = (size, addresses = at600.addresses) => vaultExpect('approve', { policies: { [VENUE]: addresses.map(String) },
    pools: { [VENUE]: both }, ...(size === undefined ? {} : { policyBytes: size }) });
  for (const w of at600.wires) ok(decodeSol(w, expect(600), pinned));
  for (const t of at600.txs) assert.ok(t.serializeMessage().length <= 930, `card: ${t.serializeMessage().length} bytes`);
  // the default size splits the rules otherwise: the set is refused without the vault's size (a policy that
  // happens to be the same at both sizes passes on its own, the others do not)
  refused(decodeSolSet(at600.wires, expect(undefined), pinned), /differs from the rules in git|rules make/);
  assert.ok(at600.wires.some(w => !decodeSol(w, expect(undefined), pinned).ok));
  // a size outside the bounds is refused, never compiled
  for (const bad of [100, 2000, 600.5, 'x']) refused(decodeSol(at600.wires[0], expect(bad), pinned), /cannot be compiled for your vault/, String(bad));
});

test('the page and the offline verifier pin the same pools: each venue\'s home pool, then its extra ones', async () => {
  const fs = await import('node:fs');
  const { pinsOf } = await import('../onboard/verify.mjs');
  const read = (f) => fs.readFileSync(new URL(f, import.meta.url), 'utf8');
  const p = pinsOf(read('../chains/solana/vault/rules.json'), read('../chains/solana/solana.json'), read('../onboard/onboard.json'));
  for (const [v, home] of Object.entries(SOLANA.sol_usdc_pools)) {
    assert.deepEqual(p.solana.pools[v], [home, ...(SOLANA.vault_extra_pools[v] ?? [])], v);
  }
  // the bundle's builder pins with the same expression
  const build = read('../onboard/web/build.mjs'), verify = read('../onboard/verify.mjs');
  const expr = '[v, [p, ...(solana.vault_extra_pools?.[v] ?? [])]]';
  assert.ok(build.includes(expr) && verify.includes(expr));
});

test('close: the venue\'s exit instructions only, pinned to the vault and the approved pool', () => {
  const rule = (name) => instructionsOf(VENUE).find(r => r.name === name);
  const pool = new PublicKey(POOLS[VENUE][0]), program = new PublicKey(RULES.venues[VENUE].program);
  const ix = (name, keys) => new TransactionInstruction({ programId: program, data: Buffer.concat([Buffer.from(rule(name).discriminator, 'hex'), Buffer.alloc(24)]),
    keys: keys.map(k => ({ pubkey: k, isSigner: false, isWritable: true })) });
  // decrease_liquidity_v2 as the rules pin it: pool, the vault's token accounts, the bitmap
  const dec = rule('decrease_liquidity_v2');
  const keys = Array.from({ length: 20 }, () => key());
  const solAta = ata(VAULT, WSOL), usdcAta = ata(VAULT, USDC);
  const reward = key();
  keys[14] = WSOL; keys[15] = USDC; keys[19] = reward;     // mints named in the instruction: the vault's accounts derive from them
  for (const p of dec.pins) {
    keys[p.index] = p.role === 'pool' ? pool : p.role === 'vault' ? VAULT : p.role === 'pda'
      ? PublicKey.findProgramAddressSync(p.seeds.map(s => s === 'pool' ? pool.toBuffer() : Buffer.from(s)), program)[0]
      : new PublicKey(p.index >= 16 ? ata(VAULT, reward) : p.index % 2 ? solAta : usdcAta);
  }
  const good = ix('decrease_liquidity_v2', keys);
  const e = { ...base, kind: 'close' };
  const r = decodeSol(wire([ownerExec([good])]), e, PINS);
  ok(r);
  assert.match(r.lines.join('\n'), /Raydium: decrease_liquidity_v2/);
  for (const p of dec.pins.filter(x => x.role !== 'none')) {
    const bad = [...keys]; bad[p.index] = STRANGER;
    refused(decodeSol(wire([ownerExec([ix('decrease_liquidity_v2', bad)])]), e, PINS), /not your vault's/, `pin ${p.index} (${p.role})`);
  }
  refused(decodeSol(wire([ownerExec([ix('swap_v2', keys)])]), e, PINS), /not a way out/, 'a swap is no close');
  refused(decodeSol(wire([ownerExec([ix('open_position_v2', keys)])]), e, PINS), /not a way out/, 'an open is no close');
  refused(decodeSol(wire([ownerExec([transfer(VAULT, STRANGER, 5)])]), e, PINS), /does not need|closes no position/, 'a transfer out');
  refused(decodeSol(wire([ownerExec([good, spl.createCloseAccountInstruction(new PublicKey(solAta), STRANGER, VAULT)])]), e, PINS), /closes to somewhere/);
  ok(decodeSol(wire([ownerExec([good, spl.createCloseAccountInstruction(new PublicKey(solAta), VAULT, VAULT)])]), e, PINS));
});

// --- the edges: what is read as what, byte by byte ------------------------------------------

const T22 = new PublicKey(SOLANA.programs.token_2022);
const raw = (programId, data, keys = []) => new TransactionInstruction({ programId, data: Buffer.from(data),
  keys: keys.map(k => ({ pubkey: k, isSigner: false, isWritable: true })) });

test('edges: only exact transfers, closes and account opens are read as such', () => {
  const vaultUsdc = new PublicKey(ata(VAULT, USDC)), ownerUsdc = new PublicKey(ata(OWNER, USDC));
  const all = (ixs) => decodeSol(wire([ownerExec(ixs)]), { ...base, kind: 'withdraw', mint: null, raw: 'all' }, PINS);
  // a Token-2022 mint's transfer, account close and the owner's account: read, checked, named by its address
  const m22 = key(), src22 = new PublicKey(ata(VAULT, m22, T22)), dst22 = new PublicKey(ata(OWNER, m22, T22));
  const r = all([spl.createTransferCheckedInstruction(src22, m22, dst22, VAULT, 7n, 0, [], T22), spl.createCloseAccountInstruction(src22, OWNER, VAULT, [], T22)]);
  ok(r);
  assert.match(r.lines.join('\n'), new RegExp(`Send 7 ${m22.toBase58().slice(0, 4)}…`), 'an unknown mint by its address');
  // the same bytes with one more, or another instruction of the token program, or another program: not a transfer
  const checked = spl.createTransferCheckedInstruction(vaultUsdc, USDC, ownerUsdc, VAULT, 7n, 6);
  for (const [what, ix] of [
    ['a longer TransferChecked', raw(spl.TOKEN_PROGRAM_ID, Buffer.concat([checked.data, Buffer.from([0])]), checked.keys.map(k => k.pubkey))],
    ['a plain Transfer (3)', spl.createTransferInstruction(vaultUsdc, ownerUsdc, VAULT, 7n)],
    ['TransferChecked bytes on another program', raw(STRANGER, checked.data, checked.keys.map(k => k.pubkey))],
    ['a longer CloseAccount', raw(spl.TOKEN_PROGRAM_ID, [9, 0], [vaultUsdc, OWNER, VAULT])],
    ['a SyncNative', raw(spl.TOKEN_PROGRAM_ID, [17], [vaultUsdc])],
    ['a SOL transfer with seed', SystemProgram.transfer({ fromPubkey: VAULT, basePubkey: VAULT, toPubkey: OWNER, lamports: 5, seed: 's', programId: SystemProgram.programId })],
    ['a longer SOL transfer', raw(SystemProgram.programId, Buffer.concat([transfer(VAULT, OWNER, 5).data, Buffer.from([0])]), [VAULT, OWNER])],
    ['transfer bytes on another program', raw(STRANGER, transfer(VAULT, OWNER, 5).data, [VAULT, OWNER])],
  ]) refused(all([ix]), /does not need|cannot be read/, what);
  // an account open other than create / create-idempotent (2: recover nested), or on another program
  const open = spl.createAssociatedTokenAccountIdempotentInstruction(OWNER, ownerUsdc, OWNER, USDC);
  const okOpen = decodeSol(wire([open, ownerExec([checked])]), { ...base, kind: 'withdraw', mint: USDC.toBase58(), raw: '7' }, PINS);
  ok(okOpen);
  assert.match(okOpen.lines[0], /Open the USDC account of your wallet .* if missing \(rent from your wallet/);
  for (const [what, ix] of [['recover nested (2)', raw(open.programId, [2], open.keys.map(k => k.pubkey))],
    ['create bytes on another program', raw(STRANGER, open.data, open.keys.map(k => k.pubkey))]]) {
    refused(decodeSol(wire([ix, ownerExec([checked])]), { ...base, kind: 'withdraw', mint: USDC.toBase58(), raw: '7' }, PINS), /does not need|cannot be read/, what);
  }
  ok(decodeSol(wire([raw(open.programId, [], open.keys.map(k => k.pubkey)), ownerExec([checked])]), { ...base, kind: 'withdraw', mint: USDC.toBase58(), raw: '7' }, PINS),
    'the older create (no data) reads as an open too');
});

test('edges: sizes, the priority fee at its limit, inner accounts out of range, policy calls, other signers', () => {
  const e = { ...base, kind: 'deposit', mint: SOLANA.native_mint, raw: '1' };
  refused(decodeSol(Buffer.alloc(1233, 1).toString('base64'), e, PINS), /1233 bytes, over the 1232/);
  const at = decodeSol(Buffer.alloc(1232, 1).toString('base64'), e, PINS);
  refused(at, /cannot be read/, '1232 bytes is a size a transaction may have: read, not too long');
  // the fee: limit x price, at most MAX_PRIORITY_LAMPORTS
  const fee = (units, micro) => decodeSol(wire([ComputeBudgetProgram.setComputeUnitLimit({ units }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: micro }),
    transfer(OWNER, VAULT, 1)]), e, PINS);
  ok(fee(1_000_000, 1_000_000));
  assert.match(fee(1_000_000, 1_000_000).lines[0], /Priority fee: at most 0\.001 SOL/);
  refused(fee(1_000_000, 1_000_001), /priority fee/);
  refused(decodeSol(wire([raw(ComputeBudgetProgram.programId, [9]), transfer(OWNER, VAULT, 1)]), e, PINS), /unknown compute budget/);
  // an inner account index past the accounts the call names
  const call = ownerExec([transfer(VAULT, OWNER, 5)]);
  const data = Buffer.from(call.data);
  const inner = 8 + 1 + 1 + 1 + 4;                      // discriminator, account index, signers, payload, length
  const n = call.keys.length - 3;                        // the metas after settings, program, the owner
  for (const [off, what] of [[inner + 1, 'the program'], [inner + 3, 'an account']]) {
    const bad = Buffer.from(data); bad[off] = n;
    refused(decodeSol(wire([raw(sq.PROGRAM_ID, bad, call.keys.map(k => k.pubkey))]), { ...base, kind: 'withdraw', mint: WSOL.toBase58(), raw: '5' }, PINS),
      /an inner account out of range/, what);
    const edge = Buffer.from(data); edge[off] = n - 1;  // the last account: in range
    const r = decodeSol(wire([raw(sq.PROGRAM_ID, edge, call.keys.map(k => k.pubkey))]), { ...base, kind: 'withdraw', mint: WSOL.toBase58(), raw: '5' }, PINS);
    assert.doesNotMatch(r.problems.join(), /out of range/, what);
  }
  // a Squads instruction that is no execute (a settings change) in a withdrawal: an instruction it does not need
  const settings = policyTx(OWNER)[0];
  refused(decodeSol(wire([settings]), { ...base, kind: 'withdraw', mint: WSOL.toBase58(), raw: '5' }, PINS), /does not need/);
  // the vault account another than 0
  const idx1 = Buffer.from(data); idx1[8] = 1;
  refused(decodeSol(wire([raw(sq.PROGRAM_ID, idx1, call.keys.map(k => k.pubkey))]), { ...base, kind: 'withdraw', mint: WSOL.toBase58(), raw: '5' }, PINS), /vault account 1/);
});

test('edges: deposits, withdrawals and closes refuse every other account, payer, wallet or mint', () => {
  const src = new PublicKey(ata(OWNER, USDC)), dst = new PublicKey(ata(VAULT, USDC));
  const e = { ...base, kind: 'deposit', mint: USDC.toBase58(), raw: '10' };
  const open = (payer, account, wallet, mint) => spl.createAssociatedTokenAccountIdempotentInstruction(payer, account, wallet, mint);
  const send = (s = src, d = dst, auth = OWNER, mint = USDC) => spl.createTransferCheckedInstruction(s, mint, d, auth, 10n, 6);
  ok(decodeSol(wire([send()]), e, PINS), 'no open: the vault has the account');
  for (const [what, ixs, re] of [
    ['two sends', [send(), send()], /one transfer/], ['two opens', [open(OWNER, dst, VAULT, USDC), open(OWNER, dst, VAULT, USDC), send()], /one transfer/],
    ['an open for another wallet', [open(OWNER, dst, STRANGER, USDC), send()], /not your vault's/],
    ['an open of another mint', [open(OWNER, dst, VAULT, WSOL), send()], /not your vault's/],
    ['an open of another account', [open(OWNER, new PublicKey(ata(STRANGER, USDC)), VAULT, USDC), send()], /not your vault's/],
    ['an open paid by someone else', [open(STRANGER, dst, VAULT, USDC), send()], /not your vault's/],
    ['tokens from another account', [send(new PublicKey(ata(STRANGER, USDC)))], /do not come from your wallet/],
    ['tokens under another authority', [send(src, dst, STRANGER)], /do not come from your wallet/],
    ['another mint', [send(src, dst, OWNER, WSOL)], /another token/],
    ['a SOL send for a token deposit', [transfer(OWNER, VAULT, 10)], /one transfer/],
  ]) refused(decodeSol(wire(ixs), e, PINS), re, what);
  refused(decodeSol(wire([transfer(OWNER, VAULT, 5), transfer(OWNER, VAULT, 5)]), { ...base, kind: 'deposit', mint: WSOL.toBase58(), raw: '10' }, PINS), /one transfer/);
  refused(decodeSol(wire([send()]), { ...base, kind: 'deposit', mint: WSOL.toBase58(), raw: '10' }, PINS), /one transfer/);
  // a withdrawal: the owner's account opened by the owner, from the vault's account under the vault
  const w = { ...base, kind: 'withdraw', mint: USDC.toBase58(), raw: '10' };
  const vsrc = new PublicKey(ata(VAULT, USDC)), odst = new PublicKey(ata(OWNER, USDC));
  const out = (s = vsrc, auth = VAULT) => ownerExec([spl.createTransferCheckedInstruction(s, USDC, odst, auth, 10n, 6)]);
  ok(decodeSol(wire([open(OWNER, odst, OWNER, USDC), out()]), w, PINS));
  refused(decodeSol(wire([open(OWNER, new PublicKey(ata(STRANGER, USDC)), STRANGER, USDC), out()]), w, PINS), /not your wallet's/, 'an open for another wallet');
  refused(decodeSol(wire([open(STRANGER, odst, OWNER, USDC), out()]), w, PINS), /not your wallet's|signatures of/, 'an open paid by another');
  refused(decodeSol(wire([out(new PublicKey(ata(STRANGER, USDC)))]), w, PINS), /not your vault's/, 'from another account');
  refused(decodeSol(wire([out(vsrc, STRANGER)]), w, PINS), /not your vault's|signatures of/, 'under another authority');
  refused(decodeSol(wire([open(OWNER, odst, OWNER, USDC)]), w, PINS), /withdraws nothing/, 'an open alone');
  // a close: outside the vault only its accounts opened by the owner; inside only the vault's
  const c = { ...base, kind: 'close' };
  const meteora = (receiver) => {
    const rule = instructionsOf('meteora-dlmm').find(x => x.name === 'close_position2');
    const keys = Array.from({ length: 8 }, () => key());
    for (const p of rule.pins) keys[p.index] = p.role === 'vaultOrPayer' ? receiver : p.role === 'vault' ? VAULT : new PublicKey(POOLS['meteora-dlmm'][0]);
    return raw(new PublicKey(RULES.venues['meteora-dlmm'].program), Buffer.concat([Buffer.from(rule.discriminator, 'hex'), Buffer.alloc(8)]), keys);
  };
  const r = decodeSol(wire([ownerExec([meteora(VAULT)])]), c, PINS);
  ok(r);
  assert.match(r.lines.join('\n'), /Meteora: close_position2/);
  refused(decodeSol(wire([ownerExec([meteora(PAYER)])]), c, PINS), /vaultOrPayer/, 'the rent to the bot: never on an owner\'s close');
  const vOpen = open(OWNER, new PublicKey(ata(VAULT, WSOL)), VAULT, WSOL);
  ok(decodeSol(wire([vOpen, ownerExec([meteora(VAULT)])]), c, PINS));
  for (const [what, ixs] of [
    ['an outside transfer', [transfer(OWNER, STRANGER, 1), ownerExec([meteora(VAULT)])]],
    ['an outside open for another wallet', [open(OWNER, new PublicKey(ata(STRANGER, WSOL)), STRANGER, WSOL), ownerExec([meteora(VAULT)])]],
    ['an outside open paid by another', [open(STRANGER, new PublicKey(ata(VAULT, WSOL)), VAULT, WSOL), ownerExec([meteora(VAULT)])]],
    ['an inside open for another wallet', [ownerExec([open(VAULT, new PublicKey(ata(STRANGER, WSOL)), STRANGER, WSOL), meteora(VAULT)])]],
  ]) refused(decodeSol(wire(ixs), c, PINS), /outside the vault|not your vault's|signatures of/, what);
});

test('edges: a policy update with hooks, limits, another account, threshold, time lock or expiry, or other actions, is refused', () => {
  const policy = sq.encodeProgramInteraction(compile(VENUE, { vault: VAULT, payer: PAYER, pools: POOLS[VENUE].map(p => new PublicKey(p)),
    payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies[0].policy);
  const [ix] = policyTx(POLICY_KEY);
  const data = Buffer.from(ix.data);
  const at = data.indexOf(policy);
  assert.ok(at > 0);
  const tail = at + policy.length;                       // after the policy: the expiry option, then the memo option
  const patched = (fn) => { const d = Buffer.from(data); fn(d); return raw(ix.programId, d, ix.keys.map(k => k.pubkey)); };
  const splice = (offset, remove, add) => { const d = Buffer.concat([data.subarray(0, offset), add, data.subarray(offset + remove)]); return raw(ix.programId, d, ix.keys.map(k => k.pubkey)); };
  const e = vaultExpect('approve');
  ok(decodeSol(wire([ix]), e, PINS));
  // the hooks (two options) and the spending limits (a u32 count) end the policy
  const pre = policy.length - 4 - 2, keyBytes = STRANGER.toBuffer();
  for (const [what, tx, re] of [
    ['a pre hook', splice(at + pre, 1, Buffer.concat([Buffer.from([1]), keyBytes])), /hooks/],
    ['a post hook', splice(at + pre + 1, 1, Buffer.concat([Buffer.from([1]), keyBytes])), /hooks/],
    ['a spending limit', patched(d => { d.writeUInt32LE(1, at + pre + 2); }), /spending limits|cannot be read/],
    ['another vault account', patched(d => { d[at] = 1; }), /another vault account|differs/],
    ['a threshold of 2', patched(d => { d.writeUInt16LE(2, at - 1 - 4 - 2); }), /threshold/],
    ['a time lock', patched(d => { d.writeUInt32LE(60, at - 1 - 4); }), /time lock/],
    ['an expiry', splice(tail, 1, Buffer.concat([Buffer.from([1]), Buffer.from([5])])), /expiry|cannot be read/],
  ]) refused(decodeSol(wire([tx]), e, PINS), re, what);
  // signed by two, or one action that is no policy update, or a Squads call beside it
  const two = sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER, STRANGER], rentPayer: OWNER, extra: [new PublicKey(POLICIES[0])],
    actions: [{ kind: 'policyUpdate', address: new PublicKey(POLICIES[0]), policy: compile(VENUE, { vault: VAULT, payer: PAYER,
      pools: POOLS[VENUE].map(p => new PublicKey(p)), payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies[0].policy, signers: [{ key: POLICY_KEY }] }] });
  refused(decodeSol(wire([two]), e, PINS), /your wallet alone|signatures of/);
  const remove = sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER], rentPayer: OWNER, actions: [{ kind: 'removeSigner', key: OWNER }] });
  refused(decodeSol(wire([remove]), e, PINS), /changes your vault's signers/);
  refused(decodeSol(wire([ix, transfer(OWNER, STRANGER, 1)]), e, PINS), /more than update/);
  const rentByOther = sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER], rentPayer: STRANGER, extra: [new PublicKey(POLICIES[0])], actions: [] });
  refused(decodeSol(wire([rentByOther]), e, PINS), /rent is paid by someone else|signatures of/);
  // the lines name the payout accounts by whose they are
  const last = POLICIES.length - 1;
  const lines = decodeSol(wire(policyTx(POLICY_KEY, last)), e, PINS).lines.join('\n');
  const short = (k) => `${k.slice(0, 4)}…${k.slice(-4)}`;
  assert.match(lines, new RegExp(`your profit wallet's USDC account \\(${short(ata(PROFIT, USDC))}\\)`));
  assert.match(lines, new RegExp(`the operator's fee share's USDC account \\(${short(ata(OPERATOR, USDC))}\\)`));
  assert.match(lines, /Token: pay out \(TransferChecked to a payout wallet only\)/);
  assert.match(lines, /Token: open a token account for the vault/, 'a shared rule by its name');
});

test('approve: a venue staged on the vault: its new policies at the vault\'s next seeds, exactly the rules; never in a revoke', () => {
  const compiled = compile(VENUE, { vault: VAULT, payer: PAYER, pools: POOLS[VENUE].map(p => new PublicKey(p)),
    payouts: payoutWallets.map(w => new PublicKey(ata(w, USDC))) }).policies;
  const seeds = compiled.map((_, i) => 5n + BigInt(i));
  const staged = seeds.map(s => sq.policyPda(SETTINGS, s).toBase58());
  const e = (kind) => ({ ...base, kind, policies: { [VENUE]: staged }, pools: { [VENUE]: POOLS[VENUE] }, payoutWallets });
  const create = (i, signer = POLICY_KEY, policy = compiled[i].policy, seed = seeds[i]) => [sq.settingsSyncIx({ settings: SETTINGS, signers: [OWNER], rentPayer: OWNER,
    extra: [sq.policyPda(SETTINGS, seed)], actions: [{ kind: 'policyCreate', seed, policy, signers: [{ key: signer }] }] })];
  for (let i = 0; i < compiled.length; i++) {
    const r = decodeSol(wire(create(i)), e('approve'), PINS);
    ok(r);
    assert.match(r.lines[0], /^New policy .*exactly the rules in git/);
  }
  refused(decodeSol(wire(create(0, OWNER)), e('revoke'), PINS), /a revoke creates no policy/);
  // a start or an expiry on a new policy: the rules set none
  for (const [back, re] of [[3, /a policy with a start/], [2, /a policy with an expiry/]]) {
    const [ix] = create(0);
    const d = Buffer.from(ix.data); d[d.length - back] = 1;
    refused(decodeSol(wire([new TransactionInstruction({ programId: ix.programId, keys: ix.keys, data: d })]), e('approve'), PINS), re);
  }
  refused(decodeSol(wire(create(0, STRANGER)), e('approve'), PINS), /not the bot's rules key/);
  refused(decodeSol(wire(create(0, POLICY_KEY, compiled[0].policy, 99n)), e('approve'), PINS), /not one of your vault's/, 'another seed');
  const last = compiled.length - 1;
  const p = compiled[last].policy;
  const wider = { ...p, constraints: [...p.constraints, p.constraints[0]] };
  refused(decodeSol(wire(create(last, POLICY_KEY, wider)), e('approve'), PINS), /differs from the rules in git/);
});

test('setup edges: the creator, its programs and memo; a policy\'s signers, threshold, time lock, hooks, limits, vault account; look-alikes', () => {
  const e = setupExpect();
  const p = ownerSetupPlan({ owner: OWNER, treasury: TREASURY, seed: SEED, policyKey: POLICY_KEY, venues: setupCompiled(), memo: 'lp-bot vault' });
  const steps = p.steps, fund = [...solDeposit(), transfer(OWNER, PAYER, GAS)];
  const sets = (first, mid = steps[1], last = steps[2]) => setupSet({ groups: [[first], [mid], [last, ...fund]] });
  const lines = decodeSolSet(sets(steps[0]), e, PINS);
  ok(lines);
  assert.match(lines.lines.join('\n'), /Create your vault .*threshold 1 \(memo "lp-bot vault"\); rent from your wallet/);
  assert.doesNotMatch(decodeSolSet(setupSet(), e, PINS).lines.join('\n'), /\(memo/, 'no memo, none shown');
  // the creator: another key that does not sign is still not your wallet
  const other = (i, k) => new TransactionInstruction({ programId: steps[0].programId, data: steps[0].data,
    keys: steps[0].keys.map((x, j) => (j === i ? { ...x, ...k } : x)) });
  refused(decodeSolSet(sets(other(2, { pubkey: STRANGER, isSigner: false })), e, PINS), /the creator is not your wallet/);
  refused(decodeSolSet(sets(other(2, { pubkey: STRANGER, isSigner: true })), e, PINS), /the creator is not your wallet/, 'another key that signs');
  refused(decodeSolSet(sets(other(3, { pubkey: STRANGER })), e, PINS), /another program than Squads and System/);
  refused(decodeSolSet(sets(other(4, { pubkey: STRANGER })), e, PINS), /another program than Squads and System/);
  // a policy: a second signer of the change, the threshold, the time lock, hooks, limits, another vault account
  const twoSigners = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [OWNER, STRANGER], rentPayer: OWNER, extra: [sq.policyPda(SETUP_SETTINGS, 1)],
    actions: [{ kind: 'policyCreate', seed: 1, policy: setupCompiled()[0].policies[0].policy, signers: [{ key: POLICY_KEY }] }] });
  refused(decodeSolSet(sets(steps[0], twoSigners), e, PINS), /not signed by your wallet alone/);
  const patched = (at, value) => {
    const d = Buffer.from(steps[1].data);
    d[at < 0 ? d.length + at : at] = value;
    return new TransactionInstruction({ programId: steps[1].programId, keys: steps[1].keys, data: d });
  };
  refused(decodeSolSet(sets(steps[0], patched(-9, 2)), e, PINS), /a threshold or time lock other than the rules'/, 'threshold 2');
  refused(decodeSolSet(sets(steps[0], patched(-7, 60)), e, PINS), /a threshold or time lock other than the rules'/, 'a time lock');
  refused(decodeSolSet(sets(steps[0], patched(-50, 1)), e, PINS), /hooks, spending limits or another vault account/, 'a spending limit');
  refused(decodeSolSet(sets(steps[0], patched(23, 1)), e, PINS), /hooks, spending limits or another vault account/, 'another vault account');
  // a look-alike of the last policy from another program goes with the money: never read as a policy
  const alike = new TransactionInstruction({ programId: STRANGER, keys: steps[2].keys, data: steps[2].data });
  const r = decodeSolSet(setupSet({ groups: [[steps[0]], [steps[1]], [steps[2]], [alike, ...fund]] }), e, PINS);
  refused(r, /one transfer/);
  assert.doesNotMatch(r.problems.join(' | '), /out of order/);
  // no policy at all: the set ends at none
  refused(decodeSolSet(setupSet({ groups: [[steps[0], ...fund]] }), e, PINS), /the set ends at policy none, not 2/);
  // a payout wallet beside yours that is not the operator's; a venue or pool the rules do not approve
  refused(decodeSolSet(setupSet(), setupExpect({ payoutWallets: [OWNER.toBase58(), STRANGER.toBase58()] }), PINS), /not the operator's/);
  refused(decodeSolSet(setupSet(), setupExpect({ pools: { [VENUE]: [STRANGER.toBase58()] } }), PINS), /not the raydium-clmm pool/);
  refused(decodeSolSet(setupSet(), setupExpect({ pools: { [VENUE]: [] } }), PINS), /no pool for raydium-clmm/);
  refused(decodeSolSet(setupSet(), setupExpect({ venues: [] }), PINS), /no venue for the bot/);
  refused(decodeSolSet(setupSet(), setupExpect({ venues: ['nowhere'] }), PINS), /nowhere has no rules in git/);
  refused(decodeSolSet(setupSet(), setupExpect({ vault: VAULT.toBase58() }), PINS), /not the account of these settings/);
});

// --- the set-up with the page's one-time set-up key (a hardware wallet signs 930 bytes at most) ---

const KEY = Keypair.generate().publicKey, KEY2 = key();
const keyExpect = (over = {}) => setupExpect({ setupKey: KEY.toBase58(), ...over });
const keyPlan = (over = {}) => ownerSetupPlan({ owner: OWNER, treasury: TREASURY, seed: SEED, policyKey: POLICY_KEY, venues: setupCompiled(), memo: 'lp-bot vault',
  setupKey: KEY, ...over });
const keyFund = (lamports = 20_000_000n) => transfer(OWNER, KEY, lamports);
// the set as solana.mjs setupTxs builds it, in the order the page sends it: yours, the key's, the funding
function keySet({ plan = keyPlan(), grant = null, keyTxs = null, fund = [...solDeposit(), transfer(OWNER, PAYER, GAS)] } = {}) {
  const owner = grant ?? [plan.steps[0], keyFund()];
  const policies = keyTxs ?? plan.steps.slice(1).map(ix => [ix]);
  return [...(owner.length ? [wire(owner)] : []), ...policies.map(ixs => wire(ixs, KEY)), ...(fund.length ? [wire(fund)] : [])];
}

test('setup with a set-up key: your wallet signs the creation and the key\'s SOL, the key each policy, the funding last; each of yours fits a card', () => {
  const set = keySet();
  const r = decodeSolSet(set, keyExpect(), PINS);
  ok(r);
  const text = r.lines.join('\n');
  assert.match(text, /Create your vault .*you its owner, threshold 1; this page's one-time set-up key .* its second signer until the bot's rules are on it/);
  assert.match(text, /Send 0\.02 SOL from your wallet .* to this page's one-time set-up key .*: the rent of the bot's rules and the fees; what is left comes back to you/);
  assert.equal(text.match(/exactly the rules in git; signed and paid by the set-up key/g).length, setupCompiled()[0].policies.length);
  assert.match(text, /Funding, sent last, once the set-up key is off your vault: Send 0\.1186 SOL/);
  // a policy alone is longer than a Tangem card signs; what your wallet signs is not
  const len = (b64) => Transaction.from(Buffer.from(b64, 'base64')).compileMessage().serialize().length;
  assert.ok(set.slice(1, -1).some(t => len(t) > 930));
  assert.ok(len(set[0]) <= OWNER_MESSAGE_BYTES && len(set.at(-1)) <= OWNER_MESSAGE_BYTES);
  // a resume: the key put on the vault that exists (an older one taken off), the rest of the policies, the funding
  const p = keyPlan();
  const swap = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [OWNER], rentPayer: OWNER,
    actions: [{ kind: 'removeSigner', key: KEY2 }, { kind: 'addSigner', signer: { key: KEY } }] });
  ok(decodeSolSet(keySet({ grant: [swap, keyFund(10_000_000n)], keyTxs: [[p.steps[2]]] }), keyExpect({ created: false }), PINS));
  // the key on the vault already: no part of yours but the funding
  ok(decodeSolSet(keySet({ grant: [], keyTxs: [[p.steps[2]]] }), keyExpect(), PINS));
  // the key's last step: it takes itself off and sends its SOL back; then the funding
  const back = [p.release, transfer(KEY, OWNER, 1_234_567n)];
  const release = decodeSolSet([wire(back, KEY), wire([...solDeposit(), transfer(OWNER, PAYER, GAS)])], keyExpect(), PINS);
  ok(release);
  assert.match(release.lines.join('\n'), /The set-up key takes itself off your vault: you are its only owner again/);
  ok(decodeSolSet([wire(back, KEY)], keyExpect(), PINS), 'the funding on chain already: none');
  // an older key the owner takes off, the vault whole
  ok(decodeSolSet([wire([sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [OWNER], rentPayer: OWNER, actions: [{ kind: 'removeSigner', key: KEY2 }] })]),
    wire([...solDeposit(), transfer(OWNER, PAYER, GAS)])], keyExpect(), PINS));
});

test('setup with a set-up key: every way it could go wrong is refused', () => {
  const e = keyExpect(), p = keyPlan();
  const fund = [...solDeposit(), transfer(OWNER, PAYER, GAS)];
  const policies = p.steps.slice(1).map(ix => [ix]);
  // the creation names another second signer, or none, or the key with fewer rights
  const create = (signers) => sq.createSmartAccountIx({ creator: OWNER, treasury: TREASURY, seed: SEED, threshold: 1, memo: 'lp-bot vault', signers }).ix;
  for (const [signers, what] of [[[{ key: OWNER }, { key: STRANGER }], 'a stranger'], [[{ key: OWNER }], 'no key'], [[{ key: OWNER }, { key: KEY, permissions: 1 }], 'fewer rights'],
    [[{ key: KEY }, { key: OWNER }], 'the key first'], [[{ key: OWNER }, { key: KEY }, { key: STRANGER }], 'a third']]) {
    refused(decodeSolSet(keySet({ grant: [create(signers), keyFund()] }), e, PINS), /not your wallet and this page's set-up key/, what);
  }
  // the page's key is the bot's or your wallet
  refused(decodeSolSet(keySet(), keyExpect({ setupKey: PAYER.toBase58() }), PINS), /the set-up key is your wallet or a key of the bot/);
  // someone else signs a policy, or your wallet does (too long for a card), or the key's rent is paid by you
  refused(decodeSolSet([wire([p.steps[0], keyFund()]), ...policies.map(ixs => wire(ixs, STRANGER)), wire(fund)], e, PINS), /fee payer is .*not your wallet/);
  const byOwner = keyPlan({ setupKey: null });
  refused(decodeSolSet([wire([p.steps[0], keyFund()]), ...byOwner.steps.slice(1).map(ix => wire([ix])), wire(fund)], e, PINS),
    /with a set-up key, your wallet signs no policy|a hardware wallet signs no more than 900/);
  // more SOL to the key than its policies take; SOL to it with no policy; SOL to it from the key's part
  refused(decodeSolSet(keySet({ grant: [p.steps[0], keyFund(10n ** 9n)] }), e, PINS), /it sends the set-up key 1 SOL; 2 policies take no more than .*/);
  refused(decodeSolSet([wire([keyFund()]), wire(fund)], e, PINS), /it sends the set-up key 0\.02 SOL; 0 policies take no more than 0/);
  // the key moves money elsewhere, or back before it is off the vault, or takes you off
  refused(decodeSolSet(keySet({ keyTxs: [[p.steps[1], transfer(KEY, STRANGER, 5n)], [p.steps[2]]] }), e, PINS), /the set-up key may only send its SOL back to your wallet/);
  refused(decodeSolSet(keySet({ keyTxs: [[p.steps[1], transfer(KEY, OWNER, 5n)], [p.steps[2]]] }), e, PINS), /once it is off your vault/);
  const offOwner = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [KEY], rentPayer: KEY, actions: [{ kind: 'removeSigner', key: OWNER }] });
  refused(decodeSolSet(keySet({ keyTxs: [...policies, [offOwner]] }), e, PINS), /it takes your wallet off your vault/);
  const offOther = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [KEY], rentPayer: KEY, actions: [{ kind: 'removeSigner', key: KEY2 }] });
  refused(decodeSolSet(keySet({ keyTxs: [...policies, [offOther]] }), e, PINS), /the set-up key takes .* off your vault/);
  const addOther = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [KEY], rentPayer: KEY, actions: [{ kind: 'addSigner', signer: { key: STRANGER } }] });
  refused(decodeSolSet(keySet({ keyTxs: [...policies, [addOther]] }), e, PINS), /it adds the signer .* to your vault/);
  // your wallet puts another key on the vault
  const putOther = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [OWNER], rentPayer: OWNER, actions: [{ kind: 'addSigner', signer: { key: STRANGER } }] });
  refused(decodeSolSet(keySet({ grant: [putOther, keyFund()], keyTxs: [[p.steps[2]]] }), e, PINS), /not this page's set-up key/);
  // a policy after the key left; the key's part after the funding; your part after the key's
  refused(decodeSolSet(keySet({ keyTxs: [[p.steps[1]], [p.release], [p.steps[2]]] }), e, PINS), /a policy after the set-up key took itself off/);
  refused(decodeSolSet([wire([p.steps[0], keyFund()]), wire(fund), ...policies.map(ixs => wire(ixs, KEY))], e, PINS), /out of order|after the deposit/);
  refused(decodeSolSet([...keySet({ fund: [] }), wire([keyFund()]), wire(fund)], e, PINS), /SOL to the set-up key comes after the set-up key's part: out of order/);
  // the funding: the gas to someone else, or none and the deposit alone
  refused(decodeSolSet(keySet({ fund: [...solDeposit(), transfer(OWNER, STRANGER, GAS)] }), e, PINS), /0 payments to the bot's gas key/);
  // a policy the rules do not make, from the key
  const wrong = keyPlan({ venues: setupCompiled({ payouts: [new PublicKey(ata(STRANGER.toBase58(), USDC))] }) });
  refused(decodeSolSet(keySet({ keyTxs: wrong.steps.slice(1).map(ix => [ix]) }), e, PINS), /differs from the rules in git/);
  // a message of yours longer than a card signs
  const memo = sq.createSmartAccountIx({ creator: OWNER, treasury: TREASURY, seed: SEED, threshold: 1, memo: 'x'.repeat(700),
    signers: [{ key: OWNER }, { key: KEY }] }).ix;
  refused(decodeSolSet(keySet({ grant: [memo, keyFund()] }), e, PINS), /your wallet would sign \d+ bytes; a hardware wallet signs no more than 900/);
  // without the page's key in what it expects, a set the key signs is refused: only your wallet may sign
  refused(decodeSolSet(keySet(), setupExpect(), PINS), /fee payer is .*not your wallet/);
});

test('setup with a set-up key, edges: the order and who pays, the card\'s bound to the byte, the key only in a set-up\'s set', () => {
  const e = keyExpect(), p = keyPlan();
  const fund = [...solDeposit(), transfer(OWNER, PAYER, GAS)];
  const policies = p.steps.slice(1).map(ixs => wire([ixs], KEY));
  assert.match(decodeSolSet(keySet(), e, PINS).lines.join('\n'), /then it takes itself off \(memo "lp-bot vault"\); rent from your wallet/);
  // out of order: the key's part after the funding; the creation after the key's SOL
  refused(decodeSolSet([wire([p.steps[0], keyFund()]), wire(fund), ...policies], e, PINS), /a set-up key's transaction comes after the deposit: out of order/);
  refused(decodeSolSet([wire([keyFund()]), wire([p.steps[0]]), ...policies, wire(fund)], e, PINS), /the vault's creation is not the first step/);
  refused(decodeSolSet([wire([p.steps[0], keyFund()]), ...policies, wire([keyFund(1n)]), wire(fund)], e, PINS),
    /SOL to the set-up key comes after the set-up key's part: out of order/);
  // the key's policy with its rent from your wallet, or signed by someone else beside it
  const rentFromYou = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [KEY], rentPayer: OWNER, extra: [p.steps[1].keys[5].pubkey],
    actions: [{ kind: 'policyCreate', seed: 1, policy: setupCompiled()[0].policies[0].policy, signers: [{ key: POLICY_KEY }] }] });
  refused(decodeSolSet(keySet({ keyTxs: [[rentFromYou], [p.steps[2]]] }), e, PINS), /the rent is paid by someone else than the set-up key/);
  const byStranger = sq.settingsSyncIx({ settings: SETUP_SETTINGS, signers: [STRANGER], rentPayer: KEY, extra: [p.steps[1].keys[5].pubkey],
    actions: [{ kind: 'policyCreate', seed: 1, policy: setupCompiled()[0].policies[0].policy, signers: [{ key: POLICY_KEY }] }] });
  refused(decodeSolSet(keySet({ keyTxs: [[byStranger], [p.steps[2]]] }), e, PINS), /it is not signed by the set-up key alone/);
  // your wallet's part signed by the key as well: only one signer
  refused(decodeSolSet([wire([p.steps[0], keyFund(), transfer(KEY, OWNER, 1n)]), ...policies, wire(fund)], e, PINS), /only your wallet may sign/);
  // the card's bound, to the byte: a message of exactly OWNER_MESSAGE_BYTES passes, one more is refused
  const grantOf = (n) => [sq.createSmartAccountIx({ creator: OWNER, treasury: TREASURY, seed: SEED, threshold: 1, memo: 'm'.repeat(n),
    signers: [{ key: OWNER }, { key: KEY }] }).ix, keyFund()];
  const bytes = (n) => Transaction.from(Buffer.from(wire(grantOf(n)), 'base64')).compileMessage().serialize().length;
  let n = OWNER_MESSAGE_BYTES - bytes(0);
  while (bytes(n) > OWNER_MESSAGE_BYTES) n--;            // a longer memo's length takes a byte more
  assert.equal(bytes(n), OWNER_MESSAGE_BYTES);
  assert.equal(bytes(n + 1), OWNER_MESSAGE_BYTES + 1);
  const at = (k) => decodeSolSet(keySet({ grant: grantOf(k) }), e, PINS).problems.filter(x => /hardware wallet/.test(x));
  assert.deepEqual(at(n), [], 'exactly the bound');
  assert.equal(at(n + 1).length, 1, 'one byte more');
  // the key signs only in a set-up's set, never in another kind's
  refused(decodeSolSet([policies[0]], { ...vaultExpect('approve'), setupKey: KEY.toBase58() }, PINS), /fee payer is .*not your wallet/);
  // a kind no set has
  refused(decodeSolSet([wire(fund)], vaultExpect('withdraw'), PINS), /nothing to check a withdraw against in a set/);
});
