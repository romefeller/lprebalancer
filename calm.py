"""Calm mode: a tight band while the market is quiet, the normal band otherwise.

The idea, from the owner and tested in research/income_experiment: some hours
are cold. Five-minute volatility falls, the price barely moves, and a band of
+/-1% survives long enough to earn several times the fee share of the normal
band. When the calm ends, the bot widens back to the band the ladder picks.

The signal is the one the study selected on validation data and the audit in
lp_research/audited_research.md checked: the EWMA of squared five-minute log
returns with a one-hour half-life (12 bars). The market is calm while that
sigma is at or below `calm_sigma_cut`; it stays calm until sigma rises above
`calm_sigma_cut * calm_exit_mult`, so a signal that flickers around the cut
does not flip the band every poll.

While the tight band is held, the hourly exit forecast (engine.band_forecast)
says nothing useful: a 1% band almost always leaves within six hours. So the
tight band gets its own forecast on the five-minute tape: the probability
that the price touches either edge within `calm_horizon_minutes`, from the
pool's recent first-passage excursions scaled by the volatility at each
origin, read at today's volatility. Highs and lows decide a touch, not closes:
on a 1% band, closes miss a third of the exits.

Everything here is pure except `tape_5m`, which reads GeckoTerminal through
the engine's shared rate gate.
"""
import math
import time

import numpy as np

import engine

HALF_LIFE_BARS = 12          # one hour of five-minute bars
BAR_SECONDS = 300


