-- 033: the HOT pause on some pools of a profile only (owner, 2026-10-07: the
-- swing pauses its SOL/USDC hours and keeps DJT through HOT ones; DJT's fees
-- rose with its HOT volume in the 14-session regime replay). NULL: every
-- pool the profile holds, as before. A pool switch during a pause to a pool
-- not listed ends the pause at once.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config
  add column if not exists hot_pause_pools text[];
alter table rebalancer.config drop constraint if exists config_hot_pause_pools_sane;
alter table rebalancer.config add constraint config_hot_pause_pools_sane check (
  hot_pause_pools is null
  or (cardinality(hot_pause_pools) > 0 and array_position(hot_pause_pools, null) is null
      and array_position(hot_pause_pools, '') is null));
commit;
