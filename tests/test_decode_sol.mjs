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
import { ata, decodeSol, expectedPolicies, MAX_PRIORITY_LAMPORTS } from '../onboard/decode_sol.mjs';
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

test('payment: the signed amount to the bot\'s gas key, from the wallet only', () => {
  const e = { ...base, kind: 'payment', raw: '145000000' };
  const r = decodeSol(wire([transfer(OWNER, PAYER, 145_000_000)]), e, PINS);
  ok(r);
  assert.match(r.lines[0], /Send 0\.145 SOL from your wallet .* to the bot's gas key/);
  refused(decodeSol(wire([transfer(OWNER, STRANGER, 145_000_000)]), e, PINS), /not the bot's gas key/, 'another recipient');
  refused(decodeSol(wire([transfer(OWNER, PAYER, 145_000_001)]), e, PINS), /you signed for/, 'another amount');
  refused(decodeSol(wire([transfer(OWNER, PAYER, 145_000_000), transfer(OWNER, STRANGER, 1)]), e, PINS), /one transfer/, 'a second transfer');
  refused(decodeSol(wire([transfer(STRANGER, PAYER, 145_000_000)]), e, PINS), /signatures of/, 'another signer');
  refused(decodeSol(wire([transfer(OWNER, PAYER, 145_000_000)], STRANGER), e, PINS), /fee payer/, 'another fee payer');
  fc.assert(fc.property(fc.bigInt({ min: 1n, max: 10n ** 12n }), (lamports) => {
    const r2 = decodeSol(wire([transfer(OWNER, PAYER, lamports)]), e, PINS);
    assert.equal(r2.ok, lamports === 145_000_000n);
  }), { numRuns: 200 });
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
  const e = { ...base, kind: 'payment', raw: '1' };
  refused(decodeSol('not base64 at all', e, PINS), /cannot be read/);
  const v0 = new VersionedTransaction(new TransactionMessage({ payerKey: OWNER, recentBlockhash: PublicKey.default.toBase58(),
    instructions: [transfer(OWNER, PAYER, 1)] }).compileToV0Message()).serialize();
  refused(decodeSol(Buffer.from(v0).toString('base64'), e, PINS), /cannot be read|version 0/);
  const pricey = [ComputeBudgetProgram.setComputeUnitLimit({ units: 1_400_000 }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 10_000_000 }), transfer(OWNER, PAYER, 1)];
  refused(decodeSol(wire(pricey), e, PINS), /priority fee/);
  const cheap = [ComputeBudgetProgram.setComputeUnitLimit({ units: 200_000 }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1000 }), transfer(OWNER, PAYER, 1)];
  ok(decodeSol(wire(cheap), e, PINS));
  assert.ok(MAX_PRIORITY_LAMPORTS <= 1_000_000n);
  const unknown = new TransactionInstruction({ programId: STRANGER, keys: [], data: Buffer.from([1, 2, 3]) });
  refused(decodeSol(wire([unknown]), e, PINS), /transfer/);
  refused(decodeSol(wire([transfer(OWNER, PAYER, 1)]), { ...base, kind: 'teleport' }, PINS), /nothing to check/);
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
  const e = { ...base, kind: 'payment', raw: '1' };
  refused(decodeSol(Buffer.alloc(1233, 1).toString('base64'), e, PINS), /1233 bytes, over the 1232/);
  const at = decodeSol(Buffer.alloc(1232, 1).toString('base64'), e, PINS);
  refused(at, /cannot be read/, '1232 bytes is a size a transaction may have: read, not too long');
  // the fee: limit x price, at most MAX_PRIORITY_LAMPORTS
  const fee = (units, micro) => decodeSol(wire([ComputeBudgetProgram.setComputeUnitLimit({ units }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: micro }),
    transfer(OWNER, PAYER, 1)]), e, PINS);
  ok(fee(1_000_000, 1_000_000));
  assert.match(fee(1_000_000, 1_000_000).lines[0], /Priority fee: at most 0\.001 SOL/);
  refused(fee(1_000_000, 1_000_001), /priority fee/);
  refused(decodeSol(wire([raw(ComputeBudgetProgram.programId, [9]), transfer(OWNER, PAYER, 1)]), e, PINS), /unknown compute budget/);
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
  refused(decodeSol(wire([remove]), e, PINS), /other than a policy update/);
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
  refused(decodeSol(wire(create(0, STRANGER)), e('approve'), PINS), /not the bot's rules key/);
  refused(decodeSol(wire(create(0, POLICY_KEY, compiled[0].policy, 99n)), e('approve'), PINS), /not one of your vault's/, 'another seed');
  const last = compiled.length - 1;
  const p = compiled[last].policy;
  const wider = { ...p, constraints: [...p.constraints, p.constraints[0]] };
  refused(decodeSol(wire(create(last, POLICY_KEY, wider)), e('approve'), PINS), /differs from the rules in git/);
});
