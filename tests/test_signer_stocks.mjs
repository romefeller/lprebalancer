// The signers on tokenized-stock pools, from recorded mainnet accounts
// (fixtures_stocks_20261001.json): MU on Meteora DLMM, DJT on Orca, MSFTx on
// Raydium CLMM, all Token-2022 with scaled UI amounts.
//
// Proven here, with no network:
//   - each signer's mint read decodes the recorded mints, and refuses writes
//     on a paused or hooked mint;
//   - missing extension data and junk RPC answers fail the read loudly;
//   - Orca's price comes from the pool's own sqrt price, and a pool whose
//     chain mints or decimals disagree with the API is refused;
//   - position marks: amounts are UI (raw × multiplier), and the dollar value
//     does not move with the multiplier, because a multiplier changes what a
//     raw unit is called, not what the pool holds.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import fc from 'fast-check';
import { createRequire } from 'node:module';
import { sqrtPriceToPrice } from '@orca-so/whirlpools-core';
import * as dlmm from '../venues/meteora_dlmm/signer.mjs';
import * as orca from '../venues/orca/signer.mjs';
import * as raydium from '../venues/raydium_clmm/signer.mjs';
import { assertWritable, uiPrice } from '../shared/token2022.mjs';

const require = createRequire(import.meta.url);
const BN = require('bn.js');
const { TickUtil } = require('@raydium-io/raydium-sdk-v2');

const FIX = JSON.parse(fs.readFileSync(new URL('./fixtures_stocks_20261001.json', import.meta.url)));
const ADDR = FIX.addresses;
const BY_ADDR = Object.fromEntries(Object.entries(ADDR).map(([k, a]) => [a, FIX.mints[k]]));
const clone = (x) => JSON.parse(JSON.stringify(x));
const ext = (acct, name) => acct.data.parsed.info.extensions.find(e => e.extension === name);

// web3.js Connection (DLMM, Raydium) and @solana/kit rpc (Orca) fakes over a table of accounts.
const web3Conn = (table) => ({
  getMultipleParsedAccounts: async (keys) => ({ value: keys.map(k => table[k.toBase58()] ?? null) }),
});
const kitRpc = (table, extra = {}) => ({
  getMultipleAccounts: (addrs, cfg) => ({
    send: async () => {
      assert.equal(cfg?.encoding, 'jsonParsed');
      return { value: addrs.map(a => table[String(a)] ?? null) };
    },
  }),
  ...extra,
});

const SIGNERS = [
  ['signer_dlmm.mjs', 'MU', (t) => dlmm.poolMints(web3Conn(t), [ADDR.MU, ADDR.USDC])],
  ['signer2.mjs', 'DJT', (t) => orca.poolMints(kitRpc(t), [ADDR.DJT, ADDR.USDC])],
  ['signer_raydium.mjs', 'MSFTx', (t) => raydium.poolMints(web3Conn(t), [ADDR.MSFTx, ADDR.USDC])],
];

