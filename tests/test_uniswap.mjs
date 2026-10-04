// signer_uniswap.mjs and evm/unichain.mjs without a node: the money arithmetic, every
// refusal, the price reference, the v4 swap calldata and the partial-send report. Failure
// paths first. No network: the CLI cases use a closed loopback port, which
// unichainEndpoints() makes the ONLY endpoint, so nothing can reach mainnet.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import fc from 'fast-check';
import { decodeFunctionData, decodeAbiParameters, getAddress, keccak256, encodeAbiParameters } from 'viem';
import * as M from '../evm/clmath.mjs';
import * as U from '../evm/unichain.mjs';
import * as S from '../signer_uniswap.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const script = path.join(dir, '..', 'signer_uniswap.mjs');
const POOL = '0x5d3e7f5dA38FBf476E8B36E3b90D02FC4C1A08C3';
const PIN = '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142';
const OTHER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8';
const E6 = 10n ** 6n, E18 = 10n ** 18n;
const made = [];
test.after(() => { for (const x of made) fs.rmSync(x, { recursive: true, force: true }); });

function scratch() {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'uni-'));
  made.push(d); fs.chmodSync(d, 0o700);
  return d;
}

function run(args, env = {}) {
  return spawnSync('node', [script, ...args], {
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: '/nonexistent', LPBOT_RPC: 'http://127.0.0.1:9', LPBOT_POOL: POOL, ...env },
    encoding: 'utf8', timeout: 60_000,
  });
}

// A USDC/HYPE pool view at `usdPerHype`, as describe() returns it (token A = USDC, the stable).
function poolInfo(usdPerHype = 90, extra = {}) {
  const price = 1 / usdPerHype;                       // HYPE per USDC, B per A
  const tick = M.tickAtPrice(price, 6, 18);
  const sp = M.sqrtRatioAtTick(tick);
  const p = M.priceFromSqrtX96(sp, 6, 18);
  return {
    pool: POOL, npm: U.V3.npm, mintA: U.USDC, mintB: U.HYPE, symbolA: 'USDC', symbolB: 'HYPE', decimalsA: 6, decimalsB: 18,
    tickSpacing: 60, tick, sqrtPriceX96: sp.toString(), price: p, quoteUsd: 1 / p, quoteUsdSource: 'stable A: 1/price',
    nativeSide: null, feePips: 3000, volatile: U.HYPE, poolUsd: 1 / p, referenceUsd: 1 / p, referenceDeviation: 0, ...extra,
  };
}
const CFG = { maxUsd: 260, slippageBps: 100, gasReserve: M.toRaw('0.0005', 18), maxImpact: 0.01, maxOracleDev: 0.02,
  maxFeeWei: M.toRaw('0.5', 9), pin: PIN, profit: '', sleeve: '' };
const rich = { eth: E18 / 100n, rawA: 200n * E6, rawB: 2n * E18 };

// --- addresses ---------------------------------------------------------------------------
test('every address constant is in EIP-55 form (a wrong checksum made viem refuse a v4 quote)', () => {
  for (const a of [U.USDC, U.HYPE, U.WETH, ...Object.values(U.V3), ...Object.values(U.V4)]) assert.strictEqual(a, getAddress(a.toLowerCase()));
  for (const p of U.V4_POOLS) {
    assert.strictEqual(p.currency0, getAddress(p.currency0.toLowerCase()));
    assert.ok(BigInt(p.currency0) < BigInt(p.currency1), 'v4 currencies sorted');
    const id = keccak256(encodeAbiParameters([{ type: 'address' }, { type: 'address' }, { type: 'uint24' }, { type: 'int24' }, { type: 'address' }],
      [p.currency0, p.currency1, p.fee, p.tickSpacing, p.hooks]));
    assert.strictEqual(id, p.id, 'the registry pool id is the hash of its key');
  }
});

