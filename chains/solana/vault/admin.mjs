// The transactions that set a vault up and change it. Pure builders: they
// return instruction groups, one group per transaction, and sign nothing.
// vault.mjs sends the bot's; the owners' go to the signing page.
import { createRequire } from 'module';
import * as sq from './squads.mjs';
import SOLANA from '../solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, AddressLookupTableProgram, SystemProgram } = require('@solana/web3.js');
const { getAssociatedTokenAddressSync, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID } = require('@solana/spl-token');

const MEMO_PROGRAM = new PublicKey(SOLANA.programs.memo);
// Addresses one extend instruction carries and still fits a transaction.
const EXTEND_CHUNK = 24;

// Connect: a new smart account owned by `owners`, with each venue's policies
// for `policyKey`, and the bot nowhere among the owners at the end.
// `venues` = [compile(...)], one per venue the owners approve.
//
// The bot creates the account as a temporary owner (so the owners sign
// nothing), adds one policy per transaction (a policy fills most of one), and
// removes itself in the last. Until then the vault is empty: owners deposit
// only after `vault.mjs status` shows the bot gone. Policy seeds run on
// across venues in the order given; `byVenue` names each venue's policies.
export function connectPlan({ creator, treasury, seed, owners, threshold, policyKey, venues, memo = null }) {
  if (!owners.length) throw new Error('a vault needs at least one owner');
  if (threshold < 1 || threshold > owners.length) throw new Error(`threshold ${threshold} with ${owners.length} owners`);
  if (owners.some(o => new PublicKey(o).equals(creator))) throw new Error('the bot\'s key cannot be an owner');
  if (owners.some(o => new PublicKey(o).equals(policyKey))) throw new Error('the policy key cannot be an owner');
  if (!venues.length) throw new Error('a vault needs at least one venue');
  if (new Set(venues.map(v => v.venue)).size !== venues.length) throw new Error('a venue is listed twice');
  // The bot alone reaches the threshold while it sets up; the real threshold
  // is set in the last transaction, after the bot has left.
  const { settings, ix: create } = sq.createSmartAccountIx({ creator, treasury, seed, threshold: 1, memo,
    signers: [...owners.map(k => ({ key: new PublicKey(k) })), { key: creator }] });
  const all = venues.flatMap(v => v.policies);
  const policies = all.map((_, i) => sq.policyPda(settings, i + 1));
  const byVenue = {};
  let n = 0;
  for (const v of venues) { byVenue[v.venue] = policies.slice(n, n + v.policies.length); n += v.policies.length; }
  const sync = (actions, extra) => sq.settingsSyncIx({ settings, signers: [creator], rentPayer: creator, actions, extra });
  const txs = [[create]];
  all.forEach((p, i) => txs.push([sync(
    [{ kind: 'policyCreate', seed: i + 1, policy: p.policy, signers: [{ key: policyKey }] }], [policies[i]])]));
  const leave = [{ kind: 'removeSigner', key: creator }];
  if (threshold > 1) leave.push({ kind: 'changeThreshold', threshold });
  txs.push([sync(leave, [])]);
  return { settings, vault: sq.vaultPda(settings), policies, byVenue, txs };
}

// Which steps of a connectPlan already landed (their indexes in plan.txs),
// read from the chain: the vault's settings data (null when not created) and
// whether each policy account exists. A resumed connect sends only the rest
// (2026-10-11: the first connect's create landed but its confirmation failed,
// and a second try would have made another vault). Pure.
export function connectDone(settingsData, policyExists, { owners, creator }) {
  if (!settingsData) {
    if (policyExists.some(Boolean)) throw new Error('policies exist for a vault that does not');
    return new Set();
  }
  const s = sq.decodeSettings(settingsData);
  const have = new Set(s.signers.map(x => x.key.toBase58()));
  const missing = owners.map(k => new PublicKey(k).toBase58()).filter(k => !have.has(k));
  if (missing.length) throw new Error(`the vault at this seed is not this request's: ${missing.join(', ')} is not an owner`);
  const done = new Set([0]);
  policyExists.forEach((e, i) => { if (e) done.add(i + 1); });
  if (!have.has(new PublicKey(creator).toBase58())) {
    if (policyExists.some(e => !e)) throw new Error('the bot left the vault before every policy was created; only the owners can add them');
    done.add(policyExists.length + 1);
  }
  return done;
}

// What a connected vault must look like before anyone deposits: these owners
// at this threshold, the bot's keys not among them. Returns the problems.
export function connectProblems(settingsData, { owners, threshold, botKeys }) {
  const s = sq.decodeSettings(settingsData);
  const have = s.signers.map(x => x.key.toBase58()).sort();
  const want = owners.map(k => new PublicKey(k).toBase58()).sort();
  const problems = [];
  if (JSON.stringify(have) !== JSON.stringify(want)) problems.push(`owners are ${have.join(', ')}, expected ${want.join(', ')}`);
  if (s.threshold !== threshold) problems.push(`threshold is ${s.threshold}, expected ${threshold}`);
  for (const k of botKeys) if (have.includes(new PublicKey(k).toBase58())) problems.push(`the bot key ${k} is an owner`);
  if (!s.settingsAuthority.equals(PublicKey.default)) problems.push(`settings authority ${s.settingsAuthority.toBase58()} can change everything`);
  if (s.timeLock !== 0) problems.push(`time lock ${s.timeLock}s: the policy cannot run synchronously`);
  return problems;
}

