-- 057 (owner, 2026-10-19): record the great fee moments and the bad ones, with
-- every stat of the hour, so they can be found and compared later.
--
-- Every poll (lp/moments.py) adds to its risk_profile row, for the held pool:
--   fee_pct_1h    fees the held band earned over the last hour, % of its value,
--                 from the pool's own fee counters (fee per unit liquidity x the
--                 band's concentration)
--   fee_loss_1h   the pool's fees over its in-band loss over the same hour
--   vol_1h_usd    the pool's traded volume of the hour (five-minute tape)
--   range_1h_pct  high over low of the hour, in %
--   drift_1h_pct  close now over close an hour ago, in %
--   l_share       our liquidity over the pool's active liquidity
--   moment        'GREAT' (fee_pct_1h at or above the pool's 7-day 90th
--                 percentile), 'BAD' (fees under 0.8x the loss over 3h while the
--                 hour's volatility is at or above its 7-day 90th percentile), or
--                 null. A spell's start and end is a FEE_MOMENT event and a
--                 Telegram message with the stats; nothing is traded on it.
begin;
set local lock_timeout = '5s';
alter table rebalancer.risk_profile
  add column if not exists fee_pct_1h   numeric,
  add column if not exists fee_loss_1h  numeric,
  add column if not exists vol_1h_usd   numeric,
  add column if not exists range_1h_pct numeric,
  add column if not exists drift_1h_pct numeric,
  add column if not exists l_share      numeric,
  add column if not exists moment       text;
create index if not exists risk_profile_moment_idx on rebalancer.risk_profile (pool, ts) where moment is not null;
commit;
