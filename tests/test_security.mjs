// Security tests for the Jupiter verification. Offline: the functions under
// test are pure checks on a quote and a transaction's shape.
import test from 'node:test';
import assert from 'node:assert';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { PublicKey, TransactionMessage, VersionedTransaction, SystemProgram, Keypair } = require('@solana/web3.js');
process.env.LPBOT_SLIPPAGE_BPS = '100';
const { verifyQuote, verifyTxShape } = await import('../swap_jupiter.mjs');

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
  assert.throws(() => verifyQuote({ ...fair, slippageBps: 5000 }, USDC, SOL, 12000000n), /slippage/);
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
