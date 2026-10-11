// A local fork of Solana mainnet for the vault tests: the real deployed
// programs and the real pool accounts, run in-process by LiteSVM. Nothing is
// sent anywhere; a transaction that passes here was checked by the same
// program binaries that run on mainnet.
//
// Program binaries are read from mainnet once per test run into TMPDIR
// (tests/run.sh removes it at the end), or into LPBOT_SVM_CACHE when set:
// tests/mutate.py shares one copy over all its runs, and one snapshot of the
// accounts too. Accounts are cloned on demand.
import { createRequire } from 'module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { LiteSvm, Account } = require('litesvm/dist/internal');
const { Connection, PublicKey, Transaction, Keypair, AddressLookupTableAccount, VersionedTransaction } = require('@solana/web3.js');
const { AccountLayout, TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID } = require('@solana/spl-token');

const bs58 = require('bs58').default;
// memcmp filter bytes come as base58 text from most SDKs
const bytesOf = (b) => typeof b === 'string' ? Buffer.from(bs58.decode(b)) : Buffer.from(b);
const UPGRADEABLE_LOADER = new PublicKey('BPFLoaderUpgradeab1e11111111111111111111111');
// The bot's own order (config.py): LPBOT_RPC, the keyed Helius endpoint, then
// the public one. A fork test reads hundreds of accounts; the public endpoint
// rate-limits that, so reads are retried below, as are dropped connections.
const KEY = (process.env.KAMINO_RPC_KEY ?? '').trim();
export const RPC = process.env.LPBOT_RPC
  || (/^[A-Za-z0-9-]{16,128}$/.test(KEY) ? `https://mainnet.helius-rpc.com/?api-key=${KEY}` : null)
  || process.env.SOLANA_RPC_URL || 'https://api.mainnet-beta.solana.com';

// A read that stalls is cut off and tried again, never waited on for ever.
const READ_TIMEOUT_MS = 30_000;
const timedFetch = (url, opts) => fetch(url, { ...opts, signal: AbortSignal.timeout(READ_TIMEOUT_MS) });

async function retry(fn, tries = 8) {
  for (let i = 0; ; i++) {
    try { return await fn(); } catch (e) {
      if (i + 1 >= tries || !/429|Too Many|fetch failed|ECONNRESET|ETIMEDOUT|socket hang up|50[234]|TimeoutError|aborted/.test(String(e?.message ?? e))) throw e;
      await new Promise(r => setTimeout(r, 500 * 2 ** i));
    }
  }
}

// The executable bytes of a deployed upgradeable program, cached for this run.
async function programBytes(connection, programId) {
  const file = path.join(process.env.LPBOT_SVM_CACHE || os.tmpdir(), `svm-${programId.toBase58()}.so`);
  if (fs.existsSync(file)) return fs.readFileSync(file);
  const program = await retry(() => connection.getAccountInfo(programId));
  if (!program?.owner.equals(UPGRADEABLE_LOADER)) throw new Error(`${programId.toBase58()} is not an upgradeable program`);
  const data = await retry(() => connection.getAccountInfo(new PublicKey(program.data.subarray(4, 36))));
  const bytes = data.data.subarray(45);                      // after the programdata header
  // written whole or not at all: test files may run in parallel
  const part = `${file}.${process.pid}`;
  fs.writeFileSync(part, bytes);
  fs.renameSync(part, file);
  return bytes;
}

