"""Swaps and idle cash: 50/50 before an open, the Orca fallback, sweeps, leftovers, adds."""

import json
import math
import os
import re
import time
from datetime import datetime, timezone

import audit
import calm
import chains
import config
import db
import fees
import health
import wallets
from venues.jupiter import prices as jupiter_api
from lp import books, capital, moves, paths, regime, signers, tuning

IDLE_MIN_AGE_S = 600              # an open this recent may still be settling
IDLE_DEPLOYS_PER_DAY = 3          # re-centres to deploy idle money, at most, in 24 h
IDLE_ADDS_PER_DAY = 24            # adds (signer `increase`) of idle money, at most, in 24 h: one per open and more


def idle_deploys_left(times, now, per_day=IDLE_DEPLOYS_PER_DAY):
    """Idle deploys still allowed in the 24 h before `now`: `per_day` of them
    (re-centres by default; adds where the venue can add). Pure."""
    return max(0, per_day - sum(1 for t in (times or []) if now - t < tuning.DAY_S))


def adds_open_leftover():
    """Whether this profile adds an open's leftover to the position (owner,
    2026-10-08: "almost 99% in LP, idle money is bad"; the same strategy on
    every pool but the DJT swing test). Only where the venue's signer has
    `increase`: there the leftover goes in as a swap of the leftover alone
    and one add, not a re-centre. The swing (LPBOT_SWING_POOLS) keeps the
    leftover excused, as before."""
    return config.DEX in INCREASE_DEXES and not config.SWING_POOLS


def idle_to_deploy(deployable_usd, equity_usd, seconds_since_open):
    """Whether money idle beside an open position is worth a re-centre that
    deploys it: more than max($2, 2% of equity), the audit's idle limit, and
    the band at least IDLE_MIN_AGE_S old. A cycle costs about $0.012
    (lp-cost-structure); $22 idle forgoes about $0.20 a day of fees."""
    if seconds_since_open is None or seconds_since_open < IDLE_MIN_AGE_S:
        return False
    return deployable_usd > max(audit.IDLE_ABS_USD, audit.IDLE_SHARE * (equity_usd or 0.0))


def deploy_idle(state, status, wbal, rv, price):
    """Deploy-all: money idle beside the band (a swap that failed before an
    open, a leftover) goes in at the next allowed move, a re-centre at the
    regime's width, which swaps the wallet to 50/50 first. True when it moved."""
    if 'balanceA' not in wbal or wbal.get('walletUsd') is None:
        return False
    opened = db.position_opened(status['positionMint'])
    age = (datetime.now(timezone.utc) - opened).total_seconds() if opened else None
    idle = capital.deployable_usd(wbal)
    if idle is None:
        return False                                     # quote price unknown: nothing is valued
    equity = float(wbal['walletUsd']) + (capital.position_usd(status) or 0.0)
    # What a balanced open leaves out is its price tolerance, by design: in a
    # narrow band the deposit ratio moves ~80x faster than the price, so a
    # 7.5 bp tolerance leaves ~6% of one side (2026-09-28: $13 of $234). The
    # first reading of a band after such an open is that leftover; only new
    # money beyond it (a deposit, a sweep) is deployed. After an open that
    # skipped its swap, nothing is excused.
    # Where the venue can add (adds_open_leftover), that leftover is not
    # excused: it goes in once the band is IDLE_MIN_AGE_S old, by `increase`.
    # What that add leaves out is excused (add_idle), so it does not repeat.
    adds = adds_open_leftover()
    base = state.get('idle_baseline') or {}
    if base.get('mint') != status['positionMint']:
        excused = 0.0 if state.get('open_unbalanced') or adds else idle
        state['idle_baseline'] = {'mint': status['positionMint'], 'usd': round(excused, 4)}; paths.save(state)
        base = state['idle_baseline']
    idle_new = idle - float(base['usd'])                 # always written as a number above
    # An add is not a move: it waits for no move gap and spends no move
    # budget; the venue's breaker still holds it.
    allowed = (health.allowed(f'venue:{config.DEX}', time.time())[0] if adds
               else regime.calm_budget_left(state) > 0 and regime.voluntary_move_allowed(state))
    if not (idle_to_deploy(idle_new, equity, age) and allowed):
        return False
    # The re-centre deploys idle money only through its swap: while swaps
    # fail (the 'swap' breaker, exponential backoff, health.py), it would
    # close and reopen lopsided again, and spend the move budget the regime
    # needs (2026-09-30: every ~11 minutes). At most a few a day.
    now = time.time()
    ok, _st, wait, rec = health.allowed('swap', now)
    left = idle_deploys_left(state.get('idle_deploys'), now, IDLE_ADDS_PER_DAY if adds else IDLE_DEPLOYS_PER_DAY)
    if not ok or left <= 0:
        told = state.get('idle_deferred_told')
        key = f"{rec.get('last_fail')}:{left}"
        if told != key:
            state['idle_deferred_told'] = key; paths.save(state)
            why = (f"the swap failed {rec.get('fails')}x in a row: next try in {wait / 60:.0f} min"
                   if not ok else f'{IDLE_ADDS_PER_DAY if adds else IDLE_DEPLOYS_PER_DAY} idle deploys in 24 h already')
            books.notify('deploy_idle_deferred', idle_usd=round(idle, 2), reason=why)
            db.event('deploy_idle_deferred', f'${idle:.2f} idle: {why}')
        return False
    state['idle_deploys'] = [t for t in (state.get('idle_deploys') or []) if now - t < tuning.DAY_S] + [now]; paths.save(state)
    if config.DEX in INCREASE_DEXES:
        return add_idle(state, status, wbal, idle, price)
    k = rv['choice'] if rv else math.sqrt(status['upperPrice'] / status['lowerPrice'])
    books.notify_book('DEPLOY_IDLE', idle_usd=round(idle, 2), price=price, lower=status['lowerPrice'],
                      upper=status['upperPrice'])
    db.event('DEPLOY_IDLE', f'${idle:.2f} idle beside the band: re-centre to deploy it')
    moves.rebalance(state, status, f'deploy ${idle:.2f} idle', band=k, calm_move=True)
    return True


