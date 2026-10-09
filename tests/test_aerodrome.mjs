// venues/aerodrome/signer.mjs and its helpers (chains/evm/clmath.mjs, chains/evm/rpc.mjs), without a node:
// the money arithmetic, every refusal, the endpoint policy and the partial-send report.
// Failure paths first. No network: the CLI cases use a closed loopback port, which
// baseEndpoints() makes the ONLY endpoint, so nothing can reach mainnet.
import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import { spawnSync } from 'node:child_process';
import fc from 'fast-check';
import * as M from '../chains/evm/clmath.mjs';
import { baseEndpoints, evmErrorKind, overBase, isLoopback } from '../chains/evm/rpc.mjs';
import { AfterSignError } from '../shared/rpc_policy.mjs';
import * as S from '../venues/aerodrome/signer.mjs';
import { WETH, USDC, DEPLOYMENTS, ETH_USD_FEED } from '../chains/evm/base_addresses.mjs';

const dir = path.dirname(new URL(import.meta.url).pathname);
const script = path.join(dir, '..', 'venues/aerodrome/signer.mjs');
const POOL = '0xb2cc224c1c9feE385f8ad6a55b4d94E92359DC59';
const PIN = '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142';
const OTHER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8';
const E18 = 10n ** 18n;
const [INITIAL, V3] = DEPLOYMENTS;
const NPM = INITIAL.npm;
const POOL_V3 = '0x3FE04A59Ebd38cF06080a6F60a98D124eb59392A';
// Real addresses that are NOT in the registry: the "Gauge Caps" deployment's factory and NPM,
// and the initial factory's own WETH/USDC tickSpacing-50 pool (the pool the initial router
// would swap through if it were paired with tickSpacing 50).
const CAPS_FACTORY = '0xaDe65c38CD4849aDBA595a4323a8C7DdfE89716a';
const CAPS_NPM = '0xa990C6a764b73BF43cee5Bb40339c3322FB9D55F';
const INITIAL_TS50_POOL = '0xAaD23a67F2AC693ABBe543489aeB3F24F561D517';

const made = [];
test.after(() => { for (const x of made) fs.rmSync(x, { recursive: true, force: true }); });

function scratch() {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'aero-'));
  made.push(d);
  fs.chmodSync(d, 0o700);
  return d;
}

function run(args, env = {}) {
  return spawnSync('node', [script, ...args], {
    env: { PATH: process.env.PATH, WALLET_SECRET_PATH: '/nonexistent', LPBOT_RPC: 'http://127.0.0.1:9', LPBOT_POOL: POOL, ...env },
    encoding: 'utf8', timeout: 60_000,
  });
}

// A WETH/USDC pool view at `price`, as describe() returns it.
function poolInfo(price = 2700, extra = {}) {
  const tick = M.tickAtPrice(price, 18, 6);
  const sp = M.sqrtRatioAtTick(tick);
  return {
    pool: POOL, mintA: WETH, mintB: USDC, symbolA: 'WETH', symbolB: 'USDC', decimalsA: 18, decimalsB: 6,
    tickSpacing: 100, tick, sqrtPriceX96: sp.toString(), price: M.priceFromSqrtX96(sp, 18, 6),
    quoteUsd: 1, quoteUsdSource: 'stable', nativeSide: 'A',
    ethUsd: price, oracleAgeS: 30, poolEthUsd: price, oracleDeviation: 0, unstakedFee: 0.05, feePips: 500, ...extra,
  };
}

const CFG = { maxUsd: 260, slippageBps: 100, gasReserve: M.toRaw('0.002', 18), maxImpact: 0.01, maxOracleDev: 0.02,
  maxFeeWei: M.toRaw('2', 9), pin: PIN, profit: '', sleeve: '' };
const rich = { eth: 1n * E18, rawA: 0n, rawB: 1000n * 10n ** 6n };

// --- deployment registry (failure paths first) ------------------------------------------
// A stand-in for the public client: answers describe()'s two multicalls from `chain` and
// records every contract call. `chain` describes a pool of deployment `dep`.
function fakeChain(over = {}) {
  const dep = over.dep ?? V3;
  const c = {
    pool: POOL_V3, factory: dep.factory, nft: dep.npm, spacing: 50, mapsTo: over.pool ?? POOL_V3,
    npmFactory: dep.factory, routerFactory: dep.factory, quoterFactory: dep.factory,
    t0: WETH, t1: USDC, liquidity: 4n * E18, staked: 3n * E18, feeModule: OTHER, ethAnswer: 270000000000n, ...over,
  };
  const tick = M.tickAtPrice(2700, 18, 6);
  const asked = [];
  const now = BigInt(Math.floor(Date.now() / 1000));
  const answer = ({ address, functionName, args }) => {
    asked.push({ address, functionName, args });
    const at = a => address.toLowerCase() === a.toLowerCase();
    if (at(c.pool)) {
      return { factory: c.factory, token0: c.t0, token1: c.t1, tickSpacing: c.spacing, fee: 500, unstakedFee: 50000,
        liquidity: c.liquidity, stakedLiquidity: c.staked, gauge: OTHER, nft: c.nft,
        slot0: [M.sqrtRatioAtTick(tick), tick, 0, 1, 1, true] }[functionName];
    }
    if (functionName === 'getPool') return c.mapsTo;
    if (functionName === 'factory') {
      if (at(dep.npm)) return c.npmFactory;
      if (at(dep.router)) return c.routerFactory;
      if (at(dep.quoter)) return c.quoterFactory;
    }
    if (at(ETH_USD_FEED)) return functionName === 'decimals' ? 8 : [1n, c.ethAnswer, now, now, 1n];
    if (functionName === 'decimals') return at(USDC) ? 6 : 18;
    if (functionName === 'symbol') return at(WETH) ? 'WETH' : at(USDC) ? 'USDC' : 'OTH';
    if (functionName === 'swapFeeModule') return c.feeModule;
    if (functionName === 'tickSpacingToFee') return 500;
    throw new Error(`fakeChain: unexpected ${functionName} on ${address}`);
  };
  return { asked, pub: { multicall: async ({ contracts }) => contracts.map(answer) } };
}

test('registry: a pool of an unknown factory is refused before anything is asked of that factory', async () => {
  const { pub, asked } = fakeChain({ factory: CAPS_FACTORY, nft: CAPS_NPM });
  await assert.rejects(S.describe(pub, POOL_V3), /belongs to factory 0xaDe65c38.*not a known Slipstream deployment/);
  assert.ok(asked.every(a => a.address === POOL_V3), 'a call reached a contract of the unknown deployment');
  assert.throws(() => S.deploymentOf(POOL_V3, CAPS_FACTORY, CAPS_NPM), /not a known Slipstream deployment/);
});

