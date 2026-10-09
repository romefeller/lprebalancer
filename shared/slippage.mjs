// Slippage as a PRICE range, for concentrated-liquidity closes and opens.
//
// A per-token tolerance ("each amount may fall 1%") is the wrong model for a
// narrow band: in a +/-1% band a 0.1% price move shifts about a tenth of the
// position from one token to the other, so a 1% token tolerance is roughly a
// 0.01% price tolerance, and closes and opens failed with PriceSlippageCheck
// (Raydium error 6017) on 2026-09-26. Here the tolerance is on the price:
// the transaction must succeed anywhere in [p / (1 + s), p * (1 + s)].
//
// Pure functions, no network.

export const PRICE_SLIPPAGE_BPS = Number(process.env.LPBOT_PRICE_SLIPPAGE_BPS ?? 50);   // 0.5% of price

// Token amounts of liquidity L at price p in [pa, pb] (human or raw, any consistent unit).
export function amountsAt(L, p, pa, pb) {
  const sp = Math.sqrt(p), sa = Math.sqrt(pa), sb = Math.sqrt(pb);
  if (p <= pa) return { a: L * (1 / sa - 1 / sb), b: 0 };
  if (p >= pb) return { a: 0, b: L * (sb - sa) };
  return { a: L * (1 / sp - 1 / sb), b: L * (sp - sa) };
}

// Close: the least of each token the position can return while the price
// stays in the range. Token A is least when the price is highest, token B
// when it is lowest.
export function closeMinimums(L, p, pa, pb, bps = PRICE_SLIPPAGE_BPS) {
  const f = 1 + bps / 1e4;
  return { minA: amountsAt(L, p * f, pa, pb).a, minB: amountsAt(L, p / f, pa, pb).b };
}

// Open with one side fixed ("base") and the other capped: the other side
// the program asks for grows as the price moves against it. Size the base so
// that, anywhere in the price range, the other side fits under its cap.
// Returns { base: 'A' | 'B', amount } with the larger deposit value, or null.
export function safeBase(p, pa, pb, capA, capB, bps = PRICE_SLIPPAGE_BPS) {
  if (!(p > pa && p < pb && capA >= 0 && capB >= 0)) return null;
  const f = 1 + bps / 1e4;
  const hi = Math.min(p * f, pb * 0.999999), lo = Math.max(p / f, pa * 1.000001);
  const sa = Math.sqrt(pa), sb = Math.sqrt(pb);
  // base A: B needed per unit A = (sp - sa) / (1/sp - 1/sb), rising with price
  const perA = (x) => (Math.sqrt(x) - sa) / (1 / Math.sqrt(x) - 1 / sb);
  const baseA = Math.min(capA, capB / perA(hi));
  // base B: A needed per unit B = 1 / perA, rising as the price falls
  const baseB = Math.min(capB, capA * perA(lo));
  const valA = baseA * p + baseA * perA(p), valB = baseB + (baseB / perA(p)) * p;
  if (!(baseA > 0) && !(baseB > 0)) return null;
  return valA >= valB ? { base: 'A', amount: baseA, otherAt: baseA * perA(p) }
                      : { base: 'B', amount: baseB, otherAt: baseB / perA(p) };
}

// The open's price tolerance, scaled to the band: 3% of the half-width, at
// most PRICE_SLIPPAGE_BPS. Both sides sit at their caps, so tolerance costs
// deposit (0.1% of price cost 18% of a +/-1% deposit); the price moves about
// 0.02% while a transaction lands, and a refusal reverts and is rebuilt.
export function openToleranceBps(pa, pb) {
  const halfWidthBps = (Math.sqrt(pb / pa) - 1) * 1e4;
  return Math.max(1, Math.min(PRICE_SLIPPAGE_BPS, 0.03 * halfWidthBps));
}

// A refusal on slippage is a reverted transaction: nothing moved, and a
// rebuild on fresh pool state is safe. Anything else is not retried here.
export const SLIPPAGE_REFUSAL = /PriceSlippageCheck|\b6017\b|0x1781/i;
