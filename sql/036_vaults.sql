-- 036: vaults (owner, 2026-10-10: "any wallet that wants to connect with the
-- bot, the bot can operate"; "a non-special one ... pays a 10% tax on
-- usdc/usdt0 fees to my profit wallet").
--
-- A vault wallet keeps its money in a Squads smart account the owners
-- control with their own wallets; the bot acts on it only through policies
-- (chains/solana/vault/). Its rebalancer.wallets row names the vault as
-- `address` (where the money is) and the bot's fee payer key as `secret_env`.
--
--   vaults           one row per vault wallet: the smart account, its owners,
--                    the bot's policy key, the lookup table
--   vault_policies   per venue: the policies, and what the owners approved
--                    (pools, payout token accounts). The bot refuses to act
--                    when the chain's policies differ from these.
--   config.special   an instance of the operator's own (no fee share)
--   config.fee_bps   share of the payout-token fees sent to fee_wallet at each
--                    harvest; only a vault instance pays one
--   payouts.kind 'fee'  that share, paid
--
-- Additive and idempotent.
begin;
set local lock_timeout = '5s';

create table if not exists rebalancer.vaults (
  wallet_id      text primary key references rebalancer.wallets (id),
  kind           text not null check (kind in ('squads')),
  settings       text not null unique check (settings ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$'),
  owners         text[] not null check (cardinality(owners) between 1 and 10),
  threshold      smallint not null check (threshold >= 1),
  policy_key_env text not null check (policy_key_env ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  lookup         text check (lookup ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$'),
  created_at     timestamptz not null default now(),
  check (threshold <= cardinality(owners))
);

create table if not exists rebalancer.vault_policies (
  wallet_id   text not null references rebalancer.vaults (wallet_id),
  venue       text not null,
  policies    text[] not null check (cardinality(policies) >= 1),
  pools       text[] not null check (cardinality(pools) >= 1),
  payouts     text[] not null default '{}',
  updated_at  timestamptz not null default now(),
  primary key (wallet_id, venue)
);

alter table rebalancer.config add column if not exists special boolean not null default true;
alter table rebalancer.config add column if not exists fee_bps integer not null default 0;
alter table rebalancer.config add column if not exists fee_wallet text;
alter table rebalancer.config drop constraint if exists config_fee_sane;
alter table rebalancer.config add constraint config_fee_sane check (
  fee_bps between 0 and 5000
  and (fee_bps = 0 or fee_wallet is not null)
  and (not special or fee_bps = 0));

alter table rebalancer.payouts drop constraint if exists payouts_kind_check;
alter table rebalancer.payouts add constraint payouts_kind_check
  check (kind in ('paid', 'reinvested', 'gas', 'owed', 'uncertain', 'settled', 'fee'));
commit;
