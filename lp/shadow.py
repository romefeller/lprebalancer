"""The fee-to-loss shadow (owner, 2026-10-19, sql/056): every poll, the held
pool's fees over its in-band loss over the last R_SHADOW_HOURS, from the
pool's own counters and the five-minute tape (pauses.fee_loss_recent), and
the state a pause rule on it would be in: OUT when the ratio falls below
R_SHADOW_OUT, back IN when it rises above R_SHADOW_IN. Nothing is traded:
the ratio and the state go to the book's REGIME block, to the risk_profile
row, and to an R_SHADOW event at each flip, so the rule is judged on live
hours before it ever moves money.

Why a shadow: on 8sLb (14 days, 336 hours) the ratio's hour-to-hour
correlation was +0.42. Hours after a trailing-3h ratio above 2.25 paid 2.15x
their loss; hours after one below 1.35 paid 0.8-0.9x. A pause on it netted
$2.02 against $1.22 per $1k/day all in, with half the fees, positive in the
losing week and about even in the winning one. Fourteen days of one pool,
thresholds picked on the same data: evidence to collect, not a rule.
"""
import time

import config
import db
from lp import books, paths, pauses, tuning


def shadow_step(inside, ratio, out_below=tuning.R_SHADOW_OUT, in_above=tuning.R_SHADOW_IN):
    """The shadow pause's next state: False (OUT) when `inside` and `ratio`
    is below `out_below`; True (IN) when out and it is above `in_above`;
    else as it was. An unknown ratio (None) changes nothing. Pure."""
    if ratio is None:
        return bool(inside)
    if inside and ratio < out_below:
        return False
    if not inside and ratio > in_above:
        return True
    return bool(inside)


def r_shadow(state, status, rv):
    """Read the ratio, move the shadow state in `state['r_shadow']`
    ({'in', 'since'}), and put 'fee_loss_3h', 'shadow_in' and 'shadow' (the
    rule's numbers, for the book) on the regime view `rv`. A flip writes an
    R_SHADOW event. Returns the ratio; None when there is no view, no quote,
    or the read failed (the view is then left as it was)."""
    if not rv:
        return None
    q = status.get('quoteUsd')
    if q is None or status.get('price') is None:
        return None
    pool = status.get('whirlpool') or config.POOL
    try:
        ratio = pauses.fee_loss_recent(pool, status['price'] * q, q, tuning.R_SHADOW_HOURS)
    except Exception as e:
        books.notify('r_shadow_unread', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return None
    sh = state.get('r_shadow')
    if not sh:
        sh = {'in': True, 'since': time.time()}
        state['r_shadow'] = sh; paths.save(state)
    nxt = shadow_step(sh['in'], ratio)
    if nxt != sh['in']:
        db.event('R_SHADOW', f"shadow pause {'IN' if nxt else 'OUT'}: fees {ratio}x the in-band loss over "
                             f"{tuning.R_SHADOW_HOURS:g}h (out < {tuning.R_SHADOW_OUT:g}, in > {tuning.R_SHADOW_IN:g}); "
                             f"nothing traded")
        sh = {'in': nxt, 'since': time.time()}
        state['r_shadow'] = sh; paths.save(state)
    rv['fee_loss_3h'] = ratio
    rv['shadow_in'] = sh['in']
    rv['shadow'] = {'in': sh['in'], 'since': sh['since'], 'hours': tuning.R_SHADOW_HOURS,
                    'out_below': tuning.R_SHADOW_OUT, 'in_above': tuning.R_SHADOW_IN}
    return ratio
