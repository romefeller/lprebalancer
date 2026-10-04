-- 030: a scheduled pause around FOMC decisions (2026-10-04 study, 400 days,
-- official Fed calendar, hour-matched windows): SOL variance x6.2 from 15 min
-- before to 2 h after the statement, fees x2.0 only; a pause won 8 of 9
-- events, ~+$9.5/yr of equity for ~$4.7/yr of fees on $230. CPI and NFP lost
-- more fees than they saved: not listed. The band is closed and the capital
-- waits 50/50 from macro_pause_before_minutes before each event in
-- rebalancer.macro_events until macro_pause_after_minutes after it.
begin;
set local lock_timeout = '5s';
create table if not exists rebalancer.macro_events (
  ts      timestamptz primary key,            -- the release (statement) time
  kind    text not null,                      -- 'FOMC'
  source  text not null
);
-- FOMC statements: 14:00 New York time on the second meeting day
-- (federalreserve.gov/monetarypolicy/fomccalendars.htm, read 2026-10-04).
insert into rebalancer.macro_events (ts, kind, source) values
  ('2026-10-28 18:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2026-12-09 19:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-01-27 19:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-03-17 18:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-04-28 18:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-06-09 18:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-07-28 18:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-09-15 18:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-10-27 18:00+00', 'FOMC', 'federalreserve.gov 2026-10-04'),
  ('2027-12-08 19:00+00', 'FOMC', 'federalreserve.gov 2026-10-04')
on conflict (ts) do nothing;
alter table rebalancer.config
  add column if not exists macro_pause_enabled        boolean not null default false,
  add column if not exists macro_pause_before_minutes integer not null default 15,
  add column if not exists macro_pause_after_minutes  integer not null default 120;
alter table rebalancer.config drop constraint if exists config_macro_pause_sane;
alter table rebalancer.config add constraint config_macro_pause_sane check (
  macro_pause_before_minutes between 0 and 240 and macro_pause_after_minutes between 15 and 720);
commit;
