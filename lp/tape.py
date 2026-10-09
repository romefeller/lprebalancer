"""The pool's five-minute price tape: storage, the Binance surrogate, quiet pools, liquidity."""

import math
import numpy as np
import time

import calm
import config
import db
import dexes
import engine
from venues import solana_state
from lp import capital, tuning

# --- the tape, the forecast, the dividend -------------------------------------

_TAPE = {}                      # pool -> (fetched_at, candles)
TAPE_REFRESH = 3600             # one GeckoTerminal call an hour, at most


def native_bars(bars, scale):
    """Bars with their price columns (open, high, low, close) times `scale`:
    UI prices made pool-native. Pure; None stays None."""
    if bars is None or scale == 1.0:
        return bars
    return tuple(c * scale if i in (1, 2, 3, 4) else c for i, c in enumerate(bars))


def tape(pool):
    """The pool's hourly closes, refreshed at most once an hour. A fetch that
    fails leaves the last tape in place: a forecast an hour stale beats none."""
    t, c = _TAPE.get(pool, (0, None))
    if c is None or time.time() - t > TAPE_REFRESH:
        try:
            fresh = engine.candles(pool)
        except Exception:
            fresh = None
        if fresh:
            s = capital.UI_SCALE.get(pool, 1.0)
            c = (fresh[0], fresh[1] * s, fresh[2]) if s != 1.0 else fresh
            _TAPE[pool] = (time.time(), c)
    return c


def forecast_for(status):
    """The band forecast for the held position, or None without a tape."""
    c = tape(status.get('whirlpool') or config.POOL)
    if not c:
        return None
    opened = db.position_opened(status['positionMint'])
    hours = ((db.now() - opened).total_seconds() / tuning.HOUR_S) if opened else None
    return engine.band_forecast(c[1], status['price'], status['lowerPrice'], status['upperPrice'],
                                open_price=db.position_open_price(status['positionMint']),
                                hours_alive=hours, horizon=config.PROACTIVE_HORIZON,
                                threshold=config.PROACTIVE_THRESHOLD)


# --- calm mode: a tight band while the market is cold ---------------------------

_TAPE5 = {}                     # pool -> (fetched_at, bars)
TAPE5_REFRESH = 240             # one GeckoTerminal call per five-minute bar, at most


def tape_bars():
    return max(tuning.BARS_PER_DAY, int(config.REGIME_TAPE_DAYS) * tuning.BARS_PER_DAY)


def _merge(bars_list):
    """Union of five-minute bar arrays, by timestamp, newest wins, oldest
    first, trimmed to the window."""
    rows = {}
    for b in bars_list:
        if b is None:
            continue
        for row in zip(*b):
            rows[int(row[0])] = row
    ks = sorted(rows)[-tape_bars():]
    if not ks:
        return None
    cols = list(zip(*[rows[k] for k in ks]))
    return tuple(np.array(c, dtype=float) for c in cols)


def tape5(pool, price, pair=None):
    """The pool's five-minute tape, `regime_tape_days` deep, from the
    database. Each refresh fetches the newest 1000 bars, pages back while the
    window is short, stores the new bars and deletes those older than the
    window. Memory holds this pool's window only (plus at most one other
    pool, for a move in progress). A 3.5-day tape made the width choice
    noisy and churned 6 to 7 moves a day in the replay."""
    t, b = _TAPE5.get(pool, (0, None))
    if b is not None and time.time() - t <= TAPE5_REFRESH:
        return with_surrogate(pool, b, price, pair)
    window_s = tape_bars() * calm.BAR_SECONDS
    if b is None:
        try:
            # an hour of slack: the newest stored bar can trail the clock, and
            # _merge trims to the window anyway
            b = db.tape_load(pool, time.time() - window_s - tuning.HOUR_S)
        except Exception:
            b = None
        if b is not None and abs(b[4][-1] / price - 1) > tuning.TAPE_REORIENT_JUMP:
            b = None                           # another orientation or stale rows: rebuild
    scale = capital.UI_SCALE.get(pool, 1.0)
    try:
        fresh = native_bars(calm.tape_5m(pool, live_price=price / scale), scale)
    except Exception:
        fresh = None
    merged = _merge([b, fresh])
    tries = 0
    pages = int(np.ceil(tape_bars() / tuning.GECKO_PAGE_BARS)) + 1
    while merged is not None and len(merged[0]) < tape_bars() and tries < pages:
        try:
            # The orientation of a historical page is checked against the bar
            # it joins (the oldest one held), not today's price: SOL moved
            # more than 15% in a month, and the check threw every older page
            # away, capping the tape at ten days.
            older = native_bars(calm.tape_5m(pool, live_price=float(merged[4][0]) / scale,
                                             before=float(merged[0][0])), scale)
        except Exception:
            older = None
        if older is None:
            break
        n0 = len(merged[0]); merged = _merge([older, merged]); tries += 1
        if len(merged[0]) == n0:
            break
    if merged is None:
        return with_surrogate(pool, b, price, pair)
    if fresh is not None:
        try:
            db.tape_store(pool, merged, merged[0][-1] - window_s)
            # Pools left a day ago. Every profile's pool stays: each process
            # prunes, and keeping only its own cut the others' tapes to a day
            # (2026-10-02: mu-usdc's prune left sol-usdc 287 of 8640 bars,
            # so a restart would decide on a one-day tape).
            db.tape_prune_other_pools(db.config_pools() | {pool}, time.time() - tuning.DAY_S)
        except Exception:
            pass
        # memory: this pool, and at most one other
        for other in [p for p in _TAPE5 if p != pool][:-1]:
            _TAPE5.pop(other, None)
        _TAPE5[pool] = (time.time(), merged)
    return with_surrogate(pool, merged, price, pair)


