// venues/uniswap_v3/signer.mjs on Polygon without a node: the chain registry, the Polygon module's
// endpoints and addresses, and the signer switched to Polygon (useChain): its gas cap, its
// native token, its price references, no v4 route, and pool genuineness against Polygon's
// factory. A loopback LPBOT_RPC is the ONLY endpoint, so nothing can reach mainnet.
import test from 'node:test';
import assert from 'node:assert';
import { getAddress } from 'viem';
import fc from 'fast-check';
import * as M from '../chains/evm/clmath.mjs';
import * as P from '../chains/evm/polygon.mjs';
import * as UNI from '../chains/evm/unichain.mjs';
import { chainModule, MODULES } from '../chains/evm/chains.mjs';
import * as S from '../venues/uniswap_v3/signer.mjs';

const POOL = getAddress('0x9b08288c3be4f62bbf8d1c20ac9c5e6f9467d8b7');

test.afterEach(() => S.useChain('unichain'));

test('registry: no name is Unichain, polygon is Polygon, any other chain is refused', () => {
  assert.strictEqual(chainModule(undefined), UNI);
  assert.strictEqual(chainModule(''), UNI);
  assert.strictEqual(chainModule('unichain'), UNI);
  assert.strictEqual(chainModule('polygon'), P);
  for (const bad of ['base', 'solana', 'Polygon', 'polygon ', '__proto__', 'constructor']) {
    assert.throws(() => chainModule(bad), /does not serve chain/, bad);
  }
  assert.deepStrictEqual(Object.keys(MODULES).sort(), ['polygon', 'unichain']);
});

test('the polygon module: chain 137, its own factory and dex, POL gas, no v4, the shared ABIs', () => {
  assert.strictEqual(P.CHAIN_ID, 137);
  assert.strictEqual(P.VIEM_CHAIN.id, 137);
  assert.deepStrictEqual([P.NAME, P.CHAIN, P.DEX, P.NATIVE_SYMBOL], ['Polygon', 'polygon', 'uniswap-v3-polygon', 'POL']);
  assert.strictEqual(P.WRAPPED_NATIVE, P.WPOL);
  assert.strictEqual(P.V3.factory, '0x1F98431c8aD98523631AE4a59f267346ea31F984');
  assert.notStrictEqual(P.V3.factory, UNI.V3.factory);
  for (const a of [...Object.values(P.V3), P.WPOL, P.USDT0, P.USDC]) assert.strictEqual(a, getAddress(a.toLowerCase()), 'EIP-55 form');
  assert.ok(Object.isFrozen(P.V3) && Object.isFrozen(P.V4_POOLS) && Object.isFrozen(P.REFERENCES));
  assert.deepStrictEqual(P.V4_POOLS, []);
  assert.ok(P.STABLES.has(P.USDT0.toLowerCase()) && P.STABLES.has(P.USDC.toLowerCase()) && !P.STABLES.has(P.WPOL.toLowerCase()));
  assert.strictEqual(P.NPM_ABI, UNI.NPM_ABI, 'one ABI module for both chains');
  assert.strictEqual(P.MAX_GWEI_DEFAULT, 1500); assert.strictEqual(UNI.MAX_GWEI_DEFAULT, 0.5);
  assert.deepStrictEqual(P.REFERENCES[P.WPOL.toLowerCase()].map(r => r.name), ['binance POLUSDT', 'bybit POLUSDT']);
});

