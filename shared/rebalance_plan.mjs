// The one swap that brings a wallet's split of two tokens to the targets.
// Shared by every swapper the loop drives with `rebalance` (venues/jupiter/swap.mjs,
// venues/orca/swap.mjs, venues/aerodrome/signer.mjs), so they all plan alike. No imports:
// a signer may load it without the Solana SDK.

export const TARGET_TOLERANCE = 0.02;                // "at target" = within 2%

// Pure: dollars in, the plan out, or null for no swap. Enough value: fill the
// short side up to its target from the other's surplus. Too little: split what
// there is in the targets' proportion.
export function planRebalance(usdA, usdB, targetA, targetB) {
  const total = usdA + usdB, want = targetA + targetB;
  if (!(want > 0)) return null;
  const mode = total >= want ? 'fill' : 'proportional';
  const desiredA = mode === 'fill' ? targetA : total * targetA / want;
  const desiredB = mode === 'fill' ? targetB : total * targetB / want;
  let side, sellUsd, deficit;
  if (usdA < desiredA) { side = 'A'; deficit = desiredA - usdA; sellUsd = Math.min(deficit, Math.max(0, usdB - desiredB)); }
  else if (usdB < desiredB) { side = 'B'; deficit = desiredB - usdB; sellUsd = Math.min(deficit, Math.max(0, usdA - desiredA)); }
  else return null;
  const ref = side === 'A' ? desiredA : desiredB;
  if (deficit <= TARGET_TOLERANCE * ref || !(sellUsd > 0)) return null;
  return { mode, buySide: side, sellSide: side === 'A' ? 'B' : 'A', sellUsd, desiredA, desiredB };
}
