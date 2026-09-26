"""The database. Every parameter and every statistic lives here.

Two things used to be scattered and are now in one place:

  * configuration, which used to be constants in a Python file, so retuning
    meant editing code and restarting;
  * accounting, which used to be a SQLite file next to the code, so it was
    invisible to anything that was not the bot.

Both are Postgres tables now. The bot reads its parameters from
`rebalancer.config` at startup and writes everything it observes to
`rebalancer.snapshots`, `harvests`, `positions` and `events`. You can retune a
running deployment with an UPDATE and a restart, and you can read the numbers
with psql while the bot is mid-rebalance.

    python3 db.py add <name> <pool> [k=v ...]   describe a pool; symbols come from Orca
    python3 db.py activate <name>               make it the one the bot runs
    python3 db.py set <name> k=v [k=v ...]      retune
    python3 db.py [stats|daily|history|json]    the book
    python3 db.py forecasts                     the survival model against the tape

Connections are opened per operation. At one write every few minutes the cost
is nothing, and it means a dropped connection cannot wedge the loop.
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# Peer authentication as the invoking user, by default. Set LPBOT_DSN for
# anything else (a password, a remote host, a different database).
DSN = os.environ.get('LPBOT_DSN', 'dbname=rebalancer')

MIN_RATE_DAYS = 0.04        # ~1 hour: below this, a per-day rate is noise


@contextmanager
def cursor(commit=False):
    con = psycopg2.connect(DSN)
    try:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('set search_path to rebalancer, public')
            yield cur
        if commit:
            con.commit()
    finally:
        con.close()


def now():
    return datetime.now(timezone.utc)


# --- configuration -----------------------------------------------------------

def load_config(name=None):
    """The active profile, or a named one.

    Raises rather than returning defaults. A bot that cannot read its
    parameters must not run on guesses: it signs transactions with real money.
    """
    with cursor() as cur:
        if name:
            cur.execute('select * from config where name = %s', (name,))
        else:
            cur.execute('select * from config where active')
        row = cur.fetchone()
    if not row:
        raise SystemExit(
            f'No {"profile named " + name if name else "active profile"} in '
            f'rebalancer.config ({DSN}). Nothing was started.\n'
            'Seed one with:  python3 db.py seed')
    return dict(row)


def set_param(name, key, value):
    """UPDATE one column of one profile, with the column name checked first."""
    with cursor(commit=True) as cur:
        cur.execute("select column_name, data_type from information_schema.columns "
                    "where table_schema='rebalancer' and table_name='config'")
        cols = {r['column_name']: r['data_type'] for r in cur.fetchall()}
        if key not in cols:
            raise SystemExit(f'No such parameter: {key}\n'
                             f'Known: {", ".join(sorted(cols))}')
        if key in ('id', 'name'):
            raise SystemExit(f'{key} is not changeable this way.')
        if cols[key] == 'ARRAY':
            value = '{' + ','.join(x.strip() for x in str(value).split(',')) + '}'
        cur.execute(f'update config set {key} = %s, updated_at = now() '
                    'where name = %s returning *', (value, name))
        row = cur.fetchone()
    if not row:
        raise SystemExit(f'No profile named {name}.')
    return dict(row)


def activate(name):
    with cursor(commit=True) as cur:
        cur.execute('update config set active = false where active')
        cur.execute('update config set active = true, updated_at = now() '
                    'where name = %s returning name', (name,))
        if not cur.fetchone():
            raise SystemExit(f'No profile named {name}.')
    return name


def repoint(name, dex, pool, pair_label, token_a, token_b):
    """Move a profile to another pool, on any DEX. The bot calls this when the
    board says a different pool pays better and it can open there; the
    operator can call it by hand through `db.py repoint`."""
    with cursor(commit=True) as cur:
        cur.execute('update config set dex = %s, pool = %s, pair_label = %s, token_a = %s, '
                    'token_b = %s, updated_at = now() where name = %s returning *',
                    (dex, pool, pair_label, token_a, token_b, name))
        row = cur.fetchone()
    if not row:
        raise SystemExit(f'No profile named {name}.')
    return dict(row)


# --- the board ---------------------------------------------------------------

def _num(x):
    return None if x is None else float(x)


def record_scan(config_name, dexes, rows, errors, duration_s, listed, season=None):
    """One scan of the board: the run, then every pool it looked at, ranked.
    Everything the decision used is in `detail`, so a move can be explained
    later from the table alone."""
    scored = [r for r in rows if r.get('net_day_pct') is not None]
    best = scored[0] if scored else None
    with cursor(commit=True) as cur:
        cur.execute("""
            insert into scan_runs (ts, config_name, dexes, pools_listed, pools_scored,
                                   duration_s, errors, best, season)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id
        """, (now(), config_name, list(dexes), listed, len(scored), duration_s,
              json.dumps(errors or {}), json.dumps(_brief(best), default=str) if best else None,
              json.dumps(season) if season else None))
        run_id = cur.fetchone()['id']
        psycopg2.extras.execute_batch(cur, """
            insert into scan_pools (run_id, rank, dex, kind, address, pair, fee, tvl_usd,
                                    volume_24h_usd, c_pool, band, net_day_pct, rebal_per_day,
                                    p25_net_day, worst_net_day, share_positive, p_survive_168h,
                                    executable, screen_ok, screen_reason, skipped, detail)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, [(run_id, i + 1, r['dex'], r.get('kind'), r['address'], r.get('pair'),
               _num(r.get('fee')), _num(r.get('tvl_usd')), _num(r.get('volume_24h_usd')),
               _num(r.get('c_pool')), _num(r.get('band')), _num(r.get('net_day_pct')),
               _num(r.get('rebal_per_day')), _num(r.get('p25_net_day')), _num(r.get('worst_net_day')),
               _num(r.get('share_positive')), _num(r.get('p_survive_168h')),
               bool(r.get('executable')), r.get('screen_ok'), r.get('screen_reason'),
               r.get('skipped'), json.dumps(r, default=str))
              for i, r in enumerate(rows)])
    return run_id


def _brief(r):
    if not r:
        return None
    return {k: r.get(k) for k in ('dex', 'address', 'pair', 'band_pct', 'net_day_pct',
                                  'decision_day_pct', 'liquidity_drift', 'realised_day_pct',
                                  'rebal_per_day', 'tvl_usd', 'volume_24h_usd', 'fee',
                                  'executable', 'screen_ok', 'screen_reason')}


def latest_scan(max_age_seconds=None):
    """The most recent board: (run, rows) with rows in rank order, or (None, [])
    when there is none or it is older than `max_age_seconds`."""
    with cursor() as cur:
        cur.execute('select * from scan_runs order by id desc limit 1')
        run = cur.fetchone()
        if not run:
            return None, []
        if max_age_seconds is not None and \
                (now() - run['ts']).total_seconds() > max_age_seconds:
            return dict(run), []
        cur.execute('select detail, executable, screen_ok, screen_reason, rank, skipped '
                    'from scan_pools where run_id = %s order by rank', (run['id'],))
        rows = []
        for r in cur.fetchall():
            d = r['detail'] or {}
            d.update(executable=r['executable'], screen_ok=r['screen_ok'],
                     screen_reason=r['screen_reason'], rank=r['rank'], skipped=r['skipped'])
            rows.append(d)
    return dict(run), rows