INCREASE_DEXES = {'raydium-clmm', 'uniswap-v3-polygon'}  # signers with `increase`: idle cash goes into the open position
INCREASE_RECHECKS, INCREASE_RECHECK_S = 6, 15  # after an errored add: re-reads for ~90 s (blockhash expiry)


def added_usd(before_l, after_status):
    """The dollars an increase put in, from the chain: the position's mark
    after it times the share of its liquidity that is new. None when the
    read has no position, no liquidity or no growth. Pure."""
    try:
        l0, l1 = int(before_l), int(after_status['liquidity'])
    except (TypeError, ValueError, KeyError):
        return None
    mark = capital.position_usd(after_status)
    if l1 <= l0 or l0 < 0 or mark is None:
        return None
    # tokens only: the rent in the mark is not part of what the add put in
    return (mark - capital.rent_usd(after_status)) * (l1 - l0) / l1


def add_idle(state, status, wbal, idle, price):
    """Idle cash into the open position (signer `increase`), not a close, swap
    and reopen (2026-10-03 audit: 10 such re-centres in 6.5 days, ~$0.04
    each). The idle cash alone is swapped to the band's share of token A at
    the live price (balance_wallet), then added at the position's own ticks.
    What went in is measured on chain after the send (added_usd), so an add
    that landed is booked even when its send reported an error, and the
    booked deposit is the program's, not the quote's. The add never feeds the
    venue breaker (record=False): its refusals say nothing about the venue,
    and three of them must not move the position (audit 2026-10-03). A
    failure moves nothing and is retried at the next allowed deploy. True
    when liquidity was added."""
    mint, lo, hi = status['positionMint'], status['lowerPrice'], status['upperPrice']
    try:
        share_a = calm.band_share_a(wbal['price'], lo, hi)
    except ValueError:
        return False                                     # the price left the band: the exit handles it
    bal = balance_wallet(state, wbal, capital.pool_record(), share_a=share_a)
    if bal is None or not capital.quote_known(state, bal, 'the add'):
        return False
    cap_a, cap_b = capital.deposit_caps(bal, share_a=share_a)
    if cap_a <= 0 or cap_b <= 0:
        return False                                     # in range an add takes both tokens: the signer refuses one
    out, err = signers.chain('increase', mint, f'{cap_a:.9f}', f'{cap_b:.9f}', '--execute', record=False)
    if signers.held(err):
        return False
    after, _ = signers.read_status(mint)
    added = added_usd(status.get('liquidity'), after)
    # After an error the add may still land until its blockhash expires: look
    # again for about 90 s before calling it failed (audit 2026-10-03).
    for _ in range(INCREASE_RECHECKS if err and added is None else 0):
        time.sleep(INCREASE_RECHECK_S)
        after, _ = signers.read_status(mint)
        added = added_usd(status.get('liquidity'), after)
        if added is not None:
            break
    sent = bool((out or {}).get('signature'))
    if added is None and sent and not err:
        added = float(out.get('depositUsd') or 0.0)      # the read failed: the signer's chain amounts
    if added is None:
        books.notify('increase_failed', reason=err or 'no signature', idle_usd=round(idle, 2))
        db.event('increase_failed', f'${idle:.2f} idle: {err or "no signature"}')
        return False
    if err:
        books.notify('increase_recovered', detail='the add reported an error but the position grew', added_usd=round(added, 4))
    db.add_deposit(mint, added)
    wl = capital.wallet(status.get('whirlpool') or config.POOL)
    left = capital.deployable_usd(wl) if 'balanceA' in wl else None
    # what this add leaves out is its price tolerance, as after an open: excused;
    # nothing is excused after a swap that did not land (open_unbalanced)
    excused = 0.0 if state.get('open_unbalanced') else (left or 0.0)
    state['idle_baseline'] = {'mint': mint, 'usd': round(excused, 4)}; paths.save(state)
    sig = (out or {}).get('signature')
    books.notify_book('INCREASE', positionMint=mint, added_usd=round(added, 4), idle_usd=round(idle, 2),
                      left_usd=None if left is None else round(left, 2), price=price, lower=lo, upper=hi, signature=sig)
    db.event('INCREASE', f'${added:.2f} of ${idle:.2f} idle added to {mint[:8]}: {sig}')
    return True


