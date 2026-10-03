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
the engine's shared rate gate, and `binance_5m`, which reads Binance.
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
    url = engine.gecko(f'/pools/{pool}/ohlcv/minute?aggregate=5&limit=1000&currency=token')
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


# --- the surrogate tape: another venue's klines, where GeckoTerminal has no bar ---
# 2026-09-29 10:55-11:40Z GeckoTerminal stopped indexing every Solana pool while
# the pools traded every second. A lone bar after the gap looked fresh and the
# bot narrowed, then widened again. The rule: GeckoTerminal first; a slot it
# lacks is filled from the first surrogate in SURROGATES that prices the pool.
# A surrogate bar never replaces a GeckoTerminal bar and is never stored.
#
# Binance, measured on 30 days of the held SOL/USDC pool against SOLUSDC:
# closes within 2.6 bp (p5-p95), five-minute return correlation 0.989, EWMA
# sigma ratio 0.995 (median). The pool's high-low range is 1.15x Binance's
# (arbitrage lags on chain), so a Binance bar's range is scaled 1.15x;
# unscaled, the regime chose one width narrower in 6 of 60 hours.
#
# Every surrogate bar passes three checks, or the whole fetch is refused:
#   1. the bar itself: aligned slot, finite, positive, low <= open, close <= high
#   2. the level: the newest close within SURROGATE_MATCH of the pool's live
#      price (either way up; the other way up is inverted)
#   3. the join: where GeckoTerminal has bars in the same slots, the median
#      close difference is at most SURROGATE_BASIS_MAX

BINANCE_KLINES = 'https://data-api.binance.vision/api/v3/klines'
SURROGATE_MATCH = 0.005      # the newest surrogate close within 0.5% of the pool's price
SURROGATE_BASIS_MAX = 0.002  # median |log close ratio| to GeckoTerminal, where both have bars
SURROGATE_JOIN_MIN = 3       # overlapping bars needed before the join check applies
SURROGATE_TIMEOUT_S = 10     # a surrogate fetch never holds the loop longer than this
GECKO_GRACE_S = 120          # GeckoTerminal may publish a closed bar this late
FRESH_BARS = 6               # the last half hour of bars must be complete
FRESH_MAX_AGE_S = 900        # and its newest bar no older than this
TOKEN_ALIASES = {'WSOL': 'SOL', 'WETH': 'ETH', 'WBTC': 'BTC', 'CBBTC': 'BTC'}


def pair_tokens(pair):
    """('SOL', 'USDC') from 'SOL/USDC' (aliases applied, upper case), or None. Pure."""
    parts = [p.strip().upper() for p in str(pair or '').replace('-', '/').split('/')]
    if len(parts) != 2 or not all(p.isalnum() for p in parts):
        return None
    return tuple(TOKEN_ALIASES.get(p, p) for p in parts)


def clean_bars(rows, now):
    """Six arrays (ts, open, high, low, close, volume), oldest first, from rows
    of six numbers: closed, slot-aligned, finite, positive and consistent
    (low <= open, close <= high) bars only, one per slot. None when none
    pass. Pure; any junk goes in, nothing raises."""
    out = {}
    for r in rows or []:
        try:
            t, o, h, l, c, v = (float(x) for x in r[:6])
        except (TypeError, ValueError, IndexError):
            continue
        vals = (t, o, h, l, c)
        if not all(math.isfinite(x) for x in vals) or min(o, h, l, c) <= 0:
            continue
        if t % BAR_SECONDS or t + BAR_SECONDS > now or not (l <= min(o, c) and max(o, c) <= h):
            continue
        out[int(t)] = (t, o, h, l, c, v if math.isfinite(v) and v >= 0 else 0.0)
    if not out:
        return None
    a = np.array([out[k] for k in sorted(out)], dtype=float)
    return tuple(a[:, i].copy() for i in range(6))


def fit_surrogate(bars, live_price, ref=None, range_scale=1.0):
    """The surrogate's bars in the pool's quote units, or None when they do not
    describe this pool (check 2 and 3 above). Inverts a pair listed the other
    way up; widens the high-low range `range_scale` times around each bar.
    Pure."""
    if bars is None or not len(bars[0]) or not live_price or not math.isfinite(live_price) or live_price <= 0:
        return None
    ts, o, h, l, c, v = (np.asarray(x, dtype=float).copy() for x in bars)
    if abs(c[-1] / live_price - 1) > SURROGATE_MATCH:
        if abs((1 / c[-1]) / live_price - 1) > SURROGATE_MATCH:
            return None
        o, h, l, c = 1 / o, 1 / l, 1 / h, 1 / c
    if ref is not None and len(ref[0]):
        at = {int(t): float(x) for t, x in zip(ref[0], ref[4])}
        d = [abs(math.log(at[int(t)] / x)) for t, x in zip(ts, c) if int(t) in at and at[int(t)] > 0]
        if len(d) >= SURROGATE_JOIN_MIN and float(np.median(d)) > SURROGATE_BASIS_MAX:
            return None
    if range_scale != 1.0:
        e = np.exp((max(range_scale, 1.0) - 1) / 2 * np.log(h / l))
        h, l = h * e, l / e
    return ts, o, h, l, c, v