test('polygon endpoints: a loopback LPBOT_RPC is the only one; no Unichain or dead endpoint otherwise', () => {
  assert.deepStrictEqual(P.polygonEndpoints({ LPBOT_RPC: 'http://127.0.0.1:8545', LPBOT_POLYGON_RPC: 'https://own.example' }), ['http://127.0.0.1:8545']);
  assert.deepStrictEqual(P.simulationEndpoints({ LPBOT_RPC: 'http://127.0.0.1:8545' }), ['http://127.0.0.1:8545']);
  const pub = P.polygonEndpoints({ LPBOT_RPC: 'https://x.example', LPBOT_POLYGON_RPC: 'https://own.example', LPBOT_UNICHAIN_RPC: 'https://uni.example' });
  assert.deepStrictEqual(pub, ['https://x.example', 'https://own.example', P.POLYGON_PUBLICNODE, P.POLYGON_DRPC]);
  assert.deepStrictEqual(P.polygonEndpoints({}), [P.POLYGON_PUBLICNODE, P.POLYGON_DRPC]);
  // a remote LPBOT_RPC is not a simulator: simulation stays on the endpoints that answer eth_simulateV1
  assert.deepStrictEqual(P.simulationEndpoints({ LPBOT_RPC: 'https://x.example' }), [P.POLYGON_PUBLICNODE, P.POLYGON_DRPC]);
  assert.deepStrictEqual(P.simulationEndpoints({}), [P.POLYGON_PUBLICNODE, P.POLYGON_DRPC]);
  for (const e of [P.polygonEndpoints({}), P.simulationEndpoints({})]) {
    assert.ok(e.every(u => !/unichain|polygon-rpc\.com/.test(u)), JSON.stringify(e));
  }
  assert.strictEqual(P.endpoints, P.polygonEndpoints);
});

test('useChain: the signer switches its gas cap, native token and v4 registry with the chain', () => {
  S.useChain('polygon');
  assert.strictEqual(S.settings({}).maxFeeWei, M.toRaw(1500, 9));
  assert.strictEqual(S.settings({ LPBOT_EVM_MAX_GWEI: '700' }).maxFeeWei, M.toRaw(700, 9));
  assert.ok(S.isNative('POL') && S.isNative('pol') && !S.isNative('ETH'));
  assert.deepStrictEqual(S.v4PoolsFor(P.WPOL, P.USDT0), []);
  S.useChain('unichain');
  assert.strictEqual(S.settings({}).maxFeeWei, M.toRaw(0.5, 9));
  assert.ok(S.isNative('ETH') && !S.isNative('POL'));
  assert.strictEqual(S.v4PoolsFor(UNI.USDC, UNI.HYPE).length, 1);
  assert.throws(() => S.useChain('base'), /does not serve chain/);
});

test('referencePrices on Polygon: WPOL has the POL references; USDT0 has none', async () => {
  S.useChain('polygon');
  const asked = [];
  const fetchFn = async (url) => {
    asked.push(new URL(url).hostname + new URL(url).search);
    return { ok: true, json: async () => (url.includes('bybit') ? { result: { list: [{ lastPrice: '0.102' }] } } : { price: '0.100' }) };
  };
  const r = await S.referencePrices(P.WPOL, fetchFn);
  assert.strictEqual(r.usd, 0.101);
  assert.deepStrictEqual(asked, ['data-api.binance.vision?symbol=POLUSDT', 'api.bybit.com?category=spot&symbol=POLUSDT']);
  assert.strictEqual((await S.referencePrices(P.USDT0, fetchFn)).usd, null);
  S.useChain('unichain');
  assert.strictEqual((await S.referencePrices(P.WPOL, fetchFn)).usd, null, 'Unichain has no WPOL reference');
});

// The tick of $0.10 per WPOL (USDT0 6 decimals, WPOL 18): the fake pool's slot0 is consistent.
const TICK = M.tickAtPrice(0.1, 18, 6);
// A WPOL/USDT0 pool view, as describe() reads it from the chain (token A = WPOL, B = USDT0).
function fakePub(over = {}) {
  const c = { factory: P.V3.factory, t0: P.WPOL, t1: P.USDT0, fee: 500, ts: 10, mapsTo: POOL, npmF: P.V3.factory, routerF: P.V3.factory, quoterF: P.V3.factory, ...over };
  const sp = BigInt(Math.floor(Math.sqrt(0.1 * 1e-12) * 2 ** 96));   // $0.10 per WPOL
  return {
    async multicall({ contracts }) {
      if (contracts[0].functionName === 'factory' && contracts[0].address === POOL) {
        return [c.factory, c.t0, c.t1, c.fee, c.ts, 10n ** 15n, [sp, TICK, 0, 0, 0, 0, true]];
      }
      return [c.mapsTo, c.npmF, c.routerF, c.quoterF, 18, 6, 'WPOL', 'USDT0'];
    },
  };
}

