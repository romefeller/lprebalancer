// onboard/decode_evm.mjs: the page decodes every EVM transaction before the wallet signs it.
// Each kind built the way the server builds it passes; every change of target, amount,
// recipient, operation, refund or calldata is refused with a plain reason.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fc from 'fast-check';
import { concat, encodeFunctionData, encodePacked, getAddress, maxUint128, maxUint256, parseAbi, size, slice, toHex } from 'viem';
import * as U from '../chains/evm/polygon.mjs';
import { MODULE_FACTORY_ABI, SAFE_ABI, SAFE_ABI as SAFE_FULL, SAFE_FACTORY_ABI, SENTINEL, preValidated as safePreValidated } from '../chains/evm/vault/safe.mjs';
import { compile, RULES } from '../chains/evm/vault/roles.mjs';
import { configureCalls, fundCalls, ownerSetupCalls, predictSafe } from '../chains/evm/vault/owner_setup.mjs';
import { closeData, ownerExec, regrantCall } from '../chains/evm/vault/exit.mjs';
import { decodeEvm, formatRaw, preValidated } from '../onboard/decode_evm.mjs';
import { fromRaw } from '../onboard/intent.mjs';

const A = (n) => getAddress(`0x${n.toString(16).padStart(40, '0')}`);
const OWNER = A(0xaa01), SAFE = A(0x5afe), ROLES = A(0x7015), BOT = A(0xb07), STRANGER = A(0xbad);
const ZERO = '0x0000000000000000000000000000000000000000';
const PINS = { chainId: 137, wrapped: { WPOL: U.WPOL }, botKey: BOT, npm: U.V3.npm,
  tokens: { POL: { address: null, decimals: 18 }, WPOL: { address: U.WPOL, decimals: 18 }, USDT0: { address: U.USDT0, decimals: 6 } } };
const erc20 = (functionName, args) => encodeFunctionData({ abi: U.ERC20_ABI, functionName, args });
const safeData = (functionName, args) => encodeFunctionData({ abi: SAFE_ABI, functionName, args });
const npm = (functionName, args) => encodeFunctionData({ abi: U.NPM_ABI, functionName, args });
const E18 = 10n ** 18n;

// A Safe transaction with any field changed: the refusals below change one at a time.
function exec({ to, value = 0n, data, operation = 0, safeTxGas = 0n, baseGas = 0n, gasPrice = 0n, gasToken = ZERO, refundReceiver = ZERO,
  signatures = safePreValidated(OWNER), safe = SAFE, outer = 0n }) {
  return { to: safe, value: toHex(outer), data: safeData('execTransaction', [to, value, data, operation, safeTxGas, baseGas, gasPrice, gasToken, refundReceiver, signatures]) };
}
const ok = (r) => { assert.equal(r.ok, true, r.problems.join('; ')); assert.deepEqual(r.problems, []); return r; };
const refused = (r, re) => { assert.equal(r.ok, false, `accepted: ${r.lines.join('; ')}`); if (re) assert.match(r.problems.join('; '), re); return r; };

const WITHDRAW = { kind: 'withdraw', owner: OWNER, safe: SAFE, token: 'USDT0', raw: '1500000' };
const withdrawTx = (o = {}) => exec({ to: U.USDT0, data: erc20('transfer', [OWNER, 1_500_000n]), ...o });
const REVOKE = { kind: 'revoke', owner: OWNER, safe: SAFE, roles: ROLES };
const revokeTx = (o = {}) => exec({ to: SAFE, data: safeData('disableModule', [SENTINEL, ROLES]), ...o });
const CLOSE = { kind: 'close', owner: OWNER, safe: SAFE };
const position = { tokenId: 42n, liquidity: 1000n };

test('the page\'s pre-validated signature is the Safe module\'s; amounts print as intent.fromRaw does', () => {
  fc.assert(fc.property(fc.integer({ min: 1, max: 2 ** 31 }), (n) => {
    assert.equal(preValidated(A(n)), safePreValidated(A(n)));
  }), { numRuns: 50 });
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 30n }), fc.integer({ min: 0, max: 18 }), (raw, d) => {
    assert.equal(formatRaw(raw, d), fromRaw(raw, d));
  }), { numRuns: 300 });
  assert.equal(formatRaw(1_500_000n, 6), '1.5');
  assert.equal(formatRaw(10n, 6), '0.00001');
  assert.equal(formatRaw(7n * E18, 18), '7');
});

