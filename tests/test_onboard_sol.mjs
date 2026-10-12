// onboard/solana.mjs, the owner's page side: a deposit read back from the
// chain (only what the owner moved into the vault counts), the vault's
// accounts the page's decoder checks, a signed transaction sent as it is,
// and a signature's state. Stub connections; the fork: test_onboard_fork.mjs.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import fc from 'fast-check';
import * as sq from '../chains/solana/vault/squads.mjs';
import { loadConfig } from '../onboard/server.mjs';
import { broadcast, checkDeposit, confirm, context, depositVerdict, ownerTxs, status } from '../onboard/solana.mjs';
import { toRaw } from '../onboard/units.mjs';

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, SystemProgram, Transaction, TransactionMessage } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const bs58 = require('bs58').default;

const C = loadConfig().chains.solana;
const OWNER = Keypair.generate().publicKey.toBase58(), VAULT = Keypair.generate().publicKey.toBase58(), OTHER = Keypair.generate().publicKey.toBase58();
const USDC_ATA = spl.getAssociatedTokenAddressSync(new PublicKey(C.tokens.USDC.mint), new PublicKey(VAULT), true).toBase58();
const TOKENS = { SOL: C.tokens.SOL, USDC: { ...C.tokens.USDC, ata: USDC_ATA } };
const OK = { err: null, confirmationStatus: 'confirmed' };
const sol = (source, destination, lamports) => ({ program: 'system', parsed: { type: 'transfer', info: { source, destination, lamports } } });
const usdc = (authority, destination, amount, type = 'transferChecked') => ({ program: 'spl-token', parsed: { type,
  info: type === 'transfer' ? { authority, destination, amount: String(amount) } : { authority, destination, tokenAmount: { amount: String(amount) } } } });
const parsed = (...instructions) => ({ meta: { err: null }, transaction: { message: { instructions } } });
const expect = { owner: OWNER, vault: VAULT, tokens: TOKENS };

test('a deposit: only the owner\'s transfers into the vault count, summed exactly per token', () => {
  assert.deepEqual(depositVerdict(OK, parsed(sol(OWNER, VAULT, 1_355_000_000)), expect), { state: 'ok', deposits: [{ token: 'SOL', amount: '1.355' }] });
  assert.deepEqual(depositVerdict(OK, parsed(usdc(OWNER, USDC_ATA, 10_500_000), usdc(OWNER, USDC_ATA, 1, 'transfer'), sol(OWNER, VAULT, 1)), expect),
    { state: 'ok', deposits: [{ token: 'USDC', amount: '10.500001' }, { token: 'SOL', amount: '0.000000001' }] });
  const bad = [
    [parsed(sol(OTHER, VAULT, 5)), 'from someone else'], [parsed(sol(OWNER, OTHER, 5)), 'to someone else'],
    [parsed(usdc(OTHER, USDC_ATA, 5)), 'a token someone else signed'], [parsed(usdc(OWNER, OTHER, 5)), 'into another account'],
    [parsed({ program: 'spl-token', parsed: { type: 'approve', info: { authority: OWNER, destination: USDC_ATA, amount: '5' } } }), 'not a transfer'],
    [parsed(sol(OWNER, VAULT, 0)), 'nothing'], [parsed(), 'no instruction'],
  ];
  for (const [p, what] of bad) assert.equal(depositVerdict(OK, p, expect).state, 'bad', what);
  assert.equal(depositVerdict(null, null, expect).state, 'pending');
  assert.equal(depositVerdict({ err: null, confirmationStatus: 'processed' }, null, expect).state, 'pending');
  assert.equal(depositVerdict(OK, null, expect).state, 'pending');
  assert.equal(depositVerdict({ err: { x: 1 }, confirmationStatus: 'confirmed' }, null, expect).state, 'bad');
  assert.equal(depositVerdict(OK, { meta: { err: 'x' } }, expect).state, 'bad');
  fc.assert(fc.property(fc.array(fc.bigInt({ min: 1n, max: 10n ** 15n }), { minLength: 1, maxLength: 5 }), (xs) => {
    const v = depositVerdict(OK, parsed(...xs.map(x => sol(OWNER, VAULT, x.toString()))), expect);
    const sum = xs.reduce((a, b) => a + b, 0n);
    assert.equal(toRaw(v.deposits[0].amount, 9), sum);
  }), { numRuns: 200 });
});

