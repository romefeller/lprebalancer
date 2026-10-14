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
import * as I from '../onboard/intent.mjs';
import { broadcast, checkDeposit, confirm, context, depositTxs, depositVerdict, findDeposit, findFunding, funding, ownerTxs, setupCost, setupState, setupTxs, status,
  vaultVenues, SETTINGS_ACCOUNT_BYTES, POLICY_ACCOUNT_BYTES, TOKEN_ACCOUNT_BYTES, SETUP_KEYS_MAX } from '../onboard/solana.mjs';
import { toRaw } from '../onboard/units.mjs';
import { ata, decodeSolSet, OWNER_MESSAGE_BYTES } from '../onboard/decode_sol.mjs';
import { compile, RULES } from '../chains/solana/vault/policy.mjs';
import { ownerSetupPlan } from '../chains/solana/vault/admin.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, SystemInstruction, SystemProgram, Transaction, TransactionMessage } = require('@solana/web3.js');
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
    /the network refused the transaction: Blockhash not found/);
  // a program's refusal: its decoded error and log lines, one bounded line
  const { SendTransactionError } = require('@solana/web3.js');
  const refused = new SendTransactionError({ action: 'simulate', signature: '', transactionMessage: 'Simulation failed.',
    logs: ['Program SMRTzfY6DfH5ik3TKiyLFfXexV8uSG3d2UksSCYdunG invoke [1]', 'Program log: AnchorError: SeedMismatch', 'Program failed: custom program error: 0x1772'] });
  const e = await broadcast({ sendRawTransaction: async () => { throw refused; } }, tx.serialize().toString('base64')).catch(x => x);
  assert.match(e.message, /^the network refused the transaction: .*SeedMismatch/);
  assert.ok(e.message.length <= 450 && !e.message.includes('\n'));
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
    assert.equal(r.out.transactions.length, 1, 'packed: both small steps in one transaction');
  }
  // a re-grant with more capital: the rules, then the deposit, in as few transactions as fit
  r = await run({ action: 'approve', token: 'SOL', amount: '0.7' });
  const regrant = r.out.transactions.map(t => Transaction.from(Buffer.from(t, 'base64')));
  const last = regrant.at(-1).instructions.at(-1);
  assert.equal(SystemInstruction.decodeInstructionType(last), 'Transfer');
  assert.deepEqual(SystemInstruction.decodeTransfer(last), { fromPubkey: new PublicKey(OWNER), toPubkey: sq.vaultPda(new PublicKey(SETTINGS)), lamports: 700_000_000n });
  assert.match(r.out.summary, /then deposit 0\.7 SOL into your vault$/);
  // the wallet must hold the deposit too
  const short = { ...conn, getBalance: async () => 700_000_000 };
  await assert.rejects(ownerTxs(short, C, ROW, KEYS, { owner: OWNER, action: 'approve', token: 'SOL', amount: '0.7' }, stubs().deps), /approving needs|deposit needs/);
  await assert.rejects(run({ action: 'approve', token: 'DOGE', amount: '1' }), /token DOGE/);
  // a revoke never carries a deposit, whatever the body says
  r = await run({ action: 'revoke', token: 'SOL', amount: '0.7' });
  assert.deepEqual(r.out, (await run({ action: 'revoke' })).out);
  // the wallet holds the deposit and a fee, not the rules' rent beside it: the approval is refused
  const rulesOnly = { ...conn, getBalance: async () => 700_000_000 + 20_000 };
  await assert.rejects(ownerTxs(rulesOnly, C, ROW, KEYS, { owner: OWNER, action: 'approve', token: 'SOL', amount: '0.7' }, stubs().deps), /approving needs/);
  await ownerTxs({ ...conn, getBalance: async () => 2e9 }, C, ROW, KEYS, { owner: OWNER, action: 'approve', token: 'SOL', amount: '0.7' }, stubs().deps);
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

// --- the owner's own set-up, over a chain kept here ---------------------------------------------

