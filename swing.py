"""The swing: a profile that LPs one pool while its market is open and another
while it is closed (owner, 2026-10-02: DJT/USDC earns 1.14% of its TVL a day
in fees, but its volume follows the US session; after the close it earned
$0.0002 in 20 minutes while SOL/USDC earned $0.07).

One process serves every enabled row of rebalancer.swing (sql/023), whatever
its wallet. It moves nothing itself: each minute it decides which pool each
swing profile should hold (its calendar's session), and when the profile's
pool differs it writes run/<profile>/MIGRATE ("<dex> <pool>"), which the
profile's own loop executes with every guard it has: harvest, close,
repoint, sell what the old pair left behind (rebalancer.sell_left_behind),
swap to 50/50, open. That loop accepts a pair change only to a pool its
service environment pins (LPBOT_SWING_POOLS), so neither this process nor a
row can redirect money.

It is also each switch's auditor: within SWITCH_DEADLINE_S of a request the
profile must hold an open position on the wanted pool (once it has, the
switch has arrived, and a later re-centre's close-to-open gap is no lateness),
and within
LEFTOVER_DEADLINE_S nothing the old pair left may remain; otherwise one
SWING_LATE / SWING_LEFTOVER alert per switch, on run/swing/events.jsonl (the
Telegram bridge tails it) and in the events table, under the profile.

    python3 swing.py              # the service (ops/lp-swing.service)
    python3 swing.py --once       # one decision per row, printed; writes nothing
"""
import datetime as dt
import json
import pathlib
import sys
import time
from zoneinfo import ZoneInfo

import db

ROOT = pathlib.Path(__file__).resolve().parent
FEED = ROOT / 'run' / 'swing' / 'events.jsonl'
STATE = ROOT / 'run' / 'swing' / 'state.json'

TICK_S = 60
REQUEST_AGAIN_S = 900             # a request the loop has not acted on is written again after this
SWITCH_DEADLINE_S = 900           # the wanted pool must hold a position by then
LEFTOVER_DEADLINE_S = 1200        # and nothing the old pair left may remain


class CalendarEnded(Exception):
    """A calendar's holiday table does not cover this date: never guess a session."""


class Calendar:
    """A market's regular session: local open and close, the early-close time,
    full-day holidays, early-close days, and the last date the tables cover."""

    def __init__(self, tz, open_t, close_t, early_close_t, holidays, early_closes, ends):
        self.tz, self.open_t, self.close_t, self.early_close_t = ZoneInfo(tz), open_t, close_t, early_close_t
        self.holidays, self.early_closes, self.ends = frozenset(holidays), frozenset(early_closes), ends

    def session(self, day):
        """(open, close) aware datetimes of `day`'s session, or None when the
        market is closed all day. Raises CalendarEnded past the tables. Pure."""
        if day > self.ends:
            raise CalendarEnded(f'the calendar ends {self.ends}; extend its holidays')
        if day.weekday() >= 5 or day in self.holidays:
            return None
        close = self.early_close_t if day in self.early_closes else self.close_t
        return dt.datetime.combine(day, self.open_t, self.tz), dt.datetime.combine(day, close, self.tz)

    def is_open(self, now_utc, lead_s=0):
        """Whether the session is open at `now_utc` (aware), counting it open
        `lead_s` seconds before the bell. Pure."""
        t = now_utc.astimezone(self.tz)
        s = self.session(t.date())
        return s is not None and s[0] - dt.timedelta(seconds=lead_s) <= t < s[1]


def _days(*ds):
    return {dt.date(*d) for d in ds}


# A row's `calendar` names one of these (sql/023 checks the names).
# NYSE: full-day holidays and 1 pm closes (nyse.com, "Holidays & Trading Hours").
CALENDARS = {
    'nyse': Calendar(
        'America/New_York', dt.time(9, 30), dt.time(16, 0), dt.time(13, 0),
        _days((2026, 1, 1), (2026, 1, 19), (2026, 2, 16), (2026, 4, 3), (2026, 5, 25), (2026, 6, 19), (2026, 7, 3),
              (2026, 9, 7), (2026, 11, 26), (2026, 12, 25),
              (2027, 1, 1), (2027, 1, 18), (2027, 2, 15), (2027, 3, 26), (2027, 5, 31), (2027, 6, 18), (2027, 7, 5),
              (2027, 9, 6), (2027, 11, 25), (2027, 12, 24)),
        _days((2026, 11, 27), (2026, 12, 24), (2027, 11, 26)),
        dt.date(2027, 12, 31)),
}


def wanted(now_utc, row):
    """(dex, pool) the swing row should hold at `now_utc`. Pure."""
    if CALENDARS[row['calendar']].is_open(now_utc, row['lead_s']):
        return row['open_dex'], row['open_pool']
    return row['closed_dex'], row['closed_pool']


def decide(now_utc, row, last_request):
    """('hold' | 'request', (dex, pool) wanted) for one swing row this tick:
    'request' when the profile's pool (row['held_pool']) is not the wanted
    one and no request for it was written in the last REQUEST_AGAIN_S.
    `last_request` is {'pool', 'at'} or None. Pure."""
    want = wanted(now_utc, row)
    if row['held_pool'] == want[1]:
        return 'hold', want
    if last_request and last_request.get('pool') == want[1] and \
            now_utc.timestamp() - float(last_request.get('at') or 0) < REQUEST_AGAIN_S:
        return 'hold', want
    return 'request', want


