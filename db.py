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
    python3 db.py [stats|daily|history|json]    the book (this process's or every enabled profile's)
    python3 db.py stats --pool P --wallet W     one profile's book, or one wallet's (stats.py: all, summed)
                                                W: a wallet id, or its address's first 10 characters
    python3 db.py forecasts                     the survival model against the tape

Connections are opened per operation. At one write every few minutes the cost
is nothing, and it means a dropped connection cannot wedge the loop.
"""
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
import datetime as dt

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


# --- who is writing ----------------------------------------------------------
# One process runs one profile (config.py sets this at import). Rows the
# process writes carry the profile and its wallet; per-process state that
# lives in shared tables (breakers, audit cursors) is keyed by them, so two
# profiles never read each other's breaker or cursor. Empty until set: a
# tool that imports db without config writes unscoped rows, as before 020.
CONTEXT = {'profile': None, 'wallet_id': None}


def set_context(profile, wallet_id):
    """Scope this process's writes to `profile` on `wallet_id`."""
    if not (isinstance(profile, str) and re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', profile)):
        raise ValueError(f'bad profile name {profile!r}')
    if wallet_id is not None and not (isinstance(wallet_id, str)
                                      and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,40}', wallet_id)):
        raise ValueError(f'bad wallet id {wallet_id!r}')
    CONTEXT.update(profile=profile, wallet_id=wallet_id)


def scoped(key, by='profile'):
    """`key` prefixed with this process's profile (or wallet), when one is set:
    the shared key-value tables hold one row per process, not one in all."""
    owner = CONTEXT.get(by)
    return f'{owner}|{key}' if owner else key


def unscoped(key, by='profile'):
    """`key` without this process's prefix, or None when it is another's. Pure."""
    owner = CONTEXT.get(by)
    if not owner:
        return None if '|' in key else key
    pre = f'{owner}|'
    return key[len(pre):] if key.startswith(pre) else None


# --- wallets and profiles ----------------------------------------------------

def wallet_row(wallet_id):
    """The wallets row `wallet_id`, or None. Holds no secret."""
    with cursor() as cur:
        cur.execute('select id, chain, address, secret_env, label from wallets where id = %s', (wallet_id,))
        r = cur.fetchone()
    return dict(r) if r else None


def wallets():
    with cursor() as cur:
        cur.execute('select id, chain, address, secret_env, label from wallets order by id')
        return [dict(r) for r in cur.fetchall()]


# A wallet is named in every report by its id and by the first WALLET_TAG_LEN
# characters of its public address (owner, 2026-10-02): two wallets on one
# chain are told apart by what the chain shows, not only by a name.
WALLET_TAG_LEN = 10


def wallet_tag(address):
    """The first WALLET_TAG_LEN characters of `address`, or None without one. Pure."""
    return str(address or '').strip()[:WALLET_TAG_LEN] or None


def _address_starts(address, key):
    """Whether `address` starts with `key`: an EVM address (0x...) in any
    case, any other (base58) in its own case. Pure."""
    a = str(address or '')
    if a[:2].lower() == '0x':
        return a.lower().startswith(key.lower())
    return a.startswith(key)


def resolve_wallet(key, rows):
    """The wallet id that `key` names, from the wallets rows `rows`: an id as
    it is; else the one wallet whose address starts with `key`, a key of at
    least WALLET_TAG_LEN characters (the tag, or the whole address). A key that
    matches nothing is returned as it is: it names no book. Raises ValueError
    when several wallets match. Pure."""
    if key is None or any(r.get('id') == key for r in rows) or len(key) < WALLET_TAG_LEN:
        return key
    hits = sorted({r['id'] for r in rows if _address_starts(r.get('address'), key)})
    if len(hits) > 1:
        raise ValueError(f'wallet {key!r} matches several wallets: {", ".join(hits)}')
    return hits[0] if hits else key


def profiles(wallet_id=None, enabled_only=True):
    """Profiles (config rows), optionally of one wallet, enabled ones by default."""
    with cursor() as cur:
        cur.execute('select * from config where (%s::text is null or wallet_id = %s) '
                    'and (not %s or enabled) order by name', (wallet_id, wallet_id, enabled_only))
        return [dict(r) for r in cur.fetchall()]


# --- whose book: the scope of a report ------------------------------------------
# Every report reads the rows of a set of profiles. Money rows find their
# profile through the position they belong to (positions.config_name); payouts
# carry config_name; flows and events carry profile. Rows from before 020 carry
# no profile (and one position carries no config_name): they are the book of
# the one profile that existed then, LEGACY_PROFILE on LEGACY_WALLET. That
# attribution is done here, in every query, so the book is right whether or not
# the deploy backfilled those NULLs.
LEGACY_PROFILE = 'sol-usdc'
LEGACY_WALLET = 'sol-lp'

POS_IN = 'coalesce(p.config_name, %(legacy)s) = any(%(names)s::text[])'
PAYOUT_IN = 'coalesce(config_name, %(legacy)s) = any(%(names)s::text[])'
PROFILE_IN = 'coalesce(profile, %(legacy)s) = any(%(names)s::text[])'
# A flow carries its wallet: one with no profile is the legacy book's only when
# it is the legacy wallet's (or names no wallet, before 020), never another's.
FLOW_IN = ('coalesce(profile, case when coalesce(wallet_id, %(legacy_wallet)s) = %(legacy_wallet)s '
           'then %(legacy)s end) = any(%(names)s::text[])')


def mint_in(col):
    """SQL: the row whose position mint is `col` belongs to the scope. A mint
    with no positions row is the legacy book's."""
    return (f'coalesce((select x.config_name from positions x where x.mint = {col}), %(legacy)s) '
            '= any(%(names)s::text[])')


def _scope_args(names, **kw):
    return {'names': list(names), 'legacy': LEGACY_PROFILE, 'legacy_wallet': LEGACY_WALLET, **kw}


def book_profiles(wallet_id=None, include_disabled=False):
    """Config rows with two derived keys: `wallet` (the wallet id; the legacy
    wallet when the row names none) and `on` (whether the profile runs:
    `enabled`, or, while no row is enabled yet (before the 020 deploy), the
    one `active` row). Enabled ones only, unless include_disabled."""
    with cursor() as cur:
        cur.execute('select * from config order by name')
        rows = [dict(r) for r in cur.fetchall()]
    any_enabled = any(r.get('enabled') for r in rows)
    out = []
    for r in rows:
        r['wallet'] = r.get('wallet_id') or LEGACY_WALLET
        r['on'] = bool(r.get('enabled')) if any_enabled else bool(r.get('active'))
        if (wallet_id is None or r['wallet'] == wallet_id) and (include_disabled or r['on']):
            out.append(r)
    return out


def book_scope(profile=None, wallet_id=None):
    """The profile names a report covers. With neither filter: this
    process's own profile (CONTEXT) when it has one, so a process's books show
    only its pool; else every enabled profile. A profile with a wallet it does
    not belong to covers nothing. With no profile running at all (a pre-020
    database with no active row), the legacy book."""
    if profile is None and wallet_id is None:
        profile = CONTEXT.get('profile')
    if profile is not None:
        if wallet_id is not None:
            rows = [r for r in book_profiles(include_disabled=True) if r['name'] == profile]
            mine = rows[0]['wallet'] if rows else LEGACY_WALLET
            if mine != wallet_id:
                return []
        return [profile]
    names = [r['name'] for r in book_profiles(wallet_id)]
    if not names and wallet_id is None:
        return [LEGACY_PROFILE]
    return names


def open_by_profile():
    """{profile: positions open now}, legacy rows attributed as everywhere."""
    with cursor() as cur:
        cur.execute('select coalesce(config_name, %s) name, count(*) n from positions '
                    'where closed_at is null group by 1', (LEGACY_PROFILE,))
        return {r['name']: int(r['n']) for r in cur.fetchall()}


def flow_totals(profile=None, wallet_id=None):
    """Deposits and withdrawals the flows audit recorded for the scope, in
    dollars at the time of each flow, with their counts."""
    with cursor() as cur:
        cur.execute(f"""select coalesce(sum(usd) filter (where kind = 'deposit'), 0) dep,
                               count(*) filter (where kind = 'deposit') ndep,
                               coalesce(sum(usd) filter (where kind = 'withdrawal'), 0) wd,
                               count(*) filter (where kind = 'withdrawal') nwd
                        from capital_flows where kind in ('deposit', 'withdrawal') and {FLOW_IN}""",
                    _scope_args(book_scope(profile, wallet_id)))
        r = cur.fetchone()
    return {'deposits_usd': round(_f(r['dep']), 4), 'deposits': int(r['ndep']),
            'withdrawals_usd': round(_f(r['wd']), 4), 'withdrawals': int(r['nwd'])}


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


def tape_ref_pool(native_mint, stable_mints, not_pool):
    """The pool of a profile whose token A is `native_mint` and token B a
    stablecoin, other than `not_pool`: one that trades every five minutes,
    GeckoTerminal's canary for the quiet-pool fill. None when there is none."""
    with cursor() as cur:
        cur.execute("""select pool from config where mints[1] = %s and mints[2] = any(%s) and pool <> %s
                        order by enabled desc, name limit 1""", (native_mint, list(stable_mints), not_pool))
        r = cur.fetchone()
    return r['pool'] if r else None


def config_pools():
    """Every profile's pool, enabled or not: tapes that some process reads."""
    with cursor() as cur:
        cur.execute('select distinct pool from config')
        return {r['pool'] for r in cur.fetchall()}


def tape_prune_other_pools(keep_pools, older_than_ts):
    """Drop tapes of pools no longer held once they are stale."""
    with cursor(commit=True) as cur:
        cur.execute('delete from tape5 where pool <> all(%s) and ts < %s', (list(keep_pools), int(older_than_ts)))


FEE_GROWTH_KEEP_DAYS = 14


def guard_fee_rows(profile, pool, since):
    """The fee/variance guard's inputs since `since` (a datetime): this
    profile's snapshots on `pool` as (epoch ts, mint, accrued_usd, liquidity,
    in_range, price) ordered by mint then ts, and the harvests of those
    positions as (epoch ts, mint, fee_usd)."""
    with cursor() as cur:
        cur.execute("""select extract(epoch from s.ts)::float t, s.mint, s.accrued_usd::float f,
                              s.liquidity::float l, s.in_range, s.price::float p
                       from snapshots s join positions x on x.mint = s.mint
                       where x.config_name = %s and x.pool = %s and s.ts >= %s
                       order by s.mint, s.ts, s.id""", (profile, pool, since))
        rows = [(r['t'], r['mint'], r['f'], r['l'], r['in_range'], r['p']) for r in cur.fetchall()]
        cur.execute("""select extract(epoch from h.ts)::float t, h.mint, h.fee_usd::float f
                       from harvests h join positions x on x.mint = h.mint
                       where x.config_name = %s and x.pool = %s and h.ts >= %s""", (profile, pool, since))
        harv = [(r['t'], r['mint'], r['f']) for r in cur.fetchall()]
    return rows, harv


def record_fee_state(dex, pool, st):
    """One sample of a pool's counters; samples older than FEE_GROWTH_KEEP_DAYS
    go. Two weeks, not two days: the fee/variance guard's backtest needs on-chain
    fees over many days, and two days of them could not decide it (2026-10-03)."""
    with cursor(commit=True) as cur:
        cur.execute('insert into fee_growth (ts, dex, pool, sqrt_price, g0, g1, rewards, dec_a, dec_b, mint_a, mint_b) '
                    'values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (now(), dex, pool, st['sqrt_price'], st['g0'], st['g1'], json.dumps(st.get('rewards') or []),
                     st['dec_a'], st['dec_b'], st['mint_a'], st['mint_b']))
        cur.execute('delete from fee_growth where ts < now() - make_interval(days => %s)', (FEE_GROWTH_KEEP_DAYS,))


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


