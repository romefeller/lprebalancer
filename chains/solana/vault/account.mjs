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

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, Transaction, TransactionMessage, VersionedTransaction } = require('@solana/web3.js');

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
export async function actingAccount({ connection, payer, venue, env = process.env }) {
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
  const compiled = compile(venue, { vault: cfg.vault, payer: payer.publicKey, pools: cfg.pools, payouts: cfg.payouts });
  await verifyPolicy(connection, cfg, policyKey.publicKey, compiled);
  const tables = [];
  if (cfg.lookup) {
    const t = (await connection.getAddressLookupTable(cfg.lookup)).value;
    if (!t) throw new Error(`vault lookup table ${cfg.lookup.toBase58()} does not exist`);
    tables.push(t);
  }
  const wrapOf = (ixs) => wrap(ixs, { ...cfg, policyKey: policyKey.publicKey }, compiled);
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

export async function verifyPolicy(connection, cfg, policyKey, compiled) {
  if (cfg.policies.length !== compiled.policies.length) {
    throw new PolicyMismatch(`the ${compiled.venue} rules compile to ${compiled.policies.length} policies; `
      + `the config lists ${cfg.policies.length}`);
  }
  const infos = await connection.getMultipleAccountsInfo(cfg.policies, 'confirmed');
  infos.forEach((info, i) => {
    const at = cfg.policies[i].toBase58();
    if (!info) throw new PolicyMismatch(`vault policy ${at} does not exist (removed by the owners?)`);
    if (!info.owner.equals(sq.PROGRAM_ID)) throw new PolicyMismatch(`${at} is not a Squads account`);
    const head = sq.decodePolicyHead(info.data);
    if (!head.settings.equals(cfg.settings)) throw new PolicyMismatch(`policy ${at} belongs to another vault`);
    if (!head.signers.some(s => s.key.equals(policyKey))) {
      throw new PolicyMismatch(`policy ${at} no longer lists the bot's policy key: the owners revoked the bot`);
    }
    if (!sq.policyStateIs(info.data, compiled.policies[i].policy)) {
      throw new PolicyMismatch(`policy ${at} differs from the ${compiled.venue} rules and approved pools in the config; `
        + 'the owners must approve the change (vault.mjs policy)');
    }
  });
}

// The instructions as the transaction runs them: each vault run wrapped in
// one call to its policy, everything else as built. Pure.
export function wrap(ixs, cfg, compiled) {
  return plan(ixs, cfg.vault, compiled).flatMap(step => step.policy === null ? step.ixs
    : [sq.policyExecuteIx({ policy: cfg.policies[step.policy], signer: cfg.policyKey, vault: cfg.vault, ixs: step.ixs, indices: step.indices })]);
}