test('checkDeposit reads the signature\'s status and transaction; a non-signature reads nothing', async () => {
  const sig = bs58.encode(Buffer.alloc(64, 7));
  const reads = [];
  const conn = { getSignatureStatuses: async (s) => { reads.push(s); return { value: [OK] }; },
    getParsedTransaction: async () => parsed(sol(OWNER, VAULT, 2_000_000)), getAccountInfo: async () => ({ owner: spl.TOKEN_PROGRAM_ID }) };
  assert.deepEqual(await checkDeposit(conn, C, sig, { owner: OWNER, vault: VAULT }), { state: 'ok', deposits: [{ token: 'SOL', amount: '0.002' }] });
  for (const bad of ['', 'xyz', bs58.encode(Buffer.alloc(32, 1)), `${sig}0`]) {
    assert.equal((await checkDeposit(conn, C, bad, { owner: OWNER, vault: VAULT })).state, 'bad', bad);
  }
  assert.equal(reads.length, 1);
});

test('confirm: confirmed, failed or pending, from the status', async () => {
  const sig = bs58.encode(Buffer.alloc(64, 9));
  const at = (st) => ({ getSignatureStatuses: async () => ({ value: [st] }) });
  assert.deepEqual(await confirm(at(OK), sig), { state: 'confirmed' });
  assert.deepEqual(await confirm(at({ ...OK, confirmationStatus: 'finalized' }), sig), { state: 'confirmed' });
  assert.equal((await confirm(at(null), sig)).state, 'pending');
  assert.equal((await confirm(at({ err: null, confirmationStatus: 'processed' }), sig)).state, 'pending');
  assert.match((await confirm(at({ err: { InstructionError: [0, 'x'] }, confirmationStatus: 'confirmed' }), sig)).reason, /failed on chain/);
  assert.equal((await confirm(at(OK), 'nope')).state, 'failed');
});

test('broadcast: a signed transaction is sent as it is; an unsigned one or garbage is not', async () => {
  const key = Keypair.generate();
  const tx = new Transaction().add(SystemProgram.transfer({ fromPubkey: key.publicKey, toPubkey: new PublicKey(VAULT), lamports: 1 }));
  tx.feePayer = key.publicKey; tx.recentBlockhash = PublicKey.default.toBase58();
  const unsigned = tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');
  tx.sign(key);
  const sent = [];
  const conn = { sendRawTransaction: async (raw) => { sent.push(Buffer.from(raw)); return 'SIG'; } };
  assert.equal(await broadcast(conn, tx.serialize().toString('base64')), 'SIG');
  assert.deepEqual(sent, [tx.serialize()], 'the bytes the wallet signed, unchanged');
  await assert.rejects(broadcast(conn, unsigned), /not signed/);
  await assert.rejects(broadcast(conn, Buffer.from('garbage').toString('base64')), /not a signed transaction/);
  await assert.rejects(broadcast({ sendRawTransaction: async () => { throw new Error('Blockhash not found\nlogs…'); } }, tx.serialize().toString('base64')),
    /the network refused the transaction: Blockhash not found$/);
  assert.equal(sent.length, 1);
});

test('the context the page checks: the vault from its settings, each venue\'s policies and pools, the payout wallets', () => {
  const settings = Keypair.generate().publicKey.toBase58();
  const r = { profit_wallet: OWNER, fee_wallet: OTHER, vault_row: { settings, venues: { 'raydium-clmm': { policies: ['P1', 'P2'], pools: ['X'], payouts: [] } } } };
  assert.deepEqual(context(r), { settings, vault: sq.vaultPda(new PublicKey(settings)).toBase58(), policies: { 'raydium-clmm': ['P1', 'P2'] },
    pools: { 'raydium-clmm': ['X'] }, payoutWallets: [OWNER, OTHER] });
  assert.deepEqual(context({ ...r, fee_wallet: null }).payoutWallets, [OWNER], 'the operator\'s own wallet pays no fee share');
});

