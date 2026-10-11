// A vault on a fork, connected by the operator's tool as in production
// (chains/solana/vault/cli.mjs connect --execute). Shared by the vault tests.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'module';
import { Fork } from './_svm.mjs';
import * as sq from '../chains/solana/vault/squads.mjs';
import { connect } from '../chains/solana/vault/cli.mjs';
import { venue as venueOf } from '../chains/solana/vault/venues.mjs';

const require = createRequire(import.meta.url);
const { Keypair } = require('@solana/web3.js');

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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vault-cli-'));
  const payer = Keypair.generate(), policyKey = Keypair.generate(), owner = fork.wallet(1), profit = Keypair.generate();
  fork.fund(payer.publicKey, 1);
  fs.writeFileSync(path.join(dir, 'payer.json'), JSON.stringify([...payer.secretKey]));
  fs.writeFileSync(path.join(dir, 'policy.json'), JSON.stringify([...policyKey.secretKey]));
  process.env.WALLET_SECRET_PATH = path.join(dir, 'payer.json');
  process.env.LPBOT_VAULT_POLICY_KEY = path.join(dir, 'policy.json');
  const connection = fork.connection();
  const log = console.log; console.log = () => {};
  let out;
  try {
    out = await connect({ owners: owner.publicKey.toBase58(), venue: venues.join(','), pools: pools.join(','),
      payout: profit.publicKey.toBase58(), execute: true }, { connection });
  } finally { console.log = log; }
  fork.nextSlot();                                  // the lookup table's addresses become usable
  const envFor = (v) => ({ LPBOT_VAULT_SETTINGS: out.settings, LPBOT_VAULT_POLICIES: out.venues[v].policies.join(','),
    LPBOT_VAULT_POLICY_KEY: process.env.LPBOT_VAULT_POLICY_KEY, LPBOT_VAULT_POOLS: out.venues[v].pools.join(','),
    LPBOT_VAULT_PAYOUTS: out.payouts.join(','), LPBOT_VAULT_LOOKUP: out.lookup, LPBOT_VAULT_VENUE: v });
  return { fork, connection, out, env: envFor(venues[0]), envFor, owner, payer, policyKey, profit };
}
