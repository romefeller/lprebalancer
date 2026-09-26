// payout.mjs refuses any destination that is not the pinned profit wallet,
// before it reads a key or touches the network.
import test from 'node:test';
import assert from 'node:assert';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
const dir = path.dirname(new URL(import.meta.url).pathname);
const script = path.join(dir, '..', 'payout.mjs');
const USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v';
const A = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h', B = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f';
const run = (env, dest) => spawnSync('node', [script, 'send', USDC, '0.01', dest],
  { env: { PATH: process.env.PATH, WALLET_SECRET_PATH: '/nonexistent', SOLANA_RPC_URL: 'http://127.0.0.1:9', ...env }, encoding: 'utf8' });
test('no pin: refused', () => { assert.match(run({ LPBOT_PROFIT_WALLET: A }, A).stderr, /PIN is not set/); });
test('database says B, pin says A: refused', () => { assert.match(run({ LPBOT_PROFIT_WALLET: B, LPBOT_PROFIT_WALLET_PIN: A }, B).stderr, /pinned profit wallet/); });
test('destination differs from both: refused', () => { assert.match(run({ LPBOT_PROFIT_WALLET: A, LPBOT_PROFIT_WALLET_PIN: A }, B).stderr, /pinned profit wallet/); });
