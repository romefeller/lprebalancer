"""What the wallet holds and what a band may deposit: sleeves, reserves, prices, the pool record."""

import json
import math
import os
import time

import chains
import config
import db
import dexes
import engine
import wallets
from lp import books, paths, signers, tuning

def wallet(pool):
    """What this profile holds of this pool's two tokens, and its dollar
    value: its sleeve of the wallet (sleeve_of).

    Both tokens, not just SOL. After a close the withdrawn quote token sits in
    the wallet; counting SOL alone would drop it from equity and report a loss
    on every rebalance that never happened.
    """
    before = settle_mark()
    out, _ = signers.chain('balance', pool)
    note_scale(out)
    if not out or 'balanceA' not in out:
        # One retry. The read that follows a close lands on an endpoint that
        # has just confirmed a transaction for us and is quick to rate-limit;
        # a second try ten seconds later has read cleanly every time so far.
        time.sleep(10)
        out, _ = signers.chain('balance', pool)
        note_scale(out)
    return unsettled_guard(sleeve_of(out or {}), before, settle_mark())


def settle_mark():
    """The wallet's (settled slot, pending write) now, to bracket a balance
    read and the claims read after it; None without a wallet, UNREADABLE
    (a pending write that is never booked) when the database cannot say."""
    if not config.WALLET_ID:
        return None
    try:
        return wallets.settle_state(config.WALLET_ID)
    except Exception:
        return UNREADABLE


UNREADABLE = (None, 'settle state unreadable')


def unsettled_guard(view, before, after):
    """`view` with walletUsd None when a write of the wallet was pending, or
    was booked, between the balance read (`before`) and the claims read
    (`after`): the balances and the claims then describe two different
    moments, and the residual holder's sleeve is off by the write (2026-10-02
    20:15Z: djt-usdc's open landed, its claim was not yet cut, and sol-usdc
    read $0 of USDC: equity -$5.84 for one snapshot). The snapshot then
    records equity unknown, not a loss. Pure."""
    if before is None or 'walletUsd' not in view:
        return view
    if before == after and not before[1]:
        return view
    return dict(view, walletUsd=None, unsettled=True)


_MINTS_SEEN = {}                  # profile -> the mints last written to config.mints


def is_stable_mint(mint):
    return wallets.norm(mint) in engine.STABLE_MINTS


def stable_quote_usd(ma, mb, price):
    """USD per token B from the mints alone: 1 for a stablecoin B, 1/price for
    a stablecoin A (USDC/HYPE: price is B per A, so B costs 1/price dollars),
    None otherwise. The signer's quoteUsd comes first; this fills a null.
    Pure."""
    if mb and is_stable_mint(mb):
        return 1.0
    try:
        p = float(price or 0)
    except (TypeError, ValueError):
        return None
    return 1.0 / p if ma and is_stable_mint(ma) and p > 0 else None


