"""The poll loop of one profile."""

import time

import config
import db
import scanner
from lp import (board, books, capital, harvest, housekeeping, moves, paths, pauses, polls, regime, signers,
                swaps, tape, tuning)

def profile_enabled():
    """Whether this profile is enabled now (config.enabled, read every poll:
    an operator disables a running profile with an UPDATE). A pre-020
    profile (no wallet) is always. A read that fails changes nothing."""
    if not config.WALLET_ID:
        return True
    try:
        with db.cursor() as cur:
            cur.execute('select enabled from config where name = %s', (config.PROFILE,))
            r = cur.fetchone()
        return bool(r and r['enabled'])
    except Exception:
        return True


def disabled_hold(state, status):
    """A disabled profile with a position: it holds it (no swap, no open, no
    move), its claim and mints still its own, and closes it only when the
    operator asks (touch run/<profile>/CLOSE)."""
    if paths.CLOSE.exists():
        paths.CLOSE.unlink()                    # consumed before it runs, like the other triggers
        db.event('CLOSE_REQUESTED', 'operator touched CLOSE on a disabled profile')
        moves.rebalance(state, status, 'operator closed a disabled profile', close_only=True)
        return
    if not state.get('disabled_told'):
        state['disabled_told'] = time.time(); paths.save(state)
        books.notify('disabled', reason='profile disabled: holding its position; touch CLOSE to close it',
                     positionMint=status['positionMint'])


DORMANT_POLL_S = 300              # a dormant profile reads its wallet at most this often


def dormant(state, bal):
    """Whether this profile, which holds no position, stays dormant: its
    sleeve could not deploy min_deploy_usd. A dormant profile attempts no
    open and fetches no tape; the feed hears 'dormant' once on entry and
    'deposit_seen' once on exit, never a line per poll. A wallet that cannot
    be read, or a quote with no price, changes nothing: a dormant profile
    stays dormant, an active one goes on as before. A reopen intent left by
    a close is never dormant: its funds are in the wallet."""
    was = bool(state.get('dormant'))
    if state.get('pending_reopen'):
        return False
    if 'balanceA' not in bal:
        return was
    usd = capital.deployable_usd(bal)
    if usd is None:
        return was
    if usd < config.MIN_DEPLOY_USD:
        if not was:
            state['dormant'] = True; paths.save(state)
            books.notify('dormant', deployable_usd=round(usd, 4), min_deploy_usd=config.MIN_DEPLOY_USD,
                         poll_seconds=max(DORMANT_POLL_S, config.POLL_SECONDS),
                         reason='no position and too little to deploy: no open until a deposit')
            db.event('dormant', f'${usd:.2f} deployable, under ${config.MIN_DEPLOY_USD:.2f}')
        return True
    if was:
        state['dormant'] = False; paths.save(state)
        books.notify('deposit_seen', usd=round(usd, 2), deployable_usd=round(usd, 4),
                     reason='waking: swap to 50/50, then open')
        db.event('deposit_seen', f'${usd:.2f} deployable: leaving dormant')
    return False


