// An Orca Whirlpool position run by a vault, end to end, on a local fork of
// mainnet: the deployed Squads and Whirlpool programs, the live SOL/USDC pool.
// The signer's own commands, as the loop calls them, in vault mode; then the
// program's refusals of what the policy does not allow.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { connectedVault } from './_vault.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { NotAllowed } from '../chains/solana/vault/policy.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';
import { forVault, madeSigners, signWith } from '../venues/orca/vault.mjs';
import { kitToWeb3 } from '../venues/orca/swap.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, Keypair, TransactionMessage, VersionedTransaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const { openPositionInstructions, swapInstructions, harvestPositionInstructions, closePositionInstructions,
  increaseLiquidityInstructions, setNativeMintWrappingStrategy } = await import('@orca-so/whirlpools');
const { createNoopSigner, address } = await import('@solana/kit');
const { fetchWhirlpool } = await import('@orca-so/whirlpools-client');

const POOL = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE';      // SOL/USDC 0.04%
const OTHER = 'HJPjoWUrhoZzkNfRpHuieeFk9WcZWjwy6PBjZ81ngndJ';     // SOL/USDC 0.3%, not approved
const USDC = new PublicKey(SOLANA.usdc_mint);
const SOL = spl.NATIVE_MINT;
const WHIRLPOOL = new PublicKey(SOLANA.programs.orca_whirlpool);

async function setup({ usdc = 300_000_000n } = {}) {
  const v = await connectedVault({ venues: ['orca'], pools: [POOL] });
  const { fork, connection, env, payer } = v;
  const vault = new PublicKey(v.out.vault);
  fork.fund(vault, 2);
  await fork.clone([USDC]);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, vault, true), USDC, vault, usdc);
  const acting = await actingAccount({ connection, payer, venue: 'orca', env });
  const rpc = fork.kitRpc();
  const owner = createNoopSigner(address(vault.toBase58()));
  setNativeMintWrappingStrategy('ata');
  const deps = { urls: ['http://fork.test'], connectFn: async () => ({ rpc, connection, payer, acting, owner }), sleep: async () => {} };
  const signer = await import('../venues/orca/signer.mjs');
  process.env.LPBOT_POOL = POOL;
  process.env.LPBOT_MAX_USD = '100000';
  delete process.env.LPBOT_TOKEN_HINTS;
  return { ...v, vault, acting, rpc, owner, deps, signer };
}

async function said(fn) {
  const lines = [], log = console.log;
  console.log = (x) => lines.push(x);
  try { await fn(); } finally { console.log = log; }
  return JSON.parse(lines.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null');
}

const usdcOf = (fork, owner) => fork.tokenAmount(spl.getAssociatedTokenAddressSync(USDC, owner, true)) ?? 0n;
const keyOf = (k) => createNoopSigner(address(k.publicKey.toBase58()));

// An outside trader swaps through the pool both ways, so positions in range earn fees.
async function trade(fork, rpc) {
  const trader = fork.wallet(500);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, trader.publicKey), USDC, trader.publicKey, 50_000_000_000n);
  const w = (await fetchWhirlpool(rpc, address(POOL))).data;
  const px = (Number(w.sqrtPrice) / 2 ** 64) ** 2 * 1e3;
  // 100 SOL out, their worth in USDC back in: the price ends near where it began
  for (const [mint, amount] of [[SOL, 100_000_000_000n], [USDC, BigInt(Math.round(100 * px * 1e6))]]) {
    const s = await swapInstructions(rpc, { inputAmount: amount, mint: address(mint.toBase58()) }, address(POOL),
      { slippageToleranceBps: 5000, signer: keyOf(trader) });
    const r = await fork.send(s.instructions.map(kitToWeb3), trader);
    assert.ok(r.ok, r.logs.join('\n'));
  }
}

