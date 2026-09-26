-- Calm mode: a tight band while five-minute volatility is low, the ladder's
-- band otherwise. See calm.py and lp_research/audited_research.md.
--
--   calm_enabled             master switch; false leaves the bot exactly as before
--   calm_band                tight half-width multiplier (1.01 = +/-1%)
--   calm_sigma_cut           enter calm at or below this EWMA sigma of 5-min log returns
--   calm_exit_mult           leave calm only above cut * this (hysteresis)
--   calm_horizon_minutes     look-ahead of the tight band's touch probability
--   calm_threshold           re-centre (still calm) or widen (calm over, no budget)
--                            when P(touch within horizon) reaches this; also the
--                            ceiling a FRESH tight band must be under to narrow
--   calm_min_gap_seconds     minimum time between two calm moves
--   calm_max_moves_per_day   calm moves allowed in 24h; spent = no more narrowing
--   calm_poll_seconds        poll interval while the tight band is held
--   rebalance_swap           swap to 50/50 through Jupiter before an open when the
--                            wallet is lopsided (one side under 40% of capital)
--
-- Idempotent.
alter table rebalancer.config
  add column if not exists calm_enabled           boolean       not null default false,
  add column if not exists calm_band              numeric(6,4)  not null default 1.0100,
  add column if not exists calm_sigma_cut         numeric(10,8) not null default 0.00152720,
  add column if not exists calm_exit_mult         numeric(5,3)  not null default 1.250,
  add column if not exists calm_horizon_minutes   integer       not null default 30,
  add column if not exists calm_threshold         numeric(5,4)  not null default 0.2500,
  add column if not exists calm_min_gap_seconds   integer       not null default 600,
  add column if not exists calm_max_moves_per_day integer       not null default 12,
  add column if not exists calm_poll_seconds      integer       not null default 120,
  add column if not exists rebalance_swap         boolean       not null default false;

alter table rebalancer.config drop constraint if exists config_calm_sane;
alter table rebalancer.config add constraint config_calm_sane check (
  calm_band > 1.001 and calm_band < 1.10
  and calm_sigma_cut > 0 and calm_sigma_cut < 0.05
  and calm_exit_mult >= 1.0 and calm_exit_mult <= 3.0
  and calm_horizon_minutes between 5 and 360
  and calm_threshold > 0 and calm_threshold <= 1
  and calm_min_gap_seconds >= 60
  and calm_max_moves_per_day between 0 and 48
  and calm_poll_seconds between 30 and 600
);
