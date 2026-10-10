"""Shared test plumbing.

Every test runs against a separate database. The guard below refuses to run if
LPBOT_DSN points anywhere that does not end in `_test`, because the ledger
tests truncate tables and the live book is not a fixture.
"""
import atexit
import os
import pathlib
import shutil
import sys
import tempfile

# A test process run on its own (not through run.sh) gets its own temp folder,
# removed when it exits; the Node scripts it starts inherit it (2026-10-10).
if 'lpbot-tests' not in os.environ.get('TMPDIR', ''):
    _TMP = tempfile.mkdtemp(prefix='lpbot-tests.')
    os.environ['TMPDIR'] = tempfile.tempdir = _TMP
    atexit.register(shutil.rmtree, _TMP, True)

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
        cur.execute('truncate positions, band_profile, harvests, snapshots, events, capital_flows, payouts, health')


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


# Tests never read transactions from the chain. A harvest in a test carries a
# made-up signature; without this, txfees.fetch would retry it against the
# public RPC for ten seconds and then fall back. A test that wants a measured
# harvest patches txfees.fetch itself.
import txfees  # noqa: E402
txfees.network_fetch = txfees.fetch                # the real one, for its own test
txfees.fetch = lambda rpc, signature, **kw: None


# Tests never write the live feed. events.jsonl is tailed by the Telegram
# bridge, so a test notification there reaches the owner's phone: on
# 2026-09-27 test rows ("fee_read_rejected", "$1,000 rejected") did. Every
# notify() in a test goes to a scratch file instead.
import tempfile  # noqa: E402
import lp.paths  # noqa: E402
FEED = pathlib.Path(tempfile.mkdtemp(prefix='lp_bot_test_feed_')) / 'events.jsonl'
lp.paths.FEED = FEED
# The same for the state file and the profile's HALT: run/<profile>/ in the
# code directory is the live bot's once deployed, never a test's.
lp.paths.STATE = FEED.parent / 'runtime.json'
lp.paths.HALT = FEED.parent / 'HALT'