_SURR = {}                      # pool -> (asked_at, name, surrogate bars); one pool
LAST_SURROGATE = {}             # pool -> tape_source(): where the last hour's bars came from
SURROGATE_LOOKBACK_S = 86400    # a slot GeckoTerminal lacks in the last day is filled
SURROGATE_REFRESH = TAPE5_REFRESH


def tape_source(ts, filled_ts, now, name, fresh, quiet_ts=()):
    """Where the last hour's bars came from, for the book: 'Gecko', 'Binance'
    (every bar of the hour), 'Gecko+Binance', or 'none' when the tape is
    stale. Pure."""
    hour = [t for t in (ts if ts is not None else []) if t >= now - tuning.HOUR_S - calm.BAR_SECONDS]
    filled = set(int(t) for t in filled_ts)
    n_s = sum(1 for t in hour if int(t) in filled)
    label = ('none' if not fresh else 'Gecko' if n_s == 0 else name if n_s == len(hour) else f'Gecko+{name}')
    quiet = set(int(t) for t in quiet_ts)
    return {'source': label, 'surrogate': name if filled else None,
            'filled_1h': n_s, 'bars_1h': len(hour), 'filled_24h': len(filled),
            'quiet_1h': sum(1 for t in hour if int(t) in quiet)}


_QUIET_MISMATCH = {}              # pool -> when the live price first left the last close
_QUIET_REF = {}                   # {'at', 'pool', 'ts'}: the canary pool's bar times, cached
QUIET_REF_REFRESH = 60


def quiet_ref_ts(pool, now):
    """Bar times of the last QUIET_HISTORY_S of the reference pool, the one
    pool that trades every slot (a native/stable profile's: sol-usdc's), from
    the database; None for that pool itself, when there is none, or on a
    failed read. Cached QUIET_REF_REFRESH seconds."""
    c = _QUIET_REF
    if c and c.get('for') == pool and now - c['at'] <= QUIET_REF_REFRESH:
        return c['ts']
    try:
        ref = db.tape_ref_pool(config.CAPS['native_mint'], sorted(engine.STABLE_MINTS), pool)
        got = db.tape_load(ref, now - calm.QUIET_HISTORY_S) if ref else None
        ts = got[0] if got is not None else None
    except Exception:
        ts = None
    _QUIET_REF.clear(); _QUIET_REF.update({'at': now, 'for': pool, 'ts': ts})
    return ts


QUIET_FEE_MAX = 0.01            # a pool fee above this is not taken as a price gap


def quiet_tolerance(pool):
    """calm.QUIET_TOL plus the pool's fee, from the record liquidity_view
    cached. GeckoTerminal's closes are trade prices, which carry the fee: on
    DJT/USDC (0.30%) the live price sat a median 0.300% from the last close
    (SOL/USDC, 0.04%: 0.04%), so every quiet stretch read STALE (2026-10-06).
    No record or no usable fee: calm.QUIET_TOL."""
    entry = _LIQ.get(pool)
    try:
        fee = float(entry[1]['fee'])
    except (TypeError, ValueError, KeyError):
        fee = 0.0
    if not math.isfinite(fee) or fee < 0:
        fee = 0.0
    return calm.QUIET_TOL + min(fee, QUIET_FEE_MAX)