// --- the owner's transactions and the vault's state, with the builders stubbed ---------------

const KEYS = { payer: Keypair.generate(), policyKey: Keypair.generate(), policyPath: '/keys/policy.json' };
const SETTINGS = Keypair.generate().publicKey.toBase58();
const ROW = { settings: SETTINGS, venues: { 'raydium-clmm': { policies: [Keypair.generate().publicKey.toBase58()], pools: [C.pools['raydium-clmm']], payouts: [] },
  orca: { policies: [Keypair.generate().publicKey.toBase58()], pools: [C.pools.orca], payouts: [] } } };
const message = (payer) => Buffer.from(new TransactionMessage({ payerKey: new PublicKey(payer), recentBlockhash: PublicKey.default.toBase58(),
  instructions: [SystemProgram.transfer({ fromPubkey: new PublicKey(payer), toPubkey: new PublicKey(VAULT), lamports: 1 })] }).compileToLegacyMessage().serialize()).toString('base64');
const BLOCKHASH = Keypair.generate().publicKey.toBase58();
const conn = { getLatestBlockhash: async () => ({ blockhash: BLOCKHASH }), getMinimumBalanceForRentExemption: async (n) => 890_880 + 6960 * n,
  getBalance: async () => 10e9 };

function stubs(positionsByVenue = {}) {
  const calls = [];
  return { calls, deps: {
    request: async (action, f, { env }) => { calls.push([action, f, env.LPBOT_VAULT_VENUE]); return { summary: `${action} ${env.LPBOT_VAULT_VENUE}`, messages: [message(f.owner)] }; },
    rulesRequest: async (action, a) => {
      calls.push([action, a]);
      const ix = SystemProgram.transfer({ fromPubkey: a.owner, toPubkey: new PublicKey(VAULT), lamports: 1 });
      return { summary: `${action} ${a.venues.map(v => v.venue).join(' and ')}`, groups: a.venues.map(() => [ix]), steps: a.venues.map(() => ({ kind: 'update' })) };
    },
    positions: async (_c, { venue }) => positionsByVenue[venue] ?? [],
  } };
}

