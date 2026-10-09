// The Orca fallback swap (swap_orca.mjs): pure checks, no network.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, TransactionMessage, VersionedTransaction, TransactionInstruction, SystemProgram,
  ComputeBudgetProgram } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const { getSwapV2InstructionDataEncoder } = await import('@orca-so/whirlpools-client');

// Importing must print nothing and run nothing.
const logged = [];
const realLog = console.log;
console.log = (...a) => logged.push(a.join(' '));
const M = await import('../swap_orca.mjs');
console.log = realLog;
const { errorKind, overEndpoints, isEntry, AfterSignError } = await import('../rpc_policy.mjs');

const SCRIPT = fileURLToPath(new URL('../swap_orca.mjs', import.meta.url));
const SOL = M.NATIVE_MINT, USDC = M.USDC_MINT;
const POOL = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE';
const TOKEN = spl.TOKEN_PROGRAM_ID;

// Deterministic pseudo-random numbers for the property loops.
function rng(seed) { let s = seed >>> 0; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32); }

test('importing has no side effects', () => {
  assert.deepEqual(logged, []);
  assert.equal(isEntry(new URL('../swap_orca.mjs', import.meta.url).href, ['node', 'other.mjs']), false);
  assert.equal(typeof M.main, 'function');
});

test('CLI without a command prints usage, exits 0, sends nothing', () => {
  for (const args of [[], ['--execute'], ['quote', SOL, USDC, '1', '--execute']]) {
    const env = { ...process.env, SOLANA_RPC_URL: 'http://127.0.0.1:9' };   // a dead endpoint: no network
    delete env.WALLET_SECRET_PATH;
    const r = spawnSync('node', [SCRIPT, ...args], { env, encoding: 'utf8', timeout: 30_000 });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), M.USAGE);
    assert.doesNotMatch(r.stdout, /signature/);
  }
});

test('rebalance --execute without a key refuses before any read', () => {
  const env = { ...process.env, SOLANA_RPC_URL: 'http://127.0.0.1:9' };
  delete env.WALLET_SECRET_PATH; delete env.LPBOT_OWNER;
  const r = spawnSync('node', [SCRIPT, 'rebalance', SOL, USDC, '50', '50', '--execute'], { env, encoding: 'utf8', timeout: 30_000 });
  assert.equal(r.status, 1);
  assert.match(r.stderr, /ERROR: WALLET_SECRET_PATH is not set/);
  assert.equal(r.stdout.trim(), '');
});

test('rebalance refuses a pair with no default pool and bad arguments', async () => {
  const other = Keypair.generate().publicKey.toBase58();
  await assert.rejects(M.main(['rebalance', SOL, other, '1', '1']), /no default Orca pool/);
  await assert.rejects(M.main(['rebalance', SOL, SOL, '1', '1']), /same token/);
  await assert.rejects(M.main(['rebalance', 'notamint', USDC, '1', '1']), /not a valid mint/);
  await assert.rejects(M.main(['rebalance', SOL, USDC, '-1', '1']), /non-negative/);
  await assert.rejects(M.main(['rebalance', SOL, USDC, '1', '1', '--pool', 'x']), /not a valid address/);
});

test('parseArgs reads --execute and --pool anywhere', () => {
  assert.deepEqual(M.parseArgs(['rebalance', 'a', 'b', '1', '2', '--pool', 'P', '--execute']),
    { cmd: 'rebalance', args: ['a', 'b', '1', '2'], execute: true, pool: 'P' });
  assert.deepEqual(M.parseArgs(['--execute', 'rebalance', 'a']), { cmd: 'rebalance', args: ['a'], execute: true, pool: null });
  assert.equal(M.parseArgs([]).execute, false);
});

test('defaultPool knows SOL/USDC in both orders and nothing else', () => {
  assert.equal(M.defaultPool(SOL, USDC), POOL);
  assert.equal(M.defaultPool(USDC, SOL), POOL);
  assert.equal(M.defaultPool(SOL, SOL), null);
});

test('HALT refuses, as a fatal error', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'orca-halt-'));
  const halt = path.join(dir, 'HALT');
  assert.equal(M.guard(halt), undefined);
  fs.writeFileSync(halt, 'operator stop');
  assert.throws(() => M.guard(halt), /HALT present: operator stop/);
  try { M.guard(halt); } catch (e) { assert.equal(errorKind(e), 'fatal'); }
  fs.rmSync(dir, { recursive: true });
});

test('checkPool: Whirlpool program, exactly the two mints', () => {
  const pool = { address: POOL, programAddress: M.WHIRLPOOL_PROGRAM, tokenMintA: SOL, tokenMintB: USDC };
  assert.ok(M.checkPool(pool, SOL, USDC));
  assert.ok(M.checkPool(pool, USDC, SOL));
  const x = Keypair.generate().publicKey.toBase58();
  assert.throws(() => M.checkPool(pool, SOL, x), /refusing/);
  assert.throws(() => M.checkPool(pool, SOL, SOL), /refusing/);
  assert.throws(() => M.checkPool({ ...pool, tokenMintB: SOL }, SOL, USDC), /refusing/);
  assert.throws(() => M.checkPool({ ...pool, programAddress: x }, SOL, USDC), /not the Whirlpool program/);
});

