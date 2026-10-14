// Solana for the onboarding server: which venue a new vault gets, the bot's
// keys and gas, the owner's own set-up of its vault (creation, the bot's
// policies, the deposit and the bot's gas, signed by the owner alone) and
// the bot's finish of it (its lookup table), and the owner's transactions
// (deposit, withdraw, close, revoke, approve) built unsigned for the owner's
// wallet to sign and send; deposits read back from the chain; signed
// transactions sent.
import { createRequire } from 'module';
import * as sq from '../chains/solana/vault/squads.mjs';
import { RULES, compile } from '../chains/solana/vault/policy.mjs';
import { VENUES } from '../chains/solana/vault/venues.mjs';
import { readKeypair } from '../chains/solana/vault/account.mjs';
import { finishSetup, request as buildRequest, rulesRequest as buildRules, setupRequest } from '../chains/solana/vault/cli.mjs';
import { packSteps } from '../chains/solana/vault/admin.mjs';
import { vaultPositions } from '../chains/solana/vault/exit.mjs';
import { signerError } from '../shared/signer_errors.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };
import { Invalid, fromRaw, nativeOf, own, toRaw } from './intent.mjs';
// the longest message the owner's wallet signs in a set-up (a hardware wallet's limit)
import { OWNER_MESSAGE_BYTES } from './decode_sol.mjs';

const bs58 = createRequire(import.meta.url)('bs58').default;

