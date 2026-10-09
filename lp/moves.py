"""Opening and closing positions: reopen and the full rebalance."""

import re
import time

import calm
import config
import db
import dexes
import guards
import health
from lp import board, books, capital, harvest, paths, regime, signers, swaps, tuning

# The chain's mark of a position just opened is read this many times, this
# far apart, before the signer's estimate stands in (settle_deposit then
# books the first mark a poll reads).
OPEN_MARK_TRIES = 3
OPEN_MARK_PAUSE_S = 5


def opened_mark(mint):
    """The chain's mark of the position `mint` just opened (position_usd:
    tokens plus rent, as every snapshot), or None. 2026-10-07: an Orca read
    right after an open missed the new position, and the ledger kept the
    signer's estimate on the requested band, $13 above what went into the
    tick-rounded one: the DJT P&L showed -$19.66 for a -$5.73 session."""
    for i in range(OPEN_MARK_TRIES):
        if i:
            time.sleep(OPEN_MARK_PAUSE_S)
        st, _ = signers.read_status(mint)
        if st and st.get('positionMint') == mint:
            usd = capital.position_usd(st)
            if usd is not None:
                return usd
    return None


def settle_deposit(state, status):
    """Replace an estimated deposit (state['deposit_estimate']) with the
    first chain mark of that position, once. A different position held, or
    none, drops the estimate: that band is closed, its deposit stands."""
    mint = state.get('deposit_estimate')
    if not mint:
        return False
    if status.get('positionMint') != mint:
        state.pop('deposit_estimate', None); paths.save(state)
        return False
    usd = capital.position_usd(status)
    if usd is None:
        return False                                     # unpriced this poll: the next one books it
    db.set_deposit(mint, usd)
    state.pop('deposit_estimate', None); paths.save(state)
    books.notify('deposit_settled', positionMint=mint, deposit_usd=round(usd, 2))
    return True


