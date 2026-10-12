// One more venue on a vault that exists, end to end, on a local fork of
// mainnet: the deployed Squads, DLMM and Raydium programs and the two
// SOL/USDC pools test0 moves between (owner, 2026-10-12: "move test0 to
// raydium"). A vault connected by an older release with Meteora policies
// only (the old WSOL pin) holds a DLMM position; Raydium is staged on the
// same vault; the owner approves at /bot (onboard/solana.mjs ownerTxs, one
// transaction at a time as the page asks for them); the bot closes on
// Meteora, opens on Raydium, harvests; the owner withdraws and revokes.
// The WSOL pin is checked by the program itself, before and after.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import { keyDir } from './_vault.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { RULES, compile, vaultWsolAccount } from '../chains/solana/vault/policy.mjs';
import { actingAccount, AWAITS_OWNERS, PolicyMismatch } from '../chains/solana/vault/account.mjs';
import { connectPlan, lookupPlan, stagedPolicies, staticAccounts } from '../chains/solana/vault/admin.mjs';
import { request, rulesRequest, stageVenue } from '../chains/solana/vault/cli.mjs';
import { venue as venueOf } from '../chains/solana/vault/venues.mjs';
import * as S from '../onboard/solana.mjs';
import * as vaultIx from '../venues/raydium_clmm/vault.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, Keypair, SystemProgram, Transaction, Message } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const { Raydium, PoolUtils } = require('@raydium-io/raydium-sdk-v2');
const dlmmPkg = require('@meteora-ag/dlmm');
const DLMM = dlmmPkg.default ?? dlmmPkg;
const BN = require('bn.js');

const DLMM_POOL = new PublicKey(SOLANA.sol_usdc_pools['meteora-dlmm']);     // test0's pool today
const RAY_POOL = new PublicKey(SOLANA.sol_usdc_pools['raydium-clmm']);      // sol-usdc's, test0's next
const USDC = new PublicKey(SOLANA.usdc_mint);
const SOL = spl.NATIVE_MINT;
const TOKEN = new PublicKey(SOLANA.programs.token);
const WRAP = 'wrap SOL (transfer into a WSOL account of the vault)';

// The policies an older release compiled: the WSOL account checked by its
// owner field (bytes 32..64 = the vault) and its program, not by its key.
function olderRelease(compiled, vault) {
  const wsol = vaultWsolAccount(vault);
  const pin = { index: 1, owner: TOKEN, data: [{ offset: 32, type: 'bytes', value: vault.toBuffer(), op: 'eq' }] };
  for (const p of compiled.policies) {
    for (const c of p.policy.constraints) {
      const at = c.accounts.findIndex(a => a.keys?.length === 1 && a.keys[0].equals(wsol));
      if (at >= 0) c.accounts[at] = pin;              // the index entry shares this array
    }
  }
  return compiled;
}

// What a signer command printed: its one JSON object.
async function said(fn) {
  const lines = [], log = console.log;
  console.log = (x) => lines.push(x);
  try { await fn(); } finally { console.log = log; }
  return JSON.parse(lines.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null');
}

// `ixs` through the constraint the rule `name` compiled to, past the bot's
// own check: what a forged transaction gets from the program itself.
async function force(fork, acting, policyKey, ixs, name) {
  const p = acting.compiled.policies.findIndex(x => x.index.some(e => e.name === name));
  const c = acting.compiled.policies[p].index.findIndex(e => e.name === name);
  const call = sq.policyExecuteIx({ policy: acting.policies[p], signer: policyKey.publicKey, vault: acting.vault, ixs, indices: ixs.map(() => c) });
  return fork.sendSigned(acting.transaction([call], fork.svm.latestBlockhash(), [policyKey]));
}

// Accounts the bot's key could take SOL from, yet whose bytes 32..64 are the
// vault and whose program is SPL Token: a token account of the attacker's
// mint (its freeze authority is theirs) and an SPL Multisig.
function attackAccounts(fork, vault) {
  const tokenAccount = Keypair.generate().publicKey;
  fork.tokenAccount(tokenAccount, Keypair.generate().publicKey, vault, 0n);
  const multisig = Keypair.generate().publicKey;
  const data = Buffer.alloc(spl.MULTISIG_SIZE);
  data[0] = 1; data[1] = 1; data[2] = 1;
  vault.toBuffer().copy(data, 32);
  fork.set(multisig, { lamports: 3_000_000, data, owner: TOKEN });
  return [tokenAccount, multisig];
}

// An outside trader swaps through the Raydium pool both ways, so a position in range earns fees.
async function trade(fork, raydium) {
  const trader = fork.wallet(500);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, trader.publicKey), USDC, trader.publicKey, 50_000_000_000n);
  const pool = RAY_POOL.toBase58();
  const px = Number((await raydium.clmm.getPoolInfoFromRpc(pool)).poolInfo.price);
  for (const [mint, amount] of [[SOL, 20e9], [USDC, Math.round(20 * px * 1e6)]]) {
    const r = await raydium.clmm.getPoolInfoFromRpc(pool);
    const q = PoolUtils.computeAmountOut({ poolInfo: r.computePoolInfo, tickarrayBitmapExtension: r.computePoolInfo.exBitmapInfo,
      tickArrayCache: r.tickData[pool], baseMint: mint, epochInfo: { epoch: 0 }, amountIn: new BN(amount), slippage: 0.5,
      blockTimestamp: Math.floor(Date.now() / 1000), catchLiquidityInsufficient: true });
    const ixs = vaultIx.swapIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, owner: trader.publicKey, inputMint: mint,
      amountIn: new BN(amount), amountOutMin: new BN(0), remainingAccounts: q.remainingAccounts });
    const res = await fork.send(ixs, trader);
    assert.ok(res.ok, res.logs.join('\n'));
  }
}

