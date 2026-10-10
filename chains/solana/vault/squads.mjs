// The Squads Smart Account program, as far as a vault needs it.
//
// A vault is a Squads smart account: a PDA that holds the money, a settings
// account that names its owners, and one policy per venue that lets the bot's
// policy key run a fixed set of instructions on the vault's behalf
// (vault/policy.mjs). The owners keep every other right: they withdraw,
// change the policy or remove it with their own wallet, whichever it is.
//
// The program publishes its IDL only in the legacy Anchor 0.29 format, which
// the Anchor coder we ship (0.32) cannot read, so the few layouts we use are
// written out here field by field. Source of truth: Squads-Protocol/
// smart-account-program at 80bf1f7 (2026-01-27), the build deployed at
// SMRTzfY6… (programdata slot 443245754). tests/test_vault_fork.mjs runs every
// builder below against that deployed binary, so a wrong byte fails a test,
// not a transaction.
import { createRequire } from 'module';
import { createHash } from 'node:crypto';
import SOLANA from '../solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, SystemProgram, TransactionInstruction } = require('@solana/web3.js');

export const PROGRAM_ID = new PublicKey(SOLANA.programs.squads_smart_account);

// Permission bits of a signer (state/settings.rs Permission). A signer needs
// all three to act alone through a synchronous transaction.
export const PERMISSION = { initiate: 1, vote: 2, execute: 4, all: 7 };

const SEED = {
  prefix: Buffer.from('smart_account'),
  config: Buffer.from('program_config'),
  settings: Buffer.from('settings'),
  account: Buffer.from('smart_account'),
  policy: Buffer.from('policy'),
};

// --- addresses -------------------------------------------------------------

export function programConfigPda() {
  return PublicKey.findProgramAddressSync([SEED.prefix, SEED.config], PROGRAM_ID)[0];
}

// The settings account of the smart account created with `seed` (the program
// config's counter plus one at creation).
export function settingsPda(seed) {
  return PublicKey.findProgramAddressSync([SEED.prefix, SEED.settings, u128(seed)], PROGRAM_ID)[0];
}

// The vault: the address that holds the money. Index 0 is the default
// account; a settings may own more.
export function vaultPda(settings, index = 0) {
  return PublicKey.findProgramAddressSync([SEED.prefix, settings.toBuffer(), SEED.account, Buffer.from([index])], PROGRAM_ID)[0];
}

export function policyPda(settings, seed) {
  return PublicKey.findProgramAddressSync([SEED.prefix, SEED.policy, settings.toBuffer(), u64(seed)], PROGRAM_ID)[0];
}

// --- Borsh, only what the layouts below use ----------------------------------

function u64(n) { const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt(n)); return b; }
function u128(n) {
  const v = BigInt(n), b = Buffer.alloc(16);
  b.writeBigUInt64LE(v & 0xffffffffffffffffn, 0);
  b.writeBigUInt64LE(v >> 64n, 8);
  return b;
}

class Writer {
  constructor() { this.parts = []; }
  raw(buf) { this.parts.push(Buffer.from(buf)); return this; }
  u8(n) { return this.raw([n]); }
  bool(v) { return this.u8(v ? 1 : 0); }
  u16(n) { const b = Buffer.alloc(2); b.writeUInt16LE(n); return this.raw(b); }
  u32(n) { const b = Buffer.alloc(4); b.writeUInt32LE(n); return this.raw(b); }
  u64(n) { return this.raw(u64(n)); }
  i64(n) { const b = Buffer.alloc(8); b.writeBigInt64LE(BigInt(n)); return this.raw(b); }
  u128(n) { return this.raw(u128(n)); }
  key(k) { return this.raw(new PublicKey(k).toBuffer()); }
  bytes(buf) { return this.u32(buf.length).raw(buf); }
  str(s) { return this.bytes(Buffer.from(s, 'utf8')); }
  option(v, put) { if (v == null) return this.u8(0); this.u8(1); put(this, v); return this; }
  vec(items, put) { this.u32(items.length); for (const x of items) put(this, x); return this; }
  done() { return Buffer.concat(this.parts); }
}

function discriminator(namespace, name) {
  return createHash('sha256').update(`${namespace}:${name}`).digest().subarray(0, 8);
}

