"""Fees and rewards: harvest timing, the split, payouts, measured income."""

import json
import re
import time

import config
import db
import fees
import guards
import txfees
import wallets
from venues.jupiter import prices as jupiter_api
from lp import books, capital, paths, signers, swaps, tuning

def harvest_ready(fees_usd, since_s, interval_s, min_usd):
    """The dividend: accrued fees go to the wallet every `interval_s`, once
    at least `min_usd` has accrued; `since_s` is the time since the last
    one. Never while a rebalance is about to harvest anyway. Pure."""
    if not interval_s:
        return False
    if (fees_usd or 0) < min_usd:
        return False
    return since_s >= interval_s


def distribute_rewards(state, position, signatures=()):
    """Reward tokens, after a harvest: every reward mint the pool names (and
    every one it has named before, so a program that just ended is still
    swept), other than the pool's own two tokens and every mint of every
    profile of the wallet. Only what this profile's harvests are shown to
    have brought is paid (txfees.inflow over the harvest's `signatures`,
    carried in state['reward_due'] until paid): the wallet's balance of a
    reward token may be another profile's. Under the 'payout' policy an amount
    worth at least reward_min_usd is swapped through Jupiter (LPBOT_SLEEVE:
    that amount at most) to the payout token and sent to the profit wallet;
    while gas is under the reserve it is swapped to native SOL and kept as
    gas. Never blocks a move."""
    if not config.PAYOUT_ENABLED or config.REWARD_POLICY != 'payout' or not config.CAPS.get('rewards'):
        return None
    rec = capital.pool_record()
    own = {rec['token_a']['address'], rec['token_b']['address']}
    seen = state.setdefault('reward_mints_seen', [])
    for m in rec.get('reward_mints') or []:
        if guards.is_address(m) and m not in seen:
            seen.append(m)
    del seen[:-8]                                       # a bounded memory of programs
    theirs = swaps.wallet_mints()
    mints = [m for m in seen if m not in own and m not in theirs and guards.is_address(m)]
    if not mints:
        return None
    bal = capital.wallet(config.POOL)
    arrived = txfees.inflow(config.RPC, signatures, bal.get('owner') or config.WALLET_ADDRESS, mints)
    due = state.setdefault('reward_due', {})
    if arrived is None:
        books.notify('reward_unmeasured', reason='the harvest transactions are unreadable: rewards wait')
        paths.save(state)
        return None
    for m, v in arrived.items():
        due[m] = float(due.get(m, 0.0)) + v
    gas_low = (bal.get('sol') or 0.0) < config.GAS_RESERVE_SOL
    target = fees.NATIVE_MINT if gas_low else config.PAYOUT_MINT
    if not target:
        return None
    prices = jupiter_api.jupiter_prices(mints)
    done = []
    for m in mints:
        out, err = signers.chain('balance', m, dex='payout')
        amt = min(float((out or {}).get('amount') or 0.0), float(due.get(m, 0.0)))
        usd = amt * prices.get(m, 0.0)
        if amt <= 0 or usd < config.REWARD_MIN_USD:
            continue
        if usd > config.REWARD_MAX_USD:
            # a reward balance worth more than the cap is not swept blind: a
            # wrong price or an unexpected token needs an operator's look
            books.notify('reward_held', reason=f'${usd:.2f} of {m} exceeds reward_max_usd ${config.REWARD_MAX_USD:.2f}')
            continue
        before, _ = signers.chain('balance', target, dex='payout')
        sw, err = signers.chain('swap', m, target, f'{amt:.9f}', '--execute', dex='jupiter',
                                extra_env={'LPBOT_SLEEVE': json.dumps({m: amt})})
        after, _ = signers.chain('balance', target, dex='payout')
        # Pay what actually arrived, not what the quote promised.
        measured = float((after or {}).get('amount') or 0.0) - float((before or {}).get('amount') or 0.0)
        quoted = float(((sw or {}).get('bought') or {}).get('amount') or 0.0)
        got = min(measured, quoted) if measured > 0 else 0.0
        if err or not (sw or {}).get('signature') or got <= 0:
            books.notify('reward_swap_failed', reason=err or 'no signature', mint=m, amount=amt)
            continue
        due[m] = max(float(due.get(m, 0.0)) - amt, 0.0)
        if gas_low:
            db.record_payout(config.PROFILE, position, target, 'SOL', got, usd, 'gas',
                             signature=sw['signature'], detail=f'reward {m} swapped for gas')
            done.append({'mint': m, 'usd': round(usd, 4), 'to': 'gas'})
            continue
        tx, err = signers.chain('send', target, f'{got:.9f}', config.PROFIT_WALLET, '--execute', dex='payout')
        if tx and tx.get('signature') and not err:
            db.record_payout(config.PROFILE, position, target, 'reward', got, usd, 'paid',
                             to_address=config.PROFIT_WALLET, signature=tx['signature'],
                             detail=f'reward {m} swapped {sw["signature"]}')
            done.append({'mint': m, 'usd': round(usd, 4), 'to': 'profit wallet', 'signature': tx['signature']})
        else:
            owed = state.setdefault('payout_owed', {})
            owed[target] = float(owed.get(target, 0.0)) + got
            db.record_payout(config.PROFILE, position, target, 'reward', got, usd, 'owed',
                             to_address=config.PROFIT_WALLET, detail=err or 'no signature')
            books.notify('payout_failed', reason=err or 'no signature', symbol='reward', owed=round(got, 6))
    paths.save(state)
    if done:
        books.notify('REWARD_PAYOUT', rewards=done, gas_low=gas_low)
    return done


