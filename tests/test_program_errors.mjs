// The program's own words for a failed Solana transaction
// (shared/program_errors.mjs): decoding by the failing program, the bounded
// log tail, redaction, and the paths that carry them to the loop.
import test from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import {
  decodeCustom, collectLogs, failingProgram, describeFailure, enrichError, instructionPrograms,
  errorText, simulationFailure, baseMessage, LOG_LINES, LINE_CHARS, MAX_CHARS,
} from '../shared/program_errors.mjs';
import { signerError } from '../shared/signer_errors.mjs';
import { sendUntilLanded } from '../shared/tx_send.mjs';
import TABLE from '../shared/program_errors.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { SendTransactionError, Keypair, PublicKey, TransactionMessage, VersionedTransaction, TransactionInstruction } = require('@solana/web3.js');

const DLMM = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo';
const SQUADS = 'SMRTzfY6DfH5ik3TKiyLFfXexV8uSG3d2UksSCYdunG';
const RAYDIUM = 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK';
const ORCA = 'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc';
const JUPITER = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4';
const BUDGET = 'ComputeBudget111111111111111111111111111111';

// The logs of the 2026-10-12 17:00:31Z test0 open, in shape: the DLMM
// program fails inside the Squads policy's call.
const TEST0_LOGS = [
  `Program ${BUDGET} invoke [1]`, `Program ${BUDGET} success`,
  `Program ${SQUADS} invoke [1]`, 'Program log: Instruction: ExecuteTransactionSyncV2',
  `Program ${DLMM} invoke [2]`, 'Program log: Instruction: AddLiquidityByStrategy2',
  'Program log: AnchorError occurred. Error Code: InvalidBinArray. Error Number: 6027. Error Message: Invalid bin array.',
  `Program ${DLMM} consumed 41234 of 1350000 compute units`,
  `Program ${DLMM} failed: custom program error: 0x178b`,
  `Program ${SQUADS} consumed 60000 of 1400000 compute units`,
  `Program ${SQUADS} failed: custom program error: 0x178b`,
];
const preflight = (logs, msg = 'Transaction simulation failed: Error processing Instruction 7: custom program error: 0x178b') =>
  new SendTransactionError({ action: 'simulate', signature: '', transactionMessage: msg, logs });

