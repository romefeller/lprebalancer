-- 060 (owner, 2026-10-20): the payout is 1 - 1/R, and tang0 swaps in a deep pool.
--
-- 1. governor_keep 0: each harvest pays (1 - 1/R) of its fees, R = fees /
--    re-centre loss of the closed bands of the last 7 days. The owner rejected
--    any extra margin; the book's erosion line (02414d2) shows whether the
--    unmeasured costs erode the capital.
-- 2. config.swap_pool: the pool a vault swaps in when not the held one. tang0's
--    vault has no router and swapped about half its equity inside 8sLb at each
--    re-centre, moving it 30-50 bp (audit 2026-10-20, three swaps read on
--    chain: each filled at its 5-minute bar's worst price), about $6.7/day and
--    more than its bands earned. Raydium 3ucN (SOL/USDC 0.04%), about 400 times
--    deeper, is on tang0's vault policy already (tang0 swapped there on 10-17).
--    The band stays in 8sLb; only the swap moves.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config add column if not exists swap_pool text;
comment on column rebalancer.config.swap_pool is
  'the pool a vault swaps in when not the held one; used only if the vault approves it (lp/swaps.vault_swap_pool, 060)';
update rebalancer.config set governor_keep = 0, updated_at = now() where name in ('sol-usdc', 'tang0', 'test0');
update rebalancer.config set swap_pool = '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv', updated_at = now()
 where name = 'tang0';
commit;
