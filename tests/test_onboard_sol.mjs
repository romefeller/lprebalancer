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
import { broadcast, checkDeposit, confirm, context, depositVerdict } from '../onboard/solana.mjs';
import { toRaw } from '../onboard/units.mjs';

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, SystemProgram, Transaction } = require('@solana/web3.js');
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
