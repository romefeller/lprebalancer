// What a Solana transaction does, read from its bytes, and whether that is
// what the owner asked for. The page runs this in the browser on every
// transaction before the wallet is asked to sign it, and refuses to go on
// when it differs; onboard/verify.mjs runs the same code from a terminal
// against the rules of any git commit. Nothing here trusts the server: the
// expectation comes from what the owner typed and from the rules and keys
// pinned in git (the page's bundle), and every address is checked against
// them or derived from them.
//
//   decodeSol(base64 wire transaction, expect, pins) -> {ok, lines, problems}
//   decodeSolSet([base64, ...], expect, pins) -> {ok, lines, problems}
//       transactions the wallet signs in one approval, sent in this order
//
// expect.kind: setup (a set) | deposit | withdraw | revoke | approve (a set
// when it carries a deposit) | close. A setup's expect.setupKey: the page's
// own one-time key, which signs the policies (each too long for a hardware
// wallet) and then takes itself off the vault.
// pins: {payer, policyKey (the bot's keys), operator, pools {venue: [pool]},
//   rules (rules.json), solana (solana.json)}. Pure, no network.
import { createRequire } from 'module';
import * as sq from '../chains/solana/vault/squads.mjs';
import { compile, instructionsOf, poolPda } from '../chains/solana/vault/policy.mjs';
import { EXIT_INSTRUCTIONS } from '../chains/solana/vault/exit_rules.mjs';
import { fromRaw } from './units.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { ComputeBudgetProgram, PublicKey, SystemProgram, Transaction } = require('@solana/web3.js');

const TOKEN = SOLANA.programs.token;
const TOKEN_2022 = SOLANA.programs.token_2022;
const ATA = SOLANA.programs.associated_token;
const SYSTEM = SystemProgram.programId.toBase58();
const BUDGET = ComputeBudgetProgram.programId.toBase58();
const SQUADS = sq.PROGRAM_ID.toBase58();
// What the owner pays in priority fees on one transaction, at most (lamports).
export const MAX_PRIORITY_LAMPORTS = 1_000_000n;
// The longest message the owner's wallet signs in a set-up: a Tangem card
// signs at most 930 bytes (Android) or 964 (iOS, firmware before 8), its
// APDU (tangem-app-android SOLANA_TRANSACTION_SIZE_THRESHOLD_BYTES,
// tangem-app-ios supportedTransactionSize, 2026-10-14). A policy is longer:
// the page's one-time set-up key signs those.
export const OWNER_MESSAGE_BYTES = 900;
// Rent-exempt lamports of an account of `bytes` (mainnet's rent: 3,480
// lamports a byte-year, two years, 128 bytes of header).
export const rentOf = (bytes) => BigInt(bytes + 128) * 6_960n;
// What the set-up key may get from your wallet for each policy it creates
// (its rent, at most a policy account of 1,536 bytes: solana.mjs
// POLICY_ACCOUNT_BYTES, and a fee), and for its own account while it works.
export const KEY_PER_POLICY = rentOf(1536) + 5_000n;
export const KEY_BASE = rentOf(0) + 5_000n;

export const short = (k) => { const s = String(k); return s.length > 12 ? `${s.slice(0, 4)}…${s.slice(-4)}` : s; };

export function ata(wallet, mint, program = TOKEN) {
  return PublicKey.findProgramAddressSync([new PublicKey(wallet).toBuffer(), new PublicKey(program).toBuffer(),
    new PublicKey(mint).toBuffer()], new PublicKey(ATA))[0].toBase58();
}

// --- byte reading ------------------------------------------------------------------

class Reader {
  constructor(buf) { this.b = Buffer.from(buf); this.o = 0; }
  need(n) { if (this.o + n > this.b.length) throw new Error('the data ends early'); }
  u8() { this.need(1); return this.b[this.o++]; }
  u16() { this.need(2); const v = this.b.readUInt16LE(this.o); this.o += 2; return v; }
  u32() { this.need(4); const v = this.b.readUInt32LE(this.o); this.o += 4; return v; }
  u64() { this.need(8); const v = this.b.readBigUInt64LE(this.o); this.o += 8; return v; }
  u128() { const lo = this.u64(), hi = this.u64(); return lo + (hi << 64n); }
  key() { this.need(32); const k = new PublicKey(this.b.subarray(this.o, this.o + 32)).toBase58(); this.o += 32; return k; }
  bytes(n) { this.need(n); const v = this.b.subarray(this.o, this.o + n); this.o += n; return Buffer.from(v); }
  vec(get, max = 64) { const n = this.u32(); if (n > max) throw new Error(`a list of ${n}`); return Array.from({ length: n }, () => get(this)); }
  option(get) { const t = this.u8(); if (t > 1) throw new Error('a bad option tag'); return t ? get(this) : null; }
  done() { return this.o === this.b.length; }
}

const DATA_VALUE = ['u8', 'u16', 'u32', 'u64', 'u128', 'bytes'];

function readDataConstraint(r) {
  const offset = r.u64(), type = DATA_VALUE[r.u8()];
  if (!type) throw new Error('an unknown data constraint type');
  const value = type === 'u8' ? r.u8() : type === 'u16' ? r.u16() : type === 'u32' ? r.u32() : type === 'u64' ? r.u64()
    : type === 'u128' ? r.u128() : r.bytes(r.u32());
  const op = sq.DATA_OPERATOR[r.u8()];
  if (!op) throw new Error('an unknown operator');
  return { offset, type, value, op };
}

// A ProgramInteraction policy's constraints, as the program stores them.
export function readPolicy(r) {
  const accountIndex = r.u8();
  const constraints = r.vec(r => ({
    programId: r.key(),
    accounts: r.vec(r => {
      const index = r.u8(), kind = r.u8();
      if (kind > 1) throw new Error('an unknown account constraint');
      const c = kind === 0 ? { index, keys: r.vec(r => r.key(), 256) } : { index, data: r.vec(readDataConstraint) };
      c.owner = r.option(r => r.key());
      return c;
    }),
    data: r.vec(readDataConstraint),
  }), 20);
  const pre = r.option(r => r.key()), post = r.option(r => r.key());
  const limits = r.u32();
  return { accountIndex, constraints, hooks: Boolean(pre || post), spendingLimits: limits };
}