test('browser-safe: viem and relative pure modules only, no node:*', () => {
  const seen = new Set();
  const walk = (url) => {
    if (seen.has(url.href)) return;
    seen.add(url.href);
    const src = fs.readFileSync(url, 'utf8');
    for (const [, spec] of src.matchAll(/^\s*(?:import|export)\s[^'"]*from\s+['"]([^'"]+)['"]/gm)) {
      if (spec.startsWith('.')) walk(new URL(spec, url));
      else assert.ok(spec === 'viem', `${url.pathname} imports ${spec}`);
    }
    assert.doesNotMatch(src, /\bprocess\.|require\(|node:/, url.pathname);
  };
  walk(new URL('../onboard/decode_evm.mjs', import.meta.url));
  assert.ok(seen.size >= 2);
});

test('payment: the exact coin amount to the bot\'s key, no calldata', () => {
  const pay = { kind: 'payment', owner: OWNER, lamports: String(4n * E18) };
  const r = ok(decodeEvm({ to: BOT, data: '0x', value: toHex(4n * E18) }, pay, PINS));
  assert.deepEqual(r.lines, [`Pay 4 POL from your wallet ${OWNER} to the bot's key ${BOT}`]);
  ok(decodeEvm({ to: BOT.toLowerCase(), value: 4n * E18 }, pay, PINS));
  ok(decodeEvm({ to: BOT, data: '', value: String(4n * E18) }, pay, PINS));
  refused(decodeEvm({ to: STRANGER, data: '0x', value: toHex(4n * E18) }, pay, PINS), /not the bot's key/);
  refused(decodeEvm({ to: BOT, data: '0x', value: toHex(4n * E18 - 1n) }, pay, PINS), /is 3.999999999999999999 POL, not 4 POL/);
  refused(decodeEvm({ to: BOT, data: '0x00', value: toHex(4n * E18) }, pay, PINS), /carries calldata/);
  refused(decodeEvm({ to: BOT, data: '0xzz', value: toHex(4n * E18) }, pay, PINS), /not hex bytes/);
  refused(decodeEvm({ to: BOT, data: '0x', value: 'lots' }, pay, PINS), /not a number/);
  refused(decodeEvm({ to: BOT, data: '0x', value: 1.5 }, pay, PINS), /not a whole number/);
  refused(decodeEvm({ to: BOT, data: '0x', value: -1 }, pay, PINS), /not a whole number/);
  ok(decodeEvm({ to: BOT, data: '0x', value: 4 }, { ...pay, lamports: '4' }, PINS));
  // zero in every form a wallet may give it
  for (const value of [0, '0', '0x0', null, undefined, '', 0n]) ok(decodeEvm({ to: BOT, data: '0x', value }, { ...pay, lamports: '0' }, PINS));
  // the coin's symbol is the pinned token with no address
  const matic = { ...PINS, tokens: { MATIC: { address: null, decimals: 18 } } };
  assert.match(ok(decodeEvm({ to: BOT, data: '0x', value: toHex(4n * E18) }, pay, matic)).lines[0], /^Pay 4 MATIC /);
  refused(decodeEvm({ to: BOT, data: '0x' }, pay, PINS), /is 0 POL/);
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 24n }), (v) => {
    fc.pre(v !== 4n * E18);
    refused(decodeEvm({ to: BOT, data: '0x', value: toHex(v) }, pay, PINS));
  }), { numRuns: 200 });
});

test('deposit: a wrap of the exact coin, a token transfer of the exact amount to the vault', () => {
  const wrap = { kind: 'deposit', owner: OWNER, safe: SAFE, token: 'POL', raw: String(26n * E18) };
  const wrapData = encodeFunctionData({ abi: U.WRAPPED_ABI, functionName: 'deposit' });
  const r = ok(decodeEvm({ to: U.WPOL, data: wrapData, value: toHex(26n * E18) }, wrap, PINS));
  assert.deepEqual(r.lines, [`Wrap 26 POL into WPOL in your wallet ${OWNER}`]);
  for (const wrapped of [U.WPOL, { address: U.WPOL }, { address: U.WPOL, symbol: 'WPOL' }]) {
    assert.match(ok(decodeEvm({ to: U.WPOL, data: wrapData, value: toHex(26n * E18) }, wrap, { ...PINS, wrapped })).lines[0], /into WPOL/);
  }
  assert.match(ok(decodeEvm({ to: U.WPOL, data: wrapData, value: toHex(26n * E18) }, wrap,
    { ...PINS, wrapped: U.WPOL, tokens: { POL: PINS.tokens.POL } })).lines[0], /into WPOL/, 'the symbol falls back to WPOL');
  refused(decodeEvm({ to: U.WPOL, data: wrapData, value: toHex(26n * E18) }, wrap, { ...PINS, wrapped: {} }), /no wrapped token pinned/);
  refused(decodeEvm({ to: U.WPOL, data: wrapData, value: toHex(26n * E18) }, wrap, { ...PINS, wrapped: undefined }), /no wrapped token pinned/);
  refused(decodeEvm({ to: U.WPOL, data: wrapData, value: toHex(26n * E18 + 1n) }, wrap, PINS), /the wrap is 26.000000000000000001 POL, not 26/);
  refused(decodeEvm({ to: STRANGER, data: wrapData, value: toHex(26n * E18) }, wrap, PINS), /not WPOL/);
  refused(decodeEvm({ to: U.WPOL, data: `${wrapData}00`, value: toHex(26n * E18) }, wrap, PINS), /extra or non-standard bytes/);
  refused(decodeEvm({ to: U.WPOL, data: erc20('transfer', [SAFE, 1n]), value: toHex(26n * E18) }, wrap, PINS), /unknown function/);

  const dep = { kind: 'deposit', owner: OWNER, safe: SAFE, token: 'USDT0', raw: '25000000' };
  const tx = (o = {}) => ({ to: U.USDT0, data: erc20('transfer', [SAFE, 25_000_000n]), value: '0x0', ...o });
  assert.deepEqual(ok(decodeEvm(tx(), dep, PINS)).lines, [`Deposit 25 USDT0 from your wallet ${OWNER} into your vault ${SAFE}`]);
  ok(decodeEvm({ ...tx(), chainId: 137, from: OWNER.toLowerCase() }, dep, PINS));
  refused(decodeEvm({ ...tx(), chainId: 1 }, dep, PINS), /for chain 1, not 137/);
  refused(decodeEvm({ ...tx(), from: STRANGER }, dep, PINS), /sent by/);
  refused(decodeEvm(tx({ data: erc20('transfer', [STRANGER, 25_000_000n]) }), dep, PINS), /not your vault/);
  refused(decodeEvm(tx({ data: erc20('transfer', [SAFE, 25_000_001n]) }), dep, PINS), /is 25.000001 USDT0, not 25/);
  refused(decodeEvm(tx({ data: erc20('approve', [SAFE, 25_000_000n]) }), dep, PINS), /calls approve, not transfer/);
  refused(decodeEvm(tx({ to: U.WPOL }), dep, PINS), /not the USDT0 token/);
  refused(decodeEvm(tx({ value: '0x1' }), dep, PINS), /also sends coin/);
  refused(decodeEvm(tx({ data: `${erc20('transfer', [SAFE, 25_000_000n])}00` }), dep, PINS), /extra or non-standard bytes/);
  refused(decodeEvm(tx({ data: '0xdeadbeef' }), dep, PINS), /unknown function \(selector 0xdeadbeef\)/);
  refused(decodeEvm(tx(), { ...dep, token: 'USDC' }, PINS), /USDC is not one the vault takes/);
  refused(decodeEvm(tx(), { ...dep, token: 'constructor' }, PINS), /not one the vault takes/);
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: maxUint128 }), fc.integer({ min: 1, max: 2 ** 40 }), (raw, who) => {
    const r = decodeEvm(tx({ data: erc20('transfer', [A(who), raw]) }), dep, PINS);
    assert.equal(r.ok, raw === 25_000_000n && A(who) === SAFE);
    assert.equal(r.lines[0], `Deposit ${fromRaw(raw, 6)} USDT0 from your wallet ${OWNER} into your vault ${A(who)}`);
  }), { numRuns: 300 });
});

