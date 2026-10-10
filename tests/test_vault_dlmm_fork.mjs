// A Meteora DLMM position run by a vault, end to end, on a local fork of
// mainnet: the deployed Squads and DLMM programs, the live SOL/USDC pool.
// The venue SDK builds for the vault as owner and the bot's key as payer;
// vault/account.mjs wraps what needs the vault through its policy.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { compile, NotAllowed } from '../chains/solana/vault/policy.mjs';
import { actingAccount, PolicyMismatch } from '../chains/solana/vault/account.mjs';
import { connectPlan, connectProblems, lookupPlan, staticAccounts } from '../chains/solana/vault/admin.mjs';
import { venue } from '../chains/solana/vault/venues.mjs';

const require = createRequire(import.meta.url);
const { PublicKey, Keypair, Transaction } = require('@solana/web3.js');
const { getAssociatedTokenAddressSync, NATIVE_MINT } = require('@solana/spl-token');
const dlmmPkg = require('@meteora-ag/dlmm');
const DLMM = dlmmPkg.default ?? dlmmPkg;
const { StrategyType } = dlmmPkg;
const BN = require('bn.js');

const DLMM_PROGRAM = new PublicKey('LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo');
const POOL = new PublicKey('BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y');        // SOL/USDC
const OTHER = new PublicKey('5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6');       // SOL/USDC, not approved
const USDC = new PublicKey('EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v');

async function vaultOnFork({ usdc = 200_000_000n } = {}) {
  const fork = new Fork();
  await Promise.all([fork.program(sq.PROGRAM_ID), fork.program(DLMM_PROGRAM)]);
  await fork.syncClock();
  await fork.clone([sq.programConfigPda()]);
  const config = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data);
  await fork.clone([config.treasury]);
  const owner = fork.wallet(1), payer = fork.wallet(1), policyKey = Keypair.generate();
  const seed = config.smartAccountIndex + 1n, vault = sq.vaultPda(sq.settingsPda(seed));
  const compiled = compile('meteora-dlmm', { vault, payer: payer.publicKey, pools: [POOL] });
  const plan = connectPlan({ creator: payer.publicKey, treasury: config.treasury, seed, owners: [owner.publicKey],
    threshold: 1, policyKey: policyKey.publicKey, compiled });
  for (const ixs of plan.txs) { const r = await fork.send(ixs, payer); assert.ok(r.ok, r.logs.join('\n')); assert.ok(r.size <= 1232); }
  assert.deepEqual(connectProblems(fork.get(plan.settings).data,
    { owners: [owner.publicKey], threshold: 1, botKeys: [payer.publicKey, policyKey.publicKey] }), []);
  const { settings, policies } = plan;
  // the owner's deposit: SOL and USDC
  fork.fund(vault, 2);
  await fork.clone([USDC]);
  fork.tokenAccount(getAssociatedTokenAddressSync(USDC, vault, true), USDC, vault, usdc);
  const keyFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'vault-')), 'policy.json');
  fs.writeFileSync(keyFile, JSON.stringify([...policyKey.secretKey]));
  const env = { LPBOT_VAULT_SETTINGS: settings.toBase58(), LPBOT_VAULT_POLICIES: policies.join(','),
    LPBOT_VAULT_POLICY_KEY: keyFile, LPBOT_VAULT_POOLS: POOL.toBase58() };
  const connection = fork.connection();
  // the vault's lookup table: the pool's accounts and the vault's own
  const dlmmVenue = venue('meteora-dlmm');
  const addresses = staticAccounts({ vault, policies, venue: dlmmVenue, pools: [await dlmmVenue.poolAccounts(connection, POOL)] });
  const lookup = lookupPlan({ payer: payer.publicKey, recentSlot: fork.recentSlot(), addresses });
  for (const ixs of lookup.txs) { const r = await fork.send(ixs, payer); assert.ok(r.ok, r.logs.join('\n')); assert.ok(r.size <= 1232); }
  fork.nextSlot();
  env.LPBOT_VAULT_LOOKUP = lookup.table.toBase58();
  const acting = await actingAccount({ connection, payer, venue: 'meteora-dlmm', env });
  return { fork, connection, acting, owner, payer, vault, settings, policies, env, policyKey };
}

