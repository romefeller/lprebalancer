-- 063 (owner, 2026-10-23): sol-usdc may move venue again when the board finds a better one.
-- Reverts only the execute_dexes part of 062; min_deploy_usd stays 2.
begin;
set local lock_timeout = '5s';
update rebalancer.config set execute_dexes = array['orca', 'meteora-dlmm', 'raydium-clmm', 'byreal'], updated_at = now()
 where name = 'sol-usdc';
commit;