test('the Orca signer runs a vault: open, harvest, swap, close', { timeout: 600_000 }, async () => {
  const { fork, vault, payer, rpc, deps, signer } = await setup();
  const b = await said(() => signer.balance(POOL, deps));
  assert.equal(b.owner, vault.toBase58());
  assert.equal(b.custody, 'vault');
  assert.equal(b.balanceB, 300);
  assert.ok(b.gasSol > 0.5 && b.sol === 2, JSON.stringify(b));
  const payerBefore = fork.balance(payer.publicKey), vaultSolBefore = fork.balance(vault);

  const opened = await said(() => signer.open(POOL, String(b.price * 0.99), String(b.price * 1.01), '0.5', '100', true, deps));
  assert.equal(opened.sent, true, JSON.stringify(opened));
  const st = await said(() => signer.status(opened.positionMint, deps));
  assert.equal(st.positionMint, opened.positionMint);
  assert.ok(st.inRange && st.positionUsd > 50, JSON.stringify(st));
  assert.ok(fork.balance(vault) < vaultSolBefore - 0.1e9, 'the SOL side came from the vault');

  await trade(fork, rpc);
  const usdcPre = usdcOf(fork, vault);
  const h = await said(() => signer.harvest(opened.positionMint, true, deps));
  assert.equal(h.harvested, opened.positionMint, JSON.stringify(h));
  assert.ok(usdcOf(fork, vault) > usdcPre, 'the fees came into the vault');

  const c = await said(() => signer.close(opened.positionMint, true, deps));
  assert.equal(c.closed, opened.positionMint, JSON.stringify(c));
  assert.equal((await said(() => signer.status(undefined, deps))).positions, 0);
  // the payer paid the position's rent at open and got it back at close
  assert.ok(payerBefore - fork.balance(payer.publicKey) < 0.0005e9, `payer spent ${(payerBefore - fork.balance(payer.publicKey)) / 1e9} SOL`);
  assert.equal(fork.get(spl.getAssociatedTokenAddressSync(SOL, vault, true)), null, 'no WSOL left wrapped');

  // the loop's swap, on the approved pool: SOL to USDC
  const usdcBefore = usdcOf(fork, vault);
  const px = (await said(() => signer.balance(POOL, deps))).price;
  const out = await said(() => signer.rebalance(SOL.toBase58(), USDC.toBase58(), String(px * 0.5), String(Number(usdcBefore) / 1e6 + px * 0.5), true, deps));
  assert.equal(out.sent, true, JSON.stringify(out));
  assert.ok(usdcOf(fork, vault) > usdcBefore, 'the swap paid USDC into the vault');
  assert.ok(fork.balance(vault) >= 0.005e9, 'the vault kept its floor');
});

// Run `ixs` through the policy constraint the rule `name` compiled to, past
// the bot's own check (policy.match): what a forged transaction gets from the
// program itself.
async function force(fork, acting, policyKey, ixs, name, signers = []) {
  const p = acting.compiled.policies.findIndex(x => x.index.some(e => e.name === name));
  const c = acting.compiled.policies[p].index.findIndex(e => e.name === name);
  const call = sq.policyExecuteIx({ policy: acting.policies[p], signer: policyKey.publicKey, vault: acting.vault, ixs, indices: ixs.map(() => c) });
  return fork.sendSigned(await signWith(acting.transaction([call], fork.svm.latestBlockhash(), [policyKey]), signers));
}

const theirs = (fork, mint, who) => {
  const a = spl.getAssociatedTokenAddressSync(mint, who, true);
  fork.tokenAccount(a, mint, who, 0n);
  return a;
};
// The Whirlpool instructions the vault signs, as the vault would run them.
const vaultCalls = (kitIxs, acting) => forVault(kitIxs, acting.payer.publicKey)
  .filter(ix => ix.programId.equals(WHIRLPOOL) && ix.keys.some(k => k.isSigner && k.pubkey.equals(acting.vault)));

test('Orca: a pool the owners did not approve is refused, by the bot and by the program', { timeout: 600_000 }, async () => {
  const { fork, rpc, owner, acting, policyKey } = await setup();
  const w = (await fetchWhirlpool(rpc, address(OTHER))).data;
  const price = (Number(w.sqrtPrice) / 2 ** 64) ** 2 * 1e3;
  const built = await openPositionInstructions(rpc, address(OTHER), { tokenMaxA: 100_000_000n, tokenMaxB: 20_000_000n },
    price * 0.98, price * 1.02, { slippageToleranceBps: 100, funder: owner });
  assert.throws(() => acting.wrap(forVault(built.instructions, acting.payer.publicKey)), NotAllowed);
  // the open itself needs no vault signature (the payer funds it); the deposit into it does
  const deposit = vaultCalls(built.instructions, acting);
  assert.equal(deposit.length, 1);
  const res = await force(fork, acting, policyKey, deposit, 'increase_liquidity_by_token_amounts_v2');
  assert.ok(!res.ok);
  assert.match(res.logs.join('\n'), /ProgramInteractionAccountConstraintViolated/);
});

