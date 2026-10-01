"""Audits: the ledger reconciled against the chain, every hour.

Each check is a pure function of what it is given (the wallet as the chain
reports it, rows from the ledger, transactions) and returns (status, detail)
with status 'ok', 'warn' or 'fail'. `run` gathers the inputs, runs every
check, records each result in rebalancer.audits, keeps cursors and derived
values in audit_state, and notifies only when a check's status changes, so
the feed carries a new problem once, not every hour.

Checks (2026-09-28, the owner: "put audits on everything"):
  idle          money the wallet could deploy but does not (deploy-all)
  gas           native SOL under the reserve: the bot could not pay fees
  equity        the chain's total holdings against the last snapshot's equity;
                keeps `uncounted_usd` (rent in empty accounts, reward dust)
  flows         every new LP-wallet transaction classified: the bot's own,
                dust spam, a deposit or withdrawal (recorded in capital_flows),
                or a bot operation the ledger never recorded
  harvests      each new harvest row against its transaction's pool outflow
  payouts       each new paid row against the USDC that reached the profit wallet
  positions     open positions in the ledger against position NFTs in the wallet
  band_profile  every closed band has a final profile (written if missing)
  owed          payouts owed for more than three days
  empty         empty token accounts whose rent the janitor can reclaim
  fee_reads     status reads the plausibility guard rejected in the last day

One wallet, many profiles (MULTI_DESIGN.md): the audits run in the wallet's
residual owner only, and `run(..., wallet=book)` reconciles the WHOLE wallet
against the sum of its profiles: every profile's tokens are equity, every
profile's open position is counted, every profile's rows are checked.
Without a book (a pre-020 profile) the ledger is one book, as before.
"""
import json
import time
import urllib.request
import uuid

import engine
import wallets

