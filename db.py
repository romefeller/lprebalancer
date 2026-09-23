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


# --- accounting: writes ------------------------------------------------------

def open_position(mint, pool, pair, lower, upper, band_pct, sig,
                  deposit_usd, reason='', config_name=None):
    with cursor(commit=True) as cur:
        cur.execute("""
            insert into positions (mint, config_name, pool, pair_label, opened_at,
                                   lower_price, upper_price, band_pct, open_sig,
                                   deposit_usd, open_reason)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (mint) do update set
                pool = excluded.pool, pair_label = excluded.pair_label,
                lower_price = excluded.lower_price, upper_price = excluded.upper_price,
                band_pct = excluded.band_pct, open_sig = excluded.open_sig,
                deposit_usd = excluded.deposit_usd, open_reason = excluded.open_reason
        """, (mint, config_name, pool, pair, now(), lower, upper, band_pct, sig,
              deposit_usd, reason))


def close_position(mint, sig, withdraw_usd):
    with cursor(commit=True) as cur:
        cur.execute('update positions set closed_at = %s, close_sig = %s, '
                    'withdraw_usd = %s where mint = %s',
                    (now(), sig, withdraw_usd, mint))


def record_harvest(mint, fee_a, fee_b, fee_usd, sig):
    with cursor(commit=True) as cur:
        cur.execute('insert into harvests (ts, mint, fee_a, fee_b, fee_usd, signature) '
                    'values (%s,%s,%s,%s,%s,%s)',
                    (now(), mint, fee_a or 0, fee_b or 0, fee_usd or 0, sig))


