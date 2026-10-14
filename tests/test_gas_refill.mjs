// chains/evm/gas_refill.mjs and the refill helpers of the EVM signers, without a node: what a
// refill takes (never more than the target needs, the wrapped token first), what it sells of
// the other token, the swap's minimum, the worth check, the swap refused under the gas
// reserve, and the unwrap read from its receipt. Properties with fast-check, and the table of
// the 2026-10-14 case (POL 1.65 against a 2 POL reserve, WPOL fees in the wallet).
import test from 'node:test';
import assert from 'node:assert';
import fc from 'fast-check';
import { encodeAbiParameters, encodeEventTopics, getAddress } from 'viem';
import * as G from '../chains/evm/gas_refill.mjs';
import * as M from '../chains/evm/clmath.mjs';
import * as P from '../chains/evm/polygon.mjs';
import { WRAPPED_ABI } from '../chains/evm/uniswap_abi.mjs';
import * as S from '../venues/uniswap_v3/signer.mjs';
import * as AERO from '../venues/aerodrome/signer.mjs';

const E = 10n ** 18n;
const U = (bits) => fc.bigInt({ min: 0n, max: (1n << BigInt(bits)) - 1n });
const ME = getAddress('0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266');
const OTHER = getAddress('0x70997970c51812dc3a010c7d01b50e0d17dc79c8');

test.afterEach(() => S.useChain('unichain'));

test('planRefill: the 2026-10-14 wallet refills 1.35 POL from its WPOL, no swap', () => {
  const p = G.planRefill({ native: M.toRaw('1.6466', 18), wrapped: M.toRaw('900', 18) }, 3n * E, 1n * E);
  assert.deepStrictEqual(p, { need: M.toRaw('1.3534', 18), fromWrapped: M.toRaw('1.3534', 18), short: 0n, noop: false, refusals: [] });
});

test('planRefill: at or above the target nothing; a target not above the reserve is refused', () => {
  assert.strictEqual(G.planRefill({ native: 3n * E, wrapped: E }, 3n * E, E).noop, true);
  assert.strictEqual(G.planRefill({ native: 4n * E, wrapped: E }, 3n * E, E).need, 0n);
  assert.strictEqual(G.planRefill({ native: 3n * E - 1n, wrapped: E }, 3n * E, E).noop, false);
  for (const target of [E, E - 1n, 0n]) {
    assert.match(G.planRefill({ native: 0n, wrapped: E }, target, E).refusals[0], /not above the gas reserve/);
  }
  assert.deepStrictEqual(G.planRefill({ native: 0n, wrapped: 0n }, E + 1n, E).refusals, []);
});

test('planRefill (property): never above the target, the wrapped token first, the rest short', () => {
  fc.assert(fc.property(U(80), U(80), U(70), U(70), (native, wrapped, reserve, extra) => {
    const target = reserve + extra;
    const p = G.planRefill({ native, wrapped }, target, reserve);
    assert.strictEqual(p.refusals.length, extra > 0n ? 0 : 1);
    assert.strictEqual(p.need, native >= target ? 0n : target - native);
    assert.ok(native + p.need <= (native > target ? native : target));          // never past the target
    assert.ok(p.fromWrapped <= wrapped && p.fromWrapped <= p.need);
    assert.strictEqual(p.fromWrapped + p.short, p.need);
    assert.strictEqual(p.short > 0n, wrapped < p.need);
    assert.strictEqual(p.noop, p.need === 0n);
  }), { numRuns: 2000 });
});

test('sellFor: the shortfall at the pool price, fee and slippage, rounded up and capped by the wallet', () => {
  // 2.3 WPOL at $0.10, 0.05% fee, 100 bp: 0.2323... USDT0 (6 decimals), plus one unit
  const s = G.sellFor(M.toRaw('2.3', 18), 0.1, 500, 100, 6, 50_000_000n);
  assert.strictEqual(s.capped, false);
  assert.strictEqual(s.sell, M.rawFromFloat(2.3 * 0.1 * 1.0005 * 1.01, 6) + 1n);
  assert.deepStrictEqual(G.sellFor(M.toRaw('2.3', 18), 0.1, 500, 100, 6, 1000n), { sell: 1000n, capped: true });
  assert.deepStrictEqual(G.sellFor(M.toRaw('2.3', 18), 0.1, 500, 100, 6, 0n), { sell: 0n, capped: true });
  assert.deepStrictEqual(G.sellFor(0n, 0.1, 500, 100, 6, 10n), { sell: 0n, capped: false });
  // a wallet holding exactly what the sale needs pays for the whole shortfall: not capped
  assert.deepStrictEqual(G.sellFor(M.toRaw('2.3', 18), 0.1, 500, 100, 6, s.sell), { sell: s.sell, capped: false });
  assert.deepStrictEqual(G.sellFor(M.toRaw('2.3', 18), 0.1, 500, 100, 6, s.sell - 1n), { sell: s.sell - 1n, capped: true });
  for (const px of [0, -1, NaN]) assert.deepStrictEqual(G.sellFor(E, px, 500, 100, 6, 10n ** 9n), { sell: 0n, capped: true });
});

