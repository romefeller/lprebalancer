// Polygon for the onboarding server: the bot's key and gas, the connect (a
// Safe and its Roles module, chains/evm/vault/), and the owner's transactions
// (deposit, withdraw, revoke, approve, close) as {to, data, value} for the
// owner's wallet to send. A Safe with one owner at threshold 1 runs a
// transaction its owner sends with a pre-validated signature: no Safe app needed.
import { concat, encodeAbiParameters, encodeFunctionData, getAddress, parseEventLogs, toFunctionSelector, toHex } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { readKey } from '../chains/evm/keyfile.mjs';
import { clients, connect, poolOf } from '../chains/evm/vault/cli.mjs';
import { SAFE_ABI, SENTINEL, readState } from '../chains/evm/vault/safe.mjs';
import { ROLES_ABI, roleKey } from '../chains/evm/vault/roles.mjs';
import { ownerCloseCalls, ownerExec, regrantCall, vaultPositions } from '../chains/evm/vault/exit.mjs';
import * as POLYGON from '../chains/evm/polygon.mjs';
import { Invalid, fromRaw, nativeOf, own, toRaw } from './intent.mjs';

// The chain's config with each token's address from the chain module.
export function resolve(c, U = POLYGON) {
  const tokens = Object.fromEntries(Object.entries(c.tokens).map(([sym, t]) => {
    if (t.native) return [sym, { ...t, address: null }];
    if (!U[sym]) throw new Error(`token ${sym} is not an export of the ${U.NAME} module`);
    return [sym, { ...t, address: getAddress(U[sym]) }];
  }));
  if (!tokens[c.payoutToken]?.address) throw new Error(`payoutToken ${c.payoutToken} is not one of the tokens`);
  return { ...c, U, tokens, pool: getAddress(c.pool), operatorWallet: getAddress(c.operatorWallet) };
}

// The bot's key and its clients. `deps` (tests) replaces the clients.
export function botKeys(c, env = process.env, deps = null) {
  if (deps) return { ...deps, payee: deps.account.address, public: [deps.account.address], secretEnv: c.keyEnv, policyKeyEnv: c.keyEnv };
  const path = env[c.keyEnv];
  if (!path) throw new Error(`${c.keyEnv} must name the bot's EVM key file`);
  const k = clients({ ...env, WALLET_SECRET_PATH: path, LPBOT_CHAIN: c.U.CHAIN });
  const address = privateKeyToAccount(readKey(path)).address;
  return { ...k, payee: address, public: [address], secretEnv: c.keyEnv, policyKeyEnv: c.keyEnv };
}

// The key's POL against one setup (measured cost and margin, config).
export async function gas(keys, c) {
  const wei = await keys.pub.getBalance({ address: keys.account.address });
  const need = toRaw(c.gas.setup, 18) + toRaw(c.gas.margin, 18);
  return { ok: wei >= need, have: fromRaw(wei, 18), need: fromRaw(need, 18), symbol: c.U.NATIVE_SYMBOL, key: keys.account.address };
}

// The owner's gas payment: one POL transfer to the bot's key. It refuses
// when the wallet cannot also cover the deposit that follows.
export async function paymentTxs(keys, c, { owner, payer, gas: amount, token, deposit }) {
  const wei = toRaw(amount, 18), t = own(c.tokens, token);
  const pol = await keys.pub.getBalance({ address: owner });
  const needPol = wei + (t.native ? toRaw(deposit, 18) : 0n);
  if (pol <= needPol) throw new Invalid(`your wallet holds ${fromRaw(pol, 18)} POL; the gas and the deposit need ${fromRaw(needPol, 18)} and fees`);
  if (!t.native) {
    const have = await balanceOf(keys.pub, t.address, owner);
    if (have < toRaw(deposit, t.decimals)) throw new Invalid(`your wallet holds ${fromRaw(have, t.decimals)} ${token}; the deposit is ${deposit}`);
  }
  return { summary: `pay the bot ${amount} POL for the setup and the running gas, to ${payer}`, chainId: c.U.CHAIN_ID,
    transactions: [call(payer, '0x', wei)] };
}

