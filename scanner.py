"""The board: every busy concentrated-liquidity pool on every DEX the profile
names, scored under the bot's own model, screened, written to Postgres.

    python3 scanner.py            # scan once, print the board
    python3 scanner.py board      # print the last board without scanning
    touch RESCAN                  # make the running bot scan on its next tick

Inside the bot it runs as a thread. The scan is slow — one GeckoTerminal
request per pool at the free tier's pace, a few minutes for a full board — and
the trading loop must not wait on it, so the thread writes to `scan_runs` /
`scan_pools` and the loop reads the latest row when it next re-optimises. A
scan that fails leaves the previous board in place and says so in the feed.
"""
import pathlib
import threading
import time
import traceback

import config
import db
import dexes
import engine


def blocked(rec):
    """Why a pool cannot be opened at all, or None."""
    if rec['dex'] == 'orca' and rec.get('adaptive_fee'):
        return 'adaptive-fee pool: the Orca signer cannot open it (error 6069)'
    if rec.get('blacklisted'):
        return 'blacklisted by its DEX'
    return None


def run_once(notify=None, progress=None):
    """One scan. Returns (run_id, rows)."""
    t0 = time.time()
    listed, errors = dexes.fetch_all(config.DEXES, config.SCAN_LIMIT)
    flat = [r for rows in listed.values() for r in rows]
    season = {}
    rows = engine.score_board(
        flat, config.CAPITAL_USD, config.BANDS, config.SWAP_COST,
        config.MAX_REBALANCES_PER_DAY_MODELLED, config.MIN_TVL_USD, config.MIN_VOLUME_24H_USD,
        blocked=blocked, progress=progress, season_out=season, policy=config.policy())
    for r in rows:
        r['executable'] = (r['dex'] in config.EXECUTE_DEXES and r.get('net_day_pct') is not None
                           and not r.get('skipped'))
    run_id = db.record_scan(config.PROFILE, config.DEXES, rows, errors, time.time() - t0, len(flat),
                            season=season.get('profile'))
    scored = [r for r in rows if r.get('net_day_pct') is not None]
    if notify:
        notify('SCAN', run=run_id, dexes=list(config.DEXES), listed=len(flat), scored=len(scored),
               seconds=round(time.time() - t0),
               errors=errors or None,
               season_now=(round(season['profile'][db.now().hour], 2) if season.get('profile') else None),
               top=[{'dex': r['dex'], 'pair': r['pair'], 'band': f"+/-{r['band_pct']:.0f}%",
                     'net_day_pct': round(r['net_day_pct'], 3),
                     'use_day_pct': round(r.get('decision_day_pct', r['net_day_pct']), 3),
                     'drift': (round(r['liquidity_drift'], 2) if r.get('liquidity_drift') is not None else None),
                     'rebal_per_day': round(r['rebal_per_day'], 2),
                     'tvl_musd': round(r['tvl_usd'] / 1e6, 2),
                     'ok': bool(r.get('screen_ok')), 'can_open': bool(r.get('executable'))}
                    for r in scored[:6]])
    return run_id, rows


class Scanner(threading.Thread):
    """Scans every `config.SCAN_INTERVAL` seconds; never raises into the loop."""

    def __init__(self, notify, first_delay=30):
        super().__init__(name='scanner', daemon=True)
        self.notify = notify
        self.first_delay = first_delay
        self.stop = threading.Event()

    def due(self):
        trigger = pathlib.Path(__file__).resolve().parent / 'RESCAN'
        if trigger.exists():
            trigger.unlink()     # consumed before it runs, like the other triggers
            return True
        run, _ = db.latest_scan()
        if not run:
            return True
        if not run.get('season'):
            return True          # a board without the day's rhythm is from older code
        return (db.now() - run['ts']).total_seconds() >= config.SCAN_INTERVAL

    def run(self):
        self.stop.wait(self.first_delay)
        while not self.stop.is_set():
            try:
                if self.due():
                    run_once(self.notify)
            except Exception as e:           # the board is advisory; the loop is not
                self.notify('scan_failed', reason=f'{type(e).__name__}: {str(e)[:160]}',
                            trace=traceback.format_exc()[-600:])
            self.stop.wait(60)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'board':
        run, rows = db.latest_scan()
        if not run:
            raise SystemExit('no scan yet')
        print(f"scan #{run['id']} at {run['ts']:%Y-%m-%d %H:%M} UTC")
        engine.print_board(rows, top=60)
    else:
        print(f'scanning {", ".join(config.DEXES)} (top {config.SCAN_LIMIT} by volume each) '
              f'for ${config.CAPITAL_USD:.0f}', flush=True)
        run_id, rows = run_once(
            progress=lambda i, n, r: print(f'  [{i + 1}/{n}] {r["dex"]:<22} {r["pair"]}', flush=True))
        print(f'scan #{run_id}')
        engine.print_board(rows, top=60)
