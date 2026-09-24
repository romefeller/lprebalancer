"""Pool scanning, token quality, and band optimisation.

The rebalancer's problem is a single trade-off. A narrow band concentrates
capital, so it earns a larger share of the pool's fees, but price leaves it
sooner, and every exit costs a swap and locks in the loss the position took
getting there. A wide band rarely needs touching and earns little.

There is no clean closed form for that optimum once rebalancing is path
dependent, so this does not use one. For every candidate band it replays the
pool's own hourly price and volume through exact concentrated-liquidity
arithmetic, tracking real token balances across rebalances, and takes the band
with the best net return per day. The same machinery is the scanner's score,
so a pool is ranked by what a position in it would actually have returned, not
by its advertised yield.

Calibration that makes the fee side honest: share of fees is the position's
liquidity against the pool's ACTIVE liquidity, both in human units. Orca's raw
`liquidity` scales by sqrt(10^decA * 10^decB). On SOL/USDC that gives an implied
average LP concentration of 21.1x, and a +/-10% band (21.5x) then reproduces
Orca's published 0.222%/day. Dividing by dollar TVL instead overstates fees
about 57 times.

Token screening is a plain rule: a pool may hold a token that is a major, or
one Jupiter lists as verified. The reason is concrete: the highest-yielding
pool on the board was SOL/xSOL at 243%/yr, and xSOL is "Hylo 3x Leveraged
SOL", a leveraged token with volatility decay that no volatility statistic
flags. A tag list from Jupiter refuses it by name (`leveraged`, `lst-derivative`
and the like) where a yield number cannot.

Nothing here is specific to one DEX. A pool arrives as the record `dexes.py`
builds — address, tokens, price, fee, TVL, volume, and the pool's own active
liquidity — and is scored the same way whether it lives on Orca, Raydium,
Meteora or Byreal. Candles come from GeckoTerminal, which indexes all of them by
address. The fee-share model needs one number per pool, its implied
concentration, and each DEX kind has its own route to it (see `concentration`).
"""
import json, math, os, pathlib, subprocess, threading, time, urllib.error, urllib.request
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
ORCA = 'https://api.orca.so/v2/solana'
GECKO = 'https://api.geckoterminal.com/api/v2/networks/solana'

# Defaults for a bare scan from the command line. The bot passes its own values
# from the active profile; nothing below reads these when it is running.
SWAP_COST = 0.0010          # round trip swap + slippage at a few hundred dollars
MIN_TVL = 250_000.0         # thin pools move when you enter and vanish when you leave
BANDS = (1.03, 1.05, 1.08, 1.12, 1.18, 1.25, 1.40)
# Tokens whose partner is the one worth judging: on SOL/WIF the question is
# about WIF, on USDC/PUMP about PUMP.
MAJORS = {'SOL', 'USDC', 'USDT', 'PYUSD', 'USDS', 'DAI', 'FDUSD', 'USDE'}


# GeckoTerminal's free tier allows about 30 requests a minute. Every caller in
# this process — the scanner thread, the re-optimiser, the quote pricer — goes
# through this one gate, so they cannot add up to a 429 between them.
_GECKO_LOCK = threading.Lock()
_GECKO_LAST = [0.0]
GECKO_SPACING = 2.1