test('endpoints: a loopback LPBOT_RPC is the only endpoint, for writes and simulation', () => {
  assert.deepStrictEqual(U.unichainEndpoints({ LPBOT_RPC: 'http://127.0.0.1:8545' }), ['http://127.0.0.1:8545']);
  assert.deepStrictEqual(U.simulationEndpoints({ LPBOT_RPC: 'http://127.0.0.1:8545' }), ['http://127.0.0.1:8545']);
  const pub = U.unichainEndpoints({ LPBOT_RPC: 'https://x.example' });
  assert.strictEqual(pub[0], 'https://x.example'); assert.ok(pub.includes(U.UNICHAIN_PUBLIC));
  assert.ok(!U.simulationEndpoints({}).includes(U.UNICHAIN_PUBLIC), 'mainnet.unichain.org has no eth_simulateV1');
});

// --- settings ----------------------------------------------------------------------------
test('settings: refuses negative or non-numeric limits', () => {
  for (const [k, v] of [['LPBOT_MAX_USD', '-1'], ['LPBOT_SLIPPAGE_BPS', 'x'], ['LPBOT_EVM_MAX_GWEI', 'NaN']]) {
    assert.throws(() => S.settings({ [k]: v }), new RegExp(k));
  }
  const d = S.settings({});
  assert.strictEqual(d.maxUsd, 260); assert.strictEqual(d.maxFeeWei, 500000000n); assert.strictEqual(d.gasReserve, 500000000000000n);
});

// --- price reference ---------------------------------------------------------------------
test('marketRefusals: no reference answering refuses (fail closed)', () => {
  assert.match(S.marketRefusals(poolInfo(90, { referenceUsd: null, referenceDeviation: null }), CFG).join(), /no price reference/);
});

test('marketRefusals: a pool price off the reference by more than the limit refuses; within passes', () => {
  assert.match(S.marketRefusals(poolInfo(90, { referenceUsd: 95, referenceDeviation: 90 / 95 - 1 < 0 ? 1 - 90 / 95 : 0 }), CFG).join(), /from the reference/);
  assert.deepStrictEqual(S.marketRefusals(poolInfo(90, { referenceDeviation: 0.0199 }), CFG), []);
  assert.match(S.marketRefusals(poolInfo(90, { referenceDeviation: 0.0201 }), CFG).join(), /limit 2.00%/);
  assert.match(S.marketRefusals(poolInfo(90, { quoteUsd: null }), CFG).join(), /cannot price HYPE/);
});

test('referencePrices: median of the sources that answer; a failing source is reported, not fatal', async () => {
  const fake = (byHost) => async (url) => {
    const h = new URL(url).hostname;
    if (byHost[h] === 'throw') throw new Error('down');
    if (byHost[h] === 'bad') return { ok: false, json: async () => ({}) };
    return { ok: true, json: async () => byHost[h] };
  };
  const both = await S.referencePrices(U.HYPE, fake({ 'data-api.binance.vision': { price: '90' }, 'api.bybit.com': { result: { list: [{ lastPrice: '92' }] } } }));
  assert.strictEqual(both.usd, 91);
  const one = await S.referencePrices(U.HYPE, fake({ 'data-api.binance.vision': 'throw', 'api.bybit.com': { result: { list: [{ lastPrice: '92' }] } } }));
  assert.strictEqual(one.usd, 92); assert.strictEqual(one.sources[0].usd, null);
  const none = await S.referencePrices(U.HYPE, fake({ 'data-api.binance.vision': 'bad', 'api.bybit.com': { result: { list: [{ lastPrice: '-3' }] } } }));
  assert.strictEqual(none.usd, null);
  assert.strictEqual((await S.referencePrices(U.USDC, fake({}))).usd, null, 'a token without references has none');
});

// --- describe: pool genuineness ------------------------------------------------------------
function fakePub(over = {}) {
  const c = { factory: U.V3.factory, t0: U.USDC, t1: U.HYPE, fee: 3000, ts: 60, mapsTo: POOL, npmF: U.V3.factory, routerF: U.V3.factory, quoterF: U.V3.factory, ...over };
  const sp = over.sp ?? BigInt(poolInfo(90).sqrtPriceX96);
  return {
    async multicall({ contracts }) {
      if (contracts[0].functionName === 'factory' && contracts[0].address === POOL) {
        return [c.factory, c.t0, c.t1, c.fee, c.ts, 10n ** 18n, [sp, 231000, 0, 0, 0, 0, true]];
      }
      return [c.mapsTo, c.npmF, c.routerF, c.quoterF, 6, 18, 'USDC', 'HYPE'];
    },
  };
}
const refs = async () => ({ usd: 90, sources: [] });

