"""Sleeves: how the profiles that share one wallet share its tokens.

One process runs one profile; several profiles may sign with one wallet. Each
must see only its own money, or two of them deploy the same dollar twice. The
rules (MULTI_DESIGN.md, "Shared tokens in one wallet"):

  * A mint that exactly one enabled profile of the wallet uses belongs wholly
    to it (SOL to sol-usdc, MU to mu-usdc).
  * A mint several profiles use (USDC) is split by claims. Profile P sees
    claim[P]; the mint's holder sees the wallet's balance less every other
    claim. The holder is the profile whose deposit mint it is, or else the
    wallet's residual owner, so a fresh deposit routes to the profile that
    deploys it, and a token nobody deposits (USDC) to the residual owner.
  * A claim moves only by what the claimant's own transactions moved: every
    write runs under the wallet's advisory lock, and the balance after less
    the balance before goes to the caller's claim, floored at 0. A claim that
    would go below 0 is an overdraw, reported, never booked.
  * A profile whose mints are not known yet (NULL in config.mints) may use any
    mint: no mint is anyone's alone while it is unknown (fail closed).
  * Every profile of the wallet counts, disabled ones too: a disabled
    profile's claim and mints stay its own until an operator moves them.
  * A profile whose pool holds no native token pays rent and fees from the
    native owner's sleeve. Its writes measure the native balance too, and
    the change is booked as internal_in / internal_out capital flows between
    the two (native_giver, internal_flows), so neither book shows the other's
    rent as a loss or a gain. The sleeves themselves still follow the wallet.
  * Balances are read at `confirmed` commitment, the commitment the signers
    confirm at, and a read counts only from the slot of the write it
    measures on (wallet_settle): a lagging node never books a stale figure.

The functions first are pure; `sleeve()` is what `rebalancer.wallet()`
returns. The database part keeps the claims (wallet_claims) and the lock.
"""
import json
import time
import urllib.request
from contextlib import contextmanager

import psycopg2

import db

# Advisory locks take two int4 keys: this namespace and hashtext(wallet id).
LOCK_NAMESPACE = 0x4C50          # 'LP'
# A waiter gives up after this long. Longer than one signer call (420 s)
# plus its two balance reads, so only a stuck holder makes a waiter give up.
LOCK_WAIT_S = 480
LOCK_POLL_S = 0.25
DUST = 1e-12                     # human units: float noise, not money


class LockError(RuntimeError):
    """The wallet's lock could not be taken: nothing may be sent."""


class LockTimeout(LockError):
    """The wallet's lock stayed busy past the bounded wait."""


def norm(mint):
    """The mint as compared and stored: EVM addresses in lower case (their
    checksum case varies by source), Solana addresses as they are. Pure."""
    return mint.lower() if isinstance(mint, str) and mint.startswith('0x') else mint


# --- pure: who owns what -------------------------------------------------------------

def users(mint, profiles):
    """Names of the profiles that use `mint`: its pool holds it, or it is the
    profile's deposit mint. Pure."""
    m = norm(mint)
    return {p['name'] for p in profiles
            if m in {norm(x) for x in (p.get('mints') or [])} or norm(p.get('deposit_mint')) == m}


def holder(mint, profiles):
    """The profile that holds the residual of `mint`: the one whose deposit
    mint it is, else the wallet's residual owner, else None. Pure."""
    m = norm(mint)
    dep = sorted(p['name'] for p in profiles if norm(p.get('deposit_mint')) == m)
    if dep:
        return dep[0]
    res = sorted(p['name'] for p in profiles if p.get('residual_owner'))
    return res[0] if res else None


def sole_owner(mint, profiles):
    """The one profile `mint` belongs to wholly, or None when it is shared or
    a profile with unknown mints could use it. Pure."""
    u = users(mint, profiles)
    unknown = {p['name'] for p in profiles if p.get('mints') is None} - u
    return next(iter(u)) if len(u) == 1 and not unknown else None


