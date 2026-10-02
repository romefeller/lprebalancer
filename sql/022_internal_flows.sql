-- 022: native SOL that one profile's write moves out of another profile's sleeve.
--
-- On a shared wallet the native token (SOL) belongs to the profile whose pool
-- holds it (wallets.sole_owner). Another profile's write pays its rent and its
-- fees from that SOL, and a close refunds the rent to it. 2026-10-02: the first
-- MU/USDC open locked 0.0675 SOL of Meteora rent; sol-usdc's book showed it as
-- an $8.28 loss and mu-usdc's book counted it only on some polls.
--
-- Each such write is now booked as two rows in the same transaction as its
-- claims (wallets.book): 'internal_out' on the profile that gave the SOL and
-- 'internal_in' on the profile that took it (a refund swaps the two). Both rows
-- carry the same positive amount. They have no signature (the write's
-- signatures are in `detail`), because one write makes two rows.
alter table rebalancer.capital_flows drop constraint if exists capital_flows_kind_check;
alter table rebalancer.capital_flows add constraint capital_flows_kind_check
  check (kind in ('baseline', 'deposit', 'withdrawal', 'internal_in', 'internal_out'));
