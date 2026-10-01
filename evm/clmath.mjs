// Concentrated-liquidity math for the Aerodrome Slipstream signer, in BigInt where the
// chain uses integers. Pure: no I/O, no clock, so every function here is testable alone.
//
// The integer functions are ports of Uniswap v3's TickMath.getSqrtRatioAtTick,
// SqrtPriceMath.getAmount{0,1}Delta and LiquidityAmounts.getLiquidityForAmount{s,0,1}.
// Slipstream (a Uniswap v3 fork) uses them unchanged, so a deposit computed here is the
// deposit the position manager computes on chain, to the wei.
//
// Prices are token B per token A in human units, A and B in the pool's order
// (token0, token1). On WETH/USDC: A = WETH (18 decimals), B = USDC (6).

export const MIN_TICK = -887272;
export const MAX_TICK = 887272;
export const MIN_SQRT_RATIO = 4295128739n;
export const MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342n;
export const Q96 = 1n << 96n;
export const MAX_UINT128 = (1n << 128n) - 1n;
const MAX_UINT256 = (1n << 256n) - 1n;

// TickMath.getSqrtRatioAtTick: sqrt(1.0001^tick) * 2^96, rounded up.
export function sqrtRatioAtTick(tick) {
  if (!Number.isInteger(tick) || tick < MIN_TICK || tick > MAX_TICK) throw new Error(`tick ${tick} out of range`);
  const t = BigInt(tick < 0 ? -tick : tick);
  let r = (t & 0x1n) !== 0n ? 0xfffcb933bd6fad37aa2d162d1a594001n : 0x100000000000000000000000000000000n;
  const steps = [
    [0x2n, 0xfff97272373d413259a46990580e213an], [0x4n, 0xfff2e50f5f656932ef12357cf3c7fdccn],
    [0x8n, 0xffe5caca7e10e4e61c3624eaa0941cd0n], [0x10n, 0xffcb9843d60f6159c9db58835c926644n],
    [0x20n, 0xff973b41fa98c081472e6896dfb254c0n], [0x40n, 0xff2ea16466c96a3843ec78b326b52861n],
    [0x80n, 0xfe5dee046a99a2a811c461f1969c3053n], [0x100n, 0xfcbe86c7900a88aedcffc83b479aa3a4n],
    [0x200n, 0xf987a7253ac413176f2b074cf7815e54n], [0x400n, 0xf3392b0822b70005940c7a398e4b70f3n],
    [0x800n, 0xe7159475a2c29b7443b29c7fa6e889d9n], [0x1000n, 0xd097f3bdfd2022b8845ad8f792aa5825n],
    [0x2000n, 0xa9f746462d870fdf8a65dc1f90e061e5n], [0x4000n, 0x70d869a156d2a1b890bb3df62baf32f7n],
    [0x8000n, 0x31be135f97d08fd981231505542fcfa6n], [0x10000n, 0x9aa508b5b7a84e1c677de54f3e99bc9n],
    [0x20000n, 0x5d6af8dedb81196699c329225ee604n], [0x40000n, 0x2216e584f5fa1ea926041bedfe98n],
    [0x80000n, 0x48a170391f7dc42444e8fa2n],
  ];
  for (const [bit, mul] of steps) if ((t & bit) !== 0n) r = (r * mul) >> 128n;
  if (tick > 0) r = MAX_UINT256 / r;
  return (r >> 32n) + ((r & 0xffffffffn) === 0n ? 0n : 1n);
}

function mulDiv(a, b, d) { return (a * b) / d; }
function mulDivUp(a, b, d) { const p = a * b; return p / d + (p % d === 0n ? 0n : 1n); }
function divUp(a, d) { return a / d + (a % d === 0n ? 0n : 1n); }
function order(a, b) { return a <= b ? [a, b] : [b, a]; }

