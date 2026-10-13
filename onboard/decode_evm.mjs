// What an owner's EVM transaction does, decoded in the page before the wallet signs it, and
// whether that is exactly what the page asked for. Browser-safe: viem and pure modules only.
//
// decodeEvm(tx, expect, pins) -> {ok, lines, problems}. `tx` = {to, data, value}, or for the
// kinds createVault and fund the list of them, in order; `pins` = {chainId, wrapped, tokens:
// {symbol: {address, decimals}}, botKey, npm} from git-pinned config, and for createVault also
// {vault (the chain module's VAULT), router, stables: [address], pool: {token0, token1, fee},
// venue, rules? (default: the bundled rules.json)}; `expect` = {kind, owner, ...} (see each
// kind below). Each call is decoded and encoded again: the bytes must be the canonical
// encoding, so no extra calldata hides in it.
import { concat, decodeFunctionData, encodeFunctionData, getAddress, isAddress, pad, parseAbi } from 'viem';
import { ERC20_ABI, NPM_ABI, WRAPPED_ABI } from '../chains/evm/uniswap_abi.mjs';
import { compile, ROLES_ABI } from '../chains/evm/vault/roles.mjs';
import { MODULE_FACTORY_ABI, SAFE_ABI as SAFE_FULL_ABI, SAFE_FACTORY_ABI } from '../chains/evm/vault/safe.mjs';
import { configureCalls, predictRoles, predictSafe } from '../chains/evm/vault/owner_setup.mjs';

const SAFE_ABI = parseAbi([
  'function execTransaction(address to, uint256 value, bytes data, uint8 operation, uint256 safeTxGas, uint256 baseGas, uint256 gasPrice, address gasToken, address refundReceiver, bytes signatures) payable returns (bool success)',
  'function enableModule(address module)',
  'function disableModule(address prevModule, address module)',
]);
const ZERO = '0x0000000000000000000000000000000000000000';
const CLOSE_STEPS = new Set(['decreaseLiquidity', 'collect', 'burn']);

class Refused extends Error {}

function same(a, b) {
  return typeof a === 'string' && typeof b === 'string' && a.toLowerCase() === b.toLowerCase();
}

// An address as the page shows it: in full, checksummed.
function shown(a) {
  return getAddress(a);
}

// A raw amount as an exact decimal string. Integer maths only.
export function formatRaw(raw, decimals) {
  const d = 10n ** BigInt(decimals);
  const f = (raw % d).toString().padStart(decimals, '0').replace(/0+$/, '');
  return f ? `${raw / d}.${f}` : `${raw / d}`;
}

// The Safe's signature for a transaction its owner sends itself (safe.mjs preValidated).
export function preValidated(owner) {
  return concat([pad(getAddress(owner)), pad('0x00'), '0x01']);
}

// A value field as a bigint: hex, decimal string, number or bigint; absent is 0.
function valueOf(v) {
  if (v === undefined || v === null || v === '') return 0n;
  if (typeof v === 'bigint') return v;
  if (typeof v === 'number') {
    if (!Number.isSafeInteger(v) || v < 0) throw new Refused(`the value ${v} is not a whole number of wei`);
    return BigInt(v);
  }
  if (typeof v === 'string' && /^(0x[0-9a-fA-F]+|\d+)$/.test(v)) return BigInt(v);
  throw new Refused(`the value ${String(v)} is not a number`);
}

function hexData(d) {
  if (d === undefined || d === null || d === '') return '0x';
  if (typeof d !== 'string' || !/^0x([0-9a-fA-F]{2})*$/.test(d)) throw new Refused('the data is not hex bytes');
  return d.toLowerCase();
}

// One call of `abi`: its name and arguments, only when `data` is that call's exact encoding.
function decodeExact(abi, data, what) {
  let d;
  try { d = decodeFunctionData({ abi, data }); } catch { throw new Refused(`${what}: an unknown function (selector ${data.slice(0, 10)})`); }
  let again = null;
  try { again = encodeFunctionData({ abi, functionName: d.functionName, args: d.args }); } catch { /* a value out of its type's range */ }
  if (again?.toLowerCase() !== data.toLowerCase()) {
    throw new Refused(`${what}: the calldata carries extra or non-standard bytes`);
  }
  return d;
}

