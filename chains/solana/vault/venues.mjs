// What the vault needs to know about each venue beyond its rules: the
// accounts every operation on a pool touches, for the vault's lookup table,
// and whether a pool fits the rules at all. One entry per venue in
// rules.json; a venue without one cannot be approved.
import { createRequire } from 'module';
import { RULES, poolPda } from './policy.mjs';
import SOLANA from '../solana.json' with { type: 'json' };

const require = createRequire(import.meta.url);
const { PublicKey, SYSVAR_RENT_PUBKEY } = require('@solana/web3.js');

const DLMM_PROGRAM = new PublicKey(SOLANA.programs.meteora_dlmm);
const DEFAULT = PublicKey.default.toBase58();

// The Raydium CLMM layout, Byreal's too (a fork: same pool account, same
// instructions). A pool whose initialized rewards differ from the venue's
// "rewards" is refused: the decrease rule pins one recipient per reward, and
// a reward it does not pin could be paid anywhere.
function raydiumLayout(venue) {
  const program = new PublicKey(RULES.venues[venue].program);
  const bitmap = RULES.venues['raydium-clmm'].instructions.find(i => i.name === 'decrease_liquidity_v2')
    .pins.find(p => p.role === 'pda').seeds;
  return {
    program,
    shared: () => [program, SYSVAR_RENT_PUBKEY],
    async poolAccounts(connection, pool) {
      const { PoolInfoLayout } = require('@raydium-io/raydium-sdk-v2');
      const a = await connection.getAccountInfo(new PublicKey(pool));
      if (!a?.owner.equals(program)) throw new Error(`${pool} is not a ${venue} pool`);
      const p = PoolInfoLayout.decode(a.data);
      const rewards = p.rewardInfos.filter(r => r.mint.toBase58() !== DEFAULT);
      if (rewards.length !== RULES.venues[venue].rewards) {
        throw new Error(`${venue} pool ${pool} has ${rewards.length} rewards; the vault rules pin ${RULES.venues[venue].rewards}`);
      }
      return {
        mints: [p.mintA, p.mintB, ...rewards.map(r => r.mint)],
        accounts: [new PublicKey(pool), p.configId, p.vaultA, p.vaultB, p.observationId, p.mintA, p.mintB,
          poolPda(bitmap, pool, program), ...rewards.map(r => r.vault)],
      };
    },
  };
}

// Orca's Whirlpool: the pool's vaults, mints, oracle and reward vaults.
function whirlpoolLayout() {
  const program = new PublicKey(SOLANA.programs.orca_whirlpool);
  return {
    program,
    shared: () => [program],
    async poolAccounts(connection, pool) {
      const { getWhirlpoolDecoder, getOracleAddress } = await import('@orca-so/whirlpools-client');
      const a = await connection.getAccountInfo(new PublicKey(pool));
      if (!a?.owner.equals(program)) throw new Error(`${pool} is not an Orca whirlpool`);
      const w = getWhirlpoolDecoder().decode(a.data);
      const rewards = w.rewardInfos.filter(r => String(r.mint) !== DEFAULT);
      const [oracle] = await getOracleAddress(String(pool));
      const key = (k) => new PublicKey(String(k));
      return {
        mints: [w.tokenMintA, w.tokenMintB, ...rewards.map(r => r.mint)].map(key),
        accounts: [pool, w.tokenVaultA, w.tokenVaultB, w.tokenMintA, w.tokenMintB, oracle, ...rewards.map(r => r.vault)].map(key),
      };
    },
  };
}

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
  'raydium-clmm': raydiumLayout('raydium-clmm'),
  byreal: raydiumLayout('byreal'),
  orca: whirlpoolLayout(),
};

export function venue(name) {
  const v = VENUES[name];
  if (!v) throw new Error(`venue ${name} has no vault support (have: ${Object.keys(VENUES).join(', ')})`);
  return v;
}