test('withdraw: a CALL by the owner, no refund, the exact amount to the owner; the coin itself', () => {
  assert.deepEqual(ok(decodeEvm(withdrawTx(), WITHDRAW, PINS)).lines, [`Withdraw 1.5 USDT0 from your vault ${SAFE} to your wallet ${OWNER}`]);
  // built as the server builds it
  ok(decodeEvm(ownerExec({ safe: SAFE, owner: OWNER, to: U.USDT0, data: erc20('transfer', [OWNER, 1_500_000n]) }), WITHDRAW, PINS));
  const cases = [
    [{ operation: 1 }, /delegatecall/],
    [{ safe: STRANGER }, /not your vault/],
    [{ data: erc20('transfer', [STRANGER, 1_500_000n]) }, /not your wallet/],
    [{ data: erc20('transfer', [OWNER, 1_500_001n]) }, /is 1.500001 USDT0, not 1.5/],
    [{ data: erc20('approve', [OWNER, 1_500_000n]) }, /calls approve, not transfer/],
    [{ data: `${erc20('transfer', [OWNER, 1_500_000n])}00` }, /extra or non-standard bytes/],
    [{ safeTxGas: 1n }, /gas refund/], [{ baseGas: 1n }, /gas refund/], [{ gasPrice: 1n }, /gas refund/],
    [{ gasToken: U.USDT0 }, /gas token or refund receiver/], [{ refundReceiver: STRANGER }, /gas token or refund receiver/],
    [{ signatures: safePreValidated(STRANGER) }, /not signed as your wallet/],
    [{ signatures: concat([safePreValidated(OWNER), '0x00']) }, /not signed as your wallet/],
    [{ value: 1n }, /also sends coin/],
    [{ outer: 1n }, /sends 0.000000000000000001 of your coin/],
    [{ to: U.WPOL, data: erc20('transfer', [OWNER, 1_500_000n]) }, /calls 0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270, not the USDT0 token/],
    [{ to: STRANGER }, /not the USDT0 token/],
  ];
  for (const [o, re] of cases) refused(decodeEvm(withdrawTx(o), WITHDRAW, PINS), re);
  const t = withdrawTx();
  refused(decodeEvm({ ...t, data: `${t.data}00` }, WITHDRAW, PINS), /the vault transaction: the calldata carries extra/);
  refused(decodeEvm({ ...t, data: erc20('transfer', [OWNER, 1n]) }, WITHDRAW, PINS), /unknown function/);
  refused(decodeEvm({ ...t, data: safeData('enableModule', [ROLES]) }, WITHDRAW, PINS), /calls enableModule, not execTransaction/);
  // 'all': any amount, but something; the token still fixed
  const all = { ...WITHDRAW, raw: 'all' };
  ok(decodeEvm(withdrawTx({ data: erc20('transfer', [OWNER, 99n]) }), all, PINS));
  refused(decodeEvm(withdrawTx({ data: erc20('transfer', [OWNER, 0n]) }), all, PINS), /moves nothing/);
  refused(decodeEvm(withdrawTx({ to: U.WPOL, data: erc20('transfer', [OWNER, 99n]) }), all, PINS), /not the USDT0 token/);
  // the coin: to the owner with the value, no calldata
  const pol = { ...WITHDRAW, token: 'POL', raw: String(3n * E18) };
  assert.deepEqual(ok(decodeEvm(withdrawTx({ to: OWNER, value: 3n * E18, data: '0x' }), pol, PINS)).lines,
    [`Withdraw 3 POL from your vault ${SAFE} to your wallet ${OWNER}`]);
  refused(decodeEvm(withdrawTx({ to: STRANGER, value: 3n * E18, data: '0x' }), pol, PINS), /POL to 0x0+bad, not your wallet/i);
  refused(decodeEvm(withdrawTx({ to: OWNER, value: 3n * E18 + 1n, data: '0x' }), pol, PINS), /is 3.000000000000000001 POL, not 3/);
  refused(decodeEvm(withdrawTx({ to: OWNER, value: 3n * E18, data: '0x00' }), pol, PINS), /carries calldata/);
  refused(decodeEvm(withdrawTx({ to: OWNER, value: 0n, data: '0x' }), { ...pol, raw: 'all' }, PINS), /moves nothing/);
  ok(decodeEvm(withdrawTx({ to: OWNER, value: 5n, data: '0x' }), { ...pol, raw: 'all' }, PINS));
  // token ALL: each token of the vault and the coin, any amount, still only to the owner
  const every = { ...WITHDRAW, token: 'ALL', raw: 'ALL' };
  ok(decodeEvm(withdrawTx({ data: erc20('transfer', [OWNER, 7n]) }), every, PINS));
  assert.match(ok(decodeEvm(withdrawTx({ to: U.WPOL, data: erc20('transfer', [OWNER, E18]) }), { ...every, token: 'all' }, PINS)).lines[0], /Withdraw 1 WPOL/);
  ok(decodeEvm(withdrawTx({ to: OWNER, value: 5n, data: '0x' }), every, PINS));
  refused(decodeEvm(withdrawTx({ to: U.WPOL, data: erc20('transfer', [STRANGER, E18]) }), every, PINS), /not your wallet/);
  refused(decodeEvm(withdrawTx({ to: STRANGER, value: 5n, data: '0x' }), every, PINS), /not your wallet/);
  refused(decodeEvm(withdrawTx({ to: U.USDC, data: erc20('transfer', [OWNER, 1n]) }), every, PINS), /not your wallet/);
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: maxUint128 }), fc.integer({ min: 1, max: 2 ** 40 }), (raw, who) => {
    const r = decodeEvm(withdrawTx({ data: erc20('transfer', [A(who), raw]) }), WITHDRAW, PINS);
    assert.equal(r.ok, raw === 1_500_000n && A(who) === OWNER);
  }), { numRuns: 300 });
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: maxUint128 }), (v) => {
    assert.equal(decodeEvm(withdrawTx({ to: OWNER, value: v, data: '0x' }), pol, PINS).ok, v === 3n * E18);
  }), { numRuns: 200 });
});

