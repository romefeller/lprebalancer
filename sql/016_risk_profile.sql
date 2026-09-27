-- The risk profile the bot acted on, one row per poll: the regime's choice
-- and every volatility figure behind it (calm.risk_metrics), with the
-- hourly survival forecast. Kept 30 days, like touch_forecasts.
begin;
set local lock_timeout = '5s';
create table if not exists rebalancer.risk_profile (
  id                bigint generated always as identity primary key,
  ts                timestamptz not null default now(),
  pool              text not null,
  mint              text,
  price             double precision not null,
  -- the regime's decision
  mode              text,
  choice_pct        double precision,
  held_pct          double precision,
  inside            boolean,
  p_held            double precision,
  threshold         double precision,
  threshold_base    double precision,
  horizon_min       integer,
  stale             boolean not null default false,
  bar_age_s         integer,
  probs             jsonb,                  -- [[half-width %, P(touch)], ...]
  -- volatility, its velocity, its heteroskedasticity
  sigma_5m_pct      double precision,       -- EWMA, one-hour half-life
  velocity          double precision,       -- log change of sigma per hour
  instability       double precision,       -- std of d log sigma over an hour
  rms_1h_pct        double precision,
  rms_6h_pct        double precision,
  rms_24h_pct       double precision,
  vol_ratio_1h_24h  double precision,
  park_1h_pct       double precision,
  vol_of_vol_24h    double precision,
  acf_r2_lag1_24h   double precision,
  arch_lm_24h       double precision,
  arch_lm_p_24h     double precision,
  kurtosis_24h      double precision,
  n_bars            integer,
  -- the hourly tape's view
  sigma_24h_pct     double precision,
  vol_regime_x      double precision,
  p_exit_6h         double precision,
  p_exit_24h        double precision,
  p_exit_72h        double precision,
  p_exit_168h       double precision,
  band_position     double precision,
  -- liquidity
  liquidity_factor  double precision,
  inflow            double precision,
  volume_x          double precision
);
create index if not exists risk_profile_ts on rebalancer.risk_profile (ts desc);
create index if not exists risk_profile_pool_ts on rebalancer.risk_profile (pool, ts desc);
commit;