test('registry: a pool naming another deployment\'s NPM is refused', async () => {
  for (const [factory, nft] of [[V3.factory, INITIAL.npm], [INITIAL.factory, V3.npm], [V3.factory, CAPS_NPM]]) {
    const { pub } = fakeChain({ factory, nft });
    await assert.rejects(S.describe(pub, POOL_V3), /names position manager .* not 0x.* of factory/, `${factory} ${nft}`);
    assert.throws(() => S.deploymentOf(POOL_V3, factory, nft), /names position manager/);
  }
});

test('registry: a pool the factory does not map back to is refused', async () => {
  // a contract that answers like a pool of V3, but V3's getPool returns another pool or none
  for (const mapsTo of [INITIAL_TS50_POOL, '0x0000000000000000000000000000000000000000']) {
    const { pub } = fakeChain({ mapsTo });
    await assert.rejects(S.describe(pub, POOL_V3), /factory 0xf8f2eB49.* maps .* to 0x.*, not 0x3FE04A59/);
  }
});

test('registry: an NPM, router or quoter naming another factory is refused', async () => {
  for (const k of ['npmFactory', 'routerFactory', 'quoterFactory']) {
    const { pub } = fakeChain({ [k]: INITIAL.factory });
    await assert.rejects(S.describe(pub, POOL_V3), /names factory 0x5e7BB104.*, not 0xf8f2eB49/, k);
  }
});

test('registry: getPool is asked of the pool\'s own factory, with the pool\'s tokens and spacing', async () => {
  const { pub, asked } = fakeChain();
  const info = await S.describe(pub, POOL_V3);
  const get = asked.filter(a => a.functionName === 'getPool');
  assert.deepStrictEqual(get.map(a => [a.address, ...a.args]), [[V3.factory, WETH, USDC, 50]]);
  assert.deepStrictEqual([info.deployment, info.npm, info.router, info.quoter, info.tickSpacing], ['gauges-v3', V3.npm, V3.router, V3.quoter, 50]);
  const fac = asked.filter(a => a.functionName === 'factory' && a.address !== POOL_V3).map(a => a.address);
  assert.deepStrictEqual(fac, [V3.npm, V3.router, V3.quoter]);
  assert.ok(asked.filter(a => ['swapFeeModule', 'tickSpacingToFee'].includes(a.functionName)).every(a => a.address === V3.factory));
  const old = fakeChain({ dep: INITIAL, pool: POOL, spacing: 100 });
  const oi = await S.describe(old.pub, POOL);
  assert.deepStrictEqual([oi.deployment, oi.npm, oi.router, oi.quoter, oi.tickSpacing], ['initial', INITIAL.npm, INITIAL.router, INITIAL.quoter, 100]);
});

test('describe: quote, native side, oracle check and shares follow the token order', async () => {
  const d = async over => S.describe(fakeChain(over).pub, POOL_V3);
  const wu = await d({});
  assert.deepStrictEqual([wu.quoteUsd, wu.quoteUsdSource, wu.nativeSide, wu.poolEthUsd, wu.stakedShare, wu.dynamicFee],
    [1, 'stable', 'A', wu.price, 0.75, true]);
  assert.ok(Math.abs(wu.oracleDeviation - Math.abs(wu.price / 2700 - 1)) < 1e-12);
  const uw = await d({ t0: USDC, t1: WETH });
  assert.deepStrictEqual([uw.quoteUsd, uw.quoteUsdSource, uw.nativeSide, uw.poolEthUsd], [2700, 'chainlink', 'B', 1 / uw.price]);
  for (const [t0, t1, side] of [[WETH, OTHER, 'A'], [OTHER, WETH, 'B'], [OTHER, USDC, null]]) {
    const x = await d({ t0, t1 });
    assert.deepStrictEqual([x.poolEthUsd, x.oracleDeviation, x.nativeSide], [null, null, side], `${t0}/${t1}`);
    assert.deepStrictEqual([x.quoteUsd, x.quoteUsdSource], t1 === USDC ? [1, 'stable'] : t1 === WETH ? [2700, 'chainlink'] : [null, 'unknown']);
  }
  const dead = await d({ ethAnswer: 0n, liquidity: 0n, staked: 0n, feeModule: '0x0000000000000000000000000000000000000000' });
  assert.deepStrictEqual([dead.oracleDeviation, dead.stakedShare, dead.dynamicFee, dead.ethUsd], [null, null, false, 0]);
});

test('registry: entries are distinct, checksummed, and each part belongs to one deployment', () => {
  const all = DEPLOYMENTS.flatMap(d => [d.factory, d.npm, d.router, d.quoter]);
  assert.strictEqual(new Set(all.map(a => a.toLowerCase())).size, all.length);
  for (const a of all) assert.match(a, /^0x[0-9a-fA-F]{40}$/);
  for (const d of DEPLOYMENTS) assert.strictEqual(S.deploymentOf(POOL, d.factory.toLowerCase(), d.npm.toUpperCase().replace('0X', '0x')), d);
  assert.ok(Object.isFrozen(DEPLOYMENTS) && DEPLOYMENTS.every(Object.isFrozen));
});

test('ownPositions: reads the pool\'s own NPM only; keeps this pool\'s live NFTs, sorted', async () => {
  const info = { ...poolInfo(2700, { tickSpacing: 50 }), npm: V3.npm };
  const pos = (t0, t1, ts, lo, hi, L, o0 = 0n, o1 = 0n) => [0n, OTHER, t0, t1, ts, lo, hi, L, 0n, 0n, o0, o1];
  const book = {
    9n: pos(WETH, USDC, 50, -200, 100, 5n),
    3n: pos(WETH, USDC, 50, -100, 50, 0n, 0n, 7n),        // empty but owed fees: kept
    4n: pos(WETH, USDC, 50, -100, 50, 0n),                // spent: dropped
    5n: pos(WETH, USDC, 100, -100, 100, 5n),              // another spacing: another pool
    6n: pos(USDC, WETH, 50, -100, 50, 5n),                // tokens swapped: another pool
    7n: pos(WETH, OTHER, 50, -100, 50, 5n),               // another token: another pool
    8n: pos(OTHER, USDC, 50, -100, 50, 5n),
    2n: pos(WETH, USDC, 50, -50, 50, 0n, 1n, 0n),         // owed A only: kept
  };
  const ids = [9n, 3n, 4n, 5n, 6n, 7n, 8n, 2n];          // the NPM's order: not sorted
  const asked = [];
  const pub = {
    readContract: async ({ address, functionName }) => { asked.push(address); assert.strictEqual(functionName, 'balanceOf'); return BigInt(ids.length); },
    multicall: async ({ contracts }) => contracts.map(({ address, functionName, args }) => {
      asked.push(address);
      return functionName === 'tokenOfOwnerByIndex' ? ids[Number(args[1])] : book[args[0]];
    }),
  };
  const got = await S.ownPositions(pub, OTHER, info);
  assert.deepStrictEqual(got.map(x => x.tokenId), [2n, 3n, 9n]);
  assert.deepStrictEqual(got[2], { tokenId: 9n, tickLower: -200, tickUpper: 100, liquidity: 5n, owed0: 0n, owed1: 0n });
  assert.ok(asked.length > 0 && asked.every(a => a === V3.npm), `read another NPM: ${asked}`);
  const none = { readContract: async () => 0n, multicall: async () => assert.fail('no NFTs: nothing to list') };
  assert.deepStrictEqual(await S.ownPositions(none, OTHER, info), []);
  const many = { readContract: async () => 201n };
  await assert.rejects(S.ownPositions(many, OTHER, info), /holds 201 position NFTs; refusing to scan more than 200/);
  const at200 = { readContract: async () => 200n, multicall: async ({ contracts }) =>
    contracts.map(c => (c.functionName === 'positions' ? pos(WETH, USDC, 50, 0, 50, 1n) : c.args[1] + 1n)) };
  assert.strictEqual((await S.ownPositions(at200, OTHER, info)).length, 200, 'exactly the limit is scanned');
});

