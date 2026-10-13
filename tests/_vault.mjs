// A vault on a fork, set up as in production: the owner's wallet creates it
// and the bot's policies (chains/solana/vault/cli.mjs setupRequest, the
// owner signing every transaction), then the bot makes its lookup table
// (finishSetup). The bot is never a signer of the vault. Shared by the vault
// tests.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { finishSetup, setupRequest } from '../chains/solana/vault/cli.mjs';
import { packSteps } from '../chains/solana/vault/admin.mjs';
import { venue as venueOf } from '../chains/solana/vault/venues.mjs';

const require = createRequire(import.meta.url);
const { Keypair } = require('@solana/web3.js');

// Folders that hold a test's throwaway keys, removed when the test process
// exits. 2026-10-12: about 600 key files of fork tests run outside run.sh
// (its TMPDIR is removed; os.tmpdir() alone is not) were left in /tmp.
const KEY_DIRS = new Set();
process.on('exit', () => { for (const d of KEY_DIRS) fs.rmSync(d, { recursive: true, force: true }); });

// A new private folder for throwaway keys, removed at process exit.
export function keyDir(prefix = 'vault-keys-') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  KEY_DIRS.add(dir);
  return dir;
}

// The fork's mainnet snapshot is older than mainnet: keep the next smart
// account seeds, their policies and vaults out of the fork, so a set-up
// planned on it finds them free (mainnet made real accounts there since).
export function freeSeeds(fork, n = 8) {
  const next = sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).smartAccountIndex;
  for (let i = 1n; i <= BigInt(n); i++) {
    const settings = sq.settingsPda(next + i);
    fork.absent.add(settings.toBase58());
    fork.absent.add(sq.vaultPda(settings).toBase58());
    for (let p = 1; p <= 8; p++) fork.absent.add(sq.policyPda(settings, p).toBase58());
  }
}

export const DLMM_PROGRAM = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo';
export const POOL = 'BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y';

// `venues` and their `pools` as the operator names them; `env` is the
// signer environment of the first venue, `envFor(venue)` of any.
export async function connectedVault({ venues = ['meteora-dlmm'], pools = [POOL] } = {}) {
  const fork = new Fork();
  await Promise.all([fork.program(sq.PROGRAM_ID), ...venues.map(v => fork.program(venueOf(v).program))]);
  await fork.syncClock();
  await fork.clone([sq.programConfigPda()]);
  await fork.clone([sq.decodeProgramConfig(fork.get(sq.programConfigPda()).data).treasury]);
  freeSeeds(fork);
  const dir = keyDir('vault-cli-');
  const payer = Keypair.generate(), policyKey = Keypair.generate(), owner = fork.wallet(1), profit = Keypair.generate();
  fork.fund(payer.publicKey, 1);
  fs.writeFileSync(path.join(dir, 'payer.json'), JSON.stringify([...payer.secretKey]));
  fs.writeFileSync(path.join(dir, 'policy.json'), JSON.stringify([...policyKey.secretKey]));
  process.env.WALLET_SECRET_PATH = path.join(dir, 'payer.json');
  process.env.LPBOT_VAULT_POLICY_KEY = path.join(dir, 'policy.json');
  const connection = fork.connection();
  const f = { owner: owner.publicKey.toBase58(), venue: venues.join(','), pools: pools.join(','), payout: profit.publicKey.toBase58() };
  const keys = { connection, payer: payer.publicKey, policyKey: policyKey.publicKey };
  const setup = await setupRequest(f, keys);
  for (const ixs of packSteps(setup.steps, owner.publicKey)) {
    const r = await fork.send(ixs, owner);
    if (!r.ok) throw new Error(`the owner's set-up failed:\n${r.logs.join('\n')}`);
  }
  const out = await finishSetup(await setupRequest({ ...f, seed: setup.seed }, keys),
    { connection, payer, policyKey: policyKey.publicKey, log: () => {} });
  fork.nextSlot();                                  // the lookup table's addresses become usable
  const envFor = (v) => ({ LPBOT_VAULT_SETTINGS: out.settings, LPBOT_VAULT_POLICIES: out.venues[v].policies.join(','),
    LPBOT_VAULT_POLICY_KEY: process.env.LPBOT_VAULT_POLICY_KEY, LPBOT_VAULT_POOLS: out.venues[v].pools.join(','),
    LPBOT_VAULT_PAYOUTS: out.payouts.join(','), LPBOT_VAULT_LOOKUP: out.lookup, LPBOT_VAULT_VENUE: v });
  return { fork, connection, out, env: envFor(venues[0]), envFor, owner, payer, policyKey, profit };
}
