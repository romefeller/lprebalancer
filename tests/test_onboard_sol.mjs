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
  vaultVenues, SETTINGS_ACCOUNT_BYTES, POLICY_ACCOUNT_BYTES, TOKEN_ACCOUNT_BYTES } from '../onboard/solana.mjs';
import { toRaw } from '../onboard/units.mjs';

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

// --- the owner's own set-up, with the set-up reader stubbed ----------------------------------

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
const setupConn = (balance = 10e9, fee = 0n, usdc = 30_000_000n) => ({ ...conn, getBalance: async () => balance,
  getAccountInfo: async (k) => (k.equals(sq.programConfigPda()) ? { data: programConfig(fee) } : k.toBase58() === C.tokens.USDC.mint
    ? { owner: spl.TOKEN_PROGRAM_ID } : usdcAccount(usdc)) });
function setupStub({ steps = 3, done = [], settings = sq.settingsPda(42n).toBase58() } = {}) {
  const calls = [];
  const step = (i) => SystemProgram.transfer({ fromPubkey: new PublicKey(OWNER), toPubkey: new PublicKey(OTHER), lamports: i + 1 });
  return { calls, request: async (f, keys) => {
    calls.push([f, keys]);
    return { seed: f.seed ?? '42', settings, vault: sq.vaultPda(new PublicKey(settings)).toBase58(), report: { venues: {} }, done,
      steps: Array.from({ length: steps }, (_, i) => step(i)) };
  } };
}
const A = { owner: OWNER, profitWallet: OWNER, feeWallet: OTHER, token: 'SOL', deposit: '0.5', gas: '0.1186', planned: null };

test('set-up: the owner\'s transactions, the deposit and the gas with the last step; the bot\'s keys only named', async () => {
  const stub = setupStub();
  const out = await setupTxs(setupConn(), KEYS, C, A, stub);
  const [f, keys] = stub.calls[0];
  const venues = vaultVenues(C);
  assert.deepEqual(f, { owner: OWNER, venue: venues.join(','), pools: venues.map(v => C.pools[v]).join(','), payout: `${OWNER},${OTHER}`,
    'payout-mint': C.payoutMint });
  assert.deepEqual([keys.payer.toBase58(), keys.policyKey.toBase58()], [PAYER.toBase58(), KEYS.policyKey.publicKey.toBase58()]);
  assert.deepEqual(out.planned, { seed: '42', settings: sq.settingsPda(42n).toBase58(), vault: sq.vaultPda(sq.settingsPda(42n)).toBase58() });
  assert.equal(out.done, false);
  const txs = out.transactions.map(t => Transaction.from(Buffer.from(t, 'base64')));
  for (const t of txs) {
    assert.equal(t.feePayer.toBase58(), OWNER);
    assert.equal(t.recentBlockhash, BLOCKHASH);
    assert.deepEqual(t.signatures.map(s => s.publicKey.toBase58()), [OWNER], 'only the owner signs');
  }
  const ixs = txs.flatMap(t => t.instructions).map(ix => SystemInstruction.decodeTransfer(ix));
  assert.deepEqual(ixs.map(x => [x.toPubkey.toBase58(), x.lamports]), [[OTHER, 1n], [OTHER, 2n], [OTHER, 3n],
    [out.planned.vault, 500_000_000n], [PAYER.toBase58(), 118_600_000n]]);
  // the last step, the deposit and the gas: one transaction
  assert.deepEqual(txs.at(-1).instructions.slice(-3).map(ix => SystemInstruction.decodeTransfer(ix).lamports), [3n, 500_000_000n, 118_600_000n]);
  // a resume: the planned seed asked for; nothing missing is done, without transactions
  const again = setupStub({ steps: 0, done: [0, 1, 2] });
  const done = await setupTxs(setupConn(), KEYS, C, { ...A, planned: { seed: '42' } }, again);
  assert.equal(again.calls[0][0].seed, '42');
  assert.deepEqual([done.done, done.transactions], [true, []]);
  // the operator's own wallet: no fee wallet among the payouts
  const own = setupStub();
  await setupTxs(setupConn(), KEYS, C, { ...A, feeWallet: null }, own);
  assert.equal(own.calls[0][0].payout, OWNER);
  // a wallet that cannot pay all of it is refused before anything is built for it
  await assert.rejects(setupTxs(setupConn(600_000_000), KEYS, C, A, setupStub()), /your wallet holds 0\.6 SOL; the set-up needs/);
  // a token deposit: the vault's account opens before the last step; the token transfer goes with it
  const tok = await setupTxs(setupConn(), KEYS, C, { ...A, token: 'USDC', deposit: '25' }, setupStub());
  const all = tok.transactions.map(t => Transaction.from(Buffer.from(t, 'base64'))).flatMap(t => t.instructions);
  const kinds = all.map(ix => (ix.programId.equals(SystemProgram.programId) ? `sol ${SystemInstruction.decodeTransfer(ix).lamports}`
    : ix.programId.equals(spl.ASSOCIATED_TOKEN_PROGRAM_ID) ? 'open' : `token ${ix.data[0]}`));
  assert.deepEqual(kinds, ['sol 1', 'sol 2', 'open', 'sol 3', 'token 12', 'sol 118600000'], 'the opening before the last step; the money with it');
  await assert.rejects(setupTxs(setupConn(10e9, 0n, 24_999_999n), KEYS, C, { ...A, token: 'USDC', deposit: '25' }, setupStub()),
    /your wallet holds 24\.999999 USDC; the deposit is 25/);
});

