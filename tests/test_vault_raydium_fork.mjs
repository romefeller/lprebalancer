// A Raydium CLMM position run by a vault, end to end, on a local fork of
// mainnet: the deployed Squads and Raydium programs, the live SOL/USDC pool.
// The signer's own commands, as the loop calls them, in vault mode; then the
// program's refusals of what the policy does not allow.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { connectedVault } from './_vault.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { NotAllowed } from '../chains/solana/vault/policy.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';
import * as vaultIx from '../venues/raydium_clmm/vault.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, Keypair, SystemProgram } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const { Raydium, PoolUtils, TickUtil, ClmmInstrument } = require('@raydium-io/raydium-sdk-v2');
const BN = require('bn.js');

const POOL = '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv';      // SOL/USDC 0.04%, one (ended) RAY reward
const OTHER = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj';     // SOL/USDC, not approved
const USDC = new PublicKey(SOLANA.usdc_mint);
const SOL = spl.NATIVE_MINT;
const PROGRAM = new PublicKey(SOLANA.programs.raydium_clmm);

// A connected vault holding 2 SOL and 300 USDC, and the signer's deps.
async function setup({ usdc = 300_000_000n } = {}) {
  const v = await connectedVault({ venues: ['raydium-clmm'], pools: [POOL] });
  const { fork, connection, env, payer } = v;
  await fork.program(SOLANA.programs.metaplex_metadata);         // a legacy open's NFT metadata
  const vault = new PublicKey(v.out.vault);
  fork.fund(vault, 2);
  await fork.clone([USDC]);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, vault, true), USDC, vault, usdc);
  const acting = await actingAccount({ connection, payer, venue: 'raydium-clmm', env });
  // a fresh SDK per command, as each signer process loads its own
  const load = () => Raydium.load({ connection, owner: vault, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
  const raydium = await load();
  const deps = { urls: ['http://fork.test'], connectFn: async () => ({ connection, payer, raydium: await load(), acting }), sleep: async () => {} };
  const signer = await import('../venues/raydium_clmm/signer.mjs');
  process.env.LPBOT_POOL = POOL;
  process.env.LPBOT_MAX_USD = '100000';
  delete process.env.LPBOT_TOKEN_HINTS;
  return { ...v, vault, acting, raydium, deps, signer };
}

// What a signer command printed: its one JSON object.
async function said(fn) {
  const lines = [], log = console.log;
  console.log = (x) => lines.push(x);
  try { await fn(); } finally { console.log = log; }
  return JSON.parse(lines.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null');
}

const usdcOf = (fork, owner) => fork.tokenAmount(spl.getAssociatedTokenAddressSync(USDC, owner, true)) ?? 0n;

// An outside trader swaps through the pool both ways, so positions in range earn fees.
async function trade(fork, raydium) {
  const trader = fork.wallet(500);
  const ata = (m) => spl.getAssociatedTokenAddressSync(m, trader.publicKey);
  fork.tokenAccount(ata(USDC), USDC, trader.publicKey, 50_000_000_000n);
  const px = Number((await raydium.clmm.getPoolInfoFromRpc(POOL)).poolInfo.price);
  // 100 SOL out, their worth in USDC back in: the price ends near where it began
  for (const [mint, amount] of [[SOL, 100e9], [USDC, Math.round(100 * px * 1e6)]]) {
    const r = await raydium.clmm.getPoolInfoFromRpc(POOL);
    const q = PoolUtils.computeAmountOut({ poolInfo: r.computePoolInfo, tickarrayBitmapExtension: r.computePoolInfo.exBitmapInfo,
      tickArrayCache: r.tickData[POOL], baseMint: mint, epochInfo: { epoch: 0 }, amountIn: new BN(amount), slippage: 0.5,
      blockTimestamp: Math.floor(Date.now() / 1000), catchLiquidityInsufficient: true });
    const ixs = vaultIx.swapIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, owner: trader.publicKey, inputMint: mint,
      amountIn: new BN(amount), amountOutMin: new BN(0), remainingAccounts: q.remainingAccounts });
    const res = await fork.send(ixs, trader);
    assert.ok(res.ok, res.logs.join('\n'));
  }
}