test('Orca: tokens out of the vault to anyone else are refused by the program', { timeout: 600_000 }, async () => {
  const { fork, rpc, vault, owner, acting, policyKey, deps, signer } = await setup();
  const b = await said(() => signer.balance(POOL, deps));
  const opened = await said(() => signer.open(POOL, String(b.price * 0.99), String(b.price * 1.01), '0.5', '100', true, deps));
  assert.equal(opened.sent, true, JSON.stringify(opened));
  await trade(fork, rpc);
  const thief = Keypair.generate().publicKey;
  const toSol = theirs(fork, SOL, thief), toUsdc = theirs(fork, USDC, thief);
  // the vault's WSOL account exists, so each case fails on the one account it changes
  const wsol = spl.getAssociatedTokenAddressSync(SOL, vault, true);
  assert.ok((await fork.sendSigned(acting.transaction([spl.createAssociatedTokenAccountIdempotentInstruction(vault, wsol, vault, SOL)],
    fork.svm.latestBlockhash()))).ok);
  const harvest = vaultCalls((await harvestPositionInstructions(rpc, address(opened.positionMint), { authority: owner })).instructions, acting);
  const close = vaultCalls((await closePositionInstructions(rpc, address(opened.positionMint), { slippageToleranceBps: 100, authority: owner })).instructions, acting);
  const swap = vaultCalls((await swapInstructions(rpc, { inputAmount: 10_000_000n, mint: address(SOL.toBase58()) }, address(POOL),
    { slippageToleranceBps: 100, signer: owner })).instructions, acting);
  const named = (ixs, disc) => ixs.find(ix => ix.data.subarray(0, 8).toString('hex') === disc);
  const cases = [
    ['collect_fees_v2', named(harvest, 'cf755fbfe5b4e20f'), 6, toSol],
    ['collect_fees_v2', named(harvest, 'cf755fbfe5b4e20f'), 8, toUsdc],
    ['decrease_liquidity_v2', named(close, '3a7fbc3e4f52c460'), 9, toSol],
    ['decrease_liquidity_v2', named(close, '3a7fbc3e4f52c460'), 10, toUsdc],
    ['swap_v2', named(swap, '2b04ed0b1ac91e62'), 9, toUsdc],
  ];
  for (const [name, ix, i, to] of cases) {
    assert.ok(ix, name);
    const forged = { ...ix, keys: ix.keys.map((k, j) => j === i ? { ...k, pubkey: to } : k) };
    const res = await force(fork, acting, policyKey, [forged], name);
    assert.ok(!res.ok, `${name} to ${i}`);
    assert.match(res.logs.join('\n'), /ProgramInteractionInvalidNumericValue/, `${name} to ${i}`);
  }
  // the close's rent goes to the payer or the vault, and nowhere else
  const shut = named(close, '01b6873b9b1963df');
  const rent = await force(fork, acting, policyKey, [{ ...shut, keys: shut.keys.map((k, j) => j === 1 ? { ...k, pubkey: thief } : k) }],
    'close_position_with_token_extensions');
  assert.ok(!rent.ok);
  assert.match(rent.logs.join('\n'), /ProgramInteractionAccountConstraintViolated/);
  assert.equal(fork.tokenAmount(toSol), 0n);
  assert.equal(fork.tokenAmount(toUsdc), 0n);
});

test('Orca: an instruction the rules do not list is refused, by the bot and by the program', { timeout: 600_000 }, async () => {
  const { fork, rpc, vault, owner, acting, policyKey, deps, signer } = await setup();
  const b = await said(() => signer.balance(POOL, deps));
  const opened = await said(() => signer.open(POOL, String(b.price * 0.99), String(b.price * 1.01), '0.2', '40', true, deps));
  assert.equal(opened.sent, true, JSON.stringify(opened));
  // the same accounts under increase_liquidity_v2 (by liquidity), which the rules leave out
  const add = await increaseLiquidityInstructions(rpc, address(opened.positionMint), { tokenMaxA: 1_000_000n, tokenMaxB: 100_000n },
    { slippageToleranceBps: 100, authority: owner });
  const [byAmounts] = vaultCalls(add.instructions, acting);
  const byLiquidity = { ...byAmounts, data: Buffer.concat([Buffer.from('851d59df45eeb00a', 'hex'), byAmounts.data.subarray(8)]) };
  assert.throws(() => acting.wrap([byLiquidity]), NotAllowed);
  const res = await force(fork, acting, policyKey, [byLiquidity], 'increase_liquidity_by_token_amounts_v2');
  assert.ok(!res.ok);
  assert.match(res.logs.join('\n'), /ProgramInteractionInvalidNumericValue/);
  // a delegate on the vault's USDC, under the rule that closes WSOL accounts
  const approve = spl.createApproveInstruction(spl.getAssociatedTokenAddressSync(USDC, vault, true), Keypair.generate().publicKey, vault, 1_000_000n);
  assert.throws(() => acting.wrap([approve]), NotAllowed);
  const refused = await force(fork, acting, policyKey, [approve], 'unwrap SOL (close a WSOL account back into the vault)');
  assert.ok(!refused.ok);
  assert.match(refused.logs.join('\n'), /ProgramInteractionAccountConstraintViolated/, 'its account 1 is the delegate, not the vault');
});

