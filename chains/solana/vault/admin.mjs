// The transactions that set a vault up and change it. Pure builders: they
// return instructions or instruction groups, one group per transaction, and
// sign nothing. The owners' go to the signing page; the bot sends only its
// own (the lookup table).
import { createRequire } from 'module';
import * as sq from './squads.mjs';
import SOLANA from '../solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, AddressLookupTableProgram, SystemProgram, TransactionMessage } = require('@solana/web3.js');
const { getAssociatedTokenAddressSync, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID } = require('@solana/spl-token');

const MEMO_PROGRAM = new PublicKey(SOLANA.programs.memo);
// Addresses one extend instruction carries and still fits a transaction.
const EXTEND_CHUNK = 24;

// Set-up: the owner's own smart account, the owner its creator, rent payer
// and only signer (threshold 1, no settings authority, no time lock), and
// each venue's policies for the bot's `policyKey`, created in settings
// transactions. The bot signs nothing and pays nothing: it is never a
// signer of the vault (owner order, 2026-10-13). `venues` = [compile(...)],
// one per venue the owner approves. `steps` are the instructions in order:
// the creation, then one policy each; a policy seed runs on across venues in
// the order given (the account's first policy seed is 1), and `byVenue`
// names each venue's policies. The caller packs the steps into transactions
// (packSteps) and adds the deposit after them: a deposit only ever reaches a
// vault whose policies exist.
//
// `setupKey`: a one-time key of the owner's page, a second signer of the new
// account until its policies exist. One policy's creation is 900 to 1,030
// bytes of a transaction message, and a Tangem card signs at most 930 to 964
// (its APDU; tangem-app-android SOLANA_TRANSACTION_SIZE_THRESHOLD_BYTES,
// tangem-app-ios supportedTransactionSize, 2026-10-14): with the key, the
// owner's wallet signs only the creation; the key signs and pays each policy,
// then removes itself (`release`). Without it the owner signs every policy
// (the CLI's set-up). Pure.
export function ownerSetupPlan({ owner, treasury, seed, policyKey, venues, memo = null, setupKey = null }) {
  owner = new PublicKey(owner);
  if (owner.equals(new PublicKey(policyKey))) throw new Error('the bot\'s policy key cannot own the vault');
  const helper = setupKey ? new PublicKey(setupKey) : null;
  if (helper && (helper.equals(owner) || helper.equals(new PublicKey(policyKey)))) throw new Error('the set-up key is the owner\'s or the bot\'s key');
  if (!venues.length) throw new Error('a vault needs at least one venue');
  if (new Set(venues.map(v => v.venue)).size !== venues.length) throw new Error('a venue is listed twice');
  const { settings, ix: create } = sq.createSmartAccountIx({ creator: owner, treasury, seed, threshold: 1, memo,
    signers: [{ key: owner }, ...(helper ? [{ key: helper }] : [])] });
  const all = venues.flatMap(v => v.policies);
  const policies = all.map((_, i) => sq.policyPda(settings, i + 1));
  const byVenue = {};
  let n = 0;
  for (const v of venues) { byVenue[v.venue] = policies.slice(n, n + v.policies.length); n += v.policies.length; }
  const by = helper ?? owner;
  const steps = [create, ...all.map((p, i) => sq.settingsSyncIx({ settings, signers: [by], rentPayer: by, extra: [policies[i]],
    actions: [{ kind: 'policyCreate', seed: i + 1, policy: p.policy, signers: [{ key: policyKey }] }] }))];
  const release = helper ? sq.settingsSyncIx({ settings, signers: [helper], rentPayer: helper, actions: [{ kind: 'removeSigner', key: helper }] }) : null;
  return { settings, vault: sq.vaultPda(settings), policies, byVenue, steps, release };
}

// Which steps of an ownerSetupPlan already landed (their indexes), read from
// the chain: the settings data (null when not created) and whether each
// policy exists. A settings account at the seed that the owner does not
// sign, or that has another signer than the owner and the set-up keys this
// request made (`known`), or a policy beyond the plan's, means another
// account took the seed: null, plan again with a fresh one. Pure.
export function setupDone(settingsData, policyExists, { owner, known = [] }) {
  if (!settingsData) {
    if (policyExists.some(Boolean)) throw new Error('policies exist for a vault that does not');
    return new Set();
  }
  const s = sq.decodeSettings(settingsData);
  const o = new PublicKey(owner), ours = known.map(k => new PublicKey(k));
  if (!s.signers.some(x => x.key.equals(o))) return null;
  if (s.signers.some(x => !x.key.equals(o) && !ours.some(k => k.equals(x.key)))) return null;
  if ((s.policySeed ?? 0n) > BigInt(policyExists.length)) return null;
  const done = new Set([0]);
  policyExists.forEach((e, i) => { if (e) done.add(i + 1); });
  return done;
}

// The signers of a smart account other than `owner`: set-up keys still on
// it. Base58. Pure.
export function otherSigners(settingsData, owner) {
  if (!settingsData) return [];
  const o = new PublicKey(owner);
  return sq.decodeSettings(settingsData).signers.map(x => x.key).filter(k => !k.equals(o)).map(String);
}

// `steps` packed into as few legacy transactions as fit `limit` bytes each
// when `payer` signs alone, order kept: a step that does not fit the
// transaction being filled starts the next. A step is an instruction, or an
// array of instructions that must land together (one transaction). Returns
// the instruction groups. Pure.
export function packSteps(steps, payer, limit = 1232) {
  const fits = (ixs) => {
    try {
      const msg = new TransactionMessage({ payerKey: new PublicKey(payer), recentBlockhash: PublicKey.default.toBase58(), instructions: ixs })
        .compileToLegacyMessage();
      return 1 + 64 * msg.header.numRequiredSignatures + msg.serialize().length <= limit;
    } catch { return false; }
  };
  const groups = [];
  for (const step of steps) {
    const unit = Array.isArray(step) ? step : [step];
    if (!unit.length) continue;
    const last = groups.at(-1);
    if (last && fits([...last, ...unit])) last.push(...unit);
    else {
      if (!fits(unit)) throw new Error('a step alone is over a transaction\'s size');
      groups.push([...unit]);
    }
  }
  return groups;
}

// What a connected vault must look like before anyone deposits: these owners
// at this threshold, the bot's keys not among them. `policies` (a new
// vault): how many policies it has; one at a later seed was made by another
// signer (a set-up key, or whoever created the account at the planned seed)
// and could move the money. Returns the problems.
export function connectProblems(settingsData, { owners, threshold, botKeys, policies = null }) {
  const s = sq.decodeSettings(settingsData);
  const have = s.signers.map(x => x.key.toBase58()).sort();
  const want = owners.map(k => new PublicKey(k).toBase58()).sort();
  const problems = [];
  if (JSON.stringify(have) !== JSON.stringify(want)) problems.push(`owners are ${have.join(', ')}, expected ${want.join(', ')}`);
  if (policies != null && (s.policySeed ?? 0n) > BigInt(policies)) problems.push(`a policy at seed ${s.policySeed}: the bot's rules make ${policies}`);
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
