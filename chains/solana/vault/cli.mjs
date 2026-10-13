// The operator's tool for vaults: create the bot's keys, build the owner's
// own set-up of a vault, finish it on the bot's side, read a vault, and build
// the transactions only owners can sign.
//
//   node chains/solana/vault/cli.mjs keys <dir>
//       two new keys (0600): payer.json pays fees and rent, policy.json signs
//       the policies. Prints both public keys; the owner sends gas to the payer.
//   node chains/solana/vault/cli.mjs setup --owner <k> --venue <venue>[,<venue>] --pools <pool>[,<pool>]
//       [--payout <wallet>[,<wallet>]] [--payout-mint <mint>] [--seed <n>]
//       the owner's transactions that create its vault (the owner the only
//       signer, threshold 1) and each venue's policies for the bot, as one JSON
//       line: the owner signs and sends them, in order. The bot signs nothing.
//       Each pool belongs to the venue whose program owns it.
//   node chains/solana/vault/cli.mjs finish --owner <k> --venue .. --pools .. [--payout ..] --seed <n>
//       once the owner's set-up landed: checks the vault and every policy, makes
//       the vault's lookup table with the bot's key, prints the vault's config
//       (ops/add_vault.py stores it).
//   node chains/solana/vault/cli.mjs accounts --wallets <w>[,<w>] [--mints <m>[,<m>]]
//       the payout token accounts of these wallets (default mint: USDC), as JSON
//   node chains/solana/vault/cli.mjs status
//       the vault in LPBOT_VAULT_*: owners, threshold, whether the policies on
//       chain are the configured ones, balances.
//   node chains/solana/vault/cli.mjs request <withdraw|close|revoke|approve> --owner <k> [...]
//       an owner's transactions for owner/index.html, as one JSON line. withdraw
//       --to <wallet> --mint <mint|SOL> --amount <n|all> (an exact decimal), or
//       --mint ALL [--mints <m>[,<m>]] (each token account of these mints,
//       default SOL and USDC, emptied and closed, then all the SOL); close
//       (every position of the vault on LPBOT_VAULT_VENUE's pools, into the
//       vault, without the bot: vault/exit.mjs); revoke; approve (the policies
//       for the venue, pools and payouts now configured; a staged policy is
//       created).
//   node chains/solana/vault/cli.mjs add-venue --venue <venue> --pools <pool>[,<pool>] [--execute]
//       stage one more venue on the vault in LPBOT_VAULT_*: its policies'
//       addresses (the vault's next seeds), and with --execute its accounts
//       in the vault's lookup table. ops/add_vault.py venue stores the row;
//       the owners then approve (request approve, Approve bot rules).
//
// Keys: WALLET_SECRET_PATH (payer) and LPBOT_VAULT_POLICY_KEY (policy), as the
// signers read them. The owners' keys are only ever public keys here.
import { signerError } from '../../../shared/signer_errors.mjs';
import { createRequire } from 'module';
import fs from 'node:fs';
import path from 'node:path';
import * as sq from './squads.mjs';
import { compile } from './policy.mjs';
import { approvePlan, connectProblems, lookupPlan, ownerSetupPlan, packSteps, setupDone, stagedPolicies, staticAccounts } from './admin.mjs';
import { readKeypair, vaultConfig, verifyPolicy } from './account.mjs';
import { venue as venueOf } from './venues.mjs';
import { ownerCloseMessages, packMessages } from './exit.mjs';
import SOLANA from '../solana.json' with { type: 'json' };
import { assertNotHalted } from '../../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../../bot_root.mjs';
import { isEntry } from '../../../shared/rpc_policy.mjs';
import { sendUntilLanded } from '../../../shared/tx_send.mjs';

