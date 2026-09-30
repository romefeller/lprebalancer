-- Circuit breakers (2026-09-30), one row per dependency: health.py.
--
-- An RPC answered 403 to every pre-open swap and nothing remembered it: the
-- bot closed and reopened lopsided every ~11 minutes. A breaker remembers
-- consecutive failures across restarts, backs off exponentially, and trips
-- after three (a venue that trips is failed over).
begin;
set local lock_timeout = '5s';

create table if not exists rebalancer.health (
  key         text primary key,                 -- 'swap', 'venue:<dex>', 'tape', ...
  fails       integer not null default 0 check (fails >= 0),   -- consecutive failures
  trips       integer not null default 0 check (trips >= 0),   -- times it reached the trip count
  last_fail   double precision,                 -- epoch seconds
  last_ok     double precision,
  retry_at    double precision,                 -- no use before this (epoch seconds)
  last_error  text,
  updated     timestamptz not null default now()
);

commit;
