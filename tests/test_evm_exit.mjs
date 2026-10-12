// The owner's exit from a Polygon Safe vault without a node: chains/evm/vault/exit.mjs (the
// positions, the close and the re-grant) and onboard/polygon.mjs's owner transactions,
// deposit check and confirm, on a stub client. tests/test_vault_exit_polygon_fork.mjs runs
// them on the real contracts.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fc from 'fast-check';
import { concat, decodeFunctionData, encodeAbiParameters, encodeEventTopics, getAddress, maxUint128, maxUint256, pad, toFunctionSelector } from 'viem';
import * as U from '../chains/evm/polygon.mjs';
import { SAFE_ABI, SENTINEL, preValidated } from '../chains/evm/vault/safe.mjs';
import { ROLES_ABI, roleKey } from '../chains/evm/vault/roles.mjs';
import { closeData, ownerCloseCalls, ownerExec, regrantCall, vaultPositions } from '../chains/evm/vault/exit.mjs';
import * as P from '../onboard/polygon.mjs';
import { Invalid, fromRaw } from '../onboard/intent.mjs';
import { decodeEvm } from '../onboard/decode_evm.mjs';

const A = (n) => getAddress(`0x${n.toString(16).padStart(40, '0')}`);
const OWNER = A(0xaa01), SAFE = A(0x5afe), ROLES = A(0x7015), BOT = A(0xb07), STRANGER = A(0xbad);
const POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7';
const VENUE = 'uniswap-v3-polygon';
const C = P.resolve(JSON.parse(fs.readFileSync(new URL('../onboard/onboard.json', import.meta.url), 'utf8')).chains.polygon);
const ROW = { settings: SAFE.toLowerCase(), venues: { [VENUE]: { policies: [ROLES.toLowerCase()], pools: [POOL.toLowerCase()], payouts: [] } } };
const PINS = { chainId: 137, wrapped: U.WPOL, botKey: BOT, npm: U.V3.npm,
  tokens: { POL: { address: null, decimals: 18 }, WPOL: { address: U.WPOL, decimals: 18 }, USDT0: { address: U.USDT0, decimals: 6 } } };
const E18 = 10n ** 18n;
const PASSED = toFunctionSelector('ModuleTransactionFailed()');
const NO_MEMBER = toFunctionSelector('NoMembership()');
const H = `0x${'ab'.repeat(32)}`;

// A position as NonfungiblePositionManager.positions returns it.
const pos = ({ token0 = U.WPOL, token1 = U.USDT0, fee = 500, liquidity = 0n, owed0 = 0n, owed1 = 0n } = {}) =>
  [0n, A(0), token0, token1, fee, -10, 10, liquidity, 0n, 0n, owed0, owed1];

// A stub public client: the Safe, its Roles module, the pool, the position manager, balances.
function stub(o = {}) {
  const s = { owners: [OWNER], threshold: 1n, modules: [ROLES], rolesOwner: SAFE, avatar: SAFE, target: SAFE,
    balances: {}, pol: 0n, held: [], role: () => '0x', receipt: null, head: 100n, ...o };
  const same = (a, b) => a.toLowerCase() === b.toLowerCase();
  return {
    s,
    async readContract({ address, functionName, args }) {
      switch (functionName) {
        case 'getOwners': return s.owners;
        case 'getThreshold': return s.threshold;
        case 'getModulesPaginated': return [s.modules, SENTINEL];
        case 'getStorageAt': return pad('0x00');
        case 'owner': return s.rolesOwner;
        case 'avatar': return s.avatar;
        case 'target': return s.target;
        case 'factory': return U.V3.factory;
        case 'getPool': return POOL;
        case 'token0': return U.WPOL;
        case 'token1': return U.USDT0;
        case 'fee': return 500;
        case 'balanceOf':
          if (same(address, U.V3.npm)) { assert.ok(same(args[0], SAFE)); return BigInt(s.held.length); }
          return s.balances[getAddress(address)]?.[getAddress(args[0])] ?? 0n;
        case 'tokenOfOwnerByIndex':
          assert.ok(same(args[0], SAFE));
          if (args[1] >= BigInt(s.held.length)) throw new Error('index out of range');
          return s.held[Number(args[1])].id;
        case 'positions': return s.held.find(h => h.id === args[0]).p;
        default: throw new Error(`stub: ${functionName}`);
      }
    },
    async getBalance({ address }) { return same(address, SAFE) ? s.pol : 0n; },
    async request({ method, params }) {
      assert.equal(method, 'eth_call');
      s.probe = params[0];
      const d = s.role(params[0]);
      if (d === '0x') return '0x';
      if (d instanceof Error) throw d;
      throw Object.assign(new Error('execution reverted'), { cause: { data: d } });
    },
    async getTransactionReceipt({ hash }) {
      s.asked = hash;
      if (s.receipt instanceof Error) throw s.receipt;
      if (!s.receipt) throw new Error('Transaction receipt with hash "0x" could not be found.');
      return s.receipt;
    },
    async getBlockNumber() { return s.head; },
  };
}
const keys = (pub) => ({ pub, account: { address: BOT } });
const txs = (pub, a) => P.ownerTxs(keys(pub), C, ROW, { owner: OWNER, ...a });
const inner = (tx) => {
  const d = decodeFunctionData({ abi: SAFE_ABI, data: tx.data });
  assert.equal(d.functionName, 'execTransaction');
  return d.args;
};