const require = createRequire(import.meta.url);
const { Connection, Keypair, PublicKey, SystemProgram, TransactionMessage, VersionedTransaction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const RPC = process.env.LPBOT_RPC || process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';

// --- arguments ---------------------------------------------------------------

export function parseFlags(argv) {
  const flags = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--execute') flags.execute = true;
    else if (a.startsWith('--')) {
      if (i + 1 >= argv.length || argv[i + 1].startsWith('--')) throw new Error(`${a} needs a value`);
      flags[a.slice(2)] = argv[++i];
    } else flags._.push(a);
  }
  return flags;
}

const keysOf = (s, what) => (s ?? '').split(',').map(x => x.trim()).filter(Boolean).map(k => {
  try { return new PublicKey(k); } catch { throw new Error(`${what}: ${k} is not an address`); }
});

// The payout token accounts a vault approves: each payout wallet's account
// of each payout mint. Pure.
export function payoutAccounts(wallets, mints) {
  return wallets.flatMap(w => mints.map(m => spl.getAssociatedTokenAddressSync(new PublicKey(m), new PublicKey(w), true)));
}

// --- sending (the bot's own transactions only) ---------------------------------

async function sendAll(connection, payer, groups) {
  const sigs = [];
  for (const ixs of groups) {
    assertNotHalted(BOT_ROOT);
    const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
    const msg = new TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message();
    const tx = new VersionedTransaction(msg);
    tx.sign([payer]);
    // re-sent until it lands, and read from the chain at expiry: never confirmed
    // over a websocket, which some RPCs do not serve (2026-10-11)
    try {
      sigs.push(await sendUntilLanded(connection, tx.serialize(), lastValidBlockHeight));
    } catch (e) {
      throw new Error(`${e.signature ?? 'a transaction'}: ${e.message} (landed before it: ${sigs.join(', ') || 'none'})`);
    }
  }
  return sigs;
}

// --- commands ------------------------------------------------------------------

function keys(dir) {
  if (!dir) throw new Error('keys <dir>: where to write payer.json and policy.json');
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const out = {};
  for (const name of ['payer', 'policy']) {
    const file = path.join(dir, `${name}.json`);
    if (fs.existsSync(file)) throw new Error(`${file} exists; refusing to overwrite a key`);
    const k = Keypair.generate();
    fs.writeFileSync(file, JSON.stringify([...k.secretKey]), { mode: 0o600, flag: 'wx' });
    out[name] = { file, publicKey: k.publicKey.toBase58() };
  }
  console.log(JSON.stringify({ ...out, next: `send gas (SOL) to the payer ${out.payer.publicKey}; never to the policy key` }, null, 1));
}

// The approved pools of each venue: a pool is the venue's whose program owns
// it. A pool no listed venue owns, or a venue left without one, is refused.
export async function poolsByVenue(connection, venues, pools) {
  const infos = await connection.getMultipleAccountsInfo(pools);
  const out = Object.fromEntries(venues.map(v => [v, []]));
  pools.forEach((p, i) => {
    const v = venues.find(name => infos[i]?.owner.equals(venueOf(name).program));
    if (!v) throw new Error(`pool ${p.toBase58()} belongs to none of ${venues.join(', ')}`);
    out[v].push(p);
  });
  for (const v of venues) if (!out[v].length) throw new Error(`no pool for ${v}`);
  return out;
}

