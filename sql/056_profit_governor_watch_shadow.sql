-- 056 (owner, 2026-10-19): three changes from the fee-to-loss audit of 10-19.
--
-- 1. The payout governor back to the profit rule (053), keep 0.5, cap 0.3.
--    The equity rule (054) compares dollar equity with dollar deposits grown
--    2%/30d, so it pays 30% only after SOL rises about 25%: a SOL price bet,
--    not a measure of the bot. The profit rule pays a share of what the closed
--    bands of the last 7 days earned above their re-centre loss. The band
--    ledger misses the swap and transaction cost between bands (wallet vs
--    ledger, 204 bands under theta 0.25: $20, about 7% of fees, about half of
--    the ledger's profit), so keep 0.5: half of the measured profit is paid,
--    half covers those costs. No floor: a week that earned nothing pays
--    nothing, and the LP never pays a dividend from its capital.
-- 2. venue_watch: pools whose fee counters the bot samples read-only, beside
--    the held pool and its same-pair candidates, so their fee-to-loss ratio
--    (fee growth per unit liquidity over sigma^2 sqrt(p)/4) can be measured
--    from the counters for 7 days before any move. They never join the venue
--    ranking or the board. On sol-usdc only (one sampler is enough):
--    Raydium SOL/USD1 AQAG, NVDAx/USDC 49iM (1.84 on 9 days), SOL/USDC 0.05%
--    2Qdh; Orca SOL/USDT Fwew, USDC/JitoSOL 5hWJ, SOL/USDC 0.01% 83v8.
-- 3. risk_profile.fee_loss_3h, shadow_in: the shadow pause (lp/shadow.py).
--    Every poll records the held pool's fees over its in-band loss for the
--    last 3 hours and the state a pause rule on it would be in (OUT below
--    1.0, IN above 1.5). Nothing is traded; the rule is judged on live hours.
begin;
set local lock_timeout = '5s';
update rebalancer.config
   set governor_mode = 'profit', governor_keep = 0.5, governor_cap = 0.3, updated_at = now()
 where name in ('sol-usdc', 'tang0', 'test0', 'sol-swing');
alter table rebalancer.config
  add column if not exists venue_watch text[] not null default '{}';
comment on column rebalancer.config.venue_watch is
  'dex:address of pools whose fee counters this profile samples read-only (fee_growth), outside the venue ranking (056)';
update rebalancer.config
   set venue_watch = array['raydium-clmm:AQAGYQsdU853WAKhXM79CgNdoyhrRwXvYHX6qrDyC1FS',
                           'raydium-clmm:49iMatQtoyabsYAQc8GafVq6aeBFVDxSRH44oiatyyw6',
                           'raydium-clmm:2QdhepnKRTLjjSqPL1PtKNwqrUkoLee5Gqs8bvZhRdMv',
                           'orca:FwewVm8u6tFPGewAyHmWAqad9hmF7mvqxK4mJ7iNqqGC',
                           'orca:5hWJUNTtEtKmKgDXpthJXXRRmJrz5vJ7uJzrUNVdrwLg',
                           'orca:83v8iPyZihDEjDdY8RdZddyZNyUtXngz69Lgo9Kt5d6d'],
       updated_at = now()
 where name = 'sol-usdc';
alter table rebalancer.risk_profile
  add column if not exists fee_loss_3h numeric,
  add column if not exists shadow_in boolean;
comment on column rebalancer.risk_profile.fee_loss_3h is
  'the held pool''s fees over its in-band loss over the last 3h, from its counters and the tape (lp/shadow.py, 056)';
comment on column rebalancer.risk_profile.shadow_in is
  'the shadow pause state at this poll: true IN, false OUT (out below 1.0, in above 1.5); nothing traded (056)';
commit;
