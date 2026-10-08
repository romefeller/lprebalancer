-- 035: native_keep (owner, 2026-10-08: "let 10 POL for gas and the rest swap
-- to pool"). On a chain whose wrapped native token is one of the pool's tokens
-- (Polygon: WPOL), native coin above native_keep is wrapped into the pool
-- token each poll; native_keep stays for gas. NULL or 0: never wrap (every
-- profile before). In the chain's native units (POL on Polygon).
--
-- Additive and idempotent.
begin;
set local lock_timeout = '5s';
alter table rebalancer.config add column if not exists native_keep numeric(18, 9);
alter table rebalancer.config drop constraint if exists config_native_keep_sane;
alter table rebalancer.config add constraint config_native_keep_sane check (native_keep is null or native_keep >= 0);
commit;