test('vaultPositions: the Safe\'s positions on the pool only, with liquidity and tokens owed', async () => {
  const held = [
    { id: 7n, p: pos({ liquidity: 1000n, owed0: 3n, owed1: 4n }) },
    { id: 8n, p: pos({ fee: 3000, liquidity: 1n }) },
    { id: 9n, p: pos({ token0: U.USDC, liquidity: 1n }) },
    { id: 10n, p: pos({ token1: U.USDC, liquidity: 1n }) },
    { id: 11n, p: pos({ token0: U.WPOL.toLowerCase() }) },
  ];
  const pub = stub({ held });
  assert.deepEqual(await vaultPositions(pub, { U, safe: SAFE, pool: POOL }),
    [{ tokenId: 7n, liquidity: 1000n, owed0: 3n, owed1: 4n }, { tokenId: 11n, liquidity: 0n, owed0: 0n, owed1: 0n }]);
  assert.deepEqual(await vaultPositions(stub(), { U, safe: SAFE, pool: POOL }), []);
  assert.deepEqual(await P.positions(keys(pub), C, ROW), [
    { pool: POOL, tokenId: '7', liquidity: '1000', owed0: '3', owed1: '4' }, { pool: POOL, tokenId: '11', liquidity: '0', owed0: '0', owed1: '0' }]);
});

test('closeData: decrease all, collect everything to the Safe, burn; an emptied position skips the decrease', () => {
  const steps = (d) => decodeFunctionData({ abi: U.NPM_ABI, data: d }).args[0].map(x => decodeFunctionData({ abi: U.NPM_ABI, data: x }));
  const s = steps(closeData(U, SAFE, { tokenId: 7n, liquidity: 1000n }));
  assert.deepEqual(s.map(x => x.functionName), ['decreaseLiquidity', 'collect', 'burn']);
  assert.deepEqual(s[0].args[0], { tokenId: 7n, liquidity: 1000n, amount0Min: 0n, amount1Min: 0n, deadline: maxUint256 });
  assert.deepEqual(s[1].args[0], { tokenId: 7n, recipient: SAFE, amount0Max: maxUint128, amount1Max: maxUint128 });
  assert.deepEqual(s[2].args, [7n]);
  assert.deepEqual(steps(closeData(U, SAFE, { tokenId: 7n, liquidity: 0n })).map(x => x.functionName), ['collect', 'burn']);
  assert.deepEqual(steps(closeData(U, SAFE, { tokenId: 7n, liquidity: 1n })).map(x => x.functionName), ['decreaseLiquidity', 'collect', 'burn']);
});