test('describe on Polygon: a genuine WPOL/USDT0 pool, priced in USDT0 per WPOL, checked against POL', async () => {
  S.useChain('polygon');
  const refs = async () => ({ usd: 0.1, sources: [] });
  const info = await S.describe(fakePub(), POOL, refs);
  assert.deepStrictEqual([info.dex, info.chain, info.symbolA, info.symbolB, info.feePips], ['uniswap-v3-polygon', 'polygon', 'WPOL', 'USDT0', 500]);
  assert.strictEqual(info.npm, P.V3.npm); assert.strictEqual(info.router, P.V3.router);
  assert.ok(Math.abs(info.price - 0.1) < 1e-9, String(info.price));
  assert.strictEqual(info.quoteUsd, 1); assert.strictEqual(info.volatile, P.WPOL);
  assert.ok(info.referenceDeviation < 1e-6);
  assert.deepStrictEqual(S.marketRefusals(info, S.settings({})), []);
  const far = await S.describe(fakePub(), POOL, async () => ({ usd: 0.2, sources: [] }));
  assert.match(S.marketRefusals(far, S.settings({})).join(), /from the reference/);
});

test('describe on Polygon refuses Unichain\'s factory and a contract naming another factory', async () => {
  S.useChain('polygon');
  const refs = async () => ({ usd: 0.1, sources: [] });
  await assert.rejects(S.describe(fakePub({ factory: UNI.V3.factory }), POOL, refs), /belongs to factory/);
  await assert.rejects(S.describe(fakePub({ npmF: UNI.V3.factory }), POOL, refs), /position manager .* names factory/);
  await assert.rejects(S.describe(fakePub({ quoterF: UNI.V3.factory }), POOL, refs), /quoter .* names factory/);
  // and Unichain refuses Polygon's genuine pool
  S.useChain('unichain');
  await assert.rejects(S.describe(fakePub(), POOL, refs), /belongs to factory/);
});

test('the process picks its chain from LPBOT_CHAIN at start', async () => {
  const { spawnSync } = await import('node:child_process');
  const js = "import * as S from './venues/uniswap_v3/signer.mjs'; console.log(S.settings({}).maxFeeWei.toString(), S.isNative('POL'))";
  const run = env => spawnSync('node', ['--input-type=module', '-e', js], { cwd: new URL('..', import.meta.url).pathname, env: { PATH: process.env.PATH, ...env }, encoding: 'utf8' });
  assert.strictEqual(run({ LPBOT_CHAIN: 'polygon' }).stdout.trim(), `${M.toRaw(1500, 9)} true`);
  assert.strictEqual(run({}).stdout.trim(), `${M.toRaw(0.5, 9)} false`);
  const bad = run({ LPBOT_CHAIN: 'base' });
  assert.notStrictEqual(bad.status, 0); assert.match(bad.stderr, /does not serve chain base/);
});

// --- increase: idle cash into the open position -------------------------------------------
async function polyInfo(refUsd = 0.1) {
  S.useChain('polygon');
  return S.describe(fakePub(), POOL, async () => ({ usd: refUsd, sources: [] }));
}
// A position around the price: +/-2% is about +/-200 ticks, on the pool's spacing of 10.
const POS = { tokenId: 7n, tickLower: Math.floor((TICK - 200) / 10) * 10, tickUpper: Math.ceil((TICK + 200) / 10) * 10, liquidity: 10n ** 15n };
const wallet = (a = 10n ** 24n, b = 10n ** 12n, eth = 10n ** 19n) => ({ rawA: a, rawB: b, eth });
const CFG = (over = {}) => ({ ...S.settings({}), ...over });

test('planIncrease: in range, both caps, the deposit at the position ticks never above a cap', async () => {
  const info = await polyInfo();
  const plan = S.planIncrease(info, POS, wallet(), CFG(), new Map(), '1000', '100');
  assert.deepStrictEqual(plan.refusals, []);
  assert.ok(plan.dep.liquidity > 0n);
  assert.ok(plan.dep.amountA <= plan.capA && plan.dep.amountB <= plan.capB);
  assert.ok(plan.minA <= plan.dep.amountA && plan.minB <= plan.dep.amountB);
  assert.ok(plan.addUsd > 0 && plan.addUsd <= 1000 * 0.1 + 100 + 1e-9);
  assert.ok(plan.heldUsd > 0);
});

