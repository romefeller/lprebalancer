-- 023: the swing (swing.py). A swing profile LPs one pool while its market
-- is open and another while it is closed (2026-10-02: DJT/USDC during the US
-- session, SOL/USDC outside it). One row per swing profile; swing.py serves
-- every enabled row. A row only names pools: the profile's own process moves
-- only to a pair its service environment pins (LPBOT_SWING_POOLS), so a write
-- here cannot redirect money.
create table if not exists rebalancer.swing (
  profile      text primary key references rebalancer.config (name) on delete cascade,
  open_dex     text not null,
  open_pool    text not null,
  closed_dex   text not null,
  closed_pool  text not null,
  calendar     text not null default 'nyse' check (calendar in ('nyse')),
  lead_s       integer not null default 300 check (lead_s between 0 and 3600),
  enabled      boolean not null default true,
  updated_at   timestamptz not null default now(),
  check (open_pool <> closed_pool)
);