SWEEP_EVERY_S = 600               # a token-account read at most every ten minutes
SWEEP_MIN_USD = 1.0               # smaller balances are dust: a swap would cost more than it moves


def plan_sweep(accounts, pool_mints, reward_mints, prices, facts):
    """Which foreign tokens in the LP wallet to convert into the pool's
    tokens, so they can be deployed (owner, 2026-09-28: "every idle capital
    should be put on the LP"). Pure. `accounts` are audit.token_accounts rows;
    `prices` USD per mint; `facts` Jupiter's token facts per mint. Never: the
    pool's own tokens, reward tokens (paid out by distribute_rewards), wrapped
    SOL, position NFTs, unverified tokens (airdropped spam), or balances under
    SWEEP_MIN_USD."""
    plan = []
    for a in accounts:
        m = a['mint']
        if a['amount'] <= 0 or m in pool_mints or m in reward_mints or m == fees.NATIVE_MINT:
            continue
        if a['decimals'] == 0 and a['amount'] == 1:
            continue                                   # a position NFT
        human = audit.human(a)                          # UI units: a scaled mint's multiplier applied
        usd = human * float(prices.get(m) or 0.0)
        if usd < SWEEP_MIN_USD or not (facts.get(m) or {}).get('verified'):
            continue
        plan.append({'mint': m, 'amount': human, 'usd': round(usd, 4),
                     'symbol': (facts.get(m) or {}).get('symbol')})
    return plan


def wallet_mints():
    """Every mint of every profile of this wallet, disabled ones too (their pools'
    tokens and deposit mints): what the sweep must not sell and the janitor
    must not close. Empty without a wallet. Raises when the table cannot be
    read: a chore that cannot tell whose a token is does not run."""
    if not config.WALLET_ID:
        return set()
    out = set()
    for p in wallets.wallet_profiles(config.WALLET_ID):
        out |= {wallets.norm(m) for m in (p.get('mints') or []) + [p.get('deposit_mint')] if m}
    return out