test('direction: selling the pool token A is a->b', () => {
  assert.deepEqual(M.direction(SOL, USDC, SOL, USDC), { aToB: true });
  assert.deepEqual(M.direction(USDC, SOL, SOL, USDC), { aToB: false });
  assert.throws(() => M.direction(SOL, SOL, SOL, USDC), /not this pool's pair/);
});

const infoSol = { mint: SOL, symbol: 'SOL', decimals: 9, usdPrice: 120 };
const infoUsdc = { mint: USDC, symbol: 'USDC', decimals: 6, usdPrice: 1 };

test('plan -> swap: direction and amount, never above sellable', () => {
  const r = rng(7);
  for (let i = 0; i < 500; i++) {
    const sellUsd = r() * 200, availA = r() * 2, availB = r() * 200;
    for (const sellSide of ['A', 'B']) {
      const s = M.planSwap({ sellSide, sellUsd }, infoSol, infoUsdc, availA, availB);
      const sell = sellSide === 'A' ? infoSol : infoUsdc, buy = sellSide === 'A' ? infoUsdc : infoSol;
      assert.equal(s.sellInfo, sell); assert.equal(s.buyInfo, buy);
      const avail = sellSide === 'A' ? availA : availB;
      assert.ok(s.amount <= avail + 1e-12, `amount ${s.amount} > sellable ${avail}`);
      assert.ok(s.amount <= sellUsd / sell.usdPrice + 1e-12);
      assert.equal(typeof s.rawIn, 'bigint');
      assert.ok(s.rawIn >= 0n);
      assert.equal(s.amount, Number(s.rawIn) / 10 ** sell.decimals);
    }
  }
  assert.equal(M.planSwap({ sellSide: 'A', sellUsd: 0 }, infoSol, infoUsdc, 1, 1).rawIn, 0n);
});

test('valueLossOk: property — passes iff out >= (1 - L) x in', () => {
  const r = rng(11);
  for (let i = 0; i < 5000; i++) {
    const inUsd = r() * 1000, L = r() * 0.1, outUsd = inUsd * (1 - 0.2 + r() * 0.4);
    assert.equal(M.valueLossOk(inUsd, outUsd, L), outUsd >= inUsd * (1 - L));
    // monotone: more output never turns a pass into a refusal
    if (M.valueLossOk(inUsd, outUsd, L)) assert.ok(M.valueLossOk(inUsd, outUsd * 1.01 + 1e-9, L));
  }
  assert.equal(M.valueLossOk(NaN, 1), false);
  assert.equal(M.valueLossOk(1, Infinity), false);
  assert.equal(M.valueLossOk(-1, 0), false);
  assert.equal(M.MAX_VALUE_LOSS, 0.02);
});

// An exact-in quote of SOL -> USDC at price p, losing `loss` of fair value.
function q(rawIn, p, loss = 0, slip = 100) {
  const est = BigInt(Math.floor(Number(rawIn) / 1e9 * p * (1 - loss) * 1e6));
  return { tokenIn: BigInt(rawIn), tokenEstOut: est, tokenMinOut: est * BigInt(10_000 - slip) / 10_000n, tradeFee: 0n };
}

test('verifyQuote: the swap asked for, at our slippage', () => {
  const raw = 1_000_000_000n;
  assert.ok(M.verifyQuote(q(raw, 120), infoSol, infoUsdc, raw));
  assert.throws(() => M.verifyQuote(q(raw + 1n, 120), infoSol, infoUsdc, raw), /tokenIn/);
  assert.throws(() => M.verifyQuote({ ...q(raw, 120), tokenMinOut: 10n ** 12n }, infoSol, infoUsdc, raw), /exceeds its own estimate/);
  assert.throws(() => M.verifyQuote({ ...q(raw, 120), tokenEstOut: 0n, tokenMinOut: 0n }, infoSol, infoUsdc, raw), /pays nothing/);
  assert.throws(() => M.verifyQuote(q(raw, 120, 0, 500), infoSol, infoUsdc, raw), /below 100 bps slippage/);
  assert.throws(() => M.verifyQuote(q(raw, 120), { ...infoSol, usdPrice: null }, infoUsdc, raw), /no USD price/);
});

test('verifyQuote: property — refuses iff the estimate is more than MAX_VALUE_LOSS below fair', () => {
  const r = rng(3);
  for (let i = 0; i < 2000; i++) {
    const raw = BigInt(1 + Math.floor(r() * 5e9));
    const loss = r() * 0.05;
    const quote = q(raw, 120, loss);
    const inUsd = Number(raw) / 1e9 * 120, estUsd = Number(quote.tokenEstOut) / 1e6;
    const ok = estUsd >= inUsd * (1 - M.MAX_VALUE_LOSS) && Number(quote.tokenEstOut) > 0;
    if (ok) assert.ok(M.verifyQuote(quote, infoSol, infoUsdc, raw));
    else assert.throws(() => M.verifyQuote(quote, infoSol, infoUsdc, raw), /refusing/);
  }
});

test('verifyQuote refusals are fatal in the endpoint loop, even with "$500" in the text', () => {
  try { M.verifyQuote(q(5_000_000_000n, 120, 0.1), infoSol, infoUsdc, 5_000_000_000n); assert.fail('no refusal'); } catch (e) {
    assert.match(e.message, /\$5\d\d\./);
    assert.equal(errorKind(e), 'fatal');
  }
});

test('priceImpact: zero at spot, positive below, checkImpact caps it', () => {
  const sqrt = BigInt(Math.floor(Math.sqrt(120 * 1e6 / 1e9) * 2 ** 64));  // B per A raw: 120 USDC/SOL
  const spot = M.spotOutPerIn(sqrt, true);
  assert.ok(Math.abs(spot - 0.12) < 1e-9);
  assert.ok(Math.abs(M.spotOutPerIn(sqrt, false) - 1 / 0.12) < 1e-6);
  const at = { tokenIn: 1_000_000_000n, tradeFee: 0n, tokenEstOut: 120_000_000n };
  assert.ok(M.priceImpact(at, sqrt, true) < 1e-6);
  const worse = { ...at, tokenEstOut: 118_800_000n };
  assert.ok(Math.abs(M.priceImpact(worse, sqrt, true) - 0.01) < 1e-6);
  const fee = { tokenIn: 1_000_000_000n, tradeFee: 400_000n, tokenEstOut: 119_952_000n };   // fee is not impact
  assert.ok(M.priceImpact(fee, sqrt, true) < 1e-6);
  assert.ok(M.checkImpact(0.009));
  assert.throws(() => M.checkImpact(0.011), /exceeds the 1% limit/);
  assert.throws(() => M.checkImpact(NaN), /refusing/);
});

test('chooseCuPrice: property — limit x price never exceeds the cap', () => {
  const r = rng(5);
  for (let i = 0; i < 2000; i++) {
    const units = 1 + Math.floor(r() * 1_400_000);
    const recent = Array.from({ length: Math.floor(r() * 50) }, () => ({ prioritizationFee: Math.floor(r() * 1e8) }));
    const cap = Math.floor(r() * 1_000_000);
    const p = M.chooseCuPrice(recent, units, cap);
    assert.ok(p >= 0 && Number.isInteger(p));
    assert.ok(BigInt(units) * BigInt(p) <= BigInt(cap) * 1_000_000n, `${units} x ${p} > cap ${cap}`);
  }
  assert.equal(M.chooseCuPrice([], 100_000), 50_000);                       // default when nothing reported
  assert.equal(M.chooseCuPrice([{ prioritizationFee: 1 }], 100_000), 20_000);  // floor
  assert.equal(M.cuLimit(48_000), 65_000);
  assert.equal(M.cuLimit(10_000_000), 1_400_000);
});

// --- transaction shape -------------------------------------------------------------
const payer = Keypair.generate();
const owner = payer.publicKey.toBase58();
const wsolAta = spl.getAssociatedTokenAddressSync(new PublicKey(SOL), payer.publicKey, false).toBase58();
const usdcAta = spl.getAssociatedTokenAddressSync(new PublicKey(USDC), payer.publicKey, false).toBase58();
const vaultA = Keypair.generate().publicKey.toBase58(), vaultB = Keypair.generate().publicKey.toBase58();
const want = { payer: owner, pool: POOL, rawIn: 1000n, minOut: 900n, aToB: true, ownerA: wsolAta, ownerB: usdcAta, vaultA, vaultB, wsolAta };
const pk = (s) => new PublicKey(s);
const ro = (s) => ({ pubkey: pk(s), isSigner: false, isWritable: false });
const rw = (s) => ({ pubkey: pk(s), isSigner: false, isWritable: true });

function swapIx({ amount = 1000n, threshold = 900n, aToB = true, isInput = true, authority = owner, ownerB = usdcAta, pool = POOL,
  vA = vaultA, vB = vaultB, sqrtPriceLimit = 0n } = {}) {
  const data = Buffer.from(getSwapV2InstructionDataEncoder().encode({
    amount, otherAmountThreshold: threshold, sqrtPriceLimit, amountSpecifiedIsInput: isInput, aToB,
    remainingAccountsInfo: { __option: 'None' },
  }));
  const keys = [ro(TOKEN.toBase58()), ro(TOKEN.toBase58()), ro('MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr'),
    { pubkey: pk(authority), isSigner: true, isWritable: false }, rw(pool), ro(SOL), ro(USDC),
    rw(wsolAta), rw(vA), rw(ownerB), rw(vB),
    rw(Keypair.generate().publicKey.toBase58()), rw(Keypair.generate().publicKey.toBase58()),
    rw(Keypair.generate().publicKey.toBase58()), rw(Keypair.generate().publicKey.toBase58())];
  return new TransactionInstruction({ programId: pk(M.WHIRLPOOL_PROGRAM), keys, data });
}
const wrap = () => [
  spl.createAssociatedTokenAccountIdempotentInstruction(payer.publicKey, pk(wsolAta), payer.publicKey, pk(SOL)),
  SystemProgram.transfer({ fromPubkey: payer.publicKey, toPubkey: pk(wsolAta), lamports: 1000 }),
  spl.createSyncNativeInstruction(pk(wsolAta)),
];
const close = () => spl.createCloseAccountInstruction(pk(wsolAta), payer.publicKey, payer.publicKey);
const budget = () => [ComputeBudgetProgram.setComputeUnitLimit({ units: 80_000 }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 50_000 })];
function txOf(ixs, feePayer = payer.publicKey) {
  return new VersionedTransaction(new TransactionMessage({ payerKey: feePayer, recentBlockhash: '11111111111111111111111111111111', instructions: ixs }).compileToV0Message());
}
const good = () => [...budget(), ...wrap(), swapIx(), close()];

