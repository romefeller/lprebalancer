-- The regime's touch forecasts, checked against the tape. Every few minutes
-- one row: the price, and for every width the predicted P(touch within the
-- horizon) of a band centred there. After the horizon it is resolved from
-- the five-minute highs and lows in rebalancer.tape5. Kept 30 days.
create table if not exists rebalancer.touch_forecasts (
  id           bigint generated always as identity primary key,
  ts           timestamptz not null default now(),
  pool         text not null,
  price        double precision not null,
  horizon_min  integer not null,
  threshold    double precision not null,
  choice       double precision not null,
  probs        jsonb not null,             -- [[half-width %, p], ...]
  resolved     boolean not null default false,
  touched      jsonb                       -- [bool, ...] aligned with probs
);
create index if not exists touch_forecasts_open on rebalancer.touch_forecasts (pool, ts) where not resolved;
create index if not exists touch_forecasts_ts on rebalancer.touch_forecasts (ts desc);
