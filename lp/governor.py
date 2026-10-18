"""The payout governor (sql/053, owner 2026-10-18): pay out only what the
bands earned above their re-centre loss, so the LP never shrinks to pay a
dividend.

Over the profile's bands closed in the last GOVERNOR_DAYS:

    profit share = (fees + loss) / fees        (loss <= 0)
    payout share = profit share x (1 - GOVERNOR_KEEP), inside [0, GOVERNOR_CAP]

The loss of a band is its LP value at the close minus the value, at the close
price, of the tokens its liquidity held when it went in (each later increase
held from its own snapshot). Fees and price moves of SOL itself are apart:
a band that only rode SOL up or down shows no loss. This is the method the
2026-10-18 audit used (211 bands on 8sLb: fees $178.13, loss -$153.02).

Concentrated-liquidity venues only (value per unit of liquidity known); a
band on another venue, or without liquidity snapshots, is left out of both
fees and loss. No measurable band: nothing is paid (the fees are reinvested).
Values are in quote units: on SOL/USDC that is dollars.

The equity mode (sql/054, owner 2026-10-18) replaces the share above with two
states: each harvest pays GOVERNOR_HIGH while the equity is ahead of its
target, else GOVERNOR_FLOOR (equity_share). The target is the money put in,
grown GOVERNOR_GROWTH_PCT per 30 days (target_usd). The equity counts SOL's
price, so a fall in SOL holds the floor until fees make it up.
"""
import math

import config
import db

# The venues whose snapshot liquidity is the Uniswap-v3 L of the band.
CLMM_DEXES = ('raydium-clmm', 'byreal', 'pancakeswap-v3-solana', 'orca')


def amounts(price, lower, upper):
    """Base and quote tokens per unit of liquidity at `price`, in human units
    times 10^((dec_a+dec_b)/2) (see value_scale). Pure."""
    sp, sa, sb = math.sqrt(price), math.sqrt(lower), math.sqrt(upper)
    if price <= lower:
        return 1 / sa - 1 / sb, 0.0
    if price >= upper:
        return 0.0, sb - sa
    return 1 / sp - 1 / sb, sp - sa


def value_scale(dec_a, dec_b):
    """Quote value of one unit of on-chain liquidity times amounts():
    10^-((dec_a+dec_b)/2) (SOL 9 / USDC 6: 10^-7.5, checked against
    position_usd in the audit). Pure."""
    return 10 ** (-(dec_a + dec_b) / 2)


def band_loss(lower, upper, open_price, close_price, liquidity_steps, scale):
    """The re-centre loss of one band (<= 0 when it lost): LP value at
    `close_price` minus the tokens held at each liquidity step, valued at
    `close_price`. `liquidity_steps` is [(price, liquidity)] in time order;
    the first step is taken at `open_price`. A fall in liquidity scales the
    held tokens down in proportion. None without liquidity. Pure."""
    tranches, prev = [], 0
    for i, (price, liq) in enumerate(liquidity_steps):
        liq = int(liq or 0)
        if liq == prev:
            continue
        if liq > prev:
            tranches.append((liq - prev, open_price if i == 0 else price))
        elif prev > 0:
            f = liq / prev
            tranches = [(l * f, p) for l, p in tranches]
        prev = liq
    if prev <= 0:
        return None
    hold = 0.0
    for l, p in tranches:
        x, y = amounts(p, lower, upper)
        hold += scale * l * (x * close_price + y)
    x, y = amounts(close_price, lower, upper)
    return scale * prev * (x * close_price + y) - hold


def payout_share(fees, loss, keep, cap):
    """The share of each harvest to pay out, from the window's fees and loss.
    0 without fees or when the loss took them all. Pure."""
    if not fees or fees <= 0:
        return 0.0
    profit = (fees + min(loss, 0.0)) / fees
    return min(max(profit * (1 - keep), 0.0), cap)


