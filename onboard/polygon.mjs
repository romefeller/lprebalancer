// Polygon for the onboarding server: the bot's key and gas, the connect (a
// Safe and its Roles module, chains/evm/vault/), and the owner's transactions
// (deposit, withdraw, revoke) as {to, data, value} for the owner's wallet to
// send. A Safe with one owner at threshold 1 runs a transaction its owner
// sends with a pre-validated signature: no Safe app needed.
import { encodeFunctionData, getAddress, toHex } from 'viem';
import { privateKeyToAccount } from 'viem/accounts';
import { readKey } from '../chains/evm/keyfile.mjs';
import { clients, connect, poolOf } from '../chains/evm/vault/cli.mjs';
import { SAFE_ABI, SENTINEL, preValidated, readState } from '../chains/evm/vault/safe.mjs';
import * as POLYGON from '../chains/evm/polygon.mjs';
import { Invalid, fromRaw, own, toRaw } from './intent.mjs';

const ZERO = '0x0000000000000000000000000000000000000000';

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
const execAsSoleOwner = (safe, owner, to, data, value = 0n) => call(safe, encodeFunctionData({ abi: SAFE_ABI, functionName: 'execTransaction',
  args: [getAddress(to), value, data, 0, 0n, 0n, 0n, ZERO, ZERO, preValidated(owner)] }));

export async function ownerTxs(keys, c, row, { action, owner, token, amount }) {
  const safe = getAddress(row.settings), roles = getAddress(Object.values(row.venues)[0].policies[0]);
  const st = await readState(keys.pub, safe, roles);
  if (!st.owners.some(o => o.toLowerCase() === owner.toLowerCase())) throw new Invalid(`${owner} is not an owner of this vault`);
  if (st.threshold !== 1) throw new Invalid(`the vault needs ${st.threshold} owner signatures; use app.safe.global`);
  if (action === 'withdraw') {
    const t = own(c.tokens, token);
    if (!t) throw new Invalid(`token ${token}`);
    const have = t.native ? await keys.pub.getBalance({ address: safe }) : await balanceOf(keys.pub, t.address, safe);
    const raw = amount === 'all' ? have : toRaw(amount, t.decimals);
    if (!(raw > 0n && raw <= have)) throw new Invalid(`the vault holds ${fromRaw(have, t.decimals)} ${token}`);
    const tx = t.native ? execAsSoleOwner(safe, owner, owner, '0x', raw) : execAsSoleOwner(safe, owner, t.address, erc20('transfer', [getAddress(owner), raw]));
    return { summary: `withdraw ${fromRaw(raw, t.decimals)} ${token} from the vault ${safe} to ${owner}`, chainId: c.U.CHAIN_ID, transactions: [tx] };
  }
  if (action === 'revoke') {
    const i = st.modules.findIndex(m => m.toLowerCase() === roles.toLowerCase());
    if (i < 0) throw new Invalid('the bot is already revoked: the Roles module is not enabled');
    const prev = i === 0 ? SENTINEL : st.modules[i - 1];
    const data = encodeFunctionData({ abi: SAFE_ABI, functionName: 'disableModule', args: [prev, roles] });
    return { summary: `revoke the bot: disable the Roles module ${roles} of the vault ${safe}`, chainId: c.U.CHAIN_ID,
      transactions: [execAsSoleOwner(safe, owner, safe, data)] };
  }
  throw new Invalid(`action ${action}`);
}

export async function status(keys, c, row) {
  const safe = getAddress(row.settings), roles = getAddress(Object.values(row.venues)[0].policies[0]);
  const balances = {};
  for (const [sym, t] of Object.entries(c.tokens)) {
    balances[sym] = fromRaw(t.native ? await keys.pub.getBalance({ address: safe }) : await balanceOf(keys.pub, t.address, safe), t.decimals);
  }
  const st = await readState(keys.pub, safe, roles);
  return { vault: safe, balances, botActive: st.modules.some(m => m.toLowerCase() === roles.toLowerCase()) };
}

export const hasDeposit = (st) => Object.values(st.balances).some(v => Number(v) > 0);