test('tickSpacing 50: the open snaps its band to multiples of 50, not 100', () => {
  const info = poolInfo(2700, { tickSpacing: 50 });
  let off100 = 0;
  for (let k = 0; k < 40; k++) {
    const lo = 2600 + k * 1.37, hi = 2800 + k * 0.91;
    const p = S.planOpen(info, rich, CFG, new Map(), lo, hi, 0.02, 60);
    assert.ok(p.tickLower % 50 === 0 && p.tickUpper % 50 === 0, `${p.tickLower} ${p.tickUpper}`);
    assert.ok(M.priceAtTick(p.tickLower, 18, 6) <= lo * (1 + 1e-9) && M.priceAtTick(p.tickUpper, 18, 6) >= hi * (1 - 1e-9));
    // snapped outward by less than one spacing: tighter than a spacing-100 band can be
    assert.ok(M.priceAtTick(p.tickLower + 50, 18, 6) > lo && M.priceAtTick(p.tickUpper - 50, 18, 6) < hi);
    if (p.tickLower % 100 || p.tickUpper % 100) off100++;
    const wide = S.planOpen(poolInfo(2700), rich, CFG, new Map(), lo, hi, 0.02, 60);
    assert.ok(wide.tickLower <= p.tickLower && wide.tickUpper >= p.tickUpper);
  }
  assert.ok(off100 > 0, 'every band landed on a multiple of 100: spacing 50 never used');
});

// --- refusals (failure paths first) ----------------------------------------------------
test('open refuses a price outside the band', () => {
  const p = S.planOpen(poolInfo(2700), rich, CFG, new Map(), 2800, 2900, 0.05, 100);
  assert.ok(p.refusals.some(r => /outside the band/.test(r)), p.refusals.join('; '));
});

test('open refuses over LPBOT_MAX_USD', () => {
  const p = S.planOpen(poolInfo(2700), rich, { ...CFG, maxUsd: 50 }, new Map(), 2600, 2800, 0.05, 100);
  assert.ok(p.refusals.some(r => /exceeds cap \$50/.test(r)), p.refusals.join('; '));
});

test('open refuses when ETH is below the gas reserve', () => {
  const p = S.planOpen(poolInfo(2700), { eth: M.toRaw('0.001', 18), rawA: E18, rawB: 1000n * 10n ** 6n }, CFG, new Map(), 2600, 2800, 0.01, 20);
  assert.ok(p.refusals.some(r => /below the 0.002 gas reserve/.test(r)), p.refusals.join('; '));
});

test('open refuses when WETH + ETH above the reserve cannot cover side A', () => {
  // 0.01 ETH, reserve 0.002: 0.008 spendable, the band needs more
  const p = S.planOpen(poolInfo(2700), { eth: M.toRaw('0.01', 18), rawA: 0n, rawB: 1000n * 10n ** 6n }, CFG, new Map(), 2600, 2800, 0.05, 100);
  assert.ok(p.refusals.some(r => /wallet lacks .* WETH/.test(r)), p.refusals.join('; '));
  assert.strictEqual(p.wrapA.ok, false);
});

test('open refuses when USDC is short', () => {
  const p = S.planOpen(poolInfo(2700), { eth: E18, rawA: 0n, rawB: 1n }, CFG, new Map(), 2600, 2800, 0.05, 100);
  assert.ok(p.refusals.some(r => /wallet lacks 100 USDC|wallet lacks .* USDC/.test(r)), p.refusals.join('; '));
});

test('open refuses when the pool disagrees with Chainlink, or Chainlink is stale', () => {
  const off = S.planOpen(poolInfo(2700, { oracleDeviation: 0.05, ethUsd: 2571 }), rich, CFG, new Map(), 2600, 2800, 0.05, 100);
  assert.ok(off.refusals.some(r => /from Chainlink/.test(r)), off.refusals.join('; '));
  const stale = S.planOpen(poolInfo(2700, { oracleAgeS: 7200 }), rich, CFG, new Map(), 2600, 2800, 0.05, 100);
  assert.ok(stale.refusals.some(r => /Chainlink ETH\/USD is 7200s old/.test(r)), stale.refusals.join('; '));
});

test('open refuses zero caps (nothing to deposit) and an unpriceable quote', () => {
  const z = S.planOpen(poolInfo(2700), rich, CFG, new Map(), 2600, 2800, 0, 0);
  assert.ok(z.refusals.some(r => /nothing to deposit/.test(r)));
  const u = S.planOpen(poolInfo(2700, { quoteUsd: null }), rich, CFG, new Map(), 2600, 2800, 0.01, 20);
  assert.ok(u.refusals.some(r => /cannot price/.test(r)) && u.refusals.some(r => /exceeds cap/.test(r)));
});

test('a funded open in range has no refusals, and its deposit respects caps and sleeve', () => {
  const p = S.planOpen(poolInfo(2700), rich, CFG, new Map(), 2600, 2800, 0.02, 60);
  assert.deepStrictEqual(p.refusals, []);
  assert.ok(p.dep.amountA <= M.toRaw('0.02', 18) && p.dep.amountB <= M.toRaw('60', 6));
  assert.ok(p.minA < p.dep.amountA && p.minB < p.dep.amountB);
  const s = S.planOpen(poolInfo(2700), rich, CFG, M.parseSleeve(JSON.stringify({ [USDC]: 10 })), 2600, 2800, 0.02, 60);
  assert.ok(s.dep.amountB <= 10n * 10n ** 6n, `sleeve breached: ${s.dep.amountB}`);
});

test('send refuses: no pin, wrong recipient, profit wallet disagreeing, self', () => {
  assert.throws(() => S.checkRecipient(PIN, { ...CFG, pin: '' }), /PIN is not set/);
  assert.throws(() => S.checkRecipient(OTHER, CFG), /pinned profit wallet/);
  assert.throws(() => S.checkRecipient(PIN, { ...CFG, profit: OTHER }), /pinned profit wallet/);
  assert.throws(() => S.checkRecipient('0x1234', CFG), /not an address/);
  assert.throws(() => S.checkRecipient(PIN, { ...CFG, pin: 'nope' }), /not an address/);
  assert.throws(() => S.checkRecipient(PIN, CFG, PIN.toLowerCase()), /equals the LP wallet/);
  assert.strictEqual(S.checkRecipient(PIN.toLowerCase(), { ...CFG, profit: PIN }), PIN);
});

