-- Capital flows and audits (2026-09-28).
--
-- capital_flows: what the book started with and every deposit or withdrawal
-- since, so profit is value now + payouts - baseline - deposits + withdrawals,
-- not "equity now - the first snapshot". The baseline is reconstructed from
-- the chain: the LP wallet's holdings before the bot's first transaction.
--
-- audits: every reconciliation of the ledger against the chain, one row per
-- check per run (ok, warn or fail), kept 30 days. audit_state holds cursors
-- (the last wallet signature classified) so a run reads only what is new.
begin;
set local lock_timeout = '5s';

create table if not exists rebalancer.capital_flows (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null,
  kind        text not null check (kind in ('baseline', 'deposit', 'withdrawal')),
  sol         numeric(38,18) not null default 0,     -- SOL (or SOL-equivalent) moved
  usdc        numeric(38,18) not null default 0,
  usd         numeric(18,6) not null,                -- its dollar value at ts
  price       double precision,                      -- SOL in USD at ts
  signature   text unique,                           -- the transaction, when there is one
  detail      text
);
create unique index if not exists capital_flows_one_baseline on rebalancer.capital_flows (kind) where kind = 'baseline';

create table if not exists rebalancer.audits (
  id       bigint generated always as identity primary key,
  ts       timestamptz not null default now(),
  run_id   text not null,
  check_name text not null,
  status   text not null check (status in ('ok', 'warn', 'fail')),
  detail   jsonb
);
create index if not exists audits_ts on rebalancer.audits (ts desc);
create index if not exists audits_check on rebalancer.audits (check_name, ts desc);

create table if not exists rebalancer.audit_state (
  key    text primary key,
  value  text not null,
  ts     timestamptz not null default now()
);

-- an owed payout later carried by a paid one is 'settled', not paid twice
alter table rebalancer.payouts drop constraint if exists payouts_kind_check;
alter table rebalancer.payouts add constraint payouts_kind_check
  check (kind in ('paid', 'reinvested', 'gas', 'owed', 'uncertain', 'settled'));
commit;
