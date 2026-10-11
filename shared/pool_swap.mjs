// The loop's `rebalance` (SWAP_HOOK.md) on the profile's own pool, as the
// venue signers run it: a vault may swap only on pools its owners approved,
// so a vault profile's swaps go to its venue signer, never to Jupiter
// (lp/signers.route). What each side of the swap is, and what may be sold.
import { createRequire } from 'node:module';
import { Refused } from '../venues/orca/swap.mjs';
import { parseSleeve, sleeveCap } from '../venues/jupiter/swap.mjs';
import SOLANA from '../chains/solana/solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey } = require('@solana/web3.js');
const spl = require('@solana/spl-token');

// The two sides of a swap on the pool `info` describes, in the order asked.
// Prices: the loop's hints (the prices it sized the targets with), else the
// pool's own price and the quote token's USD value. Pure.
export function swapSides(info, mintA, mintB, hints) {
  if (new Set([mintA, mintB, info.mintA, info.mintB]).size !== 2) {
    throw new Refused(`swap ${mintA}/${mintB} is not this pool's pair (${info.mintA}/${info.mintB}); refusing`);
  }
  if (info.multiplierA !== 1 || info.multiplierB !== 1) throw new Refused('a scaled Token-2022 mint: no swap here; refusing');
  const side = (mint) => {
    const x = mint === info.mintA;
    const decimals = x ? info.decimalsA : info.decimalsB;
    const h = hints.get(mint);
    if (h && h.decimals !== decimals) throw new Refused(`${h.symbol ?? mint} decimals ${h.decimals} disagree with the chain's ${decimals}; refusing`);
    const own = info.quoteUsd == null ? null : x ? info.uiPrice * info.quoteUsd : info.quoteUsd;
    return { mint, symbol: x ? info.symbolA : info.symbolB, decimals, usdPrice: h?.usdPrice ?? own,
      program: x ? info.tokenProgramA : info.tokenProgramB };
  };
  const sides = [side(mintA), side(mintB)];
  for (const i of sides) if (i.usdPrice == null) throw new Refused(`no USD price for ${i.symbol}; cannot size a rebalance`);
  return sides;
}

// What `acting.owner` may sell of one side, in human units: the profile's
// sleeve (LPBOT_SLEEVE) of what it holds, less the native reserve: a key
// wallet's gas, a vault's floor (its payer key pays the gas). A vault is a
// PDA, so its token accounts are derived off the curve.
export async function sellable(connection, acting, info, { vaultFloor, gasReserve }) {
  const ata = spl.getAssociatedTokenAddressSync(new PublicKey(info.mint), acting.owner, true, new PublicKey(info.program));
  const a = await connection.getAccountInfo(ata);
  let raw = a ? a.data.readBigUInt64LE(64) : 0n;
  if (info.mint === SOLANA.native_mint) raw += BigInt(await connection.getBalance(acting.owner));
  const total = sleeveCap(Number(raw) / 10 ** info.decimals, info.mint, parseSleeve(process.env.LPBOT_SLEEVE));
  const reserve = info.mint !== SOLANA.native_mint ? 0 : acting.mode === 'vault' ? vaultFloor : gasReserve;
  const avail = Math.max(0, total - reserve);
  return { total, avail, usd: avail * info.usdPrice };
}