// SqrtPriceMath.getAmount0Delta: token0 for liquidity L between two sqrt prices.
export function amount0Delta(sa, sb, L, roundUp) {
  [sa, sb] = order(sa, sb);
  if (sa <= 0n) throw new Error('sqrt price must be positive');
  const n1 = L << 96n, n2 = sb - sa;
  return roundUp ? divUp(mulDivUp(n1, n2, sb), sa) : mulDiv(n1, n2, sb) / sa;
}

// SqrtPriceMath.getAmount1Delta: token1 for liquidity L between two sqrt prices.
export function amount1Delta(sa, sb, L, roundUp) {
  [sa, sb] = order(sa, sb);
  return roundUp ? mulDivUp(L, sb - sa, Q96) : mulDiv(L, sb - sa, Q96);
}

// What liquidity L in [sa, sb] is worth at sqrt price sp, as (amount0, amount1).
// roundUp=true is what a deposit costs (the pool rounds in its own favour on mint);
// roundUp=false is what a withdrawal returns.
export function amountsForLiquidity(sp, sa, sb, L, roundUp) {
  [sa, sb] = order(sa, sb);
  if (sp <= sa) return [amount0Delta(sa, sb, L, roundUp), 0n];
  if (sp < sb) return [amount0Delta(sp, sb, L, roundUp), amount1Delta(sa, sp, L, roundUp)];
  return [0n, amount1Delta(sa, sb, L, roundUp)];
}

export function liquidityForAmount0(sa, sb, a0) {
  [sa, sb] = order(sa, sb);
  return mulDiv(a0, mulDiv(sa, sb, Q96), sb - sa);
}

export function liquidityForAmount1(sa, sb, a1) {
  [sa, sb] = order(sa, sb);
  return mulDiv(a1, Q96, sb - sa);
}

// LiquidityAmounts.getLiquidityForAmounts: the most liquidity both caps fund at sp.
export function liquidityForAmounts(sp, sa, sb, a0, a1) {
  [sa, sb] = order(sa, sb);
  if (sp <= sa) return liquidityForAmount0(sa, sb, a0);
  if (sp < sb) {
    const l0 = liquidityForAmount0(sp, sb, a0), l1 = liquidityForAmount1(sa, sp, a1);
    return l0 < l1 ? l0 : l1;
  }
  return liquidityForAmount1(sa, sb, a1);
}

// The deposit for caps (capA, capB) at sp: the liquidity the smaller cap allows, the
// amounts it costs (rounded up as the pool rounds), never above either cap. These are
// handed to the position manager as amountDesired, so it pulls at most these.
export function depositFor(sp, sa, sb, capA, capB) {
  const L = liquidityForAmounts(sp, sa, sb, capA, capB);
  const [a, b] = amountsForLiquidity(sp, sa, sb, L, true);
  return { liquidity: L, amountA: a < capA ? a : capA, amountB: b < capB ? b : capB };
}

// amount less `bps` basis points, rounded down: the amountMin for a mint, a decrease or a
// swap. bps outside [0, 10000) is a configuration error, not a number to clamp.
export function minWithSlippage(amount, bps) {
  if (!Number.isInteger(bps) || bps < 0 || bps >= 10000) throw new Error(`slippage ${bps} bps out of range [0, 10000)`);
  return (amount * BigInt(10000 - bps)) / 10000n;
}

// Human price (B per A) at a tick and back. Floats: band edges, not money.
export function priceAtTick(tick, decA, decB) {
  return 1.0001 ** tick * 10 ** (decA - decB);
}

export function tickAtPrice(price, decA, decB) {
  if (!(price > 0) || !Number.isFinite(price)) throw new Error(`price must be positive, got ${price}`);
  return Math.floor(Math.log(price / 10 ** (decA - decB)) / Math.log(1.0001));
}

export function priceFromSqrtX96(sp, decA, decB) {
  const r = Number(sp) / 2 ** 96;
  return r * r * 10 ** (decA - decB);
}

