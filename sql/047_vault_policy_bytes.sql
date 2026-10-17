-- 047: a vault's policy size (owner, 2026-10-17). A Tangem card signs at most
-- 930 bytes per transaction, and every update of tang0's Raydium policies was
-- larger (1,043-1,050 bytes at the default 760 policy bytes): its owner could
-- neither approve 3ucNos nor revoke. A vault whose owner signs on a card packs
-- its rules into smaller policies (policy_bytes, from 400 to 760; null: 760),
-- each update one card-sized transaction. The bot's signer, the CLI, the owner
-- page's server and its decoder all compile with it
-- (chains/solana/vault/policy.mjs policyBytesOf).
--
-- No vault's value is set here: a new size changes the policies the bot
-- expects, so it is stored with the rows the owners approve
-- (ops/add_vault.py pool --policy-bytes). Additive and idempotent.
begin;
set local lock_timeout = '5s';
alter table rebalancer.vaults add column if not exists policy_bytes integer;
alter table rebalancer.vaults drop constraint if exists vaults_policy_bytes_range;
alter table rebalancer.vaults add constraint vaults_policy_bytes_range
  check (policy_bytes is null or policy_bytes between 400 and 760);
commit;
