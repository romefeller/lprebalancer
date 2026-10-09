"""Standing aside: the hot pause and the macro (FOMC) pause."""

import time
from datetime import datetime, timezone

import calm
import config
import db
import health
from venues import solana_state
from lp import board, books, capital, harvest, housekeeping, moves, paths, regime, swaps, tuning

HOT_PAUSE_TELL_S = 1800          # a waiting pause says so at most this often


def hot_pause_on(pool):
    """Whether the HOT pause guards `pool`: switched on, and `pool` in
    HOT_PAUSE_POOLS when that list is set (sql/033: the swing pauses its
    SOL/USDC hours and keeps DJT through HOT ones)."""
    return config.HOT_PAUSE_ENABLED and (not config.HOT_PAUSE_POOLS or pool in config.HOT_PAUSE_POOLS)


def hot_pause_view(pool, usd_a, usd_b, choice):
    """The bad-moment signal (sql/029): the width choice above
    +/-HOT_PAUSE_HOT_PCT and the pool's fees over the last HOT_PAUSE_FG_HOURS
    under HOT_PAUSE_FG_THRESHOLD x the in-band loss of the same hours. Prices
    are pool-native (the counters count raw units). A reading that cannot be
    made is not bad: no data never pauses."""
    out = {'hot': None, 'ratio': None, 'bad': False}
    if choice is None:
        return out
    out['hot'] = choice > 1 + config.HOT_PAUSE_HOT_PCT / 100
    if not out['hot']:
        return out
    hours = config.HOT_PAUSE_FG_HOURS
    try:
        span = db.fee_state_span(pool, hours=max(int(round(hours)), 1))
        if not span or span[2] < tuning.HOT_PAUSE_MIN_COVERAGE * hours * tuning.HOUR_S:
            return out
        first, last, _ = span
        t0, t1 = first['ts'].timestamp(), last['ts'].timestamp()
        bars = db.tape_load(pool, t0)
        if bars is None:
            return out
        out['ratio'] = calm.fee_loss_ratio(solana_state.fee_yield(first, last, usd_a, usd_b), bars[0], bars[4], t0, t1)
    except Exception as e:
        books.notify('hot_pause_unread', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return out
    if out['ratio'] is not None:
        out['ratio'] = round(out['ratio'], 3)
        out['bad'] = out['ratio'] < config.HOT_PAUSE_FG_THRESHOLD
    return out


def hot_pause_swap(state, p):
    """The paused capital to 50/50, once per pause, whatever the outcome:
    a failed swap counts its failure and the pause waits as it is."""
    p['swapped'] = True
    paths.save(state)
    bal = capital.wallet(config.POOL)
    if 'balanceA' not in bal:
        return
    try:
        rec = capital.pool_record()
    except Exception:
        rec = None
    swaps.balance_wallet(state, bal, rec, share_a=0.5)     # wait as a holder: half in each token


MACRO_CALENDAR_DAYS = 60          # warn when no macro event is listed this far ahead
MACRO_UNREAD_TELL_S = 3600        # an unreadable calendar says so at most this often


def macro_view(state=None):
    """The scheduled macro window open now (sql/030): {'ts', 'kind', 'until'},
    or None (switched off, no event near, or the calendar unreadable). Once
    a day, says so when the calendar lists nothing in MACRO_CALENDAR_DAYS."""
    if not config.MACRO_PAUSE_ENABLED:
        return None
    try:
        if state is not None and time.time() - state.get('macro_calendar_checked', 0) >= tuning.DAY_S:
            state['macro_calendar_checked'] = time.time(); paths.save(state)
            nxt = db.macro_next_ts()
            if nxt is None or nxt - time.time() > MACRO_CALENDAR_DAYS * tuning.DAY_S:
                books.notify('macro_calendar_empty', reason=f'no macro event listed in the next {MACRO_CALENDAR_DAYS} days: '
                                                      'add the next FOMC dates to rebalancer.macro_events')
        m = db.macro_event_near(config.MACRO_PAUSE_BEFORE_S, config.MACRO_PAUSE_AFTER_S)
    except Exception as e:
        if state is None or time.time() - state.get('macro_unread_told', 0) >= MACRO_UNREAD_TELL_S:
            if state is not None:
                state['macro_unread_told'] = time.time(); paths.save(state)
            books.notify('macro_unread', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return None
    if not m:
        return None
    return dict(m, until=m['ts'] + config.MACRO_PAUSE_AFTER_S)


def macro_hold(state):
    """No band held and a macro window open: record a macro pause (the wallet
    waits 50/50) instead of opening one the next poll would close. True when
    the poll ends here; a reopen intent left by a failed open is kept for
    after the window."""
    m = macro_view(state)
    if not m:
        return False
    now = time.time()
    when = datetime.fromtimestamp(m['ts'], timezone.utc).strftime('%m-%d %H:%M')
    why = f"macro pause: {m['kind']} at {when} UTC; no band held, waiting 50/50"
    state['hot_pause'] = {'since': now, 'last_bad': now, 'pool': config.POOL, 'ratio': None, 'told': now,
                          'mint': None, 'withdraw_usd': None, 'reason': why, 'booked': True, 'swapped': False,
                          'kind': 'macro', 'until': m['until']}
    paths.save(state)
    books.notify_book('MACRO_PAUSE', kind=m['kind'], event_at=when, resume_minutes=round((m['until'] - now) / 60),
                      held=False)
    db.event('MACRO_PAUSE', why)
    return True


def hot_pause(state, status, rv):
    """A held band in a scheduled macro window, or in a bad HOT moment:
    harvest, close, and wait 50/50. True when it closed (or tried to), so
    the poll ends here."""
    state.pop('hot_pause', None)                     # a held band is never paused
    now = time.time()
    m = macro_view(state)
    if m:
        when = datetime.fromtimestamp(m['ts'], timezone.utc).strftime('%m-%d %H:%M')
        # No calm gap: the window has a start time. The budget keeps the hard
        # ceiling's halt away; a venue in backoff gets no voluntary close.
        blocked = regime.calm_budget_left(state) <= 0 or not health.allowed(f'venue:{config.DEX}', now)[0]
        if blocked:
            if state.get('macro_blocked_told') != m['ts']:
                state['macro_blocked_told'] = m['ts']; paths.save(state)
                books.notify('macro_blocked', kind=m['kind'], event_at=when,
                             reason='the move budget is spent or the venue is in backoff: the band stays open')
            return False
        why = f"macro pause: {m['kind']} at {when} UTC; waiting 50/50 until {config.MACRO_PAUSE_AFTER_S // 60} min after it"
        books.notify_book('MACRO_PAUSE', price=status['price'], lower=status['lowerPrice'], upper=status['upperPrice'],
                          kind=m['kind'], event_at=when, resume_minutes=round((m['until'] - now) / 60), held=True, regime=rv)
        db.event('MACRO_PAUSE', why)
        return hot_pause_close(state, status, why, now, kind='macro', until=m['until'])
    pool = status.get('whirlpool') or config.POOL
    if not hot_pause_on(pool):
        return False
    q = status.get('quoteUsd')
    if q is None or not rv or rv.get('stale'):
        return False
    if now - state.get('hot_pause_resumed', 0) < config.HOT_PAUSE_COOLDOWN_S:
        return False                                 # just resumed: no pause straight back
    v = hot_pause_view(pool, status['price'] * q, q, rv.get('choice'))
    if calm.hot_pause_step(None, v['bad'], now, resume_s=config.HOT_PAUSE_RESUME_S,
                           max_s=config.HOT_PAUSE_MAX_S) != 'pause':
        return False
    if regime.calm_budget_left(state) <= 0 or not regime.voluntary_move_allowed(state):
        return False
    why = (f"hot pause: +/-{rv.get('choice_pct')}% chosen and fees {v['ratio']}x the in-band loss "
           f"over {config.HOT_PAUSE_FG_HOURS:g}h (< {config.HOT_PAUSE_FG_THRESHOLD:g})")
    books.notify_book('HOT_PAUSE', price=status['price'], lower=status['lowerPrice'], upper=status['upperPrice'],
                      ratio=v['ratio'], threshold=config.HOT_PAUSE_FG_THRESHOLD, hours=config.HOT_PAUSE_FG_HOURS,
                      resume_minutes=config.HOT_PAUSE_RESUME_S // 60, regime=rv)
    db.event('HOT_PAUSE', why)
    return hot_pause_close(state, status, why, now, ratio=v['ratio'])


def hot_pause_close(state, status, why, now, kind='hot', until=None, ratio=None):
    """The pause's close: record the pause, harvest and close the band, then
    swap to 50/50. True: the poll ends here whether the close landed or not."""
    mint = status['positionMint']
    # Recorded before the close: a restart after it lands keeps waiting, and
    # books the close itself if this process stopped before rebalance did.
    state['hot_pause'] = {'since': now, 'last_bad': now, 'pool': config.POOL, 'ratio': ratio, 'told': now,
                          'mint': mint, 'withdraw_usd': capital.position_usd(status), 'reason': why,
                          'booked': False, 'swapped': False, 'kind': kind, 'until': until}
    paths.save(state)
    moves.rebalance(state, status, why, calm_move=True, exit_move=True, close_only=True)
    # The ledger says whether the close landed: rebalance books it only then
    # (or when a reported failure proved to have landed). A fresh chain read
    # could lag behind the close.
    if not db.position_closed(mint):
        state.pop('hot_pause', None); paths.save(state)    # the close did not land: nothing waits
        return True
    p = state['hot_pause']
    p['booked'] = True
    hot_pause_swap(state, p)
    return True


def hot_paused(state):
    """Paused: True to keep waiting this poll; False when the pause is over
    and the loop reopens as usual. A macro pause ends when its window does
    (a window opening during any pause extends it); a HOT pause when the
    signal has been clear for HOT_PAUSE_RESUME_S or HOT_PAUSE_MAX_S is
    reached. Either ends at once when its switch is off or the pool changed."""
    p = state['hot_pause']
    now = time.time()
    kind = p.get('kind')                             # 'macro', or a HOT pause ('hot' / older: none)
    if not p.get('booked'):
        # The process stopped between the close and its bookkeeping: the chain
        # holds no position (this is the no-position path), so book the close.
        if p.get('mint') and db.position_closed(p['mint']) is False:
            db.close_position(p['mint'], None, p.get('withdraw_usd'))
            harvest.band_profile(p['mint'], 'rebalance', p.get('reason'))
            state['calm_times'] = state.get('calm_times', []) + [p['since']]
        p['booked'] = True
        paths.save(state)
    enabled = config.MACRO_PAUSE_ENABLED if kind == 'macro' else hot_pause_on(config.POOL)
    if p.get('pool') != config.POOL and p.get('until') and now < p['until']:
        # A pool switch inside a window (the swing's move at the NYSE close):
        # the window still holds; wait on the new pool, 50/50 in its tokens.
        p['pool'] = config.POOL; p['swapped'] = False; paths.save(state)
    if not enabled or p.get('pool') != config.POOL:
        state.pop('hot_pause', None)
        if kind != 'macro':
            state['hot_pause_resumed'] = now
        paths.save(state)
        books.notify('HOT_RESUME', reason='the pause is switched off or the pool changed',
                     paused_minutes=round((now - p.get('since', now)) / 60))
        return False
    if not p.get('swapped'):
        hot_pause_swap(state, p)
    board.sample_fee_growth(state)
    books.daily_report(state)
    housekeeping.run_audits(state)
    m = macro_view(state)
    if m:
        p['until'] = max(p.get('until') or 0, m['until'])
    if p.get('until') and now < p['until']:
        if now - p.get('told', 0) >= HOT_PAUSE_TELL_S:
            p['told'] = now
            books.notify('hot_paused', paused_minutes=round((now - p['since']) / 60), ratio=None, hot=None,
                         clear_minutes=0, until_minutes=round((p['until'] - now) / 60), kind='macro')
        paths.save(state)
        return True
    if kind == 'macro':
        state.pop('hot_pause', None); paths.save(state)    # no HOT cooldown: after FOMC is when HOT is likely
        why = 'the macro window is over'
        books.notify('HOT_RESUME', reason=why, paused_minutes=round((now - p['since']) / 60))
        db.event('HOT_RESUME', f"{why}; paused {round((now - p['since']) / 60)} min")
        return False
    bal = capital.wallet(config.POOL)
    q, price = bal.get('quoteUsd'), bal.get('price')
    v = {'hot': None, 'ratio': None, 'bad': False}
    if q is not None and price:
        v = hot_pause_view(config.POOL, price * q, q, regime.regime_choice_now(config.POOL, price))
    if v['bad']:
        p['last_bad'] = now
    step = calm.hot_pause_step(p, v['bad'], now, resume_s=config.HOT_PAUSE_RESUME_S, max_s=config.HOT_PAUSE_MAX_S)
    if step == 'resume':
        state.pop('hot_pause', None); state['hot_pause_resumed'] = now; paths.save(state)
        why = 'the pause reached its limit' if now - p['since'] >= config.HOT_PAUSE_MAX_S else \
            f'clear for {config.HOT_PAUSE_RESUME_S // 60} min'
        books.notify('HOT_RESUME', reason=why, paused_minutes=round((now - p['since']) / 60),
                     ratio=v['ratio'], hot=v['hot'])
        db.event('HOT_RESUME', f"{why}; paused {round((now - p['since']) / 60)} min")
        return False
    if now - p.get('told', 0) >= HOT_PAUSE_TELL_S:
        p['told'] = now
        books.notify('hot_paused', paused_minutes=round((now - p['since']) / 60), ratio=v['ratio'], hot=v['hot'],
                     clear_minutes=round((now - p['last_bad']) / 60))
    paths.save(state)
    return True
