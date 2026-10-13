// The owner's own vault: the owner's wallet creates the Safe and sets it up. The bot's key is
// never an owner and pays nothing.
//
// It takes two owner transactions, not one. The Safe's address is a hash of its initializer
// (SafeProxyFactory CREATE2), and the Roles module's address is a hash of its own initializer,
// which names the Safe (owner, avatar, target). An initializer that enabled the module or
// granted its role would name the module's address, so each address would be a hash of the
// other. So:
//   1. createProxyWithNonce: the Safe, the owner alone at threshold 1, nothing else;
//   2. one Safe transaction the owner sends (a pre-validated signature): a delegatecall of
//      MultiSendCallOnly whose calls deploy the Roles module (owner, avatar, target = the
//      Safe), enable it, and give the bot's key its role (roles.mjs roleCalls).
// A wallet with EIP-5792 (wallet_sendCalls) sends both, and the funding, in one approval.
//
// Pure but verifySetup (it reads the chain). Browser-safe: the owner's page builds the same
// bytes to check what it signs (onboard/decode_evm.mjs).
import { concat, decodeEventLog, encodeFunctionData, encodePacked, getAddress, getContractAddress, hexToBigInt, isAddress,
  keccak256, pad, parseAbi, toHex } from 'viem';
import { roleCalls } from './roles.mjs';
import { MODULE_FACTORY_ABI, SAFE_ABI, SAFE_FACTORY_ABI, checkOwners, connectProblems, multiSend, preValidated, readState,
  rolesSetup } from './safe.mjs';
import { ERC20_ABI, WRAPPED_ABI } from '../uniswap_abi.mjs';

const ZERO = '0x0000000000000000000000000000000000000000';
const DELEGATECALL = 1;

// SafeProxy 1.4.1's creation code (SafeProxyFactory.proxyCreationCode(), the same on every chain).
export const SAFE_PROXY_CODE = '0x608060405234801561001057600080fd5b506040516101e63803806101e68339818101604052602081101561003357600080fd5b8101908080519060200190929190505050600073ffffffffffffffffffffffffffffffffffffffff168173ffffffffffffffffffffffffffffffffffffffff1614156100ca576040517f08c379a00000000000000000000000000000000000000000000000000000000081526004018080602001828103825260228152602001806101c46022913960400191505060405180910390fd5b806000806101000a81548173ffffffffffffffffffffffffffffffffffffffff021916908373ffffffffffffffffffffffffffffffffffffffff1602179055505060ab806101196000396000f3fe608060405273ffffffffffffffffffffffffffffffffffffffff600054167fa619486e0000000000000000000000000000000000000000000000000000000060003514156050578060005260206000f35b3660008037600080366000845af43d6000803e60008114156070573d6000fd5b3d6000f3fea264697066735822122003d1488ee65e08fa41e58e888a9865554c535f2c77126a82cb4c0f917f31441364736f6c63430007060033496e76616c69642073696e676c65746f6e20616464726573732070726f7669646564';

// The Zodiac ModuleProxyFactory's proxy around a mastercopy: creation code, and the code it leaves.
function moduleProxyCode(mastercopy) {
  return concat(['0x602d8060093d393df3363d3d373d3d3d363d73', getAddress(mastercopy), '0x5af43d82803e903d91602b57fd5bf3']);
}
export function moduleRuntime(mastercopy) {
  return concat(['0x363d3d373d3d3d363d73', getAddress(mastercopy), '0x5af43d82803e903d91602b57fd5bf3']).toLowerCase();
}

// Both factories salt CREATE2 with keccak256(keccak256(initializer) ++ saltNonce).
function salted(initializer, saltNonce) {
  return keccak256(encodePacked(['bytes32', 'uint256'], [keccak256(initializer), BigInt(saltNonce)]));
}

// The salt nonce of an owner's n-th vault request: the same owner and n give the same addresses,
// so a resumed request rebuilds the same transactions.
export function saltOf(owner, n) {
  if (!Number.isSafeInteger(n) || n < 0) throw new Error(`request number ${n}: a whole number from 0`);
  return hexToBigInt(keccak256(encodePacked(['string', 'address', 'uint256'], ['lp-bot vault', getAddress(owner), BigInt(n)])));
}

