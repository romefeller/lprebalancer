-- 039: the owner's page at romefeller.app/bot (2026-10-12: Tangem, Ledger and
-- Phantom; "the same stats as the Telegram message"; owner exit and revoke).
--
--   feed_latest      the newest feed row of each kind a vault profile wrote
--                    (book: the latest row carrying the book, DAILY, REVOKED,
--                    OWNER_EXIT): the page renders it with the Telegram
--                    bridge's formatter. One row per (profile, kind), replaced.
--   owner_requests   what an owner asked the bot to do, signed on the page:
--                    exit = close the positions into the vault and stop. The
--                    loop reads the pending one each poll and marks it done.
--   onboard_deposits every deposit into an onboarded vault, verified on chain
--                    by the server (one row per transaction). The profile's
--                    max_usd follows their sum (onboard/store.py cap).
-- Additive and idempotent.
begin;
set local lock_timeout = '5s';

create table if not exists rebalancer.feed_latest (
  profile     text not null,
  kind        text not null,
  row         jsonb not null,
  updated_at  timestamptz not null default now(),
  primary key (profile, kind)
);

create table if not exists rebalancer.owner_requests (
  id          bigserial primary key,
  profile     text not null,
  kind        text not null check (kind in ('exit')),
  nonce       text not null unique,
  signature   text not null,
  created_at  timestamptz not null default now(),
  done_at     timestamptz,
  outcome     text
);
-- one pending request per profile and kind
create unique index if not exists owner_requests_pending on rebalancer.owner_requests (profile, kind) where done_at is null;

create table if not exists rebalancer.onboard_deposits (
  id          bigserial primary key,
  onboard_id  bigint not null references rebalancer.onboard (id),
  signature   text not null unique,
  token       text not null,
  amount      numeric not null check (amount > 0),
  usd         numeric,
  created_at  timestamptz not null default now()
);
create index if not exists onboard_deposits_request on rebalancer.onboard_deposits (onboard_id);
commit;