def reopen(state, reason, band=None, recovering=False, exit_side=0):
    """Open a fresh position at the best band, or at `band` when calm mode
    asks for the tight one, sized to what the wallet holds (after a swap to
    50/50 when `rebalance_swap` is on and the wallet is lopsided)."""
    pool = config.POOL
    best = board.best_band_for(pool)
    if not best and band:
        # A tight calm band needs no ladder: it is centred on the live chain
        # price. Only the pool's record (tokens, for the swap) is needed. On
        # 2026-09-26 an hourly-candle fetch failure left the capital idle.
        try:
            best = {'band': band, 'price': None, 'net_day_pct': 0.0, 'rebal_per_day': 0.0,
                    'record': dexes.pool(config.DEX, pool)}
        except Exception:
            best = None
    if not best:
        books.notify('idle', reason='could not price the pool; opening nothing')
        return False
    read_at = db.now()                                   # before the read: the baseline's time
    bal = capital.wallet(pool)
    if 'balanceA' not in bal:
        books.notify('idle', reason='could not read the wallet; opening nothing')
        return False
    if not capital.quote_known(state, bal, 'the open'):
        return False
    if recovering and band and config.REGIME_ENABLED:
        k_now = regime.regime_choice_now(pool, bal['price'])
        if k_now is None:
            books.notify('idle', reason='regime recovery waits for fresh five-minute data')
            return False
        band = k_now
    elif recovering and band:
        if not config.CALM_ENABLED:
            band = None
        else:
            price = bal['price']
            v = regime.calm_view(state, {'price': price, 'whirlpool': pool,
                                 'lowerPrice': price / band, 'upperPrice': price * band})
            if not v or v['bar_age_s'] > tuning.REOPEN_MAX_BAR_AGE_S or v.get('p_touch_fresh') is None:
                books.notify('idle', reason='CALM recovery waits for fresh five-minute data')
                return False
            if not v['calm'] or v['p_touch_fresh'] >= config.CALM_THRESHOLD:
                band = None
    k = band or best['band']
    if not capital.gas_for_open(state, bal):
        return False                                     # before the swap: no swap for an open that cannot run
    funded = bal                                         # the sleeve as funded: a first open's baseline
    # After an exit the band may sit off centre, against the exit (sql/026):
    # the swap aims at that band's share of token A, not at one half.
    frac = config.REOPEN_OFFSET if (band and exit_side and not recovering) else 0.0
    share_a = calm.band_share_a(bal['price'], *calm.offset_band(bal['price'], k, exit_side, frac)) if frac else None
    bal = swaps.balance_wallet(state, bal, best.get('record'), share_a=share_a)
    if bal is None or not capital.quote_known(state, bal, 'the open'):
        return False
    # A tight band is centred on the LIVE price: the ladder's price can be
    # minutes old, and on a +/-1% band that is a large part of the width.
    price = bal['price'] if band else best['price']
    lower, upper = calm.offset_band(price, k, exit_side, frac) if frac else (price / k, price * k)
    cap_a, cap_b = capital.deposit_caps(bal, share_a=share_a)
    if cap_a * capital.ui_price(bal) + cap_b < capital.capital(bal) * 0.1 / bal['quoteUsd']:
        books.notify('idle', reason=f'wallet holds too little {bal["tokenA"]} and '
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
                            capital_usd=capital.capital(bal), max_usd=config.MAX_USD,
                            quote_usd=bal['quoteUsd'], chain=config.CHAIN, ui_price=capital.ui_price(bal),
                            execute_dexes=config.EXECUTE_DEXES, signers=signers.SIGNERS)
    except guards.Refused as e:
        state['failures'] += 1; paths.save(state)
        db.event('open_refused', str(e))
        books.notify('open_refused', reason=str(e), failures=state['failures'])
        if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
            books.halt(f'{state["failures"]} consecutive failures')
        return False
    out, err = signers.chain('open', pool, f'{lower:.6f}', f'{upper:.6f}',
                             f'{cap_a:.9f}', f'{cap_b:.9f}', '--execute')
    if signers.held(err):
        return False                                     # held, said once by chain(): no failure, no retry storm
    if err:
        # The open may still have landed. Ask the chain before believing this.
        time.sleep(15)
        status, _ = signers.read_status()
        if status and status.get('positionMint'):
            books.notify('open_recovered', detail='open reported an error but the '
                                            'position exists on chain',
                         positionMint=status['positionMint'])
            out = {'positionMint': status['positionMint'], 'signature': None}
        else:
            state['failures'] += 1; paths.save(state)
            db.event('open_failed', err)
            books.notify('open_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                books.halt(f'{state["failures"]} consecutive failures')
            return False
    state['failures'] = 0; paths.save(state)
    mint = (out or {}).get('positionMint')
    # What actually went in, not the configured capital: a wallet short of one
    # side opens a smaller position, and the ledger must say so. The chain's
    # own mark of the new position is the truth; the signer's estimate is the
    # fallback if that read fails.
    deposit_usd = opened_mark(mint) if mint else None
    estimated = deposit_usd is None and bool(mint)
    if deposit_usd is None:
        deposit_usd = (out or {}).get('depositUsd')
    if deposit_usd is None:
        deposit_usd = min(capital.capital(bal), (cap_a * capital.ui_price(bal) + cap_b) * bal['quoteUsd'])
    db.open_position(mint, pool, config.PAIR_LABEL, lower, upper,
                     (k - 1) * 100, (out or {}).get('signature'),
                     deposit_usd, reason, config_name=config.PROFILE, dex=config.DEX)
    capital.record_baseline(funded, read_at)
    state.pop('pending_reopen', None)
    if estimated:
        state['deposit_estimate'] = mint                  # settle_deposit books the first chain mark
        books.notify('deposit_estimated', positionMint=mint, deposit_usd=round(deposit_usd, 2),
                     reason='the chain did not show the new position yet: the next poll books its mark')
    paths.save(state)
    books.notify_book('OPEN', pair=config.PAIR_LABEL, pool=pool, dex=config.DEX, lp_now_usd=deposit_usd,
           moves_24h_now=config.CALM_MAX_MOVES - regime.calm_budget_left(state),
           opened_band=books.band_label(k), calm_band=bool(band),
           lower=round(lower, 4), upper=round(upper, 4),
           deposit_usd=round(deposit_usd, 2),
           deposit_a=(out or {}).get('depositEstA'), deposit_b=(out or {}).get('depositEstB'),
           cap_a=f'{cap_a:.6f} {bal["tokenA"]}', cap_b=f'{cap_b:.6f} {bal["tokenB"]}',
           expected_net_day_pct=None if band else round(best['net_day_pct'], 3),
           modelled_rebalances_per_day=None if band else round(best['rebal_per_day'], 2),
           model_scope='CALM policy not modelled by hourly ladder' if band else 'hourly ladder',
           signature=(out or {}).get('signature'), reason=reason)
    return True


# A close error that a second try 15 s later may fix. 2026-10-07: "Signature
# X has expired: block height exceeded" did not match 'blockhash', and an
# out-of-range band waited five minutes for the next poll in a 2% drop.
CLOSE_RETRY_ERRORS = r'rate limit|429|timeout|timed out|ECONNRESET|blockhash|block height'


def rebalance(state, status, reason, target=None, band=None, calm_move=False, exit_move=False, close_only=False,
              operator=False, exit_side=0):
    """Harvest, close, and reopen: on the same pool, or on `target` (a board
    row) after repointing the profile. The close runs on the DEX the position
    is on, whatever the profile says by then.

    A calm move (narrow, re-centre, widen, or the exit of a tight band) has
    its own gap (`calm_min_gap_seconds`) and its own budget, counted apart
    from the normal ones; both together sit under one hard ceiling,
    max_rebalances_per_day + calm_max_moves_per_day, past which the bot
    halts as before. An operator move (the MIGRATE file: the swing's switch
    at the US open and close) never waits for the gap either; the ceilings
    hold."""
    now = time.time()
    recent = [t for t in state['rebalance_times'] if now - t < tuning.DAY_S]
    calm_recent = [t for t in state.get('calm_times', []) if now - t < tuning.DAY_S]
    last_any = max([state['last_rebalance']] + calm_recent)
    # An exit under regime mode never waits: out of range earns nothing.
    gap = 0 if (exit_move and config.REGIME_ENABLED) or operator else \
        (config.CALM_MIN_GAP if calm_move else config.MIN_REBALANCE_GAP)
    last = last_any if calm_move else state['last_rebalance']
    if now - last < gap:
        books.notify('rebalance_deferred', seconds_remaining=int(gap - (now - last)),
                     kind='calm' if calm_move else 'normal')
        return
    if len(recent) + len(calm_recent) >= config.MAX_REBALANCES_PER_DAY + config.CALM_MAX_MOVES:
        books.halt(f'{len(recent) + len(calm_recent)} rebalances in 24h, at the hard ceiling')
        return
    if not calm_move and len(recent) >= config.MAX_REBALANCES_PER_DAY:
        books.halt(f'{len(recent)} rebalances in 24h, at the ceiling')
        return
    mint = status['positionMint']

    accrued_a = status.get('feesAccruedA', 0.0)
    accrued_b = status.get('feesAccruedB', 0.0)
    accrued_usd = status.get('feesAccrued_USD', 0.0)
    out, err = signers.chain('harvest', mint, '--execute')
    if signers.held(err):
        return                      # a paused mint refuses the close too: hold the position as it is
    state['last_harvest'] = now
    harvested = False
    if out and out.get('signature') and not err:
        harvested = True
        accrued_a, accrued_b, accrued_usd = harvest.measured_fees(out, status, accrued_a, accrued_b, accrued_usd)
        db.record_harvest(mint, accrued_a, accrued_b, accrued_usd,
                              out['signature'])
        # The fees just moved from the position to the wallet. Record the
        # position's counter at zero now, or the book double-counts them as
        # both realised and unrealised until the next poll.
        db.snapshot(mint, capital.ui_price(status), status.get('inRange'),
                    status.get('liquidity'), 0.0, 0.0, 0.0,
                    capital.wallet(status['whirlpool']).get('walletUsd'),
                    capital.position_usd(status))
        books.notify('HARVEST', collected_usd=round(accrued_usd, 4),
                     signature=out['signature'])
        try:
            harvest.distribute(state, mint, accrued_a, accrued_b)
            harvest.distribute_rewards(state, mint, signers.signatures_of(out))
        except Exception as e:          # the split must never stop a move
            books.notify('payout_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
    else:
        # Never let a harvest block the close. An out-of-range position must
        # move, and the close collects any fees the harvest could not: the
        # Meteora and Byreal signers answer "nothing to claim" with no
        # signature, and treating that as a failure would leave the band out
        # of range until the bot halted.
        books.notify('harvest_skipped', reason=err or (out or {}).get('note') or 'no signature returned')

    if calm_move and target is None and not close_only:
        state['pending_reopen'] = {'mint': mint, 'pool': config.POOL, 'dex': config.DEX,
                                   'band': band, 'reason': reason, 'started_at': now,
                                   'withdraw_usd': capital.position_usd(status), 'closed': False}
        paths.save(state)

    venue = signers.health_key(('close',), config.DEX)
    out, err = signers.chain('close', mint, '--execute', record=False)
    if signers.held(err):
        state.pop('pending_reopen', None); paths.save(state)
        return                      # held: said once by chain(), no failure counted
    if err and re.search(CLOSE_RETRY_ERRORS, str(err), re.I):
        # A transport failure: if the position is provably still there, the
        # close did not land and one more try is safe. On 2026-09-26 a
        # rate-limited close left a move half-done until the next poll.
        time.sleep(15)
        check, _ = signers.read_status()
        if check is not None and check.get('positionMint') == mint:
            books.notify('close_retry', reason=err)
            out, err = signers.chain('close', mint, '--execute', record=False)
    if err:
        # A close that reports failure may have landed. This exact false
        # negative left a position closed and the capital idle in production.
        time.sleep(15)
        check, _ = signers.read_status()
        if check is not None and not check.get('positionMint'):
            health.record_success(venue)              # the close landed: the venue works
            db.event('close_recovered', err)
            books.notify('close_recovered',
                         detail='close reported an error but the position is gone; '
                          'treating it as closed', error=err)
            out = {'signature': None}
        else:
            # The position is still open: nothing to resume later. A stale
            # intent would replay after an unrelated failure, on a pool the
            # bot may have left by then.
            state.pop('pending_reopen', None)
            signers.record_health(venue, None, err)            # one close, one failure, whatever the attempts
            state['failures'] += 1; paths.save(state)
            db.event('close_failed', err)
            books.notify('close_failed', reason=err, failures=state['failures'])
            if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
                books.halt(f'{state["failures"]} consecutive failures')
            return
    else:
        signers.record_health(venue, out, None)
    # What the close returns: the mark taken just before it, tokens plus the
    # rent the chain refunds. The best figure available without a second
    # read, and what per-pool P&L is measured against.
    db.close_position(mint, (out or {}).get('signature'), capital.position_usd(status))
    if not harvested and (accrued_usd or 0) > 0:
        # The close collected the fees the harvest could not. Record them as
        # realised and split them, or they vanish from the ledger and from the
        # payout (review, 2026-09-26).
        db.record_harvest(mint, accrued_a, accrued_b, accrued_usd,
                          # 'close:' marks fees the close collected: its tx moves
                          # principal too, so the harvest audit cannot measure them
                          # from the vault outflow (audit 2026-09-30, row 63)
                          f"close:{(out or {}).get('signature') or mint}")
        try:
            harvest.distribute(state, mint, accrued_a, accrued_b)
        except Exception as e:
            books.notify('payout_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
    harvest.band_profile(mint, 'rebalance', reason)
    # The move counts from the moment the close landed: record it before the
    # CLOSE book, so the book's moves_24h includes it (audit, 2026-09-30: the
    # CLOSE and OPEN books were one short).
    if calm_move:
        state['calm_times'] = calm_recent + [now]
        if state.get('pending_reopen'):
            state['pending_reopen']['closed'] = True
    else:
        state['last_rebalance'] = now
        state['rebalance_times'] = recent + [now]
    paths.save(state)
    books.notify_book('CLOSE', positionMint=mint, lp_now_usd=0.0, moves_24h_now=config.CALM_MAX_MOVES - regime.calm_budget_left(state),
                      signature=(out or {}).get('signature'), reason=reason)
    if close_only:
        return                      # a disabled profile's last move: nothing reopens
    if target:
        board.repoint_with_leftovers(state, target)
        swaps.sell_left_behind(state, force=True)
    reopen(state, reason, band=band, exit_side=0 if target else exit_side)