test('owner transactions: each action builds what it should, for every venue it concerns, with a fresh blockhash', async () => {
  const run = async (a, s = stubs()) => ({ out: await ownerTxs(conn, C, ROW, KEYS, { owner: OWNER, ...a }, s.deps), calls: s.calls });
  // withdraw one token: SOL by name, USDC by mint; an amount the token cannot hold is refused before anything is built
  let r = await run({ action: 'withdraw', token: 'SOL', amount: '1.5' });
  assert.deepEqual(r.calls, [['withdraw', { owner: OWNER, to: OWNER, mint: 'SOL', amount: '1.5' }, 'raydium-clmm']]);
  r = await run({ action: 'withdraw', token: 'USDC', amount: 'all' });
  assert.deepEqual(r.calls[0][1].mint, C.tokens.USDC.mint);
  for (const [token, amount, re] of [['USDC', '1.0000001', /at most 6 decimals/], ['SOL', '0', /a number or all/], ['SOL', '0.0000000001', /at most 9 decimals/],
    ['DOGE', '1', /token DOGE/]]) {
    await assert.rejects(run({ action: 'withdraw', token, amount }), re, `${amount} ${token}`);
  }
  r = await run({ action: 'withdraw', token: 'SOL', amount: '0.000000001' });
  assert.equal(r.calls.length, 1, 'one lamport is an amount');
  // withdraw everything: every configured mint
  r = await run({ action: 'withdraw', token: 'ALL', amount: 'all' });
  assert.deepEqual(r.calls, [['withdraw', { owner: OWNER, to: OWNER, mint: 'ALL', mints: Object.values(C.tokens).map(t => t.mint).join(',') }, 'raydium-clmm']]);
  // close: only the venues holding a position; none at all is no transaction
  r = await run({ action: 'close' }, stubs({ orca: [{ position: 'P' }] }));
  assert.deepEqual(r.calls.map(c => [c[0], c[2]]), [['close', 'orca']]);
  assert.equal(r.out.transactions.length, 1);
  r = await run({ action: 'close' }, stubs({}));
  assert.deepEqual([r.calls, r.out.transactions], [[], []]);
  assert.match(r.out.summary, /nothing to close/);
  // revoke and approve: every venue's policies in one request, the bot's keys named
  for (const action of ['revoke', 'approve']) {
    r = await run({ action });
    assert.equal(r.calls.length, 1);
    const [a, req] = r.calls[0];
    assert.deepEqual([a, req.settings, req.owner.toBase58(), req.payer.toBase58(), req.policyKey.toBase58(), req.venues.map(v => v.venue)],
      [action, SETTINGS, OWNER, KEYS.payer.publicKey.toBase58(), KEYS.policyKey.publicKey.toBase58(), ['raydium-clmm', 'orca']]);
    assert.equal(r.out.summary, `${action} raydium-clmm and orca`);
    assert.equal(r.out.transactions.length, 2);
  }
  // an approval the wallet cannot pay for is refused before it is built for signing
  const poor = { ...conn, getBalance: async () => 1000 };
  await assert.rejects(ownerTxs(poor, C, ROW, KEYS, { owner: OWNER, action: 'approve' }, stubs().deps), /approving needs/);
  await ownerTxs(poor, C, ROW, KEYS, { owner: OWNER, action: 'revoke' }, stubs().deps);
  await assert.rejects(run({ action: 'teleport' }), /action teleport/);
  // each transaction: the message as built, its blockhash the newest
  const tx = Transaction.from(Buffer.from((await run({ action: 'revoke' })).out.transactions[0], 'base64'));
  assert.equal(tx.recentBlockhash, BLOCKHASH);
  assert.equal(tx.feePayer.toBase58(), OWNER);
});

test('the vault\'s state: balances, whether every policy still names the bot, its positions on every venue', async () => {
  const head = (signers) => {
    const n = Buffer.alloc(4); n.writeUInt32LE(signers.length);
    return Buffer.concat([require('node:crypto').createHash('sha256').update('account:Policy').digest().subarray(0, 8), new PublicKey(SETTINGS).toBuffer(),
      Buffer.alloc(8), Buffer.from([255]), Buffer.alloc(16), n, ...signers.map(s => Buffer.concat([s.toBuffer(), Buffer.from([7])])), Buffer.alloc(6)]);
  };
  const connOf = (infos) => ({ getBalance: async () => 2_500_000_000, getAccountInfo: async (k) => (k.toBase58() === C.tokens.USDC.mint ? { owner: spl.TOKEN_PROGRAM_ID } : null),
    getMultipleAccountsInfo: async () => infos });
  const bot = KEYS.policyKey.publicKey, owner = new PublicKey(OWNER);
  const at = (infos, pos = {}) => status(connOf(infos), C, ROW, KEYS, { positionsOf: async (_c, { venue }) => pos[venue] ?? [] });
  let s = await at([{ data: head([bot]) }, { data: head([owner, bot]) }], { orca: [1, 2], 'raydium-clmm': [3] });
  assert.deepEqual([s.balances, s.botActive, s.positions], [{ SOL: '2.5', USDC: '0' }, true, 3]);
  assert.equal(s.vault, sq.vaultPda(new PublicKey(SETTINGS)).toBase58());
  s = await at([{ data: head([bot]) }, { data: head([owner]) }]);
  assert.deepEqual([s.botActive, s.positions], [false, 0], 'one policy without the bot: revoked');
  assert.equal((await at([{ data: head([bot]) }, null])).botActive, true, 'a policy not created yet (a venue staged) says nothing');
  assert.equal((await at([null, null])).botActive, false, 'none at all: no bot');
  assert.equal((await at([])).botActive, false, 'no policy at all: no bot');
});
