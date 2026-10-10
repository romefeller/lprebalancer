// What the vault needs to know about each venue beyond its rules: the
// accounts every operation on a pool touches, for the vault's lookup table.
// One entry per venue in rules.json; a venue without one cannot be approved.
import { createRequire } from 'module';

const require = createRequire(import.meta.url);
const { PublicKey } = require('@solana/web3.js');

const DLMM_PROGRAM = new PublicKey('LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo');

export const VENUES = {
  'meteora-dlmm': {
    program: DLMM_PROGRAM,
    // Anchor's event authority: every DLMM instruction lists it.
    shared: () => [DLMM_PROGRAM, PublicKey.findProgramAddressSync([Buffer.from('__event_authority')], DLMM_PROGRAM)[0]],
    async poolAccounts(connection, pool) {
      const pkg = require('@meteora-ag/dlmm');
      const dlmm = await (pkg.default ?? pkg).create(connection, new PublicKey(pool));
      const p = dlmm.lbPair;
      return { mints: [p.tokenXMint, p.tokenYMint], accounts: [new PublicKey(pool), p.reserveX, p.reserveY, p.tokenXMint, p.tokenYMint, p.oracle] };
    },
  },
};

export function venue(name) {
  const v = VENUES[name];
  if (!v) throw new Error(`venue ${name} has no vault support (have: ${Object.keys(VENUES).join(', ')})`);
  return v;
}