// A ProgramInteraction policy where `r` stands: its constraints and its bytes.
function policyAt(r) {
  const payload = r.u8();
  if (payload !== 3) throw new Error(`a change other than a program-interaction policy (payload ${payload})`);
  const start = r.o;
  const policy = readPolicy(r);
  return { policy, bytes: Buffer.from(r.b.subarray(start, r.o)) };
}

// The settings actions of execute_settings_transaction_sync: policy updates
// (squads.mjs putSettingsAction 8), policy creations (7, a venue staged on
// the vault, admin.mjs approvePlan), and a signer added (0) or removed (1)
// (a set-up key, admin.mjs ownerSetupPlan: only the set-up's check accepts
// these). Any other action (a threshold, a policy's removal) is refused, not
// read. A creation's address is its seed's (squads.mjs policyPda).
function readSettingsActions(data, settings) {
  const r = new Reader(data);
  r.bytes(8);
  const signers = r.u8();
  const readSigners = (r) => r.vec(r => ({ key: r.key(), permissions: r.u8() }));
  // a start or an expiry (an option holding a time): the rules set none, so any is refused, not read
  const noTime = (r, what) => { if (r.u8() !== 0) throw new Error(`a policy with ${what}`); };
  const actions = r.vec(r => {
    const kind = r.u8();
    if (kind === 8) {
      const address = r.key();
      const policySigners = readSigners(r);
      const threshold = r.u16(), timeLock = r.u32();
      const { policy, bytes } = policyAt(r);
      noTime(r, 'an expiry');
      return { kind: 'policyUpdate', address, signers: policySigners, threshold, timeLock, policy, bytes };
    }
    if (kind === 7) {
      const seed = r.u64();
      const { policy, bytes } = policyAt(r);
      const policySigners = readSigners(r);
      const threshold = r.u16(), timeLock = r.u32();
      noTime(r, 'a start');
      noTime(r, 'an expiry');
      return { kind: 'policyCreate', seed, address: sq.policyPda(new PublicKey(settings), seed).toBase58(), signers: policySigners, threshold, timeLock,
        policy, bytes };
    }
    if (kind === 0) return { kind: 'addSigner', key: r.key(), permissions: r.u8() };
    if (kind === 1) return { kind: 'removeSigner', key: r.key() };
    throw new Error(`a change other than a policy update (settings action ${kind})`);
  }, 16);
  const memo = r.option(r => r.bytes(r.u32()).toString('utf8'));
  if (!r.done()) throw new Error('bytes after the settings actions');
  return { signers, actions, memo };
}

// The inner instructions of an owner's execute_transaction_sync_v2.
function readExecute(data, keys) {
  const r = new Reader(data);
  r.bytes(8);
  const accountIndex = r.u8(), nSigners = r.u8(), payload = r.u8();
  if (payload !== 0) return { policy: true };
  const inner = new Reader(r.bytes(r.u32()));
  if (!r.done()) throw new Error('bytes after the inner transaction');
  const metas = keys.slice(2 + nSigners);
  const n = inner.u8(), ixs = [];
  for (let i = 0; i < n; i++) {
    const p = inner.u8(), count = inner.u8();
    const idx = Array.from(inner.bytes(count));
    const d = inner.bytes(inner.u16());
    if ([p, ...idx].some(x => x >= metas.length)) throw new Error('an inner account out of range');
    ixs.push({ programId: metas[p].pubkey, keys: idx.map(x => metas[x]), data: d });
  }
  if (!inner.done()) throw new Error('bytes after the inner instructions');
  return { accountIndex, signers: keys.slice(2, 2 + nSigners).map(k => k.pubkey), settings: keys[0]?.pubkey, ixs };
}

// --- the rules, by program and discriminator ---------------------------------------------

function prefixOf(d) {
  if (typeof d === 'string') return Buffer.from(d, 'hex');
  const b = Buffer.alloc(d.type === 'u8' ? 1 : 4);
  d.type === 'u8' ? b.writeUInt8(d.value) : b.writeUInt32LE(d.value);
  return b;
}

// Every rule of every venue, with its program and the bytes its data starts with.
export function ruleIndex(rules) {
  const out = [];
  for (const venue of Object.keys(rules.venues)) {
    const program = rules.venues[venue].program;
    for (const r of instructionsOf(venue, rules)) out.push({ ...r, venue, program, prefix: prefixOf(r.discriminator) });
  }
  for (const r of [...rules.common, ...rules.payout]) out.push({ ...r, venue: null, prefix: prefixOf(r.discriminator) });
  return out;
}

const ruleOf = (index, programId, data) => index.find(r => r.program === programId && Buffer.from(data).subarray(0, r.prefix.length).equals(r.prefix));

// --- the transaction ------------------------------------------------------------------------

// Each instruction as a plain record: {program, keys [{pubkey, signer, writable}], data}.
function instructionsOfTx(tx) {
  return tx.instructions.map(ix => ({ programId: ix.programId.toBase58(), data: Buffer.from(ix.data),
    keys: ix.keys.map(k => ({ pubkey: k.pubkey.toBase58(), isSigner: k.isSigner, isWritable: k.isWritable })) }));
}

const u64At = (d, o) => Buffer.from(d).readBigUInt64LE(o);