def distribute(state, position, fee_a, fee_b):
    """Split one harvest by the owner's rule (fees.py): payout-token fees to
    the profit wallet now, native SOL to gas while it is under the reserve,
    the rest reinvested. A failed transfer is owed and retried with the next
    harvest; it never blocks a move and never counts toward a halt."""
    if not config.PAYOUT_ENABLED or not (fee_a or fee_b):
        return None
    try:
        (mint_a, sym_a), (mint_b, sym_b) = capital.pool_tokens()
    except Exception as e:
        books.notify('payout_skipped', reason=f'pool tokens unreadable: {books.tidy(e)}')
        return None
    bal = capital.wallet(config.POOL)
    if 'balanceA' not in bal:
        books.notify('payout_skipped', reason='could not read the LP wallet; the fees stay in it')
        return None
    q = capital.quote_price(bal)
    if q is None:
        books.notify('payout_skipped', reason='the quote token has no USD price; the fees stay in the wallet')
        return None
    px_a, px_b = capital.ui_price(bal) * q, q
    native_fee = fee_a if mint_a == fees.NATIVE_MINT else fee_b if mint_b == fees.NATIVE_MINT else 0.0
    sol_before = (bal.get('sol') or 0.0) - (native_fee or 0.0)
    # The harvest has landed, so the wallet holds every fee it names. A fee
    # larger than the wallet is a bad read, not income: on 2026-09-27 a
    # $6,237 claim on a $230 position put sol_before at -23 SOL and booked
    # $2,830 as gas and $3,408 as reinvested. Split nothing; the fees stay in
    # the wallet.
    if sol_before < 0 or (fee_a or 0.0) > float(bal['balanceA'] or 0.0) + 1e-9 \
            or (fee_b or 0.0) > float(bal['balanceB'] or 0.0) + 1e-9:
        books.notify('payout_skipped', reason=f'fees {fee_a} {sym_a} + {fee_b} {sym_b} exceed the LP wallet; '
                                        'the fee read is wrong, nothing split')
        return None
    parts = fees.split([(mint_a, sym_a, fee_a, px_a), (mint_b, sym_b, fee_b, px_b)],
                       wallets.norm(config.PAYOUT_MINT), sol_before, config.GAS_RESERVE_SOL)
    owed = state.setdefault('payout_owed', {})
    held = {mint_a: bal['balanceA'], mint_b: bal['balanceB']}
    sent = []
    pin_ok = capital.profit_wallet_pinned()
    for p in parts:
        if p['kind'] != 'paid':
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], p['amount'], p['usd'], p['kind'],
                             detail=('gas under the reserve' if p['kind'] == 'gas' else None))
            continue
        due = p['amount'] + float(owed.get(p['mint'], 0.0))
        amt = min(due, float(held.get(p['mint']) or 0.0))
        px = p['usd'] / p['amount'] if p['usd'] is not None and p['amount'] else None
        if amt <= 0:
            continue
        if not pin_ok:
            # The destination in the database does not match the address
            # pinned in the service environment: pay nothing, keep it owed.
            owed[p['mint']] = due
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], p['amount'],
                             p['usd'], 'owed', to_address=config.PROFIT_WALLET,
                             detail='profit wallet does not match LPBOT_PROFIT_WALLET_PIN')
            books.notify('payout_refused', reason='profit_wallet in the database does not match the pinned address',
                         symbol=p['symbol'], owed=round(due, 6))
            continue
        out, err = signers.chain('send', p['mint'], f'{amt:.9f}', config.PROFIT_WALLET, '--execute', dex=signers.route('payout'))
        if out and out.get('signature') and not err:
            rest = due - amt
            if rest > 1e-9:
                owed[p['mint']] = rest          # what the wallet could not cover this time
            else:
                owed.pop(p['mint'], None)
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], amt,
                             amt * px if px else None, 'paid', to_address=config.PROFIT_WALLET,
                             signature=out['signature'])
            sent.append({'symbol': p['symbol'], 'amount': round(amt, 6),
                         'usd': round(amt * px, 4) if px else None, 'signature': out['signature']})
        elif (out or {}).get('signature') or (out or {}).get('partial') or \
                re.search(r'timed out|timeout|confirm', str(err), re.I):
            # It may have landed. Never owe it, or the next harvest sends it
            # twice (review, 2026-09-26). Recorded as uncertain for the book.
            owed.pop(p['mint'], None)
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], amt,
                             amt * px if px else None, 'uncertain', to_address=config.PROFIT_WALLET,
                             signature=(out or {}).get('signature'), detail=err)
            books.notify('payout_uncertain', reason=err, symbol=p['symbol'], amount=round(amt, 6),
                         signature=(out or {}).get('signature'))
        else:
            owed[p['mint']] = due
            db.record_payout(config.PROFILE, position, p['mint'], p['symbol'], amt,
                             amt * px if px else None, 'owed', to_address=config.PROFIT_WALLET,
                             detail=err or 'no signature')
            books.notify('payout_failed', reason=err or 'no signature', symbol=p['symbol'], owed=round(due, 6))
    paths.save(state)
    summary = {k: round(sum((x['usd'] or 0) for x in parts if x['kind'] == k), 4)
               for k in ('paid', 'reinvested', 'gas')}
    payout_mint = wallets.norm(config.PAYOUT_MINT)
    held = [{'symbol': x['symbol'], 'amount': round(x['amount'], 6),
             'usd': round(x['usd'], 4) if x['usd'] is not None else None}
            for x in parts if x['kind'] == 'reinvested' and x['mint'] == payout_mint]
    books.notify('PAYOUT', sent=sent, split=summary, gas_low=sol_before < config.GAS_RESERVE_SOL,
                 sol_before=round(sol_before, 6), gas_reserve=config.GAS_RESERVE_SOL, held=held, to=config.PROFIT_WALLET,
                 parts=[{k: (round(v, 6) if isinstance(v, float) else v) for k, v in x.items()} for x in parts])
    return parts


