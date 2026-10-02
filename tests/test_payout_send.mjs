// payout.mjs: the priority fee and the send loop.
//
// 2026-10-02: two mu-usdc payouts, sent with no priority fee and sent once,
// expired unconfirmed ("block height exceeded"); the loop booked them as
// 'uncertain' (counted paid, never re-sent) and the wallet's writes waited
// five minutes on each. Now: a capped priority fee, the same transaction
// re-sent until it confirms or expires, and an expiry the chain has no record
// of is NeverLanded, which the loop owes again.
import test from 'node:test';
import assert from 'node:assert';
import fc from 'fast-check';
import { payoutCuPrice, sendUntilLanded, NeverLanded, CU_LIMIT, CU_PRICE_FLOOR, PRIORITY_MAX_LAMPORTS,
         REBROADCAST_MS } from '../payout.mjs';

const ceiling = Math.floor(PRIORITY_MAX_LAMPORTS * 1e6 / CU_LIMIT);

test('the price is the 75th percentile of the recent non-zero fees', () => {
  const recent = [0, 0, 20_000, 30_000, 40_000, 50_000].map(f => ({ slot: 1, prioritizationFee: f }));
  assert.strictEqual(payoutCuPrice(recent), 50_000);           // 4 non-zero: index floor(4 * 0.75) = 3
  assert.strictEqual(payoutCuPrice([12_000, 15_000, 18_000, 21_000]), 21_000);
  assert.strictEqual(payoutCuPrice([12_000, 15_000, 18_000, 21_000, 24_000]), 21_000);   // index 3 of 5
});

test('no fees or only zeros: the floor', () => {
  for (const r of [[], null, undefined, [0, 0], [{ prioritizationFee: 0 }], [-5]]) {
    assert.strictEqual(payoutCuPrice(r), CU_PRICE_FLOOR);
  }
  assert.strictEqual(payoutCuPrice([CU_PRICE_FLOOR - 1]), CU_PRICE_FLOOR);
});

test('the total never passes the cap', () => {
  assert.strictEqual(payoutCuPrice([1e12]), ceiling);
  assert.strictEqual(payoutCuPrice([1e12], 100_000, 5_000), 50_000);
  assert.strictEqual(payoutCuPrice([1e12], 0, 5_000), 5_000 * 1e6);    // units 0 reads as 1
  assert.ok(PRIORITY_MAX_LAMPORTS <= 50_000);                            // about $0.006 at most
});

test('property: floor <= price <= ceiling, and price * units within the cap', () => {
  fc.assert(fc.property(fc.array(fc.integer({ min: -10, max: 1e9 }), { maxLength: 40 }), fees => {
    const p = payoutCuPrice(fees);
    assert.ok(p >= Math.min(CU_PRICE_FLOOR, ceiling) && p <= ceiling, `${p}`);
    assert.ok(p * CU_LIMIT / 1e6 <= PRIORITY_MAX_LAMPORTS + 1e-9);
    const pos = fees.filter(f => f > 0).sort((a, b) => a - b);
    if (pos.length) {
      const p75 = pos[Math.min(pos.length - 1, Math.floor(pos.length * 0.75))];
      assert.strictEqual(p, Math.min(Math.max(p75, CU_PRICE_FLOOR), ceiling));
    }
  }));
});

// A connection whose signature becomes known after `landsAfter` status reads
// (never when null), at block heights that rise by `step` per read.
function chain({ landsAfter = null, err = null, start = 100, step = 10, firstSendThrows = false,
                 resendThrows = false, knownLate = false } = {}) {
  const c = { sends: [], reads: 0, history: 0, height: start };
  c.sendRawTransaction = async (raw, opts) => {
    c.sends.push(opts);
    if (c.sends.length === 1 && firstSendThrows) throw new Error('rpc down');
    if (c.sends.length > 1 && resendThrows) throw new Error('429');
    return 'SIG';
  };
  c.getSignatureStatuses = async (sigs, opts) => {
    assert.deepStrictEqual(sigs, ['SIG']);
    if (opts?.searchTransactionHistory) {
      c.history += 1;
      return { value: [knownLate ? { confirmationStatus: 'finalized', err } : null] };
    }
    c.reads += 1;
    const known = landsAfter !== null && c.reads > landsAfter;
    return { value: [known ? { confirmationStatus: 'confirmed', err } : null] };
  };
  c.getBlockHeight = async () => (c.height += step);
  return c;
}
const noSleep = { sleep: async () => {} };