// One instruction in words, and its money movement: {text, move?}. `names`
// labels addresses ("your wallet"); `vault` signs inside an owner's execute.
function describe(ix, ctx) {
  const name = (k) => ctx.names[k] ? `${ctx.names[k]} (${short(k)})` : short(k);
  const d = ix.data, k = ix.keys.map(x => x.pubkey);
  if (ix.programId === SYSTEM && d.length === 12 && d.readUInt32LE(0) === 2) {
    const lamports = u64At(d, 4);
    return { text: `Send ${fromRaw(lamports, 9)} SOL from ${name(k[0])} to ${name(k[1])}`,
      move: { kind: 'sol', from: k[0], to: k[1], raw: lamports } };
  }
  if ((ix.programId === TOKEN || ix.programId === TOKEN_2022) && d[0] === 12 && d.length === 10) {
    const amount = u64At(d, 1), decimals = d[9], sym = ctx.symbols[k[1]] ?? short(k[1]);
    return { text: `Send ${fromRaw(amount, decimals)} ${sym} from ${name(k[0])} to ${name(k[2])}`,
      move: { kind: 'token', src: k[0], mint: k[1], dst: k[2], authority: k[3], raw: amount, decimals, program: ix.programId } };
  }
  if ((ix.programId === TOKEN || ix.programId === TOKEN_2022) && d[0] === 9 && d.length === 1) {
    return { text: `Close the token account ${name(k[0])}; its rent goes to ${name(k[1])}`,
      move: { kind: 'closeAccount', account: k[0], dst: k[1], authority: k[2], program: ix.programId } };
  }
  if (ix.programId === ATA && (d.length === 0 || d[0] <= 1)) {
    const sym = ctx.symbols[k[3]] ?? short(k[3]);
    return { text: `Open the ${sym} account of ${name(k[2])} if missing (rent from ${name(k[0])})`,
      move: { kind: 'ata', payer: k[0], account: k[1], wallet: k[2], mint: k[3], program: k[5] } };
  }
  if (ix.programId === BUDGET) {
    if (d[0] === 2) return { text: `Compute limit ${d.readUInt32LE(1).toLocaleString('en-US')} units`, move: { kind: 'budget', units: BigInt(d.readUInt32LE(1)) } };
    if (d[0] === 3) return { text: `Priority fee ${u64At(d, 1)} micro-lamports per unit`, move: { kind: 'price', micro: u64At(d, 1) } };
    return { text: 'An unknown compute budget instruction', move: { kind: 'unknown' } };
  }
  const rule = ruleOf(ctx.rules, ix.programId, d);
  if (rule) return { text: `${ctx.venueNames[rule.venue]}: ${rule.name}`, move: { kind: 'venue', rule } };
  return { text: `An unknown instruction ${d.subarray(0, 8).toString('hex')} of program ${short(ix.programId)}`, move: { kind: 'unknown' } };
}

// A constraint of a policy in words: the instruction it allows and what it pins.
function describeConstraint(c, ctx) {
  const prefix = c.data.find(x => x.offset === 0n && x.op === 'eq');
  const bytes = !prefix ? null : prefix.type === 'bytes' ? Buffer.from(prefix.value)
    : prefixOf({ type: prefix.type, value: Number(prefix.value) });
  const rule = bytes && ctx.rules.find(r => r.program === c.programId && r.prefix.equals(bytes));
  const what = rule ? `${ctx.venueNames[rule.venue] ?? 'Program'}: ${rule.name}` : `an instruction of ${short(c.programId)}`;
  const pins = c.accounts.map(a => a.keys
    ? `#${a.index} one of ${a.keys.map(k => ctx.names[k] ? `${ctx.names[k]} (${short(k)})` : short(k)).join(', ')}`
    : `#${a.index} a token account owned by ${a.data.some(x => x.type === 'bytes' && ctx.vault && new PublicKey(x.value).toBase58() === ctx.vault) ? 'your vault' : 'another owner'}`);
  return `${what}${pins.length ? ` — ${pins.join('; ')}` : ''}`;
}

// The names the lines use for the addresses the owner knows.
function namesOf(expect, pins) {
  const n = {};
  if (expect.owner) n[expect.owner] = 'your wallet';
  if (expect.vault) n[expect.vault] = 'your vault';
  if (expect.setupKey) n[expect.setupKey] = 'this page\'s one-time set-up key';
  if (pins.payer) n[pins.payer] = 'the bot\'s gas key';
  if (pins.policyKey) n[pins.policyKey] = 'the bot\'s rules key';
  for (const [venue, list] of Object.entries(pins.pools)) for (const p of list) n[p] = `the ${venue} pool`;
  const mints = symbolsOf(pins);
  for (const w of expect.payoutWallets ?? []) {
    // the owner's own accounts are named below, whatever pays them
    const who = w === pins.operator ? 'the operator\'s fee share' : 'your profit wallet';
    for (const m of Object.keys(mints)) n[ata(w, m)] = `${who}'s ${mints[m]} account`;
  }
  for (const w of [expect.owner, expect.vault].filter(Boolean)) {
    for (const m of Object.keys(mints)) {
      for (const p of [TOKEN, TOKEN_2022]) n[ata(w, m, p)] = `${w === expect.owner ? 'your wallet' : 'your vault'}'s ${mints[m]} account`;
    }
  }
  return n;
}

const symbolsOf = (pins) => ({ [pins.solana.native_mint]: 'SOL', [pins.solana.usdc_mint]: 'USDC' });

// The policy the bot's rules compile to for this vault and venue, with the
// payouts of `payoutWallets` (rules.json's compile, the same code the bot runs).
export function expectedPolicies(venue, expect, pins) {
  const payouts = (expect.payoutWallets ?? []).map(w => new PublicKey(ata(w, pins.solana.usdc_mint)));
  return compile(venue, { vault: new PublicKey(expect.vault), payer: new PublicKey(pins.payer),
    pools: expect.pools[venue].map(p => new PublicKey(p)), payouts }, pins.rules).policies;
}

// --- the check ----------------------------------------------------------------------------------

