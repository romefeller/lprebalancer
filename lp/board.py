"""Venues and pools: the board, failover, migration, the swing's pool moves."""

import time

import config
import db
import dexes
import engine
import guards
import health
import wallets
from venues import solana_state
from venues.meteora_dlmm import pools as meteora_pools
from venues.uniswap_v3 import pools as uniswap_pools
from lp import books, capital, harvest, moves, paths, regime, signers, swaps, tuning

FAILOVER_MIN_RATIO = 0.8          # a failover target earns at least this share of the held venue


def failover_pick(held_dex, venues, allowed, *, execute_dexes, signers, min_hours, min_ratio=FAILOVER_MIN_RATIO):
    """The venue to fail over to when `held_dex` is tripped, or None. Pure.
    `venues` is venue_view(); `allowed(dex)` says whether that venue's
    breaker allows it. A target has on-chain evidence (min_hours), an armed
    signer, a breaker that allows it, and at least `min_ratio` of the held
    venue's on-chain income ("similar fee goodness"); the best such wins.
    Without evidence on the held venue, any eligible venue qualifies."""
    held = next((v for v in venues if v.get('held')), None)
    floor = (held.get('total_pct_day') or 0.0) * min_ratio if held and (held.get('hours') or 0) >= min_hours else None
    ok = [v for v in venues
          if not v.get('held') and v.get('dex') != held_dex and v.get('row')
          and (v.get('hours') or 0) >= min_hours and v.get('dex') in execute_dexes and v.get('dex') in signers
          and allowed(v['dex']) and (floor is None or (v.get('total_pct_day') or 0.0) >= floor)]
    return max(ok, key=lambda v: v.get('total_pct_day') or 0.0) if ok else None


