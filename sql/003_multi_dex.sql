-- The optimiser looks across DEXes, not only across bands.
--
-- The scanner lists the busiest concentrated-liquidity pools on every DEX in
-- `dexes`, scores each one under the same replay the bot uses to choose a
-- band, screens the top of the board, and writes the result here. The
-- bot then compares the pool it holds against the best pool it could hold, and
-- moves when the modelled gain clears `migrate_min_gain` and the target's DEX
-- has a signer (`execute_dexes`).
--
-- Idempotent: every statement checks before it acts.

alter table rebalancer.config
  -- which DEX the pinned pool lives on; the signer is chosen by it
  add column if not exists dex                    text        not null default 'orca',
  -- the scan
  add column if not exists dexes                  text[]      not null
      default '{orca,raydium-clmm,meteora-dlmm,byreal,pancakeswap-v3-solana}',
  add column if not exists scan_limit             integer     not null default 30,
  add column if not exists scan_interval_seconds  integer     not null default 21600,
  add column if not exists min_volume_24h_usd     numeric(18,2) not null default 1000000,
  -- the move
  add column if not exists migrate_min_gain       numeric(6,4) not null default 0.50,
  add column if not exists execute_dexes          text[]      not null default '{orca}',
  add column if not exists pool_pinned            boolean     not null default false,
  add column if not exists allow_swap             boolean     not null default false;

alter table rebalancer.positions
  add column if not exists dex text not null default 'orca';

alter table rebalancer.config drop constraint if exists config_sane;
alter table rebalancer.config add constraint config_sane check (
  capital_usd > 0 and max_usd >= capital_usd
  and gas_reserve_sol >= 0
  and poll_seconds between 30 and 86400
  and min_rebalance_gap_seconds >= 0
  and max_rebalances_per_day between 1 and 100
  and reopt_min_gain >= 0
  and slippage_bps between 1 and 1000
  and swap_cost_bps between 0 and 500
  and side_cap_fraction between 0.5 and 1.0
  and array_length(bands, 1) >= 1
  and scan_limit between 1 and 200
  and scan_interval_seconds >= 600
  and migrate_min_gain >= 0
  and min_volume_24h_usd >= 0
);

-- ---------------------------------------------------------------------------
-- scan_runs: one row per scan of the board
-- ---------------------------------------------------------------------------
create table if not exists rebalancer.scan_runs (
  id            bigint generated always as identity primary key,
  ts            timestamptz not null default now(),
  config_name   text,
  dexes         text[]      not null,
  pools_listed  integer     not null default 0,
  pools_scored  integer     not null default 0,
  duration_s    numeric(10,2),
  errors        jsonb       not null default '{}'::jsonb,
  best          jsonb
);
create index if not exists scan_runs_ts on rebalancer.scan_runs (ts desc);

-- ---------------------------------------------------------------------------
-- scan_pools: every pool the scan looked at, scored or with the reason not
-- ---------------------------------------------------------------------------
create table if not exists rebalancer.scan_pools (
  id              bigint generated always as identity primary key,
  run_id          bigint      not null references rebalancer.scan_runs (id) on delete cascade,
  rank            integer,
  dex             text        not null,
  kind            text,
  address         text        not null,
  pair            text,
  fee             numeric(12,10),
  tvl_usd         numeric(18,2),
  volume_24h_usd  numeric(18,2),
  c_pool          numeric(10,3),
  band            numeric(8,4),
  net_day_pct     numeric(10,5),
  rebal_per_day   numeric(8,4),
  p25_net_day     numeric(10,5),
  worst_net_day   numeric(10,5),
  share_positive  numeric(5,4),
  p_survive_168h  numeric(5,4),
  executable      boolean     not null default false,
  screen_ok       boolean,
  screen_reason   text,
  skipped         text,
  detail          jsonb
);
create index if not exists scan_pools_run  on rebalancer.scan_pools (run_id, rank);
create index if not exists scan_pools_pool on rebalancer.scan_pools (address, run_id desc);