export function decodeSol(base64, expect, pins) {
  const lines = [], problems = [];
  const no = (m) => { problems.push(m); };
  let tx;
  try {
    const raw = Buffer.from(base64, 'base64');
    if (raw.length > 1232) return { ok: false, lines, problems: [`the transaction is ${raw.length} bytes, over the 1232 a transaction may have`] };
    tx = Transaction.from(raw);
  } catch (e) {
    return { ok: false, lines, problems: [`the transaction cannot be read (${e.message}); a version 0 transaction with address tables is refused here`] };
  }
  const ctx = { rules: ruleIndex(pins.rules), names: namesOf(expect, pins), symbols: symbolsOf(pins), vault: expect.vault,
    venueNames: { 'meteora-dlmm': 'Meteora', 'raydium-clmm': 'Raydium', orca: 'Orca', byreal: 'Byreal', null: 'Token' } };
  const owner = expect.owner;
  // a set-up's policies are signed by the page's own one-time key (setupPart); everything else by your wallet alone
  const helper = expect.kind === 'setup' && expect.part && expect.setupKey ? expect.setupKey : null;
  const payer = tx.feePayer?.toBase58();
  if (payer !== owner && (!helper || payer !== helper)) no(`the fee payer is ${short(payer)}, not your wallet`);
  const msg = tx.compileMessage();
  const signers = msg.accountKeys.slice(0, msg.header.numRequiredSignatures).map(k => k.toBase58());
  if (signers.length !== 1 || signers[0] !== payer || (payer !== owner && payer !== helper)) {
    no(`it asks for the signatures of ${signers.map(short).join(', ')}; only your wallet may sign`);
  }
  ctx.signer = payer;
  // what your wallet signs in a set-up fits a hardware wallet (a Tangem card signs 930 bytes at most)
  if (helper && payer === owner && msg.serialize().length > OWNER_MESSAGE_BYTES) {
    no(`your wallet would sign ${msg.serialize().length} bytes; a hardware wallet signs no more than ${OWNER_MESSAGE_BYTES}`);
  }
  const ixs = instructionsOfTx(tx);
  if (expect.vault && expect.settings && sq.vaultPda(new PublicKey(expect.settings)).toBase58() !== expect.vault) {
    no('the vault is not the account of these settings');
  }

  // compute budget: limits only, and a priority fee the owner barely notices
  let units = 200_000n * BigInt(ixs.length), micro = 0n;
  const outer = [];
  for (const ix of ixs) {
    if (ix.programId === BUDGET) {
      const d = describe(ix, ctx);
      if (d.move.kind === 'budget') units = d.move.units;
      else if (d.move.kind === 'price') micro = d.move.micro;
      else no(d.text);
      continue;
    }
    outer.push(ix);
  }
  const priority = (units * micro + 999_999n) / 1_000_000n;
  if (priority > MAX_PRIORITY_LAMPORTS) no(`a priority fee of ${fromRaw(priority, 9)} SOL`);
  if (priority > 0n) lines.push(`Priority fee: at most ${fromRaw(priority, 9)} SOL`);

  const check = expect.part ? SET_CHECKS[expect.kind] : CHECKS[expect.kind];
  if (!check) return { ok: false, lines, problems: [`nothing to check a ${expect.kind} against${expect.part ? ' in a set' : ''}`] };
  try { check(outer, expect, pins, ctx, lines, no); } catch (e) { no(`the transaction cannot be read: ${e.message}`); }
  return { ok: problems.length === 0, lines, problems };
}

// The owner's execute: its inner instructions run as the vault, by the owner.
function ownerExecute(ix, expect, ctx, no) {
  if (ix.programId !== SQUADS || !Buffer.from(ix.data).subarray(0, 8).equals(sqDisc('execute'))) return null;
  const e = readExecute(ix.data, ix.keys);
  if (e.policy) { no('it runs through a bot policy, not as your own transaction'); return null; }
  if (e.settings !== expect.settings) no(`it acts on the vault settings ${short(e.settings)}, not yours`);
  if (e.accountIndex !== 0) no(`it acts for vault account ${e.accountIndex}, not your vault`);
  if (e.signers.length !== 1 || e.signers[0] !== expect.owner) no('the vault transaction is not signed by your wallet alone');
  return e.ixs;
}

const DISC = {};
function sqDisc(name) {
  if (!DISC[name]) {
    // the instruction builders' own bytes: build one and read its first 8
    const ix = name === 'execute'
      ? sq.ownerExecuteIx({ settings: PublicKey.default, signers: [], vault: PublicKey.default, ixs: [] })
      : name === 'create'
        ? sq.createSmartAccountIx({ creator: PublicKey.default, treasury: PublicKey.default, seed: 1n, signers: [], threshold: 1 }).ix
        : sq.settingsSyncIx({ settings: PublicKey.default, signers: [], rentPayer: PublicKey.default, actions: [] });
    DISC[name] = Buffer.from(ix.data.subarray(0, 8));
  }
  return DISC[name];
}

// Each inner instruction in words, under "As your vault:".
function innerLines(inner, ctx, lines) {
  return inner.map(ix => {
    const d = describe(ix, ctx);
    lines.push(`  · ${d.text}`);
    return d;
  });
}

// a deposit: SOL to the vault, or a token from your account to the vault's
function deposit(outer, expect, pins, ctx, lines, no) {
  const native = expect.mint === pins.solana.native_mint;
  const moves = outer.map(ix => { const d = describe(ix, ctx); lines.push(d.text); return d.move; });
  if (native) {
    if (moves.length !== 1 || moves[0].kind !== 'sol') return no('a SOL deposit is one transfer');
    const m = moves[0];
    if (m.from !== expect.owner || m.to !== expect.vault) no('the SOL does not go from your wallet to your vault');
    if (m.raw !== BigInt(expect.raw)) no(`it deposits ${fromRaw(m.raw, 9)} SOL, not ${fromRaw(BigInt(expect.raw), 9)}`);
    return;
  }
  const sends = moves.filter(m => m.kind === 'token'), opens = moves.filter(m => m.kind === 'ata');
  if (sends.length !== 1 || opens.length + sends.length !== moves.length || opens.length > 1) return no('a token deposit is one transfer, and the vault\'s account opened if missing');
  const m = sends[0], dst = ata(expect.vault, expect.mint, m.program);
  for (const o of opens) {
    if (o.wallet !== expect.vault || o.mint !== expect.mint || o.account !== dst || o.payer !== expect.owner) no('it opens an account that is not your vault\'s');
  }
  if (m.mint !== expect.mint) no('another token than the one you chose');
  if (m.src !== ata(expect.owner, expect.mint, m.program) || m.authority !== expect.owner) no('the tokens do not come from your wallet');
  if (m.dst !== dst) no(`the tokens go to ${short(m.dst)}, not your vault's account`);
  if (m.raw !== BigInt(expect.raw)) no(`it deposits ${fromRaw(m.raw, m.decimals)}, not ${fromRaw(BigInt(expect.raw), m.decimals)}`);
}

