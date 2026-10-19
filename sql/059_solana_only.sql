-- 059 (owner, 2026-10-19): "destroy every unneeded code". The bot is Solana
-- only, on Raydium and Orca, key or Squads custody. Gone with their code:
--   * the EVM chains (Base/Aerodrome, Unichain and Polygon/Uniswap v3, Safe
--     vaults): their three profiles and three wallet rows. Base and Unichain
--     wallets are empty; the Polygon wallet holds 2.07 POL (about $0.40); the
--     key files stay on disk outside the repository.
--   * the stock "swing" (DJT/MU/MSFTx, sol-swing): the swing table and its
--     four profiles. sol-lp2 (FogqBWLC) keeps its wallet row: it holds $5.68.
--   * the equity payout-governor mode (054): its columns. governor_mode goes
--     too: the profit rule (053, 056, 058) is the only one.
--   * the EVM gas wrap/refill settings (035, 041): native_keep, gas_refill_to.
-- The positions, payouts, snapshots and capital_flows history of every removed
-- profile stays (no foreign key binds it): stats.py --all no longer lists
-- them, the audit can still read them.
begin;
set local lock_timeout = '5s';
delete from rebalancer.wallet_claims
 where profile in ('base-weth-usdc', 'poly-wpol-usdt', 'uni-hype-usdc', 'sol-swing', 'djt-usdc', 'mu-usdc', 'msftx-usdc')
    or wallet_id in ('base-lp', 'poly-lp', 'uni-lp');
delete from rebalancer.wallet_settle where wallet_id in ('base-lp', 'poly-lp', 'uni-lp');
drop table if exists rebalancer.swing;
delete from rebalancer.config
 where name in ('base-weth-usdc', 'poly-wpol-usdt', 'uni-hype-usdc', 'sol-swing', 'djt-usdc', 'mu-usdc', 'msftx-usdc');
delete from rebalancer.wallets where id in ('base-lp', 'poly-lp', 'uni-lp');
alter table rebalancer.config
  drop column if exists governor_mode,
  drop column if exists governor_floor,
  drop column if exists governor_high,
  drop column if exists governor_enter_pct,
  drop column if exists governor_exit_pct,
  drop column if exists governor_growth_pct,
  drop column if exists governor_paying,
  drop column if exists governor_switched_at,
  drop column if exists native_keep,
  drop column if exists gas_refill_to;
commit;