// The owner's set-up of a new vault, unsigned: the owner creates the smart
// account (its only signer, threshold 1) and the bot's policies for `venues`
// and `pools`, paying the rent itself; the bot signs nothing and pays
// nothing (admin.mjs ownerSetupPlan). `f.seed` resumes a set-up planned
// before: only the steps the chain lacks are returned, and a seed another
// account took is planned again at the next free one. The pools are read
// before anything is built: one that does not fit its venue's rules stops
// it. {seed, settings, vault, report, steps (instructions to send in order),
// lookupAddresses}. `payer` and `policyKey` are the bot's public keys.
export async function setupRequest(f, { connection = new Connection(RPC, 'confirmed'), payer, policyKey }) {
  const owner = keysOf(f.owner, '--owner')[0];
  const venues = (f.venue ?? '').split(',').map(v => v.trim()).filter(Boolean);
  const pools = keysOf(f.pools, '--pools');
  const payoutWallets = keysOf(f.payout, '--payout');
  const payoutMints = keysOf(f['payout-mint'] || SOLANA.usdc_mint, '--payout-mint');
  if (!owner) throw new Error('setup needs --owner <the wallet that owns the vault>');
  if (!venues.length || !pools.length) throw new Error('setup needs --venue and --pools');
  payer = new PublicKey(payer); policyKey = new PublicKey(policyKey);
  const config = sq.decodeProgramConfig((await connection.getAccountInfo(sq.programConfigPda(), 'confirmed')).data);
  const approved = await poolsByVenue(connection, venues, pools);
  const poolInfo = await Promise.all(venues.flatMap(v => approved[v].map(p => venueOf(v).poolAccounts(connection, p))));
  const payouts = payoutAccounts(payoutWallets, payoutMints);
  const planAt = (seed) => {
    const vault = sq.vaultPda(sq.settingsPda(seed));
    const compiled = venues.map(v => compile(v, { vault, payer, pools: approved[v], payouts }));
    return ownerSetupPlan({ owner, treasury: config.treasury, seed, policyKey, venues: compiled, memo: 'lp-bot vault' });
  };
  let seed = f.seed != null ? BigInt(f.seed) : config.smartAccountIndex + 1n;
  let plan = planAt(seed), done;
  for (;;) {
    const [settingsInfo, ...policyInfos] = await connection.getMultipleAccountsInfo([plan.settings, ...plan.policies], 'confirmed');
    done = setupDone(settingsInfo?.data ?? null, policyInfos.map(Boolean), { owner });
    if (done) break;
    // another account took this seed: the next free one
    seed = (seed > config.smartAccountIndex ? seed : config.smartAccountIndex) + 1n;
    plan = planAt(seed);
  }
  const report = {
    vault: plan.vault.toBase58(), settings: plan.settings.toBase58(), owners: [owner.toBase58()], threshold: 1,
    venues: Object.fromEntries(venues.map(v => [v, { policies: plan.byVenue[v].map(String), pools: approved[v].map(String) }])),
    payouts: payouts.map(String), payoutWallets: payoutWallets.map(String), payer: payer.toBase58(), policyKey: policyKey.toBase58(),
  };
  return { seed: seed.toString(), settings: report.settings, vault: report.vault, report, done: [...done].sort((x, y) => x - y),
    steps: plan.steps.filter((_, i) => !done.has(i)),
    lookupAddresses: staticAccounts({ vault: plan.vault, policies: plan.policies, venues: venues.map(venueOf), pools: poolInfo }) };
}

// After the owner's set-up landed: the vault is the owner's alone and every
// policy is the one the rules compile, or nothing is done; then the bot makes
// the vault's lookup table with its own key (its first spend: the owner's
// gas is in by then). `lookup`, the table an earlier try planned, is reused:
// created if it is missing, its missing addresses added. `onLookup(table)`
// runs before anything is sent, so a retry finds the table. Returns the
// connect report ops/add_vault.py and onboard/store.py register.
export async function finishSetup(setup, { connection = new Connection(RPC, 'confirmed'), payer, policyKey, lookup = null,
  onLookup = async () => {}, log = console.log }) {
  const { report } = setup;
  if (setup.steps.length) throw new Error(`the owner's set-up is not on chain yet: ${setup.steps.length} steps missing`);
  const settings = new PublicKey(report.settings);
  const problems = connectProblems((await connection.getAccountInfo(settings, 'confirmed')).data,
    { owners: report.owners, threshold: 1, botKeys: [payer.publicKey, policyKey] });
  if (problems.length) throw new Error(`the vault is not as planned: ${problems.join('; ')}`);
  for (const [venue, v] of Object.entries(report.venues)) {
    const cfg = { settings, vault: new PublicKey(report.vault), policies: v.policies.map(k => new PublicKey(k)) };
    const compiled = compile(venue, { vault: cfg.vault, payer: payer.publicKey, pools: v.pools.map(k => new PublicKey(k)),
      payouts: report.payouts.map(k => new PublicKey(k)) });
    await verifyPolicy(connection, cfg, policyKey, compiled);
  }
  const table = lookup ? (await connection.getAddressLookupTable(new PublicKey(lookup))).value : null;
  const plan = lookupPlan({ payer: payer.publicKey, recentSlot: await connection.getSlot('finalized'), table: table ? lookup : null,
    have: table?.state.addresses ?? [], addresses: setup.lookupAddresses });
  await onLookup(plan.table.toBase58());
  const sigs = plan.txs.length ? await sendAll(connection, payer, plan.txs) : [];
  const done = { ...report, lookup: plan.table.toBase58(), lookupAddresses: setup.lookupAddresses.length, signatures: sigs, sent: true };
  log(JSON.stringify(done, null, 1));
  return done;
}