// a withdrawal: out of the vault, into your own wallet only
function withdraw(outer, expect, pins, ctx, lines, no) {
  const moves = [];
  for (const ix of outer) {
    const inner = ownerExecute(ix, expect, ctx, no);
    if (inner) {
      lines.push('As your vault, signed by your wallet:');
      moves.push(...innerLines(inner, ctx, lines).map(d => ({ ...d.move, inside: true })));
      continue;
    }
    const d = describe(ix, ctx); lines.push(d.text); moves.push(d.move);
  }
  let sent = 0;
  for (const m of moves) {
    if (m.kind === 'ata' && !m.inside) {
      if (m.wallet !== expect.owner || m.payer !== expect.owner) no('it opens an account that is not your wallet\'s');
    } else if (m.kind === 'sol' && m.inside) {
      sent++;
      if (m.from !== expect.vault || m.to !== expect.owner) no('SOL moves elsewhere than from your vault to your wallet');
      if (expect.raw !== 'all' && (expect.mint !== pins.solana.native_mint || m.raw !== BigInt(expect.raw))) no(`it withdraws ${fromRaw(m.raw, 9)} SOL, not what you asked for`);
    } else if (m.kind === 'token' && m.inside) {
      sent++;
      if (m.authority !== expect.vault || m.src !== ata(expect.vault, m.mint, m.program)) no('tokens leave an account that is not your vault\'s');
      if (m.dst !== ata(expect.owner, m.mint, m.program)) no(`tokens go to ${short(m.dst)}, not your wallet's account`);
      if (expect.raw !== 'all' && (m.mint !== expect.mint || m.raw !== BigInt(expect.raw))) no('another token or amount than you asked for');
    } else if (m.kind === 'closeAccount' && m.inside && expect.raw === 'all') {
      if (m.authority !== expect.vault || m.dst !== expect.owner) no('a vault account closes to somewhere other than your wallet');
    } else no(`an instruction a withdrawal does not need: ${m.kind}`);
  }
  if (!sent) no('it withdraws nothing');
}

// revoke: every policy names you as its only signer; the rules themselves unchanged
function revoke(outer, expect, pins, ctx, lines, no) { policyUpdates(outer, expect, pins, ctx, lines, no, expect.owner); }
// approve: every policy names the bot's rules key, with exactly the rules in git
function approve(outer, expect, pins, ctx, lines, no) { policyUpdates(outer, expect, pins, ctx, lines, no, pins.policyKey); }

// the owner's close: the venue's exit instructions only, everything back into the vault
function close(outer, expect, pins, ctx, lines, no) {
  let venueIxs = 0;
  for (const ix of outer) {
    const inner = ownerExecute(ix, expect, ctx, no);
    if (!inner) {
      const d = describe(ix, ctx); lines.push(d.text);
      if (d.move.kind !== 'ata' || d.move.wallet !== expect.vault || d.move.payer !== expect.owner) no(`outside the vault: ${d.text}`);
      continue;
    }
    lines.push('As your vault, signed by your wallet:');
    const descs = innerLines(inner, ctx, lines);
    inner.forEach((ix2, i) => {
      const m = descs[i].move;
      if (m.kind === 'venue') { venueIxs++; return exitPins(ix2, m.rule, expect, pins, no); }
      if (m.kind === 'ata') { if (m.wallet !== expect.vault) no('it opens an account that is not your vault\'s'); return; }
      if (m.kind === 'closeAccount') {
        if (m.authority !== expect.vault || m.dst !== expect.vault) no('a vault account closes to somewhere other than your vault');
        return;
      }
      no(`an instruction a close does not need: ${descs[i].text}`);
    });
  }
  if (!venueIxs) no('it closes no position');
}

const CHECKS = { deposit, withdraw, revoke, approve, close };

// --- sets: transactions signed in one approval ---------------------------------------

// Every transaction of the set checked as a part of it (expect.part: its
// index, the count and the state the parts share), then the set as a whole.
export function decodeSolSet(list, expect, pins) {
  const lines = [], problems = [];
  if (!Array.isArray(list) || !list.length) return { ok: false, lines, problems: ['no transaction to check'] };
  const state = { policies: [], funding: [], opens: [], created: false, fundedIn: null, policyIn: [], phase: 0, keyFund: 0n, released: false };
  list.forEach((b64, index) => {
    const r = decodeSol(b64, { ...expect, part: { index, count: list.length, state } }, pins);
    lines.push(`Transaction ${index + 1} of ${list.length}:`, ...r.lines.map(l => `  ${l}`));
    problems.push(...r.problems.map(p => `transaction ${index + 1}: ${p}`));
  });
  const whole = SET_FINALS[expect.kind];
  if (whole) {
    try { whole(state, expect, pins, lines, (m) => problems.push(m)); } catch (e) { problems.push(`the set cannot be read: ${e.message}`); }
  }
  return { ok: problems.length === 0, lines, problems };
}

// The policies a vault gets, in seed order: each venue's compiled rules
// (expectedPolicies) at the seeds 1, 2, ... in the order of expect.venues.
// [{venue, seed, address, policy}].
export function setupPolicies(expect, pins) {
  const out = [];
  for (const venue of expect.venues ?? []) {
    for (const p of expectedPolicies(venue, expect, pins)) {
      const seed = out.length + 1;
      out.push({ venue, seed, address: sq.policyPda(new PublicKey(expect.settings), seed).toBase58(), policy: p.policy });
    }
  }
  return out;
}

