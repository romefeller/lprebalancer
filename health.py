"""Circuit breakers: one per dependency the loop can lose.

2026-09-30: an RPC answered 403 to every pre-open swap. Nothing remembered the
failure, so the bot closed and reopened lopsided every ~11 minutes. Every
dependency now has a breaker that remembers its failures across restarts
(Postgres table `health`) and backs off exponentially.

    key             what fails                         what the loop does while it is open
    swap            the Jupiter pre-open swap          no re-centre whose only purpose is a swap
    venue:<dex>     that DEX's signer: open, close,    after TRIP_FAILS: fail over to another venue
                    harvest                            with similar on-chain income
    tape            the five-minute tape (Gecko+       STALE (widest width, no narrowing)
                    surrogates)
    rpc             wallet and position reads          the existing read-failure halt

States:
    closed     working; failures below TRIP_FAILS
    backoff    failed; retry after cooldown(fails) = BASE_S * 2^(fails-1), capped
    tripped    fails >= TRIP_FAILS: the dependency is out; the same cooldown
               applies; one probe is allowed when it elapses (half-open)

A success closes the breaker and clears the failure count. `trips` counts how
often it tripped; it only feeds the book.

The core (`after_failure`, `after_success`, `verdict`) is pure: a record in, a
record out. `record_failure`, `record_success` and `allowed` read and write
the table and never raise: a health write that fails must not stop the loop.
"""
import time

BASE_S = 600                 # the first retry, after one failure
MAX_S = 6 * 3600             # no wait longer than this
TRIP_FAILS = 3               # this many failures in a row: the dependency is out

CLOSED, BACKOFF, TRIPPED, PROBING = 'closed', 'backoff', 'tripped', 'probing'
EMOJI = {CLOSED: '🟢', BACKOFF: '🟡', TRIPPED: '🔴', PROBING: '🟡'}


def cooldown(fails, base_s=BASE_S, max_s=MAX_S):
    """The wait after `fails` failures in a row: base * 2^(fails-1), capped. Pure."""
    if fails <= 0:
        return 0.0
    return float(min(base_s * 2 ** min(fails - 1, 30), max_s))


def empty(key):
    return {'key': key, 'fails': 0, 'trips': 0, 'last_fail': None, 'last_ok': None,
            'retry_at': None, 'last_error': None}


def after_failure(rec, now, error=''):
    """The record after one more failure at `now`. Pure."""
    r = dict(rec)
    r['fails'] = int(r.get('fails') or 0) + 1
    if r['fails'] == TRIP_FAILS:
        r['trips'] = int(r.get('trips') or 0) + 1
    r['last_fail'] = float(now)
    r['retry_at'] = float(now) + cooldown(r['fails'])
    r['last_error'] = str(error or '')[:300]
    return r


def after_success(rec, now):
    """The record after a success at `now`: closed, failures cleared. Pure."""
    r = dict(rec)
    r['fails'] = 0
    r['last_ok'] = float(now)
    r['retry_at'] = None
    return r


def verdict(rec, now):
    """(state, allowed, wait_s) for a record at `now`. Pure. `allowed` is
    whether the loop may use the dependency now: always while closed; after
    the cooldown while it still holds failures (state PROBING: the next use,
    or rebalancer.probe_breakers, closes the breaker or re-opens it).
    2026-10-01: a breaker past its cooldown still showed red for hours, until
    a real swap happened to clear it."""
    if not rec or int(rec.get('fails') or 0) <= 0:
        return CLOSED, True, 0.0
    wait = min(max(0.0, float(rec.get('retry_at') or 0.0) - now), float(MAX_S))   # float rounding: never past MAX_S
    if wait <= 0:
        return PROBING, True, 0.0
    return (TRIPPED if int(rec['fails']) >= TRIP_FAILS else BACKOFF), False, wait


# --- persistence ---------------------------------------------------------------

def _db():
    import db
    return db


def load(key):
    """The record of `key` from the table, or an empty one. Never raises."""
    try:
        return _db().health_get(key) or empty(key)
    except Exception:
        return empty(key)


def record_failure(key, error='', now=None):
    """Store one failure of `key`; the new record. Never raises."""
    now = time.time() if now is None else now
    r = after_failure(load(key), now, error)
    try:
        _db().health_put(r)
    except Exception:
        pass
    return r


def record_success(key, now=None):
    """Store a success of `key` when it changes anything; the new record.
    Never raises. A breaker that was already closed is not rewritten on every
    poll, only when its last success is more than a minute old."""
    now = time.time() if now is None else now
    rec = load(key)
    if int(rec.get('fails') or 0) == 0 and rec.get('last_ok') and now - float(rec['last_ok']) < 60:
        return rec
    r = after_success(rec, now)
    try:
        _db().health_put(r)
    except Exception:
        pass
    return r


def allowed(key, now=None):
    """(allowed, state, wait_s, record) for `key` now. Never raises; an
    unreadable table allows (the breaker must not become the outage)."""
    now = time.time() if now is None else now
    rec = load(key)
    state, ok, wait = verdict(rec, now)
    return ok, state, wait, rec


def summary(now=None):
    """Every breaker, for the book: [{key, state, emoji, fails, trips,
    wait_s, last_error}], worst first. Never raises."""
    now = time.time() if now is None else now
    try:
        rows = _db().health_all()
    except Exception:
        return []
    out = []
    for r in rows:
        state, _ok, wait = verdict(r, now)
        out.append({'key': r['key'], 'state': state, 'emoji': EMOJI[state], 'fails': int(r.get('fails') or 0),
                    'trips': int(r.get('trips') or 0), 'wait_s': round(wait), 'last_error': r.get('last_error')})
    order = {TRIPPED: 0, BACKOFF: 1, PROBING: 2, CLOSED: 3}
    return sorted(out, key=lambda x: (order[x['state']], x['key']))
