// The Safe of a vault: what connects it (Safe, Roles module, role, owners) and what checks
// it. The bot's key creates the Safe as its only owner, sets everything up in one Safe
// transaction, adds the owners and removes itself: no owner signs anything to connect.
// Encoders are pure; readState is the one network read.
import { concat, encodeAbiParameters, encodeFunctionData, encodePacked, getAddress, pad, parseAbi, size } from 'viem';
import { roleCalls, ROLES_ABI } from './roles.mjs';

export const SAFE_ABI = parseAbi([
  'function setup(address[] owners, uint256 threshold, address to, bytes data, address fallbackHandler, address paymentToken, uint256 payment, address paymentReceiver)',
  'function execTransaction(address to, uint256 value, bytes data, uint8 operation, uint256 safeTxGas, uint256 baseGas, uint256 gasPrice, address gasToken, address refundReceiver, bytes signatures) payable returns (bool success)',
  'function getOwners() view returns (address[])',
  'function getThreshold() view returns (uint256)',
  'function getModulesPaginated(address start, uint256 pageSize) view returns (address[] array, address next)',
  'function enableModule(address module)',
  'function disableModule(address prevModule, address module)',
  'function addOwnerWithThreshold(address owner, uint256 threshold)',
  'function removeOwner(address prevOwner, address owner, uint256 threshold)',
  'function getStorageAt(uint256 offset, uint256 length) view returns (bytes)',
]);
export const SAFE_FACTORY_ABI = parseAbi([
  'function createProxyWithNonce(address singleton, bytes initializer, uint256 saltNonce) returns (address proxy)',
]);
export const MODULE_FACTORY_ABI = parseAbi([
  'function deployModule(address masterCopy, bytes initializeParams, uint256 saltNonce) returns (address proxy)',
]);
const MULTISEND_ABI = parseAbi(['function multiSend(bytes transactions) payable']);

export const SENTINEL = '0x0000000000000000000000000000000000000001';
const ZERO = '0x0000000000000000000000000000000000000000';
const CALL = 0, DELEGATECALL = 1;
// keccak256("guard_manager.guard.address"): the Safe's transaction guard slot (GuardManager.sol)
const GUARD_SLOT = 0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8n;

// A Safe signature that needs no signing: valid when the owner itself sends the transaction.
export function preValidated(owner) {
  return concat([pad(getAddress(owner)), pad('0x00'), '0x01']);
}

// MultiSendCallOnly's packed list: operation, to, value, data length, data, per call.
export function multiSend(calls) {
  if (!calls.length) throw new Error('a multisend of no calls');
  return encodeFunctionData({ abi: MULTISEND_ABI, functionName: 'multiSend', args: [concat(calls.map(c =>
    encodePacked(['uint8', 'address', 'uint256', 'uint256', 'bytes'], [CALL, getAddress(c.to), 0n, BigInt(size(c.data)), c.data])))] });
}

// The Safe's initializer: the bot alone, threshold 1, the 1.4.1 fallback handler.
export const safeSetup = (bot, fallbackHandler) => encodeFunctionData({ abi: SAFE_ABI, functionName: 'setup',
  args: [[getAddress(bot)], 1n, ZERO, '0x', getAddress(fallbackHandler), ZERO, 0n, ZERO] });

// The Roles module's initializer: the Safe owns it, and it acts on the Safe.
export const rolesSetup = (safe) => encodeFunctionData({ abi: ROLES_ABI, functionName: 'setUp',
  args: [encodeAbiParameters([{ type: 'address' }, { type: 'address' }, { type: 'address' }], [safe, safe, safe])] });

// Every call of the set-up, in order: the module, the role, the owners, the bot leaves.
// Owners are added at the head of the Safe's list, so the first one added comes before the bot.
export function setupCalls({ safe, roles, compiled, bot, owners, threshold }) {
  const self = (functionName, args) => ({ to: getAddress(safe), data: encodeFunctionData({ abi: SAFE_ABI, functionName, args }) });
  return [
    self('enableModule', [roles]),
    ...roleCalls(compiled, roles, bot),
    ...owners.map(o => self('addOwnerWithThreshold', [getAddress(o), 1n])),
    self('removeOwner', [getAddress(owners[0]), getAddress(bot), BigInt(threshold)]),
  ];
}

// One Safe transaction from its sole owner `bot`: a delegatecall of MultiSendCallOnly.
export function execAsOwner({ multiSendCallOnly, calls, bot }) {
  return encodeFunctionData({ abi: SAFE_ABI, functionName: 'execTransaction',
    args: [getAddress(multiSendCallOnly), 0n, multiSend(calls), DELEGATECALL, 0n, 0n, 0n, ZERO, ZERO, preValidated(bot)] });
}

export function checkOwners(owners, threshold, botKeys) {
  const list = owners.map(o => getAddress(o));
  if (!list.length) throw new Error('a vault needs at least one owner');
  if (new Set(list).size !== list.length) throw new Error('an owner is listed twice');
  for (const k of botKeys) if (list.includes(getAddress(k))) throw new Error(`${k} is the bot's key: it cannot be an owner`);
  if (!(Number.isInteger(threshold) && threshold >= 1 && threshold <= list.length)) throw new Error(`threshold ${threshold} for ${list.length} owners`);
  return list;
}

// What the chain says of a vault.
export async function readState(pub, safe, roles) {
  const [owners, threshold, [modules], guard, rolesOwner, avatar, target] = await Promise.all([
    pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getOwners' }),
    pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getThreshold' }),
    pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getModulesPaginated', args: [SENTINEL, 10n] }),
    pub.readContract({ address: safe, abi: SAFE_ABI, functionName: 'getStorageAt', args: [GUARD_SLOT, 1n] }),
    pub.readContract({ address: roles, abi: ROLES_ABI, functionName: 'owner' }),
    pub.readContract({ address: roles, abi: ROLES_ABI, functionName: 'avatar' }),
    pub.readContract({ address: roles, abi: ROLES_ABI, functionName: 'target' }),
  ]);
  return { owners, threshold: Number(threshold), modules, guard: BigInt(guard) === 0n ? null : guard, rolesOwner, avatar, target };
}

// What is wrong with a vault just connected: the bot an owner, other owners or threshold,
// another module, a guard, a Roles module that is not the Safe's own. [] when right.
export function connectProblems(state, { safe, roles, owners, threshold, botKeys }) {
  const same = (a, b) => String(a).toLowerCase() === String(b).toLowerCase();
  const problems = [];
  const have = state.owners.map(o => o.toLowerCase()).sort(), want = owners.map(o => o.toLowerCase()).sort();
  for (const k of botKeys) if (have.includes(k.toLowerCase())) problems.push(`the bot's key ${k} is an owner`);
  if (have.join() !== want.join()) problems.push(`owners are ${state.owners.join(', ')}, expected ${owners.join(', ')}`);
  if (state.threshold !== threshold) problems.push(`threshold is ${state.threshold}, expected ${threshold}`);
  if (state.modules.length !== 1 || !same(state.modules[0], roles)) problems.push(`modules are ${state.modules.join(', ') || 'none'}, expected the Roles module ${roles}`);
  if (state.guard) problems.push(`a transaction guard is set: ${state.guard}`);
  for (const [what, v] of [['owner', state.rolesOwner], ['avatar', state.avatar], ['target', state.target]]) {
    if (!same(v, safe)) problems.push(`the Roles module's ${what} is ${v}, not the Safe`);
  }
  return problems;
}