async function status() {
  const cfg = vaultConfig();
  if (!cfg) throw new Error('LPBOT_VAULT_SETTINGS is not set');
  const connection = new Connection(RPC, 'confirmed');
  const s = sq.decodeSettings((await connection.getAccountInfo(cfg.settings, 'confirmed')).data);
  const out = { vault: cfg.vault.toBase58(), settings: cfg.settings.toBase58(),
    owners: s.signers.map(x => x.key.toBase58()), threshold: s.threshold, timeLock: s.timeLock,
    sol: (await connection.getBalance(cfg.vault)) / 1e9 };
  const payer = process.env.WALLET_SECRET_PATH ? (await readKeypair(process.env.WALLET_SECRET_PATH)).publicKey : null;
  if (payer) out.payerGasSol = (await connection.getBalance(payer)) / 1e9;
  const policyKey = process.env.LPBOT_VAULT_POLICY_KEY ? (await readKeypair(process.env.LPBOT_VAULT_POLICY_KEY)).publicKey : null;
  if (payer && policyKey && process.env.LPBOT_VAULT_VENUE) {
    const compiled = compile(process.env.LPBOT_VAULT_VENUE, { vault: cfg.vault, payer, pools: cfg.pools, payouts: cfg.payouts });
    try { await verifyPolicy(connection, cfg, policyKey, compiled); out.policies = 'as configured'; }
    catch (e) { out.policies = e.message; }
    out.botIsOwner = out.owners.includes(payer.toBase58()) || out.owners.includes(policyKey.toBase58());
  }
  console.log(JSON.stringify(out, null, 1));
}

// A decimal amount as raw units of a token with `decimals`: digits, an
// optional point and at most `decimals` digits after it. Exact (BigInt): no
// sign, exponent or rounding. Pure.
export function parseAmount(text, decimals) {
  const m = /^(\d+)(?:\.(\d+))?$/.exec(String(text ?? ''));
  if (!m) throw new Error(`${text} is not an amount (digits, an optional point and digits)`);
  const frac = m[2] ?? '';
  if (frac.length > decimals) throw new Error(`${text} has more than ${decimals} decimals`);
  return BigInt(m[1]) * 10n ** BigInt(decimals) + BigInt(frac.padEnd(decimals, '0') || '0');
}

// Raw units as an exact decimal, without trailing zeros. Pure.
export function formatAmount(raw, decimals) {
  const v = BigInt(raw);
  if (v < 0n) throw new Error('a negative amount');
  const unit = 10n ** BigInt(decimals);
  const frac = (v % unit).toString().padStart(decimals, '0').replace(/0+$/, '');
  return frac ? `${v / unit}.${frac}` : `${v / unit}`;
}

// `f.amount` of a balance `have` (raw): 'all' is the whole of it; anything
// else must parse (parseAmount), be above zero and at most `have`. `holds`
// says what the vault holds, in every refusal.
function amountOf(f, have, decimals, holds) {
  let raw;
  try { raw = f.amount === 'all' ? have : parseAmount(f.amount, decimals); } catch (e) { throw new Error(`${e.message}; ${holds}`); }
  if (!(raw > 0n && raw <= have)) throw new Error(holds);
  return raw;
}

