// chains/evm/landing.mjs and the signers' runSteps: a transaction that landed is never
// reported failed (2026-10-13, poly-wpol-usdt: three landed swaps reported as failed, HALT).
//
// The incident, reproduced without a node: a viem client whose transport answers
// eth_getTransactionReceipt with JSON-RPC -32602 for a hash it does not have yet, as
// polygon-bor-rpc.publicnode.com does ("Archive requests require a personal token", HTTP 403).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createPublicClient, custom } from 'viem';
import { polygon } from 'viem/chains';
import { findReceipt, sendSteps, receiptsOf, SEND_ERROR_LOOKUP_MS } from '../chains/evm/landing.mjs';
import * as S from '../venues/uniswap_v3/signer.mjs';
import * as AERO from '../venues/aerodrome/signer.mjs';

S.useChain('polygon');

const HASH = '0x' + 'ab'.repeat(32);
const ARCHIVE = { code: -32602, message: 'Archive requests require a personal token. Get one at: https://www.allnodes.com/publicnode' };
const clock = (step = 1000) => { let t = 0; return () => (t += step); };
const fast = () => ({ sleep: async () => {}, now: clock() });
const rcpt = (hash, status = 'success', blockNumber = 100n) => ({ transactionHash: hash, status, blockNumber, gasUsed: 1n, effectiveGasPrice: 1n, logs: [] });
const notFound = (hash) => Object.assign(new Error(`Transaction receipt with hash "${hash}" could not be found.`), { name: 'TransactionReceiptNotFoundError' });

// A JSON-RPC node, as a viem transport: sendRawTransaction lands the tx (its "hash" is the
// serialized payload in these tests); the receipt is there after `lagPolls` asks, and before
// that the node answers like publicnode (-32602) or like a node that has nothing (null).
function node({ lagPolls = 3, refuse = true, status = '0x1', sendError = null, block = 500 } = {}) {
  const asks = new Map();
  const landed = new Set();
  const calls = [];
  const request = async ({ method, params }) => {
    calls.push(method);
    if (method === 'eth_chainId') return '0x89';
    if (method === 'eth_blockNumber') return `0x${block.toString(16)}`;
    if (method === 'eth_sendRawTransaction') {
      landed.add(params[0]);
      if (sendError) throw sendError;
      return params[0];
    }
    if (method === 'eth_getTransactionByHash') {
      // the node that took the send knows the pending transaction
      if (!landed.has(params[0])) { if (refuse) throw ARCHIVE; return null; }
      return { hash: params[0], nonce: '0x1', blockHash: null, blockNumber: null, transactionIndex: null, from: '0x' + '22'.repeat(20),
        to: '0x' + '33'.repeat(20), value: '0x0', gas: '0x5208', input: '0x', type: '0x2', maxFeePerGas: '0x1', maxPriorityFeePerGas: '0x1',
        chainId: '0x89', v: '0x0', r: '0x1', s: '0x1' };
    }
    if (method === 'eth_getTransactionReceipt') {
      const h = params[0];
      const n = (asks.get(h) ?? 0) + 1; asks.set(h, n);
      if (!landed.has(h) || n <= lagPolls) {
        if (refuse) throw ARCHIVE;
        return null;
      }
      return { transactionHash: h, status, blockNumber: `0x${block.toString(16)}`, blockHash: '0x' + '11'.repeat(32), transactionIndex: '0x0',
        from: '0x' + '22'.repeat(20), to: '0x' + '33'.repeat(20), cumulativeGasUsed: '0x1', gasUsed: '0x5208', effectiveGasPrice: '0x1',
        logs: [], logsBloom: '0x' + '00'.repeat(256), type: '0x2', contractAddress: null };
    }
    throw new Error(`unexpected ${method}`);
  };
  const pub = createPublicClient({ chain: polygon, transport: custom({ request }), pollingInterval: 5 });
  return { pub, calls, landed };
}
const signAs = (...hashes) => { let i = 0; return async () => { const h = hashes[i++]; return { serialized: h, hash: h }; }; };