def sleeve_of(bal):
    """`bal` as this profile sees it (wallets.sleeve): the wallet's figures
    stay under rawBalanceA/rawBalanceB/rawWalletUsd. A quote price the signer
    could not give is unknown, except for a stablecoin by mint, which is a
    dollar. Without a wallet (a pre-020 profile), or when this profile is its
    wallet's only enabled one, the read as it is. When the sleeve cannot be
    worked out the answer is {}: an unreadable wallet, never the whole one."""
    if 'balanceA' not in bal or (config.WALLET_ID is None and bal.get('quoteUsd') is not None):
        return bal                                        # a pre-020 profile with a priced read: as it is
    try:
        (ma, _), (mb, _) = pool_tokens()
    except Exception:
        ma = mb = None
    if bal.get('quoteUsd') is None:
        q = stable_quote_usd(ma, mb, bal.get('uiPrice') or bal.get('price'))
        if q is not None:
            bal = dict(bal, quoteUsd=q, quoteUsdSource='stable mint')
    if not config.WALLET_ID:
        return bal
    try:
        profiles = wallets.wallet_profiles(config.WALLET_ID)
        if ma is None:
            if any(p['name'] != config.PROFILE for p in profiles):
                books.notify('sleeve_unreadable', reason='pool tokens unknown; the wallet is shared, so no figure')
                return {}
            return bal
        if _MINTS_SEEN.get(config.PROFILE) != [ma, mb]:
            wallets.register_mints(config.PROFILE, [ma, mb])
            _MINTS_SEEN[config.PROFILE] = [ma, mb]
        view = wallets.sleeve(bal, config.PROFILE, ma, mb, wallets.with_self(profiles, signers.me_row(ma, mb)),
                              wallets.claims(config.WALLET_ID), config.CAPS['native_mint'])
        if view.get('nativeSide') is not None and not config.VAULT:
            # This profile's pool holds the native token: every other profile
            # of the wallet pays its opens' rent from it, so it keeps that back.
            # (A vault pays no rent, its payer key does: the signer's own
            # nativeReserve, the vault's floor, stands.)
            view['nativeReserve'] = config.GAS_RESERVE_SOL + open_headroom(config.DEX) + sum(
                open_headroom(p.get('dex')) for p in profiles if p['name'] != config.PROFILE)
        return view
    except Exception as e:
        books.notify('sleeve_unreadable', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return {}


def ui_price(rec):
    """B per A in UI units, for valuing the UI amounts a signer reports
    (balances, caps, close estimates). The signer's `uiPrice`; its pool-native
    `price` when it has none (a plain mint: the same number). Bands and the
    open's lower/upper stay pool-native. Pure."""
    v = rec.get('uiPrice')
    return float(v if v is not None else rec['price'])


# The rent an open takes and does not give back at once, by venue, in SOL: a
# position's accounts and the tick or bin arrays its range is first to use.
# Raydium layout: 0.0053 + two tick arrays of 0.0018. Meteora DLMM: a new bin
# array is 0.0435 (the MU open, simulated 2026-10-01).
OPEN_RENT_HEADROOM_SOL = 0.009       # every venue not listed below
# Aerodrome (Base) and Uniswap v3 (Unichain) keep no rent: an NFT mint costs gas
# only, inside the gas reserve; 0.009 of ETH there would leave ~$25 never deployed.
# Uniswap v3 on Polygon keeps no rent either, but the swap before an open or an add
# (an exact approve and the swap, ~200k gas) costs up to 0.3 POL at the 1500 gwei cap,
# and the signer refuses the open or add under the reserve: the gas must cover the
# reserve AFTER that swap (2026-10-14: four paid swaps, four refused adds).
OPEN_RENT_HEADROOM = {'meteora-dlmm': 0.05, 'aerodrome-slipstream': 0.0, 'uniswap-v3-unichain': 0.0,
                      'uniswap-v3-polygon': 0.3}


def open_headroom(dex):
    return OPEN_RENT_HEADROOM.get(dex, OPEN_RENT_HEADROOM_SOL)


def native_reserve(bal):
    """Native SOL never deployed: the gas reserve and this venue's open rent,
    plus (sleeve_of puts it in `nativeReserve`) the open rent of every other
    profile of the wallet, which pays its rent and fees from this SOL too."""
    v = bal.get('nativeReserve')
    return float(v) if v is not None else config.GAS_RESERVE_SOL + open_headroom(config.DEX)


def deployable_usd(bal):
    """What the wallet can put into a position, in dollars: every unit of the
    pool's two tokens, less the native reserve on the native side. The only
    money that stays out is the gas the bot needs. None when the quote
    token's price is unknown: never valued as a dollar."""
    q = bal.get('quoteUsd')
    if q is None:
        return None
    res = native_reserve(bal)
    a = max(float(bal.get('balanceA') or 0.0) - (res if bal.get('nativeSide') == 'A' else 0.0), 0.0)
    b = max(float(bal.get('balanceB') or 0.0) - (res if bal.get('nativeSide') == 'B' else 0.0), 0.0)
    return (a * ui_price(bal) + b) * q


def capital(bal=None):
    """The sizing base, held under the ceiling a deposit must respect (each
    side is at most side_cap_fraction of it, and the signer refuses an open
    worth more than max_usd). With deploy_all and a wallet read, it is
    everything the wallet can deploy (deployable_usd); otherwise the
    configured capital plus every fee reinvested under the split. Callers
    with a wallet read check its quote price first (quote_known)."""
    if config.DEPLOY_ALL and bal and 'balanceA' in bal and bal.get('price'):
        base = deployable_usd(bal)
        if base is None:
            raise ValueError('quote price unknown: no capital figure')
    else:
        base = config.CAPITAL_USD
        if config.PAYOUT_ENABLED:
            try:
                base += db.reinvested_usd(config.PROFILE)
            except Exception:
                pass
    return min(base, config.MAX_USD / (2 * config.SIDE_CAP_FRACTION))


def side_target_fraction():
    """The share of the capital each side should hold before an open. A
    centred band takes exactly half in value (both sides are L*sqrt(P)*(1 -
    1/sqrt(k))), so when the capital is the whole wallet each side aims at
    one half; when it is a slice, each side may hold up to the side cap."""
    return 0.5 if config.DEPLOY_ALL else config.SIDE_CAP_FRACTION


def deposit_caps(bal, share_a=None):
    """Per-token deposit caps for an open, from what the wallet actually holds.

    Each side is capped at `side_cap_fraction` of the capital, in that token's
    own units, and at the wallet's balance of it less the gas reserve when the
    token is native SOL. The SDK's quote picks the liquidity both caps allow, so
    a wallet that is short one side opens a smaller position rather than failing.
    Caps are UI amounts, so token A's is divided by the UI price.
    """
    price = ui_price(bal)
    quote_usd = float(bal['quoteUsd'])               # known: reopen() checked (quote_known)
    capital_quote = capital(bal) / quote_usd
    # The gas reserve, plus the rent the open itself takes (open_headroom)
    # and the other profiles' (native_reserve). Without it every open left
    # gas below the reserve (0.0429 of 0.05 on 2026-09-26).
    reserve = native_reserve(bal)
    avail_a = bal['balanceA'] - (reserve if bal.get('nativeSide') == 'A' else 0)
    avail_b = bal['balanceB'] - (reserve if bal.get('nativeSide') == 'B' else 0)
    # An off-centre band (share_a) takes more of one side than the side cap
    # allows a centred one: that side's cap is its share plus the same margin.
    frac_a = frac_b = config.SIDE_CAP_FRACTION
    if share_a is not None:
        margin = config.SIDE_CAP_FRACTION - 0.5
        # each side at most the whole capital: the two caps never pass the
        # open guard's 2x capital (property test, 2026-10-03)
        frac_a = max(frac_a, min(1.0, share_a + margin))
        frac_b = max(frac_b, min(1.0, 1 - share_a + margin))
    cap_a = min(max(avail_a, 0), capital_quote * frac_a / price)
    cap_b = min(max(avail_b, 0), capital_quote * frac_b)
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
    rent = rent_usd(status)
    if status.get('positionUsd') is not None:
        return status['positionUsd'] + rent
    a, b, q = status.get('closeEstA'), status.get('closeEstB'), status.get('quoteUsd')
    if a is None or b is None or q is None:
        return None                                  # an unknown quote price is no dollar
    return (a * ui_price(status) + b) * q + rent


# The native token's last known USD price (rent is native): {'px', 'at'}.
NATIVE_PX = {}
NATIVE_PX_MAX_AGE_S = 3600        # a cached price this old still prices rent
NATIVE_SNAPSHOT_MAX_AGE_S = 900   # a native/stable pool's snapshot this old prices it


def note_native_px(px):
    """Remember a good native price (USD per native unit)."""
    if px is not None and px > 0:
        NATIVE_PX.update(px=float(px), at=time.time())


def native_usd():
    """USD per native token (SOL): the latest snapshot of a native/stable
    pool on this database (sol-usdc's pool) when it is fresh, else the last
    good price seen in this process when it is under an hour old, else None."""
    try:
        px = db.native_price(config.CAPS['native_mint'], sorted(engine.STABLE_MINTS), NATIVE_SNAPSHOT_MAX_AGE_S)
    except Exception:
        px = None
    if px:
        note_native_px(px)
        return px
    if NATIVE_PX and time.time() - NATIVE_PX['at'] <= NATIVE_PX_MAX_AGE_S:
        return NATIVE_PX['px']
    return None


def rent_usd(status):
    """The rent in the position accounts, in USD. The signer prices it when
    it can; a pool with no native side asks Jupiter, and one failed request
    gave null, which counted as $0 (2026-10-02: mu-usdc's equity jumped by
    the $8.26 rent on one poll in four). A null is priced here
    (native_usd); only with no price at all is it 0, said once."""
    usd, sol = status.get('rentUsd'), status.get('rentSol')
    if usd is not None:
        if sol:
            note_native_px(float(usd) / float(sol))
        return float(usd)
    if not sol:
        return 0.0
    px = native_usd()
    if px is None:
        if not RENT_UNPRICED.get('told'):
            RENT_UNPRICED['told'] = True
            books.notify('rent_unpriced', reason=f'{sol} native rent has no price; counted as $0 this poll')
        return 0.0
    RENT_UNPRICED.pop('told', None)
    return float(sol) * px


RENT_UNPRICED = {}                # 'told' while an unpriced rent has been said


def quote_price(rec):
    """USD per unit of the quote token for a balance or status read, or None
    when unknown. A null from the signer is unknown, never a dollar, except
    when one of the held pool's tokens is a stablecoin by mint
    (stable_quote_usd)."""
    q = rec.get('quoteUsd')
    if q is not None:
        return float(q)
    try:
        (ma, _), (mb, _) = pool_tokens()
    except Exception:
        return None
    return stable_quote_usd(ma, mb, rec.get('uiPrice') or rec.get('price'))


def quote_known(state, bal, what):
    """Whether `bal` carries its quote token's price. When it does not, the
    move (`what`) is skipped and why is said once, until a price is back."""
    if bal.get('quoteUsd') is not None:
        if state.pop('quote_unknown_told', None) is not None:
            paths.save(state)
        return True
    if not state.get('quote_unknown_told'):
        state['quote_unknown_told'] = time.time(); paths.save(state)
        why = f'{config.PAIR_LABEL}: the quote token has no USD price; {what} skipped until it has one'
        books.notify('quote_unknown', reason=why)
        try:
            db.event('quote_unknown', why)
        except Exception:
            pass
    return False
# GeckoTerminal prices a pool in UI units; the bands, the status price and
# the stored tape are pool-native. pool -> pool-native / UI price, from the
# last signer read that gave both (1 for a pool of plain mints). A Token-2022
# stock's multiplier (MSFTx 1.0059) is more than half a +/-1% band's margin.
UI_SCALE = {}


def note_scale(rec):
    """Remember the pool's native/UI price ratio from a signer read."""
    rec = rec or {}
    pool, px, ui = rec.get('whirlpool') or rec.get('pool'), rec.get('price'), rec.get('uiPrice')
    try:
        if pool and px and ui and float(px) > 0 and float(ui) > 0:
            UI_SCALE[pool] = float(px) / float(ui)
    except (TypeError, ValueError):
        pass


_POOL_REC = {}


def profit_wallet_pinned():
    """The payout destination must match the address pinned in the service
    environment, which a database write cannot change: the chain row names
    the variable (LPBOT_PROFIT_WALLET_PIN on Solana, LPBOT_EVM_PROFIT_WALLET_PIN
    on Base). chains/solana/payout.mjs and the EVM signer check the same pin themselves.
    A vault's pin is on chain: its policy."""
    if config.VAULT:
        # The vault's policy lets payouts reach only the wallets its owners
        # approved; chains/solana/payout.mjs checks the account against it.
        return bool(config.PROFIT_WALLET)
    pin = os.environ.get(config.CAPS['pin_env'], '')
    # an EVM address is one address in any letter case (EIP-55 checksums)
    return bool(pin) and wallets.norm(pin) == wallets.norm(config.PROFIT_WALLET) and chains.is_address(config.CHAIN, pin)


def pool_record():
    """The held pool's record, cached for an hour (reward programs change)."""
    key = (config.DEX, config.POOL)
    t, rec = _POOL_REC.get(key, (0, None))
    if rec is None or time.time() - t > tuning.POOL_RECORD_TTL_S:
        fresh = dexes.pool(config.DEX, config.POOL)
        if fresh:
            rec = fresh
            _POOL_REC[key] = (time.time(), rec)
    if rec is None:
        raise RuntimeError('pool record unavailable')
    return rec


def pool_tokens():
    """(mint, symbol) of token A and token B of the held pool; an EVM token
    address in lower case (wallets.norm), as every mint the loop compares."""
    rec = pool_record()
    return ((wallets.norm(rec['token_a']['address']), rec['token_a']['symbol']),
            (wallets.norm(rec['token_b']['address']), rec['token_b']['symbol']))


def wallet_book():
    """The wallet as audit.run reconciles it: every profile of the wallet
    (a disabled one's position and tokens are still in the wallet), the mints they use,
    each one's pool mints, and the claims. None without a wallet: the ledger
    is one book."""
    if not config.WALLET_ID:
        return None
    rows = [{'name': r['name'], 'mints': list(r['mints']) if r.get('mints') else None,
             'deposit_mint': r.get('deposit_mint'), 'residual_owner': r.get('residual_owner'),
             'enabled': r.get('enabled')} for r in db.profiles(config.WALLET_ID, enabled_only=False)]
    return {'profiles': rows,
            'mints': {wallets.norm(m) for r in rows for m in (r['mints'] or []) + [r['deposit_mint']] if m},
            'mints_of': {r['name']: r['mints'] for r in rows if r['mints']},
            'claims': wallets.claims(config.WALLET_ID)}


def gas_need(bal):
    """(have, need): the native gas the wallet read `bal` holds, and what an
    open or an add needs before it starts (gas_for_open). Pure on config."""
    side = bal.get('nativeSide')
    if config.VAULT:
        have = float(bal.get('gasSol') or 0.0)
        need = config.GAS_RESERVE_SOL + open_headroom(config.DEX)
    else:
        have = bal.get('sol')
        if have is None and side is not None:
            have = bal.get('balanceA' if side == 'A' else 'balanceB')      # the pool's native token is the gas
        have = float(have or 0.0)
        need = config.GAS_RESERVE_SOL if side is not None else native_reserve(bal)
    return have, need


def gas_for_open(state, bal):
    """Whether native gas covers this open's rent and the reserve after it.
    A profile whose pool holds no native token pays its open's rent (a new
    DLMM bin array: 0.0435 SOL) from SOL another profile owns. A pool that
    holds it needs only the reserve in the wallet first: deposit_caps keeps
    reserve and rent out of the deposit, and the swap to 50/50 buys the rest;
    but every signer refuses every write below the reserve (2026-10-02: a
    SOL/USDC profile funded with USDC alone failed its open on each poll,
    toward a halt). Short of it, it holds, says so once, counts no failure.
    A vault's gas is its payer key's (`gasSol`): that key pays the fees and
    every rent, and only the owners top it up."""
    have, need = gas_need(bal)
    if have >= need:
        state.pop('gas_short_told', None)
        return True
    if not state.get('gas_short_told'):
        state['gas_short_told'] = time.time(); paths.save(state)
        where = "the bot's fee payer key (the owners top it up)" if config.VAULT else 'the wallet'
        why = (f'{have:.4f} {config.CAPS["native_symbol"]} in {where}, an open or add on {config.DEX} needs '
               f'{need:.4f} (reserve + rent): holding until gas is topped up or refilled')
        books.notify('gas_short', reason=why)
        db.event('gas_short', why)
    return False


def record_baseline(bal, at=None):
    """The capital a profile started with, once: its sleeve when its first
    open went in (capital_flows kind 'baseline', one per wallet and profile;
    db.since_start measures profit and the hold benchmarks against it). Token
    A in amounts[<mint A>] and, when it is native SOL, in `sol`; the stablecoin
    side in `usdc`; the price in UI units, B per A, as the snapshots'. A profile
    without a wallet (pre-020) has its baseline already. Never blocks the
    open that just landed. `at` is when `bal` was read: the baseline's time,
    so db.since_start counts every flow after the read and none before it."""
    if not config.WALLET_ID:
        return False
    try:
        (ma, _), (mb, _) = pool_tokens()
        a, b, q = float(bal.get('balanceA') or 0.0), float(bal.get('balanceB') or 0.0), float(bal['quoteUsd'])
        px = ui_price(bal)
        usdc = a if is_stable_mint(ma) and not is_stable_mint(mb) else b
        with db.cursor(commit=True) as cur:
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, amounts, "
                        "wallet_id, profile) values (%s, 'baseline', %s, %s, %s, %s, null, %s, %s, %s, %s) "
                        "on conflict do nothing",
                        (at or db.now(), a if ma == config.CAPS['native_mint'] else 0.0, usdc, (a * px + b) * q, px,
                         'the sleeve at the first open', json.dumps({ma: a, mb: b}), config.WALLET_ID,
                         config.PROFILE))
            return cur.rowcount == 1
    except Exception as e:
        books.notify('baseline_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return False


WRAP_MIN = 1.0                    # native units: a smaller excess waits (a wrap costs ~45k gas)


def native_to_wrap(native, keep, reserve, min_wrap=WRAP_MIN):
    """How much native coin to wrap into the pool token: everything above
    the larger of `keep` (the gas float) and `reserve` (the signer's floor),
    or 0 when that is under `min_wrap`, unknown, or keep is off (0). Pure."""
    try:
        native, keep, reserve = float(native), float(keep), float(reserve)
    except (TypeError, ValueError):
        return 0.0
    if not (keep > 0) or not all(math.isfinite(x) for x in (native, keep, reserve)):
        return 0.0
    excess = native - max(keep, reserve)
    return excess if excess >= min_wrap else 0.0


def wrapped_in_pool():
    """Whether the chain names a wrapped native token (chains.py
    'wrapped_native') and it is one of this pool's two tokens. False when the
    pool's tokens are unknown."""
    wrapped = config.CAPS.get('wrapped_native')
    if not wrapped:
        return False
    try:
        (ma, _), (mb, _) = pool_tokens()
    except Exception:
        return False
    return wallets.norm(wrapped) in {wallets.norm(ma), wallets.norm(mb)}


def wrap_native(state, bal=None):
    """Native coin above native_keep into the pool's wrapped native token
    (owner, 2026-10-08: "let 10 POL for gas and the rest swap to pool"), so
    a deposit of native POL is seen like a WPOL deposit: deposit_seen, the
    swap to 50/50, the open; with a position open, the idle WPOL goes in by
    `increase`. Only where the chain names a wrapped native that is one of
    the pool's tokens. `bal`: a wallet read of this poll, else one is made.
    Never feeds the venue breaker. True when it wrapped."""
    if not (config.NATIVE_KEEP > 0) or not wrapped_in_pool():
        return False
    bal = bal if bal is not None else wallet(config.POOL)
    amount = native_to_wrap(bal.get('sol'), config.NATIVE_KEEP, config.GAS_RESERVE_SOL)
    if amount <= 0:
        return False
    sym = config.CAPS['native_symbol']
    out, err = signers.chain('wrap', f'{amount:.9f}', '--execute', record=False)
    if err or not (out or {}).get('signature'):
        told = state.get('wrap_failed_told')
        if told != str(err):
            state['wrap_failed_told'] = str(err); paths.save(state)
            books.notify('wrap_failed', reason=err or 'no signature', amount=round(amount, 6), symbol=sym)
            db.event('wrap_failed', f'{amount:.4f} {sym}: {err or "no signature"}')
        return False
    state.pop('wrap_failed_told', None); paths.save(state)
    books.notify('WRAP', amount=round(amount, 6), symbol=sym, kept=config.NATIVE_KEEP,
                 native_before=round(float(bal.get('sol') or 0), 6), signature=out['signature'])
    db.event('WRAP', f'{amount:.4f} {sym} wrapped into the pool token, {config.NATIVE_KEEP:g} kept for gas: {out["signature"]}')
    return True


# --- gas refill: the native coin from the pool's own wrapped token ------------
# On an EVM chain the fees arrive as the wrapped native token (WPOL), never as
# the native coin that pays the gas: the gas ran down under the reserve, every
# add and open was refused, and $435 sat idle (poly-wpol-usdt, 2026-10-14).
# The bot now refills its gas from its own wallet, as native SOL fees refill
# it on Solana (fees.split): the signer's `refill` unwraps the wrapped token
# (and first buys it with the pool's other token when the wrapped token is
# short) up to gas_refill_to (sql/041), never further.

REFILL_RETRY_S = 600              # a refill that failed waits this long, unless the wallet changed


def refill_target():
    """The native amount a refill reaches: gas_refill_to, or 0 when this
    profile does not refill (a chain without `refill`, a vault, a pool
    without the wrapped native, a target not above the reserve)."""
    t = config.GAS_REFILL_TO
    if not t > config.GAS_RESERVE_SOL or not config.CAPS.get('gas_refill') or config.VAULT or not wrapped_in_pool():
        return 0.0
    return t


def refill_due(native, reserve, target, need=0.0):
    """Whether to refill the gas now: a target above the reserve, a known
    native balance, and that balance under the low-water mark halfway between
    the reserve and the target, or under `need` (what the next open or add
    needs). Halfway: a refill brings at least half the band, so one
    transaction pays for several cycles. Pure."""
    try:
        native, reserve, target, need = float(native), float(reserve), float(target), float(need or 0.0)
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(x) for x in (native, reserve, target, need)) or not target > reserve:
        return False
    return native < max((reserve + target) / 2, need)


