-- 051: the small bots test a second pool beside 8sLb (owner, 2026-10-17: "FIND A
-- SIMILAR POOL TO PUT THE SMALL BOTS. NOW."). tang0 and sol-usdc hold 8sLb alone.
-- test0 moved to Meteora DLMM 5rCf (its vault already holds that pool; MIGRATE file).
-- sol-swing: Byreal 9GTj (0.012% fee, thin like 8sLb) after the NYSE close instead
-- of 8sLb; byreal armed for it. Applied live 20:24Z. Idempotent.
begin;
set local lock_timeout = '5s';
update rebalancer.config set execute_dexes = array_append(execute_dexes, 'byreal'), updated_at = now()
 where name = 'sol-swing' and not 'byreal' = any(execute_dexes);
update rebalancer.swing set closed_dex = 'byreal', closed_pool = '9GTj99g9tbz9U6UYDsX6YeRTgUnkYG6GTnHv3qLa5aXq', updated_at = now()
 where profile = 'sol-swing' and closed_pool <> '9GTj99g9tbz9U6UYDsX6YeRTgUnkYG6GTnHv3qLa5aXq';
commit;
