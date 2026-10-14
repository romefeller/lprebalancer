-- 041: gas refill on EVM chains (2026-10-14). On Polygon the pool pays its fees
-- in WPOL, never in the native POL that pays the gas: the gas ran down under
-- gas_reserve_sol (2 POL), every add and open was refused, and $435 sat idle
-- (poly-wpol-usdt, 00:15-00:37Z). The bot now refills its gas from its own
-- wallet: it unwraps WPOL (and first buys it with USDT0 when WPOL is short)
-- up to gas_refill_to, and no further (lp/capital.py refill_gas, the signer's
-- `refill`).
--
--   config.gas_refill_to  native units (POL on Polygon, ETH on Base and
--                         Unichain). NULL or 0: never refill. Above the
--                         reserve, and not above native_keep when that is
--                         on (the wrap would take the refill back).
--   payouts.kind 'gas_refill'  native gas unwrapped from the book's own wrapped
--                         token, where the book does not count the native
--                         coin (Uniswap v3: POL is gas only). since_start and
--                         the DAILY line add it to value as they add payouts.
--                         Where the native coin is in the book (Aerodrome:
--                         ETH counts as WETH) a refill is booked as 'gas'.
--
-- poly-wpol-usdt: the reserve is 1 POL. Measured on chain 2026-10-14 at
-- ~280 gwei: harvest 0.06 POL, close 0.09, swap 0.04, mint 0.14; a whole
-- re-centre ~0.35 POL. 1 POL covers a harvest and a close at the 1500 gwei
-- cap (~460k gas, 0.69 POL), so the bot can always leave the pool; the swap
-- before an open or an add has its own headroom of 0.3 POL
-- (capital.OPEN_RENT_HEADROOM). The refill target is 3 POL (about $0.30): the
-- gas is refilled when it falls under 2 POL, halfway, so one unwrap
-- (~36k gas) pays for several re-centres. This replaces the operator's
-- stopgap of 2026-10-14 00:38Z (gas_reserve_sol set to 1 by hand, from 2).
--
-- Additive and idempotent.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config add column if not exists gas_refill_to numeric(18, 9);
alter table rebalancer.config drop constraint if exists config_gas_refill_sane;
alter table rebalancer.config add constraint config_gas_refill_sane
  check (gas_refill_to is null or gas_refill_to = 0
         or (gas_refill_to > gas_reserve_sol
             and (native_keep is null or native_keep = 0 or gas_refill_to <= native_keep)));
alter table rebalancer.payouts drop constraint if exists payouts_kind_check;
alter table rebalancer.payouts add constraint payouts_kind_check
  check (kind in ('paid', 'reinvested', 'gas', 'owed', 'uncertain', 'settled', 'fee', 'gas_refill'));
update rebalancer.config set gas_reserve_sol = 1, gas_refill_to = 3 where name = 'poly-wpol-usdt';
commit;
