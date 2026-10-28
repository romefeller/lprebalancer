-- 064 (owner, 2026-10-28): "tracking of other SOL/USDC pools ... fee stats and R in a shadow
-- manner ... a second best option ... also POL, Arbitrum and ETH".
--
-- One row per pool and hour, rolled up by ops/pool_watch.py from fee_growth: the fee yield
-- per dollar of full-range value and the sum of squared log price steps over the seconds
-- sampled. R over any window = sum(fee_yield) / (sum(var_sum) / 8). fee_growth keeps 14
-- days; this keeps a year, so R can be judged over months. Read-only research: no bot
-- reads it and it moves no money.
begin;
create table if not exists rebalancer.pool_hourly (
    pool      text not null,
    hour      timestamptz not null,
    dex       text not null,
    mint_a    text,
    mint_b    text,
    fee_yield double precision not null,
    var_sum   double precision not null,
    secs      double precision not null,
    primary key (pool, hour)
);
create index if not exists pool_hourly_hour on rebalancer.pool_hourly (hour);
commit;
