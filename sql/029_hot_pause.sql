-- 029: pause in bad HOT moments (owner, 2026-10-03: "the bot stops on hot
-- moments, waiting for warmth/calm"). While the regime's width choice is
-- wider than +/-hot_pause_hot_pct AND the pool's own fee income over the last
-- hot_pause_fg_hours has paid less than hot_pause_fg_threshold x the in-band
-- (gamma) loss of the same hours (fee_growth counters / sum r^2 / 8 of the
-- five-minute tape), the band is closed and the capital waits 50/50.
-- The band reopens hot_pause_resume_minutes after the signal clears, or after
-- hot_pause_max_hours whatever the signal. No new pause for
-- hot_pause_cooldown_minutes after a resume (a ratio near the threshold
-- re-paused 17 min after a resume on the 10-02 data). A missing fee or tape reading is
-- not a bad moment: the bot earns rather than waits on no data.
-- 1-s replay, real fees 09-27..10-03, $230: fees -$0.35/day, equity +$0.57/day
-- vs live; halves +0.79 / +0.34; paused 12% of the time. Real fees from our
-- own position's accrued fees (snapshots), reconciled to harvests and to the
-- on-chain counters; the 1 h cooldown changes nothing in that replay.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config
  add column if not exists hot_pause_enabled        boolean      not null default false,
  add column if not exists hot_pause_hot_pct        numeric(5,2) not null default 2.0,
  add column if not exists hot_pause_fg_threshold   numeric(5,3) not null default 0.8,
  add column if not exists hot_pause_fg_hours       numeric(5,2) not null default 6,
  add column if not exists hot_pause_resume_minutes integer      not null default 30,
  add column if not exists hot_pause_max_hours      numeric(5,2) not null default 12,
  add column if not exists hot_pause_cooldown_minutes integer    not null default 60;
alter table rebalancer.config drop constraint if exists config_hot_pause_sane;
alter table rebalancer.config add constraint config_hot_pause_sane check (
  hot_pause_hot_pct > 0 and hot_pause_hot_pct <= 10 and hot_pause_fg_threshold > 0 and hot_pause_fg_threshold <= 3
  and hot_pause_fg_hours >= 1 and hot_pause_fg_hours <= 24 and hot_pause_resume_minutes between 5 and 720
  and hot_pause_max_hours >= 1 and hot_pause_max_hours <= 48 and hot_pause_cooldown_minutes between 0 and 1440);
commit;