test('decodeSwapV2 reads what the Orca client encodes', () => {
  const d = M.decodeSwapV2(swapIx({ amount: 123456789n, threshold: 98765n, aToB: false }).data);
  assert.deepEqual(d, { amount: 123456789n, otherAmountThreshold: 98765n, sqrtPriceLimit: 0n, amountSpecifiedIsInput: true, aToB: false });
  assert.equal(M.decodeSwapV2(Buffer.alloc(42)), null);
  assert.equal(M.decodeSwapV2(Buffer.from([1, 2])), null);
});

test('verifyTxShape: the swap the SDK builds passes', () => {
  assert.ok(M.verifyTxShape(txOf(good()), want));
  assert.ok(M.verifyTxShape(txOf([...budget(), swapIx(), close()]), want));   // buying SOL: no wrap
});

test('verifyTxShape: refuses every deviation', () => {
  const other = Keypair.generate();
  const cases = [
    ['foreign program', [...good(), new TransactionInstruction({ programId: other.publicKey, keys: [], data: Buffer.alloc(0) })], /calls/],
    ['fee payer', null, /fee payer/],
    ['second signer', [...good(), SystemProgram.transfer({ fromPubkey: other.publicKey, toPubkey: payer.publicKey, lamports: 1 })], /signers other than the wallet|system instruction/],
    ['extra signer only', [new TransactionInstruction({ programId: ComputeBudgetProgram.programId, keys: [{ pubkey: other.publicKey, isSigner: true, isWritable: false }], data: ComputeBudgetProgram.setComputeUnitLimit({ units: 80_000 }).data }), swapIx()], /signers other than the wallet/],
    ['transfer elsewhere', [...budget(), SystemProgram.transfer({ fromPubkey: payer.publicKey, toPubkey: other.publicKey, lamports: 1 }), swapIx()], /system instruction/],
    ['token transfer', [...good(), spl.createTransferInstruction(pk(usdcAta), pk(vaultA), payer.publicKey, 1)], /token instruction 3/],
    ['close to someone else', [...budget(), swapIx(), spl.createCloseAccountInstruction(pk(wsolAta), other.publicKey, payer.publicKey)], /token instruction 9/],
    ['approve', [...good(), spl.createApproveInstruction(pk(usdcAta), other.publicKey, payer.publicKey, 1)], /token instruction/],
    ['amount', [...budget(), swapIx({ amount: 1001n })], /differs from the verified quote/],
    ['threshold', [...budget(), swapIx({ threshold: 1n })], /differs from the verified quote/],
    ['direction', [...budget(), swapIx({ aToB: false })], /differs from the verified quote/],
    ['exact out', [...budget(), swapIx({ isInput: false })], /differs from the verified quote/],
    ['pays elsewhere', [...budget(), swapIx({ ownerB: other.publicKey.toBase58() })], /not the wallet/],
    ['other pool', [...budget(), swapIx({ pool: other.publicKey.toBase58() })], /authority or pool/],
    ['two swaps', [...budget(), swapIx(), swapIx()], /2 swaps/],
    ['no swap', [...budget(), ...wrap(), close()], /0 swaps/],
    ['other whirlpool ix', [...budget(), new TransactionInstruction({ programId: pk(M.WHIRLPOOL_PROGRAM), keys: [], data: Buffer.alloc(48) })], /other than swapV2/],
    ['budget ix', [new TransactionInstruction({ programId: ComputeBudgetProgram.programId, keys: [], data: Buffer.from([1, 0, 0, 0, 0]) }), swapIx()], /compute-budget/],
    ['ata for another owner', [...budget(), spl.createAssociatedTokenAccountIdempotentInstruction(payer.publicKey, spl.getAssociatedTokenAddressSync(pk(SOL), other.publicKey), other.publicKey, pk(SOL)), swapIx()], /ATA instruction/],
  ];
  for (const [name, ixs, re] of cases) {
    const tx = name === 'fee payer' ? txOf(good(), other.publicKey) : txOf(ixs);
    assert.throws(() => M.verifyTxShape(tx, want), re, name);
    try { M.verifyTxShape(tx, want); } catch (e) { assert.equal(errorKind(e), 'fatal', name); }
  }
});