def measure(profile, days):
    """{'fees', 'loss', 'bands', 'left_out'} over `profile`'s bands closed in
    the last `days`, from the ledger (positions, band_profile, snapshots,
    fee_growth for the decimals, tape5 for the open price)."""
    with db.cursor() as cur:
        cur.execute("""select p.mint, p.pool, p.dex, p.lower_price, p.upper_price, p.opened_at, p.closed_at,
                              coalesce(b.fees_usd, 0) fees_usd
                       from positions p left join band_profile b using (mint)
                       where p.config_name = %s and p.closed_at is not null
                         and p.closed_at > now() - make_interval(secs => %s)
                       order by p.closed_at""", (profile, days * 86400))
        bands = [dict(r) for r in cur.fetchall()]
        out = {'fees': 0.0, 'loss': 0.0, 'bands': 0, 'left_out': 0}
        decs = {}
        for b in bands:
            if b['dex'] not in CLMM_DEXES or not b['lower_price'] or not b['upper_price']:
                out['left_out'] += 1
                continue
            if b['pool'] not in decs:
                cur.execute('select dec_a, dec_b from fee_growth where pool = %s order by ts desc limit 1', (b['pool'],))
                r = cur.fetchone()
                decs[b['pool']] = (r['dec_a'], r['dec_b']) if r else None
            if not decs[b['pool']]:
                out['left_out'] += 1
                continue
            cur.execute("""select ts, price, liquidity from snapshots
                           where mint = %s and price is not null and liquidity is not null order by ts""", (b['mint'],))
            snaps = cur.fetchall()
            if not snaps:
                out['left_out'] += 1
                continue
            # The open price: the tape bar holding the open, interpolated; else
            # the first snapshot. The close price: the last snapshot.
            t = b['opened_at'].timestamp()
            cur.execute('select open, close from tape5 where pool = %s and ts = %s', (b['pool'], int(t // 300 * 300)))
            bar = cur.fetchone()
            w = (t % 300) / 300
            p_open = bar['open'] * (1 - w) + bar['close'] * w if bar else float(snaps[0]['price'])
            loss = band_loss(float(b['lower_price']), float(b['upper_price']), p_open, float(snaps[-1]['price']),
                             [(float(s['price']), s['liquidity']) for s in snaps], value_scale(*decs[b['pool']]))
            if loss is None:
                out['left_out'] += 1
                continue
            out['fees'] += float(b['fees_usd'])
            out['loss'] += loss
            out['bands'] += 1
    return out


def equity_share(gap_pct, paying, enter_pct, exit_pct, floor, high):
    """(share, paying) of the equity mode: `high` while paying, else `floor`.
    Paying starts at `gap_pct` >= `enter_pct` (the equity's lead on its
    target, in %) and stops at `gap_pct` <= `exit_pct`; between the two the
    state stays as it was. Pure."""
    on = gap_pct > exit_pct if paying else gap_pct >= enter_pct
    return (high if on else floor), on


def target_usd(flows, now, growth_pct):
    """The equity target: each (ts, usd) put in (money out negative), grown
    `growth_pct` per 30 days from its own ts to `now`. Pure."""
    g = 1 + growth_pct / 100
    return sum(usd * g ** (max((now - ts).total_seconds(), 0.0) / 86400 / 30) for ts, usd in flows)


def equity_measure(profile, growth_pct):
    """{'equity_usd', 'target_usd', 'put_in_usd', 'gap_pct'} of `profile` now
    (db.governor_capital), or None without a book or a positive target."""
    cap = db.governor_capital(profile)
    if not cap:
        return None
    target = target_usd(cap['flows'], db.now(), growth_pct)
    if target <= 0:
        return None
    return {'equity_usd': cap['equity_usd'], 'target_usd': target,
            'put_in_usd': sum(usd for _, usd in cap['flows']),
            'gap_pct': (cap['equity_usd'] / target - 1) * 100}


def equity_share_now():
    """(share, measurement) of the equity mode for this profile now. A
    switch is written to the database and named in the measurement
    ('switched'). No measurement or an error pays the floor."""
    try:
        m = equity_measure(config.PROFILE, config.GOVERNOR_GROWTH_PCT)
        paying, _ = db.governor_state(config.PROFILE)
    except Exception as e:
        return config.GOVERNOR_FLOOR, {'mode': 'equity', 'error': f'{type(e).__name__}: {e}'}
    if not m:
        return config.GOVERNOR_FLOOR, {'mode': 'equity', 'error': 'no equity or target to read'}
    share, on = equity_share(m['gap_pct'], paying, config.GOVERNOR_ENTER_PCT, config.GOVERNOR_EXIT_PCT,
                             config.GOVERNOR_FLOOR, config.GOVERNOR_HIGH)
    if on != paying:
        db.set_governor_state(config.PROFILE, on)
    return share, dict(m, mode='equity', paying=on, switched=on != paying)


def share_now():
    """(payout share, measurement) for this profile now. Errors pay nothing."""
    if config.GOVERNOR_MODE == 'equity':
        return equity_share_now()
    try:
        m = measure(config.PROFILE, config.GOVERNOR_DAYS)
    except Exception as e:
        return 0.0, {'error': f'{type(e).__name__}: {e}'}
    return payout_share(m['fees'], m['loss'], config.GOVERNOR_KEEP, config.GOVERNOR_CAP), m
