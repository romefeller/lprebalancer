// Solana for the onboarding server: which venue a new vault gets, the bot's
// keys and gas, the connect, and the owner's transactions (deposit, withdraw,
// revoke) built unsigned for the owner's wallet to sign and send.
import { createRequire } from 'module';
import * as sq from '../chains/solana/vault/squads.mjs';
import { RULES } from '../chains/solana/vault/policy.mjs';
import { VENUES } from '../chains/solana/vault/venues.mjs';
import { readKeypair } from '../chains/solana/vault/account.mjs';
import { connect, request } from '../chains/solana/vault/cli.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };
import { Invalid, fromRaw, nativeOf, own, toRaw } from './intent.mjs';

const bs58 = createRequire(import.meta.url)('bs58').default;

const require = createRequire(import.meta.url);
const { Message, PublicKey, SystemProgram, Transaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

// The chain's config with every solana.json key replaced by its address.
export function resolve(c) {
  const pools = SOLANA[c.poolsKey] ?? {};
  const tokens = Object.fromEntries(Object.entries(c.tokens).map(([sym, t]) => {
    if (!SOLANA[t.mintKey]) throw new Error(`token ${sym}: ${t.mintKey} is not in solana.json`);
    return [sym, { ...t, mint: SOLANA[t.mintKey] }];
  }));
  const payout = tokens[c.payoutToken];
  if (!payout) throw new Error(`payoutToken ${c.payoutToken} is not one of the tokens`);
  if (!SOLANA[c.operatorWallet]) throw new Error(`operatorWallet: ${c.operatorWallet} is not in solana.json`);
  return { ...c, pools, tokens, payoutMint: payout.mint, operatorWallet: SOLANA[c.operatorWallet] };
}

// The venues a new vault can use, in the config's order: vault rules, vault
// support and a configured pool. The first is the new vault's. Pure.
export function vaultVenues(c, rules = RULES, venues = VENUES) {
  return c.venues.filter(v => rules.venues?.[v] && venues[v] && c.pools[v]);
}

export async function botKeys(c, env = process.env) {
  const payerPath = env[c.payerKeyEnv], policyPath = env[c.policyKeyEnv];
  if (!payerPath || !policyPath) throw new Error(`${c.payerKeyEnv} and ${c.policyKeyEnv} must name the bot's key files`);
  const payer = await readKeypair(payerPath), policyKey = await readKeypair(policyPath);
  return { payer, policyKey, policyPath, payee: payer.publicKey.toBase58(), public: [payer.publicKey.toBase58(), policyKey.publicKey.toBase58()],
    secretEnv: c.payerKeyEnv, policyKeyEnv: c.policyKeyEnv };
}

// The payer's SOL against one setup (measured cost and margin, config).
export async function gas(connection, keys, c) {
  const lamports = BigInt(await connection.getBalance(keys.payer.publicKey, 'confirmed'));
  const need = toRaw(c.gas.setup, 9) + toRaw(c.gas.margin, 9);
  return { ok: lamports >= need, have: fromRaw(lamports, 9), need: fromRaw(need, 9), symbol: 'SOL', key: keys.payer.publicKey.toBase58() };
}

// The owner's gas payment, unsigned: one SOL transfer to the bot's payer key.
// It refuses when the wallet cannot also cover the deposit that follows.
export async function paymentTxs(connection, c, { owner, payer, gas: amount, token, deposit }) {
  const from = new PublicKey(owner), lamports = toRaw(amount, 9), t = own(c.tokens, token);
  const sol = BigInt(await connection.getBalance(from, 'confirmed'));
  const needSol = lamports + (t.native ? toRaw(deposit, 9) : 0n) + 20_000n;          // and two fees
  if (sol < needSol) throw new Invalid(`your wallet holds ${fromRaw(sol, 9)} SOL; the gas and the deposit need ${fromRaw(needSol, 9)}`);
  if (!t.native) {
    const have = await tokenBalance(connection, t, from, false);
    if (have < toRaw(deposit, t.decimals)) throw new Invalid(`your wallet holds ${fromRaw(have, t.decimals)} ${token}; the deposit is ${deposit}`);
  }
  const tx = new Transaction().add(SystemProgram.transfer({ fromPubkey: from, toPubkey: new PublicKey(payer), lamports }));
  const { blockhash } = await connection.getLatestBlockhash('confirmed');
  tx.feePayer = from;
  tx.recentBlockhash = blockhash;
  return { summary: `pay the bot ${amount} SOL for the setup and the running gas, to ${payer}`, transactions: [asWire(tx)] };
}

async function tokenBalance(connection, t, owner, offCurve) {
  const mint = new PublicKey(t.mint);
  const program = (await connection.getAccountInfo(mint)).owner;
  const ata = spl.getAssociatedTokenAddressSync(mint, owner, offCurve, program);
  const a = await connection.getAccountInfo(ata);
  return a ? spl.unpackAccount(ata, a, program).amount : 0n;
}

// What the owner's wallet holds of the native coin and of `token`, in raw
// units by symbol (intent.mjs maxAmount). The token: its associated account,
// the one a deposit spends from.
export async function holdings(connection, c, owner, token) {
  const from = new PublicKey(owner), t = own(c.tokens, token);
  const held = { [nativeOf(c)[0]]: BigInt(await connection.getBalance(from, 'confirmed')) };
  if (!t.native) held[token] = await tokenBalance(connection, t, from, false);
  return held;
}

// What is wrong with a gas payment, from its status and its parsed
// transaction (jsonParsed): {state: 'ok'} | {state: 'pending', reason} |
// {state: 'bad', reason}. Only top-level System transfers from the owner (it
// signed them: a transfer needs its source's signature) to the payer count.
export function paymentVerdict(status, parsed, { owner, payer, lamports, notBefore }) {
  if (!status) return { state: 'pending', reason: 'the payment is not on chain (yet)' };
  if (status.err) return { state: 'bad', reason: 'the payment failed on chain' };
  if (!['confirmed', 'finalized'].includes(status.confirmationStatus)) return { state: 'pending', reason: 'the payment is not confirmed yet' };
  if (!parsed) return { state: 'pending', reason: 'the payment is not readable yet' };
  if (parsed.meta?.err) return { state: 'bad', reason: 'the payment failed on chain' };
  if (!(Number(parsed.blockTime) >= notBefore)) return { state: 'bad', reason: 'the payment is older than this request' };
  const ixs = parsed.transaction?.message?.instructions;
  const transfers = (Array.isArray(ixs) ? ixs : [])
    .filter(ix => ix.program === 'system' && ix.parsed?.type === 'transfer').map(ix => ix.parsed.info);
  const toPayer = transfers.filter(i => i.destination === payer), fromOwner = transfers.filter(i => i.source === owner);
  if (!fromOwner.length) return { state: 'bad', reason: `the payment is not from the owner ${owner}` };
  const paid = toPayer.filter(i => i.source === owner).reduce((a, i) => a + BigInt(i.lamports), 0n);
  if (paid === 0n) return { state: 'bad', reason: `the payment does not go to the bot's key ${payer}` };
  if (paid < lamports) return { state: 'bad', reason: `the payment is ${fromRaw(paid, 9)} SOL, less than ${fromRaw(lamports, 9)}` };
  return { state: 'ok' };
}

export async function checkPayment(connection, signature, expect) {
  const sig = String(signature);
  let bytes = null;
  try { bytes = /^[1-9A-HJ-NP-Za-km-z]{86,88}$/.test(sig) ? bs58.decode(sig) : null; } catch { bytes = null; }
  if (bytes?.length !== 64) return { state: 'bad', reason: 'not a transaction signature' };
  const [status] = (await connection.getSignatureStatuses([sig], { searchTransactionHistory: true })).value;
  const parsed = status ? await connection.getParsedTransaction(sig, { commitment: 'confirmed', maxSupportedTransactionVersion: 0 }) : null;
  return paymentVerdict(status, parsed, { ...expect, lamports: toRaw(expect.amount, 9) });
}

// The pool's tokens in pool order, as add_profile's record: each mint must
// be one of the configured tokens (the pair the template trades).
export async function poolRecord(connection, venue, pool, c) {
  const { mints } = await VENUES[venue].poolAccounts(connection, pool);
  const bySym = Object.entries(c.tokens).map(([sym, t]) => [t.mint, sym]);
  const side = (m) => {
    const hit = bySym.find(([mint]) => mint === m.toBase58());
    if (!hit) throw new Error(`pool ${pool} holds ${m.toBase58()}, not one of ${c.pair}`);
    return { address: hit[0], symbol: hit[1] };
  };
  return { token_a: side(mints[0]), token_b: side(mints[1]) };
}

// Connect: the bot creates the vault (owner = the signer, threshold 1) with
// the venue's policies; the payouts approved are the profit wallet's and,
// for an instance that pays a fee share, the operator's.
export async function connectVault(connection, keys, c, f, log) {
  const venue = vaultVenues(c)[0];
  if (!venue) throw new Error(`no venue of ${c.venues.join(', ')} has vault support yet`);
  const wallets = [f.profitWallet, ...(f.feeWallet ? [f.feeWallet] : [])];
  const out = await connect({ owners: f.owner, venue, pools: c.pools[venue], payout: wallets.join(','),
    'payout-mint': c.payoutMint, execute: true, seed: f.planned?.seed },
  { connection, payer: keys.payer, policyKey: keys.policyKey, log, onPlan: f.onPlan });
  return out;
}

const asWire = (tx) => tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');

// The deposit the owner signs: SOL straight to the vault, or a token to the
// vault's token account (created if missing, paid by the owner).
export async function depositTxs(connection, c, { owner, vault, token, amount }) {
  const t = own(c.tokens, token);
  if (!t) throw new Invalid(`token ${token}`);
  const from = new PublicKey(owner), to = new PublicKey(vault), raw = toRaw(amount, t.decimals);
  const tx = new Transaction();
  if (t.native) {
    const have = BigInt(await connection.getBalance(from, 'confirmed'));
    if (have < raw + 10_000n) throw new Invalid(`your wallet holds ${fromRaw(have, 9)} SOL; the deposit needs ${amount} and a fee`);
    tx.add(SystemProgram.transfer({ fromPubkey: from, toPubkey: to, lamports: raw }));
  } else {
    const mint = new PublicKey(t.mint);
    const program = (await connection.getAccountInfo(mint)).owner;
    const src = spl.getAssociatedTokenAddressSync(mint, from, false, program);
    const dst = spl.getAssociatedTokenAddressSync(mint, to, true, program);
    const acct = await connection.getAccountInfo(src);
    const have = acct ? spl.unpackAccount(src, acct, program).amount : 0n;
    if (have < raw) throw new Invalid(`your wallet holds ${fromRaw(have, t.decimals)} ${token}; the deposit is ${amount}`);
    tx.add(spl.createAssociatedTokenAccountIdempotentInstruction(from, dst, to, mint, program),
      spl.createTransferCheckedInstruction(src, mint, dst, from, raw, t.decimals, [], program));
  }
  const { blockhash } = await connection.getLatestBlockhash('confirmed');
  tx.feePayer = from;
  tx.recentBlockhash = blockhash;
  return { summary: `deposit ${amount} ${token} into your vault ${vault}`, transactions: [asWire(tx)] };
}

// What cli.mjs request reads for one venue of a vault row (store.py's vault_row).
export function requestEnv(row, venue, keys) {
  const v = row.venues[venue];
  return { LPBOT_VAULT_SETTINGS: row.settings, LPBOT_VAULT_POLICIES: v.policies.join(','), LPBOT_VAULT_POOLS: v.pools.join(','),
    LPBOT_VAULT_PAYOUTS: v.payouts.join(','), LPBOT_VAULT_LOOKUP: row.lookup ?? '', LPBOT_VAULT_VENUE: venue,
    LPBOT_VAULT_POLICY_KEY: keys.policyPath };
}

// Withdraw (to the owner's own wallet) or revoke (every venue's policies),
// as cli.mjs request builds them, each with a fresh blockhash.
export async function ownerTxs(connection, c, row, keys, { action, owner, token, amount }) {
  const venues = Object.keys(row.venues);
  let parts;
  if (action === 'withdraw') {
    const t = own(c.tokens, token);
    if (!t) throw new Invalid(`token ${token}`);
    if (amount !== 'all' && !(toRaw(amount, t.decimals) > 0n)) throw new Invalid('amount: a number or all');
    parts = [await request('withdraw', { owner, to: owner, mint: t.native ? 'SOL' : t.mint, amount },
      { connection, env: requestEnv(row, venues[0], keys) })];
  } else if (action === 'revoke') {
    parts = [];
    for (const v of venues) {
      parts.push(await request('revoke', { owner, payer: keys.payer.publicKey.toBase58() }, { connection, env: requestEnv(row, v, keys) }));
    }
  } else throw new Invalid(`action ${action}`);
  const { blockhash } = await connection.getLatestBlockhash('confirmed');
  const transactions = parts.flatMap(p => p.messages.map(b64 => {
    const m = Message.from(Buffer.from(b64, 'base64'));
    m.recentBlockhash = blockhash;
    return asWire(Transaction.populate(m));
  }));
  return { summary: parts.map(p => p.summary).join('; '), transactions };
}

// What the vault holds and whether the bot may still act (every policy
// lists the bot's policy key).
export async function status(connection, c, row, keys) {
  const vault = sq.vaultPda(new PublicKey(row.settings));
  const balances = {};
  for (const [sym, t] of Object.entries(c.tokens)) {
    if (t.native) { balances[sym] = fromRaw(BigInt(await connection.getBalance(vault, 'confirmed')), 9); continue; }
    balances[sym] = fromRaw(await tokenBalance(connection, t, vault, true), t.decimals);
  }
  const policies = Object.values(row.venues).flatMap(v => v.policies).map(p => new PublicKey(p));
  const infos = await connection.getMultipleAccountsInfo(policies, 'confirmed');
  const botActive = infos.length > 0 && infos.every(i => i && sq.decodePolicyHead(i.data).signers.some(s => s.key.equals(keys.policyKey.publicKey)));
  return { vault: vault.toBase58(), balances, botActive };
}

export const hasDeposit = (st) => Object.values(st.balances).some(v => Number(v) > 0);
