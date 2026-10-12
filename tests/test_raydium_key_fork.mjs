// The Raydium signer with a key wallet (no vault), on a local fork of mainnet,
// through a connection that serves no signature subscription (as Alchemy).
// 2026-10-12 20:09:12Z: a sol-swing open was refused on slippage, rebuilt, and
// the rebuilt send confirmed over a websocket the RPC does not serve. The
// position landed; the signer reported a failure and the breaker backed off.
// Here the first open is refused on slippage at preflight, the rebuild lands,
// and the increase and close land too, all polled.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, SendTransactionError } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const { Raydium } = require('@raydium-io/raydium-sdk-v2');

const POOL = '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv';      // SOL/USDC 0.04%
const USDC = new PublicKey(SOLANA.usdc_mint);
const RAYDIUM = SOLANA.programs.raydium_clmm;

async function said(fn) {
  const lines = [], log = console.log;
  console.log = (x) => lines.push(x);
  try { await fn(); } finally { console.log = log; }
  return JSON.parse(lines.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null');
}

test('key wallet: a slippage refusal is rebuilt, and every send lands without a subscription', { timeout: 600_000 }, async () => {
  const fork = new Fork();
  await Promise.all([fork.program(RAYDIUM), fork.program(SOLANA.programs.metaplex_metadata)]);
  await fork.syncClock();
  await fork.clone([USDC]);
  const payer = fork.wallet(5);
  fork.tokenAccount(spl.getAssociatedTokenAddressSync(USDC, payer.publicKey), USDC, payer.publicKey, 300_000_000n);
  const base = fork.connection();
  assert.throws(() => base.onSignature(), /not served/);
  await assert.rejects(base.confirmTransaction('x'), /not served/);
  // the first send is refused at preflight on slippage, as a node answers it
  let sends = 0;
  const connection = Object.create(base, { sendRawTransaction: { value: async (raw, opts) => {
    sends += 1;
    if (sends === 1) {
      throw new SendTransactionError({ action: 'simulate', signature: '',
        transactionMessage: 'Transaction simulation failed: Error processing Instruction 3: custom program error: 0x1781',
        logs: [`Program ${RAYDIUM} invoke [1]`, `Program ${RAYDIUM} failed: custom program error: 0x1781`] });
    }
    return base.sendRawTransaction(raw, opts);
  } } });
  const acting = await actingAccount({ connection, payer, venue: 'raydium-clmm', env: {} });
  assert.equal(acting.mode, 'key');
  const load = () => Raydium.load({ connection, owner: payer.publicKey, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
  const deps = { urls: ['http://fork.test'], connectFn: async () => ({ connection, payer, raydium: await load(), acting }), sleep: async () => {} };
  const signer = await import('../venues/raydium_clmm/signer.mjs');
  process.env.LPBOT_POOL = POOL;
  process.env.LPBOT_MAX_USD = '100000';

  const price = (await said(() => signer.balance(undefined, deps))).price;
  const errs = [], err = console.error;
  console.error = (...x) => errs.push(x.join(' '));
  let opened;
  try {
    opened = await said(() => signer.rebuildOnRefusal(
      () => signer.open(POOL, String(price * 0.99), String(price * 1.01), '0.5', '50', true, deps), true, { sleep: async () => {} }));
  } finally { console.error = err; }
  assert.equal(opened.sent, true, JSON.stringify(opened) + errs.join('\n'));
  assert.ok(errs.some(l => /slippage refusal; rebuilding on fresh pool state \(attempt 2\/3\)/.test(l)), errs.join('\n'));
  assert.equal(sends, 2, 'one refused send, one that landed');
  const st = await said(() => signer.status(opened.positionMint, deps));
  assert.equal(st.positionMint, opened.positionMint);

  const inc = await said(() => signer.increase(opened.positionMint, '0.05', '5', true, deps));
  assert.equal(inc.sent, true, JSON.stringify(inc));
  const c = await said(() => signer.close(opened.positionMint, true, deps));
  assert.equal(c.closed, opened.positionMint, JSON.stringify(c));
  assert.equal((await said(() => signer.status(undefined, deps))).positions, 0);
});