// The creation of the owner's smart account: the owner its creator and only
// owner (every permission), with `expect.setupKey` (this page's one-time
// key, every permission) as its second signer when the set-up has one,
// threshold 1, no settings authority, no time lock, no rent collector, at
// the settings of the planned seed.
function readCreate(ix, expect, no, lines) {
  const r = new Reader(ix.data);
  r.bytes(8);
  const authority = r.option(r => r.key());
  const threshold = r.u16();
  const signers = r.vec(r => ({ key: r.key(), permissions: r.u8() }));
  const timeLock = r.u32();
  const collector = r.option(r => r.key());
  const memo = r.option(r => r.bytes(r.u32()).toString('utf8'));
  if (!r.done()) no('bytes after the vault\'s creation');
  const k = ix.keys.map(x => x.pubkey);
  const want = [expect.owner, ...(expect.setupKey ? [expect.setupKey] : [])];
  lines.push(expect.setupKey
    ? `Create your vault ${short(expect.vault)}: you its owner, threshold 1; this page's one-time set-up key ${short(expect.setupKey)} its second signer `
      + `until the bot's rules are on it, then it takes itself off${memo ? ` (memo "${memo}")` : ''}; rent from your wallet`
    : `Create your vault ${short(expect.vault)}: you its only owner, threshold 1${memo ? ` (memo "${memo}")` : ''}; rent from your wallet`);
  if (authority) no(`the vault would have a settings authority ${short(authority)} that can change everything`);
  if (threshold !== 1) no(`threshold ${threshold}, not 1`);
  if (signers.length !== want.length || signers.some((s, i) => s.key !== want[i] || s.permissions !== sq.PERMISSION.all)) {
    no(`the vault's signers would be ${signers.map(x => short(x.key)).join(', ')}, not ${expect.setupKey ? 'your wallet and this page\'s set-up key' : 'your wallet alone'}`);
  }
  if (timeLock !== 0) no(`a time lock of ${timeLock}s`);
  if (collector) no(`a rent collector ${short(collector)}`);
  if (k[0] !== sq.programConfigPda().toBase58()) no('another program config than Squads\'');
  if (k[2] !== expect.owner || !ix.keys[2].isSigner) no('the creator is not your wallet');
  if (k[3] !== SYSTEM || k[4] !== SQUADS) no('another program than Squads and System');
  if (k[5] !== expect.settings) no(`it creates the settings ${short(k[5])}, not your vault's`);
  if (ix.keys.length !== 6) no('accounts a creation does not take');
}

// The phases of a set-up, in the order they are sent: your wallet's part
// (the vault's creation, the set-up key put on it, its SOL), the set-up
// key's (the policies, then it takes itself off), the funding (your deposit
// and the bot's gas). A step of an earlier phase after a later one is out
// of order.
const PHASE = { owner: 0, key: 1, fund: 2 };
function phase(state, p, no, what) {
  if (state.phase > p) no(`${what} comes after ${state.phase === PHASE.fund ? 'the deposit' : 'the set-up key\'s part'}: out of order`);
  state.phase = Math.max(state.phase, p);
}

// One part of an owner's set-up: the vault's creation (first, once), the
// bot's policies one by one in seed order, then the funding (the deposit and
// the bot's gas). With `expect.setupKey` (the page's one-time key, the
// owner's set-up since 2026-10-14), the set-up key signs and pays the
// policies, then takes itself off the vault and sends its SOL back; your
// wallet signs the creation (or puts the key on a vault that exists, and
// takes an older key off), the key's SOL, and the funding, last. Without it
// (the CLI's set-up), your wallet signs each policy and the funding goes in
// the last transaction.
function setupPart(outer, expect, pins, ctx, lines, no) {
  const { index, count, state } = expect.part;
  const helper = expect.setupKey ?? null;
  const byKey = Boolean(helper) && ctx.signer === helper;
  if (sq.settingsPda(BigInt(expect.seed)).toBase58() !== expect.settings) no('the vault settings are not those of the planned seed');
  if (sq.vaultPda(new PublicKey(expect.settings)).toBase58() !== expect.vault) no('the vault is not the account of these settings');
  if (!expect.venues?.length) no('no venue for the bot');
  for (const v of expect.venues ?? []) {
    if (!pins.rules.venues?.[v]) no(`${v} has no rules in git`);
    for (const p of expect.pools?.[v] ?? []) if (!(pins.pools[v] ?? []).includes(p)) no(`the pool ${short(p)} is not the ${v} pool the rules approve`);
    if (!(expect.pools?.[v] ?? []).length) no(`no pool for ${v}`);
  }
  if ((expect.payoutWallets ?? []).slice(1).some(w => w !== pins.operator)) no('a payout wallet beside yours that is not the operator\'s');
  if (helper && (helper === expect.owner || helper === pins.payer || helper === pins.policyKey)) no('the set-up key is your wallet or a key of the bot');
  const all = setupPolicies(expect, pins);
  let releasedHere = false;
  if (byKey) phase(state, PHASE.key, no, 'a set-up key\'s transaction');
  for (const ix of outer) {
    const disc = Buffer.from(ix.data).subarray(0, 8);
    if (ix.programId === SQUADS && disc.equals(sqDisc('create'))) {
      if (byKey) no('the set-up key creates no vault');
      if (state.created || state.policies.length || state.funding.length || state.keyFund) no('the vault\'s creation is not the first step');
      phase(state, PHASE.owner, no, 'the vault\'s creation');
      state.created = true;
      readCreate(ix, expect, no, lines);
      continue;
    }
    if (ix.programId === SQUADS && disc.equals(sqDisc('settings'))) {
      const by = byKey ? helper : expect.owner;
      if (state.funding.length) no('a change of your vault after the deposit: money would reach it before all its rules');
      if (ix.keys[0]?.pubkey !== expect.settings) no('it changes another vault');
      if (ix.keys[1]?.pubkey !== by) no(byKey ? 'the rent is paid by someone else than the set-up key' : 'the rent is paid by someone else than you');
      const s = readSettingsActions(ix.data, expect.settings);
      if (s.signers !== 1 || ix.keys[4]?.pubkey !== by) no(byKey ? 'it is not signed by the set-up key alone' : 'it is not signed by your wallet alone');
      for (const a of s.actions) {
        if (a.kind === 'addSigner') {
          if (!helper || byKey) { no(`it adds the signer ${short(a.key)} to your vault`); continue; }
          if (a.key !== helper || a.permissions !== sq.PERMISSION.all) { no(`it adds ${short(a.key)} to your vault, not this page's set-up key`); continue; }
          phase(state, PHASE.owner, no, 'the set-up key put on your vault');
          lines.push(`Put this page's one-time set-up key ${short(helper)} on your vault: it adds the bot's rules, then takes itself off`);
          continue;
        }
        if (a.kind === 'removeSigner') {
          if (!helper) { no(`it takes the signer ${short(a.key)} off your vault`); continue; }
          if (a.key === expect.owner) { no('it takes your wallet off your vault'); continue; }
          if (byKey) {
            if (a.key !== helper) { no(`the set-up key takes ${short(a.key)} off your vault`); continue; }
            state.released = true; releasedHere = true;
            lines.push('The set-up key takes itself off your vault: you are its only owner again');
          } else {
            phase(state, PHASE.owner, no, 'an older set-up key taken off');
            lines.push(`Take the older set-up key ${short(a.key)} off your vault`);
          }
          continue;
        }
        if (a.kind !== 'policyCreate') { no('a set-up only creates policies'); continue; }
        if (helper && !byKey) no('with a set-up key, your wallet signs no policy');
        if (state.released) no('a policy after the set-up key took itself off');
        const w = all[Number(a.seed) - 1];
        const prev = state.policies.at(-1);
        if (!w) { no(`policy seed ${a.seed}: the rules make ${all.length} policies`); continue; }
        if (prev ? Number(a.seed) !== prev + 1 : state.created && Number(a.seed) !== 1) no(`policy seed ${a.seed} out of order`);
        if (!ix.keys.slice(5).some(x => x.pubkey === w.address)) no(`policy ${short(w.address)}: its account is not in the instruction`);
        state.policies.push(Number(a.seed));
        state.policyIn.push(index);
        if (a.signers.length !== 1 || a.signers[0].key !== pins.policyKey || a.signers[0].permissions !== sq.PERMISSION.all) {
          no(`policy ${short(w.address)} would name ${a.signers.map(x => short(x.key)).join(', ')}, not the bot's rules key alone`);
        }
        if (a.threshold !== 1 || a.timeLock !== 0) no('a threshold or time lock other than the rules\'');
        if (a.policy.hooks || a.policy.spendingLimits || a.policy.accountIndex !== 0) no('hooks, spending limits or another vault account');
        const same = Buffer.from(a.bytes).equals(sq.encodeProgramInteraction(w.policy));
        lines.push(`New policy ${short(w.address)} (${w.venue}): signer the bot's rules key ${short(pins.policyKey)}; `
          + `${a.policy.constraints.length} allowed instructions${same ? ', exactly the rules in git' : ''}${byKey ? '; signed and paid by the set-up key' : ''}:`);
        for (const c of a.policy.constraints) lines.push(`  · ${describeConstraint(c, ctx)}`);
        if (!same) no(`policy ${short(w.address)} differs from the rules in git (rules.json) for your vault`);
      }
      continue;
    }
    const d = describe(ix, ctx), m = d.move;
    if (byKey) {
      // the set-up key moves only its own SOL back to your wallet, after it took itself off
      if (m.kind === 'sol' && m.from === helper && m.to === expect.owner && releasedHere) { lines.push(d.text); continue; }
      no(`the set-up key may only send its SOL back to your wallet, once it is off your vault: ${d.text}`);
      continue;
    }
    if (helper && m.kind === 'sol' && m.to === helper) {
      if (m.from !== expect.owner) no('the set-up key\'s SOL does not come from your wallet');
      phase(state, PHASE.owner, no, 'SOL to the set-up key');
      state.keyFund += m.raw;
      lines.push(`${d.text}: the rent of the bot's rules and the fees; what is left comes back to you`);
      continue;
    }
    // the vault's token account may open earlier (rent only, no money): read with the deposit
    if (m.kind === 'ata') { state.opens.push(ix); continue; }
    if (helper) phase(state, PHASE.fund, no, 'the deposit');
    else if (index !== count - 1) no('money moves before the last transaction: it would reach the vault before all its rules');
    state.funding.push(ix);
    state.fundedIn = index;
  }
}