def split(total, mint, profiles, claims):
    """Every profile's share of `total` (the wallet's balance of `mint`).
    Returns (views {name: amount}, overdraw, unassigned). Pure.

    With a holder, the views partition the total: none negative, and their
    sum is the total. Claims larger than the total (an overdraw: a claim
    booked on a balance that has since gone) are scaled down pro rata and
    the holder sees nothing; `overdraw` is by how much. Without a holder the
    remainder is `unassigned`."""
    total = max(float(total or 0.0), 0.0)
    names = [p['name'] for p in profiles]
    views = {n: 0.0 for n in names}
    own = sole_owner(mint, profiles)
    if own is not None:
        views[own] = total
        return views, 0.0, 0.0
    h = holder(mint, profiles)
    m = norm(mint)
    want = {n: max(float((claims.get(n) or {}).get(m) or 0.0), 0.0) for n in names if n != h}
    s = sum(want.values())
    if s <= total:
        views.update(want)
        rest = total - s
        if h is None:
            return views, 0.0, rest
        views[h] = rest
        return views, 0.0, 0.0
    scale = total / s
    views.update({n: w * scale for n, w in want.items()})
    if h is not None:
        views[h] = 0.0
    return views, s - total, 0.0


def claim_after(claim, delta):
    """A claim after a transaction moved `delta` of its mint: (new claim,
    overdraw). Never below 0; what would have gone below is the overdraw.
    Pure."""
    v = float(claim or 0.0) + float(delta or 0.0)
    return (v, 0.0) if v >= 0 else (0.0, -v)


def claimed_mints(name, mints, profiles):
    """The mints of `name`'s pool whose movements go to its claim: shared
    ones it does not hold. Its own mints and the ones it holds the residual
    of need no claim; its view of them follows the wallet. Pure."""
    out = []
    for m in mints:
        if sole_owner(m, profiles) is None and holder(m, profiles) != name and norm(m) not in out:
            out.append(norm(m))
    return out


def native_giver(name, mints, profiles, native_mint):
    """The profile whose native token `name`'s writes spend, or None. A
    profile whose pool holds no native token pays its rent and fees from the
    native owner's sleeve (sole owner, else holder), and a close refunds that
    rent there; each write's native change is booked between the two as
    internal flows (internal_flows). None when `name` is that owner, when its
    own pool holds the native token (its sleeve or its claim moves), or when
    no owner is known. Pure."""
    if norm(native_mint) in {norm(m) for m in mints}:
        return None
    giver = sole_owner(native_mint, profiles) or holder(native_mint, profiles)
    return giver if giver not in (None, name) else None


def internal_flows(taker, giver, delta, price, profiles, native_mint, detail):
    """The two capital_flows rows for a write of `taker` that moved `delta`
    (after - before, human units) of `giver`'s native token, priced at
    `price` USD per native unit. Spent (delta < 0): `giver` has an
    'internal_out', `taker` an 'internal_in'. Refunded (delta > 0): the
    reverse. Both carry the same positive amount. Each row's amounts name its
    profile's token A too (0 when it is not the native token), so since_start
    never reads the native amount as token A. An unknown price books 0 USD
    and says so in `detail`. [] for no movement. Pure."""
    x = abs(float(delta or 0.0))
    if x <= DUST:
        return []
    out_p, in_p = (giver, taker) if delta < 0 else (taker, giver)
    usd = round(x * float(price), 6) if price is not None else 0.0
    if price is None:
        detail = f'{detail}; price unknown, booked at 0 USD'
    nm = norm(native_mint)
    rows = []
    for kind, who in (('internal_out', out_p), ('internal_in', in_p)):
        row = next((p for p in profiles if p['name'] == who), {})
        mint_a = norm((row.get('mints') or [None])[0])
        amounts = {nm: x}
        if mint_a and mint_a != nm:
            amounts[mint_a] = 0.0
        rows.append({'kind': kind, 'profile': who, 'sol': x if mint_a == nm else 0.0, 'usd': usd,
                     'price': price, 'amounts': amounts, 'detail': detail})
    return rows


def with_self(profiles, me):
    """The wallet's profiles with this process's own row (its live mints)
    in place of the stored one: the process knows its pool's mints before
    the table does. Pure."""
    return [p for p in profiles if p['name'] != me['name']] + [me]


