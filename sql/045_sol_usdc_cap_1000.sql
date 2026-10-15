-- 045: sol-usdc's position cap from $600 to $1,000 (owner, 2026-10-15: "RISE THE CAP
-- TO 1000 OF SOL/USDC"). The poly capital came in as 4.095 SOL (sql-free booking,
-- tx 51RJUxGR…) and equity is about $627: at $600 the sizing held each side under
-- 55% of the cap ($545 deployable) and the signer refused any position over $600.
-- The profile reads max_usd at start: restart lp-bot@sol-usdc after this. Idempotent.
begin;
set local lock_timeout = '5s';
update rebalancer.config set max_usd = 1000, updated_at = now()
 where name = 'sol-usdc' and max_usd <> 1000;
commit;