def measured_fees(out, status, a, b, usd):
    """What the harvest really took out of the pool, from its transactions
    (txfees.py): (a, b, usd). The status figures stand when the transactions
    cannot be read; a disagreement is reported. The status read is an
    estimate made before the harvest; the transaction is what happened.
    On a chain whose transactions txfees cannot read, the status figures
    stand, guarded as always."""
    if not config.CAPS.get('txfees'):
        why = signers.fee_problem(dict(status, feesAccruedA=a, feesAccruedB=b, feesAccrued_USD=usd))
        if why:
            s2 = signers.sanitised(status, why)
            return s2['feesAccruedA'], s2['feesAccruedB'], s2['feesAccrued_USD']
        return a, b, usd
    try:
        (mint_a, _), (mint_b, _) = capital.pool_tokens()
        sigs = (out or {}).get('signatures') or [(out or {}).get('signature')]
        m = txfees.harvested(config.RPC, sigs, status.get('whirlpool') or config.POOL, mint_a, mint_b)
    except Exception as e:
        m = None
        books.notify('harvest_unmeasured', reason=f'{type(e).__name__}: {books.tidy(e)}')
    if m is None:
        why = signers.fee_problem(dict(status, feesAccruedA=a, feesAccruedB=b, feesAccrued_USD=usd))
        if why:
            # Neither the transaction nor the status read can be trusted: the
            # last snapshot's figures, a lower bound, are booked instead.
            s2 = signers.sanitised(status, why)
            return s2['feesAccruedA'], s2['feesAccruedB'], s2['feesAccrued_USD']
        books.notify('harvest_unmeasured', reason='harvest transactions unreadable; the status figures stand')
        return a, b, usd
    # txfees measures raw / 10^decimals; the book, like every amount the
    # signers report, is in UI units (a Token-2022 scaled mint's multiplier).
    ma, mb = m[0] * float(status.get('multiplierA') or 1.0), m[1] * float(status.get('multiplierB') or 1.0)
    q = capital.quote_price(status)
    if q is None:
        # The amounts are the transaction's; their dollar value is not known.
        books.notify('harvest_measured', reported_usd=usd, measured_a=ma, measured_b=mb,
                     reason='quote price unknown: the status dollar figure stands')
        return ma, mb, usd
    musd = (ma * capital.ui_price(status) + mb) * q
    if abs(musd - (usd or 0.0)) > max(0.01, 0.05 * musd):
        books.notify('harvest_measured', reported_a=a, reported_b=b, reported_usd=usd,
                     measured_a=ma, measured_b=mb, measured_usd=round(musd, 6))
    return ma, mb, musd