test('set-up cost: the creation fee, the rent of what it creates, the fees, the gas, a SOL deposit, the wallet\'s floor', async () => {
  const rent = (n) => 890_880 + 6960 * n;
  const base = { owner: OWNER, created: 1, policies: 2, txs: 2, gas: 118_600_000n, token: 'SOL', deposit: '0.5' };
  const need = BigInt(rent(SETTINGS_ACCOUNT_BYTES) + 2 * rent(POLICY_ACCOUNT_BYTES) + 2 * 10_000 + rent(0)) + 118_600_000n + 500_000_000n;
  assert.equal(await setupCost(setupConn(), C, base), need);
  assert.equal(await setupCost(setupConn(10e9, 5_000_000n), C, base), need + 5_000_000n, 'a creation fee the program charges');
  assert.equal(await setupCost(setupConn(10e9, 5_000_000n), C, { ...base, created: 0 }), need - BigInt(rent(SETTINGS_ACCOUNT_BYTES)), 'a resume creates no account, pays no fee');
  const usdcNeed = need - 500_000_000n + BigInt(rent(TOKEN_ACCOUNT_BYTES));
  assert.equal(await setupCost(setupConn(), C, { ...base, token: 'USDC', deposit: '25' }), usdcNeed);
  assert.equal(await setupCost(setupConn(Number(need)), C, base), need, 'exactly enough');
  await assert.rejects(setupCost(setupConn(Number(need) - 1), C, base), /the set-up needs .*\(the vault's and its rules' rent, the bot's gas 0\.1186, the deposit 0\.5, fees/);
  assert.match(await setupCost(setupConn(1), C, { ...base, token: 'USDC', deposit: '25' }).catch(e => e.message), /the bot's gas 0\.1186(?!, the deposit)/);
});

test('set-up: a resume creates no account and pays no fee; a deposit takes what the wallet holds, to the unit', async () => {
  const rent = (n) => 890_880 + 6960 * n;
  // the vault exists, one policy is missing: its rent, one fee, the gas, the deposit, the floor
  const need = rent(POLICY_ACCOUNT_BYTES) + 10_000 + 118_600_000 + 500_000_000 + rent(0);
  const resumed = setupStub({ steps: 1, done: [0, 1] });
  const out = await setupTxs(setupConn(need, 9_000_000n), KEYS, C, { ...A, planned: { seed: '42' } }, resumed);
  assert.equal(out.cost, I.fromRaw(BigInt(need), 9));
  await assert.rejects(setupTxs(setupConn(need - 1), KEYS, C, { ...A, planned: { seed: '42' } }, setupStub({ steps: 1, done: [0, 1] })), /the set-up needs/);
  // a deposit alone (Add capital): SOL with a fee beside it, a token to the unit
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

test('set-up state: done, pending with what is missing, or planned again when the seed was taken', async () => {
  const r = { owner: OWNER, profit_wallet: OWNER, fee_wallet: null, connected: { planned: { seed: '42', settings: sq.settingsPda(42n).toBase58() } } };
  assert.deepEqual(await setupState(conn, KEYS, C, r, setupStub({ steps: 0 })), { state: 'done' });
  assert.deepEqual(await setupState(conn, KEYS, C, r, setupStub({ steps: 2 })), { state: 'pending', missing: 2 });
  assert.deepEqual(await setupState(conn, KEYS, C, r, setupStub({ steps: 0, settings: sq.settingsPda(43n).toBase58() })), { state: 'replan' });
  assert.deepEqual(await setupState(conn, KEYS, C, { ...r, connected: null }, setupStub()), { state: 'replan' });
  const stub = setupStub({ steps: 0 });
  await setupState(conn, KEYS, C, r, stub);
  assert.equal(stub.calls[0][0].seed, '42');
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