test('program allow-list is exactly the six programs', () => {
  assert.deepEqual([...M.ALLOWED_PROGRAMS].sort(), [
    '11111111111111111111111111111111', 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL',
    'ComputeBudget111111111111111111111111111111', 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
    'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb', M.WHIRLPOOL_PROGRAM].sort());
  assert.ok(!M.ALLOWED_PROGRAMS.has('JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'));
});

test('priority fee: the built budget is inside the cap; above it is refused', () => {
  assert.equal(M.priorityFeeLamports(txOf(good())), 4000n);
  assert.ok(M.verifyPriorityFee(txOf(good())));
  const big = [ComputeBudgetProgram.setComputeUnitLimit({ units: 1_000_000 }), ComputeBudgetProgram.setComputeUnitPrice({ microLamports: 1_000_000 }), swapIx()];
  assert.throws(() => M.verifyPriorityFee(txOf(big)), /exceeds the 500000 cap/);
  assert.equal(M.PRIORITY_MAX_LAMPORTS, 500_000);
});

test('withWsolClose appends one close to the wallet, once', () => {
  const ixs = [...budget(), swapIx()];
  const once = M.withWsolClose(ixs, wsolAta, owner);
  assert.equal(once.length, ixs.length + 1);
  assert.equal(M.withWsolClose(once, wsolAta, owner).length, once.length);
  assert.ok(M.verifyTxShape(txOf(once), want));
});

test('kitToWeb3 maps roles: bit 1 writable, bit 2 signer', () => {
  const a = (r) => ({ address: Keypair.generate().publicKey.toBase58(), role: r });
  const ix = M.kitToWeb3({ programAddress: M.WHIRLPOOL_PROGRAM, accounts: [a(0), a(1), a(2), a(3)], data: new Uint8Array([7, 8]) });
  assert.deepEqual(ix.keys.map(k => [k.isSigner, k.isWritable]), [[false, false], [false, true], [true, false], [true, true]]);
  assert.equal(ix.programId.toBase58(), M.WHIRLPOOL_PROGRAM);
  assert.deepEqual([...ix.data], [7, 8]);
});