// The symbol of the native coin: the one token with no address.
function nativeSymbol(pins) {
  const hit = Object.entries(pins.tokens ?? {}).find(([, t]) => !t.address);
  return hit ? hit[0] : 'POL';
}

// The wrapped native token: pins.wrapped is its address, {address} or {SYMBOL: address}.
function wrappedOf(pins) {
  const w = pins.wrapped;
  if (typeof w === 'string') return { address: w, symbol: tokenAt(pins, w)?.[0] ?? 'WPOL' };
  if (w && typeof w.address === 'string') return { address: w.address, symbol: w.symbol ?? tokenAt(pins, w.address)?.[0] ?? 'WPOL' };
  const [symbol, address] = Object.entries(w ?? {})[0] ?? [];
  if (typeof address !== 'string') throw new Refused('the page has no wrapped token pinned');
  return { address, symbol };
}

function tokenAt(pins, address) {
  return Object.entries(pins.tokens ?? {}).find(([, t]) => t.address && same(t.address, address));
}

// The outer Safe transaction: to the expected Safe, no value, a CALL (the set-up: a delegatecall
// of MultiSendCallOnly, `operation` 1) by the owner with no gas refund. Returns the inner {to, value, data}.
function safeCall(tx, expect, problems, operation0 = 0) {
  if (!same(tx.to, expect.safe)) throw new Refused(`the transaction goes to ${tx.to}, not your vault ${expect.safe}`);
  if (valueOf(tx.value) !== 0n) problems.push(`the transaction sends ${formatRaw(valueOf(tx.value), 18)} of your coin to the vault`);
  const d = decodeExact(SAFE_ABI, hexData(tx.data), 'the vault transaction');
  if (d.functionName !== 'execTransaction') throw new Refused(`the vault transaction calls ${d.functionName}, not execTransaction`);
  const [to, value, data, operation, safeTxGas, baseGas, gasPrice, gasToken, refundReceiver, signatures] = d.args;
  if (operation !== operation0) {
    problems.push(operation === 1 ? `the vault transaction is a delegatecall (operation ${operation}): it could run any code as the vault`
      : `the vault transaction is operation ${operation}, not a delegatecall of MultiSendCallOnly`);
  }
  if (safeTxGas !== 0n || baseGas !== 0n || gasPrice !== 0n) problems.push('the vault transaction pays a gas refund (safeTxGas, baseGas or gasPrice is not zero)');
  if (!same(gasToken, ZERO) || !same(refundReceiver, ZERO)) problems.push('the vault transaction names a gas token or refund receiver');
  if (!same(signatures, preValidated(expect.owner))) problems.push(`the vault transaction is not signed as your wallet ${expect.owner} sending it`);
  return { to, value, data: data.toLowerCase() };
}

// A plain coin transfer to the bot's key: the gas payment.
function payment(tx, expect, pins, lines, problems) {
  const want = valueOf(expect.lamports), sym = nativeSymbol(pins);
  if (!same(tx.to, pins.botKey)) problems.push(`the payment goes to ${tx.to}, not the bot's key ${pins.botKey}`);
  if (hexData(tx.data) !== '0x') problems.push('the payment carries calldata: it is not a plain transfer');
  const got = valueOf(tx.value);
  if (got !== want) problems.push(`the payment is ${formatRaw(got, 18)} ${sym}, not ${formatRaw(want, 18)} ${sym}`);
  lines.push(`Pay ${formatRaw(got, 18)} ${sym} from your wallet ${shown(expect.owner)} to the bot's key ${shown(tx.to)}`);
}

