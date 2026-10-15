// Meteora DLMM fee counters around the active bin, for the fee / LVR study
// (research/dlmm_fees.py). DLMM has no pool-wide fee growth: each bin keeps
// its own fee per unit of liquidity share, which only grows.
//
//   node venues/meteora_dlmm/fee_sample.mjs <pool> [span]
//
// Prints one JSON object: the active bin, the bin step, the base and the
// current dynamic fee (%), the token decimals, and for every bin within
// `span` bins of the active one [id, price (raw Y per raw X), x raw, y raw,
// supply, fee X per token stored, fee Y per token stored]. Big integers are
// decimal strings. Read only; no key is loaded.
// The package's ESM build fails to load under Node 24 (see probe.mjs).
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const dlmmPkg = require('@meteora-ag/dlmm');
const DLMM = dlmmPkg.default ?? dlmmPkg;
const { Connection, PublicKey } = require('@solana/web3.js');

const RPC = process.env.LPBOT_RPC ?? process.env.SOLANA_RPC_URL
  ?? 'https://api.mainnet-beta.solana.com';

async function main() {
  const [pool, spanArg] = process.argv.slice(2);
  if (!pool) throw new Error('usage: node venues/meteora_dlmm/fee_sample.mjs <pool> [span]');
  const span = Number(spanArg ?? 60);
  if (!Number.isInteger(span) || span < 1 || span > 200) throw new Error(`span must be 1..200, got ${spanArg}`);
  const connection = new Connection(RPC, 'confirmed');
  const dlmm = await DLMM.create(connection, new PublicKey(pool));
  const { activeBin, bins } = await dlmm.getBinsAroundActiveBin(span, span);
  const fee = dlmm.getFeeInfo();
  console.log(JSON.stringify({
    pool,
    active_id: activeBin,
    bin_step: dlmm.lbPair.binStep,
    base_fee_pct: Number(fee.baseFeeRatePercentage.toString()),
    dynamic_fee_pct: Number(dlmm.getDynamicFee().toString()),
    dec_x: dlmm.tokenX.mint.decimals,
    dec_y: dlmm.tokenY.mint.decimals,
    mint_x: dlmm.tokenX.publicKey.toBase58(),
    mint_y: dlmm.tokenY.publicKey.toBase58(),
    bins: bins.map(b => [b.binId, Number(b.price), b.xAmount.toString(), b.yAmount.toString(),
      b.supply.toString(), b.feeAmountXPerTokenStored.toString(), b.feeAmountYPerTokenStored.toString()]),
  }));
}

main().catch(e => { console.error('ERROR:', String(e?.message ?? e).slice(0, 300)); process.exitCode = 1; });
