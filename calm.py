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


def tape_5m(pool, live_price=None):
    """Five-minute OHLCV for the pool, oldest first, in the pool's own quote
    units: (ts, open, high, low, close, volume) arrays, or None.

    GeckoTerminal sometimes lists a pair the other way up. When the latest
    close is closer to 1/price than to price, every price column is inverted
    (high and low swap) so the series matches the pool."""
    d = engine.curl(f'{engine.GECKO}/pools/{pool}/ohlcv/minute?aggregate=5&limit=1000&currency=token',
                    accept='application/json;version=20230203')
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
