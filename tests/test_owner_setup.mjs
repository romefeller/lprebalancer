// chains/evm/vault/owner_setup.mjs without a node: the owner's own vault (the addresses its
// two transactions create, their bytes, the funding) and the check of a vault on chain, over
// stub clients. tests/test_owner_setup_fork.mjs proves the same on the real contracts.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fc from 'fast-check';
import { decodeFunctionData, encodeAbiParameters, encodeEventTopics, getAddress, keccak256, size, slice, toHex } from 'viem';
import * as U from '../chains/evm/polygon.mjs';
import { compile, roleCalls, roleKey } from '../chains/evm/vault/roles.mjs';
import { MODULE_FACTORY_ABI, preValidated, rolesSetup, SAFE_ABI, SAFE_FACTORY_ABI } from '../chains/evm/vault/safe.mjs';
import { configureCalls, fundCalls, grantProblems, moduleRuntime, ownerSetupCalls, predictRoles, predictSafe, ROLES_EVENTS,
  safeInitializer, saltOf, verifySetup } from '../chains/evm/vault/owner_setup.mjs';
import { check, request } from '../chains/evm/vault/cli.mjs';
import * as P from '../onboard/polygon.mjs';

const A = (n) => getAddress(`0x${n.toString(16).padStart(40, '0')}`);
const ZERO = A(0);
const OWNER = A(0xaa01), BOT = A(0xb07), PROFIT = A(0x9), FEE = A(0x8);
const POOL = { pool: '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7', token0: U.WPOL, token1: U.USDT0, fee: 500 };
const COMPILED = compile(U.DEX, { npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools: [POOL], payouts: [PROFIT, FEE] });
const MULTISEND = [{ type: 'function', name: 'multiSend', inputs: [{ type: 'bytes' }], outputs: [] }];
const E18 = 10n ** 18n;

// Measured on an anvil fork of Polygon (2026-10-13): createProxyWithNonce and deployModule
// returned these addresses for these owners and request numbers.
const VECTORS = [
  [OWNER, 0, '3987726346952843124300972176466787653581072606267443368739509740830093286990', '0xE20688dB5524778C5421522A74455B977c9b24Af', '0x4A4c71320a20e3cfF5aF71313Cd8A44048F51186'],
  [OWNER, 1, '104530768817633900241883711851301748622351988213843536674515425515964393079518', '0x90B71eC0BAefb902De4BCccCfAA9db58B236b577', '0x0d2Cc62FC9bE38cB4d3435fbB11c064FC3734ACE'],
  [A(0xaa02), 0, '80906153795937845290951693540571622099811248530203054841224722931820426553451', '0xB87D557F938D4BA6BBe28206B29fD7b6Bf6d0c39', '0xea7C3EaE61769865b79Aea6b4ba164Ee81AEb59e'],
];
const [, , SALT0, SAFE0, ROLES0] = VECTORS[0];

// MultiSendCallOnly's packed list, read back.
function unpacked(data) {
  const packed = decodeFunctionData({ abi: MULTISEND, data }).args[0];
  const out = [];
  for (let at = 0; at < size(packed);) {
    const n = Number(BigInt(slice(packed, at + 53, at + 85)));
    out.push({ operation: Number(slice(packed, at, at + 1)), to: getAddress(slice(packed, at + 1, at + 21)),
      value: BigInt(slice(packed, at + 21, at + 53)), data: n ? slice(packed, at + 85, at + 85 + n) : '0x' });
    at += 85 + n;
  }
  return out;
}

test('saltOf: the same owner and number give the same salt; any other owner or number another', () => {
  assert.equal(String(saltOf(OWNER, 0)), SALT0);
  assert.equal(saltOf(OWNER.toLowerCase(), 0), saltOf(OWNER, 0));
  fc.assert(fc.property(fc.integer({ min: 1, max: 2 ** 40 }), fc.integer({ min: 0, max: 1000 }), fc.integer({ min: 0, max: 1000 }), (o, n, m) => {
    assert.equal(saltOf(A(o), n) === saltOf(A(o), m), n === m);
    assert.notEqual(saltOf(A(o), n), saltOf(A(o + 1), n));
  }), { numRuns: 100 });
  for (const n of [-1, 1.5, NaN, '0', 2 ** 53]) assert.throws(() => saltOf(OWNER, n), /request number/);
});