test('revoke and approve: the vault disables or enables exactly the bot\'s Roles module', () => {
  assert.deepEqual(ok(decodeEvm(revokeTx(), REVOKE, PINS)).lines, [`Revoke the bot: your vault ${SAFE} disables its Roles module ${ROLES}`]);
  ok(decodeEvm(revokeTx({ data: safeData('disableModule', [A(0x77), ROLES]) }), REVOKE, PINS));
  refused(decodeEvm(revokeTx({ data: safeData('disableModule', [SENTINEL, STRANGER]) }), REVOKE, PINS), /not the bot's Roles module/);
  refused(decodeEvm(revokeTx({ to: STRANGER }), REVOKE, PINS), /not itself/);
  refused(decodeEvm(revokeTx({ operation: 1 }), REVOKE, PINS), /is a delegatecall \(operation 1\): it could run any code as the vault/);
  refused(decodeEvm(revokeTx({ value: 1n }), REVOKE, PINS), /sends coin/);
  refused(decodeEvm(revokeTx({ data: safeData('enableModule', [ROLES]) }), REVOKE, PINS), /calls enableModule, not disableModule/);
  refused(decodeEvm(revokeTx({ data: erc20('transfer', [OWNER, 1n]) }), REVOKE, PINS), /unknown function/);
  const APPROVE = { kind: 'approve', owner: OWNER, safe: SAFE, roles: ROLES };
  assert.deepEqual(ok(decodeEvm(regrantCall({ safe: SAFE, owner: OWNER, roles: ROLES }), APPROVE, PINS)).lines,
    [`Approve the bot again: your vault ${SAFE} enables its Roles module ${ROLES}`]);
  refused(decodeEvm(regrantCall({ safe: SAFE, owner: OWNER, roles: STRANGER }), APPROVE, PINS), /not the bot's Roles module/);
  refused(decodeEvm(regrantCall({ safe: SAFE, owner: STRANGER, roles: ROLES }), APPROVE, PINS), /not signed as your wallet/);
  refused(decodeEvm(regrantCall({ safe: STRANGER, owner: OWNER, roles: ROLES }), APPROVE, PINS), /not your vault/);
  refused(decodeEvm(revokeTx(), APPROVE, PINS), /calls disableModule, not enableModule/);
  refused(decodeEvm(exec({ to: SAFE, data: safeData('enableModule', [ROLES]), gasPrice: 5n }), APPROVE, PINS), /gas refund/);
});

test('close: a multicall of decrease, collect to the vault and burn on the position manager only', () => {
  const r = ok(decodeEvm(ownerExec({ safe: SAFE, owner: OWNER, to: U.V3.npm, data: closeData(U, SAFE, position) }), CLOSE, PINS));
  assert.deepEqual(r.lines, [`Close positions of your vault ${SAFE}; the tokens stay in the vault`, 'Remove liquidity 1000 from position #42',
    `Collect position #42's tokens to ${SAFE}`, 'Burn the emptied position #42']);
  ok(decodeEvm(ownerExec({ safe: SAFE, owner: OWNER, to: U.V3.npm, data: closeData(U, SAFE, { tokenId: 7n, liquidity: 0n }) }), CLOSE, PINS));
  const multi = (steps) => npm('multicall', [steps]);
  const dec = npm('decreaseLiquidity', [{ tokenId: 42n, liquidity: 1n, amount0Min: 0n, amount1Min: 0n, deadline: maxUint256 }]);
  const col = (recipient) => npm('collect', [{ tokenId: 42n, recipient, amount0Max: maxUint128, amount1Max: maxUint128 }]);
  const burn = npm('burn', [42n]);
  const tx = (data, o = {}) => exec({ to: U.V3.npm, data, ...o });
  ok(decodeEvm(tx(multi([dec, col(SAFE), burn])), CLOSE, PINS));
  refused(decodeEvm(tx(multi([dec, col(STRANGER), burn])), CLOSE, PINS), /collected to 0x0+bad, not your vault/i);
  refused(decodeEvm(tx(multi([dec, col(SAFE), burn, npm('increaseLiquidity', [{ tokenId: 42n, amount0Desired: 1n, amount1Desired: 1n,
    amount0Min: 0n, amount1Min: 0n, deadline: 1n }])])), CLOSE, PINS), /calls increaseLiquidity: only decreaseLiquidity, collect and burn/);
  refused(decodeEvm(tx(multi([dec, `${col(SAFE)}00`, burn])), CLOSE, PINS), /a close step: the calldata carries extra/);
  refused(decodeEvm(tx(multi([dec, erc20('transfer', [STRANGER, 1n])])), CLOSE, PINS), /a close step: an unknown function/);
  refused(decodeEvm(tx(multi([multi([burn])])), CLOSE, PINS), /calls multicall: only/);
  refused(decodeEvm(tx(multi([])), CLOSE, PINS), /does nothing/);
  refused(decodeEvm(tx(burn), CLOSE, PINS), /calls burn, not multicall/);
  refused(decodeEvm(tx(multi([dec, col(SAFE), burn]), { to: U.V3.router }), CLOSE, PINS), /not the position manager/);
  refused(decodeEvm(tx(multi([dec, col(SAFE), burn]), { operation: 1 }), CLOSE, PINS), /delegatecall/);
  refused(decodeEvm(tx(multi([dec, col(SAFE), burn]), { value: 1n }), CLOSE, PINS), /sends coin/);
  refused(decodeEvm(tx(multi([dec, col(SAFE), burn]), { safe: STRANGER }), CLOSE, PINS), /not your vault/);
  fc.assert(fc.property(fc.integer({ min: 1, max: 2 ** 40 }), (who) => {
    assert.equal(decodeEvm(tx(multi([col(A(who))])), CLOSE, PINS).ok, A(who) === SAFE);
  }), { numRuns: 200 });
});

test('anything else: an unknown kind, no transaction, no owner, a target that is no address', () => {
  refused(decodeEvm(withdrawTx(), { ...WITHDRAW, kind: 'mint' }, PINS), /no transaction of kind mint/);
  refused(decodeEvm(withdrawTx(), { ...WITHDRAW, kind: 'toString' }, PINS), /no transaction of kind toString/);
  refused(decodeEvm(withdrawTx(), null, PINS), /of kind undefined/);
  refused(decodeEvm(null, WITHDRAW, PINS), /no transaction/);
  refused(decodeEvm({ ...withdrawTx(), to: 'nowhere' }, WITHDRAW, PINS), /not an address/);
  refused(decodeEvm(withdrawTx(), { ...WITHDRAW, owner: undefined }, PINS), /knows no wallet/);
  assert.throws(() => decodeEvm(withdrawTx(), WITHDRAW, null), TypeError, 'a page with no pins is a bug, not a refusal');
  // any change to a byte of a good transaction is refused
  const good = withdrawTx();
  const opAt = 2 + 8 + 3 * 64;                 // the operation word, a uint8
  const wide = good.data.slice(0, opAt) + (256).toString(16).padStart(64, '0') + good.data.slice(opAt + 64);
  refused(decodeEvm({ ...good, data: wide }, WITHDRAW, PINS), /the vault transaction: the calldata carries extra or non-standard bytes/);
  fc.assert(fc.property(fc.integer({ min: 2, max: good.data.length - 1 }), fc.constantFrom(...'0123456789abcdef'), (i, h) => {
    fc.pre(good.data[i].toLowerCase() !== h.toLowerCase());
    const data = good.data.slice(0, i) + h + good.data.slice(i + 1);
    assert.equal(decodeEvm({ ...good, data }, WITHDRAW, PINS).ok, false, `byte ${i}`);
  }), { numRuns: 300 });
});

// parseAbi keeps this file's helpers honest: a Safe call the decoder does not know
test('a Safe call the decoder does not know is an unknown function', () => {
  const other = encodeFunctionData({ abi: parseAbi(['function addOwnerWithThreshold(address owner, uint256 threshold)']), functionName: 'addOwnerWithThreshold', args: [STRANGER, 1n] });
  refused(decodeEvm(exec({ to: SAFE, data: other }), REVOKE, PINS), /the module change: an unknown function/);
  refused(decodeEvm({ to: SAFE, value: '0x0', data: other }, REVOKE, PINS), /the vault transaction: an unknown function/);
});

// --- the owner's own vault: createVault (the Safe, then its set-up) and fund ------------------

const VAULT_PINS = { ...PINS, vault: { ...U.VAULT }, router: U.V3.router, stables: [...U.STABLES], venue: U.DEX,
  pool: { token0: U.WPOL, token1: U.USDT0, fee: 500 } };
const PROFIT = A(0x9), FEE = A(0x8), SALT = 7n;
const COMPILED = compile(U.DEX, { npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools: [VAULT_PINS.pool], payouts: [PROFIT, FEE] });
const SETUP = ownerSetupCalls({ U, owner: OWNER, bot: BOT, compiled: COMPILED, saltNonce: SALT });
const CREATE = { kind: 'createVault', owner: OWNER, safe: SETUP.safe, saltNonce: String(SALT), profitWallet: PROFIT, feeWallet: FEE };
const MULTI = parseAbi(['function multiSend(bytes transactions) payable']);
const packedOf = (list) => concat(list.map(c => encodePacked(['uint8', 'address', 'uint256', 'uint256', 'bytes'], [c.operation, c.to, c.value, BigInt(size(c.data)), c.data])));
const BASE = configureCalls({ U, safe: SETUP.safe, roles: SETUP.roles, compiled: COMPILED, bot: BOT, saltNonce: SALT }).map(c => ({ operation: 0, value: 0n, ...c }));
// The set-up transaction with any part changed: its calls (each {operation, to, value, data}) or the outer Safe fields.
function configure({ calls = BASE, safe = SETUP.safe, operation = 1, to = U.VAULT.multiSendCallOnly, signer = OWNER, gasPrice = 0n, value = 0n, outer = 0n, packed } = {}) {
  const data = encodeFunctionData({ abi: MULTI, functionName: 'multiSend', args: [packed ?? packedOf(calls)] });
  return { to: safe, value: toHex(outer), data: safeData('execTransaction', [to, value, data, operation, 0n, 0n, gasPrice, ZERO, ZERO, safePreValidated(signer)]) };
}
const setupData = (o = {}) => encodeFunctionData({ abi: SAFE_FULL, functionName: 'setup',
  args: [o.owners ?? [OWNER], o.threshold ?? 1n, o.to ?? ZERO, o.data ?? '0x', o.handler ?? U.VAULT.fallbackHandler, o.token ?? ZERO, o.payment ?? 0n, o.receiver ?? ZERO] });
const createTx = (o = {}) => ({ to: o.factory ?? U.VAULT.safeFactory, value: toHex(o.value ?? 0n), data: o.raw ?? encodeFunctionData({ abi: SAFE_FACTORY_ABI,
  functionName: 'createProxyWithNonce', args: [o.singleton ?? U.VAULT.safeSingleton, o.initializer ?? setupData(o), o.salt ?? SALT] }) });

test('createVault: the owner\'s two transactions as built pass, with what they do in plain words', () => {
  assert.deepEqual(createTx(), SETUP.txs[0], 'the helpers build what ownerSetupCalls builds');
  assert.deepEqual(configure().data, SETUP.txs[1].data);
  const r = ok(decodeEvm(SETUP.txs, CREATE, VAULT_PINS));
  assert.deepEqual(r.lines.slice(0, 2), [`Create your vault ${SETUP.safe}: a Safe 1.4.1 with your wallet ${OWNER} its only owner`,
    `Set your vault up: create and enable the bot's Roles module ${SETUP.roles}`]);
  assert.match(r.lines[2], new RegExp(`^Give the bot's key ${BOT} only these calls: approve the position manager or the router; `));
  assert.equal(r.lines[3], `Pay-outs only to ${PROFIT} and ${FEE}`);
  ok(decodeEvm(SETUP.txs.map(t => ({ ...t, chainId: 137, from: OWNER.toLowerCase() })), { ...CREATE, safe: SETUP.safe.toLowerCase() }, VAULT_PINS));
  // a resume: the Safe exists (the set-up alone), the module exists too (without its deployment)
  assert.equal(ok(decodeEvm([SETUP.txs[1]], CREATE, VAULT_PINS)).lines.length, 3);
  const later = ownerSetupCalls({ U, owner: OWNER, bot: BOT, compiled: COMPILED, saltNonce: SALT, rolesDeployed: true });
  assert.equal(ok(decodeEvm(later.txs, CREATE, VAULT_PINS)).lines[1], `Set your vault up: enable the bot's Roles module ${SETUP.roles}`);
  // the operator's own vault: one payout wallet
  const solo = ownerSetupCalls({ U, owner: OWNER, bot: BOT, saltNonce: SALT,
    compiled: compile(U.DEX, { npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools: [VAULT_PINS.pool], payouts: [PROFIT] }) });
  assert.equal(ok(decodeEvm(solo.txs, { ...CREATE, feeWallet: undefined }, VAULT_PINS)).lines[3], `Pay-outs only to ${PROFIT}`);
  refused(decodeEvm(solo.txs, CREATE, VAULT_PINS), /is not what the pinned rules give/);
  // the rules the page passes, when it passes its own
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, rules: { ...RULES, payout: [] } }), /makes \d+ calls, not \d+/);
});