RISK_COLUMNS = ('mode', 'choice_pct', 'held_pct', 'inside', 'p_held', 'threshold', 'threshold_base',
                'horizon_min', 'stale', 'bar_age_s', 'probs',
                'sigma_5m_pct', 'velocity', 'instability', 'rms_1h_pct', 'rms_6h_pct', 'rms_24h_pct',
                'vol_ratio_1h_24h', 'park_1h_pct', 'vol_of_vol_24h', 'acf_r2_lag1_24h', 'arch_lm_24h',
                'arch_lm_p_24h', 'kurtosis_24h', 'n_bars',
                'sigma_24h_pct', 'vol_regime_x', 'p_exit_6h', 'p_exit_24h', 'p_exit_72h', 'p_exit_168h',
                'band_position', 'liquidity_factor', 'inflow', 'volume_x')


def risk_row(regime, metrics, forecast):
    """One risk_profile row from the regime view, calm.risk_metrics and the
    hourly forecast. Pure; unknown figures are None."""
    v, m, f = regime or {}, metrics or {}, forecast or {}
    lq = v.get('liquidity') or {}
    row = {'mode': v.get('mode'), 'choice_pct': v.get('choice_pct'), 'held_pct': v.get('held_pct'),
           'inside': v.get('inside'), 'p_held': v.get('p_held'), 'threshold': v.get('threshold'),
           'threshold_base': v.get('threshold_base'), 'horizon_min': v.get('horizon_minutes'),
           'stale': bool(v.get('stale')), 'bar_age_s': v.get('bar_age_s'),
           'probs': json.dumps(v['probs']) if v.get('probs') is not None else None,
           'sigma_5m_pct': v.get('sigma_5m_pct'), 'velocity': v.get('velocity'),
           'instability': v.get('instability'),
           'sigma_24h_pct': f.get('sigma_24h_pct'), 'vol_regime_x': f.get('vol_regime_x'),
           'band_position': f.get('position'),
           'liquidity_factor': lq.get('factor'), 'inflow': lq.get('inflow'), 'volume_x': lq.get('volume_x')}
    for h in (6, 24, 72, 168):
        row[f'p_exit_{h}h'] = f.get(f'p_exit_{h}h_regime', f.get(f'p_exit_{h}h'))
    for k in ('rms_1h_pct', 'rms_6h_pct', 'rms_24h_pct', 'vol_ratio_1h_24h', 'park_1h_pct', 'vol_of_vol_24h',
              'acf_r2_lag1_24h', 'arch_lm_24h', 'arch_lm_p_24h', 'kurtosis_24h', 'n_bars'):
        row[k] = m.get(k)
    return row


def record_risk_profile(pool, mint, price, regime, metrics, forecast):
    row = risk_row(regime, metrics, forecast)
    cols = ('ts', 'pool', 'mint', 'price') + RISK_COLUMNS
    vals = (now(), pool, mint, price) + tuple(row[c] for c in RISK_COLUMNS)
    with cursor(commit=True) as cur:
        cur.execute(f"insert into risk_profile ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})", vals)
        cur.execute("delete from risk_profile where ts < now() - interval '30 days'")


# risk_profile column -> band_profile mean column
BAND_MEANS = {'sigma_5m_pct': 'sigma_mean', 'velocity': 'velocity_mean', 'instability': 'instability_mean',
              'vol_of_vol_24h': 'vol_of_vol_mean', 'arch_lm_24h': 'arch_lm_mean', 'arch_lm_p_24h': 'arch_lm_p_mean',
              'acf_r2_lag1_24h': 'acf_r2_mean', 'kurtosis_24h': 'kurtosis_mean', 'vol_ratio_1h_24h': 'vol_ratio_mean',
              'rms_1h_pct': 'rms_1h_mean', 'rms_24h_pct': 'rms_24h_mean', 'park_1h_pct': 'park_1h_mean',
              'liquidity_factor': 'liquidity_factor_mean', 'volume_x': 'volume_x_mean'}
# risk_profile column -> band_profile column, read at the first poll of the band
BAND_OPEN = {'mode': 'mode_open', 'sigma_5m_pct': 'sigma_open', 'velocity': 'velocity_open',
             'instability': 'instability_open', 'arch_lm_p_24h': 'arch_lm_p_open', 'vol_ratio_1h_24h': 'vol_ratio_open',
             'choice_pct': 'choice_pct_open', 'p_held': 'p_held_open', 'p_exit_6h': 'p_exit_6h_open'}
BAND_CLOSE = {'mode': 'mode_close', 'sigma_5m_pct': 'sigma_close', 'velocity': 'velocity_close'}