NATIVE = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN_PROGRAMS = ('TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA', 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb')
# Programs of the bot's own operations: a transaction that calls one and is
# signed by the wallet is the bot's, recorded or not.
BOT_PROGRAMS = {
    'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK': 'raydium',
    'whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc': 'orca',
    'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo': 'meteora',
    'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4': 'jupiter',
    'REALQqNEomY6cQGZJUGwywTBD2UmDT32rZcNnfxQ5N2': 'byreal',
    'HpNfyc2Saw7RKkQd8nEL4khUcuPhQ7WwY1B2qjx8jxFq': 'pancake',
}
DUST_LAMPORTS = 10_000          # a spam transfer moves a few lamports, never more
DUST_USDC = 0.01                # an unsigned USDC transfer in of at most this is spam, not capital


def lookalike(addr, known):
    """Whether `addr` imitates one of `known`: the same first four and last
    three characters (58^-7: no chance collision), not the same address. Address poisoning (2026-09-30:
    8funvFQK...D1h imitating the profit wallet 8funmDkP...D1h) sends dust
    after each payout, hoping the owner copies the address from the history.
    Pure."""
    a = str(addr or '')
    return any(a != k and len(a) > 8 and a[:4] == k[:4] and a[-3:] == k[-3:] for k in known if k)
IDLE_ABS_USD = 2.0              # idle above max($2, 2% of equity) is a warning,
IDLE_SHARE = 0.02
IDLE_FAIL_SHARE = 0.10          # above 10% a failure
EQUITY_TOLERANCE_USD = 1.0
HARVEST_TOLERANCE = 1e-9         # tokens; float noise only
PAYOUT_TOLERANCE = 1e-6          # tokens; USDC has six decimals
OWED_DAYS = 3


# --- the checks, pure ----------------------------------------------------------------

def check_idle(deployable_usd, equity_usd, position_open):
    """Deploy-all: with a position open, what the wallet could still deploy."""
    if not position_open:
        return 'ok', {'note': 'no position open', 'deployable_usd': round(deployable_usd, 4)}
    limit = max(IDLE_ABS_USD, IDLE_SHARE * equity_usd)
    d = {'deployable_usd': round(deployable_usd, 4), 'limit_usd': round(limit, 4)}
    if equity_usd > 0 and deployable_usd > IDLE_FAIL_SHARE * equity_usd:
        return 'fail', d
    return ('warn', d) if deployable_usd > limit else ('ok', d)


def check_gas(native_sol, reserve_sol):
    d = {'native_sol': native_sol, 'reserve_sol': reserve_sol}
    return ('fail', d) if native_sol < reserve_sol else ('ok', d)


def check_equity(chain_total_usd, snapshot_equity_usd, uncounted_usd):
    """The chain's holdings less what equity is known not to count, against
    the last snapshot's equity."""
    if snapshot_equity_usd is None:
        return 'warn', {'note': 'no snapshot with equity'}
    diff = chain_total_usd - uncounted_usd - snapshot_equity_usd
    d = {'chain_total_usd': round(chain_total_usd, 4), 'uncounted_usd': round(uncounted_usd, 4),
         'snapshot_equity_usd': round(snapshot_equity_usd, 4), 'diff_usd': round(diff, 4)}
    return ('warn', d) if abs(diff) > EQUITY_TOLERANCE_USD else ('ok', d)


def classify_tx(tx, owner, watch=(), capital=()):
    """What one wallet transaction was: ('failed'|'poison'|'dust'|'bot'|
    'deposit'|'withdrawal'|'other', detail). `tx` is jsonParsed; the runner
    counts known signatures before it fetches, so none reaches here.
    `watch` are addresses an attacker may imitate (the owner, the profit
    wallet): an unsigned transaction paid by a lookalike is 'poison'. Neither
    poison nor dust is capital. `capital` are the mints, beyond SOL and USDC,
    that are money in this wallet (the tokens of its profiles' pools): their
    movements are flows, in `amounts` (human units), not other tokens."""
    m = tx['meta']
    keys = [k['pubkey'] if isinstance(k, dict) else k for k in tx['transaction']['message']['accountKeys']]
    if m.get('err') is not None:
        return 'failed', {}
    signer = keys[:1] == [owner]
    lam = 0
    if owner in keys:
        i = keys.index(owner)
        lam = m['postBalances'][i] - m['preBalances'][i] + (m.get('fee', 0) if signer else 0)
    def tok(mint):
        pre = sum(int(b['uiTokenAmount']['amount']) for b in m.get('preTokenBalances') or []
                  if b.get('owner') == owner and b.get('mint') == mint)
        post = sum(int(b['uiTokenAmount']['amount']) for b in m.get('postTokenBalances') or []
                   if b.get('owner') == owner and b.get('mint') == mint)
        return post - pre
    mints = {b.get('mint') for b in (m.get('preTokenBalances') or []) + (m.get('postTokenBalances') or [])
             if b.get('owner') == owner}
    def ui(mint):
        side = lambda key: sum(ui_amount(b['uiTokenAmount']) for b in m.get(key) or []
                               if b.get('owner') == owner and b.get('mint') == mint)
        return side('postTokenBalances') - side('preTokenBalances')
    moves = {mt: tok(mt) for mt in mints if tok(mt) != 0}
    wsol = moves.pop(NATIVE, 0)
    sol = lam / 1e9 + wsol / 1e9
    usdc = moves.pop(USDC, 0) / 1e6
    extra = {mt: ui(mt) for mt in sorted(moves) if mt in set(capital)}
    for mt in extra:
        moves.pop(mt)
    progs = sorted({BOT_PROGRAMS[k] for k in keys if k in BOT_PROGRAMS})
    d = {'sol': round(sol, 9), 'usdc': round(usdc, 6), 'programs': progs, 'other_tokens': moves, 'signer': signer}
    if extra:
        d['amounts'] = extra
    if not signer and lookalike(keys[0] if keys else None, [owner, *watch]):
        d['lookalike_of'] = next(k for k in [owner, *watch] if lookalike(keys[0], [k]))
        d['sender'] = keys[0]
        return 'poison', d
    # wrapped SOL is SOL: 0.5 wSOL sent in is a deposit, not dust
    if not signer and abs(lam + wsol) <= DUST_LAMPORTS and not moves and not extra and 0 <= usdc <= DUST_USDC:
        return 'dust', d
    if signer and progs:
        return 'bot', d
    # A flow moves money one way only: in (a deposit) or out (a withdrawal).
    # SOL and USDC in opposite directions is a swap-like movement, not a flow.
    if not progs:                     # other tokens (dust, airdrops) may ride along; they are noted
        flow = [sol, usdc, *extra.values()]
        if all(x >= 0 for x in flow) and any(x > 0 for x in flow):
            return 'deposit', d
        if all(x <= 0 for x in flow) and any(x < 0 for x in flow):
            return 'withdrawal', d
    return 'other', d


def flow_owner(d, profiles):
    """The profile a deposit or withdrawal belongs to (d: classify_tx's
    detail): the one its token routes to (wallets.holder: the profile whose
    deposit mint it is, else the residual owner), by the first token moved
    that is not USDC; USDC alone is the residual owner's. Pure."""
    moved = ([NATIVE] if d.get('sol') else []) + sorted(d.get('amounts') or {})
    return wallets.holder(moved[0] if moved else USDC, profiles)


def check_flows(classified):
    """Summary of a batch of classified transactions: deposits and
    withdrawals are recorded by the runner and warned; unrecorded bot
    operations and anything unexplained are warned."""
    counts = {}
    for kind, _ in classified:
        counts[kind] = counts.get(kind, 0) + 1
    flagged = [(k, d) for k, d in classified if k in ('deposit', 'withdrawal', 'bot', 'other')]
    # Address poisoning is not capital, but the owner must hear of it: never
    # copy an address from the wallet history.
    poison = sorted({(d.get('sender'), d.get('lookalike_of')) for k, d in classified if k == 'poison'})
    status = 'warn' if flagged or poison else 'ok'
    out = {'counts': counts, 'flagged': flagged[:10]}
    if poison:
        out['poison'] = [{'sender': a, 'imitates': b} for a, b in poison]
    return status, out


def check_harvest(row_a, row_b, measured):
    """A harvest row against its transaction's pool outflow (txfees), both in
    UI units (the runner scales a Token-2022 mint's outflow)."""
    if measured is None:
        return 'warn', {'note': 'transaction unreadable'}
    ma, mb = measured
    d = {'row': [row_a, row_b], 'tx': [ma, mb]}
    # rows are booked from the same transaction (txfees), times the same
    # multiplier, so they match it exactly
    ok = abs(ma - row_a) <= HARVEST_TOLERANCE and abs(mb - row_b) <= HARVEST_TOLERANCE
    return ('ok', d) if ok else ('fail', d)


def payout_received(tx, profit_wallet, mint, amount):
    """Whether a payout tx moved `amount` of `mint` into the profit wallet."""
    if tx is None or tx.get('meta') is None or tx['meta'].get('err') is not None:
        return False
    m = tx['meta']
    def total(key):
        return sum(ui_amount(b['uiTokenAmount'])
                   for b in m.get(key) or [] if b.get('owner') == profit_wallet and b.get('mint') == mint)
    got = total('postTokenBalances') - total('preTokenBalances')
    return abs(got - amount) <= PAYOUT_TOLERANCE


DLMM_PROGRAM = 'LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo'


def check_positions(db_open_mints, chain_nft_mints, db_open_dexes, dlmm_live, db_open_profiles=None):
    """Open positions in the ledger against the chain: the position NFTs the
    wallet holds, and for Meteora (positions are accounts, not NFTs) the
    position accounts that exist and belong to the DLMM program (`dlmm_live`).
    A DLMM position the ledger does not know cannot be found without an
    indexed read: orphans are NFT-only. Each profile holds at most one
    position; `db_open_profiles` names each row's profile (None: one book)."""
    nft = set(chain_nft_mints)
    rows = list(zip(db_open_mints, db_open_dexes))
    missing = [m for m, dx in rows if dx != 'meteora-dlmm' and m not in nft] + \
              [m for m, dx in rows if dx == 'meteora-dlmm' and m not in set(dlmm_live)]
    orphans = [m for m in nft if m not in set(db_open_mints)]
    d = {'db_open': list(db_open_mints), 'nfts': sorted(nft), 'missing_on_chain': missing, 'orphans': orphans}
    if db_open_profiles is None:
        doubled = len(db_open_mints) > 1
    else:
        books = list(db_open_profiles)
        doubled = sorted({str(b) for b in books if books.count(b) > 1})
        if doubled:
            d['more_than_one'] = doubled
    if missing or orphans or doubled:
        return 'fail', d
    return 'ok', d


def ui_amount(token_amount):
    """A jsonParsed tokenAmount in UI units: the RPC's uiAmountString, which
    carries a Token-2022 scaled mint's multiplier; raw / 10^decimals when the
    string is absent. Pure."""
    ui = token_amount.get('uiAmountString')
    if ui not in (None, ''):
        return float(ui)
    return int(token_amount['amount']) / 10 ** int(token_amount['decimals'])


PLAIN_MINTS = {NATIVE, USDC}        # SPL Token mints: no multiplier to read


def mint_scale(url, mint, now=None):
    """The UI multiplier of `mint` now: a Token-2022 scaledUiAmountConfig's
    newMultiplier from its timestamp on, else its multiplier; 1.0 for a plain
    mint; None when the mint cannot be read."""
    if mint in PLAIN_MINTS:
        return 1.0
    r = rpc(url, 'getAccountInfo', [mint, {'encoding': 'jsonParsed'}])
    try:
        info = r['value']['data']['parsed']['info']
    except (TypeError, KeyError):
        return None
    for e in info.get('extensions') or []:
        if e.get('extension') == 'scaledUiAmountConfig':
            st = e['state']
            t = time.time() if now is None else now
            return float(st['newMultiplier'] if t >= float(st['newMultiplierEffectiveTimestamp']) else st['multiplier'])
    return 1.0


def check_owed(owed_rows_old):
    return ('warn', {'rows': owed_rows_old}) if owed_rows_old else ('ok', {})


def check_empty(closable_lamports, count):
    d = {'accounts': count, 'reclaimable_sol': closable_lamports / 1e9}
    return ('warn', d) if count else ('ok', d)


def check_fee_reads(rejected_24h):
    return ('warn', {'rejected_24h': rejected_24h}) if rejected_24h else ('ok', {'rejected_24h': 0})


# --- the runner -------------------------------------------------------------------------

def rpc(url, method, params, tries=4):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    for k in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)
            if 'result' in res:
                return res['result']
        except Exception:
            pass
        time.sleep(1.5 * (k + 1))
    return None


