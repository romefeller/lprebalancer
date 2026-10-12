// Whose money a signer moves, and how its transactions are signed.
//
// A signer used to own the money it moved: one keypair paid the fees and held
// the tokens and positions. A vault splits that. The vault (a Squads smart
// account) owns the tokens and positions; the payer key pays fees; the policy
// key signs the policy that lets the bot act. A venue signer asks this module
// for the owner to build instructions for, and hands back what the SDK built:
//
//   const acting = await actingAccount({ connection, payer, venue });
//   ...sdk.build({ owner: acting.owner, payer: acting.payer.publicKey })...
//   const tx = acting.transaction(ixs, blockhash, [positionKeypair]);   // signed
//
// Without a vault configured, owner is the payer and transaction() builds the
// same legacy transaction a signer built before, so a signer has one code
// path for both. With a vault it is a v0 transaction through the vault's
// lookup table: wrapping adds accounts, and the table keeps it under 1232 bytes.
//
// Configuration (set per profile by the Python side from rebalancer.wallets):
//   LPBOT_VAULT_SETTINGS     the vault's settings address. Unset: no vault.
//   LPBOT_VAULT_POLICIES     the policy addresses for this venue, in compile order
//   LPBOT_VAULT_POLICY_KEY   path to the policy key (JSON array or base58)
//   LPBOT_VAULT_POOLS        the pools the owners approved, comma-separated
//   LPBOT_VAULT_PAYOUTS      payout token accounts, comma-separated (may be empty)
//   LPBOT_VAULT_LOOKUP       the vault's address lookup table (may be empty)
import { createRequire } from 'module';
import fs from 'node:fs/promises';
import * as sq from './squads.mjs';
import { compile, plan } from './policy.mjs';
import SOLANA from '../solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { ComputeBudgetProgram, Keypair, PublicKey, Transaction, TransactionInstruction, TransactionMessage, VersionedTransaction } = require('@solana/web3.js');

const list = (v) => (v ?? '').split(',').map(s => s.trim()).filter(Boolean);

// The vault part of the environment, or null. Pure; throws on a half-set
// configuration, since a vault without its policy cannot act at all.
export function vaultConfig(env = process.env) {
  if (!env.LPBOT_VAULT_SETTINGS) return null;
  for (const k of ['LPBOT_VAULT_POLICIES', 'LPBOT_VAULT_POLICY_KEY', 'LPBOT_VAULT_POOLS']) {
    if (!env[k]) throw new Error(`LPBOT_VAULT_SETTINGS is set but ${k} is not`);
  }
  const settings = new PublicKey(env.LPBOT_VAULT_SETTINGS);
  return {
    settings, vault: sq.vaultPda(settings), policies: list(env.LPBOT_VAULT_POLICIES).map(k => new PublicKey(k)),
    policyKeyPath: env.LPBOT_VAULT_POLICY_KEY,
    pools: list(env.LPBOT_VAULT_POOLS).map(k => new PublicKey(k)),
    payouts: list(env.LPBOT_VAULT_PAYOUTS).map(k => new PublicKey(k)),
    lookup: env.LPBOT_VAULT_LOOKUP ? new PublicKey(env.LPBOT_VAULT_LOOKUP) : null,
  };
}

export async function readKeypair(file) {
  const raw = (await fs.readFile(file, 'utf8')).trim();
  if (raw.startsWith('[')) return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(raw)));
  const bs58 = (await import('bs58')).default;
  return Keypair.fromSecretKey(bs58.decode(raw));
}

// The account a venue signer acts for. Without a vault: the payer itself.
// With one: the vault, after checking that the policy on chain is exactly the
// one these rules compile to, so the bot never signs against a policy it did
// not expect (an owner's edit, a revoke, a wrong address in the config).
// `compiled`: what the policies hold, when not this code's rules (a test's
// vault made by an older release).
export async function actingAccount({ connection, payer, venue, env = process.env, compiled: given = null }) {
  const cfg = vaultConfig(env);
  if (!cfg) {
    return { mode: 'key', owner: payer.publicKey, payer, wrap: (ixs) => ixs,
      transaction(ixs, blockhash, extra = []) {
        const tx = new Transaction().add(...ixs);
        tx.feePayer = payer.publicKey;
        tx.recentBlockhash = blockhash;
        tx.sign(payer, ...needed(tx.instructions, extra));
        return tx;
      } };
  }
  const policyKey = await readKeypair(cfg.policyKeyPath);
  const compiled = given ?? compile(venue, { vault: cfg.vault, payer: payer.publicKey, pools: cfg.pools, payouts: cfg.payouts });
  await verifyPolicy(connection, cfg, policyKey.publicKey, compiled);
  const tables = [];
  if (cfg.lookup) {
    const t = (await connection.getAddressLookupTable(cfg.lookup)).value;
    if (!t) throw new Error(`vault lookup table ${cfg.lookup.toBase58()} does not exist`);
    tables.push(t);
  }
  const wrapOf = (ixs) => wrap(ixs, { ...cfg, policyKey: policyKey.publicKey, payer: payer.publicKey }, compiled);
  return {
    mode: 'vault', owner: cfg.vault, payer, vault: cfg.vault, settings: cfg.settings, policies: cfg.policies, compiled,
    wrap: wrapOf,
    transaction(ixs, blockhash, extra = []) {
      const wrapped = wrapOf(ixs);
      const message = new TransactionMessage({ payerKey: payer.publicKey, recentBlockhash: blockhash, instructions: wrapped })
        .compileToV0Message(tables);
      const tx = new VersionedTransaction(message);
      tx.sign([payer, ...needed(wrapped, [policyKey, ...extra])]);
      return tx;
    },
  };
}

