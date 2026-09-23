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

Token quality is a semantic judgment, so Jev handles it. The reason is concrete:
the highest-yielding pool on the board was SOL/xSOL at 243%/yr, and xSOL is
"Hylo 3x Leveraged SOL" — a leveraged token with volatility decay and
spiral-to-zero risk that no volatility statistic flags.
"""
import json, math, os, pathlib, subprocess, time, urllib.error, urllib.request
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
ORCA = 'https://api.orca.so/v2/solana'
GECKO = 'https://api.geckoterminal.com/api/v2/networks/solana'
TYPESAFE = 'https://api.typesafe.ai/v1/systemone'

# Defaults for a bare scan from the command line. The bot passes its own values
# from the active profile; nothing below reads these when it is running.
SWAP_COST = 0.0010          # round trip swap + slippage at a few hundred dollars
MIN_TVL = 250_000.0         # thin pools move when you enter and vanish when you leave
BANDS = (1.03, 1.05, 1.08, 1.12, 1.18, 1.25, 1.40)
# Tokens whose partner is the one worth judging: on SOL/WIF the question is
# about WIF, on USDC/PUMP about PUMP.
MAJORS = {'SOL', 'USDC', 'USDT', 'PYUSD', 'USDS', 'DAI', 'FDUSD', 'USDE'}


def curl(url, accept='application/json'):
    r = subprocess.run(['curl', '-s', '--max-time', '40',
                        '-H', f'accept: {accept}', '-H', f'user-agent: {UA}', url],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def jev_key():
    """TypeSafe API key, from the environment or a file it points at.

    Token screening is optional: without a key the scanner simply skips it. No
    default path — a hardcoded key location in source control tells a reader
    where to look on a machine they may already be on.
    """
    k = os.environ.get('TYPESAFE_API_KEY')
    if not k:
        path = os.environ.get('TYPESAFE_API_KEY_FILE')
        if not path:
            return None
        try:
            k = pathlib.Path(path).read_text()
        except OSError:
            return None
    k = k.strip()
    if '=' in k.split('\n')[0]:
        k = k.split('=', 1)[1].strip().strip('"\'')
    return k


def token_quality(symbol, name):
    """Two narrow judgments. Returns (leveraged, established) probabilities."""
    key = jev_key()
    if not key:
        return None
    body = {
        'state': {'token_symbol': symbol, 'token_name': name,
                  'context': 'Solana SPL token quoted in a liquidity pool'},
        'model': 'jev-latest',
        'questions': {
            'is_leveraged': {
                'type': 'noul',
                'instructions': {
                    'question': 'Is this token a leveraged, synthetic or '
                                'derivative instrument rather than a plain '
                                'spot asset?',
                    'counts_as_yes': 'Leveraged tokens (3x, 2x, bull/bear), '
                                     'volatility or index tokens, tokens whose '
                                     'value is engineered from another asset '
                                     'with amplification, and tranche tokens '
                                     'that absorb another holder\'s losses.',
                    'counts_as_no': 'Plain spot tokens, governance tokens, '
                                    'memecoins, stablecoins, and liquid staking '
                                    'tokens that simply accrue staking yield.',
                },
            },
            'is_established': {
                'type': 'noul',
                'instructions': {
                    'question': 'Is this a widely recognised token with a real '
                                'project and sustained trading history?',
                    'counts_as_yes': 'Major assets and well-known Solana '
                                     'ecosystem or DeFi tokens that have traded '
                                     'for months or years with broad listings.',
                    'counts_as_no': 'Freshly launched tokens, unknown tickers, '
                                    'and tokens whose only presence is one pool.',
                },
            },
        },
    }
    req = urllib.request.Request(
        TYPESAFE, data=json.dumps(body).encode(),
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json'})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=40).read())
        return (float(r['answers']['is_leveraged']['noul']),
                float(r['answers']['is_established']['noul']))
    except Exception:
        return None


def active_liquidity(pool):
    """Pool active L in human units, from the raw CLMM liquidity field."""
    try:
        da = int(pool['tokenA'].get('decimals', 9))
        db = int(pool['tokenB'].get('decimals', 6))
        return float(pool['liquidity']) / math.sqrt(10 ** da * 10 ** db)
    except Exception:
        return None


STABLES = {'USDC', 'USDT', 'PYUSD', 'USDS', 'DAI', 'FDUSD', 'USDE'}


def pool_quote_price(pool):
    """USD price of the pool's quote token (Orca's token B), priced by MINT.

    Never by GeckoTerminal's pool record: Gecko orders a pair by its own
    convention, so its `quote_token_price_usd` on SOL/cbBTC is the price of
    SOL, not of a bitcoin. Takes the Orca pool dict.
    """
    b = pool.get('tokenB') or {}
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
           horizon_hours=240, step_hours=24):
    """Score every band on a pool three ways: the one full-window path, the
    rolling-origin distribution, and the survival curve.

    `pool` is Orca's pool dict; `candle_data` is what candles() returned.
    Returns (rows, meta) or None when the pool cannot be priced consistently.
    Each row carries `net_day_pct` and `rebal_per_day` from the ROLLING
    median, which is what the bot decides on; the single path is kept under
    `path` for comparison.
    """
    pool_l = active_liquidity(pool)
    tvl = float(pool.get('tvlUsdc') or 0)
    price = float(pool.get('price') or 0)
    quote_usd = pool_quote_price(pool)
    c_pool = pool_concentration(pool_l, tvl, price, quote_usd or 0) if pool_l else None
    if not c_pool or not (1.0 <= c_pool <= 500.0) or not candle_data:
        return None
    ts, px, vol = candle_data
    # The candle price and the pool's own price must agree, or the liquidity
    # share is computed on a different scale than the pool's L.
    if abs(px[-1] / price - 1.0) > 0.15:
        if abs((1.0 / px[-1]) / price - 1.0) > 0.15:
            return None
        px = 1.0 / px
    fee = int(pool.get('feeRate') or 0) / 1e6
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
            'quote_usd': quote_usd, 'days': rows[0]['path']['days'], 'hours': len(px)}
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


def scan(capital, limit=100, use_jev=True, verbose=True, bands=None,
         min_tvl=MIN_TVL, swap_cost=SWAP_COST):
    """Rank pools by what an optimally banded position would have returned."""
    d = curl(f'{ORCA}/pools?limit={limit}&sortBy=volume24h')
    out = []
    for p in (d or {}).get('data', []):
        tvl = float(p.get('tvlUsdc') or 0)
        if tvl < min_tvl:
            continue
        pool_L_native = active_liquidity(p)
        if not pool_L_native:
            continue
        pair = f"{p['tokenA'].get('symbol','?')}/{p['tokenB'].get('symbol','?')}"
        c = candles(p['address'])
        time.sleep(2.2)
        if not c:
            continue
        ts, px, vol = c
        # The candle price and the pool's own reported price must agree, or the
        # liquidity share is computed on a different scale than the pool's L.
        quoted = float(p.get('price') or 0)
        if quoted <= 0:
            continue
        drift = abs(px[-1] / quoted - 1.0)
        if drift > 0.15 and abs((1.0 / px[-1]) / quoted - 1.0) > 0.15:
            if verbose:
                print(f'  {pair[:22]:<23} SKIPPED: candle price {px[-1]:.6g} '
                      f'disagrees with pool price {quoted:.6g}', flush=True)
            continue
        if abs(px[-1] / quoted - 1.0) > 0.15:
            px = 1.0 / px          # pool quotes the inverse pair
        fee = int(p.get('feeRate') or 0) / 1e6
        quote_usd = pool_quote_price(p)
        c_pool = pool_concentration(pool_L_native, tvl, float(px[-1]), quote_usd or 0)
        if not c_pool or not (1.0 <= c_pool <= 500.0):
            if verbose:
                print(f'  {pair[:22]:<23} SKIPPED: implied pool concentration '
                      f'{c_pool if c_pool else float("nan"):.1f}x is out of range',
                      flush=True)
            continue
        ctx = {'tvl_usd': tvl, 'c_pool': c_pool}
        best, runs = best_band(ts, px, vol, ctx, fee, capital, bands, swap_cost)
        row = {'address': p['address'], 'pair': pair, 'fee': fee, 'tvl': tvl,
               'c_pool': c_pool, 'price': float(p.get('price') or 0), **best}
        out.append(row)
        if verbose:
            print(f'  {pair[:22]:<23} band +/-{best["band_pct"]:>4.0f}%  '
                  f'net {best["net_day_pct"]:+.3f}%/day  '
                  f'rebal/day {best["rebal_per_day"]:.2f}', flush=True)
    out.sort(key=lambda r: -r['net_day_pct'])

    if use_jev:
        for r in out[:12]:
            a, b = r['pair'].split('/')
            q = token_quality(b if a in MAJORS else a, r['pair'])
            r['jev'] = {'leveraged': q[0], 'established': q[1]} if q else None
    return out


FINE_BANDS = (1.02, 1.03, 1.04, 1.05, 1.06, 1.08, 1.10, 1.12, 1.15, 1.18, 1.25, 1.40)

if __name__ == '__main__':
    import sys
    # python3 engine.py ladder <pool> [capital_usd] [bands: 1.03,1.05,...]
    if len(sys.argv) >= 3 and sys.argv[1] == 'ladder':
        pool_addr = sys.argv[2]
        cap = float(sys.argv[3]) if len(sys.argv) > 3 else 190.0
        bands = (tuple(float(x) for x in sys.argv[4].split(','))
                 if len(sys.argv) > 4 else FINE_BANDS)
        d = curl(f'{ORCA}/pools/{pool_addr}')
        p = (d or {}).get('data') or d
        if not p or not p.get('tokenA'):
            raise SystemExit(f'Orca does not know a pool at {pool_addr}')
        out = ladder(p, candles(pool_addr), bands, cap)
        if not out:
            raise SystemExit('pool cannot be priced consistently (candles vs pool price, '
                             'or implied concentration out of range)')
        rows, meta = out
        print(f"{p['tokenA']['symbol']}/{p['tokenB']['symbol']}  ${cap:.0f}")
        print_ladder(rows, meta, choose(rows, 0.5))
    else:
        print('usage: python3 engine.py ladder <pool> [capital_usd] [bands]')