test('describe refuses: a pool of another factory, a factory that maps elsewhere, a contract naming another factory', async () => {
  await assert.rejects(S.describe(fakePub({ factory: OTHER }), POOL, refs), /belongs to factory/);
  await assert.rejects(S.describe(fakePub({ mapsTo: OTHER }), POOL, refs), /maps .* to/);
  await assert.rejects(S.describe(fakePub({ npmF: OTHER }), POOL, refs), /position manager .* names factory/);
  await assert.rejects(S.describe(fakePub({ routerF: OTHER }), POOL, refs), /router .* names factory/);
  await assert.rejects(S.describe(fakePub({ quoterF: OTHER }), POOL, refs), /quoter .* names factory/);
});

test('describe: stable token A gives quoteUsd = 1/price and the pool USD price of the volatile token', async () => {
  const d = await S.describe(fakePub(), POOL, refs);
  assert.strictEqual(d.nativeSide, null);
  assert.ok(Math.abs(d.quoteUsd * d.price - 1) < 1e-12);
  assert.ok(Math.abs(d.poolUsd - 90) / 90 < 0.001, `pool usd ${d.poolUsd}`);
  assert.ok(d.referenceDeviation < 0.001);
  assert.strictEqual(d.volatile, U.HYPE);
});

// --- open ----------------------------------------------------------------------------------
test('planOpen refuses: band not around the price, gas below the reserve, over the cap, wallet short', () => {
  const info = poolInfo(90);
  const away = S.planOpen(info, rich, CFG, new Map(), info.price * 1.1, info.price * 1.2, '100', '1');
  assert.match(away.refusals.join(), /outside the band ticks/);
  const poor = S.planOpen(info, { ...rich, eth: 0n }, CFG, new Map(), info.price / 1.02, info.price * 1.02, '100', '1');
  assert.match(poor.refusals.join(), /below the .* gas reserve/);
  const big = S.planOpen(info, { ...rich, rawA: 10_000n * E6, rawB: 100n * E18 }, CFG, new Map(), info.price / 1.02, info.price * 1.02, '10000', '100');
  assert.match(big.refusals.join(), /exceeds cap \$260/);
  const short = S.planOpen(info, { ...rich, rawB: 0n }, CFG, new Map(), info.price / 1.02, info.price * 1.02, '100', '1');
  assert.match(short.refusals.join(), /wallet lacks .* HYPE/);
  const ref = S.planOpen(poolInfo(90, { referenceUsd: null }), rich, CFG, new Map(), info.price / 1.02, info.price * 1.02, '100', '1');
  assert.match(ref.refusals.join(), /no price reference/);
});

test('planOpen: a clean open deposits within the caps and the dollar figure uses 1/price', () => {
  const info = poolInfo(90);
  const p = S.planOpen(info, rich, CFG, new Map(), info.price / 1.02, info.price * 1.02, '100', '1');
  assert.deepStrictEqual(p.refusals, []);
  assert.ok(p.dep.amountA <= 100n * E6 && p.dep.amountB <= E18);
  assert.ok(Math.abs(p.approxUsd - (p.amtA + p.amtB * 90)) < 0.2, `usd ${p.approxUsd} vs ${p.amtA + p.amtB * 90}`);
  assert.ok(p.minA <= p.dep.amountA && p.minB <= p.dep.amountB);
});

