"""Shared test plumbing.

Every test runs against a separate database. The guard below refuses to run if
LPBOT_DSN points anywhere that does not end in `_test`, because the ledger
tests truncate tables and the live book is not a fixture.
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get('LPBOT_DSN', '')
if not DSN.rstrip().endswith('_test'):
    raise SystemExit(f'tests need LPBOT_DSN=dbname=<something>_test, got {DSN!r}')

import db  # noqa: E402  (after the guard, on purpose)

LIVE_POOL = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE'      # SOL/USDC 0.04%
ADAPTIVE_POOL = 'GTHKH8s82ZR8GTSFZ1dUu6wfdxhy59wpMShxzG5zjiPm'  # ZEC/USDC, adaptive fee
BTC_QUOTE_POOL = 'CeaZcxBNLpJWtxzt58qQmfMBtJY8pQLvursXTJYGQpbN' # SOL/cbBTC, quote not a dollar


def reset_ledger():
    with db.cursor(commit=True) as cur:
        cur.execute('truncate positions, harvests, snapshots, events')


def ensure_profile(name='sol-usdc', **over):
    """An active profile without touching the network."""
    p = dict(name=name, active=True, pool=LIVE_POOL, pair_label='SOL/USDC',
             token_a='SOL', token_b='USDC', capital_usd=190, max_usd=260)
    p.update(over)
    cols = ', '.join(p)
    vals = ', '.join(['%s'] * len(p))
    with db.cursor(commit=True) as cur:
        cur.execute('update config set active = false where active')
        cur.execute(f'insert into config ({cols}) values ({vals}) '
                    'on conflict (name) do update set active = true',
                    list(p.values()))
    return db.load_config(name)