def latest_scan_id():
    """The id of the newest board, or None. Cheap: one row."""
    with cursor() as cur:
        cur.execute('select id from scan_runs order by id desc limit 1')
        r = cur.fetchone()
    return r['id'] if r else None


def record_pool_stats(dex, pool, liquidity, tvl_usd, volume_24h, price):
    """One reading; readings older than two days are deleted (the width
    choice reads the last 24 hours)."""
    with cursor(commit=True) as cur:
        cur.execute('insert into pool_stats (ts, dex, pool, liquidity, tvl_usd, volume_24h, price) '
                    'values (%s,%s,%s,%s,%s,%s,%s)', (now(), dex, pool, liquidity, tvl_usd, volume_24h, price))
        cur.execute("delete from pool_stats where ts < now() - interval '2 days'")


def pool_stats_summary(pool, hours=24):
    """Median liquidity and the TVL of `hours` ago for one pool, from its own
    record, or None with fewer than 3 readings."""
    with cursor() as cur:
        cur.execute("""
            select percentile_cont(0.5) within group (order by liquidity) med_liq, count(*) n,
                   (select tvl_usd from pool_stats p2 where p2.pool = %s and p2.ts <= now() - make_interval(hours => %s)
                    order by p2.ts desc limit 1) tvl_then,
                   min(ts) first_ts
            from pool_stats where pool = %s and ts >= now() - make_interval(hours => %s) and liquidity > 0
        """, (pool, hours, pool, hours))
        r = cur.fetchone()
    if not r or not r['n'] or r['n'] < 3:
        return None
    return {'median_liquidity': float(r['med_liq']), 'readings': int(r['n']),
            'tvl_then': float(r['tvl_then']) if r['tvl_then'] is not None else None,
            'since': r['first_ts']}


def tape_load(pool, since_ts):
    """The pool's stored five-minute bars from `since_ts`, oldest first, as
    six float arrays, or None."""
    import numpy as np
    with cursor() as cur:
        cur.execute('select ts, open, high, low, close, volume from tape5 '
                    'where pool = %s and ts >= %s order by ts', (pool, int(since_ts)))
        rows = cur.fetchall()
    if not rows:
        return None
    a = np.array([[r['ts'], r['open'], r['high'], r['low'], r['close'], r['volume']] for r in rows], dtype=float)
    return tuple(a[:, i].copy() for i in range(6))


def tape_store(pool, bars, keep_from_ts):
    """Upsert bars and delete this pool's bars older than `keep_from_ts`:
    the table holds the window and nothing else."""
    if bars is None or not len(bars[0]):
        return 0
    rows = [(pool, int(t), float(o), float(h), float(l), float(c), float(v))
            for t, o, h, l, c, v in zip(*bars) if t >= keep_from_ts]
    with cursor(commit=True) as cur:
        if rows:
            psycopg2.extras.execute_values(cur, """
                insert into tape5 (pool, ts, open, high, low, close, volume) values %s
                on conflict (pool, ts) do update set open = excluded.open, high = excluded.high,
                    low = excluded.low, close = excluded.close, volume = excluded.volume
            """, rows, page_size=1000)
        cur.execute('delete from tape5 where pool = %s and ts < %s', (pool, int(keep_from_ts)))
    return len(rows)


def tape_prune_other_pools(keep_pools, older_than_ts):
    """Drop tapes of pools no longer held once they are stale."""
    with cursor(commit=True) as cur:
        cur.execute('delete from tape5 where pool <> all(%s) and ts < %s', (list(keep_pools), int(older_than_ts)))


def record_fee_state(dex, pool, st):
    """One sample of a pool's counters; samples older than two days go."""
    with cursor(commit=True) as cur:
        cur.execute('insert into fee_growth (ts, dex, pool, sqrt_price, g0, g1, rewards, dec_a, dec_b, mint_a, mint_b) '
                    'values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (now(), dex, pool, st['sqrt_price'], st['g0'], st['g1'], json.dumps(st.get('rewards') or []),
                     st['dec_a'], st['dec_b'], st['mint_a'], st['mint_b']))
        cur.execute("delete from fee_growth where ts < now() - interval '2 days'")


def fee_state_span(pool, hours=24):
    """The oldest sample within `hours` and the newest, for one pool, with
    the seconds between them; or None with fewer than two samples."""
    with cursor() as cur:
        cur.execute("""
            (select * from fee_growth where pool = %s and ts >= now() - make_interval(hours => %s) order by ts asc limit 1)
            union all
            (select * from fee_growth where pool = %s order by ts desc limit 1)
        """, (pool, hours, pool))
        rows = [dict(r) for r in cur.fetchall()]
    if len(rows) < 2 or rows[0]['id'] == rows[1]['id']:
        return None
    first, last = rows
    return first, last, (last['ts'] - first['ts']).total_seconds()


def record_touch_forecast(pool, price, horizon_min, threshold, choice, probs):
    with cursor(commit=True) as cur:
        cur.execute('insert into touch_forecasts (ts, pool, price, horizon_min, threshold, choice, probs) '
                    'values (%s,%s,%s,%s,%s,%s,%s)',
                    (now(), pool, price, horizon_min, threshold, choice, json.dumps(probs)))
        cur.execute("delete from touch_forecasts where ts < now() - interval '30 days'")


def resolve_touch_forecasts(pool):
    """Resolve every forecast of `pool` whose horizon has passed, from the
    five-minute highs and lows in tape5. A band of half-width w centred at
    the forecast price was touched if any bar starting inside the horizon
    reached price * (1 + w) or price / (1 + w). Forecasts whose horizon the
    tape does not fully cover stay open. Returns the number resolved."""
    n = 0
    with cursor(commit=True) as cur:
        cur.execute("""select id, extract(epoch from ts) t0, price, horizon_min, probs from touch_forecasts
                       where pool = %s and not resolved
                         and ts < now() - make_interval(mins => horizon_min + 10)
                       order by ts limit 500""", (pool,))
        rows = cur.fetchall()
        for r in rows:
            t0 = float(r['t0']); t1 = t0 + r['horizon_min'] * 60
            cur.execute('select max(high) hi, min(low) lo, count(*) n, max(ts) last from tape5 '
                        'where pool = %s and ts >= %s and ts < %s', (pool, int(t0), int(t1)))
            b = cur.fetchone()
            need = int(r['horizon_min'] * 60 / 300)
            if not b or not b['n'] or b['n'] < need * 0.9:
                continue                      # the tape does not cover this horizon (yet)
            p = float(r['price'])
            touched = [bool(b['hi'] >= p * (1 + w / 100) or b['lo'] <= p / (1 + w / 100)) for w, _ in r['probs']]
            cur.execute('update touch_forecasts set resolved = true, touched = %s where id = %s',
                        (json.dumps(touched), r['id']))
            n += 1
    return n


