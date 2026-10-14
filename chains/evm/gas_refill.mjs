// Gas self-refill on an EVM chain whose wrapped native token is a pool token (WPOL on
// Polygon, WETH on Base and Unichain). The pool's fees arrive as the wrapped token, never as
// the native coin that pays the gas, so the gas runs down to the reserve and every open stops
// (2026-10-14: poly-wpol-usdt idle with $435 in its wallet). The `refill` command of each
// EVM signer brings the native coin back up to LPBOT_GAS_REFILL_TO: it unwraps the wallet's
// own wrapped token (WETH9 withdraw) and, when that is short, first buys the shortfall with
// the pool's other token. It never takes more than the target needs.
//
// Pure: every amount is raw (wei for the native coin and its wrapped token, the other
// token's own decimals), so the arithmetic is testable without a node.
import { minWithSlippage, rawFromFloat } from './clmath.mjs';

export const UNWRAP_GAS = 50_000n;   // WETH9 withdraw: ~36k measured on a Polygon fork
export const SWAP_GAS = 260_000n;    // an exact approve (~46k) and a v3 swap (~160k), with margin
export const WORTH_FACTOR = 4n;      // a refill brings at least four times its own gas cost

// What the refill takes: `need` to reach `target`, from the wrapped token first
// (`fromWrapped`), and the `short` part the wrapped token cannot cover. h = { native,
// wrapped } in wei, `wrapped` already capped by the profile's sleeve. A target at or under
// the reserve is refused: it would refill nothing the reserve does not already keep.
export function planRefill(h, target, reserve) {
  const refusals = [];
  if (!(target > reserve)) refusals.push(`the refill target ${target} wei is not above the gas reserve ${reserve} wei`);
  const need = target > h.native ? target - h.native : 0n;
  const fromWrapped = h.wrapped < need ? h.wrapped : need;
  return { need, fromWrapped, short: need - fromWrapped, noop: need === 0n, refusals };
}

// How much of the other token to sell for `short` wei of the wrapped token: at the pool
// price (`otherPerWrapped`, human units), plus the pool fee and the slippage, rounded up,
// and never more than the wallet's `held` of it. `capped` says the wallet could not pay
// for the whole shortfall.
export function sellFor(short, otherPerWrapped, feePips, slippageBps, otherDecimals, held) {
  if (short <= 0n || !(otherPerWrapped > 0)) return { sell: 0n, capped: short > 0n };
  const human = Number(short) / 1e18 * otherPerWrapped * (1 + feePips / 1e6) * (1 + slippageBps / 1e4);
  const want = rawFromFloat(human, otherDecimals) + 1n;
  return want > held ? { sell: held, capped: true } : { sell: want, capped: false };
}

// The swap's minimum out and the amount to unwrap after it. Uncapped, the swap must
// bring the whole shortfall: its quote must cover it, and the shortfall is its minimum.
// Capped (the wallet's other token buys less), the minimum is the quote less the
// slippage. Either way the unwrap is what the wallet is sure to hold after the swap.
export function swapLeg(plan, quotedOut, capped, slippageBps) {
  const minOut = capped ? minWithSlippage(quotedOut, slippageBps) : plan.short;
  const ok = capped ? minOut > 0n : quotedOut >= plan.short;
  return { minOut: ok ? minOut : 0n, ok, unwrap: plan.fromWrapped + (ok ? minOut : 0n) };
}

// Whether a refill of `unwrap` wei is worth its gas: at least WORTH_FACTOR times the gas
// of its steps at `maxFeePerGas`. A smaller one would burn most of what it brings.
export function worthIt(unwrap, gasUnits, maxFeePerGas, factor = WORTH_FACTOR) {
  return unwrap > 0n && unwrap >= gasUnits * maxFeePerGas * factor;
}
