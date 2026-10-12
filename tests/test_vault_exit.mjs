// The owners' exit (vault/exit.mjs) and the exact withdraw amounts
// (vault/cli.mjs), without a chain: every refusal, every size limit, every
// ordering. tests/test_vault_exit_fork.mjs runs the exits for real.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import fc from 'fast-check';
import { RULES, instructionsOf, poolPda } from '../chains/solana/vault/policy.mjs';
import { EXIT_INSTRUCTIONS as RULES_LIST } from '../chains/solana/vault/exit_rules.mjs';
import { EXIT_INSTRUCTIONS, EXIT_PRICE_BPS, NotAnExit, TX_BYTES, checkExitIxs, ownerCloseMessages, packMessages, rentToVault,
  sortOut } from '../chains/solana/vault/exit.mjs';
import { formatAmount, parseAmount } from '../chains/solana/vault/cli.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { ComputeBudgetProgram, Keypair, Message, PublicKey, SystemProgram, Transaction, TransactionInstruction } = require('@solana/web3.js');
const spl = require('@solana/spl-token');
const bs58 = require('bs58').default;

const key = () => Keypair.generate().publicKey;
const USDC = new PublicKey(SOLANA.usdc_mint);
const SOL = spl.NATIVE_MINT;
const ataOf = (mint, who, program = spl.TOKEN_PROGRAM_ID) => spl.getAssociatedTokenAddressSync(mint, who, true, program);
const VENUES = Object.keys(RULES.venues);

// --- amounts -------------------------------------------------------------------

test('an amount parses exactly: digits, a point, at most `decimals` digits after it', () => {
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 24n }), fc.integer({ min: 0, max: 18 }), fc.integer({ min: 0, max: 18 }),
    fc.bigInt({ min: 0n, max: 10n ** 18n - 1n }), (whole, decimals, places, digits) => {
      const n = Math.min(places, decimals);
      const frac = (digits % 10n ** BigInt(n)).toString().padStart(n, '0');
      const text = n ? `${whole}.${frac}` : `${whole}`;
      assert.equal(parseAmount(text, decimals), whole * 10n ** BigInt(decimals) + BigInt(n ? frac.padEnd(decimals, '0') : 0));
    }));
});

test('an amount with more decimals than the token has, a sign, an exponent or anything else is refused', () => {
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 12n }), fc.integer({ min: 0, max: 12 }), (whole, decimals) => {
    assert.throws(() => parseAmount(`${whole}.${'1'.repeat(decimals + 1)}`, decimals), new RegExp(`more than ${decimals} decimals`));
    for (const bad of [`-${whole}`, `+${whole}`, `${whole}e3`, `${whole}E-2`, ` ${whole}`, `${whole} `, `${whole}.`, `.${whole}`,
      `0x${whole}`, `${whole},5`, '', 'NaN', 'Infinity']) {
      assert.throws(() => parseAmount(bad, decimals), /is not an amount/, bad);
    }
  }));
  assert.throws(() => parseAmount(undefined, 9), /is not an amount/);
  assert.equal(parseAmount(0, 6), 0n, 'a number is read as its digits');
  assert.equal(parseAmount('0.000000001', 9), 1n);
  assert.equal(parseAmount('007.50', 6), 7_500_000n);
  // what floats get wrong
  assert.equal(parseAmount('0.29', 6), 290_000n);
  assert.equal(parseAmount('9007199254740993', 0), 9007199254740993n);
  assert.equal(parseAmount('1.000000000000000001', 18), 10n ** 18n + 1n);
});

test('an amount prints exactly and parses back to itself', () => {
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 30n }), fc.integer({ min: 0, max: 18 }), (raw, decimals) => {
    const text = formatAmount(raw, decimals);
    assert.match(text, /^\d+(\.\d*[1-9])?$/);
    assert.equal(parseAmount(text, decimals), raw);
  }));
  assert.equal(formatAmount(500_000_000n, 9), '0.5');
  assert.equal(formatAmount(2_000_000n, 6), '2');
  assert.equal(formatAmount(1n, 6), '0.000001');
  assert.equal(formatAmount(0, 9), '0');
  assert.throws(() => formatAmount(-1n, 6), /negative/);
});

