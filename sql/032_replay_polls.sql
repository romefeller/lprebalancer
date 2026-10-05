-- 032: replay testing. Every poll that reaches a market decision stores what
-- it saw (the band, the regime and calm views, the forecast, the move gates
-- and the knobs) and the verdict it reached. tests/replay_capture.py turns a
-- stretch of rows into a fixture; tests/test_replay.py runs the fixtures
-- through rebalancer.poll_verdict and fails on any changed verdict. Only the
-- last 14 days are kept: every insert deletes older rows of its profile.
begin;
set local lock_timeout = '5s';
create table if not exists rebalancer.replay_polls (
  id      bigint generated always as identity primary key,
  ts      timestamptz not null default now(),
  profile text        not null,
  pool    text        not null,
  seen    jsonb       not null,
  verdict jsonb       not null
);
create index if not exists replay_polls_profile_ts on rebalancer.replay_polls (profile, ts);
commit;
