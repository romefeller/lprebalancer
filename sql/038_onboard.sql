-- 038: self-serve onboarding (owner, 2026-10-11: "romefeller.app/bot ... one Sign
-- button"; "the owner pays the setup gas automatically, as part of the flow").
-- One row per signed request to create a vault and its profile
-- (onboard/server.mjs, onboard/store.py).
--
--   intent, signature   the message the owner's wallet signed and its signature:
--                       the record of what the owner asked for
--   gas, payer          the native coin the owner pays the bot's key (payer) for
--                       the setup and the running gas, before anything is spent
--   payment             the owner's payment transaction (signature or hash),
--                       verified on chain; one payment serves one request only
--   deposit             what goes into the vault after the setup
--   connected           what the vault's connect printed, kept when a later
--                       step failed, so a resume never connects twice
--   state  signed      signed, not paid yet (the name is held until the intent expires)
--          connecting  paid; the bot is setting the vault up
--          stuck       paid; a step failed. Resumable with the same payment
--          registered  the vault and the profile exist; waiting for the deposit
--          started     the profile's service runs
--          failed      never paid and expired, or given up by the operator
-- Additive and idempotent.
begin;
set local lock_timeout = '5s';

create table if not exists rebalancer.onboard (
  id             bigserial primary key,
  chain          text not null check (chain in ('solana', 'polygon')),
  owner          text not null,
  name           text not null check (name ~ '^[a-z][a-z0-9-]{1,30}[a-z0-9]$'),
  wallet_id      text not null,
  token          text not null,
  amount         numeric not null check (amount > 0),
  deposit        numeric not null check (deposit > 0),
  gas            numeric not null check (gas > 0),
  payer          text not null,
  profit_wallet  text not null,
  special        boolean not null,
  fee_bps        integer not null check (fee_bps between 0 and 5000),
  fee_wallet     text,
  intent         text not null,
  signature      text not null,
  nonce          text not null unique,
  expires_at     timestamptz not null,
  payment        text unique,
  state          text not null default 'signed'
                 check (state in ('signed', 'connecting', 'stuck', 'registered', 'started', 'failed')),
  vault          text,
  connected      jsonb,
  error          text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  check (state in ('signed', 'failed') or payment is not null)
);
-- one live request per name; a failed one gives its name back
create unique index if not exists onboard_name_live on rebalancer.onboard (name) where state <> 'failed';
create index if not exists onboard_owner on rebalancer.onboard (lower(owner));
commit;