const NATIVE_MINT = new PublicKey(SOLANA.native_mint);

// Everything of `mints` the vault holds, then its SOL, to `to`: each token
// account's balance into `to`'s account (created by the owner if missing),
// the account closed with its rent to `to`, the SOL last. A WSOL account is
// closed straight to `to`, which unwraps it. Returns {steps, summary}; the
// steps go through packMessages.
async function withdrawAll(connection, cfg, owner, to, mints) {
  const steps = [], parts = [];
  for (const mint of mints) {
    const info = await connection.getAccountInfo(mint);
    if (!info) throw new Error(`mint ${mint.toBase58()} not found`);
    const src = spl.getAssociatedTokenAddressSync(mint, cfg.vault, true, info.owner);
    if (!await connection.getAccountInfo(src)) continue;
    const have = (await spl.getAccount(connection, src, 'confirmed', info.owner)).amount;
    if (mint.equals(NATIVE_MINT)) {
      if (have > 0n) parts.push(`${formatAmount(have, 9)} wrapped SOL`);
    } else if (have > 0n) {
      const m = await spl.getMint(connection, mint, 'confirmed', info.owner);
      const dst = spl.getAssociatedTokenAddressSync(mint, to, true, info.owner);
      steps.push({ outside: spl.createAssociatedTokenAccountIdempotentInstruction(owner, dst, to, mint, info.owner) });
      steps.push({ inside: spl.createTransferCheckedInstruction(src, mint, dst, cfg.vault, have, m.decimals, [], info.owner) });
      parts.push(`${formatAmount(have, m.decimals)} of ${mint.toBase58()}`);
    }
    steps.push({ inside: spl.createCloseAccountInstruction(src, to, cfg.vault, [], info.owner) });
  }
  // the closes pay their rent straight to `to`: what the vault holds now is all its own
  const lamports = BigInt(await connection.getBalance(cfg.vault));
  if (lamports > 0n) {
    steps.push({ inside: SystemProgram.transfer({ fromPubkey: cfg.vault, toPubkey: to, lamports }) });
    parts.push(`${formatAmount(lamports, 9)} SOL`);
  }
  if (!steps.length) throw new Error(`the vault ${cfg.vault.toBase58()} holds no SOL and no account of ${mints.map(String).join(', ')}`);
  return { steps, summary: `withdraw everything from the vault ${cfg.vault.toBase58()} to ${to.toBase58()}: `
    + `${parts.join(', ') || 'nothing but account rent'}; the vault's token accounts closed` };
}

