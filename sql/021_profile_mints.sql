-- Per-profile facts the multi-pool loop needs (2026-10-01).
--
-- mints: the two token mints of each profile's pool (A, then B). wallets.py
-- decides which tokens two profiles of one wallet share from these: a mint
-- used by one enabled profile is wholly its own, a mint several use is split
-- by claims. The profile's process writes its pool's mints at start and
-- after every repoint. NULL until then: wallets.py treats an unknown profile
-- as one that may use any mint (fail closed).
--
-- signer_env: environment variables the profile's own venue signer gets, for
-- a pool that needs an opt-in (djt-usdc: LPBOT_ORCA_ADAPTIVE=1, an Orca
-- adaptive-fee pool). Only keys on the allowlist below, string values only;
-- config.py checks the same list. A key cannot be smuggled in by a write to
-- this table: anything else is refused here and at startup.
--
-- wallet_settle: per wallet, the slot up to which every write is in the
-- claims, and the one write sent and not yet booked (wallets.py). While a
-- write is pending no other write of the wallet is sent, so the next
-- measurement books it exactly, even after a crash.
--
-- Additive only: the pre-020 bot never reads any of these.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config add column if not exists mints text[];
alter table rebalancer.config drop constraint if exists config_mints_pair;
alter table rebalancer.config add constraint config_mints_pair
  check (mints is null or (array_length(mints, 1) = 2 and mints[1] <> mints[2]));
alter table rebalancer.config add column if not exists signer_env jsonb;
alter table rebalancer.config drop constraint if exists config_signer_env_allowed;
alter table rebalancer.config add constraint config_signer_env_allowed
  check (signer_env is null or (jsonb_typeof(signer_env) = 'object'
                                and signer_env - array['LPBOT_ORCA_ADAPTIVE'] = '{}'::jsonb
                                and coalesce(signer_env ->> 'LPBOT_ORCA_ADAPTIVE', '1') in ('0', '1')));
create table if not exists rebalancer.wallet_settle (
  wallet_id  text primary key references rebalancer.wallets (id),
  slot       bigint not null default 0,      -- every write up to this slot (block on EVM) is booked
  pending    jsonb,                          -- {profile, command, mints, before, signatures}: sent, not booked
  updated_at timestamptz not null default now()
);
commit;
