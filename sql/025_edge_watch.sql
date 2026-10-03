-- 025: the edge watch. Between two polls, while the price sits within
-- edge_watch_pct (percent of the price) of a band edge, the loop reads the
-- pool's price every edge_watch_seconds (one RPC account read, no signer) and
-- polls at once when it leaves the band, instead of sleeping the full poll.
-- Live exits over 2026-09-27..10-03 were seen 0.15% past the edge on average
-- (max 0.47%) with a 120 s poll. 0 turns it off.
alter table rebalancer.config
  add column if not exists edge_watch_pct     numeric(6,3) not null default 0,
  add column if not exists edge_watch_seconds integer      not null default 15;
alter table rebalancer.config drop constraint if exists config_edge_watch_sane;
alter table rebalancer.config add constraint config_edge_watch_sane check (
  edge_watch_pct >= 0 and edge_watch_pct < 50 and edge_watch_seconds between 5 and 120);