const PAYER = KEYS.payer.publicKey;
const programConfig = (fee = 0n) => {
  const disc = require('node:crypto').createHash('sha256').update('account:ProgramConfig').digest().subarray(0, 8);
  const n = Buffer.alloc(16); n.writeBigUInt64LE(41n);
  const f = Buffer.alloc(8); f.writeBigUInt64LE(fee);
  return Buffer.concat([disc, n, Keypair.generate().publicKey.toBuffer(), f, Keypair.generate().publicKey.toBuffer()]);
};
// the owner's USDC account holds `usdc` raw units
const usdcAccount = (usdc) => {
  const data = Buffer.alloc(spl.AccountLayout.span);
  spl.AccountLayout.encode({ mint: new PublicKey(C.tokens.USDC.mint), owner: new PublicKey(OWNER), amount: usdc, delegateOption: 0, delegate: PublicKey.default,
    state: 1, isNativeOption: 0, isNative: 0n, delegatedAmount: 0n, closeAuthorityOption: 0, closeAuthority: PublicKey.default }, data);
  return { data, owner: spl.TOKEN_PROGRAM_ID, lamports: 2_039_280, executable: false };
};
// the owner's wallet holds `balance` lamports; the set-up key `held`
const setupConn = (balance = 10e9, fee = 0n, usdc = 30_000_000n, held = 0) => ({ ...conn,
  getBalance: async (k) => (KEY && k.equals(KEY.publicKey) ? held : balance),
  getAccountInfo: async (k) => (k.equals(sq.programConfigPda()) ? { data: programConfig(fee) } : k.toBase58() === C.tokens.USDC.mint
    ? { owner: spl.TOKEN_PROGRAM_ID } : usdcAccount(usdc)) });
let KEY = null;
const OPERATOR = SOLANA.operator_profit_wallet, POOL = C.pools['raydium-clmm'], TREASURY = Keypair.generate().publicKey;
const rent = (n) => 890_880 + 6960 * n;

// The set-up reader (cli.mjs setupRequest) over a chain kept here: the real
// plan (admin.mjs ownerSetupPlan, Raydium) for the flags it is given, the
// vault `created` or not, `made` of its policies, the signers beside the
// owner (`others`). `funded`: the funding search's answer.
function chainStub({ created = false, made = 0, others = [], funded = null } = {}) {
  const calls = [];
  return { calls, find: async () => funded, request: async (f, keys) => {
    calls.push([f, keys]);
    const seed = BigInt(f.seed ?? '42'), settings = sq.settingsPda(seed), vault = sq.vaultPda(settings);
    const compiled = compile('raydium-clmm', { vault, payer: keys.payer, pools: [new PublicKey(POOL)],
      payouts: f.payout.split(',').map(w => new PublicKey(ata(w, SOLANA.usdc_mint))) });
    const plan = ownerSetupPlan({ owner: f.owner, treasury: TREASURY, seed, policyKey: keys.policyKey, venues: [compiled], memo: 'lp-bot vault',
      setupKey: f['setup-key'] ?? null });
    const done = [...(created ? [0] : []), ...Array.from({ length: made }, (_, i) => i + 1)];
    return { seed: String(seed), settings: settings.toBase58(), vault: vault.toBase58(), done, others, release: plan.release,
      report: { venues: { 'raydium-clmm': { policies: plan.policies.map(String), pools: [POOL] } } }, steps: plan.steps.filter((_, i) => !done.includes(i)) };
  } };
}
const A = { owner: OWNER, profitWallet: OWNER, feeWallet: OPERATOR, token: 'SOL', deposit: '0.5', gas: '0.1186', planned: null };
const PINS = { payer: PAYER.toBase58(), policyKey: KEYS.policyKey.publicKey.toBase58(), operator: OPERATOR,
  pools: { 'raydium-clmm': [POOL] }, rules: RULES, solana: SOLANA };
// what the page expects of this set-up (main.mjs setupExpect)
const pageExpect = (out, over = {}) => ({ kind: 'setup', owner: OWNER, seed: out.planned.seed, settings: out.planned.settings, vault: out.planned.vault,
  venues: ['raydium-clmm'], pools: { 'raydium-clmm': [POOL] }, payoutWallets: [OWNER, OPERATOR], mint: SOLANA.native_mint, raw: '500000000',
  gasRaw: '118600000', setupKey: KEY.publicKey.toBase58(), ...over });
const txOf = (b64) => Transaction.from(Buffer.from(b64, 'base64'));
const all = (out) => [...out.owner, ...out.key, ...out.fund];
const ownerBytes = (b64) => txOf(b64).compileMessage().serialize().length;

