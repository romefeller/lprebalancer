-- 049: sol-usdc back on the board (owner, 2026-10-17). sql/046 pinned it in 3ucNos
-- because the venue board ranked fee per unit of liquidity and would have pulled it
-- into 8sLb, where our own bots held 65% of the liquidity. The board now ranks what
-- a move adds to all our bots together (lp/board.py fleet_factor, sql/048), so it
-- may choose sol-usdc's pool again. tang0 and test0 stay pinned (their vaults hold
-- approved pools only), sol-swing too (the swing moves it). Idempotent.
begin;
set local lock_timeout = '5s';
update rebalancer.config set pool_pinned = false, updated_at = now() where name = 'sol-usdc' and pool_pinned;
commit;