def venue_failover(state, status, price=None, quote=None):
    """Fail over when the held venue's breaker is tripped (TRIP_FAILS
    failures of its own transactions in a row): move to the best venue with
    similar on-chain income. With a position, a rebalance to the target
    (its close is one more probe of the tripped venue); without one, repoint
    and reopen there. The old venue cools down; the normal pool review can
    bring the bot back once it earns more again. True when a move started."""
    key = f'venue:{config.DEX}'
    _ok, st, wait, rec = health.allowed(key)
    if int(rec.get('fails') or 0) < health.TRIP_FAILS:    # failover follows the count, not the light
        return False
    told = state.get('failover_told')
    tell = told != rec.get('last_fail')
    if config.POOL_PINNED:
        if tell:
            state['failover_told'] = rec.get('last_fail'); paths.save(state)
            books.notify('failover_none', venue=config.DEX, reason='pool pinned: no failover', fails=rec.get('fails'))
        return False
    p = price if price is not None else (status or {}).get('price')
    q = status.get('quoteUsd') if status else quote
    if not p or q is None:
        return False                    # income cannot be compared in dollars: no move on a guess
    try:
        venues = venue_view(p, q)
    except Exception as e:
        venues = []
        books.notify('venue_sample_failed', reason=books.tidy(e))
    target = failover_pick(config.DEX, venues, lambda d: health.allowed(f'venue:{d}')[0],
                           execute_dexes=config.EXECUTE_DEXES, signers=signers.SIGNERS, min_hours=config.VENUE_MIN_HOURS)
    if not target:
        if tell:
            state['failover_told'] = rec.get('last_fail'); paths.save(state)
            books.notify('failover_none', venue=config.DEX, fails=rec.get('fails'), retry_in_min=round(wait / 60),
                         reason=f'no venue with {FAILOVER_MIN_RATIO:.0%} of the income, evidence and a healthy breaker; '
                          f'backing off on {config.DEX}')
            db.event('failover_none', f"{config.DEX} tripped ({rec.get('fails')}x: {rec.get('last_error')}); no target")
        return False
    row = target['row']
    state['failover_told'] = rec.get('last_fail'); paths.save(state)
    what = (f"{config.DEX} tripped after {rec.get('fails')} failures ({rec.get('last_error')}) -> "
            f"{target['dex']} {target['total_pct_day']:.2f}%/d on chain")
    books.notify('FAILOVER', venue=config.DEX, to=target['dex'], pool=row['address'], fails=rec.get('fails'),
                 last_error=rec.get('last_error'), total_pct_day=target['total_pct_day'])
    db.event('FAILOVER', what)
    k = (regime.regime_choice_now(row['address'], p, row.get('pair')) or config.REGIME_WIDTHS[-1]) \
        if config.REGIME_ENABLED else None
    if status and status.get('positionMint'):
        moves.rebalance(state, status, f'failover: {what}', target=row, band=k, calm_move=True)
        return True
    repoint(row)
    pending = state.get('pending_reopen')
    if pending:
        # The intent's funds move with the venue: the failover is the reason.
        pending.update(pool=config.POOL, dex=config.DEX); paths.save(state)
    moves.reopen(state, f'failover: {what}', band=k)
    return True


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
    out = engine.ladder(p, engine.candles(pool), meteora_pools.feasible_bands(p, config.BANDS),
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
        books.notify('board_checked', verdict=why, scan=(run or {}).get('id'))
        return False
    cur_txt = (f"{config.DEX} {config.PAIR_LABEL} +/-{(current_best['band'] - 1) * 100:.0f}% "
               f"{current_best['net_day_pct']:.3f}%/day") if current_best else 'unscorable'
    best_txt = f"{best['dex']} {best['pair']} +/-{best['band_pct']:.0f}% {best['net_day_pct']:.3f}%/day"
    if gain is None:
        # The held pool could not be scored (a failed candle fetch): a missing
        # number is not a reason to pay for a move (review, 2026-09-26).
        books.notify('board_checked', held=cur_txt, best=best_txt,
                     verdict='the held pool could not be scored; staying')
        return False
    if gain < config.MIGRATE_MIN_GAIN:
        books.notify('board_checked', held=cur_txt, best=best_txt,
                     verdict=f'{gain * 100:.0f}% gain is under the {config.MIGRATE_MIN_GAIN * 100:.0f}% threshold')
        return False
    if best['dex'] not in config.EXECUTE_DEXES or best['dex'] not in signers.SIGNERS:
        books.notify('MIGRATE_RECOMMENDED', held=cur_txt, best=best_txt,
                     gain_pct=None if gain is None else round(gain * 100),
                     pool=best['address'], dex=best['dex'],
                     reason=f'no signer armed for {best["dex"]}; the bot stays where it is',
                     command=f'python3 db.py repoint {config.PROFILE} {best["dex"]} {best["address"]}')
        return False
    if busy_hour():
        o = db.season_outlook(db.season(), hour=utc_hour())
        books.notify('move_deferred', kind='pool move', held=cur_txt, best=best_txt,
                     reason=f"hour {o['hour_utc']:02d} UTC runs {o['now_x']}x the average; "
                      f"waiting for a quiet hour (trough {o['trough_hour_utc']:02d} UTC)")
        return False
    books.notify('MIGRATE', held=cur_txt, best=best_txt,
                 gain_pct=None if gain is None else round(gain * 100),
                 pool=best['address'], dex=best['dex'])
    db.event('MIGRATE', f'{config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} '
                        f'({cur_txt} -> {best_txt})')
    moves.rebalance(state, status, 'moved to a better pool', target=best)
    return True


def venue_candidates():
    """(dex, address, pair row) of the held pool and every screened pool of
    the same pair on the latest board, on a venue whose counters we read."""
    run, rows = db.latest_scan(max_age_seconds=config.SCAN_INTERVAL * 3)
    try:
        mints = {m for m, _ in capital.pool_tokens()}
    except Exception:
        mints = None
    out = {config.POOL: (config.DEX, config.POOL, None)}
    for r in rows or []:
        if r.get('skipped') or not r.get('screen_ok') or r.get('dex') not in solana_state.FEE_LAYOUT_DEXES:
            continue
        pair = {(r.get('token_a') or {}).get('address'), (r.get('token_b') or {}).get('address')}
        if mints and not config.ALLOW_SWAP and pair != mints:
            continue
        out[r['address']] = (r['dex'], r['address'], r)
    return list(out.values())


def sample_fee_growth(state, status=None):
    """Every VENUE_SAMPLE_S: one RPC call reads the fee counters of the held
    pool and every same-pair candidate, stores one sample each, and refreshes
    the ranking the book shows. Only where the chain has the counters. On a
    Uniswap v3 venue (Unichain, Polygon) the held pool's own counters are
    sampled, so the hot pause has its fee side (2026-10-08)."""
    if not config.CAPS.get('venues'):
        if config.DEX in uniswap_pools.UNISWAP_V3:
            sample_v3_fee_growth(state)
        return
    if time.time() - state.get('last_fee_sample', 0) < config.VENUE_SAMPLE_S:
        return
    state['last_fee_sample'] = time.time(); paths.save(state)
    try:
        cands = venue_candidates()
        states = solana_state.fee_states([(d, a) for d, a, _ in cands])
        for d, a, _ in cands:
            if a in states:
                db.record_fee_state(d, a, states[a])
        if status and status.get('quoteUsd') is not None:
            venue_view(status['price'], status['quoteUsd'])
    except Exception as e:
        books.notify('venue_sample_failed', reason=books.tidy(e))


def sample_v3_fee_growth(state):
    """The held Uniswap v3 pool's fee counters into fee_growth, every
    VENUE_SAMPLE_S, under the profile's pool key (the tape's and the hot
    pause's). A failed read is said once per kind of error and retried at
    the next sample; it never stops the poll."""
    if time.time() - state.get('last_fee_sample', 0) < config.VENUE_SAMPLE_S:
        return
    state['last_fee_sample'] = time.time(); paths.save(state)
    try:
        st = uniswap_pools.uniswap_v3_fee_state(config.POOL, dex=config.DEX)
        db.record_fee_state(config.DEX, config.POOL, st)
        state.pop('fee_sample_failed_told', None)
    except Exception as e:
        why = books.tidy(e)
        if state.get('fee_sample_failed_told') != why:
            state['fee_sample_failed_told'] = why; paths.save(state)
            books.notify('venue_sample_failed', reason=why)


def venue_income(pool, usd_a, usd_b, band=1.01):
    """The pool's on-chain income for a centred band, % per day, over up to
    the last 24 hours of samples, with the hours of evidence."""
    span = db.fee_state_span(pool)
    if not span:
        return None
    first, last, secs = span
    rw_mints = [m for m, _ in (last.get('rewards') or [])]
    rw_usd = harvest.reward_prices(rw_mints) if rw_mints else {}
    if rw_mints:
        solana_state.mint_decimals(rw_mints)
    inc = solana_state.band_income(first, last, secs, band, usd_a, usd_b, rw_usd)
    if not inc:
        return None
    return dict(inc, total_pct_day=inc['fee_pct_day'] + inc['reward_pct_day'], hours=round(secs / tuning.HOUR_S, 1))


def venue_view(price, quote_usd=1.0):
    """Every candidate's on-chain +/-1% income, best first."""
    out = []
    for d, a, row in venue_candidates():
        inc = venue_income(a, price * quote_usd, quote_usd)
        if inc:
            out.append({'dex': d, 'address': a, 'held': a == config.POOL,
                        'pair': (row or {}).get('pair') or config.PAIR_LABEL, **inc, 'row': row})
    out.sort(key=lambda v: -v['total_pct_day'])
    books.LAST_VENUES['view'] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items() if k != 'row'}
                                 for x in out]
    return out