def sleeve(bal, name, mint_a, mint_b, profiles, claims, native_mint):
    """The balance read `bal` (the signer's 'balance' answer) as profile
    `name` sees it: balanceA, balanceB and walletUsd are its sleeve; the
    wallet's own figures stay under rawBalanceA, rawBalanceB, rawWalletUsd.
    `sleeveOverdraw` is set when the claims on one of its mints exceed the
    wallet. Native value outside the pool's two tokens (gas, when neither
    token is native) counts for the profile that owns the native mint, or
    the residual owner when none uses it. Pure."""
    out = dict(bal)
    raw_a, raw_b = float(bal.get('balanceA') or 0.0), float(bal.get('balanceB') or 0.0)
    va, over_a, _ = split(raw_a, mint_a, profiles, claims)
    vb, over_b, _ = split(raw_b, mint_b, profiles, claims)
    a, b = va.get(name, 0.0), vb.get(name, 0.0)
    out.update(rawBalanceA=bal.get('balanceA'), rawBalanceB=bal.get('balanceB'), rawWalletUsd=bal.get('walletUsd'),
               balanceA=a, balanceB=b)
    if over_a > DUST or over_b > DUST:
        out['sleeveOverdraw'] = {norm(mint_a): over_a, norm(mint_b): over_b}
    native_owner = sole_owner(native_mint, profiles) or holder(native_mint, profiles)
    keep_native = bal.get('nativeSide') is not None or native_owner == name
    w, q, px = bal.get('walletUsd'), bal.get('quoteUsd'), bal.get('price')
    if a == raw_a and b == raw_b and keep_native:
        return out                                     # nothing is anyone else's: the read as it is
    if w is None or q is None or px is None:
        out['walletUsd'] = None
        return out
    pool_raw = (raw_a * float(px) + raw_b) * float(q)
    native_usd = float(w) - pool_raw if keep_native else 0.0
    out['walletUsd'] = round((a * float(px) + b) * float(q) + native_usd, 4)
    return out


def sleeve_caps(view, mint_a, mint_b):
    """LPBOT_SLEEVE for the swap scripts: {mint: the most it may sell from}.
    Pure."""
    return {norm(mint_a): max(float(view.get('balanceA') or 0.0), 0.0),
            norm(mint_b): max(float(view.get('balanceB') or 0.0), 0.0)}


# --- the database: claims, mints, the lock ------------------------------------------------

def wallet_profiles(wallet_id):
    """Every profile of `wallet_id`, disabled ones too (their claims and mints
    stay theirs): name, venue, mints, deposit mint, residual owner, enabled."""
    with db.cursor() as cur:
        cur.execute('select name, dex, mints, deposit_mint, residual_owner, enabled from config '
                    'where wallet_id = %s order by name', (wallet_id,))
        return [dict(r, mints=list(r['mints']) if r['mints'] is not None else None) for r in cur.fetchall()]


def register_mints(profile, mints):
    """Record the mints of `profile`'s pool (A, then B)."""
    with db.cursor(commit=True) as cur:
        cur.execute('update config set mints = %s where name = %s', ([norm(m) for m in mints], profile))


def claims(wallet_id):
    """{profile: {mint: amount}} of `wallet_id`."""
    with db.cursor() as cur:
        cur.execute('select profile, mint, amount from wallet_claims where wallet_id = %s', (wallet_id,))
        out = {}
        for r in cur.fetchall():
            out.setdefault(r['profile'], {})[r['mint']] = float(r['amount'])
    return out


def _adjust(cur, wallet_id, profile, deltas):
    out = {}
    for mint in sorted(deltas):
        m = norm(mint)
        cur.execute('insert into wallet_claims (wallet_id, profile, mint, amount) values (%s,%s,%s,0) '
                    'on conflict do nothing', (wallet_id, profile, m))
        cur.execute('select amount from wallet_claims where wallet_id = %s and profile = %s and mint = %s '
                    'for update', (wallet_id, profile, m))
        new, over = claim_after(float(cur.fetchone()['amount']), deltas[mint])
        cur.execute('update wallet_claims set amount = %s, updated_at = now() '
                    'where wallet_id = %s and profile = %s and mint = %s', (new, wallet_id, profile, m))
        out[m] = (new, over)
    return out