// --- the root cause, pinned ----------------------------------------------------------------
test('root cause: viem waitForTransactionReceipt rejects on -32602 from a node that lacks the receipt yet', async () => {
  const { pub } = node({ lagPolls: 2 });
  await pub.sendRawTransaction({ serializedTransaction: HASH });
  await assert.rejects(pub.waitForTransactionReceipt({ hash: HASH, pollingInterval: 5, timeout: 5_000 }),
    e => e.name === 'InvalidParamsRpcError' && /Invalid parameters were provided to the RPC method/.test(e.shortMessage));
});

// --- findReceipt ----------------------------------------------------------------------------
test('findReceipt: -32602 and "not found" are "not yet"; the receipt is found when it comes', async () => {
  const { pub } = node({ lagPolls: 4 });
  await pub.sendRawTransaction({ serializedTransaction: HASH });
  const r = await findReceipt([pub], HASH, { timeoutMs: 60_000, ...fast() });
  assert.equal(r.receipt.status, 'success'); assert.equal(r.error, null);
});

test('findReceipt: a refusing first endpoint falls through to the next one in the same round', async () => {
  let a = 0, b = 0;
  const refusing = { async getTransactionReceipt() { a += 1; throw Object.assign(new Error('Invalid parameters were provided to the RPC method.'), { code: -32602 }); } };
  const good = { async getTransactionReceipt({ hash }) { b += 1; return rcpt(hash); } };
  let slept = 0;
  const r = await findReceipt([refusing, good], HASH, { timeoutMs: 60_000, sleep: async () => { slept += 1; }, now: clock() });
  assert.equal(r.receipt.transactionHash, HASH);
  assert.deepEqual([a, b, slept], [1, 1, 0], 'one round, no pause');
});

test('findReceipt: nothing in time gives null and the last error; the deadline is exact', async () => {
  const errs = [];
  const none = { async getTransactionReceipt({ hash }) { const e = notFound(hash); errs.push(e); throw e; } };
  let t = 0, slept = 0;
  const r = await findReceipt([none], HASH, { timeoutMs: 3000, pollMs: 7, sleep: async ms => { assert.equal(ms, 7); slept += 1; t += 1000; }, now: () => t });
  assert.equal(r.receipt, null); assert.equal(r.error, errs.at(-1));
  assert.equal(errs.length, 4, 'asked at t = 0, 1000, 2000 and 3000 (the deadline itself), then stops');
  assert.equal(slept, 3);
  const nulls = { async getTransactionReceipt() { return null; } };
  const r2 = await findReceipt([nulls], HASH, { timeoutMs: 0, ...fast() });
  assert.deepEqual(r2, { receipt: null, error: null }, 'a null answer is not found, and no error');
});

test('findReceipt: a receipt of another transaction is no answer; one without a hash field is', async () => {
  const other = { async getTransactionReceipt() { return rcpt('0x' + 'cd'.repeat(32)); } };
  assert.equal((await findReceipt([other], HASH, { timeoutMs: 2000, ...fast() })).receipt, null);
  const upper = { async getTransactionReceipt() { return rcpt(HASH.toUpperCase().replace('0X', '0x')); } };
  assert.ok((await findReceipt([upper], HASH, { timeoutMs: 0, ...fast() })).receipt, 'hash compared without case');
  const bare = { async getTransactionReceipt() { return { status: 'success' }; } };
  assert.ok((await findReceipt([bare], HASH, { timeoutMs: 0, ...fast() })).receipt);
});

