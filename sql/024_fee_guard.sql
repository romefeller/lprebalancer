-- 024: the fee/variance guard (calm.fee_variance_ratio, calm.guard_width).
-- In range, fees and the price path's (gamma) loss both scale with the band's
-- concentration, so the trailing ratio of the two, not the width, says whether
-- liquidity earns more than it loses (2026-10-03 study and backtest).
--   regime_guard               'off' | 'live' (the touch rule's width, the
--                              widest while the ratio is low) | 'narrow'
--                              (the narrowest while the ratio is high, else
--                              the widest)
--   regime_guard_window_bars   trailing five-minute bars of the ratio
--   regime_guard_threshold     the ratio at which liquidity is concentrated
--   regime_guard_fee_c         {pool: fee yield per dollar of volume per
--                              dollar of full-range liquidity}; a pool not
--                              listed has no ratio, so the guard does not act
alter table rebalancer.config
  add column if not exists regime_guard             text          not null default 'off',
  add column if not exists regime_guard_window_bars integer       not null default 72,
  add column if not exists regime_guard_threshold   numeric(6,3)  not null default 1.0,
  add column if not exists regime_guard_fee_c       jsonb         not null default '{}'::jsonb;
alter table rebalancer.config drop constraint if exists config_regime_guard_sane;
alter table rebalancer.config add constraint config_regime_guard_sane check (
  regime_guard in ('off', 'live', 'narrow') and regime_guard_window_bars between 6 and 576
  and regime_guard_threshold > 0 and jsonb_typeof(regime_guard_fee_c) = 'object');