def record_band_profile(mint, event, reason=None, at=None):
    """Write the one band_profile row of `mint` (sql/017): at a harvest the
    running figures, at the rebalance that ends the band the final ones, with
    the time of that rebalance. Every figure is derived from risk_profile,
    snapshots and harvests, so a second write of the same state gives the same
    row. Returns the row, or None when the position is unknown."""
    if event not in ('harvest', 'rebalance'):
        raise ValueError(f'event must be harvest or rebalance, not {event!r}')
    at = at or now()
    means = ', '.join(f'avg(r.{k}) {v}' for k, v in BAND_MEANS.items())
    opens = ', '.join(f'o.{k} {v}' for k, v in BAND_OPEN.items())
    closes = ', '.join(f'c.{k} {v}' for k, v in BAND_CLOSE.items())
    cols = (['mint', 'updated_at', 'last_event', 'final', 'rebalanced_at', 'exit_reason', 'survived_hours',
             'polls', 'in_range_share', 'mode_main', 'mode_share', 'sigma_max', 'velocity_abs_mean',
             'fees_a', 'fees_b', 'fees_usd', 'harvests', 'fees_per_hour_usd']
            + list(BAND_MEANS.values()) + list(BAND_OPEN.values()) + list(BAND_CLOSE.values()))
    keep = {'mint', 'rebalanced_at', 'exit_reason'}
    updates = ', '.join(f'{c} = excluded.{c}' for c in cols if c not in keep)
    sql = f"""
        with p as (
            select mint, opened_at, coalesce(closed_at, %(at)s) end_at, closed_at is not null closed
            from positions where mint = %(mint)s
        ), r as (
            select r.* from risk_profile r, p where r.mint = p.mint and r.ts between p.opened_at and p.end_at
        ), o as (select * from r order by ts asc, id asc limit 1),
           c as (select * from r order by ts desc, id desc limit 1),
           m as (select mode, count(*) n from r where mode is not null group by mode),
           agg as (select count(*) polls, max(sigma_5m_pct) sigma_max, avg(abs(velocity)) velocity_abs_mean,
                          {means} from r),
           s as (select avg(case when in_range then 1.0 else 0.0 end) in_range_share
                 from snapshots sn, p where sn.mint = p.mint and sn.ts between p.opened_at and p.end_at),
           h as (select coalesce(sum(fee_a), 0) fees_a, coalesce(sum(fee_b), 0) fees_b,
                        coalesce(sum(fee_usd), 0) fees_usd, count(*) harvests
                 from harvests where mint = %(mint)s),
           band as (
            select p.mint, now() updated_at, %(event)s last_event,
                   (%(event)s = 'rebalance' and p.closed) final,
                   case when %(event)s = 'rebalance' then %(at)s end rebalanced_at,
                   %(reason)s exit_reason,
                   extract(epoch from (p.end_at - p.opened_at)) / 3600.0 survived_hours,
                   agg.polls, s.in_range_share,
                   (select mode from m order by n desc, mode limit 1) mode_main,
                   (select jsonb_object_agg(mode, round(n::numeric / nullif(agg.polls, 0), 4)) from m) mode_share,
                   agg.sigma_max, agg.velocity_abs_mean,
                   h.fees_a, h.fees_b, h.fees_usd, h.harvests,
                   h.fees_usd / nullif(extract(epoch from (p.end_at - p.opened_at)) / 3600.0, 0) fees_per_hour_usd,
                   {', '.join('agg.' + v for v in BAND_MEANS.values())},
                   {opens}, {closes}
            from p cross join agg cross join s cross join h
                 left join o on true left join c on true
           )
        insert into band_profile ({', '.join(cols)})
        select {', '.join(cols)} from band
        on conflict (mint) do update set {updates},
            rebalanced_at = coalesce(band_profile.rebalanced_at, excluded.rebalanced_at),
            exit_reason = coalesce(band_profile.exit_reason, excluded.exit_reason)   -- the rebalance that ended it
        returning *
    """
    with cursor(commit=True) as cur:
        cur.execute(sql, {'mint': mint, 'event': event, 'reason': reason, 'at': at})
        r = cur.fetchone()
    return dict(r) if r else None


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


def native_price(native_mint, stable_mints, max_age_s):
    """USD per native token from the newest snapshot, under `max_age_s` old,
    of a pool whose token A is `native_mint` and token B a stablecoin (its
    UI price is then dollars per native unit), or None. The config's mints
    are its pool's now: a profile that moves between pools (sol-swing:
    SOL/USDC, then DJT/USDC) has positions on other pools whose price is not
    the native token's, so only positions on the profile's pool count."""
    with cursor() as cur:
        cur.execute("""select s.price from snapshots s
                         join positions p on p.mint = s.mint
                         join config c on c.name = p.config_name and c.pool = p.pool
                        where c.mints[1] = %s and c.mints[2] = any(%s) and s.price > 0
                          and s.ts > now() - make_interval(secs => %s)
                        order by s.ts desc limit 1""", (native_mint, list(stable_mints), float(max_age_s)))
        r = cur.fetchone()
    return float(r['price']) if r else None


def last_fees(mint):
    """The accrued fees at the last snapshot of `mint`, and its age in hours;
    None when there is none. The yardstick of guards.fee_read_problem."""
    with cursor() as cur:
        cur.execute("""select accrued_a, accrued_b, accrued_usd,
                              extract(epoch from (now() - ts)) / 3600.0 hours
                       from snapshots where mint = %s order by ts desc, id desc limit 1""", (mint,))
        r = cur.fetchone()
    if not r:
        return None
    return {k: (float(r[k]) if r[k] is not None else None) for k in ('accrued_a', 'accrued_b', 'accrued_usd', 'hours')}


def record_payout(config_name, position, token_mint, symbol, amount, usd, kind,
                  to_address=None, signature=None, detail=None):
    with cursor(commit=True) as cur:
        cur.execute('insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, '
                    'kind, to_address, signature, detail) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (now(), config_name, position, token_mint, symbol, amount, usd, kind,
                     to_address, signature, detail))


def reinvested_usd(profile=None, wallet_id=None):
    """Dollars of fees reinvested so far: they raise the sizing base."""
    with cursor() as cur:
        cur.execute("select coalesce(sum(usd), 0) usd from payouts "
                    f"where {PAYOUT_IN} and kind = 'reinvested'", _scope_args(book_scope(profile, wallet_id)))
        return float(cur.fetchone()['usd'])


def payout_totals(profile=None, wallet_id=None):
    """Fee split so far, in dollars, by kind; and today's payout."""
    with cursor() as cur:
        cur.execute(f"""
            select kind, coalesce(sum(usd), 0) usd, count(*) n,
                   coalesce(sum(usd) filter (where ts >= date_trunc('day', now() at time zone 'utc')), 0) today
            from payouts where {PAYOUT_IN} group by kind
        """, _scope_args(book_scope(profile, wallet_id)))
        rows = {r['kind']: r for r in cur.fetchall()}
    g = lambda k, f='usd': round(float(rows[k][f]), 4) if k in rows else 0.0
    return {'paid_usd': g('paid'), 'paid_today_usd': g('paid', 'today'),
            'reinvested_usd': g('reinvested'), 'gas_usd': g('gas'),
            'payouts': int(rows['paid']['n']) if 'paid' in rows else 0}


# --- circuit breakers (health.py) ---------------------------------------------

_HEALTH_COLS = ('key', 'fails', 'trips', 'last_fail', 'last_ok', 'retry_at', 'last_error')


def health_get(key):
    """The breaker record of `key`, or None."""
    with cursor() as cur:
        cur.execute('select key, fails, trips, last_fail, last_ok, retry_at, last_error from health where key = %s',
                    (scoped(key),))
        r = cur.fetchone()
    return dict(r, key=key) if r else None


def health_put(rec):
    """Upsert one breaker record."""
    vals = [scoped(rec.get('key')) if c == 'key' else rec.get(c) for c in _HEALTH_COLS]
    with cursor(commit=True) as cur:
        cur.execute("""insert into health (key, fails, trips, last_fail, last_ok, retry_at, last_error, updated)
                       values (%s,%s,%s,%s,%s,%s,%s, now())
                       on conflict (key) do update set fails = excluded.fails, trips = excluded.trips,
                           last_fail = excluded.last_fail, last_ok = excluded.last_ok,
                           retry_at = excluded.retry_at, last_error = excluded.last_error, updated = now()""", vals)


def health_all():
    with cursor() as cur:
        cur.execute('select key, fails, trips, last_fail, last_ok, retry_at, last_error from health order by key')
        rows = [dict(r) for r in cur.fetchall()]
    out = []
    for r in rows:
        k = unscoped(r['key'])
        if k is not None:
            out.append(dict(r, key=k))
    return out


_SECRET_IN_URL = re.compile(r'(api[-_]?key=)[^&\s"\'<>]+', re.I)


def event(kind, detail=''):
    detail = _SECRET_IN_URL.sub(r'\1***', str(detail))      # the keyed RPC URL never reaches the table
    with cursor(commit=True) as cur:
        cur.execute('insert into events (ts, kind, detail, profile) values (%s,%s,%s,%s)',
                    (now(), kind, detail[:2000], CONTEXT.get('profile')))


# --- accounting: reads -------------------------------------------------------

def _f(x):
    return float(x) if x is not None else 0.0


def fees_between(start, end=None, profile=None, wallet_id=None):
    """Accrual earned between two UTC boundaries, including closed positions.

    The latest observation before the start is the baseline, so collecting
    yesterday's fees today cannot count them as today's earnings.
    """
    with cursor() as cur:
        cur.execute(f"""
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
                from fee_points where ts <= %(end)s and {mint_in('fee_points.mint')} group by mint
            )
            select coalesce(sum(a), 0) a, coalesce(sum(b), 0) b,
                   coalesce(sum(usd), 0) usd from per_mint
        """, _scope_args(book_scope(profile, wallet_id), start=start, end=end or now()))
        return dict(cur.fetchone())


