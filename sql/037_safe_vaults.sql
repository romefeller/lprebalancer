-- 037: Safe vaults on EVM chains (owner, 2026-10-10: "You will have to do the same wallet
-- connect with POL pool").
--
-- A 'safe' vault keeps its money in a Safe its owners control; the bot acts on it only
-- through a Zodiac Roles module (chains/evm/vault/). Its row differs from a 'squads' one:
--   settings        the Safe's address
--   policy_key_env  the bot's key, the same one as the wallet's secret_env (one key sends
--                   and pays on an EVM chain)
--   lookup          null
--   vault_policies.policies  the Roles module; pools and payouts are addresses
--
-- Additive and idempotent.
begin;
set local lock_timeout = '5s';

alter table rebalancer.vaults drop constraint if exists vaults_kind_check;
alter table rebalancer.vaults add constraint vaults_kind_check check (kind in ('squads', 'safe'));
alter table rebalancer.vaults drop constraint if exists vaults_settings_check;
alter table rebalancer.vaults add constraint vaults_settings_check check (
  (kind = 'squads' and settings ~ '^[1-9A-HJ-NP-Za-km-z]{32,44}$')
  or (kind = 'safe' and settings ~ '^0x[0-9a-fA-F]{40}$'));
alter table rebalancer.vaults drop constraint if exists vaults_lookup_kind_check;
alter table rebalancer.vaults add constraint vaults_lookup_kind_check check (kind = 'squads' or lookup is null);
commit;