test('CLI: HALT refuses every write before any key or network', () => {
  const d = scratch();
  fs.writeFileSync(path.join(d, 'HALT'), 'test halt');
  const env = { LPBOT_RUN_DIR: d, LPBOT_EVM_PROFIT_WALLET_PIN: PIN };
  for (const args of [['open', POOL, '2600', '2800', '0.01', '20', '--execute'], ['harvest', '1', '--execute'],
    ['close', '1', '--execute'], ['rebalance', WETH, USDC, '10', '10', '--execute'], ['send', USDC, '1', PIN, '--execute']]) {
    const r = run(args, env);
    assert.strictEqual(r.status, 1, args[0]);
    assert.match(r.stderr, /HALT present: test halt/, args[0]);
    assert.strictEqual(r.stdout, '', args[0]);
  }
});

test('CLI: send refuses without a pin and to any other recipient', () => {
  let r = run(['send', USDC, '1', PIN, '--execute']);
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /PIN is not set/);
  r = run(['send', USDC, '1', OTHER, '--execute'], { LPBOT_EVM_PROFIT_WALLET_PIN: PIN });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /pinned profit wallet/);
  r = run(['send', USDC, '1', PIN, '--execute'], { LPBOT_EVM_PROFIT_WALLET_PIN: PIN, LPBOT_PROFIT_WALLET: OTHER });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /pinned profit wallet/);
});

test('CLI: missing arguments and malformed settings are errors, not defaults', () => {
  let r = run(['open', POOL, '2600']);
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /usage: open/);
  r = run(['open', POOL, '2600', '2800', '0.01', '20'], { LPBOT_SLEEVE: '{bad' });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /LPBOT_SLEEVE is not valid JSON/);
  r = run(['open', POOL, '2600', '2800', '0.01', '20'], { LPBOT_MAX_USD: 'abc' });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /LPBOT_MAX_USD=abc/);
  r = run(['status'], { LPBOT_POOL: '' });
  assert.strictEqual(r.status, 1); assert.match(r.stderr, /no pool/);
});

test('CLI: an unreachable RPC fails the read with every endpoint named, exit 1, no stdout', () => {
  const r = run(['pool']);
  assert.strictEqual(r.status, 1);
  assert.match(r.stderr, /all RPC endpoints failed: 127\.0\.0\.1:9/);
  assert.strictEqual(r.stdout, '');
});

test('connect refuses an RPC that serves another chain', async () => {
  const srv = http.createServer((req, res) => {
    let b = ''; req.on('data', c => { b += c; });
    req.on('end', () => {
      const j = JSON.parse(b);
      const one = x => ({ jsonrpc: '2.0', id: x.id, result: '0x1' });
      res.setHeader('content-type', 'application/json');
      res.end(JSON.stringify(Array.isArray(j) ? j.map(one) : one(j)));
    });
  });
  await new Promise(r => srv.listen(0, '127.0.0.1', r));
  try {
    await assert.rejects(S.connect(`http://127.0.0.1:${srv.address().port}`), /serves chain 1, not Base \(8453\)/);
  } finally { srv.close(); }
});

// --- endpoint policy ------------------------------------------------------------------
test('baseEndpoints: a loopback RPC is the only endpoint; SOLANA_RPC_URL is never used', () => {
  assert.deepStrictEqual(baseEndpoints({ LPBOT_RPC: 'http://127.0.0.1:8545', LPBOT_BASE_RPC: 'https://x.example' }), ['http://127.0.0.1:8545']);
  assert.deepStrictEqual(baseEndpoints({ LPBOT_RPC: 'http://localhost:8545' }), ['http://localhost:8545']);
  const e = baseEndpoints({ SOLANA_RPC_URL: 'https://api.mainnet-beta.solana.com', LPBOT_BASE_RPC: 'https://k.example' });
  assert.deepStrictEqual(e, ['https://k.example', 'https://mainnet.base.org', 'https://base-rpc.publicnode.com']);
  assert.strictEqual(isLoopback('https://127.0.0.1.example.com'), false);
  assert.strictEqual(isLoopback('not a url'), false);
});

test('evmErrorKind: rate limits, 5xx and timeouts rotate; reverts and after-sign errors never do', () => {
  const wrap = (inner, depth = 3) => { let e = inner; for (let i = 0; i < depth; i++) { const o = new Error('outer'); o.cause = e; e = o; } return e; };
  const rl = Object.assign(new Error('RPC Request failed.'), { code: -32016 });
  assert.strictEqual(evmErrorKind(wrap(rl)), 'rotate');
  assert.strictEqual(evmErrorKind(wrap(Object.assign(new Error('x'), { status: 429 }))), 'rotate');
  assert.strictEqual(evmErrorKind(wrap(Object.assign(new Error('x'), { status: 503 }))), 'rotate');
  assert.strictEqual(evmErrorKind(wrap(Object.assign(new Error('slow'), { name: 'TimeoutError' }))), 'rotate');
  const rev = Object.assign(new Error('reverted'), { name: 'ContractFunctionRevertedError' });
  assert.strictEqual(evmErrorKind(wrap(rev)), 'fatal');
  assert.strictEqual(evmErrorKind(new AfterSignError('429 after sign')), 'fatal');
  assert.strictEqual(evmErrorKind(Object.assign(new Error('429'), { sent: true })), 'fatal');
  assert.strictEqual(evmErrorKind(new Error('HALT present: x')), 'fatal');
  assert.strictEqual(evmErrorKind(new Error('something odd')), 'fatal');
});

test('overBase: rotates past rate limits, stops at once on a fatal error, names every endpoint', async () => {
  const seen = [];
  const out = await overBase(['https://a.example', 'https://b.example'], async (u) => {
    seen.push(u);
    if (u.includes('a.')) throw Object.assign(new Error('limit'), { code: -32016 });
    return 'ok';
  }, { sleep: async () => {} });
  assert.strictEqual(out, 'ok');
  assert.deepStrictEqual(seen, ['https://a.example', 'https://a.example', 'https://b.example']);
  let calls = 0;
  await assert.rejects(overBase(['https://a.example', 'https://b.example'], async () => { calls++; throw new Error('execution reverted'); },
    { sleep: async () => {} }), /execution reverted/);
  assert.strictEqual(calls, 1);
  await assert.rejects(overBase(['https://a.example', 'https://b.example'], async () => { throw Object.assign(new Error('busy'), { status: 429 }); },
    { sleep: async () => {} }), /all RPC endpoints failed: a\.example: busy \| a\.example: busy \| b\.example: busy/);
});