// The band [lower, upper] snapped OUTWARD to the pool's tick spacing: the lower tick
// down, the upper tick up, so the position covers at least the band asked for.
export function bandTicks(lower, upper, spacing, decA, decB) {
  lower = Number(lower); upper = Number(upper);
  if (!(lower > 0 && upper > lower)) throw new Error(`band ${lower}-${upper} is not a positive increasing range`);
  if (!(Number.isInteger(spacing) && spacing > 0)) throw new Error(`tick spacing ${spacing} invalid`);
  const minT = Math.ceil(MIN_TICK / spacing) * spacing, maxT = Math.floor(MAX_TICK / spacing) * spacing;
  let tl = Math.floor(tickAtPrice(lower, decA, decB) / spacing) * spacing;
  // tickAtPrice floors: the tick at or below `upper`. One more when it is below, so the
  // upper edge is at or above the price asked for.
  let tu = tickAtPrice(upper, decA, decB);
  if (priceAtTick(tu, decA, decB) < upper) tu += 1;
  tu = Math.ceil(tu / spacing) * spacing;   // > tl: tu's price >= upper > lower >= tl's price
  tl = Math.max(tl, minT); tu = Math.min(tu, maxT);
  if (tu <= tl) throw new Error(`band ${lower}-${upper} is outside the tick range`);
  return { tickLower: tl, tickUpper: tu };
}

// Native ETH counts as WETH. To spend `need` WETH the wallet wraps the shortfall and no
// more, and never takes ETH below the gas reserve. All values in wei.
export function wrapPlan(need, weth, eth, reserve) {
  for (const [k, v] of Object.entries({ need, weth, eth, reserve })) {
    if (typeof v !== 'bigint' || v < 0n) throw new Error(`${k} must be a non-negative bigint`);
  }
  const wrap = need > weth ? need - weth : 0n;
  const spendable = eth > reserve ? eth - reserve : 0n;
  return { wrap, spendable, ok: wrap <= spendable };
}

// A decimal string or number in human units, to raw units, rounded DOWN. Never above
// the amount asked: a cap stays a cap.
export function toRaw(value, decimals) {
  if (typeof value === 'number') return rawFromFloat(value, decimals);
  const s = String(value).trim();
  if (/^\d+(\.\d+)?e-?\d+$/i.test(s)) return rawFromFloat(Number(s), decimals);
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error(`amount ${value} is not a non-negative decimal`);
  const [i, f = ''] = s.split('.');
  return BigInt(i + f.slice(0, decimals).padEnd(decimals, '0'));
}

// A float in human units to raw units, rounded down at 1e-12 of a unit (floats carry
// about 15 significant digits; the rest would be noise).
export function rawFromFloat(x, decimals) {
  if (!(x >= 0) || !Number.isFinite(x)) throw new Error(`amount ${x} must be a finite non-negative number`);
  const p = Math.min(decimals, 12);
  return BigInt(Math.floor(x * 10 ** p)) * 10n ** BigInt(decimals - p);
}

export function toHuman(raw, decimals) {
  return Number(raw) / 10 ** decimals;
}

// LPBOT_SLEEVE: {"<token address>": <max human amount>}. The profile may use at most that
// much of the token; for WETH the cap covers WETH and native ETH together. Unset means
// no sleeve (the whole wallet). Anything malformed is refused, never read as "no cap".
export function parseSleeve(text) {
  if (text == null || text === '') return new Map();
  let obj;
  try { obj = JSON.parse(text); } catch { throw new Error('LPBOT_SLEEVE is not valid JSON; refusing'); }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new Error('LPBOT_SLEEVE must be a JSON object; refusing');
  const out = new Map();
  for (const [k, v] of Object.entries(obj)) {
    const n = Number(v);
    if (typeof v !== 'number' || !Number.isFinite(n) || n < 0) throw new Error(`LPBOT_SLEEVE[${k}] must be a non-negative number; refusing`);
    out.set(k.toLowerCase(), n);
  }
  return out;
}

// The sleeve cap of `token` in raw units, or null when the sleeve names no cap for it.
export function sleeveCap(sleeve, token, decimals) {
  const v = sleeve.get(String(token).toLowerCase());
  return v == null ? null : toRaw(v, decimals);
}

export function capped(raw, cap) {
  return cap == null || raw < cap ? raw : cap;
}