def trailing_rate(hours, profile=None, wallet_id=None):
    """Fees earned per day over the last `hours`, from the ledger's own
    series: for every position, fees to date are its unharvested accrual plus
    what was harvested from it, so the window's earning is the rise of that
    sum, per position, summed. Realised does not move it, a rebalance does not
    reset it. None when the window has fewer than two points."""
    with cursor() as cur:
        cur.execute(f"""
            with pts as (
                select s.ts, s.mint,
                       s.accrued_usd + coalesce((select sum(h.fee_usd) from harvests h
                                                 where h.mint = s.mint and h.ts <= s.ts), 0) usd
                from snapshots s
                where s.ts >= now() - make_interval(hours => %(hours)s) - interval '10 minutes'
                  and {mint_in('s.mint')}
                union all
                select h.ts, h.mint,
                       (select sum(fee_usd) from harvests x where x.mint = h.mint and x.ts <= h.ts)
                from harvests h where h.ts >= now() - make_interval(hours => %(hours)s) - interval '10 minutes'
                  and {mint_in('h.mint')}
                union all
                select p.opened_at, p.mint, 0 from positions p
                where p.opened_at >= now() - make_interval(hours => %(hours)s) - interval '10 minutes'
                  and {POS_IN}
            ), per_mint as (
                select mint, max(usd) - min(usd) usd, min(ts) t0, max(ts) t1 from pts group by mint
            )
            select coalesce(sum(usd), 0) usd, min(t0) t0, max(t1) t1, count(*) n from per_mint
        """, _scope_args(book_scope(profile, wallet_id), hours=hours))
        r = cur.fetchone()
    if not r or not r['n'] or r['t0'] is None or r['t1'] is None:
        return None
    span_days = (r['t1'] - r['t0']).total_seconds() / 86400
    if span_days < MIN_RATE_DAYS:
        return None
    return {'fees_usd': round(_f(r['usd']), 4), 'days': round(span_days, 3),
            'fees_per_day_usd': round(_f(r['usd']) / span_days, 4)}


def by_pool(profile=None, wallet_id=None):
    """The book split by pool, and therefore by DEX: for every pool the bot
    has ever held, how long it was there, what it earned there realised and
    unrealised, and how often it was in range. The bot moves between DEXes
    now, so a single running total says nothing about which venue paid.
    One row per profile and pool, keyed on the position's pool, not the
    profile's pool now: a profile that moves between pools (sol-swing) has a
    row for each, and two profiles (two wallets) on one pool have one each.
    Token amounts (realised_a ... fees_b) are the pool's own pair's."""
    with cursor() as cur:
        cur.execute(f"""
            with per_pos as (
                select p.mint, coalesce(p.config_name, %(legacy)s) profile, p.dex, p.pool, p.pair_label,
                       p.opened_at, p.closed_at,
                       coalesce((select sum(h.fee_usd) from harvests h where h.mint = p.mint), 0) realised_usd,
                       case when p.closed_at is null then coalesce(
                           (select s.accrued_usd from snapshots s where s.mint = p.mint
                            order by s.id desc limit 1), 0) else 0 end unrealised_usd,
                       -- the pool's own tokens: one pool, one pair, so they add
                       coalesce((select sum(h.fee_a) from harvests h where h.mint = p.mint), 0) realised_a,
                       coalesce((select sum(h.fee_b) from harvests h where h.mint = p.mint), 0) realised_b,
                       case when p.closed_at is null then coalesce(
                           (select s.accrued_a from snapshots s where s.mint = p.mint
                            order by s.id desc limit 1), 0) else 0 end unrealised_a,
                       case when p.closed_at is null then coalesce(
                           (select s.accrued_b from snapshots s where s.mint = p.mint
                            order by s.id desc limit 1), 0) else 0 end unrealised_b,
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
                from positions p where {POS_IN}
            )
            select profile, dex, pool, pair_label, count(*) positions,
                   count(*) filter (where closed_at is null) open_now,
                   sum(days) days, sum(realised_usd) realised_usd, sum(unrealised_usd) unrealised_usd,
                   sum(realised_a) realised_a, sum(realised_b) realised_b,
                   sum(unrealised_a) unrealised_a, sum(unrealised_b) unrealised_b,
                   avg(in_range) in_range, min(opened_at) first_opened,
                   max(coalesce(closed_at, now())) last_seen,
                   max(equity_usd) filter (where closed_at is null) equity_usd,
                   sum(deposit_usd) deposit_usd,
                   -- position P&L: out - in, over positions where both are known
                   sum(out_usd - deposit_usd) filter (where out_usd is not null and deposit_usd is not null) position_pnl_usd,
                   count(*) filter (where out_usd is null or deposit_usd is null) unpriced,
                   sum(deposit_usd * days) / nullif(sum(days), 0) avg_deposit_usd
            from per_pos
            group by 1, 2, 3, 4
            order by max(opened_at) desc
        """, _scope_args(book_scope(profile, wallet_id)))
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
                     **{k: round(_f(d[k]), 6) for k in ('realised_a', 'realised_b', 'unrealised_a', 'unrealised_b')},
                     fees_a=round(_f(d['realised_a']) + _f(d['unrealised_a']), 6),
                     fees_b=round(_f(d['realised_b']) + _f(d['unrealised_b']), 6),
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


def _paid_usd(profile):
    try:
        return float(payout_totals(profile)['paid_usd'])
    except Exception:
        return 0.0