// --- sending: never retried after the first send ---------------------------------------
function fakeCtx({ sendThrowsAt = -1, revertAt = -1 } = {}) {
  let i = 0;
  const pub = {
    sendRawTransaction: async () => { if (i === sendThrowsAt) throw new Error('nonce too low'); },
    waitForTransactionReceipt: async () => ({ status: (i++ === revertAt) ? 'reverted' : 'success', gasUsed: 1n, effectiveGasPrice: 1n, logs: [] }),
  };
  return { pub, nonce: 7 };
}
const steps = [{ label: 'approve' }, { label: 'mint' }];
const signer = (failAt = -1) => {
  let n = 0;
  return async () => { if (n === failAt) throw new Error('simulation reverted: STF'); return { serialized: '0x', hash: `0xh${n++}` }; };
};

test('runSteps: a failure before anything is signed throws (safe to retry)', async () => {
  await assert.rejects(S.runSteps(fakeCtx(), steps, CFG, { signStep: signer(0) }), /STF/);
});

test('runSteps: a send that throws after signing is reported partial, with its hash', async () => {
  const ctx = fakeCtx({ sendThrowsAt: 0 });
  const r = await S.runSteps(ctx, steps, CFG, { signStep: signer() });
  assert.deepStrictEqual(r.sent.map(s => s.hash), ['0xh0']);
  assert.match(r.error, /nonce too low/);
  assert.strictEqual(ctx.nonce, 7, 'a send the node refused does not consume the nonce');
});

test('runSteps: a failure after the first confirmed send is partial, never thrown', async () => {
  const r1 = await S.runSteps(fakeCtx(), steps, CFG, { signStep: signer(1) });
  assert.deepStrictEqual(r1.sent.map(s => s.label), ['approve']);
  assert.match(r1.error, /STF/);
  const ctx = fakeCtx({ revertAt: 1 });
  const r2 = await S.runSteps(ctx, steps, CFG, { signStep: signer() });
  assert.deepStrictEqual(r2.sent.map(s => s.hash), ['0xh0', '0xh1']);
  assert.match(r2.error, /reverted on chain/);
  assert.strictEqual(ctx.nonce, 9);
});

test('runSteps: all confirmed, nonces consecutive', async () => {
  const ctx = fakeCtx();
  const r = await S.runSteps(ctx, steps, CFG, { signStep: signer() });
  assert.strictEqual(r.error, null);
  assert.strictEqual(r.sent.length, 2);
  assert.strictEqual(ctx.nonce, 9);
});

test('simulateSequence: a refused batch falls back to one eth_call per step; a rate limit is rethrown', async () => {
  const pub = {
    simulateBlocks: async () => { throw new Error('The total cost exceeds the balance of the account.'); },
    call: async ({ data }) => { if (data === '0xbad') throw Object.assign(new Error('Execution reverted with reason: STF.'), { reason: 'STF' }); return {}; },
  };
  const r = await S.simulateSequence(pub, OTHER, [{ label: 'a', to: NPM, data: '0x01' }, { label: 'b', to: NPM, data: '0xbad' }]);
  assert.deepStrictEqual(r.map(x => [x.label, x.ok, x.revert, x.sequence]), [['a', true, null, false], ['b', false, 'STF', false]]);
  const limited = { simulateBlocks: async () => { throw Object.assign(new Error('limit'), { code: -32016 }); } };
  await assert.rejects(S.simulateSequence(limited, OTHER, [{ label: 'a', to: NPM, data: '0x' }]), /limit/);
});

test('parseArgs strips --execute and --pool <addr> wherever they are', () => {
  assert.deepStrictEqual(S.parseArgs(['--pool', POOL, 'close', '5', '--execute']), { execute: true, cmd: 'close', args: ['5'] });
  assert.deepStrictEqual(S.parseArgs(['status']), { execute: false, cmd: 'status', args: [] });
});

test('guard: LPBOT_RUN_DIR/HALT halts this profile', () => {
  const d = scratch();
  assert.doesNotThrow(() => S.guard({ LPBOT_RUN_DIR: d }));
  fs.writeFileSync(path.join(d, 'HALT'), 'why');
  assert.throws(() => S.guard({ LPBOT_RUN_DIR: d }), /HALT present: why/);
});

test('settings: defaults, and garbage refused', () => {
  const s = S.settings({});
  assert.strictEqual(s.maxUsd, 260); assert.strictEqual(s.slippageBps, 100);
  assert.strictEqual(s.gasReserve, M.toRaw('0.002', 18));
  assert.throws(() => S.settings({ LPBOT_SLIPPAGE_BPS: '-1' }), /LPBOT_SLIPPAGE_BPS/);
});

// --- positions ---------------------------------------------------------------------------
test('positionView: in range is tickLower <= tick < tickUpper; no rent; close estimate rounds down', () => {
  const info = poolInfo(2700);
  const x = { tokenId: 42n, tickLower: info.tick - 100, tickUpper: info.tick + 100, liquidity: 10n ** 14n };
  const v = S.positionView(x, info, [10n ** 12n, 5000n]);
  assert.strictEqual(v.inRange, true); assert.strictEqual(v.positionMint, '42');
  assert.strictEqual(v.rentUsd, 0); assert.strictEqual(v.feesAccruedB, 0.005);
  assert.ok(v.positionUsd > 0 && v.feesAccrued_USD > 0);
  assert.strictEqual(S.positionView({ ...x, tickUpper: info.tick }, info, [0n, 0n]).inRange, false);
  assert.strictEqual(S.positionView({ ...x, tickLower: info.tick }, info, [0n, 0n]).inRange, true);
});

test('closeCalls: decrease (amountMin at slippage) + collect + burn; no decrease when empty', () => {
  const info = poolInfo(2700);
  const x = { tokenId: 42n, tickLower: info.tick - 1000, tickUpper: info.tick + 1000, liquidity: 10n ** 15n };
  const c = S.closeCalls(x, info, OTHER, CFG, 1n);
  assert.strictEqual(c.calls.length, 3);
  assert.ok(c.estA > 0n && c.estB > 0n);
  assert.strictEqual(S.closeCalls({ ...x, liquidity: 0n }, info, OTHER, CFG, 1n).calls.length, 2);
});

test('spendable: WETH + ETH above the reserve on the native side, capped by the sleeve', () => {
  const info = poolInfo(2700);
  const h = { eth: M.toRaw('0.5', 18), rawA: M.toRaw('0.1', 18), rawB: 50n * 10n ** 6n };
  const s = S.spendable(h, info, CFG, new Map());
  assert.strictEqual(s.a, M.toRaw('0.598', 18)); assert.strictEqual(s.b, 50n * 10n ** 6n);
  const c = S.spendable(h, info, CFG, M.parseSleeve(JSON.stringify({ [WETH.toLowerCase()]: 0.2, [USDC]: 5 })));
  assert.strictEqual(c.a, M.toRaw('0.2', 18)); assert.strictEqual(c.b, 5n * 10n ** 6n);
  assert.strictEqual(S.spendable({ ...h, eth: 1n }, info, CFG, new Map()).a, h.rawA, 'ETH under the reserve is not spendable');
});

