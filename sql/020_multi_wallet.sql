-- Multi-wallet, multi-pool (2026-10-01).
--
-- One process per profile (systemd lp-bot@<profile>). Several profiles may
-- share one wallet: each owns its pool's base token (the deposit that routes
-- to it), and a token two profiles both use (USDC) is split by claims: each
-- profile's claim is the amount its own transactions left in the wallet, and
-- the wallet's residual owner holds the rest (a fresh USDC deposit, dust).
-- Every token-moving transaction runs under the wallet's advisory lock, so
-- the claim deltas are measured with no other profile moving tokens.
--
-- Additive only. The running pre-020 bot keeps working against this schema:
-- every new column is nullable or defaulted, and `active` keeps its meaning
-- (the profile a process without LPBOT_PROFILE runs).
begin;
set local lock_timeout = '5s';

-- Wallets. The key never enters the database: `secret_env` names the
-- environment variable that holds the PATH of the key file.
create table if not exists rebalancer.wallets (
  id          text primary key check (id ~ '^[a-z0-9][a-z0-9-]{1,40}$'),
  chain       text not null check (chain in ('solana', 'base')),
  address     text not null unique,
  secret_env  text not null check (secret_env ~ '^[A-Z][A-Z0-9_]{2,63}$'),
  label       text,
  created_at  timestamptz not null default now(),
  check ((chain = 'solana' and address ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$')
      or (chain = 'base' and address ~ '^0x[0-9a-fA-F]{40}$'))
);

alter table rebalancer.config add column if not exists wallet_id text references rebalancer.wallets (id);
-- A process runs only an enabled profile; `active` stays the pre-020 default.
alter table rebalancer.config add column if not exists enabled boolean not null default false;
-- The token whose arrival in the wallet this profile deploys (the pool's base
-- token). One enabled profile per (wallet, deposit mint).
alter table rebalancer.config add column if not exists deposit_mint text;
-- Holds the residual of every token several profiles of its wallet share.
-- At most one per wallet.
alter table rebalancer.config add column if not exists residual_owner boolean not null default false;
-- Below this deployable value a profile with no position stays dormant.
alter table rebalancer.config add column if not exists min_deploy_usd numeric(12,4) not null default 5.0
  check (min_deploy_usd >= 0);
create unique index if not exists config_one_deposit_mint
  on rebalancer.config (wallet_id, deposit_mint) where enabled and deposit_mint is not null;
create unique index if not exists config_one_residual_owner
  on rebalancer.config (wallet_id) where residual_owner;

-- Payout addresses per chain: base58 on Solana, 0x-hex on Base.
alter table rebalancer.config drop constraint if exists config_payout_sane;
alter table rebalancer.config add constraint config_payout_sane check (
  (profit_wallet is null or profit_wallet ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$' or profit_wallet ~ '^0x[0-9a-fA-F]{40}$')
  and (payout_mint is null or payout_mint ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$' or payout_mint ~ '^0x[0-9a-fA-F]{40}$')
  and (not payout_enabled or (profit_wallet is not null and payout_mint is not null)));

-- Claims on tokens a wallet's profiles share. Amount in human units.
create table if not exists rebalancer.wallet_claims (
  wallet_id   text not null references rebalancer.wallets (id),
  profile     text not null references rebalancer.config (name),
  mint        text not null,
  amount      numeric(38,18) not null default 0 check (amount >= 0),
  updated_at  timestamptz not null default now(),
  primary key (wallet_id, profile, mint)
);

-- Cross-process rate gates (GeckoTerminal, Jupiter): one row per gate.
create table if not exists rebalancer.rate_gate (
  name      text primary key,
  next_at   double precision not null default 0      -- epoch seconds: no call before this
);

-- Who wrote each row. NULL on rows from before 020.
alter table rebalancer.events        add column if not exists profile text;
alter table rebalancer.audits        add column if not exists profile text;
alter table rebalancer.audits        add column if not exists wallet_id text;
alter table rebalancer.capital_flows add column if not exists wallet_id text;
alter table rebalancer.capital_flows add column if not exists profile text;
-- Any token, not only SOL and USDC: {mint: human amount}.
alter table rebalancer.capital_flows add column if not exists amounts jsonb;
create index if not exists events_profile_ts on rebalancer.events (profile, ts desc);

-- One baseline per (wallet, profile), not one in all.
drop index if exists rebalancer.capital_flows_one_baseline;
create unique index if not exists capital_flows_one_baseline_per_book
  on rebalancer.capital_flows (coalesce(wallet_id, ''), coalesce(profile, ''), kind) where kind = 'baseline';

commit;
