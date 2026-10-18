-- 053 (owner, 2026-10-18): keep the LP from shrinking to pay dividends.
--
--   payout_governor        each harvest pays out only the share of its fees
--                          that the profile's bands closed in the last
--                          governor_days earned above their re-centre loss,
--                          minus governor_keep of that profit (kept, so the LP
--                          grows), never above governor_cap (lp/governor.py).
--                          Audit 2026-10-18, 211 real bands on 8sLb over 20
--                          days: fees $178.13, re-centre loss -$153.02; the
--                          ~50% payout of then paid $101 and the LP fell ~$76
--                          apart from SOL's price.
--   hot_pause_every_hot    pause in every moment the regime calls HOT, with no
--                          fee check. Audit by the bot's own label, 333 h on
--                          8sLb: fees / loss CALM 1.55, WARM 1.04, HOT 0.84.
--                          Resume after 30 min clear, no cooldown.
--
-- And sol-swing no longer swings: it stays on SOL/USDC 8sLb.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config
  add column if not exists payout_governor     boolean      not null default false,
  add column if not exists governor_days       numeric(5,2) not null default 7,
  add column if not exists governor_keep       numeric(4,3) not null default 0.2,
  add column if not exists governor_cap        numeric(4,3) not null default 0.5,
  add column if not exists hot_pause_every_hot boolean      not null default false;
alter table rebalancer.config drop constraint if exists config_governor_sane;
alter table rebalancer.config add constraint config_governor_sane check (
  governor_days between 1 and 30 and governor_keep between 0 and 1 and governor_cap between 0 and 1);
update rebalancer.config
   set payout_governor = true, hot_pause_every_hot = true, hot_pause_enabled = true,
       hot_pause_cooldown_minutes = 0, hot_pause_resume_minutes = 30
 where name in ('sol-usdc', 'sol-swing', 'tang0', 'test0');
update rebalancer.swing set enabled = false, updated_at = now() where profile = 'sol-swing';
commit;