// --- pure math --------------------------------------------------------------------------
test('sqrtRatioAtTick: the exact TickMath constants at the ends and at 0', () => {
  assert.strictEqual(M.sqrtRatioAtTick(M.MIN_TICK), M.MIN_SQRT_RATIO);
  assert.strictEqual(M.sqrtRatioAtTick(M.MAX_TICK), M.MAX_SQRT_RATIO);
  assert.strictEqual(M.sqrtRatioAtTick(0), M.Q96);
  assert.throws(() => M.sqrtRatioAtTick(M.MAX_TICK + 1), /out of range/);
  assert.throws(() => M.sqrtRatioAtTick(1.5), /out of range/);
  // live slot0 of the pool on 2026-10-01: tick -197314, sqrtPriceX96 4116179725402233657475978
  const sp = 4116179725402233657475978n;
  assert.ok(M.sqrtRatioAtTick(-197314) <= sp && sp < M.sqrtRatioAtTick(-197313));
});

test('sqrtRatioAtTick is strictly increasing and agrees with 1.0001^(t/2)', () => {
  fc.assert(fc.property(fc.integer({ min: M.MIN_TICK, max: M.MAX_TICK - 1 }), (t) => {
    const a = M.sqrtRatioAtTick(t), b = M.sqrtRatioAtTick(t + 1);
    const f = Math.exp((t / 2) * Math.log(1.0001)) * 2 ** 96;
    return a < b && Math.abs(Number(a) / f - 1) < 1e-9;
  }), { numRuns: 500 });
});

test('tick <-> price with decimals 18/6: the tick brackets the price', () => {
  assert.ok(Math.abs(M.priceAtTick(-197314, 18, 6) - 2699.1) < 1);
  fc.assert(fc.property(fc.double({ min: 10, max: 1e6, noNaN: true }), (p) => {
    const t = M.tickAtPrice(p, 18, 6);
    return M.priceAtTick(t, 18, 6) <= p * (1 + 1e-9) && p < M.priceAtTick(t + 1, 18, 6) * (1 + 1e-9);
  }), { numRuns: 500 });
  assert.throws(() => M.tickAtPrice(0, 18, 6), /positive/);
  assert.throws(() => M.tickAtPrice(-5, 18, 6), /positive/);
});

test('bandTicks: snapped outward to the spacing, never empty, bad bands refused', () => {
  fc.assert(fc.property(fc.double({ min: 100, max: 1e5, noNaN: true }), fc.double({ min: 1.0001, max: 3, noNaN: true }),
    fc.constantFrom(1, 10, 50, 100, 200), (lo, k, s) => {
      const { tickLower, tickUpper } = M.bandTicks(lo, lo * k, s, 18, 6);
      return tickLower % s === 0 && tickUpper % s === 0 && tickLower < tickUpper
        && M.priceAtTick(tickLower, 18, 6) <= lo * (1 + 1e-9) && M.priceAtTick(tickUpper, 18, 6) >= lo * k * (1 - 1e-9);
    }), { numRuns: 300 });
  assert.throws(() => M.bandTicks(2800, 2600, 100, 18, 6), /not a positive increasing range/);
  assert.throws(() => M.bandTicks(0, 2600, 100, 18, 6), /not a positive increasing range/);
  assert.throws(() => M.bandTicks(2600, 2800, 0, 18, 6), /tick spacing/);
});

test('liquidity for amounts: the deposit never exceeds a cap, and 0.01% more liquidity would', () => {
  fc.assert(fc.property(
    fc.integer({ min: -200000, max: -195000 }), fc.integer({ min: 1, max: 40 }), fc.integer({ min: 1, max: 40 }),
    fc.bigInt({ min: 10n ** 12n, max: 10n ** 20n }), fc.bigInt({ min: 10n ** 3n, max: 10n ** 12n }),
    (t, dl, du, capA, capB) => {
      const tl = (Math.floor(t / 100) - dl) * 100, tu = (Math.floor(t / 100) + du) * 100;
      const sp = M.sqrtRatioAtTick(t), sa = M.sqrtRatioAtTick(tl), sb = M.sqrtRatioAtTick(tu);
      const d = M.depositFor(sp, sa, sb, capA, capB);
      if (d.amountA > capA || d.amountB > capB) return false;
      const [a, b] = M.amountsForLiquidity(sp, sa, sb, d.liquidity, true);
      if (a > capA || b > capB) return false;
      if (d.liquidity < 10n ** 6n) return true;            // too small for a 0.01% step to show
      const [a2, b2] = M.amountsForLiquidity(sp, sa, sb, d.liquidity + d.liquidity / 10000n + 1n, true);
      return a2 > capA || b2 > capB;
    }), { numRuns: 400 });
});

test('amountsForLiquidity: one-sided outside the band; withdrawal never exceeds deposit', () => {
  const sa = M.sqrtRatioAtTick(-197700), sb = M.sqrtRatioAtTick(-197000), L = 96631975910318n;
  assert.strictEqual(M.amountsForLiquidity(M.sqrtRatioAtTick(-198000), sa, sb, L, true)[1], 0n);
  assert.strictEqual(M.amountsForLiquidity(M.sqrtRatioAtTick(-196000), sa, sb, L, true)[0], 0n);
  fc.assert(fc.property(fc.integer({ min: -198000, max: -196000 }), fc.bigInt({ min: 1n, max: 10n ** 20n }), (t, l) => {
    const sp = M.sqrtRatioAtTick(t);
    const [u0, u1] = M.amountsForLiquidity(sp, sa, sb, l, true), [d0, d1] = M.amountsForLiquidity(sp, sa, sb, l, false);
    return d0 <= u0 && d1 <= u1 && u0 - d0 <= 1n && u1 - d1 <= 1n;
  }), { numRuns: 300 });
});

test('minWithSlippage: rounds down, bounded, refuses nonsense', () => {
  assert.strictEqual(M.minWithSlippage(10000n, 100), 9900n);
  assert.strictEqual(M.minWithSlippage(10000n, 0), 10000n);
  assert.strictEqual(M.minWithSlippage(99n, 100), 98n);
  for (const b of [-1, 10000, 1.5, NaN]) assert.throws(() => M.minWithSlippage(1n, b), /out of range/);
  fc.assert(fc.property(fc.bigInt({ min: 0n, max: 10n ** 30n }), fc.integer({ min: 0, max: 9999 }), (x, b) => {
    const m = M.minWithSlippage(x, b);
    return m <= x && m * 10000n >= x * BigInt(10000 - b) - 10000n;
  }));
});