def adjust(wallet_id, profile, deltas):
    """Add each {mint: delta} to `profile`'s claims, floored at 0, in one
    transaction (the rows locked while they change). Returns {mint: (new
    claim, overdraw)}."""
    with db.cursor(commit=True) as cur:
        return _adjust(cur, wallet_id, profile, deltas)


def settle_state(wallet_id):
    """(slot, pending) of `wallet_id`: every write up to `slot` is booked;
    `pending` is the write sent and not yet booked, or None."""
    with db.cursor() as cur:
        cur.execute('select slot, pending from wallet_settle where wallet_id = %s', (wallet_id,))
        r = cur.fetchone()
    return (int(r['slot']), r['pending']) if r else (0, None)


def set_pending(wallet_id, pending):
    """Record the write about to be sent (or, with its signatures, just sent).
    None clears it."""
    with db.cursor(commit=True) as cur:
        cur.execute('insert into wallet_settle (wallet_id, pending) values (%s, %s) on conflict (wallet_id) '
                    'do update set pending = excluded.pending, updated_at = now()',
                    (wallet_id, json.dumps(pending) if pending is not None else None))


def book(wallet_id, profile, deltas, slot, flows=()):
    """Book a measured write in one transaction: its deltas to `profile`'s
    claims, its internal native flows (internal_flows rows), the wallet's
    settled slot, the pending write cleared. A crash books all of it or none.
    Returns adjust()'s {mint: (new claim, overdraw)}."""
    with db.cursor(commit=True) as cur:
        out = _adjust(cur, wallet_id, profile, deltas)
        for f in flows:
            cur.execute('insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, amounts, '
                        'wallet_id, profile) values (now(), %s, %s, 0, %s, %s, null, %s, %s, %s, %s)',
                        (f['kind'], f['sol'], f['usd'], f['price'], f['detail'], json.dumps(f['amounts']),
                         wallet_id, f['profile']))
        cur.execute('insert into wallet_settle (wallet_id, slot) values (%s, %s) on conflict (wallet_id) '
                    'do update set slot = greatest(wallet_settle.slot, excluded.slot), pending = null, '
                    'updated_at = now()', (wallet_id, slot))
    return out


@contextmanager
def wallet_lock(wallet_id, wait_s=None, poll_s=None):
    """Hold the wallet's advisory lock for the block. Its own connection, in
    autocommit, so no transaction of the block holds it and a crash (the
    session ends) releases it. Polls pg_try_advisory_lock and raises
    LockTimeout after `wait_s`; never sleeps while holding it. Released on
    every exit path. A database that cannot be reached raises LockError.
    The waits default to LOCK_WAIT_S and LOCK_POLL_S."""
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    poll_s = LOCK_POLL_S if poll_s is None else poll_s
    try:
        con = psycopg2.connect(db.DSN)
    except psycopg2.Error as e:
        raise LockError(f'wallet {wallet_id} lock unavailable: {type(e).__name__}') from None
    try:
        try:
            con.autocommit = True
            deadline = time.monotonic() + wait_s
            with con.cursor() as cur:
                while True:
                    cur.execute('select pg_try_advisory_lock(%s, hashtext(%s))', (LOCK_NAMESPACE, wallet_id))
                    if cur.fetchone()[0]:
                        break
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f'wallet {wallet_id} lock busy for {wait_s:.0f} s')
                    time.sleep(poll_s)
        except psycopg2.Error as e:
            raise LockError(f'wallet {wallet_id} lock unavailable: {type(e).__name__}') from None
        try:
            yield
        finally:
            with con.cursor() as cur:
                cur.execute('select pg_advisory_unlock(%s, hashtext(%s))', (LOCK_NAMESPACE, wallet_id))
    finally:
        con.close()


# --- balance reads: one RPC call per mint, no signer ---------------------------------------