test('planOpen property: never deposits more than a cap or the sleeve; more than the wallet is a refusal', () => {
  fc.assert(fc.property(
    fc.double({ min: 20, max: 300, noNaN: true }), fc.double({ min: 0.001, max: 300, noNaN: true }), fc.double({ min: 0.0001, max: 3, noNaN: true }),
    fc.double({ min: 1.003, max: 1.1, noNaN: true }), fc.option(fc.double({ min: 0, max: 300, noNaN: true }), { nil: null }),
    fc.bigInt({ min: 0n, max: 400n * 10n ** 6n }), fc.bigInt({ min: 0n, max: 4n * 10n ** 18n }),
    (usd, capA, capB, k, sleeveA, walletA, walletB) => {
      const info = poolInfo(usd);
      const sleeve = sleeveA == null ? new Map() : new Map([[U.USDC.toLowerCase(), sleeveA]]);
      const p = S.planOpen(info, { eth: E18, rawA: walletA, rawB: walletB }, { ...CFG, maxUsd: 1e9 }, sleeve,
        info.price / k, info.price * k, String(capA), String(capB));
      assert.ok(p.dep.amountA <= M.toRaw(String(capA), 6), 'cap A');
      assert.ok(p.dep.amountB <= M.toRaw(String(capB), 18), 'cap B');
      if (sleeveA != null) assert.ok(p.dep.amountA <= M.toRaw(sleeveA, 6), 'sleeve A');
      if (p.dep.amountA > walletA) assert.match(p.refusals.join(), /wallet lacks .* USDC/);
      if (p.dep.amountB > walletB) assert.match(p.refusals.join(), /wallet lacks .* HYPE/);
    }), { numRuns: 500 });
});

// --- status ---------------------------------------------------------------------------------
test('positionView: principal and fees in dollars with stable A; rent is zero; inRange is tick-based', () => {
  const info = poolInfo(90);
  const tl = Math.floor((info.tick - 300) / 60) * 60, tu = Math.ceil((info.tick + 300) / 60) * 60;
  const x = { tokenId: 7n, tickLower: tl, tickUpper: tu, liquidity: 10n ** 15n };
  const v = S.positionView(x, info, [1_000_000n, 10n ** 16n]);          // 1 USDC + 0.01 HYPE of fees
  assert.strictEqual(v.feesAccrued_USD, Number((1 + 0.01 * (1 / info.price)).toFixed(6)));
  assert.ok(Math.abs(v.positionUsd - (v.closeEstA + v.closeEstB / info.price)) < 1e-3);
  assert.strictEqual(v.rentUsd, 0); assert.strictEqual(v.inRange, true); assert.strictEqual(v.positionMint, '7');
  assert.strictEqual(S.positionView({ ...x, tickLower: info.tick + 60, tickUpper: info.tick + 600 }, info, [0n, 0n]).inRange, false);
});

test('closeCalls: decrease all liquidity at slippage, collect, burn; no decrease without liquidity', () => {
  const info = poolInfo(90);
  const x = { tokenId: 7n, tickLower: info.tick - 600, tickUpper: info.tick + 600, liquidity: 10n ** 15n };
  const { calls, estA, estB } = S.closeCalls(x, info, OTHER, CFG, 123n);
  assert.strictEqual(calls.length, 3);
  const dec = decodeFunctionData({ abi: U.NPM_ABI, data: calls[0] });
  assert.strictEqual(dec.functionName, 'decreaseLiquidity');
  assert.strictEqual(dec.args[0].liquidity, x.liquidity);
  assert.strictEqual(dec.args[0].amount0Min, M.minWithSlippage(estA, 100));
  assert.strictEqual(dec.args[0].amount1Min, M.minWithSlippage(estB, 100));
  assert.deepStrictEqual(calls.slice(1).map(c => decodeFunctionData({ abi: U.NPM_ABI, data: c }).functionName), ['collect', 'burn']);
  assert.strictEqual(S.closeCalls({ ...x, liquidity: 0n }, info, OTHER, CFG, 1n).calls.length, 2);
});

// --- v4 swap ---------------------------------------------------------------------------------
test('v4PoolsFor: only hookless pools of exactly this pair, either order', () => {
  assert.strictEqual(S.v4PoolsFor(U.USDC, U.HYPE).length, 1);
  assert.strictEqual(S.v4PoolsFor(U.HYPE, U.USDC).length, 1);
  assert.strictEqual(S.v4PoolsFor(U.USDC, U.WETH).length, 0);
  const hooked = [{ ...U.V4_POOLS[0], hooks: OTHER }];
  assert.strictEqual(S.v4PoolsFor(U.USDC, U.HYPE, hooked).length, 0, 'a pool with a hook is never used');
});

