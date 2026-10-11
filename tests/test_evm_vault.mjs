// The EVM vault without a node: the role compiled from rules.json (the contract's integrity
// rules mirrored), the Safe set-up encoders, the account a signer acts for, and the signer's
// vault-only paths. tests/test_vault_polygon_fork.mjs proves the same on the real contracts.
import test from 'node:test';
import assert from 'node:assert/strict';
import fc from 'fast-check';
import { decodeAbiParameters, decodeFunctionData, getAddress, hexToString, size, slice } from 'viem';
import * as U from '../chains/evm/polygon.mjs';
import { allows, compile, flatten, integrity, NotAllowed, OP, ROLES_ABI, roleCalls, roleKey, RULES, TYPE } from '../chains/evm/vault/roles.mjs';
import { checkOwners, connectProblems, execAsOwner, multiSend, preValidated, SAFE_ABI, setupCalls } from '../chains/evm/vault/safe.mjs';
import { acting, vaultConfig, VaultMismatch, verifyVault, wrapStep } from '../chains/evm/vault/account.mjs';
import { plan, poolOf } from '../chains/evm/vault/cli.mjs';

process.env.LPBOT_CHAIN = 'polygon';
const S = await import('../venues/uniswap_v3/signer.mjs');

const A = (n) => getAddress(`0x${n.toString(16).padStart(40, '0')}`);
const POOL = { pool: A(1), token0: U.WPOL, token1: U.USDT0, fee: 500 };
const OTHER = { pool: A(2), token0: U.USDC, token1: U.USDT0, fee: 100 };
const base = (o = {}) => ({ npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools: [POOL], payouts: [A(9)], ...o });
const VENUE = 'uniswap-v3-polygon';
const by = (c, sig) => c.functions.filter(f => f.signature.startsWith(sig));

