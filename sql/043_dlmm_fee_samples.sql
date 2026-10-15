-- 043: Meteora DLMM fee counters, for the fee / LVR study (owner, 2026-10-15:
-- "add 5rCf to the fee sampler").
--
-- DLMM has no pool-wide fee growth. Each bin keeps its own fee per unit of
-- liquidity share, so one sample is the bins around the active one
-- (venues/meteora_dlmm/fees.py). A table of its own, not fee_growth: the
-- venue board ranks pools on fee_growth with the CLMM formula, and a DLMM row
-- there would be read wrongly and could move a profile. Nothing in the bot
-- reads this table. Kept 14 days, as fee_growth. Additive and idempotent.
begin;
set local lock_timeout = '5s';
create table if not exists rebalancer.dlmm_fee_samples (
  id               bigint generated always as identity primary key,
  ts               timestamptz not null default now(),
  pool             text not null,
  active_id        integer not null,
  bin_step         integer not null,
  base_fee_pct     double precision not null,
  dynamic_fee_pct  double precision not null,
  dec_x            integer not null,
  dec_y            integer not null,
  -- [[id, price raw Y per raw X, x raw, y raw, supply, fee X per token, fee Y per token]];
  -- the big integers as decimal strings
  bins             jsonb not null
);
create index if not exists dlmm_fee_samples_pool_ts on rebalancer.dlmm_fee_samples (pool, ts desc);
commit;
