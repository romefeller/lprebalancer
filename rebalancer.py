"""Aperture — a concentrated-liquidity rebalancer for Orca whirlpools.

Holds one position, watches the price against its band, and when the price
leaves, collects the fees, closes, re-optimises the band and opens again. Every
number it reports comes from the chain or from its own ledger, never from a
simulation of what it thinks happened.

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

Stop it at any time with:  touch HALT
"""
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timezone

import config
import engine
import ledger

ROOT = pathlib.Path(__file__).resolve().parent
STATE = ROOT / 'runtime.json'
FEED = ROOT / 'kmnbot_feed.jsonl'
HALT = ROOT / 'HALT'
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


def chain(*args, timeout=420):
    """Call the signer. Returns (parsed_json, tidy_error)."""
    env = dict(os.environ,
               WALLET_SECRET_PATH=config.WALLET,
               SOLANA_RPC_URL=config.RPC)
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
    ledger.event('BREAKER', reason)
    notify('BREAKER', reason=reason, action='HALT written; will not restart')


def wallet_usd(price):
    out, _ = chain('balance')
    sol = (out or {}).get('sol', 0.0)
    return sol * price, sol


def position_usd(status):
    """Rough mark of the position, from the amounts a close would return."""
    a = status.get('closeEstA_SOL')
    b = status.get('closeEstB_USDC')
    if a is None or b is None:
        return None
    return a * status['price'] + b


def best_band_for(pool):
    """Simulate every candidate band on this pool's own recent data."""
    d = engine.curl(f'{engine.ORCA}/pools/{pool}')
    p = (d or {}).get('data') or d
    if not p:
        return None
    pool_l = engine.active_liquidity(p)
    tvl = float(p.get('tvlUsdc') or 0)
    quote_usd = engine.pool_quote_price(pool)
    c_pool = engine.pool_concentration(pool_l, tvl, float(p['price']), quote_usd or 0)
    if not c_pool or not (1.0 <= c_pool <= 500.0):
        return None
    c = engine.candles(pool)
    if not c:
        return None
    ts, px, vol = c
    fee = int(p.get('feeRate') or 0) / 1e6
    ctx = {'tvl_usd': tvl, 'c_pool': c_pool}
    runs = [engine.simulate(k, ts, px, vol, ctx, fee, config.CAPITAL_USD)
            for k in config.BANDS]
    # Reject bands that churn. A high simulated yield bought with a rebalance a
    # day is a high yield bought with a daily chance of a failed transaction.
    calm = [r for r in runs
            if r['rebal_per_day'] <= config.MAX_REBALANCES_PER_DAY_MODELLED]
    pick = max(calm or runs, key=lambda r: r['net_day_pct'])
    pick['price'] = float(p['price'])
    pick['fee'] = fee
    pick['all_runs'] = runs
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
    _, sol = wallet_usd(price)
    max_sol = max(sol - config.SOL_RESERVE, 0) * 0.55
    max_usdc = config.CAPITAL_USD * 0.55
    out, err = chain('open', pool, f'{lower:.6f}', f'{upper:.6f}',
                     f'{max_sol:.6f}', f'{max_usdc:.2f}', '--execute')
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
            ledger.event('open_failed', err)
            notify('open_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                halt(f'{state["failures"]} consecutive failures')
            return False
    state['failures'] = 0; save(state)
    mint = (out or {}).get('positionMint')
    ledger.open_position(mint, pool, config.PAIR_LABEL, lower, upper,
                         (best['band'] - 1) * 100, (out or {}).get('signature'),
                         config.CAPITAL_USD, reason)
    notify('OPEN', pair=config.PAIR_LABEL, pool=pool,
           band=f'+/-{(best["band"] - 1) * 100:.0f}%',
           lower=round(lower, 4), upper=round(upper, 4),
           expected_net_day_pct=round(best['net_day_pct'], 3),
           modelled_rebalances_per_day=round(best['rebal_per_day'], 2),
           signature=(out or {}).get('signature'), reason=reason,
           **ledger.stats())
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

    accrued_a = status.get('feesAccruedA_SOL', 0.0)
    accrued_b = status.get('feesAccruedB_USDC', 0.0)
    accrued_usd = status.get('feesAccrued_USD', 0.0)
    out, err = chain('harvest', mint, '--execute')
    if out and out.get('signature'):
        ledger.record_harvest(mint, accrued_a, accrued_b, accrued_usd,
                              out['signature'])
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
            notify('close_recovered',
                   detail='close reported an error but the position is gone; '
                          'treating it as closed')
            out = {'signature': None}
        else:
            state['failures'] += 1; save(state)
            ledger.event('close_failed', err)
            notify('close_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                halt(f'{state["failures"]} consecutive failures')
            return
    ledger.close_position(mint, (out or {}).get('signature'), None)
    notify('CLOSE', positionMint=mint, signature=(out or {}).get('signature'),
           reason=reason, **ledger.stats())

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
    notify('startup', mode='ARMED — signs its own rebalances',
           **config.summary(), **ledger.stats())

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
        wusd, _ = wallet_usd(price)
        ledger.snapshot(status['positionMint'], price, status.get('inRange'),
                        status.get('liquidity'), status.get('feesAccrued_USD', 0.0),
                        wusd, position_usd(status))

        if not status.get('inRange'):
            side = 'above' if price > status['upperPrice'] else 'below'
            notify('OUT_OF_BAND', side=side, price=price,
                   lower=status['lowerPrice'], upper=status['upperPrice'],
                   action='harvest, close, re-optimise, reopen')
            rebalance(state, status, f'price went {side}')
            time.sleep(config.POLL_SECONDS)
            continue

        if time.time() - state.get('last_reopt', 0) > config.REOPT_INTERVAL:
            state['last_reopt'] = time.time(); save(state)
            best = best_band_for(status['whirlpool'])
            if best:
                # Score the band actually held against the best available, both
                # under the same model, so the comparison means something.
                held_pct = round((status['upperPrice'] / price - 1) * 100)
                runs = {round((r['band'] - 1) * 100): r for r in best['all_runs']}
                cur_run = runs.get(held_pct)
                if cur_run:
                    gain = ((best['net_day_pct'] - cur_run['net_day_pct'])
                            / max(abs(cur_run['net_day_pct']), 1e-9))
                    if gain >= config.REOPT_MIN_GAIN:
                        notify('REBAND', improvement_pct=round(gain * 100),
                               old_band=f'+/-{held_pct}%',
                               new_band=f'+/-{(best["band"] - 1) * 100:.0f}%',
                               old_net_day=round(cur_run['net_day_pct'], 3),
                               new_net_day=round(best['net_day_pct'], 3))
                        ledger.event('REBAND', f'{held_pct}% -> '
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

        notify('in_band', price=price, lower=status['lowerPrice'],
               upper=status['upperPrice'], liquidity=status.get('liquidity'),
               **ledger.stats())
        time.sleep(config.POLL_SECONDS)


if __name__ == '__main__':
    sys.exit(main() or 0)