test('predictSafe and predictRoles: the addresses the factories create on the chain', () => {
  for (const [owner, n, salt, safe, roles] of VECTORS) {
    assert.equal(String(saltOf(owner, n)), salt);
    assert.equal(predictSafe({ U, owner, saltNonce: saltOf(owner, n) }), safe);
    assert.equal(predictSafe({ U, initializer: safeInitializer(U, owner), saltNonce: BigInt(salt) }), safe);
    assert.equal(predictRoles({ U, safe, saltNonce: BigInt(salt) }), roles);
    assert.equal(predictRoles({ U, safe: safe.toLowerCase(), saltNonce: salt }), roles);
  }
  // each input moves the address
  assert.notEqual(predictSafe({ U, owner: OWNER, saltNonce: BigInt(SALT0) + 1n }), SAFE0);
  assert.notEqual(predictSafe({ U: { VAULT: { ...U.VAULT, safeSingleton: A(1) } }, owner: OWNER, saltNonce: SALT0 }), SAFE0);
  assert.notEqual(predictSafe({ U: { VAULT: { ...U.VAULT, safeFactory: A(1) } }, owner: OWNER, saltNonce: SALT0 }), SAFE0);
  assert.notEqual(predictSafe({ U: { VAULT: { ...U.VAULT, fallbackHandler: A(1) } }, owner: OWNER, saltNonce: SALT0 }), SAFE0);
  assert.notEqual(predictRoles({ U, safe: A(1), saltNonce: SALT0 }), ROLES0);
  assert.notEqual(predictRoles({ U, safe: SAFE0, saltNonce: 1n }), ROLES0);
  assert.notEqual(predictRoles({ U: { VAULT: { ...U.VAULT, moduleFactory: A(1) } }, safe: SAFE0, saltNonce: SALT0 }), ROLES0);
  assert.notEqual(predictRoles({ U: { VAULT: { ...U.VAULT, rolesMastercopy: A(1) } }, safe: SAFE0, saltNonce: SALT0 }), ROLES0);
  assert.equal(moduleRuntime(U.VAULT.rolesMastercopy), `0x363d3d373d3d3d363d73${U.VAULT.rolesMastercopy.slice(2).toLowerCase()}5af43d82803e903d91602b57fd5bf3`);
});