test('createVault: another owner, threshold, handler, salt, factory or copy of the Safe is refused', () => {
  const two = (create) => decodeEvm([create, SETUP.txs[1]], CREATE, VAULT_PINS);
  refused(two(createTx({ owners: [OWNER, STRANGER] })), /owners are .*, not your wallet alone/);
  refused(two(createTx({ owners: [STRANGER] })), /not your wallet alone/);
  refused(two(createTx({ owners: [] })), /owners are none/);
  refused(two(createTx({ threshold: 2n })), /needs 2 signatures, not 1/);
  refused(two(createTx({ to: STRANGER })), /set-up runs a call/);
  refused(two(createTx({ data: '0x12' })), /set-up runs a call/);
  refused(two(createTx({ handler: STRANGER })), /fallback handler is .*, not/);
  refused(two(createTx({ token: STRANGER })), /pays someone/);
  refused(two(createTx({ payment: 1n })), /pays someone/);
  refused(two(createTx({ receiver: STRANGER })), /pays someone/);
  refused(two(createTx({ salt: SALT + 1n })), /salt is 8, not 7/);
  refused(two(createTx({ singleton: STRANGER })), /a copy of .*, not Safe 1.4.1/);
  refused(two(createTx({ factory: STRANGER })), /not the Safe factory/);
  refused(two(createTx({ value: 1n })), /creation sends coin/);
  refused(two({ ...createTx(), data: `${createTx().data}00` }), /the vault creation: the calldata carries extra/);
  refused(two(createTx({ initializer: safeData('enableModule', [STRANGER]) })), /set-up calls enableModule, not setup/);
  refused(two(createTx({ initializer: '0xdeadbeef' })), /the vault's set-up: an unknown function/);
  refused(two(createTx({ raw: erc20('transfer', [OWNER, 1n]) })), /the vault creation: an unknown function/);
  // a changed initializer is another vault: the address the page shows is the one it checked
  for (const o of [{ owners: [STRANGER] }, { threshold: 2n }, { handler: STRANGER }]) {
    assert.notEqual(predictSafe({ U, initializer: setupData(o), saltNonce: SALT }), SETUP.safe);
    assert.match(two(createTx(o)).problems.join(), /the vault created is not your vault/);
  }
  refused(decodeEvm(SETUP.txs, { ...CREATE, safe: STRANGER }, VAULT_PINS), /your vault would be .*, not 0x0+bad/i);
  refused(decodeEvm(SETUP.txs, { ...CREATE, saltNonce: undefined }, VAULT_PINS), /knows no salt/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, saltNonce: '-1' }, VAULT_PINS), /knows no salt/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, saltNonce: '8' }, VAULT_PINS), /your vault would be/);
  refused(decodeEvm([...SETUP.txs, SETUP.txs[1]], CREATE, VAULT_PINS), /3 transactions, not 2/);
  for (const k of ['safeSingleton', 'safeFactory', 'fallbackHandler', 'multiSendCallOnly', 'rolesMastercopy', 'moduleFactory']) {
    refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, vault: { ...U.VAULT, [k]: undefined } }), new RegExp(`no ${k} pinned`));
  }
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, vault: undefined }), /no safeSingleton pinned/);
});