const require = createRequire(import.meta.url);
const { Message, PublicKey, SystemProgram, Transaction, VersionedTransaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

// The chain's config with every solana.json key replaced by its address.
export function resolve(c) {
  const pools = SOLANA[c.poolsKey] ?? {};
  const tokens = Object.fromEntries(Object.entries(c.tokens).map(([sym, t]) => {
    if (!SOLANA[t.mintKey]) throw new Error(`token ${sym}: ${t.mintKey} is not in solana.json`);
    return [sym, { ...t, mint: SOLANA[t.mintKey] }];
  }));
  if (!(Number.isInteger(c.approveVenues) && c.approveVenues >= 1)) throw new Error(`approveVenues ${c.approveVenues}: how many venues a new vault approves, 1 or more`);
  const payout = tokens[c.payoutToken];
  if (!payout) throw new Error(`payoutToken ${c.payoutToken} is not one of the tokens`);
  if (!SOLANA[c.operatorWallet]) throw new Error(`operatorWallet: ${c.operatorWallet} is not in solana.json`);
  return { ...c, pools, tokens, payoutMint: payout.mint, operatorWallet: SOLANA[c.operatorWallet] };
}

// The venues a new vault approves: in the config's order, those with vault
// rules, vault support and a configured pool, the first c.approveVenues of
// them (each adds policies to the setup gas). The first is the profile's. Pure.
export function vaultVenues(c, rules = RULES, venues = VENUES) {
  return c.venues.filter(v => rules.venues?.[v] && venues[v] && c.pools[v]).slice(0, c.approveVenues);
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

// The request's vault set-up as cli.mjs setupRequest reads it: the venues
// it approves (vaultVenues), their pools, the payouts (the profit wallet's
// and, for an instance that pays a fee share, the operator's), and the
// page's set-up keys (`known`, every one this request made; `setupKey`, the
// one that does the work now).
function setupFlags(c, { owner, profitWallet, feeWallet }, seed, { known = [], setupKey = null } = {}) {
  const venues = vaultVenues(c);
  if (!venues.length) throw new Error(`no venue of ${c.venues.join(', ')} has vault support yet`);
  return { owner, venue: venues.join(','), pools: venues.map(v => c.pools[v]).join(','),
    payout: [profitWallet, ...(feeWallet ? [feeWallet] : [])].join(','), 'payout-mint': c.payoutMint, ...(seed != null ? { seed: String(seed) } : {}),
    ...(known.length ? { 'setup-keys': known.join(',') } : {}), ...(setupKey ? { 'setup-key': setupKey } : {}) };
}

// Bounds of what the owner's set-up creates, over the program's allocations
// (measured on the fork 2026-10-13: a settings account of one signer is 168
// bytes; policies, POLICY_ACCOUNT_BYTES below).
export const SETTINGS_ACCOUNT_BYTES = 256;
export const TOKEN_ACCOUNT_BYTES = 165;
// The fee of a transaction with one signature and no priority fee.
export const FEE_LAMPORTS = 5_000n;
// Set-up keys a request may make (a page reloaded, or the verifier, makes a new one).
export const SETUP_KEYS_MAX = 16;

// The owner's set-up, unsigned, as far as the chain lacks it: what the
// owner's wallet signs (`owner`: the smart account's creation with the
// page's one-time set-up key as its second signer, and the SOL the key
// spends; or, for a vault that exists, the owner's change that puts this key
// on it and takes an older one off), what the set-up key signs (`key`: each
// policy for the bot's key, paid by the key; once they all exist, the key
// takes itself off and sends what it holds back to the owner), and the
// funding (`fund`: the deposit into the vault and the bot's gas, sent last,
// when the vault has every policy and the owner alone signs it). Every
// message the owner signs is at most OWNER_MESSAGE_BYTES: a policy is too
// long for a hardware wallet (admin.mjs ownerSetupPlan). The bot signs and
// pays nothing. `a.setupKey`: the page's key; `a.planned` ({seed,
// setupKeys}) resumes; a seed another account took is planned again.
// `a.notBefore` (seconds): a funding older than this is not this request's.
// Refuses a wallet that cannot pay it all. {planned: {seed, settings, vault,
// setupKeys}, done, owner, key, fund, report, cost}; done: nothing to sign.
// The last argument (tests): the set-up reader, the funding search.
export async function setupTxs(connection, keys, c, a, { request = setupRequest, find = findFunding } = {}) {
  const owner = new PublicKey(a.owner);
  let helper;
  try { helper = new PublicKey(a.setupKey); } catch { throw new Invalid('the page sent no set-up key: reload the page'); }
  if ([owner, keys.payer.publicKey, keys.policyKey.publicKey].some(k => k.equals(helper))) throw new Invalid('the set-up key is not the page\'s own');
  const known = [...new Set([...(a.planned?.setupKeys ?? []), helper.toBase58()])];
  if (known.length > SETUP_KEYS_MAX) throw new Invalid('this request made too many set-up keys; start a new one');
  const setup = await request(setupFlags(c, a, a.planned?.seed, { known, setupKey: helper.toBase58() }),
    { connection, payer: keys.payer.publicKey, policyKey: keys.policyKey.publicKey });
  const planned = { seed: setup.seed, settings: setup.settings, vault: setup.vault, setupKeys: known };
  const settings = new PublicKey(setup.settings), vault = new PublicKey(setup.vault);
  const created = setup.done.includes(0);
  const policies = created ? setup.steps : setup.steps.slice(1);
  const others = setup.others ?? [];
  const keyOn = others.includes(helper.toBase58());
  const stale = others.filter(k => k !== helper.toBase58()).map(k => new PublicKey(k));
  const rentOf = async (n) => BigInt(await connection.getMinimumBalanceForRentExemption(n));
  const held = BigInt(await connection.getBalance(helper, 'confirmed'));
  const ownerIxs = [], keyGroups = [];
  let keyFund = 0n;
  if (policies.length) {
    // the key pays each policy's rent and its fees, and keeps its account's floor until it sends it all back
    const need = BigInt(policies.length) * await rentOf(POLICY_ACCOUNT_BYTES) + BigInt(policies.length + 1) * FEE_LAMPORTS + await rentOf(0);
    keyFund = need > held ? need - held : 0n;
    if (!created) ownerIxs.push(setup.steps[0]);
    const actions = [...stale.map(k => ({ kind: 'removeSigner', key: k })), ...(created && !keyOn ? [{ kind: 'addSigner', signer: { key: helper } }] : [])];
    if (actions.length) ownerIxs.push(sq.settingsSyncIx({ settings, signers: [owner], rentPayer: owner, actions }));
    if (keyFund > 0n) ownerIxs.push(SystemProgram.transfer({ fromPubkey: owner, toPubkey: helper, lamports: keyFund }));
    keyGroups.push(...packSteps(policies, helper));
  } else {
    // every policy exists: the key takes itself off and sends its SOL back; an older key, or one that cannot pay its fee, the owner takes off
    const release = keyOn && held >= FEE_LAMPORTS;
    const off = [...stale, ...(keyOn && !release ? [helper] : [])];
    if (off.length) ownerIxs.push(sq.settingsSyncIx({ settings, signers: [owner], rentPayer: owner, actions: off.map(k => ({ kind: 'removeSigner', key: k })) }));
    if (release) {
      const back = held - FEE_LAMPORTS;
      keyGroups.push([setup.release, ...(back > 0n ? [SystemProgram.transfer({ fromPubkey: helper, toPubkey: owner, lamports: back })] : [])]);
    }
  }
  // the funding, unless this request's is on chain already (a vault whole and funded: done)
  const whole = !policies.length && !others.length;
  const funded = whole ? await find(connection, setup.vault, { owner: a.owner, payer: keys.payer.publicKey.toBase58(), amount: a.gas,
    notBefore: a.notBefore ?? 0 }) : null;
  const gas = toRaw(a.gas, 9);
  let fundGroups = [];
  if (!funded) {
    const dep = await depositIxs(connection, c, { owner, vault, token: a.token, amount: a.deposit });
    fundGroups = packSteps([...dep.slice(0, -1), [dep.at(-1), SystemProgram.transfer({ fromPubkey: owner, toPubkey: keys.payer.publicKey, lamports: gas })]],
      owner, 1 + 64 + OWNER_MESSAGE_BYTES);
  }
  const ownerGroups = ownerIxs.length ? packSteps(ownerIxs, owner, 1 + 64 + OWNER_MESSAGE_BYTES) : [];
  if (!ownerGroups.length && !keyGroups.length && !fundGroups.length) {
    return { planned, done: true, owner: [], key: [], fund: [], report: setup.report, funded };
  }
  const cost = await setupCost(connection, c, { owner, created: created ? 0 : 1, keyFund, txs: ownerGroups.length + fundGroups.length,
    gas: funded ? 0n : gas, token: a.token, deposit: funded ? null : a.deposit });
  const { blockhash } = await connection.getLatestBlockhash('confirmed');
  const wire = (payer) => (ixs) => {
    const tx = new Transaction().add(...ixs);
    tx.feePayer = payer;
    tx.recentBlockhash = blockhash;
    return asWire(tx);
  };
  return { planned, done: false, report: setup.report, cost: fromRaw(cost, 9), keyFund: fromRaw(keyFund, 9),
    owner: ownerGroups.map(wire(owner)), key: keyGroups.map(wire(helper)), fund: fundGroups.map(wire(owner)) };
}

// The SOL the owner's set-up takes from its wallet, or Invalid when the wallet
// holds less: the account's creation fee (the program's config) and the
// settings' rent, what the set-up key spends on the policies' rent and its
// fees (`keyFund`, lamports; what is left comes back), a fee per owner's
// transaction, the bot's gas, a SOL deposit (a token: its amount in the
// wallet, and the vault's token account's rent; none: `deposit` null), and
// the wallet's own rent-exempt floor. Raw lamports.
export async function setupCost(connection, c, { owner, created, keyFund = 0n, txs, gas, token, deposit }) {
  const t = own(c.tokens, token);
  const rentOf = async (n) => BigInt(await connection.getMinimumBalanceForRentExemption(n));
  const fee = created ? sq.decodeProgramConfig((await connection.getAccountInfo(sq.programConfigPda(), 'confirmed')).data).creationFee : 0n;
  const money = deposit == null ? 0n : t.native ? toRaw(deposit, 9) : await rentOf(TOKEN_ACCOUNT_BYTES);
  const need = BigInt(fee) + (created ? await rentOf(SETTINGS_ACCOUNT_BYTES) : 0n) + keyFund
    + BigInt(txs) * 10_000n + gas + money + await rentOf(0);
  const have = BigInt(await connection.getBalance(new PublicKey(owner), 'confirmed'));
  if (have < need) {
    throw new Invalid(`your wallet holds ${fromRaw(have, 9)} SOL; the set-up needs ${fromRaw(need, 9)} `
      + `(the vault's and its rules' rent, the bot's gas ${fromRaw(gas, 9)}${t.native && deposit != null ? `, the deposit ${deposit}` : ''}, fees, and the wallet's own minimum)`);
  }
  return need;
}

// The bot's side once the owner's set-up is on chain (cli.mjs finishSetup):
// the vault checked, then its lookup table. `r` is the request row; its
// planned seed is the vault. `onLookup` saves the table before it is sent.
export async function finishVault(connection, keys, c, r, { onLookup, log } = {}) {
  const planned = r.connected?.planned;
  if (!planned?.seed) throw new Error('the request has no planned vault');
  const setup = await setupRequest(setupFlags(c, { owner: r.owner, profitWallet: r.profit_wallet, feeWallet: r.fee_wallet }, planned.seed,
    { known: planned.setupKeys ?? [] }), { connection, payer: keys.payer.publicKey, policyKey: keys.policyKey.publicKey });
  if (setup.settings !== planned.settings) throw new Error(`the planned vault ${planned.vault} is not this owner's`);
  return finishSetup(setup, { connection, payer: keys.payer, policyKey: keys.policyKey.publicKey, lookup: r.connected?.lookup ?? null,
    onLookup, log });
}

// Where the owner's set-up stands on chain: {state: 'done'} (every step
// landed, the owner the vault's only signer), {state: 'pending', missing}
// (steps to come, or a set-up key still on it), or {state: 'replan'}
// (another account took the planned seed: the owner signs a new set-up).
export async function setupState(connection, keys, c, r, { request = setupRequest } = {}) {
  const planned = r.connected?.planned;
  if (!planned?.seed) return { state: 'replan' };
  const setup = await request(setupFlags(c, { owner: r.owner, profitWallet: r.profit_wallet, feeWallet: r.fee_wallet }, planned.seed,
    { known: planned.setupKeys ?? [] }), { connection, payer: keys.payer.publicKey, policyKey: keys.policyKey.publicKey });
  if (setup.settings !== planned.settings) return { state: 'replan' };
  const missing = setup.steps.length + (setup.others ?? []).length;
  return missing ? { state: 'pending', missing } : { state: 'done' };
}

// The transaction of a set-up that paid the bot's gas, read from the chain:
// `payment`, the page's word for it, or else the vault's recent
// transactions searched (findFunding); and the set-up's deposit (in the same
// transaction, setupTxs; else the vault's newest deposit from the owner, as
// when the owner paid the gas apart). {state: 'ok', payment, deposit (null:
// none yet)} | {state: 'pending' | 'bad', reason}.
export async function funding(connection, c, r, { payment, notBefore }, { find = findFunding, deposited = findDeposit } = {}) {
  const expect = { owner: r.owner, payer: r.payer, amount: r.gas, notBefore };
  const vault = r.connected?.planned?.vault ?? null;
  const told = payment ? await checkPayment(connection, payment, expect) : null;
  const paid = told?.state === 'ok' ? payment : vault ? await find(connection, vault, expect) : null;
  if (!paid) {
    if (told?.state === 'bad') return told;
    return { state: 'pending', reason: told?.reason ?? 'the set-up\'s gas payment is not readable yet' };
  }
  return { state: 'ok', payment: paid, deposit: vault ? await deposited(connection, c, vault, r.owner) : null };
}

// The newest of the vault's recent transactions that moves the owner's money
// into it (checkDeposit), or null.
export async function findDeposit(connection, c, vault, owner, limit = 20) {
  const sigs = await connection.getSignaturesForAddress(new PublicKey(vault), { limit }, 'confirmed');
  for (const s of sigs) {
    if (s.err) continue;
    if ((await checkDeposit(connection, c, s.signature, { owner, vault })).state === 'ok') return s.signature;
  }
  return null;
}

// The transaction that funded a set-up, when the page lost it: the newest
// of the vault's recent transactions that pays the bot's key the gas from the
// owner (paymentVerdict). Null when there is none.
export async function findFunding(connection, vault, expect, limit = 20) {
  const sigs = await connection.getSignaturesForAddress(new PublicKey(vault), { limit }, 'confirmed');
  for (const s of sigs) {
    if (s.err) continue;
    if ((await checkPayment(connection, s.signature, expect)).state === 'ok') return s.signature;
  }
  return null;
}

const asWire = (tx) => tx.serialize({ requireAllSignatures: false, verifySignatures: false }).toString('base64');

// The deposit's instructions: SOL straight to the vault, or a token to the
// vault's token account (created if missing, paid by the owner). Refuses a
// wallet that holds less.
async function depositIxs(connection, c, { owner, vault, token, amount }) {
  const t = own(c.tokens, token);
  if (!t) throw new Invalid(`token ${token}`);
  const from = new PublicKey(owner), to = new PublicKey(vault), raw = toRaw(amount, t.decimals);
  if (t.native) {
    const have = BigInt(await connection.getBalance(from, 'confirmed'));
    if (have < raw + 10_000n) throw new Invalid(`your wallet holds ${fromRaw(have, 9)} SOL; the deposit needs ${amount} and a fee`);
    return [SystemProgram.transfer({ fromPubkey: from, toPubkey: to, lamports: raw })];
  }
  const mint = new PublicKey(t.mint);
  const program = (await connection.getAccountInfo(mint)).owner;
  const src = spl.getAssociatedTokenAddressSync(mint, from, false, program);
  const dst = spl.getAssociatedTokenAddressSync(mint, to, true, program);
  const acct = await connection.getAccountInfo(src);
  const have = acct ? spl.unpackAccount(src, acct, program).amount : 0n;
  if (have < raw) throw new Invalid(`your wallet holds ${fromRaw(have, t.decimals)} ${token}; the deposit is ${amount}`);
  return [spl.createAssociatedTokenAccountIdempotentInstruction(from, dst, to, mint, program),
    spl.createTransferCheckedInstruction(src, mint, dst, from, raw, t.decimals, [], program)];
}

// The deposit the owner signs (depositIxs), one transaction.
export async function depositTxs(connection, c, { owner, vault, token, amount }) {
  const tx = new Transaction().add(...await depositIxs(connection, c, { owner, vault, token, amount }));
  const { blockhash } = await connection.getLatestBlockhash('confirmed');
  tx.feePayer = new PublicKey(owner);
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

// Withdraw (to the owner's own wallet; token ALL: everything, its accounts
// closed), close (every venue's positions, into the vault), revoke or
// approve (every venue's policies), as cli.mjs request builds them, each
// with a fresh blockhash. An approve with `token` and `amount` (a re-grant
// with more capital) carries the deposit too, after every policy, in as few
// transactions as fit: one wallet approval for both. A close of a vault
// without positions is no transaction. The last argument (tests): the
// request builders and the position reader.
export async function ownerTxs(connection, c, row, keys, { action, owner, token, amount }, { request = buildRequest, rulesRequest = buildRules, positions = vaultPositions } = {}) {
  const venues = Object.keys(row.venues);
  let parts;
  if (action === 'withdraw' && token === 'ALL') {
    const mints = Object.values(c.tokens).map(t => t.mint).join(',');
    parts = [await request('withdraw', { owner, to: owner, mint: 'ALL', mints }, { connection, env: requestEnv(row, venues[0], keys) })];
  } else if (action === 'withdraw') {
    const t = own(c.tokens, token);
    if (!t) throw new Invalid(`token ${token}`);
    if (amount !== 'all' && (amount.split('.')[1]?.length ?? 0) > t.decimals) throw new Invalid(`amount: a number or all, at most ${t.decimals} decimals`);
    if (amount !== 'all' && !(toRaw(amount, t.decimals) > 0n)) throw new Invalid('amount: a number or all');
    parts = [await request('withdraw', { owner, to: owner, mint: t.native ? 'SOL' : t.mint, amount },
      { connection, env: requestEnv(row, venues[0], keys) })];
  } else if (action === 'close') {
    parts = [];
    for (const v of venues) {
      const open = await positions(connection, { venue: v, pools: row.venues[v].pools, vault: sq.vaultPda(new PublicKey(row.settings)).toBase58() });
      if (open.length) parts.push(await request('close', { owner }, { connection, env: requestEnv(row, v, keys) }));
    }
    if (!parts.length) return { summary: 'nothing to close: the vault holds no position', transactions: [] };
  } else if (action === 'revoke' || action === 'approve') {
    // approve: the bot's policy key back on every policy, with the rules as the
    // bot now compiles them (after a revoke, or after rules.json grew), and the
    // policies of a venue staged on this vault created; every venue in one
    // request, new policies first (cli.mjs rulesRequest)
    const r = await rulesRequest(action, { connection, settings: row.settings, owner: new PublicKey(owner), payer: keys.payer.publicKey,
      policyKey: keys.policyKey.publicKey, venues: venues.map(venue => ({ venue, ...row.venues[venue] })) });
    const t = action === 'approve' && token ? own(c.tokens, token) : null;
    if (action === 'approve' && token && !t) throw new Invalid(`token ${token}`);
    const deposit = t ? await depositIxs(connection, c, { owner, vault: sq.vaultPda(new PublicKey(row.settings)), token, amount }) : [];
    const groups = packSteps([...r.groups, ...(deposit.length ? [deposit] : [])], new PublicKey(owner));
    if (action === 'approve') await affordsRules(connection, owner, r.steps, t?.native ? toRaw(amount, 9) : 0n);
    const { blockhash } = await connection.getLatestBlockhash('confirmed');
    const summary = r.summary + (t ? `; then deposit ${amount} ${token} into your vault` : '');
    return { summary, transactions: groups.map(ixs => {
      const tx = new Transaction().add(...ixs);
      tx.feePayer = new PublicKey(owner);
      tx.recentBlockhash = blockhash;
      return asWire(tx);
    }) };
  } else throw new Invalid(`action ${action}`);
  const { blockhash } = await connection.getLatestBlockhash('confirmed');
  const transactions = parts.flatMap(p => p.messages.map(b64 => {
    const m = Message.from(Buffer.from(b64, 'base64'));
    m.recentBlockhash = blockhash;
    return asWire(Transaction.populate(m));
  }));
  return { summary: parts.map(p => p.summary).join('; '), transactions };
}

// The SOL an approval takes from the owner's wallet, which pays it: a fee
// per transaction, the rent of each new policy and of what an update grows
// a policy by, and the wallet's own rent-exempt floor, which it may not drop
// below. Refuses before the wallet is asked to sign what would fail. Bounds
// over the program's allocations, measured on the fork 2026-10-12: 1324
// bytes the largest policy (test0's first Raydium one; every venue's for
// the largest vault: 353 to 1231), 183 bytes the most an update grew one (a
// DLMM policy of an older release). tests/test_vault_add_venue_fork.mjs
// checks both bounds and that the owner spends less than asked for.
export const POLICY_ACCOUNT_BYTES = 1536;
export const POLICY_GROWTH_BYTES = 256;
export async function affordsRules(connection, owner, steps, extra = 0n) {
  const created = steps.filter(s => s.kind === 'create').length;
  const rentOf = async (n) => BigInt(await connection.getMinimumBalanceForRentExemption(n));
  const floor = await rentOf(0);
  const grow = (await rentOf(POLICY_GROWTH_BYTES)) - floor;
  const need = BigInt(created) * await rentOf(POLICY_ACCOUNT_BYTES) + BigInt(steps.length - created) * grow
    + BigInt(steps.length) * 10_000n + floor + extra;
  const have = BigInt(await connection.getBalance(new PublicKey(owner), 'confirmed'));
  if (have < need) {
    throw new Invalid(`your wallet holds ${fromRaw(have, 9)} SOL; approving needs ${fromRaw(need, 9)} `
      + `(${created} new policies' rent, ${steps.length} transaction fees, and the wallet's own minimum)`);
  }
  return need;
}

// What the vault holds and whether the bot may still act (every policy
// lists the bot's policy key; a policy staged for a venue the owners have
// not approved yet does not exist and says nothing), and how many positions
// it holds.
export async function status(connection, c, row, keys, { positionsOf = vaultPositions } = {}) {
  const vault = sq.vaultPda(new PublicKey(row.settings));
  const balances = {};
  for (const [sym, t] of Object.entries(c.tokens)) {
    if (t.native) { balances[sym] = fromRaw(BigInt(await connection.getBalance(vault, 'confirmed')), 9); continue; }
    balances[sym] = fromRaw(await tokenBalance(connection, t, vault, true), t.decimals);
  }
  const policies = Object.values(row.venues).flatMap(v => v.policies).map(p => new PublicKey(p));
  const infos = await connection.getMultipleAccountsInfo(policies, 'confirmed');
  const present = infos.filter(Boolean);
  const botActive = present.length > 0 && present.every(i => sq.decodePolicyHead(i.data).signers.some(s => s.key.equals(keys.policyKey.publicKey)));
  let positions = 0;
  for (const [venue, v] of Object.entries(row.venues)) {
    positions += (await positionsOf(connection, { venue, pools: v.pools, vault: vault.toBase58() })).length;
  }
  return { vault: vault.toBase58(), balances, botActive, rulesCurrent: rulesCurrent(row, vault, keys, infos), positions };
}

// The accounts of a request's vault the page's decoder checks against its
// own pins: settings and vault, each venue's policies and pools, and the
// payout wallets the policies pay (the profit wallet, the fee wallet). Pure.
export function context(r) {
  const row = r.vault_row;
  return { settings: row.settings, vault: sq.vaultPda(new PublicKey(row.settings)).toBase58(),
    policies: Object.fromEntries(Object.entries(row.venues).map(([v, x]) => [v, x.policies])),
    pools: Object.fromEntries(Object.entries(row.venues).map(([v, x]) => [v, x.pools])),
    payoutWallets: [r.profit_wallet, ...(r.fee_wallet ? [r.fee_wallet] : [])] };
}

// What a deposit transaction moved from the owner into the vault, from its
// status and parsed transaction (jsonParsed): {state: 'ok', deposits:
// [{token, amount}]} | {state: 'pending' | 'bad', reason}. Only top-level
// System transfers from the owner to the vault, and token transfers the
// owner signed into the vault's account of a configured mint, count. Pure.
export function depositVerdict(status, parsed, { owner, vault, tokens }) {
  if (!status) return { state: 'pending', reason: 'the deposit is not on chain (yet)' };
  if (status.err || parsed?.meta?.err) return { state: 'bad', reason: 'the deposit failed on chain' };
  if (!['confirmed', 'finalized'].includes(status.confirmationStatus)) return { state: 'pending', reason: 'the deposit is not confirmed yet' };
  if (!parsed) return { state: 'pending', reason: 'the deposit is not readable yet' };
  const ixs = parsed.transaction?.message?.instructions;
  const deposits = [];
  for (const ix of Array.isArray(ixs) ? ixs : []) {
    const info = ix.parsed?.info, type = ix.parsed?.type;
    if (ix.program === 'system' && type === 'transfer' && info.source === owner && info.destination === vault) {
      const [sym, t] = Object.entries(tokens).find(([, x]) => x.native);
      deposits.push({ token: sym, raw: BigInt(info.lamports), decimals: t.decimals });
    }
    if (['spl-token', 'spl-token-2022'].includes(ix.program) && ['transfer', 'transferChecked'].includes(type) && info.authority === owner) {
      const hit = Object.entries(tokens).find(([, t]) => !t.native && t.ata === info.destination);
      if (!hit) continue;
      const raw = BigInt(info.tokenAmount?.amount ?? info.amount);
      deposits.push({ token: hit[0], raw, decimals: hit[1].decimals });
    }
  }
  const sum = new Map();
  for (const d of deposits) sum.set(d.token, { ...d, raw: (sum.get(d.token)?.raw ?? 0n) + d.raw });
  const out = [...sum.values()].filter(d => d.raw > 0n).map(d => ({ token: d.token, amount: fromRaw(d.raw, d.decimals) }));
  if (!out.length) return { state: 'bad', reason: `the transaction moves nothing from ${owner} into the vault ${vault}` };
  return { state: 'ok', deposits: out };
}

export async function checkDeposit(connection, c, signature, { owner, vault }) {
  const sig = String(signature);
  let bytes = null;
  try { bytes = /^[1-9A-HJ-NP-Za-km-z]{86,88}$/.test(sig) ? bs58.decode(sig) : null; } catch { bytes = null; }
  if (bytes?.length !== 64) return { state: 'bad', reason: 'not a transaction signature' };
  const [status] = (await connection.getSignatureStatuses([sig], { searchTransactionHistory: true })).value;
  const parsed = status ? await connection.getParsedTransaction(sig, { commitment: 'confirmed', maxSupportedTransactionVersion: 0 }) : null;
  const tokens = {};
  for (const [sym, t] of Object.entries(c.tokens)) {
    if (t.native) { tokens[sym] = t; continue; }
    const mint = new PublicKey(t.mint), program = (await connection.getAccountInfo(mint)).owner;
    tokens[sym] = { ...t, ata: spl.getAssociatedTokenAddressSync(mint, new PublicKey(vault), true, program).toBase58() };
  }
  return depositVerdict(status, parsed, { owner, vault, tokens });
}

// Send a transaction the owner's wallet signed (a WalletConnect wallet that
// signs only). The node checks it; the answer is its signature.
export async function broadcast(connection, base64) {
  const raw = Buffer.from(base64, 'base64');
  let tx;
  try { tx = VersionedTransaction.deserialize(raw); } catch { throw new Invalid('not a signed transaction'); }
  if (tx.signatures.some(s => s.every(b => b === 0))) throw new Invalid('the transaction is not signed');
  try {
    return await connection.sendRawTransaction(raw, { maxRetries: 5 });
  } catch (e) {
    // the program's own error, decoded, and its last log lines (a simulation refusal names nothing else)
    throw new Invalid(`the network refused the transaction: ${signerError(e).slice(0, 400)}`);
  }
}

// Whether a signature landed: confirmed, failed, or pending (not seen yet).
export async function confirm(connection, signature) {
  const sig = String(signature);
  if (!/^[1-9A-HJ-NP-Za-km-z]{86,88}$/.test(sig)) return { state: 'failed', reason: 'not a transaction signature' };
  const [st] = (await connection.getSignatureStatuses([sig], { searchTransactionHistory: true })).value;
  if (!st) return { state: 'pending', reason: 'not on chain yet' };
  if (st.err) return { state: 'failed', reason: `failed on chain: ${JSON.stringify(st.err).slice(0, 200)}` };
  if (!['confirmed', 'finalized'].includes(st.confirmationStatus)) return { state: 'pending', reason: 'not confirmed yet' };
  return { state: 'confirmed' };
}

// Whether every policy on chain holds the rules the bot now compiles for this
// vault. False after rules.json grew (2026-10-11: increase_position_length2):
// the bot then refuses to act until the owner approves the new rules.
export function rulesCurrent(row, vault, keys, infos) {
  let i = 0;
  for (const [venue, v] of Object.entries(row.venues)) {
    const compiled = compile(venue, { vault, payer: keys.payer.publicKey, pools: v.pools.map(k => new PublicKey(k)),
      payouts: v.payouts.map(k => new PublicKey(k)) });
    if (compiled.policies.length !== v.policies.length) return false;
    for (const p of compiled.policies) {
      const info = infos[i++];
      if (!info || !sq.policyStateIs(info.data, p.policy)) return false;
    }
  }
  return true;
}

export const hasDeposit = (st) => Object.values(st.balances).some(v => Number(v) > 0);