def with_surrogate(pool, bars, price, pair=None):
    """_with_surrogate's tape with the slots a quiet pool did not trade in
    filled flat at the last close (calm.quiet_fill): GeckoTerminal emits no
    bar for a slot without a swap, and a thin pool (MU/USDC) read as STALE
    91.6% of the time. Flat bars are never stored; a GeckoTerminal or a
    surrogate bar always wins over one. The last hour's flat bars are
    `quiet_1h` in LAST_SURROGATE. Any failure returns the tape unfilled."""
    out = _with_surrogate(pool, bars, price, pair)
    if out is None:
        return None
    now = time.time()
    try:
        ok, _QUIET_MISMATCH[pool] = calm.quiet_tail_ok(price, float(out[4][-1]), _QUIET_MISMATCH.get(pool), now,
                                                       tol=quiet_tolerance(pool))
        window_s = tape_bars() * calm.BAR_SECONDS
        q = calm.quiet_fill(out[0], out[4], now, quiet_ref_ts(pool, now), ok, window_s)
        if q is None:
            return out
        merged = _merge_all([q, out])                         # the later array wins: the real bars
        keep = merged[0] >= merged[0][-1] - window_s
        merged = tuple(c[keep] for c in merged)
        src = LAST_SURROGATE.get(pool) or {}
        LAST_SURROGATE[pool] = dict(tape_source(merged[0], _surrogate_ts(src, out, bars), now, src.get('surrogate'),
                                                calm.tape_fresh(merged[0], now), q[0]))
        return merged
    except Exception as e:
        print(f'quiet fill failed: {type(e).__name__}: {e}', flush=True)
        return out


def _surrogate_ts(src, out, bars):
    """The bar times of `out` that are not GeckoTerminal's `bars`: the surrogate's."""
    gecko = set(int(t) for t in bars[0])
    return [t for t in out[0] if int(t) not in gecko]


def _merge_all(bars_list):
    """_merge without its trim to tape_bars(): union by timestamp, the later
    array winning, oldest first."""
    rows = {}
    for b in bars_list:
        for row in zip(*b):
            rows[int(row[0])] = row
    cols = list(zip(*[rows[k] for k in sorted(rows)]))
    return tuple(np.array(c, dtype=float) for c in cols)


def _with_surrogate(pool, bars, price, pair=None):
    """The GeckoTerminal tape with the slots it lacks in the last day filled
    from a surrogate (calm.surrogate_5m). GeckoTerminal wins wherever it has a
    bar; surrogate bars are never stored, so a late GeckoTerminal bar replaces
    them and the tape is GeckoTerminal's again as soon as it is complete. The
    surrogate is asked only while a slot is missing and not already filled,
    at most once per SURROGATE_REFRESH; a failed ask keeps the bars an earlier
    ask gave. Any failure returns the GeckoTerminal tape as it is."""
    if bars is None:
        return None
    now = time.time()
    try:
        pair = pair or (config.PAIR_LABEL if pool == config.POOL else None)
        gaps = calm.missing_slots(bars[0], now, SURROGATE_LOOKBACK_S)
        if not gaps:
            _SURR.pop(pool, None)
            LAST_SURROGATE[pool] = tape_source(bars[0], [], now, None, calm.tape_fresh(bars[0], now))
            return bars
        t, name, s = _SURR.get(pool, (0, None, None))
        have = set(int(x) for x in s[0]) if s is not None else set()
        if any(g not in have for g in gaps) and now - t > SURROGATE_REFRESH:
            got_name, got = calm.surrogate_5m(pair, gaps[0], price, ref=bars)
            if got is not None:
                name, s = got_name, _merge([s, got])
                s = tuple(c[s[0] >= now - SURROGATE_LOOKBACK_S - tuning.HOUR_S] for c in s)   # the last day only
            for other in [p for p in _SURR if p != pool]:
                _SURR.pop(other, None)
            _SURR[pool] = (now, name, s)
        fill = None
        if s is not None:
            want = set(gaps)
            keep = np.array([int(x) in want for x in s[0]], dtype=bool)
            fill = tuple(c[keep] for c in s) if keep.any() else None
        out = _merge([fill, bars]) if fill is not None else bars    # the later array wins: GeckoTerminal
        LAST_SURROGATE[pool] = tape_source(out[0], fill[0] if fill is not None else [], now, name,
                                           calm.tape_fresh(out[0], now))
        return out
    except Exception as e:
        print(f'surrogate tape failed: {type(e).__name__}: {e}', flush=True)
        return bars


