-- The fee split: payout-token fees to the profit wallet, the rest reinvested.
--
--   payout_enabled   master switch; false leaves every fee in the LP wallet
--   profit_wallet    where payout-token fees go (the owner's own address)
--   payout_mint      which token's fees are paid out (USDC on SOL/USDC). A pool
--                    that does not hold it pays nothing out: every fee is
--                    reinvested and the book says so
--
-- Native SOL fees refill gas first when the LP wallet's SOL is under
-- gas_reserve_sol, up to the reserve only; while gas is low, the payout-token
-- fees of that harvest are reinvested instead of paid.
--
-- payouts: one row per fee amount and its fate. Reinvested dollars raise the
-- sizing base (capital_usd + reinvested), so the position grows by them.
--
-- Idempotent.
alter table rebalancer.config
  add column if not exists payout_enabled boolean not null default false,
  add column if not exists profit_wallet  text,
  add column if not exists payout_mint    text;

alter table rebalancer.config drop constraint if exists config_payout_sane;
alter table rebalancer.config add constraint config_payout_sane check (
  (profit_wallet is null or profit_wallet ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$')
  and (payout_mint is null or payout_mint ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$')
  and (not payout_enabled or (profit_wallet is not null and payout_mint is not null))
);

create table if not exists rebalancer.payouts (
  id           bigint generated always as identity primary key,
  ts           timestamptz not null default now(),
  config_name  text,
  position     text,
  token_mint   text not null,
  symbol       text,
  amount       numeric(38,18) not null,
  usd          numeric(18,6),
  kind         text not null check (kind in ('paid', 'reinvested', 'gas', 'owed')),
  to_address   text,
  signature    text,
  detail       text
);
create index if not exists payouts_ts on rebalancer.payouts (ts desc);
create index if not exists payouts_kind on rebalancer.payouts (config_name, kind);