test('v4SwapCalldata: V4_SWAP with swap / settle-all / take-all; direction and limits round-trip', () => {
  const p = U.V4_POOLS[0];
  for (const [tokIn, tokOut, zeroForOne] of [[U.USDC, U.HYPE, true], [U.HYPE, U.USDC, false]]) {
    const data = S.v4SwapCalldata(p, tokIn, 123n, 45n, 999n);
    const { functionName, args } = decodeFunctionData({ abi: U.UNIVERSAL_ROUTER_ABI, data });
    assert.strictEqual(functionName, 'execute');
    assert.strictEqual(args[0], '0x10'); assert.strictEqual(args[2], 999n);
    const [actions, params] = decodeAbiParameters([{ type: 'bytes' }, { type: 'bytes[]' }], args[1][0]);
    assert.strictEqual(actions, '0x060c0f');
    const [swap] = decodeAbiParameters([{ type: 'tuple', components: [
      { name: 'poolKey', type: 'tuple', components: [{ name: 'currency0', type: 'address' }, { name: 'currency1', type: 'address' }, { name: 'fee', type: 'uint24' }, { name: 'tickSpacing', type: 'int24' }, { name: 'hooks', type: 'address' }] },
      { name: 'zeroForOne', type: 'bool' }, { name: 'amountIn', type: 'uint128' }, { name: 'amountOutMinimum', type: 'uint128' }, { name: 'hookData', type: 'bytes' }] }], params[0]);
    assert.strictEqual(swap.zeroForOne, zeroForOne); assert.strictEqual(swap.amountIn, 123n); assert.strictEqual(swap.amountOutMinimum, 45n);
    assert.strictEqual(swap.poolKey.hooks, '0x0000000000000000000000000000000000000000');
    const [settleTok, settleMax] = decodeAbiParameters([{ type: 'address' }, { type: 'uint256' }], params[1]);
    const [takeTok, takeMin] = decodeAbiParameters([{ type: 'address' }, { type: 'uint256' }], params[2]);
    assert.strictEqual(settleTok, tokIn); assert.strictEqual(settleMax, 123n, 'pays at most amountIn');
    assert.strictEqual(takeTok, tokOut); assert.strictEqual(takeMin, 45n, 'receives at least minOut');
  }
});

// --- simulation gate ---------------------------------------------------------------------------
test('blockingFailure: a sequenced simulation blocks on any revert; a per-call one only before an approval', () => {
  const seq = [{ label: 'approve x', ok: true, sequence: true }, { label: 'mint', ok: false, revert: 'STF', sequence: true }];
  assert.strictEqual(S.blockingFailure(seq).label, 'mint');
  const solo = [{ label: 'approve x', ok: true, sequence: false }, { label: 'mint', ok: false, revert: 'STF', sequence: false }];
  assert.strictEqual(S.blockingFailure(solo), null, 'a call after an approval is checked again before its send');
  const soloBad = [{ label: 'mint', ok: false, revert: 'x', sequence: false }];
  assert.strictEqual(S.blockingFailure(soloBad).label, 'mint');
  assert.strictEqual(S.blockingFailure([{ label: 'permit2 t', ok: true, sequence: false }, { label: 'swap', ok: false, sequence: false }]), null);
  assert.strictEqual(S.blockingFailure([{ label: 'swap', ok: true, sequence: true }]), null);
});

