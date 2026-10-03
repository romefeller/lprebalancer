-- 026: the shape of the band reopened after an exit (2026-10-03 math study,
-- 1-second simulator with real fees, fixed +/-1% bands).
--   reopen_offset_frac    the new band's centre sits this fraction of its
--                         half-width (log) AGAINST the exit's direction: a
--                         band left above reopens a little below the price.
--                         The swap shrinks (the wallet after an exit holds the
--                         side the offset band needs more of) and the weak
--                         reversion after exits is on its side. 0 is centred.
--   reopen_widen_p        when P(touch the narrowest width within
--                         reopen_widen_minutes) is at least this at an exit,
--                         that one band reopens at reopen_widen_band; 0 is off
--   reopen_widen_band     the width of that band (1.015 = +/-1.5%)
--   reopen_widen_minutes  the forecast horizon
alter table rebalancer.config
  add column if not exists reopen_offset_frac   numeric(5,3) not null default 0,
  add column if not exists reopen_widen_p       numeric(5,3) not null default 0,
  add column if not exists reopen_widen_band    numeric(8,4) not null default 1.015,
  add column if not exists reopen_widen_minutes integer      not null default 30;
alter table rebalancer.config drop constraint if exists config_reopen_shape_sane;
alter table rebalancer.config add constraint config_reopen_shape_sane check (
  reopen_offset_frac >= 0 and reopen_offset_frac <= 0.5 and reopen_widen_p >= 0 and reopen_widen_p <= 1
  and reopen_widen_band > 1 and reopen_widen_band <= 1.2 and reopen_widen_minutes between 5 and 240);