// What is wrong with a gas payment, from its receipt, transaction, block and
// the chain's head: {state: 'ok' | 'pending' | 'bad', reason}. Pure.
export function paymentVerdict({ receipt, tx, block, head }, { owner, payer, wei, notBefore, confirmations }) {
  if (!receipt || !tx) return { state: 'pending', reason: 'the payment is not on chain (yet)' };
  if (receipt.status !== 'success') return { state: 'bad', reason: 'the payment failed on chain' };
  if (head - receipt.blockNumber + 1n < BigInt(confirmations)) return { state: 'pending', reason: 'the payment is not confirmed yet' };
  if (String(tx.from).toLowerCase() !== owner.toLowerCase()) return { state: 'bad', reason: `the payment is not from the owner ${owner}` };
  if (String(tx.to).toLowerCase() !== payer.toLowerCase()) return { state: 'bad', reason: `the payment does not go to the bot's key ${payer}` };
  if (tx.value < wei) return { state: 'bad', reason: `the payment is ${fromRaw(tx.value, 18)} POL, less than ${fromRaw(wei, 18)}` };
  if (!(Number(block.timestamp) >= notBefore)) return { state: 'bad', reason: 'the payment is older than this request' };
  return { state: 'ok' };
}

export async function checkPayment(keys, c, hash, expect) {
  if (!/^0x[0-9a-fA-F]{64}$/.test(String(hash))) return { state: 'bad', reason: 'not a transaction hash' };
  const got = (p) => p.catch(e => { if (/not (be )?found|could not be found/i.test(String(e?.message))) return null; throw e; });
  const receipt = await got(keys.pub.getTransactionReceipt({ hash }));
  const tx = receipt && await got(keys.pub.getTransaction({ hash }));
  const block = receipt && await keys.pub.getBlock({ blockNumber: receipt.blockNumber });
  const head = await keys.pub.getBlockNumber({ cacheTime: 0 });
  return paymentVerdict({ receipt, tx, block, head }, { ...expect, wei: toRaw(expect.amount, 18), confirmations: c.gas.confirmations });
}

// The pool's tokens in pool order, as add_profile's record (addresses in
// lower case, as the profile rows keep them).
export async function poolRecord(keys, c) {
  const p = await poolOf(keys.pub, c.U, c.pool);
  const side = (a) => {
    const hit = Object.entries(c.tokens).find(([, t]) => t.address && t.address.toLowerCase() === a.toLowerCase());
    if (!hit) throw new Error(`pool ${c.pool} holds ${a}, not one of ${c.pair}`);
    return { address: a.toLowerCase(), symbol: hit[0] };
  };
  return { token_a: side(p.token0), token_b: side(p.token1) };
}

export async function connectVault(keys, c, f, log) {
  const venue = c.venues[0];
  const wallets = [f.profitWallet, ...(f.feeWallet ? [f.feeWallet] : [])];
  const out = await connect({ owners: f.owner, pools: c.pool, payout: wallets.join(','), venue, execute: true },
    { U: c.U, account: keys.account, pub: keys.pub, wallet: keys.wallet, log });
  if (out.problems?.length) throw Object.assign(new Error(`the vault is not as planned: ${out.problems.join('; ')}`), { connected: { ...out, sent: false } });
  return out;
}

const erc20 = (functionName, args) => encodeFunctionData({ abi: POLYGON.ERC20_ABI, functionName, args });
const balanceOf = (pub, token, who) => pub.readContract({ address: token, abi: POLYGON.ERC20_ABI, functionName: 'balanceOf', args: [who] });
const call = (to, data, value = 0n) => ({ to: getAddress(to), data, value: toHex(value) });

// What the owner's wallet holds of POL and of `token`, in raw units by
// symbol (intent.mjs maxAmount).
export async function holdings(keys, c, owner, token) {
  const t = own(c.tokens, token);
  const held = { [nativeOf(c)[0]]: await keys.pub.getBalance({ address: owner }) };
  if (!t.native) held[token] = await balanceOf(keys.pub, t.address, owner);
  return held;
}