test('simulateSequence: falls back to per-call calls when no endpoint answers eth_simulateV1', async () => {
  const failing = { simulateBlocks: async () => { throw new Error('method does not exist'); } };
  const pub = { ...failing, call: async ({ data }) => { if (data === '0xbad') throw new Error('execution reverted: STF'); return '0x'; } };
  const out = await S.simulateSequence(pub, OTHER, [{ label: 'a', to: OTHER, data: '0x01' }, { label: 'b', to: OTHER, data: '0xbad' }], { clients: [failing, failing] });
  assert.deepStrictEqual(out.map(o => [o.label, o.ok, o.sequence]), [['a', true, false], ['b', false, false]]);
  const okClient = { simulateBlocks: async () => [{ calls: [{ status: 'success', gasUsed: 5n }] }] };
  const out2 = await S.simulateSequence(pub, OTHER, [{ label: 'a', to: OTHER, data: '0x01' }], { clients: [failing, okClient] });
  assert.deepStrictEqual(out2.map(o => [o.ok, o.sequence, o.gasUsed]), [[true, true, '5']]);
});

// --- sending ---------------------------------------------------------------------------------------
test('runSteps: throws when nothing was sent; reports partial after the first send, never retries', async () => {
  const pub = { sendRawTransaction: async () => '0x', waitForTransactionReceipt: async () => ({ status: 'success', gasUsed: 1n, effectiveGasPrice: 1n, logs: [] }) };
  await assert.rejects(S.runSteps({ pub, nonce: 0 }, [{ label: 'a' }], CFG, { signStep: async () => { throw new Error('refused: no gas'); } }), /no gas/);
  let n = 0;
  const sign = async () => { n += 1; if (n === 2) throw new Error('boom'); return { serialized: '0x', hash: `0x${n}` }; };
  const r = await S.runSteps({ pub, nonce: 0 }, [{ label: 'a' }, { label: 'b' }, { label: 'c' }], CFG, { signStep: sign });
  assert.deepStrictEqual(r.sent.map(s => s.hash), ['0x1']); assert.match(r.error, /boom/);
  assert.strictEqual(n, 2, 'nothing after the failure');
  const rev = { ...pub, waitForTransactionReceipt: async () => ({ status: 'reverted' }) };
  const r2 = await S.runSteps({ pub: rev, nonce: 0 }, [{ label: 'a' }], CFG, { signStep: async () => ({ serialized: '0x', hash: '0xaa' }) });
  assert.deepStrictEqual(r2.sent.map(s => s.hash), ['0xaa'], 'a signed, sent, reverted tx still counts'); assert.match(r2.error, /reverted on chain/);
});

test('checkRecipient: only the pinned profit wallet, never the LP wallet', () => {
  assert.throws(() => S.checkRecipient(PIN, { ...CFG, pin: '' }), /PIN is not set/);
  assert.throws(() => S.checkRecipient(PIN, { ...CFG, pin: 'nope' }), /not an address/);
  assert.throws(() => S.checkRecipient('x', CFG), /destination x is not an address/);
  assert.throws(() => S.checkRecipient(OTHER, CFG), /not the pinned profit wallet/);
  assert.throws(() => S.checkRecipient(PIN, { ...CFG, profit: OTHER }), /not the pinned profit wallet/);
  assert.throws(() => S.checkRecipient(PIN, CFG, PIN), /equals the LP wallet/);
  assert.strictEqual(S.checkRecipient(PIN.toLowerCase(), CFG), PIN);
});

test('parseArgs: --execute and --pool <x> are flags, the rest is positional', () => {
  assert.deepStrictEqual(S.parseArgs(['open', '--pool', POOL, '1', '2', '--execute']), { execute: true, cmd: 'open', args: ['1', '2'] });
});

// --- the CLI, offline ---------------------------------------------------------------------------
test('CLI: a bad recipient is refused before any key or network', () => {
  const r = run(['send', U.USDC, '1', OTHER, '--execute'], { LPBOT_EVM_PROFIT_WALLET_PIN: PIN });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /not the pinned profit wallet/);
});

test('CLI: a HALT in the run directory refuses a write before the network', () => {
  const d = scratch(); fs.writeFileSync(path.join(d, 'HALT'), 'operator halt');
  const r = run(['open', POOL, '0.01', '0.012', '1', '1', '--execute'], { LPBOT_RUN_DIR: d });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /HALT present: operator halt/);
});

test('CLI: the only endpoint is the closed loopback port: reads fail, nothing reaches mainnet', () => {
  const r = run(['pool']);
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /all RPC endpoints failed: 127\.0\.0\.1:9/);
});