test('each signer decodes its recorded stock mint and USDC', async () => {
  const want = { MU: [6, 1.0001069314314899], DJT: [6, 1], MSFTx: [8, 1.0059033904787456] };
  for (const [name, sym, read] of SIGNERS) {
    const [a, b] = await read(BY_ADDR);
    assert.deepEqual([a.decimals, a.multiplier], want[sym], name);
    assert.equal(a.programId, 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb', name);
    assert.deepEqual([b.decimals, b.multiplier, b.programId], [6, 1, 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'], name);
    assert.doesNotThrow(() => assertWritable([a, b]), name);
  }
});

test('each signer refuses writes on a paused mint and on a hooked mint', async () => {
  for (const [name, sym, read] of SIGNERS) {
    const paused = clone(BY_ADDR);
    ext(paused[ADDR[sym]], 'pausableConfig').state.paused = true;
    const p = await read(paused);
    assert.equal(p[0].paused, true, name);                       // the read itself works
    assert.throws(() => assertWritable(p), { message: 'refused: mint paused' }, name);

    const hooked = clone(BY_ADDR);
    ext(hooked[ADDR[sym]], 'transferHook').state.programId = 'HookProgram1111111111111111111111111111111';
    const h = await read(hooked);
    assert.throws(() => assertWritable(h), { message: 'refused: transfer hook' }, name);
  }
});

test('missing extension data fails the read, for every signer', async () => {
  for (const [name, sym, read] of SIGNERS) {
    const t = clone(BY_ADDR);
    delete ext(t[ADDR[sym]], 'scaledUiAmountConfig').state;
    await assert.rejects(read(t), /scaledUiAmountConfig unreadable/, name);
    const u = clone(BY_ADDR);
    u[ADDR[sym]].data = ['AAAA', 'base64'];                         // the RPC did not parse it
    await assert.rejects(read(u), /no parsed mint data/, name);
    const v = clone(BY_ADDR);
    delete v[ADDR[sym]];                                            // account missing
    await assert.rejects(read(v), /account not found/, name);
  }
});

test('junk RPC answers fail the read, for every signer', async () => {
  const junk = [
    ['value not a list', { value: 'x' }],
    ['value missing', {}],
    ['one account short', null],
  ];
  for (const [what, answer] of junk) {
    const short = (n) => (answer ?? { value: [FIX.mints.USDC] });
    await assert.rejects(dlmm.poolMints({ getMultipleParsedAccounts: async () => short() }, [ADDR.MU, ADDR.USDC]), /accounts for 2 mints/, what);
    await assert.rejects(raydium.poolMints({ getMultipleParsedAccounts: async () => short() }, [ADDR.MSFTx, ADDR.USDC]), /accounts for 2 mints/, what);
    await assert.rejects(orca.poolMints({ getMultipleAccounts: () => ({ send: async () => short() }) }, [ADDR.DJT, ADDR.USDC]), /accounts for 2 mints/, what);
  }
  // a transport error is the RPC's, thrown as itself (rpc_policy decides on rotation)
  await assert.rejects(dlmm.poolMints({ getMultipleParsedAccounts: async () => { throw new Error('429 Too Many Requests'); } },
    [ADDR.MU, ADDR.USDC]), /429/);
});

// The recorded DJT whirlpool, served the way fetchWhirlpool reads it.
function orcaRpc(mints) {
  const w = FIX.djtWhirlpool;
  return kitRpc(mints, {
    getAccountInfo: (a, cfg) => ({
      send: async () => {
        assert.equal(String(a), w.address);
        assert.equal(cfg?.encoding, 'base64');
        return { value: { ...w.account, lamports: BigInt(w.account.lamports), space: BigInt(w.account.space) } };
      },
    }),
  });
}
const DJT_INFO = { address: FIX.djtWhirlpool.address, mintA: ADDR.DJT, mintB: ADDR.USDC, decimalsA: 6, decimalsB: 6,
  symbolA: 'DJT', symbolB: 'USDC', price: 1 /* the API's figure, which the chain's replaces */ };

test('Orca: the price is the pool\'s own sqrt price; uiPrice follows the multiplier', async () => {
  const v = await orca.chainView(orcaRpc(BY_ADDR), DJT_INFO);
  assert.ok(v.price > 1 && v.price < 1000, `price ${v.price}`);
  assert.equal(v.uiPrice, v.price);                                // DJT multiplier 1
  assert.equal(v.multiplierA, 1);
  assert.equal(v.tokenProgramA, 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb');
  assert.ok(!('mints' in JSON.parse(JSON.stringify(v))), 'the facts stay out of the JSON');
  // the same pool with a 2x multiplier on DJT: the pool price is unchanged, a UI DJT is worth half
  const scaled = clone(BY_ADDR);
  Object.assign(ext(scaled[ADDR.DJT], 'scaledUiAmountConfig').state, { multiplier: '2', newMultiplier: '2' });
  const s = await orca.chainView(orcaRpc(scaled), DJT_INFO);
  assert.equal(s.price, v.price);
  assert.ok(Math.abs(s.uiPrice - v.price / 2) < 1e-12);
  // the recorded sqrt price, decoded independently
  const buf = Buffer.from(FIX.djtWhirlpool.account.data[0], 'base64');
  const sqrt = buf.readBigUInt64LE(65) + (buf.readBigUInt64LE(73) << 64n);   // Whirlpool.sqrtPrice (u128 at byte 65)
  assert.ok(Math.abs(sqrtPriceToPrice(sqrt, 6, 6) - v.price) < 1e-12);
});

test('Orca: a pool whose chain mints or decimals disagree with the API is refused', async () => {
  await assert.rejects(orca.chainView(orcaRpc(BY_ADDR), { ...DJT_INFO, mintA: ADDR.MU }), /chain's mints differ/);
  await assert.rejects(orca.chainView(orcaRpc(BY_ADDR), { ...DJT_INFO, decimalsA: 9 }), /disagree with Orca's/);
  const paused = clone(BY_ADDR);
  ext(paused[ADDR.DJT], 'pausableConfig').state.paused = true;
  const v = await orca.chainView(orcaRpc(paused), DJT_INFO);
  assert.equal(v.paused, true);
  assert.throws(() => assertWritable(v.mints), { message: 'refused: mint paused' });
});

// --- position marks -------------------------------------------------------------
const multiplier = fc.double({ min: 0.5, max: 2, noNaN: true });

function dlmmInfo(m) {
  const price = 1093.354010617109;
  return { pool: 'P', step: 20, activeBin: 10, decimalsA: 6, decimalsB: 6, symbolA: 'MU', symbolB: 'USDC',
    price, uiPrice: uiPrice(price, m, 1), multiplierA: m, multiplierB: 1, quoteUsd: 1, quoteUsdSource: 'stable',
    paused: false, transferHookA: null, transferHookB: null };
}
const dlmmPos = (key, lo, hi, x, y, fx, fy) => ({ publicKey: { toBase58: () => key },
  positionData: { lowerBinId: lo, upperBinId: hi, totalXAmount: x, totalYAmount: y, feeX: new BN(fx), feeY: new BN(fy) } });

test('DLMM mark: UI amounts, and a dollar value the multiplier does not move', () => {
  const lot = [dlmmPos('B', 11, 20, '1500000', '0', 300, 100), dlmmPos('A', 0, 10, '2000000.7', '3000000', 1000, 500)];
  const base = dlmm.unionView(lot, dlmmInfo(1));
  fc.assert(fc.property(multiplier, (m) => {
    const v = dlmm.unionView(lot, dlmmInfo(m));
    const close = (x, y) => Math.abs(x - y) <= 1e-9 * Math.max(1, Math.abs(y));
    return v.positionMint === 'A' && close(v.closeEstA, 3.5 * m) && v.closeEstB === 3
      && close(v.feesAccruedA, 0.0013 * m) && close(v.positionUsd, base.positionUsd)
      && close(v.feesAccrued_USD, base.feesAccrued_USD) && v.price === base.price && v.multiplierA === m;
  }));
  assert.ok(Math.abs(base.positionUsd - (3.5 * 1093.354010617109 + 3)) < 1e-3);
});

test('Raydium mark: UI amounts, and a dollar value the multiplier does not move', () => {
  const tickCurrent = 16415;
  const r = { rpcPoolInfo: { sqrtPriceX64: TickUtil.getSqrtPriceAtTick(tickCurrent) } };
  const p = { nftMint: { toBase58: () => 'NFT' }, tickLower: 16300, tickUpper: 16500, liquidity: new BN('5000000000') };
  const tickOf = () => ({ feeA: new BN(12345), feeB: new BN(678), feesSource: 'feeGrowth' });
  const info = (m) => {
    const price = 516.2616982233687;
    return { pool: 'P', tickCurrent, decimalsA: 8, decimalsB: 6, symbolA: 'MSFTx', symbolB: 'USDC',
      price, uiPrice: uiPrice(price, m, 1), multiplierA: m, multiplierB: 1, quoteUsd: 1, quoteUsdSource: 'stable',
      paused: false, transferHookA: null, transferHookB: null };
  };
  const base = raydium.positionView(p, r, info(1), tickOf);
  assert.ok(base.closeEstA > 0 && base.closeEstB > 0 && base.inRange);
  fc.assert(fc.property(multiplier, (m) => {
    const v = raydium.positionView(p, r, info(m), tickOf);
    const close = (x, y) => Math.abs(x - y) <= 1e-9 * Math.max(1, Math.abs(y));
    return close(v.closeEstA, base.closeEstA * m) && v.closeEstB === base.closeEstB
      && close(v.feesAccruedA, 0.00012345 * m) && close(v.positionUsd, base.positionUsd)
      && close(v.feesAccrued_USD, base.feesAccrued_USD) && v.lowerPrice === base.lowerPrice;
  }));
  // the recorded live multiplier: 0.59% more MSFTx on screen than raw/10^8, the same dollars
  const live = raydium.positionView(p, r, info(1.0059033904787456), tickOf);
  assert.ok(Math.abs(live.closeEstA / base.closeEstA - 1.0059033904787456) < 1e-12);
  assert.ok(Math.abs(live.positionUsd - base.positionUsd) < 1e-3);
  const two = raydium.unionView([p, { ...p, nftMint: { toBase58: () => 'NFT2' } }], r, info(1.0059033904787456), tickOf);
  assert.ok(Math.abs(two.positionUsd - 2 * live.positionUsd) < 1e-3);
});

test('DLMM: in range on both edge bins, out of range one bin beyond', () => {
  const info = (bin) => ({ ...dlmmInfo(1.0001069314314899), activeBin: bin });
  const pos = dlmmPos('A', 5, 15, '1000000', '1000000', 0, 0);
  const lot = [pos, dlmmPos('B', 16, 20, '1000000', '0', 0, 0)];
  for (const [bin, want] of [[4, false], [5, true], [10, true], [15, true], [16, false]]) {
    assert.equal(dlmm.positionView(pos, info(bin)).inRange, want, `bin ${bin}`);
  }
  for (const [bin, want] of [[4, false], [5, true], [20, true], [21, false]]) {
    assert.equal(dlmm.unionView(lot, info(bin)).inRange, want, `lot, bin ${bin}`);
  }
});

test('Raydium: in range from the lower tick up to, not including, the upper tick; union sums', () => {
  const p = { nftMint: { toBase58: () => 'NFT' }, tickLower: 16300, tickUpper: 16500, liquidity: new BN('5000000000') };
  const q = { ...p, nftMint: { toBase58: () => 'NFT2' }, liquidity: new BN('7000000000') };
  const fresh = () => ({ feeA: new BN(1), feeB: new BN(1), feesSource: 'feeGrowth' });
  const view = (tick, list, tickOf = fresh) => {
    const r = { rpcPoolInfo: { sqrtPriceX64: TickUtil.getSqrtPriceAtTick(tick) } };
    const info = { pool: 'P', tickCurrent: tick, decimalsA: 8, decimalsB: 6, symbolA: 'MSFTx', symbolB: 'USDC',
      price: 516, uiPrice: 513, multiplierA: 1.0059033904787456, multiplierB: 1, quoteUsd: 1 };
    return list.length === 1 ? raydium.positionView(list[0], r, info, tickOf) : raydium.unionView(list, r, info, tickOf);
  };
  for (const [tick, want] of [[16299, false], [16300, true], [16499, true], [16500, false]]) {
    assert.equal(view(tick, [p]).inRange, want, `tick ${tick}`);
  }
  const both = view(16400, [p, q]);
  assert.equal(both.liquidity, '12000000000');
  assert.deepEqual(both.positions, ['NFT', 'NFT2']);
  assert.equal(both.feesSource, 'feeGrowth');
  const staleOne = (x) => (x.nftMint.toBase58() === 'NFT2' ? { ...fresh(), feesSource: 'tokenFeesOwed (stale: x)' } : fresh());
  assert.equal(view(16400, [p, q], staleOne).feesSource, 'mixed');
});