// The deposit: a token transfer to the Safe; native POL is wrapped first
// (the vault never wraps), then the wrapped token is sent.
export async function depositTxs(keys, c, { owner, vault, token, amount }) {
  const t = own(c.tokens, token);
  if (!t) throw new Invalid(`token ${token}`);
  const raw = toRaw(amount, t.decimals), safe = getAddress(vault);
  if (t.native) {
    const w = c.tokens[t.wrapsTo];
    const have = await keys.pub.getBalance({ address: owner });
    if (have <= raw) throw new Invalid(`your wallet holds ${fromRaw(have, 18)} ${token}; the deposit is ${amount} and gas`);
    return { summary: `wrap ${amount} ${token} into ${t.wrapsTo}, then deposit it into your vault ${safe}`, chainId: c.U.CHAIN_ID,
      transactions: [call(w.address, encodeFunctionData({ abi: POLYGON.WRAPPED_ABI, functionName: 'deposit' }), raw),
        call(w.address, erc20('transfer', [safe, raw]))] };
  }
  const have = await balanceOf(keys.pub, t.address, owner);
  if (have < raw) throw new Invalid(`your wallet holds ${fromRaw(have, t.decimals)} ${token}; the deposit is ${amount}`);
  return { summary: `deposit ${amount} ${token} into your vault ${safe}`, chainId: c.U.CHAIN_ID,
    transactions: [call(t.address, erc20('transfer', [safe, raw]))] };
}

// One Safe transaction the owner sends itself (threshold 1, pre-validated).
const execAsSoleOwner = (safe, owner, to, data, value = 0n) => ownerExec({ safe, owner, to, data, value });

// The vault row's Safe, Roles module, venue and pools.
function vaultOf(row) {
  const [venue, v] = Object.entries(row.venues)[0];
  return { safe: getAddress(row.settings), roles: getAddress(v.policies[0]), venue, pools: v.pools.map(p => getAddress(p)) };
}

// The Safe's open positions on the vault's pools: [{pool, tokenId, liquidity, owed0, owed1}],
// numbers as decimal strings.
export async function positions(keys, c, row) {
  const { safe, pools } = vaultOf(row);
  const out = [];
  for (const pool of pools) {
    for (const p of await vaultPositions(keys.pub, { U: c.U, safe, pool })) {
      out.push({ pool, tokenId: String(p.tokenId), liquidity: String(p.liquidity), owed0: String(p.owed0), owed1: String(p.owed1) });
    }
  }
  return out;
}

// The revert data of a failed eth_call, or null.
function revertData(e) {
  for (let x = e; x; x = x.cause) if (typeof x.data === 'string' && /^0x[0-9a-fA-F]*$/.test(x.data)) return x.data;
  return null;
}
// The reverts of a call the role let through: the Safe refuses a disabled module (GS104),
// or the inner call fails (the Roles module's ModuleTransactionFailed).
const PASSED_ROLE = new Set([toFunctionSelector('ModuleTransactionFailed()'),
  concat([toFunctionSelector('Error(string)'), encodeAbiParameters([{ type: 'string' }], ['GS104'])])]);

// Whether the Roles module still lets the bot's key make its role's calls: an approve of
// the position manager for 0, simulated. Any other revert (NoMembership, a condition) is no.
export async function botHasRole(keys, c, { roles, venue, pools }) {
  const { token0 } = await poolOf(keys.pub, c.U, pools[0]);
  const data = encodeFunctionData({ abi: ROLES_ABI, functionName: 'execTransactionWithRole',
    args: [token0, 0n, erc20('approve', [c.U.V3.npm, 0n]), 0, roleKey(venue), true] });
  try {
    await keys.pub.request({ method: 'eth_call', params: [{ from: keys.account.address, to: roles, data }, 'latest'] });
    return true;
  } catch (e) {
    const d = revertData(e);
    if (d === null) throw e;
    return PASSED_ROLE.has(d.toLowerCase());
  }
}