// An outside trader swaps through the pool both ways, so positions in the
// active bins earn real fees.
async function trade(fork, dlmm) {
  const trader = fork.wallet(500);
  for (const [inToken, outToken, amount] of [[NATIVE_MINT, USDC, 200e9], [USDC, NATIVE_MINT, 20_000e6]]) {
    await dlmm.refetchStates();
    const swapForY = inToken.equals(NATIVE_MINT);
    const q = dlmm.swapQuote(new BN(amount), swapForY, new BN(500), await dlmm.getBinArrayForSwap(swapForY));
    const t = await dlmm.swap({ inToken, outToken, inAmount: new BN(amount), minOutAmount: new BN(0), lbPair: POOL,
      user: trader.publicKey, binArraysPubkey: q.binArraysPubkey });
    const r = await fork.send(t.instructions, trader);
    assert.ok(r.ok, r.logs.join('\n'));
  }
}

// Run what an SDK built, the way a signer will: wrapped, signed, sent.
async function run(fork, acting, ixs, extra = []) {
  const r = await fork.sendSigned(acting.transaction(ixs, fork.svm.latestBlockhash(), extra));
  assert.ok(r.size <= 1232);
  return r;
}

test('a vault opens, harvests, swaps on and closes a DLMM position', { timeout: 240_000 }, async () => {
  const { fork, connection, acting, vault, payer } = await vaultOnFork();
  assert.equal(acting.mode, 'vault');
  assert.ok(acting.owner.equals(vault));
  const wsol = getAssociatedTokenAddressSync(NATIVE_MINT, vault, true), usdc = getAssociatedTokenAddressSync(USDC, vault, true);
  const dlmm = await DLMM.create(connection, POOL);
  const active = dlmm.lbPair.activeId;
  const vaultSol0 = fork.balance(vault), payerSol0 = fork.balance(payer.publicKey);

  const built = await dlmm.initializeMultiplePositionAndAddLiquidityByStrategy2(
    async (n) => Array.from({ length: n }, () => Keypair.generate()),
    new BN(500_000_000), new BN(50_000_000), { minBinId: active - 8, maxBinId: active + 8, strategyType: StrategyType.Spot },
    vault, payer.publicKey, 1);
  for (const part of built.instructionsByPositions) {
    for (const ixs of part.transactionInstructions) {
      const r = await run(fork, acting, ixs, [part.positionKeypair]);
      assert.ok(r.ok, r.logs.join('\n'));
    }
  }
  const { userPositions } = await dlmm.getPositionsByUserAndLbPair(vault);
  assert.equal(userPositions.length, 1, 'one position, owned by the vault');
  assert.ok(fork.balance(vault) < vaultSol0 - 400_000_000, 'the SOL side came from the vault');
  assert.ok(fork.tokenAmount(usdc) < 200_000_000n, 'the USDC side came from the vault');
  assert.ok(payerSol0 - fork.balance(payer.publicKey) < 200_000_000, 'the payer paid fees and rent only');

  await trade(fork, dlmm);                            // the position earns fees
  const usdcPreHarvest = fork.tokenAmount(usdc);
  const claim = await dlmm.claimAllSwapFee({ owner: vault, positions: (await dlmm.getPositionsByUserAndLbPair(vault)).userPositions });
  assert.ok(claim.length > 0);
  for (const tx of claim) { const r = await run(fork, acting, tx.instructions); assert.ok(r.ok, r.logs.join('\n')); }
  assert.ok(fork.tokenAmount(usdc) > usdcPreHarvest, 'the fees were paid into the vault');

  // a swap on the approved pool, in and out of the vault's own accounts
  const usdcBefore = fork.tokenAmount(usdc);
  await dlmm.refetchStates();
  const arrays = await dlmm.getBinArrayForSwap(true);
  const quote = dlmm.swapQuote(new BN(50_000_000), true, new BN(100), arrays);
  const swap = await dlmm.swap({ inToken: NATIVE_MINT, outToken: USDC, inAmount: new BN(50_000_000), minOutAmount: quote.minOutAmount,
    lbPair: POOL, user: vault, binArraysPubkey: quote.binArraysPubkey });
  const rs = await run(fork, acting, swap.instructions);
  assert.ok(rs.ok, rs.logs.join('\n'));
  assert.ok(fork.tokenAmount(usdc) > usdcBefore, 'the swap paid USDC into the vault');

  // close: everything back to the vault, position rent back to the payer or vault
  const p = userPositions[0];
  const close = await dlmm.removeLiquidity({ user: vault, position: p.publicKey, fromBinId: p.positionData.lowerBinId,
    toBinId: p.positionData.upperBinId, bps: new BN(10_000), shouldClaimAndClose: true });
  for (const tx of close) { const r = await run(fork, acting, tx.instructions); assert.ok(r.ok, r.logs.join('\n')); }
  assert.equal((await dlmm.getPositionsByUserAndLbPair(vault)).userPositions.length, 0);
  assert.equal(fork.get(wsol), null, 'the WSOL account was closed back into the vault');
  assert.ok(fork.balance(vault) > vaultSol0 - 100_000_000, 'the SOL came back to the vault');
});

