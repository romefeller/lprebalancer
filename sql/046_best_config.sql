-- 046: every SOL/USDC bot on the replayed best configuration (owner, 2026-10-17:
-- "ALL BOTS MUST BE IN THE BEST CONFIGURATION ... WITHOUT TAKING FROM EACH OTHER").
--
-- Replay of 2026-10-03..10-17: our four bots held 65% of 8sLb's active liquidity
-- and split its ~$29/day of fees among themselves. Best placement: tang0 and
-- sol-usdc in Raydium 3ucNos (fee flow ~$5,168/day, our share ~1%), test0 and
-- sol-swing in 8sLb; touch threshold 0.60 for all: fees +89% (all four $14.19
-- -> $26.77/day), money kept +$8.26/day.
--
--   1. regime_threshold 0.60 on every enabled SOL/USDC bot (lp/tuning.py lifts the
--      ceiling of the factor-scaled threshold from 0.40 to 0.96);
--   2. sol-usdc pinned: the home-pool rule ranks per unit of liquidity and would
--      pull it back into 8sLb, where our own liquidity dilutes it. Its move to
--      3ucNos is the operator's MIGRATE file. tang0 moves once its vault owners
--      approve 3ucNos (a vault policy for the pool).
-- Idempotent.
begin;
set local lock_timeout = '5s';
update rebalancer.config set regime_threshold = 0.60, updated_at = now()
 where name in ('sol-usdc', 'sol-swing', 'test0', 'tang0') and regime_threshold <> 0.60;
update rebalancer.config set pool_pinned = true, updated_at = now()
 where name = 'sol-usdc' and not pool_pinned;
commit;