def _rpc(url, method, params, timeout):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.load(r)
    if 'result' not in res:
        raise RuntimeError(f'{method}: no result')
    return res['result']


COMMITMENT = {'commitment': 'confirmed'}         # what the signers confirm at; finalized lags ~32 slots


def _solana_balance(url, owner, mint, native_mint, timeout, at=None):
    """(human balance, slot of the read) at confirmed commitment."""
    if mint == native_mint:
        r = _rpc(url, 'getBalance', [owner, COMMITMENT], timeout)
        return r['value'] / 1e9, int(r['context']['slot'])
    r = _rpc(url, 'getTokenAccountsByOwner', [owner, {'mint': mint}, dict(COMMITMENT, encoding='jsonParsed')], timeout)
    return (sum(float(a['account']['data']['parsed']['info']['tokenAmount']['uiAmountString']) for a in r['value']),
            int(r['context']['slot']))


def _evm_call(url, to, data, timeout, block):
    return int(_rpc(url, 'eth_call', [{'to': to, 'data': data}, hex(block)], timeout), 16)


def _evm_balance(url, owner, mint, native_mint, timeout, at):
    """(ERC-20 balanceOf in human units, block) at block `at`; the native
    mint counts native ETH too (the EVM signer reports native ETH as WETH)."""
    dec = _evm_call(url, mint, '0x313ce567', timeout, at)                       # decimals()
    raw = _evm_call(url, mint, '0x70a08231' + owner[2:].lower().rjust(64, '0'), timeout, at)   # balanceOf(owner)
    human = raw / 10 ** dec
    if mint == native_mint:
        human += int(_rpc(url, 'eth_getBalance', [owner, hex(at)], timeout), 16) / 1e18
    return human, at


def _evm_head(url, timeout):
    return int(_rpc(url, 'eth_blockNumber', [], timeout), 16)


READERS = {'solana': _solana_balance, 'base': _evm_balance}
# The slot (block) every read of one measurement is pinned to: EVM reads
# name a block; Solana reads report theirs.
HEADS = {'base': _evm_head}


def read_balances(chain, url, owner, mints, native_mint, timeout=10, tries=2):
    """({mint: human balance}, slot) of `owner`, or None when any read fails.
    `slot` is the oldest slot any of the reads saw (the block they are pinned
    to on EVM). The caller never guesses: a failed read leaves the claim as
    it was."""
    reader = READERS.get(chain)
    if reader is None or not owner:
        return None
    out, slots = {}, []
    for k in range(tries):
        try:
            at = HEADS[chain](url, timeout) if chain in HEADS else None
            break
        except Exception:
            if k + 1 == tries:
                return None
            time.sleep(1.0)
    for m in mints:
        for k in range(tries):
            try:
                out[m], slot = reader(url, owner, m, norm(native_mint), timeout, at)
                slots.append(slot)
                break
            except Exception:
                if k + 1 == tries:
                    return None
                time.sleep(1.0)
    return out, (min(slots) if slots else at or 0)


def _solana_write_slot(url, signatures, timeout):
    r = _rpc(url, 'getSignatureStatuses', [list(signatures), {'searchTransactionHistory': True}], timeout)
    slots = []
    for st in r['value']:
        # a failed transaction landed too (its fee is the change): its slot counts
        if not st or st.get('confirmationStatus') not in ('confirmed', 'finalized'):
            return None
        slots.append(int(st['slot']))
    return max(slots)


def _evm_write_slot(url, signatures, timeout):
    slots = []
    for h in signatures:
        rc = _rpc(url, 'eth_getTransactionReceipt', [h], timeout)
        if not rc or rc.get('blockNumber') is None:
            return None
        slots.append(int(rc['blockNumber'], 16))
    return max(slots)


WRITE_SLOTS = {'solana': _solana_write_slot, 'base': _evm_write_slot}


def write_slot(chain, url, signatures, timeout=10):
    """The slot (block) at which every one of `signatures` is confirmed, or
    None while one is unknown or unconfirmed, or the read fails."""
    try:
        return WRITE_SLOTS[chain](url, signatures, timeout) if signatures else None
    except Exception:
        return None
