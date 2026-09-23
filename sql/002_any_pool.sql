-- Make the config describe any whirlpool, not SOL/USDC with the names changed.
-- Idempotent: every statement checks before it acts.
--
--   reserve_a      -> gas_reserve_sol   it was always the native SOL kept back
--                                       for transaction fees, whatever the pool
--                                       holds. Calling it "token A" is a trap on
--                                       any pool whose token A is not SOL.
--   decimals_a/b   dropped              the signer reads them from the pool.
--   swap_cost_bps  added                the simulator's round-trip swap cost was
--                                       a constant in engine.py.
--   side_cap_fraction added             each token's deposit cap, as a fraction
--                                       of capital, was a literal 0.55 in the
--                                       loop.

do $$
begin
  if exists (select 1 from information_schema.columns
             where table_schema = 'rebalancer' and table_name = 'config'
               and column_name = 'reserve_a') then
    alter table rebalancer.config rename column reserve_a to gas_reserve_sol;
  end if;
end $$;

alter table rebalancer.config
  drop column if exists decimals_a,
  drop column if exists decimals_b;

alter table rebalancer.config
  add column if not exists swap_cost_bps     integer      not null default 10,
  add column if not exists side_cap_fraction numeric(4,3) not null default 0.550;

alter table rebalancer.config drop constraint if exists config_sane;
alter table rebalancer.config add constraint config_sane check (
  capital_usd > 0 and max_usd >= capital_usd
  and gas_reserve_sol >= 0
  and poll_seconds between 30 and 86400
  and min_rebalance_gap_seconds >= 0
  and max_rebalances_per_day between 1 and 100
  and reopt_min_gain >= 0
  and slippage_bps between 1 and 1000
  and swap_cost_bps between 0 and 500
  and side_cap_fraction between 0.5 and 1.0
  and array_length(bands, 1) >= 1
);
