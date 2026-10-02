-- Re-point profile base-weth-usdc from the Aerodrome Slipstream WETH/USDC pool of the
-- "initial" deployment (0xb2cc224c…, tickSpacing 100) to the WETH/USDC pool of the
-- "gauges-v3" deployment (0x3fe04a59…, tickSpacing 50). Evidence: evm/ADDRESSES.md.
--
--   psql -d rebalancer -v ON_ERROR_STOP=1 -f ops/repoint_base_cl3.sql
--
-- Run it only after the signer and dexes.py that know the "gauges-v3" deployment are live:
-- the old code refuses the new pool (every read fails, the profile stops working).
-- Restart lp-bot@base-weth-usdc after the commit: config.py reads the row at start.
--
-- One transaction. Idempotent: a second run finds the new pool and changes nothing.
-- It refuses (raises, so nothing commits) when:
--   - the profile has an open position (positions.closed_at is null): close it first;
--   - a write of wallet base-lp is in flight (its advisory lock is held, or
--     wallet_settle.pending is set);
--   - the row is not the WETH/USDC Aerodrome profile this script expects.
--
-- Columns that depend on the pool and stay as they are (both pools are WETH/USDC, token0
-- WETH, token1 USDC): pair_label 'WETH/USDC', token_a 'WETH', token_b 'USDC',
-- mints {weth, usdc} (token0, token1), deposit_mint WETH, payout_mint USDC,
-- dex / dexes / execute_dexes 'aerodrome-slipstream'. The script checks each of them.
-- Rows keyed by pool (tape5, fee_growth, pool_stats, risk_profile, touch_forecasts) are
-- not moved: they describe the old pool, and the new pool starts its own rows.
begin;
set local lock_timeout = '5s';

do $$
declare
  old_pool constant text := '0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59';
  new_pool constant text := '0x3fe04a59ebd38cf06080a6f60a98d124eb59392a';
  weth constant text := '0x4200000000000000000000000000000000000006';
  usdc constant text := '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913';
  c rebalancer.config%rowtype;
  open_n int;
begin
  -- the row first, locked: no other session changes it until this transaction ends
  select * into c from rebalancer.config where name = 'base-weth-usdc' for update;
  if not found then
    raise exception 'refused: no profile named base-weth-usdc';
  end if;

  -- the same lock wallets.py takes around every write of the wallet (namespace 0x4C50 'LP')
  if not pg_try_advisory_xact_lock(19536, hashtext(c.wallet_id)) then
    raise exception 'refused: a write of wallet % is in flight (advisory lock held); try again', c.wallet_id;
  end if;
  if exists (select 1 from rebalancer.wallet_settle where wallet_id = c.wallet_id and pending is not null) then
    raise exception 'refused: wallet % has a sent, unbooked write (wallet_settle.pending)', c.wallet_id;
  end if;

  select count(*) into open_n from rebalancer.positions
   where config_name = 'base-weth-usdc' and closed_at is null;
  if open_n > 0 then
    raise exception 'refused: base-weth-usdc holds % open position(s); close before the repoint', open_n;
  end if;

  if c.wallet_id is distinct from 'base-lp'
     or c.dex is distinct from 'aerodrome-slipstream'
     or c.dexes is distinct from array['aerodrome-slipstream']
     or c.execute_dexes is distinct from array['aerodrome-slipstream']
     or c.pair_label is distinct from 'WETH/USDC'
     or c.token_a is distinct from 'WETH'
     or c.token_b is distinct from 'USDC'
     or c.mints is distinct from array[weth, usdc]
     or lower(c.deposit_mint) is distinct from weth
     or lower(c.payout_mint) is distinct from usdc then
    raise exception 'refused: base-weth-usdc is not the WETH/USDC aerodrome profile this script expects (wallet %, dex %, pair %, mints %, deposit %, payout %)',
      c.wallet_id, c.dex, c.pair_label, c.mints, c.deposit_mint, c.payout_mint;
  end if;

  if lower(c.pool) = new_pool then
    raise notice 'base-weth-usdc already on %: nothing to do', new_pool;
    return;
  end if;
  if lower(c.pool) is distinct from old_pool then
    raise exception 'refused: base-weth-usdc is on %, neither % nor %', c.pool, old_pool, new_pool;
  end if;

  update rebalancer.config set pool = new_pool, updated_at = now() where name = 'base-weth-usdc';
  raise notice 'base-weth-usdc moved from % to %', old_pool, new_pool;
end
$$;

-- what the transaction leaves behind
select name, pool, dex, pair_label, token_a, token_b, mints, deposit_mint, payout_mint, enabled, updated_at
  from rebalancer.config where name = 'base-weth-usdc';

commit;
