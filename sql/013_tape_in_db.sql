-- The five-minute tape lives in the database, one row per bar, and only the
-- window the regime uses is kept (regime_tape_days): older bars are deleted
-- on every write. No cache files in the bot directory.
create table if not exists rebalancer.tape5 (
  pool   text   not null,
  ts     bigint not null,
  open   double precision not null,
  high   double precision not null,
  low    double precision not null,
  close  double precision not null,
  volume double precision not null,
  primary key (pool, ts)
);