def main():
    if paths.halted():
        print(f'HALT present: {paths.halted()}')
        return 2
    config.require_wallet()
    signers.probe_rpc()
    state = paths.load()
    books.notify_book('startup', mode='ARMED — signs its own rebalances',
                      **config.summary())
    # The board is wallet-wide work for the residual owner, and only for a
    # profile that may move pools.
    if signers.housekeeper('scanner') and not config.POOL_PINNED:
        scanner.Scanner(books.notify, active=lambda: not state.get('dormant')).start()

    while True:
        why = paths.halted()
        if why:
            books.notify('halted', reason=why)
            return 2

        status, err = signers.read_status()

        if status is None and state.get('dormant'):
            # A dormant profile holds nothing and attempts nothing: a flaky
            # read is no news and no reason to halt. It waits for the next one.
            print(f'status unreadable while dormant: {err}', flush=True)
            time.sleep(max(DORMANT_POLL_S, config.POLL_SECONDS))
            continue
        if status is None:
            state['read_failures'] += 1; paths.save(state)
            books.notify('status_unreadable', reason=err,
                         consecutive=state['read_failures'],
                         action='holding; opening nothing')
            if state['read_failures'] >= config.MAX_UNREADABLE_POLLS:
                books.halt(f'{state["read_failures"]} unreadable polls')
            time.sleep(config.POLL_SECONDS)
            continue
        state['read_failures'] = 0; paths.save(state)
        if not profile_enabled():
            if not status.get('positionMint'):
                books.notify('disabled', reason='profile disabled and holds no position: the process stops')
                return 0
            disabled_hold(state, status)
            time.sleep(config.POLL_SECONDS)
            continue
        state.pop('disabled_told', None)
        books.portfolio_report(state)
        capital.wrap_native(state)

        if not status.get('positionMint'):
            if paths.MIGRATE.exists():
                # An operator move with nothing held (the swing's switch when
                # an open failed): no close, the pool changes and what the
                # old pair left behind is sold before the dormant test.
                spec = paths.MIGRATE.read_text().split()
                paths.MIGRATE.unlink()
                target = board.operator_target(spec)
                if target:
                    db.event('MIGRATE_REQUESTED', f'operator: {target["dex"]} {target["address"]} (no position)')
                    board.repoint_with_leftovers(state, target)
                    swaps.sell_left_behind(state, force=True)
            swaps.sell_left_behind(state)
            if state.get('hot_pause') and pauses.hot_paused(state):
                time.sleep(config.POLL_SECONDS)
                continue
            b0 = capital.wallet(config.POOL)
            if dormant(state, b0):
                time.sleep(max(DORMANT_POLL_S, config.POLL_SECONDS))
                continue
            if pauses.macro_hold(state):
                time.sleep(config.POLL_SECONDS)
                continue
            books.notify('no_position', detail='chain reports no open position')
            try:
                fo = board.venue_failover(state, None, price=b0.get('price'), quote=b0.get('quoteUsd'))
            except Exception as e:
                fo = False
                books.notify('failover_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
            if fo:
                time.sleep(config.CALM_POLL_SECONDS)
                continue
            if regime.resume_reopen(state):
                time.sleep(config.CALM_POLL_SECONDS)
                continue
            # Nothing is held, so nothing is closed: choose the pool first.
            if not config.POOL_PINNED:
                cur = board.best_band_for(config.POOL)
                run, best, gain, why = board.board_pick(cur, config.POOL)
                if best and best['dex'] in config.EXECUTE_DEXES and best['dex'] in signers.SIGNERS \
                        and gain is not None and gain >= config.MIGRATE_MIN_GAIN:
                    books.notify('MIGRATE', held=f'{config.DEX} {config.PAIR_LABEL} (no position)',
                                 best=f"{best['dex']} {best['pair']} {best['net_day_pct']:.3f}%/day",
                                 gain_pct=None if gain is None else round(gain * 100),
                                 pool=best['address'], dex=best['dex'])
                    db.event('MIGRATE', f'{config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} (no position)')
                    board.repoint(best)
            k0 = None
            if config.REGIME_ENABLED:
                try:
                    if b0.get('pool') != config.POOL:
                        b0 = capital.wallet(config.POOL)                 # repointed above: the new pool's read
                    k0 = regime.regime_choice_now(config.POOL, b0['price']) if 'price' in b0 else None
                except Exception:
                    k0 = None
            moves.reopen(state, 'no position held', band=k0)
            time.sleep(config.POLL_SECONDS)
            continue

        price = status['price']
        wbal = capital.wallet(status['whirlpool'])
        wusd = wbal.get('walletUsd')
        fc = tape.forecast_for(status)
        board.sample_fee_growth(state, status)
        cv = regime.calm_view(state, status) if not config.REGIME_ENABLED else None
        rv = regime.regime_view(state, status)
        regime.track_touch_forecasts(state, rv, status)
        if rv and fc:
            # the held band's survival over the long horizons, from the hourly
            # tape: what the MODE section shows next to the 2-hour choice
            rv['p_exit'] = {h: fc.get(f'p_exit_{h}h_regime', fc.get(f'p_exit_{h}h')) for h in (6, 24, 72, 168)}
            rv['median_life_hours'] = fc.get('median_life_hours_regime', fc.get('median_life_hours'))
        # Under regime mode every band is the regime's, whatever its width,
        # and with or without a view this poll: without one it holds, and an
        # exit reopens at the widest regime width. Letting the hourly rules
        # take a +/-1% band fired PROACTIVE at once (review, 2026-09-26).
        tight = bool(config.REGIME_ENABLED) or bool(cv and cv.get('tight_held'))
        if tight and fc:
            # The hourly six-hour rule says nothing about a +/-1% band; the
            # five-minute rule in calm.py is in charge of it.
            fc = dict(fc, act=False, suspended=('regime mode: the five-minute width rule is in charge' if rv
                                                  else 'tight band: the five-minute calm rule is in charge'))
        # The book's price is in UI units (Token-2022 scaled mints), as the
        # baseline's and the flows': a plain pool's uiPrice is its price.
        db.snapshot(status['positionMint'], capital.ui_price(status), status.get('inRange'),
                        status.get('liquidity'),
                        status.get('feesAccruedA', 0.0),
                        status.get('feesAccruedB', 0.0),
                        status.get('feesAccrued_USD', 0.0),
                        wusd, capital.position_usd(status), forecast=fc)
        moves.settle_deposit(state, status)
        regime.record_risk(status, rv, fc)
        books.daily_report(state)
        housekeeping.run_audits(state)
        signers.probe_breakers()

        try:
            fo = board.venue_failover(state, status)
        except Exception as e:
            fo = False
            books.notify('failover_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
        if fo:
            time.sleep(config.CALM_POLL_SECONDS)
            continue

        if (config.HOT_PAUSE_ENABLED or config.MACRO_PAUSE_ENABLED) and pauses.hot_pause(state, status, rv):
            time.sleep(config.POLL_SECONDS)
            continue
        state.pop('hot_pause', None)

        if not status.get('inRange'):
            seen = polls.poll_seen(state, status, rv, cv, fc)
            verdict = polls.poll_verdict(seen)
            polls.record_poll(status.get('whirlpool') or config.POOL, seen, verdict)
            side = verdict['side']
            k = regime.reopen_width(verdict['band'], status.get('whirlpool') or config.POOL, price)
            books.notify('OUT_OF_BAND', side=side, price=price,
                         lower=status['lowerPrice'], upper=status['upperPrice'],
                         action=('harvest, close, reopen tight' if k else 'harvest, close, re-optimise, reopen'),
                         forecast=fc, calm=cv)
            moves.rebalance(state, status, f'price went {side}', band=k, calm_move=tight, exit_move=True,
                            exit_side=1 if side == 'above' else -1)
            time.sleep(config.POLL_SECONDS)
            continue

        # Calm mode: narrow when the market goes cold, re-centre the tight
        # band before it is touched, widen when the calm ends.
        swaps.sweep_foreign(state, wbal)
        if swaps.sell_left_behind(state):
            wbal = capital.wallet(status['whirlpool'])                  # the sale's quote token is idle now
        if swaps.deploy_idle(state, status, wbal, rv, price):
            time.sleep(config.CALM_POLL_SECONDS)
            continue

        seen = polls.poll_seen(state, status, rv, cv, fc)
        verdict = polls.poll_verdict(seen)
        polls.record_poll(status.get('whirlpool') or config.POOL, seen, verdict)
        if verdict['act'] in ('regime_widen', 'regime_narrow'):
            ev = {'regime_widen': 'REGIME_WIDEN', 'regime_narrow': 'REGIME_NARROW'}[verdict['act']]
            books.notify_book(ev, price=price, lower=status['lowerPrice'], upper=status['upperPrice'],
                              regime=rv, forecast=fc)
            db.event(ev, f"+/-{rv['held_pct']}% -> +/-{rv['choice_pct']}% ({rv['mode']}) "
                         f"sigma {rv['sigma_5m_pct']}% velocity {rv['velocity']}")
            moves.rebalance(state, status, f"regime {rv['mode']}: +/-{rv['held_pct']}% -> +/-{rv['choice_pct']}%",
                            band=verdict['band'], calm_move=True)
            time.sleep(config.CALM_POLL_SECONDS)
            continue
        # A calm move inside the gap is no verdict: announce nothing, try next poll.
        if verdict['act'] in polls.CALM_ACTS.values():
            what = {'calm_narrow': ('CALM_NARROW', 'calm: tight band'),
                    'calm_recentre': ('CALM_RECENTRE', 'calm: tight band re-centred before a touch'),
                    'calm_widen': ('CALM_WIDEN', 'calm over: back to the ladder band')}[verdict['act']]
            what = (what[0], verdict['band'], what[1])
            books.notify_book(what[0], price=price, lower=status['lowerPrice'], upper=status['upperPrice'],
                              calm=cv, forecast=fc)
            db.event(what[0], f"sigma {cv['sigma_5m_pct']}% cut {cv['cut_pct']}% "
                              f"p_touch {cv.get('p_touch')} fresh {cv.get('p_touch_fresh')}")
            moves.rebalance(state, status, what[2], band=what[1], calm_move=True)
            time.sleep(config.CALM_POLL_SECONDS if what[1] else config.POLL_SECONDS)
            continue

        # Still inside, but likely not for long: re-centre now, at this price,
        # rather than at whatever price the exit happens to land on. Waits
        # for a quiet hour only while the probability is below the ceiling
        # (90%): past that the exit is imminent and the hour does not matter.
        if verdict['deferred'] or verdict['act'] == 'proactive':
            p_now = fc.get('p_exit_horizon')
            if verdict['deferred']:
                o = db.season_outlook(db.season(), hour=board.utc_hour())
                books.notify('recentre_deferred', p_exit=p_now, horizon_hours=config.PROACTIVE_HORIZON,
                             reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                              f"waiting for a quiet hour unless the probability reaches 90%",
                             forecast=fc)
            else:
                books.notify_book('PROACTIVE', price=price, lower=status['lowerPrice'],
                                  upper=status['upperPrice'], p_exit=p_now,
                                  horizon_hours=config.PROACTIVE_HORIZON,
                                  threshold=config.PROACTIVE_THRESHOLD, forecast=fc,
                                  action='harvest, close, re-centre on the current price')
                db.event('PROACTIVE', f"P(exit within {config.PROACTIVE_HORIZON}h) = {p_now} "
                                      f"at {price}, band {status['lowerPrice']}..{status['upperPrice']}")
                moves.rebalance(state, status, f'P(exit within {config.PROACTIVE_HORIZON}h) '
                                         f'{(p_now or 0) * 100:.0f}% >= {config.PROACTIVE_THRESHOLD * 100:.0f}%')
                time.sleep(config.POLL_SECONDS)
                continue

        if verdict['harvest']:
            harvest.dividend(state, status)
            status, err = signers.read_status()
            if not status or not status.get('positionMint'):
                time.sleep(config.POLL_SECONDS)
                continue

        if paths.MIGRATE.exists():
            # "<dex> <pool>". Consumed before it runs. The same gates as an
            # automatic move: a signer must exist and be armed for the DEX.
            spec = paths.MIGRATE.read_text().split()
            paths.MIGRATE.unlink()
            target = board.operator_target(spec)
            if target:
                db.event('MIGRATE_REQUESTED', f'operator: {target["dex"]} {target["address"]}')
                books.notify('MIGRATE', held=f'{config.DEX} {config.PAIR_LABEL}',
                             best=f"{target['dex']} {target['pair']} (operator)", gain_pct=None,
                             pool=target['address'], dex=target['dex'])
                # While calm holds the tight band, the move keeps it: reopen
                # tight on the new pool (if still calm) instead of at the
                # ladder band, which would cost a second move to narrow again.
                k = (regime.regime_choice_now(target['address'], price, target.get('pair')) or rv['choice'] if rv
                     else regime.calm_reopen_band(cv, state)) if tight else None
                moves.rebalance(state, status, 'operator requested move', target=target,
                                band=k, calm_move=bool(k), operator=True)
                time.sleep(config.POLL_SECONDS)
                continue

        if paths.REBALANCE.exists():
            # Consumed before it runs, so a failure cannot loop on the trigger.
            paths.REBALANCE.unlink()
            db.event('REBALANCE_REQUESTED', 'operator touched REBALANCE')
            books.notify('REBALANCE_REQUESTED', price=price,
                         action='harvest, close, re-optimise, reopen')
            moves.rebalance(state, status, 'operator requested')
            time.sleep(config.POLL_SECONDS)
            continue

        forced = paths.REOPT.exists()
        if forced:
            paths.REOPT.unlink()
            db.event('REOPT_REQUESTED', 'operator touched REOPT')
            books.notify('REOPT_REQUESTED', action='board and band review now')
        scan_id = db.latest_scan_id() if tight else None
        if tight and (forced or scan_id != state.get('calm_review_scan')):
            # The band review waits while calm holds the tight band; the pool
            # review does not. It ranks venues on fee and reward density,
            # which a band does not change, once for every new board.
            state['calm_review_scan'] = scan_id; state['last_reopt'] = time.time(); paths.save(state)
            if regime.calm_budget_left(state) > 0 and board.calm_board_check(state, status):
                time.sleep(config.CALM_POLL_SECONDS)
                continue
        elif not tight and (forced or time.time() - state.get('last_reopt', 0) > config.REOPT_INTERVAL):
            state['last_reopt'] = time.time(); paths.save(state)
            best = board.best_band_for(status['whirlpool'])
            # The board first: a better pool elsewhere outranks a better band
            # here, and a move re-optimises the band on arrival anyway.
            if board.consider_migration(state, status, best):
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
                cur_run = runs.get(held_pct) or books.nearest(runs, held_pct)
                if cur_run:
                    gain = ((best['net_day_pct'] - cur_run['net_day_pct'])
                            / max(abs(cur_run['net_day_pct']), 1e-9))
                    if gain >= config.REOPT_MIN_GAIN and board.busy_hour():
                        o = db.season_outlook(db.season(), hour=board.utc_hour())
                        books.notify('move_deferred', kind='reband',
                                     held=f'+/-{held_pct}%', best=f'+/-{(best["band"] - 1) * 100:.0f}%',
                                     reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                                      f"waiting for a quiet hour (trough {o['trough_hour_utc']:02d} UTC)")
                        # come back next poll cycle rather than in six hours
                        state['last_reopt'] = time.time() - config.REOPT_INTERVAL + tuning.REOPT_RETRY_S; paths.save(state)
                    elif gain >= config.REOPT_MIN_GAIN:
                        books.notify('REBAND', improvement_pct=round(gain * 100),
                                     old_band=f'+/-{held_pct}%',
                                     new_band=f'+/-{(best["band"] - 1) * 100:.0f}%',
                                     old_net_day=round(cur_run['net_day_pct'], 3),
                                     new_net_day=round(best['net_day_pct'], 3))
                        db.event('REBAND', f'{held_pct}% -> '
                                           f'{(best["band"] - 1) * 100:.0f}%')
                        moves.rebalance(state, status, 're-optimised band')
                        time.sleep(config.POLL_SECONDS)
                        continue
                    books.notify('reopt_checked',
                                 held=f'+/-{held_pct}% {cur_run["net_day_pct"]:.3f}%/day',
                                 best=f'+/-{(best["band"] - 1) * 100:.0f}% '
                                f'{best["net_day_pct"]:.3f}%/day',
                                 verdict=f'{gain * 100:.0f}% gain is under the '
                                   f'{config.REOPT_MIN_GAIN * 100:.0f}% threshold')

        if time.time() - state.get('last_book', 0) >= config.POLL_SECONDS - 5:
            state['last_book'] = time.time()
            books.notify_book('in_band', price=price, lower=status['lowerPrice'],
                              upper=status['upperPrice'],
                              liquidity=status.get('liquidity'), forecast=fc, calm=cv, regime=rv)
        # Last, and only in a poll that made no move: a transaction built
        # seconds after a close reads stale accounts. On 2026-09-28 18:56Z the
        # janitor closed the RAY account and the re-centre that followed
        # failed in simulation.
        housekeeping.janitor(state)
        regime.edge_sleep(config.CALM_POLL_SECONDS if tight else config.POLL_SECONDS, status)
