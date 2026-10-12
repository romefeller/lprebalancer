// onboard/: what a request must be (fields, signature, nonce, expiry, invite),
// the rate limits, and the server's refusals, with the chains and the database
// replaced by fakes. The real chains: test_onboard_fork.mjs and
// test_onboard_polygon_fork.mjs; the real database: test_onboard_store.py.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import fc from 'fast-check';
import { privateKeyToAccount } from 'viem/accounts';
import * as I from '../onboard/intent.mjs';
import { StoreError, Windows, clientOf, loadConfig, makeServer, profileVenues, serviceCommand } from '../onboard/server.mjs';
import { resolve as resolveSolana, vaultVenues, paymentVerdict as solVerdict, paymentTxs as solPaymentTxs, gas as solGas, holdings as solHoldings } from '../onboard/solana.mjs';
import { paymentVerdict as evmVerdict, paymentTxs as evmPaymentTxs, gas as evmGas, holdings as evmHoldings } from '../onboard/polygon.mjs';

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Transaction } = require('@solana/web3.js');
const bs58 = require('bs58').default;
const spl = require('@solana/spl-token');

const CFG = loadConfig();
const SOL = CFG.chains.solana, POLY = CFG.chains.polygon;
const OPERATOR_SOL = SOL.operatorWallet, OPERATOR_POL = POLY.operatorWallet;
const EVM_OWNER = privateKeyToAccount('0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d');
const EVM_OTHER = privateKeyToAccount('0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a');
const INVITE = 'correct horse battery staple';
const HASH = crypto.createHash('sha256').update(INVITE).digest('hex');

// An ed25519 signature by a web3.js Keypair, with node's crypto.
function solSign(key, text) {
  const pk = crypto.createPrivateKey({ format: 'jwk', key: { kty: 'OKP', crv: 'Ed25519',
    d: Buffer.from(key.secretKey.subarray(0, 32)).toString('base64url'), x: Buffer.from(key.publicKey.toBytes()).toString('base64url') } });
  return crypto.sign(null, Buffer.from(text, 'utf8'), pk);
}

const body = (over = {}) => ({ chain: 'solana', token: 'SOL', amount: '1.5', name: 'my-pool',
  owner: OWNER.publicKey.toBase58(), profitWallet: PROFIT.publicKey.toBase58(), ...over });
const OWNER = Keypair.generate(), PROFIT = Keypair.generate(), BOT = Keypair.generate(), OWNER2 = Keypair.generate();

// --- fields -----------------------------------------------------------------------

test('fields: a good request, in canonical form', () => {
  const f = I.fields(body({ amount: '01.50' }), CFG);
  assert.deepEqual(f, { chain: 'solana', owner: OWNER.publicKey.toBase58(), profitWallet: PROFIT.publicKey.toBase58(),
    token: 'SOL', amount: '1.5', deposit: '1.355', gas: '0.145', gasToken: 'SOL', name: 'my-pool', special: false,
    feeBps: CFG.fee.bps, feeWallet: OPERATOR_SOL });
  const u = I.fields(body({ token: 'USDC', amount: '10' }), CFG);
  assert.deepEqual([u.amount, u.deposit, u.gas, u.gasToken], ['10', '10', '0.145', 'SOL'], 'a token deposit pays the gas in SOL beside it');
  const e = I.fields(body({ chain: 'polygon', token: 'USDT0', amount: '25', owner: EVM_OWNER.address.toLowerCase(), profitWallet: EVM_OWNER.address }), CFG);
  assert.equal(e.owner, EVM_OWNER.address, 'an EVM address in its checksum spelling');
  assert.equal(e.feeWallet, OPERATOR_POL);
});

