"""What the bot says: the event feed, Telegram's books, emoji, redaction."""

import datetime as dt
import json
import math
import re
from datetime import datetime, timezone

import config
import db
import health
import stats
from lp import paths

def band_label(k):
    """'+/-1.5%' for 1.015: two decimals, trailing zeros dropped. The OPEN
    message said '+/-1%' for a +/-1.5% band (rounding) on 2026-09-26."""
    return '+/-' + f'{(float(k) - 1) * 100:.2f}'.rstrip('0').rstrip('.') + '%'


NOISE = re.compile(r'^\s*bigint: Failed to load bindings|^\s*\(node:\d+\) \[?\w*\]? ?(Experimental|Deprecation)Warning'
                   r'|^\s*\(Use `node --trace-')


def tidy(err, limit=140):
    """Turn a wall of RPC error text into one readable line.

    Raw provider errors arrive as a nested dump with headers and cookies. Sent
    to Telegram verbatim they read as gibberish with a 429 buried in them, which
    is exactly how a healthy bridge came to look like a broken one.
    """
    if not err:
        return None
    # Known noise lines first: the bigint warning took 76 of 140 characters
    # and cut the 2026-09-30 403 off before its cause.
    s = ' '.join(ln for ln in str(err).splitlines() if not NOISE.search(ln))
    s = ' '.join(s.split())
    if not s:
        return None
    # A program rejection is authoritative even if earlier RPC retries logged 429.
    if re.search(r'PriceSlippageCheck|price slippage check|0x1781\b|Custom["\s:]+6017\b', s, re.I):
        return 'PriceSlippageCheck (6017): price moved beyond the slippage limit'
    # The specific part of a program failure first: an Anchor "Error Message",
    # the custom error code, a JSON-RPC message. A simulation failure's log
    # dump buried them (2026-09-28 18:56: only "Program data: 7XCU..." kept).
    anchor = re.search(r'Error Code: (\w+)\. Error Number: (\d+)\. Error Message: ([^."\]]+)', s)
    if anchor:
        return f'program error {anchor.group(1)} ({anchor.group(2)}): {anchor.group(3).strip()}'[:limit]
    code = re.search(r'custom program error: 0x[0-9a-fA-F]+', s)
    if code and re.search(r'simulation failed|failed on chain|InstructionError', s, re.I):
        head = re.search(r'Error processing Instruction \d+', s)
        return (f'{head.group(0)}: ' if head else '') + code.group(0)
    program = re.search(r'(?:InstructionError|custom program error|failed on chain|simulation failed).*', s, re.I)
    if program:
        return program.group(0)[:limit]
    if re.search(r'Indexed requests|personal token', s, re.I):
        return 'RPC endpoint refuses indexed reads (403: needs a personal token)'
    if re.search(r'^Jupiter \d+|\bJupiter 429\b', s):
        return ('Jupiter rate limited: ' if re.search(r'\bJupiter 429\b', s) else '') + s[:limit]
    for needle, plain in (
            ('Too Many Requests', 'RPC rate limited'),
            ('timeout', 'RPC timeout'),
            ('ECONNRESET', 'RPC connection reset'),
            ('blockhash', 'blockhash expired')):
        if needle.lower() in s.lower():
            return plain
    if re.search(r'\b429\b', s):
        return 'RPC rate limited'
    rpc_msg = re.search(r'"message"\s*:\s*"([^"]{3,})"', s)
    if rpc_msg:
        return rpc_msg.group(1)[:limit]
    return s[:limit]


def _load_emoji():
    try:
        m = json.loads((paths.ROOT / 'event_emoji.json').read_text())
        return {k: v for k, v in m.items() if not k.startswith('_') and isinstance(v, str)}
    except Exception:
        return {}


EVENT_EMOJI = _load_emoji()


def emoji_for(event, payload=None):
    """The event's emoji (event_emoji.json, shared with the Telegram bridge),
    or by rule: a failure ❌, a deferral ⏳, anything else ▫️. A row that says
    gas is under the reserve (`gas_low`) shows GAS_LOW: the payout was held
    for gas. Pure."""
    e = str(event)
    if (payload or {}).get('gas_low') and 'GAS_LOW' in EVENT_EMOJI:
        return EVENT_EMOJI['GAS_LOW']
    if e in EVENT_EMOJI:
        return EVENT_EMOJI[e]
    if re.search(r'fail|unreadable|refused|rejected|error', e, re.I):
        return '❌'
    if re.search(r'defer|skip|wait|held', e, re.I):
        return '⏳'
    return '▫️'


def redact(text):
    """Text with every secret replaced by *** (db.redact): the api-key of a
    URL and every value the service environment holds under a *_KEY, *_TOKEN
    or *_SECRET name. No feed row, log line or event may show one."""
    return db.redact(text)


def notify(event, **payload):
    # Every row says whose it is: the bridge tails every profile's feed.
    row = {'t': paths.stamp(), 'event': event, **payload}
    # wallet_tag: the address's first 10 characters, how every report names a wallet
    for k, v in (('profile', config.PROFILE), ('wallet_id', config.WALLET_ID),
                 ('wallet_tag', db.wallet_tag(config.WALLET_ADDRESS)), ('chain', config.CHAIN),
                 ('pair', config.PAIR_LABEL)):
        row.setdefault(k, v)
    line = redact(json.dumps(row, default=str))
    paths.FEED.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.FEED, 'a') as fh:
        fh.write(line + '\n')
    print(redact(f'[{row["t"]}] {emoji_for(event, payload)} {event}: {json.dumps(payload, default=str, sort_keys=True)}'),
          flush=True)