// Whirlpool lets a position's delegate act for it. A stranger who makes the
// vault the delegate of their position could have the vault deposit into it;
// the rule pins the position's token account to one the vault owns.
test('Orca: a stranger\'s position, even with the vault as its delegate, takes nothing from the vault', { timeout: 600_000 }, async () => {
  const { fork, rpc, vault, owner, acting, policyKey } = await setup();
  const stranger = fork.wallet(10);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, stranger.publicKey), USDC, stranger.publicKey, 1_000_000_000n);
  const w = (await fetchWhirlpool(rpc, address(POOL))).data;
  const price = (Number(w.sqrtPrice) / 2 ** 64) ** 2 * 1e3;
  const theirs = await openPositionInstructions(rpc, address(POOL), { tokenMaxA: 100_000_000n, tokenMaxB: 20_000_000n },
    price * 0.98, price * 1.02, { slippageToleranceBps: 100, funder: keyOf(stranger) });
  const mint = new PublicKey(String(theirs.positionMint));
  const nft = spl.getAssociatedTokenAddressSync(mint, stranger.publicKey, false, spl.TOKEN_2022_PROGRAM_ID);
  const ixs = [...theirs.instructions.map(kitToWeb3), spl.createApproveInstruction(nft, vault, stranger.publicKey, 1n, [], spl.TOKEN_2022_PROGRAM_ID)];
  const msg = new TransactionMessage({ payerKey: stranger.publicKey, recentBlockhash: fork.svm.latestBlockhash(), instructions: ixs }).compileToV0Message();
  const tx = new VersionedTransaction(msg);
  tx.sign([stranger]);
  const o = await fork.sendSigned(await signWith(tx, madeSigners(theirs.instructions, stranger.publicKey)));
  assert.ok(o.ok, o.logs.join('\n'));
  // the vault's deposit into the stranger's position, through their token account
  const add = await increaseLiquidityInstructions(rpc, address(mint.toBase58()), { tokenMaxA: 10_000_000n, tokenMaxB: 2_000_000n },
    { slippageToleranceBps: 100, authority: owner });
  const [deposit] = vaultCalls(add.instructions, acting);
  const forged = { ...deposit, keys: deposit.keys.map((k, j) => j === 6 ? { ...k, pubkey: nft } : k) };
  const usdcBefore = usdcOf(fork, vault), solBefore = fork.balance(vault);
  const res = await force(fork, acting, policyKey, [forged], 'increase_liquidity_by_token_amounts_v2');
  assert.ok(!res.ok, 'the vault paid into a stranger\'s position');
  assert.match(res.logs.join('\n'), /ProgramInteractionInvalidNumericValue/);
  assert.equal(usdcOf(fork, vault), usdcBefore);
  assert.equal(fork.balance(vault), solBefore);
});

test('Orca: payouts leave the vault for the approved payout wallet only', { timeout: 600_000 }, async () => {
  const { fork, connection, vault, env, profit } = await setup();
  const payout = await import('../chains/solana/payout.mjs');
  const penv = { ...env, LPBOT_PROFIT_WALLET: profit.publicKey.toBase58() };
  await said(() => payout.sendFromVault(USDC.toBase58(), '7', profit.publicKey.toBase58(), true, { env: penv, connection }));
  assert.equal(usdcOf(fork, profit.publicKey), 7_000_000n);
  assert.equal(usdcOf(fork, vault), 293_000_000n);
  const stranger = Keypair.generate().publicKey;
  await assert.rejects(payout.sendFromVault(USDC.toBase58(), '1', stranger.toBase58(), true,
    { env: { ...penv, LPBOT_PROFIT_WALLET: stranger.toBase58() }, connection }), /not an approved payout account/);
});

