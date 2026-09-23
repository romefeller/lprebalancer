"""Durable accounting for the rebalancer.

The problem this solves: a position's fee counter is a property of the POSITION,
not of you. Harvest it, close it, open a new one, and the counter is back at
zero — so a bot that reports the live position's accrual tells you it has earned
nothing every time it rebalances, no matter how much it has actually made.

Everything here is cumulative and survives rebalances, restarts and reboots.
Realised fees (harvested to the wallet) and unrealised fees (accrued in the open
position) are tracked separately, because only the first is money you hold.

Tables
------
positions   one row per position ever opened, with its band, deposits and
            withdrawals, so a position's realised profit is closable arithmetic
harvests    every fee collection, with its signature
snapshots   a time series of price, range status, liquidity and equity
events      anything worth explaining later: rebands, failures, breakers

Money is stored in native token units AND in USD at the time observed. The
token amounts are the truth; the USD is a convenience that depends on a price
which was only correct at that moment.
"""
import pathlib
import sqlite3
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
DB = ROOT / 'ledger.sqlite'
MIN_RATE_DAYS = 0.04        # ~1 hour: below this a per-day rate is noise

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    mint          TEXT PRIMARY KEY,
    pool          TEXT NOT NULL,
    pair          TEXT,
    opened_at     TEXT NOT NULL,
    closed_at     TEXT,
    lower_price   REAL,
    upper_price   REAL,
    band_pct      REAL,
    open_sig      TEXT,
    close_sig     TEXT,
    deposit_usd   REAL,
    withdraw_usd  REAL,
    open_reason   TEXT
);
CREATE TABLE IF NOT EXISTS harvests (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    mint      TEXT NOT NULL,
    fee_a     REAL NOT NULL,
    fee_b     REAL NOT NULL,
    fee_usd   REAL NOT NULL,
    signature TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    mint          TEXT,
    price         REAL,
    in_range      INTEGER,
    liquidity     TEXT,
    accrued_a     REAL,
    accrued_b     REAL,
    accrued_usd   REAL,
    wallet_usd    REAL,
    position_usd  REAL,
    equity_usd    REAL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_ts  ON snapshots(ts);
CREATE INDEX IF NOT EXISTS harvests_mint ON harvests(mint);
"""


def now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.executescript(SCHEMA)
    # Migrate a ledger written before fees were tracked per token. Adding the
    # columns is enough: old rows keep their USD figure and report no split,
    # which is honest — that split was never recorded.
    have = {r[1] for r in con.execute('PRAGMA table_info(snapshots)')}
    for col in ('accrued_a', 'accrued_b'):
        if col not in have:
            con.execute(f'ALTER TABLE snapshots ADD COLUMN {col} REAL')
    con.commit()
    return con


def open_position(mint, pool, pair, lower, upper, band_pct, sig,
                  deposit_usd, reason=''):
    con = connect()
    con.execute(
        'INSERT OR REPLACE INTO positions '
        '(mint, pool, pair, opened_at, lower_price, upper_price, band_pct, '
        ' open_sig, deposit_usd, open_reason) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (mint, pool, pair, now(), lower, upper, band_pct, sig, deposit_usd, reason))
    con.commit(); con.close()


def close_position(mint, sig, withdraw_usd):
    con = connect()
    con.execute('UPDATE positions SET closed_at=?, close_sig=?, withdraw_usd=? '
                'WHERE mint=?', (now(), sig, withdraw_usd, mint))
    con.commit(); con.close()


def record_harvest(mint, fee_a, fee_b, fee_usd, sig):
    con = connect()
    con.execute('INSERT INTO harvests (ts, mint, fee_a, fee_b, fee_usd, signature) '
                'VALUES (?,?,?,?,?,?)', (now(), mint, fee_a, fee_b, fee_usd, sig))
    con.commit(); con.close()


def snapshot(mint, price, in_range, liquidity, accrued_a, accrued_b,
             accrued_usd, wallet_usd, position_usd):
    con = connect()
    con.execute(
        'INSERT INTO snapshots (ts, mint, price, in_range, liquidity, '
        'accrued_a, accrued_b, accrued_usd, wallet_usd, position_usd, equity_usd) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (now(), mint, price, 1 if in_range else 0, str(liquidity),
         accrued_a, accrued_b, accrued_usd, wallet_usd, position_usd,
         (wallet_usd or 0) + (position_usd or 0)))
    con.commit(); con.close()


def event(kind, detail=''):
    con = connect()
    con.execute('INSERT INTO events (ts, kind, detail) VALUES (?,?,?)',
                (now(), kind, str(detail)[:2000]))
    con.commit(); con.close()


def stats(token_a='SOL', token_b='USDC'):
    """Everything cumulative, denominated in both tokens and in dollars.

    Fees are earned in two currencies, not one. A position pays you token A and
    token B in whatever proportion the trading happened to take, so a single USD
    figure hides what you actually hold and moves when the price moves even if
    you earned nothing. Both are reported; the USD is the convenience.

    Realised means harvested into the wallet and permanent. Unrealised means
    still sitting in the position and reset to zero the moment it closes. Their
    sum is what you have earned since the ledger began, and it only goes up.
    """
    con = connect()
    q = lambda sql, *a: con.execute(sql, a).fetchone()
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    r_a, r_b, r_usd = q('SELECT COALESCE(SUM(fee_a),0), COALESCE(SUM(fee_b),0), '
                        'COALESCE(SUM(fee_usd),0) FROM harvests')
    t_a, t_b, t_usd = q('SELECT COALESCE(SUM(fee_a),0), COALESCE(SUM(fee_b),0), '
                        'COALESCE(SUM(fee_usd),0) FROM harvests WHERE ts LIKE ?',
                        today + '%')
    harvest_count = q('SELECT COUNT(*) FROM harvests')[0]
    positions_total = q('SELECT COUNT(*) FROM positions')[0]
    positions_open = q('SELECT COUNT(*) FROM positions WHERE closed_at IS NULL')[0]
    rebands = q("SELECT COUNT(*) FROM events WHERE kind='REBAND'")[0]
    failures = q("SELECT COUNT(*) FROM events WHERE kind LIKE '%fail%'")[0]
    in_range_pct = q('SELECT AVG(in_range)*100 FROM snapshots')[0]

    latest = q('SELECT ts, price, accrued_a, accrued_b, accrued_usd, equity_usd '
               'FROM snapshots ORDER BY id DESC LIMIT 1')
    first_ts = q('SELECT MIN(ts) FROM snapshots')[0]
    first_eq = q('SELECT equity_usd FROM snapshots WHERE equity_usd IS NOT NULL '
                 'ORDER BY id ASC LIMIT 1')
    # Unrealised at the first snapshot of today, so "today" counts what accrued
    # since midnight rather than everything the open position has ever earned.
    day_open = q('SELECT accrued_a, accrued_b, accrued_usd FROM snapshots '
                 'WHERE ts LIKE ? ORDER BY id ASC LIMIT 1', today + '%')
    con.close()

    u_a = (latest[2] or 0.0) if latest else 0.0
    u_b = (latest[3] or 0.0) if latest else 0.0
    u_usd = (latest[4] or 0.0) if latest else 0.0
    price = latest[1] if latest else None
    equity = latest[5] if latest else None
    started = first_eq[0] if first_eq else None

    # Today's unrealised is the growth since this morning, unless the position
    # was rebalanced today, in which case that growth restarts from zero and the
    # harvested part already counts in the realised column.
    d0_a, d0_b, d0_usd = (day_open or (0.0, 0.0, 0.0))
    du_a = max(u_a - (d0_a or 0.0), 0.0)
    du_b = max(u_b - (d0_b or 0.0), 0.0)
    du_usd = max(u_usd - (d0_usd or 0.0), 0.0)

    days = None
    if first_ts and latest:
        t0 = datetime.strptime(first_ts, '%Y-%m-%dT%H:%M:%SZ')
        t1 = datetime.strptime(latest[0], '%Y-%m-%dT%H:%M:%SZ')
        days = max((t1 - t0).total_seconds() / 86400, 1e-9)

    total_usd = (r_usd or 0) + u_usd
    rate = total_usd / days if (days and days >= MIN_RATE_DAYS) else None
    # Annualised on capital actually at work, not on notional.
    apr = (rate / equity * 365 * 100) if (rate and equity) else None

    fmt = lambda x, d=6: round(x or 0.0, d)
    return {
        'token_a': token_a, 'token_b': token_b,
        'fees_today_a': fmt(t_a + du_a), 'fees_today_b': fmt(t_b + du_b),
        'fees_today_usd': round((t_usd or 0) + du_usd, 4),
        'fees_realised_a': fmt(r_a), 'fees_realised_b': fmt(r_b),
        'fees_realised_usd': round(r_usd or 0, 4),
        'fees_unrealised_a': fmt(u_a), 'fees_unrealised_b': fmt(u_b),
        'fees_unrealised_usd': round(u_usd, 4),
        'fees_total_a': fmt((r_a or 0) + u_a), 'fees_total_b': fmt((r_b or 0) + u_b),
        'fees_total_usd': round(total_usd, 4),
        'fees_per_day_usd': round(rate, 4) if rate else None,
        'apr_pct': round(apr, 2) if apr else None,
        'harvests': harvest_count,
        'positions_opened': positions_total,
        'positions_open_now': positions_open,
        'rebands': rebands, 'failures': failures,
        'equity_usd': round(equity, 2) if equity is not None else None,
        'equity_start_usd': round(started, 2) if started is not None else None,
        'pnl_usd': (round(equity - started, 2)
                    if (equity is not None and started is not None) else None),
        'in_range_pct': round(in_range_pct, 1) if in_range_pct is not None else None,
        'tracked_days': round(days, 3) if days else None,
        'last_price': price, 'last_seen': latest[0] if latest else None,
    }


def history(limit=20):
    con = connect()
    rows = con.execute(
        'SELECT ts, price, in_range, accrued_usd, equity_usd FROM snapshots '
        'ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    con.close()
    return rows


if __name__ == '__main__':
    import json, sys
    if len(sys.argv) > 1 and sys.argv[1] == 'history':
        for r in reversed(history(30)):
            print(f'{r[0]}  price {r[1]:>9.4f}  in_range {bool(r[2])!s:<5}  '
                  f'accrued ${r[3]:.4f}  equity ${r[4] or 0:.2f}')
    else:
        print(json.dumps(stats(), indent=1))
