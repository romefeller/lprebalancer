-- Rebalancer schema. Idempotent: safe to run against an existing database.
--
-- Conventions follow Postgres defaults rather than fighting them: lowercase
-- unquoted identifiers, bigint identity keys, timestamptz everywhere, and
-- numeric for anything that represents money. Token amounts are numeric too,
-- never float — a fee of 0.000265 SOL must survive a round trip exactly.

create schema if not exists rebalancer;

-- ---------------------------------------------------------------------------
-- config: every tunable. One row per profile; exactly one may be active.
-- ---------------------------------------------------------------------------
create table if not exists rebalancer.config (
  id                          bigint generated always as identity primary key,
  name                        text        not null unique,
  active                      boolean     not null default false,

  -- what to trade. Decimals live here so the bot is not pinned to one pair.
  pool                        text        not null,
  pair_label                  text        not null,
  token_a                     text        not null default 'A',
  token_b                     text        not null default 'B',
  decimals_a                  smallint    not null default 9,
  decimals_b                  smallint    not null default 6,

  -- size
  capital_usd                 numeric(18,6) not null,
  max_usd                     numeric(18,6) not null,
  reserve_a                   numeric(18,9) not null default 0.05,

  -- band search. bands are half-widths as multipliers: 1.05 means +/-5%.
  bands                       numeric(8,4)[] not null
                                default '{1.03,1.05,1.08,1.12,1.18,1.25,1.40}',
  max_modelled_rebal_per_day  numeric(8,4) not null default 0.50,

  -- cadence
  poll_seconds                integer     not null default 300,
  min_rebalance_gap_seconds   integer     not null default 3600,
  max_rebalances_per_day      integer     not null default 6,
  reopt_interval_seconds      integer     not null default 21600,
  reopt_min_gain              numeric(6,4) not null default 0.25,

  -- safety
  max_consecutive_failures    integer     not null default 3,
  max_unreadable_polls        integer     not null default 12,
  slippage_bps                integer     not null default 100,

  -- pool screening, used only when no pool is pinned
  max_leveraged               numeric(4,3) not null default 0.350,
  min_established             numeric(4,3) not null default 0.300,
  min_net_day_pct             numeric(8,4) not null default 0.0500,
  min_tvl_usd                 numeric(18,2) not null default 250000,

  updated_at                  timestamptz not null default now()
);

-- Only one profile may be active: the bot must never have to guess.
create unique index if not exists config_one_active
  on rebalancer.config ((active)) where active;

-- Values that would be nonsense rather than merely aggressive.
do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'config_sane') then
    alter table rebalancer.config add constraint config_sane check (
      capital_usd > 0 and max_usd >= capital_usd
      and poll_seconds between 30 and 86400
      and min_rebalance_gap_seconds >= 0
      and max_rebalances_per_day between 1 and 100
      and reopt_min_gain >= 0
      and slippage_bps between 1 and 1000
      and array_length(bands, 1) >= 1
    );
  end if;
end $$;

-- ---------------------------------------------------------------------------
-- positions: one row per position ever opened
-- ---------------------------------------------------------------------------
create table if not exists rebalancer.positions (
  mint          text primary key,
  config_name   text,
  pool          text not null,
  pair_label    text,
  opened_at     timestamptz not null default now(),
  closed_at     timestamptz,
  lower_price   numeric(38,12),
  upper_price   numeric(38,12),
  band_pct      numeric(8,4),
  open_sig      text,
  close_sig     text,
  deposit_usd   numeric(18,6),
  withdraw_usd  numeric(18,6),
  open_reason   text
);
create index if not exists positions_open
  on rebalancer.positions (opened_at desc) where closed_at is null;

-- ---------------------------------------------------------------------------
-- harvests: realised fees. Token amounts are the truth; usd is a convenience.
-- ---------------------------------------------------------------------------
create table if not exists rebalancer.harvests (
  id        bigint generated always as identity primary key,
  ts        timestamptz not null default now(),
  mint      text not null,
  fee_a     numeric(38,18) not null default 0,
  fee_b     numeric(38,18) not null default 0,
  fee_usd   numeric(18,6)  not null default 0,
  signature text
);
create index if not exists harvests_ts   on rebalancer.harvests (ts desc);
create index if not exists harvests_mint on rebalancer.harvests (mint);

-- ---------------------------------------------------------------------------
-- snapshots: the time series behind every statistic
-- ---------------------------------------------------------------------------
create table if not exists rebalancer.snapshots (
  id            bigint generated always as identity primary key,
  ts            timestamptz not null default now(),
  mint          text,
  price         numeric(38,12),
  in_range      boolean,
  liquidity     numeric(40,0),
  accrued_a     numeric(38,18),
  accrued_b     numeric(38,18),
  accrued_usd   numeric(18,6),
  wallet_usd    numeric(18,6),
  position_usd  numeric(18,6),
  equity_usd    numeric(18,6)
);
create index if not exists snapshots_ts on rebalancer.snapshots (ts desc);

-- ---------------------------------------------------------------------------
-- events: anything worth explaining later
-- ---------------------------------------------------------------------------
create table if not exists rebalancer.events (
  id     bigint generated always as identity primary key,
  ts     timestamptz not null default now(),
  kind   text not null,
  detail text
);
create index if not exists events_ts   on rebalancer.events (ts desc);
create index if not exists events_kind on rebalancer.events (kind);