// Wrap the coin (WPOL.deposit with the value) or send a token to the vault.
function deposit(tx, expect, pins, lines, problems) {
  const t = Object.hasOwn(pins.tokens ?? {}, expect.token) ? pins.tokens[expect.token] : undefined;
  if (!t) throw new Refused(`the token ${expect.token} is not one the vault takes`);
  const want = valueOf(expect.raw), data = hexData(tx.data);
  if (!t.address) {
    const w = wrappedOf(pins);
    if (!same(tx.to, w.address)) throw new Refused(`the wrap goes to ${tx.to}, not ${w.symbol} ${w.address}`);
    const d = decodeExact(WRAPPED_ABI, data, 'the wrap');
    if (d.functionName !== 'deposit') throw new Refused(`the wrap calls ${d.functionName}, not deposit`);
    const got = valueOf(tx.value);
    if (got !== want) problems.push(`the wrap is ${formatRaw(got, 18)} ${expect.token}, not ${formatRaw(want, 18)}`);
    lines.push(`Wrap ${formatRaw(got, 18)} ${expect.token} into ${w.symbol} in your wallet ${shown(expect.owner)}`);
    return;
  }
  if (!same(tx.to, t.address)) throw new Refused(`the deposit goes to ${tx.to}, not the ${expect.token} token ${t.address}`);
  if (valueOf(tx.value) !== 0n) problems.push('the deposit also sends coin');
  const d = decodeExact(ERC20_ABI, data, 'the deposit');
  if (d.functionName !== 'transfer') throw new Refused(`the deposit calls ${d.functionName}, not transfer`);
  const [to, raw] = d.args;
  if (!same(to, expect.safe)) problems.push(`the deposit sends to ${to}, not your vault ${expect.safe}`);
  if (raw !== want) problems.push(`the deposit is ${formatRaw(raw, t.decimals)} ${expect.token}, not ${formatRaw(want, t.decimals)}`);
  lines.push(`Deposit ${formatRaw(raw, t.decimals)} ${expect.token} from your wallet ${shown(expect.owner)} into your vault ${shown(to)}`);
}

// From the vault to the owner: a token transfer, or the coin itself. raw 'all': any amount;
// token 'ALL': any token of the vault.
function withdraw(tx, expect, pins, lines, problems) {
  const inner = safeCall(tx, expect, problems);
  const anyToken = String(expect.token).toUpperCase() === 'ALL', anyAmount = String(expect.raw).toLowerCase() === 'all';
  const want = anyAmount ? null : valueOf(expect.raw);
  const hit = tokenAt(pins, inner.to);
  if (hit && (anyToken || hit[0] === expect.token)) {
    const [sym, t] = hit;
    if (inner.value !== 0n) problems.push('the withdrawal also sends coin from the vault');
    const d = decodeExact(ERC20_ABI, inner.data, 'the withdrawal');
    if (d.functionName !== 'transfer') throw new Refused(`the withdrawal calls ${d.functionName}, not transfer`);
    const [to, raw] = d.args;
    if (!same(to, expect.owner)) problems.push(`the withdrawal sends to ${to}, not your wallet ${expect.owner}`);
    if (want !== null && raw !== want) problems.push(`the withdrawal is ${formatRaw(raw, t.decimals)} ${sym}, not ${formatRaw(want, t.decimals)}`);
    if (raw === 0n) problems.push('the withdrawal moves nothing');
    lines.push(`Withdraw ${formatRaw(raw, t.decimals)} ${sym} from your vault ${shown(expect.safe)} to your wallet ${shown(to)}`);
    return;
  }
  const sym = nativeSymbol(pins);
  if (!anyToken && expect.token !== sym) throw new Refused(`the withdrawal calls ${inner.to}, not the ${expect.token} token`);
  if (!same(inner.to, expect.owner)) throw new Refused(`the withdrawal sends ${sym} to ${inner.to}, not your wallet ${expect.owner}`);
  if (inner.data !== '0x') problems.push('the coin withdrawal carries calldata');
  if (want !== null && inner.value !== want) problems.push(`the withdrawal is ${formatRaw(inner.value, 18)} ${sym}, not ${formatRaw(want, 18)}`);
  if (inner.value === 0n) problems.push('the withdrawal moves nothing');
  lines.push(`Withdraw ${formatRaw(inner.value, 18)} ${sym} from your vault ${shown(expect.safe)} to your wallet ${shown(inner.to)}`);
}

// The vault disables the bot's Roles module.
function revoke(tx, expect, pins, lines, problems) {
  const inner = moduleCall(tx, expect, problems, 'disableModule');
  lines.push(`Revoke the bot: your vault ${shown(expect.safe)} disables its Roles module ${shown(inner.module)}`);
}