test('mutation gaps: endpoint lists carry no empty entry; a public LPBOT_RPC is never a simulation endpoint', () => {
  for (const e of [{}, { LPBOT_RPC: '' }, { LPBOT_UNICHAIN_RPC: 'https://y.example' }]) {
    assert.ok(U.unichainEndpoints(e).every(Boolean), JSON.stringify(U.unichainEndpoints(e)));
  }
  assert.ok(!U.simulationEndpoints({ LPBOT_RPC: 'https://x.example' }).includes('https://x.example'));
});

test('mutation gaps: --pool first, then the command', () => {
  assert.deepStrictEqual(S.parseArgs(['--pool', POOL, 'status']), { execute: false, cmd: 'status', args: [] });
});

test('mutation gaps: describe names the quote source; no reference gives no deviation', async () => {
  const d = await S.describe(fakePub(), POOL, async () => ({ usd: null, sources: [] }));
  assert.strictEqual(d.quoteUsdSource, 'stable A: 1/price');
  assert.strictEqual(d.referenceDeviation, null);
  const b = await S.describe(fakePub({ t0: U.HYPE, t1: U.USDC }), POOL, refs);
  assert.strictEqual(b.quoteUsdSource, 'stable'); assert.strictEqual(b.quoteUsd, 1);
  const neither = await S.describe(fakePub({ t0: U.HYPE, t1: U.WETH }), POOL, refs);
  assert.strictEqual(neither.quoteUsdSource, 'unknown'); assert.strictEqual(neither.quoteUsd, null);
});

test('mutation gaps: describe by token order; price zero prices nothing; references used for a stable A', async () => {
  const d = await S.describe(fakePub(), POOL, refs);
  assert.strictEqual(d.referenceUsd, 90); assert.strictEqual(typeof d.referenceDeviation, 'number');
  const b = await S.describe(fakePub({ t0: U.HYPE, t1: U.USDC }), POOL, refs);
  assert.strictEqual(b.volatile, U.HYPE); assert.strictEqual(b.poolUsd, b.price);
  const neither = await S.describe(fakePub({ t0: U.HYPE, t1: U.WETH }), POOL, refs);
  assert.strictEqual(neither.volatile, null); assert.strictEqual(neither.poolUsd, null); assert.strictEqual(neither.referenceUsd, null);
  const zero = await S.describe(fakePub({ sp: 0n }), POOL, refs);
  assert.strictEqual(zero.quoteUsd, null); assert.strictEqual(zero.poolUsd, null);
});

test('mutation gaps: band edges are inclusive below, exclusive above (tickLower <= tick < tickUpper)', () => {
  const base = poolInfo(90);
  const T = Math.floor(base.tick / 60) * 60;
  const info = { ...base, tick: T, sqrtPriceX96: M.sqrtRatioAtTick(T).toString(), price: M.priceAtTick(T, 6, 18) };
  const atLower = S.planOpen(info, rich, CFG, new Map(), M.priceAtTick(T, 6, 18) * (1 + 1e-9), M.priceAtTick(T + 600, 6, 18), '100', '1');
  assert.strictEqual(atLower.tickLower, T);
  assert.ok(!atLower.refusals.join().includes('outside the band'), atLower.refusals.join());
  const x = { tokenId: 1n, tickLower: T, tickUpper: T + 600, liquidity: 10n ** 12n };
  assert.strictEqual(S.positionView(x, info, [0n, 0n]).inRange, true, 'tick == tickLower is in range');
  assert.strictEqual(S.positionView({ ...x, tickLower: T - 600, tickUpper: T }, info, [0n, 0n]).inRange, false, 'tick == tickUpper is out');
  const v = S.positionView(x, info, [2_000_000n, 3n * 10n ** 16n]);
  assert.strictEqual(v.feesAccruedA, 2); assert.strictEqual(v.feesAccruedB, 0.03);
});

