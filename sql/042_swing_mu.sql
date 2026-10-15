-- 042: the swing holds MU/USDC in NYSE hours instead of DJT/USDC (owner,
-- 2026-10-15: "make sol-swing swap to MU instead of DJT").
--
-- MU/USDC: Meteora DLMM 13MEx6gj…, the largest MU pool (2026-10-15: TVL
-- $4.0M, volume $4.3M/24h, bin step 20, base fee 0.20%, variable fee on top).
-- mu-usdc ran on it 2026-10-02 from wallet sol-lp; the Meteora signer opened
-- and closed MU (Token-2022) there.
--
-- Three writes, all needed for the switch at the NYSE open:
--   1. the swing row's open pool (swing.py reads the row every tick);
--   2. sol-swing may execute on Meteora DLMM (the MIGRATE gate needs an armed
--      signer: lp/board.py operator_target);
--   3. outside this file: /etc/systemd/system/lp-bot@sol-swing.service.d/swing.conf
--      pins LPBOT_SWING_POOLS to the MU and SOL/USDC pools, and the profile
--      restarts to read it and the new execute_dexes.
-- Orca stays armed: DJT left behind by an earlier switch can still sell
-- through the held-pool fallback. Idempotent.
begin;
set local lock_timeout = '5s';

update rebalancer.swing
   set open_dex = 'meteora-dlmm',
       open_pool = '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5',
       updated_at = now()
 where profile = 'sol-swing'
   and open_pool is distinct from '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5';

update rebalancer.config
   set execute_dexes = array['raydium-clmm', 'orca', 'meteora-dlmm'],
       dexes = array['raydium-clmm', 'orca', 'meteora-dlmm'],
       updated_at = now()
 where name = 'sol-swing'
   and not ('meteora-dlmm' = any(execute_dexes));
commit;