// Mainnet accounts as AccountInfo (null for a key mainnet does not have). With
// LPBOT_SVM_CACHE set, each answer is kept there, so every run of a mutation
// sweep forks the same snapshot and asks the RPC once per key.
async function mainnetAccounts(connection, keys) {
  const dir = process.env.LPBOT_SVM_CACHE;
  const file = (k) => path.join(dir, `acct-${k.toBase58()}.json`);
  const out = new Map();
  if (dir) {
    for (const k of keys) {
      if (!fs.existsSync(file(k))) continue;
      const a = JSON.parse(fs.readFileSync(file(k), 'utf8'));
      out.set(k.toBase58(), a && { lamports: a.lamports, data: Buffer.from(a.data, 'base64'), owner: new PublicKey(a.owner), executable: a.executable });
    }
  }
  const missing = keys.filter(k => !out.has(k.toBase58()));
  for (let i = 0; i < missing.length; i += 100) {
    const got = await retry(() => connection.getMultipleAccountsInfo(missing.slice(i, i + 100)));
    got.forEach((a, j) => {
      const k = missing[i + j];
      out.set(k.toBase58(), a);
      if (!dir) return;
      const part = `${file(k)}.${process.pid}`;          // written whole or not at all
      fs.writeFileSync(part, JSON.stringify(a && { lamports: a.lamports, data: Buffer.from(a.data).toString('base64'), owner: a.owner.toBase58(), executable: a.executable }));
      fs.renameSync(part, file(k));
    });
  }
  return keys.map(k => out.get(k.toBase58()));
}

// One mainnet answer, kept under `name` in LPBOT_SVM_CACHE when set.
async function cached(name, fetchFn) {
  const dir = process.env.LPBOT_SVM_CACHE;
  const file = dir && path.join(dir, `${name}.json`);
  if (file && fs.existsSync(file)) return JSON.parse(fs.readFileSync(file, 'utf8'));
  const value = await retry(fetchFn);
  if (file) {
    const part = `${file}.${process.pid}`;
    fs.writeFileSync(part, JSON.stringify(value));
    fs.renameSync(part, file);
  }
  return value;
}

// Parsed accounts (mints), one cache entry per key; only JSON-parsed data is kept.
async function mainnetParsed(connection, keys) {
  const revive = (a) => a && { ...a, owner: new PublicKey(a.owner) };
  const out = [];
  for (const k of keys.map(x => new PublicKey(x))) {
    out.push(revive(await cached(`parsed-${k.toBase58()}`, async () => {
      const [a] = (await connection.getMultipleParsedAccounts([k])).value;
      return a && { ...a, owner: a.owner.toBase58() };
    })));
  }
  return out;
}

export class Fork {
  constructor(mainnet = new Connection(RPC, { commitment: 'confirmed', fetch: timedFetch })) {
    this.mainnet = mainnet;
    this.svm = new LiteSvm();
    this.svm.setDefaultPrograms();                           // system, SPL Token, Token-2022, ATA, memo
    this.svm.withNativeMints();                              // the WSOL mint
    this.svm.setSigverify(true);
    this.absent = new Set();                                 // keys mainnet does not have
    this.seen = new Set();                                   // every key set, cloned or sent
  }

  // Mainnet's clock, so programs that read time (fees, activation, oracles)
  // see the present rather than slot 0.
  async syncClock() {
    const slot = await cached('slot', () => this.mainnet.getSlot('confirmed'));
    const now = BigInt(Math.floor(Date.now() / 1000));
    const c = this.svm.getClock();
    c.slot = BigInt(slot); c.unixTimestamp = now; c.epochStartTimestamp = now;
    this.svm.setClock(c);
  }

  async program(programId) {
    this.svm.addProgram(new PublicKey(programId).toBytes(), await programBytes(this.mainnet, new PublicKey(programId)));
  }

  // Copy mainnet accounts as they are now. Missing ones are skipped: a
  // transaction that needs them fails here as it would there.
  async clone(keys) {
    const list = keys.map(k => new PublicKey(k));
    const infos = await mainnetAccounts(this.mainnet, list);
    infos.forEach((a, j) => { if (a && !a.executable) this.set(list[j], a); });
  }

  set(key, { lamports, data, owner, executable = false }) {
    this.seen.add(new PublicKey(key).toBase58());
    this.svm.setAccount(new PublicKey(key).toBytes(),
      new Account(BigInt(lamports), Uint8Array.from(data ?? []), new PublicKey(owner).toBytes(), executable, 0n));
  }

  get(key) {
    const a = this.svm.getAccount(new PublicKey(key).toBytes());
    return a && { lamports: a.lamports(), data: Buffer.from(a.data()), owner: new PublicKey(a.owner()) };
  }