test('the Raydium signer runs a vault: open, harvest, increase, swap, close', { timeout: 600_000 }, async () => {
  const { fork, connection, vault, payer, raydium, deps, signer } = await setup();
  const b = await said(() => signer.balance(undefined, deps));
  assert.equal(b.owner, vault.toBase58());
  assert.equal(b.custody, 'vault');
  assert.equal(b.balanceB, 300);
  assert.ok(b.gasSol > 0.5 && b.sol === 2, JSON.stringify(b));
  const payerBefore = fork.balance(payer.publicKey);
  const vaultSolBefore = fork.balance(vault);

  const price = b.price;
  for (const lean of ['1', '0']) {
    process.env.LPBOT_RAYDIUM_LEAN = lean;
    const opened = await said(() => signer.open(POOL, String(price * 0.99), String(price * 1.01), '0.5', '100', true, deps));
    assert.equal(opened.sent, true, JSON.stringify(opened));
    const st = await said(() => signer.status(opened.positionMint, deps));
    assert.equal(st.positionMint, opened.positionMint);
    assert.ok(st.inRange && st.positionUsd > 50, JSON.stringify(st));

    await trade(fork, raydium);
    const usdcPre = usdcOf(fork, vault);
    const h = await said(() => signer.harvest(opened.positionMint, true, deps));
    assert.equal(h.harvested, opened.positionMint, JSON.stringify(h));
    assert.ok(usdcOf(fork, vault) > usdcPre, 'the fees came into the vault');

    const liq0 = BigInt((await said(() => signer.status(opened.positionMint, deps))).liquidity);
    const inc = await said(() => signer.increase(opened.positionMint, '0.1', '20', true, deps));
    assert.equal(inc.sent, true, JSON.stringify(inc));
    assert.ok(BigInt((await said(() => signer.status(opened.positionMint, deps))).liquidity) > liq0, 'the add went into the vault\'s position');

    const c = await said(() => signer.close(opened.positionMint, true, deps));
    assert.equal(c.closed, opened.positionMint, JSON.stringify(c));
    assert.equal((await said(() => signer.status(undefined, deps))).positions, 0);
  }
  delete process.env.LPBOT_RAYDIUM_LEAN;
  // a cycle costs the payer fees only; the vault paid the rent and got it back
  assert.ok(payerBefore - fork.balance(payer.publicKey) < 0.001e9, `payer spent ${(payerBefore - fork.balance(payer.publicKey)) / 1e9} SOL`);
  assert.equal(fork.get(spl.getAssociatedTokenAddressSync(SOL, vault, true)), null, 'no WSOL left wrapped');
  assert.ok(fork.balance(vault) > vaultSolBefore - 0.6e9, 'the SOL came back to the vault');

  // the loop's swap, on the approved pool: SOL to USDC
  const usdcBefore = usdcOf(fork, vault);
  const px = (await said(() => signer.balance(undefined, deps))).price;
  const out = await said(() => signer.rebalance(SOL.toBase58(), USDC.toBase58(), String(px * 0.5), String(Number(usdcBefore) / 1e6 + px * 0.5), true, deps));
  assert.equal(out.sent, true, JSON.stringify(out));
  assert.ok(usdcOf(fork, vault) > usdcBefore, 'the swap paid USDC into the vault');
  assert.equal(fork.get(spl.getAssociatedTokenAddressSync(SOL, vault, true)), null, 'no WSOL left wrapped after the swap');
});

// Run `ixs` through the policy constraint the rule `name` compiled to, past
// the bot's own check (policy.match): what a forged transaction gets from the
// program itself.
async function force(fork, acting, policyKey, ixs, name, signers = []) {
  const p = acting.compiled.policies.findIndex(x => x.index.some(e => e.name === name));
  const c = acting.compiled.policies[p].index.findIndex(e => e.name === name);
  const call = sq.policyExecuteIx({ policy: acting.policies[p], signer: policyKey.publicKey, vault: acting.vault, ixs, indices: ixs.map(() => c) });
  return fork.sendSigned(acting.transaction([call], fork.svm.latestBlockhash(), [policyKey, ...signers]));
}

const theirs = (fork, mint, who) => {
  const a = spl.getAssociatedTokenAddressSync(mint, who, true);
  fork.tokenAccount(a, mint, who, 0n);
  return a;
};
const vaultCalls = (ixs, acting) => ixs.filter(ix => ix.programId.equals(PROGRAM) && ix.keys.some(k => k.isSigner && k.pubkey.equals(acting.vault)));