def refill_kind(bal):
    """How a refill is booked in payouts. 'gas' where the native coin is
    part of the pool's side and of the book (Aerodrome: ETH counts as WETH),
    as Solana's gas from SOL fees: value moves inside the book. 'gas_refill'
    where the book does not count the native coin (Uniswap v3: POL is gas
    only): the value leaves the book, and since_start and the DAILY line add
    it back as they add payouts, so equity and P&L stay right. Pure."""
    return 'gas' if bal.get('nativeSide') is not None else 'gas_refill'


def refill_gas(state, bal=None, need=0.0, position=None):
    """Refill the native gas from the wallet's wrapped native token when it is
    under the low-water mark or under `need` (refill_due). One signer call
    (`refill --execute`), never fed to the venue breaker. Booked in payouts
    (refill_kind) from the unwrap's own receipt, said as GAS_REFILL. A
    failure is said once per reason and waits REFILL_RETRY_S unless the
    wallet's pool tokens changed. True when native gas arrived."""
    target = refill_target()
    if not target:
        return False
    bal = bal if bal is not None else wallet(config.POOL)
    if 'balanceA' not in bal or not refill_due(bal.get('sol'), config.GAS_RESERVE_SOL, target, need):
        return False
    held = [round(float(bal.get('balanceA') or 0.0), 9), round(float(bal.get('balanceB') or 0.0), 9)]
    last = state.get('gas_refill_failed') or {}
    if last.get('held') == held and time.time() - float(last.get('at') or 0) < REFILL_RETRY_S:
        return False
    sym = config.CAPS['native_symbol']
    env = None
    if config.WALLET_ID:
        # the wallet may hold other profiles' tokens: the refill takes from this profile's sleeve only
        try:
            (ma, _), (mb, _) = pool_tokens()
        except Exception:
            return False
        env = {'LPBOT_SLEEVE': json.dumps(wallets.sleeve_caps(bal, ma, mb))}
    out, err = signers.chain('refill', '--execute', record=False, extra_env=env)
    if signers.held(err):
        return False
    got = float((out or {}).get('unwrapped') or 0.0)
    if (out or {}).get('noop') and not err:
        state.pop('gas_refill_failed', None); paths.save(state)
        return False
    if got <= 0:
        why = err or 'no unwrap landed'
        if last.get('why') != why:
            books.notify('gas_refill_failed', reason=why, native=bal.get('sol'), target=target, symbol=sym,
                         **({'signatures': out.get('signatures')} if (out or {}).get('signatures') else {}))
            db.event('gas_refill_failed', f'{bal.get("sol")} {sym}, target {target:g}: {why}')
        state['gas_refill_failed'] = {'at': time.time(), 'held': held, 'why': why}; paths.save(state)
        return False
    state.pop('gas_refill_failed', None); paths.save(state)
    usd = out.get('unwrappedUsd')
    kind = refill_kind(bal)
    sig = out.get('signature')
    sold = out.get('sold')
    detail = (f'{got:.6f} {sym} unwrapped for gas from {bal.get("sol")} toward {target:g}'
              + (f'; bought with {sold["amount"]} {sold["symbol"]}' if sold else ''))
    db.record_payout(config.PROFILE, position, wallets.norm(config.CAPS['wrapped_native']), sym, got,
                     float(usd) if usd is not None else None, kind, signature=sig, detail=detail)
    books.notify('GAS_REFILL', unwrapped=round(got, 6), symbol=sym, usd=usd, native_before=bal.get('sol'),
                 target=target, reserve=config.GAS_RESERVE_SOL, kind=kind, sold=sold, signature=sig,
                 **({'error': err} if err else {}))
    db.event('GAS_REFILL', f'{detail}: {sig}')
    return True