test('checkDeltas: the simulation must match the quote', () => {
  const sides = { inNative: true, outNative: false, bothSpl: false };
  assert.ok(M.checkDeltas(sides, { inDrop: 1000n + 6000n, outGain: 900n, solDrop: 7000n }, 1000n, 900n));
  assert.throws(() => M.checkDeltas(sides, { inDrop: 1000n + 10_000_001n, outGain: 900n, solDrop: 0n }, 1000n, 900n), /takes/);
  assert.throws(() => M.checkDeltas(sides, { inDrop: 1000n, outGain: 899n, solDrop: 0n }, 1000n, 900n), /under the minimum/);
  const spl2 = { inNative: false, outNative: false, bothSpl: true };
  assert.throws(() => M.checkDeltas(spl2, { inDrop: 1001n, outGain: 900n, solDrop: 0n }, 1000n, 900n), /takes/);
  assert.throws(() => M.checkDeltas(spl2, { inDrop: 1000n, outGain: 900n, solDrop: 10_000_001n }, 1000n, 900n), /lamports of SOL/);
  const buySol = { inNative: false, outNative: true, bothSpl: false };
  assert.ok(M.checkDeltas(buySol, { inDrop: 1000n, outGain: 900n - 6000n, solDrop: 0n }, 1000n, 900n));   // fees come out of the SOL bought
});

test('loadOwner: execute needs the key; a dry run may use a public key only', async () => {
  await assert.rejects(M.loadOwner(true, {}), /WALLET_SECRET_PATH is not set/);
  await assert.rejects(M.loadOwner(true, { LPBOT_OWNER: owner }), /WALLET_SECRET_PATH is not set/);
  await assert.rejects(M.loadOwner(false, {}), /needs WALLET_SECRET_PATH or LPBOT_OWNER/);
  const dry = await M.loadOwner(false, { LPBOT_OWNER: owner });
  assert.equal(dry.payer, null); assert.equal(dry.owner.toBase58(), owner);
  const read = async () => payer.secretKey;
  const full = await M.loadOwner(true, { WALLET_SECRET_PATH: '/x' }, read);
  assert.equal(full.payer.publicKey.toBase58(), owner);
  await assert.rejects(M.loadOwner(true, { WALLET_SECRET_PATH: '/x', LPBOT_OWNER: Keypair.generate().publicKey.toBase58() }, read), /differs/);
});

test('parseHints keeps valid hints and ignores bad JSON', () => {
  const h = M.parseHints(JSON.stringify({ [SOL]: { usd: 120, decimals: 9, symbol: 'SOL' }, [USDC]: { usd: 0, decimals: 6 }, x: { usd: 1, decimals: 1.5 } }));
  assert.deepEqual([...h.keys()], [SOL]);
  assert.equal(h.get(SOL).usdPrice, 120);
  assert.equal(M.parseHints('{nope').size, 0);
  assert.equal(M.parseHints(undefined).size, 0);
});

test('toRaw has no float drift and refuses junk', () => {
  assert.equal(M.toRaw('0.1', 9), 100_000_000n);
  assert.equal(M.toRaw(0.1, 9), 100_000_000n);
  assert.equal(M.toRaw('1.1234567891', 9), 1_123_456_789n);
  assert.throws(() => M.toRaw('-1', 9), /bad amount/);
  assert.throws(() => M.toRaw('1e3', 9), /bad amount/);
});

// --- error kinds and the one send ----------------------------------------------------
test('error kinds: an RPC limit before signing rotates; refusals and anything after signing do not', async () => {
  assert.equal(errorKind(new Error('HTTP error (429): Too Many Requests')), 'rotate');
  assert.equal(errorKind(new M.Refused('quote pays $500.12; refusing')), 'fatal');
  assert.equal(errorKind(new M.SentError('sent x but 429')), 'fatal');
  assert.equal(errorKind(new AfterSignError('fetch failed')), 'fatal');
  let calls = 0;
  const r = await overEndpoints(['http://a', 'http://b'], async (u) => { calls++; if (u === 'http://a') throw new Error('429 Too Many Requests'); return u; }, { sleep: async () => {} });
  assert.equal(r, 'http://b'); assert.equal(calls, 3);
  for (const err of [new M.Refused('price impact 5% exceeds'), new M.SentError('sent abc'), new AfterSignError('send failed after signing: ETIMEDOUT')]) {
    calls = 0;
    await assert.rejects(overEndpoints(['http://a', 'http://b'], async () => { calls++; throw err; }, { sleep: async () => {} }), e => e === err);
    assert.equal(calls, 1, err.message);
  }
});

function signed() {
  const tx = txOf([SystemProgram.transfer({ fromPubkey: payer.publicKey, toPubkey: payer.publicKey, lamports: 1 })]);
  tx.sign([payer]);
  return tx;
}
const REPORT = { sold: { amount: 1 }, bought: { amount: 2 }, swapUsdValue: 1, priceImpactPct: 0, routePlan: ['Orca Whirlpool'], signature: null, sent: false };

// A connection for sendLanded: the signature is known after `landsAfter`
// status reads (never when null); the block height rises 10 per read.
function landing({ landsAfter = null, err = null, start = 100, firstSendThrows = false, knownLate = false } = {}) {
  const c = { sends: [], reads: 0, height: start };
  c.sendRawTransaction = async (raw, opts) => {
    c.sends.push(opts);
    if (c.sends.length === 1 && firstSendThrows) throw firstSendThrows === true ? new Error('fetch failed') : firstSendThrows;
    return 'SIG1';
  };
  c.getSignatureStatuses = async (sigs, opts) => {
    if (opts?.searchTransactionHistory) return { value: [knownLate ? { confirmationStatus: 'finalized', err } : null] };
    c.reads += 1;
    return { value: [landsAfter !== null && c.reads > landsAfter ? { confirmationStatus: 'confirmed', err } : null] };
  };
  c.getBlockHeight = async () => (c.height += 10);
  c.confirmTransaction = async () => assert.fail('sendLanded never waits on confirmTransaction');
  return c;
}
const fast = { sleep: async () => {} };

test('sendLanded: a send that throws without a signature is AfterSignError, sent once', async () => {
  const conn = landing({ firstSendThrows: true });
  const out = [];
  await assert.rejects(M.sendLanded(conn, signed(), 1_000, REPORT, { log: s => out.push(s), ...fast }),
                       e => e instanceof AfterSignError && /not retried/.test(e.message));
  assert.equal(conn.sends.length, 1);
  assert.deepEqual(out, []);
});