def token_accounts(url, owner):
    out = []
    for prog in TOKEN_PROGRAMS:
        r = rpc(url, 'getTokenAccountsByOwner', [owner, {'programId': prog}, {'encoding': 'jsonParsed'}])
        for a in (r or {}).get('value') or []:
            info = a['account']['data']['parsed']['info']
            out.append({'pubkey': a['pubkey'], 'program': prog, 'lamports': a['account']['lamports'],
                        'mint': info['mint'], 'amount': int(info['tokenAmount']['amount']),
                        'decimals': int(info['tokenAmount']['decimals']), 'ui': ui_amount(info['tokenAmount'])})
    return out


def human(account):
    """A token_accounts row in UI units (its uiAmountString when read)."""
    return account['ui'] if account.get('ui') is not None else account['amount'] / 10 ** account['decimals']


def known_signatures(db, feed_path):
    known = set()
    with db.cursor() as c:
        for q in ('select open_sig s from positions', 'select close_sig s from positions',
                  'select signature s from harvests', 'select signature s from payouts',
                  'select signature s from capital_flows'):
            c.execute(q)
            known |= {r['s'] for r in c.fetchall() if r['s']}
    try:
        with open(feed_path) as fh:
            for line in fh:
                if '"signature' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue                    # a JSON line that is no event: `["signature"]` crashed the check
                if r.get('signature'):
                    known.add(r['signature'])
                known |= {s for s in r.get('signatures') or [] if isinstance(s, str)}
                known |= {x.get('signature') for x in r.get('sent') or [] if isinstance(x, dict) and x.get('signature')}
    except OSError:
        pass
    return known