test('findReceipt: the default pause is one second, and real time is the default clock', async () => {
  const seen = [];
  let n = 0;
  const later = { async getTransactionReceipt({ hash }) { n += 1; if (n < 2) return null; return rcpt(hash); } };
  const r = await findReceipt([later], HASH, { timeoutMs: 10_000, sleep: async ms => { seen.push(ms); } });
  assert.ok(r.receipt); assert.deepEqual(seen, [1000]);
  const t0 = Date.now();
  const r2 = await findReceipt([{ async getTransactionReceipt() { return null; } }], HASH, { timeoutMs: 30, pollMs: 5 });
  assert.equal(r2.receipt, null); assert.ok(Date.now() - t0 >= 30, 'the default clock and sleep are real');
});

// --- sendSteps / runSteps: the incident --------------------------------------------------
test('incident: send OK, then -32602 on the receipt: reported landed, with its receipt', async () => {
  const { pub } = node({ lagPolls: 5 });
  const ctx = { pub, nonce: 189 };
  const r = await S.runSteps(ctx, [{ label: 'swap' }], {}, { signStep: signAs(HASH), receipt: fast() });
  assert.equal(r.error, null);
  assert.deepEqual(receiptsOf(r.sent), [{ label: 'swap', hash: HASH, status: 'success' }]);
  assert.equal(r.sent[0].receipt.status, 'success');
  assert.equal(ctx.nonce, 190); assert.equal(ctx.minBlock, 500n);
  assert.deepEqual(r.recovered, []);
});

test('incident, two steps: the approve landed behind -32602, then the swap is sent and lands too', async () => {
  const H1 = '0x' + '01'.repeat(32), H2 = '0x' + '02'.repeat(32);
  const { pub, landed } = node({ lagPolls: 3 });
  const ctx = { pub, nonce: 189 };
  const at = [];
  const sign = async (c, step) => { at.push([step.label, c.minBlock]); const h = step.label === 'approve' ? H1 : H2; return { serialized: h, hash: h }; };
  const r = await S.runSteps(ctx, [{ label: 'approve' }, { label: 'swap' }], {}, { signStep: sign, receipt: fast() });
  assert.equal(r.error, null);
  assert.deepEqual(receiptsOf(r.sent).map(x => [x.label, x.status]), [['approve', 'success'], ['swap', 'success']]);
  assert.deepEqual([...landed], [H1, H2]);
  assert.deepEqual(at, [['approve', undefined], ['swap', 500n]], 'the swap is checked at the approve\'s block');
  assert.equal(ctx.nonce, 191);
});

test('the receipt is asked of the other endpoints when the connected one refuses it', async () => {
  const refusing = { async sendRawTransaction() {}, async getTransactionReceipt() { throw ARCHIVE; } };
  const other = { async getTransactionReceipt({ hash }) { return rcpt(hash, 'success', 77n); } };
  const ctx = { pub: refusing, nonce: 0, receiptClients: [refusing, other] };
  const r = await S.runSteps(ctx, [{ label: 'swap' }], {}, { signStep: signAs(HASH), receipt: fast() });
  assert.equal(r.error, null); assert.equal(ctx.minBlock, 77n);
  const r2 = await AERO.runSteps({ pub: refusing, nonce: 0, receiptClients: [refusing, other] }, [{ label: 'swap' }], {}, { signStep: signAs(HASH), receipt: fast() });
  assert.equal(r2.error, null, 'the Base signer takes the same path');
});

