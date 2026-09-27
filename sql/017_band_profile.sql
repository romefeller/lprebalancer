-- One row per band (position): how long it survived, the market it lived in,
-- the mode, and what it earned. Written at a harvest (the running figures)
-- and at the rebalance that ends it (final). The data set for an automatic
-- mode switch: which conditions at open give which life and which fees.
--
-- Nothing here repeats positions: pool, dex, pair, band width, open and close
-- times, deposit and withdrawal are joined from there by mint. Every figure is
-- derived from risk_profile, snapshots and harvests, so rewriting a row gives
-- the same row: one per mint, never two. Kept without a window: it is the
-- learning set, one small row per band.
begin;
set local lock_timeout = '5s';
create table if not exists rebalancer.band_profile (
  mint                  text primary key references rebalancer.positions (mint),
  updated_at            timestamptz not null default now(),
  last_event            text not null check (last_event in ('harvest', 'rebalance')),
  final                 boolean not null default false,  -- true once the band is closed
  rebalanced_at         timestamptz,                     -- when the rebalance that ended it ran
  exit_reason           text,                            -- 'price went below', 'regime WARM: ...'
  survived_hours        double precision,                -- open to close (or to now, while it runs)
  polls                 integer,                         -- risk_profile rows over the life
  in_range_share        double precision,                -- share of snapshots in range
  -- the mode
  mode_open             text,
  mode_close            text,
  mode_main             text,                            -- the most frequent mode over the life
  mode_share            jsonb,                           -- {"WARM": 0.8, "HOT": 0.2}
  -- at open: what a decision at open time can see
  sigma_open            double precision,
  velocity_open         double precision,
  instability_open      double precision,
  arch_lm_p_open        double precision,
  vol_ratio_open        double precision,
  choice_pct_open       double precision,
  p_held_open           double precision,
  p_exit_6h_open        double precision,
  -- over the life
  sigma_mean            double precision,
  sigma_max             double precision,
  sigma_close           double precision,
  velocity_mean         double precision,
  velocity_abs_mean     double precision,
  velocity_close        double precision,
  instability_mean      double precision,                -- heteroskedasticity of the tape
  vol_of_vol_mean       double precision,
  arch_lm_mean          double precision,
  arch_lm_p_mean        double precision,
  acf_r2_mean           double precision,
  kurtosis_mean         double precision,
  vol_ratio_mean        double precision,
  rms_1h_mean           double precision,
  rms_24h_mean          double precision,
  park_1h_mean          double precision,
  liquidity_factor_mean double precision,
  volume_x_mean         double precision,
  -- what it earned (harvests, which include fees a close collected)
  fees_a                numeric(38,18),
  fees_b                numeric(38,18),
  fees_usd              numeric(18,6),
  harvests              integer,
  fees_per_hour_usd     double precision
);
create index if not exists band_profile_rebalanced on rebalancer.band_profile (rebalanced_at desc);
create index if not exists band_profile_mode on rebalancer.band_profile (mode_main, final);
commit;