  // A Connection for SDKs that reads this fork: an account the fork lacks is
  // copied from mainnet on first read, so an SDK sees mainnet plus every
  // change the tests made. Only the calls the venue SDKs make are here.
  connection() {
    const fork = this, main = this.mainnet;
    const info = (a) => a && { lamports: Number(a.lamports), data: a.data, owner: a.owner, executable: false, rentEpoch: 0 };
    const read = async (keys) => {
      const missing = keys.filter(k => !fork.get(k) && !fork.absent.has(k.toBase58()));
      const got = await mainnetAccounts(main, missing);
      got.forEach((a, j) => { const k = missing[j]; if (a) fork.set(k, a); else fork.absent.add(k.toBase58()); });
      return keys.map(k => info(fork.get(k)));
    };
    const matches = (data, f) => f.dataSize != null ? data.length === f.dataSize
      : Buffer.from(data.subarray(f.memcmp.offset, f.memcmp.offset + bytesOf(f.memcmp.bytes).length)).equals(bytesOf(f.memcmp.bytes));
    return {
      rpcEndpoint: 'fork', commitment: 'confirmed',
      getAccountInfo: async (k) => (await read([new PublicKey(k)]))[0],
      getMultipleAccountsInfo: async (ks) => read(ks.map(k => new PublicKey(k))),
      getAccountInfoAndContext: async (k) => ({ context: { slot: Number(fork.svm.getClock().slot) }, value: (await read([new PublicKey(k)]))[0] }),
      getMultipleAccountsInfoAndContext: async (ks) => ({ context: { slot: Number(fork.svm.getClock().slot) }, value: await read(ks.map(k => new PublicKey(k))) }),
      getBalance: async (k) => fork.balance(k),
      getMinimumBalanceForRentExemption: async (n) => Number(fork.svm.minimumBalanceForRentExemption(BigInt(n))),
      getLatestBlockhash: async () => ({ blockhash: fork.svm.latestBlockhash(), lastValidBlockHeight: 1e12 }),
      // the newest slot the fork's SlotHashes knows, as mainnet's finalized slot is
      getSlot: async () => fork.recentSlot(),
      // What a signer sends: run at once; a failure throws with the program's
      // logs, as a node's preflight would.
      sendRawTransaction: async (raw) => {
        const tx = VersionedTransaction.deserialize(raw);
        const r = await fork.sendSigned(tx);
        if (!r.ok) throw new Error(`fork: transaction failed\n${r.logs.join('\n')}`);
        return bs58.encode(tx.signatures[0]);
      },
      confirmTransaction: async () => ({ value: { err: null } }),
      // a send that returned ran in full: it is confirmed at once
      getSignatureStatuses: async (sigs) => ({ value: sigs.map(() => ({ err: null, confirmationStatus: 'confirmed' })) }),
      getBlockHeight: async () => 0,
      getRecentPrioritizationFees: async () => [],
      getMultipleParsedAccounts: async (ks) => ({ context: { slot: Number(fork.svm.getClock().slot) }, value: await mainnetParsed(main, ks) }),
      getParsedTokenAccountsByOwner: async (owner, { mint }) => ({ value: [...fork.seen].flatMap(k => {
        const a = fork.get(k);
        if (!a || !(a.owner.equals(TOKEN_PROGRAM_ID) || a.owner.equals(TOKEN_2022_PROGRAM_ID)) || a.data.length < AccountLayout.span) return [];
        const t = AccountLayout.decode(a.data.subarray(0, AccountLayout.span));
        if (!t.owner.equals(new PublicKey(owner)) || !t.mint.equals(new PublicKey(mint))) return [];
        return [{ pubkey: new PublicKey(k), account: { data: { parsed: { info: { tokenAmount: { amount: String(t.amount) } } } } } }];
      }) }),
      getAddressLookupTable: async (k) => {
        const [a] = await read([new PublicKey(k)]);
        return { value: a ? new AddressLookupTableAccount({ key: new PublicKey(k), state: AddressLookupTableAccount.deserialize(a.data) }) : null };
      },
      // Over the accounts this fork has seen: LiteSVM's own scan aborted the
      // process once accounts had been closed (litesvm 1.4.1).
      getProgramAccounts: async (program, opts = {}) => [...fork.seen]
        .map(k => ({ pubkey: new PublicKey(k), account: info(fork.get(k)) }))
        .filter(x => x.account && x.account.owner.equals(new PublicKey(program)) && x.account.lamports > 0)
        .filter(x => (opts.filters ?? []).every(f => matches(x.account.data, f))),
      // SDKs simulate only to size compute budgets and fall back to their
      // defaults when that fails. LiteSVM 1.4.1 aborts the whole process on
      // some simulations (crates/node-litesvm/src/lib.rs:308), so the fork
      // declines them; every send still executes in full.
      simulateTransaction: async () => ({ value: { err: 'not simulated on the fork', logs: [], unitsConsumed: 0 } }),
    };
  }