// The vault enables the bot's Roles module again.
function approve(tx, expect, pins, lines, problems) {
  const inner = moduleCall(tx, expect, problems, 'enableModule');
  lines.push(`Approve the bot again: your vault ${shown(expect.safe)} enables its Roles module ${shown(inner.module)}`);
}

// The vault closes positions on the position manager: decrease, collect to the vault, burn.
function close(tx, expect, pins, lines, problems) {
  const inner = safeCall(tx, expect, problems);
  if (!same(inner.to, pins.npm)) throw new Refused(`the close calls ${inner.to}, not the position manager ${pins.npm}`);
  if (inner.value !== 0n) problems.push('the close sends coin from the vault');
  const d = decodeExact(NPM_ABI, inner.data, 'the close');
  if (d.functionName !== 'multicall') throw new Refused(`the close calls ${d.functionName}, not multicall`);
  if (!d.args[0].length) problems.push('the close does nothing');
  for (const step of d.args[0]) {
    const s = decodeExact(NPM_ABI, step.toLowerCase(), 'a close step');
    if (!CLOSE_STEPS.has(s.functionName)) throw new Refused(`a close step calls ${s.functionName}: only decreaseLiquidity, collect and burn`);
    const [p] = s.args;
    if (s.functionName === 'decreaseLiquidity') lines.push(`Remove liquidity ${p.liquidity} from position #${p.tokenId}`);
    if (s.functionName === 'collect') {
      if (!same(p.recipient, expect.safe)) problems.push(`position #${p.tokenId} is collected to ${p.recipient}, not your vault ${expect.safe}`);
      lines.push(`Collect position #${p.tokenId}'s tokens to ${shown(p.recipient)}`);
    }
    if (s.functionName === 'burn') lines.push(`Burn the emptied position #${p}`);
  }
  lines.unshift(`Close positions of your vault ${shown(expect.safe)}; the tokens stay in the vault`);
}

// enableModule/disableModule of the expected Roles module, run by the vault on itself.
function moduleCall(tx, expect, problems, name) {
  const inner = safeCall(tx, expect, problems);
  if (!same(inner.to, expect.safe)) throw new Refused(`the vault calls ${inner.to}, not itself`);
  if (inner.value !== 0n) problems.push('the module change sends coin from the vault');
  const d = decodeExact(SAFE_ABI, inner.data, 'the module change');
  if (d.functionName !== name) throw new Refused(`the vault calls ${d.functionName}, not ${name}`);
  const module = d.args.at(-1);
  if (!same(module, expect.roles)) problems.push(`the module is ${module}, not the bot's Roles module ${expect.roles}`);
  return { module };
}

// The vault contracts the page trusts: the pinned VAULT addresses, every one an address.
function vaultPins(pins) {
  const v = pins.vault || {};
  for (const k of ['safeSingleton', 'safeFactory', 'fallbackHandler', 'multiSendCallOnly', 'rolesMastercopy', 'moduleFactory']) {
    if (!isAddress(String(v[k]), { strict: false })) throw new Refused(`the page has no ${k} pinned`);
  }
  return v;
}

// MultiSendCallOnly's packed list as [{operation, to, value, data}].
function unpack(packed) {
  const hex = packed.toLowerCase(), calls = [];
  for (let i = 2; i < hex.length;) {
    const n = hex.length - i < 170 ? -1n : BigInt(`0x${hex.slice(i + 106, i + 170)}`);
    if (n < 0n || BigInt(hex.length - i - 170) < n * 2n) throw new Refused('the set-up\'s list of calls is cut short');
    calls.push({ operation: parseInt(hex.slice(i, i + 2), 16), to: getAddress(`0x${hex.slice(i + 2, i + 42)}`),
      value: BigInt(`0x${hex.slice(i + 42, i + 106)}`), data: `0x${hex.slice(i + 170, i + 170 + Number(n) * 2)}` });
    i += 170 + Number(n) * 2;
  }
  return calls;
}

