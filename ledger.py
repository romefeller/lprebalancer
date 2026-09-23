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


def snapshot(mint, price, in_range, liquidity, accrued_usd,
             wallet_usd, position_usd):
    con = connect()
    con.execute(
        'INSERT INTO snapshots (ts, mint, price, in_range, liquidity, '
        'accrued_usd, wallet_usd, position_usd, equity_usd) VALUES (?,?,?,?,?,?,?,?,?)',
        (now(), mint, price, 1 if in_range else 0, str(liquidity), accrued_usd,
         wallet_usd, position_usd, (wallet_usd or 0) + (position_usd or 0)))
    con.commit(); con.close()


def event(kind, detail=''):
    con = connect()
    con.execute('INSERT INTO events (ts, kind, detail) VALUES (?,?,?)',
                (now(), kind, str(detail)[:2000]))
    con.commit(); con.close()


def stats():
    """Everything cumulative, in one dict. This is what gets reported."""
    con = connect()
    q = lambda sql, *a: con.execute(sql, a).fetchone()
    realised = q('SELECT COALESCE(SUM(fee_usd),0) FROM harvests')[0]
    harvest_count = q('SELECT COUNT(*) FROM harvests')[0]
    positions_total = q('SELECT COUNT(*) FROM positions')[0]
    positions_open = q('SELECT COUNT(*) FROM positions WHERE closed_at IS NULL')[0]
    first = q('SELECT MIN(ts) FROM snapshots')[0]
    latest = con.execute(
        'SELECT ts, mint, price, in_range, accrued_usd, equity_usd '
        'FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    first_equity = q('SELECT equity_usd FROM snapshots WHERE equity_usd IS NOT NULL '
                     'ORDER BY id ASC LIMIT 1')
    in_range_pct = q('SELECT AVG(in_range)*100 FROM snapshots')[0]
    rebands = q("SELECT COUNT(*) FROM events WHERE kind='REBAND'")[0]
    failures = q("SELECT COUNT(*) FROM events WHERE kind LIKE '%fail%'")[0]
    con.close()

    accrued = latest[4] if latest else 0.0
    equity = latest[5] if latest else None
    started = first_equity[0] if first_equity else None
    days = None
    if first and latest:
        t0 = datetime.strptime(first, '%Y-%m-%dT%H:%M:%SZ')
        t1 = datetime.strptime(latest[0], '%Y-%m-%dT%H:%M:%SZ')
        days = max((t1 - t0).total_seconds() / 86400, 1e-9)
    total_fees = (realised or 0) + (accrued or 0)
    return {
        'fees_realised_usd': round(realised or 0, 4),
        'fees_unrealised_usd': round(accrued or 0, 4),
        'fees_total_usd': round(total_fees, 4),
        'harvests': harvest_count,
        'positions_opened': positions_total,
        'positions_open_now': positions_open,
        'rebands': rebands,
        'failures': failures,
        'equity_usd': round(equity, 2) if equity is not None else None,
        'equity_start_usd': round(started, 2) if started is not None else None,
        'pnl_usd': round(equity - started, 2) if (equity is not None and started is not None) else None,
        'tracked_days': round(days, 3) if days else None,
        # A rate needs a window. On the first snapshot the elapsed time is
        # effectively zero and the division produces a number in the millions.
        'fees_per_day_usd': (round(total_fees / days, 4)
                             if days and days >= MIN_RATE_DAYS else None),
        'in_range_pct': round(in_range_pct, 1) if in_range_pct is not None else None,
        'last_price': latest[2] if latest else None,
        'last_seen': latest[0] if latest else None,
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
