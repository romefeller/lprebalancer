"""Rebalancer — a concentrated-liquidity rebalancer across Solana DEXes.

Holds one position, watches the price against its band, and when the price
leaves, collects the fees, closes, re-optimises the band and opens again. Every
number it reports comes from the chain or from its own ledger, never from a
simulation of what it thinks happened.

It also optimises the pool, not only the band. A scanner thread lists the
busiest pools on every DEX in the profile's `dexes`, scores each under the same
replay the band optimiser uses, and writes the board to Postgres. At each
re-optimisation the loop compares the pool it holds with the best pool on the
board; when the board's best beats it by `migrate_min_gain` and its DEX has a
signer (`execute_dexes`), the bot closes here and opens there. When it has no
signer for that DEX it says so, with the command to move by hand.

Every parameter comes from the `rebalancer.config` table and every observation
goes back into Postgres, so the pool, the band ladder and the guards are data,
not code.

    read chain -> in band?  yes -> forecast: P(exit within H hours) from the pool's own tape
                                   high -> harvest, close, re-centre NOW (quiet hour if one is near)
                                   low  -> record a snapshot, wait
                            no  -> harvest, close, re-optimise, reopen

The bot acts before the price leaves, not after. A rebalance at the edge makes
the position's whole loss against holding permanent and leaves it one-sided
and earning nothing until it runs; a re-centre while still inside is done at
a price of the bot's choosing, and the survival figures that drive it are in
every book it sends. Accrued fees are harvested into the wallet on a schedule
(the dividend), so income is realised and permanent rather than a number on
an open position.

Three things it does that a simpler loop gets wrong:

**A failed write is not a known outcome.** A transaction can land and still
report failure, because confirmation runs over the same rate-limited RPC that
just timed out. This happened in production: a close succeeded, reported
`close_failed`, and the bot left the capital idle. So after any write error the
bot re-reads the chain and believes what it finds there, not the error.

**A failed read is not an empty wallet.** Treating an RPC error as "no position"
makes a bot open a second one, and on a flaky endpoint it keeps opening them
until the wallet is empty. Reads that fail hold; only a read that succeeds and
reports nothing may open.

**Fees belong to you, not to the position.** A position's fee counter resets
every time you rebalance. Cumulative earnings live in the ledger, split into
realised (harvested into the wallet) and unrealised (still in the position).

Stop it at any time with:       touch HALT
Force one rebalance now with:   touch REBALANCE
Force the pool and band review: touch REOPT
Move to a pool by hand:         echo "<dex> <pool>" > MIGRATE

The trigger exists because the rebalance path is the one that runs unattended,
and a path that has only ever run at 3am has never been watched. Touch the file
while you are looking and the next poll runs harvest -> close -> reopen at the
best band, subject to the same gap and daily limits as an automatic one.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import calm
import config
import db
import dexes
import engine
import guards
import scanner

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / 'runtime.json'
FEED = ROOT / 'events.jsonl'
HALT = ROOT / 'HALT'
REBALANCE = ROOT / 'REBALANCE'
REOPT = ROOT / 'REOPT'          # run the board and band review on the next poll
MIGRATE = ROOT / 'MIGRATE'      # "<dex> <pool>": move there on the next poll
# One signer per DEX. A DEX without an entry can be scanned and recommended
# but never opened; `execute_dexes` must not name it.
SIGNERS = {'orca': str(ROOT / 'signer2.mjs'),
           'meteora-dlmm': str(ROOT / 'signer_dlmm.mjs'),
           'raydium-clmm': str(ROOT / 'signer_raydium.mjs'),
           'byreal': str(ROOT / 'signer_byreal.mjs'),
           'pancakeswap-v3-solana': str(ROOT / 'signer_pancake.mjs'),
           'jupiter': str(ROOT / 'swap_jupiter.mjs')}       # swaps, not positions


def stamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def tidy(err, limit=140):
    """Turn a wall of RPC error text into one readable line.

    Raw provider errors arrive as a nested dump with headers and cookies. Sent
    to Telegram verbatim they read as gibberish with a 429 buried in them, which
    is exactly how a healthy bridge came to look like a broken one.
    """
    if not err:
        return None
    s = ' '.join(str(err).split())
    # A program rejection is authoritative even if earlier RPC retries logged 429.
    if re.search(r'PriceSlippageCheck|price slippage check|0x1781\b|Custom["\s:]+6017\b', s, re.I):
        return 'PriceSlippageCheck (6017): price moved beyond the slippage limit'
    program = re.search(r'(?:InstructionError|custom program error|failed on chain|simulation failed).*', s, re.I)
    if program:
        return program.group(0)[:limit]
    for needle, plain in (
            ('Too Many Requests', 'RPC rate limited'),
            ('timeout', 'RPC timeout'),
            ('ECONNRESET', 'RPC connection reset'),
            ('blockhash', 'blockhash expired')):
        if needle.lower() in s.lower():
            return plain
    if re.search(r'\b429\b', s):
        return 'RPC rate limited'
    return s[:limit]


def notify(event, **payload):
    row = {'t': stamp(), 'event': event, **payload}
    with open(FEED, 'a') as fh:
        fh.write(json.dumps(row, default=str) + '\n')
    print(f'[{row["t"]}] {event}: {json.dumps(payload, default=str, sort_keys=True)}',
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


def notify_book(event, **payload):
    """notify() with the cumulative book attached.

    The book is merged last and wins on any shared key, so a caller can pass a
    convenient label without risking a duplicate-keyword error at the one moment
    the bot most needs to report something.
    """
    return notify(event, **{**payload, **db.stats()})


def chain(*args, dex=None, timeout=420):
    """Call the signer for `dex` (the active pool's by default).
    Returns (parsed_json, tidy_error)."""
    script = SIGNERS.get(dex or config.DEX)
    if not script or not pathlib.Path(script).exists():
        return None, f'no signer for {dex or config.DEX}'
    # Arguments reach node's argv, never a shell, but an address with a
    # newline in it is still not an address. Refuse before spawning.
    try:
        guards.signer_args(args)
        guards.inside(pathlib.Path(script), ROOT)
    except guards.Refused as e:
        return None, f'refused: {e}'
    env = dict(os.environ,
               WALLET_SECRET_PATH=config.WALLET,
               SOLANA_RPC_URL=config.RPC,
               LPBOT_POOL=config.POOL,
               LPBOT_MAX_USD=str(config.MAX_USD),
               LPBOT_SLIPPAGE_BPS=str(config.SLIPPAGE_BPS),
               LPBOT_GAS_RESERVE_SOL=str(config.GAS_RESERVE_SOL))
    try:
        r = subprocess.run(['node', script, *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, 'signer timed out'
    text = (r.stdout or '') + (r.stderr or '')
    # Only stdout carries signer results. Decode one object despite trailing logs.
    # An error/partial JSON result is never silently turned into success.
    decoder = json.JSONDecoder()
    for match in re.finditer(r'(?m)^\s*\{', r.stdout or ''):
        try:
            out, _ = decoder.raw_decode(r.stdout[match.start():].lstrip())
        except ValueError:
            continue
        if not isinstance(out, dict):
            continue
        err = out.get('error') or ('partial transaction execution' if out.get('partial') else None)
        if r.returncode or err:
            return out, tidy(err or r.stderr or text) or f'signer exited {r.returncode}'
        return out, None
    return None, tidy(text) or (f'signer exited {r.returncode}' if r.returncode else 'no signer result')


def read_status(mint=None):
    """The open position, from the chain. Every signer gets the pool through
    LPBOT_POOL in its environment (see chain); a positional argument is a
    POSITION filter, never the pool. Passing the pool there once made the
    Meteora signer answer "no position" for a position it held, and the loop
    went to open a second one."""
    return chain('status', *([mint] if mint else []))


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {'last_rebalance': 0, 'rebalance_times': [], 'failures': 0,
            'read_failures': 0, 'last_reopt': 0, 'last_harvest': 0,
            'calm_times': [], 'calm': False}


def save(s):
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(s, indent=1, default=str))
    tmp.replace(STATE)


def halt(reason):
    HALT.write_text(f'{stamp()} {reason}')
    db.event('BREAKER', reason)
    notify('BREAKER', reason=reason, action='HALT written; will not restart')


def wallet(pool):
    """What the wallet holds of this pool's two tokens, and its dollar value.

    Both tokens, not just SOL. After a close the withdrawn quote token sits in
    the wallet; counting SOL alone would drop it from equity and report a loss
    on every rebalance that never happened.
    """
    out, _ = chain('balance', pool)
    if not out or 'balanceA' not in out:
        # One retry. The read that follows a close lands on an endpoint that
        # has just confirmed a transaction for us and is quick to rate-limit;
        # a second try ten seconds later has read cleanly every time so far.
        time.sleep(10)
        out, _ = chain('balance', pool)
    return out or {}


def deposit_caps(bal):
    """Per-token deposit caps for an open, from what the wallet actually holds.

    Each side is capped at `side_cap_fraction` of the capital, in that token's
    own units, and at the wallet's balance of it less the gas reserve when the
    token is native SOL. The SDK's quote picks the liquidity both caps allow, so
    a wallet that is short one side opens a smaller position rather than failing.
    """
    price = bal['price']
    quote_usd = bal.get('quoteUsd') or 1.0
    capital_quote = config.CAPITAL_USD / quote_usd
    reserve = config.GAS_RESERVE_SOL
    avail_a = bal['balanceA'] - (reserve if bal.get('nativeSide') == 'A' else 0)
    avail_b = bal['balanceB'] - (reserve if bal.get('nativeSide') == 'B' else 0)
    cap_a = min(max(avail_a, 0), capital_quote * config.SIDE_CAP_FRACTION / price)
    cap_b = min(max(avail_b, 0), capital_quote * config.SIDE_CAP_FRACTION)
    return cap_a, cap_b


def position_usd(status):
    """Mark of the position: what a close would return right now, tokens AND
    the rent locked in the position accounts.

    The signer already values the tokens in dollars when it knows the quote
    token's price. Falling back to quote units is only correct when the quote
    token is a dollar, which is why the signer's figure is preferred. The rent
    is a deposit the chain refunds on close: a 387-bin Meteora position holds
    0.2 SOL of it, and a book that ignored it reported the first move to
    Meteora as a $27 loss that never happened.
    """
    rent = status.get('rentUsd') or 0.0
    if status.get('positionUsd') is not None:
        return status['positionUsd'] + rent
    a, b = status.get('closeEstA'), status.get('closeEstB')
    if a is None or b is None:
        return None
    return (a * status['price'] + b) * (status.get('quoteUsd') or 1) + rent


# --- the tape, the forecast, the dividend -------------------------------------

_TAPE = {}                      # pool -> (fetched_at, candles)
TAPE_REFRESH = 3600             # one GeckoTerminal call an hour, at most


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
            c = fresh
            _TAPE[pool] = (time.time(), c)
    return c


def forecast_for(status):
    """The band forecast for the held position, or None without a tape."""
    c = tape(status.get('whirlpool') or config.POOL)
    if not c:
        return None
    opened = db.position_opened(status['positionMint'])
    hours = ((db.now() - opened).total_seconds() / 3600) if opened else None
    return engine.band_forecast(c[1], status['price'], status['lowerPrice'], status['upperPrice'],
                                open_price=db.position_open_price(status['positionMint']),
                                hours_alive=hours, horizon=config.PROACTIVE_HORIZON,
                                threshold=config.PROACTIVE_THRESHOLD)


def harvest_due(state, status):
    """The dividend: accrued fees go to the wallet every HARVEST_INTERVAL,
    once at least MIN_HARVEST_USD has accrued. Never while a rebalance is
    about to harvest anyway."""
    if not config.HARVEST_INTERVAL:
        return False
    if (status.get('feesAccrued_USD') or 0) < config.MIN_HARVEST_USD:
        return False
    return time.time() - state.get('last_harvest', 0) >= config.HARVEST_INTERVAL


def dividend(state, status):
    """Harvest into the wallet and report it. The ledger counts it once: the
    snapshot after the harvest records the position's counter at zero."""
    mint = status['positionMint']
    a, b, usd = status.get('feesAccruedA', 0.0), status.get('feesAccruedB', 0.0), status.get('feesAccrued_USD', 0.0)
    out, err = chain('harvest', mint, '--execute')
    state['last_harvest'] = time.time(); save(state)
    if out and out.get('signature') and not err:
        db.record_harvest(mint, a, b, usd, out['signature'])
        db.snapshot(mint, status['price'], status.get('inRange'), status.get('liquidity'),
                    0.0, 0.0, 0.0, wallet(status['whirlpool']).get('walletUsd'), position_usd(status))
        db.event('DIVIDEND', f'${usd:.4f} harvested to the wallet')
        notify_book('DIVIDEND', collected_usd=round(usd, 4), collected_a=a, collected_b=b,
                    signature=out['signature'])
        return True
    notify('harvest_skipped', reason=err or 'no signature returned', kind='dividend')
    return False


# --- calm mode: a tight band while the market is cold ---------------------------

_TAPE5 = {}                     # pool -> (fetched_at, bars)
TAPE5_REFRESH = 240             # one GeckoTerminal call per five-minute bar, at most


def tape5(pool, price):
    t, b = _TAPE5.get(pool, (0, None))
    if b is None or time.time() - t > TAPE5_REFRESH:
        try:
            fresh = calm.tape_5m(pool, live_price=price)
        except Exception:
            fresh = None
        if fresh is not None:
            b = fresh
            _TAPE5[pool] = (time.time(), b)
    return b


def calm_budget_left(state):
    now = time.time()
    used = [t for t in state.get('calm_times', []) if now - t < 86400]
    return max(config.CALM_MAX_MOVES - len(used), 0)


def calm_view(state, status):
    """calm.view for the held position, or None when calm mode is off or the
    five-minute tape is unavailable. Remembers the calm state across polls,
    which the hysteresis needs."""
    if not config.CALM_ENABLED:
        return None
    bars = tape5(status.get('whirlpool') or config.POOL, status['price'])
    v = calm.view(bars, status['price'], status['lowerPrice'], status['upperPrice'],
                  was_calm=bool(state.get('calm')), cut=config.CALM_SIGMA_CUT,
                  exit_mult=config.CALM_EXIT_MULT, band=config.CALM_BAND,
                  horizon_minutes=config.CALM_HORIZON_MINUTES, threshold=config.CALM_THRESHOLD)
    if v:
        if bool(state.get('calm')) != v['calm']:
            state['calm'] = v['calm']; save(state)
            db.event('CALM_ON' if v['calm'] else 'CALM_OFF',
                     f"sigma {v['sigma_5m_pct']}% vs cut {v['cut_pct']}%")
        v['budget_left'] = calm_budget_left(state)
        v['moves_24h'] = config.CALM_MAX_MOVES - v['budget_left']
    return v


def calm_reopen_band(v, state):
    """The band to reopen at after a tight band exits: tight again while calm,
    budget left and a fresh tight band is unlikely to be touched soon;
    otherwise None, which means the ladder's band."""
    if not v or not v.get('calm') or calm_budget_left(state) <= 0:
        return None
    pf = v.get('p_touch_fresh')
    if pf is not None and pf >= config.CALM_THRESHOLD:
        return None
    return config.CALM_BAND


def resume_reopen(state):
    """Resume a confirmed CALM close without a pool review or a second move.

    The persisted intent survives a swap/open failure and a service restart.
    Reopen rechecks current conditions; missing data means wait, not wide-then-tight.
    """
    pending = state.get('pending_reopen')
    if not pending:
        return False
    if time.time() - pending.get('started_at', 0) > 86400:
        state.pop('pending_reopen', None); save(state)
        notify('idle', reason='dropped a CALM reopen intent older than a day')
        return False
    if (pending['pool'], pending['dex']) != (config.POOL, config.DEX):
        halt('pending reopen belongs to a different pool; refusing to spend its funds elsewhere')
        return True
    if not pending.get('closed'):
        # The process may have stopped after close landed but before recording it.
        db.close_position(pending['mint'], None, pending.get('withdraw_usd'))
        moves = state.setdefault('calm_times', [])
        if pending['started_at'] not in moves:
            moves.append(pending['started_at'])
        pending['closed'] = True
        save(state)
    reopen(state, pending['reason'], band=pending['band'], recovering=True)
    return True


def balance_wallet(state, bal, rec):
    """Swap the wallet to about 50/50 through Jupiter before an open, when
    either side holds less than half the capital, which is what a centred
    band needs of each. Returns the wallet as it is after, or None when a
    swap failed (counted as a failure; the caller opens nothing).

    The audit of the calm study showed why the gate is "short of half" and
    not "empty": with swaps only on a one-sided wallet, every in-range move
    reopens capped by the scarcer token and the tight band's gain turns
    negative. The swap script itself does nothing within 2% of target.

    Without this an open after an exit is limited by the scarcer token: a
    band that left above holds only the quote token, and the reopen deposits
    a sliver of the capital. See SWAP_HOOK.md for the contract."""
    if not config.REBALANCE_SWAP or not rec:
        return bal
    q = bal.get('quoteUsd') or 1.0
    res = config.GAS_RESERVE_SOL
    usd_a = max(bal['balanceA'] - (res if bal.get('nativeSide') == 'A' else 0), 0) * bal['price'] * q
    usd_b = max(bal['balanceB'] - (res if bal.get('nativeSide') == 'B' else 0), 0) * q
    C = config.CAPITAL_USD
    if min(usd_a, usd_b) >= 0.5 * C:
        return bal
    mint_a = (rec.get('token_a') or {}).get('address')
    mint_b = (rec.get('token_b') or {}).get('address')
    if not (guards.is_address(mint_a) and guards.is_address(mint_b)):
        notify('swap_skipped', reason='pool record has no mints')
        return bal
    target = f'{C * config.SIDE_CAP_FRACTION:.2f}'
    out, err = chain('rebalance', mint_a, mint_b, target, target, '--execute', dex='jupiter')
    if out and out.get('noop'):
        notify('swap_skipped', reason='already at target', usd_a=round(usd_a, 2), usd_b=round(usd_b, 2))
        return bal
    if err or not out or out.get('partial') or not out.get('sent'):
        state['failures'] += 1; save(state)
        db.event('swap_failed', err or 'no signature')
        notify('swap_failed', reason=err or 'no signature', failures=state['failures'],
               signature=(out or {}).get('signature'))
        if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
            halt(f'{state["failures"]} consecutive failures')
        return None
    db.event('SWAP', f"{out.get('signature')} usd {out.get('swapUsdValue')}")
    notify('SWAP', signature=out.get('signature'), usd=out.get('swapUsdValue'),
           sold=out.get('sold'), bought=out.get('bought'),
           price_impact_pct=out.get('priceImpactPct'), route=out.get('routePlan'),
           before_usd_a=round(usd_a, 2), before_usd_b=round(usd_b, 2))
    time.sleep(5)
    after = wallet(config.POOL)
    return after if 'balanceA' in after else None


def best_band_for(pool, dex=None):
    """Score every candidate band on this pool's own recent data.

    Each band is replayed from many origins, not once: the bot decides on the
    median net return per day across rolling windows, so a band that fitted
    the last six weeks by luck does not beat one that works from most starting
    points. The churn gate then discards bands that rebalance too often.
    """
    try:
        p = dexes.pool(dex or config.DEX, pool)
    except Exception:
        p = None
    if not p:
        return None
    out = engine.ladder(p, engine.candles(pool), dexes.feasible_bands(p, config.BANDS),
                        config.CAPITAL_USD, config.SWAP_COST, policy=config.policy())
    if not out:
        return None
    rows, meta = out
    pick = dict(engine.choose(rows, config.MAX_REBALANCES_PER_DAY_MODELLED))
    pick['price'] = meta['price']
    pick['fee'] = meta['fee']
    pick['all_runs'] = rows
    pick['record'] = p
    return pick


# --- the board: which pool to be in -----------------------------------------

def board_pick(current_best, exclude_address):
    """The best pool on the board other than the one held, and the case for
    moving to it.

    Returns (run, best_row, gain, verdict). `gain` is the relative improvement
    of the board's best modelled net/day over `current_best`, the held pool's
    own best band under the same model. A candidate must be scored, screened,
    and — unless swaps are allowed — hold the same two tokens the wallet
    already holds, because entering a different pair means buying it.
    """
    run, rows = db.latest_scan(max_age_seconds=config.SCAN_INTERVAL * 3)
    if not rows:
        return run, None, None, 'no fresh board'
    held = (current_best or {}).get('record') or {}
    mints = {(held.get('token_a') or {}).get('address'), (held.get('token_b') or {}).get('address')}
    cands = []
    for r in rows:
        if r.get('net_day_pct') is None or r.get('skipped') or r['address'] == exclude_address:
            continue
        if not r.get('screen_ok'):
            continue
        if not config.ALLOW_SWAP and mints - {None}:
            if {r['token_a']['address'], r['token_b']['address']} != mints:
                continue
        cands.append(r)
    if not cands:
        return run, None, None, 'no eligible candidate on the board'
    # Ranked on the decision figure: the modelled median, scaled down where
    # the pool's own last-24h fees say the model's fee share is stale.
    use = lambda r: r.get('decision_day_pct') if r.get('decision_day_pct') is not None else r['net_day_pct']
    best = max(cands, key=use)
    if not current_best:
        return run, best, None, 'held pool unscorable'
    cur = current_best['net_day_pct']
    gain = (use(best) - cur) / max(abs(cur), 1e-9)
    return run, best, gain, None


def utc_hour():
    return db.now().hour


def busy_hour():
    """True when this UTC hour usually carries more than the average volume,
    so a voluntary move would cost the most. Uses the board's latest profile;
    with none, no hour is busy."""
    if not config.DEFER_MOVES_TO_QUIET_HOURS:
        return False
    o = db.season_outlook(db.season(), hour=utc_hour())
    return bool(o and not o['quiet'])


def consider_migration(state, status, current_best):
    """Compare the held pool with the board and act. Returns True when a
    rebalance was started (the caller then skips its own)."""
    if config.POOL_PINNED:
        return False
    run, best, gain, why = board_pick(current_best, config.POOL)
    if not best:
        notify('board_checked', verdict=why, scan=(run or {}).get('id'))
        return False
    cur_txt = (f"{config.DEX} {config.PAIR_LABEL} +/-{(current_best['band'] - 1) * 100:.0f}% "
               f"{current_best['net_day_pct']:.3f}%/day") if current_best else 'unscorable'
    best_txt = f"{best['dex']} {best['pair']} +/-{best['band_pct']:.0f}% {best['net_day_pct']:.3f}%/day"
    if gain is not None and gain < config.MIGRATE_MIN_GAIN:
        notify('board_checked', held=cur_txt, best=best_txt,
               verdict=f'{gain * 100:.0f}% gain is under the {config.MIGRATE_MIN_GAIN * 100:.0f}% threshold')
        return False
    if best['dex'] not in config.EXECUTE_DEXES or best['dex'] not in SIGNERS:
        notify('MIGRATE_RECOMMENDED', held=cur_txt, best=best_txt,
               gain_pct=None if gain is None else round(gain * 100),
               pool=best['address'], dex=best['dex'],
               reason=f'no signer armed for {best["dex"]}; the bot stays where it is',
               command=f'python3 db.py repoint {config.PROFILE} {best["dex"]} {best["address"]}')
        return False
    if busy_hour():
        o = db.season_outlook(db.season(), hour=utc_hour())
        notify('move_deferred', kind='pool move', held=cur_txt, best=best_txt,
               reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                      f"waiting for a quiet hour (trough {o['trough_hour_utc']:02d} UTC)")
        return False
    notify('MIGRATE', held=cur_txt, best=best_txt,
           gain_pct=None if gain is None else round(gain * 100),
           pool=best['address'], dex=best['dex'])
    db.event('MIGRATE', f'{config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} '
                        f'({cur_txt} -> {best_txt})')
    rebalance(state, status, 'moved to a better pool', target=best)
    return True


def operator_target(spec):
    """Turn "<dex> <pool>" from the MIGRATE file into a board-shaped target,
    or explain why not."""
    if len(spec) != 2 or not guards.is_address(spec[1]):
        notify('migrate_refused', reason=f'MIGRATE must contain "<dex> <pool>", got {spec!r}')
        return None
    dex, pool = spec
    if dex not in SIGNERS or dex not in config.EXECUTE_DEXES:
        notify('migrate_refused', reason=f'{dex} has no armed signer (execute_dexes)')
        return None
    if dex == 'jupiter':
        notify('migrate_refused', reason='jupiter is a swap route, not a pool')
        return None
    try:
        rec = dexes.pool(dex, pool)
    except Exception as e:
        rec = None
        err = str(e)[:120]
    if not rec:
        notify('migrate_refused', reason=f'{dex} does not know a pool at {pool}')
        return None
    if dex == 'orca' and rec.get('adaptive_fee'):
        notify('migrate_refused', reason='adaptive-fee Orca pool: the signer cannot open it')
        return None
    return {'dex': dex, 'address': pool, 'pair': rec['pair'], 'token_a': rec['token_a'],
            'token_b': rec['token_b'], 'net_day_pct': None, 'band_pct': None}


def repoint(target):
    """Point the profile at the target pool and reload the configuration, so
    everything the loop reads from `config` is the new pool's."""
    guards.migration_target(target, execute_dexes=config.EXECUTE_DEXES, signers=SIGNERS,
                            known=dexes.KNOWN)
    db.repoint(config.PROFILE, target['dex'], target['address'], target['pair'],
               target['token_a']['symbol'], target['token_b']['symbol'])
    config.reload()
    assert config.DEX == target['dex'] and config.POOL == target['address'], \
        'config did not reload to the target pool'


def reopen(state, reason, band=None, recovering=False):
    """Open a fresh position at the best band, or at `band` when calm mode
    asks for the tight one, sized to what the wallet holds (after a swap to
    50/50 when `rebalance_swap` is on and the wallet is lopsided)."""
    pool = config.POOL
    best = best_band_for(pool)
    if not best:
        notify('idle', reason='could not price the pool; opening nothing')
        return False
    bal = wallet(pool)
    if 'balanceA' not in bal:
        notify('idle', reason='could not read the wallet; opening nothing')
        return False
    if recovering and band:
        if not config.CALM_ENABLED:
            band = None
        else:
            price = bal['price']
            v = calm_view(state, {'price': price, 'whirlpool': pool,
                                 'lowerPrice': price / band, 'upperPrice': price * band})
            if not v or v['bar_age_s'] > 900 or v.get('p_touch_fresh') is None:
                notify('idle', reason='CALM recovery waits for fresh five-minute data')
                return False
            if not v['calm'] or v['p_touch_fresh'] >= config.CALM_THRESHOLD:
                band = None
    k = band or best['band']
    bal = balance_wallet(state, bal, best.get('record'))
    if bal is None:
        return False
    # A tight band is centred on the LIVE price: the ladder's price can be
    # minutes old, and on a +/-1% band that is a large part of the width.
    price = bal['price'] if band else best['price']
    lower, upper = price / k, price * k
    cap_a, cap_b = deposit_caps(bal)
    if cap_a * price + cap_b < config.CAPITAL_USD * 0.1 / (bal.get('quoteUsd') or 1):
        notify('idle', reason=f'wallet holds too little {bal["tokenA"]} and '
                              f'{bal["tokenB"]} to open; nothing to do',
               balanceA=bal['balanceA'], balanceB=bal['balanceB'])
        return False
    # Hard invariants before anything is signed. Each has been the shape of
    # a real loss somewhere: a band that does not contain the LIVE price, a
    # model price stale against the chain, a cap above the capital, a DEX
    # without an armed signer. A refusal is counted like a failed open.
    try:
        guards.open_request(pool=pool, dex=config.DEX, price=bal['price'], model_price=price,
                            lower=lower, upper=upper, cap_a=cap_a, cap_b=cap_b,
                            capital_usd=config.CAPITAL_USD, max_usd=config.MAX_USD,
                            quote_usd=bal.get('quoteUsd') or 1.0,
                            execute_dexes=config.EXECUTE_DEXES, signers=SIGNERS)
    except guards.Refused as e:
        state['failures'] += 1; save(state)
        db.event('open_refused', str(e))
        notify('open_refused', reason=str(e), failures=state['failures'])
        if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
            halt(f'{state["failures"]} consecutive failures')
        return False
    out, err = chain('open', pool, f'{lower:.6f}', f'{upper:.6f}',
                     f'{cap_a:.9f}', f'{cap_b:.9f}', '--execute')
    if err:
        # The open may still have landed. Ask the chain before believing this.
        time.sleep(15)
        status, _ = read_status()
        if status and status.get('positionMint'):
            notify('open_recovered', detail='open reported an error but the '
                                            'position exists on chain',
                   positionMint=status['positionMint'])
            out = {'positionMint': status['positionMint'], 'signature': None}
        else:
            state['failures'] += 1; save(state)
            db.event('open_failed', err)
            notify('open_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                halt(f'{state["failures"]} consecutive failures')
            return False
    state['failures'] = 0; save(state)
    mint = (out or {}).get('positionMint')
    # What actually went in, not the configured capital: a wallet short of one
    # side opens a smaller position, and the ledger must say so. The chain's
    # own mark of the new position is the truth; the signer's estimate is the
    # fallback if that read fails.
    deposit_usd = None
    if mint:
        st, _ = read_status(mint)
        # The same mark the snapshots use: tokens plus the rent, so the
        # deposit and every later mark are measured the same way.
        deposit_usd = position_usd(st) if st else None
    if deposit_usd is None:
        deposit_usd = (out or {}).get('depositUsd')
    if deposit_usd is None:
        deposit_usd = min(config.CAPITAL_USD, cap_a * price + cap_b)
    db.open_position(mint, pool, config.PAIR_LABEL, lower, upper,
                     (k - 1) * 100, (out or {}).get('signature'),
                     deposit_usd, reason, config_name=config.PROFILE, dex=config.DEX)
    state.pop('pending_reopen', None)
    save(state)
    notify_book('OPEN', pair=config.PAIR_LABEL, pool=pool, dex=config.DEX,
           opened_band=f'+/-{(k - 1) * 100:.0f}%', calm_band=bool(band),
           lower=round(lower, 4), upper=round(upper, 4),
           deposit_usd=round(deposit_usd, 2),
           deposit_a=(out or {}).get('depositEstA'), deposit_b=(out or {}).get('depositEstB'),
           cap_a=f'{cap_a:.6f} {bal["tokenA"]}', cap_b=f'{cap_b:.6f} {bal["tokenB"]}',
           expected_net_day_pct=None if band else round(best['net_day_pct'], 3),
           modelled_rebalances_per_day=None if band else round(best['rebal_per_day'], 2),
           model_scope='CALM policy not modelled by hourly ladder' if band else 'hourly ladder',
           signature=(out or {}).get('signature'), reason=reason)
    return True


def rebalance(state, status, reason, target=None, band=None, calm_move=False):
    """Harvest, close, and reopen: on the same pool, or on `target` (a board
    row) after repointing the profile. The close runs on the DEX the position
    is on, whatever the profile says by then.

    A calm move (narrow, re-centre, widen, or the exit of a tight band) has
    its own gap (`calm_min_gap_seconds`) and its own budget, counted apart
    from the normal ones; both together sit under one hard ceiling,
    max_rebalances_per_day + calm_max_moves_per_day, past which the bot
    halts as before."""
    now = time.time()
    recent = [t for t in state['rebalance_times'] if now - t < 86400]
    calm_recent = [t for t in state.get('calm_times', []) if now - t < 86400]
    last_any = max([state['last_rebalance']] + calm_recent)
    gap = config.CALM_MIN_GAP if calm_move else config.MIN_REBALANCE_GAP
    last = last_any if calm_move else state['last_rebalance']
    if now - last < gap:
        notify('rebalance_deferred', seconds_remaining=int(gap - (now - last)),
               kind='calm' if calm_move else 'normal')
        return
    if len(recent) + len(calm_recent) >= config.MAX_REBALANCES_PER_DAY + config.CALM_MAX_MOVES:
        halt(f'{len(recent) + len(calm_recent)} rebalances in 24h, at the hard ceiling')
        return
    if not calm_move and len(recent) >= config.MAX_REBALANCES_PER_DAY:
        halt(f'{len(recent)} rebalances in 24h, at the ceiling')
        return
    mint = status['positionMint']

    accrued_a = status.get('feesAccruedA', 0.0)
    accrued_b = status.get('feesAccruedB', 0.0)
    accrued_usd = status.get('feesAccrued_USD', 0.0)
    out, err = chain('harvest', mint, '--execute')
    state['last_harvest'] = now
    if out and out.get('signature') and not err:
        db.record_harvest(mint, accrued_a, accrued_b, accrued_usd,
                              out['signature'])
        # The fees just moved from the position to the wallet. Record the
        # position's counter at zero now, or the book double-counts them as
        # both realised and unrealised until the next poll.
        db.snapshot(mint, status['price'], status.get('inRange'),
                    status.get('liquidity'), 0.0, 0.0, 0.0,
                    wallet(status['whirlpool']).get('walletUsd'),
                    position_usd(status))
        notify('HARVEST', collected_usd=round(accrued_usd, 4),
               signature=out['signature'])
    else:
        # Never let a harvest block the close. An out-of-range position must
        # move, and the close collects any fees the harvest could not: the
        # Meteora and Byreal signers answer "nothing to claim" with no
        # signature, and treating that as a failure would leave the band out
        # of range until the bot halted.
        notify('harvest_skipped', reason=err or (out or {}).get('note') or 'no signature returned')

    if calm_move and target is None:
        state['pending_reopen'] = {'mint': mint, 'pool': config.POOL, 'dex': config.DEX,
                                   'band': band, 'reason': reason, 'started_at': now,
                                   'withdraw_usd': position_usd(status), 'closed': False}
        save(state)

    out, err = chain('close', mint, '--execute')
    if err:
        # A close that reports failure may have landed. This exact false
        # negative left a position closed and the capital idle in production.
        time.sleep(15)
        check, _ = read_status()
        if check is not None and not check.get('positionMint'):
            db.event('close_recovered', err)
            notify('close_recovered',
                   detail='close reported an error but the position is gone; '
                          'treating it as closed', error=err)
            out = {'signature': None}
        else:
            # The position is still open: nothing to resume later. A stale
            # intent would replay after an unrelated failure, on a pool the
            # bot may have left by then.
            state.pop('pending_reopen', None)
            state['failures'] += 1; save(state)
            db.event('close_failed', err)
            notify('close_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                halt(f'{state["failures"]} consecutive failures')
            return
    # What the close returns: the mark taken just before it, tokens plus the
    # rent the chain refunds. The best figure available without a second
    # read, and what per-pool P&L is measured against.
    db.close_position(mint, (out or {}).get('signature'), position_usd(status))
    notify_book('CLOSE', positionMint=mint,
                signature=(out or {}).get('signature'), reason=reason)

    if calm_move:
        state['calm_times'] = calm_recent + [now]
        if state.get('pending_reopen'):
            state['pending_reopen']['closed'] = True
    else:
        state['last_rebalance'] = now
        state['rebalance_times'] = recent + [now]
    save(state)
    if target:
        repoint(target)
        notify('REPOINTED', dex=config.DEX, pool=config.POOL, pair=config.PAIR_LABEL)
    reopen(state, reason, band=band)


def main():
    if HALT.exists():
        print(f'HALT present: {HALT.read_text().strip()}')
        return 2
    config.require_wallet()
    state = load()
    notify_book('startup', mode='ARMED — signs its own rebalances',
                **config.summary())
    scanner.Scanner(notify).start()

    while True:
        if HALT.exists():
            notify('halted', reason=HALT.read_text().strip())
            return 2

        status, err = read_status()

        if status is None:
            state['read_failures'] += 1; save(state)
            notify('status_unreadable', reason=err,
                   consecutive=state['read_failures'],
                   action='holding; opening nothing')
            if state['read_failures'] >= config.MAX_UNREADABLE_POLLS:
                halt(f'{state["read_failures"]} unreadable polls')
            time.sleep(config.POLL_SECONDS)
            continue
        state['read_failures'] = 0; save(state)

        if not status.get('positionMint'):
            notify('no_position', detail='chain reports no open position')
            if resume_reopen(state):
                time.sleep(config.CALM_POLL_SECONDS)
                continue
            # Nothing is held, so nothing is closed: choose the pool first.
            if not config.POOL_PINNED:
                cur = best_band_for(config.POOL)
                run, best, gain, why = board_pick(cur, config.POOL)
                if best and best['dex'] in config.EXECUTE_DEXES and best['dex'] in SIGNERS \
                        and (gain is None or gain >= config.MIGRATE_MIN_GAIN):
                    notify('MIGRATE', held=f'{config.DEX} {config.PAIR_LABEL} (no position)',
                           best=f"{best['dex']} {best['pair']} {best['net_day_pct']:.3f}%/day",
                           gain_pct=None if gain is None else round(gain * 100),
                           pool=best['address'], dex=best['dex'])
                    db.event('MIGRATE', f'{config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} (no position)')
                    repoint(best)
            reopen(state, 'no position held')
            time.sleep(config.POLL_SECONDS)
            continue

        price = status['price']
        wusd = wallet(status['whirlpool']).get('walletUsd')
        fc = forecast_for(status)
        cv = calm_view(state, status)
        tight = bool(cv and cv.get('tight_held'))
        if tight and fc:
            # The hourly six-hour rule says nothing about a +/-1% band; the
            # five-minute rule in calm.py is in charge of it.
            fc = dict(fc, act=False, suspended='tight band: the five-minute calm rule is in charge')
        db.snapshot(status['positionMint'], price, status.get('inRange'),
                        status.get('liquidity'),
                        status.get('feesAccruedA', 0.0),
                        status.get('feesAccruedB', 0.0),
                        status.get('feesAccrued_USD', 0.0),
                        wusd, position_usd(status), forecast=fc)

        if not status.get('inRange'):
            side = 'above' if price > status['upperPrice'] else 'below'
            k = calm_reopen_band(cv, state) if tight else None
            notify('OUT_OF_BAND', side=side, price=price,
                   lower=status['lowerPrice'], upper=status['upperPrice'],
                   action=('harvest, close, reopen tight' if k else 'harvest, close, re-optimise, reopen'),
                   forecast=fc, calm=cv)
            rebalance(state, status, f'price went {side}', band=k, calm_move=tight)
            time.sleep(config.POLL_SECONDS)
            continue

        # Calm mode: narrow when the market goes cold, re-centre the tight
        # band before it is touched, widen when the calm ends.
        act = calm.decide(cv, enabled=config.CALM_ENABLED,
                          budget_left=(cv or {}).get('budget_left', 0))
        if act:
            what = {'narrow': ('CALM_NARROW', config.CALM_BAND, 'calm: tight band'),
                    'recentre': ('CALM_RECENTRE', config.CALM_BAND, 'calm: tight band re-centred before a touch'),
                    'widen': ('CALM_WIDEN', None, 'calm over: back to the ladder band')}[act]
            notify_book(what[0], price=price, lower=status['lowerPrice'], upper=status['upperPrice'],
                        calm=cv, forecast=fc)
            db.event(what[0], f"sigma {cv['sigma_5m_pct']}% cut {cv['cut_pct']}% "
                              f"p_touch {cv.get('p_touch')} fresh {cv.get('p_touch_fresh')}")
            rebalance(state, status, what[2], band=what[1], calm_move=True)
            time.sleep(config.CALM_POLL_SECONDS if what[1] else config.POLL_SECONDS)
            continue

        # Still inside, but likely not for long: re-centre now, at this price,
        # rather than at whatever price the exit happens to land on. Waits
        # for a quiet hour only while the probability is below the ceiling
        # (90%): past that the exit is imminent and the hour does not matter.
        if fc and fc.get('act') and config.PROACTIVE_THRESHOLD:
            p_now = fc.get('p_exit_horizon')
            if busy_hour() and (p_now or 0) < 0.9:
                o = db.season_outlook(db.season(), hour=utc_hour())
                notify('recentre_deferred', p_exit=p_now, horizon_hours=config.PROACTIVE_HORIZON,
                       reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                              f"waiting for a quiet hour unless the probability reaches 90%",
                       forecast=fc)
            else:
                notify_book('PROACTIVE', price=price, lower=status['lowerPrice'],
                            upper=status['upperPrice'], p_exit=p_now,
                            horizon_hours=config.PROACTIVE_HORIZON,
                            threshold=config.PROACTIVE_THRESHOLD, forecast=fc,
                            action='harvest, close, re-centre on the current price')
                db.event('PROACTIVE', f"P(exit within {config.PROACTIVE_HORIZON}h) = {p_now} "
                                      f"at {price}, band {status['lowerPrice']}..{status['upperPrice']}")
                rebalance(state, status, f'P(exit within {config.PROACTIVE_HORIZON}h) '
                                         f'{(p_now or 0) * 100:.0f}% >= {config.PROACTIVE_THRESHOLD * 100:.0f}%')
                time.sleep(config.POLL_SECONDS)
                continue

        if harvest_due(state, status):
            dividend(state, status)
            status, err = read_status()
            if not status or not status.get('positionMint'):
                time.sleep(config.POLL_SECONDS)
                continue

        if MIGRATE.exists():
            # "<dex> <pool>". Consumed before it runs. The same gates as an
            # automatic move: a signer must exist and be armed for the DEX.
            spec = MIGRATE.read_text().split()
            MIGRATE.unlink()
            target = operator_target(spec)
            if target:
                db.event('MIGRATE_REQUESTED', f'operator: {target["dex"]} {target["address"]}')
                notify('MIGRATE', held=f'{config.DEX} {config.PAIR_LABEL}',
                       best=f"{target['dex']} {target['pair']} (operator)", gain_pct=None,
                       pool=target['address'], dex=target['dex'])
                rebalance(state, status, 'operator requested move', target=target)
                time.sleep(config.POLL_SECONDS)
                continue

        if REBALANCE.exists():
            # Consumed before it runs, so a failure cannot loop on the trigger.
            REBALANCE.unlink()
            db.event('REBALANCE_REQUESTED', 'operator touched REBALANCE')
            notify('REBALANCE_REQUESTED', price=price,
                   action='harvest, close, re-optimise, reopen')
            rebalance(state, status, 'operator requested')
            time.sleep(config.POLL_SECONDS)
            continue

        forced = REOPT.exists()
        if forced:
            REOPT.unlink()
            db.event('REOPT_REQUESTED', 'operator touched REOPT')
            notify('REOPT_REQUESTED', action='board and band review now')
        if (forced or time.time() - state.get('last_reopt', 0) > config.REOPT_INTERVAL) and tight:
            notify('reopt_checked', held='tight band', best='-',
                   verdict='calm mode holds the tight band; the review waits until it widens')
            state['last_reopt'] = time.time() - config.REOPT_INTERVAL + 1800; save(state)
        elif forced or time.time() - state.get('last_reopt', 0) > config.REOPT_INTERVAL:
            state['last_reopt'] = time.time(); save(state)
            best = best_band_for(status['whirlpool'])
            # The board first: a better pool elsewhere outranks a better band
            # here, and a move re-optimises the band on arrival anyway.
            if consider_migration(state, status, best):
                time.sleep(config.POLL_SECONDS)
                continue
            if best:
                # Score the band actually held against the best available, both
                # under the same model, so the comparison means something.
                # The held band's half-width is a property of the band, not of
                # where the price happens to sit inside it. Measuring it against
                # the current price returns a number that drifts the moment the
                # price moves off centre, misses the lookup below, and quietly
                # turns the whole re-optimiser into a no-op.
                held = (status['upperPrice'] / status['lowerPrice']) ** 0.5
                held_pct = round((held - 1) * 100)
                runs = {round((r['band'] - 1) * 100): r for r in best['all_runs']}
                cur_run = runs.get(held_pct) or nearest(runs, held_pct)
                if cur_run:
                    gain = ((best['net_day_pct'] - cur_run['net_day_pct'])
                            / max(abs(cur_run['net_day_pct']), 1e-9))
                    if gain >= config.REOPT_MIN_GAIN and busy_hour():
                        o = db.season_outlook(db.season(), hour=utc_hour())
                        notify('move_deferred', kind='reband',
                               held=f'+/-{held_pct}%', best=f'+/-{(best["band"] - 1) * 100:.0f}%',
                               reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                                      f"waiting for a quiet hour (trough {o['trough_hour_utc']:02d} UTC)")
                        # come back next poll cycle rather than in six hours
                        state['last_reopt'] = time.time() - config.REOPT_INTERVAL + 1800; save(state)
                    elif gain >= config.REOPT_MIN_GAIN:
                        notify('REBAND', improvement_pct=round(gain * 100),
                               old_band=f'+/-{held_pct}%',
                               new_band=f'+/-{(best["band"] - 1) * 100:.0f}%',
                               old_net_day=round(cur_run['net_day_pct'], 3),
                               new_net_day=round(best['net_day_pct'], 3))
                        db.event('REBAND', f'{held_pct}% -> '
                                           f'{(best["band"] - 1) * 100:.0f}%')
                        rebalance(state, status, 're-optimised band')
                        time.sleep(config.POLL_SECONDS)
                        continue
                    notify('reopt_checked',
                           held=f'+/-{held_pct}% {cur_run["net_day_pct"]:.3f}%/day',
                           best=f'+/-{(best["band"] - 1) * 100:.0f}% '
                                f'{best["net_day_pct"]:.3f}%/day',
                           verdict=f'{gain * 100:.0f}% gain is under the '
                                   f'{config.REOPT_MIN_GAIN * 100:.0f}% threshold')

        if time.time() - state.get('last_book', 0) >= config.POLL_SECONDS - 5:
            state['last_book'] = time.time()
            notify_book('in_band', price=price, lower=status['lowerPrice'],
                        upper=status['upperPrice'],
                        liquidity=status.get('liquidity'), forecast=fc, calm=cv)
        time.sleep(config.CALM_POLL_SECONDS if tight else config.POLL_SECONDS)


if __name__ == '__main__':
    sys.exit(main() or 0)