test('every program in the table decodes its own codes; one code means different things', () => {
  assert.deepEqual(decodeCustom(DLMM, 6027), { program: 'Meteora DLMM', name: 'InvalidBinArray' });
  assert.deepEqual(decodeCustom(DLMM, 6028), { program: 'Meteora DLMM', name: 'NonContinuousBinArrays' });
  assert.deepEqual(decodeCustom(SQUADS, 6027), { program: 'Squads Smart Account', name: 'SpendingLimitExceeded' });
  assert.equal(decodeCustom(RAYDIUM, 6017).name, 'PriceSlippageCheck');
  assert.equal(decodeCustom(RAYDIUM, 6000).name, 'NotApproved');
  assert.equal(decodeCustom(ORCA, 6000).name, 'InvalidEnum');
  assert.equal(decodeCustom(JUPITER, 6001).name, 'SlippageToleranceExceeded');
  assert.equal(decodeCustom(DLMM, 3012).name, 'AccountNotInitialized');           // Anchor's own
  assert.equal(decodeCustom('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 1).name, 'InsufficientFunds');
  assert.equal(decodeCustom('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 3012).name, null);   // not Anchor
  assert.equal(decodeCustom(DLMM, 9999).name, null);
  assert.equal(decodeCustom(Keypair.generate().publicKey.toBase58(), 6000), null);
  assert.equal(decodeCustom(DLMM, -1), null);
  assert.equal(decodeCustom(DLMM, 'x'), null);
  for (const [id, p] of Object.entries(TABLE.programs)) {
    assert.doesNotThrow(() => new PublicKey(id));
    for (const [code, name] of Object.entries(p.errors)) assert.equal(decodeCustom(id, Number(code)).name, name);
  }
});

test('the failing program is the first that logged the failure (the innermost), not the outer caller', () => {
  const f = failingProgram('Error processing Instruction 7: custom program error: 0x178b', TEST0_LOGS, []);
  assert.deepEqual(f, { programId: DLMM, code: 6027, index: 7, source: 'logs' });
});

test('without logs the failing instruction names the program; a caller program is flagged', () => {
  const ids = [BUDGET, BUDGET, DLMM];
  assert.deepEqual(failingProgram('{"InstructionError":[2,{"Custom":6028}]}', [], ids),
    { programId: DLMM, code: 6028, index: 2, source: 'instruction' });
  assert.deepEqual(failingProgram('Error processing Instruction 1: custom program error: 0x1771', [], [BUDGET, RAYDIUM]),
    { programId: RAYDIUM, code: 6001, index: 1, source: 'instruction' });
  const vault = describeFailure(new Error('transaction failed on chain: {"InstructionError":[1,{"Custom":6027}]}'),
    { programIds: [BUDGET, SQUADS], logs: [] });
  assert.match(vault, /Squads Smart Account SpendingLimitExceeded \(6027 = 0x178b\) at instruction 1 \(by instruction index: may be an inner program's code\)/);
  assert.equal(failingProgram('a 429 and nothing else', [], []), null);
  assert.deepEqual(failingProgram('custom program error: 0x1', [], []), { programId: null, code: 1, index: null, source: null });
  assert.match(describeFailure(new Error('custom program error: 0x1')), /unknown program \(1 = 0x1\)/);
  assert.match(describeFailure(new Error('{"InstructionError":[0,{"Custom":5}]}'), { programIds: [BUDGET] }),
    /program ComputeBudget1+ \(not in the table\) \(5 = 0x5\) at instruction 0/);
});

test('the test0 failure reads as the program said it', () => {
  const line = describeFailure(preflight(TEST0_LOGS));
  assert.match(line, /^Simulation failed\. Message: Transaction simulation failed: Error processing Instruction 7: custom program error: 0x178b\./);
  assert.match(line, / \| program error: Meteora DLMM InvalidBinArray \(6027 = 0x178b\) at instruction 7 \| logs: /);
  assert.match(line, /Error Code: InvalidBinArray/);
  assert.doesNotMatch(line, /Catch the `SendTransactionError`/);
  assert.doesNotMatch(line, /\n/);
  assert.equal(line.split(' | logs: ')[1].split(' ; ').length, LOG_LINES);
  assert.equal(describeFailure({ message: line }), line, 'described again, the line stays the same');
});

test('signerError keeps the slippage head the loop matches and adds the decoded detail', () => {
  const logs = [`Program ${RAYDIUM} invoke [1]`, `Program ${RAYDIUM} failed: custom program error: 0x1781`];
  const s = signerError(preflight(logs, 'Transaction simulation failed: Error processing Instruction 2: custom program error: 0x1781'));
  assert.match(s, /^PriceSlippageCheck \(6017\): price moved beyond the slippage limit \| program error: Raydium CLMM PriceSlippageCheck/);
  assert.equal(signerError(new Error('PriceSlippageCheck (6017)')), 'PriceSlippageCheck (6017): price moved beyond the slippage limit');
  const plain = signerError(new Error('429 Too Many Requests'));
  assert.equal(plain, '429 Too Many Requests');
  const described = Object.assign(new Error('x | program error: Meteora DLMM InvalidBinArray (6027 = 0x178b)'), { described: true });
  assert.equal(signerError(described), described.message);
});

test('logs are found on the error or its causes; a getter that throws is no logs', () => {
  assert.deepEqual(collectLogs(preflight(['a', 'b'])), ['a', 'b']);
  assert.deepEqual(collectLogs({ cause: { cause: { simulationResponse: { logs: ['c'] } } } }), ['c']);
  assert.deepEqual(collectLogs({ value: { logs: ['d'] } }), ['d']);
  const bad = { get logs() { throw new Error('no'); } };
  assert.deepEqual(collectLogs(bad), []);
  assert.deepEqual(collectLogs(null), []);
  let deep = { logs: ['too deep'] };
  for (let i = 0; i < 6; i += 1) deep = { cause: deep };
  assert.deepEqual(collectLogs(deep), []);
});

test('secrets never pass: an api key in a URL and every *_KEY value of the environment', () => {
  const key = 'abcDEF1234567890secretvalue';
  const line = describeFailure(new Error(`fetch https://rpc.example/v2?api-key=${key} failed: custom program error: 0x1`),
    { logs: [`Program log: ${key}`, 'Program log: from https://x.io/?api_key=zzzzzzzzzzzzzzzzzz'], secrets: [key] });
  assert.doesNotMatch(line, new RegExp(key));
  assert.doesNotMatch(line, /zzzzzzzzzzzzzzzz/);
  process.env.TEST_PROGRAM_ERRORS_KEY = 'q9q9q9q9q9q9q9q9q9q9';
  try {
    assert.doesNotMatch(describeFailure(new Error('x'), { logs: ['Program log: q9q9q9q9q9q9q9q9q9q9'] }), /q9q9q9q9/);
  } finally { delete process.env.TEST_PROGRAM_ERRORS_KEY; }
});

test('property: any error and any logs give one bounded, idempotent, redacted line, never a throw', () => {
  const secret = 'S3cr3tS3cr3tS3cr3t';
  fc.assert(fc.property(
    fc.string({ maxLength: 3000 }), fc.array(fc.string({ maxLength: 600 }), { maxLength: 60 }),
    fc.option(fc.integer({ min: 0, max: 0xffffffff })), fc.constantFrom(DLMM, SQUADS, RAYDIUM, ORCA, JUPITER, BUDGET),
    fc.boolean(),
    (msg, logs, code, program, withSecret) => {
      const all = [...logs];
      if (code != null) all.push(`Program ${program} failed: custom program error: 0x${code.toString(16)}`);
      if (withSecret) all.splice(Math.floor(all.length / 2), 0, `Program log: ${secret}`);
      const line = describeFailure(new Error(msg), { logs: all, secrets: [secret] });
      assert.ok(line.length <= MAX_CHARS, `length ${line.length}`);
      assert.doesNotMatch(line, /[\n\r]/);
      assert.ok(!line.includes(secret));
      const tail = line.includes(' | logs: ') ? line.split(' | logs: ').at(-1).split(' ; ') : [];
      assert.ok(tail.length <= LOG_LINES);
      if (line.length < MAX_CHARS) for (const l of tail) assert.ok(l.length <= LINE_CHARS, l);
      if (code != null && line.length < MAX_CHARS) {
        const d = decodeCustom(program, code);
        if (d?.name) assert.ok(line.includes(`${d.program} ${d.name} (${code} = 0x${code.toString(16)})`), line);
      }
      if (line.length < MAX_CHARS) assert.equal(describeFailure({ message: line }, { secrets: [secret] }), line);
    }), { numRuns: 400 });
});

test('property: the decoded program is the first failing log line, whatever comes after', () => {
  fc.assert(fc.property(
    fc.array(fc.constantFrom(DLMM, SQUADS, RAYDIUM, ORCA, JUPITER), { minLength: 1, maxLength: 5 }),
    fc.integer({ min: 6000, max: 6100 }), fc.array(fc.string({ maxLength: 50 }), { maxLength: 5 }),
    (chainOf, code, noise) => {
      const logs = [...noise.map(n => `Program log: ${n}`), ...chainOf.map(p => `Program ${p} failed: custom program error: 0x${code.toString(16)}`)];
      const f = failingProgram('', logs, []);
      assert.equal(f.programId, chainOf[0]);
      assert.equal(f.code, code);
    }), { numRuns: 300 });
});

test('baseMessage drops web3.js log dumps and advice, and a described tail', () => {
  const e = preflight(['x]y', 'z']);
  assert.equal(baseMessage(e), 'Simulation failed. Message: Transaction simulation failed: Error processing Instruction 7: custom program error: 0x178b.');
  assert.equal(baseMessage({ message: 'a | program error: b | logs: c' }), 'a');
});

test('instructionPrograms reads v0 bytes, a legacy transaction, and nothing from garbage', () => {
  const payer = Keypair.generate();
  const ixs = [new TransactionInstruction({ programId: new PublicKey(BUDGET), keys: [], data: Buffer.alloc(1) }),
    new TransactionInstruction({ programId: new PublicKey(DLMM), keys: [], data: Buffer.alloc(1) })];
  const v0 = new VersionedTransaction(new TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: Keypair.generate().publicKey.toBase58(), instructions: ixs }).compileToV0Message());
  assert.deepEqual(instructionPrograms(v0.serialize()), [BUDGET, DLMM]);
  assert.deepEqual(instructionPrograms(v0), [BUDGET, DLMM]);
  assert.deepEqual(instructionPrograms({ instructions: ixs }), [BUDGET, DLMM]);
  assert.deepEqual(instructionPrograms(Buffer.from([1, 2, 3])), []);
  assert.deepEqual(simulationFailure({ InstructionError: [1, { Custom: 6027 }] }, [], v0).match(/Meteora DLMM InvalidBinArray/)?.length, 1);
  return v0;
});

test('errorText: a described error whole, any other cut to its limit', () => {
  assert.equal(errorText(Object.assign(new Error('y'.repeat(900)), { described: true })).length, 900);
  assert.equal(errorText(new Error('y'.repeat(900))).length, 300);
  assert.equal(errorText('plain', 3), 'pla');
});

// --- the send path ------------------------------------------------------------

function v0Bytes(programs) {
  const payer = Keypair.generate();
  const ixs = programs.map(p => new TransactionInstruction({ programId: new PublicKey(p), keys: [], data: Buffer.alloc(1) }));
  const tx = new VersionedTransaction(new TransactionMessage({ payerKey: payer.publicKey,
    recentBlockhash: Keypair.generate().publicKey.toBase58(), instructions: ixs }).compileToV0Message());
  tx.sign([payer]);
  return tx.serialize();
}

test('integration: a preflight refusal leaves sendUntilLanded with the decoded error and the log tail', async () => {
  const raw = v0Bytes([BUDGET, DLMM]);
  const conn = { sendRawTransaction: async () => { throw preflight([`Program ${DLMM} failed: custom program error: 0x178b`], 'Transaction simulation failed: Error processing Instruction 1: custom program error: 0x178b'); } };
  const e = await sendUntilLanded(conn, raw, 100).then(() => null, x => x);
  assert.ok(e instanceof SendTransactionError, 'the same error object, so every caller sees its kind');
  assert.ok(!e.afterSend);
  assert.equal(e.described, true);
  assert.deepEqual(e.programIds, [BUDGET, DLMM]);
  assert.match(e.message, /program error: Meteora DLMM InvalidBinArray \(6027 = 0x178b\) at instruction 1 \| logs: Program LBUZ/);
});

test('integration: a refusal without logs fetches them once (getLogs), bounded in time', async () => {
  const raw = v0Bytes([BUDGET, DLMM]);
  const err = new SendTransactionError({ action: 'send', signature: 'SIG', transactionMessage: 'Error processing Instruction 1: custom program error: 0x178c' });
  let reads = 0;
  const conn = {
    sendRawTransaction: async () => { throw err; },
    getTransaction: async (sig) => { reads += 1; assert.equal(sig, 'SIG'); return { meta: { logMessages: [`Program ${DLMM} failed: custom program error: 0x178c`] } }; },
  };
  const e = await sendUntilLanded(conn, raw, 100).then(() => null, x => x);
  assert.equal(reads, 1);
  assert.match(e.message, /Meteora DLMM NonContinuousBinArrays \(6028 = 0x178c\) at instruction 1 \| logs: Program LBUZ/);
  const slow = { getTransaction: () => new Promise(() => {}) };
  const t0 = Date.now();
  const e2 = await enrichError(new Error('transaction failed on chain: {"InstructionError":[1,{"Custom":6027}]}'),
    { connection: slow, raw, signature: 'S', timeoutMs: 50 });
  assert.ok(Date.now() - t0 < 2000);
  assert.match(e2.message, /Meteora DLMM InvalidBinArray/);
});

test('integration: a failure on chain reads the landed transaction\'s logs and stays afterSend', async () => {
  const raw = v0Bytes([BUDGET, SQUADS]);
  const conn = {
    sendRawTransaction: async () => 'SIG',
    getSignatureStatuses: async () => ({ value: [{ err: { InstructionError: [1, { Custom: 6027 }] }, confirmationStatus: 'confirmed' }] }),
    getTransaction: async () => ({ meta: { logMessages: TEST0_LOGS } }),
  };
  const e = await sendUntilLanded(conn, raw, 100, { sleep: async () => {} }).then(() => null, x => x);
  assert.equal(e.afterSend, true);
  assert.equal(e.signature, 'SIG');
  assert.match(e.message, /^transaction failed on chain: .* \| program error: Meteora DLMM InvalidBinArray \(6027 = 0x178b\) at instruction 1 \| logs: /);
});

test('integration: a transport error passes as it was, with no extra read', async () => {
  let reads = 0;
  const e = new Error('fetch failed: 429 Too Many Requests');
  const conn = { sendRawTransaction: async () => { throw e; }, getTransaction: async () => { reads += 1; } };
  const got = await sendUntilLanded(conn, v0Bytes([DLMM]), 100).then(() => null, x => x);
  assert.equal(got, e);
  assert.equal(got.message, 'fetch failed: 429 Too Many Requests');
  assert.equal(got.described, undefined);
  assert.equal(reads, 0);
  assert.equal(await enrichError(null), null);
  assert.equal(await enrichError('text'), 'text');
});

// --- the EVM signers: the revert reason in the one error line -------------------

test('EVM signers: the first line of a viem error and its revert reason, bounded', async () => {
  for (const file of ['../venues/uniswap_v3/signer.mjs', '../venues/aerodrome/signer.mjs']) {
    const { errorLine } = await import(file);
    const viem = Object.assign(new Error('long'), { shortMessage: 'The contract function "mint" reverted.\nDocs: x',
      cause: { cause: { reason: 'Price slippage check' } } });
    assert.equal(errorLine(viem), 'The contract function "mint" reverted. | revert: Price slippage check', file);
    assert.equal(errorLine({ shortMessage: 'reverted', cause: { data: { errorName: 'Panic', args: [17n] } } }), 'reverted | revert: Panic(17)');
    assert.equal(errorLine(new Error('fetch failed\nmore')), 'fetch failed', 'no reason: the first line once');
    assert.equal(errorLine({ shortMessage: 'execution reverted: STF', cause: { reason: 'STF' } }), 'execution reverted: STF', 'never twice');
    assert.equal(errorLine({ shortMessage: 'x'.repeat(500) }).length, 300);
    assert.equal(errorLine({ shortMessage: 'x'.repeat(500) }, 1000).length, 500);
  }
});

// --- every Solana signer's send path carries the program's words -------------------

const LONG_LOGS = [...Array.from({ length: 12 }, (_, i) => `Program log: step ${i} ${'y'.repeat(60)}`),
  `Program ${DLMM} failed: custom program error: 0x178b`];
const refusal = () => preflight(LONG_LOGS, 'Transaction simulation failed: Error processing Instruction 1: custom program error: 0x178b');

test('DLMM sendAll: a first refusal says it in full, a later one comes back whole in the report', async () => {
  const dlmm = await import('../venues/meteora_dlmm/signer.mjs');
  const raw = v0Bytes([BUDGET, DLMM]);
  const acting = { transaction: () => ({ serialize: () => raw }) };
  const bh = async () => ({ blockhash: 'x', lastValidBlockHeight: 1 });
  await assert.rejects(dlmm.sendAll({ getLatestBlockhash: bh, sendRawTransaction: async () => { throw refusal(); } }, [{ instructions: [] }], acting),
    e => /\(not retried\): .* \| program error: Meteora DLMM InvalidBinArray \(6027 = 0x178b\) at instruction 1 \| logs: /.test(e.message));
  let k = 0;
  const conn = { getLatestBlockhash: bh, getBlockHeight: async () => 0,
    sendRawTransaction: async () => { if (k++) throw refusal(); return 'SIG1'; },
    getSignatureStatuses: async () => ({ value: [{ err: null, confirmationStatus: 'confirmed' }] }) };
  const out = await dlmm.sendAll(conn, [{ instructions: [] }, { instructions: [] }], acting);
  assert.deepEqual(out.sigs, ['SIG1']);
  assert.ok(out.error.length > 300, 'the described line, not cut to 300');
  assert.match(out.error, /\| logs: .*failed: custom program error: 0x178b$/);
});

test('Byreal sendAll: the refusal comes back described and whole; a transport error as before', async () => {
  const byreal = await import('../venues/byreal/signer.mjs');
  const raw = v0Bytes([BUDGET, DLMM]);
  const tx = { message: {}, sign() {}, serialize: () => raw };
  const out = await byreal.sendAll({ sendRawTransaction: async () => { throw refusal(); } }, [tx], {});
  assert.deepEqual(out.sigs, []);
  assert.ok(out.error.length > 300);
  assert.match(out.error, /Meteora DLMM InvalidBinArray \(6027 = 0x178b\) at instruction 1 \| logs: /);
  const plain = await byreal.sendAll({ sendRawTransaction: async () => { throw new Error('x'.repeat(400)); } }, [tx], {});
  assert.equal(plain.error.length, 300);
});

test('Raydium sendAll: a later refusal keeps its described line in the partial report', async () => {
  const raydium = await import('../venues/raydium_clmm/signer.mjs');
  const said = [], log = console.log;
  console.log = (x) => said.push(x);
  let k = 0;
  try {
    await assert.rejects(raydium.sendAll([{}, {}], { pool: 'P' }, async () => {
      if (k++) throw new Error(`refused | program error: Raydium CLMM NotApproved (6000 = 0x1770) | logs: ${'z'.repeat(600)}`);
      return 'SIG1';
    }), /partial send: 1\/2 sent/);
  } finally { console.log = log; }
  const report = JSON.parse(said.find(x => typeof x === 'string' && x.startsWith('{')));
  assert.ok(report.error.length > 600, 'not cut to 300');
  assert.match(report.error, /Raydium CLMM NotApproved/);
});

test('every Solana signer reports its fatal error through signerError', () => {
  for (const f of ['venues/meteora_dlmm/signer.mjs', 'venues/orca/signer.mjs', 'venues/byreal/signer.mjs', 'venues/pancakeswap_v3/signer.mjs',
    'venues/raydium_clmm/signer.mjs', 'venues/jupiter/swap.mjs', 'venues/orca/swap.mjs', 'chains/solana/vault/cli.mjs',
    'chains/solana/janitor.mjs', 'chains/solana/payout.mjs']) {
    const src = fs.readFileSync(new URL(`../${f}`, import.meta.url), 'utf8');
    const tail = src.slice(src.lastIndexOf('main().catch'));
    assert.match(tail, /signerError\(e\)/, f);
    assert.doesNotMatch(tail, /ERROR:'?,? ?\$?\{?e\.message/, f);
  }
});

test('Orca sendOnce: a @solana/kit preflight refusal (logs in context) reads as the program said it', async () => {
  const orca = await import('../venues/orca/signer.mjs');
  const kit = Object.assign(new Error('Transaction simulation failed'), {
    context: { logs: [`Program ${ORCA} invoke [1]`, `Program ${ORCA} failed: custom program error: 0x1787`] } });
  await assert.rejects(orca.sendOnce({ callback: async () => { throw kit; } }),
    e => /\(not retried\): Transaction simulation failed \| program error: Orca Whirlpool \w+ \(6023 = 0x1787\) \| logs: /.test(e.message));
  await assert.rejects(orca.sendOnce({ callback: async () => { throw new Error('fetch failed'); } }),
    e => e.message === 'send failed after signing (not retried): fetch failed');
});

// --- the bounds, exactly (mutation testing found each one unchecked) ----------------

test('bounds: a log line of exactly 160 characters is kept whole, a longer one cut to 160 with an ellipsis', () => {
  const exact = 'L'.repeat(LINE_CHARS), long = 'M'.repeat(300);
  const line = describeFailure(new Error('x'), { logs: [exact, long], secrets: [] });
  const tail = line.split(' | logs: ')[1].split(' ; ');
  assert.equal(tail[0], exact);
  assert.equal(tail[1], `${'M'.repeat(LINE_CHARS - 1)}…`);
  assert.equal(tail[1].length, LINE_CHARS);
});

test('bounds: a line of exactly 1,800 characters is kept whole, a longer one cut to 1,800', () => {
  const exact = 'E'.repeat(MAX_CHARS);
  assert.equal(describeFailure(new Error(exact), { logs: [], secrets: [] }), exact);
  const long = describeFailure(new Error('F'.repeat(5000)), { logs: [], secrets: [] });
  assert.equal(long.length, MAX_CHARS);
  assert.ok(long.endsWith('…'));
});

test('the caller note is said only for a code found by instruction index in a caller program', () => {
  // Squads itself failed, and its logs say so: no note
  const own = describeFailure(new Error('x'), { logs: [`Program ${SQUADS} failed: custom program error: 0x1770`], secrets: [] });
  assert.match(own, /Squads Smart Account \w+ \(6000 = 0x1770\)$|Squads Smart Account \w+ \(6000 = 0x1770\) \| logs/);
  assert.doesNotMatch(own, /inner program/);
  // DLMM by instruction index: not a caller, no note
  const direct = describeFailure(new Error('{"InstructionError":[1,{"Custom":6027}]}'), { programIds: [BUDGET, DLMM], logs: [], secrets: [] });
  assert.match(direct, /Meteora DLMM InvalidBinArray \(6027 = 0x178b\) at instruction 1$/);
});

test('errorText keeps an empty message empty', () => {
  assert.equal(errorText(new Error('')), '');
});

test('enrichError: logs it has are never replaced by a read; a described error is left alone', async () => {
  let getLogs = 0, reads = 0;
  const conn = { getTransaction: async () => { reads += 1; return { meta: { logMessages: ['Program log: other'] } }; } };
  const e = Object.assign(new Error('Error processing Instruction 1: custom program error: 0x178b'), {
    signature: 'SIG', transactionLogs: [`Program ${DLMM} failed: custom program error: 0x178b`],
    getLogs: async () => { getLogs += 1; return ['Program log: other']; } });
  await enrichError(e, { connection: conn, signature: 'SIG', programIds: [] });
  assert.equal(getLogs, 0);
  assert.equal(reads, 0);
  assert.match(e.message, /\| logs: Program LBUZ/);
  assert.doesNotMatch(e.message, /other/);
  // no connection: no read at all, though the error offers one
  const e2 = Object.assign(new Error('custom program error: 0x1'), { signature: 'S', getLogs: async () => { getLogs += 1; return []; } });
  await enrichError(e2, { signature: 'S' });
  assert.equal(getLogs, 0);
  const done = Object.assign(new Error('already said'), { described: true, transactionLogs: ['Program log: x'] });
  assert.equal(await enrichError(done, { connection: conn }), done);
  assert.equal(done.message, 'already said');
});

test('signerError: a slippage refusal with no message of its own still carries the decoded detail', () => {
  const s = signerError({ message: '', programLogs: [`Program ${RAYDIUM} failed: custom program error: 0x1781`] });
  assert.equal(s, 'PriceSlippageCheck (6017): price moved beyond the slippage limit | program error: Raydium CLMM PriceSlippageCheck (6017 = 0x1781)'
    + ` | logs: Program ${RAYDIUM} failed: custom program error: 0x1781`);
});
