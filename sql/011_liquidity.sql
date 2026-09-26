-- Pool liquidity and TVL, recorded every few minutes, and the regime settings
-- that use them (rebalancer.liquidity_view).
--   regime_tape_days      depth of the five-minute tape (30: 8640 bars)
--   regime_liq_min/max    bounds on the risk multiplier from fee density:
--                         the touch threshold is multiplied by
--                         clamp(volume factor / liquidity inflow, min, max)
create table if not exists rebalancer.pool_stats (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null default now(),
  dex         text not null,
  pool        text not null,
  liquidity   numeric(40,12),
  tvl_usd     numeric(18,2),
  volume_24h  numeric(18,2),
  price       numeric(38,12)
);
create index if not exists pool_stats_pool_ts on rebalancer.pool_stats (pool, ts desc);
alter table rebalancer.config
  add column if not exists regime_tape_days integer      not null default 30,
  add column if not exists regime_liq_min   numeric(4,3) not null default 0.600,
  add column if not exists regime_liq_max   numeric(4,3) not null default 1.250;
alter table rebalancer.config drop constraint if exists config_liq_sane;
alter table rebalancer.config add constraint config_liq_sane check (
  regime_tape_days between 3 and 60 and regime_liq_min > 0 and regime_liq_min <= 1
  and regime_liq_max >= 1 and regime_liq_max <= 2);
