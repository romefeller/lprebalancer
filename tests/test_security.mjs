// Security tests for the Jupiter verification. Offline: the functions under
// test are pure checks on a quote and a transaction's shape.
import test from 'node:test';
import assert from 'node:assert';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { PublicKey, TransactionMessage, VersionedTransaction, SystemProgram, Keypair } = require('@solana/web3.js');
process.env.LPBOT_SLIPPAGE_BPS = '100';
process.env.LPBOT_MAX_VALUE_LOSS = '0.25';             // with slippage 0: a limit of exactly 3/4, exact in binary
const { verifyQuote, verifyTxShape, verifyInstructions } = await import('../venues/jupiter/swap.mjs');

const SOL = { mint: 'So11111111111111111111111111111111111111112', symbol: 'SOL', decimals: 9, usdPrice: 120 };
const USDC = { mint: 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', symbol: 'USDC', decimals: 6, usdPrice: 1 };
const fair = { inputMint: USDC.mint, outputMint: SOL.mint, inAmount: '12000000', outAmount: '100000000',
  otherAmountThreshold: '99000000', swapMode: 'ExactIn', slippageBps: 100 };

test('a fair quote passes', () => { assert.ok(verifyQuote(fair, USDC, SOL, 12000000n)); });
test('a quote for other mints is refused', () => {
  assert.throws(() => verifyQuote({ ...fair, outputMint: USDC.mint }, USDC, SOL, 12000000n), /mints/);
});
test('a quote for another amount is refused', () => {
  assert.throws(() => verifyQuote({ ...fair, inAmount: '99999999' }, USDC, SOL, 12000000n), /inAmount/);
});
test('a quote at half the fair value is refused', () => {
  assert.throws(() => verifyQuote({ ...fair, outAmount: '50000000', otherAmountThreshold: '49500000' }, USDC, SOL, 12000000n), /fair value/);
});
test('an exact-out or wide-slippage quote is refused', () => {
  assert.throws(() => verifyQuote({ ...fair, swapMode: 'ExactOut' }, USDC, SOL, 12000000n), /swapMode/);
  assert.throws(() => verifyQuote({ ...fair, swapMode: '' }, USDC, SOL, 12000000n), /swapMode/);     // only an absent mode is ExactIn
  assert.throws(() => verifyQuote({ ...fair, slippageBps: 5000 }, USDC, SOL, 12000000n), /slippage/);
});
test('the edges of the quote checks', () => {
  // the minimum may equal the output; a quote worth exactly the limit passes, one lamport under it does not
  assert.ok(verifyQuote({ ...fair, otherAmountThreshold: fair.outAmount }, USDC, SOL, 12000000n));
  const atLimit = { ...fair, inAmount: '16000000', slippageBps: 0, outAmount: '100000000', otherAmountThreshold: '100000000' };   // $16 in, $12 at worst
  assert.ok(verifyQuote(atLimit, USDC, SOL, 16000000n));
  assert.throws(() => verifyQuote({ ...atLimit, otherAmountThreshold: '99999999' }, USDC, SOL, 16000000n), /fair value/);
  // no price on one side: the value check cannot run, and the quote is not refused on a guess
  assert.ok(verifyQuote({ ...fair, otherAmountThreshold: '1', outAmount: '1' }, USDC, { ...SOL, usdPrice: null }, 12000000n));
});

function txWith(programIds, payer) {
  const ixs = programIds.map(pid => ({ programId: new PublicKey(pid), keys: [{ pubkey: payer.publicKey, isSigner: true, isWritable: true }], data: Buffer.alloc(0) }));
  const msg = new TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: '11111111111111111111111111111111', instructions: ixs }).compileToV0Message();
  return new VersionedTransaction(msg);
}
test('a transaction calling only allowed programs passes', () => {
  const kp = Keypair.generate();
  assert.ok(verifyTxShape(txWith(['ComputeBudget111111111111111111111111111111', 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'], kp), kp.publicKey.toBase58()));
});
test('a transaction calling an unknown program is refused', () => {
  const kp = Keypair.generate();
  assert.throws(() => verifyTxShape(txWith(['JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4', 'Evi1Program11111111111111111111111111111111'], kp), kp.publicKey.toBase58()), /refusing/);
});
test('a transaction paid by another key is refused', () => {
  const kp = Keypair.generate(), other = Keypair.generate();
  assert.throws(() => verifyTxShape(txWith(['JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'], other), kp.publicKey.toBase58()), /fee payer/);
});

// --- instructions a swap never needs (review, 2026-10-09) -------------------------------
// The program allow-list lets the Token and System programs through, and the
// balance simulation compares two token accounts and the SOL balance. A
// delegate, a new authority or an Assign moves no balance and hands the
// account over later. Refused by instruction, before anything is signed.
const TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', T22 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb';
const SYS = '11111111111111111111111111111111', JUP = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4';
const ATA = 'ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL';
const u32 = (n) => { const b = Buffer.alloc(4); b.writeUInt32LE(n); return b; };
function txOf(ixs, payer) {
  const other = Keypair.generate().publicKey;
  const built = ixs.map(([pid, data]) => ({ programId: new PublicKey(pid), data: Buffer.from(data),
    keys: [{ pubkey: payer.publicKey, isSigner: true, isWritable: true }, { pubkey: other, isSigner: false, isWritable: true }] }));
  const msg = new TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: '11111111111111111111111111111111', instructions: built }).compileToV0Message();
  return new VersionedTransaction(msg);
}
const SWAP_IXS = [[ATA, []], [SYS, Buffer.concat([u32(2), Buffer.alloc(8)])], [TOKEN, [17]], [JUP, [1, 2, 3]], [TOKEN, [9]], [TOKEN, []],
  [T22, [12, 0, 0, 0, 0, 0, 0, 0, 0, 6]]];
test("a swap's own instructions pass: ATA create, SOL transfer, SyncNative, route, CloseAccount, TransferChecked", () => {
  const kp = Keypair.generate();
  assert.ok(verifyInstructions(txOf(SWAP_IXS, kp)));
  assert.ok(verifyTxShape(txOf(SWAP_IXS, kp), kp.publicKey.toBase58()));
});
test('a delegate, a new authority or a burn on either Token program is refused', () => {
  const kp = Keypair.generate();
  for (const prog of [TOKEN, T22]) {
    for (const [first, name] of [[4, 'Approve'], [6, 'SetAuthority'], [8, 'Burn'], [13, 'ApproveChecked'], [15, 'BurnChecked']]) {
      const ixs = [...SWAP_IXS, [prog, [first, 1, 0, 0, 0, 0, 0, 0, 0]]];
      assert.throws(() => verifyInstructions(txOf(ixs, kp)), new RegExp(`${name}.*refusing`), `${prog} ${name}`);
    }
  }
});
test('an Assign of an account to another program is refused', () => {
  const kp = Keypair.generate();
  for (const [index, name] of [[1, 'Assign'], [10, 'AssignWithSeed']]) {
    const ixs = [...SWAP_IXS, [SYS, Buffer.concat([u32(index), Buffer.alloc(32)])]];
    assert.throws(() => verifyInstructions(txOf(ixs, kp)), new RegExp(`${name}.*refusing`), name);
  }
});
test('the shortest payloads count: a one-byte Approve, a four-byte Assign', () => {
  const kp = Keypair.generate();
  assert.throws(() => verifyInstructions(txOf([...SWAP_IXS, [TOKEN, [4]]], kp)), /Approve.*refusing/);
  assert.throws(() => verifyInstructions(txOf([...SWAP_IXS, [SYS, u32(1)]], kp)), /Assign.*refusing/);
});
test('the same first byte on another program is not a Token instruction', () => {
  const kp = Keypair.generate();
  assert.ok(verifyInstructions(txOf([[JUP, [4, 0, 0]], [ATA, [6]], [SYS, [1]]], kp)));     // a 1-byte System payload is no index
});