test('wrapPlan: wraps the shortfall only, never below the reserve', () => {
  assert.deepStrictEqual(M.wrapPlan(10n, 15n, 100n, 5n), { wrap: 0n, spendable: 95n, ok: true });
  assert.deepStrictEqual(M.wrapPlan(10n, 4n, 10n, 5n), { wrap: 6n, spendable: 5n, ok: false });
  assert.deepStrictEqual(M.wrapPlan(10n, 0n, 3n, 5n), { wrap: 10n, spendable: 0n, ok: false });
  assert.throws(() => M.wrapPlan(-1n, 0n, 0n, 0n), /non-negative bigint/);
  assert.throws(() => M.wrapPlan(1, 0n, 0n, 0n), /non-negative bigint/);
  fc.assert(fc.property(...[0, 1, 2, 3].map(() => fc.bigInt({ min: 0n, max: 10n ** 22n })), (need, weth, eth, res) => {
    const p = M.wrapPlan(need, weth, eth, res);
    const okRes = !p.ok || eth - p.wrap >= res || p.wrap === 0n;
    return okRes && p.wrap + weth >= need && (weth >= need ? p.wrap === 0n : p.wrap === need - weth);
  }));
});

test('toRaw: floors, never above the amount; refuses non-decimals', () => {
  assert.strictEqual(M.toRaw('0.1', 18), 10n ** 17n);
  assert.strictEqual(M.toRaw('1.1234567', 6), 1123456n);
  assert.strictEqual(M.toRaw('12', 6), 12000000n);
  assert.strictEqual(M.toRaw('1e-5', 6), 10n);
  assert.ok(M.toRaw(2.7, 18) <= 27n * 10n ** 17n);
  for (const bad of ['-1', 'abc', '1,5', '', '0x10']) assert.throws(() => M.toRaw(bad, 6), /not a non-negative decimal/, bad);
  assert.throws(() => M.toRaw(-1, 6), /finite non-negative/);
  fc.assert(fc.property(fc.double({ min: 0, max: 1e9, noNaN: true }), (x) => Number(M.toRaw(x, 18)) / 1e18 <= x * (1 + 1e-12)));
});

test('parseSleeve: unset is no cap; anything malformed is refused; keys are case-insensitive', () => {
  assert.strictEqual(M.parseSleeve(undefined).size, 0);
  assert.strictEqual(M.parseSleeve('').size, 0);
  for (const bad of ['{bad', '[1]', '"x"', 'null', JSON.stringify({ [USDC]: -1 }), JSON.stringify({ [USDC]: '5' }), JSON.stringify({ [USDC]: null })]) {
    assert.throws(() => M.parseSleeve(bad), /LPBOT_SLEEVE/, bad);
  }
  const s = M.parseSleeve(JSON.stringify({ [USDC]: 12.5 }));
  assert.strictEqual(M.sleeveCap(s, USDC.toLowerCase(), 6), 12500000n);
  assert.strictEqual(M.sleeveCap(s, WETH, 18), null);
  assert.strictEqual(M.capped(5n, null), 5n); assert.strictEqual(M.capped(5n, 3n), 3n); assert.strictEqual(M.capped(2n, 3n), 2n);
});

test('marketRefusals: none when the pool agrees with a fresh oracle', () => {
  assert.deepStrictEqual(S.marketRefusals(poolInfo(2700), CFG), []);
  assert.deepStrictEqual(S.marketRefusals(poolInfo(2700, { poolEthUsd: null, oracleDeviation: null }), CFG), []);
});

test('revertText: the reason, else the error name and args, else the first line', () => {
  assert.strictEqual(S.revertText({ cause: { reason: 'STF' } }), 'STF');
  assert.strictEqual(S.revertText({ data: { errorName: 'Panic', args: [17n] } }), 'Panic(17)');
  assert.strictEqual(S.revertText(new Error('first\nsecond')), 'first');
});

test('isNative: ETH and the 0xEeee sentinel only', () => {
  assert.ok(S.isNative('ETH') && S.isNative('eth') && S.isNative('0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'));
  assert.ok(!S.isNative(WETH));
});

// --- boundaries (each one a mutant the tests above could not see) -------------------------
// A pool view whose tick and sqrt price sit exactly on `tick`.
function atTick(tick, extra = {}) {
  const sp = M.sqrtRatioAtTick(tick);
  const price = M.priceFromSqrtX96(sp, 18, 6);
  return poolInfo(2700, { tick, sqrtPriceX96: sp.toString(), price, poolEthUsd: price, ethUsd: price, ...extra });
}

test('open: in range at tickLower, out of range at tickUpper', () => {
  const { tickLower, tickUpper } = M.bandTicks(2600, 2800, 100, 18, 6);
  const lo = M.priceAtTick(tickLower, 18, 6) * 1.00001, hi = M.priceAtTick(tickUpper, 18, 6) * 0.99999;
  const at = (t) => S.planOpen(atTick(t), rich, CFG, new Map(), lo, hi, 0.02, 50);
  assert.deepStrictEqual([at(tickLower).tickLower, at(tickLower).tickUpper], [tickLower, tickUpper]);
  assert.ok(!at(tickLower).refusals.some(r => /outside the band/.test(r)), 'tick == tickLower is in range');
  assert.ok(at(tickUpper).refusals.some(r => /outside the band/.test(r)), 'tick == tickUpper is out of range');
});

test('open: ETH exactly at the reserve passes; a deposit exactly at LPBOT_MAX_USD passes', () => {
  const h = { eth: CFG.gasReserve, rawA: E18, rawB: 1000n * 10n ** 6n };
  assert.ok(!S.planOpen(poolInfo(2700), h, CFG, new Map(), 2600, 2800, 0.01, 20).refusals.some(r => /gas reserve/.test(r)));
  const first = S.planOpen(poolInfo(2700), rich, CFG, new Map(), 2600, 2800, 0.01, 20);
  const exact = S.planOpen(poolInfo(2700), rich, { ...CFG, maxUsd: first.approxUsd }, new Map(), 2600, 2800, 0.01, 20);
  assert.deepStrictEqual(exact.refusals, []);
  const over = S.planOpen(poolInfo(2700), rich, { ...CFG, maxUsd: 10 }, new Map(), 2600, 2800, 0.01, 20);
  assert.ok(over.refusals.some(r => /position about \$\d+\.\d\d exceeds cap \$10/.test(r)), over.refusals.join('; '));
});

test('open: the shortfall message names the native side only for the native token', () => {
  const poor = { eth: CFG.gasReserve, rawA: 0n, rawB: 0n };
  const a = S.planOpen(poolInfo(2700), poor, CFG, new Map(), 2600, 2800, 0.01, 20).refusals;
  assert.ok(a.some(r => /lacks .* WETH \(WETH \+ ETH above the gas reserve\)$/.test(r)), a.join('; '));
  assert.ok(a.some(r => /lacks 20 USDC$/.test(r)), a.join('; '));
  const b = S.planOpen(poolInfo(2700, { nativeSide: 'B' }), poor, CFG, new Map(), 2600, 2800, 0.01, 20).refusals;
  assert.ok(b.some(r => /lacks 20 USDC \(WETH \+ ETH above the gas reserve\)$/.test(r)), b.join('; '));
  assert.ok(b.some(r => /lacks [\d.e-]+ WETH$/.test(r)), b.join('; '));
});

