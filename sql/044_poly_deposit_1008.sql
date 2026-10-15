-- 044: the native POL deposit of 2026-10-08 16:47Z on poly-wpol-usdt, booked
-- (owner, 2026-10-15). 576.3723 POL arrived in the Polygon LP wallet, the bot
-- wrapped it (tx 0x5b2ea694…) and added it to the position, but nothing booked
-- a deposit: Polygon has no flows audit (chains.py 'audit': False), and the
-- P&L read the $56 as profit (+$29.93 instead of about -$26). From now on
-- capital.wrap_native books every wrap (book_native_deposit).
--
-- Price: WPOL 0.09700 USDT0 at the 16:47:15Z snapshot, USDT0 at $1; the
-- equity step that minute was $55.91 (wallet $4.50 -> $60.40). Only on the
-- database where poly-lp is that wallet; one row per signature. Idempotent.
begin;
set local lock_timeout = '5s';
insert into rebalancer.capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, amounts, wallet_id, profile)
select '2026-10-08 16:47:11+00', 'deposit', 0, 0, 576.3723 * 0.09700, 0.09700,
       '0x5b2ea6949d205ab6774a942f49602c575365b15850439018057b441b986e53c4',
       '576.372300 POL deposit, wrapped into the pool token; booked by sql/044 (missed 2026-10-08)',
       jsonb_build_object('0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270', 576.3723),
       'poly-lp', 'poly-wpol-usdt'
 where exists (select 1 from rebalancer.config where name = 'poly-wpol-usdt' and wallet_id = 'poly-lp')
on conflict (signature) do nothing;
commit;
