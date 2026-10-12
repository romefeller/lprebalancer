// What an owner's EVM transaction does, decoded in the page before the wallet signs it, and
// whether that is exactly what the page asked for. Browser-safe: viem and pure modules only.
//
// decodeEvm(tx, expect, pins) -> {ok, lines, problems}. `tx` = {to, data, value}; `pins` =
// {chainId, wrapped, tokens: {symbol: {address, decimals}}, botKey, npm} from git-pinned
// config; `expect` = {kind, owner, ...} (see each kind below). Each call is decoded and
// encoded again: the bytes must be the canonical encoding, so no extra calldata hides in it.
import { concat, decodeFunctionData, encodeFunctionData, getAddress, isAddress, pad, parseAbi } from 'viem';
import { ERC20_ABI, NPM_ABI, WRAPPED_ABI } from '../chains/evm/uniswap_abi.mjs';

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

// The outer Safe transaction: to the expected Safe, no value, a CALL by the owner with no gas
// refund. Returns the inner {to, value, data}.
function safeCall(tx, expect, problems) {
  if (!same(tx.to, expect.safe)) throw new Refused(`the transaction goes to ${tx.to}, not your vault ${expect.safe}`);
  if (valueOf(tx.value) !== 0n) problems.push(`the transaction sends ${formatRaw(valueOf(tx.value), 18)} of your coin to the vault`);
  const d = decodeExact(SAFE_ABI, hexData(tx.data), 'the vault transaction');
  if (d.functionName !== 'execTransaction') throw new Refused(`the vault transaction calls ${d.functionName}, not execTransaction`);
  const [to, value, data, operation, safeTxGas, baseGas, gasPrice, gasToken, refundReceiver, signatures] = d.args;
  if (operation !== 0) problems.push(`the vault transaction is a delegatecall (operation ${operation}): it could run any code as the vault`);
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

const KINDS = { payment, deposit, withdraw, revoke, approve, close };

export function decodeEvm(tx, expect, pins) {
  const lines = [], problems = [];
  try {
    if (!tx || typeof tx !== 'object') throw new Refused('no transaction');
    if (!expect || !Object.hasOwn(KINDS, expect.kind)) throw new Refused(`the page expects no transaction of kind ${expect?.kind}`);
    if (!isAddress(String(tx.to), { strict: false })) throw new Refused(`the transaction goes to ${tx.to}, not an address`);
    if (!isAddress(String(expect.owner), { strict: false })) throw new Refused('the page knows no wallet for you');
    if (tx.chainId !== undefined && Number(tx.chainId) !== Number(pins.chainId)) problems.push(`the transaction is for chain ${tx.chainId}, not ${pins.chainId}`);
    if (tx.from !== undefined && !same(tx.from, expect.owner)) problems.push(`the transaction is sent by ${tx.from}, not your wallet ${expect.owner}`);
    KINDS[expect.kind](tx, expect, pins, lines, problems);
  } catch (e) {
    if (!(e instanceof Refused)) throw e;
    problems.push(e.message);
  }
  return { ok: problems.length === 0, lines, problems };
}
