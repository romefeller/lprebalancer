// janitor.mjs: which empty accounts may be closed, and that a close tx can
// only return rent to the wallet.
import test from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Transaction, SystemProgram } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const { planClose, closeInstructions, verifyCloseTx, TOKEN_PROGRAMS, MAX_PER_TX } = await import('../janitor.mjs');

const OWNER = Keypair.generate().publicKey.toBase58();
const OTHER = Keypair.generate().publicKey.toBase58();
const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', SOL = 'So11111111111111111111111111111111111111112';
const MSOL = 'mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So';
const acct = (over = {}, info = {}) => ({ pubkey: Keypair.generate().publicKey.toBase58(), program: TOKEN_PROGRAMS[0], lamports: 2039280,
  ...over, info: { mint: MSOL, owner: OWNER, state: 'initialized', tokenAmount: { amount: '0' }, closeAuthority: null, ...info } });

test('an empty account of another mint is closed', () => {
  const a = acct();
  assert.deepEqual(planClose([a], OWNER, [USDC, SOL]), [a]);
});

test('kept mints, balances, foreign owners, frozen, other programs and foreign close authorities are not', () => {
  const cases = [
    acct({}, { mint: USDC }), acct({}, { tokenAmount: { amount: '1' } }), acct({}, { tokenAmount: { amount: '3272695' } }),
    acct({}, { owner: OTHER }), acct({}, { state: 'frozen' }), acct({ program: SystemProgram.programId.toBase58() }),
    acct({}, { closeAuthority: OTHER }), { pubkey: 'x', program: TOKEN_PROGRAMS[0], lamports: 1 },
    acct({ program: TOKEN_PROGRAMS[1] }, { extensions: [{ extension: 'transferFeeAmount', state: { withheldAmount: 5 } }] }),
  ];
  assert.deepEqual(planClose(cases, OWNER, [USDC, SOL]), []);
  // the owner as close authority, Token-2022, and nothing withheld: allowed
  const ok = [acct({}, { closeAuthority: OWNER }), acct({ program: TOKEN_PROGRAMS[1] }, { extensions: [{ extension: 'transferFeeAmount', state: { withheldAmount: 0 } }] })];
  assert.equal(planClose(ok, OWNER, []).length, 2);
});

test('property: nothing kept, non-empty or foreign is ever planned', () => {
  const arb = fc.record({ keep: fc.boolean(), amount: fc.constantFrom('0', '1', '99'), mine: fc.boolean(),
                          state: fc.constantFrom('initialized', 'frozen'), prog: fc.constantFrom(...TOKEN_PROGRAMS, 'X') });
  fc.assert(fc.property(fc.array(arb, { maxLength: 12 }), (rows) => {
    const accs = rows.map(r => acct({ program: r.prog }, { mint: r.keep ? USDC : MSOL, owner: r.mine ? OWNER : OTHER,
                                                         tokenAmount: { amount: r.amount }, state: r.state }));
    for (const p of planClose(accs, OWNER, [USDC])) {
      assert.equal(p.info.tokenAmount.amount, '0'); assert.equal(p.info.owner, OWNER);
      assert.notEqual(p.info.mint, USDC); assert.equal(p.info.state, 'initialized'); assert.ok(TOKEN_PROGRAMS.includes(p.program));
    }
  }), { numRuns: 300 });
});

function txOf(ixs, payer = OWNER) {
  const tx = ixs.length ? new Transaction().add(...ixs) : new Transaction(); tx.feePayer = new PublicKey(payer); return tx;
}

test('the close tx returns rent to the wallet and passes verification', () => {
  const plan = [acct(), acct({ program: TOKEN_PROGRAMS[1] })];
  const ixs = closeInstructions(plan, OWNER);
  assert.equal(ixs.length, 2);
  for (const [i, ix] of ixs.entries()) {
    assert.equal(ix.programId.toBase58(), plan[i].program);
    assert.deepEqual(ix.keys.map(k => k.pubkey.toBase58()), [plan[i].pubkey, OWNER, OWNER]);
    assert.deepEqual([...ix.data], [9]);
  }
  assert.ok(verifyCloseTx(txOf(ixs), OWNER, plan));
});

test('verification refuses anything else', () => {
  const plan = [acct()];
  const good = closeInstructions(plan, OWNER);
  assert.throws(() => verifyCloseTx(txOf(good, OTHER), OWNER, plan), /fee payer/);
  assert.throws(() => verifyCloseTx(txOf([]), OWNER, plan), /instruction count/);
  const toOther = spl.createCloseAccountInstruction(new PublicKey(plan[0].pubkey), new PublicKey(OTHER), new PublicKey(OWNER));
  assert.throws(() => verifyCloseTx(txOf([toOther]), OWNER, plan), /someone else/);
  const byOther = spl.createCloseAccountInstruction(new PublicKey(plan[0].pubkey), new PublicKey(OWNER), new PublicKey(OTHER));
  assert.throws(() => verifyCloseTx(txOf([byOther]), OWNER, plan), /someone else/);
  const notPlanned = closeInstructions([acct()], OWNER);
  assert.throws(() => verifyCloseTx(txOf(notPlanned), OWNER, plan), /not in the plan/);
  const transfer = SystemProgram.transfer({ fromPubkey: new PublicKey(OWNER), toPubkey: new PublicKey(OTHER), lamports: 1 });
  assert.throws(() => verifyCloseTx(txOf([transfer]), OWNER, plan), /program/);
  const tokTransfer = spl.createTransferInstruction(new PublicKey(plan[0].pubkey), new PublicKey(OTHER), new PublicKey(OWNER), 1);
  assert.throws(() => verifyCloseTx(txOf([tokTransfer]), OWNER, plan), /other than CloseAccount/);
  const many = Array.from({ length: MAX_PER_TX + 1 }, () => acct());
  assert.throws(() => verifyCloseTx(txOf(closeInstructions(many, OWNER)), OWNER, many), /instruction count/);
  assert.ok(verifyCloseTx(txOf(closeInstructions(many.slice(0, MAX_PER_TX), OWNER)), OWNER, many));
});

test('a one-byte instruction that is not CloseAccount is refused', () => {
  const plan = [acct()];
  for (const data of [[7], [9, 0]]) {
    const ix = { ...closeInstructions(plan, OWNER)[0], data: Buffer.from(data) };
    assert.throws(() => verifyCloseTx(txOf([ix]), OWNER, plan), /other than CloseAccount/);
  }
});
