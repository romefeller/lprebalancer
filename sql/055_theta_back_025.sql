-- 055 (owner, 2026-10-19): the touch threshold back to 0.25 on every SOL bot.
--
-- sql/046 (10-17) set regime_threshold 0.60 for the 3ucN plan; the 3ucN move was
-- reverted the same day (sql/050) but 0.60 stayed. The mode is a label on the width
-- choice (calm.regime_view: CALM = narrowest width passes P(touch 2h) <= theta x
-- liquidity factor, HOT = width > +-2%), so at 0.60 CALM was 93-100% and HOT 0% from
-- 10-18: the every-HOT pause (sql/053) could not fire (last hot_paused 10-15 06:12Z).
-- Replay, 14 days, 8sLb fee counter, per $1k/day (audited): theta 0.25 net +1.65,
-- 0.60 +0.60 (fees 21.07 vs 13.95). 6.5 months: HOT-like hours net -0.38/h vs +0.09.
-- Open bands are not moved: a widen needs 3 width steps; the next exit reopens at the
-- new choice. REGIME_THETA_MAX stays 0.96 (0.25 x factor <= 0.3125).
begin;
set local lock_timeout = '5s';
update rebalancer.config set regime_threshold = 0.25, updated_at = now()
 where name in ('sol-usdc', 'tang0', 'test0', 'sol-swing') and regime_threshold <> 0.25;
commit;