// A call of the set-up by name, for a problem.
function callName(call) {
  for (const abi of [ROLES_ABI, SAFE_FULL_ABI, MODULE_FACTORY_ABI]) {
    try { return `${decodeFunctionData({ abi, data: call.data }).functionName} on ${call.to}`; } catch { /* another ABI */ }
  }
  return `${call.data.slice(0, 10)} on ${call.to}`;
}

const MULTISEND_ABI = parseAbi(['function multiSend(bytes transactions) payable']);

// The owner creates its own vault: (1) the Safe, the owner alone at threshold 1, at expect.safe;
// (2) one Safe transaction, a delegatecall of the pinned MultiSendCallOnly whose calls are
// exactly the Roles module, its enabling and the bot's role as the pinned rules compile it for
// this owner, bot, pool and payout wallets. (1) is left out on a resume (the Safe exists); (2)
// may leave out the module's deployment (it exists). expect = {owner, safe, saltNonce,
// profitWallet, feeWallet?}.
function createVault(txs, expect, pins, lines, problems) {
  const V = vaultPins(pins), U = { VAULT: V };
  if (txs.length > 2) throw new Refused(`the vault set-up is ${txs.length} transactions, not 2`);
  if (!/^\d+$/.test(String(expect.saltNonce))) throw new Refused('the page knows no salt for your vault');
  const saltNonce = BigInt(expect.saltNonce), owner = getAddress(expect.owner);
  const safe = predictSafe({ U, owner, saltNonce });
  if (!same(safe, expect.safe)) throw new Refused(`your vault would be ${safe}, not ${expect.safe}`);
  if (txs.length === 2) {
    const tx = txs[0];
    if (!same(tx.to, V.safeFactory)) throw new Refused(`the vault is created by ${tx.to}, not the Safe factory ${V.safeFactory}`);
    if (valueOf(tx.value) !== 0n) problems.push('the vault creation sends coin');
    const d = decodeExact(SAFE_FACTORY_ABI, hexData(tx.data), 'the vault creation');
    const [singleton, initializer, salt] = d.args;
    if (!same(singleton, V.safeSingleton)) problems.push(`the vault is a copy of ${singleton}, not Safe 1.4.1 ${V.safeSingleton}`);
    const s = decodeExact(SAFE_FULL_ABI, initializer.toLowerCase(), 'the vault\'s set-up');
    if (s.functionName !== 'setup') throw new Refused(`the vault's set-up calls ${s.functionName}, not setup`);
    const [owners, threshold, to, data, handler, paymentToken, payment, receiver] = s.args;
    if (owners.length !== 1 || !same(owners[0], owner)) problems.push(`the vault's owners are ${owners.join(', ') || 'none'}, not your wallet alone`);
    if (threshold !== 1n) problems.push(`the vault needs ${threshold} signatures, not 1`);
    if (!same(to, ZERO) || data !== '0x') problems.push('the vault\'s set-up runs a call');
    if (!same(handler, V.fallbackHandler)) problems.push(`the vault's fallback handler is ${handler}, not ${V.fallbackHandler}`);
    if (!same(paymentToken, ZERO) || payment !== 0n || !same(receiver, ZERO)) problems.push('the vault\'s set-up pays someone');
    if (salt !== saltNonce) problems.push(`the vault's salt is ${salt}, not ${saltNonce}`);
    else if (!same(predictSafe({ U, initializer, saltNonce }), safe)) problems.push(`the vault created is not your vault ${shown(safe)}`);
    lines.push(`Create your vault ${shown(safe)}: a Safe 1.4.1 with your wallet ${shown(owner)} its only owner`);
  }
  const roles = predictRoles({ U, safe, saltNonce });
  const payouts = [expect.profitWallet, ...(expect.feeWallet ? [expect.feeWallet] : [])];
  if (!payouts.every(w => isAddress(String(w), { strict: false }))) throw new Refused('the page knows no payout wallet');
  const { token0, token1, fee } = pins.pool;
  const compiled = compile(pins.venue, { npm: pins.npm, router: pins.router, stables: new Set((pins.stables || []).map(a => a.toLowerCase())),
    pools: [{ token0, token1, fee }], payouts }, pins.rules);
  const inner = safeCall(txs.at(-1), { ...expect, safe }, problems, 1);
  if (!same(inner.to, V.multiSendCallOnly)) throw new Refused(`the set-up runs ${inner.to}, not MultiSendCallOnly ${V.multiSendCallOnly}`);
  if (inner.value !== 0n) problems.push('the set-up sends coin from the vault');
  const m = decodeExact(MULTISEND_ABI, inner.data, 'the set-up');
  const calls = unpack(m.args[0]);
  const deploys = calls.length > 0 && same(calls[0].to, V.moduleFactory);
  const want = configureCalls({ U, safe, roles, compiled, bot: pins.botKey, saltNonce, rolesDeployed: !deploys });
  if (calls.length !== want.length) problems.push(`the set-up makes ${calls.length} calls, not ${want.length}`);
  calls.forEach((c, i) => {
    if (c.operation !== 0) problems.push(`call ${i + 1} is a delegatecall`);
    if (c.value !== 0n) problems.push(`call ${i + 1} sends coin`);
    const w = want[i];
    if (!w || !same(c.to, w.to) || c.data !== w.data.toLowerCase()) problems.push(`call ${i + 1} (${callName(c)}) is not what the pinned rules give`);
  });
  lines.push(`Set your vault up: ${deploys ? 'create and ' : ''}enable the bot's Roles module ${shown(roles)}`);
  lines.push(`Give the bot's key ${shown(pins.botKey)} only these calls: ${[...new Set(compiled.functions.map(f => f.name))].join('; ')}`);
  lines.push(`Pay-outs only to ${payouts.map(shown).join(' and ')}`);
}