// The owner's wallet signs one wire transaction from the server and sends it.
async function ownerSends(fork, b64, owner) {
  const tx = Transaction.from(Buffer.from(b64, 'base64'));
  tx.partialSign(owner);
  return fork.sendSigned(tx);
}

const awaiting = (e) => e instanceof PolicyMismatch && e.message.startsWith(AWAITS_OWNERS);

test('a Meteora-only vault adds Raydium: owner approves once, the bot closes on Meteora, opens on Raydium, harvests; '
  + 'the owner withdraws and revokes', { timeout: 900_000 }, async () => {
  const fork = new Fork();
  await Promise.all([fork.program(sq.PROGRAM_ID), fork.program(venueOf('meteora-dlmm').program), fork.program(venueOf('raydium-clmm').program)]);
  await fork.syncClock();
  await fork.clone([sq.programConfigPda()]);
  const config = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data);
  await fork.clone([config.treasury]);
  const owner = fork.wallet(0.002), payer = fork.wallet(1), policyKey = Keypair.generate(), profit = Keypair.generate();
  const seed = config.smartAccountIndex + 1n, settings = sq.settingsPda(seed), vault = sq.vaultPda(settings);
  const payouts = [spl.getAssociatedTokenAddressSync(USDC, profit.publicKey, true)];
  const connection = fork.connection();

  // --- the vault an older release connected: Meteora only, the old WSOL pin -----------------------
  const older = olderRelease(compile('meteora-dlmm', { vault, payer: payer.publicKey, pools: [DLMM_POOL], payouts }), vault);
  const plan = connectPlan({ creator: payer.publicKey, treasury: config.treasury, seed, owners: [owner.publicKey], threshold: 1,
    policyKey: policyKey.publicKey, venues: [older] });
  for (const ixs of plan.txs) { const r = await fork.send(ixs, payer); assert.ok(r.ok, r.logs.join('\n')); }
  const dlmmVenue = venueOf('meteora-dlmm');
  const lookup = lookupPlan({ payer: payer.publicKey, recentSlot: fork.recentSlot(),
    addresses: staticAccounts({ vault, policies: plan.policies, venues: [dlmmVenue], pools: [await dlmmVenue.poolAccounts(connection, DLMM_POOL)] }) });
  for (const ixs of lookup.txs) { const r = await fork.send(ixs, payer); assert.ok(r.ok, r.logs.join('\n')); }
  fork.nextSlot();
  const dir = keyDir('vault-add-venue-');                 // removed at exit (tests/_vault.mjs)
  const policyPath = path.join(dir, 'policy.json'), payerPath = path.join(dir, 'payer.json');
  fs.writeFileSync(policyPath, JSON.stringify([...policyKey.secretKey]));
  fs.writeFileSync(payerPath, JSON.stringify([...payer.secretKey]));
  process.env.WALLET_SECRET_PATH = payerPath;
  const envM = { LPBOT_VAULT_SETTINGS: settings.toBase58(), LPBOT_VAULT_POLICIES: plan.policies.join(','), LPBOT_VAULT_POLICY_KEY: policyPath,
    LPBOT_VAULT_POOLS: DLMM_POOL.toBase58(), LPBOT_VAULT_PAYOUTS: payouts.join(','), LPBOT_VAULT_LOOKUP: lookup.table.toBase58(),
    LPBOT_VAULT_VENUE: 'meteora-dlmm' };
  fork.fund(vault, 2);
  await fork.clone([USDC]);
  const vaultUsdc = spl.getAssociatedTokenAddressSync(USDC, vault, true);
  fork.tokenAccount(vaultUsdc, USDC, vault, 300_000_000n);

  // the old pin lets the bot's key send the vault's SOL to accounts it could take it from
  const olderActing = await actingAccount({ connection, payer, venue: 'meteora-dlmm', env: envM, compiled: older });
  for (const evil of attackAccounts(fork, vault)) {
    const before = fork.balance(evil);
    const r = await force(fork, olderActing, policyKey, [SystemProgram.transfer({ fromPubkey: vault, toPubkey: evil, lamports: 1_000_000 })], WRAP);
    assert.ok(r.ok, `the old pin passes ${evil.toBase58()}: ${r.logs.join('\n')}`);
    assert.equal(fork.balance(evil) - before, 1_000_000);
  }

  // the DLMM position the vault holds before the move, opened by the older release
  const dlmmSigner = await import('../venues/meteora_dlmm/signer.mjs');
  const dlmmDeps = (acting) => ({ urls: ['http://fork.test'], connectFn: async () => ({ connection, payer, acting }), sleep: async () => {} });
  process.env.LPBOT_POOL = DLMM_POOL.toBase58();
  process.env.LPBOT_MAX_USD = '100000';
  const dlmm = await DLMM.create(connection, DLMM_POOL);
  const dlmmPx = Number((await dlmm.getActiveBin()).pricePerToken);
  const held = await said(() => dlmmSigner.open(DLMM_POOL.toBase58(), String(dlmmPx * 0.99), String(dlmmPx * 1.01), '1', '100', true, dlmmDeps(olderActing)));
  assert.equal(held.sent, true, JSON.stringify(held));

  // --- this release: the Meteora policies differ (the WSOL pin), so the bot holds ------------------
  await assert.rejects(actingAccount({ connection, payer, venue: 'meteora-dlmm', env: envM }), awaiting);

  // --- staged: Raydium on the same vault, at its next policy seeds; the lookup table grows -----------
  const payerBeforeStage = fork.balance(payer.publicKey);
  const staged = await stageVenue({ venue: 'raydium-clmm', pools: RAY_POOL.toBase58(), execute: true }, { connection, env: envM, payer, log: () => {} });
  fork.nextSlot();
  const seedBefore = sq.decodeSettings(fork.get(settings).data).policySeed;
  assert.deepEqual(staged.policies, stagedPolicies(settings, seedBefore, 2).map(String));
  assert.ok(staged.sent && staged.lookupAdds > 0, JSON.stringify(staged));
  const stageGas = (payerBeforeStage - fork.balance(payer.publicKey)) / 1e9;
  console.log(`stage: ${staged.lookupAdds} lookup addresses, payer spent ${stageGas} SOL`);
  // staged again: the table has every account, nothing to add
  assert.equal((await stageVenue({ venue: 'raydium-clmm', pools: RAY_POOL.toBase58() }, { connection, env: envM, payer, log: () => {} })).lookupAdds, 0);
  await assert.rejects(stageVenue({ venue: 'meteora-dlmm', pools: DLMM_POOL.toBase58() }, { connection, env: envM, payer, log: () => {} }), /has it already/);
  const envR = { ...envM, LPBOT_VAULT_POLICIES: staged.policies.join(','), LPBOT_VAULT_POOLS: RAY_POOL.toBase58(), LPBOT_VAULT_VENUE: 'raydium-clmm' };
  await assert.rejects(actingAccount({ connection, payer, venue: 'raydium-clmm', env: envR }), awaiting);

  // --- the owner approves at /bot: one request over both venues, one transaction at a time ----------
  const row = { settings: settings.toBase58(), lookup: lookup.table.toBase58(), venues: {
    'meteora-dlmm': { policies: plan.policies.map(String), pools: [DLMM_POOL.toBase58()], payouts: payouts.map(String) },
    'raydium-clmm': { policies: staged.policies, pools: [RAY_POOL.toBase58()], payouts: payouts.map(String) } } };
  const keys = { payer, policyKey, policyPath };
  const ask = (action) => S.ownerTxs(connection, null, row, keys, { action, owner: owner.publicKey.toBase58() });
  const st0 = await S.status(connection, { tokens: {} }, row, keys);
  assert.equal(st0.botActive, true, 'a staged policy that does not exist yet is no revoke');
  assert.equal(st0.rulesCurrent, false, 'the page offers Approve bot rules');
  // the owner's wallet cannot pay two policies' rent yet: refused before it is asked to sign
  await assert.rejects(ask('approve'), (e) => /approving needs/.test(e.message));
  fork.fund(owner.publicKey, 0.05);
  const first = await ask('approve');
  const steps = (await rulesRequest('approve', { connection, settings, owner: owner.publicKey,
    payer: payer.publicKey, policyKey: policyKey.publicKey, venues: Object.entries(row.venues).map(([venue, v]) => ({ venue, ...v })) })).steps;
  assert.deepEqual(steps.map(s => `${s.kind} ${s.seed}`), ['update 1', 'update 2', 'update 3', 'create 4', 'create 5']);
  const need = await S.affordsRules(connection, owner.publicKey.toBase58(), steps);
  assert.equal(first.transactions.length, 5);
  assert.match(first.summary, /meteora-dlmm .* and raydium-clmm .*2 new policies, their rent paid by you/);
  const ownerBefore = fork.balance(owner.publicKey);
  for (let i = 0; i < first.transactions.length; i++) {
    const answer = await ask('approve');                 // the page asks for each one when its turn comes
    assert.equal(answer.transactions.length, first.transactions.length, 'the list does not shift as the owner signs');
    const r = await ownerSends(fork, answer.transactions[i], owner);
    assert.ok(r.ok && r.size <= 1232, r.logs.join('\n'));
    if (i === 2) {
      // the Meteora policies are this release's; Raydium is not approved yet: the move waits
      await actingAccount({ connection, payer, venue: 'meteora-dlmm', env: envM });
      await assert.rejects(actingAccount({ connection, payer, venue: 'raydium-clmm', env: envR }), awaiting);
    }
  }
  const ownerSpent = ownerBefore - fork.balance(owner.publicKey);
  const rents = staged.policies.map(p => fork.get(p));
  console.log(`approve: owner spent ${ownerSpent / 1e9} SOL; new policies ${rents.map(a => `${a.data.length} bytes ${Number(a.lamports) / 1e9} SOL`).join(', ')}`);
  const bound = Number(await connection.getMinimumBalanceForRentExemption(S.POLICY_ACCOUNT_BYTES));
  assert.ok(rents.every(a => Number(a.lamports) <= bound), `a policy's rent is over the estimate of ${bound} lamports`);
  const floor = await connection.getMinimumBalanceForRentExemption(0);
  assert.ok(BigInt(ownerSpent + floor) <= need, `the owner spent ${ownerSpent}; the check asked for ${need} with the floor`);
  assert.equal(sq.decodeSettings(fork.get(settings).data).policySeed, seedBefore + 2n);
  const actingM = await actingAccount({ connection, payer, venue: 'meteora-dlmm', env: envM });
  const actingR = await actingAccount({ connection, payer, venue: 'raydium-clmm', env: envR });
  assert.equal((await S.status(connection, { tokens: {} }, row, keys)).rulesCurrent, true);
  assert.equal(sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).smartAccountIndex, config.smartAccountIndex + 1n, 'one vault, never a second');

  // the new pin: the program refuses both attack accounts; the vault's own WSOL account passes
  for (const evil of attackAccounts(fork, vault)) {
    const r = await force(fork, actingM, policyKey, [SystemProgram.transfer({ fromPubkey: vault, toPubkey: evil, lamports: 1_000_000 })], WRAP);
    assert.ok(!r.ok, `the pinned rule passes ${evil.toBase58()}`);
  }
  const wsol = vaultWsolAccount(vault);
  assert.ok(wsol.equals(spl.getAssociatedTokenAddressSync(SOL, vault, true)));
  for (const acting of [actingM, actingR]) {
    const r = await fork.sendSigned(acting.transaction([spl.createAssociatedTokenAccountIdempotentInstruction(vault, wsol, vault, SOL),
      SystemProgram.transfer({ fromPubkey: vault, toPubkey: wsol, lamports: 10_000_000 }), spl.createSyncNativeInstruction(wsol),
      spl.createCloseAccountInstruction(wsol, vault, vault)], fork.svm.latestBlockhash()));
    assert.ok(r.ok, r.logs.join('\n'));
  }

  // --- the move: close on Meteora into the vault, open on Raydium --------------------------------------
  const closed = await said(() => dlmmSigner.close(held.positionMint, true, dlmmDeps(actingM)));
  assert.ok(closed.closed === held.positionMint && !closed.partial, JSON.stringify(closed));
  assert.equal((await said(() => dlmmSigner.status(undefined, dlmmDeps(actingM)))).positions, 0);
  const raySigner = await import('../venues/raydium_clmm/signer.mjs');
  const load = () => Raydium.load({ connection, owner: vault, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
  const rayDeps = { urls: ['http://fork.test'], connectFn: async () => ({ connection, payer, raydium: await load(), acting: actingR }), sleep: async () => {} };
  process.env.LPBOT_POOL = RAY_POOL.toBase58();
  process.env.LPBOT_RAYDIUM_LEAN = '1';
  try {
    const b = await said(() => raySigner.balance(undefined, rayDeps));
    assert.equal(b.owner, vault.toBase58());
    assert.ok(b.balanceB > 250, JSON.stringify(b));
    const opened = await said(() => raySigner.open(RAY_POOL.toBase58(), String(b.price * 0.99), String(b.price * 1.01), '0.5', '100', true, rayDeps));
    assert.equal(opened.sent, true, JSON.stringify(opened));
    const st = await said(() => raySigner.status(opened.positionMint, rayDeps));
    assert.ok(st.inRange && st.positionUsd > 50, JSON.stringify(st));

    await trade(fork, await load());
    const usdcPre = fork.tokenAmount(vaultUsdc);
    const h = await said(() => raySigner.harvest(opened.positionMint, true, rayDeps));
    assert.equal(h.harvested, opened.positionMint, JSON.stringify(h));
    assert.ok(fork.tokenAmount(vaultUsdc) > usdcPre, 'the Raydium fees came into the vault');
  } finally { delete process.env.LPBOT_RAYDIUM_LEAN; }

  // --- the owner withdraws, then revokes the bot on both venues ----------------------------------------
  const w = await request('withdraw', { owner: owner.publicKey.toBase58(), to: owner.publicKey.toBase58(), mint: USDC.toBase58(), amount: '10' },
    { connection, env: envR });
  for (const b64 of w.messages) {
    const tx = Transaction.populate(Message.from(Buffer.from(b64, 'base64')));
    tx.recentBlockhash = fork.svm.latestBlockhash();
    tx.sign(owner);
    const r = await fork.sendSigned(tx);
    assert.ok(r.ok, r.logs.join('\n'));
  }
  assert.equal(fork.tokenAmount(spl.getAssociatedTokenAddressSync(USDC, owner.publicKey)), 10_000_000n);
  const revoke = await ask('revoke');
  assert.equal(revoke.transactions.length, 5);
  for (let i = 0; i < revoke.transactions.length; i++) {
    const r = await ownerSends(fork, (await ask('revoke')).transactions[i], owner);     // a fresh blockhash each, as the page asks
    assert.ok(r.ok, r.logs.join('\n'));
  }
  for (const [venue, env] of [['meteora-dlmm', envM], ['raydium-clmm', envR]]) {
    await assert.rejects(actingAccount({ connection, payer, venue, env }), (e) => awaiting(e) && /revoked the bot/.test(e.message));
  }
  assert.equal((await S.status(connection, { tokens: {} }, row, keys)).botActive, false);

  // the rent bound of the approval check: every policy of every venue, for the largest
  // vault the config allows (3 pools, 2 payout accounts), as the program allocates it
  fork.fund(owner.publicKey, 1);
  const worst = { vault, payer: payer.publicKey, pools: [Keypair.generate().publicKey, Keypair.generate().publicKey, Keypair.generate().publicKey],
    payouts: [Keypair.generate().publicKey, Keypair.generate().publicKey] };
  const largest = [];
  for (const v of Object.keys(RULES.venues)) {
    for (const { policy } of compile(v, worst).policies) {
      const next = sq.decodeSettings(fork.get(settings).data).policySeed + 1n, address = sq.policyPda(settings, next);
      const r = await fork.send([sq.settingsSyncIx({ settings, signers: [owner.publicKey], rentPayer: owner.publicKey, extra: [address],
        actions: [{ kind: 'policyCreate', seed: next, policy, signers: [{ key: policyKey.publicKey }] }] })], owner);
      assert.ok(r.ok, r.logs.join('\n'));
      largest.push(fork.get(address).data.length);
    }
  }
  console.log(`policy accounts, largest vault: ${largest.join(', ')} bytes`);
  assert.ok(Math.max(...largest) <= S.POLICY_ACCOUNT_BYTES, `policy accounts of ${largest.join(', ')} bytes`);
});
