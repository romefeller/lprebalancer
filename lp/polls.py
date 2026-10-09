"""One poll's verdict, recorded for the replay harness."""

import json
import time

import calm
import config
import db
import health
from lp import board, books, harvest, regime, tuning

# --- replay testing: each poll's market decision as a pure function ----------
#
# poll_seen gathers what a poll knows; poll_verdict turns it into the poll's
# move, reading nothing else. The loop acts on that verdict and stores both
# (sql/032), so tests/test_replay.py can run recorded polls through the
# current code and fail on any verdict that changed.

SEEN_REGIME = ('mode', 'choice', 'choice_pct', 'held', 'held_pct', 'inside', 'stale', 'p_held',
               'sigma_5m_pct', 'velocity')
SEEN_CALM = ('calm', 'tight_held', 'p_touch', 'p_touch_fresh', 'threshold', 'sigma_5m_pct', 'cut_pct')
CALM_ACTS = {'narrow': 'calm_narrow', 'recentre': 'calm_recentre', 'widen': 'calm_widen'}


def _plain(x):
    """A numpy scalar as its Python value, for json.dumps."""
    return x.item() if hasattr(x, 'item') else str(x)


def poll_knobs():
    """The configuration poll_verdict reads, from config."""
    return {'regime_enabled': bool(config.REGIME_ENABLED), 'calm_enabled': bool(config.CALM_ENABLED),
            'widths': list(config.REGIME_WIDTHS), 'steps': config.REGIME_STEPS,
            'calm_band': config.CALM_BAND, 'calm_threshold': config.CALM_THRESHOLD,
            'calm_min_gap': config.CALM_MIN_GAP, 'calm_max_moves': config.CALM_MAX_MOVES,
            'proactive_threshold': config.PROACTIVE_THRESHOLD,
            'harvest_interval': config.HARVEST_INTERVAL, 'min_harvest_usd': config.MIN_HARVEST_USD}


def poll_seen(state, status, rv, cv, fc):
    """Everything poll_verdict needs from this poll, as plain JSON values.
    Reads the venue's breaker, and the hour's volume profile only when a
    proactive re-centre is on the table (the only verdict that reads it)."""
    now = time.time()
    act = bool(fc and fc.get('act') and config.PROACTIVE_THRESHOLD)
    seen = {'at': now, 'knobs': poll_knobs(),
            'band': {'price': status['price'], 'lower': status['lowerPrice'], 'upper': status['upperPrice'],
                     'in_range': bool(status.get('inRange')), 'fees_usd': status.get('feesAccrued_USD')},
            'regime': {k: rv.get(k) for k in SEEN_REGIME} if rv else None,
            'calm': {k: cv.get(k) for k in SEEN_CALM} if cv else None,
            'forecast': {'act': bool(fc.get('act')), 'p_exit_horizon': fc.get('p_exit_horizon')} if fc else None,
            'gates': {'calm_times': [t for t in state.get('calm_times', []) if now - t < tuning.DAY_S],
                      'last_rebalance': state.get('last_rebalance', 0),
                      'last_harvest': state.get('last_harvest', 0),
                      'breaker_ok': bool(health.allowed(f'venue:{config.DEX}', now)[0]),
                      'busy': board.busy_hour() if act else None}}
    return json.loads(json.dumps(seen, default=_plain))


def poll_verdict(seen):
    """The poll's move, from poll_seen alone. Pure.

    {'act', 'band', 'side', 'deferred', 'harvest'}: `act` is 'exit' (the
    price left the band: reopen at `band`, None meaning the ladder's), a
    voluntary move (regime_widen, regime_narrow, calm_narrow, calm_recentre,
    calm_widen, proactive) or None; `deferred` a proactive re-centre held
    for a quiet hour; `harvest` the dividend is due (only when `act` is
    None)."""
    k, b, g = seen['knobs'], seen['band'], seen['gates']
    rv, cv, fc = seen['regime'], seen['calm'], seen['forecast']
    out = {'act': None, 'band': None, 'side': None, 'deferred': False, 'harvest': False}
    budget = regime.moves_left(g['calm_times'], seen['at'], k['calm_max_moves'])
    if not b['in_range']:
        if k['regime_enabled']:
            band = rv['choice'] if rv else k['widths'][-1]
        else:
            tight = bool(cv and cv.get('tight_held'))
            band = regime.tight_reopen(cv, budget, k['calm_band'], k['calm_threshold']) if tight else None
        return dict(out, act='exit', band=band, side='above' if b['price'] > b['upper'] else 'below')
    voluntary = regime.move_gap_ok(g['calm_times'], g['last_rebalance'], seen['at'], k['calm_min_gap']) and g['breaker_ok']
    # A stale tape moves no band that is still inside, either way: its widest
    # width is for an exit to reopen at. Widening on it closed and reopened a
    # DJT band, then narrowed it back when the tape returned, every time
    # GeckoTerminal published late (2026-10-06, three loops in an hour).
    ract = calm.regime_decide(rv, widths=k['widths'], steps=k['steps']) if rv and not rv.get('stale') else None
    if ract and budget > 0 and voluntary:
        return dict(out, act=f'regime_{ract}', band=rv['choice'])
    cact = None if rv else calm.decide(cv, enabled=k['calm_enabled'], budget_left=budget)
    if cact and voluntary:
        return dict(out, act=CALM_ACTS[cact], band=None if cact == 'widen' else k['calm_band'])
    if fc and fc['act'] and k['proactive_threshold']:
        if g['busy'] and (fc['p_exit_horizon'] or 0) < tuning.BUSY_HOUR_MAX_P_EXIT:
            out['deferred'] = True
        else:
            return dict(out, act='proactive')
    out['harvest'] = harvest.harvest_ready(b['fees_usd'], seen['at'] - g['last_harvest'],
                                           k['harvest_interval'], k['min_harvest_usd'])
    return out


def record_poll(pool, seen, verdict):
    """Store this poll for replay testing. Never blocks the loop."""
    try:
        db.record_replay_poll(pool, seen, verdict)
    except Exception as e:
        books.notify('replay_record_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
