-- 028: the liquidity factor reads the pool's active liquidity over a window,
-- not one reading (2026-10-03 forensic of 09-28). Active liquidity is the
-- liquidity at the current tick: it jumps (104k <-> 43k on 8sLb) each time
-- the price crosses the edge of a large position, so one reading flipped the
-- touch threshold 0.25 <-> 0.21 between polls and tipped marginal re-centres.
--   regime_liq_smooth_hours  inflow = geometric mean of the readings in the
--                            last N hours / their 24-hour median; 0 is the
--                            old rule (the newest reading alone)
-- Replay on 6 days of stored polls: factor steps > 0.05 fell 96 -> 3, width
-- choice changes 270 -> 240, mean factor 0.985 -> 0.990.
-- risk_profile.inflow is the smoothed inflow from now on; inflow_raw keeps
-- the newest reading's.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config
  add column if not exists regime_liq_smooth_hours numeric(4,2) not null default 2;
alter table rebalancer.config drop constraint if exists config_liq_smooth_sane;
alter table rebalancer.config add constraint config_liq_smooth_sane check (
  regime_liq_smooth_hours >= 0 and regime_liq_smooth_hours <= 12);
-- the newest reading's inflow, next to the smoothed one the factor used
alter table rebalancer.risk_profile add column if not exists inflow_raw double precision;
commit;