const simulating = (connection, value) => Object.create(connection, { simulateTransaction: { value: async () => ({ value }) } });
const brokenBlockhash = (connection) => Object.create(connection, { getLatestBlockhash: { value: async () => { throw new Error('rpc down'); } } });

test('Orca: a dry run reports the simulation of the vault\'s transaction and sends nothing', { timeout: 600_000 }, async () => {
  const { fork, connection, vault, payer, acting, rpc, owner, deps, signer } = await setup();
  const via = (conn) => ({ urls: ['http://fork.test'], connectFn: async () => ({ rpc, connection: conn, payer, acting, owner }), sleep: async () => {} });
  const price = (await said(() => signer.balance(POOL, deps))).price;
  const before = fork.balance(vault);
  const dry = async (conn) => (await said(() => signer.open(POOL, String(price * 0.99), String(price * 1.01), '0.5', '100', false, via(conn)))).simulation;
  assert.deepEqual(await dry(simulating(connection, { err: null, logs: ['a', 'Program log: Error: x', 'c', 'd', 'e'], unitsConsumed: 0 })),
    { ok: true, err: null, logError: 'Program log: Error: x', unitsConsumed: 0, logTail: ['Program log: Error: x', 'c', 'd', 'e'] });
  assert.deepEqual(await dry(simulating(connection, { err: { InstructionError: [3, 'Custom'] }, logs: null })),
    { ok: false, err: '{"InstructionError":[3,"Custom"]}', logError: null, unitsConsumed: null, logTail: [] });
  assert.deepEqual(await dry(brokenBlockhash(connection)), { ok: false, err: 'rpc down', unitsConsumed: null, logTail: [] });
  assert.equal(fork.balance(vault), before, 'a dry run moved nothing');
  const opened = await said(() => signer.open(POOL, String(price * 0.99), String(price * 1.01), '0.5', '100', true, deps));
  for (const run of [() => signer.harvest(opened.positionMint, false, via(simulating(connection, { err: null, logs: [], unitsConsumed: 7 }))),
    () => signer.close(opened.positionMint, false, via(simulating(connection, { err: null, logs: [], unitsConsumed: 7 })))]) {
    const out = await said(run);
    assert.equal(out.sent, false);
    assert.deepEqual(out.simulation, { ok: true, err: null, logError: null, unitsConsumed: 7, logTail: [] });
  }
  assert.equal((await said(() => signer.status(opened.positionMint, deps))).positionMint, opened.positionMint, 'still open');
  const s = await said(() => signer.rebalance(SOL.toBase58(), USDC.toBase58(), '1', '400', false, via(simulating(connection, { err: null, logs: [], unitsConsumed: 9 }))));
  assert.equal(s.sent, false);
  assert.equal(s.simulation.unitsConsumed, 9);
});

test('Orca rebalance: bad targets are refused; nothing to do is a report; a key wallet is sent elsewhere', { timeout: 600_000 }, async () => {
  const { fork, connection, vault, payer, rpc, deps, signer } = await setup({ usdc: 0n });
  const ask = (a, b) => said(() => signer.rebalance(SOL.toBase58(), USDC.toBase58(), a, b, true, deps));
  await assert.rejects(signer.rebalance(SOL.toBase58(), USDC.toBase58(), '-1', '1', true, deps), /non-negative/);
  await assert.rejects(signer.rebalance(SOL.toBase58(), USDC.toBase58(), '1', '-1', true, deps), /non-negative/);
  await assert.rejects(signer.rebalance(SOL.toBase58(), SOL.toBase58(), '1', '1', true, deps), /same token/);
  const none = await ask('0', '0');
  assert.equal(none.noop, true);
  assert.match(none.reason, /within .* or nothing to sell/);
  assert.ok(Math.abs(none.A.sellable - (2 - 0.005)) < 1e-9, `a vault keeps only its floor: ${none.A.sellable}`);
  const dust = await ask('1', '0.0000001');
  assert.equal(dust.noop, true);
  assert.match(dust.reason, /rounds to zero/);
  assert.equal(fork.balance(vault), 2e9, 'nothing moved');
  const key = await actingAccount({ connection, payer, venue: 'orca', env: {} });
  await assert.rejects(signer.rebalance(SOL.toBase58(), USDC.toBase58(), '1', '1', true,
    { urls: ['http://fork.test'], connectFn: async () => ({ rpc, connection, payer, acting: key, owner: keyOf(payer) }), sleep: async () => {} }),
  /a key wallet swaps through venues\/orca\/swap.mjs/);
});