test('set-up: the owner signs the creation and the key\'s SOL; the set-up key each policy; the funding last; every message the owner signs fits a card', async () => {
  KEY = Keypair.generate();
  const stub = chainStub();
  const out = await setupTxs(setupConn(), KEYS, C, { ...A, setupKey: KEY.publicKey.toBase58() }, stub);
  const [f, keys] = stub.calls[0];
  const venues = vaultVenues(C);
  assert.deepEqual(f, { owner: OWNER, venue: venues.join(','), pools: venues.map(v => C.pools[v]).join(','), payout: `${OWNER},${OPERATOR}`,
    'payout-mint': C.payoutMint, 'setup-keys': KEY.publicKey.toBase58(), 'setup-key': KEY.publicKey.toBase58() });
  assert.deepEqual([keys.payer.toBase58(), keys.policyKey.toBase58()], [PAYER.toBase58(), KEYS.policyKey.publicKey.toBase58()]);
  assert.deepEqual(out.planned, { seed: '42', settings: sq.settingsPda(42n).toBase58(), vault: sq.vaultPda(sq.settingsPda(42n)).toBase58(),
    setupKeys: [KEY.publicKey.toBase58()] });
  assert.deepEqual([out.done, out.owner.length, out.key.length, out.fund.length], [false, 1, 2, 1]);
  for (const t of [...out.owner, ...out.fund]) {
    assert.deepEqual(txOf(t).signatures.map(s => s.publicKey.toBase58()), [OWNER], 'the owner alone signs its part');
    assert.ok(ownerBytes(t) <= OWNER_MESSAGE_BYTES, `${ownerBytes(t)} bytes`);
  }
  for (const t of out.key) assert.deepEqual(txOf(t).signatures.map(s => s.publicKey.toBase58()), [KEY.publicKey.toBase58()], 'the key alone signs its part');
  assert.ok(out.key.some(t => ownerBytes(t) > 930), 'a policy is longer than a Tangem card signs: the key signs it');
  assert.ok([...out.owner, ...out.key, ...out.fund].every(t => txOf(t).recentBlockhash === BLOCKHASH));
  // the key gets what its two policies and three transactions take, and its account's floor
  const keyFund = BigInt(2 * rent(POLICY_ACCOUNT_BYTES) + 3 * 5000 + rent(0));
  assert.equal(out.keyFund, I.fromRaw(keyFund, 9));
  const grant = txOf(out.owner[0]).instructions;
  assert.equal(grant.length, 2, 'the creation and the key\'s SOL');
  assert.deepEqual(SystemInstruction.decodeTransfer(grant[1]), { fromPubkey: new PublicKey(OWNER), toPubkey: KEY.publicKey, lamports: keyFund });
  const money = txOf(out.fund[0]).instructions.map(ix => SystemInstruction.decodeTransfer(ix));
  assert.deepEqual(money.map(x => [x.toPubkey.toBase58(), x.lamports]), [[out.planned.vault, 500_000_000n], [PAYER.toBase58(), 118_600_000n]]);
  // the page's decoder reads the whole set as the page sends it: the owner's, the key's, the funding
  const r = decodeSolSet(all(out), pageExpect(out), PINS);
  assert.deepEqual(r.problems, [], r.lines.join('\n'));
  // its cost: the creation's rent, the key's SOL, two owner's fees, the gas, the deposit, the floor
  assert.equal(out.cost, I.fromRaw(BigInt(rent(SETTINGS_ACCOUNT_BYTES)) + keyFund + 20_000n + 118_600_000n + 500_000_000n + BigInt(rent(0)), 9));
  // a wallet that cannot pay all of it is refused before anything is built for it
  await assert.rejects(setupTxs(setupConn(600_000_000), KEYS, C, { ...A, setupKey: KEY.publicKey.toBase58() }, chainStub()), /your wallet holds 0\.6 SOL; the set-up needs/);
  // a token deposit: the vault's account opened, the transfer and the gas, all in the funding, short
  const tok = await setupTxs(setupConn(), KEYS, C, { ...A, token: 'USDC', deposit: '25', setupKey: KEY.publicKey.toBase58() }, chainStub());
  const kinds = tok.fund.map(txOf).flatMap(t => t.instructions).map(ix => (ix.programId.equals(SystemProgram.programId) ? `sol ${SystemInstruction.decodeTransfer(ix).lamports}`
    : ix.programId.equals(spl.ASSOCIATED_TOKEN_PROGRAM_ID) ? 'open' : `token ${ix.data[0]}`));
  assert.deepEqual(kinds, ['open', 'token 12', 'sol 118600000']);
  for (const t of tok.fund) assert.ok(ownerBytes(t) <= OWNER_MESSAGE_BYTES);
  assert.deepEqual(decodeSolSet(all(tok), pageExpect(tok, { mint: SOLANA.usdc_mint, raw: '25000000' }), PINS).problems, []);
  await assert.rejects(setupTxs(setupConn(10e9, 0n, 24_999_999n), KEYS, C, { ...A, token: 'USDC', deposit: '25', setupKey: KEY.publicKey.toBase58() }, chainStub()),
    /your wallet holds 24\.999999 USDC; the deposit is 25/);
});

