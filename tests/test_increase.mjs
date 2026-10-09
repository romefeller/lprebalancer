// The Raydium signer's `increase`: arguments are checked before any RPC.
import test from 'node:test';
import assert from 'node:assert/strict';
import { increase } from '../venues/raydium_clmm/signer.mjs';

test('increase refuses bad amounts before it reads anything', async () => {
  for (const [a, b] of [['x', '1'], ['-1', '1'], ['0', '0'], ['1', 'NaN'], [undefined, '1'], ['Infinity', '1']]) {
    await assert.rejects(increase('M', a, b, false), /increase needs <positionMint> <maxA> <maxB>/, `${a} ${b}`);
  }
});

test('increase with good amounts goes on to need a pool', async () => {
  const saved = process.env.LPBOT_POOL;
  delete process.env.LPBOT_POOL;
  try {
    await assert.rejects(increase('M', '0', '1', false), /no pool/);
    await assert.rejects(increase('M', '0.5', '0', false), /no pool/);
  } finally { if (saved !== undefined) process.env.LPBOT_POOL = saved; }
});