// The set-up as a whole: every policy the rules make exists after it (the
// set creates the missing ones), the vault created with them when the set
// creates it, the set-up key's SOL no more than its work takes, and the
// funding exactly the deposit you chose and the bot's gas. Without a set-up
// key, the last policy is in the funding's transaction and the funding must
// be there; with one, the funding is the set's last part, or none (it is on
// chain already, or comes with the key's last step).
function setupWhole(state, expect, pins, lines, no) {
  const all = setupPolicies(expect, pins);
  const helper = expect.setupKey ?? null;
  // without the creation (a resume), a vault that does not exist fails on chain with the whole last transaction
  if (expect.created === false && state.created) no('it creates a vault that exists');
  if (expect.created === true && !state.created) no('the vault is not created');
  if (state.created && state.policies[0] !== 1) no('the vault is created without its first policy');
  if ((helper ? state.policies.length : all.length) && state.policies.at(-1) !== all.length) no(`the set ends at policy ${state.policies.at(-1) ?? 'none'}, not ${all.length}: rules would be missing`);
  if (helper) {
    const bound = state.policies.length ? KEY_BASE + BigInt(state.policies.length) * KEY_PER_POLICY : 0n;
    if (state.keyFund > bound) {
      no(`it sends the set-up key ${fromRaw(state.keyFund, 9)} SOL; ${state.policies.length} policies take no more than ${fromRaw(bound, 9)}`);
    }
  } else if (state.policies.length && state.policyIn.at(-1) !== state.fundedIn) no('the deposit is not in the same transaction as the last policy');
  if (helper && !state.funding.length && !state.opens.length) return;
  const ctx = { rules: ruleIndex(pins.rules), names: namesOf(expect, pins), symbols: symbolsOf(pins), vault: expect.vault,
    venueNames: {} };
  const gas = state.funding.filter(ix => {
    const m = describe(ix, ctx).move;
    return m.kind === 'sol' && m.to === pins.payer;
  });
  if (gas.length !== 1) return no(`${gas.length} payments to the bot's gas key; the set-up pays it once`);
  const g = describe(gas[0], ctx);
  lines.push(`Funding${helper ? ', sent last, once the set-up key is off your vault' : ''}: ${g.text}`);
  if (g.move.from !== expect.owner) no('the gas does not come from your wallet');
  if (g.move.raw !== BigInt(expect.gasRaw)) no(`it pays the bot ${fromRaw(g.move.raw, 9)} SOL, not the ${fromRaw(BigInt(expect.gasRaw), 9)} SOL gas shown`);
  const rest = [...state.opens, ...state.funding.filter(ix => ix !== gas[0])];
  const dl = [];
  deposit(rest, expect, pins, ctx, dl, no);
  lines.push(...dl.map(l => `Funding: ${l}`));
}

