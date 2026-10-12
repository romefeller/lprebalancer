// Check an owner's pending transaction outside the public server: decode it
// and diff it against the vault rules and pins of a git commit you trust,
// with the same decoder the page runs (decode_sol.mjs, decode_evm.mjs).
//
//   node onboard/verify.mjs --commit <sha> --paste '<what the page copied>'
//   node onboard/verify.mjs --commit <sha> --from https://romefeller.app/bot --invite <code>
//        --name <vault name> --owner <your wallet> --action approve|revoke|close|withdraw
//        [--token SOL|USDC|ALL --amount <n>|all] [--chain solana|polygon]
//   --rpc <url>   Solana: also read the vault from the chain (default: the public endpoint;
//                 --rpc none skips it): your wallet must be its only owner, each policy the
//                 transaction names must be one of its policies.
//
// The page's "Copy the transaction" button copies {chain, tx, expect}. The
// rules (rules.json), the pools (solana.json) and the bot's keys
// (onboard.json) come from `git show <commit>:<file>`, not from this
// checkout or the server. Exit status 0 only when every transaction checks.
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'module';
import { decodeSol } from './decode_sol.mjs';
import { decodeEvm } from './decode_evm.mjs';
import { toRaw } from './units.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { isEntry } from '../shared/rpc_policy.mjs';

const require = createRequire(import.meta.url);
const { Connection, PublicKey } = require('@solana/web3.js');
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

export function flags(argv) {
  const f = {};
  for (let i = 0; i < argv.length; i += 2) {
    if (!argv[i].startsWith('--') || argv[i + 1] === undefined) throw new Error(`${argv[i]}: --name value pairs only`);
    f[argv[i].slice(2)] = argv[i + 1];
  }
  return f;
}

// A file of the repository at `commit`, as git has it.
export function atCommit(commit, file, git = (args) => execFileSync('git', args, { cwd: ROOT, encoding: 'utf8' })) {
  if (!/^[0-9a-f]{7,40}$/.test(commit)) throw new Error('--commit: a commit id (hex)');
  return git(['show', `${commit}:${file}`]);
}

// The pins of `commit`: what decode_sol and decode_evm check against. Pure
// over the three files' text and the Polygon module's addresses.
export function pinsOf(rulesText, solanaText, onboardText, polygon) {
  const rules = JSON.parse(rulesText), solana = JSON.parse(solanaText), cfg = JSON.parse(onboardText);
  const sol = cfg.chains.solana, poly = cfg.chains.polygon;
  return {
    rulesSha256: crypto.createHash('sha256').update(rulesText).digest('hex'),
    solana: { payer: sol.botKeys[0], policyKey: sol.botKeys[1], operator: solana[sol.operatorWallet],
      pools: Object.fromEntries(Object.entries(solana[sol.poolsKey]).map(([v, p]) => [v, [p]])), rules, solana },
    polygon: { chainId: polygon.CHAIN_ID, botKey: poly.botKeys[0], npm: polygon.V3.npm, wrapped: { WPOL: polygon.WPOL },
      tokens: Object.fromEntries(Object.entries(poly.tokens).map(([s, t]) => [s, { address: t.native ? null : polygon[s], decimals: t.decimals }])) },
  };
}

// What the chain says of a Solana vault the expectation names: problems, or [].
export async function chainProblems(connection, expect) {
  const out = [];
  if (!expect.settings) return out;
  const info = await connection.getAccountInfo(new PublicKey(expect.settings), 'confirmed');
  if (!info) return [`the settings account ${expect.settings} does not exist`];
  const s = sq.decodeSettings(info.data);
  const owners = s.signers.map(x => x.key.toBase58());
  if (owners.length !== 1 || owners[0] !== expect.owner || s.threshold !== 1) out.push(`the vault's owners are ${owners.join(', ')} (threshold ${s.threshold}), not your wallet alone`);
  if (sq.vaultPda(new PublicKey(expect.settings)).toBase58() !== expect.vault) out.push('the vault is not the account of these settings');
  const policies = Object.values(expect.policies ?? {}).flat();
  const infos = policies.length ? await connection.getMultipleAccountsInfo(policies.map(p => new PublicKey(p)), 'confirmed') : [];
  infos.forEach((p, i) => {
    if (!p) out.push(`policy ${policies[i]} does not exist`);
    else if (!sq.decodePolicyHead(p.data).settings.equals(new PublicKey(expect.settings))) out.push(`policy ${policies[i]} belongs to another vault`);
  });
  return out;
}

