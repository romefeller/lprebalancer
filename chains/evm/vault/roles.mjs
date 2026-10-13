// The bot's role on a Safe vault, compiled from rules.json for one vault: which functions of
// which contracts it may call, and what each argument must be (Zodiac Roles v2 conditions).
// Pure: no network, so every scope is testable before anything is signed. Browser-safe (the
// owner's page compiles the same role to check the setup it signs).
//
// A condition is a tree; the contract takes it flattened, breadth first (ConditionFlat). The
// checks in `integrity` mirror the contract's Integrity.sol, so a wrong tree fails here and
// not in a transaction.
import { encodeAbiParameters, encodeFunctionData, getAddress, parseAbi, parseAbiItem, stringToHex, toFunctionSelector } from 'viem';
import RULES from './rules.json' with { type: 'json' };

export { RULES };

// Zodiac Roles v2 enums (contracts/Types.sol of the deployed mastercopy).
export const TYPE = Object.freeze({ None: 0, Static: 1, Dynamic: 2, Tuple: 3, Array: 4, Calldata: 5, AbiEncoded: 6 });
export const OP = Object.freeze({ Pass: 0, And: 1, Or: 2, Nor: 3, Matches: 5, EqualToAvatar: 15, EqualTo: 16 });
const EXEC_NONE = 0;              // ExecutionOptions.None: no value, no delegatecall

export const ROLES_ABI = parseAbi([
  'function setUp(bytes initParams)',
  'function owner() view returns (address)',
  'function avatar() view returns (address)',
  'function target() view returns (address)',
  'function assignRoles(address module, bytes32[] roleKeys, bool[] memberOf)',
  'function scopeTarget(bytes32 roleKey, address targetAddress)',
  'function revokeTarget(bytes32 roleKey, address targetAddress)',
  'function allowFunction(bytes32 roleKey, address targetAddress, bytes4 selector, uint8 options)',
  'function scopeFunction(bytes32 roleKey, address targetAddress, bytes4 selector, (uint8 parent, uint8 paramType, uint8 operator, bytes compValue)[] conditions, uint8 options)',
  'function execTransactionWithRole(address to, uint256 value, bytes data, uint8 operation, bytes32 roleKey, bool shouldRevert) returns (bool success)',
]);

export class NotAllowed extends Error {}

// The role of a venue: its name as a bytes32, so the chain shows what it is.
export const roleKey = (venue) => stringToHex(venue, { size: 32 });

const word = (type, value) => encodeAbiParameters([{ type }], [value]);
const leaf = (operator, compValue = '0x') => ({ paramType: TYPE.Static, operator, compValue, children: [] });
const pass = (abi) => shape(abi);

// A Pass tree with the structure of `abi` (a tuple keeps its fields).
function shape(abi) {
  if (abi.type === 'tuple') return { paramType: TYPE.Tuple, operator: OP.Matches, compValue: '0x', children: abi.components.map(shape) };
  if (abi.type === 'bytes' || abi.type === 'string' || abi.type.endsWith(']')) throw new Error(`argument type ${abi.type} is not supported`);
  return leaf(OP.Pass);
}

// One of `nodes`: the node itself when there is one, an Or of them otherwise.
function oneOf(nodes) {
  if (!nodes.length) throw new Error('a choice of nothing');
  return nodes.length === 1 ? nodes[0] : { paramType: TYPE.None, operator: OP.Or, compValue: '0x', children: nodes };
}

// Equal to one of `values` (each once).
function equal(type, values) {
  const distinct = [...new Set(values.map(v => String(v).toLowerCase()))];
  return oneOf(distinct.map(v => leaf(OP.EqualTo, word(type, type === 'address' ? getAddress(v) : BigInt(v)))));
}

// The condition of one argument. `pool` is set inside a perPool tuple.
function condition(spec, abi, ctx, pool) {
  if (spec === null) return pass(abi);
  if (Array.isArray(spec)) {
    if (abi.type !== 'tuple' || spec.length !== abi.components.length) throw new Error(`tuple rule of ${spec.length} fields for ${abi.type}`);
    return { paramType: TYPE.Tuple, operator: OP.Matches, compValue: '0x', children: spec.map((s, i) => condition(s, abi.components[i], ctx, pool)) };
  }
  if (typeof spec === 'object') {
    if (!Array.isArray(spec.perPool)) throw new Error(`unknown rule ${JSON.stringify(spec)}`);
    return oneOf(ctx.pools.map(p => condition(spec.perPool, abi, ctx, p)));
  }
  const need = (list, what) => { if (!list?.length) throw new Error(`role ${spec} needs at least one ${what}`); return list; };
  switch (spec) {
    case 'avatar': return leaf(OP.EqualToAvatar);
    case 'spender': return equal(abi.type, [ctx.npm, ctx.router]);
    case 'payout': return equal(abi.type, need(ctx.payouts, 'payout wallet'));
    case 'token0': case 'token1': case 'fee':
      if (!pool) throw new Error(`role ${spec} is only valid inside perPool`);
      return equal(abi.type, [pool[spec]]);
    case 'pairToken':
      if (!pool) throw new Error('role pairToken is only valid inside perPool');
      return equal(abi.type, [pool.token0, pool.token1]);
    default: throw new Error(`unknown role ${spec}`);
  }
}

// Breadth first, as the contract stores it: [{parent, paramType, operator, compValue}].
export function flatten(root) {
  const out = [], queue = [[root, 0]];
  while (queue.length) {
    const [node, parent] = queue.shift();
    const index = out.length;
    out.push({ parent, paramType: node.paramType, operator: node.operator, compValue: node.compValue });
    for (const c of node.children) queue.push([c, index]);
  }
  return out;
}