const IX = {
  createSmartAccount: discriminator('global', 'create_smart_account'),
  executeSettingsTransactionSync: discriminator('global', 'execute_settings_transaction_sync'),
  executeTransactionSyncV2: discriminator('global', 'execute_transaction_sync_v2'),
};
const ACCOUNT = {
  settings: discriminator('account', 'Settings'),
  policy: discriminator('account', 'Policy'),
  programConfig: discriminator('account', 'ProgramConfig'),
};

// --- policy layouts (state/policies/implementations/program_interaction.rs) --

export const DATA_OPERATOR = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte'];
const DATA_VALUE = ['u8', 'u16', 'u32', 'u64', 'u128', 'bytes'];

function putDataConstraint(w, c) {
  w.u64(c.offset);
  const kind = DATA_VALUE.indexOf(c.type);
  if (kind < 0) throw new Error(`data constraint type ${c.type} is not one of ${DATA_VALUE}`);
  w.u8(kind);
  if (c.type === 'u8') w.u8(Number(c.value));
  else if (c.type === 'u16') w.u16(Number(c.value));
  else if (c.type === 'u32') w.u32(Number(c.value));
  else if (c.type === 'u64') w.u64(c.value);
  else if (c.type === 'u128') w.u128(c.value);
  else w.bytes(Buffer.from(c.value));
  const op = DATA_OPERATOR.indexOf(c.op);
  if (op < 0) throw new Error(`data constraint operator ${c.op} is not one of ${DATA_OPERATOR}`);
  if (c.type === 'bytes' && op > 1) throw new Error('a byte constraint can only be eq or ne');
  w.u8(op);
}

function putAccountConstraint(w, c) {
  w.u8(c.index);
  if (c.keys) { w.u8(0).vec(c.keys, (w, k) => w.key(k)); }
  else if (c.data) { w.u8(1).vec(c.data, putDataConstraint); }
  else throw new Error(`account constraint at ${c.index} pins neither keys nor data`);
  w.option(c.owner, (w, k) => w.key(k));
}

function putInstructionConstraint(w, c) {
  w.key(c.programId);
  w.vec(c.accounts ?? [], putAccountConstraint);
  w.vec(c.data ?? [], putDataConstraint);
}

function putSigner(w, s) { w.key(s.key).u8(s.permissions ?? PERMISSION.all); }

// SettingsAction (state/settings_transaction.rs). Only the actions a vault
// uses; anything else is refused rather than encoded by guesswork.
function putSettingsAction(w, a) {
  switch (a.kind) {
    case 'addSigner': w.u8(0); putSigner(w, a.signer); return;
    case 'removeSigner': w.u8(1).key(a.key); return;
    case 'changeThreshold': w.u8(2).u16(a.threshold); return;
    case 'policyCreate':
      w.u8(7).u64(a.seed);
      w.u8(3);                                   // PolicyCreationPayload::ProgramInteraction
      putProgramInteraction(w, a.policy);
      w.vec(a.signers, putSigner).u16(1).u32(0);   // threshold 1: its one signer, no time lock
      w.option(null).option(null);               // starts now, never expires
      return;
    case 'policyUpdate':
      w.u8(8).key(a.address).vec(a.signers, putSigner).u16(1).u32(0);
      w.u8(3); putProgramInteraction(w, a.policy);
      w.option(null);
      return;
    case 'policyRemove': w.u8(9).key(a.address); return;
    default: throw new Error(`settings action ${a.kind} is not supported`);
  }
}

// A ProgramInteraction policy without hooks or spending limits: the
// instruction constraints are the whole of what the bot may do.
function putProgramInteraction(w, p) {
  if (!p.constraints?.length) throw new Error('a policy without instruction constraints would allow anything');
  if (p.constraints.length > 20) throw new Error(`${p.constraints.length} instruction constraints; the program allows 20`);
  if (!Number.isInteger(p.accountIndex)) throw new Error('a policy names the vault index it acts for (accountIndex)');
  w.u8(p.accountIndex);
  w.vec(p.constraints, putInstructionConstraint);
  w.option(null).option(null);                   // no pre or post hook
  w.vec([], () => {});                           // no spending limits
}

// The bytes of a ProgramInteraction policy as the program stores them: the
// same layout as its creation payload while it has no spending limits. The
// bot compares these with the policy on chain before it acts (policyState).
export function encodeProgramInteraction(policy) {
  const w = new Writer(); putProgramInteraction(w, policy); return w.done();
}

// --- the inner instructions of a synchronous transaction ---------------------