// The Safe's initializer: the owner alone at threshold 1, the fallback handler, no module call,
// no payment.
export function safeInitializer(U, owner) {
  return encodeFunctionData({ abi: SAFE_ABI, functionName: 'setup', args: [[getAddress(owner)], 1n, ZERO, '0x', U.VAULT.fallbackHandler, ZERO, 0n, ZERO] });
}

// The Safe that createProxyWithNonce(singleton, initializer, saltNonce) creates. `owner` gives the
// initializer when none is passed.
export function predictSafe({ U, owner, initializer = safeInitializer(U, owner), saltNonce }) {
  return getContractAddress({ opcode: 'CREATE2', from: U.VAULT.safeFactory, salt: salted(initializer, saltNonce),
    bytecode: concat([SAFE_PROXY_CODE, pad(U.VAULT.safeSingleton)]) });
}

// The Roles module that deployModule(rolesMastercopy, rolesSetup(safe), saltNonce) creates.
export function predictRoles({ U, safe, saltNonce }) {
  return getContractAddress({ opcode: 'CREATE2', from: U.VAULT.moduleFactory, salt: salted(rolesSetup(getAddress(safe)), saltNonce),
    bytecode: moduleProxyCode(U.VAULT.rolesMastercopy) });
}

// The calls of the Safe transaction (2): the module (left out when it exists already: anyone
// can deploy it, and the address holds only this module, the Safe its owner), enable, the role.
export function configureCalls({ U, safe, roles, compiled, bot, saltNonce, rolesDeployed = false }) {
  const self = (functionName, args) => ({ to: getAddress(safe), data: encodeFunctionData({ abi: SAFE_ABI, functionName, args }) });
  const deploy = { to: U.VAULT.moduleFactory, data: encodeFunctionData({ abi: MODULE_FACTORY_ABI, functionName: 'deployModule',
    args: [U.VAULT.rolesMastercopy, rolesSetup(getAddress(safe)), BigInt(saltNonce)] }) };
  return [...(rolesDeployed ? [] : [deploy]), self('enableModule', [getAddress(roles)]), ...roleCalls(compiled, roles, getAddress(bot))];
}

// The owner's two transactions: {safe, roles, txs: [create, configure]}, each {to, data, value}.
export function ownerSetupCalls({ U, owner, bot, compiled, saltNonce, rolesDeployed = false }) {
  const [who] = checkOwners([owner], 1, [bot]);
  const initializer = safeInitializer(U, who);
  const safe = predictSafe({ U, initializer, saltNonce }), roles = predictRoles({ U, safe, saltNonce });
  const create = { to: U.VAULT.safeFactory, value: '0x0', data: encodeFunctionData({ abi: SAFE_FACTORY_ABI, functionName: 'createProxyWithNonce',
    args: [U.VAULT.safeSingleton, initializer, BigInt(saltNonce)] }) };
  const calls = configureCalls({ U, safe, roles, compiled, bot, saltNonce, rolesDeployed });
  const configure = { to: safe, value: '0x0', data: encodeFunctionData({ abi: SAFE_ABI, functionName: 'execTransaction',
    args: [U.VAULT.multiSendCallOnly, 0n, multiSend(calls), DELEGATECALL, 0n, 0n, 0n, ZERO, ZERO, preValidated(who)] }) };
  return { safe, roles, txs: [create, configure] };
}

// A token of the config (c.tokens, onboard/) or of the chain module: {address} or, for the
// native coin, {wrapped}.
function fundToken(U, c, token) {
  if (c) {
    const t = Object.hasOwn(c.tokens, token) ? c.tokens[token] : null;
    if (!t) throw new Error(`token ${token} is not one the vault takes`);
    return t.native ? { wrapped: c.tokens[t.wrapsTo].address } : { address: t.address };
  }
  if (token === U.NATIVE_SYMBOL) return { wrapped: U.WRAPPED_NATIVE };
  if (typeof U[token] === 'string' && isAddress(U[token])) return { address: U[token] };
  throw new Error(`token ${token} is not one the vault takes`);
}