// An owner's transaction, unsigned, for the signing page: the page sets a
// fresh blockhash, the owner's wallet signs it, the page sends it. `rpc`: a
// @solana/kit RPC for an Orca close (default: one on the connection's endpoint).
export async function request(action, f, { connection = new Connection(RPC, 'confirmed'), env = process.env, rpc = null } = {}) {
  const cfg = vaultConfig(env);
  if (!cfg) throw new Error('LPBOT_VAULT_SETTINGS is not set');
  const owner = keysOf(f.owner, '--owner')[0];
  if (!owner) throw new Error('--owner <the owner wallet that signs>');
  const settings = sq.decodeSettings((await connection.getAccountInfo(cfg.settings, 'confirmed')).data);
  if (!settings.signers.some(s => s.key.equals(owner))) throw new Error(`${owner.toBase58()} is not an owner of this vault`);
  if (settings.threshold > 1) throw new Error(`the vault needs ${settings.threshold} owner signatures; this tool builds one-signature requests`);
  let groups, summary, transactions;
  if (action === 'withdraw') {
    const to = keysOf(f.to, '--to')[0];
    if (!to) throw new Error('withdraw needs --to <wallet>');
    const ixs = [];
    const which = (f.mint ?? 'SOL').toUpperCase();
    if (which === 'ALL') {
      if (f.amount != null && f.amount !== 'all') throw new Error('--mint ALL takes everything; leave out --amount');
      const all = await withdrawAll(connection, cfg, owner, to, keysOf(f.mints ?? `${SOLANA.native_mint},${SOLANA.usdc_mint}`, '--mints'));
      summary = all.summary;
      transactions = packMessages(all.steps, { settings: cfg.settings, vault: cfg.vault, owner });
    } else if (which === 'SOL') {
      const have = BigInt(await connection.getBalance(cfg.vault));
      const lamports = amountOf(f, have, 9, `the vault holds ${formatAmount(have, 9)} SOL`);
      ixs.push(SystemProgram.transfer({ fromPubkey: cfg.vault, toPubkey: to, lamports }));
      summary = `withdraw ${formatAmount(lamports, 9)} SOL from the vault ${cfg.vault.toBase58()} to ${to.toBase58()}`;
    } else {
      const mint = new PublicKey(f.mint);
      const program = (await connection.getAccountInfo(mint)).owner;
      const m = await spl.getMint(connection, mint, 'confirmed', program);
      const src = spl.getAssociatedTokenAddressSync(mint, cfg.vault, true, program);
      const dst = spl.getAssociatedTokenAddressSync(mint, to, true, program);
      const have = (await spl.getAccount(connection, src, 'confirmed', program)).amount;
      const raw = amountOf(f, have, m.decimals, `the vault holds ${formatAmount(have, m.decimals)} of ${mint.toBase58()}`);
      ixs.push(spl.createTransferCheckedInstruction(src, mint, dst, cfg.vault, raw, m.decimals, [], program));
      summary = `withdraw ${formatAmount(raw, m.decimals)} of ${mint.toBase58()} from the vault to ${to.toBase58()}`;
      groups = [[spl.createAssociatedTokenAccountIdempotentInstruction(owner, dst, to, mint, program),
        sq.ownerExecuteIx({ settings: cfg.settings, signers: [owner], vault: cfg.vault, ixs })]];
    }
    if (!transactions) groups ??= [[sq.ownerExecuteIx({ settings: cfg.settings, signers: [owner], vault: cfg.vault, ixs })]];
  } else if (action === 'close') {
    const venue = env.LPBOT_VAULT_VENUE;
    if (!venue) throw new Error('close needs LPBOT_VAULT_VENUE');
    const out = await ownerCloseMessages(connection, { settings: cfg.settings, vault: cfg.vault, owner, venue, pools: cfg.pools, rpc });
    summary = out.summary;
    transactions = out.messages;
  } else if (action === 'revoke' || action === 'approve') {
    const venue = env.LPBOT_VAULT_VENUE;
    const payer = keysOf(f.payer, '--payer')[0];
    if (!venue || !payer) throw new Error(`${action} needs LPBOT_VAULT_VENUE and --payer <the bot's payer key>`);
    const policyKey = keysOf(f['policy-key'], '--policy-key')[0];
    if (action === 'approve' && !policyKey) throw new Error('approve needs --policy-key <the bot\'s policy key>');
    const rules = await rulesRequest(action, { connection, settings: cfg.settings, owner, payer, policyKey,
      venues: [{ venue, policies: cfg.policies, pools: cfg.pools, payouts: cfg.payouts }] });
    groups = rules.groups;
    summary = rules.summary;
  } else throw new Error(`unknown request ${action} (withdraw, close, revoke, approve)`);
  transactions ??= groups.map(ixs => {
    const msg = new TransactionMessage({ payerKey: owner, recentBlockhash: PublicKey.default.toBase58(), instructions: ixs }).compileToLegacyMessage();
    return Buffer.from(msg.serialize()).toString('base64');
  });
  return { kind: 'lp-bot vault request', network: 'solana:mainnet', vault: cfg.vault.toBase58(), owner: owner.toBase58(),
    action, summary, messages: transactions };
}