test('mutation gaps: an unpriceable open says "$?"; a v4 pool of another pair is not used', () => {
  const info = poolInfo(90, { quoteUsd: null });
  const p = S.planOpen(info, rich, CFG, new Map(), info.price / 1.02, info.price * 1.02, '100', '1');
  assert.match(p.refusals.join(), /position about \$\? exceeds/);
  const other = [{ ...U.V4_POOLS[0], currency1: U.WETH }];
  assert.strictEqual(S.v4PoolsFor(U.USDC, U.HYPE, other).length, 0);
  assert.strictEqual(S.v4PoolsFor(U.HYPE, U.HYPE, U.V4_POOLS).length, 0);
});

test('mutation gaps: a sequenced simulation reports reverts only for failed calls', async () => {
  const client = { simulateBlocks: async () => [{ calls: [{ status: 'success', gasUsed: 1n }, { status: 'failure', error: { reason: 'STF' } }] }] };
  const out = await S.simulateSequence({}, OTHER, [{ label: 'a', to: OTHER, data: '0x' }, { label: 'b', to: OTHER, data: '0x' }], { clients: [client] });
  assert.deepStrictEqual(out.map(o => [o.ok, o.revert]), [[true, null], [false, 'STF']]);
});

test('mutation gaps: another LP wallet address is fine as long as it is not the pin', () => {
  assert.strictEqual(S.checkRecipient(PIN, CFG, OTHER), PIN);
});

test('boundaries: zero limits are allowed, an empty setting is the default', () => {
  assert.strictEqual(S.settings({ LPBOT_MAX_USD: '0' }).maxUsd, 0);
  assert.strictEqual(S.settings({ LPBOT_MAX_USD: '' }).maxUsd, 260);
});

test('boundaries: a deviation exactly at the limit passes; a zero reference price is no price', async () => {
  assert.deepStrictEqual(S.marketRefusals(poolInfo(90, { referenceDeviation: 0.02 }), CFG), []);
  const zero = await S.referencePrices(U.HYPE, async (url) => ({ ok: true, json: async () => (url.includes('binance') ? { price: '0' } : { result: { list: [{ lastPrice: '0' }] } }) }));
  assert.strictEqual(zero.usd, null);
});

test('boundaries: planOpen at the exact edges', () => {
  const base = poolInfo(90);
  const T = Math.floor(base.tick / 60) * 60;
  const info = { ...base, tick: T, sqrtPriceX96: M.sqrtRatioAtTick(T).toString(), price: M.priceAtTick(T, 6, 18) };
  // the band's upper tick is the current tick: out of range (tick < tickUpper is required)
  const atUpper = S.planOpen(info, rich, CFG, new Map(), M.priceAtTick(T - 600, 6, 18) * (1 + 1e-9), M.priceAtTick(T, 6, 18) * (1 - 1e-12), '100', '1');
  assert.strictEqual(atUpper.tickUpper, T);
  assert.match(atUpper.refusals.join(), /outside the band ticks/);
  const lo = base.price / 1.02, hi = base.price * 1.02;
  // gas exactly at the reserve is enough
  assert.ok(!S.planOpen(base, { ...rich, eth: CFG.gasReserve }, CFG, new Map(), lo, hi, '100', '1').refusals.join().includes('gas reserve'));
  // a wallet holding exactly the deposit is enough
  const p = S.planOpen(base, rich, CFG, new Map(), lo, hi, '100', '1');
  const exact = S.planOpen(base, { ...rich, rawA: p.dep.amountA, rawB: p.dep.amountB }, CFG, new Map(), lo, hi, '100', '1');
  assert.ok(!exact.refusals.join().includes('wallet lacks'), exact.refusals.join());
  // a position exactly at the cap is allowed; one cent over is refused, with its dollar figure
  assert.ok(!S.planOpen(base, rich, { ...CFG, maxUsd: p.approxUsd }, new Map(), lo, hi, '100', '1').refusals.join().includes('exceeds cap'));
  const over = S.planOpen(base, rich, { ...CFG, maxUsd: p.approxUsd - 0.01 }, new Map(), lo, hi, '100', '1');
  assert.match(over.refusals.join(), new RegExp(`position about \\$${p.approxUsd.toFixed(2).replace('.', '\\.')} exceeds cap`));
});
