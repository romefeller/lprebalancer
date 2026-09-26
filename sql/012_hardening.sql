-- Hardening after the 2026-09-26 reviews.
--   payouts.kind 'uncertain'  a transfer that may have landed: recorded, never re-sent
--   reward_max_usd            a reward balance worth more than this is held for a look
alter table rebalancer.payouts drop constraint if exists payouts_kind_check;
alter table rebalancer.payouts add constraint payouts_kind_check
  check (kind in ('paid', 'reinvested', 'gas', 'owed', 'uncertain'));
alter table rebalancer.config add column if not exists reward_max_usd numeric(12,4) not null default 25.0;
