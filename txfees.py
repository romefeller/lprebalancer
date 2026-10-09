"""What a harvest actually delivered, from its own transactions.

A signer's status read says what the position has earned; the harvest
transaction's balance changes say what arrived in the wallet. The second is
the ground truth. On 2026-09-27 a status read claimed 23.16 SOL + 3,403 USDC
of fees on a $230 position; the harvest transaction moved 0.000771773 SOL +
0.086267 USDC. The ledger, the gas refill and the split now book the second
figure whenever the transactions can be read.

`parse` is pure: one jsonParsed getTransaction result in, what left the
pool's vaults of each mint out. `harvested` fetches and sums over every
signature of a harvest.
"""
import json
import time
import urllib.request

import chains

NATIVE_MINT = chains.SOLANA['native_mint']


def _raw(balances, holder, mint):
    """{account index: (raw amount, decimals)} of `holder`'s `mint` accounts."""
    out = {}
    for b in balances or []:
        if b.get('owner') == holder and b.get('mint') == mint:
            ui = b['uiTokenAmount']
            out[b['accountIndex']] = (int(ui['amount']), int(ui['decimals']))
    return out


def outflow(meta, holder, mint):
    """How much of `mint` left `holder`'s token accounts in one transaction,
    in tokens (negative when it gained)."""
    pre, post = _raw(meta.get('preTokenBalances'), holder, mint), _raw(meta.get('postTokenBalances'), holder, mint)
    if not pre and not post:
        return 0.0
    dec = next(d for _, d in list(pre.values()) + list(post.values()))
    return (sum(a for a, _ in pre.values()) - sum(a for a, _ in post.values())) / 10 ** dec


def parse(tx, pool, mint_a, mint_b):
    """(mint_a, mint_b) the harvest took out of the pool's vaults.

    Measured at the pool, not at the wallet: the wallet's SOL also pays the
    network fee and any priority tip, and a wrapped-SOL fee may arrive as
    lamports. The pool's vaults are token accounts the pool owns, so each
    fee is exactly the vault's decrease, native SOL included."""
    if tx is None or tx.get('meta') is None:
        raise ValueError('transaction not available')
    if tx['meta'].get('err') is not None:
        return 0.0, 0.0                               # a failed transaction moved nothing
    return outflow(tx['meta'], pool, mint_a), outflow(tx['meta'], pool, mint_b)


def fetch(rpc, signature, tries=5, pause=2.0, timeout=20):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'getTransaction',
                       'params': [signature, {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                              'maxSupportedTransactionVersion': 0}]}).encode()
    for k in range(tries):
        try:
            req = urllib.request.Request(rpc, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.load(r).get('result')
            if res is not None:
                return res
        except Exception:
            pass
        if k + 1 < tries:
            time.sleep(pause)
    return None


def harvested(rpc, signatures, pool, mint_a, mint_b, fetcher=None):
    """(a, b) the harvest took out of `pool`, summed over its signatures;
    None when any transaction cannot be read or a side comes out negative
    (the pool gained: not a harvest)."""
    sigs = [s for s in (signatures or []) if s]
    if not sigs or not pool:
        return None
    fetcher = fetcher or fetch                   # looked up at call time: tests replace it
    a = b = 0.0
    for s in sigs:
        tx = fetcher(rpc, s)
        try:                                          # parse refuses None and a tx without meta
            da, db_ = parse(tx, pool, mint_a, mint_b)
        except (KeyError, ValueError, IndexError, TypeError):
            return None
        a += da; b += db_
    # raw amounts are integers, so the sign is exact: no tolerance needed
    if a < 0 or b < 0:
        return None
    return a, b


def _ui(token_amount):
    ui = token_amount.get('uiAmountString')
    return float(ui) if ui not in (None, '') else int(token_amount['amount']) / 10 ** int(token_amount['decimals'])


def inflow(rpc, signatures, owner, mints, fetcher=None):
    """{mint: what `owner` received of it (UI units)} over a harvest's
    transactions: post less pre of its token accounts, never below 0. None
    when there is no signature or a transaction cannot be read: a reward is
    paid out only up to what the harvest is shown to have brought."""
    sigs = [s for s in (signatures or []) if s]
    if not sigs or not owner:
        return None
    fetcher = fetcher or fetch                   # looked up at call time: tests replace it
    got = {m: 0.0 for m in mints}
    for s in sigs:
        tx = fetcher(rpc, s)
        if tx is None or tx.get('meta') is None:
            return None
        meta = tx['meta']
        for m in mints:
            side = lambda key: sum(_ui(b['uiTokenAmount']) for b in meta.get(key) or []
                                   if b.get('owner') == owner and b.get('mint') == m)
            got[m] += side('postTokenBalances') - side('preTokenBalances')
    return {m: max(v, 0.0) for m, v in got.items()}