test('Raydium: a pool the owners did not approve is refused, by the bot and by the program', { timeout: 600_000 }, async () => {
  const { fork, vault, acting, policyKey, raydium } = await setup();
  const r = await raydium.clmm.getPoolInfoFromRpc(OTHER);
  const tick = r.computePoolInfo.tickCurrent;
  const built = await vaultIx.openIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, owner: vault, tickLower: tick - 50, tickUpper: tick + 50,
    base: 'MintB', baseAmount: new BN(10_000_000), otherAmountMax: new BN(1e9), lean: true });
  assert.throws(() => acting.wrap(built.ixs), NotAllowed);
  const [open] = vaultCalls(built.ixs, acting);
  const res = await force(fork, acting, policyKey, [open], 'open_position_with_token22_nft', built.signers);
  assert.ok(!res.ok);
  assert.match(res.logs.join('\n'), /ProgramInteractionAccountConstraintViolated/);
});

test('Raydium: tokens out of the vault to anyone else are refused by the program', { timeout: 600_000 }, async () => {
  const { fork, vault, acting, policyKey, raydium, deps, signer } = await setup();
  const price = (await said(() => signer.balance(undefined, deps))).price;
  const opened = await said(() => signer.open(POOL, String(price * 0.99), String(price * 1.01), '0.5', '100', true, deps));
  assert.equal(opened.sent, true, JSON.stringify(opened));
  const thief = Keypair.generate().publicKey;
  const r = await raydium.clmm.getPoolInfoFromRpc(POOL);
  const [position] = (await raydium.clmm.getOwnerPositionInfo({ programId: PROGRAM })).filter(p => p.poolId.toBase58() === POOL);
  const decrease = (liquidity) => vaultCalls(vaultIx.decreaseIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, position, owner: vault,
    liquidity, amountMinA: new BN(0), amountMinB: new BN(0), nft2022: true, close: false }), acting)[0];
  const swapTo = (to) => {
    const ix = vaultCalls(vaultIx.swapIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, owner: vault, inputMint: SOL, amountIn: new BN(1e7),
      amountOutMin: new BN(0), remainingAccounts: [] }), acting)[0];
    ix.keys[4] = { ...ix.keys[4], pubkey: to };
    return ix;
  };
  const ray = new PublicKey(r.poolKeys.rewardInfos[0].mint.address);
  await fork.clone([ray]);
  // the vault's own accounts exist, so each case fails on the one account it changes
  const atas = vaultIx.ensureAtas(vault, [SOL, USDC, ray].map(m => ({ mint: m, program: spl.TOKEN_PROGRAM_ID })));
  assert.ok((await fork.sendSigned(acting.transaction(atas, fork.svm.latestBlockhash()))).ok);
  // the recipient of each side, and of the RAY reward (after the bitmap the signer puts at 16)
  for (const [i, to, liquidity] of [[9, theirs(fork, SOL, thief), position.liquidity], [10, theirs(fork, USDC, thief), position.liquidity],
    [18, theirs(fork, ray, thief), new BN(0)]]) {
    const ix = decrease(liquidity);
    ix.keys[i] = { ...ix.keys[i], pubkey: to };
    const res = await force(fork, acting, policyKey, [ix], 'decrease_liquidity_v2');
    assert.ok(!res.ok, `recipient ${i}`);
    assert.match(res.logs.join('\n'), /ProgramInteractionInvalidNumericValue/, `recipient ${i}`);
  }
  // the SDK's own layout, without the bitmap the rule pins at 16: refused
  const raw = decrease(new BN(0));
  const bare = { ...raw, keys: raw.keys.filter((_, i) => i !== 16) };
  const res = await force(fork, acting, policyKey, [bare], 'decrease_liquidity_v2');
  assert.ok(!res.ok);
  const swap = await force(fork, acting, policyKey, [swapTo(theirs(fork, USDC, Keypair.generate().publicKey))], 'swap_v2');
  assert.ok(!swap.ok);
  assert.match(swap.logs.join('\n'), /ProgramInteractionInvalidNumericValue/);
});

test('Raydium: an instruction the rules do not list is refused, by the bot and by the program', { timeout: 600_000 }, async () => {
  const { fork, vault, acting, policyKey } = await setup();
  const thief = Keypair.generate().publicKey;
  const usdc = spl.getAssociatedTokenAddressSync(USDC, vault, true);
  // a delegate on the vault's USDC would let the thief take it later
  const approve = spl.createApproveInstruction(usdc, thief, vault, 1_000_000n);
  assert.throws(() => acting.wrap([approve]), NotAllowed);
  for (const rule of ['swap_v2', 'unwrap SOL (close a WSOL account back into the vault)']) {
    const res = await force(fork, acting, policyKey, [approve], rule);
    assert.ok(!res.ok, rule);
    assert.match(res.logs.join('\n'), /ProgramInteractionProgramIdMismatch|ProgramInteractionInvalidNumericValue|ProgramInteractionAccountConstraintViolated/);
  }
  // a SOL transfer to a wallet, under the wrap rule
  const res = await force(fork, acting, policyKey, [SystemProgram.transfer({ fromPubkey: vault, toPubkey: thief, lamports: 1e8 })],
    'wrap SOL (transfer into a WSOL account of the vault)');
  assert.ok(!res.ok);
  assert.equal(fork.balance(thief), 0);
});