def calm_board_check(state, status):
    """The pool review while calm mode holds the tight band. The hourly ladder
    says nothing about a +/-1% band, but fee (and reward) density does: at any
    band, a dollar at the active price earns in proportion to it. Moves to the
    densest eligible pool, still tight, when it beats the held one by
    migrate_min_gain. Returns True when a move started."""
    if config.POOL_PINNED or status.get('quoteUsd') is None:
        return False
    # On-chain evidence first: the board's density put PancakeSwap 18% above
    # Raydium and live it earned half (2026-09-26). A move needs at least
    # VENUE_MIN_HOURS of counter samples on both pools.
    try:
        venues = venue_view(status['price'], status['quoteUsd'])
    except Exception as e:
        venues = []
        books.notify('venue_sample_failed', reason=books.tidy(e))
    held_v = next((v for v in venues if v['held']), None)
    ok = [v for v in venues if not v['held'] and v['hours'] >= config.VENUE_MIN_HOURS and v.get('row')
          and v['dex'] in config.EXECUTE_DEXES and v['dex'] in signers.SIGNERS]
    if not held_v or held_v['hours'] < config.VENUE_MIN_HOURS:
        books.notify('board_checked', verdict=f'on-chain income: under {config.VENUE_MIN_HOURS}h of evidence on the held pool; staying')
        return False
    if not ok:
        books.notify('board_checked', held=f"{config.DEX} {held_v['total_pct_day']:.2f}%/d on chain",
                     verdict='on-chain income: no other pool with enough evidence; staying')
        return False
    top = ok[0]
    gain = top['total_pct_day'] / max(held_v['total_pct_day'], 1e-9) - 1
    held_txt = f"{config.DEX} {config.PAIR_LABEL} {held_v['total_pct_day']:.2f}%/d at ±1% on chain ({held_v['hours']}h)"
    best_txt = f"{top['dex']} {top['pair']} {top['total_pct_day']:.2f}%/d at ±1% on chain ({top['hours']}h)"
    if gain < config.MIGRATE_MIN_GAIN:
        books.notify('board_checked', held=held_txt, best=best_txt,
                     verdict=f'on-chain: {gain * 100:.0f}% more income is under the {config.MIGRATE_MIN_GAIN * 100:.0f}% threshold')
        return False
    best = top['row']
    books.notify('MIGRATE', held=held_txt, best=best_txt, gain_pct=round(gain * 100),
                 pool=best['address'], dex=best['dex'], calm=True)
    db.event('MIGRATE', f'on-chain: {config.DEX} {config.POOL} -> {best["dex"]} {best["address"]} '
                        f'({held_txt} -> {best_txt})')
    k = (regime.regime_choice_now(best['address'], status['price'], best.get('pair')) or config.REGIME_WIDTHS[0]) \
        if config.REGIME_ENABLED else config.CALM_BAND
    moves.rebalance(state, status, 'moved to the pool that earns more on chain', target=best, band=k, calm_move=True)
    return True


