-- The proactive policy and the survival record.
--
--   config.proactive_horizon_hours   look-ahead of the exit probability
--   config.proactive_threshold       re-centre when P(exit within horizon) is
--                                    at or above this; 0 disables the rule
--   config.harvest_interval_hours    the dividend: harvest accrued fees into
--                                    the wallet this often (0 = only on
--                                    rebalance)
--   config.min_harvest_usd           and only when at least this much accrued
--
--   snapshots.p_exit_6h / _24h / _72h   the forecast made at that poll, kept
--                                    so predicted and realised exits can be
--                                    compared later (db.py forecasts)
--   snapshots.band_position          -1 at the lower edge, 0 centred, +1 upper
--
-- Idempotent.
alter table rebalancer.config
  add column if not exists proactive_horizon_hours integer      not null default 6,
  add column if not exists proactive_threshold     numeric(5,4) not null default 0.5,
  add column if not exists harvest_interval_hours  integer      not null default 24,
  add column if not exists min_harvest_usd         numeric(12,4) not null default 0.25;

alter table rebalancer.snapshots
  add column if not exists p_exit_6h     numeric(5,4),
  add column if not exists p_exit_24h    numeric(5,4),
  add column if not exists p_exit_72h    numeric(5,4),
  add column if not exists band_position numeric(6,3);

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
  and proactive_horizon_hours between 1 and 168
  and proactive_threshold between 0 and 1
  and harvest_interval_hours between 0 and 720
  and min_harvest_usd >= 0
);