// The owner's transactions for approve or revoke over `venues` of one vault
// ([{venue, policies, pools, payouts}], as the bot's config names them), in
// the order admin.mjs approvePlan gives: new policies first, updates last.
// Approve: the rules this code compiles, the bot's policy key their signer;
// a policy staged for a new venue is created. Revoke: the same rules, the
// owner their only signer; a staged policy stays uncreated. {groups,
// summary, steps}; nothing to sign is an error.
export async function rulesRequest(action, { connection, settings, owner, payer, policyKey, venues }) {
  settings = new PublicKey(settings);
  const vault = sq.vaultPda(settings);
  const signer = action === 'revoke' ? owner : policyKey;
  if (!signer) throw new Error(`${action} needs the bot's policy key`);
  const settingsInfo = await connection.getAccountInfo(settings, 'confirmed');
  if (!settingsInfo) throw new Error(`the vault settings ${settings.toBase58()} do not exist`);
  const addresses = venues.flatMap(v => v.policies.map(k => new PublicKey(k)));
  const infos = addresses.length ? await connection.getMultipleAccountsInfo(addresses, 'confirmed') : [];
  let at = 0;
  const steps = approvePlan({ settings, settingsData: settingsInfo.data, owner, signer, create: action === 'approve',
    venues: venues.map(v => {
      const keys = (xs) => xs.map(k => new PublicKey(k));
      const compiled = compile(v.venue, { vault, payer, pools: keys(v.pools), payouts: keys(v.payouts) });
      const mine = infos.slice(at, at + v.policies.length);
      at += v.policies.length;
      return { venue: v.venue, compiled, addresses: v.policies, infos: mine };
    }) });
  if (!steps.length) throw new Error(`nothing to ${action}: every policy of the vault ${vault.toBase58()} is already as asked`);
  const created = steps.filter(s => s.kind === 'create').length;
  const summary = action === 'revoke'
    ? `revoke the bot: ${steps.length} policies of the vault ${vault.toBase58()} will name only you`
    : `approve for the bot (${signer.toBase58()}) on ${venues.map(v => `${v.venue} (pools ${v.pools.map(String).join(', ')})`).join(' and ')}; `
      + `payouts ${[...new Set(venues.flatMap(v => v.payouts.map(String)))].join(', ') || 'none'}`
      + (created ? `; ${created} new policies, their rent paid by you` : '');
  return { groups: steps.map(s => s.ixs), summary, steps };
}