// The owner's funding, in order: the deposit (the native coin is wrapped first: the vault never
// wraps), then the bot's gas LAST. An EOA's transactions land in nonce order, so a gas payment
// on chain means every deposit before it is on chain too (each still checked: it may revert).
export function fundCalls({ U, c, safe, token, amountRaw, gasWei, botKey }) {
  const raw = BigInt(amountRaw), wei = BigInt(gasWei);
  if (raw <= 0n) throw new Error('the deposit must be more than 0');
  if (wei <= 0n) throw new Error('the bot\'s gas must be more than 0');
  const t = fundToken(U, c, token), to = getAddress(safe);
  const transfer = (address) => ({ to: getAddress(address), value: '0x0', data: encodeFunctionData({ abi: ERC20_ABI, functionName: 'transfer', args: [to, raw] }) });
  const deposit = t.wrapped
    ? [{ to: getAddress(t.wrapped), value: toHex(raw), data: encodeFunctionData({ abi: WRAPPED_ABI, functionName: 'deposit' }) }, transfer(t.wrapped)]
    : [transfer(t.address)];
  return [...deposit, { to: getAddress(botKey), value: toHex(wei), data: '0x' }];
}

// The Roles module's events that set up or change who may do what.
export const ROLES_EVENTS = parseAbi([
  'event RolesModSetup(address indexed initiator, address indexed owner, address indexed avatar, address target)',
  'event OwnershipTransferred(address indexed previousOwner, address indexed newOwner)',
  'event AvatarSet(address indexed previousAvatar, address indexed newAvatar)',
  'event TargetSet(address indexed previousTarget, address indexed newTarget)',
  'event Initialized(uint64 version)',
  'event AssignRoles(address module, bytes32[] roleKeys, bool[] memberOf)',
  'event AllowTarget(bytes32 roleKey, address targetAddress, uint8 options)',
  'event RevokeTarget(bytes32 roleKey, address targetAddress)',
  'event ScopeTarget(bytes32 roleKey, address targetAddress)',
  'event AllowFunction(bytes32 roleKey, address targetAddress, bytes4 selector, uint8 options)',
  'event RevokeFunction(bytes32 roleKey, address targetAddress, bytes4 selector)',
  'event ScopeFunction(bytes32 roleKey, address targetAddress, bytes4 selector, (uint8 parent, uint8 paramType, uint8 operator, bytes compValue)[] conditions, uint8 options)',
  'event SetAllowance(bytes32 allowanceKey, uint128 balance, uint128 maxRefill, uint128 refill, uint64 period, uint64 timestamp)',
  'event SetDefaultRole(address module, bytes32 defaultRoleKey)',
  'event SetUnwrapAdapter(address to, bytes4 selector, address adapter)',
  'event EnabledModule(address module)',
  'event DisabledModule(address module)',
]);
// Set-up events whose outcome readState checks (owner, avatar, target).
const STATE_EVENTS = new Set(['RolesModSetup', 'OwnershipTransferred', 'AvatarSet', 'TargetSet', 'Initialized']);