def sweep_foreign(state, bal):
    """Convert foreign tokens in the LP wallet into the pool's quote token
    (plan_sweep); deploy_idle then puts them in the band. Wallet-wide: the
    residual owner's chore only, and never a token any profile of the wallet
    uses. Never blocks the loop. Returns the swaps made."""
    if not signers.housekeeper('sweep'):
        return []
    if time.time() - state.get('last_sweep', 0) < SWEEP_EVERY_S:
        return []
    state['last_sweep'] = time.time(); paths.save(state)
    try:
        owner = bal.get('owner')
        if not owner:
            return []
        (mint_a, _), (mint_b, _) = capital.pool_tokens()
        mine = {mint_a, mint_b} | wallet_mints()
        accounts = audit.token_accounts(config.RPC, owner)
        rewards = set(state.get('reward_mints_seen') or [])
        others = [a['mint'] for a in accounts if a['amount'] > 0 and a['mint'] not in mine]
        if not others:
            return []
        prices = jupiter_api.jupiter_prices(others)
        # Facts only for what is worth a sweep: dust is never swapped, so its
        # token search is wasted Jupiter budget (2026-10-01). Valued in UI
        # units, so a scaled mint's multiplier counts (audit.human).
        worth = {a['mint'] for a in accounts if a['mint'] in prices
                 and audit.human(a) * float(prices[a['mint']] or 0.0) >= SWEEP_MIN_USD}
        facts = {m: jupiter_api.jupiter_token(m) for m in others if m in worth}
        plan = plan_sweep(accounts, mine, rewards, prices, facts)
        target = mint_b if mint_b != fees.NATIVE_MINT else mint_a
        done = []
        for p in plan:
            sw, err = signers.chain('swap', p['mint'], target, f"{p['amount']:.9f}", '--execute', dex='jupiter')
            if err or not (sw or {}).get('signature'):
                books.notify('sweep_failed', reason=err or 'no signature', mint=p['mint'], usd=p['usd'])
                continue
            db.event('SWEEP', f"{p['amount']} {p['symbol'] or p['mint']} (${p['usd']:.2f}) swapped to the pool, "
                              f"{sw['signature']}")
            done.append({**p, 'signature': sw['signature']})
        if done:
            books.notify('SWEEP', swept=done, total_usd=round(sum(d['usd'] for d in done), 2))
        return done
    except Exception as e:
        books.notify('sweep_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return []


SWAP_RETRY_PAUSES = (15, 30)      # three attempts in all
# Jupiter's free API limits by IP per minute: its 429 needs the minute to pass,
# not 15 s (2026-10-01 20:33Z: all three attempts inside 76 s were refused).
# An RPC rate limit keeps the short pauses: rpc_policy moves to the next endpoint.
SWAP_RATE_LIMIT_PAUSES = (30, 60)
# When Jupiter fails without sending, the same swap is tried once directly on
# an Orca whirlpool (venues/orca/swap.mjs), so a Jupiter outage or rate limit does not
# open a lopsided band (owner, 2026-10-01). '' turns the fallback off.
SWAP_FALLBACK = os.environ.get('LPBOT_SWAP_FALLBACK', 'orca-swap')
# A failed swap that leaves a side the band needs under this share of the
# capital opens nothing: Orca refuses a zero side (0x177c, LiquidityZero) and
# three such refusals halted the swing on DJT (2026-10-09 13:47Z).
OPEN_SIDE_MIN = 0.02


def fallback_pool_args(mint_a, mint_b):
    """--pool for the Orca fallback swap of mint_a/mint_b: the held pool when
    it is an Orca whirlpool of exactly that pair, else nothing (swap_orca's
    DEFAULT_POOLS). Without it the fallback knew SOL/USDC only and refused
    DJT/USDC while Jupiter answered 429 (2026-10-09). Pure but for config."""
    if config.DEX != 'orca':
        return []
    try:
        (ma, _), (mb, _) = capital.pool_tokens()
    except Exception:
        return []
    return ['--pool', config.POOL] if {ma, mb} == {wallets.norm(mint_a), wallets.norm(mint_b)} else []


def one_side_short(usd_a, usd_b, frac_a, frac_b, capital_usd):
    """Whether a side the band wants (its target share > 0) holds under
    OPEN_SIDE_MIN of the capital: an open from it is refused or deposits a
    sliver. Pure."""
    floor = OPEN_SIDE_MIN * max(capital_usd, 0.0)
    return (frac_a > 0 and usd_a < floor) or (frac_b > 0 and usd_b < floor)


def balance_wallet(state, bal, rec, share_a=None):
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
    state['open_unbalanced'] = False
    if not config.REBALANCE_SWAP or not rec:
        return bal
    q = bal.get('quoteUsd')
    if q is None:
        books.notify('swap_skipped', reason='the quote token has no USD price; no swap sized on a guess')
        return bal
    res = capital.native_reserve(bal)
    usd_a = max(bal['balanceA'] - (res if bal.get('nativeSide') == 'A' else 0), 0) * capital.ui_price(bal) * q
    usd_b = max(bal['balanceB'] - (res if bal.get('nativeSide') == 'B' else 0), 0) * q
    C = capital.capital(bal)
    # Swap when either side is short of what the open may deposit of it (its
    # target share, less 3% for price movement), not merely short of half: a
    # side at 51% capped a deposit at $195 while $45 sat idle (2026-09-26).
    # The swap script itself does nothing within 2% of target.
    # An off-centre band (share_a, sql/026) wants share_a of the capital in
    # token A and the rest in token B; a centred one each side's target.
    if share_a is None:
        frac_a = frac_b = capital.side_target_fraction()
        if min(usd_a, usd_b) >= C * frac_a * tuning.BALANCED_SIDE_SHARE or abs(usd_a - usd_b) <= tuning.BALANCED_SPREAD * (usd_a + usd_b):
            return bal
    else:
        # within 2 points of the band's share: the swap script's own tolerance
        frac_a, frac_b = share_a, 1 - share_a
        if abs(usd_a - share_a * (usd_a + usd_b)) <= 0.02 * (usd_a + usd_b):
            return bal
    mint_a = (rec.get('token_a') or {}).get('address')
    mint_b = (rec.get('token_b') or {}).get('address')
    if not (chains.is_address(config.CHAIN, mint_a) and chains.is_address(config.CHAIN, mint_b)):
        books.notify('swap_skipped', reason='pool record has no mints')
        return bal
    # The record comes from a DEX's API; the signer read the pool itself. A
    # record that names tokens the pool does not hold would swap the capital
    # into them: no swap then (security review, 2026-10-09).
    on_chain = {wallets.norm(bal.get('mintA')), wallets.norm(bal.get('mintB'))}
    if None not in on_chain and on_chain != {wallets.norm(mint_a), wallets.norm(mint_b)}:
        books.notify('swap_skipped', reason='the pool record names tokens the pool does not hold; no swap',
                     record=sorted({wallets.norm(mint_a), wallets.norm(mint_b)}), chain=sorted(on_chain))
        return bal
    # The swap script keeps the gas reserve out of what it sells, but not the
    # open's rent headroom: the native side's target carries it, or an open
    # after a buy of SOL comes up short by the headroom and leaves the other
    # token idle.
    head = res - config.GAS_RESERVE_SOL
    head_usd = head * capital.ui_price(bal) * q if bal.get('nativeSide') == 'A' else \
        (head * q if bal.get('nativeSide') == 'B' else 0.0)
    target_a = f"{C * frac_a + (head_usd if bal.get('nativeSide') == 'A' else 0.0):.2f}"
    target_b = f"{C * frac_b + (head_usd if bal.get('nativeSide') == 'B' else 0.0):.2f}"
    # Prices and decimals the loop already has, so the swap needs no call to
    # Jupiter's rate-limited price API for the pool's own tokens.
    hints = {}
    try:
        ra, rb = rec.get('token_a') or {}, rec.get('token_b') or {}
        if ra.get('decimals') is not None and rb.get('decimals') is not None:
            hints = {mint_a: {'usd': capital.ui_price(bal) * q, 'decimals': int(ra['decimals']), 'symbol': ra.get('symbol')},
                     mint_b: {'usd': q, 'decimals': int(rb['decimals']), 'symbol': rb.get('symbol')}}
    except (KeyError, TypeError, ValueError):
        hints = {}
    env = {'LPBOT_TOKEN_HINTS': json.dumps(hints)} if hints else None
    if config.WALLET_ID:
        # The wallet may hold other profiles' tokens: the swap plans from
        # this profile's sleeve only (venues/jupiter/swap.mjs, venues/orca/swap.mjs, the
        # venue signer).
        env = dict(env or {}, LPBOT_SLEEVE=json.dumps(wallets.sleeve_caps(bal, mint_a, mint_b)))
    swap_dex = signers.route('swap')
    out, err = signers.chain('rebalance', mint_a, mint_b, target_a, target_b, '--execute', dex=swap_dex, extra_env=env,
                             record=False)
    limited = re.search(r'Jupiter (rate limited|429)', str(err))        # an RPC limit rotates endpoints instead
    for pause in (SWAP_RATE_LIMIT_PAUSES if limited else SWAP_RETRY_PAUSES):
        # Nothing left this process: a transport failure is safe to repeat.
        # One retry was not enough: two rate limits in a row on 2026-09-28
        # 18:16Z opened a lopsided band and left $22 of SOL idle.
        if not ((err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial')
                and re.search(r'rate limit|429|timeout|timed out|ECONNRESET|blockhash', str(err), re.I)):
            break
        time.sleep(pause)
        out, err = signers.chain('rebalance', mint_a, mint_b, target_a, target_b, '--execute', dex=swap_dex, extra_env=env,
                                 record=False)
    if signers.held(err):
        return None                 # the open would be refused too: hold, no failure counted
    # The swapper's own health ('jupiter' on Solana, the venue signer on Base);
    # 'swap' is whether the bot could swap at all, by it or the fallback:
    # deploy_idle waits on 'swap' only.
    signers.record_health(swap_dex, out, err)                 # one swap, one outcome, whatever the attempts
    if (SWAP_FALLBACK and SWAP_FALLBACK in signers.SIGNERS and swap_dex == 'jupiter'
            and signers.counts_as_failure(err or 'no result')
            and (err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial')):
        books.notify('swap_fallback', reason=f'Jupiter failed without sending ({err or "no result"}); swapping on Orca')
        out, err = signers.chain('rebalance', mint_a, mint_b, target_a, target_b, '--execute',
                                 *fallback_pool_args(mint_a, mint_b), dex=SWAP_FALLBACK, extra_env=env, record=False)
    signers.record_health('swap', out, err)
    if (err or not out) and not (out or {}).get('signature') and not (out or {}).get('partial'):
        # Nothing was sent. A side the band needs is (almost) empty: hold, no
        # failure counted, the next poll swaps again. An open would be refused
        # and three refusals write HALT (2026-10-09: DJT 0, USDC $13.70).
        if one_side_short(usd_a, usd_b, frac_a, frac_b, C):
            books.notify('swap_skipped', reason=f'swap failed without sending ({err or "no result"}); '
                                          f'holding: a side the band needs is under {OPEN_SIDE_MIN:.0%} '
                                          f'of the capital, swapping again at the next poll',
                         usd_a=round(usd_a, 2), usd_b=round(usd_b, 2))
            db.event('swap_skipped', f"hold, one side short | {err or 'no result'}")
            return None
        # Otherwise open with what the wallet holds: a smaller position
        # earning fees beats capital idle until the next poll.
        books.notify('swap_skipped', reason=f'swap failed without sending ({err or "no result"}); opening with the wallet as it is')
        db.event('swap_skipped', f"{err or 'no result'} | full: {signers.LAST_CHAIN_ERROR.get('text', '')}")
        state['open_unbalanced'] = True; paths.save(state)   # its leftover is not tolerance: deploy_idle deploys it
        return bal
    if out and out.get('noop'):
        books.notify('swap_skipped', reason='already at target', usd_a=round(usd_a, 2), usd_b=round(usd_b, 2))
        return bal
    if err or not out or out.get('partial') or not out.get('sent'):
        state['failures'] += 1; paths.save(state)
        db.event('swap_failed', err or 'no signature')
        books.notify('swap_failed', reason=err or 'no signature', failures=state['failures'],
                     signature=(out or {}).get('signature'))
        if state['failures'] >= config.MAX_CONSECUTIVE_FAILURES:
            books.halt(f'{state["failures"]} consecutive failures')
        return None
    db.event('SWAP', f"{out.get('signature')} usd {out.get('swapUsdValue')}")
    books.notify('SWAP', signature=out.get('signature'), usd=out.get('swapUsdValue'),
                 sold=out.get('sold'), bought=out.get('bought'),
                 price_impact_pct=out.get('priceImpactPct'), route=out.get('routePlan'),
                 before_usd_a=round(usd_a, 2), before_usd_b=round(usd_b, 2))
    time.sleep(5)
    after = capital.wallet(config.POOL)
    return after if 'balanceA' in after else None


LEFT_BEHIND_RETRY_S = 600        # an unsold leftover is tried again this often
LEFT_BEHIND_GAS_MARGIN = 0.002   # native kept above reserve + rent: float dust must not fail gas_for_open


def left_behind(old_tokens, new_tokens):
    """The old pool's mints the new pool does not hold: what a pair-changing
    move leaves in the wallet. Pure."""
    new = {wallets.norm(m) for m, _ in new_tokens}
    return [wallets.norm(m) for m, _ in old_tokens if wallets.norm(m) not in new]


def sell_left_behind(state, force=False):
    """Sell what a pair-changing move left behind (state['left_behind'])
    into the held pool's stablecoin side, so the reopen and deploy_idle put
    it in the band (the swing: SOL when it moves to DJT/USDC, DJT when it
    moves back). The native token is sold above the gas reserve only (the
    swap keeps it). Only a profile alone on its wallet sells: on a shared
    wallet the balance may be another profile's. Tried at most every
    LEFT_BEHIND_RETRY_S unless `force`; a mint leaves the list once sold or
    worth under SWEEP_MIN_USD. True when something was sold."""
    todo = list(state.get('left_behind') or [])
    if not todo:
        return False
    if not force and time.time() - float(state.get('left_behind_at') or 0) < LEFT_BEHIND_RETRY_S:
        return False
    state['left_behind_at'] = time.time(); paths.save(state)
    mints, why, shared = signers.claim_mints()
    if shared or mints is None:
        books.notify('left_behind_held', reason=why or 'the wallet is shared: a leftover may be another profile\'s',
                     mints=todo)
        return False
    (ma, _), (mb, _) = capital.pool_tokens()
    quote = mb if capital.is_stable_mint(mb) or not capital.is_stable_mint(ma) else ma
    native = wallets.norm(config.CAPS['native_mint'])
    got = wallets.read_balances(config.CHAIN, config.RPC, config.WALLET_ADDRESS, todo, native)
    if got is None:
        books.notify('left_behind_unsold', reason='balance unreadable', mints=todo)
        return False
    try:
        px = jupiter_api.jupiter_prices(todo)
    except Exception:
        px = {}
    sold = False
    for m in todo:
        have = float(got[0].get(m) or 0.0)
        # The native token keeps the gas reserve (the swap keeps that itself)
        # and the new venue's open rent (held out of the cap): selling down
        # to the reserve left an Orca open 0.009 SOL short (test, 2026-10-02).
        cap = max(have - capital.open_headroom(config.DEX) - LEFT_BEHIND_GAS_MARGIN, 0.0) if m == native else have
        amt = max(cap - config.GAS_RESERVE_SOL, 0.0) if m == native else cap
        usd = amt * float(px.get(m) or 0.0)
        if amt <= 0 or (px.get(m) and usd < SWEEP_MIN_USD):
            state['left_behind'] = [x for x in state['left_behind'] if x != m]; paths.save(state)
            continue
        sleeve = {'LPBOT_SLEEVE': json.dumps({m: cap, quote: 0.0})}
        out, err = signers.chain('rebalance', m, quote, '0', '1000000', '--execute', dex='jupiter', extra_env=sleeve)
        if (SWAP_FALLBACK and SWAP_FALLBACK in signers.SIGNERS and (err or not out)
                and not (out or {}).get('signature') and not (out or {}).get('partial')):
            # Jupiter sent nothing (429, outage): the same sale on an Orca
            # whirlpool. 2026-10-09: 1.6 SOL sat unsold 2 h after the DJT switch.
            books.notify('swap_fallback', reason=f'Jupiter failed without sending ({err or "no result"}); '
                                           f'selling the leftover on Orca', mint=m)
            out, err = signers.chain('rebalance', m, quote, '0', '1000000', '--execute', *fallback_pool_args(m, quote),
                                     dex=SWAP_FALLBACK, extra_env=sleeve)
        if out and out.get('signature') and not err:
            state['left_behind'] = [x for x in state['left_behind'] if x != m]; paths.save(state)
            db.event('LEFT_BEHIND_SOLD', f"{m} {amt:.9f} -> {quote} {out['signature']}")
            books.notify('LEFT_BEHIND_SOLD', mint=m, amount=round(amt, 9), usd=round(usd, 4) if usd else None,
                         into=quote, signature=out['signature'])
            sold = True
        elif out and out.get('noop'):
            state['left_behind'] = [x for x in state['left_behind'] if x != m]; paths.save(state)
        else:
            books.notify('left_behind_unsold', reason=err or 'no signature', mint=m, amount=round(amt, 9),
                         retry_in_s=LEFT_BEHIND_RETRY_S)
    return sold