test('planIncrease refuses: out of range, gas under the reserve, past LPBOT_MAX_USD, empty caps, a short wallet', async () => {
  const info = await polyInfo();
  const out = S.planIncrease(info, { ...POS, tickLower: -298000, tickUpper: -297000 }, wallet(), CFG(), new Map(), '1000', '100');
  assert.match(out.refusals.join(), /outside the position ticks/);
  assert.match(S.planIncrease(info, POS, wallet(10n ** 24n, 10n ** 12n, 0n), CFG(), new Map(), '1000', '100').refusals.join(), /POL .* below the .* gas reserve/);
  assert.match(S.planIncrease(info, POS, wallet(), CFG({ maxUsd: 1 }), new Map(), '1000', '100').refusals.join(), /position about \$\d+\.\d\d after the add exceeds cap \$1/);
  assert.match(S.planIncrease(info, POS, wallet(), CFG(), new Map(), '0', '0').refusals.join(), /fund no liquidity/);
  assert.match(S.planIncrease(info, POS, wallet(0n, 0n), CFG(), new Map(), '1000', '100').refusals.join(), /wallet lacks/);
  const far = await polyInfo(0.2);
  assert.match(S.planIncrease(far, POS, wallet(), CFG(), new Map(), '1000', '100').refusals.join(), /from the reference/);
});

test('planIncrease property: never above a cap or the sleeve, and the sleeve binds', async () => {
  const info = await polyInfo();
  await fc.assert(fc.asyncProperty(fc.integer({ min: 0, max: 50_000 }), fc.integer({ min: 0, max: 5_000 }), fc.integer({ min: 0, max: 300 }),
    async (ma, mb, sleeveB) => {
      const parsed = S.planIncrease(info, POS, wallet(), CFG({ maxUsd: 1e9 }), M.parseSleeve(JSON.stringify({ [P.USDT0]: sleeveB })), String(ma), String(mb));
      assert.ok(parsed.dep.amountA <= M.toRaw(String(ma), 18));
      assert.ok(parsed.dep.amountB <= M.toRaw(String(Math.min(mb, sleeveB)), 6));
    }), { numRuns: 200 });
});

test('planIncrease edges: the tick at the lower edge is in, at the upper edge out; exact gas, wallet and cap pass', async () => {
  const info = await polyInfo();
  const t = info.tick;
  const atLower = S.planIncrease(info, { ...POS, tickLower: t, tickUpper: t + 200 }, wallet(), CFG(), new Map(), '1000', '100');
  assert.ok(!atLower.refusals.some(r => /outside/.test(r)), 'tick == tickLower is in range');
  const atUpper = S.planIncrease(info, { ...POS, tickLower: t - 200, tickUpper: t }, wallet(), CFG(), new Map(), '1000', '100');
  assert.ok(atUpper.refusals.some(r => /outside/.test(r)), 'tick == tickUpper is out of range');
  const reserve = S.settings({}).gasReserve;
  assert.ok(!S.planIncrease(info, POS, wallet(10n ** 24n, 10n ** 12n, reserve), CFG(), new Map(), '1000', '100').refusals.some(r => /gas reserve/.test(r)),
    'gas exactly at the reserve passes');
  const base = S.planIncrease(info, POS, wallet(), CFG(), new Map(), '1000', '100');
  const exact = S.planIncrease(info, POS, wallet(base.dep.amountA, base.dep.amountB), CFG(), new Map(), '1000', '100');
  assert.ok(!exact.refusals.some(r => /wallet lacks/.test(r)), 'a wallet holding exactly the deposit passes');
  // WPOL not binding (a huge cap, USDT0 binds): the wallet holds exactly the computed WPOL deposit
  const freeA = S.planIncrease(info, POS, wallet(), CFG(), new Map(), '100000', '1');
  const exactA = S.planIncrease(info, POS, wallet(freeA.dep.amountA, freeA.dep.amountB), CFG(), new Map(), '100000', '1');
  assert.strictEqual(exactA.dep.amountA, freeA.dep.amountA);
  assert.ok(!exactA.refusals.some(r => /wallet lacks/.test(r)), exactA.refusals.join());
  // asking for exactly what the deposit needs: 1 wei short of WPOL (18 decimals) is rounding and
  // the holding is used; 1e9 + 1 wei short is a real shortfall; USDT0 (6 decimals) has no slack
  const need = [M.toHuman(base.dep.amountA, 18).toFixed(9), M.toHuman(base.dep.amountB, 6).toFixed(6)];
  const oneWei = S.planIncrease(info, POS, wallet(base.dep.amountA - 1n, base.dep.amountB), CFG(), new Map(), ...need);
  assert.ok(!oneWei.refusals.some(r => /wallet lacks/.test(r)), oneWei.refusals.join());
  assert.ok(oneWei.dep.amountA <= base.dep.amountA - 1n, 'never above the holding');
  const real = S.planIncrease(info, POS, wallet(M.toRaw(need[0], 18) - 10n ** 9n - 1n, base.dep.amountB), CFG(), new Map(), need[0], '0.000001');
  assert.ok(real.dep.amountA < M.toRaw(need[0], 18), 'a real shortfall is not stretched to the request');
  const shortB = S.planIncrease(info, POS, wallet(base.dep.amountA, base.dep.amountB - 1n), CFG(), new Map(), '1000', '100');
  assert.ok(shortB.dep.amountB <= base.dep.amountB - 1n || shortB.refusals.some(r => /wallet lacks .* USDT0/.test(r)));
  const atCap = S.planIncrease(info, POS, wallet(), CFG({ maxUsd: base.heldUsd + base.addUsd }), new Map(), '1000', '100');
  assert.ok(!atCap.refusals.some(r => /exceeds cap/.test(r)), 'a position exactly at LPBOT_MAX_USD passes');
});