test('createVault: the set-up is exactly the module, its enabling and the role for this bot and these payout wallets', () => {
  const conf = (o) => decodeEvm([SETUP.txs[0], configure(o)], CREATE, VAULT_PINS);
  ok(conf({}));
  refused(conf({ calls: [...BASE, { operation: 0, value: 0n, to: U.USDT0, data: erc20('transfer', [STRANGER, 1n]) }] }), /makes \d+ calls, not \d+.*call \d+ \(0xa9059cbb on 0x/);
  refused(conf({ calls: BASE.slice(0, -1) }), /makes \d+ calls, not \d+/);
  refused(conf({ calls: [BASE[0], ...BASE.slice(2)] }), /call 2 \(assignRoles on .*\) is not what the pinned rules give/);
  const stranger = configureCalls({ U, safe: SETUP.safe, roles: SETUP.roles, compiled: COMPILED, bot: STRANGER, saltNonce: SALT }).map(c => ({ operation: 0, value: 0n, ...c }));
  refused(conf({ calls: stranger }), /call 3 \(assignRoles on .*\) is not what the pinned rules give/);
  refused(conf({ calls: BASE.map((c, i) => (i === 2 ? { ...c, operation: 1 } : c)) }), /call 3 is a delegatecall/);
  refused(conf({ calls: BASE.map((c, i) => (i === 2 ? { ...c, value: 1n } : c)) }), /call 3 sends coin/);
  refused(conf({ calls: BASE.map((c, i) => (i === 1 ? { ...c, to: STRANGER } : c)) }), /call 2 \(enableModule on 0x0+bad\) is not/i);
  refused(conf({ calls: BASE.map((c, i) => (i === 1 ? { ...c, data: safeData('enableModule', [STRANGER]) } : c)) }), /call 2 \(enableModule/);
  refused(conf({ calls: BASE.map((c, i) => (i === 0 ? { ...c, data: encodeFunctionData({ abi: MODULE_FACTORY_ABI, functionName: 'deployModule', args: [STRANGER, '0x', SALT] }) } : c)) }),
    /call 1 \(deployModule on .*\) is not/);
  refused(conf({ operation: 0 }), /operation 0, not a delegatecall of MultiSendCallOnly/);
  refused(conf({ operation: 2 }), /operation 2/);
  refused(conf({ to: STRANGER }), /runs 0x0+bad, not MultiSendCallOnly/i);
  refused(conf({ signer: STRANGER }), /not signed as your wallet/);
  refused(conf({ gasPrice: 1n }), /gas refund/);
  refused(conf({ value: 1n }), /set-up sends coin from the vault/);
  refused(conf({ outer: 1n }), /sends 0.000000000000000001 of your coin/);
  refused(conf({ safe: STRANGER }), /not your vault/);
  refused(conf({ packed: '0x00' }), /cut short/);
  refused(conf({ packed: '0x' }), /makes 0 calls/);
  const full = packedOf(BASE);
  refused(conf({ packed: slice(full, 0, size(full) - 1) }), /cut short/);
  refused(conf({ packed: concat([full, '0x00']) }), /cut short/);
  refused(conf({ packed: concat([full, slice(full, 0, 85)]) }), /cut short/);
  refused(conf({ calls: [{ operation: 0, value: 0n, to: SETUP.safe, data: '0x' }] }), /makes 1 calls.*call 1 \(0x on/);
  refused(conf({ calls: [...BASE, { operation: 0, value: 0n, to: SETUP.safe, data: '0x' }] }), /makes \d+ calls/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, profitWallet: STRANGER }, VAULT_PINS), /is not what the pinned rules give/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, profitWallet: 'nobody' }, VAULT_PINS), /knows no payout wallet/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, profitWallet: undefined }, VAULT_PINS), /knows no payout wallet/);
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, botKey: STRANGER }), /is not what the pinned rules give/);
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, pool: { ...VAULT_PINS.pool, fee: 3000 } }), /is not what the pinned rules give/);
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, router: STRANGER }), /is not what the pinned rules give/);
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, stables: [] }), /is not what the pinned rules give/);
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, stables: undefined }), /is not what the pinned rules give/);
  refused(decodeEvm(SETUP.txs, CREATE, { ...VAULT_PINS, npm: STRANGER }), /is not what the pinned rules give/);
  // any change to a byte of either transaction is refused
  for (const at of [0, 1]) {
    const good = SETUP.txs[at];
    fc.assert(fc.property(fc.integer({ min: 2, max: good.data.length - 1 }), fc.constantFrom(...'0123456789abcdef'), (i, h) => {
      fc.pre(good.data[i].toLowerCase() !== h.toLowerCase());
      const txs = [...SETUP.txs];
      txs[at] = { ...good, data: good.data.slice(0, i) + h + good.data.slice(i + 1) };
      assert.equal(decodeEvm(txs, CREATE, VAULT_PINS).ok, false, `tx ${at} byte ${i}`);
    }), { numRuns: 150 });
  }
});

