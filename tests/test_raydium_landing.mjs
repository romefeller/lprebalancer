// signer_raydium.mjs: a harvest or close that lands in a fast market.
//
// 2026-10-07: a Raydium close, sent once with the base fee only, expired
// unconfirmed ("block height exceeded") in a 2% drop, and the band sat out of
// range six more minutes. Now: a capped priority fee, the same transaction
// re-sent until it confirms or expires (tx_send.sendUntilLanded), and a close
// that provably never landed (NeverLanded) is rebuilt at once.
import test from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';
import { createRequire } from 'node:module';
import { NeverLanded, priorityCuPrice } from '../tx_send.mjs';
import { errorKind } from '../rpc_policy.mjs';
import * as raydium from '../signer_raydium.mjs';
const require = createRequire(import.meta.url);
const { Keypair, SystemProgram, Transaction } = require('@solana/web3.js');

const payer = Keypair.generate(), extra = Keypair.generate();
const BLOCKHASH = Keypair.generate().publicKey.toBase58();       // any 32 bytes in base58
const noSleep = { sleep: async () => {} };

function built(signers = []) {
  const tx = new Transaction().add(SystemProgram.transfer({ fromPubkey: payer.publicKey, toPubkey: payer.publicKey, lamports: 1 }));
  return { transaction: tx, signers };
}

// A connection whose signature confirms after `landsAfter` status reads
// (never when null); block heights rise by `step` per read past `valid`.
function conn({ landsAfter = null, err = null, valid = 1_000, step = 10, firstSendThrows = null } = {}) {
  const c = { sends: [], raws: [], reads: 0, height: valid - 30 };
  c.getLatestBlockhash = async () => ({ blockhash: BLOCKHASH, lastValidBlockHeight: valid });
  c.sendRawTransaction = async (raw, opts) => {
    c.sends.push(opts); c.raws.push(Buffer.from(raw).toString('base64'));
    if (c.sends.length === 1 && firstSendThrows) throw firstSendThrows;
    return 'SIG';
  };
  c.getSignatureStatuses = async (sigs, opts) => {
    if (opts?.searchTransactionHistory) return { value: [null] };
    c.reads += 1;
    return { value: [landsAfter !== null && c.reads > landsAfter ? { confirmationStatus: 'confirmed', err } : null] };
  };
  c.getBlockHeight = async () => (c.height += step);
  return c;
}

test('the exit fee: never more than the cap, and the floor when the history is empty', () => {
  const ceiling = Math.floor(raydium.EXIT_PRIORITY_MAX_LAMPORTS * 1e6 / raydium.EXIT_CU_LIMIT);
  const price = r => priorityCuPrice(r, raydium.EXIT_CU_LIMIT, raydium.EXIT_PRIORITY_MAX_LAMPORTS, raydium.EXIT_CU_PRICE_FLOOR);
  assert.equal(price([]), raydium.EXIT_CU_PRICE_FLOOR);
  assert.equal(price([1e12]), ceiling);
  assert.ok(raydium.EXIT_PRIORITY_MAX_LAMPORTS <= 100_000);                 // about $0.012 at most
  assert.ok(raydium.EXIT_CU_LIMIT >= 2 * 79_000);                           // twice the largest close measured
  fc.assert(fc.property(fc.array(fc.integer({ min: -10, max: 1e10 }), { maxLength: 40 }), fees => {
    const p = price(fees);
    assert.ok(p >= raydium.EXIT_CU_PRICE_FLOOR && p <= ceiling, `${p}`);
    assert.ok(p * raydium.EXIT_CU_LIMIT / 1e6 <= raydium.EXIT_PRIORITY_MAX_LAMPORTS);
  }));
});

test('sendLanded: signed by the payer and the builder\'s signers, on a fresh blockhash', async () => {
  const c = conn({ landsAfter: 0 });
  const b = built([extra]);
  b.transaction.add(SystemProgram.transfer({ fromPubkey: extra.publicKey, toPubkey: payer.publicKey, lamports: 1 }));
  assert.equal(await raydium.sendLanded(c, payer, b, noSleep), 'SIG');
  assert.equal(b.transaction.recentBlockhash, BLOCKHASH);
  assert.ok(b.transaction.feePayer.equals(payer.publicKey));
  assert.ok(b.transaction.verifySignatures());
  assert.deepEqual(c.sends, [{ skipPreflight: false, maxRetries: 0 }]);   // the first send runs the preflight
});

