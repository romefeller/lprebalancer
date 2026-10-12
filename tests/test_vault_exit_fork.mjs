// The owners' way out (chains/solana/vault/exit.mjs), on a fork of mainnet,
// on every venue a vault approves: the bot opens a position through its
// signer, the owner revokes the bot, closes the position into the vault with
// their own signature only, then takes everything out of the vault.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import { connectedVault } from './_vault.mjs';
import { request } from '../chains/solana/vault/cli.mjs';
import { actingAccount } from '../chains/solana/vault/account.mjs';
import { vaultPositions, EXIT_INSTRUCTIONS, TX_BYTES } from '../chains/solana/vault/exit.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Message, Transaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const USDC = new PublicKey(SOLANA.usdc_mint);
const SOL = spl.NATIVE_MINT;

// Each venue: its approved SOL/USDC pool, another it did not approve, and the signer's deps as its own
// fork test builds them.
const VENUES = {
  'meteora-dlmm': {
    pool: 'BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y',
    other: '5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6',
    signer: '../venues/meteora_dlmm/signer.mjs',
    deps: async ({ connection, payer, acting }) => ({ connectFn: async () => ({ connection, payer, acting }) }),
  },
  'raydium-clmm': {
    pool: '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv',
    other: '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj',
    signer: '../venues/raydium_clmm/signer.mjs',
    deps: async ({ connection, payer, acting, vault }) => {
      const { Raydium } = require('@raydium-io/raydium-sdk-v2');
      const load = () => Raydium.load({ connection, owner: vault, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
      return { connectFn: async () => ({ connection, payer, raydium: await load(), acting }) };
    },
  },
  byreal: {
    pool: '9GTj99g9tbz9U6UYDsX6YeRTgUnkYG6GTnHv3qLa5aXq',
    other: '2kdeJHNGAChkUGPTWe2FR2ah5bcJrhoynYNccVxV1GCN',
    signer: '../venues/byreal/signer.mjs',
    deps: async ({ connection, payer, acting }) => {
      const { Chain } = await import('@byreal-io/byreal-clmm-sdk');
      const chain = new Chain({ connection, programId: new PublicKey(SOLANA.programs.byreal_clmm) });
      // the fork declines simulations (tests/_svm.mjs); the send is the verdict
      const passing = Object.create(connection, { simulateTransaction: { value: async () => ({ value: { err: null, logs: [], unitsConsumed: 0 } }) } });
      return { connectFn: async () => ({ connection: passing, payer, chain, acting }) };
    },
  },
  orca: {
    pool: 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE',
    other: 'HJPjoWUrhoZzkNfRpHuieeFk9WcZWjwy6PBjZ81ngndJ',
    signer: '../venues/orca/signer.mjs',
    poolArg: true,
    deps: async ({ connection, payer, acting, vault, rpc }) => {
      const { createNoopSigner, address } = await import('@solana/kit');
      const { setNativeMintWrappingStrategy } = await import('@orca-so/whirlpools');
      setNativeMintWrappingStrategy('ata');
      const owner = createNoopSigner(address(vault.toBase58()));
      return { connectFn: async () => ({ rpc, connection, payer, acting, owner }) };
    },
  },
};

async function said(fn) {
  const lines = [], log = console.log;
  console.log = (x) => lines.push(x);
  try { await fn(); } finally { console.log = log; }
  return JSON.parse(lines.find(x => typeof x === 'string' && x.startsWith('{')) ?? 'null');
}

// What the signing page does: a fresh blockhash per message, the owner's
// signature, sent in order. Every message fits a transaction as signed.
// `expectOk` false: stop at the first that fails and return what ran.
async function ownerSigns(fork, req, owner, { expectOk = true } = {}) {
  const out = [];
  for (const b64 of req.messages) {
    const tx = Transaction.populate(Message.from(Buffer.from(b64, 'base64')));
    tx.recentBlockhash = fork.svm.latestBlockhash();
    tx.sign(owner);
    const r = await fork.sendSigned(tx);
    assert.ok(r.size <= TX_BYTES, `a message of ${r.size} bytes`);
    out.push(r);
    if (!expectOk && !r.ok) return out;
    assert.ok(r.ok, r.logs.join('\n'));
  }
  return out;
}

const ataOf = (mint, who) => spl.getAssociatedTokenAddressSync(mint, who, true);
const usdcOf = (fork, who) => fork.tokenAmount(ataOf(USDC, who)) ?? 0n;
// the vault's SOL and USDC in USDC at `px`
const valueOf = (fork, vault, px) => fork.balance(vault) / 1e9 * px + Number(usdcOf(fork, vault)) / 1e6;

const measured = {};

// A connected vault on `venue` holding 2 SOL and 300 USDC, and the bot's
// signer for it, as the venue's own fork test sets them up.
async function setup(venue) {
  const v = VENUES[venue];
  const c = await connectedVault({ venues: [venue], pools: [v.pool] });
  const { fork, connection, env, payer } = c;
  await fork.program(SOLANA.programs.metaplex_metadata);           // a legacy Raydium open's NFT metadata
  const vault = new PublicKey(c.out.vault);
  fork.fund(vault, 2);
  await fork.clone([USDC]);
  fork.tokenAccount(ataOf(USDC, vault), USDC, vault, 300_000_000n);
  const rpc = fork.kitRpc();
  const acting = await actingAccount({ connection, payer, venue, env });
  const deps = { urls: ['http://fork.test'], sleep: async () => {}, ...await v.deps({ connection, payer, acting, vault, rpc }) };
  const signer = await import(v.signer);
  process.env.LPBOT_POOL = v.pool;
  process.env.LPBOT_MAX_USD = '100000';
  delete process.env.LPBOT_TOKEN_HINTS;
  const px = (await said(() => signer.balance(v.poolArg ? v.pool : undefined, deps))).price;
  // the bot opens a position of `width` either side of the price, as the loop does
  const open = async (width = 0.01) => {
    const opened = await said(() => signer.open(v.pool, String(px * (1 - width)), String(px * (1 + width)), '0.5', '100', true, deps));
    assert.equal(opened.sent, true, JSON.stringify(opened));
    return opened;
  };
  return { ...c, v, vault, rpc, deps, signer, px, open };
}

const revokeBot = async ({ fork, connection, env, owner, payer }) =>
  ownerSigns(fork, await request('revoke', { owner: owner.publicKey.toBase58(), payer: payer.publicKey.toBase58() }, { connection, env }), owner);

for (const [venue, v] of Object.entries(VENUES)) {
  test(`${venue}: the owner revokes the bot, closes its position into the vault, and takes everything`, { timeout: 600_000 }, async () => {
    process.env.LPBOT_RAYDIUM_LEAN = '1';                   // a Token-2022 position NFT
    const c = await setup(venue);
    const { fork, connection, env, payer, owner, vault, rpc, deps, signer, px } = c;
    const opened = await c.open();
    const st = await said(() => signer.status(opened.positionMint, deps));
    assert.ok(st.positionUsd > 50, JSON.stringify(st));
    const [held] = await vaultPositions(connection, { venue, pools: [v.pool], vault, rpc });
    assert.equal(held.venue, venue);
    assert.equal(held.pool, v.pool);
    assert.ok(BigInt(held.liquidity) > 0n, JSON.stringify(held));
    assert.ok(held.position === opened.positionMint || held.nftMint === opened.positionMint, JSON.stringify(held));
    // only the pools asked about: none on another pool of the venue
    assert.deepEqual(await vaultPositions(connection, { venue, pools: [v.other], vault, rpc }), []);

    // (a) the owner revokes: the bot cannot act any more
    await revokeBot(c);
    await assert.rejects(actingAccount({ connection, payer, venue, env }), /revoked the bot/);

    // (b) the owner closes it into the vault, the bot nowhere
    const payerBefore = fork.balance(payer.publicKey), valueBefore = valueOf(fork, vault, px);
    const ownerSolBefore = fork.balance(owner.publicKey);
    const close = await request('close', { owner: owner.publicKey.toBase58() }, { connection, env, rpc });
    assert.equal(close.action, 'close');
    assert.match(close.summary, new RegExp(`^close 1 ${venue} position of the vault ${vault.toBase58()} into the vault`));
    const n = close.messages.length;
    assert.ok(close.summary.endsWith(`: ${n} transaction${n === 1 ? '' : 's'}, sign and send in order`), close.summary);
    for (const b64 of close.messages) {
      const keys = Message.from(Buffer.from(b64, 'base64')).accountKeys.map(String);
      assert.equal(keys[0], owner.publicKey.toBase58(), 'the owner pays');
      for (const k of [payer.publicKey, c.policyKey.publicKey]) assert.ok(!keys.includes(k.toBase58()), 'no bot key in an exit');
    }
    const sent = await ownerSigns(fork, close, owner);
    measured[venue] = sent.map(r => r.size);
    assert.deepEqual(await vaultPositions(connection, { venue, pools: [v.pool], vault, rpc }), []);
    assert.ok(!(fork.get(held.position)?.lamports > 0n), 'the position account is closed');
    if (held.nftMint) assert.ok(!(fork.get(held.nftMint)?.lamports > 0n), 'the position NFT is burnt');
    assert.equal(fork.balance(payer.publicKey), payerBefore, 'the bot\'s payer got nothing and paid nothing');
    assert.equal(fork.get(ataOf(SOL, vault)), null, 'no WSOL left wrapped');
    const gain = valueOf(fork, vault, px) - valueBefore;
    // the position's worth came back; rent the bot's payer paid at open
    // (DLMM, Orca) comes to the vault too: at most 0.1 SOL
    assert.ok(gain > st.positionUsd * 0.97 && gain < st.positionUsd * 1.03 + 0.1 * px,
      `the vault gained ${gain} for a position worth ${st.positionUsd}`);
    // the owner paid fees and the rent of any token account the vault lacked, nothing more
    assert.ok(ownerSolBefore - fork.balance(owner.publicKey) < 0.02e9, `the owner spent ${(ownerSolBefore - fork.balance(owner.publicKey)) / 1e9} SOL`);
    // a second request finds nothing to close
    const again = await request('close', { owner: owner.publicKey.toBase58() }, { connection, env, rpc });
    assert.deepEqual(again.messages, []);
    assert.match(again.summary, /holds no .* position/);

    // (c) everything out to the owner
    const vaultUsdc = usdcOf(fork, vault), vaultSol = fork.balance(vault);
    const ownerUsdc = usdcOf(fork, owner.publicKey), ownerSol = fork.balance(owner.publicKey);
    const usdcRent = fork.balance(ataOf(USDC, vault));
    const created = fork.get(ataOf(USDC, owner.publicKey)) ? 0 : 2_039_280;     // the owner's USDC account, new
    const all = await request('withdraw', { owner: owner.publicKey.toBase58(), to: owner.publicKey.toBase58(), mint: 'ALL' }, { connection, env });
    assert.match(all.summary, /^withdraw everything from the vault/);
    const done = await ownerSigns(fork, all, owner);
    assert.equal(fork.get(ataOf(USDC, vault)), null, 'the vault\'s USDC account is closed');
    assert.equal(fork.get(ataOf(SOL, vault)), null);
    assert.equal(fork.balance(vault), 0);
    assert.equal(usdcOf(fork, owner.publicKey) - ownerUsdc, vaultUsdc);
    // the vault's SOL and its account's rent, less the fees and the rent of the owner's new USDC account
    assert.equal(fork.balance(owner.publicKey) - ownerSol, vaultSol + usdcRent - 5000 * done.length - created);
  });
}

// Several positions, one wide: more than one transaction's worth, sent in order.
for (const venue of ['meteora-dlmm', 'raydium-clmm']) {
  test(`${venue}: several positions, a wide one among them, close over several transactions in order`, { timeout: 600_000 }, async () => {
    process.env.LPBOT_RAYDIUM_LEAN = '0';                   // a legacy (SPL) position NFT
    const c = await setup(venue);
    const { fork, connection, env, owner, payer, vault, rpc } = c;
    await c.open(0.01);
    await c.open(0.05);
    const held = await vaultPositions(connection, { venue, pools: [c.v.pool], vault, rpc });
    assert.ok(held.length >= 2, JSON.stringify(held));
    await revokeBot(c);
    const payerBefore = fork.balance(payer.publicKey);
    const close = await request('close', { owner: owner.publicKey.toBase58() }, { connection, env, rpc });
    assert.ok(close.messages.length > 1, close.summary);
    assert.match(close.summary, new RegExp(`^close ${held.length} ${venue} positions .*: ${close.messages.length} transactions, sign and send in order`));
    measured[`${venue} x${held.length}`] = (await ownerSigns(fork, close, owner)).map(r => r.size);
    assert.deepEqual(await vaultPositions(connection, { venue, pools: [c.v.pool], vault, rpc }), []);
    for (const h of held) assert.ok(!(fork.get(h.position)?.lamports > 0n), `${h.position} is closed`);
    assert.equal(fork.balance(payer.publicKey), payerBefore);
    assert.equal(fork.get(ataOf(SOL, vault)), null, 'no WSOL left wrapped');
  });
}

// A close built before the price moved more than its tolerance fails whole on
// chain, and nothing moves; one built again closes.
test('raydium-clmm: a price move past the tolerance refuses the close; a fresh request closes', { timeout: 600_000 }, async () => {
  process.env.LPBOT_RAYDIUM_LEAN = '1';
  const c = await setup('raydium-clmm');
  const { fork, connection, env, owner, vault, rpc, px } = c;
  await c.open(0.02);
  await revokeBot(c);
  const stale = await request('close', { owner: owner.publicKey.toBase58() }, { connection, env, rpc });
  // a trader buys SOL until the price is up ~2%
  const { Raydium, PoolUtils } = require('@raydium-io/raydium-sdk-v2');
  const BN = require('bn.js');
  const vaultIx = await import('../venues/raydium_clmm/vault.mjs');
  const trader = fork.wallet(10);
  fork.tokenAccount(ataOf(USDC, trader.publicKey), USDC, trader.publicKey, 10_000_000_000_000n);
  const raydium = await Raydium.load({ connection, owner: trader.publicKey, cluster: 'mainnet', disableFeatureCheck: true, disableLoadToken: true });
  for (let i = 0; i < 100; i++) {
    const r = await raydium.clmm.getPoolInfoFromRpc(c.v.pool);
    if (Number(r.poolInfo.price) > px * 1.02) break;
    const amountIn = new BN(50_000_000_000);             // small enough to stay inside the compute limit
    const q = PoolUtils.computeAmountOut({ poolInfo: r.computePoolInfo, tickarrayBitmapExtension: r.computePoolInfo.exBitmapInfo,
      tickArrayCache: r.tickData[c.v.pool], baseMint: USDC, epochInfo: { epoch: 0 }, amountIn, slippage: 0.5,
      blockTimestamp: Math.floor(Date.now() / 1000), catchLiquidityInsufficient: true });
    const ixs = vaultIx.swapIxs({ poolInfo: r.poolInfo, poolKeys: r.poolKeys, owner: trader.publicKey, inputMint: USDC,
      amountIn, amountOutMin: new BN(0), remainingAccounts: q.remainingAccounts });
    const res = await fork.send(ixs, trader);
    assert.ok(res.ok, res.logs.join('\n'));
  }
  assert.ok(Number((await raydium.clmm.getPoolInfoFromRpc(c.v.pool)).poolInfo.price) > px * 1.02, 'the price moved 2%');
  const usdcBefore = usdcOf(fork, vault);
  const sent = await ownerSigns(fork, stale, owner, { expectOk: false });
  assert.ok(!sent.at(-1).ok, 'the stale close was refused');
  assert.match(sent.at(-1).logs.join('\n'), /PriceSlippageCheck|slippage/i);
  assert.equal(usdcOf(fork, vault), usdcBefore);
  assert.equal((await vaultPositions(connection, { venue: 'raydium-clmm', pools: [c.v.pool], vault, rpc })).length, 1);
  await ownerSigns(fork, await request('close', { owner: owner.publicKey.toBase58() }, { connection, env, rpc }), owner);
  assert.deepEqual(await vaultPositions(connection, { venue: 'raydium-clmm', pools: [c.v.pool], vault, rpc }), []);
});

test('withdraw ALL: wrapped SOL unwraps to the owner, a mint the vault has no account of is skipped, nothing left is refused', { timeout: 300_000 }, async () => {
  const { fork, connection, env, owner, out } = await connectedVault();
  const vault = new PublicKey(out.vault), me = owner.publicKey.toBase58();
  const ask = (f) => request('withdraw', { owner: me, to: me, ...f }, { connection, env });
  fork.fund(vault, 0.5);
  await fork.clone([USDC]);
  fork.tokenAccount(ataOf(USDC, vault), USDC, vault, 2_000_000n);
  // a WSOL account holding 0.1 SOL
  const wsol = Buffer.alloc(spl.AccountLayout.span);
  spl.AccountLayout.encode({ mint: SOL, owner: vault, amount: 100_000_000n, delegateOption: 0, delegate: PublicKey.default, state: 1,
    isNativeOption: 1, isNative: 2_039_280n, delegatedAmount: 0n, closeAuthorityOption: 0, closeAuthority: PublicKey.default }, wsol);
  fork.set(ataOf(SOL, vault), { lamports: 102_039_280, data: wsol, owner: spl.TOKEN_PROGRAM_ID });
  await assert.rejects(ask({ mint: 'ALL', amount: '1' }), /--mint ALL takes everything; leave out --amount/);
  const usdt = new PublicKey(SOLANA.usdt_mint);
  await fork.clone([usdt]);
  await assert.rejects(ask({ mint: 'all', mints: `${SOL},${Keypair.generate().publicKey}` }), /mint .* not found/);
  const req = await ask({ mint: 'all', amount: 'all', mints: `${SOL},${USDC},${usdt}` });
  assert.equal(req.summary, `withdraw everything from the vault ${vault.toBase58()} to ${me}: 0.1 wrapped SOL, 2 of ${USDC.toBase58()}, `
    + `${fork.balance(vault) / 1e9} SOL; the vault's token accounts closed`);
  const before = fork.balance(owner.publicKey), vaultSol = fork.balance(vault);
  const sent = await ownerSigns(fork, req, owner);
  assert.equal(fork.get(ataOf(SOL, vault)), null);
  assert.equal(fork.get(ataOf(SOL, owner.publicKey)), null, 'the owner got SOL, not WSOL');
  assert.equal(usdcOf(fork, owner.publicKey), 2_000_000n);
  assert.equal(fork.balance(vault), 0);
  // the wrapped SOL and both accounts' rent, the vault's SOL; less fees and the owner's new USDC account
  assert.equal(fork.balance(owner.publicKey) - before, 102_039_280 + 2_039_280 + vaultSol - 5000 * sent.length - 2_039_280);
  await assert.rejects(ask({ mint: 'ALL' }), /holds no SOL and no account of/);
  // empty accounts: closed for their rent, no transfer, nothing said of them
  fork.fund(vault, 0.2);
  fork.tokenAccount(ataOf(USDC, vault), USDC, vault, 0n);
  wsol.writeBigUInt64LE(0n, 64);
  fork.set(ataOf(SOL, vault), { lamports: 2_039_280, data: wsol, owner: spl.TOKEN_PROGRAM_ID });
  // no mints: the SOL only
  const solOnly = await ask({ mint: 'ALL', mints: '' });
  assert.equal(solOnly.summary, `withdraw everything from the vault ${vault.toBase58()} to ${me}: 0.2 SOL; the vault's token accounts closed`);
  await ownerSigns(fork, solOnly, owner);
  assert.ok(fork.get(ataOf(USDC, vault)) && fork.get(ataOf(SOL, vault)), 'no account was touched');
  const rent = await ask({ mint: 'ALL' });
  assert.equal(rent.summary, `withdraw everything from the vault ${vault.toBase58()} to ${me}: nothing but account rent; the vault's token accounts closed`);
  const usdcAt = fork.tokenAmount(ataOf(USDC, owner.publicKey)), at = fork.balance(owner.publicKey);
  const closed = await ownerSigns(fork, rent, owner);
  assert.equal(fork.get(ataOf(USDC, vault)), null);
  assert.equal(fork.get(ataOf(SOL, vault)), null);
  assert.equal(fork.tokenAmount(ataOf(USDC, owner.publicKey)), usdcAt);
  assert.equal(fork.balance(owner.publicKey) - at, 2 * 2_039_280 - 5000 * closed.length);
});

test('every venue a vault approves has an exit', () => {
  assert.deepEqual(Object.keys(VENUES).sort(), Object.keys(EXIT_INSTRUCTIONS).sort());
});

test.after(() => { if (Object.keys(measured).length) console.error('exit message sizes (bytes, signed):', JSON.stringify(measured)); });
