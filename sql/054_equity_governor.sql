-- 054 (owner, 2026-10-18): the payout governor pays from the equity, with two
-- states, in place of the closed bands' profit share (053).
--
--   governor_mode          'equity' (this file) or 'profit' (053, kept as the
--                          fallback: one UPDATE goes back to it).
--   governor_floor         the share of each harvest paid while the equity is
--                          not ahead of its target (5%: something is paid on
--                          every harvest).
--   governor_high          the share paid while it is ahead (30%).
--   governor_enter_pct     pay `high` from this far above the target (+5%).
--   governor_exit_pct      back to `floor` at this far above it or less (+2%).
--                          Between the two the state stays as it was.
--   governor_growth_pct    the target: every dollar put in (the baseline, each
--                          deposit, less each withdrawal), grown this much per
--                          30 days from the day it came (2%).
--   governor_paying        the state, written by the bot when it switches.
--   governor_switched_at   when it last switched.
--
-- Sweep 2026-10-18 (real days 09-28..10-17 resampled to one year, regime +
-- HOT pause, 200 paths, per $1,000): fees $16.1/d, paid $2.93/d, equity
-- $1,310 (100% of paths grow); at 18% less fee income: fees $11.1/d, paid
-- $0.72/d, equity $972. 0%/50% paid the same but went 45-60 days with no
-- payout; a fixed 20% left $563 at the lower fee income.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config
  add column if not exists governor_mode        text         not null default 'profit',
  add column if not exists governor_floor       numeric(4,3) not null default 0.05,
  add column if not exists governor_high        numeric(4,3) not null default 0.30,
  add column if not exists governor_enter_pct   numeric(5,2) not null default 5,
  add column if not exists governor_exit_pct    numeric(5,2) not null default 2,
  add column if not exists governor_growth_pct  numeric(5,2) not null default 2,
  add column if not exists governor_paying      boolean      not null default false,
  add column if not exists governor_switched_at timestamptz;
alter table rebalancer.config drop constraint if exists config_equity_governor_sane;
alter table rebalancer.config add constraint config_equity_governor_sane check (
  governor_mode in ('profit', 'equity')
  and governor_floor between 0 and 1 and governor_high between 0 and 1 and governor_floor <= governor_high
  and governor_exit_pct < governor_enter_pct and governor_growth_pct between -10 and 10);
update rebalancer.config set governor_mode = 'equity', updated_at = now()
 where payout_governor and name in ('sol-usdc', 'tang0', 'test0');
commit;