test('sendLanded: the same bytes again until they confirm', async () => {
  const c = conn({ landsAfter: 3 });
  assert.equal(await raydium.sendLanded(c, payer, built(), noSleep), 'SIG');
  assert.equal(c.sends.length, 4);
  assert.equal(new Set(c.raws).size, 1);
  assert.ok(c.sends.slice(1).every(o => o.skipPreflight === true));
});

test('sendLanded: an expiry the chain has no record of is NeverLanded, not sent', async () => {
  const c = conn({ landsAfter: null });
  await assert.rejects(raydium.sendLanded(c, payer, built(), noSleep), e => e instanceof NeverLanded && e.sent === undefined);
  assert.equal(errorKind(new NeverLanded('expired')), 'fatal');            // never rotated to another endpoint
});

test('sendLanded: every other failure is marked sent', async () => {
  const onChain = conn({ landsAfter: 0, err: { InstructionError: [2, { Custom: 6017 }] } });
  await assert.rejects(raydium.sendLanded(onChain, payer, built(), noSleep),
    e => e.sent === true && /PriceSlippageCheck/.test(e.message));
  const refused = conn({ firstSendThrows: new Error('Simulation failed: custom program error: 0x1781') });
  await assert.rejects(raydium.sendLanded(refused, payer, built(), noSleep),
    e => e.sent === true && /PriceSlippageCheck/.test(e.message));
  const down = conn({ firstSendThrows: new Error('fetch failed') });
  await assert.rejects(raydium.sendLanded(down, payer, built(), noSleep), e => e.sent === true);
});

test('sendAll: NeverLanded of the first transaction is thrown as itself; of a later one, a partial send', async () => {
  const log = console.log; console.log = () => {};
  try {
    const never = new NeverLanded('expired');
    await assert.rejects(raydium.sendAll([{}], {}, async () => { throw never; }), e => e === never);
    let k = 0;
    const send = async () => { if (k++) throw new NeverLanded('expired'); return 'SIG1'; };
    await assert.rejects(raydium.sendAll([{}, {}], {}, send), e => e.sent === true && /partial send: 1\/2/.test(e.message));
  } finally { console.log = log; }
});

test('rebuildOnRefusal: an expired or refused first send is rebuilt, up to three builds', async () => {
  const quiet = console.error, said = [];
  console.error = m => said.push(m);
  try {
    for (const [make, why] of [[() => new NeverLanded('expired'), 'expired unsent'],
                               [() => new Error('PriceSlippageCheck (6017)'), 'slippage refusal']]) {
      let n = 0;
      said.length = 0;
      assert.equal(await raydium.rebuildOnRefusal(async () => { if (n++ < 2) throw make(); return 'ok'; }, true, noSleep), 'ok');
      assert.equal(n, 3);
      assert.deepEqual(said, [`${why}; rebuilding on fresh pool state (attempt 2/3)`,
                              `${why}; rebuilding on fresh pool state (attempt 3/3)`]);
      n = 0;
      await assert.rejects(raydium.rebuildOnRefusal(async () => { n++; throw make(); }, true, noSleep));
      assert.equal(n, 3);
    }
  } finally { console.error = quiet; }
});

test('rebuildOnRefusal: a partial send, a dry run or any other error is never rebuilt', async () => {
  const cases = [
    [Object.assign(new Error('partial send: 1/2 sent; expired'), { sent: true }), true],
    [new NeverLanded('expired'), false],                                    // a dry run sends nothing to rebuild
    [Object.assign(new Error('fetch failed'), { sent: true }), true],
    [new Error('position M not found for this wallet on this pool'), true],
  ];
  for (const [err, execute] of cases) {
    let n = 0;
    await assert.rejects(raydium.rebuildOnRefusal(async () => { n++; throw err; }, execute, noSleep), e => e === err);
    assert.equal(n, 1, err.message);
  }
});