def binance_symbols(base, quote):
    """Binance spot symbols that may price base/quote, either way up."""
    return [base + quote, quote + base]


def binance_5m(symbol, start_ts, pages=2):
    """Binance five-minute klines of `symbol` from `start_ts`, as raw rows
    (ts, open, high, low, close, quote volume). [] on any failure: an error
    answer, a rate limit, a timeout, junk."""
    rows, start = [], int(start_ts) * 1000
    for _ in range(pages):
        try:
            d = engine.curl(f'{BINANCE_KLINES}?symbol={symbol}&interval=5m&limit=1000&startTime={start}',
                            retries=0, max_time=SURROGATE_TIMEOUT_S)
        except Exception:
            break
        if not isinstance(d, list) or not d:
            break
        for k in d:
            try:
                rows.append((int(k[0]) // 1000, k[1], k[2], k[3], k[4], k[7]))
            except (TypeError, ValueError, IndexError):
                continue
        if len(d) < 1000:
            break
        try:
            start = int(d[-1][0]) + 1
        except (TypeError, ValueError, IndexError):
            break
    return rows


# name, symbols for (base, quote), raw fetch, high-low range scale. First that
# prices the pool wins. A new source goes here after the 30-day comparison.
SURROGATES = (
    ('Binance', binance_symbols, binance_5m, 1.15),
)


def surrogate_5m(pair, start_ts, live_price, ref=None, sources=None):
    """(name, bars) from the first surrogate that prices `pair` at
    `live_price` from `start_ts`, or (None, None). Never raises."""
    toks = pair_tokens(pair)
    if toks is None:
        return None, None
    now = time.time()
    for name, symbols, fetch, scale in (SURROGATES if sources is None else sources):
        try:
            for sym in symbols(*toks):
                fit = fit_surrogate(clean_bars(fetch(sym, start_ts), now), live_price, ref, scale)
                if fit is not None:
                    return name, fit
        except Exception:
            continue
    return None, None


def missing_slots(ts, now, lookback_s, grace_s=GECKO_GRACE_S):
    """The five-minute slots of the last `lookback_s` with no bar in `ts`,
    oldest first. A slot counts once its bar closed `grace_s` ago. Pure."""
    last = int((now - BAR_SECONDS - grace_s) // BAR_SECONDS) * BAR_SECONDS
    first = last - int(lookback_s) // BAR_SECONDS * BAR_SECONDS
    have = set(int(t) for t in (ts if ts is not None else []))
    return [s for s in range(first, last + 1, BAR_SECONDS) if s not in have]


# A quiet pool: GeckoTerminal emits no bar for a five-minute slot without a
# swap. MU/USDC had no bar in 54% of its slots (2026-10-02, 30 days; on chain,
# 92.5% of those slots had no successful transaction and the rest no swap),
# so tape_fresh called it STALE 91.6% of the time, and its gapped tape
# overstated P(touch 2h) 2-3x. A slot the pool did not trade in is a flat bar
# at the last close; quiet_fill decides which missing slots were quiet.
QUIET_HISTORY_S = 86400           # older missing slots are quiet: GeckoTerminal backfills an outage
QUIET_TOL = 0.003                 # the live price this close to the last close: no swap moved it
QUIET_MISMATCH_S = BAR_SECONDS + GECKO_GRACE_S   # a moved price waits this long for its bar
QUIET_MAX_S = 6 * 3600            # a quiet tail longer than this is an outage (longest seen: 4.75 h)
# The canary's newest bar may be this old: its own freshness limit, plus its
# process's refresh (rebalancer.TAPE5_REFRESH, 240 s) and the reader's cache
# (rebalancer.QUIET_REF_REFRESH, 60 s), so a lagging copy is not an outage.
QUIET_REF_FRESH_S = FRESH_MAX_AGE_S + 240 + 60


def quiet_tail_ok(price, last_close, mismatch_since, now, tol=QUIET_TOL, wait_s=QUIET_MISMATCH_S):
    """(ok, mismatch_since): whether the slots after the pool's last bar may
    be filled flat. They may while the live price is within `tol` of the last
    close (no swap moved the pool), and for `wait_s` after it first differs
    (the swap's bar is still forming or within GeckoTerminal's grace). A
    price that stays moved with no bar is a swap GeckoTerminal missed. An
    unknown price is not ok. Pure."""
    if price is None or last_close is None or not last_close > 0 or not price > 0:
        return False, None
    if abs(price / last_close - 1) <= tol:
        return True, None
    since = now if mismatch_since is None else mismatch_since
    return now - since <= wait_s, since


def quiet_fill(ts, close, now, ref_ts, tail_ok, window_s, history_s=QUIET_HISTORY_S,
               max_tail_s=QUIET_MAX_S, grace_s=GECKO_GRACE_S, fresh_s=QUIET_REF_FRESH_S):
    """Flat bars (ts, o, h, l, c, volume 0) at the previous close for the
    closed five-minute slots of the last `window_s` the tape lacks, where the
    pool was quiet; None when there are none. Pure.

    A missing slot is quiet when it is older than `history_s` (GeckoTerminal
    backfills an outage, so an old gap is a slot without a swap), or when
    the reference pool `ref_ts` (one that trades every slot: GeckoTerminal's
    canary) has a bar in it, or the slot is newer than the reference's newest
    bar and that bar is at most `fresh_s` old (the reference not refreshed
    yet). Without a reference only old slots are quiet: a pool cannot vouch
    for itself, so a gap in the reference pool's own recent tape stays a gap.
    Slots after the tape's last bar (the tail) also need `tail_ok`
    (quiet_tail_ok) and a tail at most `max_tail_s` long. A lone bar after a
    gap the canary did not cover (2026-09-29) still leaves the gap."""
    if ts is None or not len(ts):
        return None
    last = int((now - BAR_SECONDS - grace_s) // BAR_SECONDS) * BAR_SECONDS
    t_arr = np.asarray(ts, dtype=float).astype(np.int64)
    c_arr = np.asarray(close, dtype=float)
    first = max(int(t_arr[0]), last - int(window_s) // BAR_SECONDS * BAR_SECONDS)   # bars sit on the grid
    ref = set(int(t) for t in (ref_ts if ref_ts is not None else []))
    ref_newest = max(ref) if ref else None
    ref_live = ref_newest is not None and now - ref_newest <= fresh_s
    last_bar = int(t_arr[-1])
    tail_fill = tail_ok and last - last_bar <= max_tail_s
    have = set(int(t) for t in t_arr)
    out_t, out_c = [], []
    j = 0                                                          # the newest bar before slot s
    for s in range(first, last + BAR_SECONDS, BAR_SECONDS):
        while j + 1 < len(t_arr) and t_arr[j + 1] <= s:
            j += 1
        if s in have:
            continue
        quiet = (s < now - history_s or s in ref or (ref_live and s > ref_newest))
        if quiet and (s < last_bar or tail_fill):
            out_t.append(s)
            out_c.append(float(c_arr[j]))
    if not out_t:
        return None
    t = np.array(out_t, dtype=float)
    c = np.array(out_c, dtype=float)
    return t, c.copy(), c.copy(), c.copy(), c, np.zeros(len(t))


def tape_fresh(ts, now, bars=FRESH_BARS, max_age_s=FRESH_MAX_AGE_S):
    """Whether the tape describes the market now: its last `bars` bars are
    consecutive (no gap) and the newest is at most `max_age_s` old. A lone
    bar after a gap is not fresh. Pure."""
    if ts is None or len(ts) < bars:
        return False
    tail = np.asarray(ts[-bars:], dtype=float)
    return bool(now - tail[-1] <= max_age_s and np.all(np.abs(np.diff(tail) - BAR_SECONDS) < 1))


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
    table, sigma, s_now, v_now = touch_state(bars, horizon_minutes)
    inst = instability(sigma)
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
            # a list, not an object: JavaScript sorts integer-like keys first
            'probs': [[round((k - 1) * 100, 2), None if p is None else round(p, 3)] for k, p in zip(widths, probs)],
            'sigma_5m_pct': round(s_now * 100, 4), 'velocity': round(v_now, 3),
            'instability': round(float(inst[-1]), 4),
            'horizon_minutes': horizon_minutes, 'threshold': threshold,
            'bar_age_s': int(time.time() - ts[-1]) if len(ts) else None}


def near_edge(price, lower, upper, near_pct):
    """True when `price` is inside [lower, upper] and within `near_pct`
    percent of the price of either edge: the zone where the edge watch reads
    the price between polls. False for near_pct <= 0 or a bad band. Pure."""
    try:
        if not (near_pct > 0 and 0 < lower <= price <= upper):
            return False
    except TypeError:                     # a missing figure
        return False
    return min(upper - price, price - lower) <= price * near_pct / 100


def watch_verdict(price, lower, upper, near_pct):
    """What one edge-watch read means: 'exit' (outside the band: poll now),
    'near' (keep watching) or 'away' (back in the middle: sleep out the
    poll). Pure."""
    if price is None or price <= 0:
        return 'away'
    if price < lower or price > upper:
        return 'exit'
    return 'near' if near_edge(price, lower, upper, near_pct) else 'away'


def offset_band(price, k, side, frac):
    """(lower, upper) of a band of half-width k around `price`, its centre
    moved `frac` of the half-width (in log) against `side`: +1 for a band
    left above (the new one sits a little below the price), -1 for below,
    0 centred. The price stays inside for frac < 1. Pure."""
    if side not in (-1, 0, 1) or not 0 <= frac < 1 or not k > 1 or not price > 0:
        raise ValueError(f'offset_band({price}, {k}, {side}, {frac})')
    c = price * k ** (-frac * side)
    return c / k, c * k


def band_share_a(price, lower, upper):
    """The share of a concentrated position's value held in token A at
    `price` inside [lower, upper]: x*P / (x*P + y) with x = 1/sqrt(P) -
    1/sqrt(upper), y = sqrt(P) - sqrt(lower). 0.5 for a centred band. Pure."""
    if not 0 < lower <= price <= upper or lower == upper:
        raise ValueError(f'band_share_a({price}, {lower}, {upper})')
    sp = math.sqrt(price)
    x, y = 1 / sp - 1 / math.sqrt(upper), sp - math.sqrt(lower)
    return x * price / (x * price + y)


def touch_state(bars, horizon_minutes):
    """(touch table, sigma, sigma now, velocity now) of the five-minute tape
    for a horizon: what regime_view and p_touch_width read touches from."""
    _ts, _o, high, low, close, _v = bars
    sigma = ewma_sigma(close)
    vel = velocity(sigma)
    H = max(1, round(horizon_minutes * 60 / BAR_SECONDS))
    return touch_table(high, low, close, sigma, H, vel), sigma, float(sigma[-1]), float(vel[-1])


def p_touch_width(bars, k, horizon_minutes):
    """P(the price touches a band of half-width k centred now within
    `horizon_minutes`), as regime_view reports it for that width (rounded to
    0.001). None without enough tape. Pure."""
    if bars is None or not len(bars[0]):
        return None
    table, _sigma, s_now, v_now = touch_state(bars, horizon_minutes)
    p = p_touch_cond(table, math.log(k), math.log(k), s_now, v_now)
    return None if p is None else round(p, 3)


def fee_loss_ratio(fee_yield, ts, close, t0, t1, min_cover=0.8):
    """Fees over in-band loss for any centred band in [t0, t1]: the fee yield
    per full-range dollar (dexes.fee_yield) over the sum of r^2/8 of the
    five-minute closes of the bars inside the window. Below 1 the band lost
    more to the price path than it earned. None when the fees are unknown, the
    bars cover less than `min_cover` of the window, or nothing moved."""
    if fee_yield is None:
        return None
    ts = np.asarray(ts, dtype=float); close = np.asarray(close, dtype=float)
    m = (ts >= t0) & (ts + BAR_SECONDS <= t1)
    c = close[m]
    if len(c) < max(2, min_cover * (t1 - t0) / BAR_SECONDS):
        return None
    g = float(np.sum(np.diff(np.log(c)) ** 2)) / 8
    if g <= 0:
        return None
    return fee_yield / g


def hot_pause_step(paused, bad, now, *, resume_s, max_s):
    """The pause's next step: 'pause' (a held band in a bad moment), 'resume'
    (paused, and the signal has been clear for `resume_s`, or the pause is
    `max_s` old), or None. `paused` is None or {'since', 'last_bad'}."""
    if paused is None:
        return 'pause' if bad else None
    if now - paused['since'] >= max_s:
        return 'resume'
    if not bad and now - paused['last_bad'] >= resume_s:
        return 'resume'
    return None


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



# --- the risk profile, for the book ------------------------------------------
# Every poll the loop stores these next to the regime's choice (table
# rebalancer.risk_profile), so the volatility the bot acted on can be read
# back later. Per-bar figures are in percent of a five-minute bar.
#
#   rms_*_pct        root mean square of the five-minute log return
#   vol_ratio        rms over the last hour / rms over the last day
#   park_1h_pct      Parkinson high-low volatility over the last hour
#   vol_of_vol_24h   std of the per-bar change of log sigma over a day
#   acf_r2_lag1_24h  autocorrelation of squared returns at lag 1 (clustering)
#   arch_lm_24h      Engle's ARCH-LM statistic, 6 lags, over a day; with its
#                    p-value (chi-square, 6 degrees of freedom): a small p is
#                    heteroskedasticity the tape shows
#   kurtosis_24h     excess kurtosis of the returns over a day

DAY_BARS = 288
ARCH_LAGS = 6


def chi2_sf_even(x, k):
    """P(X > x) for a chi-square with an even number k of degrees of freedom:
    exp(-x/2) * sum_{i < k/2} (x/2)^i / i!. Exact; no scipy."""
    if k <= 0 or k % 2:
        raise ValueError('k must be a positive even integer')
    if x <= 0:
        return 1.0
    h, term, total = x / 2.0, 1.0, 1.0
    for i in range(1, k // 2):
        term *= h / i
        total += term
    return min(1.0, math.exp(-h) * total)


def arch_lm(r, lags=ARCH_LAGS):
    """(LM statistic, p-value) of Engle's test: regress r^2 on a constant and
    its `lags` lags; LM = n * R^2. None when the sample is too short or has
    no variance."""
    r = np.asarray(r, dtype=float)
    e = r * r
    n = len(e) - lags
    if n < 5 * (lags + 1):
        return None
    y = e[lags:]
    X = np.column_stack([np.ones(n)] + [e[lags - j:len(e) - j] for j in range(1, lags + 1)])
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot <= 1e-12 * float((y * y).sum()):        # no variance, up to float noise
        return None
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    ss_res = float(((y - X @ beta) ** 2).sum())
    r2 = min(max(1.0 - ss_res / ss_tot, 0.0), 1.0)
    lm = n * r2
    return lm, chi2_sf_even(lm, lags)


def _rms(r):
    return float(np.sqrt(np.mean(r * r))) if len(r) else None


def risk_metrics(bars):
    """The risk profile of a five-minute tape (see above). Pure. Every field
    is None when the tape is too short to support it."""
    keys = ('n_bars', 'rms_1h_pct', 'rms_6h_pct', 'rms_24h_pct', 'vol_ratio_1h_24h', 'park_1h_pct',
            'vol_of_vol_24h', 'acf_r2_lag1_24h', 'arch_lm_24h', 'arch_lm_p_24h', 'kurtosis_24h')
    out = dict.fromkeys(keys)
    if bars is None:
        return out
    _ts, _o, high, low, close, _v = bars
    close = np.asarray(close, dtype=float)
    out['n_bars'] = int(len(close))
    if len(close) < 13:
        return out
    r = np.diff(np.log(close))
    pct = lambda x: None if x is None else round(100 * x, 5)
    out['rms_1h_pct'] = pct(_rms(r[-12:]))
    if len(r) >= 72:
        out['rms_6h_pct'] = pct(_rms(r[-72:]))
    hl = np.log(np.asarray(high, dtype=float)[-12:] / np.asarray(low, dtype=float)[-12:])
    out['park_1h_pct'] = pct(float(np.sqrt(np.mean(hl * hl) / (4 * math.log(2)))))
    if len(r) >= DAY_BARS:
        d = r[-DAY_BARS:]
        rms24 = _rms(d)
        out['rms_24h_pct'] = pct(rms24)
        if rms24 > 0:
            out['vol_ratio_1h_24h'] = round(_rms(r[-12:]) / rms24, 4)
        s = ewma_sigma(close)[-(DAY_BARS + 1):]
        out['vol_of_vol_24h'] = round(float(np.std(np.diff(np.log(s)))), 5)
        e = d * d
        tiny = 1e-6 * float(np.mean(e))                  # float noise, not variance
        if np.std(e[1:]) > tiny and np.std(e[:-1]) > tiny:
            out['acf_r2_lag1_24h'] = round(float(np.corrcoef(e[1:], e[:-1])[0, 1]), 4)
        a = arch_lm(d)
        if a:
            out['arch_lm_24h'], out['arch_lm_p_24h'] = round(a[0], 3), round(a[1], 6)
        sd = float(np.std(d))
        if sd > 1e-6 * rms24:
            out['kurtosis_24h'] = round(float(np.mean(((d - d.mean()) / sd) ** 4) - 3.0), 4)
    return out
