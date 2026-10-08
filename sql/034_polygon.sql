-- 034: Polygon wallets (2026-10-08). The first profile is Uniswap v3
-- WPOL/USDT0 0.05% (pool 0x9b08288c3be4f62bbf8d1c20ac9c5e6f9467d8b7). A wallet
-- row may now name chain 'polygon', with a 0x address like Base's and
-- Unichain's. Nothing else changes: payout addresses are 0x already
-- (config_payout_sane), and the profile row is the operator's
-- (ops/POLYGON_SETUP.md).
--
-- Additive and idempotent: each check is dropped and added again in one
-- transaction, and every row allowed before stays allowed.
begin;
set local lock_timeout = '5s';
alter table rebalancer.wallets drop constraint if exists wallets_chain_check;
alter table rebalancer.wallets add constraint wallets_chain_check
  check (chain in ('solana', 'base', 'unichain', 'polygon'));
alter table rebalancer.wallets drop constraint if exists wallets_check;
alter table rebalancer.wallets add constraint wallets_check
  check ((chain = 'solana' and address ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$')
      or (chain = 'base' and address ~ '^0x[0-9a-fA-F]{40}$')
      or (chain = 'unichain' and address ~ '^0x[0-9a-fA-F]{40}$')
      or (chain = 'polygon' and address ~ '^0x[0-9a-fA-F]{40}$'));
commit;
