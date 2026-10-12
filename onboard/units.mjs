// Token amounts as integer base units, and back. Pure and exact: no float
// ever touches an amount. Shared by the server (intent.mjs) and the page's
// bundle (decode_sol.mjs, main.mjs), so both read an amount the same way.

// A decimal string ("1.5") to base units at `decimals`. Digits past
// `decimals` are cut, never rounded; callers check the count first.
export function toRaw(s, decimals) {
  const [i, f = ''] = String(s).split('.');
  return BigInt(i) * 10n ** BigInt(decimals) + BigInt((f + '0'.repeat(decimals)).slice(0, decimals) || '0');
}

// Base units to the shortest decimal string ("1.5", "2").
export function fromRaw(raw, decimals) {
  const d = 10n ** BigInt(decimals);
  const f = (raw % d).toString().padStart(decimals, '0').replace(/0+$/, '');
  return f ? `${raw / d}.${f}` : `${raw / d}`;
}