// Compile instructions against one flat account list, the way the program
// reads them (utils/synchronous_transaction_message.rs): SmallVec<u8> of
// {program index u8, SmallVec<u8, u8> account indexes, SmallVec<u16, u8> data}.
// Returns the bytes and the account metas to append after the signers.
// `pda` is the vault: listed as a plain account, the program signs for it.
export function compileInner(ixs, pda) {
  const metas = [];
  const at = (pubkey, signer, writable) => {
    let i = metas.findIndex(m => m.pubkey.equals(pubkey));
    if (i < 0) { metas.push({ pubkey, isSigner: false, isWritable: false }); i = metas.length - 1; }
    const m = metas[i];
    m.isWritable ||= writable;
    if (signer && !pubkey.equals(pda)) m.isSigner = true;
    return i;
  };
  if (ixs.length > 255) throw new Error(`${ixs.length} instructions; at most 255 fit`);
  const w = new Writer().u8(ixs.length);
  for (const ix of ixs) {
    const program = at(ix.programId, false, false);
    const accounts = ix.keys.map(k => at(k.pubkey, k.isSigner, k.isWritable));
    if (accounts.length > 255) throw new Error('an instruction with more than 255 accounts');
    if (ix.data.length > 0xffff) throw new Error('instruction data over 65535 bytes');
    w.u8(program).u8(accounts.length).raw(accounts).u16(ix.data.length).raw(ix.data);
  }
  if (metas.length > 255) throw new Error(`${metas.length} accounts; an index is one byte`);
  return { bytes: w.done(), metas };
}

// --- instructions --------------------------------------------------------------

