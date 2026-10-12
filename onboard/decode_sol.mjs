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
//
// expect.kind: payment | deposit | withdraw | revoke | approve | close.
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

// The settings actions of execute_settings_transaction_sync. Only policy
// updates are read; any other action is reported by kind.
function readSettingsActions(data) {
  const r = new Reader(data);
  r.bytes(8);
  const signers = r.u8();
  const actions = r.vec(r => {
    const kind = r.u8();
    if (kind !== 8) return { kind: `settings action ${kind}`, unknown: true };
    const address = r.key();
    const policySigners = r.vec(r => ({ key: r.key(), permissions: r.u8() }));
    const threshold = r.u16(), timeLock = r.u32(), payload = r.u8();
    if (payload !== 3) return { kind: 'policyUpdate', address, unknown: true };
    const start = r.o;
    const policy = readPolicy(r);
    const bytes = r.b.subarray(start, r.o);
    const expiration = r.option(r => r.u8());
    return { kind: 'policyUpdate', address, signers: policySigners, threshold, timeLock, policy, bytes: Buffer.from(bytes), expiration };
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
  if ((ix.programId === TOKEN || ix.programId === TOKEN_2022) && d[0] === 17 && d.length === 1) {
    return { text: `Sync the wrapped SOL account ${name(k[0])}`, move: { kind: 'sync', account: k[0] } };
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
  if (rule) return { text: `${ctx.venueNames[rule.venue] ?? 'Program'}: ${rule.name}`, move: { kind: 'venue', rule } };
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
  if (pins.payer) n[pins.payer] = 'the bot\'s gas key';
  if (pins.policyKey) n[pins.policyKey] = 'the bot\'s rules key';
  for (const [venue, list] of Object.entries(pins.pools ?? {})) for (const p of list) n[p] = `the ${venue} pool`;
  const mints = symbolsOf(pins);
  for (const w of expect.payoutWallets ?? []) {
    const who = w === pins.operator ? 'the operator\'s fee share' : w === expect.owner ? 'your wallet' : 'your profit wallet';
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
  if (tx.feePayer?.toBase58() !== owner) no(`the fee payer is ${short(tx.feePayer?.toBase58())}, not your wallet`);
  const msg = tx.compileMessage();
  const signers = msg.accountKeys.slice(0, msg.header.numRequiredSignatures).map(k => k.toBase58());
  if (signers.length !== 1 || signers[0] !== owner) no(`it asks for the signatures of ${signers.map(short).join(', ')}; only your wallet may sign`);
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

  const check = CHECKS[expect.kind];
  if (!check) return { ok: false, lines, problems: [`nothing to check a ${expect.kind} against`] };
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

// the gas payment: one transfer from the wallet to the bot's gas key, the signed amount
function payment(outer, expect, pins, ctx, lines, no) {
  if (outer.length !== 1) return no(`${outer.length} instructions; a gas payment is one transfer`);
  const d = describe(outer[0], ctx);
  lines.push(d.text);
  const m = d.move;
  if (m.kind !== 'sol') return no('it is not a SOL transfer');
  if (m.from !== expect.owner) no('the SOL does not come from your wallet');
  if (m.to !== pins.payer) no(`the SOL goes to ${short(m.to)}, not the bot's gas key ${short(pins.payer)}`);
  if (m.raw !== BigInt(expect.raw)) no(`it pays ${fromRaw(m.raw, 9)} SOL, not the ${fromRaw(BigInt(expect.raw), 9)} SOL you signed for`);
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
      if (m.kind === 'sync') return;
      no(`an instruction a close does not need: ${descs[i].text}`);
    });
  }
  if (!venueIxs) no('it closes no position');
}

const CHECKS = { payment, deposit, withdraw, revoke, approve, close };

// A venue instruction of an owner's close: one the venue's exit list names,
// on an approved pool, paying only the vault.
function exitPins(ix, rule, expect, pins, no) {
  if (!(EXIT_INSTRUCTIONS[rule.venue] ?? []).includes(rule.name)) return no(`${rule.name} is not a way out of a position`);
  const mints = ix.keys.map(k => k.pubkey);
  const vaultAtas = new Set(mints.flatMap(m => [TOKEN, TOKEN_2022].map(p => { try { return ata(expect.vault, m, p); } catch { return null; } })));
  for (const p of rule.pins) {
    const key = ix.keys[p.index]?.pubkey;
    if (!key) { no(`${rule.name}: account ${p.index} is missing`); continue; }
    const ok = p.role === 'vault' || p.role === 'vaultOrPayer' ? key === expect.vault
      : p.role === 'vaultToken' || p.role === 'vaultWsol' ? vaultAtas.has(key)
        : p.role === 'pool' ? (pins.pools[rule.venue] ?? []).includes(key)
          : p.role === 'pda' ? (pins.pools[rule.venue] ?? []).some(pool => poolPda(p.seeds, pool, new PublicKey(rule.program)).toBase58() === key)
            : p.role === 'none' ? key === rule.program : false;
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
    const s = readSettingsActions(ix.data);
    if (s.signers !== 1 || ix.keys[4]?.pubkey !== expect.owner) no('it is not signed by your wallet alone');
    for (const a of s.actions) {
      if (a.kind !== 'policyUpdate' || a.unknown) { no(`a change other than a policy update (${a.kind})`); continue; }
      const w = want[a.address];
      if (!w) { no(`policy ${short(a.address)} is not one of your vault's`); continue; }
      if (a.signers.length !== 1 || a.signers[0].key !== signer || a.signers[0].permissions !== sq.PERMISSION.all) {
        no(`policy ${short(a.address)} would name ${a.signers.map(s => short(s.key)).join(', ')}, not ${who}`);
      }
      if (a.threshold !== 1 || a.timeLock !== 0 || a.expiration !== null) no('a threshold, time lock or expiry other than the rules\'');
      if (a.policy.hooks || a.policy.spendingLimits || a.policy.accountIndex !== 0) no('hooks, spending limits or another vault account');
      const same = w.policy && Buffer.from(a.bytes).equals(sq.encodeProgramInteraction(w.policy));
      lines.push(`Policy ${short(a.address)} (${w.venue}): signer ${who}; ${a.policy.constraints.length} allowed instructions${same ? ', exactly the rules in git' : ''}:`);
      for (const c of a.policy.constraints) lines.push(`  · ${describeConstraint(c, ctx)}`);
      if (!same) no(`policy ${short(a.address)} differs from the rules in git (rules.json) for your vault`);
      delete want[a.address];
    }
  }
  const left = Object.keys(want);
  if (left.length && expect.complete) no(`${left.length} of your vault's policies are not in it`);
}

