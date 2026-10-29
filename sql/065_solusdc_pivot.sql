-- 065: sol-usdc unpinned for the kept-fee pool review (owner 2026-10-29:
-- "if SOL/USD1 beats 8sLb, the bot pivots"). lp/board.py ranks pools on
-- +/-1% fees x (1 - 1/R7d) and moves between the pivot pools of solana.json
-- (8sLb and SOL/USD1 0.25%); payouts follow the pool's stablecoin, no swap.
-- tang0 and test0 stay pinned: a vault holds one payout token (USDC) by its
-- owners' policies. They log what they would do.
update rebalancer.config set pool_pinned = false, updated_at = now() where name = 'sol-usdc' and pool_pinned;
