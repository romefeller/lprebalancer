// From the rules (vault/rules.json) to the policies on chain, and from an
// instruction list a venue SDK built to the transaction the vault runs.
//
// One compilation serves both directions: its constraints are what the
// program enforces, and its index is how the bot names, for each instruction,
// the policy and constraint it claims to satisfy. The program checks the
// claim; the bot checks it first only to fail with a readable message.
//
// A venue's rules usually do not fit one policy: a policy is created by one
// settings transaction, and a transaction holds 1232 bytes. compile() packs
// the rules, in file order, into as few policies as stay under POLICY_BYTES.
import { createRequire } from 'module';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { encodeProgramInteraction } from './squads.mjs';

const require = createRequire(import.meta.url);
const { PublicKey, ComputeBudgetProgram } = require('@solana/web3.js');

const TOKEN_PROGRAM = new PublicKey('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA');
// Byte 32 of an SPL token account is its owner (spl-token state.rs Account).
const TOKEN_OWNER_OFFSET = 32;
const MAX_CONSTRAINTS = 20;
// Encoded policy bytes one settings transaction can carry next to its own
// accounts and signature. tests/test_vault_policy.mjs builds the real
// transaction for every packed policy and checks it fits.
export const POLICY_BYTES = 760;

export const RULES = JSON.parse(fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'rules.json'), 'utf8'));

export class NotAllowed extends Error {}

// The discriminator of a rule as a data constraint at offset 0, and as the
// bytes an instruction must start with.
function discriminator(d) {
  if (typeof d === 'string') {
    const bytes = Buffer.from(d, 'hex');
    if (bytes.length !== 8 || bytes.toString('hex') !== d) throw new Error(`discriminator ${d} is not 8 bytes of hex`);
    return { constraint: { offset: 0, type: 'bytes', value: bytes, op: 'eq' }, prefix: bytes };
  }
  const width = { u8: 1, u32: 4 }[d.type];
  if (!width) throw new Error(`discriminator type ${d.type} is not u8 or u32`);
  const prefix = Buffer.alloc(width);
  width === 1 ? prefix.writeUInt8(d.value) : prefix.writeUInt32LE(d.value);
  return { constraint: { offset: 0, type: d.type, value: d.value, op: 'eq' }, prefix };
}

// What a role pins, for one vault. `ctx` = {vault, payer, pools, payouts}.
function pin(role, index, programId, ctx) {
  const keys = (list, what) => {
    if (!list?.length) throw new Error(`role ${role} needs at least one ${what}`);
    return { index, keys: list.map(k => new PublicKey(k)) };
  };
  const ownedByVault = [{ offset: TOKEN_OWNER_OFFSET, type: 'bytes', value: new PublicKey(ctx.vault).toBuffer(), op: 'eq' }];
  switch (role) {
    case 'vault': return keys([ctx.vault], 'vault');
    case 'vaultOrPayer': return keys([ctx.vault, ctx.payer], 'vault and payer');
    case 'pool': return keys(ctx.pools, 'approved pool');
    case 'payout': return keys(ctx.payouts, 'payout token account');
    case 'none': return keys([programId], 'program');
    case 'vaultToken': return { index, data: ownedByVault };
    case 'vaultWsol': return { index, owner: TOKEN_PROGRAM, data: ownedByVault };
    default: throw new Error(`unknown role ${role}`);
  }
}

// The rules a vault's policies for `venue` hold: the venue's own, the shared
// ones, and payouts when the vault has payout accounts.
export function rulesFor(venue, rules = RULES, withPayout = true) {
  const v = rules.venues[venue];
  if (!v) throw new Error(`no vault rules for venue ${venue} (have: ${Object.keys(rules.venues).join(', ')})`);
  return [
    ...v.instructions.map(i => ({ ...i, program: v.program })),
    ...rules.common,
    ...(withPayout ? rules.payout : []),
  ];
}

function constraintOf(rule, ctx) {
  const programId = new PublicKey(rule.program);
  const d = discriminator(rule.discriminator);
  const seen = new Set();
  const accounts = rule.pins.map(p => {
    if (seen.has(p.index)) throw new Error(`${rule.name}: account ${p.index} pinned twice`);
    seen.add(p.index);
    return pin(p.role, p.index, programId, ctx);
  });
  return { constraint: { programId, accounts, data: [d.constraint] },
    entry: { name: rule.name, programId, prefix: d.prefix, accounts, opens: Boolean(rule.opens) } };
}

const size = (constraints) => encodeProgramInteraction({ accountIndex: 0, constraints }).length;

// Compile `venue`'s rules for one vault: {venue, policies: [{policy, index}]},
// the policies in the order they are created.
export function compile(venue, ctx, rules = RULES) {
  const policies = [];
  let current = null;
  for (const rule of rulesFor(venue, rules, Boolean(ctx.payouts?.length))) {
    const { constraint, entry } = constraintOf(rule, ctx);
    if (size([constraint]) > POLICY_BYTES) throw new Error(`${rule.name}: one rule alone is over ${POLICY_BYTES} bytes`);
    if (!current || current.policy.constraints.length >= MAX_CONSTRAINTS
        || size([...current.policy.constraints, constraint]) > POLICY_BYTES) {
      current = { policy: { accountIndex: 0, constraints: [] }, index: [] };
      policies.push(current);
    }
    current.policy.constraints.push(constraint);
    current.index.push(entry);
  }
  return { venue, policies };
}

// True when `ix` needs the vault's signature: it must run through a policy.
export function needsVault(ix, vault) {
  return ix.keys.some(k => k.isSigner && k.pubkey.equals(vault));
}

// The policy and constraint `ix` satisfies, checked as the program will check
// it as far as keys go (token-owner fields are left to the program).
export function match(ix, compiled) {
  for (const [p, { index }] of compiled.policies.entries()) {
    const c = index.findIndex(e => e.programId.equals(ix.programId) && ix.data.subarray(0, e.prefix.length).equals(e.prefix));
    if (c < 0) continue;
    const e = index[c];
    for (const a of e.accounts) {
      const key = ix.keys[a.index]?.pubkey;
      if (!key) throw new NotAllowed(`${e.name}: account ${a.index} is missing`);
      if (a.keys && !a.keys.some(k => k.equals(key))) {
        throw new NotAllowed(`${e.name}: account ${a.index} is ${key.toBase58()}, which the vault policy does not allow there`);
      }
    }
    return { policy: p, constraint: c, opens: e.opens };
  }
  throw new NotAllowed(`instruction ${ix.data.subarray(0, 8).toString('hex')} of program ${ix.programId.toBase58()} `
    + `is not in the ${compiled.venue} vault policy`);
}

// A transaction's instructions as steps, in order: a run of instructions that
// need the vault under one policy goes through it in one call; the rest run as
// built. Compute-budget instructions always stay outside: they set the limits
// of the whole transaction, so they belong at its top level. An instruction
// that opens accounts closes its call (rules.json "opens").
export function plan(ixs, vault, compiled) {
  const steps = [];
  for (const ix of ixs) {
    if (ix.programId.equals(ComputeBudgetProgram.programId) || !needsVault(ix, vault)) {
      steps.push({ policy: null, ixs: [ix] });
      continue;
    }
    const m = match(ix, compiled);
    const last = steps.at(-1);
    if (last?.policy === m.policy && !last.closed) { last.ixs.push(ix); last.indices.push(m.constraint); }
    else steps.push({ policy: m.policy, ixs: [ix], indices: [m.constraint] });
    steps.at(-1).closed = m.opens;
  }
  return steps;
}