test('sendLanded: the same signed bytes are re-sent until they land (2026-10-09 expiry)', async () => {
  const sent = [];
  const conn = landing({ landsAfter: 3 });
  const raw0 = conn.sendRawTransaction;
  conn.sendRawTransaction = async (raw, opts) => { sent.push(Buffer.from(raw).toString('hex')); return raw0(raw, opts); };
  const res = await M.sendLanded(conn, signed(), 10_000, REPORT, { log: () => {}, ...fast });
  assert.equal(res.signature, 'SIG1'); assert.equal(res.sent, true); assert.equal(res.partial, undefined);
  assert.equal(sent.length, 4, 'one send and three re-sends before the fourth read sees it');
  assert.equal(new Set(sent).size, 1, 'never a new transaction');
});

test('sendLanded: expired and unknown to the chain is a plain error, no signature, nothing logged', async () => {
  const out = [];
  await assert.rejects(M.sendLanded(landing({ landsAfter: null, start: 200 }), signed(), 100, REPORT, { log: s => out.push(s), ...fast }), e => {
    assert.strictEqual(e.constructor, Error);
    assert.match(e.message, /^swap expired: .*nothing was sent$/);
    assert.ok(!(e instanceof M.SentError) && !(e instanceof AfterSignError));
    return true;
  });
  assert.deepEqual(out, []);
});

test('sendLanded: landed after the block height passed is a success, not an expiry', async () => {
  const res = await M.sendLanded(landing({ landsAfter: null, start: 200, knownLate: true }), signed(), 100, REPORT, { log: () => {}, ...fast });
  assert.equal(res.signature, 'SIG1'); assert.equal(res.sent, true);
});

test('sendLanded: an on-chain failure is partial with the signature; success has the caller\'s JSON shape', async () => {
  const out = [];
  await assert.rejects(M.sendLanded(landing({ landsAfter: 0, err: { InstructionError: [3, { Custom: 6000 }] } }), signed(), 1_000, REPORT,
                                    { log: s => out.push(s), ...fast }), e => e instanceof M.SentError && errorKind(e) === 'fatal');
  const j = JSON.parse(out[0]);
  assert.equal(j.partial, true); assert.equal(j.signature, 'SIG1'); assert.equal(j.sent, true); assert.match(j.error, /failed on chain/);
  const res = await M.sendLanded(landing({ landsAfter: 0 }), signed(), 4242, REPORT, { log: () => {}, ...fast });
  assert.equal(res.sent, true); assert.equal(res.signature, 'SIG1'); assert.equal(res.partial, undefined);
  for (const k of ['sold', 'bought', 'swapUsdValue', 'priceImpactPct', 'routePlan', 'signature', 'sent']) assert.ok(k in res, k);
  assert.deepEqual(res.routePlan, ['Orca Whirlpool']);
});

test('noop report has the caller\'s shape', () => {
  const r = M.noopReport('within 2%', { owner, A: {}, B: {} });
  assert.equal(r.noop, true); assert.equal(r.reason, 'within 2%'); assert.equal(r.owner, owner);
  assert.equal(r.sent, undefined); assert.equal(r.signature, undefined);
});

// --- exact boundaries and partial conditions (mutation gaps) -------------------------
test('toRaw: empty, a lone dot and two dots are refused; a number is read with its decimals', () => {
  for (const bad of ['', '.', '1.2.3', 'abc']) assert.throws(() => M.toRaw(bad, 9), /bad amount/, JSON.stringify(bad));
  assert.equal(M.toRaw('.5', 9), 500_000_000n);
  assert.equal(M.toRaw('5.', 9), 5_000_000_000n);
  assert.equal(M.toRaw(1e-7, 9), 100n);            // String(1e-7) is '1e-7': only toFixed reads it
  assert.equal(M.toRaw(2, 6), 2_000_000n);
  assert.equal(M.toRaw('2', 6), 2_000_000n);
});

test('parseHints keeps the given symbol and falls back to the mint prefix', () => {
  const h = M.parseHints(JSON.stringify({ [SOL]: { usd: 120, decimals: 9, symbol: 'SOL' }, [USDC]: { usd: 1, decimals: 6 } }));
  assert.equal(h.get(SOL).symbol, 'SOL');
  assert.equal(h.get(USDC).symbol, USDC.slice(0, 6));
});

test('planSwap: a positive amount gives its raw units', () => {
  const s = M.planSwap({ sellSide: 'A', sellUsd: 120 }, infoSol, infoUsdc, 2, 0);
  assert.equal(s.rawIn, 1_000_000_000n); assert.equal(s.amount, 1);
  assert.equal(M.planSwap({ sellSide: 'B', sellUsd: 50 }, infoSol, infoUsdc, 0, 30).rawIn, 30_000_000n);
});

test('priceImpact: no input after the fee is the full impact; a bad fee is not zero', () => {
  const sqrt = BigInt(Math.floor(Math.sqrt(120 * 1e6 / 1e9) * 2 ** 64));
  assert.equal(M.priceImpact({ tokenIn: 1000n, tradeFee: 1000n, tokenEstOut: 5n }, sqrt, true), 1);
  assert.equal(M.priceImpact({ tokenIn: 0n, tradeFee: 0n, tokenEstOut: 0n }, sqrt, true), 1);
  assert.throws(() => M.priceImpact({ tokenIn: 1000n, tradeFee: NaN, tokenEstOut: 5n }, sqrt, true));
  const noFee = { tokenIn: 1_000_000_000n, tokenEstOut: 120_000_000n };
  assert.ok(M.priceImpact(noFee, sqrt, true) < 1e-6);   // a missing fee is zero
});

