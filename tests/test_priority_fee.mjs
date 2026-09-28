// The swap's priority fee: requested with a ceiling, and checked against it
// before anything is signed (2026-09-28: 'auto' had no ceiling).
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { PublicKey, TransactionMessage, VersionedTransaction, ComputeBudgetProgram, Keypair, SystemProgram } = require('@solana/web3.js');
const { swapRequestBody, priorityFeeLamports, verifyPriorityFee, PRIORITY_MAX_LAMPORTS } = await import('../swap_jupiter.mjs');

const kp = Keypair.generate();
function tx(budget, others = 1) {
  const ixs = [...budget];
  for (let i = 0; i < others; i++) ixs.push(SystemProgram.transfer({ fromPubkey: kp.publicKey, toPubkey: kp.publicKey, lamports: 1 }));
  const msg = new TransactionMessage({ payerKey: kp.publicKey, recentBlockhash: '11111111111111111111111111111111', instructions: ixs }).compileToV0Message();
  return new VersionedTransaction(msg);
}
const limit = (u) => ComputeBudgetProgram.setComputeUnitLimit({ units: u });
const price = (p) => ComputeBudgetProgram.setComputeUnitPrice({ microLamports: p });

test('the request asks for the auto fee, the cheapest measured', () => {
  const b = swapRequestBody({ inAmount: '1', __quotedAt: 5 }, 'PAYER');
  assert.equal(b.prioritizationFeeLamports, 'auto');
  assert.equal(b.userPublicKey, 'PAYER');
  assert.deepEqual(b.quoteResponse, { inAmount: '1' });                 // internal fields stripped
  assert.equal(b.wrapAndUnwrapSol, true); assert.equal(b.dynamicComputeUnitLimit, true);
  assert.equal(PRIORITY_MAX_LAMPORTS, 500_000);
});

test('the fee is units times price, in lamports, rounded up', () => {
  assert.equal(priorityFeeLamports(tx([limit(300_000), price(100_000)])), 30_000n);
  assert.equal(priorityFeeLamports(tx([price(100_000), limit(300_000)])), 30_000n);   // order does not matter
  assert.equal(priorityFeeLamports(tx([limit(3), price(1)])), 1n);                     // 3e-6 rounds up to 1
  assert.equal(priorityFeeLamports(tx([limit(300_000)])), 0n);                         // no price: no priority fee
  assert.equal(priorityFeeLamports(tx([])), 0n);
});

test('without a unit limit the runtime default applies', () => {
  assert.equal(priorityFeeLamports(tx([price(1_000_000)], 2)), 400_000n);   // 2 instructions x 200k units
  assert.equal(priorityFeeLamports(tx([price(1_000_000)], 9)), 1_400_000n); // capped at 1.4M units
  assert.equal(priorityFeeLamports(tx([price(1_000_000)], 0)), 0n);
});

test('a fee at the cap passes; one lamport over is refused', () => {
  assert.ok(verifyPriorityFee(tx([limit(500_000), price(1_000_000)])));             // exactly 500,000
  assert.throws(() => verifyPriorityFee(tx([limit(500_001), price(1_000_000)])), /exceeds the 500000 cap/);
  assert.ok(verifyPriorityFee(tx([limit(1_400_000), price(71_428)])));              // what auto asks today: ~100k
  assert.ok(verifyPriorityFee(tx([limit(1_000), price(1_000_000)]), 1_000));
  assert.throws(() => verifyPriorityFee(tx([limit(1_001), price(1_000_000)]), 1_000), /refusing/);
});

test('a malformed budget instruction is ignored, not misread', () => {
  const short = { programId: ComputeBudgetProgram.programId, keys: [], data: Buffer.from([3, 1]) };
  const shortLimit = { programId: ComputeBudgetProgram.programId, keys: [], data: Buffer.from([2, 1]) };
  assert.equal(priorityFeeLamports(tx([short, limit(1000), price(1000)])), 1n);
  // limit unreadable: the default, 200k units per instruction that is not a budget one (1 transfer)
  assert.equal(priorityFeeLamports(tx([shortLimit, price(1_000_000)], 1)), 200_000n);
});

test('an explicit zero unit limit means zero units, not the default', () => {
  assert.equal(priorityFeeLamports(tx([limit(0), price(1_000_000)], 3)), 0n);
});