// --- the exit's instructions -----------------------------------------------------

test('EXIT_INSTRUCTIONS: the browser list is the one exit.mjs enforces, every name a rule of its venue', () => {
  assert.equal(EXIT_INSTRUCTIONS, RULES_LIST);
  assert.deepEqual(Object.keys(EXIT_INSTRUCTIONS).sort(), [...VENUES].sort());
  for (const [venue, names] of Object.entries(EXIT_INSTRUCTIONS)) {
    const rules = instructionsOf(venue).map(r => r.name);
    for (const n of names) assert.ok(rules.includes(n), `${venue}: ${n}`);
    // no deposit, open, or swap: an exit only takes out
    for (const n of names) assert.doesNotMatch(n, /open|increase|swap|rebalance|initialize/, `${venue}: ${n}`);
  }
  assert.ok(Object.isFrozen(EXIT_INSTRUCTIONS) && Object.values(EXIT_INSTRUCTIONS).every(Object.isFrozen));
});

// One instruction of `rule` whose every pin holds for `ctx`: the vault, the
// vault's token accounts, the approved pool.
function ixOf(venue, rule, ctx) {
  const program = new PublicKey(RULES.venues[venue].program);
  const width = Math.max(...rule.pins.map(p => p.index)) + 3;
  const keys = Array.from({ length: width }, () => ({ pubkey: key(), isSigner: false, isWritable: true }));
  for (const p of rule.pins) {
    const pubkey = { vault: ctx.vault, vaultOrPayer: ctx.vault, vaultToken: ctx.tokenAccounts[p.index % ctx.tokenAccounts.length],
      pool: ctx.pools[0], pda: p.seeds && poolPda(p.seeds, ctx.pools[0], program), none: program }[p.role];
    keys[p.index] = { pubkey, isSigner: p.role === 'vault', isWritable: true };
  }
  return new TransactionInstruction({ programId: program, keys, data: Buffer.concat([Buffer.from(rule.discriminator, 'hex'), Buffer.alloc(8)]) });
}

function context() {
  const vault = key();
  return { vault, pools: [key()], tokenAccounts: [ataOf(SOL, vault), ataOf(USDC, vault), ataOf(key(), vault, spl.TOKEN_2022_PROGRAM_ID)] };
}
const unwrapOf = (vault) => spl.createCloseAccountInstruction(ataOf(SOL, vault), vault, vault);

test('checkExitIxs: each venue\'s exit instructions pass, and the unwrap into the vault', () => {
  for (const venue of VENUES) {
    const ctx = context();
    const rules = instructionsOf(venue).filter(r => EXIT_INSTRUCTIONS[venue].includes(r.name));
    assert.equal(rules.length, EXIT_INSTRUCTIONS[venue].length);
    const ixs = [...rules.map(r => ixOf(venue, r, ctx)), unwrapOf(ctx.vault)];
    assert.deepEqual(checkExitIxs(venue, ixs, ctx), [...EXIT_INSTRUCTIONS[venue], 'unwrap SOL']);
    // pools are optional: without them any pool passes
    assert.deepEqual(checkExitIxs(venue, ixs, { ...ctx, pools: null }), [...EXIT_INSTRUCTIONS[venue], 'unwrap SOL']);
  }
});

test('checkExitIxs refuses every other rule of the venue: no open, deposit or swap runs in an exit', () => {
  for (const venue of VENUES) {
    const ctx = context();
    for (const rule of instructionsOf(venue).filter(r => !EXIT_INSTRUCTIONS[venue].includes(r.name))) {
      assert.throws(() => checkExitIxs(venue, [ixOf(venue, rule, ctx)], ctx), (e) => e instanceof NotAnExit && /is none of/.test(e.message), rule.name);
    }
  }
});