def daily_line(day, profile=None, wallet_id=None):
    """One UTC day of the book: re-centres (bands opened), fees harvested, and
    the value of the book (equity plus what was paid out) against a 50/50
    SOL/USDC hold of the day's opening equity. `day` is a date. None when the
    day has no snapshot with equity. A deposit, withdrawal or internal flow
    (capital_flows) between the day's first and last snapshot moves the
    opening capital and the hold by its dollar value, as in since_start: a
    withdrawal is not a loss (2026-10-03: the 0.07 SOL sent to the swing
    wallet read as -$8.06 against holding). Several profiles: one line per
    profile, added up (combine_days)."""
    names = book_scope(profile, wallet_id)
    if len(names) != 1:
        return combine_days([x for x in (daily_line(day, n) for n in names) if x])
    start = dt.datetime.combine(day, dt.time(0), tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    w = _scope_args(names, s=start, e=end)
    with cursor() as cur:
        cur.execute(f"""select (array_agg(equity_usd order by ts asc, id asc))[1] e0,
                               (array_agg(price order by ts asc, id asc))[1] p0,
                               (array_agg(equity_usd order by ts desc, id desc))[1] e1,
                               (array_agg(price order by ts desc, id desc))[1] p1,
                               min(ts) t0, max(ts) t1
                        from snapshots where ts >= %(s)s and ts < %(e)s and equity_usd is not null
                          and {mint_in('snapshots.mint')}""", w)
        a = cur.fetchone()                    # an aggregate: always one row
        if a['e0'] is None:
            return None
        cur.execute(f"""select count(*) n, count(*) filter (where open_reason ilike 'deploy%%idle%%') idle
                        from positions p where opened_at >= %(s)s and opened_at < %(e)s and {POS_IN}""", w)
        r = cur.fetchone()
        n, idle = r['n'], r['idle']
        cur.execute('select coalesce(sum(fee_usd), 0) f from harvests where ts >= %(s)s and ts < %(e)s '
                    f"and {mint_in('harvests.mint')}", w)
        f = float(cur.fetchone()['f'])
        cur.execute("select coalesce(sum(usd), 0) u from payouts where kind in ('paid', 'uncertain') "
                    f"and ts >= %(s)s and ts < %(e)s and {PAYOUT_IN}", w)
        paid = float(cur.fetchone()['u'])
        cur.execute("select coalesce(sum(case when kind in ('deposit', 'internal_in') then usd else -usd end), 0) n "
                    "from capital_flows where kind in ('deposit', 'withdrawal', 'internal_in', 'internal_out') "
                    f"and ts > %(t0)s and ts <= %(t1)s and {FLOW_IN}", dict(w, t0=a['t0'], t1=a['t1']))
        net = float(cur.fetchone()['n'])
        mixed = mixed_sides(_pairs_held(cur, w, start, end))['a']
    e0, p0, e1, p1 = float(a['e0']), float(a['p0']), float(a['e1']), float(a['p1'])
    # Earned is accrual, the same basis as the book's "today" (fees_between);
    # fees_usd is what was harvested in the day (audit 2026-09-30: $0.61 of
    # the 09-30 figure was earned before midnight and harvested after).
    earned = float(fees_between(start, min(end, now()), names[0])['usd'])
    value = e1 + paid
    # a day across pairs of different base tokens (sol-swing) opens on one
    # token's price and closes on another's: no price and no hold benchmark
    hold = e0 * (0.5 + 0.5 * p1 / p0) + net if p0 > 0 and not mixed else None
    return {'day': day.isoformat(), 'complete': now() >= end,
            'recentres': int(n), 'idle_redeploys': int(idle), 'fees_usd': round(f, 4),
            'fees_earned_usd': round(earned, 4),
            'fees_per_recentre_usd': round(f / n, 4) if n else None,
            'price_open': None if mixed else round(p0, 4), 'price_close': None if mixed else round(p1, 4),
            'equity_open': round(e0, 4), 'equity_close': round(e1, 4), 'paid_out_usd': round(paid, 4),
            'net_flows_usd': round(net, 4),
            'value_change_usd': round(value - e0 - net, 4),
            'hold_50_50_usd': round(hold, 4) if hold is not None else None,
            'vs_hold_usd': round(value - hold, 4) if hold is not None else None}


def daily_lines(days=2, profile=None, wallet_id=None):
    """The last `days` UTC days, newest first (today is the running one)."""
    today = now().date()
    out = []
    for k in range(days):
        line = daily_line(today - dt.timedelta(days=k), profile, wallet_id)
        if line:
            out.append(line)
    return out


def since_start(extra_usd=0.0, profile=None, wallet_id=None):
    """Profit since the bot started, against the capital it started with and
    every deposit or withdrawal since (capital_flows), not against the first
    snapshot. `extra_usd` is value the snapshots do not count (rent in empty
    token accounts, reward dust), from the equity audit. Benchmarks: holding
    what the baseline held (token A, the pool's base token, plus USDC) plus the
    flows, and a 50/50 hold of the baseline. None without a baseline or a
    snapshot. A flow's token A amount is amounts[<token A mint>] (config
    mints[1], else deposit_mint) when the flow carries it, else the pre-020
    `sol` column. Several profiles: each one's
    own book with its own uncounted value (extra_usd is not used), added up
    (combine_since)."""
    names = book_scope(profile, wallet_id)
    if len(names) != 1:
        return combine_since([x for x in (since_start(_uncounted_usd(n), n) for n in names) if x])
    with cursor() as cur:
        cur.execute('select * from config where name = %s', (names[0],))
        cfg = cur.fetchone() or {}
        a = _scope_args(names, mint=(cfg.get('mints') or [None])[0] or cfg.get('deposit_mint'))
        cur.execute("select ts, coalesce((amounts->>%(mint)s::text)::numeric, sol) sol, usdc, usd, price "
                    f"from capital_flows where kind = 'baseline' and {FLOW_IN} "
                    "order by (profile is null), ts limit 1", a)
        base = cur.fetchone()
        cur.execute("select equity_usd, price, ts from snapshots where equity_usd is not null "
                    f"and {mint_in('snapshots.mint')} order by ts desc, id desc limit 1", a)
        last = cur.fetchone()
        if not base or not last:
            return None
        # Only flows after the baseline: the baseline is the sleeve as funded,
        # so a deposit that landed before it is in it already (2026-10-02:
        # mu-usdc's deposit and baseline were the same MU, and its P&L read
        # -$208 on $207 of equity). Internal flows move native rent and fees
        # between two books of one wallet; one with no token A amount (the
        # native token is not this pool's) counts in the hold benchmark at
        # its dollar value, as `fixed`.
        cur.execute(f"""select coalesce(sum(sgn * usd), 0) net_usd,
                               coalesce(sum(sgn * coalesce((amounts->>%(mint)s::text)::numeric, sol)), 0) net_sol,
                               coalesce(sum(sgn * usdc), 0) net_usdc,
                               coalesce(sum(sgn * usd) filter (where kind like 'internal%%'
                                   and coalesce((amounts->>%(mint)s::text)::numeric, sol) = 0), 0) net_fixed_usd
                        from (select *, case when kind in ('deposit', 'internal_in') then 1 else -1 end sgn
                              from capital_flows
                              where kind in ('deposit', 'withdrawal', 'internal_in', 'internal_out')
                                and ts > %(since)s and {FLOW_IN}) f""", dict(a, since=base['ts']))
        fl = cur.fetchone()
        cur.execute("select coalesce(sum(usd), 0) u from payouts where kind in ('paid', 'uncertain') "
                    f"and ts >= %(since)s and {PAYOUT_IN}", dict(a, since=base['ts']))
        paid = float(cur.fetchone()['u'])
        mixed = mixed_sides(_pairs_held(cur, a))['a']
    p0, p1 = float(base['price']), float(last['price'])
    start = float(base['usd']) + float(fl['net_usd'])
    value = float(last['equity_usd']) + float(extra_usd or 0.0) + paid
    hold = ((float(base['sol']) + float(fl['net_sol'])) * p1 + float(base['usdc']) + float(fl['net_usdc'])
            + float(fl['net_fixed_usd']))
    hold_50 = float(base['usd']) * (0.5 + 0.5 * p1 / p0) + float(fl['net_usd'])
    days = (last['ts'] - base['ts']).total_seconds() / 86400
    out = {'since': base['ts'].isoformat(), 'days': round(days, 2),
           'start_usd': round(start, 4), 'start_sol': round(float(base['sol']) + float(fl['net_sol']), 6),
           'price_start': p0, 'price_now': round(p1, 4),
           'equity_usd': round(float(last['equity_usd']), 4), 'uncounted_usd': round(float(extra_usd or 0.0), 4),
           'paid_out_usd': round(paid, 4), 'value_usd': round(value, 4),
           'profit_usd': round(value - start, 4), 'profit_pct': round((value / start - 1) * 100, 3) if start else None,
           'hold_start_assets_usd': round(hold, 4), 'vs_hold_start_assets_usd': round(value - hold, 4),
           'hold_50_50_usd': round(hold_50, 4), 'vs_hold_50_50_usd': round(value - hold_50, 4)}
    if mixed:
        # A book that moved between pairs of different base tokens (sol-swing:
        # SOL, then DJT) has no one token to hold: its last price is the
        # pool's now, its baseline amount another token's. Its dollar profit
        # stands; the hold benchmarks and the token amount are None.
        out.update({k: None for k in ('start_sol', 'price_start', 'price_now') + SINCE_HOLD})
    return out


def _pnl(equity, started, paid, sst):
    """{'pnl_usd', 'pnl_basis'}: the capital-baseline profit when there is a
    baseline, else equity - first snapshot + payouts. Pure."""
    if sst and sst.get('profit_usd') is not None:
        return {'pnl_usd': round(float(sst['profit_usd']), 2), 'pnl_basis': 'capital baseline'}
    if equity is None or started is None:
        return {'pnl_usd': None, 'pnl_basis': None}
    return {'pnl_usd': round(_f(equity) - _f(started) + paid, 2), 'pnl_basis': 'first snapshot'}


def _since_start_or_none(profile=None, wallet_id=None):
    try:
        names = book_scope(profile, wallet_id)
        return since_start(_uncounted_usd(names[0]) if len(names) == 1 else 0.0, profile, wallet_id)
    except Exception:
        return None


def _uncounted_usd(profile):
    """Value the snapshots do not count (rent in empty token accounts, reward
    dust) that the equity audit keeps per wallet (audit_state). It is the
    wallet's, so only the wallet's residual owner carries it (a profile with
    no wallet row is its own residual owner): a sum over profiles counts it
    once. The legacy wallet's key may still be unprefixed (before the deploy
    moved it)."""
    with cursor() as cur:
        cur.execute('select wallet_id, residual_owner from config where name = %s', (profile,))
        cfg = cur.fetchone()
        wid = cfg['wallet_id'] if cfg else None
        if wid and not cfg['residual_owner']:
            return 0.0
        keys = [f'{wid}|uncounted_usd'] if wid else ['uncounted_usd']
        if wid == LEGACY_WALLET:
            keys.append('uncounted_usd')
        cur.execute('select key, value from audit_state where key = any(%s)', (keys,))
        vals = {r['key']: r['value'] for r in cur.fetchall()}
    return next((float(vals[k] or 0.0) for k in keys if k in vals), 0.0)


def audit_value(key):
    """A value the audits keep (audit_state), or None."""
    with cursor() as cur:
        cur.execute('select value from audit_state where key = %s', (scoped(key, 'wallet_id'),))
        r = cur.fetchone()
    return r['value'] if r else None


def set_audit_value(key, value):
    with cursor(commit=True) as cur:
        cur.execute('insert into audit_state (key, value, ts) values (%s, %s, now()) '
                    'on conflict (key) do update set value = excluded.value, ts = excluded.ts',
                    (scoped(key, 'wallet_id'), str(value)))


def record_audit(run_id, check_name, status, detail):
    if status not in ('ok', 'warn', 'fail'):
        raise ValueError(f'status must be ok, warn or fail, not {status!r}')
    with cursor(commit=True) as cur:
        cur.execute('insert into audits (run_id, check_name, status, detail, profile, wallet_id) '
                    'values (%s,%s,%s,%s,%s,%s)',
                    (run_id, check_name, status, json.dumps(detail, default=str),
                     CONTEXT.get('profile'), CONTEXT.get('wallet_id')))
        cur.execute("delete from audits where ts < now() - interval '30 days'")


def record_flow(ts, kind, sol, usdc, usd, price, signature, detail, amounts=None, profile=None, wallet_id=None):
    """A deposit or withdrawal the flows audit found. One row per signature.
    `amounts` is {mint: human amount} for any token; sol/usdc stay for the
    pre-020 readers. `profile` and `wallet_id` name the book the flow is
    for (the audit runs in the residual owner and books a MU deposit to
    mu-usdc); by default this process's own (CONTEXT)."""
    if kind not in ('deposit', 'withdrawal'):
        raise ValueError(f'kind must be deposit or withdrawal, not {kind!r}')
    with cursor(commit=True) as cur:
        cur.execute('insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, '
                    'amounts, wallet_id, profile) '
                    'values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (signature) do nothing',
                    (ts, kind, sol, usdc, usd, price, signature, detail,
                     json.dumps(amounts) if amounts is not None else None,
                     wallet_id or CONTEXT.get('wallet_id'), profile or CONTEXT.get('profile')))
        return cur.rowcount == 1


def _daily_or_none(profile=None, wallet_id=None):
    try:
        return daily_lines(2, profile, wallet_id)
    except Exception:
        return None             # the book never fails over its daily line


DEPLOYMENT_TOLERANCE_USD = 1.0     # LP + wallet + fees may differ from equity by this much


def _pct(part, whole):
    """part / whole in percent, clamped to [0, 100], or None without a whole."""
    if not whole or whole <= 0:
        return None
    return round(min(max(part / whole * 100, 0.0), 100.0), 1)


def _deployment(latest, equity):
    """What is in the LP position and what is in the wallet, from the latest
    snapshot. The position mark is zero when the latest snapshot's position is
    closed (the money is back in the wallet). The share is None (shown as no
    share) when the parts do not add up to the equity: a book that cannot be
    checked says nothing, not a wrong figure."""
    if not latest:
        return {'lp_usd': None, 'wallet_usd': None, 'deployed_pct': None}
    lp = _f(latest.get('position_usd')) if latest.get('open') else 0.0
    wallet = latest.get('wallet_usd')
    eq = _f(equity) if equity is not None else None
    pct = _pct(lp, eq)
    if pct is not None and wallet is not None and latest.get('open'):
        if abs(lp + _f(wallet) + _f(latest.get('accrued_usd')) - eq) > DEPLOYMENT_TOLERANCE_USD:
            pct = None
    return {'lp_usd': round(lp, 2),
            'wallet_usd': round(_f(wallet), 2) if wallet is not None else None,
            'deployed_pct': pct}


def deployment_now(book, lp_usd):
    """The book's LP line at an OPEN or a CLOSE, from the position's mark the
    loop just read (lp_usd; 0 at a close) and the equity on record. The latest
    snapshot still describes the position before the move, so without this an
    OPEN book said "in LP $0.00 (0.0%)" (2026-09-30). No wallet read: one
    seconds after a move can still show the tokens that just left it. Pure."""
    eq = book.get('equity_usd')
    if lp_usd is None or eq is None or eq <= 0:
        return {}
    lp = min(max(float(lp_usd), 0.0), float(eq))
    return {'lp_usd': round(lp, 2), 'wallet_usd': round(float(eq) - lp, 2), 'deployed_pct': _pct(lp, eq)}


def stats(token_a=None, token_b=None, profile=None, wallet_id=None):
    """Everything cumulative, in both tokens and in dollars.

    Fees are earned in two currencies, not one. A position pays you token A and
    token B in whatever proportion the trading happened to take, so a single
    dollar figure hides what you hold and moves with the price even in an hour
    when you earned nothing. Both are reported; the dollar figure is the
    convenience.

    Realised means harvested into the wallet: permanent. Unrealised means still
    sitting in the position and reset to zero the moment it closes. Their sum is
    what the ledger has seen you earn, and it only goes up.

    The book of one profile: this process's own when no filter is given (see
    book_scope). Several profiles: one book each, added up (combine_books).
    """
    names = book_scope(profile, wallet_id)
    if len(names) != 1:
        return combine_books([stats(profile=n) for n in names])
    name = names[0]
    a = _scope_args(names)
    with cursor() as cur:
        cur.execute('select * from config where name = %s', (name,))
        cfg = cur.fetchone() or {}

        cur.execute('select coalesce(sum(fee_a),0) a, coalesce(sum(fee_b),0) b, '
                    f"coalesce(sum(fee_usd),0) usd, count(*) n from harvests where {mint_in('harvests.mint')}", a)
        r = cur.fetchone()

        # The latest snapshot, and whether the position it describes still
        # exists. Unrealised fees are what is sitting in an OPEN position. A
        # snapshot of a position that has since been harvested and closed
        # describes money that is now in the wallet and already counted as
        # realised; reading it as unrealised too is how a $0.26 harvest was
        # reported as $0.51 of fees.
        cur.execute('select s.ts, s.mint, s.price, s.accrued_a, s.accrued_b, '
                    's.accrued_usd, s.equity_usd, s.in_range, s.p_exit_6h, s.p_exit_24h, '
                    's.p_exit_72h, s.band_position, s.position_usd, s.wallet_usd, p.opened_at, '
                    '(p.mint is not null and p.closed_at is null) as open '
                    'from snapshots s left join positions p on p.mint = s.mint '
                    f"where {mint_in('s.mint')} order by s.id desc limit 1", a)
        latest = cur.fetchone()
        if latest and not latest['open']:
            latest = dict(latest, accrued_a=0, accrued_b=0, accrued_usd=0)

        # The clock starts when the first position opened, not when the first
        # snapshot landed. A restart must not reset the denominator of the rate.
        cur.execute(f'''
            select least(
                     (select min(ts) from snapshots where {mint_in('snapshots.mint')}),
                     (select min(opened_at) from positions p where {POS_IN})
                   ) t0,
                   (select avg(case when in_range then 1.0 else 0.0 end)
                    from snapshots where {mint_in('snapshots.mint')}) ir
        ''', a)
        span = cur.fetchone()

        cur.execute('select equity_usd from snapshots where equity_usd is not null '
                    f"and {mint_in('snapshots.mint')} order by id asc limit 1", a)
        first_eq = cur.fetchone()

        cur.execute(f"""
            select count(*) filter (where closed_at is null) open_now,
                   count(*) total from positions p where {POS_IN}
        """, a)
        pos = cur.fetchone()

        # Where the money is, from the ledger rather than the config: the two
        # agree except in the seconds between a repoint and the next open.
        cur.execute('select dex, pool, pair_label from positions p where closed_at is null '
                    f'and {POS_IN} order by opened_at desc limit 1', a)
        cur_pos = cur.fetchone()
        mixed = mixed_sides(_pairs_held(cur, a))

        cur.execute(f"""
            select count(*) filter (where kind = 'REBAND') rebands,
                   count(*) filter (where kind ilike '%%fail%%') failures
            from events where {PROFILE_IN}
        """, a)
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
                        f"and {mint_in('snapshots.mint')} order by id desc limit 1", a)
            last_priced = cur.fetchone()
        equity = last_priced and last_priced['equity_usd']
    started = first_eq and first_eq['equity_usd']
    sst = _since_start_or_none(name)

    today = fees_between(now().replace(hour=0, minute=0, second=0, microsecond=0), None, name)

    days = None
    if latest and span and span['t0']:
        days = max((latest['ts'] - span['t0']).total_seconds() / 86400, 1e-9)

    total_usd = _f(r['usd']) + u_usd
    rate = total_usd / days if (days and days >= MIN_RATE_DAYS) else None
    # Annualised on the capital actually at work, not on notional.
    apr = (rate / float(equity) * 365 * 100) if (rate and equity) else None

    pools = by_pool(name)
    t6, t24 = trailing_rate(6, name), trailing_rate(24, name)
    outlook = season_outlook(season())
    eq_f = _f(equity) if equity else None
    apr_of = lambda t: (round(t['fees_per_day_usd'] / eq_f * 365 * 100, 2)
                        if t and eq_f else None)
    rnd = lambda x, d=6: round(_f(x), d)
    tok = lambda side, x: None if mixed[side] else rnd(x)
    return {
        'pair': cfg.get('pair_label'),
        'dex': cfg.get('dex'),
        'position_dex': cur_pos['dex'] if cur_pos else None,
        'position_pair': cur_pos['pair_label'] if cur_pos else None,
        'position_pool': cur_pos['pool'] if cur_pos else None,
        'dexes_held': sorted({r['dex'] for r in pools}),
        'by_pool': [{k: r[k] for k in ('profile', 'dex', 'pair_label', 'pool', 'positions', 'open_now', 'days',
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
        # a side whose token changed between pools has no amount (SOL and
        # DJT do not add); each pool's own is in by_pool
        'fees_today_a': tok('a', today['a']),
        'fees_today_b': tok('b', today['b']),
        'fees_today_usd': round(_f(today['usd']), 4),
        'fees_realised_a': tok('a', r['a']), 'fees_realised_b': tok('b', r['b']),
        'fees_realised_usd': round(_f(r['usd']), 4),
        'fees_unrealised_a': tok('a', u_a), 'fees_unrealised_b': tok('b', u_b),
        'fees_unrealised_usd': round(u_usd, 4),
        'fees_total_a': tok('a', _f(r['a']) + u_a),
        'fees_total_b': tok('b', _f(r['b']) + u_b),
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
        # what is in the LP position (its mark, rent included) and what is not
        **_deployment(latest, equity),
        'equity_start_usd': round(_f(started), 2) if started is not None else None,
        # P&L is since_start's profit (the capital baseline and every flow):
        # the first snapshot is not the start (audit 2026-09-30: $7.69 apart).
        # Without a baseline, the first-snapshot figure, labelled as such.
        # Payouts leave the LP wallet by design; they are income, not a loss
        # (review, 2026-09-26: the book counted every payout against P&L).
        **_pnl(equity, started, _paid_usd(name), sst),
        'in_range_pct': (round(_f(span['ir']) * 100, 1)
                         if span and span['ir'] is not None else None),
        'tracked_days': round(days, 3) if days else None,
        'split': payout_totals(name) if cfg.get('payout_enabled') else None,
        'daily': _daily_or_none(name),
        'since_start': sst,
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


def mixed_sides(labels):
    """{'a': bool, 'b': bool}: whether the pairs `labels` (e.g. 'SOL/USDC',
    'DJT/USDC') hold more than one token on that side. A label with no '/'
    (or None) names no token. Pure."""
    sides = {'a': set(), 'b': set()}
    for label in labels:
        a, sep, b = str(label or '').partition('/')
        if sep:
            sides['a'].add(a.strip())
            sides['b'].add(b.strip())
    return {k: len(v) > 1 for k, v in sides.items()}


def _pairs_held(cur, a, since=None, until=None):
    """The pair labels of the scope's positions (`a`: _scope_args); with
    `since`/`until`, of the positions with an equity snapshot in that window."""
    if since is None:
        cur.execute(f'select distinct pair_label from positions p where {POS_IN}', a)
    else:
        cur.execute('select distinct p.pair_label from snapshots s join positions p on p.mint = s.mint '
                    f'where s.ts >= %(s)s and s.ts < %(e)s and s.equity_usd is not null and {POS_IN}',
                    dict(a, s=since, e=until))
    return [r['pair_label'] for r in cur.fetchall()]


# How books of several profiles add up (combine_*). Dollars add across pools;
# token amounts only across books of the same token; counts add.
BOOK_USD = ('fees_today_usd', 'fees_realised_usd', 'fees_unrealised_usd', 'fees_total_usd', 'fees_per_day_usd',
            'fees_per_day_6h_usd', 'fees_per_day_24h_usd', 'expected_next_hours_fees_per_day_usd', 'equity_usd',
            'lp_usd', 'wallet_usd', 'equity_start_usd', 'pnl_usd', 'pnl_all_pools_usd', 'position_pnl_all_pools_usd')
BOOK_COUNTS = ('harvests', 'positions_opened', 'positions_open_now', 'rebands', 'failures')
BOOK_TOKENS = {'token_a': ('fees_today_a', 'fees_realised_a', 'fees_unrealised_a', 'fees_total_a'),
               'token_b': ('fees_today_b', 'fees_realised_b', 'fees_unrealised_b', 'fees_total_b')}
BOOK_SAME = ('pair', 'dex', 'position_dex', 'position_pair', 'position_pool', 'pnl_basis')
DAY_USD = ('fees_usd', 'fees_earned_usd', 'equity_open', 'equity_close', 'paid_out_usd', 'net_flows_usd',
           'value_change_usd')
SINCE_HOLD = ('hold_start_assets_usd', 'vs_hold_start_assets_usd', 'hold_50_50_usd', 'vs_hold_50_50_usd')
SINCE_USD = ('start_usd', 'equity_usd', 'uncounted_usd', 'paid_out_usd', 'value_usd', 'profit_usd') + SINCE_HOLD


def _sum_known(values, digits=4):
    """The sum of the values that are known, or None when none is. Pure."""
    known = [_f(v) for v in values if v is not None]
    return round(sum(known), digits) if known else None


def _same(values):
    """The one value all share, or None when they differ. Pure."""
    s = set(values)
    return s.pop() if len(s) == 1 else None


def combine_days(lines):
    """One UTC day's lines (daily_line) of several profiles as one line.
    Counts and dollars add; the prices are one pool's and are dropped; the
    hold benchmark adds only when every line has one. One line is returned
    as it is; no line is None. Pure."""
    if len(lines) <= 1:
        return lines[0] if lines else None
    days = {x['day'] for x in lines}
    if len(days) != 1:
        raise ValueError(f'lines of different days: {sorted(days)}')
    out = {'day': days.pop(), 'complete': all(x['complete'] for x in lines),
           'recentres': sum(x['recentres'] for x in lines), 'idle_redeploys': sum(x['idle_redeploys'] for x in lines)}
    for k in DAY_USD:
        out[k] = round(sum(_f(x.get(k)) for x in lines), 4)
    out['fees_per_recentre_usd'] = round(out['fees_usd'] / out['recentres'], 4) if out['recentres'] else None
    out['price_open'] = out['price_close'] = None
    for k in ('hold_50_50_usd', 'vs_hold_usd'):
        out[k] = round(sum(_f(x[k]) for x in lines), 4) if all(x[k] is not None for x in lines) else None
    return out


def combine_since(books):
    """since_start() of several profiles as one: dollars add, the start is the
    earliest, the token amount and the prices (one pool's each) are None, a
    hold benchmark is None unless every book has one. One book is returned as
    it is; none is None. Pure."""
    if len(books) <= 1:
        return books[0] if books else None
    out = {k: round(sum(_f(s[k]) for s in books), 4) for k in SINCE_USD}
    # a benchmark adds only when every book has one (a book across pairs has none)
    out.update({k: None for k in SINCE_HOLD if any(s.get(k) is None for s in books)})
    out.update(since=min(s['since'] for s in books), days=max(s['days'] for s in books),
               start_sol=None, price_start=None, price_now=None,
               profit_pct=(round((out['value_usd'] / out['start_usd'] - 1) * 100, 3) if out['start_usd'] else None))
    return out


def combine_books(books):
    """Several profiles' books (stats()) as one. Dollars and counts add (the
    snapshot's wallet_usd is each profile's own sleeve, so nothing is counted
    twice). A token amount adds only across books that hold the same token on
    that side, else it is None: SOL and MU do not add. What belongs to one
    pool (its price, its band) is None. APRs are recomputed over the books that
    have both the rate and an equity. One book is returned as it is. Pure."""
    if len(books) == 1:
        return books[0]
    out = {k: _sum_known([b.get(k) for b in books]) for k in BOOK_USD}
    out.update({k: sum(int(b.get(k) or 0) for b in books) for k in BOOK_COUNTS})
    for side, keys in BOOK_TOKENS.items():
        sym = _same(b.get(side) for b in books)
        out[side] = sym
        out.update({k: (_sum_known([b.get(k) for b in books], 6) if sym else None) for k in keys})
    out.update({k: _same(b.get(k) for b in books) for k in BOOK_SAME})

    def apr(rate_key):
        known = [(_f(b[rate_key]), _f(b['equity_usd'])) for b in books
                 if b.get(rate_key) is not None and b.get('equity_usd')]
        eq = sum(e for _, e in known)
        return round(sum(r for r, _ in known) / eq * 365 * 100, 2) if eq > 0 else None
    ranged = [(_f(b['in_range_pct']), _f(b.get('tracked_days'))) for b in books if b.get('in_range_pct') is not None]
    weight = sum(d for _, d in ranged)
    days = {}
    for b in books:
        for line in b.get('daily') or []:
            days.setdefault(line['day'], []).append(line)
    splits = [b['split'] for b in books if b.get('split')]
    out.update(
        apr_pct=apr('fees_per_day_usd'), apr_6h_pct=apr('fees_per_day_6h_usd'), apr_24h_pct=apr('fees_per_day_24h_usd'),
        in_range_pct=round(sum(p * d for p, d in ranged) / weight, 1) if weight > 0 else None,
        tracked_days=max((b['tracked_days'] for b in books if b.get('tracked_days') is not None), default=None),
        # a share only when every book's share could be checked
        deployed_pct=(_pct(out['lp_usd'] or 0.0, out['equity_usd'])
                      if all(b.get('deployed_pct') is not None for b in books) else None),
        dexes_held=sorted({d for b in books for d in b.get('dexes_held') or []}),
        by_pool=[p for b in books for p in b.get('by_pool') or []],
        season=next((b['season'] for b in books if b.get('season')), None),
        split=({k: (sum(int(s[k]) for s in splits) if k == 'payouts' else round(sum(_f(s[k]) for s in splits), 4))
                for k in splits[0]} if splits else None),
        daily=[combine_days(v) for _, v in sorted(days.items(), reverse=True)],
        since_start=combine_since([b['since_start'] for b in books if b.get('since_start')]),
        last_price=None, band=None,
        last_seen=max((b['last_seen'] for b in books if b.get('last_seen')), default=None))
    return out


def forecasts(horizons=(6, 24, 72), profile=None, wallet_id=None):
    """The survival model against the tape it ran on. For every snapshot that
    carried a forecast, whether the SAME position was seen out of range within
    the horizon (a position that closed for another reason before the horizon
    ran out is censored: dropped, not counted as a survivor). Predictions are
    bucketed by decile so a bucket's mean forecast can be read next to its
    realised exit rate; Brier is the mean squared error of the probability."""
    out = {}
    names = book_scope(profile, wallet_id)
    with cursor() as cur:
        for h in horizons:
            cur.execute(f"""
                with f as (
                    select s.id, s.ts, s.mint, s.p_exit_{h}h p
                    from snapshots s
                    where s.p_exit_{h}h is not null and s.in_range and {mint_in('s.mint')}
                ), o as (
                    select f.id, f.p,
                           exists (select 1 from snapshots x where x.mint = f.mint
                                   and x.ts > f.ts and x.ts <= f.ts + make_interval(hours => %(h)s)
                                   and not x.in_range) exited,
                           (select max(x.ts) from snapshots x where x.mint = f.mint) last_seen
                    from f
                )
                select width_bucket(p, 0, 1.0000001, 10) bucket, count(*) n, avg(p) p_mean,
                       avg(case when exited then 1.0 else 0.0 end) exit_rate,
                       avg((p - case when exited then 1.0 else 0.0 end) ^ 2) brier
                from o
                where exited or last_seen >= (select ts from snapshots where id = o.id) + make_interval(hours => %(h)s)
                group by 1 order by 1
            """, _scope_args(names, h=h))
            rows = [dict(r) for r in cur.fetchall()]
            n = sum(r['n'] for r in rows)
            brier = (sum(_f(r['brier']) * r['n'] for r in rows) / n) if n else None
            out[h] = {'n': n, 'brier': (round(brier, 4) if brier is not None else None),
                      'buckets': [{'n': r['n'], 'p_mean': round(_f(r['p_mean']), 3),
                                   'exit_rate': round(_f(r['exit_rate']), 3)} for r in rows]}
    return out


def daily(profile=None, wallet_id=None):
    """Fees per UTC day. One row per day.

    A position's earnings to date are its unharvested accrual plus everything
    already harvested from it. That sum is what grows as fees come in and does
    not move when a harvest turns accrued into realised, so the day's earning
    is its rise over the day, per position, summed. Adding harvests to the
    accrual delta instead counted a $0.26 harvest twice. The day's equity is
    each profile's highest of the day, added up over the profiles.
    """
    with cursor() as cur:
        cur.execute(f"""
            with last_per_day as (
                select distinct on ((ts at time zone 'utc')::date, mint)
                       (ts at time zone 'utc')::date d, mint, a, b, usd
                from fee_points where {mint_in('fee_points.mint')}
                order by (ts at time zone 'utc')::date, mint, ts desc, kind desc, id desc
            ), per_mint as (
                select d, mint,
                       a - coalesce(lag(a) over w, 0) a,
                       b - coalesce(lag(b) over w, 0) b,
                       usd - coalesce(lag(usd) over w, 0) usd
                from last_per_day window w as (partition by mint order by d)
            ), f as (
                select d, sum(a) a, sum(b) b, sum(usd) usd from per_mint group by d
            ), snaps as (
                select (ts at time zone 'utc')::date d, in_range, equity_usd,
                       coalesce((select x.config_name from positions x where x.mint = s.mint), %(legacy)s) prof
                from snapshots s where {mint_in('s.mint')}
            ), eq as (
                select d, sum(eq) eq from (select d, prof, max(equity_usd) eq from snaps group by 1, 2) t group by d
            ), s as (
                select snaps.d, avg(case when in_range then 1.0 else 0.0 end) ir, max(eq.eq) eq
                from snaps join eq on eq.d = snaps.d group by 1
            )
            , where_ as (
                -- the pools the money sat in that day, most-snapshotted first
                select (s.ts at time zone 'utc')::date d,
                       string_agg(distinct p.dex || ' ' || p.pair_label, ', ') pools
                from snapshots s join positions p on p.mint = s.mint where {POS_IN} group by 1
            )
            select coalesce(f.d, s.d)::date as day,
                   coalesce(f.a, 0) fee_a, coalesce(f.b, 0) fee_b, coalesce(f.usd, 0) fee_usd,
                   s.ir in_range, s.eq equity_usd, w.pools
            from f full outer join s on f.d = s.d
            left join where_ w on w.d = coalesce(f.d, s.d)
            order by 1 desc limit 60
        """, _scope_args(book_scope(profile, wallet_id)))
        return [dict(r) for r in cur.fetchall()]


def history(limit=30, profile=None, wallet_id=None):
    with cursor() as cur:
        cur.execute('select ts, price, in_range, accrued_usd, equity_usd '
                    f"from snapshots where {mint_in('snapshots.mint')} order by id desc limit %(limit)s",
                    _scope_args(book_scope(profile, wallet_id), limit=limit))
        return [dict(r) for r in cur.fetchall()]


# --- command line ------------------------------------------------------------

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


def _print_stats(profile=None, wallet_id=None):
    s = stats(profile=profile, wallet_id=wallet_id)
    # a combined book of different tokens has no token amount on that side
    a, b = s['token_a'] or '-', s['token_b'] or '-'
    tk = lambda x: f'{x:>12.6f}' if x is not None else f"{'-':>12}"
    w = lambda lbl, ka, kb, ku: print(
        f"  {lbl:<11} {tk(s[ka])} {a:<5} {tk(s[kb])} {b:<5} ${s[ku]:.4f}")
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


def _scope_flags(argv):
    """argv without `--pool P` / `--wallet W`, and the book filters they name.
    Pure."""
    rest, scope = [], {'profile': None, 'wallet_id': None}
    it = iter(argv)
    for x in it:
        if x in ('--pool', '--wallet'):
            v = next(it, None)
            if not v:
                raise SystemExit(f'{x} needs a value')
            scope['profile' if x == '--pool' else 'wallet_id'] = v
        else:
            rest.append(x)
    return rest, scope


if __name__ == '__main__':
    import sys
    argv, SCOPE = _scope_flags(sys.argv)
    if SCOPE['wallet_id'] is not None:          # an id, or its address's first characters
        SCOPE['wallet_id'] = resolve_wallet(SCOPE['wallet_id'], wallets())
    cmd = argv[1] if len(argv) > 1 else 'stats'
    arg = argv[2] if len(argv) > 2 else None

    if cmd == 'seed':
        print(json.dumps(seed(), indent=1, default=str))
    elif cmd == 'add':
        # db.py add <name> <pool> [key=value ...]
        if len(argv) < 4:
            raise SystemExit('usage: db.py add <name> <pool> [capital_usd=... ...]')
        kv = dict(p.partition('=')[::2] for p in argv[4:])
        print(json.dumps(add(arg, argv[3], **kv), indent=1, default=str))
    elif cmd == 'migrate':
        print(json.dumps(migrate_sqlite(arg or 'ledger.sqlite'), indent=1, default=str))
    elif cmd == 'config':
        print(json.dumps(load_config(arg), indent=1, default=str))
    elif cmd == 'activate':
        print('active:', activate(arg))
    elif cmd == 'set':
        # db.py set <profile> key=value [key=value ...]
        profile, pairs = arg, argv[3:]
        for p in pairs:
            k, _, v = p.partition('=')
            row = set_param(profile, k.strip(), v.strip())
            print(f'{k.strip()} = {row[k.strip()]}')
    elif cmd == 'repoint':
        # db.py repoint <profile> <dex> <pool>
        if len(argv) < 5:
            raise SystemExit('usage: db.py repoint <profile> <dex> <pool>')
        info = describe_pool(argv[4], argv[3])
        row = repoint(arg, argv[3], argv[4], info['pair_label'],
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
        engine.print_board(rows, top=int(argv[3]) if len(argv) > 3 else 40)
    elif cmd == 'pools':
        for r in by_pool(**SCOPE):
            rate = f"${r['fees_per_day_usd']:.4f}/day" if r['fees_per_day_usd'] is not None else '-'
            print(f"{'>' if r['open_now'] else ' '} {r['dex']:<22} {r['pair_label']:<12} "
                  f"{r['positions']} pos  {r['days']:>6.2f}d  fees ${r['fees_usd']:.4f} "
                  f"(real ${r['realised_usd']:.4f} + unreal ${r['unrealised_usd']:.4f})  {rate:>14}  "
                  f"in range {r['in_range_pct'] if r['in_range_pct'] is not None else '-'}%  {r['pool']}")
    elif cmd == 'daily':
        for d in reversed(daily(**SCOPE)):
            print(f"{d['day']}  {float(d['fee_a']):.6f} A  {float(d['fee_b']):.6f} B  "
                  f"${float(d['fee_usd']):.4f}  in range "
                  f"{(float(d['in_range'] or 0) * 100):.0f}%  {d.get('pools') or ''}")
    elif cmd == 'history':
        for r in reversed(history(**SCOPE)):
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
        for h, r in forecasts(**SCOPE).items():
            print(f"P(exit within {h}h): {r['n']} forecasts resolved, Brier {r['brier']}")
            for b in r['buckets']:
                print(f"    predicted {b['p_mean']:.0%}  realised {b['exit_rate']:.0%}  (n={b['n']})")
    elif cmd == 'json':
        print(json.dumps(stats(**SCOPE), indent=1, default=str))
    else:
        _print_stats(**SCOPE)
