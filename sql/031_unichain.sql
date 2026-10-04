-- 031: Unichain wallets (2026-10-04). The first profile is Uniswap v3
-- USDC/HYPE 0.3% (pool 0x5d3e7f5da38fbf476e8b36e3b90d02fc4c1a08c3). A wallet
-- row may now name chain 'unichain', with a 0x address like Base's. Nothing
-- else changes: payout addresses are 0x already (config_payout_sane), and the
-- profile row is the operator's (ops/UNICHAIN_SETUP.md).
--
-- Additive and idempotent: each check is dropped and added again in one
-- transaction, and every row allowed before stays allowed.
begin;
set local lock_timeout = '5s';
alter table rebalancer.wallets drop constraint if exists wallets_chain_check;
alter table rebalancer.wallets add constraint wallets_chain_check
  check (chain in ('solana', 'base', 'unichain'));
alter table rebalancer.wallets drop constraint if exists wallets_check;
alter table rebalancer.wallets add constraint wallets_check
  check ((chain = 'solana' and address ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$')
      or (chain = 'base' and address ~ '^0x[0-9a-fA-F]{40}$')
      or (chain = 'unichain' and address ~ '^0x[0-9a-fA-F]{40}$'));
commit;