def band_profile(mint, event, reason=None):
    """The band's lifetime profile (db.record_band_profile), after a harvest
    or the rebalance that ends it. Never blocks a move."""
    try:
        db.record_band_profile(mint, event, reason)
    except Exception as e:
        books.notify('band_profile_failed', reason=f'{type(e).__name__}: {books.tidy(e)}', mint=mint, band_event=event)


def dividend(state, status):
    """Harvest into the wallet and report it. The ledger counts it once: the
    snapshot after the harvest records the position's counter at zero."""
    mint = status['positionMint']
    a, b, usd = status.get('feesAccruedA', 0.0), status.get('feesAccruedB', 0.0), status.get('feesAccrued_USD', 0.0)
    out, err = signers.chain('harvest', mint, '--execute')
    state['last_harvest'] = time.time(); paths.save(state)
    if signers.held(err):
        return False                # said once by chain(); the next interval tries again
    if out and out.get('signature') and not err:
        a, b, usd = measured_fees(out, status, a, b, usd)
        db.record_harvest(mint, a, b, usd, out['signature'])
        db.snapshot(mint, capital.ui_price(status), status.get('inRange'), status.get('liquidity'),
                    0.0, 0.0, 0.0, capital.wallet(status['whirlpool']).get('walletUsd'), capital.position_usd(status))
        db.event('DIVIDEND', f'${usd:.4f} harvested to the wallet')
        band_profile(mint, 'harvest')
        books.notify_book('DIVIDEND', collected_usd=round(usd, 4), collected_a=a, collected_b=b,
                          signature=out['signature'])
        try:
            distribute(state, mint, a, b)
            distribute_rewards(state, mint, signers.signatures_of(out))
        except Exception as e:          # the split must never stop the loop
            books.notify('payout_failed', reason=f'{type(e).__name__}: {books.tidy(e)}')
        return True
    books.notify('harvest_skipped', reason=err or 'no signature returned', kind='dividend')
    return False


_REWARD_PX = {}                 # mint -> (fetched_at, usd): the last good price


def reward_prices(mints, max_age=tuning.REWARD_PRICE_MAX_AGE_S):
    """Jupiter prices for reward mints, falling back to the last good price
    (up to six hours old) when the free API rate-limits: a missing price
    valued PancakeSwap's CAKE at nothing in the first live ranking."""
    try:
        fresh = jupiter_api.jupiter_prices(mints)
    except Exception:
        fresh = {}
    now = time.time()
    for m, p in fresh.items():
        if p and p > 0:
            _REWARD_PX[m] = (now, float(p))
    return {m: _REWARD_PX[m][1] for m in mints if m in _REWARD_PX and now - _REWARD_PX[m][0] <= max_age}