// The owner funds the vault: the deposit (a wrap first for the native coin), then the bot's gas.
// expect = {owner, safe, token, raw, gasWei}.
function fund(txs, expect, pins, lines, problems) {
  const t = Object.hasOwn(pins.tokens || {}, expect.token) ? pins.tokens[expect.token] : undefined;
  if (!t) throw new Refused(`the token ${expect.token} is not one the vault takes`);
  const steps = t.address ? [expect] : [expect, { ...expect, token: wrappedOf(pins).symbol }];
  if (txs.length !== steps.length + 1) throw new Refused(`the funding is ${txs.length} transactions, not ${steps.length + 1}`);
  steps.forEach((e, i) => deposit(txs[i], e, pins, lines, problems));
  payment(txs.at(-1), { ...expect, lamports: expect.gasWei }, pins, lines, problems);
}

const KINDS = { payment, deposit, withdraw, revoke, approve, close };
const LISTS = { createVault, fund };

// What every transaction carries: an address to go to, the pinned chain, the owner's wallet.
function envelope(tx, expect, pins, problems) {
  if (!tx || typeof tx !== 'object') throw new Refused('no transaction');
  if (!isAddress(String(tx.to), { strict: false })) throw new Refused(`the transaction goes to ${tx.to}, not an address`);
  if (tx.chainId !== undefined && Number(tx.chainId) !== Number(pins.chainId)) problems.push(`the transaction is for chain ${tx.chainId}, not ${pins.chainId}`);
  if (tx.from !== undefined && !same(tx.from, expect.owner)) problems.push(`the transaction is sent by ${tx.from}, not your wallet ${expect.owner}`);
}

export function decodeEvm(tx, expect, pins) {
  const lines = [], problems = [];
  try {
    if (!tx || typeof tx !== 'object') throw new Refused('no transaction');
    const list = Object.hasOwn(LISTS, String(expect?.kind));
    if (!expect || !(list || Object.hasOwn(KINDS, expect.kind))) throw new Refused(`the page expects no transaction of kind ${expect?.kind}`);
    if (list !== Array.isArray(tx)) throw new Refused(list ? `a ${expect.kind} is a list of transactions` : 'one transaction, not a list');
    if (!tx.length && list) throw new Refused('no transaction');
    for (const t of list ? tx : [tx]) envelope(t, expect, pins, problems);
    if (!isAddress(String(expect.owner), { strict: false })) throw new Refused('the page knows no wallet for you');
    (list ? LISTS : KINDS)[expect.kind](tx, expect, pins, lines, problems);
  } catch (e) {
    if (!(e instanceof Refused)) throw e;
    problems.push(e.message);
  }
  return { ok: problems.length === 0, lines, problems };
}
