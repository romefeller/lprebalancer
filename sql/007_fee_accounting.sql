-- Equity includes uncollected fees. Recompute, rather than increment, so this
-- repair is idempotent and historical and future equity use the same definition.
begin;
set local lock_timeout = '5s';
update rebalancer.snapshots
set equity_usd = wallet_usd + position_usd + coalesce(accrued_usd, 0)
where equity_usd is distinct from wallet_usd + position_usd + coalesce(accrued_usd, 0);

-- Cumulative fees by position at each observation. Harvest moves an existing
-- balance into the wallet; it does not create new earnings on the harvest day.
create or replace view rebalancer.fee_points as
with points as (
    select ts, mint, 2 as kind, id,
           coalesce(accrued_a, 0) a, coalesce(accrued_b, 0) b, coalesce(accrued_usd, 0) usd,
           0::numeric ha, 0::numeric hb, 0::numeric husd
    from rebalancer.snapshots
    union all
    select ts, mint, 1, id, 0, 0, 0, fee_a, fee_b, fee_usd from rebalancer.harvests
    union all
    select opened_at, mint, 0, 0, 0, 0, 0, 0, 0, 0 from rebalancer.positions
)
select ts, mint, kind, id,
       a + sum(ha) over w as a,
       b + sum(hb) over w as b,
       usd + sum(husd) over w as usd
from points
window w as (partition by mint order by ts, kind, id rows unbounded preceding);
commit;