// The owners' transactions that make a vault's policies what the bot now
// compiles, one per policy: an existing one is updated (its rules, and
// `signer` as its only signer), a missing one is created at the vault's next
// policy seed. A venue added to an existing vault is staged as policies at
// those seeds (stagedPolicies) and created here: one vault per wallet, never
// a second one. `venues` = [{venue, compiled, addresses, infos}], infos
// being the accounts at `addresses` (null: missing). `create: false` (a
// revoke) leaves a missing policy missing.
//
// In seed order, whatever has landed: the signing page asks for each
// transaction by its index, rebuilt from the chain each time, so the list
// must not shift as the owner signs (a policy created by an earlier one is
// updated at its own place; `current` says it already holds these rules).
// A policy at a new venue's seed waits for the ones before it. Until every
// policy of a venue is in place the bot does not act on it, and a move to it
// waits (account.mjs verifyPolicy, lp/board.py move_waits). `owner` signs
// and pays the fees and the rent. Pure.
export function approvePlan({ settings, settingsData, owner, signer, venues, create = true }) {
  settings = new PublicKey(settings);
  let next = (sq.decodeSettings(settingsData).policySeed ?? 0n) + 1n;
  const steps = [];
  for (const v of venues) {
    if (v.addresses.length !== v.compiled.policies.length) {
      throw new Error(`the ${v.venue} rules compile to ${v.compiled.policies.length} policies; the config lists ${v.addresses.length}`);
    }
    v.compiled.policies.forEach(({ policy }, i) => {
      const address = new PublicKey(v.addresses[i]), info = v.infos[i];
      if (!info) {
        if (!create) return;
        const want = sq.policyPda(settings, next);
        if (!want.equals(address)) {
          throw new Error(`${v.venue} policy ${address.toBase58()} is not the vault's next policy (seed ${next}: ${want.toBase58()})`);
        }
        steps.push({ venue: v.venue, address, seed: next, kind: 'create', current: false,
          action: { kind: 'policyCreate', seed: next, policy, signers: [{ key: signer }] } });
        next += 1n;
        return;
      }
      if (!info.owner.equals(sq.PROGRAM_ID)) throw new Error(`${address.toBase58()} is not a Squads account`);
      const head = sq.decodePolicyHead(info.data);
      if (!head.settings.equals(settings)) throw new Error(`policy ${address.toBase58()} belongs to another vault`);
      const current = head.signers.length === 1 && head.signers[0].key.equals(signer) && sq.policyStateIs(info.data, policy);
      steps.push({ venue: v.venue, address, seed: head.seed, kind: 'update', current,
        action: { kind: 'policyUpdate', address, policy, signers: [{ key: signer }] } });
    });
  }
  return steps.sort((a, b) => Number(a.seed - b.seed)).map(st => ({ ...st,
    ixs: [sq.settingsSyncIx({ settings, signers: [owner], rentPayer: owner, extra: [st.address], actions: [st.action] })] }));
}

// The addresses `count` policies of a venue get when the owners approve
// them on a vault whose last policy seed is `policySeed`, after `before`
// other staged ones. Pure.
export function stagedPolicies(settings, policySeed, count, before = 0) {
  return Array.from({ length: count }, (_, i) => sq.policyPda(new PublicKey(settings), BigInt(policySeed ?? 0n) + BigInt(before + i + 1)));
}

// The accounts a vault's transactions on these pools share: everything but
// signers and per-position accounts. `venues` are entries of venues.mjs,
// `pools` = [{accounts, mints}] from their poolAccounts. Pure; the order is
// stable so a table only ever grows.
export function staticAccounts({ vault, policies, venues, pools }) {
  const out = [sq.PROGRAM_ID, ...policies, vault, ...venues.flatMap(v => v.shared()),
    TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID, SystemProgram.programId, MEMO_PROGRAM];
  for (const p of pools) {
    out.push(...p.accounts);
    for (const m of p.mints) out.push(getAssociatedTokenAddressSync(m, vault, true));
  }
  const seen = new Set();
  return out.map(k => new PublicKey(k)).filter(k => !seen.has(k.toBase58()) && seen.add(k.toBase58()));
}

// Create the vault's lookup table (when `table` is null) and add the
// `addresses` it lacks. The payer is its authority; one group per transaction.
export function lookupPlan({ payer, recentSlot, table = null, have = [], addresses }) {
  const known = new Set(have.map(k => new PublicKey(k).toBase58()));
  const missing = addresses.filter(k => !known.has(k.toBase58()));
  if (known.size + missing.length > 256) throw new Error(`a lookup table holds 256 addresses; this one would need ${known.size + missing.length}`);
  const txs = [];
  let address = table && new PublicKey(table);
  if (!address) {
    const [create, created] = AddressLookupTableProgram.createLookupTable({ authority: payer, payer, recentSlot });
    address = created;
    txs.push([create]);
  }
  for (let i = 0; i < missing.length; i += EXTEND_CHUNK) {
    const ix = AddressLookupTableProgram.extendLookupTable({ lookupTable: address, authority: payer, payer, addresses: missing.slice(i, i + EXTEND_CHUNK) });
    if (i === 0 && txs.length) txs[0].push(ix); else txs.push([ix]);
  }
  return { table: address, txs };
}