// The keypairs among `candidates` that some instruction asks to sign.
function needed(ixs, candidates) {
  return candidates.filter(k => ixs.some(ix => ix.keys.some(m => m.isSigner && m.pubkey.equals(k.publicKey))));
}

export class PolicyMismatch extends Error {}

// What a PolicyMismatch says when the owners can end it with one signature
// ("Approve bot rules"): the rules or the pools changed, a policy staged for
// a new venue is not created yet, the bot was revoked. The loop holds on it
// without counting failures (lp/loop.py); every other mismatch is a wrong
// config. The text is matched there.
export const AWAITS_OWNERS = 'vault awaits the owners\' approval';
const awaiting = (why) => new PolicyMismatch(`${AWAITS_OWNERS}: ${why}`);

export async function verifyPolicy(connection, cfg, policyKey, compiled) {
  if (cfg.policies.length !== compiled.policies.length) {
    throw awaiting(`the ${compiled.venue} rules compile to ${compiled.policies.length} policies; `
      + `the config lists ${cfg.policies.length}`);
  }
  const infos = await connection.getMultipleAccountsInfo(cfg.policies, 'confirmed');
  infos.forEach((info, i) => {
    const at = cfg.policies[i].toBase58();
    if (!info) throw awaiting(`vault policy ${at} does not exist (not approved yet, or removed by the owners)`);
    if (!info.owner.equals(sq.PROGRAM_ID)) throw new PolicyMismatch(`${at} is not a Squads account`);
    const head = sq.decodePolicyHead(info.data);
    if (!head.settings.equals(cfg.settings)) throw new PolicyMismatch(`policy ${at} belongs to another vault`);
    if (!head.signers.some(s => s.key.equals(policyKey))) {
      throw awaiting(`policy ${at} no longer lists the bot's policy key: the owners revoked the bot`);
    }
    if (!sq.policyStateIs(info.data, compiled.policies[i].policy)) {
      throw awaiting(`policy ${at} differs from the ${compiled.venue} rules and approved pools in the config; `
        + 'the owners must approve the change (Approve bot rules)');
    }
  });
}

// The vault pays for its own token accounts. SDKs fund an owner's associated
// token accounts from the payer they are given, but the vault gets the rent
// back when it closes one (a WSOL account unwrapped into the vault): each
// cycle would move that rent from the bot's payer key into the vault. Funded
// by the vault, it goes round within the vault. The create then needs the
// vault's signature, so it runs through the policy's rule for it. Pure.
export function vaultFundsItsAccounts(ixs, vault, payer) {
  return ixs.map(ix => {
    const create = ix.programId.toBase58() === SOLANA.programs.associated_token && (ix.data.length === 0 || ix.data[0] <= 1);
    if (!create || !ix.keys[2]?.pubkey.equals(vault) || !ix.keys[0]?.pubkey.equals(payer)) return ix;
    const keys = ix.keys.map((k, i) => i === 0 ? { pubkey: vault, isSigner: true, isWritable: true } : k);
    return new TransactionInstruction({ programId: ix.programId, keys, data: Buffer.from([1]) });  // idempotent
  });
}

// The instructions as the transaction runs them: each vault run wrapped in
// one call to its policy, everything else as built. Pure.
export function wrap(ixs, cfg, compiled) {
  ixs = vaultFundsItsAccounts(ixs, cfg.vault, cfg.payer);
  const steps = plan(ixs, cfg.vault, compiled);
  const calls = steps.filter(step => step.policy !== null).length;
  return withVaultBudget(steps.flatMap(step => step.policy === null ? step.ixs
    : [sq.policyExecuteIx({ policy: cfg.policies[step.policy], signer: cfg.policyKey, vault: cfg.vault, ixs: step.ixs, indices: step.indices })]), calls);
}

// What one policy call costs on top of the instructions it carries (account
// checks, the CPI). An SDK sizes its compute limit by simulating its bare
// instructions; wrapped, they need this much more (2026-10-11: a DLMM swap
// sized at ~100k ran out inside the vault at 117,644).
export const VAULT_CALL_UNITS = 150_000;
const MAX_UNITS = 1_400_000;
const SET_LIMIT = 2;                                  // ComputeBudget SetComputeUnitLimit

// `ixs` with every compute-unit limit raised by VAULT_CALL_UNITS per policy
// call, at most the transaction maximum. No limit set: the runtime default
// already scales with the instruction count. Pure.
export function withVaultBudget(ixs, calls) {
  if (!calls) return ixs;
  return ixs.map(ix => {
    if (!ix.programId.equals(ComputeBudgetProgram.programId) || ix.data[0] !== SET_LIMIT) return ix;
    const units = Math.min(Buffer.from(ix.data).readUInt32LE(1) + calls * VAULT_CALL_UNITS, MAX_UNITS);
    return ComputeBudgetProgram.setComputeUnitLimit({ units });
  });
}
