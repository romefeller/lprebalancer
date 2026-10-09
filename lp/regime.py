"""The width choice: calm and regime views, touch forecasts, move limits, the reopen width."""

import time

import calm
import config
import db
import health
from lp import books, harvest, moves, paths, tape, tuning

def voluntary_move_allowed(state):
    """Whether a calm or regime move would pass rebalance()'s gap now, and
    the held venue's breaker allows its transactions (a venue in backoff gets
    no voluntary move; exits and failover still run). The loop asks first,
    so a move held back is not announced on every poll (review, 2026-09-26:
    up to five duplicate messages a move)."""
    now = time.time()
    return (move_gap_ok(state.get('calm_times', []), state.get('last_rebalance', 0), now, config.CALM_MIN_GAP)
            and health.allowed(f'venue:{config.DEX}', now)[0])


def move_gap_ok(calm_times, last_rebalance, now, min_gap):
    """Whether `min_gap` seconds have passed since the last move: the last
    rebalance or a voluntary move of the last 24 h. Pure."""
    recent = [t for t in calm_times if now - t < tuning.DAY_S]
    return now - max([last_rebalance] + recent) >= min_gap


def calm_budget_left(state):
    return moves_left(state.get('calm_times', []), time.time(), config.CALM_MAX_MOVES)


def moves_left(calm_times, now, max_moves):
    """Voluntary moves still allowed in the 24 h before `now`. Pure."""
    used = [t for t in calm_times if now - t < tuning.DAY_S]
    return max(max_moves - len(used), 0)


def calm_view(state, status):
    """calm.view for the held position, or None when calm mode is off or the
    five-minute tape is unavailable. Remembers the calm state across polls,
    which the hysteresis needs."""
    if not config.CALM_ENABLED:
        return None
    bars = tape.tape5(status.get('whirlpool') or config.POOL, status['price'])
    v = calm.view(bars, status['price'], status['lowerPrice'], status['upperPrice'],
                  was_calm=bool(state.get('calm')), cut=config.CALM_SIGMA_CUT,
                  exit_mult=config.CALM_EXIT_MULT, band=config.CALM_BAND,
                  horizon_minutes=config.CALM_HORIZON_MINUTES, threshold=config.CALM_THRESHOLD)
    if v:
        if bool(state.get('calm')) != v['calm']:
            state['calm'] = v['calm']; paths.save(state)
            db.event('CALM_ON' if v['calm'] else 'CALM_OFF',
                     f"sigma {v['sigma_5m_pct']}% vs cut {v['cut_pct']}%")
        v['budget_left'] = calm_budget_left(state)
        v['moves_24h'] = config.CALM_MAX_MOVES - v['budget_left']
        books.LAST_CALM['view'] = v
    return v
REGIME_UNSTALE_S = 900          # a tape back from STALE must stay fresh this long


def track_tape_source(state, src):
    """An event and a Telegram line when the tape's source changes between
    GeckoTerminal only, GeckoTerminal with a surrogate, and none (stale).
    'Binance' and 'Gecko+Binance' are one state here, so bars that come and
    go one at a time do not repeat the message."""
    kind = 'none' if src['source'] == 'none' else 'surrogate' if src['filled_1h'] else 'gecko'
    was = state.get('tape_source_kind')
    if was == kind:
        return
    state['tape_source_kind'] = kind; paths.save(state)
    if was is None and kind == 'gecko':
        return                                        # first poll, all normal: nothing to say
    detail = (f"{src['source']}: {src['surrogate']} fills {src['filled_1h']} of {src['bars_1h']} bars "
              f"in the last hour (GeckoTerminal lacks them)" if kind == 'surrogate'
              else 'none: GeckoTerminal and every surrogate lack recent bars (STALE)' if kind == 'none'
              else 'Gecko: GeckoTerminal complete again, surrogate off')
    db.event('TAPE_SOURCE', detail)
    books.notify('TAPE_SOURCE', source=src['source'], was=was, kind=kind, detail=detail)


def regime_theta(factor):
    """The touch threshold for the regime's width choice: the configured one
    times the pool's liquidity factor, kept inside [REGIME_THETA_MIN,
    REGIME_THETA_MAX]."""
    return min(max(config.REGIME_THRESHOLD * factor, tuning.REGIME_THETA_MIN), tuning.REGIME_THETA_MAX)