test('sellFor (property): never more than held; uncapped it pays at least the fair price plus the fee', () => {
  fc.assert(fc.property(U(70), fc.double({ min: 1e-4, max: 1e4, noNaN: true }), fc.integer({ min: 0, max: 10_000 }),
    fc.integer({ min: 0, max: 500 }), U(60), (short, px, fee, slip, held) => {
      const s = G.sellFor(short, px, fee, slip, 6, held);
      assert.ok(s.sell <= held);
      if (short > 0n && held > 0n && !s.capped) {
        assert.ok(Number(s.sell) / 1e6 >= Number(short) / 1e18 * px * (1 + fee / 1e6) * 0.999999);
      }
    }), { numRuns: 2000 });
});

test('swapLeg: uncapped the shortfall is the minimum; capped the quote less the slippage', () => {
  const plan = { fromWrapped: 5n * E / 10n, short: 23n * E / 10n };
  assert.deepStrictEqual(G.swapLeg(plan, 2324n * E / 1000n, false, 100), { minOut: plan.short, ok: true, unwrap: 28n * E / 10n });
  assert.deepStrictEqual(G.swapLeg(plan, plan.short, false, 100), { minOut: plan.short, ok: true, unwrap: 28n * E / 10n });
  assert.deepStrictEqual(G.swapLeg(plan, plan.short - 1n, false, 100), { minOut: 0n, ok: false, unwrap: plan.fromWrapped });
  assert.deepStrictEqual(G.swapLeg(plan, E, true, 100), { minOut: 99n * E / 100n, ok: true, unwrap: plan.fromWrapped + 99n * E / 100n });
  assert.deepStrictEqual(G.swapLeg(plan, 0n, true, 100), { minOut: 0n, ok: false, unwrap: plan.fromWrapped });
});

test('swapLeg (property): the unwrap never passes the need nor what the swap is sure to bring', () => {
  fc.assert(fc.property(U(70), U(70), U(72), fc.boolean(), fc.integer({ min: 0, max: 9999 }),
    (fromWrapped, short, quoted, capped, slip) => {
      const l = G.swapLeg({ fromWrapped, short }, quoted, capped, slip);
      assert.ok(l.minOut <= quoted);
      assert.strictEqual(l.unwrap, fromWrapped + l.minOut);
      if (!capped) assert.ok(l.unwrap <= fromWrapped + short);
      if (!l.ok) assert.strictEqual(l.unwrap, fromWrapped);
    }), { numRuns: 2000 });
});

test('worthIt: at least four times its own gas, and never for nothing', () => {
  const fee = 600n * 10n ** 9n;                                         // 600 gwei
  const gas = G.UNWRAP_GAS;
  assert.strictEqual(G.worthIt(gas * fee * 4n, gas, fee), true);
  assert.strictEqual(G.worthIt(gas * fee * 4n - 1n, gas, fee), false);
  assert.strictEqual(G.worthIt(0n, gas, 0n), false);
  assert.strictEqual(G.worthIt(1n, gas, 0n), true);
  assert.strictEqual(G.worthIt(gas * fee * 2n, gas, fee, 2n), true);
  assert.deepStrictEqual([G.UNWRAP_GAS, G.SWAP_GAS, G.WORTH_FACTOR], [50_000n, 260_000n, 4n]);
});

test('the swap of an open or an add is refused under the gas reserve (2026-10-14)', () => {
  S.useChain('polygon');
  const cfg = { gasReserve: 2n * E };
  assert.deepStrictEqual(S.planSwapRefusals({ eth: 2n * E }, cfg, null), []);
  // one wei under is under (the message rounds it, the comparison does not)
  assert.deepStrictEqual(S.planSwapRefusals({ eth: 2n * E - 1n }, cfg, null), [`POL ${M.toHuman(2n * E - 1n, 18)} is below the 2 gas reserve`]);
  assert.deepStrictEqual(S.planSwapRefusals({ eth: M.toRaw('1.9626', 18) }, cfg, 'wallet lacks 3 WPOL to sell'),
    ['wallet lacks 3 WPOL to sell', 'POL 1.9626 is below the 2 gas reserve']);
  assert.deepStrictEqual(S.planSwapRefusals({ eth: 5n * E }, cfg, 'wallet lacks 3 WPOL to sell'), ['wallet lacks 3 WPOL to sell']);
});