def operator_target(spec):
    """Turn "<dex> <pool>" from the MIGRATE file into a board-shaped target,
    or explain why not."""
    if len(spec) != 2 or not guards.is_address(spec[1]):
        books.notify('migrate_refused', reason=f'MIGRATE must contain "<dex> <pool>", got {spec!r}')
        return None
    dex, pool = spec
    if dex not in signers.SIGNERS or dex not in config.EXECUTE_DEXES:
        books.notify('migrate_refused', reason=f'{dex} has no armed signer (execute_dexes)')
        return None
    if dex == 'jupiter':
        books.notify('migrate_refused', reason='jupiter is a swap route, not a pool')
        return None
    try:
        rec = dexes.pool(dex, pool)
    except Exception:
        rec = None
    if not rec:
        books.notify('migrate_refused', reason=f'{dex} does not know a pool at {pool}')
        return None
    if dex == 'orca' and rec.get('adaptive_fee') and config.SIGNER_ENV.get('LPBOT_ORCA_ADAPTIVE') != '1':
        books.notify('migrate_refused', reason='adaptive-fee Orca pool: the signer cannot open it')
        return None
    try:
        held = {m for m, _ in capital.pool_tokens()}
    except Exception:
        held = None
    want = {wallets.norm((rec.get('token_a') or {}).get('address')), wallets.norm((rec.get('token_b') or {}).get('address'))}
    if held is None or want != held:
        # Security review, 2026-09-26: with rebalance_swap on, the reopen
        # would swap half the capital into whatever the target names. A pair
        # change needs allow_swap AND a pool the service environment pins
        # (LPBOT_SWING_POOLS: the swing's two pools).
        if not config.ALLOW_SWAP:
            books.notify('migrate_refused', reason='target holds a different pair and allow_swap is off')
            return None
        if pool not in config.SWING_POOLS:
            books.notify('migrate_refused', reason='target holds a different pair and is not in LPBOT_SWING_POOLS')
            return None
    return {'dex': dex, 'address': pool, 'pair': rec['pair'], 'token_a': rec['token_a'],
            'token_b': rec['token_b'], 'net_day_pct': None, 'band_pct': None}


def repoint_with_leftovers(state, target):
    """repoint(), and remember in state['left_behind'] the old pool's tokens
    the new pool does not hold (sell_left_behind sells them). A reopen intent
    moves with the pool, as on a failover: its funds are in the wallet, and
    an intent left on the old pool halted resume_reopen (a failed open held
    through an FOMC window that ends at the NYSE close, the swing's switch)."""
    old = capital.pool_tokens()
    repoint(target)
    rest = swaps.left_behind(old, capital.pool_tokens())
    state['left_behind'] = sorted(set(state.get('left_behind') or []) | set(rest))
    state.pop('left_behind_at', None)
    pending = state.get('pending_reopen')
    if pending:
        pending.update(pool=config.POOL, dex=config.DEX)
    paths.save(state)
    books.notify('REPOINTED', dex=config.DEX, pool=config.POOL, pair=config.PAIR_LABEL, left_behind=rest)


def repoint(target):
    """Point the profile at the target pool and reload the configuration, so
    everything the loop reads from `config` is the new pool's."""
    guards.migration_target(target, execute_dexes=config.EXECUTE_DEXES, signers=signers.SIGNERS,
                            known=dexes.KNOWN)
    db.repoint(config.PROFILE, target['dex'], target['address'], target['pair'],
               target['token_a']['symbol'], target['token_b']['symbol'])
    config.reload()
    assert config.DEX == target['dex'] and config.POOL == target['address'], \
        'config did not reload to the target pool'