def touch_calibration(days=7, pool=None):
    """Predicted against realised touch rates, per width and for the chosen
    width, over resolved forecasts of the last `days`. Brier is the mean
    squared error of the probability; 'said' the mean prediction, 'saw' the
    share of bands actually touched."""
    with cursor() as cur:
        cur.execute("""select choice, probs, touched from touch_forecasts
                       where resolved and ts >= now() - make_interval(days => %s)
                         and (%s::text is null or pool = %s)""", (days, pool, pool))
        rows = cur.fetchall()
    per, chosen = {}, {'n': 0, 'said': 0.0, 'saw': 0.0, 'sq': 0.0}
    for r in rows:
        for (w, p), t in zip(r['probs'], r['touched'] or []):
            if p is None:
                continue
            d = per.setdefault(float(w), {'n': 0, 'said': 0.0, 'saw': 0.0, 'sq': 0.0})
            d['n'] += 1; d['said'] += p; d['saw'] += float(t); d['sq'] += (p - float(t)) ** 2
            if abs(float(w) - (float(r['choice']) - 1) * 100) < 1e-6:
                chosen['n'] += 1; chosen['said'] += p; chosen['saw'] += float(t); chosen['sq'] += (p - float(t)) ** 2
    fmt = lambda d: ({'n': d['n'], 'said': round(d['said'] / d['n'], 3), 'saw': round(d['saw'] / d['n'], 3),
                      'brier': round(d['sq'] / d['n'], 4)} if d['n'] else {'n': 0})
    return {'days': days, 'widths': {f'{w:g}': fmt(d) for w, d in sorted(per.items())}, 'chosen': fmt(chosen)}


def season():
    """The latest hour-of-day profile the board built, or None."""
    with cursor() as cur:
        cur.execute('select season from scan_runs where season is not null order by id desc limit 1')
        r = cur.fetchone()
    return list(r['season']) if r and r['season'] else None


def season_outlook(profile, hour=None, ahead=6):
    """Where in the day's rhythm we are: the current hour's multiplier, the
    mean multiplier of the next `ahead` hours, and whether this is a quiet
    hour (at or under the average) in which a voluntary move costs least."""
    if not profile or len(profile) != 24:
        return None
    h = now().hour if hour is None else hour
    nxt = [profile[(h + i) % 24] for i in range(1, ahead + 1)]
    return {'hour_utc': h, 'now_x': round(profile[h], 2), 'next_hours': ahead,
            'next_x': round(sum(nxt) / len(nxt), 2), 'quiet': profile[h] <= 1.0,
            'peak_hour_utc': int(max(range(24), key=lambda i: profile[i])),
            'trough_hour_utc': int(min(range(24), key=lambda i: profile[i]))}


def scan_history(address, limit=30):
    """How one pool has scored over time."""
    with cursor() as cur:
        cur.execute('select s.ts, p.rank, p.band, p.net_day_pct, p.rebal_per_day, p.tvl_usd, '
                    'p.volume_24h_usd, p.c_pool from scan_pools p join scan_runs s on s.id = p.run_id '
                    'where p.address = %s order by s.id desc limit %s', (address, limit))
        return [dict(r) for r in cur.fetchall()]


# --- accounting: writes ------------------------------------------------------

def open_position(mint, pool, pair, lower, upper, band_pct, sig,
                  deposit_usd, reason='', config_name=None, dex='orca'):
    with cursor(commit=True) as cur:
        cur.execute("""
            insert into positions (mint, config_name, pool, pair_label, opened_at,
                                   lower_price, upper_price, band_pct, open_sig,
                                   deposit_usd, open_reason, dex)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (mint) do update set
                pool = excluded.pool, pair_label = excluded.pair_label,
                lower_price = excluded.lower_price, upper_price = excluded.upper_price,
                band_pct = excluded.band_pct, open_sig = excluded.open_sig,
                deposit_usd = excluded.deposit_usd, open_reason = excluded.open_reason,
                dex = excluded.dex
        """, (mint, config_name, pool, pair, now(), lower, upper, band_pct, sig,
              deposit_usd, reason, dex))


def close_position(mint, sig, withdraw_usd):
    with cursor(commit=True) as cur:
        cur.execute('update positions set closed_at = coalesce(closed_at, %s), '
                    'close_sig = coalesce(close_sig, %s), '
                    'withdraw_usd = coalesce(withdraw_usd, %s) where mint = %s',
                    (now(), sig, withdraw_usd, mint))


def position_opened(mint):
    with cursor() as cur:
        cur.execute('select opened_at from positions where mint = %s', (mint,))
        r = cur.fetchone()
    return r['opened_at'] if r else None


def position_open_price(mint):
    """The price the band was centred on: the geometric middle of the band."""
    with cursor() as cur:
        cur.execute('select lower_price, upper_price from positions where mint = %s', (mint,))
        r = cur.fetchone()
    if not r or not r['lower_price'] or not r['upper_price']:
        return None
    return float(r['lower_price'] * r['upper_price']) ** 0.5


def record_harvest(mint, fee_a, fee_b, fee_usd, sig):
    with cursor(commit=True) as cur:
        cur.execute('insert into harvests (ts, mint, fee_a, fee_b, fee_usd, signature) '
                    'values (%s,%s,%s,%s,%s,%s)',
                    (now(), mint, fee_a or 0, fee_b or 0, fee_usd or 0, sig))


