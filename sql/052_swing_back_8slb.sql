-- 052: sol-swing back on Raydium 8sLb after the NYSE close (owner, 2026-10-17: "is byreal good?").
-- 20:54-23:50Z, same hours: Byreal 9GTj paid $0.38/h per $1k (0.91 -> 0.26 by hour), 8sLb
-- $0.78-0.83. Meteora 5rCf (test0) paid 0.095 and went back at 20:53Z. No second pool beats
-- 8sLb now. byreal stays armed for sol-swing (unused). Applied live 23:51Z. Idempotent.
begin;
set local lock_timeout = '5s';
update rebalancer.swing set closed_dex = 'raydium-clmm', closed_pool = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj', updated_at = now()
 where profile = 'sol-swing' and closed_pool <> '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj';
commit;