// Everything the vault holds, all to the owner: each ERC20 of the config, then its POL.
async function withdrawAll(keys, c, safe, owner) {
  const txs = [], parts = [];
  for (const [sym, t] of Object.entries(c.tokens)) {
    if (t.native) continue;
    const have = await balanceOf(keys.pub, t.address, safe);
    if (have > 0n) {
      txs.push(execAsSoleOwner(safe, owner, t.address, erc20('transfer', [getAddress(owner), have])));
      parts.push(`${fromRaw(have, t.decimals)} ${sym}`);
    }
  }
  const pol = await keys.pub.getBalance({ address: safe });
  if (pol > 0n) {
    txs.push(execAsSoleOwner(safe, owner, owner, '0x', pol));
    parts.push(`${fromRaw(pol, 18)} ${c.U.NATIVE_SYMBOL}`);
  }
  const summary = txs.length ? `withdraw everything from the vault ${safe} to ${owner}: ${parts.join(', ')}` : `the vault ${safe} holds nothing to withdraw`;
  return { summary, chainId: c.U.CHAIN_ID, transactions: txs };
}

function isAll(s) {
  return String(s).toLowerCase() === 'all';
}

export async function ownerTxs(keys, c, row, { action, owner, token, amount }) {
  const v = vaultOf(row), { safe, roles } = v;
  const st = await readState(keys.pub, safe, roles);
  if (!st.owners.some(o => o.toLowerCase() === owner.toLowerCase())) throw new Invalid(`${owner} is not an owner of this vault`);
  if (st.threshold !== 1) throw new Invalid(`the vault needs ${st.threshold} owner signatures; use app.safe.global`);
  const enabled = st.modules.findIndex(m => m.toLowerCase() === roles.toLowerCase());
  if (action === 'withdraw') {
    if (isAll(token)) {
      if (!isAll(amount)) throw new Invalid('token ALL takes amount ALL');
      return withdrawAll(keys, c, safe, owner);
    }
    const t = own(c.tokens, token);
    if (!t) throw new Invalid(`token ${token}`);
    const have = t.native ? await keys.pub.getBalance({ address: safe }) : await balanceOf(keys.pub, t.address, safe);
    const raw = isAll(amount) ? have : toRaw(amount, t.decimals);
    if (!(raw > 0n && raw <= have)) throw new Invalid(`the vault holds ${fromRaw(have, t.decimals)} ${token}`);
    const tx = t.native ? execAsSoleOwner(safe, owner, owner, '0x', raw) : execAsSoleOwner(safe, owner, t.address, erc20('transfer', [getAddress(owner), raw]));
    return { summary: `withdraw ${fromRaw(raw, t.decimals)} ${token} from the vault ${safe} to ${owner}`, chainId: c.U.CHAIN_ID, transactions: [tx] };
  }
  if (action === 'revoke') {
    if (enabled < 0) throw new Invalid('the bot is already revoked: the Roles module is not enabled');
    const prev = enabled === 0 ? SENTINEL : st.modules[enabled - 1];
    const data = encodeFunctionData({ abi: SAFE_ABI, functionName: 'disableModule', args: [prev, roles] });
    return { summary: `revoke the bot: disable the Roles module ${roles} of the vault ${safe}`, chainId: c.U.CHAIN_ID,
      transactions: [execAsSoleOwner(safe, owner, safe, data)] };
  }
  if (action === 'approve') {
    if (enabled >= 0) throw new Invalid('the bot is not revoked: the Roles module is enabled');
    for (const [what, x] of [['owner', st.rolesOwner], ['avatar', st.avatar], ['target', st.target]]) {
      if (x.toLowerCase() !== safe.toLowerCase()) throw new Invalid(`the Roles module's ${what} is ${x}, not the vault`);
    }
    if (!await botHasRole(keys, c, v)) throw new Invalid(`the Roles module ${roles} no longer gives the bot's key ${keys.account.address} its role`);
    return { summary: `approve the bot again: enable the Roles module ${roles} of the vault ${safe}`, chainId: c.U.CHAIN_ID,
      transactions: [regrantCall({ safe, owner, roles })] };
  }
  if (action === 'close') {
    const txs = [];
    for (const pool of v.pools) txs.push(...await ownerCloseCalls(keys.pub, { U: c.U, safe, owner, pool }));
    const summary = txs.length ? `close ${txs.length} position(s) of the vault ${safe}: the tokens stay in the vault`
      : `the vault ${safe} has no open position: nothing to close`;
    return { summary, chainId: c.U.CHAIN_ID, transactions: txs };
  }
  throw new Invalid(`action ${action}`);
}

