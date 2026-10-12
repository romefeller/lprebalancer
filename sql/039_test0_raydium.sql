-- 039: test0 moves from Meteora DLMM to Raydium CLMM, the SOL/USDC pool sol-usdc
-- runs on (owner, 2026-10-12: "move test0 to raydium").
--
-- test0's vault (wallet test0-vault, settings 5wcMGHQs…, vault 37mZwrAU…) was
-- connected on 2026-10-11 with Meteora policies only (seeds 1-3). Raydium is
-- staged on the same vault, never a second one: its two policies are the
-- vault's next seeds, 4 and 5 (chains/solana/vault/cli.mjs add-venue). They
-- exist on chain only once the owner signs Approve bot rules at /bot, which
-- also re-approves the Meteora policies (the WSOL pin, security fix of
-- 2026-10-12). Until then every signer of the profile refuses and the loop
-- holds (lp/loop.py awaiting_owners). The move itself is the operator's
-- MIGRATE file ("raydium-clmm 8sLbNZoA…"): the loop runs it once the
-- Raydium policies read on chain (lp/board.py move_waits), closing on
-- Meteora into the vault and opening on Raydium.
--
-- Rows written only where test0's vault is as connected: elsewhere (a test
-- database, another server) this does nothing. Additive and idempotent.
begin;
set local lock_timeout = '5s';

insert into rebalancer.vault_policies (wallet_id, venue, policies, pools, payouts)
select 'test0-vault', 'raydium-clmm',
       array['G5ExKR7yfzj3WpDNaHpwhDscceN7sZtiTNfj4ZJvxejx', 'F2u7B8GDtQSdWjj3XZutJVSZRZ61jWBL4p5f9R79ERcD'],
       array['8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'],
       p.payouts
  from rebalancer.vault_policies p
  join rebalancer.vaults v on v.wallet_id = p.wallet_id
 where p.wallet_id = 'test0-vault' and p.venue = 'meteora-dlmm'
   and v.settings = '5wcMGHQsaMe4hkTwoXVRBmt4at4VGCUk3A4NB9Yu4hcS'
   and p.policies = array['86MJFjsqMcuQZqWXxydjeRaRjwFE8oyeWoLBygS9dPL2', 'A6ohPdAvs2YUc4BAHP5Vv6qHWHNex95A3ojCQDHJmd2R',
                          'A9zDtyaW5XgvoR3H2SKBz7wHmTcShsGAq5dsMy5jezsj']
on conflict (wallet_id, venue) do nothing;

-- the profile may open on Raydium (the MIGRATE gate needs it armed); it stays
-- pinned to its pool, which the move repoints
update rebalancer.config
   set execute_dexes = array['meteora-dlmm', 'raydium-clmm'],
       dexes = array['meteora-dlmm', 'raydium-clmm'],
       updated_at = now()
 where name = 'test0' and wallet_id = 'test0-vault'
   and exists (select 1 from rebalancer.vault_policies
                where wallet_id = 'test0-vault' and venue = 'raydium-clmm')
   and not ('raydium-clmm' = any(execute_dexes));
commit;