test('role: each rule on each of its targets; the payout rule only with payout wallets', () => {
  const c = compile(VENUE, base());
  assert.equal(c.role, roleKey(VENUE));
  assert.equal(hexToString(c.role, { size: 32 }), VENUE);
  assert.deepEqual(by(c, 'approve').map(f => f.target), [U.WPOL, U.USDT0]);
  assert.deepEqual(by(c, 'transfer').map(f => f.target), [U.USDT0], 'only the stable is a payout token');
  for (const sig of ['mint', 'decreaseLiquidity', 'collect', 'burn']) assert.deepEqual(by(c, sig).map(f => f.target), [U.V3.npm]);
  assert.deepEqual(by(c, 'exactInputSingle').map(f => f.target), [U.V3.router]);
  assert.equal(by(c, 'increaseLiquidity').length + by(c, 'multicall').length, 0);
  assert.deepEqual(new Set(c.targets), new Set([U.WPOL, U.USDT0, U.V3.npm, U.V3.router]));
  assert.equal(by(compile(VENUE, base({ payouts: [] })), 'transfer').length, 0);
  assert.equal(by(compile(VENUE, base({ payouts: undefined })), 'transfer').length, 0);
  // decrease and burn take any argument; every other function is scoped
  for (const f of c.functions) assert.equal(f.conditions === null, /^(decreaseLiquidity|burn)\(/.test(f.signature), f.signature);
});

test('role: every compiled tree passes the contract\'s integrity rules, breadth first', () => {
  fc.assert(fc.property(fc.subarray([POOL, OTHER, { pool: A(3), token0: U.WPOL, token1: U.USDC, fee: 3000 }], { minLength: 1 }),
    fc.subarray([A(9), A(10)]), (pools, payouts) => {
      for (const f of compile(VENUE, base({ pools, payouts })).functions) {
        if (!f.conditions) continue;
        integrity(f.conditions);
        f.conditions.forEach((c, i) => assert.ok(i === 0 || c.parent < i, 'a parent comes before its child'));
      }
    }));
});

test('role: a pool-shaped tuple matches ONE approved pool as a whole', () => {
  const one = by(compile(VENUE, base()), 'mint')[0].conditions;
  assert.equal(one[1].paramType, TYPE.Tuple, 'one pool: the tuple itself');
  assert.equal(one.filter(c => c.parent === 1).length, 11);
  const fields = one.filter(c => c.parent === 1);
  assert.deepEqual(fields.map(c => c.operator), [OP.EqualTo, OP.EqualTo, OP.EqualTo, OP.Pass, OP.Pass, OP.Pass, OP.Pass, OP.Pass, OP.Pass, OP.EqualToAvatar, OP.Pass]);
  assert.equal(decodeAbiParameters([{ type: 'address' }], fields[0].compValue)[0], U.WPOL);
  assert.equal(decodeAbiParameters([{ type: 'uint24' }], fields[2].compValue)[0], 500);
  const two = by(compile(VENUE, base({ pools: [POOL, OTHER] })), 'mint')[0].conditions;
  assert.deepEqual([two[1].paramType, two[1].operator], [TYPE.None, OP.Or]);
  assert.deepEqual(two.filter(c => c.parent === 1).map(c => c.paramType), [TYPE.Tuple, TYPE.Tuple]);
  // the swap: either token of the pool, both ways
  const swap = by(compile(VENUE, base()), 'exactInputSingle')[0].conditions;
  const sw = swap.filter(c => c.parent === 1);
  assert.deepEqual(sw.map(c => c.operator), [OP.Or, OP.Or, OP.EqualTo, OP.EqualToAvatar, OP.Pass, OP.Pass, OP.Pass]);
  // the spender: the position manager or the router; a list of one is no Or
  const appr = by(compile(VENUE, base()), 'approve')[0].conditions;
  assert.deepEqual([appr[1].operator, appr.filter(c => c.parent === 1).length], [OP.Or, 2]);
  const single = by(compile(VENUE, base({ router: U.V3.npm })), 'approve')[0].conditions;
  assert.equal(single[1].operator, OP.EqualTo);
});

test('role: what does not compile', () => {
  assert.throws(() => compile('nowhere', base()), /no vault rules/);
  assert.throws(() => compile(VENUE, base({ pools: [] })), /at least one approved pool/);
  const rules = (fn) => ({ venues: { v: { functions: [fn] } }, payout: [] });
  const rule = (o) => rules({ name: 'r', target: 'npm', signature: 'burn(uint256)', ...o });
  assert.throws(() => compile('v', base(), rule({ params: ['token0'] })), /only valid inside perPool/);
  assert.throws(() => compile('v', base(), rule({ params: ['pairToken'] })), /only valid inside perPool/);
  assert.throws(() => compile('v', base(), rule({ params: ['banana'] })), /unknown role banana/);
  assert.throws(() => compile('v', base(), rule({ params: [null, null] })), /1 arguments, 2 rules/);
  assert.throws(() => compile('v', base(), rule({ params: [{ some: 1 }] })), /unknown rule/);
  assert.throws(() => compile('v', base(), rule({ params: [[null]] })), /tuple rule of 1 fields for uint256/);
  assert.throws(() => compile('v', base(), rule({ target: 'moon', params: 'any' })), /unknown target moon/);
  assert.throws(() => compile('v', base(), rule({ signature: 'f(bytes)', params: [null] })), /bytes is not supported/);
  assert.throws(() => compile('v', base(), rule({ signature: 'f(uint256[])', params: [null] })), /not supported/);
  assert.throws(() => compile('v', base({ payouts: [] }), rule({ signature: 'transfer(address,uint256)', params: ['payout', null] })), /needs at least one payout wallet/);
});

test('integrity: the contract\'s faults are caught before a transaction', () => {
  const ok = by(compile(VENUE, base()), 'collect')[0].conditions;
  const bad = (edit) => { const c = structuredClone(ok); edit(c); return () => integrity(c); };
  assert.throws(bad(c => { c[0].parent = 1; }), /root/);
  assert.throws(bad(c => { c[2].parent = 2; }), /root/);
  assert.throws(bad(c => { c[0].paramType = TYPE.Tuple; }), /root must match the calldata/);
  assert.throws(bad(c => { c.splice(3, 0, { parent: 0, paramType: TYPE.Static, operator: OP.Pass, compValue: '0x' }); }), /breadth first/);
  assert.throws(bad(c => { c[2].compValue = '0x01'; }), /a pass has no value/);
  assert.throws(bad(c => { c[3].compValue = '0x01'; }), /avatar/);
  assert.throws(bad(c => { c[2].operator = OP.EqualTo; c[2].compValue = `0x${'00'.repeat(31)}`; }), /equality/);
  assert.throws(bad(c => { c[1].paramType = TYPE.Static; c[1].operator = OP.Pass; }), /a static value has no children/);
  assert.throws(bad(c => { c[1].paramType = TYPE.Static; }), /unsuitable match/);
  assert.throws(bad(c => { c[2].operator = 22; }), /not written here/);
  assert.throws(bad(c => { c[1].operator = OP.Or; }), /unsuitable or/);
  assert.throws(() => integrity([]), /root/);
  assert.throws(() => integrity([{ parent: 0, paramType: TYPE.Calldata, operator: OP.Matches, compValue: '0x' }]), /unsuitable match/);
  assert.throws(() => integrity([{ parent: 0, paramType: TYPE.Calldata, operator: OP.Matches, compValue: '0x' },
    { parent: 0, paramType: TYPE.None, operator: OP.Or, compValue: '0x' }]), /unsuitable or/);
  assert.equal(integrity(ok), ok);
  // an Or of one choice is valid on chain too (the compiler never writes one, the contract allows it)
  assert.ok(integrity([{ parent: 0, paramType: TYPE.Calldata, operator: OP.Matches, compValue: '0x' },
    { parent: 0, paramType: TYPE.None, operator: OP.Or, compValue: '0x' },
    { parent: 1, paramType: TYPE.Static, operator: OP.EqualToAvatar, compValue: '0x' }]));
  // flatten: breadth first, every parent before its children
  const tree = { paramType: 5, operator: 5, compValue: '0x', children: [
    { paramType: 3, operator: 5, compValue: '0x', children: [{ paramType: 1, operator: 0, compValue: '0x', children: [] }] },
    { paramType: 1, operator: 0, compValue: '0x', children: [] }] };
  assert.deepEqual(flatten(tree).map(c => [c.parent, c.paramType]), [[0, 5], [0, 3], [0, 1], [1, 1]]);
});

test('role calls: the bot joins the role, each target is scoped, each function scoped or allowed', () => {
  const c = compile(VENUE, base()), roles = A(77), bot = A(78);
  const calls = roleCalls(c, roles, bot).map(x => ({ to: x.to, ...decodeFunctionData({ abi: ROLES_ABI, data: x.data }) }));
  assert.ok(calls.every(x => x.to === roles));
  assert.deepEqual(calls[0].args, [bot, [c.role], [true]]);
  assert.equal(calls[0].functionName, 'assignRoles');
  assert.deepEqual(calls.slice(1, 1 + c.targets.length).map(x => [x.functionName, x.args[1]]), c.targets.map(t => ['scopeTarget', t]));
  const fns = calls.slice(1 + c.targets.length);
  assert.equal(fns.length, c.functions.length);
  fns.forEach((x, i) => {
    const f = c.functions[i];
    assert.equal(x.functionName, f.conditions ? 'scopeFunction' : 'allowFunction');
    assert.deepEqual([x.args[0], x.args[1], x.args[2]], [c.role, f.target, f.selector]);
    assert.equal(x.args.at(-1), 0, 'no value, no delegatecall');
    if (f.conditions) assert.deepEqual(x.args[3].map(k => ({ ...k })), f.conditions);
  });
});

test('allows: the selector and the target, never a value', () => {
  const c = compile(VENUE, base());
  const approve = '0x095ea7b3' + '00'.repeat(64);
  assert.equal(allows(c, { to: U.USDT0.toLowerCase(), data: approve }).name, 'approve the position manager or the router');
  assert.throws(() => allows(c, { to: A(5), data: approve }), NotAllowed);
  assert.throws(() => allows(c, { to: U.USDT0, data: '0xdeadbeef' }), /not in the uniswap-v3-polygon vault role/);
  assert.throws(() => allows(c, { to: U.USDT0, data: approve, value: 1n }), /no value/);
  assert.ok(allows(c, { to: U.USDT0, data: approve, value: 0n }));
});

test('safe: the set-up adds the module first, the owners, and the bot leaves last at the threshold', () => {
  const c = compile(VENUE, base()), safe = A(50), roles = A(51), bot = A(52), owners = [A(53), A(54)];
  const calls = setupCalls({ safe, roles, compiled: c, bot, owners, threshold: 2 });
  const head = decodeFunctionData({ abi: SAFE_ABI, data: calls[0].data });
  assert.deepEqual([calls[0].to, head.functionName, head.args], [safe, 'enableModule', [roles]]);
  const tail = calls.slice(-3).map(x => decodeFunctionData({ abi: SAFE_ABI, data: x.data }));
  assert.deepEqual(tail.map(x => [x.functionName, x.args]), [
    ['addOwnerWithThreshold', [owners[0], 1n]], ['addOwnerWithThreshold', [owners[1], 1n]],
    ['removeOwner', [owners[0], bot, 2n]]]);
  assert.equal(calls.length, 1 + roleCalls(c, roles, bot).length + 3);
  // one Safe transaction: a delegatecall of MultiSendCallOnly, signed by its sender
  const exec = decodeFunctionData({ abi: SAFE_ABI, data: execAsOwner({ multiSendCallOnly: A(60), calls, bot }) });
  assert.deepEqual([exec.args[0], exec.args[1], exec.args[3]], [A(60), 0n, 1]);
  assert.equal(exec.args[9], preValidated(bot));
  // the packed list carries every call, in order, with no value
  const packed = decodeFunctionData({ abi: [{ type: 'function', name: 'multiSend', inputs: [{ type: 'bytes' }], outputs: [] }], data: exec.args[2] }).args[0];
  let at = 0;
  for (const call of calls) {
    assert.equal(slice(packed, at, at + 1), '0x00');
    assert.equal(getAddress(slice(packed, at + 1, at + 21)), call.to);
    assert.equal(BigInt(slice(packed, at + 21, at + 53)), 0n);
    const n = Number(BigInt(slice(packed, at + 53, at + 85)));
    assert.equal(slice(packed, at + 85, at + 85 + n), call.data);
    at += 85 + n;
  }
  assert.equal(at, size(packed));
  assert.throws(() => multiSend([]), /no calls/);
});

test('safe: a pre-validated signature is the owner, nothing, and v = 1', () => {
  const sig = preValidated(A(0xabc));
  assert.equal(size(sig), 65);
  assert.equal(getAddress(slice(sig, 12, 32)), A(0xabc));
  assert.equal(BigInt(slice(sig, 32, 64)), 0n);
  assert.equal(slice(sig, 64), '0x01');
});

test('safe: owners and threshold are checked before anything is sent', () => {
  const bot = A(1);
  assert.deepEqual(checkOwners([A(2).toLowerCase(), A(3)], 2, [bot]), [A(2), A(3)]);
  assert.throws(() => checkOwners([], 1, [bot]), /at least one owner/);
  assert.throws(() => checkOwners([A(2), A(2).toLowerCase()], 1, [bot]), /twice/);
  assert.throws(() => checkOwners([A(2), bot], 1, [bot]), /cannot be an owner/);
  for (const t of [0, 3, 1.5, NaN]) assert.throws(() => checkOwners([A(2), A(3)], t, [bot]), /threshold/);
});

test('safe: the check after connect catches every way the vault can be wrong', () => {
  const safe = A(50), roles = A(51), bot = A(52), owner = A(53);
  const good = { owners: [owner], threshold: 1, modules: [roles], guard: null, rolesOwner: safe, avatar: safe, target: safe };
  const want = { safe, roles, owners: [owner.toLowerCase()], threshold: 1, botKeys: [bot] };
  assert.deepEqual(connectProblems(good, want), []);
  const cases = [
    [{ owners: [owner, bot] }, /bot's key .* is an owner/], [{ owners: [A(54)] }, /owners are/], [{ threshold: 2 }, /threshold is 2/],
    [{ modules: [] }, /modules are none/], [{ modules: [roles, A(55)] }, /modules are/], [{ modules: [A(55)] }, /modules are/],
    [{ guard: '0x01' }, /guard/], [{ rolesOwner: bot }, /module's owner/], [{ avatar: bot }, /avatar/], [{ target: bot }, /target/],
  ];
  for (const [edit, rx] of cases) assert.match(connectProblems({ ...good, ...edit }, want).join('; '), rx, JSON.stringify(edit));
});

test('account: no vault, a whole vault, or a refusal; a step becomes the role call', () => {
  assert.equal(vaultConfig({}), null);
  assert.throws(() => vaultConfig({ LPBOT_VAULT_SAFE: A(1) }), /missing LPBOT_VAULT_ROLES, LPBOT_VAULT_VENUE, LPBOT_VAULT_POOLS/);
  const env = { LPBOT_VAULT_SAFE: A(1).toLowerCase(), LPBOT_VAULT_ROLES: A(2), LPBOT_VAULT_VENUE: VENUE, LPBOT_VAULT_POOLS: `${A(3)}, ${A(4)}` };
  const v = vaultConfig(env);
  assert.deepEqual(v, { safe: A(1), roles: A(2), venue: VENUE, role: roleKey(VENUE), pools: [A(3), A(4)], payouts: [] });
  assert.deepEqual(vaultConfig({ ...env, LPBOT_VAULT_PAYOUTS: A(9) }).payouts, [A(9)]);
  assert.throws(() => vaultConfig({ ...env, LPBOT_VAULT_POOLS: 'nope' }), /LPBOT_VAULT_POOLS: nope is not an address/);
  const step = { label: 'swap', to: A(7), data: '0x1234', value: 0n };
  const w = wrapStep(step, v);
  assert.deepEqual([w.label, w.to, w.value], ['swap', A(2), 0n]);
  assert.deepEqual(decodeFunctionData({ abi: ROLES_ABI, data: w.data }).args, [A(7), 0n, '0x1234', 0, v.role, true]);
  assert.ok(wrapStep({ ...step, value: undefined }, v));
  assert.throws(() => wrapStep({ ...step, value: 1n }, v), /carries 1 wei/);
  const key = acting(A(8), null);
  assert.deepEqual([key.me, key.from, key.wrap(step), key.vault], [A(8), A(8), step, null]);
  const vault = acting(A(8), v);
  assert.deepEqual([vault.me, vault.from, vault.vault], [A(1), A(8), v]);
  assert.deepEqual(vault.wrap(step), w);
});

test('signer: a vault closes call by call; a key wallet in one multicall', () => {
  const calls = [
    S.closeCalls({ tokenId: 5n, tickLower: -10, tickUpper: 10, liquidity: 1000n },
      { sqrtPriceX96: (2n ** 96n).toString() }, A(1), { slippageBps: 100, priceSlippageBps: 50 }, 99n),
    S.closeCalls({ tokenId: 5n, tickLower: -10, tickUpper: 10, liquidity: 0n },
      { sqrtPriceX96: (2n ** 96n).toString() }, A(1), { slippageBps: 100, priceSlippageBps: 50 }, 99n),
  ].map(x => x.calls);
  assert.deepEqual(S.closeSteps(calls[0], U.V3.npm, true).map(s => [s.label, s.to]),
    [['decreaseLiquidity', U.V3.npm], ['collect', U.V3.npm], ['burn', U.V3.npm]]);
  assert.deepEqual(S.closeSteps(calls[1], U.V3.npm, true).map(s => s.label), ['collect', 'burn'], 'no liquidity: nothing to decrease');
  const one = S.closeSteps(calls[0], U.V3.npm, false);
  assert.equal(one.length, 1);
  assert.equal(one[0].label, 'decrease+collect+burn');
  assert.deepEqual(decodeFunctionData({ abi: U.NPM_ABI, data: one[0].data }).args[0], calls[0]);
});

test('signer: a vault pays the profit or fee wallet, and only one its owners approved', () => {
  const v = { payouts: [A(9), A(10)] };
  const env = { LPBOT_PROFIT_WALLET: A(9), LPBOT_FEE_WALLET: A(10).toLowerCase() };
  assert.equal(S.checkVaultRecipient(A(9).toLowerCase(), v, env), A(9));
  assert.equal(S.checkVaultRecipient(A(10), v, env), A(10));
  assert.throws(() => S.checkVaultRecipient('nope', v, env), /not an address/);
  assert.throws(() => S.checkVaultRecipient(undefined, v, env), /not an address/);
  assert.throws(() => S.checkVaultRecipient(A(11), v, env), /neither the profit wallet nor the fee wallet/);
  assert.throws(() => S.checkVaultRecipient(A(9), v, {}), /neither/);
  assert.throws(() => S.checkVaultRecipient(A(9), { payouts: [A(10)] }, env), /not a payout wallet the vault's owners approved/);
  assert.equal(S.checkVaultRecipient(A(10), v, { LPBOT_FEE_WALLET: A(10) }), A(10));
});

test('rules.json: every signature parses, no increase, no multicall, no value', () => {
  const all = [...Object.values(RULES.venues).flatMap(v => v.functions), ...RULES.payout];
  for (const r of all) {
    assert.ok(!/^(increaseLiquidity|multicall)\(/.test(r.signature), r.signature);
    assert.ok(r.params === 'any' || Array.isArray(r.params), r.name);
  }
});

// A client that answers readContract by function name, from `answers`.
const stub = (answers) => ({ readContract: async ({ functionName, address }) => {
  const a = answers[functionName];
  if (a === undefined) throw new Error(`no answer for ${functionName}`);
  return typeof a === 'function' ? a(address) : a;
} });

test('verifyVault: the module enabled and acting on the Safe, or a refusal', async () => {
  const safe = A(50), roles = A(51), vault = { safe, roles };
  const good = { getOwners: [A(53)], getThreshold: 1n, getModulesPaginated: [[roles], A(1)], getStorageAt: `0x${'00'.repeat(32)}`,
    owner: safe, avatar: safe, target: safe };
  assert.equal((await verifyVault(stub(good), vault)).threshold, 1);
  assert.equal((await verifyVault(stub({ ...good, getModulesPaginated: [[A(55), roles], A(1)] }), vault)).modules.length, 2);
  for (const edit of [{ getModulesPaginated: [[], A(1)] }, { getModulesPaginated: [[A(55)], A(1)] }]) {
    await assert.rejects(verifyVault(stub({ ...good, ...edit }), vault), /has not enabled the Roles module/);
  }
  for (const edit of [{ avatar: A(9) }, { target: A(9) }, { avatar: A(9), target: A(9) }]) {
    await assert.rejects(verifyVault(stub({ ...good, ...edit }), vault), (e) => e instanceof VaultMismatch && /does not act on the Safe/.test(e.message));
  }
  assert.equal((await verifyVault(stub({ ...good, getStorageAt: `0x${'00'.repeat(31)}07` }), vault)).guard, `0x${'00'.repeat(31)}07`);
});

test('signStep: the fee cap and the gas are checked at their bounds; a later step is checked at its block', async () => {
  const seen = [];
  const pub = (o = {}) => ({
    call: async (r) => { seen.push(r); },
    estimateGas: async () => 100n,                               // x1.3 = 130 gas
    estimateFeesPerGas: async () => ({ maxFeePerGas: 10n, maxPriorityFeePerGas: 1n }),
    getBalance: async () => 1300n,                               // exactly 130 gas x 10 wei
    getBlockNumber: async () => 7n, ...o });
  const account = { signTransaction: async (tx) => { seen.push(tx); return '0x01'; } };
  const cfg = { maxFeeWei: 10n };
  const step = { label: 'approve', to: A(7), data: '0x1234' };
  const ctx = (o = {}) => ({ pub: pub(o), account, me: A(8), nonce: 3, ...o.ctx });
  assert.ok((await S.signStep(ctx(), step, cfg)).hash, 'exactly enough gas, exactly the cap');
  await assert.rejects(S.signStep(ctx({ getBalance: async () => 1299n }), step, cfg), /cannot pay approve/);
  await assert.rejects(S.signStep(ctx(), step, { maxFeeWei: 9n }), /exceeds LPBOT_EVM_MAX_GWEI/);
  seen.length = 0;
  await S.signStep(ctx({ ctx: { minBlock: 5n, blockDeps: { sleep: async () => {} } } }), step, cfg);
  assert.equal(seen[0].blockNumber, 7n, 'checked at the head, which holds the earlier step');
  seen.length = 0;
  await S.signStep(ctx(), step, cfg);
  assert.equal(seen[0].blockNumber, undefined);
  // a vault: the role call, from the key
  const v = { safe: A(1), roles: A(2), role: roleKey(VENUE) };
  seen.length = 0;
  await S.signStep({ ...ctx(), from: A(8), me: A(1), wrap: (s) => wrapStep(s, v) }, step, cfg);
  assert.deepEqual([seen[0].account, seen[0].to], [A(8), A(2)]);
  assert.deepEqual([seen.at(-1).to, seen.at(-1).value], [A(2), 0n]);
});

test('connect plan: genuine pools only, defaults, and what it refuses before sending', async () => {
  const factory = U.V3.factory, pool = A(0x99);
  const answers = (o = {}) => ({ factory, token0: U.WPOL, token1: U.USDT0, fee: 500, getPool: pool, ...o });
  const deps = (o) => ({ pub: stub(answers(o)), U, account: { address: A(1) } });
  assert.deepEqual(await poolOf(stub(answers()), U, pool), { pool, token0: U.WPOL, token1: U.USDT0, fee: 500 });
  await assert.rejects(poolOf(stub(answers({ factory: A(5) })), U, pool), /not a pool of the Uniswap v3 factory/);
  await assert.rejects(poolOf(stub(answers({ getPool: A(6) })), U, pool), /not a pool of the Uniswap v3 factory/);
  const p = await plan({ owners: A(2), pools: pool }, deps());
  assert.deepEqual([p.threshold, p.venue, p.owners, p.payouts], [1, U.DEX, [A(2)], []]);
  assert.equal(p.compiled.functions.filter(f => f.signature.startsWith('transfer')).length, 0);
  await assert.rejects(plan({ owners: A(2), pools: pool, threshold: 0 }, deps()), /threshold 0/);
  await assert.rejects(plan({ owners: A(2), pools: pool, venue: '' }, deps()), /no vault rules for venue/);
  await assert.rejects(plan({ owners: A(2), pools: '' }, deps()), /at least one approved pool/);
  await assert.rejects(plan({ owners: A(1), pools: pool }, deps()), /bot's key/);
  await assert.rejects(plan({ owners: 'x', pools: pool }, deps()), /--owners: x is not an address/);
  assert.equal((await plan({ owners: `${A(2)},${A(3)}`, threshold: '2', pools: pool, payout: A(9) }, deps())).threshold, 2);
});
