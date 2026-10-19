-- 058 (owner, 2026-10-19): the payout follows R with no cap, and every PAYOUT
-- shows R and the share.
--
-- The profit rule (053) pays (1 - 1/R) x (1 - keep) of each harvest, where
-- R = fees / re-centre loss of the profile's closed bands over the last
-- governor_days. The cap (0.3 since 056) bound only above R 2.5. Owner: a pool
-- that earns R 3 should pay what it earns, so the cap goes (1.0 = no cap). The
-- keep (0.5) stays: half of the measured profit covers the between-band costs
-- the ledger misses and the noise of a 7-day R. The PAYOUT block and every
-- PAYOUT message now state R next to the share (lp/governor.py share_now,
-- db.governor_book, telegram_bridge.mjs).
begin;
set local lock_timeout = '5s';
update rebalancer.config set governor_cap = 1.0, updated_at = now()
 where name in ('sol-usdc', 'tang0', 'test0', 'sol-swing') and governor_cap <> 1.0;
commit;