test('ownerCloseCalls and regrantCall: Safe CALLs by the owner, no refund; the page decodes each', async () => {
  const pub = stub({ held: [{ id: 7n, p: pos({ liquidity: 1000n }) }, { id: 8n, p: pos() }] });
  const calls = await ownerCloseCalls(pub, { U, safe: SAFE, owner: OWNER, pool: POOL });
  assert.equal(calls.length, 2);
  for (const [i, c] of calls.entries()) {
    assert.equal(c.to, SAFE);
    assert.equal(c.value, '0x0');
    const [to, value, data, op, g1, g2, g3, gt, rr, sig] = inner(c);
    assert.deepEqual([to, value, op, g1, g2, g3, gt, rr, sig], [U.V3.npm, 0n, 0, 0n, 0n, 0n, A(0), A(0), preValidated(OWNER).toLowerCase()]);
    assert.equal(data, closeData(U, SAFE, { tokenId: [7n, 8n][i], liquidity: [1000n, 0n][i] }));
    assert.equal(decodeEvm(c, { kind: 'close', owner: OWNER, safe: SAFE }, PINS).ok, true);
  }
  assert.deepEqual(await ownerCloseCalls(stub(), { U, safe: SAFE, owner: OWNER, pool: POOL }), []);
  const r = regrantCall({ safe: SAFE.toLowerCase(), owner: OWNER, roles: ROLES.toLowerCase() });
  assert.equal(r.to, SAFE);
  const [to, , data, op] = inner(r);
  assert.deepEqual([to, op], [SAFE, 0]);
  assert.deepEqual(decodeFunctionData({ abi: SAFE_ABI, data }), { functionName: 'enableModule', args: [ROLES] });
  assert.equal(decodeEvm(r, { kind: 'approve', owner: OWNER, safe: SAFE, roles: ROLES }, PINS).ok, true);
  const n = ownerExec({ safe: SAFE, owner: OWNER, to: OWNER, data: '0x', value: 5n });
  assert.deepEqual(inner(n).slice(0, 3), [OWNER, 5n, '0x']);
  assert.equal(n.value, '0x0', 'the owner sends no coin: the Safe pays it');
});

test('ownerTxs: only an owner, only at threshold 1', async () => {
  await assert.rejects(txs(stub({ owners: [STRANGER] }), { action: 'revoke' }), (e) => e instanceof Invalid && /is not an owner/.test(e.message));
  await assert.rejects(txs(stub({ threshold: 2n, owners: [OWNER, STRANGER] }), { action: 'revoke' }), /needs 2 owner signatures/);
  await assert.rejects(txs(stub(), { action: 'mint' }), /action mint/);
  // an owner in other case is the owner
  assert.equal((await P.ownerTxs(keys(stub()), C, ROW, { owner: OWNER.toLowerCase(), action: 'revoke' })).transactions.length, 1);
});

test('ownerTxs withdraw: one token, its all, and ALL: every token and the POL to the owner', async () => {
  const balances = { [U.USDT0]: { [SAFE]: 2_500_000n }, [U.WPOL]: { [SAFE]: 3n * E18 } };
  const pub = stub({ balances, pol: 5n });
  const one = await txs(pub, { action: 'withdraw', token: 'USDT0', amount: '1.5' });
  assert.equal(one.summary, `withdraw 1.5 USDT0 from the vault ${SAFE} to ${OWNER}`);
  assert.equal(decodeEvm(one.transactions[0], { kind: 'withdraw', owner: OWNER, safe: SAFE, token: 'USDT0', raw: '1500000' }, PINS).ok, true);
  assert.match((await txs(pub, { action: 'withdraw', token: 'USDT0', amount: 'all' })).summary, /withdraw 2.5 USDT0/);
  assert.match((await txs(pub, { action: 'withdraw', token: 'USDT0', amount: '2.5' })).summary, /withdraw 2.5 USDT0/, 'all of it, exactly');
  await assert.rejects(txs(pub, { action: 'withdraw', token: 'USDT0', amount: '2.500001' }), /the vault holds 2.5 USDT0/);
  await assert.rejects(txs(pub, { action: 'withdraw', token: 'USDT0', amount: '0' }), /the vault holds 2.5 USDT0/);
  await assert.rejects(txs(pub, { action: 'withdraw', token: 'USDC', amount: '1' }), /token USDC/);
  const pol = await txs(pub, { action: 'withdraw', token: 'POL', amount: 'all' });
  assert.deepEqual(inner(pol.transactions[0]).slice(0, 3), [OWNER, 5n, '0x']);

  const all = await txs(pub, { action: 'withdraw', token: 'ALL', amount: 'ALL' });
  assert.equal(all.summary, `withdraw everything from the vault ${SAFE} to ${OWNER}: 3 WPOL, 2.5 USDT0, 0.000000000000000005 POL`);
  assert.equal(all.chainId, 137);
  assert.equal(all.transactions.length, 3);
  const [w, u, p] = all.transactions.map(inner);
  assert.deepEqual([w[0], decodeFunctionData({ abi: U.ERC20_ABI, data: w[2] }).args], [U.WPOL, [OWNER, 3n * E18]]);
  assert.deepEqual([u[0], decodeFunctionData({ abi: U.ERC20_ABI, data: u[2] }).args], [U.USDT0, [OWNER, 2_500_000n]]);
  assert.deepEqual(p.slice(0, 3), [OWNER, 5n, '0x']);
  for (const t of all.transactions) assert.equal(decodeEvm(t, { kind: 'withdraw', owner: OWNER, safe: SAFE, token: 'ALL', raw: 'all' }, PINS).ok, true);
  assert.equal((await txs(pub, { action: 'withdraw', token: 'all', amount: 'all' })).transactions.length, 3);
  await assert.rejects(txs(pub, { action: 'withdraw', token: 'ALL', amount: '1' }), /token ALL takes amount ALL/);
  // only what is there: no zero transfer
  const some = await txs(stub({ balances: { [U.USDT0]: { [SAFE]: 1n } } }), { action: 'withdraw', token: 'ALL', amount: 'all' });
  assert.equal(some.transactions.length, 1);
  assert.equal(inner(some.transactions[0])[0], U.USDT0);
  const onlyPol = await txs(stub({ pol: 1n }), { action: 'withdraw', token: 'ALL', amount: 'all' });
  assert.deepEqual(onlyPol.transactions.map(t => inner(t)[0]), [OWNER]);
  assert.match(onlyPol.summary, /: 0.000000000000000001 POL$/);
  const none = await txs(stub(), { action: 'withdraw', token: 'ALL', amount: 'all' });
  assert.deepEqual(none.transactions, []);
  assert.equal(none.summary, `the vault ${SAFE} holds nothing to withdraw`);
});