test('Raydium: a stranger\'s position cannot take or give the vault\'s money', { timeout: 600_000 }, async () => {
  const { fork, vault, acting, policyKey, raydium } = await setup();
  const stranger = fork.wallet(10);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, stranger.publicKey), USDC, stranger.publicKey, 1_000_000_000n);
  const r = await raydium.clmm.getPoolInfoFromRpc(POOL);
  const tick = r.computePoolInfo.tickCurrent;
  const theirOpen = await vaultIx.openIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, owner: stranger.publicKey,
    tickLower: tick - 60, tickUpper: tick + 60, base: 'MintB', baseAmount: new BN(50_000_000), otherAmountMax: new BN(2e9), lean: true });
  const o = await fork.send(theirOpen.ixs, stranger, theirOpen.signers);
  assert.ok(o.ok, o.logs.join('\n'));
  const strangers = await Raydium.load({ connection: fork.connection(), owner: stranger.publicKey, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
  const [position] = await strangers.clmm.getOwnerPositionInfo({ programId: PROGRAM });
  // the vault's own (empty) account for the stranger's NFT, which the ATA rule lets the bot create
  const nftAta = spl.getAssociatedTokenAddressSync(position.nftMint, vault, true, spl.TOKEN_2022_PROGRAM_ID);
  const made = await fork.sendSigned(acting.transaction([spl.createAssociatedTokenAccountIdempotentInstruction(vault, nftAta, vault,
    position.nftMint, spl.TOKEN_2022_PROGRAM_ID)], fork.svm.latestBlockhash()));
  assert.ok(made.ok, made.logs.join('\n'));
  const add = vaultCalls(vaultIx.increaseIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, position, owner: vault, base: 'MintB',
    baseAmount: new BN(10_000_000), otherAmountMax: new BN(1e9), nft2022: true }), acting);
  const usdcBefore = usdcOf(fork, vault);
  const res = await force(fork, acting, policyKey, add, 'increase_liquidity_v2');
  assert.ok(!res.ok, 'the vault added to a stranger\'s position');
  // with the stranger's own NFT account named instead: the pin refuses it
  add[0].keys[1] = { ...add[0].keys[1], pubkey: spl.getAssociatedTokenAddressSync(position.nftMint, stranger.publicKey, false, spl.TOKEN_2022_PROGRAM_ID) };
  const res2 = await force(fork, acting, policyKey, add, 'increase_liquidity_v2');
  assert.ok(!res2.ok);
  assert.match(res2.logs.join('\n'), /ProgramInteractionInvalidNumericValue/);
  assert.equal(usdcOf(fork, vault), usdcBefore);
  // nor can the vault take the stranger's liquidity or close the position
  const take = vaultCalls(vaultIx.decreaseIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, position, owner: vault, liquidity: position.liquidity,
    amountMinA: new BN(0), amountMinB: new BN(0), nft2022: true, close: true }), acting);
  for (const [ix, name] of [[take[0], 'decrease_liquidity_v2'], [take[1], 'close_position']]) {
    const t = await force(fork, acting, policyKey, [ix], name);
    assert.ok(!t.ok, name);
  }
});

test('Raydium: payouts leave the vault for the approved payout wallet only', { timeout: 600_000 }, async () => {
  const { fork, connection, vault, env, profit } = await setup();
  const payout = await import('../chains/solana/payout.mjs');
  const penv = { ...env, LPBOT_PROFIT_WALLET: profit.publicKey.toBase58() };
  await said(() => payout.sendFromVault(USDC.toBase58(), '5', profit.publicKey.toBase58(), true, { env: penv, connection }));
  assert.equal(usdcOf(fork, profit.publicKey), 5_000_000n);
  assert.equal(usdcOf(fork, vault), 295_000_000n);
  const stranger = Keypair.generate().publicKey;
  await assert.rejects(payout.sendFromVault(USDC.toBase58(), '1', stranger.toBase58(), true,
    { env: { ...penv, LPBOT_PROFIT_WALLET: stranger.toBase58() }, connection }), /not an approved payout account/);
});
