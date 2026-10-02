-- 2026-10-02: book the SOL that mu-usdc's first writes took from sol-usdc's sleeve.
--
-- Before sql/022 these writes booked no internal flow, so sol-usdc's equity
-- fell $8.28 with nothing to offset it. Measured on chain (LP wallet
-- 83HxMUUC..., lamports before and after each write):
--   4cRekks4... 13:18:59Z Jupiter swap MU->USDC   fee          73,674
--   ZpuqaXgZ... 13:19:09Z Meteora open            rent 67,503,040 + fee 10,000
--   221umvaZ... 13:19:14Z Meteora add liquidity   fee          10,000
--   total 67,596,714 lamports = 0.067596714 SOL, at 122.509 (sol-usdc's
--   snapshot of 13:19:44Z) = $8.281.
-- The rent sits in position 7Mj3TRxu... and returns to the wallet on close;
-- from now on that refund is booked by the bot itself (wallets.internal_flows).
--
-- Dated 13:19:18Z, just after mu-usdc's baseline (13:19:17.30Z), so its
-- since_start counts it. Runs once: a second run inserts nothing.
begin;
insert into rebalancer.capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, amounts, wallet_id, profile)
select '2026-10-02 13:19:18+00', k.kind, k.sol, 0, 8.281146, 122.509, null,
       'backfill 2026-10-02: rebalance and open by mu-usdc: 4cRekks4YpsNqAGykJF3fWcvv8UbfCV2e4iKsDgjcxxEzoRa9baF2UQXCf79fUt5ikMB9Lp4JaAF1anrWZ5AGMB7 '
       'ZpuqaXgZLCcj... (prefix; position 7Mj3TRxuGvD3yNxxrJmhRFT1UjP46TSj8gJKudEoJyBS) 221umvaZEjgyzvrMD1FHHCqdMNEv26SZ2ruLQpBs2x5cvhabyk9G3P5szi1YnwqtBey8Xng1xkdqbMLAXhuyFn7o',
       k.amounts::jsonb, 'sol-lp', k.profile
  from (values
    ('internal_out', 'sol-usdc', 0.067596714, '{"So11111111111111111111111111111111111111112": 0.067596714}'),
    ('internal_in',  'mu-usdc',  0,           '{"So11111111111111111111111111111111111111112": 0.067596714, "MUxEsUKSMACyw5fZf68wxf5FLnZVhtU9CwH8uNNGay1": 0}')
  ) k(kind, profile, sol, amounts)
 where not exists (select 1 from rebalancer.capital_flows where detail like 'backfill 2026-10-02:%');
commit;