test('ownerTxs revoke: the Roles module with the module before it; refused when already revoked', async () => {
  const one = await txs(stub(), { action: 'revoke' });
  assert.deepEqual(decodeFunctionData({ abi: SAFE_ABI, data: inner(one.transactions[0])[2] }).args, [SENTINEL, ROLES]);
  const two = await txs(stub({ modules: [A(0x77), ROLES] }), { action: 'revoke' });
  assert.deepEqual(decodeFunctionData({ abi: SAFE_ABI, data: inner(two.transactions[0])[2] }).args, [A(0x77), ROLES]);
  await assert.rejects(txs(stub({ modules: [] }), { action: 'revoke' }), /already revoked/);
  await assert.rejects(txs(stub({ modules: [A(0x77)] }), { action: 'revoke' }), /already revoked/);
});

test('ownerTxs approve: only when revoked, only a Roles module of the vault that still gives the bot its role', async () => {
  const revoked = (o = {}) => stub({ modules: [], role: () => PASSED, ...o });
  const pub = revoked();
  const a = await txs(pub, { action: 'approve' });
  assert.equal(a.summary, `approve the bot again: enable the Roles module ${ROLES} of the vault ${SAFE}`);
  assert.deepEqual(a.transactions, [regrantCall({ safe: SAFE, owner: OWNER, roles: ROLES })]);
  // the probe: the bot's key, through the Roles module, an approve of the position manager for 0
  assert.equal(pub.s.probe.from, BOT);
  assert.equal(pub.s.probe.to, ROLES);
  const probe = decodeFunctionData({ abi: ROLES_ABI, data: pub.s.probe.data });
  assert.equal(probe.functionName, 'execTransactionWithRole');
  assert.equal(probe.args[0], U.WPOL);
  assert.deepEqual(decodeFunctionData({ abi: U.ERC20_ABI, data: probe.args[2] }).args, [U.V3.npm, 0n]);
  assert.equal(probe.args[4], roleKey(VENUE));
  assert.equal(probe.args[5], true);
  assert.equal((await txs(revoked({ role: () => '0x' }), { action: 'approve' })).transactions.length, 1, 'a call that goes through');
  assert.equal((await txs(revoked({ role: () => PASSED.toUpperCase().replace('0X', '0x') }), { action: 'approve' })).transactions.length, 1);
  const GS104 = concat([toFunctionSelector('Error(string)'), encodeAbiParameters([{ type: 'string' }], ['GS104'])]);
  assert.equal((await txs(revoked({ role: () => GS104 }), { action: 'approve' })).transactions.length, 1, 'the Safe refuses the disabled module');
  for (const other of [NO_MEMBER, `${PASSED}00`, concat([toFunctionSelector('Error(string)'), encodeAbiParameters([{ type: 'string' }], ['GS105'])]), '0x']) {
    await assert.rejects(txs(revoked({ role: () => other === '0x' ? Object.assign(new Error('r'), { data: '0x' }) : other }), { action: 'approve' }),
      new RegExp(`no longer gives the bot's key ${BOT} its role`), other);
  }
  await assert.rejects(txs(revoked({ role: () => new Error('connection refused') }), { action: 'approve' }), /connection refused/);
  await assert.rejects(txs(revoked({ role: () => Object.assign(new Error('x'), { data: 'not hex' }) }), { action: 'approve' }), /^Error: x$/);
  await assert.rejects(txs(stub({ role: () => PASSED }), { action: 'approve' }), /not revoked/);
  await assert.rejects(txs(stub({ modules: [A(0x77), ROLES], role: () => PASSED }), { action: 'approve' }), /not revoked/);
  for (const what of ['rolesOwner', 'avatar', 'target']) {
    await assert.rejects(txs(revoked({ [what]: STRANGER }), { action: 'approve' }), new RegExp(`'s ${what === 'rolesOwner' ? 'owner' : what} is ${STRANGER}, not the vault`));
  }
  assert.equal((await txs(revoked({ avatar: SAFE.toLowerCase() }), { action: 'approve' })).transactions.length, 1);
});

