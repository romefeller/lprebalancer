"""Meteora DLMM fee counters, sampled for the fee / LVR study (sql/043).

DLMM has no pool-wide fee growth. Each bin keeps the fees per unit of its
liquidity share, X and Y, as Q64 numbers that only grow. A bin's share supply
is its value in raw Y, Q64 (checked on 5rCf 2026-10-15: supply / 2^64 is
price * x + y within 0.05% in every bin). So between two samples a bin
earned (dfx * price + dfy) / 2^64 of its value, in raw Y per raw Y.

    python3 -m venues.meteora_dlmm.fees sample <pool> [span]   one sample into the table
    python3 -m venues.meteora_dlmm.fees compare <dlmm pool> <clmm pool> [band]
        both pools' +/-band income per hour of SOL volatility (research)

Nothing in the bot reads this; it moves no money.
"""
import json
import math
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
Q64 = 2 ** 64
U128 = 2 ** 128
KEEP_DAYS = 14
SAMPLE_TIMEOUT_S = 90


def bin_fee_fraction(before, after):
    """What one bin earned between two samples, as a fraction of its value.
    `before` and `after` are sample rows [id, price, x, y, supply, fx, fy]
    of the same bin; price is raw Y per raw X. Counters wrap at 2^128.
    None for an empty bin (it earns nothing and has no value). Pure."""
    if before[0] != after[0]:
        raise ValueError(f'bins differ: {before[0]} and {after[0]}')
    if int(before[4]) <= 0:
        return None
    dfx = (int(after[5]) - int(before[5])) % U128
    dfy = (int(after[6]) - int(before[6])) % U128
    return (dfx * float(before[1]) + dfy) / Q64


def band_income(first, last, band):
    """Fee income of a uniform-value position on the bins within +/-band
    (1.01 = +/-1%) of the FIRST sample's active price, in % of its value per
    day, from two samples (dicts with 'ts' seconds, 'active_id', 'bins').
    A spot-shaped DLMM position holds equal value per bin. None if no bin of
    the band has liquidity in both samples. Pure."""
    seconds = last['ts'] - first['ts']
    if seconds <= 0 or band <= 1:
        return None
    by_id = {b[0]: b for b in last['bins']}
    active = next((b for b in first['bins'] if b[0] == first['active_id']), None)
    if active is None:
        return None
    p0 = float(active[1])
    fracs = []
    for b in first['bins']:
        if not p0 / band <= float(b[1]) <= p0 * band or b[0] not in by_id:
            continue
        f = bin_fee_fraction(b, by_id[b[0]])
        if f is not None:
            fracs.append(f)
    if not fracs:
        return None
    return sum(fracs) / len(fracs) / seconds * 86400 * 100


def read_sample(pool, span=60):
    """One read of the pool through fee_sample.mjs (no key). Raises on failure."""
    r = subprocess.run(['node', str(ROOT / 'venues' / 'meteora_dlmm' / 'fee_sample.mjs'), pool, str(span)],
                       capture_output=True, text=True, timeout=SAMPLE_TIMEOUT_S, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    s = json.loads(r.stdout)
    if s.get('pool') != pool or not isinstance(s.get('bins'), list):
        raise RuntimeError('fee_sample.mjs answered for another pool or without bins')
    return s


def record(s):
    """Store one sample; samples older than KEEP_DAYS go."""
    import db
    with db.cursor(commit=True) as cur:
        cur.execute('insert into dlmm_fee_samples (pool, active_id, bin_step, base_fee_pct, dynamic_fee_pct, '
                    'dec_x, dec_y, bins) values (%s,%s,%s,%s,%s,%s,%s,%s)',
                    (s['pool'], s['active_id'], s['bin_step'], s['base_fee_pct'], s['dynamic_fee_pct'],
                     s['dec_x'], s['dec_y'], json.dumps(s['bins'])))
        cur.execute('delete from dlmm_fee_samples where ts < now() - make_interval(days => %s)', (KEEP_DAYS,))


def _samples(pool):
    import db
    with db.cursor() as cur:
        cur.execute('select extract(epoch from ts) ts, active_id, dynamic_fee_pct, bins from dlmm_fee_samples '
                    'where pool = %s order by ts', (pool,))
        return [dict(r, ts=float(r['ts'])) for r in cur.fetchall()]


def _clmm_samples(pool):
    import db
    with db.cursor() as cur:
        cur.execute('select extract(epoch from ts) ts, sqrt_price, g0, g1, dec_a, dec_b from fee_growth '
                    'where pool = %s and ts >= (select min(ts) from dlmm_fee_samples) order by ts', (pool,))
        return [dict(r, ts=float(r['ts'])) for r in cur.fetchall()]


def compare(dlmm_pool, clmm_pool, band=1.01):
    """Both pools' +/-band income, % per day, per hour, with the hour's SOL
    variance from the CLMM pool's own price: rows (hour, dlmm %, clmm %,
    sum r^2, mean dynamic fee %). Research only."""
    from venues import solana_state
    d, c = _samples(dlmm_pool), _clmm_samples(clmm_pool)
    hours = {}
    for a, b in zip(d, d[1:]):
        if 60 < b['ts'] - a['ts'] < 1800:
            inc = band_income(a, b, band)
            if inc is not None:
                h = hours.setdefault(int(a['ts'] // 3600), {'d': [], 'c': [], 'r2': 0.0, 'fee': []})
                h['d'].append(inc); h['fee'].append(a['dynamic_fee_pct'])
    for a, b in zip(c, c[1:]):
        if 60 < b['ts'] - a['ts'] < 1800:
            h = hours.get(int(a['ts'] // 3600))
            if h is None:
                continue
            # token B is a $1 stable (SOL/USDC): token A's price is the pool's own
            sp0, sp1 = int(a['sqrt_price']) / Q64, int(b['sqrt_price']) / Q64
            usd_a = sp1 ** 2 * 10 ** (int(b['dec_a']) - int(b['dec_b']))
            inc = solana_state.band_income(a, b, b['ts'] - a['ts'], band, usd_a, 1.0)
            h['r2'] += (2 * math.log(sp1 / sp0)) ** 2
            if inc:
                h['c'].append(inc['fee_pct_day'])
    out = []
    for hr, h in sorted(hours.items()):
        if h['d'] and h['c']:
            out.append((hr, sum(h['d']) / len(h['d']), sum(h['c']) / len(h['c']), h['r2'],
                        sum(h['fee']) / len(h['fee'])))
    return out


def main(argv):
    if len(argv) >= 2 and argv[0] == 'sample':
        record(read_sample(argv[1], int(argv[2]) if len(argv) > 2 else 60))
        return 0
    if len(argv) >= 3 and argv[0] == 'compare':
        rows = compare(argv[1], argv[2], float(argv[3]) if len(argv) > 3 else 1.01)
        for r in rows:
            print(json.dumps({'hour': r[0], 'dlmm_pct_day': round(r[1], 4), 'clmm_pct_day': round(r[2], 4),
                              'sum_r2': r[3], 'dynamic_fee_pct': round(r[4], 5)}))
        return 0
    print(__doc__.strip().split('\n\n')[1], file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