test('fields: every refusal', () => {
  const pda = PublicKey.findProgramAddressSync([Buffer.from('x')], BOT.publicKey)[0].toBase58();
  const cases = [
    [{ chain: 'ethereum' }, /chain/], [{ chain: undefined }, /chain/], [{ token: 'BTC' }, /token/],
    [{ owner: 'nope' }, /owner: not a Solana address/], [{ owner: pda }, /owner: not a wallet/],
    [{ profitWallet: pda }, /profit wallet: not a wallet/], [{ profitWallet: `${PROFIT.publicKey.toBase58()}\n` }, null],
    [{ profitWallet: '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142' }, /profit wallet/],
    [{ amount: '0' }, /more than zero/], [{ amount: '-1' }, /a number/], [{ amount: '1e3' }, /a number/],
    [{ amount: '0.0000000001' }, /at most 9 decimals/], [{ amount: '0.164999999' }, /at least 0.165/], [{ amount: '0.165' }, null],
    [{ amount: '51' }, /at most 50/], [{ token: 'USDC', amount: '9.99' }, /at least 10$/],
    [{ amount: '' }, /a number/], [{ amount: ' 1 ' }, null],
    [{ amount: '0,18' }, null], [{ amount: '1,000.5' }, /a number/], [{ amount: '1.000,5' }, /a number/], [{ amount: '0,1,8' }, /a number/], [{ amount: ',5' }, /a number/], [{ name: 'My-Pool' }, /name/], [{ name: 'ab' }, /name/],
    [{ name: 'a--b' }, /name/], [{ name: 'pool-' }, /name/], [{ name: '1pool' }, /name/], [{ name: 'a'.repeat(33) }, /name/],
    [{ name: 'sol-usdc' }, /reserved/], [{ name: 'poly-wpol-usdt' }, /reserved/], [{ name: 'sol-lp' }, /reserved/],
    [{ name: 'my pool' }, /name/], [{ name: 'pool;rm' }, /name/],
    [{ chain: 'polygon', token: 'USDT0', amount: '20', owner: '0x123', profitWallet: EVM_OWNER.address }, /owner: not an address/],
  ];
  for (const [over, re] of cases) {
    if (re === null) { assert.doesNotThrow(() => I.fields(body(over), CFG), JSON.stringify(over)); continue; }
    assert.throws(() => I.fields(body(over), CFG), (e) => e instanceof I.Invalid && re.test(e.message), JSON.stringify(over));
  }
  assert.throws(() => I.fields(body({ owner: BOT.publicKey.toBase58() }), CFG, [BOT.publicKey.toBase58()]), /bot's own key/);
  assert.throws(() => I.fields(body({ profitWallet: BOT.publicKey.toBase58() }), CFG, [BOT.publicKey.toBase58()]), /bot's own key/);
  assert.throws(() => I.fields(body({ chain: 'polygon', token: 'WPOL', amount: '30', owner: EVM_OWNER.address,
    profitWallet: EVM_OWNER.address.toLowerCase() }), CFG, [EVM_OWNER.address]), /bot's own key/);
  assert.throws(() => I.fields(null, CFG), /chain/);
  // only JSON strings, and only the config's own chains and tokens
  for (const [over, re] of [[{ amount: 1.5 }, /a number like/], [{ chain: '__proto__' }, /chain/], [{ chain: 'constructor' }, /chain/],
    [{ token: 'constructor' }, /token/], [{ token: '__proto__' }, /token/], [{ owner: 12345 }, /owner/], [{ name: ['my-pool'] }, /name/],
    [{ profitWallet: { a: 1 } }, /profit wallet/]]) {
    assert.throws(() => I.fields(body(over), CFG), (e) => e instanceof I.Invalid && re.test(e.message), JSON.stringify(over));
  }
  assert.equal(I.own({ a: 1 }, 'a'), 1);
  assert.equal(I.own({ a: 1 }, 'toString'), undefined);
  assert.equal(I.own(undefined, 'a'), undefined);
});

test('amount: exact decimal arithmetic, any input', () => {
  fc.assert(fc.property(fc.bigInt({ min: 1n, max: 10n ** 20n }), fc.integer({ min: 0, max: 18 }), (raw, d) => {
    assert.equal(I.toRaw(I.fromRaw(raw, d), d), raw);
  }));
  const t = { decimals: 6, min: '10', max: '10000' };
  fc.assert(fc.property(fc.string(), (s) => {
    try { const v = I.amount(s, t); assert.match(v, /^\d+(\.\d{1,6})?$/); assert.ok(Number(v) >= 10 && Number(v) <= 10000); } catch (e) { assert.ok(e instanceof I.Invalid); }
  }));
  assert.equal(I.amount('10', t), '10');
  assert.equal(I.amount('10000.000000', t), '10000');
  assert.throws(() => I.amount('10000.000001', t), /at most/);
  assert.throws(() => I.amount('9.999999', t), /at least/);
});

test('fee terms: the operator\'s own wallet is special, any other pays the configured share', () => {
  assert.deepEqual(I.feeTerms('solana', OPERATOR_SOL, OPERATOR_SOL, 1000), { special: true, feeBps: 0, feeWallet: null });
  assert.deepEqual(I.feeTerms('solana', PROFIT.publicKey.toBase58(), OPERATOR_SOL, 1000), { special: false, feeBps: 1000, feeWallet: OPERATOR_SOL });
  assert.deepEqual(I.feeTerms('polygon', OPERATOR_POL.toLowerCase(), OPERATOR_POL, 1000), { special: true, feeBps: 0, feeWallet: null });
  assert.deepEqual(I.feeTerms('polygon', EVM_OWNER.address, OPERATOR_POL, 700), { special: false, feeBps: 700, feeWallet: OPERATOR_POL });
  assert.equal(I.fields(body({ profitWallet: OPERATOR_SOL }), CFG).special, true);
  // a Solana address differs by case: never folded
  assert.equal(I.sameAddress('solana', 'Abc', 'abc'), false);
});

test('gas: the configured setup, margin and reserve; the minimum covers it', () => {
  assert.equal(I.fromRaw(I.gasTotal(SOL), 9), '0.145');
  assert.equal(I.fromRaw(I.gasTotal(POLY), 18), '4');
  assert.equal(I.minimum(SOL, 'SOL'), '0.165');
  assert.equal(I.minimum(SOL, 'USDC'), '10');
  assert.equal(I.minimum(POLY, 'POL'), '24');
  assert.equal(I.minimum(POLY, 'WPOL'), '20', 'wrapped: the gas is paid in POL beside it');
  assert.deepEqual(I.nativeOf(POLY)[0], 'POL');
  assert.throws(() => I.nativeOf({ label: 'x', tokens: { A: {}, B: {} } }), /exactly one native/);
  assert.throws(() => I.nativeOf({ label: 'x', tokens: { A: { native: true }, B: { native: true } } }), /exactly one native/);
  fc.assert(fc.property(fc.integer({ min: 195, max: 50_000 }), (milli) => {
    const f = I.fields(body({ amount: I.fromRaw(BigInt(milli), 3) }), CFG);
    assert.equal(I.toRaw(f.deposit, 9) + I.gasTotal(SOL), I.toRaw(f.amount, 9), 'deposit + gas = amount, exactly');
    assert.ok(I.toRaw(f.deposit, 9) >= I.toRaw(SOL.tokens.SOL.min, 9));
  }));
});

// --- Max: the most a wallet can put in ---------------------------------------------------

const SOL_FEES = 1_000_000n, USDC_FEES = 3_000_000n, GAS = 145_000_000n;      // onboard.json, in lamports
const E18 = 10n ** 18n;

test('max (native): the balance less the owner\'s fees, exact to the unit, capped at the max', () => {
  assert.deepEqual([SOL.tokens.SOL.fees, SOL.tokens.USDC.fees], ['0.001', '0.003'], 'the config this test reads');
  const max = (lamports) => I.maxAmount(SOL, 'SOL', { SOL: lamports });
  assert.deepEqual(max(2_000_000_000n), { amount: '1.999' });
  assert.deepEqual(max(1_234_567_891n), { amount: '1.233567891' }, 'every lamport, no float rounding');
  assert.deepEqual(max(50_001_000_000n), { amount: '50' }, 'at the max: the max');
  assert.deepEqual(max(50_000_999_999n), { amount: '49.999999999' }, 'one lamport under it');
  assert.deepEqual(max(10n ** 15n), { amount: '50' });
  assert.deepEqual(max(165_000_000n + SOL_FEES), { amount: '0.165' }, 'exactly the minimum and the fees');
  assert.deepEqual(max(165_000_000n + SOL_FEES - 1n), { short: 'not enough: you have 0.165999999 SOL, the minimum is 0.166 SOL with your fees' });
  assert.deepEqual(max(0n), { short: 'not enough: you have 0 SOL, the minimum is 0.166 SOL with your fees' });
  assert.deepEqual(max(500n), { short: 'not enough: you have 0.0000005 SOL, the minimum is 0.166 SOL with your fees' }, 'less than the fees');
  const pol = (wei) => I.maxAmount(POLY, 'POL', { POL: wei });
  assert.deepEqual(pol(88n * E18 + 123n), { amount: '87.900000000000000123' }, '18 decimals, a point');
  assert.deepEqual(pol(24n * E18 + E18 / 10n), { amount: '24' });
  assert.match(pol(24n * E18).short, /you have 24 POL, the minimum is 24.1 POL with your fees/);
  assert.deepEqual(pol(200_000n * E18), { amount: '100000' });
});

test('max (token): the whole balance, capped; the native coin must hold the gas and the fees beside it', () => {
  const enough = GAS + USDC_FEES;
  const max = (usdc, lamports = enough) => I.maxAmount(SOL, 'USDC', { USDC: usdc, SOL: lamports });
  assert.deepEqual(max(123_456_789n), { amount: '123.456789' });
  assert.deepEqual(max(10_000_000n), { amount: '10' }, 'exactly the minimum: no gas comes out of a token');
  assert.deepEqual(max(9_999_999n), { short: 'not enough: you have 9.999999 USDC, the minimum is 10 USDC' });
  assert.deepEqual(max(0n), { short: 'not enough: you have 0 USDC, the minimum is 10 USDC' });
  assert.deepEqual(max(20_000_000_000n), { amount: '10000' });
  assert.deepEqual(max(10_000_000_001n), { amount: '10000' });
  assert.deepEqual(max(9_999_999_999n), { amount: '9999.999999' });
  assert.deepEqual(max(50_000_000n, enough - 1n),
    { short: 'not enough SOL for the bot\'s gas and your fees: you have 0.147999999 SOL, you need 0.148 SOL' });
  assert.deepEqual(max(50_000_000n, 0n), { short: 'not enough SOL for the bot\'s gas and your fees: you have 0 SOL, you need 0.148 SOL' });
  assert.deepEqual(I.maxAmount(POLY, 'USDT0', { USDT0: 25_000_000n, POL: 4n * E18 + E18 / 20n }), { amount: '25' });
  assert.match(I.maxAmount(POLY, 'WPOL', { WPOL: 30n * E18, POL: 4n * E18 }).short, /not enough POL .* you have 4 POL, you need 4.05 POL/);
  assert.deepEqual(I.maxAmount(POLY, 'WPOL', { WPOL: 30n * E18 + 7n, POL: 5n * E18 }), { amount: '30.000000000000000007' });
});

test('max: whatever the wallet holds, Max is an amount the server signs, and no more than the wallet can pay', () => {
  for (const [c, chain, token, owner] of [[SOL, 'solana', 'SOL', OWNER.publicKey.toBase58()], [SOL, 'solana', 'USDC', OWNER.publicKey.toBase58()],
    [POLY, 'polygon', 'POL', EVM_OWNER.address], [POLY, 'polygon', 'WPOL', EVM_OWNER.address], [POLY, 'polygon', 'USDT0', EVM_OWNER.address]]) {
    const t = c.tokens[token], [native, n] = I.nativeOf(c);
    const scale = 10n ** BigInt(t.decimals), nscale = 10n ** BigInt(n.decimals);
    fc.assert(fc.property(fc.bigInt({ min: 0n, max: 200_000n * scale }), fc.bigInt({ min: 0n, max: 10n * nscale }), (have, gasCoin) => {
      const held = t.native ? { [token]: have } : { [token]: have, [native]: gasCoin };
      const m = I.maxAmount(c, token, held);
      assert.equal(Object.keys(m).length, 1);
      if (m.short !== undefined) {
        assert.match(m.short, /^not enough/);
        return;
      }
      const raw = I.toRaw(m.amount, t.decimals);
      const f = I.fields({ chain, token, amount: m.amount, owner, profitWallet: owner, name: 'my-pool' }, CFG);
      assert.equal(f.amount, m.amount, 'the server takes it as it is');
      const fees = I.toRaw(t.fees, n.decimals);
      if (t.native) assert.ok(raw + fees <= have && (raw === I.toRaw(t.max, t.decimals) || raw + fees === have));
      else assert.ok(raw <= have && (raw === I.toRaw(t.max, t.decimals) || raw === have) && gasCoin >= I.gasTotal(c) + fees);
    }), { numRuns: 300 });
  }
});

test('max: every token of the config names its fees, or the server does not start', () => {
  for (const c of Object.values(CFG.chains)) for (const [s, t] of Object.entries(c.tokens)) assert.match(t.fees, /^\d+(\.\d+)?$/, s);
  const raw = JSON.parse(fs.readFileSync(new URL('../onboard/onboard.json', import.meta.url), 'utf8'));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'onboard-cfg-'));
  try {
    for (const fees of [undefined, '', '1e-3', '-0.1', 0.001]) {
      const bad = structuredClone(raw);
      bad.chains.polygon.tokens.WPOL.fees = fees;
      fs.writeFileSync(path.join(dir, 'c.json'), JSON.stringify(bad));
      assert.throws(() => loadConfig(path.join(dir, 'c.json')), /chains\.polygon\.tokens\.WPOL\.fees: a decimal/, String(fees));
    }
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('max: the balances it reads, the native coin and the token\'s own account', async () => {
  const owner = OWNER.publicKey, usdc = new PublicKey(SOL.tokens.USDC.mint);
  const ata = spl.getAssociatedTokenAddressSync(usdc, owner);
  const other = spl.getAssociatedTokenAddressSync(usdc, PROFIT.publicKey);
  const account = (amount, mintOwner = owner) => {
    const data = Buffer.alloc(spl.AccountLayout.span);
    spl.AccountLayout.encode({ mint: usdc, owner: mintOwner, amount, delegateOption: 0, delegate: PublicKey.default, state: 1, isNativeOption: 0,
      isNative: 0n, delegatedAmount: 0n, closeAuthorityOption: 0, closeAuthority: PublicKey.default }, data);
    return { data, owner: spl.TOKEN_PROGRAM_ID, lamports: 1, executable: false };
  };
  const asked = [];
  const conn = (lamports, usdcRaw) => ({ getBalance: async (k) => { asked.push(k.toBase58()); return Number(lamports); },
    getAccountInfo: async (k) => k.equals(usdc) ? { owner: spl.TOKEN_PROGRAM_ID, data: Buffer.alloc(82) }
      : k.equals(ata) && usdcRaw !== null ? account(usdcRaw) : k.equals(other) ? account(999_000_000n, PROFIT.publicKey) : null });
  assert.deepEqual(await solHoldings(conn(2_000_000_000n, 5n), SOL, owner.toBase58(), 'SOL'), { SOL: 2_000_000_000n });
  assert.deepEqual(await solHoldings(conn(7n, 12_345_678n), SOL, owner.toBase58(), 'USDC'), { SOL: 7n, USDC: 12_345_678n });
  assert.deepEqual(await solHoldings(conn(7n, null), SOL, owner.toBase58(), 'USDC'), { SOL: 7n, USDC: 0n }, 'no token account: none');
  assert.deepEqual(asked, Array(3).fill(owner.toBase58()), 'the owner\'s balance, nobody else\'s');
  const reads = [];
  const keys = (pol, tok) => ({ pub: { getBalance: async (a) => { reads.push(['POL', a.address]); return pol; },
    readContract: async (a) => { reads.push([a.address, ...a.args]); return tok; } } });
  assert.deepEqual(await evmHoldings(keys(5n, 9n), POLY, EVM_OWNER.address, 'POL'), { POL: 5n });
  assert.deepEqual(await evmHoldings(keys(5n, 9n), POLY, EVM_OWNER.address, 'USDT0'), { POL: 5n, USDT0: 9n });
  assert.deepEqual(reads, [['POL', EVM_OWNER.address], ['POL', EVM_OWNER.address], [POLY.tokens.USDT0.address, EVM_OWNER.address]]);
});

// --- the gas payment ---------------------------------------------------------------------

const PAYER = BOT.publicKey.toBase58();
const xfer = (source, destination, lamports, program = 'system', type = 'transfer') => ({ program, parsed: { type, info: { source, destination, lamports } } });
const parsedTx = (ixs, { blockTime = 2000, err = null } = {}) => ({ blockTime, meta: { err }, transaction: { message: { instructions: ixs } } });
const OK = { confirmationStatus: 'confirmed', err: null };

test('payment (Solana): from the owner, to the bot\'s key, enough, confirmed, newer than the request', () => {
  const own = OWNER.publicKey.toBase58(), other = PROFIT.publicKey.toBase58();
  const ex = { owner: own, payer: PAYER, lamports: 145_000_000n, notBefore: 1000 };
  const v = (status, parsed) => solVerdict(status, parsed, ex);
  assert.deepEqual(v(OK, parsedTx([xfer(own, PAYER, 145_000_000)])), { state: 'ok' });
  assert.deepEqual(v({ ...OK, confirmationStatus: 'finalized' }, parsedTx([xfer(own, PAYER, 100_000_000), xfer(own, PAYER, 45_000_000)])), { state: 'ok' }, 'two transfers add up');
  const cases = [
    [null, parsedTx([xfer(own, PAYER, 145_000_000)]), 'pending', /not on chain/],                       // missing
    [{ ...OK, confirmationStatus: 'processed' }, parsedTx([xfer(own, PAYER, 145_000_000)]), 'pending', /not confirmed/],
    [{ ...OK, confirmationStatus: undefined }, parsedTx([xfer(own, PAYER, 145_000_000)]), 'pending', /not confirmed/],
    [OK, null, 'pending', /not readable/],
    [{ ...OK, err: { x: 1 } }, parsedTx([xfer(own, PAYER, 145_000_000)]), 'bad', /failed on chain/],
    [OK, parsedTx([xfer(own, PAYER, 145_000_000)], { err: { x: 1 } }), 'bad', /failed on chain/],
    [OK, parsedTx([xfer(own, PAYER, 144_999_999)]), 'bad', /less than 0.145/],                          // too small
    [OK, parsedTx([xfer(other, PAYER, 145_000_000)]), 'bad', /not from the owner/],                      // another sender
    [OK, parsedTx([xfer(own, other, 145_000_000)]), 'bad', /does not go to the bot's key/],             // another address
    [OK, parsedTx([xfer(own, other, 145_000_000), xfer(other, PAYER, 145_000_000)]), 'bad', /does not go/],
    [OK, parsedTx([xfer(own, PAYER, 145_000_000, 'spl-token')]), 'bad', /not from the owner/],
    [OK, parsedTx([xfer(own, PAYER, 145_000_000, 'system', 'createAccount')]), 'bad', /not from the owner/],
    [OK, parsedTx([xfer(own, PAYER, 145_000_000)], { blockTime: 999 }), 'bad', /older than this request/],
    [OK, parsedTx([xfer(own, PAYER, 145_000_000)], { blockTime: null }), 'bad', /older than this request/],
    [OK, parsedTx([]), 'bad', /not from the owner/],
  ];
  for (const [status, parsed, state, re] of cases) {
    const got = v(status, parsed);
    assert.equal(got.state, state, JSON.stringify([status, parsed]));
    assert.match(got.reason, re);
  }
  assert.equal(v(OK, parsedTx([xfer(own, PAYER, 145_000_000)], { blockTime: 1000 })).state, 'ok', 'at the bound');
});

test('payment (EVM): from the owner, to the bot\'s key, enough, confirmed, newer than the request', () => {
  const ex = { owner: EVM_OWNER.address, payer: EVM_OTHER.address, wei: 4n * 10n ** 18n, notBefore: 1000, confirmations: 5 };
  const good = { receipt: { status: 'success', blockNumber: 100n }, tx: { from: EVM_OWNER.address.toLowerCase(), to: EVM_OTHER.address, value: 4n * 10n ** 18n },
    block: { timestamp: 1000n }, head: 104n };
  assert.deepEqual(evmVerdict(good, ex), { state: 'ok' });
  const cases = [
    [{ receipt: null, tx: null }, 'pending', /not on chain/],
    [{ tx: null }, 'pending', /not on chain/],
    [{ head: 103n }, 'pending', /not confirmed/],
    [{ receipt: { status: 'reverted', blockNumber: 100n } }, 'bad', /failed on chain/],
    [{ tx: { ...good.tx, value: 4n * 10n ** 18n - 1n } }, 'bad', /less than 4/],
    [{ tx: { ...good.tx, from: EVM_OTHER.address } }, 'bad', /not from the owner/],
    [{ tx: { ...good.tx, to: EVM_OWNER.address } }, 'bad', /does not go to the bot's key/],
    [{ tx: { ...good.tx, to: null } }, 'bad', /does not go/],
    [{ block: { timestamp: 999n } }, 'bad', /older/],
  ];
  for (const [over, state, re] of cases) {
    const got = evmVerdict({ ...good, ...over }, ex);
    assert.equal(got.state, state, JSON.stringify(over, (k, x) => typeof x === 'bigint' ? String(x) : x));
    assert.match(got.reason, re);
  }
});

test('the gas payment (Solana): the wallet must cover the gas, the deposit and two fees, or nothing is held', async () => {
  const owner = OWNER.publicKey, usdc = new PublicKey(SOL.tokens.USDC.mint);
  const ata = spl.getAssociatedTokenAddressSync(usdc, owner);
  const account = (amount) => {
    const data = Buffer.alloc(spl.AccountLayout.span);
    spl.AccountLayout.encode({ mint: usdc, owner, amount, delegateOption: 0, delegate: PublicKey.default, state: 1, isNativeOption: 0,
      isNative: 0n, delegatedAmount: 0n, closeAuthorityOption: 0, closeAuthority: PublicKey.default }, data);
    return { data, owner: spl.TOKEN_PROGRAM_ID, lamports: 1, executable: false };
  };
  const conn = (lamports, usdcRaw) => ({ getBalance: async () => Number(lamports),
    getAccountInfo: async (k) => k.equals(usdc) ? { owner: spl.TOKEN_PROGRAM_ID, data: Buffer.alloc(82) } : k.equals(ata) && usdcRaw !== null ? account(usdcRaw) : null,
    getLatestBlockhash: async () => ({ blockhash: PublicKey.default.toBase58() }) });
  const pay = (c, token, deposit) => solPaymentTxs(c, SOL, { owner: owner.toBase58(), payer: PAYER, gas: '0.145', token, deposit });
  const need = 145_000_000n + 1_355_000_000n + 20_000n;
  const ok = await pay(conn(need, null), 'SOL', '1.355');
  const tx = Transaction.from(Buffer.from(ok.transactions[0], 'base64'));
  assert.equal(tx.instructions.length, 1);
  assert.deepEqual([tx.instructions[0].keys[0].pubkey.toBase58(), tx.instructions[0].keys[1].pubkey.toBase58(),
    tx.instructions[0].data.readBigUInt64LE(4)], [owner.toBase58(), PAYER, 145_000_000n], 'one transfer of the gas, to the payer');
  await assert.rejects(pay(conn(need - 1n, null), 'SOL', '1.355'), /holds 1.500019999 SOL; the gas and the deposit need 1.50002/);
  await assert.rejects(pay(conn(145_020_000n + 1_000n, null), 'SOL', '1.355'), /need/, 'the deposit counts');
  assert.ok(await pay(conn(145_020_000n, 10_000_000n), 'USDC', '10'), 'a token deposit: the gas in SOL, the deposit in the token, exactly');
  await assert.rejects(pay(conn(145_020_000n, 9_999_999n), 'USDC', '10'), /holds 9.999999 USDC; the deposit is 10/);
  await assert.rejects(pay(conn(145_020_000n, null), 'USDC', '10'), /holds 0 USDC/);
  await assert.rejects(pay(conn(145_019_999n, 10_000_000n), 'USDC', '10'), /SOL/);
});

test('the bot\'s key: one setup and its margin, exactly', async () => {
  const need = 10n ** 18n;                                     // Polygon: 0.35 + 0.65
  const k = (wei) => ({ account: { address: EVM_OTHER.address }, pub: { getBalance: async () => wei } });
  assert.deepEqual(await evmGas(k(need), POLY), { ok: true, have: '1', need: '1', symbol: 'POL', key: EVM_OTHER.address });
  assert.equal((await evmGas(k(need - 1n), POLY)).ok, false);
  const sk = { payer: BOT };
  const sc = (l) => ({ getBalance: async () => l });
  assert.equal((await solGas(sc(45_000_000), sk, SOL)).ok, true, '0.0329 + 0.0121');
  assert.equal((await solGas(sc(44_999_999), sk, SOL)).ok, false);
});

test('the gas payment (EVM): the wallet must cover the gas, a native deposit and fees', async () => {
  const keys = (pol, usdt) => ({ pub: { getBalance: async () => pol, readContract: async () => usdt } });
  const pay = (k, token, deposit) => evmPaymentTxs(k, POLY, { owner: EVM_OWNER.address, payer: EVM_OTHER.address, gas: '4', token, deposit });
  const ok = await pay(keys(30n * 10n ** 18n + 1n, 0n), 'POL', '26');
  assert.deepEqual(ok.transactions, [{ to: EVM_OTHER.address, data: '0x', value: `0x${(4n * 10n ** 18n).toString(16)}` }]);
  assert.equal(ok.chainId, 137);
  await assert.rejects(pay(keys(30n * 10n ** 18n, 0n), 'POL', '26'), /and fees/, 'exactly gas + deposit leaves nothing for fees');
  await assert.rejects(pay(keys(5n * 10n ** 18n, 0n), 'POL', '26'), /need 30/, 'the deposit counts');
  assert.ok(await pay(keys(4n * 10n ** 18n + 1n, 25_000_000n), 'USDT0', '25'), 'a token deposit, exactly');
  await assert.rejects(pay(keys(4n * 10n ** 18n + 1n, 24_999_999n), 'USDT0', '25'), /holds 24.999999 USDT0/);
});

test('the message names every value the server acts on', () => {
  const f = I.fields(body(), CFG);
  const m = I.message(f, SOL, { site: 'romefeller.app/bot', nonce: 'ab'.repeat(16), expires: Date.UTC(2026, 9, 11, 12), payer: 'PAYERKEY' });
  for (const s of ['Chain: Solana', 'Pool: SOL/USDC (the strategy of sol-usdc)', 'Amount: 1.5 SOL',
    'Gas to the bot: 0.145 SOL, paid now to the bot\'s key PAYERKEY', 'Deposit into the vault: 1.355 SOL', `Profit wallet: ${f.profitWallet}`,
    'Name: my-pool', `Owner: ${f.owner}`, `Fee share: 10% of the USDC fees, paid to ${OPERATOR_SOL}`, `Nonce: ${'ab'.repeat(16)}`,
    'Expires: 2026-10-11T12:00:00.000Z']) assert.ok(m.includes(s), s);
  assert.match(I.message({ ...f, special: true, feeBps: 0, feeWallet: null }, SOL, { site: 's', nonce: 'n', expires: 0 }), /Fee share: none/);
});

// --- signatures -----------------------------------------------------------------------

test('verify (Solana): the owner\'s signature only, over the exact text', async () => {
  const text = 'hello\nNonce: 1';
  const sig = solSign(OWNER, text);
  assert.equal(await I.verify('solana', OWNER.publicKey.toBase58(), text, sig.toString('base64')), true);
  assert.equal(await I.verify('solana', OWNER.publicKey.toBase58(), text, bs58.encode(sig)), true, 'base58 too');
  assert.equal(await I.verify('solana', PROFIT.publicKey.toBase58(), text, sig.toString('base64')), false, 'wrong signer');
  assert.equal(await I.verify('solana', OWNER.publicKey.toBase58(), text + ' ', sig.toString('base64')), false, 'tampered text');
  const flipped = Buffer.from(sig); flipped[5] ^= 1;
  assert.equal(await I.verify('solana', OWNER.publicKey.toBase58(), text, flipped.toString('base64')), false, 'tampered signature');
  for (const bad of ['', 'x', null, 42, sig.subarray(0, 63).toString('base64'), bs58.encode(sig.subarray(0, 63)), sig.toString('hex'),
    `${sig.toString('base64')}A`, `${bs58.encode(sig)}0`]) {
    assert.equal(await I.verify('solana', OWNER.publicKey.toBase58(), text, bad), false, String(bad));
  }
});

// An ed25519 signature over raw bytes.
function solSignBytes(key, bytes) {
  const pk = crypto.createPrivateKey({ format: 'jwk', key: { kty: 'OKP', crv: 'Ed25519',
    d: Buffer.from(key.secretKey.subarray(0, 32)).toString('base64url'), x: Buffer.from(key.publicKey.toBytes()).toString('base64url') } });
  return crypto.sign(null, Buffer.from(bytes), pk);
}

// The full off-chain layout: domain, format, signers, length, text.
function offchainFull(text, format, signers, domain = Buffer.alloc(32, 7)) {
  const msg = Buffer.from(text, 'utf8'), len = Buffer.alloc(2); len.writeUInt16LE(msg.length);
  return Buffer.concat([I.OFFCHAIN_DOMAIN, Buffer.from([0]), domain, Buffer.from([format, signers.length]),
    ...signers.map(k => Buffer.from(k.toBytes())), len, msg]);
}

test('verify (Solana): a hardware wallet\'s off-chain message of the same text, both layouts', async () => {
  const text = 'romefeller.app/bot: create one vault\n\nNonce: 3', owner = OWNER.publicKey.toBase58();
  const v = (sig, signed) => I.verify('solana', owner, text, sig.toString('base64'), signed ? Buffer.from(signed).toString('base64') : null);
  // the short layout (the Ledger app): accepted with or without the bytes the wallet reports
  for (const format of [1, 2]) {
    const short = I.offchainShort(text, format);
    assert.equal(await v(solSignBytes(OWNER, short)), true, `short, format ${format}, found by the server`);
    assert.equal(await v(solSignBytes(OWNER, short), short), true, `short, format ${format}, as reported`);
  }
  assert.equal(await v(solSignBytes(OWNER, I.offchainShort(text, 0))), false, 'format 0 is printable ASCII: a newline is not');
  const ascii = 'romefeller.app/bot: one line, Nonce: 4';
  assert.equal(await I.verify('solana', owner, ascii, solSignBytes(OWNER, I.offchainShort(ascii, 0)).toString('base64')), true, 'format 0 for ASCII');
  // the full layout: only when the wallet reports the bytes, the owner its one signer
  const full = offchainFull(text, 1, [OWNER.publicKey]);
  assert.equal(await v(solSignBytes(OWNER, full), full), true, 'full layout, owner the signer');
  assert.equal(await v(solSignBytes(OWNER, full)), false, 'full layout without the reported bytes: its domain is unknown');
  const two = offchainFull(text, 1, [OWNER.publicKey, PROFIT.publicKey]);
  assert.equal(await v(solSignBytes(OWNER, two), two), false, 'another signer named beside the owner');
  const other = offchainFull(text, 1, [PROFIT.publicKey]);
  assert.equal(await v(solSignBytes(OWNER, other), other), false, 'the owner not named');
  // the same envelope around another text, or a changed one, is refused
  const tampered = I.offchainShort(`${text}.`, 1);
  assert.equal(await v(solSignBytes(OWNER, tampered), tampered), false, 'another text');
  const longer = Buffer.concat([I.offchainShort(text, 1), Buffer.from([0])]);
  assert.equal(await v(solSignBytes(OWNER, longer), longer), false, 'bytes after the text');
  const wrongLen = I.offchainShort(text, 1); wrongLen.writeUInt16LE(5, 18);
  assert.equal(await v(solSignBytes(OWNER, wrongLen), wrongLen), false, 'a length that is not the text\'s');
  const v1 = I.offchainShort(text, 1); v1[16] = 1;
  assert.equal(await v(solSignBytes(OWNER, v1), v1), false, 'an unknown version');
  const f3 = I.offchainShort(text, 3);
  assert.equal(await v(solSignBytes(OWNER, f3), f3), false, 'an unknown format');
  // reported bytes that are not what was signed, or not base64
  assert.equal(await v(solSignBytes(OWNER, Buffer.from(text)), I.offchainShort(text, 1)), false, 'signed raw, reported the envelope');
  assert.equal(await I.verify('solana', owner, text, solSignBytes(OWNER, Buffer.from(text)).toString('base64'), '!!'), false, 'not base64');
  // the wrong key over a right envelope
  assert.equal(await v(solSignBytes(PROFIT, I.offchainShort(text, 1))), false, 'another key');
});

test('verify (Solana): no format confusion: a transaction is never a message, a message never a transaction', async () => {
  const owner = OWNER.publicKey.toBase58();
  const tx = new Transaction().add(require('@solana/web3.js').SystemProgram.transfer({ fromPubkey: OWNER.publicKey, toPubkey: BOT.publicKey, lamports: 1 }));
  tx.feePayer = OWNER.publicKey; tx.recentBlockhash = PublicKey.default.toBase58();
  const txBytes = tx.serializeMessage();
  const sig = solSignBytes(OWNER, txBytes);
  // a signed transaction does not pass as any intent, reported or found
  assert.equal(await I.verify('solana', owner, txBytes.toString('utf8'), sig.toString('base64')), false);
  assert.equal(await I.verify('solana', owner, 'anything', sig.toString('base64'), txBytes.toString('base64')), false);
  assert.equal(I.parseOffchain(txBytes), null, 'a transaction message is no off-chain message');
  // an off-chain message is never a transaction: its first byte, 0xff, is no signature count or version
  const env = I.offchainShort('hello', 1);
  assert.equal(env[0], 0xff);
  assert.throws(() => require('@solana/web3.js').Message.from(env));
  assert.throws(() => require('@solana/web3.js').VersionedMessage.deserialize(env));
  // property: for any text, every accepted form is the text or an envelope of exactly it
  fc.assert(fc.property(fc.string({ maxLength: 300 }), (t) => {
    for (const m of I.signedForms(t, owner)) {
      if (m.equals(Buffer.from(t, 'utf8'))) continue;
      const p = I.parseOffchain(m);
      assert.ok(p && p.text === t && p.layout === 'short');
    }
  }), { numRuns: 300 });
});

test('verify (EVM): personal_sign recovered to the owner only', async () => {
  const text = 'hello\nNonce: 2';
  const sig = await EVM_OWNER.signMessage({ message: text });
  assert.equal(await I.verify('polygon', EVM_OWNER.address, text, sig), true);
  assert.equal(await I.verify('polygon', EVM_OWNER.address.toLowerCase(), text, sig), true);
  assert.equal(await I.verify('polygon', EVM_OTHER.address, text, sig), false, 'wrong signer');
  assert.equal(await I.verify('polygon', EVM_OWNER.address, `${text}.`, sig), false, 'tampered text');
  assert.equal(await I.verify('polygon', EVM_OWNER.address, text, sig.slice(0, -2)), false, 'short');
  assert.equal(await I.verify('polygon', EVM_OWNER.address, text, `${sig.slice(0, 10)}zz${sig.slice(12)}`), false, 'not hex');
  assert.equal(await I.verify('polygon', EVM_OWNER.address, text, `0x${'00'.repeat(65)}`), false, 'garbage');
});

test('invite: the hash of the code, nothing else', () => {
  assert.equal(I.inviteOk(INVITE, [HASH]), true);
  assert.equal(I.inviteOk(INVITE, ['bad', HASH.toUpperCase(), HASH]), true);
  assert.equal(I.inviteOk(INVITE, [HASH.toUpperCase()]), false, 'hashes are lower-case hex');
  assert.equal(I.inviteOk(`${INVITE} `, [HASH]), false);
  assert.equal(I.inviteOk(HASH, [HASH]), false, 'the hash is not the code');
  assert.equal(I.inviteOk(INVITE, []), false, 'no hash configured: nobody gets in');
  for (const bad of [undefined, null, 42, 'short', 'x'.repeat(129)]) assert.equal(I.inviteOk(bad, [HASH]), false);
  const sha = (c) => crypto.createHash('sha256').update(c).digest('hex');
  for (const [code, ok] of [['1234567', false], ['12345678', true], ['y'.repeat(128), true], ['y'.repeat(129), false]]) {
    assert.equal(I.inviteOk(code, [sha(code)]), ok, `${code.length} characters`);
  }
});

test('windows: max events per key per window, then free again; bounded keys', () => {
  let t = 0;
  const w = new Windows({ max: 2, windowS: 10 }, () => t, 3);
  w.add('a'); assert.equal(w.full('a'), false);
  w.add('a'); assert.equal(w.full('a'), true);
  assert.equal(w.full('b'), false);
  t = 9_999; assert.equal(w.full('a'), true);
  t = 10_000; assert.equal(w.full('a'), false, 'the window slid');
  for (const k of ['b', 'c', 'd', 'e']) w.add(k);
  assert.ok(w.seen.size <= 3);
  assert.equal(w.seen.has('b'), false, 'the stalest key went first');
});

test('the service command takes a checked name only', () => {
  assert.deepEqual(serviceCommand(CFG.service.start, 'my-pool'), ['sudo', '-n', '/usr/bin/systemctl', 'enable', '--now', 'lp-bot@my-pool.service']);
  for (const bad of ['../x', 'a b', 'a;b', 'A', '', 'a--b', 'x'.repeat(40), '-x']) assert.throws(() => serviceCommand(CFG.service.start, bad), /refused/);
});

test('venues: the config\'s order, only those with vault rules, support and a pool', () => {
  const c = { venues: ['raydium-clmm', 'meteora-dlmm', 'orca'], pools: { 'meteora-dlmm': 'p', orca: 'q' }, approveVenues: 5 };
  assert.deepEqual(vaultVenues(c, { venues: { 'meteora-dlmm': {}, orca: {}, 'raydium-clmm': {} } }, { 'meteora-dlmm': {}, 'raydium-clmm': {} }), ['meteora-dlmm']);
  assert.deepEqual(vaultVenues(c, { venues: { 'meteora-dlmm': {}, orca: {} } }, { 'meteora-dlmm': {}, orca: {} }), ['meteora-dlmm', 'orca']);
  assert.deepEqual(vaultVenues({ ...c, approveVenues: 1 }, { venues: { 'meteora-dlmm': {}, orca: {} } }, { 'meteora-dlmm': {}, orca: {} }), ['meteora-dlmm'],
    'only the first approveVenues');
  assert.deepEqual(vaultVenues({ ...c, pools: {} }), []);
  assert.deepEqual(vaultVenues(SOL), ['raydium-clmm'], 'today: Raydium alone, the venue sol-usdc opens on (setup gas, onboard.json)');
  for (const bad of [0, -1, 1.5, '1', undefined, null]) {
    const raw = JSON.parse(fs.readFileSync(new URL('../onboard/onboard.json', import.meta.url), 'utf8')).chains.solana;
    assert.throws(() => resolveSolana({ ...raw, approveVenues: bad }), /approveVenues/, String(bad));
  }
});

test('profile venues: the first approved one in the config\'s order opens; every approved one may run', () => {
  const order = ['raydium-clmm', 'orca', 'meteora-dlmm'];
  const v = (...names) => ({ venues: Object.fromEntries(names.map(n => [n, { policies: ['x'], pools: [`pool-${n}`, 'second'] }])) });
  assert.deepEqual(profileVenues(v('meteora-dlmm', 'orca'), order), { dex: 'orca', pool: 'pool-orca', execute: ['orca', 'meteora-dlmm'] });
  assert.deepEqual(profileVenues(v('byreal', 'meteora-dlmm'), order), { dex: 'meteora-dlmm', pool: 'pool-meteora-dlmm', execute: ['meteora-dlmm', 'byreal'] },
    'a venue the order does not name runs, after the named ones');
  assert.deepEqual(profileVenues(v('byreal'), order), { dex: 'byreal', pool: 'pool-byreal', execute: ['byreal'] });
  // a Safe (Polygon) report names its one venue
  assert.deepEqual(profileVenues({ venue: 'uniswap-v3-polygon', pools: ['0xP'] }, ['uniswap-v3-polygon']),
    { dex: 'uniswap-v3-polygon', pool: '0xP', execute: ['uniswap-v3-polygon'] });
  for (const bad of [{ venues: {} }, { venues: { orca: { pools: [] } } }, { venues: { orca: {} } }, { venue: 'orca', pools: [] }, {}]) {
    assert.throws(() => profileVenues(bad, order), /approved no pool/, JSON.stringify(bad));
  }
  assert.deepEqual(profileVenues({ venues: { toString: { pools: ['p'] } } }, order).execute, ['toString'], 'own keys only, never the prototype');
});

test('the client address: the forwarded one only from the local proxy', () => {
  const req = (peer, fwd) => ({ socket: { remoteAddress: peer }, headers: fwd ? { 'x-forwarded-for': fwd } : {} });
  assert.equal(clientOf(req('127.0.0.1', '1.2.3.4')), '1.2.3.4');
  assert.equal(clientOf(req('::ffff:127.0.0.1', '1.2.3.4, 5.6.7.8')), '1.2.3.4');
  assert.equal(clientOf(req('9.9.9.9', '1.2.3.4')), '9.9.9.9', 'a forwarded header from elsewhere is ignored');
  assert.equal(clientOf(req('127.0.0.1')), '127.0.0.1');
});

test('the config: loopback only, a service command with {name}', () => {
  assert.equal(CFG.listen.host, '127.0.0.1');
  assert.equal(CFG.base, '/bot');
  for (const name of ['sol-usdc', 'sol-swing', 'poly-wpol-usdt']) assert.ok(CFG.reservedNames.includes(name), name);
});

// --- the server with fake chains and a fake database ----------------------------------------

// The database as store.py keeps it, in memory: the states and refusals the server relies on.
function memoryStore(over = {}) {
  const rows = [];
  let id = 0;
  const live = (r) => r.state !== 'failed';
  const byId = (i) => rows.find(r => r.id === i);
  const calls = [];
  const store = async (cmd, r) => {
    calls.push([cmd, r]);
    if (cmd === 'taken') return { taken: rows.some(x => live(x) && x.name === r.name) || ['taken-name', 'sol-usdc'].includes(r.name) };
    if (cmd === 'owned') return { count: rows.filter(x => live(x) && x.owner === r.owner).length + (over.owned ?? 0) };
    if (cmd === 'sign') {
      rows.push({ ...r, id: ++id, state: 'signed', created_at: (over.now?.() ?? Date.now()) / 1000, vault_row: null, connected: null });
      return { id };
    }
    if (cmd === 'paid') {
      if (rows.some(x => x.payment === r.payment)) throw new StoreError('this payment was already used');
      const x = byId(r.id);
      if (x.state !== 'signed') throw new StoreError('not waiting for a payment');
      Object.assign(x, { state: 'connecting', payment: r.payment });
      return { paid: true };
    }
    if (cmd === 'resume') { const x = byId(r.id); if (!['stuck', 'connecting'].includes(x.state)) throw new StoreError('not paid'); x.state = 'connecting'; return {}; }
    if (cmd === 'connected') { Object.assign(byId(r.id), { connected: r.connected, vault: r.connected.vault }); return {}; }
    if (cmd === 'stuck') { Object.assign(byId(r.id), { state: 'stuck', error: r.error, connected: r.connected ?? byId(r.id).connected }); return {}; }
    if (cmd === 'register') {
      if (over.registerFails?.()) throw new Error('database down');
      Object.assign(byId(r.id), { state: 'registered', vault: r.connected.vault, vault_row: { settings: 'S', venues: {} } });
      return { vault: r.connected.vault };
    }
    if (cmd === 'get') return { request: rows.find(x => live(x) && x.name === r.name) ?? null };
    if (cmd === 'started') { byId(r.id).state = 'started'; return { started: true }; }
    if (cmd === 'list') return { requests: rows.filter(x => live(x) && x.owner === r.owner) };
    if (cmd === 'feed') return { feed: over.feed ?? {} };
    if (cmd === 'exits') return { exit: over.exit ?? null };
    if (cmd === 'exit') return { requested: true, new: true };
    if (cmd === 'deposited') return { recorded: true, max_usd: 9 };
    throw new Error(cmd);
  };
  return { store, rows, calls };
}

function fakes(over = {}, cfg = CFG) {
  let t = 1_000_000_000;
  const db = memoryStore({ ...over, now: () => t });
  const calls = { connect: 0, start: [], checks: [], planned: [], holdings: [] };
  const payments = new Map();             // signature -> verdict
  const adapter = {
    keys: { public: [BOT.publicKey.toBase58()], payee: PAYER, secretEnv: 'P', policyKeyEnv: 'Q' },
    expires: over.expires ?? false,
    venueReady: () => true,
    gas: async () => ({ ok: over.gasOk ?? true, have: '0.01', need: '0.045', symbol: 'SOL', key: 'k' }),
    payment: async (a) => {
      if (over.poor) throw new I.Invalid('your wallet holds 0 SOL');
      return { summary: `pay ${a.gas} to ${a.payer}`, transactions: [JSON.stringify(a)] };
    },
    checkPayment: async (sig, ex) => { calls.checks.push([sig, ex]); return payments.get(sig) ?? { state: 'pending', reason: 'the payment is not on chain (yet)' }; },
    connect: async (f) => {
      calls.connect++;
      calls.planned.push(f.planned);
      await f.onPlan?.({ seed: f.planned?.seed ?? `SEED${calls.connect}`, settings: 'S', vault: 'VAULT' });
      if (over.connectFails?.()) throw new Error('rpc down');
      // a Solana connect names each venue's policies and pools (here not in the config's order)
      return { vault: 'VAULT', sent: true, owners: [f.owner], f,
        venues: { 'meteora-dlmm': { policies: ['M1'], pools: ['PM'] }, byreal: { policies: ['B1'], pools: ['PB'] }, 'raydium-clmm': { policies: ['R1', 'R2'], pools: ['PR'] } } };
    },
    record: async () => ({ token_a: {}, token_b: {} }),
    deposit: async (a) => ({ summary: 'deposit', transactions: [JSON.stringify(a)] }),
    owner: async (row, a) => ({ summary: a.action, transactions: ['one', 'two'] }),
    status: async () => ({ vault: 'VAULT', balances: { SOL: over.deposited ? '1.5' : '0' }, botActive: over.botActive ?? true, positions: over.positions ?? 0 }),
    hasDeposit: (st) => Number(st.balances.SOL) > 0,
    holdings: async (owner, token) => { calls.holdings.push([owner, token]); return over.held ?? { SOL: 2_000_000_000n, USDC: 0n }; },
    context: () => ({ settings: 'S', vault: 'VAULT', policies: {}, pools: {}, payoutWallets: [] }),
    mintOf: (token) => cfg.chains.solana.tokens[token]?.mint ?? null,
    checkDeposit: async () => over.deposit ?? { state: 'ok', deposits: [{ token: 'SOL', amount: '1' }] },
    confirm: async () => ({ state: 'confirmed' }),
    broadcast: async () => 'SIG',
  };
  const server = makeServer(cfg, { chains: { solana: adapter }, store: db.store, inviteHashes: HASH, log: () => {}, now: () => t,
    start: async (cmd) => { calls.start.push(cmd); return { code: over.startCode ?? 0, out: '', err: 'boom' }; },
    status: async () => ({ code: 0, out: 'active\n', err: '' }) });
  return { server, calls, db, payments, adapter, tick: (ms) => { t += ms; } };
}

async function serve(f, fn) {
  await new Promise(r => f.server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${f.server.address().port}/bot`;
  const post = async (p, b, headers = {}) => {
    const r = await fetch(`${base}/api/${p}`, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(b) });
    return { status: r.status, json: await r.json().catch(() => null) };
  };
  try { await fn({ base, post }); } finally { f.server.close(); }
}

const signed = async (post, over = {}, key = OWNER) => {
  const it = await post('intent', { invite: INVITE, ...body(over) });
  assert.equal(it.status, 200, JSON.stringify(it.json));
  return { it: it.json, sig: solSign(key, it.json.message).toString('base64') };
};

// intent, signature, payment answer
const signedUp = async (post, over = {}, key = OWNER) => {
  const { it, sig } = await signed(post, over, key);
  const s = await post('sign', { invite: INVITE, nonce: it.nonce, message: it.message, signature: sig });
  assert.equal(s.status, 200, JSON.stringify(s.json));
  return { it, sig, pay: s.json };
};

test('server: sign, pay the gas, set up, deposit, start — and only signed values are used', async () => {
  const f = fakes({ deposited: true });
  await serve(f, async ({ post }) => {
    const { it, pay } = await signedUp(post);
    assert.match(it.message, /Gas to the bot: 0.145 SOL, paid now to the bot's key /);
    assert.equal(it.gas, '0.145');
    assert.deepEqual(JSON.parse(pay.transactions[0]), { owner: OWNER.publicKey.toBase58(), payer: PAYER, gas: '0.145', token: 'SOL', deposit: '1.355' });
    assert.equal(f.calls.connect, 0, 'nothing spent before the payment');
    // not on chain yet: asked again, nothing spent
    const early = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'SIG1' });
    assert.equal(early.status, 409);
    assert.equal(f.calls.connect, 0);
    f.payments.set('SIG1', { state: 'ok' });
    const made = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'SIG1', amount: '999' });
    assert.equal(made.status, 200, JSON.stringify(made.json));
    assert.deepEqual(made.json, { name: 'my-pool', chain: 'solana', state: 'registered', vault: 'VAULT', token: 'SOL', deposit: '1.355' });
    const [, ex] = f.calls.checks.at(-1);
    assert.deepEqual(ex, { owner: OWNER.publicKey.toBase58(), payer: PAYER, amount: '0.145', notBefore: 1_000_000 - CFG.paymentSlackS });
    assert.equal(f.calls.connect, 1);
    const row = f.db.rows[0];
    assert.deepEqual([row.name, row.wallet_id, row.amount, row.deposit, row.gas, row.payer, row.special, row.fee_bps, row.fee_wallet, row.intent],
      ['my-pool', 'my-pool-vault', '1.5', '1.355', '0.145', PAYER, false, 1000, OPERATOR_SOL, it.message]);
    const reg = f.db.calls.find(c => c[0] === 'register')[1];
    assert.equal(reg.profile.template, 'sol-usdc');
    assert.deepEqual([reg.secret_env, reg.policy_key_env, reg.profile.dex, reg.profile.pool, reg.profile.execute_dexes],
      ['P', 'Q', 'raydium-clmm', 'PR', ['raydium-clmm', 'meteora-dlmm', 'byreal']], 'opens on the first approved venue in the config\'s order, may use all');
    const { onPlan, ...signed } = reg.connected.f;
    assert.deepEqual(signed, { owner: OWNER.publicKey.toBase58(), profitWallet: PROFIT.publicKey.toBase58(), feeWallet: OPERATOR_SOL, planned: null });
    assert.equal(typeof onPlan, 'function', 'the plan is saved before the connect sends anything');
    const dep = await post('tx', { invite: INVITE, name: 'my-pool', action: 'deposit', amount: '50', token: 'USDC' });
    assert.deepEqual(JSON.parse(dep.json.transactions[0]), { owner: OWNER.publicKey.toBase58(), vault: 'VAULT', token: 'SOL', amount: '1.355' },
      'the deposit is the signed amount less the gas, whatever the body says');
    const again = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'SIG1' });
    assert.deepEqual([again.status, again.json.state], [200, 'registered'], 'a done request answers, spends nothing');
    assert.equal(f.calls.connect, 1);
    const st = await post('start', { invite: INVITE, name: 'my-pool' });
    assert.equal(st.status, 200, JSON.stringify(st.json));
    assert.deepEqual(f.calls.start, [['sudo', '-n', '/usr/bin/systemctl', 'enable', '--now', 'lp-bot@my-pool.service']]);
    const mine = await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() });
    assert.equal(mine.json.vaults[0].service, 'active');
    assert.equal(mine.json.vaults[0].state, 'started');
  });
});

test('server: a bad payment spends nothing; a payment serves one request only', async () => {
  const f = fakes();
  await serve(f, async ({ post }) => {
    await signedUp(post, { name: 'pool-a' });
    f.payments.set('SMALL', { state: 'bad', reason: 'the payment is 0.1 SOL, less than 0.145' });
    const bad = await post('pay', { invite: INVITE, name: 'pool-a', payment: 'SMALL' });
    assert.deepEqual([bad.status, bad.json.error], [400, 'the payment is 0.1 SOL, less than 0.145']);
    assert.equal(f.db.rows[0].state, 'signed', 'still waiting for a good payment');
    f.payments.set('GOOD', { state: 'ok' });
    assert.equal((await post('pay', { invite: INVITE, name: 'pool-a', payment: 'GOOD' })).status, 200);
    // replay: the same payment for another wallet's request
    await signedUp(post, { name: 'pool-b', owner: OWNER2.publicKey.toBase58() }, OWNER2);
    const replay = await post('pay', { invite: INVITE, name: 'pool-b', payment: 'GOOD' });
    assert.equal(replay.status, 400);
    assert.match(replay.json.error, /already used/);
    assert.equal(f.calls.connect, 1);
    assert.equal((await post('pay', { invite: INVITE, name: 'nobody', payment: 'GOOD' })).status, 404);
  });
});

test('server: a setup that fails keeps the payment and resumes without paying or connecting twice', async () => {
  let failConnect = true, failRegister = true;
  const f = fakes({ connectFails: () => failConnect, registerFails: () => failRegister });
  await serve(f, async ({ post }) => {
    await signedUp(post);
    f.payments.set('PAID', { state: 'ok' });
    const one = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'PAID' });
    assert.equal(one.status, 502);
    assert.match(one.json.error, /the setup stopped: rpc down\. Your gas payment is kept: press Resume/);
    assert.equal(f.db.rows[0].state, 'stuck');
    const listed = await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() });
    assert.deepEqual([listed.json.vaults[0].state, listed.json.vaults[0].error], ['stuck', 'rpc down']);
    failConnect = false;
    const two = await post('pay', { invite: INVITE, name: 'my-pool' });
    assert.equal(two.status, 502, 'connected, then the registration failed');
    assert.match(two.json.error, /The vault VAULT exists; do not deposit/);
    assert.equal(f.calls.connect, 2, 'the first try failed before any vault, so the resume connected');
    assert.deepEqual(f.calls.planned, [null, { seed: 'SEED1', settings: 'S', vault: 'VAULT' }],
      'the resume finishes the vault the first try planned (its seed), never a new one');
    failRegister = false;
    const three = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'SOMETHING-ELSE' });
    assert.deepEqual([three.status, three.json.state], [200, 'registered']);
    assert.equal(f.calls.connect, 2, 'the saved vault is registered, never connected again');
    assert.equal(f.calls.checks.length, 1, 'the payment was checked once, never asked again');
    assert.equal(f.db.rows[0].payment, 'PAID');
  });
});

test('server: two calls for one request at once: one setup, one connect', async () => {
  const f = fakes();
  let release;
  const gate = new Promise(r => { release = r; });
  const connect = f.adapter.connect;
  f.adapter.connect = async (x) => { await gate; return connect(x); };
  await serve(f, async ({ post }) => {
    await signedUp(post);
    f.payments.set('P', { state: 'ok' });
    const first = post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    await new Promise(r => setTimeout(r, 100));
    const second = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    assert.deepEqual([second.status, second.json.error], [409, 'the setup of this vault is running; wait']);
    release();
    assert.equal((await first).status, 200);
    assert.equal(f.calls.connect, 1);
    const third = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    assert.deepEqual([third.status, third.json.state], [200, 'registered'], 'and after it, a call answers');
  });
});

test('server: a wallet that cannot pay holds no name; a short bot key stops the setup, resumable', async () => {
  const poor = fakes({ poor: true });
  await serve(poor, async ({ post }) => {
    const { it, sig } = await signed(post);
    const r = await post('sign', { invite: INVITE, nonce: it.nonce, message: it.message, signature: sig });
    assert.deepEqual([r.status, r.json.error], [400, 'your wallet holds 0 SOL']);
    assert.equal(poor.db.rows.length, 0);
  });
  const short = fakes({ gasOk: false });
  await serve(short, async ({ post }) => {
    await signedUp(post);
    short.payments.set('P', { state: 'ok' });
    const r = await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    assert.equal(r.status, 502);
    assert.match(r.json.error, /holds 0.01 SOL, less than one setup/);
    assert.equal(short.calls.connect, 0);
    assert.equal(short.db.rows[0].state, 'stuck');
  });
});

test('server: a replayed, expired, tampered or foreign signature records nothing', async () => {
  const f = fakes({}, { ...CFG, limits: { ...CFG.limits, vaultsPerOwner: 5 } });      // one owner, many tries
  await serve(f, async ({ post }) => {
    const a = await signedUp(post, { name: 'pool-a' });
    const again = await post('sign', { invite: INVITE, nonce: a.it.nonce, message: a.it.message, signature: a.sig });
    assert.equal(again.status, 400);
    assert.match(again.json.error, /unknown or used/);
    const b = await signed(post, { name: 'pool-b' });
    const forged = b.it.message.replace('Amount: 1.5 SOL', 'Amount: 50 SOL');
    const t = await post('sign', { invite: INVITE, nonce: b.it.nonce, message: forged, signature: solSign(OWNER, forged).toString('base64') });
    assert.equal(t.status, 400);
    assert.match(t.json.error, /not the one this server issued/);
    assert.equal((await post('sign', { invite: INVITE, nonce: b.it.nonce, message: b.it.message, signature: b.sig })).status, 400, 'a failed try uses the nonce');
    const c = await signed(post, { name: 'pool-c' }, PROFIT);
    assert.equal((await post('sign', { invite: INVITE, nonce: c.it.nonce, message: c.it.message, signature: c.sig })).status, 401);
    const d = await signed(post, { name: 'pool-d' });
    f.tick(CFG.intentTtlS * 1000);
    const x = await post('sign', { invite: INVITE, nonce: d.it.nonce, message: d.it.message, signature: d.sig });
    assert.equal(x.status, 400);
    assert.match(x.json.error, /expired/);
    assert.deepEqual(f.db.rows.map(r => r.name), ['pool-a'], 'only the one good signature was recorded');
    assert.equal(f.calls.connect, 0);
  });
});

test('server: no invite, no service; wrong codes lock the address out', async () => {
  const f = fakes();
  await serve(f, async ({ post }) => {
    for (const p of ['intent', 'sign', 'pay', 'tx', 'start', 'vaults']) {
      const r = await post(p, { ...body() });
      assert.equal(r.status, p === 'vaults' ? 429 : 403, p);
    }
    const locked = await post('intent', { invite: INVITE, ...body() });
    assert.equal(locked.status, 429, 'after too many wrong codes, even the right one waits');
    const other = await post('intent', { invite: INVITE, ...body() }, { 'x-forwarded-for': '10.0.0.9' });
    assert.equal(other.status, 200, 'another address is not locked');
    assert.equal(f.calls.connect, 0);
  });
});

test('server: names and vaults per owner', async () => {
  await serve(fakes(), async ({ post }) => {
    assert.equal((await post('intent', { invite: INVITE, ...body({ name: 'taken-name' }) })).status, 409);
    assert.equal((await post('intent', { invite: INVITE, ...body({ name: 'sol-usdc' }) })).status, 400, 'a reserved name');
    assert.equal((await post('intent', { invite: INVITE, ...body({ amount: '1000' }) })).status, 400);
    assert.equal((await post('intent', { invite: INVITE, ...body({ amount: '0.16' }) })).status, 400, 'under the gas and the minimum capital');
    // two intents for one name: the second signature finds it taken
    const a = await signed(post), b = await signed(post);
    assert.equal((await post('sign', { invite: INVITE, nonce: a.it.nonce, message: a.it.message, signature: a.sig })).status, 200);
    assert.equal((await post('sign', { invite: INVITE, nonce: b.it.nonce, message: b.it.message, signature: b.sig })).status, 409);
  });
  await serve(fakes({ owned: CFG.limits.vaultsPerOwner }), async ({ post }) => {
    const r = await post('intent', { invite: INVITE, ...body() });
    assert.equal(r.status, 403);
    assert.match(r.json.error, /has its vault already: put more capital in with Add capital/);
  });
});

// --- one pool per wallet: more capital goes into the vault it has -------------------------

test('one pool per wallet: the config allows one vault, and a wallet with one cannot sign for another', async () => {
  assert.equal(CFG.limits.vaultsPerOwner, 1);
  const f = fakes({ deposited: true });
  await serve(f, async ({ post }) => {
    await signedUp(post);
    for (const name of ['other-pool', 'my-pool']) {
      const r = await post('intent', { invite: INVITE, ...body({ name }) });
      assert.equal(r.status, name === 'my-pool' ? 409 : 403, name);
    }
    f.payments.set('P', { state: 'ok' });
    assert.equal((await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' })).status, 200);
    assert.equal((await post('intent', { invite: INVITE, ...body({ name: 'other-pool' }) })).status, 403, 'registered: still its one vault');
    const other = await post('intent', { invite: INVITE, ...body({ name: 'other-pool', owner: OWNER2.publicKey.toBase58() }) });
    assert.equal(other.status, 200, 'another wallet makes its own');
    f.db.rows[0].state = 'failed';
    assert.equal((await post('intent', { invite: INVITE, ...body({ name: 'other-pool' }) })).status, 200, 'a failed request holds no vault');
  });
});

test('add capital: any amount of a chain token into the vault the request made, never elsewhere', async () => {
  const f = fakes({ deposited: true });
  await serve(f, async ({ post }) => {
    await signedUp(post);
    const early = await post('tx', { invite: INVITE, name: 'my-pool', action: 'add', token: 'USDC', amount: '5' });
    assert.equal(early.status, 404, 'no vault yet: nothing to add to');
    f.payments.set('P', { state: 'ok' });
    await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    for (const [token, amount, want] of [['USDC', '5', '5'], ['USDC', '0,5', '0.5'], ['SOL', '0.000000001', '0.000000001'],
      ['SOL', '50', '50'], ['USDC', '10000', '10000']]) {
      const r = await post('tx', { invite: INVITE, name: 'my-pool', action: 'add', token, amount, owner: OWNER2.publicKey.toBase58(),
        vault: 'ELSEWHERE' });
      assert.equal(r.status, 200, `${amount} ${token}: ${JSON.stringify(r.json)}`);
      assert.deepEqual(JSON.parse(r.json.transactions[0]), { owner: OWNER.publicKey.toBase58(), vault: 'VAULT', token, amount: want },
        'the owner and vault of the request, whatever the body says');
    }
    for (const [token, amount] of [['USDC', '0'], ['USDC', '-1'], ['USDC', '10000.000001'], ['SOL', '50.1'], ['USDC', '1e3'],
      ['USDC', '0.0000001'], ['USDC', ''], ['BONK', '5'], ['__proto__', '5'], ['toString', '5'], [undefined, '5']]) {
      const r = await post('tx', { invite: INVITE, name: 'my-pool', action: 'add', token, amount });
      assert.equal(r.status, 400, `${amount} ${token}`);
    }
  });
});

test('add capital: Max without the gas or the minimum, and every Max is an amount add takes', () => {
  const add = { add: true };
  assert.deepEqual(I.maxAmount(SOL, 'SOL', { SOL: 21_000_000n }, add), { amount: '0.02' }, 'no gas, no minimum: all but the fees');
  assert.deepEqual(I.maxAmount(SOL, 'SOL', { SOL: SOL_FEES }, add), { short: 'not enough: you have 0.001 SOL, the minimum is 0.001000001 SOL with your fees' });
  assert.deepEqual(I.maxAmount(SOL, 'USDC', { USDC: 1_000_000n, SOL: USDC_FEES }, add), { amount: '1' });
  assert.deepEqual(I.maxAmount(SOL, 'USDC', { USDC: 1_000_000n, SOL: USDC_FEES - 1n }, add),
    { short: 'not enough SOL for your fees: you have 0.002999999 SOL, you need 0.003 SOL' });
  assert.deepEqual(I.maxAmount(SOL, 'USDC', { USDC: 0n, SOL: GAS }, add), { short: 'not enough: you have 0 USDC, the minimum is 0.000001 USDC' });
  for (const [c, token] of [[SOL, 'SOL'], [SOL, 'USDC'], [POLY, 'POL'], [POLY, 'WPOL'], [POLY, 'USDT0']]) {
    const t = c.tokens[token], [native, n] = I.nativeOf(c);
    const scale = 10n ** BigInt(t.decimals), nscale = 10n ** BigInt(n.decimals);
    fc.assert(fc.property(fc.bigInt({ min: 0n, max: 200_000n * scale }), fc.bigInt({ min: 0n, max: 10n * nscale }), (have, gasCoin) => {
      const held = t.native ? { [token]: have } : { [token]: have, [native]: gasCoin };
      const m = I.maxAmount(c, token, held, add);
      const fees = I.toRaw(t.fees, n.decimals), room = t.native ? have - fees : have;
      if (m.short !== undefined) {
        assert.ok(room <= 0n || (!t.native && gasCoin < fees), m.short);
        return;
      }
      assert.equal(I.addAmount(m.amount, t), m.amount, 'add takes it as it is');
      const raw = I.toRaw(m.amount, t.decimals);
      assert.ok(raw > 0n && raw <= room && (raw === I.toRaw(t.max, t.decimals) || raw === room));
    }), { numRuns: 300 });
  }
});

test('add capital: an amount is positive, at most the max, exact to the unit', () => {
  for (const [c, token] of [[SOL, 'SOL'], [SOL, 'USDC'], [POLY, 'WPOL']]) {
    const t = c.tokens[token], max = I.toRaw(t.max, t.decimals);
    fc.assert(fc.property(fc.bigInt({ min: -10n, max: max + 10n }), (raw) => {
      const s = raw < 0n ? `-${I.fromRaw(-raw, t.decimals)}` : I.fromRaw(raw, t.decimals);
      if (raw > 0n && raw <= max) assert.equal(I.addAmount(s, t), s);
      else assert.throws(() => I.addAmount(s, t), I.Invalid);
    }), { numRuns: 300 });
  }
});

test('server: start needs a deposit and an active bot; a failed start says so', async () => {
  for (const [over, status, re] of [[{}, 409, /deposit first/], [{ deposited: true, botActive: false }, 409, /revoked/],
    [{ deposited: true, startCode: 1 }, 502, /could not be started/]]) {
    const f = fakes(over);
    await serve(f, async ({ post }) => {
      await signedUp(post);
      f.payments.set('P', { state: 'ok' });
      assert.equal((await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' })).status, 200);
      const r = await post('start', { invite: INVITE, name: 'my-pool' });
      assert.equal(r.status, status, JSON.stringify(over));
      assert.match(r.json.error, re);
      assert.equal(f.db.rows[0].state, 'registered');
      assert.equal((await post('start', { invite: INVITE, name: '../etc' })).status, 400);
      assert.equal((await post('start', { invite: INVITE, name: 'nobody-here' })).status, 404);
    });
  }
});

test('server: a chain whose transactions expire answers one at a time', async () => {
  const f = fakes({ expires: true });
  await serve(f, async ({ post }) => {
    await signedUp(post);
    f.payments.set('P', { state: 'ok' });
    await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    const a = await post('tx', { invite: INVITE, name: 'my-pool', action: 'revoke' });
    assert.deepEqual([a.json.count, a.json.index, a.json.transactions], [2, 0, ['one']]);
    const b = await post('tx', { invite: INVITE, name: 'my-pool', action: 'revoke', index: 1 });
    assert.deepEqual([b.json.index, b.json.transactions], [1, ['two']]);
    for (const index of [2, -1, 0.5, 'x', '1', null]) assert.equal((await post('tx', { invite: INVITE, name: 'my-pool', action: 'revoke', index })).status, 400);
    assert.equal((await post('tx', { invite: INVITE, name: 'my-pool', action: 'withdraw', amount: '1e9' })).status, 400);
    assert.equal((await post('tx', { invite: INVITE, name: 'my-pool', action: 'steal' })).status, 400);
    const ap = await post('tx', { invite: INVITE, name: 'my-pool', action: 'approve' });
    assert.deepEqual([ap.status, ap.json.count], [200, 2], 'the owner approves the bot\'s current rules');
    f.adapter.expires = false;
  });
  const g = fakes();
  await serve(g, async ({ post }) => {
    await signedUp(post);
    g.payments.set('P', { state: 'ok' });
    await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    const all = await post('tx', { invite: INVITE, name: 'my-pool', action: 'withdraw', amount: 'all', token: 'SOL' });
    assert.deepEqual([all.json.count, all.json.transactions], [2, ['one', 'two']], 'EVM: every transaction at once');
  });
});

test('server: rate limits, sizes, methods and paths', async () => {
  const f = fakes();
  await serve(f, async ({ base, post }) => {
    assert.equal((await fetch(base.replace('/bot', '/'))).status, 404);
    assert.equal((await fetch(base.replace('/bot', '/botx'))).status, 404);
    assert.equal((await fetch(`${base}/../etc/passwd`)).status, 404);
    const redirect = await fetch(base, { redirect: 'manual' });
    assert.equal(redirect.status, 308);
    const page = await fetch(`${base}/`);
    assert.equal(page.status, 200);
    assert.match(page.headers.get('content-security-policy'), /script-src 'self'/);
    assert.equal((await fetch(`${base}/bundle.js`)).status, 200);
    const pub = await (await fetch(`${base}/api/config`)).json();
    assert.deepEqual(Object.keys(pub.chains), ['solana']);
    assert.deepEqual([pub.chains.solana.gas, pub.chains.solana.tokens.SOL.min, pub.chains.solana.tokens.USDC.min], ['0.145', '0.165', '10']);
    assert.equal(JSON.stringify(pub).includes('Env'), false, 'no environment names leak');
    assert.equal((await fetch(`${base}/api/intent`)).status, 404, 'GET of an action');
    const plain = await fetch(`${base}/api/intent`, { method: 'POST', body: '{}' });
    assert.equal(plain.status, 415);
    const big = await post('intent', { invite: INVITE, pad: 'x'.repeat(CFG.limits.bodyBytes) });
    assert.equal(big.status, 413);
    const arr = await fetch(`${base}/api/intent`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '[1]' });
    assert.equal(arr.status, 400);
    let last;
    for (let i = 0; i <= CFG.limits.requests.max; i++) last = await fetch(`${base}/api/config`, { headers: { 'x-forwarded-for': '10.1.1.1' } });
    assert.equal(last.status, 429);
  });
});

test('server: open intents are capped; an expired one frees its place at its expiry', async () => {
  const cfg = { ...CFG, limits: { ...CFG.limits, openIntents: 2 } };
  const f = fakes({}, cfg);
  await serve(f, async ({ post }) => {
    for (const name of ['pool-a', 'pool-b']) assert.equal((await post('intent', { invite: INVITE, ...body({ name }) })).status, 200);
    const full = await post('intent', { invite: INVITE, ...body({ name: 'pool-c' }) });
    assert.deepEqual([full.status, full.json.error], [503, 'too many open requests; try again in a few minutes']);
    f.tick(CFG.intentTtlS * 1000 - 1);
    assert.equal((await post('intent', { invite: INVITE, ...body({ name: 'pool-c' }) })).status, 503, 'not expired yet');
    f.tick(1);
    assert.equal((await post('intent', { invite: INVITE, ...body({ name: 'pool-c' }) })).status, 200, 'expired at its expiry: pruned');
  });
});

test('server: new requests per address and in all, per day', async () => {
  const f = fakes();
  await serve(f, async ({ post }) => {
    const make = async (name) => {
      const k = Keypair.generate();
      const { it, sig } = await signed(post, { name, owner: k.publicKey.toBase58() }, k);
      return post('sign', { invite: INVITE, nonce: it.nonce, message: it.message, signature: sig });
    };
    for (let i = 0; i < CFG.limits.creates.max; i++) assert.equal((await make(`pool-${i}x`)).status, 200);
    assert.equal((await make('pool-over')).status, 429);
    assert.equal(f.db.rows.length, CFG.limits.creates.max);
  });
});

test('decimal: a phone\'s decimal comma is a point; anything else is left for the checks', () => {
  assert.equal(I.decimal('0,18'), '0.18');
  assert.equal(I.decimal(' 12,5 '), '12.5');
  for (const v of ['0.18', '1,000.5', '1.000,5', '0,1,8', ',5', '5,', 'all', '']) assert.equal(I.decimal(v), v.trim(), v);
  assert.equal(I.fields(body({ amount: '0,18' }), CFG).amount, '0.18', 'the signed amount has a point');
});

test('server: Max reads the owner\'s balances behind the invite; a bad chain, address or token reads nothing', async () => {
  const f = fakes({ held: { SOL: 3_000_000_000n, USDC: 25_000_000n } });
  const owner = OWNER.publicKey.toBase58();
  await serve(f, async ({ post }) => {
    const ok = await post('max', { invite: INVITE, chain: 'solana', owner, token: 'SOL' });
    assert.deepEqual([ok.status, ok.json], [200, { token: 'SOL', amount: '2.999' }]);
    const usdc = await post('max', { invite: INVITE, chain: 'solana', owner, token: 'USDC' });
    assert.deepEqual(usdc.json, { token: 'USDC', amount: '25' });
    f.adapter.holdings = async () => ({ SOL: 100_000_000n });
    const short = await post('max', { invite: INVITE, chain: 'solana', owner, token: 'SOL' });
    assert.deepEqual([short.status, short.json], [200, { token: 'SOL', short: 'not enough: you have 0.1 SOL, the minimum is 0.166 SOL with your fees' }]);
    const added = await post('max', { invite: INVITE, chain: 'solana', owner, token: 'SOL', add: true });
    assert.deepEqual(added.json, { token: 'SOL', amount: '0.099' }, 'Add capital: no gas, no minimum');
    const truthy = await post('max', { invite: INVITE, chain: 'solana', owner, token: 'SOL', add: 'yes' });
    assert.match(truthy.json.short, /minimum is 0.166/, 'only add: true means Add capital');
    assert.deepEqual(f.calls.holdings, [[owner, 'SOL'], [owner, 'USDC']]);
    const pda = PublicKey.findProgramAddressSync([Buffer.from('x')], BOT.publicKey)[0].toBase58();
    for (const [over, status, re] of [[{ invite: 'wrong-invite-code' }, 403, /invite/], [{ invite: undefined }, 403, /invite/],
      [{ owner: 'nope' }, 400, /owner: not a Solana address/], [{ owner: pda }, 400, /owner: not a wallet/],
      [{ owner: EVM_OWNER.address }, 400, /owner/], [{ owner: undefined }, 400, /owner/],
      [{ token: 'BTC' }, 400, /token: one of SOL, USDC/], [{ token: '__proto__' }, 400, /token/], [{ token: undefined }, 400, /token/],
      [{ chain: 'polygon' }, 400, /chain polygon is not open/], [{ chain: '__proto__' }, 400, /not open/],
      [{ rpc: 'http://evil', url: 'http://evil' }, 200, null]]) {
      const r = await post('max', { invite: INVITE, chain: 'solana', owner, token: 'SOL', ...over }, { 'x-forwarded-for': `10.2.0.${status}` });
      assert.equal(r.status, status, JSON.stringify(over));
      if (re) assert.match(r.json.error, re);
    }
    assert.equal(f.calls.holdings.length, 2, 'nothing read for a refused request; an RPC the client names is ignored');
    assert.equal((await fetch(`http://127.0.0.1:${f.server.address().port}/bot/api/max`)).status, 404, 'POST only');
  });
});

test('server: Max is rate limited per address with every other request', async () => {
  const cfg = { ...CFG, limits: { ...CFG.limits, requests: { max: 4, windowS: 600 } } };
  const f = fakes({}, cfg);
  await serve(f, async ({ post }) => {
    const ask = (ip) => post('max', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58(), token: 'SOL' }, { 'x-forwarded-for': ip });
    for (let i = 0; i < 4; i++) assert.equal((await ask('10.3.0.1')).status, 200, String(i));
    const over = await ask('10.3.0.1');
    assert.deepEqual([over.status, over.json.error], [429, 'too many requests; wait a few minutes']);
    assert.equal((await ask('10.3.0.2')).status, 200, 'another address is not limited');
    assert.equal(f.calls.holdings.length, 5);
    f.tick(600_000);
    assert.equal((await ask('10.3.0.1')).status, 200, 'free again after the window');
  });
});

test('server: wrong invite codes to Max lock the address out like any other', async () => {
  const f = fakes();
  await serve(f, async ({ post }) => {
    const ask = (invite) => post('max', { invite, chain: 'solana', owner: OWNER.publicKey.toBase58(), token: 'SOL' });
    for (let i = 0; i < CFG.limits.badInvites.max; i++) assert.equal((await ask('not-the-code')).status, 403);
    assert.equal((await ask(INVITE)).status, 429);
    assert.equal(f.calls.holdings.length, 0);
  });
});

// --- the owner's page: exit requests, deposits, the cap, the views, the CSP (2026-10-12) ---------

const registered = async (f, post) => {
  await signedUp(post);
  f.payments.set('P', { state: 'ok' });
  assert.equal((await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' })).status, 200);
};

test('exit: only the owner\'s signature over the issued message asks the bot to close; once per nonce', async () => {
  const f = fakes({ deposited: true });
  await serve(f, async ({ post }) => {
    await registered(f, post);
    const it = (await post('exit-intent', { invite: INVITE, name: 'my-pool' })).json;
    assert.match(it.message, /close the positions of my-pool into its vault and stop the bot\.\n\nThis moves no money out of the vault and changes no rule\./);
    assert.match(it.message, new RegExp(`Owner: ${OWNER.publicKey.toBase58()}`));
    const exits = () => f.db.calls.filter(([c]) => c === 'exit').length;
    // another key, another text, no signature: nothing recorded
    for (const sign of [(m) => solSign(OWNER2, m), (m) => solSign(OWNER, `${m} `), () => '']) {
      const again = (await post('exit-intent', { invite: INVITE, name: 'my-pool' })).json;
      const r = await post('exit', { invite: INVITE, nonce: again.nonce, message: again.message, signature: sign(again.message).toString('base64') });
      assert.equal(r.status, 401, JSON.stringify(r.json));
    }
    const tampered = await post('exit', { invite: INVITE, nonce: it.nonce, message: `${it.message}x`, signature: solSign(OWNER, `${it.message}x`).toString('base64') });
    assert.equal(tampered.status, 400, 'not the issued text');
    assert.equal(exits(), 0);
    const fresh = (await post('exit-intent', { invite: INVITE, name: 'my-pool' })).json;
    const ok = await post('exit', { invite: INVITE, nonce: fresh.nonce, message: fresh.message, signature: solSign(OWNER, fresh.message).toString('base64') });
    assert.deepEqual([ok.status, ok.json], [200, { name: 'my-pool', requested: true }]);
    assert.equal(exits(), 1);
    const replay = await post('exit', { invite: INVITE, nonce: fresh.nonce, message: fresh.message, signature: solSign(OWNER, fresh.message).toString('base64') });
    assert.equal(replay.status, 400, 'a nonce serves once');
    // a hardware wallet's off-chain message of the same text
    const led = (await post('exit-intent', { invite: INVITE, name: 'my-pool' })).json;
    const env = I.offchainShort(led.message, 1);
    const pk = crypto.createPrivateKey({ format: 'jwk', key: { kty: 'OKP', crv: 'Ed25519', d: Buffer.from(OWNER.secretKey.subarray(0, 32)).toString('base64url'),
      x: Buffer.from(OWNER.publicKey.toBytes()).toString('base64url') } });
    const r = await post('exit', { invite: INVITE, nonce: led.nonce, message: led.message, signature: crypto.sign(null, env, pk).toString('base64'),
      signedMessage: env.toString('base64') });
    assert.equal(r.status, 200, JSON.stringify(r.json));
    // the full layout names its domain: only the bytes the wallet reports can show it
    const fl = (await post('exit-intent', { invite: INVITE, name: 'my-pool' })).json;
    const msg = Buffer.from(fl.message), len = Buffer.alloc(2); len.writeUInt16LE(msg.length);
    const fullEnv = Buffer.concat([I.OFFCHAIN_DOMAIN, Buffer.from([0]), Buffer.alloc(32, 3), Buffer.from([1, 1]), OWNER.publicKey.toBuffer(), len, msg]);
    const rf = await post('exit', { invite: INVITE, nonce: fl.nonce, message: fl.message, signature: crypto.sign(null, fullEnv, pk).toString('base64'),
      signedMessage: fullEnv.toString('base64') });
    assert.equal(rf.status, 200, JSON.stringify(rf.json));
    assert.equal((await post('exit-intent', { invite: INVITE, name: 'nobody-here' })).status, 404);
    assert.equal((await post('exit', { name: 'my-pool' })).status, 403, 'no invite');
  });
});

test('deposited: the amount comes from the chain; pending asks again; a bad one records nothing; the cap follows', async () => {
  for (const [verdict, status] of [[{ state: 'pending', reason: 'not on chain' }, 409], [{ state: 'bad', reason: 'moves nothing' }, 400],
    [{ state: 'ok', deposits: [{ token: 'SOL', amount: '1.355' }, { token: 'USDC', amount: '5' }] }, 200]]) {
    const f = fakes({ deposited: true, deposit: verdict });
    await serve(f, async ({ post }) => {
      await registered(f, post);
      const r = await post('deposited', { invite: INVITE, name: 'my-pool', signature: 'SIGX' });
      assert.equal(r.status, status, JSON.stringify(r.json));
      const rows = f.db.calls.filter(([c]) => c === 'deposited').map(([, x]) => x);
      if (status !== 200) return assert.equal(rows.length, 0);
      assert.deepEqual(rows.map(x => [x.signature, x.token, x.amount, x.mint, x.headroom]), [
        ['SIGX', 'SOL', '1.355', CFG.chains.solana.tokens.SOL.mint, CFG.capHeadroom],
        ['SIGX:1', 'USDC', '5', CFG.chains.solana.tokens.USDC.mint, CFG.capHeadroom]]);
      assert.equal(r.json.maxUsd, 9);
    });
  }
  // register asks for the provisional cap from the signed deposit
  const f = fakes({ deposited: true });
  await serve(f, async ({ post }) => {
    await registered(f, post);
    const reg = f.db.calls.find(([c]) => c === 'register')[1];
    assert.deepEqual(reg.cap, { mint: CFG.chains.solana.tokens.SOL.mint, headroom: CFG.capHeadroom });
  });
});

test('payment: a signed request\'s gas payment is built again, only while it waits for one', async () => {
  const f = fakes();
  await serve(f, async ({ post }) => {
    await signedUp(post);
    const again = await post('payment', { invite: INVITE, name: 'my-pool' });
    assert.equal(again.status, 200);
    assert.deepEqual(JSON.parse(again.json.transactions[0]), { owner: OWNER.publicKey.toBase58(), payer: PAYER, gas: '0.145', token: 'SOL', deposit: '1.355' });
    assert.equal(again.json.gas, '0.145');
    f.payments.set('P', { state: 'ok' });
    await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
    const paid = await post('payment', { invite: INVITE, name: 'my-pool' });
    assert.equal(paid.status, 409);
    assert.match(paid.json.error, /its gas is paid/);
  });
});

test('vaults: the view of each vault; owner actions pass close and ALL; withdraw amounts are checked', async () => {
  const f = fakes({ deposited: true });
  await serve(f, async ({ post }) => {
    await registered(f, post);
    const v = (await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0];
    assert.equal(v.view, 'active');
    assert.equal(v.botReachable, true);
    assert.equal(v.stats, null, 'no book yet');
    assert.equal(v.exit, null);
    for (const [a, ok] of [[{ action: 'close' }, 200], [{ action: 'withdraw', token: 'ALL', amount: 'all' }, 200],
      [{ action: 'withdraw', token: 'ALL', amount: '1' }, 400], [{ action: 'withdraw', token: 'SOL', amount: '1.5' }, 200],
      [{ action: 'withdraw', token: 'SOL', amount: '1e3' }, 400], [{ action: 'withdraw', token: 'DOGE', amount: '1' }, 400],
      [{ action: 'teleport' }, 400]]) {
      const r = await post('tx', { invite: INVITE, name: 'my-pool', ...a });
      assert.equal(r.status, ok, JSON.stringify([a, r.json]));
      if (ok === 200) assert.deepEqual(r.json.context, { settings: 'S', vault: 'VAULT', policies: {}, pools: {}, payoutWallets: [] });
    }
    // a revoked vault
    f.adapter.status = async () => ({ vault: 'VAULT', balances: { SOL: '1' }, botActive: false, positions: 0 });
    assert.equal((await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0].view, 'revoked');
    // a chain that cannot be read: said, not hidden
    f.adapter.status = async () => { throw new Error('rpc down'); };
    const u = (await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0];
    assert.deepEqual([u.view, u.error], ['unknown', 'the chain could not be read: rpc down']);
  });
});

test('broadcast and confirm: a signed transaction is sent as it is; malformed ones are refused before the chain', async () => {
  const f = fakes();
  await serve(f, async ({ post }) => {
    assert.deepEqual((await post('broadcast', { invite: INVITE, chain: 'solana', transaction: 'A'.repeat(200) })).json, { signature: 'SIG' });
    for (const t of ['short', 'not base64 !!'.repeat(20), 'A'.repeat(1800)]) {
      assert.equal((await post('broadcast', { invite: INVITE, chain: 'solana', transaction: t })).status, 400, t.slice(0, 10));
    }
    assert.equal((await post('broadcast', { invite: INVITE, chain: 'polygon', transaction: 'A'.repeat(200) })).status, 400, 'no such chain here');
    assert.deepEqual((await post('confirm', { invite: INVITE, chain: 'solana', signature: 'x' })).json, { state: 'confirmed' });
    assert.equal((await post('confirm', { chain: 'solana', signature: 'x' })).status, 403);
  });
});

test('the CSP: own scripts only; the relay and verify origins only with a project id; the id is 32 hex', async () => {
  const { csp, walletConnectOf, keysMatch } = await import('../onboard/server.mjs');
  const off = csp({ projectId: '', relayUrl: 'wss://relay.walletconnect.org', verify: ['https://verify.walletconnect.org'] });
  assert.equal(off, "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
    + "form-action 'none'; frame-ancestors 'none'");
  const on = csp({ projectId: 'a'.repeat(32), relayUrl: 'wss://relay.walletconnect.org/x', verify: ['https://verify.walletconnect.org'] });
  assert.match(on, /connect-src 'self' wss:\/\/relay\.walletconnect\.org https:\/\/verify\.walletconnect\.org; img-src 'self' data:; frame-src https:\/\/verify\.walletconnect\.org;/);
  assert.doesNotMatch(on, /unsafe|\*/);
  const cfg = { walletConnect: { projectIdEnv: 'WCID', projectId: '', relayUrl: 'wss://r', verify: [] } };
  assert.equal(walletConnectOf(cfg, {}).projectId, '');
  assert.equal(walletConnectOf(cfg, { WCID: 'b'.repeat(32) }).projectId, 'b'.repeat(32), 'the environment wins');
  assert.equal(walletConnectOf({ walletConnect: { ...cfg.walletConnect, projectId: 'c'.repeat(32) } }, {}).projectId, 'c'.repeat(32));
  for (const bad of ['xyz', 'A'.repeat(32), 'a'.repeat(31)]) assert.throws(() => walletConnectOf(cfg, { WCID: bad }), /32 hex/);
  assert.equal(CFG.walletConnect.projectId, '', 'no project id in git: the owner gives it');
  // the pinned keys: the server opens a chain only with the key files git pins
  assert.equal(keysMatch({ botKeys: ['A', 'B'] }, { public: ['A', 'B'] }), true);
  assert.equal(keysMatch({ botKeys: ['0xAb'] }, { public: ['0xab'] }), true, 'EVM: any letter case');
  for (const pub of [['A'], ['B', 'A'], ['A', 'C'], ['A', 'B', 'C']]) assert.equal(keysMatch({ botKeys: ['A', 'B'] }, { public: pub }), false, pub.join());
  assert.equal(keysMatch({}, { public: ['A'] }), false, 'nothing pinned: closed');
  assert.deepEqual(CFG.chains.solana.botKeys.length, 2);
  assert.throws(() => loadConfig(writeTemp({ ...JSON.parse(fs.readFileSync(new URL('../onboard/onboard.json', import.meta.url), 'utf8')), capHeadroom: 0.5 })), /capHeadroom/);
});

function writeTemp(obj) {
  const p = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'onboard-cfg-')), 'onboard.json');
  fs.writeFileSync(p, JSON.stringify(obj));
  return p;
}

test('off-chain messages: the formats at their bounds, and every malformed envelope is no message', () => {
  assert.deepEqual(I.offchainFormats(Buffer.from(' ~')), [0, 1, 2], 'printable ASCII: 0x20 to 0x7e');
  assert.deepEqual(I.offchainFormats(Buffer.from('a\x7f')), [1, 2]);
  assert.deepEqual(I.offchainFormats(Buffer.from('a\x1f')), [1, 2]);
  assert.deepEqual(I.offchainFormats(Buffer.alloc(0)), [1, 2], 'nothing is not ASCII text');
  assert.deepEqual(I.offchainFormats(Buffer.alloc(1212, 0x61)), [0, 1, 2], '1212 bytes: limited UTF-8 still');
  assert.deepEqual(I.offchainFormats(Buffer.alloc(1213, 0x61)), [0, 2]);
  const text = 'hello';
  const good = I.offchainShort(text, 1);
  assert.deepEqual(I.parseOffchain(good), { layout: 'short', format: 1, signers: [], text });
  const full = (count, extra = Buffer.alloc(0)) => {
    const msg = Buffer.from(text), len = Buffer.alloc(2); len.writeUInt16LE(msg.length);
    return Buffer.concat([I.OFFCHAIN_DOMAIN, Buffer.from([0]), Buffer.alloc(32, 9), Buffer.from([1, count]),
      ...Array.from({ length: count }, () => OWNER.publicKey.toBuffer()), len, msg, extra]);
  };
  assert.deepEqual(I.parseOffchain(full(1)), { layout: 'full', format: 1, signers: [OWNER.publicKey.toBase58()], text });
  for (const [what, b] of [
    ['too short', good.subarray(0, 19)], ['another domain', Buffer.concat([Buffer.from([0xfe]), good.subarray(1)])],
    ['version 1', Buffer.concat([good.subarray(0, 16), Buffer.from([1]), good.subarray(17)])],
    ['no length', good.subarray(0, 19)], ['not UTF-8', Buffer.concat([I.offchainShort('', 2).subarray(0, 18), Buffer.from([1, 0, 0xff])])],
    ['no signer', full(0)], ['a full layout cut before the length', full(1).subarray(0, 51 + 32 + 1)],
    ['a full layout with bytes after the text', full(1, Buffer.from([0]))], ['52 bytes', full(1).subarray(0, 52)],
  ]) assert.equal(I.parseOffchain(b), null, what);
  assert.equal(I.parseOffchain(full(1).subarray(0, 53)), null, 'the full layout\'s shortest frame holds no text');
  const empty = Buffer.concat([I.OFFCHAIN_DOMAIN, Buffer.from([0]), Buffer.alloc(32, 9), Buffer.from([1, 1]), OWNER.publicKey.toBuffer(), Buffer.alloc(2)]);
  assert.deepEqual(I.parseOffchain(empty), { layout: 'full', format: 1, signers: [OWNER.publicKey.toBase58()], text: '' }, 'an empty text ends the frame');
});

test('verify: the reported bytes are read as strict base64; anything else is no signature', async () => {
  const text = 'hello\nNonce: 9', owner = OWNER.publicKey.toBase58();
  const sig = solSign(OWNER, text).toString('base64');
  const b64 = Buffer.from(text).toString('base64');
  assert.equal(await I.verify('solana', owner, text, sig, b64), true);
  // a lenient decoder would read the same bytes through the junk; the server does not
  assert.equal(await I.verify('solana', owner, text, sig, `${b64.slice(0, 4)}!${b64.slice(4)}`), false);
  assert.equal(await I.verify('solana', owner, text, sig, `${b64.slice(0, 4)}-${b64.slice(4)}`), false);
});

test('vaults: the bot\'s book, rendered as Telegram renders it, and the tiles from the same row', async () => {
  const { message } = await import('../telegram_bridge.mjs');
  const book = { t: '2026-10-12T18:53:41Z', event: 'in_band', profile: 'my-pool', pair: 'SOL/USDC', token_a: 'SOL', token_b: 'USDC', price: 107.5,
    last_price: 107.53, lower: 105.4, upper: 109.7, equity_usd: 0, fees_today_usd: 0, fees_total_usd: 0, fees_per_day_usd: 0, apr_pct: 0,
    split: { paid_usd: 0 }, band: { in_range: false, p_exit_24h: 0.5 }, forecast: { p_exit_24h_regime: 0.25, p_exit_24h: 0.9 }, regime: { mode: 'CALM' },
    since_start: { profit_usd: 0 } };
  const daily = { event: 'DAILY', day: '2026-10-11', recentres: 1, fees_usd: 0, vs_hold_usd: 0.02, token_a: 'SOL', token_b: 'USDC' };
  const revoked = { event: 'REVOKED', action: 'stopped' }, exit = { event: 'OWNER_EXIT', reason: 'asked', action: 'stopped' };
  const f = fakes({ deposited: true, feed: { book: { row: book }, DAILY: { row: daily }, REVOKED: { row: revoked }, OWNER_EXIT: { row: exit } } });
  await serve(f, async ({ post }) => {
    await registered(f, post);
    const v = (await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0];
    assert.deepEqual(v.stats.text, message(book), 'the same text as Telegram');
    assert.deepEqual([v.stats.daily, v.stats.revoked, v.stats.exit, v.stats.at], [message(daily), message(revoked), message(exit), book.t]);
    assert.deepEqual(v.stats.tiles, { symbol: 'SOL', price: 107.53, band: [105.4, 109.7], inRange: false, equityUsd: 0, feesTodayUsd: 0, feesTotalUsd: 0,
      paidUsd: 0, feesPerDayUsd: 0, aprPct: 0, mode: 'CALM', pExit24h: 0.25, pnlUsd: 0, event: 'in_band' }, 'zeros are figures, not missing');
  });
  // a row without a band or a forecast: the event says in range or out; no band, no price
  const bare = { event: 'OUT_OF_BAND', pair: 'SOL/USDC', fees_total_usd: 1, equity_usd: 2, price: 100 };
  for (const [row, inRange] of [[bare, false], [{ ...bare, event: 'in_band' }, true], [{ ...bare, event: 'OPEN' }, null]]) {
    const g = fakes({ deposited: true, feed: { book: { row } } });
    await serve(g, async ({ post }) => {
      await registered(g, post);
      const t = (await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0].stats;
      assert.deepEqual([t.tiles.inRange, t.tiles.band, t.tiles.price, t.tiles.mode, t.tiles.pExit24h, t.daily, t.at, t.tiles.pnlUsd],
        [inRange, null, 100, null, null, null, null, null], row.event);
    });
  }
  const h = fakes({ deposited: true, feed: { book: { row: { ...bare, lower: 99 } } } });
  await serve(h, async ({ post }) => {
    await registered(h, post);
    assert.equal((await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0].stats.tiles.band, null, 'half a band is none');
  });
});

test('vaults: the views: a registered vault without money is still being set up; one with a position runs', async () => {
  for (const [over, view] of [[{}, 'setup'], [{ positions: 1 }, 'active'], [{ deposited: true }, 'active'], [{ botActive: false }, 'revoked']]) {
    const f = fakes(over);
    await serve(f, async ({ post }) => {
      await signedUp(post);
      const v0 = (await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0];
      assert.equal(v0.view, 'setup', 'signed, not paid');
      f.payments.set('P', { state: 'ok' });
      await post('pay', { invite: INVITE, name: 'my-pool', payment: 'P' });
      assert.equal((await post('vaults', { invite: INVITE, chain: 'solana', owner: OWNER.publicKey.toBase58() })).json.vaults[0].view, view, JSON.stringify(over));
    });
  }
  // a start with no idle money but a position is a start
  const f = fakes({ positions: 1 });
  await serve(f, async ({ post }) => {
    await registered(f, post);
    assert.equal((await post('start', { invite: INVITE, name: 'my-pool' })).status, 200);
  });
});

test('exit: the reported bytes only when a non-empty string; open exit requests are capped; an exit request expires', async () => {
  const f = fakes({ deposited: true });
  await serve(f, async ({ post }) => {
    await registered(f, post);
    for (const signedMessage of [123, '', null, { a: 1 }]) {
      const it = (await post('exit-intent', { invite: INVITE, name: 'my-pool' })).json;
      const r = await post('exit', { invite: INVITE, nonce: it.nonce, message: it.message, signature: solSign(OWNER, it.message).toString('base64'), signedMessage });
      assert.equal(r.status, 200, `${JSON.stringify(signedMessage)}: no bytes reported, the text itself`);
    }
    const it = (await post('exit-intent', { invite: INVITE, name: 'my-pool' })).json;
    f.tick(CFG.intentTtlS * 1000);
    const late = await post('exit', { invite: INVITE, nonce: it.nonce, message: it.message, signature: solSign(OWNER, it.message).toString('base64') });
    assert.deepEqual([late.status, late.json.error], [400, 'the request expired; sign a new one'], 'at its expiry, expired');
  });
  const cfg = { ...CFG, limits: { ...CFG.limits, openIntents: 2 } };
  const g = fakes({ deposited: true }, cfg);
  await serve(g, async ({ post }) => {
    await registered(g, post);
    assert.equal((await post('exit-intent', { invite: INVITE, name: 'my-pool' })).status, 200);
    assert.equal((await post('exit-intent', { invite: INVITE, name: 'my-pool' })).status, 200);
    assert.equal((await post('exit-intent', { invite: INVITE, name: 'my-pool' })).status, 503, 'two open: the third waits');
  });
});

test('the page and its config: HTML as HTML, the verify origins only with a project id', async () => {
  const f = fakes();
  await serve(f, async ({ base }) => {
    const page = await fetch(`${base}/`);
    assert.match(page.headers.get('content-type'), /text\/html/);
    assert.match(await page.text(), /^<!doctype html>/, 'the page, not a JSON string of it');
    const cfg = await (await fetch(`${base}/api/config`)).json();
    assert.deepEqual(cfg.walletConnect, { projectId: '', relayUrl: CFG.walletConnect.relayUrl, verify: [] });
    assert.deepEqual(cfg.chains.solana.botKeys, [BOT.publicKey.toBase58()]);
  });
  const g = fakes();
  const server = makeServer(CFG, { chains: { solana: g.adapter }, store: g.db.store, inviteHashes: HASH, log: () => {},
    walletConnect: { projectId: 'a'.repeat(32), relayUrl: 'wss://relay.walletconnect.org', verify: ['https://verify.walletconnect.org'] },
    start: async () => ({ code: 0 }), status: async () => ({ out: '' }) });
  await serve({ server }, async ({ base }) => {
    const cfg = await (await fetch(`${base}/api/config`)).json();
    assert.deepEqual(cfg.walletConnect.verify, ['https://verify.walletconnect.org']);
  });
});