test('planIncrease: a pool without a dollar price for token B refuses, and says the size is unknown', async () => {
  const info = { ...(await polyInfo()), quoteUsd: 0 };
  const plan = S.planIncrease(info, POS, wallet(), CFG(), new Map(), '1000', '100');
  assert.ok(Number.isNaN(plan.addUsd));
  assert.ok(plan.refusals.some(r => r === `position about $? after the add exceeds cap $${CFG().maxUsd}`), plan.refusals.join(' | '));
  assert.ok(plan.refusals.some(r => /cannot price/.test(r)));
});

test('planWrap: the native left must cover the gas reserve; a bad amount is refused', () => {
  S.useChain('polygon');
  const cfg = CFG({ gasReserve: M.toRaw('2', 18) });
  const ok = S.planWrap(M.toRaw('4010', 18), '4000', cfg);
  assert.deepStrictEqual(ok.refusals, []); assert.strictEqual(ok.raw, M.toRaw('4000', 18));
  assert.deepStrictEqual(S.planWrap(M.toRaw('4010', 18), '4008', cfg).refusals, [], 'exactly the reserve left passes');
  assert.match(S.planWrap(M.toRaw('4010', 18), '4008.000000001', cfg).refusals.join(), /would leave .* below the 2 gas reserve/);
  assert.match(S.planWrap(M.toRaw('1', 18), '5', cfg).refusals.join(), /POL would leave/);
  for (const bad of ['0', '-1', 'x', 'NaN']) assert.throws(() => S.planWrap(M.toRaw('10', 18), bad, cfg), /must be a positive number/, bad);
  assert.throws(() => S.planWrap(M.toRaw('10', 18), '1e-30', cfg), /rounds to zero/);
});

// --- 2026-10-08 incident: the three failures that halted poly-wpol-usdt ----------------------
test('capToHeld: a request rounded up at the 9th decimal is the holding; more is not', () => {
  const held = 1951983722347981500000n;                                    // the WPOL the wallet held
  const asked = M.toRaw('1951.983722348', 18);                              // what the loop wrote
  assert.ok(asked > held);
  assert.strictEqual(M.capToHeld(asked, held, 18), held);
  assert.strictEqual(M.capToHeld(held + 10n ** 9n, held, 18), held, 'one unit of the 9th decimal');
  assert.strictEqual(M.capToHeld(held + 10n ** 9n + 1n, held, 18), held + 10n ** 9n + 1n, 'beyond: unchanged');
  assert.strictEqual(M.capToHeld(held - 5n, held, 18), held - 5n, 'under the holding: unchanged');
  assert.strictEqual(M.capToHeld(1_000_001n, 1_000_000n, 6), 1_000_001n, 'six decimals: no slack');
  assert.strictEqual(M.capToHeld(1_000_000_001n, 1_000_000_000n, 9), 1_000_000_001n, 'nine decimals: no slack');
  assert.strictEqual(M.capToHeld(20n, 10n, 10), 10n, 'ten decimals: one unit of the 9th decimal is 10 raw units');
  assert.strictEqual(M.capToHeld(21n, 10n, 10), 21n);
});