// The contract's Integrity.enforce, for the operators this module writes. Throws on the first
// fault, naming its index.
export function integrity(flat) {
  if (!flat.length || flat[0].parent !== 0 || flat.filter((c, i) => c.parent === i).length !== 1) throw new Error('unsuitable root node');
  if (flat[0].paramType !== TYPE.Calldata || flat[0].operator !== OP.Matches) throw new Error('the root must match the calldata');
  flat.forEach((c, i) => {
    if (i && flat[i - 1].parent > c.parent) throw new Error('not breadth first');
    const children = flat.filter((x, j) => j && x.parent === i).length;
    const bytes = (c.compValue.length - 2) / 2;
    switch (c.operator) {
      case OP.Pass: if (bytes) throw new Error(`${i}: a pass has no value`); break;
      case OP.Or: if (c.paramType !== TYPE.None || bytes || children < 1) throw new Error(`${i}: unsuitable or`); break;
      case OP.Matches: if (![TYPE.Tuple, TYPE.Calldata].includes(c.paramType) || bytes || !children) throw new Error(`${i}: unsuitable match`); break;
      case OP.EqualToAvatar: if (c.paramType !== TYPE.Static || bytes) throw new Error(`${i}: unsuitable avatar check`); break;
      case OP.EqualTo: if (c.paramType !== TYPE.Static || bytes !== 32) throw new Error(`${i}: unsuitable equality`); break;
      default: throw new Error(`${i}: operator ${c.operator} is not written here`);
    }
    if (c.paramType === TYPE.Static && children) throw new Error(`${i}: a static value has no children`);
  });
  return flat;
}

// The approved pools' tokens, and those the chain lists as stables (the payout tokens).
function targetsOf(kind, ctx) {
  const tokens = [...new Set(ctx.pools.flatMap(p => [p.token0, p.token1]).map(t => getAddress(t)))];
  switch (kind) {
    case 'poolToken': return tokens;
    case 'payoutToken': return tokens.filter(t => ctx.stables.has(t.toLowerCase()));
    case 'npm': return [getAddress(ctx.npm)];
    case 'router': return [getAddress(ctx.router)];
    default: throw new Error(`unknown target ${kind}`);
  }
}

// The rules of `venue` (and payout, when the vault has payout wallets).
export function rulesFor(venue, withPayout, rules = RULES) {
  const v = rules.venues[venue];
  if (!v) throw new Error(`no vault rules for venue ${venue} (have: ${Object.keys(rules.venues).join(', ')})`);
  return [...v.functions, ...(withPayout ? rules.payout : [])];
}

// Compile `venue`'s rules for one vault. `ctx` = {npm, router, stables (Set of lower-case
// addresses), pools: [{token0, token1, fee}], payouts: [address]}. Returns
// {venue, role, targets: [address], functions: [{name, target, selector, signature, conditions|null}]};
// conditions null means every argument is allowed.
export function compile(venue, ctx, rules = RULES) {
  if (!ctx.pools?.length) throw new Error('a vault needs at least one approved pool');
  const functions = [];
  for (const rule of rulesFor(venue, Boolean(ctx.payouts?.length), rules)) {
    const item = parseAbiItem(`function ${rule.signature}`);
    const selector = toFunctionSelector(rule.signature);
    let conditions = null;
    if (rule.params !== 'any') {
      if (!Array.isArray(rule.params) || rule.params.length !== item.inputs.length) throw new Error(`${rule.name}: ${item.inputs.length} arguments, ${rule.params?.length} rules`);
      const root = { paramType: TYPE.Calldata, operator: OP.Matches, compValue: '0x',
        children: rule.params.map((s, i) => condition(s, item.inputs[i], ctx)) };
      conditions = integrity(flatten(root));
    }
    for (const target of targetsOf(rule.target, ctx)) functions.push({ name: rule.name, target, selector, signature: rule.signature, conditions });
  }
  return { venue, role: roleKey(venue), targets: [...new Set(functions.map(f => f.target))], functions };
}

// The calls that set the role up on the Roles module (the Safe, its owner, makes them):
// the bot becomes the role's member, each target is scoped, each function scoped or allowed.
export function roleCalls(compiled, roles, member) {
  const call = (functionName, args) => ({ to: getAddress(roles), data: encodeFunctionData({ abi: ROLES_ABI, functionName, args }) });
  const role = compiled.role;
  return [
    call('assignRoles', [member, [role], [true]]),
    ...compiled.targets.map(t => call('scopeTarget', [role, t])),
    ...compiled.functions.map(f => (f.conditions
      ? call('scopeFunction', [role, f.target, f.selector, f.conditions, EXEC_NONE])
      : call('allowFunction', [role, f.target, f.selector, EXEC_NONE]))),
  ];
}

// Whether the compiled role lets `call` ({to, data, value}) through, as far as addresses and
// selectors go (argument values are the contract's to check). Throws NotAllowed otherwise.
export function allows(compiled, call) {
  if (call.value && BigInt(call.value) !== 0n) throw new NotAllowed('a vault call carries no value');
  const selector = String(call.data).slice(0, 10).toLowerCase();
  const f = compiled.functions.find(x => x.target.toLowerCase() === String(call.to).toLowerCase() && x.selector === selector);
  if (!f) throw new NotAllowed(`call ${selector} to ${call.to} is not in the ${compiled.venue} vault role`);
  return f;
}