def snapshot(mint, price, in_range, liquidity, accrued_a, accrued_b,
             accrued_usd, wallet_usd, position_usd):
    # Equity is the wallet plus the position. If either could not be read
    # this poll, equity is unknown — not "the other half", which would show up
    # as a $170 loss in the P&L for one poll and a $170 gain in the next.
    equity = ((wallet_usd + position_usd)
              if (wallet_usd is not None and position_usd is not None) else None)
    with cursor(commit=True) as cur:
        cur.execute("""
            insert into snapshots (ts, mint, price, in_range, liquidity, accrued_a,
                                   accrued_b, accrued_usd, wallet_usd, position_usd,
                                   equity_usd)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (now(), mint, price, bool(in_range), str(liquidity or 0),
              accrued_a, accrued_b, accrued_usd, wallet_usd, position_usd, equity))


def event(kind, detail=''):
    with cursor(commit=True) as cur:
        cur.execute('insert into events (ts, kind, detail) values (%s,%s,%s)',
                    (now(), kind, str(detail)[:2000]))


# --- accounting: reads -------------------------------------------------------

def _f(x):
    return float(x) if x is not None else 0.0


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

        cur.execute("select coalesce(sum(fee_a),0) a, coalesce(sum(fee_b),0) b, "
                    "coalesce(sum(fee_usd),0) usd from harvests "
                    "where ts >= date_trunc('day', now() at time zone 'utc')")
        rt = cur.fetchone()

        # The latest snapshot, and whether the position it describes still
        # exists. Unrealised fees are what is sitting in an OPEN position. A
        # snapshot of a position that has since been harvested and closed
        # describes money that is now in the wallet and already counted as
        # realised; reading it as unrealised too is how a $0.26 harvest was
        # reported as $0.51 of fees.
        cur.execute('select s.ts, s.mint, s.price, s.accrued_a, s.accrued_b, '
                    's.accrued_usd, s.equity_usd, '
                    '(p.mint is not null and p.closed_at is null) as open '
                    'from snapshots s left join positions p on p.mint = s.mint '
                    'order by s.id desc limit 1')
        latest = cur.fetchone()
        if latest and not latest['open']:
            latest = dict(latest, accrued_a=0, accrued_b=0, accrued_usd=0)

        # The first snapshot of today FOR THIS POSITION, so "today" counts what
        # accrued since midnight rather than everything the open position has
        # ever earned. Filtering by mint matters: after a rebalance the day's
        # first snapshot belongs to a position that no longer exists, and its
        # accrual has nothing to do with the new one's.
        cur.execute("select accrued_a, accrued_b, accrued_usd from snapshots "
                    "where ts >= date_trunc('day', now() at time zone 'utc') "
                    "and mint = %s order by id asc limit 1",
                    (latest['mint'] if latest else None,))
        day0 = cur.fetchone()

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
    started = first_eq and first_eq['equity_usd']

    du_a = max(u_a - _f(day0 and day0['accrued_a']), 0.0)
    du_b = max(u_b - _f(day0 and day0['accrued_b']), 0.0)
    du_usd = max(u_usd - _f(day0 and day0['accrued_usd']), 0.0)

    days = None
    if latest and span and span['t0']:
        days = max((latest['ts'] - span['t0']).total_seconds() / 86400, 1e-9)

    total_usd = _f(r['usd']) + u_usd
    rate = total_usd / days if (days and days >= MIN_RATE_DAYS) else None
    # Annualised on the capital actually at work, not on notional.
    apr = (rate / float(equity) * 365 * 100) if (rate and equity) else None

    rnd = lambda x, d=6: round(_f(x), d)
    return {
        'pair': cfg.get('pair_label'),
        'token_a': token_a or cfg.get('token_a') or 'A',
        'token_b': token_b or cfg.get('token_b') or 'B',
        'fees_today_a': rnd(_f(rt['a']) + du_a),
        'fees_today_b': rnd(_f(rt['b']) + du_b),
        'fees_today_usd': round(_f(rt['usd']) + du_usd, 4),
        'fees_realised_a': rnd(r['a']), 'fees_realised_b': rnd(r['b']),
        'fees_realised_usd': round(_f(r['usd']), 4),
        'fees_unrealised_a': rnd(u_a), 'fees_unrealised_b': rnd(u_b),
        'fees_unrealised_usd': round(u_usd, 4),
        'fees_total_a': rnd(_f(r['a']) + u_a),
        'fees_total_b': rnd(_f(r['b']) + u_b),
        'fees_total_usd': round(total_usd, 4),
        'fees_per_day_usd': round(rate, 4) if rate else None,
        'apr_pct': round(apr, 2) if apr else None,
        'harvests': r['n'],
        'positions_opened': pos['total'], 'positions_open_now': pos['open_now'],
        'rebands': ev['rebands'], 'failures': ev['failures'],
        'equity_usd': round(_f(equity), 2) if equity is not None else None,
        'equity_start_usd': round(_f(started), 2) if started is not None else None,
        'pnl_usd': (round(_f(equity) - _f(started), 2)
                    if (equity is not None and started is not None) else None),
        'in_range_pct': (round(_f(span['ir']) * 100, 1)
                         if span and span['ir'] is not None else None),
        'tracked_days': round(days, 3) if days else None,
        'last_price': _f(latest and latest['price']) or None,
        'last_seen': latest['ts'].isoformat() if latest else None,
    }


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
            with pts as (
                -- every snapshot, marked with the position's total to date
                select s.ts, s.mint,
                       s.accrued_a + coalesce((select sum(h.fee_a) from harvests h
                                               where h.mint = s.mint and h.ts <= s.ts), 0) a,
                       s.accrued_b + coalesce((select sum(h.fee_b) from harvests h
                                               where h.mint = s.mint and h.ts <= s.ts), 0) b,
                       s.accrued_usd + coalesce((select sum(h.fee_usd) from harvests h
                                                 where h.mint = s.mint and h.ts <= s.ts), 0) usd
                from snapshots s
                union all
                -- and every harvest, as the moment accrual became realised
                select h.ts, h.mint,
                       (select sum(fee_a) from harvests x where x.mint = h.mint and x.ts <= h.ts),
                       (select sum(fee_b) from harvests x where x.mint = h.mint and x.ts <= h.ts),
                       (select sum(fee_usd) from harvests x where x.mint = h.mint and x.ts <= h.ts)
                from harvests h
                union all
                -- and every open, at zero: what accrued before the first
                -- snapshot is still the day's earning
                select p.opened_at, p.mint, 0, 0, 0 from positions p
            ), per_mint as (
                select date_trunc('day', ts) d, mint,
                       max(a) - min(a) a, max(b) - min(b) b, max(usd) - min(usd) usd
                from pts group by 1, 2
            ), f as (
                select d, sum(a) a, sum(b) b, sum(usd) usd from per_mint group by 1
            ), s as (
                select date_trunc('day', ts) d,
                       avg(case when in_range then 1.0 else 0.0 end) ir,
                       max(equity_usd) eq
                from snapshots group by 1
            )
            select coalesce(f.d, s.d)::date as day,
                   coalesce(f.a, 0) fee_a, coalesce(f.b, 0) fee_b, coalesce(f.usd, 0) fee_usd,
                   s.ir in_range, s.eq equity_usd
            from f full outer join s on f.d = s.d
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


def describe_pool(pool):
    """Ask Orca what this pool is. Refuses the one kind the signer cannot open."""
    import urllib.error, urllib.request
    req = urllib.request.Request(f'{ORCA}/pools/{pool}',
                                 headers={'accept': 'application/json',
                                          'user-agent': 'Mozilla/5.0'})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except (urllib.error.URLError, ValueError) as e:
        raise SystemExit(f'Orca does not know a pool at {pool} ({e}). Nothing was added.')
    d = d.get('data') or d
    if not d.get('tokenA'):
        raise SystemExit(f'Orca does not know a pool at {pool}. Nothing was added.')
    a, b = d['tokenA']['symbol'], d['tokenB']['symbol']
    if d.get('adaptiveFeeEnabled'):
        raise SystemExit(
            f'{a}/{b} is an adaptive-fee pool. The signer cannot open positions on '
            'it (Whirlpool error 6069, which reads like slippage and is not). '
            'Nothing was added.')
    return {'pair_label': f'{a}/{b}', 'token_a': a, 'token_b': b,
            'fee_rate': int(d.get('feeRate') or 0) / 1e6,
            'tvl_usd': float(d.get('tvlUsdc') or 0), 'price': float(d['price'])}


def add(name, pool, active=False, **params):
    """Describe a new pool to the bot. Everything but the address and the size
    comes from the pool itself or from the column defaults.

        python3 db.py add wif-usdc <pool> capital_usd=200 max_usd=300
    """
    info = describe_pool(pool)
    p = dict(name=name, pool=pool, active=active,
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
    print(f"{s['pair'] or '-'}   {s['positions_open_now']} open   "
          f"in range {s['in_range_pct']}%   over {s['tracked_days']}d")
    print('FEES')
    w('today', 'fees_today_a', 'fees_today_b', 'fees_today_usd')
    w('realised', 'fees_realised_a', 'fees_realised_b', 'fees_realised_usd')
    w('unrealised', 'fees_unrealised_a', 'fees_unrealised_b', 'fees_unrealised_usd')
    w('TOTAL', 'fees_total_a', 'fees_total_b', 'fees_total_usd')
    print('BOOK')
    print(f"  equity      ${s['equity_usd']}   P&L {s['pnl_usd']}")
    print(f"  rate        ${s['fees_per_day_usd']}/day   APR {s['apr_pct']}%")
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
    elif cmd == 'daily':
        for d in reversed(daily()):
            print(f"{d['day']}  {float(d['fee_a']):.6f} A  {float(d['fee_b']):.6f} B  "
                  f"${float(d['fee_usd']):.4f}  in range "
                  f"{(float(d['in_range'] or 0) * 100):.0f}%")
    elif cmd == 'history':
        for r in reversed(history()):
            print(f"{r['ts']:%Y-%m-%d %H:%M}  price {float(r['price'] or 0):>9.4f}  "
                  f"in_range {r['in_range']!s:<5}  accrued "
                  f"${float(r['accrued_usd'] or 0):.4f}  "
                  f"equity ${float(r['equity_usd'] or 0):.2f}")
    elif cmd == 'json':
        print(json.dumps(stats(), indent=1, default=str))
    else:
        _print_stats()
