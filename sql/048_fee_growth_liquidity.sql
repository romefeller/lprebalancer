-- 048: each fee counter sample also keeps the pool's active liquidity (owner,
-- 2026-10-17: "build the crowding-aware board"). Our four bots held 65% of 8sLb's
-- active liquidity and split its fees among themselves; the board ranked pools by
-- fee per unit of liquidity, which our own liquidity in the pool does not change.
-- With the pool's liquidity and ours (db.our_liquidity) the board ranks what a
-- move adds to all our bots together (lp/board.py fleet_factor). Null in older
-- samples and on Uniswap v3: no crowding then. Additive and idempotent.
begin;
set local lock_timeout = '5s';
alter table rebalancer.fee_growth add column if not exists liquidity numeric(40,0);
commit;