test('valueLossOk: exactly at the cap passes; a zero input passes with zero output', () => {
  assert.equal(M.valueLossOk(100, 98, 0.02), true);
  assert.equal(M.valueLossOk(100, 50, 0.5), true);
  assert.equal(M.valueLossOk(100, 49.999999, 0.5), false);
  assert.equal(M.valueLossOk(0, 0, 0.02), true);
});

test('checkImpact: exactly at the limit passes', () => {
  assert.equal(M.MAX_IMPACT, 0.01);
  assert.ok(M.checkImpact(0.01));
  assert.ok(M.checkImpact(0.005, 0.005));
  assert.throws(() => M.checkImpact(0.0050001, 0.005), /exceeds/);
});

test('chooseCuPrice: zero fees are not counted; a non-list is not the default', () => {
  const recent = [0, 0, 0, 0, { prioritizationFee: 100_000 }].map(f => (typeof f === 'number' ? { prioritizationFee: f } : f));
  assert.equal(M.chooseCuPrice(recent, 100_000), 100_000);
  assert.equal(M.chooseCuPrice([0, 0, 0, 0, 100_000], 100_000), 100_000);
  assert.equal(M.chooseCuPrice(null, 100_000), 50_000);
  assert.throws(() => M.chooseCuPrice(0, 100_000));
});

test('withWsolClose: only a token-program CloseAccount of the wSOL ATA counts', () => {
  const other = Keypair.generate().publicKey;
  const cases = [
    ['wrap, no close', [...budget(), ...wrap(), swapIx()]],                                   // SyncNative: token program, key 0 = wSOL ATA
    ['close of another account', [...budget(), swapIx(), spl.createCloseAccountInstruction(other, payer.publicKey, payer.publicKey)]],
    ['data 9 from another program', [...budget(), swapIx(), new TransactionInstruction({ programId: pk(M.WHIRLPOOL_PROGRAM), keys: [rw(wsolAta)], data: Buffer.from([9]) })]],
    ['token ix 9 at a non-token program', [...budget(), swapIx(), new TransactionInstruction({ programId: other, keys: [rw(wsolAta)], data: Buffer.from([9]) })]],
  ];
  for (const [name, ixs] of cases) {
    const out = M.withWsolClose(ixs, wsolAta, owner);
    assert.equal(out.length, ixs.length + 1, name);
    const last = out[out.length - 1];
    assert.equal(last.programId.toBase58(), TOKEN.toBase58(), name);
    assert.equal(last.data[0], 9, name);
    assert.equal(last.keys[0].pubkey.toBase58(), wsolAta, name);
  }
  const done = [...budget(), ...wrap(), swapIx(), close()];
  assert.equal(M.withWsolClose(done, wsolAta, owner), done);
});

test('decodeSwapV2: exactly 42 bytes decode; the sqrt price high word is shifted 64 bits', () => {
  const full = Buffer.from(swapIx({ sqrtPriceLimit: (3n << 64n) + 5n }).data);
  assert.equal(full.length > 42, true);
  const d42 = M.decodeSwapV2(full.subarray(0, 42));
  assert.ok(d42);
  assert.equal(d42.sqrtPriceLimit, (3n << 64n) + 5n);
  assert.equal(d42.amount, 1000n);
  assert.equal(M.decodeSwapV2(full.subarray(0, 41)), null);
  const max = (1n << 128n) - 1n;
  assert.equal(M.decodeSwapV2(swapIx({ sqrtPriceLimit: max }).data).sqrtPriceLimit, max);
});

test('verifyTxShape: ATA create forms pass, others and a foreign funder are refused', () => {
  const base = spl.createAssociatedTokenAccountIdempotentInstruction(payer.publicKey, pk(wsolAta), payer.publicKey, pk(SOL));
  const ata = (data, keys = base.keys) => new TransactionInstruction({ programId: base.programId, keys, data: Buffer.from(data) });
  for (const data of [[], [0], [1]]) assert.ok(M.verifyTxShape(txOf([...budget(), ata(data), swapIx()]), want), JSON.stringify(data));
  for (const data of [[2], [3], [2, 0]]) {
    assert.throws(() => M.verifyTxShape(txOf([...budget(), ata(data), swapIx()]), want), /ATA instruction/, JSON.stringify(data));
  }
  const other = Keypair.generate().publicKey;
  const funder = [ro(other.toBase58()), ...base.keys.slice(1)];            // not a signer: one signature only
  const tx = txOf([...budget(), ata([1], funder), swapIx()]);
  assert.equal(tx.message.header.numRequiredSignatures, 1);
  assert.throws(() => M.verifyTxShape(tx, want), /ATA instruction/);
  const ownerKeys = [...base.keys.slice(0, 2), ro(other.toBase58()), ...base.keys.slice(3)];
  assert.throws(() => M.verifyTxShape(txOf([...budget(), ata([1], ownerKeys), swapIx()]), want), /ATA instruction/);
});

test('verifyTxShape: vault A or vault B alone differing is refused', () => {
  const other = Keypair.generate().publicKey.toBase58();
  assert.throws(() => M.verifyTxShape(txOf([...budget(), swapIx({ vA: other })]), want), /vaults/);
  assert.throws(() => M.verifyTxShape(txOf([...budget(), swapIx({ vB: other })]), want), /vaults/);
});

test('verifyTxShape: lookup tables refused; an empty or missing list passes', () => {
  const tx = txOf([...budget(), swapIx()]);
  assert.deepEqual(tx.message.addressTableLookups, []);
  assert.ok(M.verifyTxShape(tx, want));
  const missing = { message: { ...tx.message, staticAccountKeys: tx.message.staticAccountKeys, header: tx.message.header, compiledInstructions: tx.message.compiledInstructions, addressTableLookups: undefined } };
  assert.ok(M.verifyTxShape(missing, want));
  const withTable = { message: { ...missing.message, addressTableLookups: [{ accountKey: pk(owner), writableIndexes: [0], readonlyIndexes: [] }] } };
  assert.throws(() => M.verifyTxShape(withTable, want), /lookup tables/);
});

