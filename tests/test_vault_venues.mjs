// The venue helpers of vault mode, without a chain: what the fork tests
// (test_vault_raydium_fork.mjs, test_vault_orca_fork.mjs, test_vault_byreal_fork.mjs)
// cannot cover by example — every account order, every wrap and unwrap.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'module';
import fc from 'fast-check';
import { around, bitmapFirst, nativeIn, wrapSol, unwrapSol, ensureAtas, ata, VAULT_CU_LIMIT } from '../venues/raydium_clmm/vault.mjs';
import { forVault, madeSigners } from '../venues/orca/vault.mjs';
import { RULES, poolPda } from '../chains/solana/vault/policy.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { Keypair, PublicKey, TransactionInstruction, ComputeBudgetProgram, SystemProgram } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

const key = () => Keypair.generate().publicKey;
const RAYDIUM = new PublicKey(SOLANA.programs.raydium_clmm);
const WHIRLPOOL = new PublicKey(SOLANA.programs.orca_whirlpool);
const DECREASE = Buffer.from(RULES.venues['raydium-clmm'].instructions.find(i => i.name === 'decrease_liquidity_v2').discriminator, 'hex');
const SOL = SOLANA.native_mint, USDC = SOLANA.usdc_mint;
const meta = (pubkey) => ({ pubkey, isSigner: false, isWritable: true });

// A decrease as the SDK lays it out: 16 fixed accounts, the bitmap when the
// band needs it, then one triplet per reward.
function decrease({ bitmap = null, rewards = 1, program = RAYDIUM } = {}) {
  const keys = [...Array.from({ length: 16 }, () => meta(key())), ...(bitmap ? [meta(bitmap)] : []),
    ...Array.from({ length: rewards * 3 }, () => meta(key()))];
  return new TransactionInstruction({ programId: program, keys, data: Buffer.concat([DECREASE, Buffer.alloc(40, 7)]) });
}

test('bitmapFirst: the pool\'s bitmap at 16, once, everything else in its order', () => {
  fc.assert(fc.property(fc.boolean(), fc.integer({ min: 0, max: 3 }), (hasBitmap, rewards) => {
    const pool = key(), bitmap = poolPda(['pool_tick_array_bitmap_extension', 'pool'], pool, RAYDIUM);
    const ix = decrease({ bitmap: hasBitmap ? bitmap : null, rewards });
    const out = bitmapFirst(ix, RAYDIUM, pool);
    assert.ok(out.keys[16].pubkey.equals(bitmap));
    assert.equal(out.keys.filter(k => k.pubkey.equals(bitmap)).length, 1);
    const others = (keys) => keys.filter(k => !k.pubkey.equals(bitmap)).map(k => String(k.pubkey));
    assert.deepEqual(others(out.keys), others(ix.keys));
    assert.deepEqual(out.data, ix.data);
    // each reward's recipient where the rule pins it: 18, 21, 24
    for (let i = 0; i < rewards; i++) assert.ok(out.keys[18 + 3 * i].pubkey.equals(ix.keys[16 + (hasBitmap ? 1 : 0) + 3 * i + 1].pubkey));
  }));
});

test('bitmapFirst: anything but a decrease of that program stays as built', () => {
  const pool = key();
  const swap = new TransactionInstruction({ programId: RAYDIUM, keys: [meta(key())], data: Buffer.alloc(16, 1) });
  assert.equal(bitmapFirst(swap, RAYDIUM, pool), swap);
  const elsewhere = decrease({ program: WHIRLPOOL });
  assert.equal(bitmapFirst(elsewhere, RAYDIUM, pool), elsewhere);
});

test('around: budget, accounts, the wrap only when asked, the unwrap only when SOL is a side', () => {
  const owner = key();
  const ix = new TransactionInstruction({ programId: RAYDIUM, keys: [], data: Buffer.from([1]) });
  const mints = [{ mint: SOL, program: spl.TOKEN_PROGRAM_ID }, { mint: USDC, program: spl.TOKEN_PROGRAM_ID }];
  const out = around(owner, mints, [ix], { wrap: 5n });
  const kinds = out.map(i => i.programId.toBase58());
  assert.deepEqual(kinds, [SOLANA.programs.compute_budget, SOLANA.programs.associated_token, SOLANA.programs.associated_token,
    SOLANA.programs.system, SOLANA.programs.token, RAYDIUM.toBase58(), SOLANA.programs.token]);
  assert.equal(out.at(-1).data[0], 9, 'the WSOL account closes last');
  assert.ok(out.at(-1).keys[1].pubkey.equals(owner) && out.at(-1).keys[2].pubkey.equals(owner), 'into the owner');
  assert.equal(out[3].data.readBigUInt64LE(4), 5n);
  assert.ok(out[3].keys[1].pubkey.equals(ata(SOL, owner)));
  assert.equal(around(owner, mints, [ix]).filter(i => i.programId.equals(SystemProgram.programId)).length, 0, 'nothing wrapped');
  const noSol = around(owner, [mints[1]], [ix], { wrap: 0 });
  assert.ok(!noSol.some(i => i.programId.toBase58() === SOLANA.programs.token), 'no SOL side, no unwrap');
  // the budget: VAULT_CU_LIMIT units unless one is given, a price only with one
  const units = (ixs) => ixs.filter(i => i.programId.equals(ComputeBudgetProgram.programId)).map(i => i.data[0]);
  assert.deepEqual(units(noSol), [2]);
  assert.equal(noSol[0].data.readUInt32LE(1), VAULT_CU_LIMIT);
  const priced = around(owner, [mints[1]], [ix], { budget: { units: 700_000, microLamports: 3 } });
  assert.deepEqual(units(priced), [2, 3]);
  assert.equal(priced[0].data.readUInt32LE(1), 700_000);
});