// What a deposit moved, from its receipt and the chain's head: the ERC20 Transfer logs from
// `owner` to `safe` of a configured token, one entry per log that moves something.
// {state: 'ok', deposits: [{token, amount}]} | {state: 'pending' | 'bad', reason}. Pure.
export function depositVerdict({ receipt, head }, { owner, safe, tokens, confirmations }) {
  if (!receipt) return { state: 'pending', reason: 'the deposit is not on chain (yet)' };
  if (receipt.status !== 'success') return { state: 'bad', reason: 'the deposit failed on chain' };
  if (head - receipt.blockNumber + 1n < BigInt(confirmations)) return { state: 'pending', reason: 'the deposit is not confirmed yet' };
  const deposits = [];
  for (const log of parseEventLogs({ abi: POLYGON.ERC20_ABI, eventName: 'Transfer', logs: receipt.logs, strict: false })) {
    const hit = Object.entries(tokens).find(([, t]) => t.address && t.address.toLowerCase() === String(log.address).toLowerCase());
    if (!hit || String(log.args.from).toLowerCase() !== owner.toLowerCase() || String(log.args.to).toLowerCase() !== safe.toLowerCase()) continue;
    if (log.args.value > 0n) deposits.push({ token: hit[0], amount: fromRaw(log.args.value, hit[1].decimals) });
  }
  if (!deposits.length) return { state: 'bad', reason: `the transaction moves no token from ${owner} to the vault ${safe}` };
  return { state: 'ok', deposits };
}

// Whether an owner's transaction is mined: {state: 'confirmed'} | {state: 'failed' | 'pending', reason}.
export async function confirm(keys, hash) {
  if (!/^0x[0-9a-fA-F]{64}$/.test(String(hash))) return { state: 'failed', reason: 'not a transaction hash' };
  const receipt = await keys.pub.getTransactionReceipt({ hash }).catch(e => {
    if (/not (be )?found|could not be found/i.test(String(e?.message))) return null;
    throw e;
  });
  if (!receipt) return { state: 'pending', reason: 'the transaction is not on chain (yet)' };
  if (receipt.status !== 'success') return { state: 'failed', reason: 'the transaction failed on chain' };
  return { state: 'confirmed' };
}

export async function checkDeposit(keys, c, hash, { owner, safe }) {
  if (!/^0x[0-9a-fA-F]{64}$/.test(String(hash))) return { state: 'bad', reason: 'not a transaction hash' };
  const receipt = await keys.pub.getTransactionReceipt({ hash }).catch(e => {
    if (/not (be )?found|could not be found/i.test(String(e?.message))) return null;
    throw e;
  });
  const head = await keys.pub.getBlockNumber({ cacheTime: 0 });
  return depositVerdict({ receipt, head }, { owner, safe, tokens: c.tokens, confirmations: c.gas.confirmations });
}

export async function status(keys, c, row) {
  const { safe, roles } = vaultOf(row);
  const balances = {};
  for (const [sym, t] of Object.entries(c.tokens)) {
    balances[sym] = fromRaw(t.native ? await keys.pub.getBalance({ address: safe }) : await balanceOf(keys.pub, t.address, safe), t.decimals);
  }
  const st = await readState(keys.pub, safe, roles);
  return { vault: safe, balances, botActive: st.modules.some(m => m.toLowerCase() === roles.toLowerCase()),
    positions: (await positions(keys, c, row)).length };
}

export const hasDeposit = (st) => Object.values(st.balances).some(v => Number(v) > 0);