def gas_ready(state, bal, position=None):
    """`bal`, or the wallet read after a gas refill, when native gas covers
    the next open or add (gas_for_open); None when it does not, said once.
    Short of gas, a profile that can refill does so first: gas_short is no
    stop while the wallet holds the wrapped token or the pool's other token,
    and the open or add goes on the same poll. Called before the swap that
    comes first, so a write the signer would refuse is never preceded by a
    paid swap."""
    have, need = gas_need(bal)
    if have < need and refill_gas(state, bal, need=need, position=position):
        fresh = wallet(config.POOL)
        if 'balanceA' in fresh:
            bal = fresh
    return bal if gas_for_open(state, bal) else None


def native_upkeep(state, position=None):
    """Once a poll, one wallet read for both: the gas refilled when it is
    under the low-water mark (refill_gas), then native coin above
    native_keep wrapped into the pool token (wrap_native). Nothing is read
    on a profile that does neither."""
    refill = bool(refill_target())
    wrap = config.NATIVE_KEEP > 0 and wrapped_in_pool()
    if not (refill or wrap):
        return
    bal = wallet(config.POOL)
    if refill and refill_gas(state, bal, position=position):
        bal = None                                       # the refill moved it: wrap_native reads again
    if wrap:
        wrap_native(state, bal)