test('open: a zero or missing quote price is refused, never read as free', () => {
  for (const q of [0, null, undefined]) {
    const r = S.planOpen(poolInfo(2700, { quoteUsd: q }), rich, CFG, new Map(), 2600, 2800, 0.01, 20).refusals;
    assert.ok(r.some(x => /cannot price/.test(x)) && r.some(x => /exceeds cap/.test(x)), `${q}: ${r.join('; ')}`);
  }
});

test('marketRefusals: exactly at the age and deviation limits passes', () => {
  assert.deepStrictEqual(S.marketRefusals(poolInfo(2700, { oracleAgeS: 3600 }), CFG), []);
  assert.deepStrictEqual(S.marketRefusals(poolInfo(2700, { oracleDeviation: 0.02 }), CFG), []);
  assert.strictEqual(S.marketRefusals(poolInfo(2700, { oracleAgeS: 3601 }), CFG).length, 1);
});

test('spendable: the native side B counts ETH above the reserve on B', () => {
  const h = { eth: M.toRaw('0.5', 18), rawA: 7n, rawB: M.toRaw('0.1', 18) };
  const s = S.spendable(h, poolInfo(2700, { nativeSide: 'B' }), CFG, new Map());
  assert.strictEqual(s.a, 7n);
  assert.strictEqual(s.b, M.toRaw('0.598', 18));
});

test('positionView: fees of A and B stay on their sides', () => {
  const info = poolInfo(2700);
  const v = S.positionView({ tokenId: 1n, tickLower: info.tick - 100, tickUpper: info.tick + 100, liquidity: 1n }, info, [10n ** 12n, 5000n]);
  assert.strictEqual(v.feesAccruedA, 1e-6); assert.strictEqual(v.feesAccruedB, 0.005);
});

test('simulateSequence: per-call status from one eth_simulateV1 batch', async () => {
  const pub = { simulateBlocks: async () => [{ calls: [{ status: 'success', gasUsed: 21000n }, { status: 'failure', error: { reason: 'STF' } }] }] };
  const r = await S.simulateSequence(pub, OTHER, [{ label: 'a', to: NPM, data: '0x' }, { label: 'b', to: NPM, data: '0x' }]);
  assert.deepStrictEqual(r, [{ label: 'a', ok: true, gasUsed: '21000', revert: null, sequence: true },
    { label: 'b', ok: false, gasUsed: null, revert: 'STF', sequence: true }]);
});

test('checkRecipient: the LP wallet check compares, it does not refuse every known wallet', () => {
  assert.strictEqual(S.checkRecipient(PIN, CFG, OTHER), PIN);
});

test('baseEndpoints: a non-loopback LPBOT_RPC keeps the public fallbacks', () => {
  assert.deepStrictEqual(baseEndpoints({ LPBOT_RPC: 'https://k.example' }),
    ['https://k.example', 'https://mainnet.base.org', 'https://base-rpc.publicnode.com']);
});

test('evmErrorKind: 500-504 rotate, 499 and 505 do not; every transport name rotates; marks beat causes', () => {
  const st = (status) => evmErrorKind(Object.assign(new Error('x'), { status }));
  assert.deepStrictEqual([499, 500, 504, 505].map(st), ['fatal', 'rotate', 'rotate', 'fatal']);
  for (const name of ['HttpRequestError', 'SocketClosedError', 'TimeoutError']) {
    assert.strictEqual(evmErrorKind(Object.assign(new Error('x'), { name })), 'rotate', name);
  }
  const limited = Object.assign(new Error('limit'), { code: -32016 });
  const after = Object.assign(new AfterSignError('after'), { cause: limited });
  assert.strictEqual(evmErrorKind(after), 'fatal', 'after signing, a rate limit underneath changes nothing');
  assert.strictEqual(evmErrorKind(Object.assign(new Error('sent'), { sent: true, cause: limited })), 'fatal');
  const reverted = Object.assign(new Error('rev'), { name: 'ExecutionRevertedError', cause: limited });
  assert.strictEqual(evmErrorKind(reverted), 'fatal', 'a revert is a revert on every endpoint');
});

test('math boundaries: zero sqrt price, rounding direction, price on a band edge, clamped bands', () => {
  assert.throws(() => M.amount0Delta(0n, M.Q96, 1n, false), /sqrt price must be positive/);
  const sa = M.sqrtRatioAtTick(-197700), sb = M.sqrtRatioAtTick(-197000), L = 96631975910319n;
  assert.strictEqual(M.amount0Delta(sa, sb, L, true), M.amount0Delta(sa, sb, L, false) + 1n);
  assert.strictEqual(M.amount1Delta(sa, sb, L, true), M.amount1Delta(sa, sb, L, false) + 1n);
  // the price on an edge: liquidity from the one side that is not zero, no division by zero
  assert.strictEqual(M.liquidityForAmounts(sb, sa, sb, 5n, 10n ** 9n), M.liquidityForAmount1(sa, sb, 10n ** 9n));
  assert.strictEqual(M.liquidityForAmounts(sa, sa, sb, 10n ** 15n, 5n), M.liquidityForAmount0(sa, sb, 10n ** 15n));
  // B-limited: A below its cap, B at it
  const d = M.depositFor(M.sqrtRatioAtTick(-197300), sa, sb, 10n ** 20n, 10n ** 8n);
  assert.ok(d.amountA < 10n ** 20n && d.amountB <= 10n ** 8n && d.amountB > 0n);
  const d2 = M.depositFor(M.sqrtRatioAtTick(-197300), sa, sb, 10n ** 15n, 10n ** 12n);
  assert.ok(d2.amountB < 10n ** 12n && d2.amountA <= 10n ** 15n);
  // an upper edge exactly on a tick price stays on that tick
  assert.strictEqual(M.bandTicks(0.999, 1, 1, 0, 0).tickUpper, 0);
  assert.throws(() => M.bandTicks(2600, 2600, 100, 18, 6), /not a positive increasing range/);
  const top = M.priceAtTick(887200, 0, 0);
  assert.throws(() => M.bandTicks(top * 1.00001, top * 2, 100, 0, 0), /outside the tick range/);
});

test('wrapPlan and sleeve boundaries: wrap == spendable is enough; a sleeve of 0 is a cap of 0', () => {
  assert.strictEqual(M.wrapPlan(10n, 0n, 15n, 5n).ok, true);
  assert.strictEqual(M.wrapPlan(10n, 0n, 14n, 5n).ok, false);
  assert.strictEqual(M.sleeveCap(M.parseSleeve(JSON.stringify({ [USDC]: 0 })), USDC, 6), 0n);
});