test('capToHeld property: any holding written with %.9f opens without a "wallet lacks"', async () => {
  const info = await polyInfo();
  await fc.assert(fc.asyncProperty(fc.bigInt({ min: 10n ** 18n, max: 10n ** 24n }), async (rawA) => {
    const asked = (Number(rawA) / 1e18).toFixed(9);                       // the loop's '%.9f' of a float read
    const plan = S.planIncrease(info, POS, wallet(rawA, 10n ** 12n), CFG({ maxUsd: 1e12 }), new Map(), asked, '1000000');
    assert.ok(!plan.refusals.some(r => /wallet lacks WPOL|lacks .* WPOL/.test(r)), `${rawA} ${asked}: ${plan.refusals}`);
    assert.ok(plan.dep.amountA <= rawA);
  }), { numRuns: 300 });
});

test('nonceFor: a pending count that clears on a second read is a lagging node; one that stays is refused', async () => {
  const pub = (seq) => ({ async getTransactionCount({ blockTag }) { const x = seq[0]; if (blockTag === 'pending') seq.shift(); return blockTag === 'latest' ? x[0] : x[1]; } });
  const naps = [];
  const sleep = async ms => naps.push(ms);
  assert.strictEqual(await S.nonceFor(pub([[12, 13], [13, 13]]), 'me', { sleep }), 13);
  assert.deepStrictEqual(naps, [2000]);
  const stuck = [[12, 13], [12, 13], [12, 13], [13, 13]];
  await assert.rejects(S.nonceFor(pub(stuck), 'me', { tries: 3, sleep }), /1 transaction\(s\) from this wallet are pending/);
  assert.strictEqual(stuck.length, 1, 'exactly 3 reads, then the refusal');
  assert.strictEqual(await S.nonceFor(pub([[7, 7]]), 'me', { sleep: async () => assert.fail('no wait when clear') }), 7);
});

test('atBlock: waits for the endpoint to reach the block, retries a node without it, keeps a real revert', async () => {
  const naps = [];
  const sleep = async ms => naps.push(ms);
  const heads = [99n, 100n, 101n];
  const pub = { async getBlockNumber() { return heads.length > 1 ? heads.shift() : heads[0]; } };
  const seen = [];
  const out = await S.atBlock(pub, 100n, async bn => { seen.push(bn); if (seen.length === 1) throw new Error('header not found'); return 'ok'; }, { sleep });
  assert.strictEqual(out, 'ok');
  assert.deepStrictEqual(seen, [100n, 101n], 'never called before block 100; the missing header was asked again');
  await assert.rejects(S.atBlock({ async getBlockNumber() { return 200n; } }, 100n,
    async () => { throw new Error('Execution reverted with reason: STF.'); }, { sleep }), /STF/);
  let asks = 0;
  await assert.rejects(S.atBlock({ async getBlockNumber() { asks += 1; return asks > 3 ? 100n : 5n; } }, 100n, async () => 'never', { tries: 3, sleep }),
    /no endpoint node reached block 100/);
  assert.strictEqual(asks, 3, 'exactly 3 head reads, then the refusal');
  assert.strictEqual(await S.atBlock(pub, undefined, async bn => bn ?? 'latest'), 'latest', 'a first step runs at latest');
});

test('runSteps: the step after a sent one is checked at or past its receipt block', async () => {
  const blocks = [];
  const ctx = { nonce: 1, pub: {
    async sendRawTransaction() {},
    async waitForTransactionReceipt({ hash }) { return { status: 'success', blockNumber: hash === 'h1' ? 500n : 501n, gasUsed: 1n, effectiveGasPrice: 1n }; },
  } };
  let n = 0;
  const sign = async (c) => { blocks.push(c.minBlock); n += 1; return { serialized: '0x', hash: `h${n}` }; };
  const r = await S.runSteps(ctx, [{ label: 'approve' }, { label: 'swap' }], {}, { signStep: sign });
  assert.strictEqual(r.error, null);
  assert.deepStrictEqual(blocks, [undefined, 500n]);
});