def regime_view(state, status):
    """calm.regime_view for the held position, or None when regime mode is
    off or the five-minute tape is unavailable."""
    if not config.REGIME_ENABLED:
        return None
    pool = status.get('whirlpool') or config.POOL
    bars = tape.tape5(pool, status['price'])
    if bars is None:
        return None
    lq = tape.liquidity_view(pool, config.DEX, bars)
    theta = regime_theta(lq['factor'])
    v = calm.regime_view(bars, status['price'], status['lowerPrice'], status['upperPrice'],
                         widths=config.REGIME_WIDTHS, horizon_minutes=config.REGIME_HORIZON,
                         threshold=theta)
    raw_fresh = fresh = calm.tape_fresh(bars[0], time.time())
    hold_left = 0
    if fresh:
        # A tape that just came back must stay complete REGIME_UNSTALE_S
        # before the bot leaves STALE: sources that flicker cannot flip the
        # band between the widest width and a tight one.
        if state.get('regime_mode') == 'STALE':
            since = state.get('tape_fresh_since')
            if since is None:
                state['tape_fresh_since'] = since = time.time(); paths.save(state)
            hold_left = max(0, int(REGIME_UNSTALE_S - (time.time() - since)))
            fresh = hold_left == 0
    elif state.get('tape_fresh_since') is not None:
        state['tape_fresh_since'] = None; paths.save(state)
    if v and not fresh:
        # The newest bar is old, or the last half hour has a gap (a data
        # outage of GeckoTerminal and Binance both): the tape no longer
        # describes the market. Choose the widest width, so an exit never
        # reopens tight into a market nobody is measuring, and narrowing
        # cannot happen (review, 2026-09-26: a 6-hour-old calm tape chose
        # +/-1%; 2026-09-29: a lone bar after a 25-minute gap passed the
        # age check and the bot narrowed, then widened again).
        v = dict(v, choice=config.REGIME_WIDTHS[-1],
                 choice_pct=round((config.REGIME_WIDTHS[-1] - 1) * 100, 2), mode='STALE', stale=True,
                 unstale_in_s=hold_left if raw_fresh else None)
    if v:
        v['threshold_base'] = config.REGIME_THRESHOLD
        v['steps'] = config.REGIME_STEPS
        v['liquidity'] = lq
        books.LAST_REGIME['risk'] = calm.risk_metrics(bars)
        v['moves_24h'] = config.CALM_MAX_MOVES - calm_budget_left(state)
        v['guard'] = config.CALM_MAX_MOVES
        src = dict(tape.LAST_SURROGATE.get(pool) or tape.tape_source(bars[0], [], time.time(), None, raw_fresh))
        if not raw_fresh:
            src['source'] = 'none'
        v['data'] = src
        track_tape_source(state, src)
        if state.get('regime_mode') != v['mode']:
            db.event('REGIME', f"{state.get('regime_mode')} -> {v['mode']}: choice +/-{v['choice_pct']}% "
                               f"sigma {v['sigma_5m_pct']}% velocity {v['velocity']}")
            state['regime_mode'] = v['mode']; paths.save(state)
        books.LAST_REGIME['view'] = v
    return v


FORECAST_SAMPLE_S = 600         # one touch forecast recorded per ten minutes


def track_touch_forecasts(state, rv, status):
    """Record the regime's forecast (every width, centred on the price) at
    most every FORECAST_SAMPLE_S, and resolve the ones whose horizon has
    passed. Stale views are not recorded: they are not forecasts."""
    if not rv or rv.get('stale') or not rv.get('probs'):
        return
    pool = status.get('whirlpool') or config.POOL
    try:
        if time.time() - state.get('last_touch_forecast', 0) >= FORECAST_SAMPLE_S:
            state['last_touch_forecast'] = time.time(); paths.save(state)
            db.record_touch_forecast(pool, status['price'], rv['horizon_minutes'], rv['threshold'],
                                     rv['choice'], rv['probs'])
            db.resolve_touch_forecasts(pool)
        if time.time() - state.get('last_touch_calibration', 0) >= tuning.TOUCH_CALIBRATION_EVERY_S:
            state['last_touch_calibration'] = time.time(); paths.save(state)
            books.LAST_REGIME['calibration'] = db.touch_calibration(7, pool)
    except Exception as e:
        books.notify('forecast_track_failed', reason=books.tidy(e))
    if books.LAST_REGIME.get('calibration'):
        rv['calibration'] = books.LAST_REGIME['calibration']


def record_risk(status, rv, fc):
    """The risk profile of this poll, to rebalancer.risk_profile. Never
    blocks the loop: a failure is reported and the poll goes on."""
    if not rv:
        return
    try:
        db.record_risk_profile(status.get('whirlpool') or config.POOL, status.get('positionMint'),
                               status['price'], rv, books.LAST_REGIME.get('risk'), fc)
    except Exception as e:
        books.notify('risk_record_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')


EDGE_MATCH = 0.02                 # a watch read further than this from the poll's price is not the same price


def edge_sleep(total_s, status, read=None, sleep=None, clock=None):
    """Sleep out the poll, or, while the price sits within EDGE_WATCH_PCT of a
    band edge, read the pool's price every EDGE_WATCH_S and return as soon as
    it leaves the band, so the exit re-centres seconds after the crossing,
    not up to a poll later (2026-10-03: exits were seen 0.15% past the edge
    on average with a 120 s poll). A read that fails, or disagrees with the
    poll's price by more than EDGE_MATCH (other units), ends the watch: the
    rest of the poll is slept as before. Returns 'exit', 'slept' or 'off'."""
    sleep, clock = sleep or time.sleep, clock or time.monotonic     # looked up now: tests patch time
    lo, hi, p = status.get('lowerPrice'), status.get('upperPrice'), status.get('price')
    if not calm.near_edge(p, lo, hi, config.EDGE_WATCH_PCT):
        sleep(total_s)
        return 'off'
    read = read or (lambda: tape.pool_price_now(config.DEX, status.get('whirlpool') or config.POOL))
    end = clock() + total_s
    while True:
        sleep(min(config.EDGE_WATCH_S, end - clock()))
        if clock() >= end:
            return 'slept'
        now_p = read()
        if now_p is None or abs(now_p - p) > EDGE_MATCH * p:
            sleep(max(0.0, end - clock()))
            return 'slept'
        verdict = calm.watch_verdict(now_p, lo, hi, config.EDGE_WATCH_PCT)
        if verdict == 'exit':
            books.notify('edge_watch', price=now_p, lower=lo, upper=hi, action='left the band: polling now')
            return 'exit'
        if verdict == 'away':
            sleep(max(0.0, end - clock()))
            return 'slept'


