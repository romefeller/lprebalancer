// The vault's position count on the owner page (onboard/solana.mjs): a
// refused lookup is read once more from the public endpoint; anything else,
// or a second refusal, stays an error and is never counted as zero.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { positionsWithFallback } from '../onboard/solana.mjs';

const primary = { name: 'primary' }, publicEp = { name: 'public' };
const args = { venue: 'meteora-dlmm', pools: ['P'], vault: 'V' };

test('a rate-limited lookup is read again from the public endpoint', async () => {
  const seen = [];
  const positionsOf = async (conn) => {
    seen.push(conn.name);
    if (conn === primary) throw new Error('429 Too Many Requests: {"code":429,"message":"Your app has exceeded its compute units per second capacity"}');
    return [{ id: 1 }, { id: 2 }];
  };
  const got = await positionsWithFallback(primary, args, { positionsOf, fallback: () => publicEp });
  assert.equal(got.length, 2);
  assert.deepEqual(seen, ['primary', 'public']);
});

test('a refused indexed call rotates too; a second refusal is thrown', async () => {
  const positionsOf = async () => { throw new Error('403 Forbidden: Indexed requests require a personal token'); };
  await assert.rejects(positionsWithFallback(primary, args, { positionsOf, fallback: () => publicEp }), /403/);
});

test('any other error is thrown at once, without a second read', async () => {
  let n = 0;
  const positionsOf = async () => { n += 1; throw new Error('account data did not decode'); };
  await assert.rejects(positionsWithFallback(primary, args, { positionsOf, fallback: () => publicEp }), /decode/);
  assert.equal(n, 1);
});

test('an answer from the first endpoint is used as it is', async () => {
  let fell = false;
  const got = await positionsWithFallback(primary, args, { positionsOf: async () => [], fallback: () => { fell = true; return publicEp; } });
  assert.deepEqual(got, []);
  assert.equal(fell, false);
});