test('set-up: no set-up key, the owner\'s or the bot\'s as one, or too many of them: refused', async () => {
  KEY = Keypair.generate();
  for (const k of [undefined, '', 'not-a-key']) await assert.rejects(setupTxs(setupConn(), KEYS, C, { ...A, setupKey: k }, chainStub()), /no set-up key: reload the page/);
  for (const k of [OWNER, PAYER.toBase58(), KEYS.policyKey.publicKey.toBase58()]) {
    await assert.rejects(setupTxs(setupConn(), KEYS, C, { ...A, setupKey: k }, chainStub()), /not the page's own/);
  }
  const full = Array.from({ length: SETUP_KEYS_MAX }, () => Keypair.generate().publicKey.toBase58());
  await assert.rejects(setupTxs(setupConn(), KEYS, C, { ...A, setupKey: KEY.publicKey.toBase58(), planned: { seed: '42', setupKeys: full } }, chainStub()),
    /too many set-up keys/);
  const out = await setupTxs(setupConn(), KEYS, C, { ...A, setupKey: full[0], planned: { seed: '42', setupKeys: full } }, chainStub());
  assert.deepEqual(out.planned.setupKeys, full, 'a key the request made already is no new one');
  const less = await setupTxs(setupConn(), KEYS, C, { ...A, setupKey: KEY.publicKey.toBase58(), planned: { seed: '42', setupKeys: full.slice(1) } }, chainStub());
  assert.equal(less.planned.setupKeys.length, SETUP_KEYS_MAX, 'the last one there is room for');
});

test('set-up resumes from the chain: the key on the vault goes on; a new page\'s key replaces the old; the owner signs little or nothing', async () => {
  KEY = Keypair.generate();
  const key = KEY.publicKey.toBase58(), old = Keypair.generate().publicKey.toBase58();
  const planned = { seed: '42', setupKeys: [key] };
  // created, one policy of two made, the key on it holding enough: the owner signs nothing until the funding
  let out = await setupTxs(setupConn(10e9, 0n, 0n, 50_000_000), KEYS, C, { ...A, setupKey: key, planned }, chainStub({ created: true, made: 1, others: [key] }));
  assert.deepEqual([out.owner.length, out.key.length, out.fund.length, out.keyFund], [0, 1, 1, '0']);
  // what it costs: no creation, nothing to the key; the funding's fee, the gas, the deposit, the floor
  assert.equal(out.cost, I.fromRaw(10_000n + 118_600_000n + 500_000_000n + BigInt(rent(0)), 9));
  // the key holds exactly what it needs: nothing more from the owner
  const exact = rent(POLICY_ACCOUNT_BYTES) + 2 * 5000 + rent(0);
  out = await setupTxs(setupConn(10e9, 0n, 0n, exact), KEYS, C, { ...A, setupKey: key, planned }, chainStub({ created: true, made: 1, others: [key] }));
  assert.deepEqual([out.owner.length, out.keyFund], [0, '0']);
  // a funding found on chain does not count while the set-up is not whole
  out = await setupTxs(setupConn(10e9, 0n, 0n, 50_000_000), KEYS, C, { ...A, setupKey: key, planned },
    chainStub({ created: true, made: 2, others: [key], funded: 'SIG' }));
  assert.deepEqual([out.done, out.key.length, out.fund.length], [false, 1, 1], 'the key to take off, and the funding');
  assert.deepEqual(decodeSolSet(all(out), pageExpect(out), PINS).problems, []);
  // the key holds too little: the owner tops it up, no more
  out = await setupTxs(setupConn(10e9, 0n, 0n, 1_000_000), KEYS, C, { ...A, setupKey: key, planned }, chainStub({ created: true, made: 1, others: [key] }));
  const need = BigInt(rent(POLICY_ACCOUNT_BYTES) + 2 * 5000 + rent(0));
  assert.deepEqual(txOf(out.owner[0]).instructions.map(ix => SystemInstruction.decodeTransfer(ix).lamports), [need - 1_000_000n]);
  assert.deepEqual(decodeSolSet(all(out), pageExpect(out), PINS).problems, []);
  // a page reloaded without its key: the owner takes the old key off and puts this one on, one short message
  out = await setupTxs(setupConn(), KEYS, C, { ...A, setupKey: key, planned: { seed: '42', setupKeys: [old] } }, chainStub({ created: true, made: 1, others: [old] }));
  assert.deepEqual(out.planned.setupKeys, [old, key]);
  assert.equal(out.owner.length, 1);
  assert.ok(ownerBytes(out.owner[0]) <= OWNER_MESSAGE_BYTES);
  let r = decodeSolSet(all(out), pageExpect(out), PINS);
  assert.deepEqual(r.problems, [], r.lines.join('\n'));
  assert.match(r.lines.join('\n'), new RegExp(`Take the older set-up key ${old.slice(0, 4)}…${old.slice(-4)} off your vault`));
  assert.match(r.lines.join('\n'), /Put this page's one-time set-up key .* on your vault/);
  // every policy made, the key still on: it takes itself off and sends back all it holds but its fee
  out = await setupTxs(setupConn(10e9, 0n, 0n, 7_000_000), KEYS, C, { ...A, setupKey: key, planned }, chainStub({ created: true, made: 2, others: [key] }));
  assert.deepEqual([out.owner.length, out.key.length, out.fund.length], [0, 1, 1]);
  const release = txOf(out.key[0]).instructions;
  assert.equal(release.length, 2);
  assert.deepEqual(SystemInstruction.decodeTransfer(release[1]), { fromPubkey: KEY.publicKey, toPubkey: new PublicKey(OWNER), lamports: 6_995_000n });
  r = decodeSolSet(all(out), pageExpect(out), PINS);
  assert.deepEqual(r.problems, [], r.lines.join('\n'));
  assert.match(r.lines.join('\n'), /The set-up key takes itself off your vault: you are its only owner again/);
  // a key holding its fee and no more: it takes itself off, nothing to send back
  out = await setupTxs(setupConn(10e9, 0n, 0n, 5000), KEYS, C, { ...A, setupKey: key, planned }, chainStub({ created: true, made: 2, others: [key] }));
  assert.deepEqual([out.owner.length, out.key.length, txOf(out.key[0]).instructions.length], [0, 1, 1]);
  // a key holding less than its fee: the owner takes it off
  out = await setupTxs(setupConn(10e9, 0n, 0n, 4999), KEYS, C, { ...A, setupKey: key, planned }, chainStub({ created: true, made: 2, others: [key] }));
  assert.deepEqual([out.owner.length, out.key.length], [1, 0]);
  assert.deepEqual(decodeSolSet(all(out), pageExpect(out), PINS).problems, []);
  // the vault whole, the owner alone on it: the funding only, or done when it is on chain
  out = await setupTxs(setupConn(), KEYS, C, { ...A, setupKey: key, planned }, chainStub({ created: true, made: 2 }));
  assert.deepEqual([out.owner.length, out.key.length, out.fund.length], [0, 0, 1]);
  const found = chainStub({ created: true, made: 2, funded: 'SIG' });
  out = await setupTxs(setupConn(), KEYS, C, { ...A, setupKey: key, planned, notBefore: 99 }, found);
  assert.deepEqual([out.done, out.owner, out.key, out.fund, out.funded], [true, [], [], [], 'SIG']);
});

test('set-up cost: the creation fee, the settings\' rent, the key\'s SOL, the fees, the gas, a SOL deposit, the wallet\'s floor', async () => {
  const base = { owner: OWNER, created: 1, keyFund: 30_000_000n, txs: 2, gas: 118_600_000n, token: 'SOL', deposit: '0.5' };
  const need = BigInt(rent(SETTINGS_ACCOUNT_BYTES) + 2 * 10_000 + rent(0)) + 30_000_000n + 118_600_000n + 500_000_000n;
  assert.equal(await setupCost(setupConn(), C, base), need);
  assert.equal(await setupCost(setupConn(10e9, 5_000_000n), C, base), need + 5_000_000n, 'a creation fee the program charges');
  assert.equal(await setupCost(setupConn(10e9, 5_000_000n), C, { ...base, created: 0 }), need - BigInt(rent(SETTINGS_ACCOUNT_BYTES)), 'a resume creates no account, pays no fee');
  const usdcNeed = need - 500_000_000n + BigInt(rent(TOKEN_ACCOUNT_BYTES));
  assert.equal(await setupCost(setupConn(), C, { ...base, token: 'USDC', deposit: '25' }), usdcNeed);
  assert.equal(await setupCost(setupConn(), C, { ...base, deposit: null }), need - 500_000_000n, 'a funding on chain already');
  assert.equal(await setupCost(setupConn(Number(need)), C, base), need, 'exactly enough');
  await assert.rejects(setupCost(setupConn(Number(need) - 1), C, base), /the set-up needs .*\(the vault's and its rules' rent, the bot's gas 0\.1186, the deposit 0\.5, fees/);
  assert.match(await setupCost(setupConn(1), C, { ...base, token: 'USDC', deposit: '25' }).catch(e => e.message), /the bot's gas 0\.1186(?!, the deposit)/);
});

test('a deposit takes what the wallet holds, to the unit', async () => {
  const dep = (balance, usdc) => ({ ...setupConn(balance, 0n, usdc) });
  const sol = await depositTxs(dep(500_010_000), C, { owner: OWNER, vault: VAULT, token: 'SOL', amount: '0.5' });
  const tx = Transaction.from(Buffer.from(sol.transactions[0], 'base64'));
  assert.deepEqual(SystemInstruction.decodeTransfer(tx.instructions[0]), { fromPubkey: new PublicKey(OWNER), toPubkey: new PublicKey(VAULT), lamports: 500_000_000n });
  assert.equal(sol.summary, `deposit 0.5 SOL into your vault ${VAULT}`);
  await assert.rejects(depositTxs(dep(500_009_999), C, { owner: OWNER, vault: VAULT, token: 'SOL', amount: '0.5' }), /the deposit needs 0\.5 and a fee/);
  const usdc = await depositTxs(dep(10e9, 25_000_000n), C, { owner: OWNER, vault: VAULT, token: 'USDC', amount: '25' });
  assert.equal(Transaction.from(Buffer.from(usdc.transactions[0], 'base64')).instructions.length, 2, 'the vault\'s account opened, the transfer');
  await assert.rejects(depositTxs(dep(10e9, 24_999_999n), C, { owner: OWNER, vault: VAULT, token: 'USDC', amount: '25' }), /holds 24\.999999 USDC/);
  await assert.rejects(depositTxs(dep(10e9), C, { owner: OWNER, vault: VAULT, token: 'DOGE', amount: '1' }), /token DOGE/);
});

test('set-up state: done only with every step and the owner alone; a set-up key still on it is pending; planned again when the seed was taken', async () => {
  const key = Keypair.generate().publicKey.toBase58();
  const r = { owner: OWNER, profit_wallet: OWNER, fee_wallet: null, connected: { planned: { seed: '42', settings: sq.settingsPda(42n).toBase58(), setupKeys: [key] } } };
  assert.deepEqual(await setupState(conn, KEYS, C, r, chainStub({ created: true, made: 2 })), { state: 'done' });
  assert.deepEqual(await setupState(conn, KEYS, C, r, chainStub({ created: true, made: 0 })), { state: 'pending', missing: 2 });
  assert.deepEqual(await setupState(conn, KEYS, C, r, chainStub({ created: true, made: 2, others: [key] })), { state: 'pending', missing: 1 });
  const moved = { ...r, connected: { planned: { ...r.connected.planned, settings: sq.settingsPda(43n).toBase58() } } };
  assert.deepEqual(await setupState(conn, KEYS, C, moved, chainStub({ created: true, made: 2 })), { state: 'replan' });
  assert.deepEqual(await setupState(conn, KEYS, C, { ...r, connected: null }, chainStub()), { state: 'replan' });
  const stub = chainStub({ created: true, made: 2 });
  await setupState(conn, KEYS, C, r, stub);
  assert.equal(stub.calls[0][0].seed, '42');
  assert.equal(stub.calls[0][0]['setup-keys'], key, 'the request\'s set-up keys do not make its vault someone else\'s');
});

test('funding: the page\'s signature when it pays the gas, else the vault\'s recent transactions; never another\'s payment', async () => {
  const sig = () => bs58.encode(Keypair.generate().secretKey.subarray(0, 64));
  const good = sig(), bad = sig(), found = sig(), vault = sq.vaultPda(sq.settingsPda(42n)).toBase58();
  const pays = (n) => parsed(sol(OWNER, PAYER.toBase58(), n), sol(OWNER, vault, 500_000_000));
  const txs = { [good]: pays(118_600_000), [bad]: pays(1), [found]: pays(118_600_000) };
  const c = { getSignatureStatuses: async ([s]) => ({ value: [txs[s] ? OK : null] }),
    getParsedTransaction: async (s) => ({ ...txs[s], blockTime: 2_000_000_000 }) };
  const r = { owner: OWNER, payer: PAYER.toBase58(), gas: '0.1186', connected: { planned: { vault } } };
  const none = async () => null, finds = async () => found;
  const dep = { deposited: async (_c, cc, v, owner) => { assert.deepEqual([cc, v, owner], [C, vault, OWNER]); return 'DEP'; } };
  const run = (hint, find) => funding(c, C, r, { notBefore: 0, ...hint }, { find, ...dep });
  assert.deepEqual(await run({ payment: good }, none), { state: 'ok', payment: good, deposit: 'DEP' });
  assert.deepEqual(await run({ payment: bad }, finds), { state: 'ok', payment: found, deposit: 'DEP' });
  assert.equal((await run({ payment: bad }, none)).state, 'bad');
  assert.match((await run({ payment: bad }, none)).reason, /less than 0\.1186/);
  assert.deepEqual(await run({ payment: null }, finds), { state: 'ok', payment: found, deposit: 'DEP' });
  assert.equal((await run({ payment: null }, none)).state, 'pending');
  assert.equal((await run({ payment: sig() }, none)).state, 'pending', 'not on chain yet');
  assert.equal((await run({ payment: good, notBefore: 3_000_000_000 }, none)).state, 'bad', 'older than the request');
  // no planned vault: nothing to search, no deposit to read
  let asked = false;
  const lone = await funding(c, C, { ...r, connected: null }, { payment: good, notBefore: 0 }, { find: async () => { asked = true; return found; },
    deposited: async () => { asked = true; return 'DEP'; } });
  assert.deepEqual([asked, lone], [false, { state: 'ok', payment: good, deposit: null }]);
  assert.equal((await funding(c, C, { ...r, connected: null }, { payment: null, notBefore: 0 }, { find: finds })).state, 'pending');
  // the search: the newest transaction of the vault that pays the gas from the owner; failed ones skipped
  const list = [{ signature: good, err: { x: 1 } }, { signature: bad, err: null }, { signature: found, err: null }, { signature: good, err: null }];
  const sc = { ...c, getSignaturesForAddress: async (k, o) => { assert.equal(k.toBase58(), vault); assert.equal(o.limit, 20); return list; } };
  assert.equal(await findFunding(sc, vault, { owner: OWNER, payer: PAYER.toBase58(), amount: '0.1186', notBefore: 0 }), found);
  assert.equal(await findFunding({ ...sc, getSignaturesForAddress: async () => list.slice(0, 2) }, vault,
    { owner: OWNER, payer: PAYER.toBase58(), amount: '0.1186', notBefore: 0 }), null);
  assert.equal(await findFunding(sc, vault, { owner: OTHER, payer: PAYER.toBase58(), amount: '0.1186', notBefore: 0 }), null, 'another owner\'s');
  // the deposit: the newest transaction that moves the owner's money into the vault
  const dc = { ...sc, getAccountInfo: async () => ({ owner: spl.TOKEN_PROGRAM_ID }) };
  assert.equal(await findDeposit(dc, C, vault, OWNER), bad, 'the newest deposit (the gas it pays does not matter)');
  assert.equal(await findDeposit({ ...dc, getSignaturesForAddress: async () => list.slice(0, 1) }, C, vault, OWNER), null, 'a failed one is none');
  assert.equal(await findDeposit(dc, C, vault, OTHER), null, 'another owner\'s money is no deposit of this one');
});
