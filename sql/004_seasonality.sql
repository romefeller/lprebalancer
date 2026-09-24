-- The board learns the day's rhythm and checks the model against the tape.
--
--   scan_runs.season            hour-of-day volume multipliers (24 numbers,
--                               1.0 = the average hour), from the candles of
--                               every pool the scan scored
--   config.defer_moves_to_quiet_hours
--                               a VOLUNTARY move (reband, pool move) waits for
--                               an hour whose multiplier is at or under 1.0;
--                               an out-of-band rebalance never waits
--
-- Idempotent.
alter table rebalancer.scan_runs add column if not exists season jsonb;
alter table rebalancer.config
  add column if not exists defer_moves_to_quiet_hours boolean not null default true;
