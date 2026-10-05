"""Replay testing: turn recorded data into fixtures for tests/test_replay.py.

Reads the database named by LPBOT_DSN (the live book by default) and only
SELECTs. Writes gzipped files under tests/replay/.

    tests/replay_capture.py polls sol-usdc '2026-10-05 19:00' '2026-10-05 23:00' evening
        the loop's recorded polls (sql/032): what each poll saw and the verdict
        it reached, so a code change that moves any verdict fails the test

    tests/replay_capture.py tape sol-usdc '2026-10-04 00:00' '2026-10-05 18:00' weekend
        the pool's five-minute tape (rebalancer.tape5) and the points where the
        loop read it (rebalancer.risk_profile): each point keeps the width view
        today's calm.regime_view gives, so a change to the width math fails
"""
import argparse
import gzip
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / 'tests' / 'replay'
sys.path.insert(0, str(ROOT))
os.environ.setdefault('LPBOT_DSN', 'dbname=rebalancer')

import calm  # noqa: E402
import db  # noqa: E402

KEPT = ('mode', 'choice', 'held', 'inside', 'p_held', 'probs', 'sigma_5m_pct', 'velocity')
MAX_POINTS = 240                # a tape fixture's points, at most: every mode change, then evenly spaced


def write(name, payload_lines):
    OUT.mkdir(exist_ok=True)
    path = OUT / name
    with gzip.open(path, 'wt') as fh:
        for line in payload_lines:
            fh.write(json.dumps(line, separators=(',', ':')) + '\n')
    print(f'{path.relative_to(ROOT)}: {len(payload_lines)} lines')


def capture_polls(profile, since, until, name):
    with db.cursor() as cur:
        cur.execute('select seen, verdict from replay_polls where profile = %s and ts >= %s and ts < %s order by ts',
                    (profile, since, until))
        rows = cur.fetchall()
    if not rows:
        raise SystemExit(f'no recorded polls of {profile} in [{since}, {until})')
    write(f'polls_{name}.jsonl.gz', [{'seen': r['seen'], 'verdict': r['verdict']} for r in rows])


def width_view(bars, at, price, lower, upper, widths, horizon, threshold, steps):
    """The width view at `at` from the bars that had closed by then, with the
    move the loop would make on it."""
    ts = bars[0]
    n = int((ts + calm.BAR_SECONDS <= at).sum())
    cut = tuple(a[:n] for a in bars)
    v = calm.regime_view(cut, price, lower, upper, widths=widths, horizon_minutes=horizon, threshold=threshold)
    out = {k: v[k] for k in KEPT}
    out['move'] = calm.regime_decide(v, widths=widths, steps=steps)
    out['bars'] = n
    return out


def capture_tape(profile, since, until, name):
    import numpy as np
    with db.cursor() as cur:
        cur.execute('select pool, regime_widths, regime_horizon_minutes, regime_steps from config where name = %s',
                    (profile,))
        cfg = cur.fetchone()
        cur.execute("""select extract(epoch from r.ts) at, r.price, r.mode, r.threshold, p.lower_price, p.upper_price
                       from risk_profile r join positions p on p.mint = r.mint
                       where r.pool = %s and r.ts >= %s and r.ts < %s and not r.stale
                         and p.lower_price is not null and p.upper_price is not null
                       order by r.ts""", (cfg['pool'], since, until))
        rows = cur.fetchall()
    if not rows:
        raise SystemExit(f'no risk_profile rows of {cfg["pool"]} in [{since}, {until})')
    widths = tuple(float(w) for w in (cfg['regime_widths'] or calm.WIDTHS))
    horizon, steps = int(cfg['regime_horizon_minutes'] or 120), int(cfg['regime_steps'] or 2)
    first, last = float(rows[0]['at']), float(rows[-1]['at'])
    bars = db.tape_load(cfg['pool'], first - 30 * 86400)
    if bars is None:
        raise SystemExit(f'no tape for {cfg["pool"]}')
    keep = bars[0] <= last
    # rounded as the fixture stores them, so the view is the one the test recomputes
    bars = tuple(np.round(a[keep], 10) for a in bars)
    changes = [i for i in range(len(rows)) if i == 0 or rows[i]['mode'] != rows[i - 1]['mode']]
    rest = [i for i in range(len(rows)) if i not in set(changes)]
    room = max(MAX_POINTS - len(changes), 0)
    picked = sorted(set(changes[:MAX_POINTS]) | set(rest[::max(1, len(rest) // max(room, 1))][:room]))
    points = []
    for i in picked:
        r = rows[i]
        p = {'at': float(r['at']), 'price': float(r['price']), 'lower': float(r['lower_price']),
             'upper': float(r['upper_price']), 'threshold': float(r['threshold'])}
        p['view'] = width_view(bars, p['at'], p['price'], p['lower'], p['upper'], widths, horizon, p['threshold'], steps)
        points.append(p)
    head = {'pool': cfg['pool'], 'widths': widths, 'horizon': horizon, 'steps': steps,
            'bars': [a.tolist() for a in bars]}
    write(f'tape_{name}.jsonl.gz', [head] + points)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('kind', choices=('polls', 'tape'))
    ap.add_argument('profile')
    ap.add_argument('since')
    ap.add_argument('until')
    ap.add_argument('name')
    a = ap.parse_args()
    (capture_polls if a.kind == 'polls' else capture_tape)(a.profile, a.since, a.until, a.name)


if __name__ == '__main__':
    main()