def tape_5m(pool, live_price=None, before=None):
    """Five-minute OHLCV for the pool, oldest first, in the pool's own quote
    units: (ts, open, high, low, close, volume) arrays, or None.

    GeckoTerminal sometimes lists a pair the other way up. When the latest
    close is closer to 1/price than to price, every price column is inverted
    (high and low swap) so the series matches the pool."""
    url = f'{engine.GECKO}/pools/{pool}/ohlcv/minute?aggregate=5&limit=1000&currency=token'
    if before:
        url += f'&before_timestamp={int(before)}'
    d = engine.curl(url, accept='application/json;version=20230203')
    rows = (((d or {}).get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []
    rows = sorted(rows, key=lambda r: r[0])
    # The newest bar is still forming: its high and low are not final.
    now = time.time()
    rows = [r for r in rows if r[0] + BAR_SECONDS <= now]
    if len(rows) < 200:
        return None
    a = np.array([[float(x) for x in r[:6]] for r in rows])
    ok = np.all(np.isfinite(a[:, 1:5]) & (a[:, 1:5] > 0), axis=1)
    a = a[ok]
    if live_price and live_price > 0:
        c = a[-1, 4]
        if abs(c / live_price - 1) > 0.15 and abs((1 / c) / live_price - 1) <= 0.15:
            o, h, l, cl = 1 / a[:, 1], 1 / a[:, 3], 1 / a[:, 2], 1 / a[:, 4]
            a[:, 1], a[:, 2], a[:, 3], a[:, 4] = o, h, l, cl
        elif abs(c / live_price - 1) > 0.15:
            return None
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5]


def ewma_sigma(close, half_life=HALF_LIFE_BARS):
    """Per-bar sigma: sqrt of the EWMA of squared log returns. Causal: the
    value at bar i uses bars 0..i only."""
    lp = np.log(np.asarray(close, dtype=float))
    r = np.diff(lp, prepend=lp[0])
    out = np.empty(len(r))
    out[0] = r[0] ** 2
    decay = 2 ** (-1 / half_life)
    for i in range(1, len(r)):
        out[i] = decay * out[i - 1] + (1 - decay) * r[i] ** 2
    return np.sqrt(np.maximum(out, 1e-14))


def is_calm(sigma_now, was_calm, cut, exit_mult):
    """Hysteresis: enter at or below `cut`, leave only above `cut * exit_mult`."""
    if sigma_now is None or not cut:
        return False
    if was_calm:
        return sigma_now <= cut * exit_mult
    return sigma_now <= cut


def p_touch(high, low, close, sigma, d_up, d_down, horizon_bars, sigma_now, window=None):
    """P(the price touches `d_up` above or `d_down` below the current price,
    both as log distances, within `horizon_bars`), from the tape's own
    volatility-scaled excursions.

    For every origin s with a full horizon ahead: the largest log move up to
    a later HIGH and down to a later LOW, each divided by sigma[s]. A touch
    now needs a scaled excursion beyond d/sigma_now. Returns None with fewer
    than 100 origins."""
    close = np.asarray(close, dtype=float)
    n = len(close)
    H = int(horizon_bars)
    if H < 1 or n < H + 100 or not sigma_now or d_up <= 0 or d_down <= 0:
        return None if (H < 1 or n < H + 100 or not sigma_now) else 1.0
    up = np.zeros(n - H)
    dn = np.zeros(n - H)
    base = close[:n - H]
    for h in range(1, H + 1):
        up = np.maximum(up, np.log(np.asarray(high)[h:n - H + h] / base))
        dn = np.maximum(dn, np.log(base / np.asarray(low)[h:n - H + h]))
    s = np.asarray(sigma)[:n - H]
    if window:
        up, dn, s = up[-window:], dn[-window:], s[-window:]
    hit = (up / s > d_up / sigma_now) | (dn / s > d_down / sigma_now)
    return float(hit.mean())


def view(bars, price, lower, upper, *, was_calm, cut, exit_mult, band, horizon_minutes, threshold):
    """Everything the loop and the book need about calm mode, from the
    five-minute tape and the held band. Pure.

    `band` is the tight band's half-width multiplier (1.01 = +/-1%). The held
    band counts as tight when its half-width is within 0.1 point of it."""
    if bars is None:
        return None
    ts, _o, high, low, close, vol = bars
    sigma = ewma_sigma(close)
    s_now = float(sigma[-1])
    calm = is_calm(s_now, was_calm, cut, exit_mult)
    H = max(1, round(horizon_minutes * 60 / BAR_SECONDS))
    half = math.sqrt(upper / lower) if lower > 0 else None
    tight = bool(half and half <= band + 0.001)
    out = {'sigma_5m_pct': round(s_now * 100, 4), 'cut_pct': round(cut * 100, 4),
           'exit_cut_pct': round(cut * exit_mult * 100, 4),
           'ratio': round(s_now / cut, 2) if cut else None,
           'calm': calm, 'tight_held': tight, 'band_pct': round((band - 1) * 100, 2),
           'horizon_minutes': horizon_minutes, 'threshold': threshold,
           'bar_age_s': int(time.time() - ts[-1]) if len(ts) else None,
           'volume_1h': round(float(np.sum(vol[-12:])), 2)}
    # share of the last 24h the tape spent at or below the cut
    out['calm_share_24h'] = round(float(np.mean(sigma[-288:] <= cut)), 3) if cut else None
    if tight and lower < price < upper:
        P = p_touch(high, low, close, sigma, math.log(upper / price), math.log(price / lower), H, s_now)
        out['p_touch'] = None if P is None else round(P, 3)
    elif tight:
        out['p_touch'] = 1.0
    # what a fresh tight band centred here would face
    Pc = p_touch(high, low, close, sigma, math.log(band), math.log(band), H, s_now)
    out['p_touch_fresh'] = None if Pc is None else round(Pc, 3)
    return out


def decide(v, *, enabled, budget_left):
    """The calm-mode action for this poll, or None.

      'narrow'   calm, a normal band held, budget left: move to the tight band
      'widen'    tight band held and the calm has ended (or no budget)
      'recentre' tight band held, still calm, and the price is likely to touch
                 an edge within the horizon: re-centre the tight band now
    An out-of-band tight band is handled by the loop's exit path, which
    reopens tight while calm and normal otherwise."""
    if not enabled or not v:
        return None
    inside = v.get('p_touch') != 1.0
    if v['tight_held']:
        if not inside:
            return None
        if not v['calm']:
            return 'widen'
        if (v.get('p_touch') or 0) >= v['threshold']:
            return 'recentre' if budget_left > 0 else 'widen'
        return None
    if v['calm'] and budget_left > 0 and (v.get('p_touch_fresh') is None or
                                          v['p_touch_fresh'] < v['threshold']):
        return 'narrow'
    return None


# --- regime mode: the band width is the market's call --------------------------
#
# Calm mode chose between two widths. Regime mode chooses the narrowest width
# in a ladder (default +/-1% .. +/-5%) whose probability of a touch within
# `horizon` stays at or under `threshold`, and keeps choosing every poll:
#
#   * volatility enters through the scaling of every excursion by sigma;
#   * velocity (log change of sigma over 30 minutes) enters by conditioning
#     on origins in the same velocity tercile, so a heating market reads
#     wider than a cooling one at the same sigma;
#   * survival is the quantity itself: P(no touch within the horizon).
#
# The held band moves on an exit (re-centred at the chosen width), when the
# chosen width is two or more steps wider (heating: widen), or two or more
# steps narrower (cooling: narrow). Walk-forward on 63 days of 5-minute
# SOL/USDC at $210 (lp_research/regime.md): horizon 2 h and threshold 0.25
# earned 2.6x the fees of the calm/ladder switch with equity kept positive in
# 6 of 7 nine-day windows. A shorter horizon earned more and eroded equity.

WIDTHS = (1.01, 1.0125, 1.015, 1.02, 1.025, 1.03, 1.04, 1.05)


def velocity(sigma, bars_back=6):
    """Log change of sigma over `bars_back` bars (30 minutes), per hour."""
    s = np.asarray(sigma, dtype=float)
    v = np.zeros(len(s))
    v[bars_back:] = np.log(s[bars_back:] / s[:-bars_back]) * (12 / bars_back)
    return v


def instability(sigma, window=12):
    """Standard deviation of the per-bar change of log sigma over an hour:
    how unsteady volatility itself is (heteroskedasticity of the tape)."""
    ds = np.diff(np.log(np.asarray(sigma, dtype=float)), prepend=math.log(sigma[0]))
    k = np.ones(window) / window
    m = np.convolve(ds, k, mode='full')[:len(ds)]
    m2 = np.convolve(ds * ds, k, mode='full')[:len(ds)]
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def touch_table(high, low, close, sigma, horizon_bars, vel=None):
    """Scaled excursions from every origin with a full horizon ahead: the
    largest move up to a later HIGH and down to a later LOW, over sigma at
    the origin. With `vel`, also each origin's velocity, for conditioning."""
    close = np.asarray(close, dtype=float); n = len(close); H = int(horizon_bars)
    if n < H + 100:
        return None
    up = np.zeros(n - H); dn = np.zeros(n - H); base = close[:n - H]
    hi, lo = np.asarray(high, dtype=float), np.asarray(low, dtype=float)
    for h in range(1, H + 1):
        up = np.maximum(up, np.log(hi[h:n - H + h] / base))
        dn = np.maximum(dn, np.log(base / lo[h:n - H + h]))
    s = np.asarray(sigma, dtype=float)[:n - H]
    t = {'u': up / s, 'd': dn / s}
    if vel is not None:
        t['v'] = np.asarray(vel, dtype=float)[:n - H]
    return t


def p_touch_cond(table, d_up, d_down, sigma_now, vel_now=None, min_origins=150):
    """P(touch) from a touch_table at today's sigma, conditioned on the
    velocity tercile of `vel_now` when that leaves enough origins."""
    if table is None or not sigma_now:
        return None
    if d_up <= 0 or d_down <= 0:
        return 1.0
    u, d = table['u'], table['d']
    if vel_now is not None and 'v' in table:
        v = table['v']
        q1, q2 = np.quantile(v, [1 / 3, 2 / 3])
        g = 0 if vel_now <= q1 else 1 if vel_now < q2 else 2
        # Boundaries include ties, so a tape with repeated velocity values
        # still conditions instead of silently falling back.
        sel = (v <= q1) if g == 0 else ((v > q1) & (v < q2)) if g == 1 else (v >= q2)
        if sel.sum() >= min_origins:
            u, d = u[sel], d[sel]
    return float(((u > d_up / sigma_now) | (d > d_down / sigma_now)).mean())


def regime_view(bars, price, lower, upper, *, widths=WIDTHS, horizon_minutes=120, threshold=0.25):
    """The market's width, from the five-minute tape. Pure."""
    if bars is None:
        return None
    ts, _o, high, low, close, vol = bars
    sigma = ewma_sigma(close)
    vel = velocity(sigma)
    inst = instability(sigma)
    H = max(1, round(horizon_minutes * 60 / BAR_SECONDS))
    table = touch_table(high, low, close, sigma, H, vel)
    s_now, v_now = float(sigma[-1]), float(vel[-1])
    probs = []
    for k in widths:
        probs.append(p_touch_cond(table, math.log(k), math.log(k), s_now, v_now))
    choice = next((k for k, p in zip(widths, probs) if p is not None and p <= threshold), widths[-1])
    half = math.sqrt(upper / lower) if lower > 0 and upper > lower else None
    held = min(widths, key=lambda k: abs(k - half)) if half else None
    inside = bool(lower <= price <= upper)
    p_held = (p_touch_cond(table, math.log(upper / price), math.log(price / lower), s_now, v_now)
              if inside and half else (1.0 if half else None))
    idx = widths.index(choice)
    mode = 'CALM' if idx == 0 else 'WARM' if choice <= 1.02 else 'HOT'
    return {'mode': mode, 'choice': choice, 'choice_pct': round((choice - 1) * 100, 2),
            'held': held, 'held_pct': round((half - 1) * 100, 2) if half else None,
            'inside': inside, 'p_held': None if p_held is None else round(p_held, 3),
            'probs': {f'{(k - 1) * 100:g}': (None if p is None else round(p, 3)) for k, p in zip(widths, probs)},
            'sigma_5m_pct': round(s_now * 100, 4), 'velocity': round(v_now, 3),
            'instability': round(float(inst[-1]), 4),
            'horizon_minutes': horizon_minutes, 'threshold': threshold,
            'bar_age_s': int(time.time() - ts[-1]) if len(ts) else None}


def regime_decide(v, *, widths=WIDTHS, steps=2):
    """'widen' / 'narrow' / None for a held band that is still inside. An
    exit is the loop's: it reopens at v['choice']."""
    if not v or v.get('held') is None or not v['inside']:
        return None
    i_held, i_choice = widths.index(v['held']), widths.index(v['choice'])
    if i_choice >= i_held + steps:
        return 'widen'
    if i_choice <= i_held - steps:
        return 'narrow'
    return None