// Each transaction checked: {lines, problems} per transaction.
export function check(chain, txs, expectOf, pins) {
  return txs.map((tx, i) => (chain === 'solana' ? decodeSol(tx, expectOf(i), { ...pins.solana }) : decodeEvm(tx, expectOf(i), pins.polygon)));
}

async function fromServer(f, pins) {
  const post = async (p, body) => {
    const r = await fetch(new URL(`api/${p}`, f.from.replace(/\/?$/, '/')), { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ invite: f.invite, ...body }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error ?? `the server answered ${r.status}`);
    return j;
  };
  const chain = f.chain ?? 'solana';
  const req = { name: f.name, action: f.action, token: f.token, amount: f.amount };
  const txs = [];
  let answer = await post('tx', { ...req, index: 0 });
  for (let i = 0; i < answer.count; i++) {
    if (i > 0 && answer.index !== undefined) answer = await post('tx', { ...req, index: i });
    txs.push(answer.index !== undefined ? answer.transactions[0] : answer.transactions[i]);
  }
  const ctx = answer.context ?? {};
  const kind = f.action;
  const decimals = chain === 'solana' ? { SOL: 9, USDC: 6 }[f.token] : pins.polygon.tokens[f.token]?.decimals;
  const raw = (f.token === 'ALL' || !f.amount || f.amount === 'all') ? 'all' : toRaw(f.amount, decimals).toString();
  const mint = chain === 'solana' && f.token && f.token !== 'ALL' ? { SOL: pins.solana.solana.native_mint, USDC: pins.solana.solana.usdc_mint }[f.token] : null;
  const expect = chain === 'solana' ? { kind, owner: f.owner, ...ctx, mint, raw } : { kind, owner: f.owner, safe: ctx.safe, roles: ctx.roles, token: f.token, raw };
  return { chain, txs, expect: () => expect };
}

export async function main(argv = process.argv.slice(2), { git, connection } = {}) {
  const f = flags(argv);
  if (!f.commit) throw new Error('--commit <the git commit whose rules you trust>');
  const polygon = await import(path.join(ROOT, 'chains/evm/polygon.mjs'));
  const at = (file) => atCommit(f.commit, file, git);
  const pins = pinsOf(at('chains/solana/vault/rules.json'), at('chains/solana/solana.json'), at('onboard/onboard.json'), polygon);
  console.log(`rules.json at ${f.commit}: sha256 ${pins.rulesSha256} (the page shows its first 16 characters)`);
  let job;
  if (f.paste) {
    const p = JSON.parse(f.paste);
    job = { chain: p.chain, txs: [p.tx], expect: () => p.expect };
  } else {
    for (const k of ['from', 'invite', 'name', 'owner', 'action']) if (!f[k]) throw new Error(`--${k} is needed with --from`);
    job = await fromServer(f, pins);
  }
  let bad = 0;
  if (job.chain === 'solana' && f.rpc !== 'none') {
    const problems = await chainProblems(connection ?? new Connection(f.rpc ?? 'https://api.mainnet-beta.solana.com', 'confirmed'), job.expect(0));
    for (const p of problems) console.log(`CHAIN: ${p}`);
    bad += problems.length;
  }
  check(job.chain, job.txs, job.expect, pins).forEach((r, i) => {
    console.log(`\ntransaction ${i + 1} of ${job.txs.length}: ${r.ok ? 'OK' : 'REFUSED'}`);
    for (const l of r.lines) console.log(`  ${l}`);
    for (const p of r.problems) console.log(`  PROBLEM: ${p}`);
    if (!r.ok) bad++;
  });
  console.log(bad ? `\n${bad} problem(s): do not sign` : '\nevery transaction is what the rules at this commit allow');
  return bad ? 1 : 0;
}

if (isEntry(import.meta.url)) main().then(code => { process.exitCode = code; }).catch(e => { console.error('ERROR:', e.message); process.exitCode = 2; });