  // An SPL token account with `amount` (raw units), as if it had been funded.
  tokenAccount(address, mint, owner, amount) {
    const data = Buffer.alloc(AccountLayout.span);
    AccountLayout.encode({ mint: new PublicKey(mint), owner: new PublicKey(owner), amount: BigInt(amount),
      delegateOption: 0, delegate: PublicKey.default, state: 1, isNativeOption: 0, isNative: 0n,
      delegatedAmount: 0n, closeAuthorityOption: 0, closeAuthority: PublicKey.default }, data);
    this.set(address, { lamports: 2_039_280, data, owner: TOKEN_PROGRAM_ID });
  }

  tokenAmount(address) { const a = this.get(address); return a ? a.data.readBigUInt64LE(64) : null; }

  // Run a signed transaction, legacy or v0. Accounts it names that the fork
  // lacks are copied from mainnet first, as a real send would find them.
  async sendSigned(tx) {
    const keys = this.keysOf(tx);
    await this.connection().getMultipleAccountsInfo(keys);
    for (const k of keys) this.seen.add(k.toBase58());
    const legacy = tx instanceof Transaction || tx.version === 'legacy';
    const r = legacy ? this.svm.sendLegacyTransaction(tx.serialize()) : this.svm.sendVersionedTransaction(tx.serialize());
    this.svm.expireBlockhash();
    const failed = typeof r.err === 'function';
    const meta = failed ? r.meta() : r;
    return { ok: !failed, logs: meta.logs(), units: Number(meta.computeUnitsConsumed()), size: tx.serialize().length };
  }

  // The newest slot a lookup table can be created at, and a slot later so a
  // table's new addresses become usable.
  recentSlot() { return Number(this.svm.getSlotHashes()[0].slot); }
  nextSlot() { this.svm.warpToSlot(this.svm.getClock().slot + 1n); }

  balance(key) { return Number(this.svm.getBalance(new PublicKey(key).toBytes()) ?? 0n); }

  fund(key, sol) { this.svm.airdrop(new PublicKey(key).toBytes(), BigInt(Math.round(sol * 1e9))); }

  wallet(sol = 1) { const k = Keypair.generate(); this.fund(k.publicKey, sol); return k; }

  // Sign and run. Returns {ok, logs, units, size}; never throws on a
  // program error, so a test can assert on the refusal it expects.
  async send(ixs, payer, extra = []) {
    const tx = new Transaction().add(...ixs);
    tx.feePayer = payer.publicKey;
    tx.recentBlockhash = this.svm.latestBlockhash();
    tx.sign(payer, ...extra);
    return this.sendSigned(tx);
  }

  // Every account a transaction names, its lookup tables resolved.
  keysOf(tx) {
    if (tx instanceof Transaction) return tx.compileMessage().accountKeys;
    const m = tx.message, keys = [...(m.staticAccountKeys ?? m.accountKeys)];
    for (const l of m.addressTableLookups ?? []) {
      const table = AddressLookupTableAccount.deserialize(this.get(l.accountKey).data);
      for (const i of [...l.writableIndexes, ...l.readonlyIndexes]) keys.push(table.addresses[i]);
    }
    return keys;
  }
}