test('sendLanded: the injected sleep paces every re-send', async () => {
  const waits = [];
  await M.sendLanded(landing({ landsAfter: 2 }), signed(), 10_000, REPORT, { log: () => {}, sleep: async ms => { waits.push(ms); } });
  assert.deepEqual(waits, [2_000, 2_000]);
});

test('sendLanded: an empty error after the send is logged as it is', async () => {
  const conn = landing({ landsAfter: 0 });
  conn.getSignatureStatuses = async () => { throw new Error(''); };
  const out = [];
  await assert.rejects(M.sendLanded(conn, signed(), 1_000, REPORT, { log: s => out.push(s), ...fast }), M.SentError);
  assert.strictEqual(JSON.parse(out[0]).error, '');
});

test('sendLanded: the error text carries the message verbatim, even an empty one', async () => {
  await assert.rejects(M.sendLanded(landing({ firstSendThrows: new Error('') }), signed(), 1_000, REPORT, { log: () => {}, ...fast }),
                       e => e instanceof AfterSignError && e.message === 'send failed after signing (not retried): ');
  await assert.rejects(M.sendLanded(landing({ firstSendThrows: 'boom' }), signed(), 1_000, REPORT, { log: () => {}, ...fast }),
                       e => e instanceof AfterSignError && e.message === 'send failed after signing (not retried): boom');
});

// --- LPBOT_SLEEVE: a wallet several profiles share ------------------------------------
// The fallback counts and sells at most the profile's sleeve, as swap_jupiter.mjs
// does; a sleeve that cannot be read refuses the swap.
function fakeConnection({ lamports = 0n, usdcRaw = 0n, wsolRaw = null }) {
  const account = (raw) => { const d = Buffer.alloc(165); d.writeBigUInt64LE(BigInt(raw), 64); return { data: d }; };
  return {
    getBalance: async () => Number(lamports),
    getAccountInfo: async (key) => {
      const k = key.toBase58();
      const usdcAta = spl.getAssociatedTokenAddressSync(new PublicKey(USDC), OWNER, false, TOKEN).toBase58();
      const wsolAta = spl.getAssociatedTokenAddressSync(new PublicKey(SOL), OWNER, false, TOKEN).toBase58();
      if (k === usdcAta) return account(usdcRaw);
      if (k === wsolAta) return wsolRaw == null ? null : account(wsolRaw);
      return null;
    },
  };
}
const OWNER = Keypair.generate().publicKey;
const USDC_INFO = { mint: USDC, decimals: 6, program: TOKEN.toBase58(), usdPrice: 1 };
const SOL_INFO = { mint: SOL, decimals: 9, program: TOKEN.toBase58(), usdPrice: 150 };

async function withSleeve(sleeve, fn) {
  const saved = process.env.LPBOT_SLEEVE;
  if (sleeve === undefined) delete process.env.LPBOT_SLEEVE; else process.env.LPBOT_SLEEVE = sleeve;
  try { return await fn(); } finally {
    if (saved === undefined) delete process.env.LPBOT_SLEEVE; else process.env.LPBOT_SLEEVE = saved;
  }
}

test('sleeve: the fallback sells at most the profile\'s share of a shared wallet', async () => {
  const conn = fakeConnection({ usdcRaw: 500_000_000n });             // 500 USDC in the wallet
  const none = await withSleeve(undefined, () => M.sellable(conn, OWNER, USDC_INFO));
  assert.equal(none.avail, 500);
  const capped = await withSleeve(JSON.stringify({ [USDC]: 40 }), () => M.sellable(conn, OWNER, USDC_INFO));
  assert.deepEqual([capped.total, capped.avail, capped.usd], [40, 40, 40]);
  const other = await withSleeve(JSON.stringify({ [SOL]: 1 }), () => M.sellable(conn, OWNER, USDC_INFO));
  assert.equal(other.avail, 0);                                     // a mint the sleeve does not name: nothing
});

test('sleeve: native SOL keeps the gas reserve inside the sleeve', async () => {
  const conn = fakeConnection({ lamports: 2_000_000_000n, wsolRaw: 0n });
  const r = await withSleeve(JSON.stringify({ [SOL]: 1 }), () => M.sellable(conn, OWNER, SOL_INFO));
  assert.equal(r.total, 1);
  assert.ok(Math.abs(r.avail - 0.95) < 1e-12);
});

test('sleeve: wrapped SOL counts with the lamports, then the sleeve caps both', async () => {
  const conn = fakeConnection({ lamports: 1_000_000_000n, wsolRaw: 500_000_000n });
  const all = await withSleeve(undefined, () => M.sellable(conn, OWNER, SOL_INFO));
  assert.ok(Math.abs(all.total - 1.5) < 1e-12);
  const capped = await withSleeve(JSON.stringify({ [SOL]: 1.2 }), () => M.sellable(conn, OWNER, SOL_INFO));
  assert.equal(capped.total, 1.2);
});

test('sleeve: one that cannot be read refuses the swap, never means the whole wallet', async () => {
  const conn = fakeConnection({ usdcRaw: 500_000_000n });
  for (const bad of ['{', '[1]', JSON.stringify({ [USDC]: -1 }), JSON.stringify({ [USDC]: '40' })]) {
    await assert.rejects(withSleeve(bad, () => M.sellable(conn, OWNER, USDC_INFO)), /LPBOT_SLEEVE/, bad);
  }
});