def nearest(runs, pct, tolerance=2):
    """The modelled band closest to the one actually held.

    A band opened at one price and measured at another rarely lands exactly on
    a rung of the ladder, and a held band with no run to compare against means
    no re-optimisation ever happens.
    """
    if not runs:
        return None
    k = min(runs, key=lambda x: abs(x - pct))
    return runs[k] if abs(k - pct) <= tolerance else None


def regime_at_move(view, lower, upper, moves_24h=None):
    """The regime view as it is right after a move. Pure. With a new band
    (lower, upper): held, held_pct, inside and p_held describe it, p_held
    from the view's own probability for that width. Without one (a close):
    nothing is held. moves_24h, when given, replaces the poll's count."""
    v = dict(view)
    if moves_24h is not None:
        v['moves_24h'] = moves_24h
    if lower and upper and upper > lower > 0:
        half = math.sqrt(upper / lower)
        widths = [k for k in config.REGIME_WIDTHS]
        held = min(widths, key=lambda k: abs(k - half))
        v['held'], v['held_pct'], v['inside'] = held, round((half - 1) * 100, 2), True
        pct = round((held - 1) * 100, 2)
        v['p_held'] = next((p for w, p in (v.get('probs') or []) if abs(float(w) - pct) < 1e-6), None)
    else:
        v['held'] = v['held_pct'] = v['p_held'] = None
        v['inside'] = False
    return v


def notify_book(event, **payload):
    """notify() with the cumulative book attached.

    The book is merged last and wins on any shared key, so a caller can pass a
    convenient label without risking a duplicate-keyword error at the one moment
    the bot most needs to report something.
    """
    # Every book carries the latest calm view while calm mode is on, not only
    # the in-band one: the OPEN/CLOSE books after a calm move are exactly the
    # ones where it matters.
    if config.CALM_ENABLED and payload.get('calm') is None and LAST_CALM.get('view'):
        payload = dict(payload, calm=LAST_CALM['view'])
    if config.REGIME_ENABLED and payload.get('regime') is None and LAST_REGIME.get('view'):
        payload = dict(payload, regime=LAST_REGIME['view'])
    if payload.get('venues') is None and LAST_VENUES.get('view'):
        payload = dict(payload, venues=LAST_VENUES['view'][:4])
    # At an OPEN or a CLOSE the caller knows the position's mark before any
    # snapshot of it exists: the LP line is built from that (db.deployment_now),
    # and the regime block describes the band the move left behind it, not
    # the one the last poll saw (audit, 2026-09-30: 15 of 19 OPEN books).
    lp_now = payload.pop('lp_now_usd', None)
    moves_now = payload.pop('moves_24h_now', None)
    if lp_now is not None and isinstance(payload.get('regime'), dict):
        opened = event == 'OPEN'
        payload = dict(payload, regime=regime_at_move(payload['regime'], payload.get('lower') if opened else None,
                                                      payload.get('upper') if opened else None, moves_now))
    if payload.get('health') is None:
        payload = dict(payload, health=health.summary())
    book = db.stats()
    if lp_now is not None:
        book = {**book, **db.deployment_now(book, lp_now)}
    return notify(event, **{**payload, **book})


def halt(reason):
    """Stop this profile (its own HALT; the others run on)."""
    paths.HALT.parent.mkdir(parents=True, exist_ok=True)
    paths.HALT.write_text(f'{paths.stamp()} {reason}')
    db.event('BREAKER', reason)
    notify('BREAKER', reason=reason, action='HALT written; will not restart')
LAST_CALM = {}                  # {'view': the latest calm.view}, for every book


LAST_REGIME = {}                # {'view': the latest calm.regime_view}, for every book


def daily_report(state):
    """Once per UTC day, after it closes: the day's line of the book (re-
    centres, fees, value against a 50/50 hold) to the feed and the events
    table. Never blocks the loop."""
    try:
        today = datetime.now(timezone.utc).date()
        day = today - dt.timedelta(days=1)
        if state.get('last_daily') == day.isoformat():
            return None
        line = db.daily_line(day)
        state['last_daily'] = day.isoformat(); paths.save(state)
        if not line:
            return None
        if config.DAILY_COMPARE:
            try:
                line = dict(line, compare=db.day_average(*config.DAILY_COMPARE))
            except Exception as e:
                notify('daily_compare_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        notify('DAILY', **line)
        vs = line['vs_hold_usd']                # None on a day across pairs (sol-swing)
        db.event('DAILY', f"{line['day']}: {line['recentres']} re-centres, fees ${line['fees_usd']:.2f}, "
                          f"vs 50/50 hold {'-' if vs is None else f'{vs:+.2f}'}")
        return line
    except Exception as e:
        notify('daily_report_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return None


def portfolio_report(state):
    """Once per UTC day: every wallet's active pools, their subtotals and
    the TOTAL (stats.portfolio) as one PORTFOLIO row. Sent by one process
    only: the residual owner of the first wallet by id. Never blocks the
    loop."""
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        if not (config.WALLET_ID and config.RESIDUAL_OWNER) or state.get('last_portfolio') == today:
            return None
        ids = sorted(w['id'] for w in db.wallets())
        if not ids or ids[0] != config.WALLET_ID:
            return None
        state['last_portfolio'] = today; paths.save(state)
        p = stats.portfolio()
        notify('PORTFOLIO', **p)
        return p
    except Exception as e:
        notify('portfolio_failed', reason=f'{type(e).__name__}: {tidy(e)}')
        return None


LAST_VENUES = {}                # {'view': [...]} the latest on-chain venue ranking, for every book