def reopen_width(k, pool, price):
    """The width an exit reopens at: `k`, or reopen_widen_band for this one
    band when `k` is the narrowest width and P(touch it within
    reopen_widen_minutes) is at least reopen_widen_p on a fresh tape
    (sql/026; a +/-1% band reopened into a likely touch is a re-centre
    paid for nothing). Regime mode only: calm mode would hand a 1.5% band to
    the hourly rules (audit 2026-10-03). Any failure keeps `k`."""
    if not (config.REGIME_ENABLED and config.REOPEN_WIDEN_P) or k != min(config.REGIME_WIDTHS):
        return k
    try:
        bars = tape.tape5(pool, price)
        if bars is None or not calm.tape_fresh(bars[0], time.time()):
            return k
        p = calm.p_touch_width(bars, k, config.REOPEN_WIDEN_MIN)
    except Exception as e:
        books.notify('reopen_widen_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return k
    if p is None or p < config.REOPEN_WIDEN_P:
        return k
    books.notify('REOPEN_WIDE', band_pct=round((config.REOPEN_WIDEN_BAND - 1) * 100, 2), p_touch=round(p, 3),
                 horizon_minutes=config.REOPEN_WIDEN_MIN)
    return config.REOPEN_WIDEN_BAND


def regime_choice_now(pool, price, pair=None):
    """The regime's width for a fresh band at `price` on `pool`, or None."""
    if not config.REGIME_ENABLED:
        return None
    bars = tape.tape5(pool, price, pair)
    if bars is None:
        return None
    try:
        dex = config.DEX if pool == config.POOL else None
        f = tape.liquidity_view(pool, dex, bars)['factor'] if dex else 1.0
    except Exception:
        f = 1.0
    theta = regime_theta(f)
    v = calm.regime_view(bars, price, price / 1.01, price * 1.01, widths=config.REGIME_WIDTHS,
                         horizon_minutes=config.REGIME_HORIZON, threshold=theta)
    if not v or not calm.tape_fresh(bars[0], time.time()):
        return None
    return v['choice']


def calm_reopen_band(v, state):
    """The band to reopen at after a tight band exits: tight again while calm,
    budget left and a fresh tight band is unlikely to be touched soon;
    otherwise None, which means the ladder's band."""
    return tight_reopen(v, calm_budget_left(state), config.CALM_BAND, config.CALM_THRESHOLD)


def tight_reopen(v, budget_left, band, threshold):
    """calm_reopen_band on its inputs: `band` while calm, with budget left
    and P(touch) of a fresh tight band under `threshold`, else None. Pure."""
    if not v or not v.get('calm') or budget_left <= 0:
        return None
    pf = v.get('p_touch_fresh')
    if pf is not None and pf >= threshold:
        return None
    return band


def resume_reopen(state):
    """Resume a confirmed CALM close without a pool review or a second move.

    The persisted intent survives a swap/open failure and a service restart.
    Reopen rechecks current conditions; missing data means wait, not wide-then-tight.
    """
    pending = state.get('pending_reopen')
    if not pending:
        return False
    if time.time() - pending.get('started_at', 0) > tuning.PENDING_REOPEN_MAX_S:
        state.pop('pending_reopen', None); paths.save(state)
        books.notify('idle', reason='dropped a CALM reopen intent older than a day')
        return False
    if (pending['pool'], pending['dex']) != (config.POOL, config.DEX):
        books.halt('pending reopen belongs to a different pool; refusing to spend its funds elsewhere')
        return True
    if not pending.get('closed'):
        # The process may have stopped after close landed but before recording it.
        db.close_position(pending['mint'], None, pending.get('withdraw_usd'))
        harvest.band_profile(pending['mint'], 'rebalance', pending.get('reason'))
        calm_times = state.setdefault('calm_times', [])
        if pending['started_at'] not in calm_times:
            calm_times.append(pending['started_at'])
        pending['closed'] = True
        paths.save(state)
    if config.REGIME_ENABLED and time.time() - pending.get('started_at', 0) > tuning.PENDING_REOPEN_WIDE_S:
        # Half an hour without fresh data: open at the widest regime width
        # rather than leave the capital idle for a day (review, 2026-09-26).
        moves.reopen(state, pending['reason'], band=config.REGIME_WIDTHS[-1])
        return True
    moves.reopen(state, pending['reason'], band=pending['band'], recovering=True)
    return True
