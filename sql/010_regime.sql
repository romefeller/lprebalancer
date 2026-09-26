-- Regime mode: the band width follows the market (calm.regime_view).
--   regime_enabled          master switch; replaces calm mode's two widths
--   regime_widths           the ladder of half-widths it chooses from
--   regime_horizon_minutes  survival horizon (walk-forward best: 120)
--   regime_threshold        the narrowest width with P(touch) <= this is chosen
--   regime_steps            widen/narrow a held band when the choice moves this
--                           many rungs; exits always re-centre at the choice
-- calm_max_moves_per_day stays only as a runaway guard.
alter table rebalancer.config
  add column if not exists regime_enabled         boolean       not null default false,
  add column if not exists regime_widths          numeric(8,4)[] not null
      default '{1.01,1.0125,1.015,1.02,1.025,1.03,1.04,1.05}',
  add column if not exists regime_horizon_minutes integer       not null default 120,
  add column if not exists regime_threshold       numeric(5,4)  not null default 0.25,
  add column if not exists regime_steps           integer       not null default 2;
alter table rebalancer.config drop constraint if exists config_regime_sane;
alter table rebalancer.config add constraint config_regime_sane check (
  array_length(regime_widths, 1) >= 2 and regime_horizon_minutes between 15 and 720
  and regime_threshold > 0 and regime_threshold < 1 and regime_steps between 1 and 6);