test('ownerSetupCalls: the Safe with the owner alone, then one delegatecall of MultiSendCallOnly: module, enable, role', () => {
  const s = ownerSetupCalls({ U, owner: OWNER.toLowerCase(), bot: BOT, compiled: COMPILED, saltNonce: BigInt(SALT0) });
  assert.deepEqual([s.safe, s.roles], [SAFE0, ROLES0]);
  const [create, configure] = s.txs;
  assert.deepEqual([create.to, create.value, configure.to, configure.value], [U.VAULT.safeFactory, '0x0', SAFE0, '0x0']);
  const c = decodeFunctionData({ abi: SAFE_FACTORY_ABI, data: create.data });
  assert.deepEqual([c.functionName, c.args[0], c.args[2]], ['createProxyWithNonce', U.VAULT.safeSingleton, BigInt(SALT0)]);
  const init = decodeFunctionData({ abi: SAFE_ABI, data: c.args[1] });
  assert.deepEqual([init.functionName, ...init.args], ['setup', [OWNER], 1n, ZERO, '0x', U.VAULT.fallbackHandler, ZERO, 0n, ZERO]);
  const e = decodeFunctionData({ abi: SAFE_ABI, data: configure.data });
  assert.equal(e.functionName, 'execTransaction');
  assert.deepEqual([e.args[0], e.args[1], e.args[3], e.args[4], e.args[5], e.args[6], e.args[7], e.args[8], e.args[9]],
    [U.VAULT.multiSendCallOnly, 0n, 1, 0n, 0n, 0n, ZERO, ZERO, preValidated(OWNER).toLowerCase()]);
  const calls = unpacked(e.args[2]);
  assert.ok(calls.every(x => x.operation === 0 && x.value === 0n), 'calls only, no value');
  const d = decodeFunctionData({ abi: MODULE_FACTORY_ABI, data: calls[0].data });
  assert.deepEqual([calls[0].to, d.functionName, ...d.args], [U.VAULT.moduleFactory, 'deployModule', U.VAULT.rolesMastercopy, rolesSetup(SAFE0), BigInt(SALT0)]);
  const en = decodeFunctionData({ abi: SAFE_ABI, data: calls[1].data });
  assert.deepEqual([calls[1].to, en.functionName, en.args], [SAFE0, 'enableModule', [ROLES0]]);
  assert.deepEqual(calls.slice(2).map(x => ({ to: x.to, data: x.data })), roleCalls(COMPILED, ROLES0, BOT));
  // the module exists already: the same transaction without its deployment
  const again = ownerSetupCalls({ U, owner: OWNER, bot: BOT, compiled: COMPILED, saltNonce: SALT0, rolesDeployed: true });
  assert.deepEqual(again.txs[0], create);
  assert.deepEqual(unpacked(decodeFunctionData({ abi: SAFE_ABI, data: again.txs[1].data }).args[2]), calls.slice(1));
  assert.deepEqual(configureCalls({ U, safe: SAFE0, roles: ROLES0, compiled: COMPILED, bot: BOT, saltNonce: SALT0 }).map(x => x.to), calls.map(x => x.to));
  assert.throws(() => ownerSetupCalls({ U, owner: BOT, bot: BOT, compiled: COMPILED, saltNonce: 1n }), /bot's key: it cannot be an owner/);
  assert.throws(() => ownerSetupCalls({ U, owner: BOT.toLowerCase(), bot: BOT, compiled: COMPILED, saltNonce: 1n }), /cannot be an owner/);
});

test('fundCalls: the deposit, a wrap first for the coin, and the bot\'s gas last', () => {
  const C = P.resolve(cfg());
  const native = fundCalls({ U, c: C, safe: SAFE0.toLowerCase(), token: 'POL', amountRaw: 26n * E18, gasWei: 4n * E18, botKey: BOT.toLowerCase() });
  assert.deepEqual(native.map(x => [x.to, x.value]), [[U.WPOL, toHex(26n * E18)], [U.WPOL, '0x0'], [BOT, toHex(4n * E18)]]);
  assert.equal(decodeFunctionData({ abi: U.WRAPPED_ABI, data: native[0].data }).functionName, 'deposit');
  assert.deepEqual(decodeFunctionData({ abi: U.ERC20_ABI, data: native[1].data }).args, [SAFE0, 26n * E18]);
  assert.equal(native[2].data, '0x');
  const token = fundCalls({ U, c: C, safe: SAFE0, token: 'USDT0', amountRaw: '25000000', gasWei: '4', botKey: BOT });
  assert.deepEqual(token.map(x => [x.to, x.value]), [[U.USDT0, '0x0'], [BOT, '0x4']]);
  assert.deepEqual(decodeFunctionData({ abi: U.ERC20_ABI, data: token[0].data }).args, [SAFE0, 25_000_000n]);
  // without the onboarding config: the chain module's tokens
  assert.deepEqual(fundCalls({ U, safe: SAFE0, token: 'POL', amountRaw: 1n, gasWei: 1n, botKey: BOT }), fundCalls({ U, c: C, safe: SAFE0, token: 'POL', amountRaw: 1n, gasWei: 1n, botKey: BOT }));
  assert.deepEqual(fundCalls({ U, safe: SAFE0, token: 'USDT0', amountRaw: 1n, gasWei: 1n, botKey: BOT }), fundCalls({ U, c: C, safe: SAFE0, token: 'USDT0', amountRaw: 1n, gasWei: 1n, botKey: BOT }));
  for (const token of ['DAI', 'constructor', 'NAME', 'V3']) {
    assert.throws(() => fundCalls({ U, safe: SAFE0, token, amountRaw: 1n, gasWei: 1n, botKey: BOT }), /not one the vault takes/, token);
  }
  assert.throws(() => fundCalls({ U, c: C, safe: SAFE0, token: 'toString', amountRaw: 1n, gasWei: 1n, botKey: BOT }), /not one the vault takes/);
  assert.throws(() => fundCalls({ U, c: C, safe: SAFE0, token: 'USDT0', amountRaw: 0n, gasWei: 1n, botKey: BOT }), /deposit must be more than 0/);
  assert.throws(() => fundCalls({ U, c: C, safe: SAFE0, token: 'USDT0', amountRaw: 1n, gasWei: 0n, botKey: BOT }), /gas must be more than 0/);
  assert.equal(fundCalls({ U, c: C, safe: SAFE0, token: 'USDT0', amountRaw: 1n, gasWei: 1n, botKey: BOT }).length, 2);
});

// The onboarding config of Polygon (onboard.json).
const cfg = () => JSON.parse(fs.readFileSync(new URL('../onboard/onboard.json', import.meta.url), 'utf8')).chains.polygon;

// --- the role on chain, from the module's events ----------------------------------------------

const ev = (eventName, args) => ({ eventName, args });
function setupEvents(compiled = COMPILED, bot = BOT) {
  return [
    ev('OwnershipTransferred', { previousOwner: ZERO, newOwner: SAFE0 }), ev('RolesModSetup', {}), ev('AvatarSet', {}), ev('TargetSet', {}),
    ev('Initialized', { version: 1n }), ev('EnabledModule', { module: bot }),
    ev('AssignRoles', { module: bot, roleKeys: [compiled.role], memberOf: [true] }),
    ...compiled.targets.map(t => ev('ScopeTarget', { roleKey: compiled.role, targetAddress: t })),
    ...compiled.functions.map(f => (f.conditions
      ? ev('ScopeFunction', { roleKey: compiled.role, targetAddress: f.target, selector: f.selector, conditions: f.conditions, options: 0 })
      : ev('AllowFunction', { roleKey: compiled.role, targetAddress: f.target, selector: f.selector, options: 0 }))),
  ];
}

test('grantProblems: exactly the compiled role, the bot its one member; every other grant named', () => {
  const good = setupEvents();
  assert.deepEqual(grantProblems(good, COMPILED, BOT), []);
  assert.deepEqual(grantProblems(good, COMPILED, BOT.toLowerCase()), []);
  const role = COMPILED.role, other = roleKey('other');
  const scoped = COMPILED.functions.find(f => f.conditions), allowed = COMPILED.functions.find(f => !f.conditions);
  const cases = [
    [[ev('AssignRoles', { module: A(5), roleKeys: [role], memberOf: [true] })], /role members are .*0x0+5/i],
    [[ev('AssignRoles', { module: BOT, roleKeys: [role], memberOf: [false] })], /role members are none/],
    [[ev('AssignRoles', { module: BOT, roleKeys: [other], memberOf: [true] })], /role members are/],
    [[ev('AllowTarget', { roleKey: role, targetAddress: U.WPOL, options: 0 })], /target .* is allow 0, not as compiled/],
    [[ev('ScopeTarget', { roleKey: role, targetAddress: A(7) })], /target .*0x0+7 is scope, not as compiled/i],
    [[ev('ScopeTarget', { roleKey: other, targetAddress: U.WPOL })], /not as compiled/],
    [[ev('RevokeTarget', { roleKey: role, targetAddress: U.WPOL })], /lacks the target/],
    [[ev('RevokeFunction', { roleKey: role, targetAddress: scoped.target, selector: scoped.selector })], /lacks the function/],
    [[ev('AllowFunction', { roleKey: role, targetAddress: scoped.target, selector: scoped.selector, options: 0 })], /is allow 0, not as compiled/],
    [[ev('AllowFunction', { roleKey: role, targetAddress: allowed.target, selector: allowed.selector, options: 2 })], /is allow 2, not as compiled/],
    [[ev('ScopeFunction', { ...good.find(e => e.eventName === 'ScopeFunction').args, options: 1 })], /is scope 1/],
    [[ev('ScopeFunction', { ...good.find(e => e.eventName === 'ScopeFunction').args, conditions: scoped.conditions.slice(0, -1) })], /not as compiled/],
    [[ev('ScopeFunction', { roleKey: role, targetAddress: A(7), selector: '0x12345678', conditions: [], options: 0 })], /function .* is scope 0 \[\], not as compiled/],
    [[ev('AllowFunction', { roleKey: role, targetAddress: U.V3.npm, selector: '0x12345678', options: 0 })], /not as compiled/],
    [[ev('SetAllowance', {})], /an event the check does not know: SetAllowance/],
    [[ev('SetDefaultRole', {})], /SetDefaultRole/],
    [[ev('SetUnwrapAdapter', {})], /SetUnwrapAdapter/],
    [[ev('undecoded', {})], /does not know: undecoded/],
    [[ev('EnabledModule', { module: A(6) })], /modules are .*0x0+6.*expected the bot's key/i],
    [[ev('DisabledModule', { module: BOT })], /modules are none/],
  ];
  for (const [more, re] of cases) assert.match(grantProblems([...good, ...more], COMPILED, BOT).join('; '), re, JSON.stringify(more[0].eventName));
  // a late re-grant puts it back as it was
  const fn = good.find(e => e.eventName === 'ScopeFunction');
  assert.deepEqual(grantProblems([...good, ev('RevokeFunction', fn.args), fn], COMPILED, BOT), []);
  assert.deepEqual(grantProblems([...good, ev('DisabledModule', { module: BOT }), ev('EnabledModule', { module: BOT })], COMPILED, BOT), []);
  // the events must reach back to the module's set-up, and miss nothing of the role
  assert.match(grantProblems(good.filter(e => e.eventName !== 'RolesModSetup'), COMPILED, BOT).join(), /do not reach back/);
  assert.match(grantProblems(good.filter(e => e.eventName !== 'ScopeTarget'), COMPILED, BOT).join(), /lacks the target/);
  assert.match(grantProblems(good.filter(e => e.eventName !== 'AllowFunction'), COMPILED, BOT).join(), /lacks the function/);
  assert.match(grantProblems(good.filter(e => e.eventName !== 'EnabledModule'), COMPILED, BOT).join(), /modules are none/);
  assert.match(grantProblems(good, COMPILED, A(5)).join(), /expected the bot's key 0x0+5 alone/i);
  assert.equal(grantProblems(good, compile(U.DEX, { npm: U.V3.npm, router: U.V3.router, stables: U.STABLES, pools: [POOL], payouts: [PROFIT] }), BOT).length > 0, true,
    'a role without the fee wallet is another role');
});

// A log as the chain gives it.
function logOf(eventName, args) {
  const item = ROLES_EVENTS.find(x => x.name === eventName);
  const topics = encodeEventTopics({ abi: ROLES_EVENTS, eventName, args });
  const rest = item.inputs.filter(i => !i.indexed);
  return { topics, data: encodeAbiParameters(rest, rest.map(i => args[i.name])) };
}

// A public client over a vault's state, code and logs. `o` changes any of it.
function chain(o = {}) {
  const state = { factory: U.V3.factory, token0: U.WPOL, token1: U.USDT0, fee: 500, getPool: POOL.pool, getOwners: [OWNER], getThreshold: 1n, getModulesPaginated: [[ROLES0], A(1)], getStorageAt: `0x${'00'.repeat(32)}`,
    owner: SAFE0, avatar: SAFE0, target: SAFE0, ...o.state };
  const logs = o.logs ?? setupEvents().map(e => logOf(e.eventName, { previousOwner: ZERO, newOwner: SAFE0, initiator: A(3), owner: SAFE0,
    avatar: SAFE0, target: SAFE0, previousAvatar: ZERO, newAvatar: SAFE0, previousTarget: ZERO, newTarget: SAFE0, ...e.args }));
  const windows = [];
  return {
    windows,
    getCode: async ({ address }) => (o.code ?? { [SAFE0]: '0x6080', [ROLES0]: moduleRuntime(U.VAULT.rolesMastercopy) })[getAddress(address)] ?? (o.noCode ? undefined : '0x'),
    getStorageAt: async ({ address, slot }) => { assert.deepEqual([address, slot], [SAFE0, '0x0']); return o.singleton ?? `0x${U.VAULT.safeSingleton.slice(2).toLowerCase().padStart(64, '0')}`; },
    getBlockNumber: async () => o.head ?? 100n,
    getLogs: async ({ address, fromBlock, toBlock }) => { assert.equal(address, ROLES0); windows.push([fromBlock, toBlock]); return fromBlock <= 50n && toBlock >= 50n ? logs : []; },
    readContract: async ({ functionName }) => state[functionName],
  };
}
const VERIFY = { U, safe: SAFE0, roles: ROLES0, owner: OWNER, bot: BOT, compiled: COMPILED, fromBlock: 40n };

test('verifySetup: a vault as planned passes; each way it can be wrong is named', async () => {
  const pub = chain();
  assert.deepEqual(await verifySetup(pub, VERIFY), []);
  assert.deepEqual(pub.windows, [[40n, 100n]]);
  // the logs are read in windows public endpoints answer, to the head
  const far = chain({ head: 25_000n });
  assert.deepEqual(await verifySetup(far, { ...VERIFY, fromBlock: 0 }), []);
  assert.deepEqual(far.windows, [[0n, 9_999n], [10_000n, 19_999n], [20_000n, 25_000n]]);
  const at = chain({ head: 50n });
  assert.deepEqual(await verifySetup(at, { ...VERIFY, fromBlock: 50n }), [], 'the set-up in the head block');
  assert.deepEqual(at.windows, [[50n, 50n]]);
  const edge = chain({ head: 10_049n });
  assert.deepEqual(await verifySetup(edge, { ...VERIFY, fromBlock: 50n }), []);
  assert.deepEqual(edge.windows, [[50n, 10_049n]]);
  await assert.rejects(verifySetup(pub, { ...VERIFY, fromBlock: undefined }), /needs fromBlock/);
  await assert.rejects(verifySetup(pub, { ...VERIFY, fromBlock: null }), /needs fromBlock/);
  assert.match((await verifySetup(chain(), { ...VERIFY, fromBlock: 51n })).join(), /do not reach back/, 'a check that starts after the set-up');
  assert.deepEqual(await verifySetup(chain({ code: { [ROLES0]: '0x01' } }), VERIFY), [`no Safe at ${SAFE0}`]);
  assert.deepEqual(await verifySetup(chain({ code: { [ROLES0]: '0x01' }, noCode: true }), VERIFY), [`no Safe at ${SAFE0}`]);
  assert.deepEqual(await verifySetup(chain({ code: { [SAFE0]: '0x01' } }), VERIFY), [`no Roles module at ${ROLES0}`]);
  assert.deepEqual(await verifySetup(chain({ code: { [SAFE0]: '0x01' }, noCode: true }), VERIFY), [`no Roles module at ${ROLES0}`]);
  const cases = [
    [{ singleton: `0x${'00'.repeat(31)}01` }, /singleton is not SafeL2 1.4.1/],
    [{ singleton: undefined, state: {} }, null],
    [{ code: { [SAFE0]: '0x01', [ROLES0]: moduleRuntime(A(1)) } }, /not a proxy of the Roles mastercopy/],
    [{ state: { getOwners: [OWNER, A(5)] } }, /owners are/],
    [{ state: { getOwners: [OWNER, BOT] } }, /bot's key .* is an owner/],
    [{ state: { getThreshold: 2n } }, /threshold is 2/],
    [{ state: { getModulesPaginated: [[ROLES0, A(6)], A(1)] } }, /modules are/],
    [{ state: { getStorageAt: `0x${'00'.repeat(31)}07` } }, /guard/],
    [{ state: { avatar: A(5) } }, /avatar is/],
    [{ logs: [...setupEvents().slice(0, 6).map(e => logOf(e.eventName, { previousOwner: ZERO, newOwner: SAFE0, initiator: A(3), owner: SAFE0,
      avatar: SAFE0, target: SAFE0, previousAvatar: ZERO, newAvatar: SAFE0, previousTarget: ZERO, newTarget: SAFE0, ...e.args }))] }, /role members are none/],
    [{ logs: [{ topics: [keccak256('0x01')], data: '0x' }] }, /does not know: undecoded/],
  ];
  for (const [o, re] of cases) {
    const p = await verifySetup(chain(o), VERIFY);
    if (re) assert.match(p.join('; '), re, String(re)); else assert.deepEqual(p, []);
  }
  assert.match((await verifySetup(chain(), { ...VERIFY, bot: A(5) })).join(), /role members are/);
  assert.match((await verifySetup(chain(), { ...VERIFY, owner: A(5) })).join(), /owners are/);
});

// --- the operator's tool and the onboarding server, over stub clients -------------------------

function stubPub({ code = {}, balance = 100n * E18, held = 10n ** 12n, modules = [], head = 77n } = {}) {
  const answers = { factory: U.V3.factory, token0: U.WPOL, token1: U.USDT0, fee: 500, getPool: POOL.pool, balanceOf: held,
    getOwners: [OWNER], getThreshold: 1n, getModulesPaginated: [modules, A(1)], getStorageAt: `0x${'00'.repeat(32)}`,
    owner: SAFE0, avatar: SAFE0, target: SAFE0 };
  return { getCode: async ({ address }) => code[getAddress(address)] ?? '0x', getBlockNumber: async () => head, getBalance: async () => balance,
    readContract: async ({ functionName }) => answers[functionName] };
}

test('cli request create: the owner\'s transactions, the same ones on a resume; check: the register JSON', async () => {
  const out = [];
  const deps = (o) => ({ U, pub: stubPub(o), account: { address: BOT }, log: (l) => out.push(JSON.parse(l)) });
  const f = { owner: OWNER, pools: POOL.pool, payout: `${PROFIT},${FEE}` };
  const r = await request(f, deps());
  const want = ownerSetupCalls({ U, owner: OWNER, bot: BOT, compiled: COMPILED, saltNonce: BigInt(SALT0) });
  assert.deepEqual([r.safe, r.roles, r.saltNonce, r.nonce, r.created, r.fromBlock, r.chainId], [SAFE0, ROLES0, SALT0, 0, false, '77', 137]);
  assert.deepEqual(r.transactions, want.txs);
  assert.deepEqual(out.at(-1), r);
  assert.equal(r.functions.length, COMPILED.functions.length);
  const one = await request({ ...f, nonce: '1' }, deps());
  assert.deepEqual([one.safe, one.roles, one.nonce], [VECTORS[1][3], VECTORS[1][4], 1]);
  // the Safe exists: only the set-up; the module exists too: the set-up without its deployment
  const created = await request(f, deps({ code: { [SAFE0]: '0x01' } }));
  assert.deepEqual([created.created, created.transactions], [true, want.txs.slice(1)]);
  const both = await request(f, deps({ code: { [SAFE0]: '0x01', [ROLES0]: '0x01' } }));
  assert.deepEqual(both.transactions, ownerSetupCalls({ U, owner: OWNER, bot: BOT, compiled: COMPILED, saltNonce: BigInt(SALT0), rolesDeployed: true }).txs.slice(1));
  const onlyRoles = await request(f, deps({ code: { [ROLES0]: '0x01' } }));
  assert.equal(onlyRoles.transactions.length, 2);
  assert.equal(unpacked(decodeFunctionData({ abi: SAFE_ABI, data: onlyRoles.transactions[1].data }).args[2]).length, roleCalls(COMPILED, ROLES0, BOT).length + 1, 'no deployment');
  await assert.rejects(request({ ...f, owner: BOT }, deps()), /cannot be an owner/);
  await assert.rejects(request({ ...f, nonce: '-1' }, deps()), /request number/);

  // check: the problems verifySetup finds, and the JSON register reads
  const deps2 = { U, pub: chain(), account: { address: BOT }, log: (l) => out.push(JSON.parse(l)) };
  const c = await check({ ...f, safe: SAFE0, roles: ROLES0, 'from-block': '40' }, deps2);
  assert.deepEqual(c.problems, []);
  assert.deepEqual(c, { sent: true, kind: 'safe', chain: 'polygon', venue: U.DEX, vault: SAFE0, settings: SAFE0, owners: [OWNER], threshold: 1,
    roles: ROLES0, policies: [ROLES0], pools: [POOL.pool], payouts: [PROFIT, FEE], role: COMPILED.role, payer: BOT, policyKey: BOT, lookup: null, problems: [] });
  const bad = await check({ ...f, payout: PROFIT, safe: SAFE0, roles: ROLES0, 'from-block': '40' }, deps2);
  assert.equal(bad.sent, false);
  assert.match(bad.problems.join(), /not as compiled/);
  assert.equal(process.exitCode, 1);
  process.exitCode = 0;
  assert.deepEqual((await check({ ...f, safe: SAFE0, roles: ROLES0, 'from-block': 0 }, deps2)).problems, []);
  for (const fb of [undefined, '', 'x', '-1', true]) await assert.rejects(check({ ...f, safe: SAFE0, roles: ROLES0, 'from-block': fb }, deps2), /--from-block/);
});

test('onboard setupTxs: the set-up still to send, the funding, the register report; verifySetup over the pinned plan', async () => {
  const C = P.resolve(cfg());
  const keys = (o) => ({ account: { address: BOT }, pub: stubPub(o) });
  const f = { owner: OWNER, profitWallet: PROFIT, feeWallet: FEE, token: 'POL', deposit: '26', gas: '4', saltNonce: SALT0 };
  const want = ownerSetupCalls({ U, owner: OWNER, bot: BOT, compiled: COMPILED, saltNonce: BigInt(SALT0) });
  const r = await P.setupTxs(keys(), C, f);
  assert.deepEqual([r.safe, r.roles, r.saltNonce, r.chainId, r.fromBlock], [SAFE0, ROLES0, SALT0, 137, '77']);
  assert.deepEqual(r.setup, want.txs);
  assert.deepEqual(r.fund, fundCalls({ U, c: C, safe: SAFE0, token: 'POL', amountRaw: 26n * E18, gasWei: 4n * E18, botKey: BOT }));
  assert.deepEqual(r.report, { sent: true, kind: 'safe', chain: 'polygon', venue: U.DEX, vault: SAFE0, settings: SAFE0, owners: [OWNER], threshold: 1,
    roles: ROLES0, policies: [ROLES0], pools: [C.pool], payouts: [PROFIT, FEE], role: COMPILED.role, payer: BOT, policyKey: BOT, lookup: null });
  // no fee wallet: the operator's own vault pays out to its profit wallet only
  const own = await P.setupTxs(keys(), C, { ...f, feeWallet: null });
  assert.deepEqual(own.report.payouts, [PROFIT]);
  assert.notDeepEqual(own.setup[1], r.setup[1]);
  assert.deepEqual(own.setup[0], r.setup[0], 'the same Safe');
  // a token deposit: the transfer and the gas
  const usdt = await P.setupTxs(keys(), C, { ...f, token: 'USDT0', deposit: '25' });
  assert.deepEqual(usdt.fund, fundCalls({ U, c: C, safe: SAFE0, token: 'USDT0', amountRaw: 25_000_000n, gasWei: 4n * E18, botKey: BOT }));
  // a resume: what is on chain is left out; enabled and deployed is done
  assert.deepEqual((await P.setupTxs(keys({ code: { [SAFE0]: '0x01' } }), C, f)).setup, want.txs.slice(1));
  const both = ownerSetupCalls({ U, owner: OWNER, bot: BOT, compiled: COMPILED, saltNonce: BigInt(SALT0), rolesDeployed: true });
  assert.deepEqual((await P.setupTxs(keys({ code: { [SAFE0]: '0x01', [ROLES0]: '0x01' } }), C, f)).setup, both.txs.slice(1));
  assert.deepEqual((await P.setupTxs(keys({ code: { [ROLES0]: '0x01' } }), C, f)).setup, both.txs);
  assert.deepEqual((await P.setupTxs(keys({ code: { [SAFE0]: '0x01', [ROLES0]: '0x01' }, modules: [ROLES0] }), C, f)).setup, []);
  assert.deepEqual((await P.setupTxs(keys({ code: { [SAFE0]: '0x01', [ROLES0]: '0x01' }, modules: [A(6)] }), C, f)).setup, both.txs.slice(1));
  assert.deepEqual((await P.setupTxs(keys({ code: { [SAFE0]: '0x01' }, modules: [ROLES0] }), C, f)).setup, want.txs.slice(1), 'enabled but no module: not done');
  // the owner's wallet must hold the deposit and the gas
  await assert.rejects(P.setupTxs(keys({ balance: 30n * E18 }), C, f), /holds 30 POL; the gas and the deposit need 30/);
  assert.equal((await P.setupTxs(keys({ balance: 30n * E18 + 1n }), C, f)).safe, SAFE0);
  assert.equal((await P.setupTxs(keys({ held: 25_000_000n }), C, { ...f, token: 'USDT0', deposit: '25' })).safe, SAFE0);
  await assert.rejects(P.setupTxs(keys({ held: 24_999_999n }), C, { ...f, token: 'USDT0', deposit: '25' }), /holds 24.999999 USDT0; the deposit is 25/);
  await assert.rejects(P.setupTxs(keys({ balance: 4n * E18 }), C, { ...f, token: 'USDT0', deposit: '25' }), /holds 4 POL/);
  await assert.rejects(P.setupTxs(keys(), C, { ...f, token: 'DAI' }), /token DAI/);
  await assert.rejects(P.setupTxs(keys(), C, { ...f, owner: BOT }), /cannot be an owner/);

  // verifySetup: the plan from the pinned config, the chain's vault against it
  const vk = { account: { address: BOT }, pub: chain() };
  const v = { owner: OWNER, safe: SAFE0.toLowerCase(), roles: ROLES0.toLowerCase(), profitWallet: PROFIT, feeWallet: FEE, fromBlock: '40' };
  assert.deepEqual(await P.verifySetup(vk, C, v), []);
  assert.match((await P.verifySetup(vk, C, { ...v, feeWallet: null })).join(), /not as compiled/);
  assert.match((await P.verifySetup(vk, C, { ...v, fromBlock: '51' })).join(), /do not reach back/);
  assert.match((await P.verifySetup({ ...vk, account: { address: A(5) } }, C, v)).join(), /role members are/);
  await assert.rejects(P.verifySetup(vk, C, { ...v, owner: BOT }), /cannot be an owner/);
});