test('confirmed on the first read: one send, no re-send', async () => {
  const c = chain({ landsAfter: 0 });
  assert.strictEqual(await sendUntilLanded(c, 'RAW', 1_000, noSleep), 'SIG');
  assert.deepStrictEqual(c.sends, [{ skipPreflight: false, maxRetries: 0 }]);
});

test('re-sent every REBROADCAST_MS until it lands', async () => {
  const waits = [];
  const c = chain({ landsAfter: 3 });
  assert.strictEqual(await sendUntilLanded(c, 'RAW', 1_000, { sleep: async ms => waits.push(ms) }), 'SIG');
  assert.strictEqual(c.sends.length, 4);
  assert.deepStrictEqual(c.sends.slice(1), Array(3).fill({ skipPreflight: true, maxRetries: 0 }));
  assert.deepStrictEqual(waits, [REBROADCAST_MS, REBROADCAST_MS, REBROADCAST_MS]);
});

test('a failed re-send is not an error: the next status read decides', async () => {
  const c = chain({ landsAfter: 2, resendThrows: true });
  assert.strictEqual(await sendUntilLanded(c, 'RAW', 1_000, noSleep), 'SIG');
});

test('expired and unknown to the chain: NeverLanded, after the send, with the signature', async () => {
  const c = chain({ landsAfter: null, start: 100, step: 10 });
  await assert.rejects(sendUntilLanded(c, 'RAW', 130, noSleep), e => {
    assert.ok(e instanceof NeverLanded);
    assert.strictEqual(e.afterSend, true);
    assert.strictEqual(e.signature, 'SIG');
    assert.match(e.message, /nothing was sent, it is owed again/);
    assert.doesNotMatch(e.message, /timed out|timeout|confirm/i);    // never 'uncertain' in rebalancer.distribute
    return true;
  });
  assert.strictEqual(c.history, 1);
  assert.strictEqual(c.reads, 4);                                    // heights 110, 120, 130 are still valid
});

test('the block height at lastValidBlockHeight is still valid', async () => {
  const c = chain({ landsAfter: 1, start: 120, step: 10 });            // read 1 at height 130: wait, read 2 lands
  assert.strictEqual(await sendUntilLanded(c, 'RAW', 130, noSleep), 'SIG');
  assert.strictEqual(c.history, 0);
});

test('expired but found in history: it landed, the signature', async () => {
  const c = chain({ landsAfter: null, knownLate: true, start: 200 });
  assert.strictEqual(await sendUntilLanded(c, 'RAW', 100, noSleep), 'SIG');
});

test('failed on chain: an error after the send, never NeverLanded', async () => {
  for (const opts of [{ landsAfter: 0, err: { InstructionError: [1, 'x'] } },
                      { landsAfter: null, knownLate: true, err: { InstructionError: [1, 'x'] }, start: 200 }]) {
    await assert.rejects(sendUntilLanded(chain(opts), 'RAW', 100, noSleep), e => {
      assert.ok(!(e instanceof NeverLanded));
      assert.strictEqual(e.afterSend, true);
      assert.strictEqual(e.signature, 'SIG');
      assert.match(e.message, /failed on chain/);
      return true;
    });
  }
});

test('the first send failing: thrown as it is, not after the send', async () => {
  await assert.rejects(sendUntilLanded(chain({ firstSendThrows: true }), 'RAW', 100, noSleep), e => {
    assert.ok(!e.afterSend);
    assert.strictEqual(e.signature, undefined);
    return true;
  });
});

test('an RPC fault after the send is partial: afterSend with the signature', async () => {
  const c = chain({ landsAfter: null });
  c.getBlockHeight = async () => { throw new Error('rpc reset'); };
  await assert.rejects(sendUntilLanded(c, 'RAW', 1_000, noSleep), e => {
    assert.ok(!(e instanceof NeverLanded));
    assert.deepStrictEqual([e.afterSend, e.signature], [true, 'SIG']);
    return true;
  });
});

test('importing payout.mjs runs nothing', async () => {
  const before = process.exitCode;
  await import('../payout.mjs');
  assert.strictEqual(process.exitCode, before);
});