test('receipt clients: the connected one, then every other endpoint of the chain; no url, no others', async () => {
  const old = process.env.LPBOT_RPC;
  try {
    process.env.LPBOT_RPC = 'http://127.0.0.1:9';           // loopback: the only endpoint
    const pub = { async sendRawTransaction() {}, async getTransactionReceipt({ hash }) { return rcpt(hash); } };
    const same = { pub, nonce: 0, url: 'http://127.0.0.1:9' };
    await S.runSteps(same, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
    assert.equal(same.receiptClients.length, 1); assert.equal(same.receiptClients[0], pub);
    const elsewhere = { pub, nonce: 0, url: 'http://127.0.0.1:8' };
    await S.runSteps(elsewhere, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
    assert.equal(elsewhere.receiptClients.length, 2); assert.equal(elsewhere.receiptClients[0], pub);
    assert.equal(typeof elsewhere.receiptClients[1].getTransactionReceipt, 'function');
    const bare = { pub, nonce: 0 };
    await S.runSteps(bare, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
    assert.deepEqual(bare.receiptClients, [pub]);
    const kept = [pub];
    const given = { pub, nonce: 0, url: 'http://127.0.0.1:8', receiptClients: kept };
    await S.runSteps(given, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
    assert.equal(given.receiptClients, kept, 'built once per context');
    const base = { pub, nonce: 0, url: 'http://127.0.0.1:8' };
    await AERO.runSteps(base, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
    assert.equal(base.receiptClients.length, 2);
    const baseSame = { pub, nonce: 0, url: 'http://127.0.0.1:9' };
    await AERO.runSteps(baseSame, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
    assert.equal(baseSame.receiptClients.length, 1);
    const baseBare = { pub, nonce: 0 };
    await AERO.runSteps(baseBare, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
    assert.deepEqual(baseBare.receiptClients, [pub]);
  } finally {
    if (old == null) delete process.env.LPBOT_RPC; else process.env.LPBOT_RPC = old;
  }
});

test('a send that threw but landed is success, named in `recovered`; the nonce moves on', async () => {
  const { pub } = node({ lagPolls: 1, sendError: { code: -32602, message: 'Archive requests require a personal token.' } });
  const ctx = { pub, nonce: 4 };
  const r = await sendSteps(ctx, [{ label: 'swap' }], {}, { sign: signAs(HASH), clients: () => [pub], errorLine: S.errorLine, receipt: fast() });
  assert.equal(r.error, null); assert.equal(r.sent[0].status, 'success'); assert.equal(ctx.nonce, 5);
  assert.equal(r.recovered.length, 1); assert.match(r.recovered[0], /^swap landed although its send reported: Invalid parameters/);
});

test('a send that threw and never landed: partial, status unknown, the send error, the nonce kept, a short lookup', async () => {
  let asked = 0;
  const pub = { async sendRawTransaction() { throw new Error('nonce too low'); }, async getTransactionReceipt() { asked += 1; return null; } };
  const ctx = { pub, nonce: 4 };
  const ends = [];
  const now = clock();
  const r = await sendSteps(ctx, [{ label: 'swap' }, { label: 'never' }], {}, { sign: signAs(HASH, HASH), clients: () => [pub], errorLine: S.errorLine,
    timeoutMs: 10 * SEND_ERROR_LOOKUP_MS, receipt: { sleep: async () => {}, now: () => { const t = now(); ends.push(t); return t; } } });
  assert.deepEqual(receiptsOf(r.sent), [{ label: 'swap', hash: HASH, status: 'unknown' }]);
  assert.match(r.error, /nonce too low/); assert.equal(ctx.nonce, 4);
  assert.equal(asked, SEND_ERROR_LOOKUP_MS / 1000, 'one ask per fake second: looked for SEND_ERROR_LOOKUP_MS, not the full receipt wait');
  assert.equal(SEND_ERROR_LOOKUP_MS, 60_000);
});

test('a send that went through but no endpoint ever has the receipt: partial, unknown, the last error named', async () => {
  let asked = 0;
  const pub = { async sendRawTransaction() {}, async getTransactionReceipt() { asked += 1; throw ARCHIVE; } };
  const ctx = { pub, nonce: 1 };
  const r = await sendSteps(ctx, [{ label: 'swap' }], {}, { sign: signAs(HASH), clients: () => [pub], errorLine: S.errorLine, timeoutMs: 5000, receipt: fast() });
  assert.equal(r.sent[0].status, 'unknown'); assert.equal(r.sent[0].receipt, null); assert.equal(ctx.nonce, 1);
  assert.match(r.error, new RegExp(`^no endpoint has a receipt for ${HASH} \\(swap\\): Archive requests`));
  assert.equal(asked, 5, 'one ask per fake second: the full receipt wait (timeoutMs), not the short one');
  const quiet = { async sendRawTransaction() {}, async getTransactionReceipt() { return null; } };
  const r2 = await sendSteps({ pub: quiet, nonce: 1 }, [{ label: 'swap' }], {}, { sign: signAs(HASH), clients: () => [quiet], errorLine: S.errorLine, timeoutMs: 0, receipt: fast() });
  assert.match(r2.error, /\(swap\): not found$/);
});

test('the default receipt wait is 120 s', async () => {
  let asked = 0;
  const pub = { async sendRawTransaction() {}, async getTransactionReceipt() { asked += 1; return null; } };
  await sendSteps({ pub, nonce: 0 }, [{ label: 'a' }], {}, { sign: signAs(HASH), clients: () => [pub], errorLine: String, receipt: fast() });
  assert.equal(asked, 120, 'one ask per fake second');
  asked = 0;
  await S.runSteps({ pub, nonce: 0 }, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
  assert.equal(asked, 120, 'the Uniswap signer waits RECEIPT_TIMEOUT_MS = 120 s');
  asked = 0;
  await AERO.runSteps({ pub, nonce: 0 }, [{ label: 'a' }], {}, { signStep: signAs(HASH), receipt: fast() });
  assert.equal(asked, 120, 'the Base signer too');
});

test('a reverted receipt stops the sequence: status reverted, its receipt kept, the nonce consumed', async () => {
  const { pub } = node({ lagPolls: 0, status: '0x0' });
  const ctx = { pub, nonce: 9 };
  const H2 = '0x' + '02'.repeat(32);
  const r = await sendSteps(ctx, [{ label: 'approve' }, { label: 'swap' }], {}, { sign: signAs(HASH, H2), clients: () => [pub], errorLine: S.errorLine, receipt: fast() });
  assert.deepEqual(receiptsOf(r.sent), [{ label: 'approve', hash: HASH, status: 'reverted' }]);
  assert.equal(r.sent[0].receipt.status, 'reverted');
  assert.equal(r.error, `transaction ${HASH} (approve) reverted on chain`);
  assert.equal(ctx.nonce, 10); assert.equal(ctx.minBlock, undefined, 'a reverted step sets no block');
});

test('nothing sent: a signing failure throws; after a landed step it is partial with the landed step', async () => {
  const pub = { async sendRawTransaction() {}, async getTransactionReceipt({ hash }) { return rcpt(hash); } };
  const deps = { clients: () => [pub], errorLine: S.errorLine, receipt: fast() };
  await assert.rejects(sendSteps({ pub, nonce: 0 }, [{ label: 'a' }], {}, { ...deps, sign: async () => { throw new Error('refused: no gas'); } }), /no gas/);
  let n = 0;
  const sign = async () => { n += 1; if (n === 2) throw new Error('boom'); return { serialized: HASH, hash: HASH }; };
  const r = await sendSteps({ pub, nonce: 0 }, [{ label: 'approve' }, { label: 'swap' }, { label: 'c' }], {}, { ...deps, sign });
  assert.deepEqual(receiptsOf(r.sent), [{ label: 'approve', hash: HASH, status: 'success' }]);
  assert.equal(r.error, 'boom'); assert.equal(n, 2);
});

test('receiptsOf: label, hash and status of each sent transaction, nothing else', () => {
  assert.deepEqual(receiptsOf([{ label: 'a', hash: '0x1', receipt: { big: 1n }, status: 'success' }, { label: 'b', hash: '0x2', receipt: null, status: 'unknown' }]),
    [{ label: 'a', hash: '0x1', status: 'success' }, { label: 'b', hash: '0x2', status: 'unknown' }]);
  assert.deepEqual(receiptsOf([]), []);
});
