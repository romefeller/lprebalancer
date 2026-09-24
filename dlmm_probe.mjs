// Meteora DLMM read probe: the liquidity around the active bin, which the
// pool API does not publish and the fee model cannot do without.
//
//   node dlmm_probe.mjs <pool> [<pool> ...]
//
// Prints one JSON object keyed by pool address. For each pool: the active bin,
// the bins `span` either side of it with their token amounts in human units,
// the current fee, and the bin step. Read only; no key is loaded.
// The package's ESM build imports a directory and fails to load under Node 24;
// the CommonJS build resolves cleanly, so it is loaded through `require`.
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const dlmmPkg = require('@meteora-ag/dlmm');
const DLMM = dlmmPkg.default ?? dlmmPkg;
const { Connection, PublicKey } = require('@solana/web3.js');

const RPC = process.env.LPBOT_RPC ?? process.env.SOLANA_RPC_URL
  ?? 'https://api.mainnet-beta.solana.com';
const SPAN = Number(process.env.DLMM_PROBE_SPAN ?? 12);

async function main() {
  const addrs = process.argv.slice(2).filter(a => !a.startsWith('--'));
  if (!addrs.length) throw new Error('usage: node dlmm_probe.mjs <pool> [...]');
  const connection = new Connection(RPC, 'confirmed');
  const pools = await DLMM.createMultiple(connection, addrs.map(a => new PublicKey(a)));
  const out = {};
  for (const [i, dlmm] of pools.entries()) {
    const addr = addrs[i];
    try {
      const dx = dlmm.tokenX.mint.decimals, dy = dlmm.tokenY.mint.decimals;
      const { activeBin, bins } = await dlmm.getBinsAroundActiveBin(SPAN, SPAN);
      const fee = dlmm.getFeeInfo();
      out[addr] = {
        activeBin,
        binStep: dlmm.lbPair.binStep,
        baseFeePct: Number(fee.baseFeeRatePercentage.toString()),
        maxFeePct: Number(fee.maxFeeRatePercentage.toString()),
        dynamicFeePct: Number(dlmm.getDynamicFee().toString()),
        bins: bins.map(b => ({
          id: b.binId,
          price: Number(b.pricePerToken),
          x: Number(b.xAmount.toString()) / 10 ** dx,
          y: Number(b.yAmount.toString()) / 10 ** dy,
        })),
      };
    } catch (e) {
      out[addr] = { error: String(e?.message ?? e).slice(0, 200) };
    }
  }
  console.log(JSON.stringify(out));
}

main().catch(e => { console.error('ERROR:', e.message); process.exitCode = 1; });