test('fund: the deposit (a wrap first for POL), then the bot\'s gas; nothing else, in this order', () => {
  const FUND = { kind: 'fund', owner: OWNER, safe: SAFE, token: 'POL', raw: String(26n * E18), gasWei: String(4n * E18) };
  const pol = fundCalls({ U, safe: SAFE, token: 'POL', amountRaw: 26n * E18, gasWei: 4n * E18, botKey: BOT });
  assert.deepEqual(ok(decodeEvm(pol, FUND, PINS)).lines, [`Wrap 26 POL into WPOL in your wallet ${OWNER}`,
    `Deposit 26 WPOL from your wallet ${OWNER} into your vault ${SAFE}`, `Pay 4 POL from your wallet ${OWNER} to the bot's key ${BOT}`]);
  const usdt = fundCalls({ U, safe: SAFE, token: 'USDT0', amountRaw: 25_000_000n, gasWei: 4n * E18, botKey: BOT });
  const FUSD = { ...FUND, token: 'USDT0', raw: '25000000' };
  assert.deepEqual(ok(decodeEvm(usdt, FUSD, PINS)).lines, [`Deposit 25 USDT0 from your wallet ${OWNER} into your vault ${SAFE}`,
    `Pay 4 POL from your wallet ${OWNER} to the bot's key ${BOT}`]);
  refused(decodeEvm([usdt[1], usdt[0]], FUSD, PINS), /not the USDT0 token/);
  refused(decodeEvm([pol[0], pol[2], pol[1]], FUND, PINS), /not the WPOL token/);
  refused(decodeEvm(usdt.slice(0, 1), FUSD, PINS), /the funding is 1 transactions, not 2/);
  refused(decodeEvm([...usdt, usdt[1]], FUSD, PINS), /the funding is 3 transactions, not 2/);
  refused(decodeEvm(pol.slice(1), FUND, PINS), /the funding is 2 transactions, not 3/);
  refused(decodeEvm([usdt[0], { ...usdt[1], to: STRANGER }], FUSD, PINS), /not the bot's key/);
  refused(decodeEvm([usdt[0], { ...usdt[1], value: toHex(4n * E18 + 1n) }], FUSD, PINS), /payment is 4.000000000000000001 POL, not 4 POL/);
  refused(decodeEvm(usdt, { ...FUSD, gasWei: undefined }, PINS), /payment is 4 POL, not 0 POL/);
  refused(decodeEvm([{ ...usdt[0], data: erc20('transfer', [STRANGER, 25_000_000n]) }, usdt[1]], FUSD, PINS), /not your vault/);
  refused(decodeEvm([pol[0], { ...pol[1], data: erc20('transfer', [SAFE, 26n * E18 - 1n]) }, pol[2]], FUND, PINS), /is 25.999999999999999999 WPOL, not 26/);
  refused(decodeEvm([{ ...pol[0], value: toHex(1n) }, pol[1], pol[2]], FUND, PINS), /the wrap is 0.000000000000000001 POL, not 26/);
  refused(decodeEvm(usdt, { ...FUSD, token: 'DAI' }, PINS), /DAI is not one the vault takes/);
  refused(decodeEvm(usdt, { ...FUSD, token: 'constructor' }, PINS), /not one the vault takes/);
  refused(decodeEvm(usdt.map(t => ({ ...t, chainId: 1 })), FUSD, PINS), /for chain 1, not 137/);
  refused(decodeEvm([usdt[0], { ...usdt[1], from: STRANGER }], FUSD, PINS), /sent by 0x0+bad/i);
});

test('a list kind takes a list, the others one transaction', () => {
  refused(decodeEvm(SETUP.txs[0], CREATE, VAULT_PINS), /a createVault is a list of transactions/);
  refused(decodeEvm([withdrawTx()], WITHDRAW, PINS), /one transaction, not a list/);
  refused(decodeEvm([], CREATE, VAULT_PINS), /no transaction/);
  refused(decodeEvm([null], CREATE, VAULT_PINS), /no transaction/);
  refused(decodeEvm([{ ...SETUP.txs[0], to: 'x' }, SETUP.txs[1]], CREATE, VAULT_PINS), /goes to x, not an address/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, owner: undefined }, VAULT_PINS), /knows no wallet/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, kind: 'toString' }, VAULT_PINS), /no transaction of kind toString/);
  refused(decodeEvm(SETUP.txs, { ...CREATE, kind: undefined }, VAULT_PINS), /no transaction of kind undefined/);
});