// approve with a deposit (a re-grant with more capital): the policy updates
// (or creations) first, the deposit after them in the last transaction.
function approvePart(outer, expect, pins, ctx, lines, no) {
  const { index, count, state } = expect.part;
  const rules = outer.filter(ix => ix.programId === SQUADS);
  const rest = outer.slice(rules.length);
  if (outer.slice(0, rules.length).some(ix => ix.programId !== SQUADS)) no('money moves before the rules are approved');
  if (rules.length) {
    if (state.funding.length) no('a rule after the deposit');
    policyUpdates(rules, expect, pins, ctx, lines, no, pins.policyKey);
  }
  if (rest.length) {
    if (index !== count - 1) no('money moves before the last transaction');
    state.funding.push(...rest);
  }
}

function approveWhole(state, expect, pins, lines, no) {
  if (!expect.deposit) { if (state.funding.length) no('it moves money; an approval moves none'); return; }
  const ctx = { rules: ruleIndex(pins.rules), names: namesOf(expect, pins), symbols: symbolsOf(pins), vault: expect.vault, venueNames: {} };
  if (!state.funding.length) return no('the deposit is missing');
  const dl = [];
  deposit(state.funding, { ...expect, ...expect.deposit }, pins, ctx, dl, no);
  lines.push(...dl.map(l => `Deposit: ${l}`));
}

// revoke: every policy of the set names you alone (revoke above), in one approval
const SET_CHECKS = { setup: setupPart, approve: approvePart, revoke };
const SET_FINALS = { setup: setupWhole, approve: approveWhole };

// A venue instruction of an owner's close: one the venue's exit list names
// (a venue rule: the shared rules are read as transfers, accounts and closes
// before), on an approved pool, paying only the vault. The exit rules pin
// only these roles; any other pin refuses.
function exitPins(ix, rule, expect, pins, no) {
  if (!EXIT_INSTRUCTIONS[rule.venue].includes(rule.name)) return no(`${rule.name} is not a way out of a position`);
  // the vault's token accounts: its associated ones of the mints the instruction names
  const vaultAtas = new Set(ix.keys.flatMap(k => [TOKEN, TOKEN_2022].map(p => ata(expect.vault, k.pubkey, p))));
  const pools = pins.pools[rule.venue];
  for (const p of rule.pins) {
    const key = ix.keys[p.index]?.pubkey;
    if (!key) { no(`${rule.name}: account ${p.index} is missing`); continue; }
    const ok = p.role === 'vault' || p.role === 'vaultOrPayer' ? key === expect.vault
      : p.role === 'vaultToken' ? vaultAtas.has(key)
        : p.role === 'pool' ? pools.includes(key)
          : p.role === 'pda' ? pools.some(pool => poolPda(p.seeds, pool, new PublicKey(rule.program)).toBase58() === key)
            : false;
    if (!ok) no(`${rule.name}: account ${p.index} (${p.role}) is ${short(key)}, not your vault's`);
  }
}

// Each transaction of a revoke or an approve: policy updates of this vault's
// policies only, signed by you, `signer` the one key the policy will name,
// the rules exactly what rules.json compiles to for this vault.
function policyUpdates(outer, expect, pins, ctx, lines, no, signer) {
  const venues = Object.keys(expect.policies ?? {});
  const want = Object.fromEntries(venues.flatMap(v => {
    for (const p of expect.pools[v] ?? []) if (!(pins.pools[v] ?? []).includes(p)) no(`the pool ${short(p)} is not the ${v} pool the rules approve`);
    const compiled = expectedPolicies(v, expect, pins);
    if (compiled.length !== expect.policies[v].length) no(`the ${v} rules make ${compiled.length} policies; your vault has ${expect.policies[v].length}`);
    return expect.policies[v].map((addr, i) => [addr, { venue: v, policy: compiled[i]?.policy }]);
  }));
  const settingsIxs = outer.filter(ix => ix.programId === SQUADS);
  if (settingsIxs.length !== outer.length || !outer.length) return no('it does more than update your vault\'s policies');
  const who = signer === expect.owner ? 'only you' : `the bot's rules key ${short(signer)}`;
  for (const ix of outer) {
    if (!Buffer.from(ix.data).subarray(0, 8).equals(sqDisc('settings'))) { no('a vault instruction other than a policy update'); continue; }
    if (ix.keys[0]?.pubkey !== expect.settings) no('it changes another vault');
    if (ix.keys[1]?.pubkey !== expect.owner) no('the rent is paid by someone else than you');
    const s = readSettingsActions(ix.data, expect.settings);
    if (s.signers !== 1 || ix.keys[4]?.pubkey !== expect.owner) no('it is not signed by your wallet alone');
    for (const a of s.actions) {
      if (a.kind === 'addSigner' || a.kind === 'removeSigner') { no(`it changes your vault's signers (${short(a.key)})`); continue; }
      const w = want[a.address];
      if (!w) { no(`policy ${short(a.address)} is not one of your vault's`); continue; }
      // a new policy (a venue staged on the vault) only names the bot: a revoke creates none
      if (a.kind === 'policyCreate' && signer !== pins.policyKey) no(`a revoke creates no policy (${short(a.address)})`);
      if (a.signers.length !== 1 || a.signers[0].key !== signer || a.signers[0].permissions !== sq.PERMISSION.all) {
        no(`policy ${short(a.address)} would name ${a.signers.map(s => short(s.key)).join(', ')}, not ${who}`);
      }
      if (a.threshold !== 1 || a.timeLock !== 0) no('a threshold or time lock other than the rules\'');
      if (a.policy.hooks || a.policy.spendingLimits || a.policy.accountIndex !== 0) no('hooks, spending limits or another vault account');
      const same = w.policy && Buffer.from(a.bytes).equals(sq.encodeProgramInteraction(w.policy));
      lines.push(`${a.kind === 'policyCreate' ? 'New policy' : 'Policy'} ${short(a.address)} (${w.venue}): signer ${who}; ${a.policy.constraints.length} allowed instructions${same ? ', exactly the rules in git' : ''}:`);
      for (const c of a.policy.constraints) lines.push(`  · ${describeConstraint(c, ctx)}`);
      if (!same) no(`policy ${short(a.address)} differs from the rules in git (rules.json) for your vault`);
    }
  }
}