def audit(now_s, request, position_pool, left_behind):
    """The alerts a switch has earned by `now_s`: [('SWING_LATE' |
    'SWING_LEFTOVER', why)]. `request` is {'pool', 'at', 'arrived'?} (the last
    switch; 'arrived' once a position opened on its pool), `position_pool` the
    pool of the profile's open position (None: none), `left_behind` the
    profile's unsold leftovers. An arrived switch is never late: on 2026-10-08
    a re-centre's 33 s with no position read as "883 min after the switch ...
    on no pool". Pure."""
    if not request:
        return []
    age = now_s - float(request['at'])
    out = []
    if age > SWITCH_DEADLINE_S and not request.get('arrived') and position_pool != request['pool']:
        out.append(('SWING_LATE', f'{age / 60:.0f} min after the switch to {request["pool"]} the position is on '
                                  f'{position_pool or "no pool"}'))
    if age > LEFTOVER_DEADLINE_S and left_behind:
        out.append(('SWING_LEFTOVER', f'{age / 60:.0f} min after the switch, still unsold: {", ".join(left_behind)}'))
    return out


# --- the process -----------------------------------------------------------------

def rows():
    """Every enabled swing row with its profile's pool, enabled flag, wallet
    and the pool of its open position."""
    with db.cursor() as cur:
        cur.execute("""select s.*, c.pool held_pool, c.enabled profile_enabled, c.wallet_id,
                              (select p.pool from positions p where p.config_name = s.profile and p.closed_at is null
                                order by p.opened_at desc limit 1) position_pool
                         from swing s join config c on c.name = s.profile
                        where s.enabled order by s.profile""")
        return [dict(r) for r in cur.fetchall()]


def feed(profile, event, **kw):
    """One row on FEED (the Telegram bridge tails it) and in the events table."""
    row = {'t': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'event': event,
           'profile': profile, **kw}
    FEED.parent.mkdir(parents=True, exist_ok=True)
    with FEED.open('a') as f:
        f.write(json.dumps(row) + '\n')
    try:
        db.event(event, json.dumps(kw, default=str))
    except Exception:
        pass
    print(json.dumps(row), flush=True)


def left_behind(profile):
    """The profile's unsold leftovers (its runtime.json), [] when unreadable."""
    try:
        return json.loads((ROOT / 'run' / profile / 'runtime.json').read_text()).get('left_behind') or []
    except Exception:
        return []


def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s))
    tmp.replace(STATE)


def tick(row, state, now_utc, dry=False):
    """One decision for one swing row: request the switch when due, audit
    the last one. `state` is this profile's part of STATE, changed in place.
    Returns 'hold', 'request', or None for a disabled profile."""
    profile = row['profile']
    db.set_context(profile, row['wallet_id'])
    if not row['profile_enabled']:
        return None
    action, want = decide(now_utc, row, state.get('request'))
    if action == 'request':
        migrate = ROOT / 'run' / profile / 'MIGRATE'
        if dry:
            print(f'{profile}: would write {migrate}: {want[0]} {want[1]} (holds {row["held_pool"]})', flush=True)
            return action
        migrate.parent.mkdir(parents=True, exist_ok=True)
        migrate.write_text(f'{want[0]} {want[1]}\n')
        state.update(request={'pool': want[1], 'dex': want[0], 'at': now_utc.timestamp(), 'from': row['held_pool']},
                     told=[])
        feed(profile, 'SWING', to=f'{want[0]} {want[1]}', held=row['held_pool'],
             market='open' if want[1] == row['open_pool'] else 'closed')
        return action
    request = state.get('request')
    if not dry and request and not request.get('arrived') and row['position_pool'] == request['pool']:
        request['arrived'] = now_utc.timestamp()       # the switch is done; re-centres after it are not late
    for kind, why in audit(now_utc.timestamp(), request, row['position_pool'], left_behind(profile)):
        if kind not in state.setdefault('told', []):
            state['told'].append(kind)
            feed(profile, kind, reason=why)
    if dry:
        print(f'{profile}: hold {want[0]} {want[1]} (position on {row["position_pool"]})', flush=True)
    return action


def tick_all(now_utc, dry=False):
    """tick() for every enabled row; one row's failure never stops the others."""
    state = load_state()
    for row in rows():
        mine = state.setdefault(row['profile'], {})
        try:
            tick(row, mine, now_utc, dry)
        except CalendarEnded as e:
            if not dry and not mine.get('calendar_told'):
                mine['calendar_told'] = True
                feed(row['profile'], 'swing_refused', reason=str(e))
        except Exception as e:
            print(f'swing {row["profile"]}: tick failed: {type(e).__name__}: {e}', flush=True)
    if not dry:
        save_state(state)


def main(argv):
    if any(a != '--once' for a in argv):
        print(__doc__, file=sys.stderr)
        return 2
    if '--once' in argv:
        tick_all(dt.datetime.now(dt.timezone.utc), dry=True)
        return 0
    print(f'swing: serving {", ".join(r["profile"] for r in rows()) or "no row"}', flush=True)
    while True:
        try:
            tick_all(dt.datetime.now(dt.timezone.utc))
        except Exception as e:                       # a bad tick never stops the swing
            print(f'swing tick failed: {type(e).__name__}: {e}', flush=True)
        time.sleep(TICK_S)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
