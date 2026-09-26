-- Rewards in the split. After each harvest, any reward token in the LP wallet
-- that is not one of the pool's own tokens is handled by `reward_policy`:
--
--   payout   swap it to payout_mint through Jupiter and send it to the profit
--            wallet; while gas is under the reserve, swap it to native SOL
--            instead and keep it as gas
--   hold     leave it in the LP wallet
--
-- reward_min_usd: a reward balance worth less than this waits for the next
-- harvest, so dust is not swapped at a loss.
alter table rebalancer.config
  add column if not exists reward_policy  text          not null default 'payout',
  add column if not exists reward_min_usd numeric(12,4) not null default 1.0;
alter table rebalancer.config drop constraint if exists config_reward_sane;
alter table rebalancer.config add constraint config_reward_sane check (
  reward_policy in ('payout', 'hold') and reward_min_usd >= 0);