def curl(url, accept='application/json', retries=2):
    if 'geckoterminal.com' in url:
        with _GECKO_LOCK:
            wait = _GECKO_LAST[0] + GECKO_SPACING - time.time()
            if wait > 0:
                time.sleep(wait)
            _GECKO_LAST[0] = time.time()
    for attempt in range(retries + 1):
        r = subprocess.run(['curl', '-s', '--max-time', '40',
                            '-H', f'accept: {accept}', '-H', f'user-agent: {UA}', url],
                           capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
        except Exception:
            d = None
        # A rate-limit answer is JSON too: {"status": {"error_code": 429}}.
        if isinstance(d, dict) and str((d.get('status') or {}).get('error_code')) == '429':
            time.sleep(5.0 * (attempt + 1))
            continue
        return d
    return None


# Jupiter tags that name an instrument the bot must not hold: leveraged and
# structured tokens pay a high headline yield for taking the other side of
# something engineered to decay.
REFUSED_TAGS = {'leveraged', 'leverage', 'perpetual', 'perp', 'synthetic', 'derivative',
                'index', 'structured', 'tranche', 'bull', 'bear', 'volatility'}
REFUSED_NAME_WORDS = ('leveraged', 'leverage', '2x', '3x', '5x', 'bull', 'bear', 'inverse')


def screen_token(symbol, facts):
    """Pass/fail for one non-major token from what Jupiter knows about it.
    Returns (ok, reason). No facts is a fail: an unknown token is not held."""
    if not facts:
        return False, f'{symbol}: unknown to Jupiter'
    name = f"{facts.get('name') or ''} {symbol}".lower()
    tags = {str(t).lower() for t in (facts.get('tags') or [])}
    if tags & REFUSED_TAGS or any(w in name for w in REFUSED_NAME_WORDS):
        return False, f'{symbol}: leveraged or structured instrument'
    if not facts.get('verified'):
        return False, f'{symbol}: not verified by Jupiter'
    if facts.get('mint_authority_disabled') is False:
        return False, f'{symbol}: mint authority still enabled'
    return True, f'{symbol}: verified'


def screening_verdict(rec, facts, majors=MAJORS):
    """Both tokens must pass: a major passes by name, anything else on its
    Jupiter facts. The worst answer decides."""
    reasons = []
    for side in ('a', 'b'):
        tok = rec[f'token_{side}']
        if tok.get('symbol') in majors:
            continue
        ok, why = screen_token(tok.get('symbol'), (facts or {}).get(side))
        if not ok:
            return False, why
        reasons.append(why)
    return True, '; '.join(reasons) or 'both tokens are majors'


def active_liquidity(pool):
    """Pool active L in human units, from Orca's raw CLMM liquidity field."""
    try:
        da = int(pool['tokenA'].get('decimals', 9))
        db = int(pool['tokenB'].get('decimals', 6))
        return float(pool['liquidity']) / math.sqrt(10 ** da * 10 ** db)
    except Exception:
        return None


STABLES = {'USDC', 'USDT', 'PYUSD', 'USDS', 'DAI', 'FDUSD', 'USDE'}


def as_record(pool):
    """Accept either the normalised record or Orca's raw pool dict."""
    if 'tokenA' in pool and 'token_a' not in pool:
        import dexes
        return dexes.from_orca(pool)
    return pool


def pool_quote_price(pool):
    """USD price of the pool's quote token (token B), priced by MINT.

    Never by GeckoTerminal's pool record: Gecko orders a pair by its own
    convention, so its `quote_token_price_usd` on SOL/cbBTC is the price of
    SOL, not of a bitcoin.
    """
    rec = as_record(pool)
    b = rec.get('token_b') or {}
    if b.get('symbol') in STABLES:
        return 1.0
    mint = b.get('address')
    if not mint:
        return None
    d = curl(f'{GECKO}/simple/networks/solana/token_price/{mint}',
             accept='application/json;version=20230203')
    try:
        p = float(((d or {}).get('data') or {}).get('attributes', {})
                  .get('token_prices', {}).get(mint) or 0)
        return p if p > 0 else None
    except Exception:
        return None


def concentration(rec, quote_usd):
    """The pool's implied concentration, by DEX kind.

    clmm  (Orca, Raydium, Byreal): active liquidity L against the L a
          full-range position of the same TVL would have.
    dlmm  (Meteora): the dollar liquidity per bin near the active bin against
          what a full-range position of the same TVL would leave in one bin.
          A full-range position holds TVL * s / 4 in a bin of log-width s (one
          token per bin, L*sqrt(P)*s/2 with L = TVL/(2 sqrt P)), so the ratio
          is 4A / (TVL * s). Both say the same thing — what fraction of the
          pool's capital is working at the current price — so one fee-share
          model serves both, and on Meteora's SOL/USDC it lands at about 18x
          against Orca's 21x.
    """
    tvl = float(rec.get('tvl_usd') or 0)
    price = float(rec.get('price') or 0)
    if tvl <= 0 or price <= 0:
        return None
    if rec.get('kind', 'clmm') == 'clmm':
        L = rec.get('liquidity')
        if not L or not quote_usd:
            return None
        return pool_concentration(float(L), tvl, price, quote_usd)
    if rec.get('kind') == 'dlmm':
        A, step = rec.get('active_bin_usd'), rec.get('bin_step')
        if not A or not step:
            return None
        return 4.0 * float(A) / (tvl * float(step) / 1e4)
    return None


def candles(address, native=True):
    """Hourly price and volume.

    currency=token gives the pool's OWN price (quote units per base), which is
    the only price consistent with the pool's liquidity figure. Asking for USD
    instead silently breaks every pool whose quote token is not a dollar: the
    liquidity share is then computed from one price scale and the pool's L from
    another. On SOL/USDC the two coincide because USDC is a dollar, which is
    precisely why that calibration passed while SOL/PUMP returned +12%/day.
    """
    cur = 'token' if native else 'usd'
    d = curl(f'{GECKO}/pools/{address}/ohlcv/hour?aggregate=1&limit=1000&currency={cur}',
             accept='application/json;version=20230203')
    rows = (((d or {}).get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []
    rows = sorted(rows, key=lambda x: x[0])
    if len(rows) < 240:
        return None
    ts = np.array([int(x[0]) for x in rows])
    px = np.array([float(x[4]) for x in rows])
    vol = np.array([float(x[5] or 0.0) for x in rows])
    ok = np.isfinite(px) & (px > 0)
    return ts[ok], px[ok], vol[ok]


# --- exact concentrated-liquidity arithmetic ---------------------------------

def liquidity_for(value, p, pa, pb):
    if p <= pa:
        return value / (p * (1 / math.sqrt(pa) - 1 / math.sqrt(pb)))
    if p >= pb:
        return value / (math.sqrt(pb) - math.sqrt(pa))
    return value / (p * (1 / math.sqrt(p) - 1 / math.sqrt(pb))
                    + (math.sqrt(p) - math.sqrt(pa)))


def amounts(L, p, pa, pb):
    if p <= pa:
        return L * (1 / math.sqrt(pa) - 1 / math.sqrt(pb)), 0.0
    if p >= pb:
        return 0.0, L * (math.sqrt(pb) - math.sqrt(pa))
    return (L * (1 / math.sqrt(p) - 1 / math.sqrt(pb)),
            L * (math.sqrt(p) - math.sqrt(pa)))


def pool_concentration(pool_L_native, tvl_usd, price_native, quote_price_usd):
    """How much harder the pool's LPs are concentrated than full range.

    Dimensionless, so it cannot be corrupted by the unit confusion that broke
    two earlier versions. L = V / (2*sqrt(P)) needs V in QUOTE-TOKEN units, not
    dollars; on SOL/USDC the quote is a dollar so the error hid, and on
    SOL/cbBTC (quote = BTC) it produced +179%/day.
    """
    if quote_price_usd <= 0 or price_native <= 0 or tvl_usd <= 0:
        return None
    tvl_quote = tvl_usd / quote_price_usd
    l_full = tvl_quote / (2 * math.sqrt(price_native))
    if l_full <= 0:
        return None
    return pool_L_native / l_full


def band_concentration(k):
    return 1.0 / (1.0 - 1.0 / math.sqrt(k))


def simulate(k, ts, px, vol, pool_L, fee, capital, swap_cost=SWAP_COST):
    """Replay one band. Returns net per day and the rebalance behaviour."""
    entry = float(px[0])
    pa, pb = entry / k, entry * k
    # pool_L here is the POOL CONCENTRATION (dimensionless), not raw liquidity.
    # Fee share per dollar of position: the position's concentration against
    # the pool's, over the pool's TVL. Multiplied by the position's CURRENT
    # value each hour, so that a position that has shrunk earns less and one
    # that has grown earns more, as it does on chain.
    share_per_usd = band_concentration(k) / pool_L['c_pool'] / pool_L['tvl_usd']
    L = liquidity_for(capital, entry, pa, pb)
    fees = cost = 0.0
    rebal = in_range = 0
    for i in range(1, len(px)):
        p = float(px[i])
        if pa <= p <= pb:
            x, y = amounts(L, p, pa, pb)
            fees += vol[i] * fee * share_per_usd * (x * p + y)
            in_range += 1
        else:
            x, y = amounts(L, p, pa, pb)
            value = x * p + y
            cost += value * swap_cost
            value -= value * swap_cost
            rebal += 1
            entry = p
            pa, pb = entry / k, entry * k
            L = liquidity_for(value, entry, pa, pb)
    x, y = amounts(L, float(px[-1]), pa, pb)
    final = x * float(px[-1]) + y
    days = (ts[-1] - ts[0]) / 86400
    net = final + fees - capital
    hold = capital * 0.5 * (px[-1] / px[0]) + capital * 0.5
    return {'band': k, 'band_pct': (k - 1) * 100, 'days': days,
            'fees': fees, 'position_pnl': final - capital, 'cost': cost,
            'net': net, 'net_day_pct': net / capital / days * 100,
            'rebalances': rebal, 'rebal_per_day': rebal / days,
            'in_range_pct': in_range / (len(px) - 1) * 100,
            'vs_hold': (final + fees) - hold}


def best_band(ts, px, vol, pool_L, fee, capital, bands=None, swap_cost=SWAP_COST):
    """Score every candidate band on this pool's own history, keep the best.

    The ladder is a parameter, not a constant: `bands` defaults to the module's
    own list only so that a bare scan still works. The bot passes the ladder
    from its active profile.
    """
    runs = [simulate(k, ts, px, vol, pool_L, fee, capital, swap_cost)
            for k in (bands or BANDS)]
    return max(runs, key=lambda r: r['net_day_pct']), runs


# --- robustness: many origins, not one path ----------------------------------
#
# One replay over the whole window is one draw. A band that happened to fit
# the last six weeks is not a band that fits this pool; a band that does well
# from most starting points is. So every candidate is also replayed from many
# origins over a fixed horizon, and scored on the MEDIAN of those runs. The
# same origins give the band's survival curve: how long it lives before the
# price leaves it, with Kaplan-Meier handling the origins that never saw an
# exit before the data ran out.

def rolling(k, ts, px, vol, pool_L, fee, capital, swap_cost=SWAP_COST,
            horizon_hours=240, step_hours=24):
    """Replay from every `step_hours`-th origin over `horizon_hours`."""
    n, H = len(px), horizon_hours
    runs = [simulate(k, ts[s:s + H + 1], px[s:s + H + 1], vol[s:s + H + 1],
                     pool_L, fee, capital, swap_cost)
            for s in range(0, n - H - 1, step_hours)]
    if not runs:
        return None
    nd = np.array([r['net_day_pct'] for r in runs])
    rb = np.array([r['rebal_per_day'] for r in runs])
    vh = np.array([r['vs_hold'] for r in runs])
    return {'windows': len(runs), 'horizon_days': H / 24,
            'median_net_day': float(np.median(nd)), 'mean_net_day': float(nd.mean()),
            'p25_net_day': float(np.percentile(nd, 25)), 'worst_net_day': float(nd.min()),
            'share_positive': float((nd > 0).mean()),
            'share_beat_hold': float((vh > 0).mean()),
            'rebal_per_day': float(rb.mean())}


def exit_times(px, k, step_hours=6):
    """First-passage time out of a +/-k band centred at each origin.

    Returns (hours, exited) pairs; an origin whose band the price never left
    before the data ended is censored at the hours that remained.
    """
    logp = np.log(np.asarray(px, dtype=float))
    lk = math.log(k)
    n = len(logp)
    out = []
    for s in range(0, n - 1, step_hours):
        gone = np.abs(logp[s + 1:] - logp[s]) > lk
        if gone.any():
            out.append((int(np.argmax(gone)) + 1, True))
        else:
            out.append((n - 1 - s, False))
    return out


def kaplan_meier(times):
    """Survival curve S(t) from (time, event) pairs, censoring respected.
    Returns a list of (t, S(t)) at each event time."""
    t = np.array([x[0] for x in times], dtype=float)
    e = np.array([x[1] for x in times], dtype=bool)
    curve, S = [], 1.0
    for ti in np.unique(t[e]):
        at_risk = int((t >= ti).sum())
        died = int(((t == ti) & e).sum())
        if at_risk:
            S *= 1.0 - died / at_risk
        curve.append((float(ti), S))
    return curve


def survival(px, k, step_hours=6, horizons=(24, 72, 168)):
    """How long a +/-k band lives on this pool's own path."""
    times = exit_times(px, k, step_hours)
    if not times:
        return None
    curve = kaplan_meier(times)

    def S_at(h):
        s = 1.0
        for t, v in curve:
            if t <= h:
                s = v
            else:
                break
        return s
    median = next((t for t, v in curve if v <= 0.5), None)
    return {'origins': len(times), 'exited': sum(1 for _, x in times if x),
            'median_exit_hours': median,
            **{f'p_survive_{h}h': S_at(h) for h in horizons}}


def edge_loss(k):
    """Value lost against holding 50/50 when the price rides to the band's
    edge — the impermanent loss a rebalance at the edge makes permanent.
    Averaged over the two edges, as a fraction of the starting value."""
    L = liquidity_for(1.0, 1.0, 1 / k, k)
    x, y = amounts(L, k, 1 / k, k)
    up = 1 - (x * k + y) / (0.5 * k + 0.5)
    x, y = amounts(L, 1 / k, 1 / k, k)
    down = 1 - (x / k + y) / (0.5 / k + 0.5)
    return (up + down) / 2


def ladder(pool, candle_data, bands, capital, swap_cost=SWAP_COST,
           horizon_hours=240, step_hours=24, quote_usd=None):
    """Score every band on a pool three ways: the one full-window path, the
    rolling-origin distribution, and the survival curve.

    `pool` is a normalised record from dexes.py (Orca's raw dict is accepted
    and converted); `candle_data` is what candles() returned. Returns
    (rows, meta) or None when the pool cannot be priced consistently. Each row
    carries `net_day_pct` and `rebal_per_day` from the ROLLING median, which
    is what the bot decides on; the single path is kept under `path`.
    """
    rec = as_record(pool)
    if not candle_data:
        return None
    tvl = float(rec.get('tvl_usd') or 0)
    price = float(rec.get('price') or 0)
    if quote_usd is None:
        quote_usd = pool_quote_price(rec)
    c_pool = concentration(rec, quote_usd or 0)
    if not c_pool or not (1.0 <= c_pool <= 500.0):
        return None
    ts, px, vol = candle_data
    # The candle price and the pool's own price must agree, or the liquidity
    # share is computed on a different scale than the pool's L.
    if abs(px[-1] / price - 1.0) > 0.15:
        if abs((1.0 / px[-1]) / price - 1.0) > 0.15:
            return None
        px = 1.0 / px
    fee = float(rec.get('fee') or 0)
    ctx = {'tvl_usd': tvl, 'c_pool': c_pool}
    rows = []
    for k in bands:
        path = simulate(k, ts, px, vol, ctx, fee, capital, swap_cost)
        roll = rolling(k, ts, px, vol, ctx, fee, capital, swap_cost, horizon_hours, step_hours)
        surv = survival(px, k)
        rows.append({
            'band': k, 'band_pct': (k - 1) * 100,
            'net_day_pct': roll['median_net_day'] if roll else path['net_day_pct'],
            'rebal_per_day': roll['rebal_per_day'] if roll else path['rebal_per_day'],
            'edge_loss_pct': edge_loss(k) * 100,
            'path': path, 'roll': roll, 'survival': surv,
        })
    meta = {'price': price, 'fee': fee, 'c_pool': c_pool, 'tvl_usd': tvl,
            'quote_usd': quote_usd, 'days': rows[0]['path']['days'], 'hours': len(px),
            'dex': rec.get('dex'), 'kind': rec.get('kind', 'clmm'), 'pair': rec.get('pair')}
    return rows, meta


def choose(rows, max_rebal_per_day):
    """The churn gate, then the best median. A high yield bought with a
    rebalance a day is a high yield bought with a daily chance of a failed
    transaction; only if every band churns does yield alone decide."""
    calm = [r for r in rows if r['rebal_per_day'] <= max_rebal_per_day]
    return max(calm or rows, key=lambda r: r['net_day_pct'])


def print_ladder(rows, meta, pick=None):
    print(f"price {meta['price']:.6g}  fee {meta['fee'] * 100:.2f}%  TVL ${meta['tvl_usd'] / 1e6:.1f}M  "
          f"pool concentration {meta['c_pool']:.1f}x  window {meta['days']:.1f}d  "
          f"rolling {rows[0]['roll']['horizon_days']:.0f}d x {rows[0]['roll']['windows']} origins")
    print(f"{'band':>7} {'path':>7} {'median':>7} {'p25':>7} {'worst':>7} {'+win':>5} "
          f"{'beat':>5} {'reb/d':>6} {'exit50%':>8} {'S24h':>5} {'S72h':>5} {'S7d':>5} {'edge':>6}")
    for r in rows:
        p, o, s = r['path'], r['roll'], r['survival']
        med = f"{s['median_exit_hours']:.0f}h" if s and s['median_exit_hours'] else '  >win'
        mark = '  <-' if pick is not None and r['band'] == pick['band'] else ''
        print(f"+/-{r['band_pct']:>4.0f}% {p['net_day_pct']:>7.3f} {o['median_net_day']:>7.3f} "
              f"{o['p25_net_day']:>7.3f} {o['worst_net_day']:>7.3f} {o['share_positive'] * 100:>4.0f}% "
              f"{o['share_beat_hold'] * 100:>4.0f}% {o['rebal_per_day']:>6.2f} {med:>8} "
              f"{s['p_survive_24h'] * 100:>4.0f}% {s['p_survive_72h'] * 100:>4.0f}% "
              f"{s['p_survive_168h'] * 100:>4.0f}% {r['edge_loss_pct']:>5.2f}%{mark}")
    print("path/median/p25/worst: net %/day (fees + position P&L - swaps) over the full window / "
          "rolling windows.  +win: share of windows with positive net.  beat: share that beat "
          "holding 50/50.  exit50%: median hours until the price leaves the band.  S: probability "
          "the band is still intact after 24h/72h/7d.  edge: value lost vs holding when the price "
          "reaches the edge.")


def _pick_row(rec, rows, meta, pick):
    """One board row: the pool, the band the bot would choose on it, and the
    distribution behind that choice."""
    o, sv = pick.get('roll') or {}, pick.get('survival') or {}
    return {
        'dex': rec['dex'], 'kind': rec.get('kind', 'clmm'), 'address': rec['address'],
        'pair': rec['pair'], 'token_a': rec['token_a'], 'token_b': rec['token_b'],
        'fee': rec['fee'], 'fee_source': rec.get('fee_source'), 'adaptive_fee': rec.get('adaptive_fee'),
        'tvl_usd': rec['tvl_usd'], 'volume_24h_usd': rec.get('volume_24h_usd'),
        'fees_24h_usd': rec.get('fees_24h_usd'), 'price': meta['price'], 'c_pool': meta['c_pool'],
        'band': pick['band'], 'band_pct': pick['band_pct'],
        'net_day_pct': pick['net_day_pct'], 'rebal_per_day': pick['rebal_per_day'],
        'path_net_day': pick['path']['net_day_pct'],
        'p25_net_day': o.get('p25_net_day'), 'worst_net_day': o.get('worst_net_day'),
        'share_positive': o.get('share_positive'), 'share_beat_hold': o.get('share_beat_hold'),
        'windows': o.get('windows'),
        'p_survive_24h': sv.get('p_survive_24h'), 'p_survive_72h': sv.get('p_survive_72h'),
        'p_survive_168h': sv.get('p_survive_168h'), 'median_exit_hours': sv.get('median_exit_hours'),
        'edge_loss_pct': pick['edge_loss_pct'], 'days': meta['days'], 'hours': meta['hours'],
        'all_runs': [{k: r[k] for k in ('band', 'net_day_pct', 'rebal_per_day')} for r in rows],
    }


def season_profile(vol_series, ts_series):
    """Hour-of-day volume multipliers, 1.0 = the average hour, pooled across
    pools after normalising each by its own mean so a big pool does not set
    the rhythm for all. Returns 24 floats, or None with too little data."""
    acc = np.zeros(24); cnt = np.zeros(24)
    for ts, vol in zip(ts_series, vol_series):
        v = np.asarray(vol, dtype=float)
        if len(v) < 48 or v.mean() <= 0:
            continue
        v = v / v.mean()
        h = (np.asarray(ts) // 3600) % 24
        for hour in range(24):
            m = h == hour
            acc[hour] += v[m].sum(); cnt[hour] += m.sum()
    if (cnt == 0).any():
        return None
    prof = acc / cnt
    return [round(float(x), 3) for x in prof / prof.mean()]


def realised_check(rows):
    """Set the model against the tape. For every scored pool: the fee yield
    the pool's own last-24h fees would have paid a position at the chosen
    band, against the gross fee yield the model's replay averaged. Volume is
    common to the whole market on a given day, so each pool's ratio is judged
    relative to the median ratio across the board: a pool whose ratio sits
    well under its peers has had liquidity flood in (or volume leave) since
    the history the model used, and its decision figure is scaled down to
    match. Never scaled up: a hot day is not a reason to trust a pool more."""
    ratios = []
    for r in rows:
        if r.get('net_day_pct') is None:
            continue
        share = band_concentration(r['band']) / r['c_pool'] / r['tvl_usd']   # per $ of position
        r['realised_day_pct'] = float(r.get('fees_24h_usd') or 0) * share * 100
        path = r.get('path') or {}
        gross = (path.get('fees') or 0) / max(path.get('days') or 1, 1e-9)
        r['modelled_gross_day_pct'] = gross / max(r.get('capital', 190.0), 1e-9) * 100
        r['realised_ratio'] = (r['realised_day_pct'] / r['modelled_gross_day_pct']
                               if r['modelled_gross_day_pct'] > 0 else None)
        if r['realised_ratio'] is not None:
            ratios.append(r['realised_ratio'])
    med = float(np.median(ratios)) if ratios else None
    for r in rows:
        if r.get('net_day_pct') is None:
            continue
        rr = r.get('realised_ratio')
        drift = (rr / med) if (rr is not None and med and med > 0) else None
        r['liquidity_drift'] = drift
        r['decision_day_pct'] = (r['net_day_pct'] * min(1.0, drift) if drift is not None
                                 else r['net_day_pct'])
    return med


def score_board(records, capital, bands, swap_cost=SWAP_COST, max_rebal_per_day=0.5,
                min_tvl=MIN_TVL, min_volume=0.0, screen_top=20, blocked=None, progress=None,
                season_out=None):
    """Rank pools from any DEX by what an optimally banded position would
    have returned, under the same model the bot uses to choose its band.

    Every pool that clears the size gates gets the full ladder: rolling
    medians, survival, churn gate, and the band the bot would pick. The top
    of the board is then screened on Jupiter's token facts. Rows that could
    not be scored are kept with the reason, so the board says what it
    declined and why.

    `blocked(rec)` may return a reason a pool cannot be opened at all (an
    adaptive-fee Orca pool, a DEX without a signer); those are listed but not
    spent a candle fetch on. `season_out`, a dict, receives the hour-of-day
    profile built from every scored pool's candles.

    Rows are ranked by `decision_day_pct`: the modelled median net per day,
    scaled down where the pool's own last-24h fees say the model's fee share
    is stale (see realised_check).
    """
    import dexes
    seen, todo, out = set(), [], []
    for rec in records:
        if not rec.get('address') or rec['address'] in seen:
            continue
        seen.add(rec['address'])
        base = {'dex': rec['dex'], 'kind': rec.get('kind', 'clmm'), 'address': rec['address'],
                'pair': rec.get('pair'), 'token_a': rec.get('token_a'), 'token_b': rec.get('token_b'),
                'fee': rec.get('fee'), 'fee_source': rec.get('fee_source'),
                'adaptive_fee': rec.get('adaptive_fee'), 'tvl_usd': rec.get('tvl_usd'),
                'volume_24h_usd': rec.get('volume_24h_usd'), 'fees_24h_usd': rec.get('fees_24h_usd'),
                'net_day_pct': None}
        reason = None
        if (rec.get('tvl_usd') or 0) < min_tvl:
            reason = f'tvl ${rec.get("tvl_usd", 0) / 1e6:.2f}M below ${min_tvl / 1e6:.2f}M'
        elif (rec.get('volume_24h_usd') or 0) < min_volume:
            reason = f'volume ${rec.get("volume_24h_usd", 0) / 1e6:.2f}M below ${min_volume / 1e6:.2f}M'
        elif rec.get('kind') == 'clmm' and not rec.get('liquidity'):
            reason = rec.get('note') or 'active liquidity unreadable'
        elif rec.get('kind') == 'dlmm' and not rec.get('active_bin_usd'):
            reason = rec.get('note') or 'bin liquidity unreadable'
        elif not rec.get('fee'):
            reason = 'fee rate unknown'
        elif blocked and blocked(rec):
            reason = blocked(rec)
        if reason:
            out.append({**base, 'skipped': reason})
            continue
        todo.append((rec, base))

    # Quote-token prices in one Jupiter call, so a non-dollar quote costs no
    # GeckoTerminal budget.
    quotes = {}
    need = [r['token_b']['address'] for r, _ in todo if r['token_b'].get('symbol') not in STABLES]
    if need:
        quotes = dexes.jupiter_prices(need)

    ts_series, vol_series = [], []
    for i, (rec, base) in enumerate(todo):
        if progress:
            progress(i, len(todo), rec)
        qsym, qmint = rec['token_b'].get('symbol'), rec['token_b'].get('address')
        quote_usd = 1.0 if qsym in STABLES else quotes.get(qmint) or pool_quote_price(rec)
        if not quote_usd:
            out.append({**base, 'skipped': 'quote token unpriced'})
            continue
        cd = candles(rec['address'])
        if not cd:
            out.append({**base, 'skipped': 'fewer than 240 hourly candles'})
            continue
        res = ladder(rec, cd, dexes.feasible_bands(rec, bands), capital, swap_cost,
                     quote_usd=quote_usd)
        if not res:
            c = concentration(rec, quote_usd)
            out.append({**base, 'skipped': (f'implied concentration {c:.1f}x out of range' if c
                                            else 'candle price disagrees with pool price')})
            continue
        rows, meta = res
        pick = choose(rows, max_rebal_per_day)
        row = _pick_row(rec, rows, meta, pick)
        row['path'] = pick['path']; row['capital'] = capital
        out.append(row)
        ts_series.append(cd[0]); vol_series.append(cd[2])

    if season_out is not None:
        season_out['profile'] = season_profile(vol_series, ts_series)
        season_out['pools'] = len(vol_series)
    scored = [r for r in out if r.get('net_day_pct') is not None]
    realised_check(scored)
    scored.sort(key=lambda r: -r['decision_day_pct'])
    unscored = [r for r in out if r.get('net_day_pct') is None]

    # Screen the top of the board. A pool of two majors passes by name; any
    # other token is looked up on Jupiter, and one lookup per mint serves
    # every pool that holds it.
    facts_cache = {}
    for i, r in enumerate(scored):
        r['facts'] = None
        if all(r[f'token_{x}']['symbol'] in MAJORS for x in ('a', 'b')):
            r['screen_ok'], r['screen_reason'] = True, 'both tokens are majors'
            continue
        if i >= screen_top:
            r['screen_ok'], r['screen_reason'] = False, 'below the screened top of the board'
            continue
        facts = {}
        for side in ('a', 'b'):
            tok = r[f'token_{side}']
            if tok['symbol'] in MAJORS:
                continue
            m = tok['address']
            if m not in facts_cache:
                facts_cache[m] = dexes.jupiter_token(m)
            facts[side] = facts_cache[m]
        r['facts'] = facts
        r['screen_ok'], r['screen_reason'] = screening_verdict(r, facts)
    return scored + unscored


def print_board(rows, top=25):
    print(f"{'dex':<22}{'pair':<16}{'fee':>7}{'tvl$M':>7}{'vol$M':>7}{'cpool':>6}{'band':>6}"
          f"{'net%/d':>8}{'real':>6}{'use':>7}{'p25':>7}{'+win':>5}{'reb/d':>6}{'S7d':>5}  screen")
    for r in rows[:top]:
        if r.get('net_day_pct') is None:
            print(f"{r['dex']:<22}{(r.get('pair') or '?')[:15]:<16}{'':>7}{(r.get('tvl_usd') or 0) / 1e6:>7.2f}"
                  f"{(r.get('volume_24h_usd') or 0) / 1e6:>7.2f}  -- {r.get('skipped')}")
            continue
        scr = ('ok' if r.get('screen_ok') else 'NO') + ' ' + (r.get('screen_reason') or '')
        drift = r.get('liquidity_drift')
        print(f"{r['dex']:<22}{r['pair'][:15]:<16}{r['fee'] * 100:>6.3f}%{r['tvl_usd'] / 1e6:>7.2f}"
              f"{(r.get('volume_24h_usd') or 0) / 1e6:>7.2f}{r['c_pool']:>6.1f}{r['band_pct']:>5.0f}%"
              f"{r['net_day_pct']:>8.3f}{(f'{drift:.2f}x' if drift is not None else '-'):>6}"
              f"{(r.get('decision_day_pct') if r.get('decision_day_pct') is not None else r['net_day_pct']):>7.3f}"
              f"{(r.get('p25_net_day') or 0):>7.3f}"
              f"{(r.get('share_positive') or 0) * 100:>4.0f}%{r['rebal_per_day']:>6.2f}"
              f"{(r.get('p_survive_168h') or 0) * 100:>4.0f}%  {scr[:40]}")


FINE_BANDS = (1.02, 1.03, 1.04, 1.05, 1.06, 1.08, 1.10, 1.12, 1.15, 1.18, 1.25, 1.40)

if __name__ == '__main__':
    import sys
    # python3 engine.py ladder <pool> [capital_usd] [bands: 1.03,1.05,...]
    if len(sys.argv) >= 3 and sys.argv[1] == 'ladder':
        pool_addr = sys.argv[2]
        cap = float(sys.argv[3]) if len(sys.argv) > 3 else 190.0
        bands = (tuple(float(x) for x in sys.argv[4].split(','))
                 if len(sys.argv) > 4 else FINE_BANDS)
        import dexes
        dex = sys.argv[5] if len(sys.argv) > 5 else 'orca'
        p = dexes.pool(dex, pool_addr)
        if not p:
            raise SystemExit(f'{dex} does not know a pool at {pool_addr}')
        out = ladder(p, candles(pool_addr), bands, cap)
        if not out:
            raise SystemExit('pool cannot be priced consistently (candles vs pool price, '
                             'or implied concentration out of range)')
        rows, meta = out
        print(f"{p['dex']} {p['pair']}  ${cap:.0f}")
        print_ladder(rows, meta, choose(rows, 0.5))
    elif len(sys.argv) >= 2 and sys.argv[1] == 'board':
        # python3 engine.py board [dex,dex,...] [limit] [capital]
        import dexes
        which = tuple(sys.argv[2].split(',')) if len(sys.argv) > 2 else dexes.KNOWN
        lim = int(sys.argv[3]) if len(sys.argv) > 3 else 15
        cap = float(sys.argv[4]) if len(sys.argv) > 4 else 190.0
        recs, errs = dexes.fetch_all(which, lim)
        for d, e in errs.items():
            print(f'{d}: {e}')
        flat = [r for rows in recs.values() for r in rows]
        board = score_board(flat, cap, BANDS, min_volume=1e6,
                            progress=lambda i, n, r: print(f'  [{i + 1}/{n}] {r["dex"]} {r["pair"]}', flush=True))
        print_board(board, top=60)
    else:
        print('usage: python3 engine.py ladder <pool> [capital_usd] [bands]\n'
              '       python3 engine.py board [dexes] [limit] [capital]')
