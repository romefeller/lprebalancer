// The operator's tool for vaults: create the bot's keys, connect an owner's
// wallet, read a vault, and build the transactions only owners can sign.
//
//   node chains/solana/vault/cli.mjs keys <dir>
//       two new keys (0600): payer.json pays fees and rent, policy.json signs
//       the policies. Prints both public keys; the owner sends gas to the payer.
//   node chains/solana/vault/cli.mjs connect --owners <k>[,<k>] [--threshold 1] --venue <venue>
//       --pools <pool>[,<pool>] [--payout <wallet>[,<wallet>]] [--payout-mint <mint>] [--execute]
//       a new vault the owners control, the venue's policies for the bot, the
//       bot gone from the owners, the lookup table. Prints the vault's config
//       (ops/add_vault.py stores it). Dry run by default.
//   node chains/solana/vault/cli.mjs accounts --wallets <w>[,<w>] [--mints <m>[,<m>]]
//       the payout token accounts of these wallets (default mint: USDC), as JSON
//   node chains/solana/vault/cli.mjs status
//       the vault in LPBOT_VAULT_*: owners, threshold, whether the policies on
//       chain are the configured ones, balances.
//   node chains/solana/vault/cli.mjs request <withdraw|revoke|approve> --owner <k> [...]
//       an owner's transaction for owner/index.html, as one JSON line. withdraw
//       --to <wallet> --mint <mint|SOL> --amount <n|all>; revoke; approve
//       (the policies for the venue, pools and payouts now configured).
//
// Keys: WALLET_SECRET_PATH (payer) and LPBOT_VAULT_POLICY_KEY (policy), as the
// signers read them. The owners' keys are only ever public keys here.
import { createRequire } from 'module';
import fs from 'node:fs';
import path from 'node:path';
import * as sq from './squads.mjs';
import { compile } from './policy.mjs';
import { connectPlan, connectProblems, lookupPlan, staticAccounts } from './admin.mjs';
import { readKeypair, vaultConfig, verifyPolicy } from './account.mjs';
import { venue as venueOf } from './venues.mjs';
import SOLANA from '../solana.json' with { type: 'json' };
import { assertNotHalted } from '../../../shared/halt_guard.mjs';
import { BOT_ROOT } from '../../../bot_root.mjs';
import { isEntry } from '../../../shared/rpc_policy.mjs';

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
    const signature = await connection.sendRawTransaction(tx.serialize());
    const r = await connection.confirmTransaction({ signature, blockhash, lastValidBlockHeight }, 'confirmed');
    if (r.value.err) throw new Error(`${signature} failed: ${JSON.stringify(r.value.err)} (sent so far: ${sigs.join(', ') || 'none'})`);
    sigs.push(signature);
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

