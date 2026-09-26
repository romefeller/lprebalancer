// Slippage as a price range: the close minimums and the open sizing hold
// anywhere in the range; the open tolerance scales with the band.
import test from 'node:test';
import assert from 'node:assert';
const { amountsAt, closeMinimums, safeBase, openToleranceBps, SLIPPAGE_REFUSAL } = await import('../slippage.mjs');

const p = 121, L = 1000;
for (const w of [0.01, 0.02, 0.05]) {
  const pa = p / (1 + w), pb = p * (1 + w);
  test(`close minimums hold across a 0.5% price range on a ±${w * 100}% band`, () => {
    const { minA, minB } = closeMinimums(L, p, pa, pb, 50);
    for (const q of [p / 1.005, p / 1.002, p, p * 1.002, p * 1.005]) {
      const at = amountsAt(L, q, pa, pb);
      assert.ok(at.a >= minA - 1e-9 && at.b >= minB - 1e-9, `price ${q}`);
    }
    // and they are far looser than a 1% cut of each amount on a narrow band
    const now = amountsAt(L, p, pa, pb);
    if (w === 0.01) assert.ok(minA < now.a * 0.99 && minB < now.b * 0.99);
  });
  test(`open sizing keeps the other side under its cap across the range, ±${w * 100}% band`, () => {
    const capA = 0.55 * 190 / p, capB = 0.55 * 190, bps = openToleranceBps(pa, pb);
    const s = safeBase(p, pa, pb, capA, capB, bps);
    const f = 1 + bps / 1e4;
    for (const q of [p / f, p, p * f]) {
      const sq = Math.sqrt(q), sa = Math.sqrt(pa), sb = Math.sqrt(pb);
      const perA = (sq - sa) / (1 / sq - 1 / sb);
      const other = s.base === 'A' ? s.amount * perA : s.amount / perA;
      assert.ok(other <= (s.base === 'A' ? capB : capA) * (1 + 1e-9), `price ${q}`);
    }
    const v = s.base === 'A' ? s.amount * p + s.otherAt : s.amount + s.otherAt * p;
    assert.ok(v > 190, `deposit ${v} keeps most of the capital`);
  });
}
test('open tolerance: 3% of the half-width, bounded', () => {
  assert.ok(Math.abs(openToleranceBps(100 / 1.01, 101) - 3) < 0.01);
  assert.ok(Math.abs(openToleranceBps(100 / 1.05, 105) - 15) < 0.05);
  assert.equal(openToleranceBps(100 / 1.4, 140), 50);
});
test('only slippage refusals are rebuilt', () => {
  assert.ok(SLIPPAGE_REFUSAL.test('AnchorError PriceSlippageCheck'));
  assert.ok(SLIPPAGE_REFUSAL.test('custom program error: 0x1781'));
  assert.ok(SLIPPAGE_REFUSAL.test('{"Custom":6017}'));
  assert.ok(!SLIPPAGE_REFUSAL.test('insufficient funds'));
  assert.ok(!SLIPPAGE_REFUSAL.test('Custom 60170'));
});
test('outside the band safeBase returns null', () => {
  assert.equal(safeBase(90, 100, 110, 1, 1), null);
});