test('ownerTxs close: one transaction per position on the vault\'s pools; none says so; status counts them', async () => {
  const none = await txs(stub(), { action: 'close' });
  assert.deepEqual(none.transactions, []);
  assert.equal(none.summary, `the vault ${SAFE} has no open position: nothing to close`);
  const pub = stub({ held: [{ id: 7n, p: pos({ liquidity: 9n }) }, { id: 8n, p: pos({ liquidity: 1n }) }] });
  const two = await txs(pub, { action: 'close' });
  assert.equal(two.summary, `close 2 position(s) of the vault ${SAFE}: the tokens stay in the vault`);
  assert.equal(two.transactions.length, 2);
  for (const t of two.transactions) assert.equal(decodeEvm(t, { kind: 'close', owner: OWNER, safe: SAFE }, PINS).ok, true);
  const st = await P.status(keys(pub), C, ROW);
  assert.deepEqual([st.vault, st.botActive, st.positions], [SAFE, true, 2]);
  assert.deepEqual([(await P.status(keys(stub({ modules: [] })), C, ROW)).botActive, (await P.status(keys(stub()), C, ROW)).positions], [false, 0]);
});

// A receipt's Transfer log of `token`.
const transfer = (token, from, to, value) => ({ address: token, data: encodeAbiParameters([{ type: 'uint256' }], [value]),
  topics: encodeEventTopics({ abi: U.ERC20_ABI, eventName: 'Transfer', args: { from, to } }) });
const receipt = (logs, o = {}) => ({ status: 'success', blockNumber: 96n, logs, ...o });
const verdict = (r, head = 100n, o = {}) => P.depositVerdict({ receipt: r, head }, { owner: OWNER, safe: SAFE, tokens: C.tokens, confirmations: 5, ...o });

