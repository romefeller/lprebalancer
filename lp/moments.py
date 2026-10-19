"""The fee moments (owner, 2026-10-19, sql/057): every poll, the held hour's
stats from the pool's own counters and tape, and a GREAT or BAD label when
the hour stands out against the pool's last 7 days. A spell's start and end
is a FEE_MOMENT event (db.event) and a book message (books.notify_book), so
the moments can be found later with every figure of their hour. Nothing is
traded on them.

GREAT: the held band's fee rate over the last hour (fee_pct_1h, % of the
band's value) at or above the pool's 7-day 90th percentile.
BAD: fees under tuning.MOMENT_BAD_RATIO x the in-band loss over the shadow's
3 hours while the hour's volatility (rms_1h_pct) is at or above its 7-day
90th percentile: the high-vol hours the 6.5-month audit found to lose.
"""
import math
import time

import config
import db
from venues import solana_state
from lp import books, paths, pauses, tuning


def concentration(price, lower, upper):
    """How many times the fees of a full-range dollar a dollar in the band
    [lower, upper] earns at `price` inside it: the full-range value per unit
    liquidity (2 sqrt p) over the band's (Uniswap v3 amounts). 1.0 when the
    band is unknown or the price is outside it. Pure."""
    if not lower or not upper or not price or not (lower < price < upper):
        return 1.0
    sp, sa, sb = math.sqrt(price), math.sqrt(lower), math.sqrt(upper)
    band = (1 / sp - 1 / sb) * price + (sp - sa)
    return 2 * sp / band if band > 0 else 1.0


def hour_stats(bars, now):
    """{'vol_1h_usd', 'range_1h_pct', 'drift_1h_pct'} of the five-minute bars
    (ts, open, high, low, close, volume arrays) whose start lies in the last
    hour before `now`; None when fewer than 6 of them exist. Pure."""
    if bars is None:
        return None
    ts, _, high, low, close, volume = bars
    m = (ts >= now - 3600) & (ts < now)
    if m.sum() < 6:
        return None
    first = int(m.argmax())
    ref = float(close[first - 1]) if first > 0 else float(bars[1][first])
    return {'vol_1h_usd': round(float(volume[m].sum()), 2),
            'range_1h_pct': round((float(high[m].max()) / float(low[m].min()) - 1) * 100, 4),
            'drift_1h_pct': round((float(close[m][-1]) / ref - 1) * 100, 4) if ref > 0 else None}


def classify(fee_pct, fee_p90, ratio_3h, sigma, sigma_p90, bad_ratio=None):
    """'GREAT', 'BAD' or None from the hour's figures against the pool's 7-day
    90th percentiles. A missing figure never labels. GREAT wins a tie. Pure."""
    bad_ratio = tuning.MOMENT_BAD_RATIO if bad_ratio is None else bad_ratio
    if fee_pct is not None and fee_p90 is not None and fee_pct >= fee_p90 > 0:
        return 'GREAT'
    if ratio_3h is not None and sigma is not None and sigma_p90 is not None \
            and ratio_3h < bad_ratio and sigma >= sigma_p90 > 0:
        return 'BAD'
    return None


def hour_view(status, rv, now=None):
    """The held hour's stats: fee_pct_1h, fee_loss_1h, l_share and
    hour_stats(), for the regime view. Reads the counters and the tape; a
    failed read leaves the missing figures None."""
    now = time.time() if now is None else now
    pool = status.get('whirlpool') or config.POOL
    q = status.get('quoteUsd')
    out = {'fee_pct_1h': None, 'fee_loss_1h': None, 'l_share': None,
           'vol_1h_usd': None, 'range_1h_pct': None, 'drift_1h_pct': None}
    try:
        span = db.fee_state_span(pool, hours=1)
        if span and q and span[2] >= tuning.HOT_PAUSE_MIN_COVERAGE * tuning.HOUR_S:
            first, last, secs = span
            y = solana_state.fee_yield(first, last, status['price'] * q, q)
            if y is not None:
                c = concentration(status['price'], status.get('lowerPrice'), status.get('upperPrice'))
                out['fee_pct_1h'] = round(y * c * 100 * tuning.HOUR_S / secs, 5)
            if last.get('liquidity') and status.get('liquidity'):
                out['l_share'] = round(float(status['liquidity']) / float(last['liquidity']), 4)
        if q:
            out['fee_loss_1h'] = pauses.fee_loss_recent(pool, status['price'] * q, q, 1)
        hs = hour_stats(db.tape_load(pool, now - 3 * tuning.HOUR_S), now)
        if hs:
            out.update(hs)
    except Exception as e:
        books.notify('moment_unread', reason=f'{type(e).__name__}: {books.tidy(e)}')
    return out


def record(state, status, rv, metrics):
    """Put the hour's stats and its label on the regime view `rv`, and open or
    close a spell in `state['moment']` ({'kind', 'since', 'fee_pct'}) with a
    FEE_MOMENT event and book message at each change. Returns the label."""
    if not rv:
        return None
    pool = status.get('whirlpool') or config.POOL
    hv = hour_view(status, rv)
    rv.update(hv)
    try:
        ref = db.moment_reference(pool, days=7)
    except Exception as e:
        books.notify('moment_unread', reason=f'{type(e).__name__}: {books.tidy(e)}')
        ref = {}
    rv['moment_ref'] = ref
    sigma = (metrics or {}).get('rms_1h_pct')
    # a day of reference polls before any label: a 90th percentile of a few rows labels noise
    ok = ref if ref.get('n', 0) >= tuning.MOMENT_MIN_REF else {}
    label = classify(hv['fee_pct_1h'], ok.get('fee_p90'), rv.get('fee_loss_3h'), sigma, ok.get('sigma_p90'))
    rv['moment'] = label
    cur = state.get('moment')
    now = time.time()
    if (cur or {}).get('kind') == label:
        return label
    stats = {k: rv.get(k) for k in ('fee_pct_1h', 'fee_loss_1h', 'fee_loss_3h', 'vol_1h_usd', 'range_1h_pct',
                                   'drift_1h_pct', 'l_share', 'mode', 'choice_pct', 'held_pct')}
    stats['sigma_1h_pct'] = sigma
    if cur:
        hours = round((now - cur['since']) / 3600, 2)
        db.event('FEE_MOMENT', f"{cur['kind']} spell ended after {hours}h; now {label or 'normal'}")
        books.notify_book('FEE_MOMENT', phase='end', kind=cur['kind'], hours=hours, next=label, ref=ref,
                          price=status['price'], **stats)
    if label:
        db.event('FEE_MOMENT', f"{label} spell: fees {hv['fee_pct_1h']}%/h of the band (7d p90 {ref.get('fee_p90')}), "
                               f"fee/loss 1h {hv['fee_loss_1h']} 3h {rv.get('fee_loss_3h')}, sigma 1h {sigma}%, "
                               f"range {hv['range_1h_pct']}% drift {hv['drift_1h_pct']}% volume ${hv['vol_1h_usd']}")
        books.notify_book('FEE_MOMENT', phase='start', kind=label, ref=ref, price=status['price'], **stats)
        state['moment'] = {'kind': label, 'since': now, 'fee_pct': hv['fee_pct_1h']}
    else:
        state.pop('moment', None)
    paths.save(state)
    return label