test('around: every account it creates or touches is the owner\'s', () => {
  const owner = key();
  const mints = [{ mint: SOL, program: spl.TOKEN_PROGRAM_ID }, { mint: USDC, program: spl.TOKEN_2022_PROGRAM_ID }];
  for (const ix of ensureAtas(owner, mints)) {
    assert.ok(ix.keys[0].pubkey.equals(owner) && ix.keys[2].pubkey.equals(owner), 'the owner pays and owns');
    assert.deepEqual([...ix.data], [1], 'idempotent');
  }
  assert.ok(ensureAtas(owner, mints)[1].keys[1].pubkey.equals(spl.getAssociatedTokenAddressSync(new PublicKey(USDC), owner, true, spl.TOKEN_2022_PROGRAM_ID)));
  const [transfer, sync] = wrapSol(owner, 7n);
  assert.ok(transfer.keys[0].pubkey.equals(owner) && transfer.keys[1].pubkey.equals(ata(SOL, owner)));
  assert.equal(sync.data[0], 17);
  assert.ok(unwrapSol(owner).keys[0].pubkey.equals(ata(SOL, owner)));
});

test('nativeIn: the SOL side\'s amount, or nothing', () => {
  assert.equal(nativeIn(SOL, USDC, 3, 4), 3);
  assert.equal(nativeIn(USDC, SOL, 3, 4), 4);
  assert.equal(nativeIn(USDC, key().toBase58(), 3, 4), 0);
});

// --- Orca ----------------------------------------------------------------------

const kitIx = (program, disc, accounts) => ({ programAddress: program.toBase58(), data: Buffer.concat([Buffer.from(disc, 'hex'), Buffer.alloc(8)]),
  accounts: accounts.map(([address, role]) => ({ address: address.toBase58(), role })) });

test('forVault: the payer funds and is refunded; nothing else changes', () => {
  const vault = key(), payer = key(), other = key();
  const cases = [['d42f5f5c726683fa', 0], ['0bbcc1d68d5b95b8', 1], ['2921a5c878e78e32', 1], ['01b6873b9b1963df', 1]];
  for (const [disc, at] of cases) {
    const accounts = [[vault, 3], [vault, 1], [other, 1]];
    const [out] = forVault([kitIx(WHIRLPOOL, disc, accounts)], payer);
    out.keys.forEach((k, i) => assert.ok(k.pubkey.equals(i === at ? payer : accounts[i][0]), `${disc} account ${i}`));
    assert.equal(out.keys[at].isSigner, (accounts[at][1] & 2) !== 0, 'the signer flag stays');
  }
  // another instruction of the program, or the same bytes elsewhere: untouched
  for (const ix of [kitIx(WHIRLPOOL, 'cf755fbfe5b4e20f', [[vault, 3], [vault, 1]]), kitIx(RAYDIUM, 'd42f5f5c726683fa', [[vault, 3], [vault, 1]])]) {
    const [out] = forVault([ix], payer);
    assert.ok(out.keys.every(k => k.pubkey.equals(vault)));
  }
});

test('madeSigners: the keypairs the SDK made, once each, never the vault', () => {
  const vault = key(), mint = key();
  const signer = (address) => ({ address: address.toBase58(), signMessages: async () => [] });
  const ixs = [
    { accounts: [{ address: vault.toBase58(), signer: signer(vault) }, { address: mint.toBase58(), signer: signer(mint) }] },
    { accounts: [{ address: mint.toBase58(), signer: signer(mint) }, { address: key().toBase58() }] },
    { accounts: [{ address: key().toBase58(), signer: { address: 'noop' } }] },          // a signer that cannot sign
  ];
  assert.deepEqual(madeSigners(ixs, vault).map(s => s.address), [mint.toBase58()]);
  assert.deepEqual(madeSigners([], vault), []);
});