test('depositVerdict: confirmed Transfer logs from the owner to the vault of configured tokens, one entry each', () => {
  assert.deepEqual(verdict(receipt([transfer(U.USDT0, OWNER, SAFE, 25_000_000n)])), { state: 'ok', deposits: [{ token: 'USDT0', amount: '25' }] });
  assert.deepEqual(verdict(receipt([transfer(U.WPOL.toLowerCase(), OWNER.toLowerCase(), SAFE.toLowerCase(), E18 / 2n),
    transfer(U.USDT0, OWNER, SAFE, 1n)]), 100n, { owner: OWNER.toLowerCase(), safe: SAFE.toLowerCase() }),
  { state: 'ok', deposits: [{ token: 'WPOL', amount: '0.5' }, { token: 'USDT0', amount: '0.000001' }] });
  assert.deepEqual(verdict(null), { state: 'pending', reason: 'the deposit is not on chain (yet)' });
  assert.deepEqual(verdict(receipt([], { status: 'reverted' })), { state: 'bad', reason: 'the deposit failed on chain' });
  assert.deepEqual(verdict(receipt([transfer(U.USDT0, OWNER, SAFE, 1n)]), 99n), { state: 'pending', reason: 'the deposit is not confirmed yet' });
  assert.equal(verdict(receipt([transfer(U.USDT0, OWNER, SAFE, 1n)]), 100n).state, 'ok', 'exactly 5 confirmations');
  const none = { state: 'bad', reason: `the transaction moves no token from ${OWNER} to the vault ${SAFE}` };
  assert.deepEqual(verdict(receipt([transfer(U.USDT0, OWNER, STRANGER, 1n)])), none);
  assert.deepEqual(verdict(receipt([transfer(U.USDT0, STRANGER, SAFE, 1n)])), none);
  assert.deepEqual(verdict(receipt([transfer(U.USDC, OWNER, SAFE, 1n)])), none);
  assert.deepEqual(verdict(receipt([])), none);
  assert.deepEqual(verdict(receipt([transfer(U.USDT0, OWNER, SAFE, 0n)])), none, 'a transfer of nothing');
  assert.deepEqual(verdict(receipt([{ address: U.USDT0, data: '0x', topics: [toFunctionSelector('Deposit(address,uint256)').padEnd(66, '0')] }])), none, 'another event');
  // a stray transfer elsewhere beside the deposit: only what reached the vault counts
  assert.deepEqual(verdict(receipt([transfer(U.USDT0, OWNER, STRANGER, 9n), transfer(U.USDT0, OWNER, SAFE, 3n)])),
    { state: 'ok', deposits: [{ token: 'USDT0', amount: '0.000003' }] });
  fc.assert(fc.property(fc.array(fc.bigInt({ min: 0n, max: 10n ** 30n }), { minLength: 1, maxLength: 5 }), (vals) => {
    const want = vals.filter(x => x > 0n).map(x => ({ token: 'USDT0', amount: fromRaw(x, 6) }));
    assert.deepEqual(verdict(receipt(vals.map(x => transfer(U.USDT0, OWNER, SAFE, x)))), want.length ? { state: 'ok', deposits: want } : none);
  }), { numRuns: 200 });
});

test('checkDeposit: a hash, a receipt that may not exist yet, the chain\'s head, the config\'s confirmations', async () => {
  const k = (o) => keys(stub(o));
  const who = { owner: OWNER, safe: SAFE };
  assert.deepEqual(await P.checkDeposit(k(), C, '0x12', who), { state: 'bad', reason: 'not a transaction hash' });
  assert.deepEqual(await P.checkDeposit(k(), C, H, who), { state: 'pending', reason: 'the deposit is not on chain (yet)' });
  await assert.rejects(P.checkDeposit(k({ receipt: new Error('rate limited') }), C, H, who), /rate limited/);
  const r = receipt([transfer(U.USDT0, OWNER, SAFE, 7n)]);
  assert.deepEqual(await P.checkDeposit(k({ receipt: r, head: 100n }), C, H, who), { state: 'ok', deposits: [{ token: 'USDT0', amount: '0.000007' }] });
  assert.equal(C.gas.confirmations, 5);
  assert.equal((await P.checkDeposit(k({ receipt: r, head: 99n }), C, H, who)).state, 'pending');
  assert.equal((await P.checkDeposit(k({ receipt: r, head: 100n }), C, H, { owner: STRANGER, safe: SAFE })).state, 'bad');
});

test('confirm: confirmed once the receipt is a success; failed when reverted or no hash; pending without a receipt', async () => {
  const k = (o) => keys(stub(o));
  assert.deepEqual(await P.confirm(k(), '0x12'), { state: 'failed', reason: 'not a transaction hash' });
  assert.deepEqual(await P.confirm(k(), `${H}00`), { state: 'failed', reason: 'not a transaction hash' });
  assert.deepEqual(await P.confirm(k(), undefined), { state: 'failed', reason: 'not a transaction hash' });
  assert.deepEqual(await P.confirm(k(), H), { state: 'pending', reason: 'the transaction is not on chain (yet)' });
  assert.deepEqual(await P.confirm(k({ receipt: new Error('Transaction receipt not found') }), H), { state: 'pending', reason: 'the transaction is not on chain (yet)' });
  await assert.rejects(P.confirm(k({ receipt: new Error('rate limited') }), H), /rate limited/);
  const pub = stub({ receipt: receipt([]) });
  assert.deepEqual(await P.confirm(keys(pub), H.toUpperCase().replace('0X', '0x')), { state: 'confirmed' });
  assert.equal(pub.s.asked, H.toUpperCase().replace('0X', '0x'));
  assert.deepEqual(await P.confirm(k({ receipt: receipt([], { status: 'reverted' }) }), H), { state: 'failed', reason: 'the transaction failed on chain' });
});
