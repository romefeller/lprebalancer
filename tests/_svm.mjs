// A local fork of Solana mainnet for the vault tests: the real deployed
// programs and the real pool accounts, run in-process by LiteSVM. Nothing is
// sent anywhere; a transaction that passes here was checked by the same
// program binaries that run on mainnet.
//
// Program binaries are read from mainnet once per test run into TMPDIR
// (tests/run.sh removes it at the end). Accounts are cloned on demand.
import { createRequire } from 'module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { LiteSvm, Account } = require('litesvm/dist/internal');
const { Connection, PublicKey, Transaction, Keypair } = require('@solana/web3.js');

const UPGRADEABLE_LOADER = new PublicKey('BPFLoaderUpgradeab1e11111111111111111111111');
export const RPC = process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';

// The executable bytes of a deployed upgradeable program, cached for this run.
async function programBytes(connection, programId) {
  const file = path.join(os.tmpdir(), `svm-${programId.toBase58()}.so`);
  if (fs.existsSync(file)) return fs.readFileSync(file);
  const program = await connection.getAccountInfo(programId);
  if (!program?.owner.equals(UPGRADEABLE_LOADER)) throw new Error(`${programId.toBase58()} is not an upgradeable program`);
  const data = await connection.getAccountInfo(new PublicKey(program.data.subarray(4, 36)));
  const bytes = data.data.subarray(45);                      // after the programdata header
  fs.writeFileSync(file, bytes);
  return bytes;
}

export class Fork {
  constructor(connection = new Connection(RPC, 'confirmed')) {
    this.connection = connection;
    this.svm = new LiteSvm();
    this.svm.setDefaultPrograms();                           // system, SPL Token, Token-2022, ATA, memo
    this.svm.withNativeMints();                              // the WSOL mint
    this.svm.setSigverify(true);
  }

  async program(programId) {
    this.svm.addProgram(new PublicKey(programId).toBytes(), await programBytes(this.connection, new PublicKey(programId)));
  }

  // Copy mainnet accounts as they are now. Missing ones are skipped: a
  // transaction that needs them fails here as it would there.
  async clone(keys) {
    const list = keys.map(k => new PublicKey(k));
    for (let i = 0; i < list.length; i += 100) {
      const infos = await this.connection.getMultipleAccountsInfo(list.slice(i, i + 100));
      infos.forEach((a, j) => { if (a && !a.executable) this.set(list[i + j], a); });
    }
  }

  set(key, { lamports, data, owner, executable = false }) {
    this.svm.setAccount(new PublicKey(key).toBytes(),
      new Account(BigInt(lamports), Uint8Array.from(data ?? []), new PublicKey(owner).toBytes(), executable, 0n));
  }

  get(key) {
    const a = this.svm.getAccount(new PublicKey(key).toBytes());
    return a && { lamports: a.lamports(), data: Buffer.from(a.data()), owner: new PublicKey(a.owner()) };
  }

  balance(key) { return Number(this.svm.getBalance(new PublicKey(key).toBytes()) ?? 0n); }

  fund(key, sol) { this.svm.airdrop(new PublicKey(key).toBytes(), BigInt(Math.round(sol * 1e9))); }

  wallet(sol = 1) { const k = Keypair.generate(); this.fund(k.publicKey, sol); return k; }

  // Sign and run. Returns {ok, logs, error, units}; never throws on a
  // program error, so a test can assert on the refusal it expects.
  send(ixs, payer, extra = []) {
    const tx = new Transaction().add(...ixs);
    tx.feePayer = payer.publicKey;
    tx.recentBlockhash = this.svm.latestBlockhash();
    tx.sign(payer, ...extra);
    const r = this.svm.sendLegacyTransaction(tx.serialize());
    this.svm.expireBlockhash();                              // the next send gets a fresh blockhash
    const failed = typeof r.err === 'function';
    const meta = failed ? r.meta() : r;
    return { ok: !failed, logs: meta.logs(), units: Number(meta.computeUnitsConsumed()),
      error: failed ? (meta.logs().find(l => /Error|failed/.test(l)) ?? r.toString()) : null, size: tx.serialize().length };
  }
}