test('Aerodrome: the swap is refused under the reserve too, and its settings carry the target', () => {
  const cfg = { gasReserve: 2n * 10n ** 15n };                           // 0.002 ETH
  assert.deepStrictEqual(AERO.planSwapRefusals({ eth: cfg.gasReserve }, cfg, null), []);
  assert.deepStrictEqual(AERO.planSwapRefusals({ eth: cfg.gasReserve - 1n }, cfg, null), [`ETH ${Number(cfg.gasReserve - 1n) / 1e18} is below the 0.002 gas reserve`]);
  assert.deepStrictEqual(AERO.planSwapRefusals({ eth: 0n }, cfg, 'wallet lacks 1 WETH to sell'), ['wallet lacks 1 WETH to sell', 'ETH 0 is below the 0.002 gas reserve']);
  assert.deepStrictEqual(AERO.planSwapRefusals({ eth: E }, cfg, 'wallet lacks 1 WETH to sell'), ['wallet lacks 1 WETH to sell']);
  assert.strictEqual(AERO.settings({}).refillTo, 0n);
  assert.strictEqual(AERO.settings({ LPBOT_GAS_REFILL_TO: '0.006' }).refillTo, 6n * 10n ** 15n);
  assert.throws(() => AERO.settings({ LPBOT_GAS_REFILL_TO: 'x' }), /not a non-negative number/);
  // an empty variable is unset: its default, not 0
  assert.deepStrictEqual([AERO.settings({ LPBOT_MAX_USD: '' }).maxUsd, AERO.settings({ LPBOT_GAS_RESERVE_NATIVE: '' }).gasReserve],
    [260, 2n * 10n ** 15n]);
});

test('settings: LPBOT_GAS_REFILL_TO in native units, 0 when unset, refused when negative', () => {
  assert.strictEqual(S.settings({}).refillTo, 0n);
  assert.strictEqual(S.settings({ LPBOT_GAS_REFILL_TO: '3' }).refillTo, 3n * E);
  assert.strictEqual(S.settings({ LPBOT_GAS_REFILL_TO: '' }).refillTo, 0n);
  assert.throws(() => S.settings({ LPBOT_GAS_REFILL_TO: '-1' }), /not a non-negative number/);
});

test('unwrapped: the Withdrawal events of the wrapped token from this wallet, nothing else', () => {
  S.useChain('polygon');
  const log = (address, src, wad, name = 'Withdrawal') => ({
    address, topics: encodeEventTopics({ abi: WRAPPED_ABI, eventName: name, args: name === 'Withdrawal' ? { src } : { dst: src } }),
    data: encodeAbiParameters([{ type: 'uint256' }], [wad]),
  });
  const r = { logs: [log(P.WPOL, ME, 2n * E), log(P.WPOL, OTHER, 7n * E), log(P.USDT0, ME, 9n * E), log(P.WPOL, ME, 5n, 'Deposit'),
    { address: P.WPOL, topics: ['0x01'], data: '0x' }, log(P.WPOL.toLowerCase(), ME, 3n)] };
  assert.strictEqual(S.unwrapped(r, ME), 2n * E + 3n);
  assert.strictEqual(S.unwrapped({ logs: [] }, ME), 0n);
  assert.strictEqual(S.unwrapped(null, ME), 0n);
});

test('refillReport: amounts in human units, the chain and its tokens', () => {
  S.useChain('polygon');
  const out = S.refillReport({ pool: '0xP' }, { need: 3n * E / 2n, fromWrapped: E }, { eth: E / 2n },
    { refillTo: 2n * E, gasReserve: E }, { unwrapPlanned: 1.5 });
  assert.deepStrictEqual(out, { chain: 'polygon', symbol: 'POL', wrapped: P.WPOL, pool: '0xP', nativeBefore: 0.5, target: 2,
    reserve: 1, need: 1.5, fromWrapped: 1, unwrapPlanned: 1.5 });
});

test('the refill command is listed and both EVM signers carry it', async () => {
  const fs = await import('node:fs');
  for (const f of ['../venues/uniswap_v3/signer.mjs', '../venues/aerodrome/signer.mjs']) {
    const src = fs.readFileSync(new URL(f, import.meta.url), 'utf8');
    assert.match(src, /if \(cmd === 'refill'\) return refill\(execute\);/, f);
    assert.match(src, /refill \[--execute\]/, f);
    assert.match(src, /functionName: 'withdraw'/, f);
  }
});
