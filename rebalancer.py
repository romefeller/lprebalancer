"""Rebalancer — a concentrated-liquidity rebalancer for Orca whirlpools.

Holds one position, watches the price against its band, and when the price
leaves, collects the fees, closes, re-optimises the band and opens again. Every
number it reports comes from the chain or from its own ledger, never from a
simulation of what it thinks happened.

Every parameter comes from the `rebalancer.config` table and every observation
goes back into Postgres, so the pool, the band ladder and the guards are data,
not code.

    read chain -> in band?  yes -> record a snapshot, wait
                            no  -> harvest, close, re-optimise, reopen

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

The trigger exists because the rebalance path is the one that runs unattended,
and a path that has only ever run at 3am has never been watched. Touch the file
while you are looking and the next poll runs harvest -> close -> reopen at the
best band, subject to the same gap and daily limits as an automatic one.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

import config
import db
import engine

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / 'runtime.json'
FEED = ROOT / 'events.jsonl'
HALT = ROOT / 'HALT'
REBALANCE = ROOT / 'REBALANCE'
CHAIN = str(ROOT / 'signer2.mjs')


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
    for needle, plain in (
            ('429', 'RPC rate limited'),
            ('Too Many Requests', 'RPC rate limited'),
            ('timeout', 'RPC timeout'),
            ('ECONNRESET', 'RPC connection reset'),
            ('blockhash', 'blockhash expired')):
        if needle.lower() in s.lower():
            return plain
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


def chain(*args, timeout=420):
    """Call the signer. Returns (parsed_json, tidy_error)."""
    env = dict(os.environ,
               WALLET_SECRET_PATH=config.WALLET,
               SOLANA_RPC_URL=config.RPC,
               LPBOT_MAX_USD=str(config.MAX_USD),
               LPBOT_SLIPPAGE_BPS=str(config.SLIPPAGE_BPS),
               LPBOT_GAS_RESERVE_SOL=str(config.GAS_RESERVE_SOL))
    try:
        r = subprocess.run(['node', CHAIN, *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, 'signer timed out'
    text = (r.stdout or '') + (r.stderr or '')
    i = text.find('{')
    if i < 0:
        return None, tidy(text)
    try:
        return json.loads(text[i:text.rindex('}') + 1]), None
    except Exception:
        return None, tidy(text)


def load():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {'last_rebalance': 0, 'rebalance_times': [], 'failures': 0,
            'read_failures': 0, 'last_reopt': 0}


def save(s):
    STATE.write_text(json.dumps(s, indent=1, default=str))


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
    """Mark of the position, from the amounts a close would return right now.

    The signer already values it in dollars when it knows the quote token's
    price. Falling back to quote units is only correct when the quote token is
    a dollar, which is why the signer's figure is preferred.
    """
    if status.get('positionUsd') is not None:
        return status['positionUsd']
    a, b = status.get('closeEstA'), status.get('closeEstB')
    if a is None or b is None:
        return None
    return (a * status['price'] + b) * (status.get('quoteUsd') or 1)


def best_band_for(pool):
    """Score every candidate band on this pool's own recent data.

    Each band is replayed from many origins, not once: the bot decides on the
    median net return per day across rolling windows, so a band that fitted
    the last six weeks by luck does not beat one that works from most starting
    points. The churn gate then discards bands that rebalance too often.
    """
    d = engine.curl(f'{engine.ORCA}/pools/{pool}')
    p = (d or {}).get('data') or d
    if not p or not p.get('tokenA'):
        return None
    out = engine.ladder(p, engine.candles(pool), config.BANDS,
                        config.CAPITAL_USD, config.SWAP_COST)
    if not out:
        return None
    rows, meta = out
    pick = dict(engine.choose(rows, config.MAX_REBALANCES_PER_DAY_MODELLED))
    pick['price'] = meta['price']
    pick['fee'] = meta['fee']
    pick['all_runs'] = rows
    return pick


def reopen(state, reason):
    """Open a fresh position at the best band, sized to what the wallet holds."""
    pool = config.POOL
    best = best_band_for(pool)
    if not best:
        notify('idle', reason='could not price the pool; opening nothing')
        return False
    price = best['price']
    lower, upper = price / best['band'], price * best['band']
    bal = wallet(pool)
    if 'balanceA' not in bal:
        notify('idle', reason='could not read the wallet; opening nothing')
        return False
    cap_a, cap_b = deposit_caps(bal)
    if cap_a * price + cap_b < config.CAPITAL_USD * 0.1 / (bal.get('quoteUsd') or 1):
        notify('idle', reason=f'wallet holds too little {bal["tokenA"]} and '
                              f'{bal["tokenB"]} to open; nothing to do',
               balanceA=bal['balanceA'], balanceB=bal['balanceB'])
        return False
    out, err = chain('open', pool, f'{lower:.6f}', f'{upper:.6f}',
                     f'{cap_a:.9f}', f'{cap_b:.9f}', '--execute')
    if err and not out:
        # The open may still have landed. Ask the chain before believing this.
        time.sleep(15)
        status, _ = chain('status')
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
        st, _ = chain('status', mint)
        deposit_usd = (st or {}).get('positionUsd')
    if deposit_usd is None:
        deposit_usd = (out or {}).get('depositUsd')
    if deposit_usd is None:
        deposit_usd = min(config.CAPITAL_USD, cap_a * price + cap_b)
    db.open_position(mint, pool, config.PAIR_LABEL, lower, upper,
                     (best['band'] - 1) * 100, (out or {}).get('signature'),
                     deposit_usd, reason, config_name=config.PROFILE)
    notify_book('OPEN', pair=config.PAIR_LABEL, pool=pool,
           band=f'+/-{(best["band"] - 1) * 100:.0f}%',
           lower=round(lower, 4), upper=round(upper, 4),
           deposit_usd=round(deposit_usd, 2),
           deposit_a=(out or {}).get('depositEstA'), deposit_b=(out or {}).get('depositEstB'),
           cap_a=f'{cap_a:.6f} {bal["tokenA"]}', cap_b=f'{cap_b:.6f} {bal["tokenB"]}',
           expected_net_day_pct=round(best['net_day_pct'], 3),
           modelled_rebalances_per_day=round(best['rebal_per_day'], 2),
           signature=(out or {}).get('signature'), reason=reason)
    return True


def rebalance(state, status, reason):
    now = time.time()
    if now - state['last_rebalance'] < config.MIN_REBALANCE_GAP:
        notify('rebalance_deferred',
               seconds_remaining=int(config.MIN_REBALANCE_GAP -
                                     (now - state['last_rebalance'])))
        return
    recent = [t for t in state['rebalance_times'] if now - t < 86400]
    if len(recent) >= config.MAX_REBALANCES_PER_DAY:
        halt(f'{len(recent)} rebalances in 24h, at the ceiling')
        return
    mint = status['positionMint']

    accrued_a = status.get('feesAccruedA', 0.0)
    accrued_b = status.get('feesAccruedB', 0.0)
    accrued_usd = status.get('feesAccrued_USD', 0.0)
    out, err = chain('harvest', mint, '--execute')
    if out and out.get('signature'):
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
        notify('harvest_skipped', reason=err or 'no signature returned')

    out, err = chain('close', mint, '--execute')
    if err and not out:
        # A close that reports failure may have landed. This exact false
        # negative left a position closed and the capital idle in production.
        time.sleep(15)
        check, _ = chain('status')
        if check is not None and not check.get('positionMint'):
            db.event('close_recovered', err)
            notify('close_recovered',
                   detail='close reported an error but the position is gone; '
                          'treating it as closed', error=err)
            out = {'signature': None}
        else:
            state['failures'] += 1; save(state)
            db.event('close_failed', err)
            notify('close_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                halt(f'{state["failures"]} consecutive failures')
            return
    db.close_position(mint, (out or {}).get('signature'), None)
    notify_book('CLOSE', positionMint=mint,
                signature=(out or {}).get('signature'), reason=reason)

    state['last_rebalance'] = now
    state['rebalance_times'] = recent + [now]
    save(state)
    reopen(state, reason)


def main():
    if HALT.exists():
        print(f'HALT present: {HALT.read_text().strip()}')
        return 2
    config.require_wallet()
    state = load()
    notify_book('startup', mode='ARMED — signs its own rebalances',
                **config.summary())

    while True:
        if HALT.exists():
            notify('halted', reason=HALT.read_text().strip())
            return 2

        status, err = chain('status')

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
            reopen(state, 'no position held')
            time.sleep(config.POLL_SECONDS)
            continue

        price = status['price']
        wusd = wallet(status['whirlpool']).get('walletUsd')
        db.snapshot(status['positionMint'], price, status.get('inRange'),
                        status.get('liquidity'),
                        status.get('feesAccruedA', 0.0),
                        status.get('feesAccruedB', 0.0),
                        status.get('feesAccrued_USD', 0.0),
                        wusd, position_usd(status))

        if not status.get('inRange'):
            side = 'above' if price > status['upperPrice'] else 'below'
            notify('OUT_OF_BAND', side=side, price=price,
                   lower=status['lowerPrice'], upper=status['upperPrice'],
                   action='harvest, close, re-optimise, reopen')
            rebalance(state, status, f'price went {side}')
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

        if time.time() - state.get('last_reopt', 0) > config.REOPT_INTERVAL:
            state['last_reopt'] = time.time(); save(state)
            best = best_band_for(status['whirlpool'])
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
                    if gain >= config.REOPT_MIN_GAIN:
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

        notify_book('in_band', price=price, lower=status['lowerPrice'],
                    upper=status['upperPrice'],
                    liquidity=status.get('liquidity'))
        time.sleep(config.POLL_SECONDS)


if __name__ == '__main__':
    sys.exit(main() or 0)
