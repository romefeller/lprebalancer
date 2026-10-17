-- 050: crowding-aware board reverted; tang0 and sol-usdc back on Raydium 8sLb
-- (owner, 2026-10-17: "build the revert, move both back to 8sLb NOW").
--
-- sql/046 moved tang0 and sol-usdc to Raydium 3ucNos on a replay that held 8sLb's
-- volume fixed: our bots held 65% of its liquidity, so leaving was to give the
-- rest more. Live 18:04-20:00Z: tang0 and sol-usdc earned $0.23-0.26/h per $1k
-- in 3ucNos, test0 and sol-swing $0.56 in 8sLb; the fee counters gave 3ucNos
-- ~0.65x the fee per unit of liquidity of 8sLb in 28 of 30 hours. The bots that
-- stayed in 8sLb earned no more after the move: its volume follows its depth.
--
-- The board ranks plain on-chain income again (lp/board.py). sol-usdc is pinned
-- in 8sLb (the MIGRATE file moved both at 20:01-20:04Z), so the board does not
-- move it. Pool liquidity stays sampled in fee_growth (sql/048) as evidence.
-- Idempotent.
begin;
set local lock_timeout = '5s';
update rebalancer.config set pool_pinned = true, updated_at = now() where name = 'sol-usdc' and not pool_pinned;
commit;