// Create a smart account. `seed` is the program config counter plus one;
// a race with another creator makes this fail, never misdirects it.
export function createSmartAccountIx({ creator, treasury, seed, signers, threshold, memo = null }) {
  const settings = settingsPda(seed);
  const data = new Writer().raw(IX.createSmartAccount)
    .option(null)                                // autonomous: owners change settings by threshold
    .u16(threshold)
    .vec(signers, putSigner)
    .u32(0)                                      // no time lock
    .option(null)                                // no rent collector
    .option(memo, (w, s) => w.str(s))
    .done();
  return {
    settings,
    ix: new TransactionInstruction({
      programId: PROGRAM_ID, data,
      keys: [
        { pubkey: programConfigPda(), isSigner: false, isWritable: true },
        { pubkey: new PublicKey(treasury), isSigner: false, isWritable: true },
        { pubkey: creator, isSigner: true, isWritable: true },
        { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
        { pubkey: PROGRAM_ID, isSigner: false, isWritable: false },
        { pubkey: settings, isSigner: false, isWritable: true },
      ],
    }),
  };
}

// Change the settings: signers, threshold, policies. `signers` sign it and
// together must reach the threshold; `extra` are accounts an action touches
// (a policy being created or closed).
export function settingsSyncIx({ settings, signers, rentPayer, actions, extra = [], memo = null }) {
  const data = new Writer().raw(IX.executeSettingsTransactionSync)
    .u8(signers.length).vec(actions, putSettingsAction).option(memo, (w, s) => w.str(s)).done();
  return new TransactionInstruction({
    programId: PROGRAM_ID, data,
    keys: [
      { pubkey: settings, isSigner: false, isWritable: true },
      { pubkey: rentPayer, isSigner: true, isWritable: true },
      { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
      { pubkey: PROGRAM_ID, isSigner: false, isWritable: false },
      ...signers.map(k => ({ pubkey: k, isSigner: true, isWritable: false })),
      ...extra.map(k => ({ pubkey: k, isSigner: false, isWritable: true })),
    ],
  });
}

// Run `ixs` as the vault through a policy. `indices[i]` names the policy
// constraint instruction i satisfies (vault/policy.mjs finds them).
export function policyExecuteIx({ policy, signer, vault, ixs, indices, accountIndex = 0 }) {
  if (indices.length !== ixs.length) throw new Error('one constraint index per instruction');
  const { bytes, metas } = compileInner(ixs, vault);
  const data = new Writer().raw(IX.executeTransactionSyncV2)
    .u8(accountIndex).u8(1)                      // one signer: the policy key
    .u8(1)                                       // SyncPayload::Policy
    .u8(1)                                       // PolicyPayload::ProgramInteraction
    .option(Buffer.from(indices), (w, b) => w.bytes(b))
    .u8(1)                                       // ProgramInteractionTransactionPayload::SyncTransaction
    .u8(accountIndex).bytes(bytes)
    .done();
  return new TransactionInstruction({
    programId: PROGRAM_ID, data,
    keys: [
      { pubkey: policy, isSigner: false, isWritable: true },
      { pubkey: PROGRAM_ID, isSigner: false, isWritable: false },
      { pubkey: signer, isSigner: true, isWritable: false },
      ...metas,
    ],
  });
}

// Run `ixs` as the vault with the owners' signatures: a withdrawal, a swap
// back, anything. No policy is involved; the owners need no bot.
export function ownerExecuteIx({ settings, signers, vault, ixs, accountIndex = 0 }) {
  const { bytes, metas } = compileInner(ixs, vault);
  const data = new Writer().raw(IX.executeTransactionSyncV2)
    .u8(accountIndex).u8(signers.length)
    .u8(0).bytes(bytes)                          // SyncPayload::Transaction
    .done();
  return new TransactionInstruction({
    programId: PROGRAM_ID, data,
    keys: [
      { pubkey: settings, isSigner: false, isWritable: true },
      { pubkey: PROGRAM_ID, isSigner: false, isWritable: false },
      ...signers.map(k => ({ pubkey: k, isSigner: true, isWritable: false })),
      ...metas,
    ],
  });
}

// --- reading accounts ------------------------------------------------------------

class Reader {
  constructor(buf) { this.b = buf; this.o = 0; }
  u8() { return this.b[this.o++]; }
  u16() { const v = this.b.readUInt16LE(this.o); this.o += 2; return v; }
  u32() { const v = this.b.readUInt32LE(this.o); this.o += 4; return v; }
  u64() { const v = this.b.readBigUInt64LE(this.o); this.o += 8; return v; }
  i64() { const v = this.b.readBigInt64LE(this.o); this.o += 8; return v; }
  u128() { const lo = this.u64(), hi = this.u64(); return lo + (hi << 64n); }
  key() { const k = new PublicKey(this.b.subarray(this.o, this.o + 32)); this.o += 32; return k; }
  option(get) { return this.u8() ? get(this) : null; }
  vec(get) { const n = this.u32(), out = []; for (let i = 0; i < n; i++) out.push(get(this)); return out; }
}

function expectAccount(data, kind) {
  if (!data || data.length < 8 || !Buffer.from(data.subarray(0, 8)).equals(ACCOUNT[kind])) {
    throw new Error(`not a Squads ${kind} account`);
  }
  return new Reader(Buffer.from(data.subarray(8)));
}

export function decodeProgramConfig(data) {
  const r = expectAccount(data, 'programConfig');
  return { smartAccountIndex: r.u128(), authority: r.key(), creationFee: r.u64(), treasury: r.key() };
}

export function decodeSettings(data) {
  const r = expectAccount(data, 'settings');
  const s = {
    seed: r.u128(), settingsAuthority: r.key(), threshold: r.u16(), timeLock: r.u32(),
    transactionIndex: r.u64(), staleTransactionIndex: r.u64(),
    archivalAuthority: r.option(r => r.key()), archivableAfter: r.u64(), bump: r.u8(),
    signers: r.vec(r => ({ key: r.key(), permissions: r.u8() })),
    accountUtilization: r.u8(), policySeed: r.option(r => r.u64()),
  };
  return s;
}

// The head of a policy account: whose it is and who may use it.
export function decodePolicyHead(data) {
  const r = expectAccount(data, 'policy');
  return {
    settings: r.key(), seed: r.u64(), bump: r.u8(), transactionIndex: r.u64(), staleTransactionIndex: r.u64(),
    signers: r.vec(r => ({ key: r.key(), permissions: r.u8() })), threshold: r.u16(), timeLock: r.u32(),
    stateOffset: 8 + r.o,
  };
}

// True when the policy on chain is exactly `policy`: a ProgramInteraction
// state with these bytes. Anything else (another kind, an owner's edit, a
// spending limit) is not the policy the bot was built for.
export function policyStateIs(data, policy) {
  const { stateOffset } = decodePolicyHead(data);
  const want = Buffer.concat([Buffer.from([3]), encodeProgramInteraction(policy)]);
  return Buffer.from(data.subarray(stateOffset, stateOffset + want.length)).equals(want);
}