test('a swap paying out to someone else is refused by the program', { timeout: 240_000 }, async () => {
  const { fork, connection, acting, vault } = await vaultOnFork();
  const thief = Keypair.generate().publicKey, theirs = getAssociatedTokenAddressSync(USDC, thief);
  fork.tokenAccount(theirs, USDC, thief, 0n);
  const dlmm = await DLMM.create(connection, POOL);
  const quote = dlmm.swapQuote(new BN(10_000_000), true, new BN(100), await dlmm.getBinArrayForSwap(true));
  const swap = await dlmm.swap({ inToken: NATIVE_MINT, outToken: USDC, inAmount: new BN(10_000_000), minOutAmount: new BN(0),
    lbPair: POOL, user: vault, binArraysPubkey: quote.binArraysPubkey });
  // point the output at the thief's account, the only change
  const ixs = swap.instructions.map(ix => !ix.programId.equals(DLMM_PROGRAM) ? ix
    : { ...ix, keys: ix.keys.map((k, i) => i === 5 ? { ...k, pubkey: theirs } : k) });
  const r = await run(fork, acting, ixs);
  assert.ok(!r.ok);
  assert.match(r.logs.join('\n'), /ProgramInteractionInvalidNumericValue/);
  assert.equal(fork.tokenAmount(theirs), 0n);
});

test('a pool the owners did not approve is refused before anything is signed', { timeout: 240_000 }, async () => {
  const { connection, acting, vault, payer } = await vaultOnFork();
  const dlmm = await DLMM.create(connection, OTHER);
  const a = dlmm.lbPair.activeId;
  const built = await dlmm.initializeMultiplePositionAndAddLiquidityByStrategy2(
    async (n) => Array.from({ length: n }, () => Keypair.generate()),
    new BN(100_000_000), new BN(10_000_000), { minBinId: a - 3, maxBinId: a + 3, strategyType: StrategyType.Spot },
    vault, payer.publicKey, 1);
  assert.throws(() => acting.wrap(built.instructionsByPositions[0].transactionInstructions[0]), NotAllowed);
});

test('the bot stops when the policy on chain is not the one it was built for', { timeout: 240_000 }, async () => {
  const { fork, connection, payer, owner, settings, policies, env, policyKey } = await vaultOnFork();
  // the owners approve another pool: the bot's config still lists one
  const vault = sq.vaultPda(settings);
  const wider = compile('meteora-dlmm', { vault, payer: payer.publicKey, pools: [POOL, OTHER] });
  for (const [i, address] of policies.entries()) {
    const r = await fork.send([sq.settingsSyncIx({ settings, signers: [owner.publicKey], rentPayer: owner.publicKey, extra: [address],
      actions: [{ kind: 'policyUpdate', address, policy: wider.policies[i].policy, signers: [{ key: policyKey.publicKey }] }] })], owner);
    assert.ok(r.ok, r.logs.join('\n'));
  }
  await assert.rejects(actingAccount({ connection, payer, venue: 'meteora-dlmm', env }), PolicyMismatch);
  // with the config updated to match, it acts again
  await actingAccount({ connection, payer, venue: 'meteora-dlmm', env: { ...env, LPBOT_VAULT_POOLS: `${POOL},${OTHER}` } });
});