export async function connect(f, { connection = new Connection(RPC, 'confirmed') } = {}) {
  const owners = keysOf(f.owners, '--owners');
  const threshold = Number(f.threshold ?? 1);
  const venue = f.venue;
  const pools = keysOf(f.pools, '--pools');
  const payoutWallets = keysOf(f.payout, '--payout');
  const payoutMints = keysOf(f['payout-mint'] ?? SOLANA.usdc_mint, '--payout-mint');
  if (!venue || !pools.length) throw new Error('connect needs --venue and --pools');
  const payer = await readKeypair(process.env.WALLET_SECRET_PATH ?? '');
  const policyKey = await readKeypair(process.env.LPBOT_VAULT_POLICY_KEY ?? '');
  const config = sq.decodeProgramConfig((await connection.getAccountInfo(sq.programConfigPda())).data);
  if (config.creationFee > 0n) console.error(`note: the program charges ${Number(config.creationFee) / 1e9} SOL per account`);
  const seed = config.smartAccountIndex + 1n;
  const vault = sq.vaultPda(sq.settingsPda(seed));
  const payouts = payoutAccounts(payoutWallets, payoutMints);
  const compiled = compile(venue, { vault, payer: payer.publicKey, pools, payouts });
  const plan = connectPlan({ creator: payer.publicKey, treasury: config.treasury, seed, owners, threshold,
    policyKey: policyKey.publicKey, compiled, memo: 'lp-bot vault' });
  const v = venueOf(venue);
  const lookupAddresses = staticAccounts({ vault: plan.vault, policies: plan.policies, venue: v,
    pools: await Promise.all(pools.map(p => v.poolAccounts(connection, p))) });
  const report = {
    vault: plan.vault.toBase58(), settings: plan.settings.toBase58(), owners: owners.map(String), threshold,
    venue, policies: plan.policies.map(String), pools: pools.map(String), payouts: payouts.map(String),
    payoutWallets: payoutWallets.map(String), payer: payer.publicKey.toBase58(), policyKey: policyKey.publicKey.toBase58(),
    transactions: plan.txs.length, lookupAddresses: lookupAddresses.length,
  };
  if (!f.execute) {
    console.log(JSON.stringify({ ...report, sent: false }, null, 1));
    console.log('DRY RUN — pass --execute to create the vault. Nothing is deposited by this.');
    return report;
  }
  const sigs = await sendAll(connection, payer, plan.txs);
  const problems = connectProblems((await connection.getAccountInfo(plan.settings, 'confirmed')).data,
    { owners, threshold, botKeys: [payer.publicKey, policyKey.publicKey] });
  if (problems.length) throw new Error(`the vault is not as planned; do not deposit: ${problems.join('; ')}`);
  const lookup = lookupPlan({ payer: payer.publicKey, recentSlot: await connection.getSlot('finalized'), addresses: lookupAddresses });
  sigs.push(...await sendAll(connection, payer, lookup.txs));
  const done = { ...report, lookup: lookup.table.toBase58(), signatures: sigs, sent: true,
    deposit: `owners deposit to ${report.vault} (the vault), never to ${report.settings}` };
  console.log(JSON.stringify(done, null, 1));
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

// An owner's transaction, unsigned, for the signing page: the page sets a
// fresh blockhash, the owner's wallet signs it, the page sends it.
export async function request(action, f, { connection = new Connection(RPC, 'confirmed'), env = process.env } = {}) {
  const cfg = vaultConfig(env);
  if (!cfg) throw new Error('LPBOT_VAULT_SETTINGS is not set');
  const owner = keysOf(f.owner, '--owner')[0];
  if (!owner) throw new Error('--owner <the owner wallet that signs>');
  const settings = sq.decodeSettings((await connection.getAccountInfo(cfg.settings, 'confirmed')).data);
  if (!settings.signers.some(s => s.key.equals(owner))) throw new Error(`${owner.toBase58()} is not an owner of this vault`);
  if (settings.threshold > 1) throw new Error(`the vault needs ${settings.threshold} owner signatures; this tool builds one-signature requests`);
  let groups, summary;
  if (action === 'withdraw') {
    const to = keysOf(f.to, '--to')[0];
    if (!to) throw new Error('withdraw needs --to <wallet>');
    const ixs = [];
    if ((f.mint ?? 'SOL').toUpperCase() === 'SOL') {
      const have = await connection.getBalance(cfg.vault);
      const lamports = f.amount === 'all' ? have : Math.round(Number(f.amount) * 1e9);
      if (!(lamports > 0 && lamports <= have)) throw new Error(`the vault holds ${have / 1e9} SOL`);
      ixs.push(SystemProgram.transfer({ fromPubkey: cfg.vault, toPubkey: to, lamports }));
      summary = `withdraw ${lamports / 1e9} SOL from the vault ${cfg.vault.toBase58()} to ${to.toBase58()}`;
    } else {
      const mint = new PublicKey(f.mint);
      const program = (await connection.getAccountInfo(mint)).owner;
      const m = await spl.getMint(connection, mint, 'confirmed', program);
      const src = spl.getAssociatedTokenAddressSync(mint, cfg.vault, true, program);
      const dst = spl.getAssociatedTokenAddressSync(mint, to, true, program);
      const have = (await spl.getAccount(connection, src, 'confirmed', program)).amount;
      const raw = f.amount === 'all' ? have : BigInt(Math.round(Number(f.amount) * 10 ** m.decimals));
      if (!(raw > 0n && raw <= have)) throw new Error(`the vault holds ${Number(have) / 10 ** m.decimals} of ${mint.toBase58()}`);
      ixs.push(spl.createTransferCheckedInstruction(src, mint, dst, cfg.vault, raw, m.decimals, [], program));
      summary = `withdraw ${Number(raw) / 10 ** m.decimals} of ${mint.toBase58()} from the vault to ${to.toBase58()}`;
      groups = [[spl.createAssociatedTokenAccountIdempotentInstruction(owner, dst, to, mint, program),
        sq.ownerExecuteIx({ settings: cfg.settings, signers: [owner], vault: cfg.vault, ixs })]];
    }
    groups ??= [[sq.ownerExecuteIx({ settings: cfg.settings, signers: [owner], vault: cfg.vault, ixs })]];
  } else if (action === 'revoke' || action === 'approve') {
    const venue = env.LPBOT_VAULT_VENUE;
    const payer = keysOf(f.payer ?? '', '--payer')[0];
    if (!venue || !payer) throw new Error(`${action} needs LPBOT_VAULT_VENUE and --payer <the bot's payer key>`);
    const compiled = compile(venue, { vault: cfg.vault, payer, pools: cfg.pools, payouts: cfg.payouts });
    if (compiled.policies.length !== cfg.policies.length) throw new Error('the rules now need a different number of policies; reconnect');
    const policyKey = keysOf(f['policy-key'] ?? '', '--policy-key')[0];
    if (action === 'approve' && !policyKey) throw new Error('approve needs --policy-key <the bot\'s policy key>');
    // Revoke keeps every rule but names the owner as the policy's only signer.
    const signer = action === 'revoke' ? owner : policyKey;
    groups = cfg.policies.map((address, i) => [sq.settingsSyncIx({ settings: cfg.settings, signers: [owner], rentPayer: owner,
      extra: [address], actions: [{ kind: 'policyUpdate', address, policy: compiled.policies[i].policy, signers: [{ key: signer }] }] })]);
    summary = action === 'revoke'
      ? `revoke the bot: ${cfg.policies.length} policies of the vault ${cfg.vault.toBase58()} will name only you`
      : `approve for the bot (${signer.toBase58()}) on ${venue}: pools ${cfg.pools.map(String).join(', ')}; `
        + `payouts ${cfg.payouts.map(String).join(', ') || 'none'}`;
  } else throw new Error(`unknown request ${action} (withdraw, revoke, approve)`);
  const transactions = groups.map(ixs => {
    const msg = new TransactionMessage({ payerKey: owner, recentBlockhash: PublicKey.default.toBase58(), instructions: ixs }).compileToLegacyMessage();
    return Buffer.from(msg.serialize()).toString('base64');
  });
  return { kind: 'lp-bot vault request', network: 'solana:mainnet', vault: cfg.vault.toBase58(), owner: owner.toBase58(),
    action, summary, messages: transactions };
}

export async function main(argv = process.argv.slice(2)) {
  const [cmd, ...rest] = argv;
  const f = parseFlags(rest);
  if (cmd === 'keys') return keys(f._[0]);
  if (cmd === 'connect') return connect(f);
  if (cmd === 'status') return status();
  if (cmd === 'accounts') {
    return console.log(JSON.stringify(payoutAccounts(keysOf(f.wallets, '--wallets'), keysOf(f.mints ?? SOLANA.usdc_mint, '--mints')).map(String)));
  }
  if (cmd === 'request') return console.log(JSON.stringify(await request(f._[0], f)));
  console.log('commands: keys <dir> | connect --owners .. --venue .. --pools .. [--payout ..] [--execute] | status '
    + '| request <withdraw|revoke|approve> --owner <k> [...]');
}

if (isEntry(import.meta.url)) main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