def snapshot(mint, price, in_range, liquidity, accrued_a, accrued_b,
             accrued_usd, wallet_usd, position_usd, forecast=None):
    # Equity includes the wallet, position principal/rent, and pending fees.
    # A harvest transfers value between these components without creating P&L.
    # If either principal component could not be read
    # this poll, equity is unknown — not "the other half", which would show up
    # as a $170 loss in the P&L for one poll and a $170 gain in the next.
    equity = ((wallet_usd + position_usd + (accrued_usd or 0))
              if (wallet_usd is not None and position_usd is not None) else None)
    # The forecast made at this poll is kept next to the observation, so the
    # survival model can be checked against what happened (see forecasts()).
    f = forecast or {}
    with cursor(commit=True) as cur:
        cur.execute("""
            insert into snapshots (ts, mint, price, in_range, liquidity, accrued_a,
                                   accrued_b, accrued_usd, wallet_usd, position_usd,
                                   equity_usd, p_exit_6h, p_exit_24h, p_exit_72h, band_position)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (now(), mint, price, bool(in_range), str(liquidity or 0),
              accrued_a, accrued_b, accrued_usd, wallet_usd, position_usd, equity,
              f.get('p_exit_6h_regime', f.get('p_exit_6h')),
              f.get('p_exit_24h_regime', f.get('p_exit_24h')),
              f.get('p_exit_72h_regime', f.get('p_exit_72h')),
              f.get('position')))


def record_payout(config_name, position, token_mint, symbol, amount, usd, kind,
                  to_address=None, signature=None, detail=None):
    with cursor(commit=True) as cur:
        cur.execute('insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, '
                    'kind, to_address, signature, detail) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (now(), config_name, position, token_mint, symbol, amount, usd, kind,
                     to_address, signature, detail))


def reinvested_usd(config_name):
    """Dollars of fees reinvested so far: they raise the sizing base."""
    with cursor() as cur:
        cur.execute("select coalesce(sum(usd), 0) usd from payouts "
                    "where config_name = %s and kind = 'reinvested'", (config_name,))
        return float(cur.fetchone()['usd'])


def payout_totals(config_name=None):
    """Fee split so far, in dollars, by kind; and today's payout."""
    with cursor() as cur:
        cur.execute("""
            select kind, coalesce(sum(usd), 0) usd, count(*) n,
                   coalesce(sum(usd) filter (where ts >= date_trunc('day', now() at time zone 'utc')), 0) today
            from payouts where (%s::text is null or config_name = %s) group by kind
        """, (config_name, config_name))
        rows = {r['kind']: r for r in cur.fetchall()}
    g = lambda k, f='usd': round(float(rows[k][f]), 4) if k in rows else 0.0
    return {'paid_usd': g('paid'), 'paid_today_usd': g('paid', 'today'),
            'reinvested_usd': g('reinvested'), 'gas_usd': g('gas'),
            'payouts': int(rows['paid']['n']) if 'paid' in rows else 0}


def event(kind, detail=''):
    with cursor(commit=True) as cur:
        cur.execute('insert into events (ts, kind, detail) values (%s,%s,%s)',
                    (now(), kind, str(detail)[:2000]))


# --- accounting: reads -------------------------------------------------------

def _f(x):
    return float(x) if x is not None else 0.0


def fees_between(start, end=None):
    """Accrual earned between two UTC boundaries, including closed positions.

    The latest observation before the start is the baseline, so collecting
    yesterday's fees today cannot count them as today's earnings.
    """
    with cursor() as cur:
        cur.execute("""
            with per_mint as (
                select mint,
                    (array_agg(a order by ts desc, kind desc, id desc))[1]
                      - coalesce((array_agg(a order by ts desc, kind desc, id desc)
                                  filter (where ts <= %(start)s))[1], 0) a,
                    (array_agg(b order by ts desc, kind desc, id desc))[1]
                      - coalesce((array_agg(b order by ts desc, kind desc, id desc)
                                  filter (where ts <= %(start)s))[1], 0) b,
                    (array_agg(usd order by ts desc, kind desc, id desc))[1]
                      - coalesce((array_agg(usd order by ts desc, kind desc, id desc)
                                  filter (where ts <= %(start)s))[1], 0) usd
                from fee_points where ts <= %(end)s group by mint
            )
            select coalesce(sum(a), 0) a, coalesce(sum(b), 0) b,
                   coalesce(sum(usd), 0) usd from per_mint
        """, {'start': start, 'end': end or now()})
        return dict(cur.fetchone())


def trailing_rate(hours):
    """Fees earned per day over the last `hours`, from the ledger's own
    series: for every position, fees to date are its unharvested accrual plus
    what was harvested from it, so the window's earning is the rise of that
    sum, per position, summed. Realised does not move it, a rebalance does not
    reset it. None when the window has fewer than two points."""
    with cursor() as cur:
        cur.execute("""
            with pts as (
                select s.ts, s.mint,
                       s.accrued_usd + coalesce((select sum(h.fee_usd) from harvests h
                                                 where h.mint = s.mint and h.ts <= s.ts), 0) usd
                from snapshots s
                where s.ts >= now() - make_interval(hours => %s) - interval '10 minutes'
                union all
                select h.ts, h.mint,
                       (select sum(fee_usd) from harvests x where x.mint = h.mint and x.ts <= h.ts)
                from harvests h where h.ts >= now() - make_interval(hours => %s) - interval '10 minutes'
                union all
                select p.opened_at, p.mint, 0 from positions p
                where p.opened_at >= now() - make_interval(hours => %s) - interval '10 minutes'
            ), per_mint as (
                select mint, max(usd) - min(usd) usd, min(ts) t0, max(ts) t1 from pts group by mint
            )
            select coalesce(sum(usd), 0) usd, min(t0) t0, max(t1) t1, count(*) n from per_mint
        """, (hours, hours, hours))
        r = cur.fetchone()
    if not r or not r['n'] or r['t0'] is None or r['t1'] is None:
        return None
    span_days = (r['t1'] - r['t0']).total_seconds() / 86400
    if span_days < MIN_RATE_DAYS:
        return None
    return {'fees_usd': round(_f(r['usd']), 4), 'days': round(span_days, 3),
            'fees_per_day_usd': round(_f(r['usd']) / span_days, 4)}


def by_pool():
    """The book split by pool, and therefore by DEX: for every pool the bot
    has ever held, how long it was there, what it earned there realised and
    unrealised, and how often it was in range. The bot moves between DEXes
    now, so a single running total says nothing about which venue paid."""
    with cursor() as cur:
        cur.execute("""
            with per_pos as (
                select p.mint, p.dex, p.pool, p.pair_label, p.opened_at, p.closed_at,
                       coalesce((select sum(h.fee_usd) from harvests h where h.mint = p.mint), 0) realised_usd,
                       case when p.closed_at is null then coalesce(
                           (select s.accrued_usd from snapshots s where s.mint = p.mint
                            order by s.id desc limit 1), 0) else 0 end unrealised_usd,
                       extract(epoch from coalesce(p.closed_at, now()) - p.opened_at) / 86400 days,
                       (select avg(case when s.in_range then 1.0 else 0.0 end)
                        from snapshots s where s.mint = p.mint) in_range,
                       (select s.equity_usd from snapshots s where s.mint = p.mint
                        order by s.id desc limit 1) equity_usd,
                       p.deposit_usd,
                       -- what came out: the recorded withdrawal for a closed
                       -- position, the latest mark for an open one
                       case when p.closed_at is null then
                           (select s.position_usd from snapshots s where s.mint = p.mint
                            and s.position_usd is not null order by s.id desc limit 1)
                       else p.withdraw_usd end out_usd
                from positions p
            )
            select dex, pool, pair_label, count(*) positions,
                   count(*) filter (where closed_at is null) open_now,
                   sum(days) days, sum(realised_usd) realised_usd, sum(unrealised_usd) unrealised_usd,
                   avg(in_range) in_range, min(opened_at) first_opened,
                   max(coalesce(closed_at, now())) last_seen,
                   max(equity_usd) filter (where closed_at is null) equity_usd,
                   sum(deposit_usd) deposit_usd,
                   -- position P&L: out - in, over positions where both are known
                   sum(out_usd - deposit_usd) filter (where out_usd is not null and deposit_usd is not null) position_pnl_usd,
                   count(*) filter (where out_usd is null or deposit_usd is null) unpriced,
                   sum(deposit_usd * days) / nullif(sum(days), 0) avg_deposit_usd
            from per_pos
            group by 1, 2, 3
            order by max(opened_at) desc
        """)
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            days = _f(d['days'])
            total = _f(d['realised_usd']) + _f(d['unrealised_usd'])
            rate = (total / days) if days >= MIN_RATE_DAYS else None
            avg_dep = _f(d['avg_deposit_usd'])
            ppnl = d['position_pnl_usd']
            d.update(days=round(days, 3), realised_usd=round(_f(d['realised_usd']), 4),
                     unrealised_usd=round(_f(d['unrealised_usd']), 4), fees_usd=round(total, 4),
                     fees_per_day_usd=(round(rate, 4) if rate is not None else None),
                     # fee APR on the capital that sat in this pool, not on notional
                     apr_pct=(round(rate / avg_dep * 365 * 100, 2) if rate is not None and avg_dep > 0 else None),
                     in_range_pct=(round(_f(d['in_range']) * 100, 1) if d['in_range'] is not None else None),
                     equity_usd=(round(_f(d['equity_usd']), 2) if d['equity_usd'] is not None else None),
                     deposit_usd=round(_f(d['deposit_usd']), 2),
                     position_pnl_usd=(round(_f(ppnl), 4) if ppnl is not None else None),
                     # total: what came out minus what went in, plus every fee
                     pnl_usd=(round(_f(ppnl) + total, 4) if ppnl is not None else None),
                     unpriced=int(d['unpriced'] or 0))
            d.pop('in_range'); d.pop('avg_deposit_usd')
            rows.append(d)
        return rows


def _paid_usd(config_name):
    try:
        return float(payout_totals(config_name)['paid_usd'])
    except Exception:
        return 0.0


def stats(token_a=None, token_b=None):
    """Everything cumulative, in both tokens and in dollars.

    Fees are earned in two currencies, not one. A position pays you token A and
    token B in whatever proportion the trading happened to take, so a single
    dollar figure hides what you hold and moves with the price even in an hour
    when you earned nothing. Both are reported; the dollar figure is the
    convenience.

    Realised means harvested into the wallet: permanent. Unrealised means still
    sitting in the position and reset to zero the moment it closes. Their sum is
    what the ledger has seen you earn, and it only goes up.
    """
    with cursor() as cur:
        cur.execute('select * from config where active')
        cfg = cur.fetchone() or {}

        cur.execute('select coalesce(sum(fee_a),0) a, coalesce(sum(fee_b),0) b, '
                    'coalesce(sum(fee_usd),0) usd, count(*) n from harvests')
        r = cur.fetchone()

        # The latest snapshot, and whether the position it describes still
        # exists. Unrealised fees are what is sitting in an OPEN position. A
        # snapshot of a position that has since been harvested and closed
        # describes money that is now in the wallet and already counted as
        # realised; reading it as unrealised too is how a $0.26 harvest was
        # reported as $0.51 of fees.
        cur.execute('select s.ts, s.mint, s.price, s.accrued_a, s.accrued_b, '
                    's.accrued_usd, s.equity_usd, s.in_range, s.p_exit_6h, s.p_exit_24h, '
                    's.p_exit_72h, s.band_position, p.opened_at, '
                    '(p.mint is not null and p.closed_at is null) as open '
                    'from snapshots s left join positions p on p.mint = s.mint '
                    'order by s.id desc limit 1')
        latest = cur.fetchone()
        if latest and not latest['open']:
            latest = dict(latest, accrued_a=0, accrued_b=0, accrued_usd=0)

        # The clock starts when the first position opened, not when the first
        # snapshot landed. A restart must not reset the denominator of the rate.
        cur.execute('''
            select least(
                     (select min(ts) from snapshots),
                     (select min(opened_at) from positions)
                   ) t0,
                   (select avg(case when in_range then 1.0 else 0.0 end)
                    from snapshots) ir
        ''')
        span = cur.fetchone()

        cur.execute('select equity_usd from snapshots where equity_usd is not null '
                    'order by id asc limit 1')
        first_eq = cur.fetchone()

        cur.execute("""
            select count(*) filter (where closed_at is null) open_now,
                   count(*) total from positions
        """)
        pos = cur.fetchone()

        # Where the money is, from the ledger rather than the config: the two
        # agree except in the seconds between a repoint and the next open.
        cur.execute('select dex, pool, pair_label from positions where closed_at is null '
                    'order by opened_at desc limit 1')
        cur_pos = cur.fetchone()

        cur.execute("""
            select count(*) filter (where kind = 'REBAND') rebands,
                   count(*) filter (where kind ilike '%fail%') failures
            from events
        """)
        ev = cur.fetchone()

    u_a = _f(latest and latest['accrued_a'])
    u_b = _f(latest and latest['accrued_b'])
    u_usd = _f(latest and latest['accrued_usd'])
    equity = latest and latest['equity_usd']
    if equity is None and latest:
        # One failed wallet read must not blank the book: the latest snapshot
        # that could be priced stands in, and says how old it is.
        with cursor() as cur:
            cur.execute('select equity_usd, ts from snapshots where equity_usd is not null '
                        'order by id desc limit 1')
            last_priced = cur.fetchone()
        equity = last_priced and last_priced['equity_usd']
    started = first_eq and first_eq['equity_usd']

    today = fees_between(now().replace(hour=0, minute=0, second=0, microsecond=0))

    days = None
    if latest and span and span['t0']:
        days = max((latest['ts'] - span['t0']).total_seconds() / 86400, 1e-9)

    total_usd = _f(r['usd']) + u_usd
    rate = total_usd / days if (days and days >= MIN_RATE_DAYS) else None
    # Annualised on the capital actually at work, not on notional.
    apr = (rate / float(equity) * 365 * 100) if (rate and equity) else None

    pools = by_pool()
    t6, t24 = trailing_rate(6), trailing_rate(24)
    outlook = season_outlook(season())
    eq_f = _f(equity) if equity else None
    apr_of = lambda t: (round(t['fees_per_day_usd'] / eq_f * 365 * 100, 2)
                        if t and eq_f else None)
    rnd = lambda x, d=6: round(_f(x), d)
    return {
        'pair': cfg.get('pair_label'),
        'dex': cfg.get('dex'),
        'position_dex': cur_pos['dex'] if cur_pos else None,
        'position_pair': cur_pos['pair_label'] if cur_pos else None,
        'position_pool': cur_pos['pool'] if cur_pos else None,
        'dexes_held': sorted({r['dex'] for r in pools}),
        'by_pool': [{k: r[k] for k in ('dex', 'pair_label', 'pool', 'positions', 'open_now', 'days',
                                       'fees_usd', 'fees_per_day_usd', 'apr_pct', 'in_range_pct',
                                       'position_pnl_usd', 'pnl_usd')}
                    for r in pools[:8]],
        # across every pool and DEX: fees earned everywhere plus position
        # P&L on every position whose deposit and withdrawal are both known
        'pnl_all_pools_usd': (round(sum(_f(r['pnl_usd']) for r in pools if r['pnl_usd'] is not None), 4)
                              if pools else None),
        'position_pnl_all_pools_usd': (round(sum(_f(r['position_pnl_usd']) for r in pools
                                                 if r['position_pnl_usd'] is not None), 4)
                                       if pools else None),
        'token_a': token_a or cfg.get('token_a') or 'A',
        'token_b': token_b or cfg.get('token_b') or 'B',
        'fees_today_a': rnd(today['a']),
        'fees_today_b': rnd(today['b']),
        'fees_today_usd': round(_f(today['usd']), 4),
        'fees_realised_a': rnd(r['a']), 'fees_realised_b': rnd(r['b']),
        'fees_realised_usd': round(_f(r['usd']), 4),
        'fees_unrealised_a': rnd(u_a), 'fees_unrealised_b': rnd(u_b),
        'fees_unrealised_usd': round(u_usd, 4),
        'fees_total_a': rnd(_f(r['a']) + u_a),
        'fees_total_b': rnd(_f(r['b']) + u_b),
        'fees_total_usd': round(total_usd, 4),
        'fees_per_day_usd': round(rate, 4) if rate else None,
        'apr_pct': round(apr, 2) if apr else None,
        # what it is doing NOW, not the average since the first position: the
        # since-start figure is a total over an ever-longer clock and decays
        # toward the true rate from wherever the first hour happened to put it
        'fees_per_day_6h_usd': t6 and t6['fees_per_day_usd'],
        'fees_per_day_24h_usd': t24 and t24['fees_per_day_usd'],
        'apr_6h_pct': apr_of(t6), 'apr_24h_pct': apr_of(t24),
        # the day's rhythm, from the board's candles: where this hour sits
        # against the average hour, and what the next hours usually carry
        'season': outlook,
        'expected_next_hours_fees_per_day_usd': (
            round(t24['fees_per_day_usd'] * outlook['next_x'], 4)
            if (t24 and outlook) else None),
        'harvests': r['n'],
        'positions_opened': pos['total'], 'positions_open_now': pos['open_now'],
        'rebands': ev['rebands'], 'failures': ev['failures'],
        'equity_usd': round(_f(equity), 2) if equity is not None else None,
        'equity_start_usd': round(_f(started), 2) if started is not None else None,
        # Payouts leave the LP wallet by design; they are income, not a loss
        # (review, 2026-09-26: the book counted every payout against P&L).
        'pnl_usd': (round(_f(equity) - _f(started) + _paid_usd(cfg.get('name')), 2)
                    if (equity is not None and started is not None) else None),
        'in_range_pct': (round(_f(span['ir']) * 100, 1)
                         if span and span['ir'] is not None else None),
        'tracked_days': round(days, 3) if days else None,
        'split': payout_totals(cfg.get('name')) if cfg.get('payout_enabled') else None,
        'last_price': _f(latest and latest['price']) or None,
        'last_seen': latest['ts'].isoformat() if latest else None,
        # the survival figures recorded at the last poll of the open position
        'band': ({'in_range': latest['in_range'],
                  'position': _num(latest['band_position']),
                  'p_exit_6h': _num(latest['p_exit_6h']),
                  'p_exit_24h': _num(latest['p_exit_24h']),
                  'p_exit_72h': _num(latest['p_exit_72h']),
                  'hours_alive': (round((latest['ts'] - latest['opened_at']).total_seconds() / 3600, 1)
                                  if latest['opened_at'] else None)}
                 if latest and latest['open'] else None),
    }


def forecasts(horizons=(6, 24, 72)):
    """The survival model against the tape it ran on. For every snapshot that
    carried a forecast, whether the SAME position was seen out of range within
    the horizon (a position that closed for another reason before the horizon
    ran out is censored: dropped, not counted as a survivor). Predictions are
    bucketed by decile so a bucket's mean forecast can be read next to its
    realised exit rate; Brier is the mean squared error of the probability."""
    out = {}
    with cursor() as cur:
        for h in horizons:
            cur.execute(f"""
                with f as (
                    select s.id, s.ts, s.mint, s.p_exit_{h}h p
                    from snapshots s
                    where s.p_exit_{h}h is not null and s.in_range
                ), o as (
                    select f.id, f.p,
                           exists (select 1 from snapshots x where x.mint = f.mint
                                   and x.ts > f.ts and x.ts <= f.ts + make_interval(hours => %s)
                                   and not x.in_range) exited,
                           (select max(x.ts) from snapshots x where x.mint = f.mint) last_seen
                    from f
                )
                select width_bucket(p, 0, 1.0000001, 10) bucket, count(*) n, avg(p) p_mean,
                       avg(case when exited then 1.0 else 0.0 end) exit_rate,
                       avg((p - case when exited then 1.0 else 0.0 end) ^ 2) brier
                from o
                where exited or last_seen >= (select ts from snapshots where id = o.id) + make_interval(hours => %s)
                group by 1 order by 1
            """, (h, h))
            rows = [dict(r) for r in cur.fetchall()]
            n = sum(r['n'] for r in rows)
            brier = (sum(_f(r['brier']) * r['n'] for r in rows) / n) if n else None
            out[h] = {'n': n, 'brier': (round(brier, 4) if brier is not None else None),
                      'buckets': [{'n': r['n'], 'p_mean': round(_f(r['p_mean']), 3),
                                   'exit_rate': round(_f(r['exit_rate']), 3)} for r in rows]}
    return out


def daily():
    """Fees per UTC day. One row per day.

    A position's earnings to date are its unharvested accrual plus everything
    already harvested from it. That sum is what grows as fees come in and does
    not move when a harvest turns accrued into realised, so the day's earning
    is its rise over the day, per position, summed. Adding harvests to the
    accrual delta instead counted a $0.26 harvest twice.
    """
    with cursor() as cur:
        cur.execute("""
            with last_per_day as (
                select distinct on ((ts at time zone 'utc')::date, mint)
                       (ts at time zone 'utc')::date d, mint, a, b, usd
                from fee_points
                order by (ts at time zone 'utc')::date, mint, ts desc, kind desc, id desc
            ), per_mint as (
                select d, mint,
                       a - coalesce(lag(a) over w, 0) a,
                       b - coalesce(lag(b) over w, 0) b,
                       usd - coalesce(lag(usd) over w, 0) usd
                from last_per_day window w as (partition by mint order by d)
            ), f as (
                select d, sum(a) a, sum(b) b, sum(usd) usd from per_mint group by d
            ), s as (
                select (ts at time zone 'utc')::date d,
                       avg(case when in_range then 1.0 else 0.0 end) ir,
                       max(equity_usd) eq
                from snapshots group by 1
            )
            , where_ as (
                -- the pools the money sat in that day, most-snapshotted first
                select (s.ts at time zone 'utc')::date d,
                       string_agg(distinct p.dex || ' ' || p.pair_label, ', ') pools
                from snapshots s join positions p on p.mint = s.mint group by 1
            )
            select coalesce(f.d, s.d)::date as day,
                   coalesce(f.a, 0) fee_a, coalesce(f.b, 0) fee_b, coalesce(f.usd, 0) fee_usd,
                   s.ir in_range, s.eq equity_usd, w.pools
            from f full outer join s on f.d = s.d
            left join where_ w on w.d = coalesce(f.d, s.d)
            order by 1 desc limit 60
        """)
        return [dict(r) for r in cur.fetchall()]


def history(limit=30):
    with cursor() as cur:
        cur.execute('select ts, price, in_range, accrued_usd, equity_usd '
                    'from snapshots order by id desc limit %s', (limit,))
        return [dict(r) for r in cur.fetchall()]


# --- command line ------------------------------------------------------------

ORCA = 'https://api.orca.so/v2/solana'
SEED_POOL = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE'   # SOL/USDC 0.04%


def describe_pool(pool, dex='orca'):
    """Ask the DEX what this pool is. Refuses the one kind the Orca signer
    cannot open."""
    import dexes
    try:
        rec = dexes.pool(dex, pool)
    except Exception as e:
        raise SystemExit(f'{dex} did not answer for {pool} ({e}). Nothing was added.')
    if not rec:
        raise SystemExit(f'{dex} does not know a pool at {pool}. Nothing was added.')
    a, b = rec['token_a']['symbol'], rec['token_b']['symbol']
    if dex == 'orca' and rec.get('adaptive_fee'):
        raise SystemExit(
            f'{a}/{b} is an adaptive-fee pool. The signer cannot open positions on '
            'it (Whirlpool error 6069, which reads like slippage and is not). '
            'Nothing was added.')
    return {'pair_label': f'{a}/{b}', 'token_a': a, 'token_b': b, 'dex': dex,
            'fee_rate': rec['fee'], 'tvl_usd': rec['tvl_usd'], 'price': rec['price']}


def add(name, pool, active=False, dex='orca', **params):
    """Describe a new pool to the bot. Everything but the address and the size
    comes from the pool itself or from the column defaults.

        python3 db.py add wif-usdc <pool> capital_usd=200 max_usd=300
        python3 db.py add sol-usdc-met <pool> dex=meteora-dlmm
    """
    info = describe_pool(pool, dex)
    p = dict(name=name, pool=pool, active=active, dex=dex,
             pair_label=info['pair_label'], token_a=info['token_a'],
             token_b=info['token_b'], **params)
    p.setdefault('capital_usd', 190)
    p.setdefault('max_usd', float(p['capital_usd']) * 1.4)
    cols = ', '.join(p)
    vals = ', '.join(['%s'] * len(p))
    with cursor(commit=True) as cur:
        if p.get('active'):
            cur.execute('update config set active = false where active')
        cur.execute(f'insert into config ({cols}) values ({vals}) '
                    'on conflict (name) do nothing returning *', list(p.values()))
        row = cur.fetchone()
    out = dict(row) if row else load_config(name)
    out['_pool'] = {k: info[k] for k in ('fee_rate', 'tvl_usd', 'price')}
    return out


def seed():
    """A first profile on SOL/USDC, so a fresh install has something to run."""
    return add('sol-usdc', SEED_POOL, active=True, capital_usd=190, max_usd=260)


def migrate_sqlite(path='ledger.sqlite'):
    """Carry an old SQLite ledger across. Idempotent on positions; the append
    tables are only copied when the Postgres side is still empty, so running it
    twice cannot double-count your fees."""
    import pathlib, sqlite3
    if not pathlib.Path(path).exists():
        return {'moved': 'nothing', 'reason': f'{path} not found'}
    old = sqlite3.connect(path)
    old.row_factory = sqlite3.Row
    moved = {}
    with cursor(commit=True) as cur:
        for row in old.execute('select * from positions'):
            cur.execute("""
                insert into positions (mint, pool, pair_label, opened_at, closed_at,
                    lower_price, upper_price, band_pct, open_sig, close_sig,
                    deposit_usd, withdraw_usd, open_reason)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (mint) do nothing
            """, (row['mint'], row['pool'], row['pair'], row['opened_at'],
                  row['closed_at'], row['lower_price'], row['upper_price'],
                  row['band_pct'], row['open_sig'], row['close_sig'],
                  row['deposit_usd'], row['withdraw_usd'], row['open_reason']))
        moved['positions'] = cur.rowcount

        for table, sql, cols in (
            ('harvests',
             'insert into harvests (ts, mint, fee_a, fee_b, fee_usd, signature) '
             'values (%s,%s,%s,%s,%s,%s)',
             ('ts', 'mint', 'fee_a', 'fee_b', 'fee_usd', 'signature')),
            ('snapshots',
             'insert into snapshots (ts, mint, price, in_range, liquidity, accrued_a,'
             ' accrued_b, accrued_usd, wallet_usd, position_usd, equity_usd) '
             'values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
             ('ts', 'mint', 'price', 'in_range', 'liquidity', 'accrued_a',
              'accrued_b', 'accrued_usd', 'wallet_usd', 'position_usd', 'equity_usd')),
            ('events', 'insert into events (ts, kind, detail) values (%s,%s,%s)',
             ('ts', 'kind', 'detail')),
        ):
            cur.execute(f'select count(*) n from {table}')
            if cur.fetchone()['n']:
                moved[table] = 'skipped, already populated'
                continue
            n = 0
            for row in old.execute(f'select * from {table}'):
                vals = [row[c] for c in cols]
                if table == 'snapshots':
                    vals[3] = bool(vals[3])
                cur.execute(sql, vals)
                n += 1
            moved[table] = n
    old.close()
    return moved


def _print_stats():
    s = stats()
    a, b = s['token_a'], s['token_b']
    w = lambda lbl, ka, kb, ku: print(
        f"  {lbl:<11} {s[ka]:>12.6f} {a:<5} {s[kb]:>12.6f} {b:<5} ${s[ku]:.4f}")
    print(f"{s.get('dex') or ''} {s['pair'] or '-'}   {s['positions_open_now']} open   "
          f"in range {s['in_range_pct']}%   over {s['tracked_days']}d")
    print('FEES')
    w('today', 'fees_today_a', 'fees_today_b', 'fees_today_usd')
    w('realised', 'fees_realised_a', 'fees_realised_b', 'fees_realised_usd')
    w('unrealised', 'fees_unrealised_a', 'fees_unrealised_b', 'fees_unrealised_usd')
    w('TOTAL', 'fees_total_a', 'fees_total_b', 'fees_total_usd')
    print('POOLS')
    for r in s['by_pool']:
        mark = '>' if r['open_now'] else ' '
        rate = f"${r['fees_per_day_usd']:.4f}/day" if r['fees_per_day_usd'] is not None else '-'
        apr = f"APR {r['apr_pct']:.1f}%" if r['apr_pct'] is not None else ''
        pnl = f"P&L {r['pnl_usd']:+.2f}" if r['pnl_usd'] is not None else 'P&L -'
        print(f"  {mark} {r['dex']:<22} {r['pair_label']:<12} {r['days']:>6.2f}d  "
              f"fees ${r['fees_usd']:.4f}  {rate:>14}  {apr:<12} {pnl:<12} in range "
              f"{r['in_range_pct'] if r['in_range_pct'] is not None else '-'}%")
    print(f"  all pools   fees ${s['fees_total_usd']:.4f}   position P&L "
          f"{s['position_pnl_all_pools_usd'] if s['position_pnl_all_pools_usd'] is not None else '-'}"
          f"   total P&L {s['pnl_all_pools_usd'] if s['pnl_all_pools_usd'] is not None else '-'}")
    print('BOOK')
    print(f"  equity      ${s['equity_usd']}   P&L {s['pnl_usd']}")
    print(f"  rate        ${s['fees_per_day_usd']}/day   APR {s['apr_pct']}%   (since start)")
    print(f"  last 6h     ${s['fees_per_day_6h_usd']}/day   APR {s['apr_6h_pct']}%")
    print(f"  last 24h    ${s['fees_per_day_24h_usd']}/day   APR {s['apr_24h_pct']}%")
    o = s.get('season')
    if o:
        print(f"  rhythm      hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average hour; "
              f"next {o['next_hours']}h {o['next_x']}x -> ~${s['expected_next_hours_fees_per_day_usd']}/day"
              f"   (peak {o['peak_hour_utc']:02d}, trough {o['trough_hour_utc']:02d} UTC)")
    b = s.get('band')
    if b and b['p_exit_24h'] is not None:
        print(f"  band        {'in' if b['in_range'] else 'OUT of'} range · position {b['position']:+.2f} "
              f"· alive {b['hours_alive']}h · P(exit) 6h {b['p_exit_6h']:.0%}  24h {b['p_exit_24h']:.0%}  "
              f"72h {b['p_exit_72h']:.0%}")
    print(f"  activity    {s['positions_opened']} positions · {s['rebands']} rebands "
          f"· {s['harvests']} harvests · {s['failures']} failures")


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'stats'
    arg = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == 'seed':
        print(json.dumps(seed(), indent=1, default=str))
    elif cmd == 'add':
        # db.py add <name> <pool> [key=value ...]
        if len(sys.argv) < 4:
            raise SystemExit('usage: db.py add <name> <pool> [capital_usd=... ...]')
        kv = dict(p.partition('=')[::2] for p in sys.argv[4:])
        print(json.dumps(add(arg, sys.argv[3], **kv), indent=1, default=str))
    elif cmd == 'migrate':
        print(json.dumps(migrate_sqlite(arg or 'ledger.sqlite'), indent=1, default=str))
    elif cmd == 'config':
        print(json.dumps(load_config(arg), indent=1, default=str))
    elif cmd == 'activate':
        print('active:', activate(arg))
    elif cmd == 'set':
        # db.py set <profile> key=value [key=value ...]
        profile, pairs = arg, sys.argv[3:]
        for p in pairs:
            k, _, v = p.partition('=')
            row = set_param(profile, k.strip(), v.strip())
            print(f'{k.strip()} = {row[k.strip()]}')
    elif cmd == 'repoint':
        # db.py repoint <profile> <dex> <pool>
        if len(sys.argv) < 5:
            raise SystemExit('usage: db.py repoint <profile> <dex> <pool>')
        info = describe_pool(sys.argv[4], sys.argv[3])
        row = repoint(arg, sys.argv[3], sys.argv[4], info['pair_label'],
                      info['token_a'], info['token_b'])
        print(f"{row['name']} -> {row['dex']} {row['pair_label']} {row['pool']}  (restart the bot)")
    elif cmd == 'board':
        run, rows = latest_scan()
        if not run:
            raise SystemExit('no scan yet')
        import engine
        print(f"scan #{run['id']} at {run['ts']:%Y-%m-%d %H:%M} UTC  "
              f"{run['pools_scored']}/{run['pools_listed']} scored  "
              f"{float(run['duration_s'] or 0):.0f}s  errors {dict(run['errors'] or {})}")
        engine.print_board(rows, top=int(sys.argv[3]) if len(sys.argv) > 3 else 40)
    elif cmd == 'pools':
        for r in by_pool():
            rate = f"${r['fees_per_day_usd']:.4f}/day" if r['fees_per_day_usd'] is not None else '-'
            print(f"{'>' if r['open_now'] else ' '} {r['dex']:<22} {r['pair_label']:<12} "
                  f"{r['positions']} pos  {r['days']:>6.2f}d  fees ${r['fees_usd']:.4f} "
                  f"(real ${r['realised_usd']:.4f} + unreal ${r['unrealised_usd']:.4f})  {rate:>14}  "
                  f"in range {r['in_range_pct'] if r['in_range_pct'] is not None else '-'}%  {r['pool']}")
    elif cmd == 'daily':
        for d in reversed(daily()):
            print(f"{d['day']}  {float(d['fee_a']):.6f} A  {float(d['fee_b']):.6f} B  "
                  f"${float(d['fee_usd']):.4f}  in range "
                  f"{(float(d['in_range'] or 0) * 100):.0f}%  {d.get('pools') or ''}")
    elif cmd == 'history':
        for r in reversed(history()):
            print(f"{r['ts']:%Y-%m-%d %H:%M}  price {float(r['price'] or 0):>9.4f}  "
                  f"in_range {r['in_range']!s:<5}  accrued "
                  f"${float(r['accrued_usd'] or 0):.4f}  "
                  f"equity ${float(r['equity_usd'] or 0):.2f}")
    elif cmd == 'touches':
        cal = touch_calibration(int(arg) if arg else 7)
        c = cal['chosen']
        print(f"regime touch forecasts, last {cal['days']} days (P(touch) within the horizon, centred bands)")
        if c['n']:
            print(f"  chosen width   said {c['said']:.0%}  saw {c['saw']:.0%}  Brier {c['brier']}  n={c['n']}")
        for w, x in cal['widths'].items():
            if x['n']:
                print(f"  +/-{w:<5}%      said {x['said']:.0%}  saw {x['saw']:.0%}  Brier {x['brier']}  n={x['n']}")
    elif cmd == 'forecasts':
        for h, r in forecasts().items():
            print(f"P(exit within {h}h): {r['n']} forecasts resolved, Brier {r['brier']}")
            for b in r['buckets']:
                print(f"    predicted {b['p_mean']:.0%}  realised {b['exit_rate']:.0%}  (n={b['n']})")
    elif cmd == 'json':
        print(json.dumps(stats(), indent=1, default=str))
    else:
        _print_stats()