test('checkExitIxs refuses rent or ownership off the vault, a stranger\'s token account, another pool, another program', () => {
  fc.assert(fc.property(fc.constantFrom(...VENUES), fc.nat(), fc.nat(), (venue, r, p) => {
    const ctx = context();
    const rules = instructionsOf(venue).filter(x => EXIT_INSTRUCTIONS[venue].includes(x.name));
    const rule = rules[r % rules.length];
    const pins = rule.pins.filter(x => ['vault', 'vaultOrPayer', 'vaultToken', 'pool'].includes(x.role));
    const pin = pins[p % pins.length];
    const ix = ixOf(venue, rule, ctx);
    ix.keys[pin.index] = { ...ix.keys[pin.index], pubkey: pin.role === 'vaultToken' ? ataOf(USDC, key()) : key() };
    assert.throws(() => checkExitIxs(venue, [ix], ctx), (e) => e instanceof NotAnExit && e.message.includes(`account ${pin.index} is`));
    // and a missing account
    const short = ixOf(venue, rule, ctx);
    short.keys = short.keys.slice(0, pin.index);
    assert.throws(() => checkExitIxs(venue, [short], ctx), /is missing/);
  }), { numRuns: 200 });
  const ctx = context();
  for (const venue of VENUES) {
    assert.throws(() => checkExitIxs(venue, [SystemProgram.transfer({ fromPubkey: ctx.vault, toPubkey: key(), lamports: 1 })], ctx),
      /is not .*'s/);
    // a token account closed anywhere else, or another account closed
    assert.throws(() => checkExitIxs(venue, [spl.createCloseAccountInstruction(ataOf(SOL, ctx.vault), key(), ctx.vault)], ctx), NotAnExit);
    assert.throws(() => checkExitIxs(venue, [spl.createCloseAccountInstruction(ataOf(USDC, ctx.vault), ctx.vault, ctx.vault)], ctx), NotAnExit);
    // another venue's program
    const other = VENUES.find(v => RULES.venues[v].program !== RULES.venues[venue].program);
    const rule = instructionsOf(other).find(x => EXIT_INSTRUCTIONS[other].includes(x.name));
    assert.throws(() => checkExitIxs(venue, [ixOf(other, rule, ctx)], ctx), NotAnExit);
  }
  assert.throws(() => checkExitIxs('pancakeswap', [], ctx), /has no owner exit/);
  // another token instruction is not taken for the unwrap; nor is another program's look-alike
  const transfer = spl.createTransferInstruction(ataOf(SOL, ctx.vault), ataOf(SOL, key()), ctx.vault, 1n);
  assert.throws(() => checkExitIxs('orca', [transfer], ctx), /program Tokenkeg\w+ is not orca's/);
  const lookalike = new TransactionInstruction({ programId: key(), keys: unwrapOf(ctx.vault).keys, data: Buffer.from([9]) });
  assert.throws(() => checkExitIxs('orca', [lookalike], ctx), /is not orca's/);
});

test('rentToVault: every rent receiver an exit names is the vault; nothing else changes', () => {
  for (const venue of VENUES) {
    const ctx = context(), payer = key();
    for (const rule of instructionsOf(venue).filter(r => EXIT_INSTRUCTIONS[venue].includes(r.name))) {
      const ix = ixOf(venue, rule, ctx);
      const at = rule.pins.filter(p => p.role === 'vaultOrPayer').map(p => p.index);
      for (const i of at) ix.keys[i] = { ...ix.keys[i], pubkey: payer };
      const [out] = rentToVault(venue, [ix], ctx.vault);
      out.keys.forEach((k, i) => assert.ok(k.pubkey.equals(at.includes(i) ? ctx.vault : ix.keys[i].pubkey), `${rule.name} ${i}`));
      assert.ok(out.data.equals(ix.data) && out.programId.equals(ix.programId));
      if (at.length) assert.throws(() => checkExitIxs(venue, [ix], ctx), NotAnExit); else assert.equal(out, ix);
    }
    // another program's instruction is left alone
    const t = SystemProgram.transfer({ fromPubkey: ctx.vault, toPubkey: payer, lamports: 1 });
    assert.equal(rentToVault(venue, [t], ctx.vault)[0], t);
  }
  assert.ok(Object.values(EXIT_INSTRUCTIONS).flat().length > 0);
  // the closes that refund rent: DLMM's two, Orca's one
  const refunds = VENUES.flatMap(v => instructionsOf(v).filter(r => EXIT_INSTRUCTIONS[v].includes(r.name) && r.pins.some(p => p.role === 'vaultOrPayer'))
    .map(r => `${v}:${r.name}`));
  assert.deepEqual(refunds, ['meteora-dlmm:close_position2', 'meteora-dlmm:close_position_if_empty', 'orca:close_position_with_token_extensions']);
});

test('sortOut: the owner pays the vault\'s accounts, compute budget goes, the unwrap is kept for the end', () => {
  const vault = key(), owner = key(), mint = key();
  const venueIx = new TransactionInstruction({ programId: key(), keys: [], data: Buffer.from([1]) });
  const out = sortOut([
    ComputeBudgetProgram.setComputeUnitLimit({ units: 1 }),
    spl.createAssociatedTokenAccountIdempotentInstruction(vault, ataOf(USDC, vault), vault, USDC),
    spl.createAssociatedTokenAccountInstruction(vault, ataOf(mint, vault, spl.TOKEN_2022_PROGRAM_ID), vault, mint, spl.TOKEN_2022_PROGRAM_ID),
    venueIx, unwrapOf(vault),
  ], vault, owner);
  assert.equal(out.unwrap, true);
  assert.deepEqual(out.inner, [venueIx]);
  assert.equal(out.creates.length, 2);
  for (const [c, [m, program]] of out.creates.map((c, i) => [c, [[USDC, spl.TOKEN_PROGRAM_ID], [mint, spl.TOKEN_2022_PROGRAM_ID]][i]])) {
    assert.ok(c.keys[0].pubkey.equals(owner) && c.keys[0].isSigner, 'the owner pays');
    assert.ok(c.keys[1].pubkey.equals(ataOf(m, vault, program)) && c.keys[2].pubkey.equals(vault) && c.keys[3].pubkey.equals(m));
    assert.ok(c.keys[5].pubkey.equals(program));
    assert.deepEqual([...c.data], [1], 'idempotent');
  }
  assert.equal(sortOut([venueIx], vault, owner).unwrap, false);
  // another WSOL account closed is the venue's, not the unwrap
  assert.equal(sortOut([unwrapOf(key())], vault, owner).inner.length, 1);
  // an account for someone else, or a non-associated address, is refused
  const stranger = key();
  assert.throws(() => sortOut([spl.createAssociatedTokenAccountIdempotentInstruction(vault, ataOf(USDC, stranger), stranger, USDC)], vault, owner), NotAnExit);
  const forged = spl.createAssociatedTokenAccountIdempotentInstruction(vault, ataOf(USDC, vault), vault, USDC);
  forged.keys[1] = { ...forged.keys[1], pubkey: key() };
  assert.throws(() => sortOut([forged], vault, owner), /not the vault's own/);
});

// --- packing ---------------------------------------------------------------------

const decoded = (b64) => Message.from(Buffer.from(b64, 'base64'));
// How many instructions a message's owner call runs: the u8 that opens its
// inner message, after the discriminator (8), account index, signer count,
// payload kind (1 each) and the bytes' length (u32).
const innerCount = (m) => bs58.decode(m.instructions.at(-1).data)[15];
// The instructions a message's owner call runs as the vault, decoded:
// [{programId, keys, data}]. Inner account indexes count from the first
// account after the call's settings, program and one signer.
function innerIxs(m) {
  const call = m.instructions.at(-1);
  if (m.accountKeys[call.programIdIndex].toBase58() !== SOLANA.programs.squads_smart_account) return [];
  const data = Buffer.from(bs58.decode(call.data));
  const meta = (i) => m.accountKeys[call.accounts[3 + i]];
  const out = [];
  let o = 16;
  for (let n = 0; n < data[15]; n++) {
    const program = data[o++], count = data[o++];
    const keys = [...data.subarray(o, o + count)].map(i => ({ pubkey: meta(i) }));
    o += count;
    const len = data.readUInt16LE(o); o += 2;
    out.push({ programId: meta(program), keys, data: data.subarray(o, o + len) });
    o += len;
  }
  assert.equal(o, data.length);
  return out;
}
const signedSize = (b64, signer) => {
  const tx = Transaction.populate(decoded(b64));
  tx.sign(signer);
  return tx.serialize().length;
};

test('packMessages: every message fits a signed transaction, steps keep their order, the owner alone signs', () => {
  const owner = Keypair.generate(), vault = key(), settings = key();
  fc.assert(fc.property(fc.array(fc.constantFrom('create', 'transfer', 'close'), { minLength: 1, maxLength: 40 }), (kinds) => {
    const steps = kinds.map((k, i) => {
      const mint = key();
      if (k === 'create') return { outside: spl.createAssociatedTokenAccountIdempotentInstruction(owner.publicKey, ataOf(mint, vault), vault, mint) };
      if (k === 'transfer') return { inside: SystemProgram.transfer({ fromPubkey: vault, toPubkey: key(), lamports: i + 1 }) };
      return { inside: spl.createCloseAccountInstruction(ataOf(mint, vault), owner.publicKey, vault) };
    });
    const messages = packMessages(steps, { settings, vault, owner: owner.publicKey });
    for (const b64 of messages) {
      assert.ok(signedSize(b64, owner) <= TX_BYTES);
      const m = decoded(b64);
      assert.equal(m.header.numRequiredSignatures, 1);
      assert.ok(m.accountKeys[0].equals(owner.publicKey));
      assert.equal(m.recentBlockhash, PublicKey.default.toBase58());
      const programs = m.instructions.map(ix => m.accountKeys[ix.programIdIndex].toBase58());
      const outside = programs.filter(p => p === SOLANA.programs.associated_token).length;
      const calls = programs.filter(p => p === SOLANA.programs.squads_smart_account).length;
      assert.ok(calls <= 1);
      // a limit only with a call, first; the creates before the call
      assert.equal(programs.filter(p => p === ComputeBudgetProgram.programId.toBase58()).length, calls);
      if (calls) assert.equal(programs.at(-1), SOLANA.programs.squads_smart_account);
      assert.equal(outside + calls + programs.filter(p => p === ComputeBudgetProgram.programId.toBase58()).length, programs.length);
    }
    // each message holds a contiguous run of the steps: creates of a run first, then its inner ones
    let i = 0;
    for (const b64 of messages) {
      const m = decoded(b64);
      const programs = m.instructions.map(ix => m.accountKeys[ix.programIdIndex].toBase58());
      const outside = programs.filter(p => p === SOLANA.programs.associated_token).length;
      const call = programs.includes(SOLANA.programs.squads_smart_account) ? innerCount(m) : 0;
      const run = kinds.slice(i, i + outside + call);
      assert.equal(run.filter(k => k === 'create').length, outside);
      i += outside + call;
    }
    assert.equal(i, kinds.length, 'every step is in one message');
  }), { numRuns: 60 });
});

test('packMessages fills each message: a step moves on only when it does not fit; one too big alone is refused', () => {
  const owner = key(), vault = key(), settings = key();
  const transfer = () => ({ inside: SystemProgram.transfer({ fromPubkey: vault, toPubkey: key(), lamports: 1 }) });
  const steps = Array.from({ length: 60 }, transfer);
  const messages = packMessages(steps, { settings, vault, owner });
  assert.ok(messages.length > 1);
  // no two neighbours would have fit together
  for (let i = 0; i + 1 < messages.length; i++) {
    const n = innerCount(decoded(messages[i]));
    const used = messages.slice(0, i).reduce((s, b) => s + innerCount(decoded(b)), 0);
    assert.throws(() => packMessages(steps.slice(used, used + n + 1), { settings, vault, owner, limit: 0 }));
    assert.equal(packMessages(steps.slice(used, used + n + 1), { settings, vault, owner }).length, 2, 'the next step did not fit');
  }
  assert.deepEqual(packMessages([], { settings, vault, owner }), []);
  const big = { inside: new TransactionInstruction({ programId: key(), keys: [], data: Buffer.alloc(1100) }) };
  assert.throws(() => packMessages([big], { settings, vault, owner }), /does not fit a 1232-byte transaction/);
  assert.throws(() => packMessages([transfer(), big], { settings, vault, owner }), /does not fit/);
  const half = { inside: new TransactionInstruction({ programId: key(), keys: [], data: Buffer.alloc(900) }) };
  assert.equal(packMessages([half], { settings, vault, owner }).length, 1);
  assert.throws(() => packMessages([half], { settings, vault, owner, limit: 900 }), /900-byte/);
  // a message of exactly the limit fits
  const owner2 = Keypair.generate();
  const exact = signedSize(packMessages([half], { settings, vault, owner: owner2.publicKey })[0], owner2);
  assert.equal(packMessages([half], { settings, vault, owner: owner2.publicKey, limit: exact }).length, 1);
  assert.throws(() => packMessages([half], { settings, vault, owner: owner2.publicKey, limit: exact - 1 }), /does not fit/);
});

// --- ownerCloseMessages, with the venue side replaced ---------------------------

// A venue side that holds one position whose close the SDK built as `ixs(ctx)`.
const builderOf = (ixs, mints) => ({
  async open(_connection, { vault, pools }) {
    const ctx = { vault, pools: pools.map(p => new PublicKey(p)), tokenAccounts: mints.map(m => ataOf(m, vault)) };
    return { mints: mints.map(mint => ({ mint, program: spl.TOKEN_PROGRAM_ID })),
      positions: [{ pool: pools[0], position: 'P', nftMint: 'N', liquidity: '7', build: async () => ixs(ctx) }] };
  },
});

test('ownerCloseMessages refuses an instruction not in EXIT_INSTRUCTIONS before building anything', async () => {
  const owner = Keypair.generate(), settings = key(), vault = key(), pool = key();
  for (const venue of VENUES) {
    const exits = instructionsOf(venue).filter(r => EXIT_INSTRUCTIONS[venue].includes(r.name));
    for (const extra of instructionsOf(venue).filter(r => !EXIT_INSTRUCTIONS[venue].includes(r.name))) {
      const builder = builderOf((ctx) => [...exits.map(r => ixOf(venue, r, ctx)), ixOf(venue, extra, ctx)], [SOL, USDC]);
      await assert.rejects(ownerCloseMessages(null, { settings, vault, owner: owner.publicKey, venue, pools: [pool], builder }), NotAnExit, extra.name);
    }
    // a transfer out of the vault hidden in what the SDK built
    const theft = builderOf((ctx) => [...exits.map(r => ixOf(venue, r, ctx)), SystemProgram.transfer({ fromPubkey: vault, toPubkey: key(), lamports: 1 })], [SOL, USDC]);
    await assert.rejects(ownerCloseMessages(null, { settings, vault, owner: owner.publicKey, venue, pools: [pool], builder: theft }), NotAnExit);
    // fees to a token account of a mint the pool does not pay: not the vault's
    const odd = builderOf((ctx) => exits.map(r => ixOf(venue, r, { ...ctx, tokenAccounts: [ataOf(key(), vault)] })), [SOL, USDC]);
    if (exits.some(r => r.pins.some(p => p.role === 'vaultToken'))) {
      await assert.rejects(ownerCloseMessages(null, { settings, vault, owner: owner.publicKey, venue, pools: [pool], builder: odd }), /not a token account of the vault/);
    }
  }
});

test('ownerCloseMessages: the exit in order, rent to the vault, the WSOL account made sure of and closed last', async () => {
  const owner = Keypair.generate(), settings = key(), vault = key(), pool = key(), payer = key();
  for (const venue of VENUES) {
    const exits = instructionsOf(venue).filter(r => EXIT_INSTRUCTIONS[venue].includes(r.name));
    const builder = builderOf((ctx) => [ComputeBudgetProgram.setComputeUnitLimit({ units: 5 }),
      spl.createAssociatedTokenAccountIdempotentInstruction(vault, ataOf(USDC, vault), vault, USDC),
      ...exits.map(r => {
        const ix = ixOf(venue, r, ctx);
        for (const p of r.pins.filter(x => x.role === 'vaultOrPayer')) ix.keys[p.index] = { ...ix.keys[p.index], pubkey: payer };
        return ix;
      })], [SOL, USDC]);
    const out = await ownerCloseMessages(null, { settings, vault, owner: owner.publicKey, venue, pools: [pool], builder });
    assert.deepEqual(out.positions, [{ venue, pool: pool.toBase58(), position: 'P', nftMint: 'N', liquidity: '7' }]);
    const noun = out.messages.length === 1 ? 'transaction' : 'transactions';
    assert.match(out.summary, new RegExp(`^close 1 ${venue} position of the vault ${vault.toBase58()} into the vault .*: ${out.messages.length} ${noun}, sign and send in order$`));
    const all = out.messages.map(decoded);
    for (const b64 of out.messages) assert.ok(signedSize(b64, owner) <= TX_BYTES);
    const keys = all.flatMap(m => m.accountKeys.map(String));
    assert.ok(!keys.includes(payer.toBase58()), 'no rent to the bot\'s payer');
    // the creates: USDC (the SDK's) and WSOL (for the unwrap), the owner paying, in the first message
    const first = all[0];
    const creates = first.instructions.filter(ix => first.accountKeys[ix.programIdIndex].toBase58() === SOLANA.programs.associated_token);
    assert.deepEqual(creates.map(ix => first.accountKeys[ix.accounts[1]].toBase58()).sort(),
      [ataOf(SOL, vault), ataOf(USDC, vault)].map(String).sort());
    for (const ix of creates) assert.equal(first.accountKeys[ix.accounts[0]].toBase58(), owner.publicKey.toBase58());
    // the vault runs the exit in the SDK's order, rent to the vault, then the unwrap of its WSOL into it
    const inner = all.flatMap(innerIxs);
    assert.deepEqual(checkExitIxs(venue, inner, { vault, tokenAccounts: [ataOf(SOL, vault), ataOf(USDC, vault)], pools: [pool] }),
      [...EXIT_INSTRUCTIONS[venue], 'unwrap SOL']);
    const unwrap = inner.at(-1);
    assert.deepEqual([unwrap.programId, unwrap.keys[0].pubkey, unwrap.keys[1].pubkey, unwrap.keys[2].pubkey].map(String),
      [SOLANA.programs.token, ataOf(SOL, vault), vault, vault].map(String));
  }
  // no SOL side: no unwrap, no WSOL account
  const venue = 'raydium-clmm';
  const exits = instructionsOf(venue).filter(r => EXIT_INSTRUCTIONS[venue].includes(r.name));
  const usdcOnly = await ownerCloseMessages(null, { settings, vault, owner: owner.publicKey, venue, pools: [pool],
    builder: builderOf((ctx) => exits.map(r => ixOf(venue, r, ctx)), [USDC, key()]) });
  assert.ok(!usdcOnly.messages.map(decoded).flatMap(m => m.accountKeys.map(String)).includes(ataOf(SOL, vault).toBase58()));
  // the SDK's own unwrap, without SOL among the mints, is still done once, last
  const sdkUnwrap = await ownerCloseMessages(null, { settings, vault, owner: owner.publicKey, venue, pools: [pool],
    builder: builderOf((ctx) => [...exits.map(r => ixOf(venue, r, { ...ctx, tokenAccounts: [...ctx.tokenAccounts] })), unwrapOf(vault)], [USDC]) });
  assert.ok(sdkUnwrap.messages.map(decoded).flatMap(m => m.accountKeys.map(String)).includes(ataOf(SOL, vault).toBase58()));
  // nothing held: nothing to sign
  const none = await ownerCloseMessages(null, { settings, vault, owner: owner.publicKey, venue, pools: [pool],
    builder: { open: async () => ({ mints: [{ mint: SOL, program: spl.TOKEN_PROGRAM_ID }], positions: [] }) } });
  assert.deepEqual(none.messages, []);
  assert.deepEqual(none.positions, []);
  assert.equal(none.summary, `the vault ${vault.toBase58()} holds no ${venue} position on ${pool.toBase58()}`);
  assert.equal(EXIT_PRICE_BPS, 100);
});