// The DLMM signer itself, called as the loop calls it, in vault mode: the
// same commands and output fields; the vault owns, the key pays.
test('the DLMM signer runs a vault with its usual commands', { timeout: 300_000 }, async () => {
  const { fork, connection, acting, vault, payer } = await vaultOnFork();
  const signer = await import('../venues/meteora_dlmm/signer.mjs');
  const deps = { urls: ['fork'], connectFn: async () => ({ connection, payer, acting }), sleep: async () => {} };
  const said = [];
  const log = console.log;
  const out = async (fn) => { said.length = 0; console.log = (x) => said.push(x); try { await fn(); } finally { console.log = log; }
    return JSON.parse(said.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null'); };
  process.env.LPBOT_POOL = POOL.toBase58();
  process.env.LPBOT_MAX_USD = '100000';

  const value = async () => {               // the vault's holdings in USDC at the pool price
    await dlmm.refetchStates();
    const px = Number((await dlmm.getActiveBin()).pricePerToken);
    return fork.balance(vault) / 1e9 * px + Number(fork.tokenAmount(getAssociatedTokenAddressSync(USDC, vault, true))) / 1e6;
  };
  const dlmm = await DLMM.create(connection, POOL);
  const before = await value();
  const b = await out(() => signer.balance(undefined, deps));
  assert.equal(b.owner, vault.toBase58());
  assert.equal(b.custody, 'vault');
  assert.equal(b.balanceB, 200);
  assert.ok(b.gasSol > 0.5 && b.sol > 1.9);

  const price = Number((await dlmm.getActiveBin()).pricePerToken);
  const opened = await out(() => signer.open(POOL.toBase58(), String(price * 0.99), String(price * 1.01), '1', '100', true, deps));
  assert.equal(opened.sent, true, JSON.stringify(opened));
  assert.ok(!opened.partial);

  const st = await out(() => signer.status(undefined, deps));
  assert.equal(st.positionMint, opened.positionMint);
  assert.equal(st.inRange, true);
  assert.ok(st.positionUsd > 0);

  await trade(fork, dlmm);
  const h = await out(() => signer.harvest(opened.positionMint, true, deps));
  assert.ok(h.harvested === opened.positionMint && !h.partial, JSON.stringify(h));

  const c = await out(() => signer.close(opened.positionMint, true, deps));
  assert.ok(c.closed === opened.positionMint && !c.partial, JSON.stringify(c));
  const after = await out(() => signer.status(undefined, deps));
  assert.equal(after.positions, 0);
  // the trades moved the price, so the mix changed; the value came home
  const back = await value();
  assert.ok(back > before * 0.97, `vault worth ${back.toFixed(2)} USDC after, ${before.toFixed(2)} before`);
  assert.equal(fork.get(getAssociatedTokenAddressSync(NATIVE_MINT, vault, true)), null, 'no WSOL left wrapped');
});

// The loop's swap, on the vault's own pool: a vault holding only SOL ends
// near half SOL, half USDC, worth what it was.
test('rebalance: the DLMM signer swaps a vault to its targets on the approved pool', { timeout: 300_000 }, async () => {
  const { fork, connection, acting, vault, payer } = await vaultOnFork({ usdc: 0n });
  const signer = await import('../venues/meteora_dlmm/signer.mjs');
  const deps = { urls: ['fork'], connectFn: async () => ({ connection, payer, acting }), sleep: async () => {} };
  process.env.LPBOT_POOL = POOL.toBase58();
  delete process.env.LPBOT_TOKEN_HINTS;
  const dlmm = await DLMM.create(connection, POOL);
  const px = Number((await dlmm.getActiveBin()).pricePerToken);
  const half = (2 - 0.005) * px / 2;
  const log = console.log; console.log = () => {};
  let out;
  try { out = await signer.rebalance(NATIVE_MINT.toBase58(), USDC.toBase58(), half.toFixed(2), half.toFixed(2), true, deps); }
  finally { console.log = log; }
  assert.equal(out.sent, true, JSON.stringify(out));
  assert.ok(!out.partial);
  const usdcNow = Number(fork.tokenAmount(getAssociatedTokenAddressSync(USDC, vault, true))) / 1e6;
  const solNow = fork.balance(vault) / 1e9;
  assert.ok(Math.abs(usdcNow - half) / half < 0.05, `USDC ${usdcNow} vs target ${half}`);
  assert.ok((solNow * px + usdcNow) > 2 * px * 0.985, 'the swap kept the value');
  assert.ok(solNow >= 0.005, 'the vault kept its floor');
});

test('rebalance: a price the pool does not pay is refused before anything is signed', { timeout: 300_000 }, async () => {
  const { connection, acting, payer } = await vaultOnFork({ usdc: 0n });
  const signer = await import('../venues/meteora_dlmm/signer.mjs');
  const deps = { urls: ['fork'], connectFn: async () => ({ connection, payer, acting }), sleep: async () => {} };
  process.env.LPBOT_POOL = POOL.toBase58();
  const dlmm = await DLMM.create(connection, POOL);
  const px = Number((await dlmm.getActiveBin()).pricePerToken);
  // the loop's hint says SOL is worth ten times the pool's price
  process.env.LPBOT_TOKEN_HINTS = JSON.stringify({ [NATIVE_MINT.toBase58()]: { usd: px * 10, decimals: 9 }, [USDC.toBase58()]: { usd: 1, decimals: 6 } });
  try {
    await assert.rejects(signer.rebalance(NATIVE_MINT.toBase58(), USDC.toBase58(), '50', '50', true, deps), /below fair value/);
  } finally { delete process.env.LPBOT_TOKEN_HINTS; }
});
