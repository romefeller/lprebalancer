-- On-chain fee counters of candidate pools, sampled every few minutes, kept
-- two days. The change of a pool's fee_growth_global over time is what a unit
-- of liquidity at the active price earned: the venue review ranks on it.
create table if not exists rebalancer.fee_growth (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null default now(),
  dex         text not null,
  pool        text not null,
  sqrt_price  numeric(40,0) not null,
  g0          numeric(40,0) not null,
  g1          numeric(40,0) not null,
  rewards     jsonb not null default '[]'::jsonb,
  dec_a       integer not null,
  dec_b       integer not null,
  mint_a      text not null,
  mint_b      text not null
);
create index if not exists fee_growth_pool_ts on rebalancer.fee_growth (pool, ts desc);
alter table rebalancer.config
  add column if not exists venue_sample_seconds integer not null default 600,
  add column if not exists venue_min_hours      integer not null default 6;
alter table rebalancer.config drop constraint if exists config_venue_sane;
alter table rebalancer.config add constraint config_venue_sane check (
  venue_sample_seconds between 60 and 3600 and venue_min_hours between 1 and 24);