// Stage a venue on a vault that exists (its config in `env`, the
// LPBOT_VAULT_* of a venue it has): the addresses its policies get when the
// owners approve them (the vault's next policy seeds), and the accounts its
// transactions need in the vault's lookup table. With --execute the bot adds
// those to the table (its own transactions; the payer is the table's
// authority). Prints the venue's row for ops/add_vault.py venue. The owners
// then approve with one signature per policy (rulesRequest). Never a second
// vault: one vault per wallet.
export async function stageVenue(f, { connection = new Connection(RPC, 'confirmed'), env = process.env, payer = null, log = console.log } = {}) {
  const cfg = vaultConfig(env);
  if (!cfg) throw new Error('LPBOT_VAULT_SETTINGS is not set: stage a venue on a vault that exists');
  const venue = String(f.venue ?? '').trim();
  const pools = keysOf(f.pools, '--pools');
  if (!venue || !pools.length) throw new Error('add-venue needs --venue and --pools');
  if (venue === env.LPBOT_VAULT_VENUE) throw new Error(`${venue} is the venue the environment names: the vault has it already`);
  payer ??= await readKeypair(process.env.WALLET_SECRET_PATH ?? '');
  await poolsByVenue(connection, [venue], pools);
  const settings = sq.decodeSettings((await connection.getAccountInfo(cfg.settings, 'confirmed')).data);
  const poolInfo = await Promise.all(pools.map(p => venueOf(venue).poolAccounts(connection, p)));
  const compiled = compile(venue, { vault: cfg.vault, payer: payer.publicKey, pools, payouts: cfg.payouts });
  const policies = stagedPolicies(cfg.settings, settings.policySeed, compiled.policies.length);
  const exist = await connection.getMultipleAccountsInfo(policies, 'confirmed');
  if (exist.some(Boolean)) throw new Error(`a policy at the vault's next seeds exists already (${policies.filter((_, i) => exist[i]).join(', ')})`);
  if (!cfg.lookup) throw new Error('the vault has no lookup table (LPBOT_VAULT_LOOKUP)');
  const table = (await connection.getAddressLookupTable(cfg.lookup)).value;
  if (!table) throw new Error(`vault lookup table ${cfg.lookup.toBase58()} does not exist`);
  if (!table.state.authority?.equals(payer.publicKey)) throw new Error(`the bot's payer is not the authority of the lookup table ${cfg.lookup.toBase58()}`);
  const addresses = staticAccounts({ vault: cfg.vault, policies, venues: [venueOf(venue)], pools: poolInfo });
  const lookup = lookupPlan({ payer: payer.publicKey, table: cfg.lookup, have: table.state.addresses, addresses });
  const report = { settings: cfg.settings.toBase58(), vault: cfg.vault.toBase58(), venue, policies: policies.map(String),
    pools: pools.map(String), payouts: cfg.payouts.map(String), lookup: cfg.lookup.toBase58(),
    lookupAdds: addresses.filter(a => !table.state.addresses.some(k => k.equals(a))).length };
  if (!f.execute) {
    log(JSON.stringify({ ...report, sent: false }, null, 1));
    log('DRY RUN: pass --execute to add the accounts to the lookup table. The owners approve the policies after.');
    return report;
  }
  const signatures = await sendAll(connection, payer, lookup.txs);
  const done = { ...report, signatures, sent: true };
  log(JSON.stringify(done, null, 1));
  return done;
}

export async function main(argv = process.argv.slice(2)) {
  const [cmd, ...rest] = argv;
  const f = parseFlags(rest);
  if (cmd === 'keys') return keys(f._[0]);
  if (cmd === 'setup' || cmd === 'finish') {
    const payer = await readKeypair(process.env.WALLET_SECRET_PATH ?? '');
    const policyKey = (await readKeypair(process.env.LPBOT_VAULT_POLICY_KEY ?? '')).publicKey;
    const setup = await setupRequest(f, { payer: payer.publicKey, policyKey });
    if (cmd === 'finish') return finishSetup(setup, { payer, policyKey });
    const owner = new PublicKey(setup.report.owners[0]);
    const messages = packSteps(setup.steps, owner).map(ixs => Buffer.from(new TransactionMessage({ payerKey: owner,
      recentBlockhash: PublicKey.default.toBase58(), instructions: ixs }).compileToLegacyMessage().serialize()).toString('base64'));
    return console.log(JSON.stringify({ kind: 'lp-bot vault request', network: 'solana:mainnet', action: 'setup', seed: setup.seed,
      vault: setup.vault, settings: setup.settings, owner: owner.toBase58(), messages }));
  }
  if (cmd === 'status') return status();
  if (cmd === 'accounts') {
    return console.log(JSON.stringify(payoutAccounts(keysOf(f.wallets, '--wallets'), keysOf(f.mints ?? SOLANA.usdc_mint, '--mints')).map(String)));
  }
  if (cmd === 'request') return console.log(JSON.stringify(await request(f._[0], f)));
  if (cmd === 'add-venue') return stageVenue(f);
  console.log('commands: keys <dir> | setup|finish --owner <k> --venue <v>[,<v>] --pools .. [--payout ..] [--seed n] | status '
    + '| request <withdraw|close|revoke|approve> --owner <k> [...] | add-venue --venue <v> --pools .. [--execute]');
}

if (isEntry(import.meta.url)) main().catch(e => { console.error('ERROR:', signerError(e)); process.exitCode = 1; });
