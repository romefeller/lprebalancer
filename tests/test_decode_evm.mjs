// onboard/decode_evm.mjs: the page decodes every EVM transaction before the wallet signs it.
// Each kind built the way the server builds it passes; every change of target, amount,
// recipient, operation, refund or calldata is refused with a plain reason.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fc from 'fast-check';
import { concat, encodeFunctionData, getAddress, maxUint128, maxUint256, parseAbi, toHex } from 'viem';
import * as U from '../chains/evm/polygon.mjs';
import { SAFE_ABI, SENTINEL, preValidated as safePreValidated } from '../chains/evm/vault/safe.mjs';
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
  refused(decodeEvm(revokeTx({ operation: 1 }), REVOKE, PINS), /delegatecall/);
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