// What the role holds after these events ([{eventName, args}], oldest first), against the
// compiled role with the bot's key its only member. [] when exactly the same. Pure.
export function grantProblems(events, compiled, bot) {
  const problems = [];
  const lc = (a) => String(a).toLowerCase();
  if (!events.some(e => e.eventName === 'RolesModSetup')) problems.push('the events do not reach back to the Roles module\'s set-up');
  const members = new Map(), targets = new Map(), functions = new Map(), modules = new Set();
  for (const e of events) {
    const a = e.args;
    const fn = () => `${lc(a.roleKey)} ${lc(a.targetAddress)} ${lc(a.selector)}`;
    switch (e.eventName) {
      case 'AssignRoles': a.roleKeys.forEach((k, i) => members.set(`${lc(k)} ${lc(a.module)}`, a.memberOf[i])); break;
      case 'AllowTarget': targets.set(`${lc(a.roleKey)} ${lc(a.targetAddress)}`, `allow ${a.options}`); break;
      case 'ScopeTarget': targets.set(`${lc(a.roleKey)} ${lc(a.targetAddress)}`, 'scope'); break;
      case 'RevokeTarget': targets.delete(`${lc(a.roleKey)} ${lc(a.targetAddress)}`); break;
      case 'AllowFunction': functions.set(fn(), `allow ${a.options}`); break;
      case 'ScopeFunction': functions.set(fn(), `scope ${a.options} ${JSON.stringify(a.conditions.map(c => [c.parent, c.paramType, c.operator, lc(c.compValue)]))}`); break;
      case 'RevokeFunction': functions.delete(fn()); break;
      // assignRoles enables the member as a module of the Roles module: the bot's key alone
      case 'EnabledModule': modules.add(lc(a.module)); break;
      case 'DisabledModule': modules.delete(lc(a.module)); break;
      default:
        if (!STATE_EVENTS.has(e.eventName)) problems.push(`the Roles module has an event the check does not know: ${e.eventName}`);
    }
  }
  const role = lc(compiled.role);
  const memberOf = [...members].filter(([, on]) => on).map(([k]) => k);
  if (memberOf.join() !== `${role} ${lc(bot)}`) problems.push(`the role members are ${memberOf.join(', ') || 'none'}, expected the bot's key ${bot} alone`);
  if ([...modules].join() !== lc(bot)) problems.push(`the Roles module's modules are ${[...modules].join(', ') || 'none'}, expected the bot's key ${bot} alone`);
  const want = new Map(compiled.targets.map(t => [`${role} ${lc(t)}`, 'scope']));
  const wantFn = new Map(compiled.functions.map(f => [`${role} ${lc(f.target)} ${lc(f.selector)}`, f.conditions
    ? `scope 0 ${JSON.stringify(f.conditions.map(c => [c.parent, c.paramType, c.operator, lc(c.compValue)]))}` : 'allow 0']));
  for (const [what, have, need] of [['target', targets, want], ['function', functions, wantFn]]) {
    for (const [k, v] of have) if (need.get(k) !== v) problems.push(`the role's ${what} ${k} is ${v}, not as compiled`);
    for (const k of need.keys()) if (!have.has(k)) problems.push(`the role lacks the ${what} ${k}`);
  }
  return problems;
}

// The Roles module's logs from `fromBlock` (at or before its set-up) to the head, in windows
// public endpoints answer.
async function rolesLogs(pub, roles, fromBlock, step = 10_000n) {
  const head = await pub.getBlockNumber({ cacheTime: 0 });
  const out = [];
  for (let a = BigInt(fromBlock); a <= head; a += step) {
    const b = a + step - 1n < head ? a + step - 1n : head;
    out.push(...await pub.getLogs({ address: roles, fromBlock: a, toBlock: b }));
  }
  return out;
}

// What is wrong with an owner's vault on chain: the Safe a SafeL2 1.4.1 proxy whose owner alone
// signs at threshold 1, the Roles module its one module and no guard, the module ours, owned by
// the Safe and acting on it, and its role exactly the compiled one with the bot's key its only
// member (from the module's events since `fromBlock`, a block at or before the setup). [] when right.
export async function verifySetup(pub, { U, safe, roles, owner, bot, compiled, fromBlock }) {
  if (fromBlock === undefined || fromBlock === null) throw new Error('verifySetup needs fromBlock: a block at or before the setup');
  const [safeCode, rolesCode, singleton] = await Promise.all([pub.getCode({ address: safe }), pub.getCode({ address: roles }),
    pub.getStorageAt({ address: safe, slot: '0x0' })]);
  if (!safeCode || safeCode === '0x') return [`no Safe at ${safe}`];
  if (!rolesCode || rolesCode === '0x') return [`no Roles module at ${roles}`];
  const problems = [];
  if (hexToBigInt(singleton) !== hexToBigInt(U.VAULT.safeSingleton)) problems.push(`the Safe's singleton is not SafeL2 1.4.1 ${U.VAULT.safeSingleton}`);
  if (rolesCode.toLowerCase() !== moduleRuntime(U.VAULT.rolesMastercopy)) problems.push(`the module ${roles} is not a proxy of the Roles mastercopy ${U.VAULT.rolesMastercopy}`);
  problems.push(...connectProblems(await readState(pub, safe, roles), { safe, roles, owners: [owner], threshold: 1, botKeys: [bot] }));
  const events = (await rolesLogs(pub, roles, fromBlock)).map(log => {
    try { return decodeEventLog({ abi: ROLES_EVENTS, data: log.data, topics: log.topics }); } catch { return { eventName: 'undecoded', args: {} }; }
  });
  problems.push(...grantProblems(events, compiled, bot));
  return problems;
}
