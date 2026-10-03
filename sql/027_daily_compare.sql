-- 027: the DAILY line's reference period. Each closed day is shown next to
-- the per-day average of daily_compare_from .. daily_compare_to (inclusive,
-- complete UTC days): fees earned, re-centres, value change and against a
-- 50/50 hold. Null: no comparison. 2026-10-03: the period before the
-- regime_threshold 0.25 / regime_steps 3 change.
alter table rebalancer.config
  add column if not exists daily_compare_from date,
  add column if not exists daily_compare_to   date;
alter table rebalancer.config drop constraint if exists config_daily_compare_sane;
alter table rebalancer.config add constraint config_daily_compare_sane check (
  (daily_compare_from is null) = (daily_compare_to is null) and
  (daily_compare_from is null or daily_compare_from <= daily_compare_to));