def idle_sleeves_usd(profiles, claims, balances, prices, holding):
    """Dollar value of the wallet money of the profiles that hold no position:
    no snapshot counts it, so the equity check adds it beside the snapshots.
    `balances` {mint: human} is the whole wallet; each mint is split as the
    sleeves split it (wallets.split); `holding` names the profiles with an
    open position. Pure."""
    total = 0.0
    for m, amt in balances.items():
        views, _over, _rest = wallets.split(amt, m, profiles, claims)
        total += sum(v for n, v in views.items() if n not in holding) * float(prices.get(m) or 0.0)
    return total


def run(bot, db, config, txfees, notify, now=None, wallet=None):
    """One audit run. `bot` is the rebalancer module (wallet, read_status,
    deployable_usd, pool_tokens, FEED, ROOT). `wallet` is the wallet's book
    (rebalancer.wallet_book: every profile of the wallet, their mints, the
    claims); None audits the ledger as one book (a pre-020 profile).
    Returns {check: status}."""
    run_id = uuid.uuid4().hex[:12]
    url = config.RPC
    results = {}
    names = [p['name'] for p in wallet['profiles']] if wallet else None
    # every profile of the wallet, disabled ones too: their sleeves are theirs
    everyone = list(wallet['profiles']) if wallet else []
    capital = set(wallet['mints']) if wallet else set()

    def record(name, status, detail):
        results[name] = status
        db.record_audit(run_id, name, status, detail)
        prev = db.audit_value(f'status:{name}')
        if status != prev:
            db.set_audit_value(f'status:{name}', status)
            if status != 'ok' or prev not in (None, 'ok'):
                notify('AUDIT', check=name, status=status, was=prev, detail=detail)

    def guarded(name, fn):
        try:
            record(name, *fn())
        except Exception as e:
            record(name, 'fail', {'error': f'{type(e).__name__}: {str(e)[:300]}'})

    bal = bot.wallet(config.POOL)
    status, _ = bot.read_status()
    open_ = bool(status and status.get('positionMint'))
    own_mint = status.get('positionMint') if open_ else None
    others_open = []                     # the wallet's other open positions, by their last snapshot
    with db.cursor() as c:
        if names is None:
            c.execute('select equity_usd from snapshots where equity_usd is not null order by ts desc, id desc limit 1')
            r = c.fetchone()
            snap_equity = float(r['equity_usd']) if r else None
        else:
            c.execute("""select distinct on (p.mint) p.mint, p.config_name, s.equity_usd, s.position_usd, s.accrued_usd
                         from positions p join snapshots s using (mint)
                         where p.closed_at is null and p.config_name = any(%s) and s.equity_usd is not null
                         order by p.mint, s.ts desc, s.id desc""", (names,))
            rows = c.fetchall()
            snap_equity = sum(float(r['equity_usd']) for r in rows) if rows else None
            others_open = [r for r in rows if r['mint'] != own_mint]
            c.execute('select distinct config_name from positions where closed_at is null and config_name = any(%s)',
                      (names,))
            holding = {r['config_name'] for r in c.fetchall()}
    owner = bal.get('owner')

    accts = token_accounts(url, owner) if owner else []
    (mint_a, _), (mint_b, _) = bot.pool_tokens()
    keep = keep_mints(bot, mint_a, mint_b, capital)
    # A quote price the signer could not give is unknown, except for a
    # stablecoin quote by mint: then the wallet is not valued at all.
    q = bal.get('quoteUsd')
    if q is None and engine.is_stable({'address': mint_b}):
        q = 1.0
    ours = {mint_a, mint_b} | capital              # every token a profile of the wallet uses

    def idle():
        # the pool's tokens beyond the gas reserve, plus every foreign token
        # the sweep would convert: all of it belongs in the LP
        others = [a['mint'] for a in accts if a['amount'] > 0 and a['mint'] not in ours]
        sweep = bot.plan_sweep(accts, ours, keep - ours - {NATIVE, USDC},
                               bot.dexes.jupiter_prices(others) if others else {},
                               {m: bot.dexes.jupiter_token(m) for m in others}) if others else []
        deployable = bot.deployable_usd(dict(bal, quoteUsd=q)) if q is not None else None
        if deployable is None:
            return 'warn', {'note': 'quote price unknown: idle money not valued'}
        # the open's tolerance leftover is by design (rebalancer.deploy_idle)
        base = (bot.load().get('idle_baseline') or {})
        excused = float(base.get('usd') or 0.0) if open_ and base.get('mint') == status.get('positionMint') else 0.0
        st, d = check_idle(max(deployable - excused, 0.0) + sum(p['usd'] for p in sweep), snap_equity or 0.0, open_)
        d['tolerance_leftover_usd'] = round(excused, 4)
        d['foreign_usd'] = round(sum(p['usd'] for p in sweep), 4)
        return st, d
    guarded('idle', idle)
    guarded('gas', lambda: check_gas(float(bal.get('sol') or 0.0), config.GAS_RESERVE_SOL))
    empty = [a for a in accts if a['amount'] == 0 and a['mint'] not in keep]

    def equity():
        # Independent of the signer's walletUsd: the native balance and every
        # token account, priced here, plus the positions' marks and accrual.
        # With a wallet book: every profile's tokens, every open position,
        # against the sum of the profiles' snapshots plus the sleeves of the
        # profiles that hold no position (idle_sleeves_usd).
        native = (rpc(url, 'getBalance', [owner]) or {}).get('value')
        if native is None:
            return 'warn', {'note': 'native balance unreadable'}
        if q is None:
            return 'warn', {'note': 'quote price unknown: the wallet cannot be valued'}
        px = {mint_a: bal['price'] * q, mint_b: q}
        extra = sorted(capital - set(px) - {NATIVE})
        if extra:
            px.update({m: p for m, p in bot.dexes.jupiter_prices(extra).items() if m in extra})
        sol_usd = px[NATIVE] if NATIVE in px else bot.dexes.jupiter_prices([NATIVE]).get(NATIVE, 0.0)
        # the profiles' SPL tokens are equity; wrapped SOL, reward dust and
        # other tokens are not counted by equity and go to `uncounted`
        pool_usd = sum(human(a) * px[a['mint']] for a in accts if a['mint'] in px and a['mint'] != NATIVE)
        others = [a for a in accts if a['amount'] > 0 and (a['mint'] not in px or a['mint'] == NATIVE)
                  and not (a['decimals'] == 0 and a['amount'] == 1)]            # position NFTs are in the position mark
        prices = dict(bot.dexes.jupiter_prices([a['mint'] for a in others if a['mint'] != NATIVE]) if others else {})
        prices[NATIVE] = sol_usd
        dust_usd = sum(human(a) * prices.get(a['mint'], 0.0) for a in others)
        rent_usd = sum(a['lamports'] for a in empty) / 1e9 * sol_usd
        uncounted = dust_usd + rent_usd
        db.set_audit_value('uncounted_usd', round(uncounted, 6))
        pos_usd = (bot.position_usd(status) or 0.0) if open_ else 0.0
        other_pos = sum(float(r['position_usd'] or 0.0) + float(r['accrued_usd'] or 0.0) for r in others_open)
        chain_total = native / 1e9 * sol_usd + pool_usd + pos_usd + other_pos + \
            float((status or {}).get('feesAccrued_USD') or 0.0) + uncounted
        books = snap_equity
        if names is not None:
            held = {}
            for a in accts:
                if a['mint'] in px and a['mint'] != NATIVE:
                    held[a['mint']] = held.get(a['mint'], 0.0) + human(a)
            held[NATIVE] = native / 1e9
            idle_usd = idle_sleeves_usd(everyone, wallet['claims'], held, dict(px, **{NATIVE: sol_usd}), holding)
            books = (snap_equity or 0.0) + idle_usd if (snap_equity is not None or idle_usd) else None
        st, d = check_equity(chain_total, books, uncounted)
        if names is not None:
            d['open_positions'] = len(others_open) + (1 if open_ else 0)
        return st, d
    guarded('equity', equity)

    def flows():
        if q is None:
            return 'warn', {'note': 'quote price unknown: flows wait for a price'}       # the cursor stays
        cursor = db.audit_value('flows_cursor')
        params = {'limit': 200}
        if cursor:
            params['until'] = cursor
        sigs = rpc(url, 'getSignaturesForAddress', [owner, params]) or []
        if not sigs:
            return 'ok', {'new': 0}
        known = known_signatures(db, bot.FEED)
        classified = []
        for s in sorted(sigs, key=lambda x: x.get('blockTime') or 0):
            if s['signature'] in known:
                classified.append(('known', {}))
                continue
            tx = txfees.fetch(url, s['signature'], tries=3)
            if tx is None:
                classified.append(('other', {'sig': s['signature'], 'note': 'unreadable'}))
                continue
            kind, d = classify_tx(tx, owner, watch=(config.PROFIT_WALLET,), capital=capital - {NATIVE, USDC})
            d['sig'] = s['signature']
            if kind in ('deposit', 'withdrawal'):
                px = bal['price'] * q
                moved = d.get('amounts') or {}
                tok_px = bot.dexes.jupiter_prices(sorted(moved)) if moved else {}
                usd = abs(d['sol']) * px + abs(d['usdc']) + sum(abs(v) * tok_px.get(m, 0.0) for m, v in moved.items())
                ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(tx.get('blockTime') or time.time()))
                detail = f"found by the flows audit; other tokens {d['other_tokens']}"
                if names is None:
                    db.record_flow(ts, kind, abs(d['sol']), abs(d['usdc']), usd, px, s['signature'], detail)
                else:
                    # the profile the token routes to, its token A amount and
                    # its price (UI units), USDC in `usdc`, SOL only for SOL
                    who = flow_owner(d, everyone) or db.CONTEXT['profile']     # no residual owner: this process
                    ma = (wallet['mints_of'].get(who) or [None])[0]
                    a = abs(d['sol']) if ma == NATIVE else abs(moved.get(ma, 0.0))
                    pa = px if ma == NATIVE else tok_px.get(ma)
                    db.record_flow(ts, kind, abs(d['sol']) if ma == NATIVE else 0.0, abs(d['usdc']), usd, pa,
                                   s['signature'], detail, amounts={ma: a, USDC: abs(d['usdc'])} if ma else
                                   {USDC: abs(d['usdc'])}, profile=who, wallet_id=config.WALLET_ID)
            classified.append((kind, d))
            time.sleep(0.3)
        db.set_audit_value('flows_cursor', max(sigs, key=lambda x: x.get('blockTime') or 0)['signature'])
        return check_flows(classified)
    guarded('flows', flows)

    def harvests():
        last = int(db.audit_value('harvest_cursor') or 0)
        with db.cursor() as c:
            c.execute("""select h.id, h.fee_a, h.fee_b, h.signature, p.pool, p.config_name
                         from harvests h join positions p using (mint)
                         where h.id > %s and h.signature not like 'close:%%'
                           and h.signature not in (select close_sig from positions where close_sig is not null)
                           and (%s::text[] is null or p.config_name = any(%s))
                         order by h.id limit 30""", (last, names, names))
            rows = c.fetchall()
        worst, details, scales = 'ok', [], {}
        for r in rows:
            a, b = (wallet['mints_of'].get(r['config_name']) if wallet else None) or (mint_a, mint_b)
            m = txfees.harvested(url, [r['signature']], r['pool'], a, b)
            for x in (a, b):
                if x not in scales:
                    scales[x] = mint_scale(url, x)
            if m is not None:
                # rows are UI amounts; txfees measures raw / 10^decimals
                m = None if None in (scales[a], scales[b]) else (m[0] * scales[a], m[1] * scales[b])
            st, d = check_harvest(float(r['fee_a']), float(r['fee_b']), m)
            if st != 'ok':
                details.append({'id': r['id'], **d})
            worst = max(worst, st, key=('ok', 'warn', 'fail').index)
            db.set_audit_value('harvest_cursor', r['id'])
        return worst, {'checked': len(rows), 'problems': details}
    guarded('harvests', harvests)

    def payouts():
        last = int(db.audit_value('payout_cursor') or 0)
        with db.cursor() as c:
            c.execute("select id, token_mint, amount, signature from payouts where kind = 'paid' and id > %s "
                      "and signature is not null and (%s::text[] is null or config_name = any(%s)) "
                      "order by id limit 30", (last, names, names))
            rows = c.fetchall()
        bad = []
        for r in rows:
            tx = txfees.fetch(url, r['signature'], tries=3)
            if not payout_received(tx, config.PROFIT_WALLET, r['token_mint'], float(r['amount'])):
                bad.append(r['id'])
            db.set_audit_value('payout_cursor', r['id'])
        return ('fail' if bad else 'ok'), {'checked': len(rows), 'not_received': bad}
    guarded('payouts', payouts)

    def positions():
        with db.cursor() as c:
            c.execute('select mint, dex, config_name from positions where closed_at is null '
                      'and (%s::text[] is null or config_name = any(%s)) order by opened_at', (names, names))
            rows = c.fetchall()
        nfts = [a['mint'] for a in accts if a['decimals'] == 0 and a['amount'] == 1]
        live = []
        for r in rows:
            if r['dex'] == 'meteora-dlmm':
                info = rpc(url, 'getAccountInfo', [r['mint'], {'encoding': 'base64'}])
                if info is None:
                    return 'warn', {'note': 'DLMM position account unreadable', 'mint': r['mint']}
                v = info.get('value')
                if v and v.get('owner') == DLMM_PROGRAM and (v.get('lamports') or 0) > 0:
                    live.append(r['mint'])
        return check_positions([r['mint'] for r in rows], nfts, [r['dex'] for r in rows], dlmm_live=live,
                               db_open_profiles=[r['config_name'] for r in rows] if names is not None else None)
    guarded('positions', positions)

    def band_profiles():
        with db.cursor() as c:
            c.execute("""select p.mint, p.open_reason from positions p
                         where p.closed_at is not null
                           and (%s::text[] is null or p.config_name = any(%s))
                           and p.closed_at > (select coalesce(min(ts), now()) from risk_profile)
                           and not exists (select 1 from band_profile b where b.mint = p.mint and b.final)""",
                      (names, names))
            rows = c.fetchall()
        for r in rows:
            db.record_band_profile(r['mint'], 'rebalance', 'written by the audit')
        return ('warn' if rows else 'ok'), {'written': [r['mint'] for r in rows]}
    guarded('band_profile', band_profiles)

    def owed():
        with db.cursor() as c:
            c.execute("select id, usd from payouts where kind = 'owed' and ts < now() - make_interval(days => %s) "
                      "and (%s::text[] is null or config_name = any(%s))", (OWED_DAYS, names, names))
            return check_owed([dict(r) for r in c.fetchall()])
    guarded('owed', owed)

    guarded('empty', lambda: check_empty(sum(a['lamports'] for a in empty), len(empty)))

    def fee_reads():
        with db.cursor() as c:
            c.execute("select count(*) n from events where kind = 'fee_read_rejected' and ts > now() - interval '1 day' "
                      "and (%s::text[] is null or profile = any(%s))", (names, names))
            return check_fee_reads(c.fetchone()['n'])
    guarded('fee_reads', fee_reads)
    return results


def keep_mints(bot, mint_a, mint_b, wallet_mints=()):
    """Accounts the janitor must not close even when empty: the pool's two
    tokens, every mint of every profile of the wallet (`wallet_mints`), the
    payout token, native SOL, and every reward mint seen."""
    keep = {NATIVE, USDC, mint_a, mint_b} | set(wallet_mints)
    try:
        st = bot.load()
        keep |= set(st.get('reward_mints_seen') or []) | set(st.get('janitor_keep') or [])
    except Exception:
        pass
    # every reward mint the held pool names, ended programs included: the
    # protocol uses these accounts at every harvest and close (2026-09-28:
    # closing the empty RAY account broke a Raydium close)
    try:
        keep |= {m for m in (bot.pool_record().get('reward_mints') or []) if m}
    except Exception:
        pass
    return keep
