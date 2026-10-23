-- 062 (owner, 2026-10-23): sol-usdc runs with the same settings as tang0 and test0.
--
-- 1. min_deploy_usd 5 -> 2: idle cash below $5 was never added to the band.
--    sol-usdc held 1.65% of equity idle since 10-20 vs 0.56% (tang0) and
--    0.99% (test0); about 1% of its fees.
-- 2. execute_dexes -> raydium-clmm only: the board may no longer move it to
--    Orca, Meteora or Byreal. It stays in 8sLb like tang0. `dexes` (the board's
--    scoring) and venue_watch (read-only sampling) stay as they are.
begin;
set local lock_timeout = '5s';
update rebalancer.config set min_deploy_usd = 2, execute_dexes = array['raydium-clmm'], updated_at = now()
 where name = 'sol-usdc';
commit;