_LIQ = {}                       # pool -> (fetched_at, record)
LIQ_REFRESH = 300


def liquidity_view(pool, dex, bars):
    """Liquidity inflow against its norm, from the pool's own record:

      inflow  = active liquidity / its 24-hour median (our share of fees
                falls when liquidity floods in; the risk of a touch does not).
                The active liquidity is the geometric mean of the readings of
                the last REGIME_LIQ_SMOOTH_H hours: one reading jumps when the
                price crosses a large position's edge (sql/028). Fewer than 3
                readings in that window: neutral. 0 hours: the newest reading.
      factor  = clamp(1 / inflow, regime_liq_min, regime_liq_max)
      volume  = last 6 hours of 5-minute volume / the tape's median 6h, and
      tvl_change_24h, both reported for the book only

    The touch threshold is multiplied by `factor`. Bounded, and neutral (1.0)
    when any input is missing, because no liquidity history exists to fit it.
    Records a reading at most every LIQ_REFRESH seconds."""
    out = {'factor': 1.0, 'inflow': None, 'volume_x': None, 'tvl_change_24h': None,
           'inflow_raw': None, 'liquidity': None, 'liquidity_smoothed': None, 'tvl_usd': None, 'readings': 0}
    t, rec = _LIQ.get(pool, (0, None))
    if rec is None or time.time() - t > LIQ_REFRESH:
        try:
            fresh = dexes.pool(dex, pool)
        except Exception:
            fresh = None
        if fresh:
            liq = fresh.get('liquidity') or fresh.get('active_bin_usd')
            try:
                db.record_pool_stats(dex, pool, liq, fresh.get('tvl_usd'), fresh.get('volume_24h_usd'),
                                     fresh.get('price'))
            except Exception:
                pass
            rec = fresh
            _LIQ[pool] = (time.time(), rec)
    if rec:
        out['liquidity'] = rec.get('liquidity') or rec.get('active_bin_usd')
        out['tvl_usd'] = rec.get('tvl_usd')
    smooth_h = config.REGIME_LIQ_SMOOTH_H
    try:
        summ = db.pool_stats_summary(pool, recent_hours=smooth_h)
    except Exception:
        summ = None
    active = out['liquidity'] if smooth_h <= 0 else (summ or {}).get('recent_liquidity')
    if summ and active and summ['median_liquidity'] > 0:
        out['inflow'] = round(float(active) / summ['median_liquidity'], 3)
        out['liquidity_smoothed'] = active if smooth_h > 0 else None
    if summ and out['liquidity'] and summ['median_liquidity'] > 0:
        out['inflow_raw'] = round(float(out['liquidity']) / summ['median_liquidity'], 3)
        out['readings'] = summ['readings']
        if summ.get('tvl_then') and out['tvl_usd']:
            out['tvl_change_24h'] = round(float(out['tvl_usd']) / summ['tvl_then'] - 1, 4)
    if bars is not None and len(bars[5]) >= tuning.BARS_PER_DAY:
        v = np.asarray(bars[5], dtype=float)
        w = tuning.LIQ_WINDOW_BARS
        recent = float(v[-w:].sum())
        sums = np.convolve(v, np.ones(w), mode='valid')
        med = float(np.median(sums))            # 288 bars give 217 sums
        if med > 0:
            out['volume_x'] = round(recent / med, 3)
    if out['inflow']:
        # Liquidity only. Volume is shown but does not scale the budget: it
        # falls every night and weekend, when the market is calm, and the
        # survival estimate already carries the variance that moves with it
        # (weekend study, 2026-09-26: volume per unit variance is unchanged).
        raw = 1.0 / out['inflow']
        out['factor'] = round(min(max(raw, config.REGIME_LIQ_MIN), config.REGIME_LIQ_MAX), 3)
    return out


def pool_price_now(dex, pool):
    """The pool's price from its account (one RPC read, no signer), in the
    pool's own units as the bands are, or None for a venue without the fee
    layout (Meteora DLMM) or on any failure."""
    try:
        st = solana_state.fee_states([(dex, pool)])[pool]           # a pool without the layout: KeyError, None
        sp = int(st['sqrt_price']) / solana_state.Q64
        return sp * sp * 10.0 ** (int(st['dec_a']) - int(st['dec_b']))
    except Exception:
        return None
