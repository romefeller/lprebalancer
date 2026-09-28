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
"""
import json
import time
import urllib.request
import uuid

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


def classify_tx(tx, owner, known):
    """What one wallet transaction was: ('known'|'failed'|'dust'|'bot'|
    'deposit'|'withdrawal'|'other', detail). `tx` is jsonParsed."""
    sig = tx['transaction']['signatures'][0]
    if sig in known:
        return 'known', {}
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
    moves = {mt: tok(mt) for mt in mints if tok(mt) != 0}
    sol = lam / 1e9 + moves.pop(NATIVE, 0) / 1e9
    usdc = moves.pop(USDC, 0) / 1e6
    progs = sorted({BOT_PROGRAMS[k] for k in keys if k in BOT_PROGRAMS})
    d = {'sol': round(sol, 9), 'usdc': round(usdc, 6), 'programs': progs, 'other_tokens': moves, 'signer': signer}
    if not signer and abs(lam) <= DUST_LAMPORTS and not moves and usdc == 0:
        return 'dust', d
    if signer and progs:
        return 'bot', d
    # A flow moves money one way only: in (a deposit) or out (a withdrawal).
    # SOL and USDC in opposite directions is a swap-like movement, not a flow.
    if not progs:                     # other tokens (dust, airdrops) may ride along; they are noted
        if sol >= 0 and usdc >= 0 and (sol > 0 or usdc > 0):
            return 'deposit', d
        if sol <= 0 and usdc <= 0 and (sol < 0 or usdc < 0):
            return 'withdrawal', d
    return 'other', d


def check_flows(classified):
    """Summary of a batch of classified transactions: deposits and
    withdrawals are recorded by the runner and warned; unrecorded bot
    operations and anything unexplained are warned."""
    counts = {}
    for kind, _ in classified:
        counts[kind] = counts.get(kind, 0) + 1
    flagged = [(k, d) for k, d in classified if k in ('deposit', 'withdrawal', 'bot', 'other')]
    status = 'warn' if flagged else 'ok'
    return status, {'counts': counts, 'flagged': flagged[:10]}


def check_harvest(row_a, row_b, measured):
    """A harvest row against its transaction's pool outflow (txfees)."""
    if measured is None:
        return 'warn', {'note': 'transaction unreadable'}
    ma, mb = measured
    d = {'row': [row_a, row_b], 'tx': [ma, mb]}
    # rows are booked from the same transaction (txfees), so they match it exactly
    ok = abs(ma - row_a) <= HARVEST_TOLERANCE and abs(mb - row_b) <= HARVEST_TOLERANCE
    return ('ok', d) if ok else ('fail', d)


def payout_received(tx, profit_wallet, mint, amount):
    """Whether a payout tx moved `amount` of `mint` into the profit wallet."""
    if tx is None or tx.get('meta') is None or tx['meta'].get('err') is not None:
        return False
    m = tx['meta']
    def total(key):
        return sum(int(b['uiTokenAmount']['amount']) / 10 ** int(b['uiTokenAmount']['decimals'])
                   for b in m.get(key) or [] if b.get('owner') == profit_wallet and b.get('mint') == mint)
    got = total('postTokenBalances') - total('preTokenBalances')
    return abs(got - amount) <= PAYOUT_TOLERANCE


def check_positions(db_open_mints, chain_nft_mints, db_open_dexes=None):
    """Open positions in the ledger against the position NFTs the wallet
    holds. Meteora positions are accounts, not NFTs: they are not compared."""
    nft = set(chain_nft_mints)
    open_ = [m for m, dx in zip(db_open_mints, db_open_dexes or [None] * len(db_open_mints)) if dx != 'meteora-dlmm']
    missing = [m for m in open_ if m not in nft]
    orphans = [m for m in nft if m not in set(db_open_mints)]
    d = {'db_open': list(db_open_mints), 'nfts': sorted(nft), 'missing_on_chain': missing, 'orphans': orphans}
    if missing or orphans or len(db_open_mints) > 1:
        return 'fail', d
    return 'ok', d


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
                        'decimals': int(info['tokenAmount']['decimals'])})
    return out


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
                if r.get('signature'):
                    known.add(r['signature'])
                known |= {s for s in r.get('signatures') or [] if isinstance(s, str)}
                known |= {x.get('signature') for x in r.get('sent') or [] if isinstance(x, dict) and x.get('signature')}
    except OSError:
        pass
    return known


def run(bot, db, config, txfees, notify, now=None):
    """One audit run. `bot` is the rebalancer module (wallet, read_status,
    deployable_usd, pool_tokens, FEED, ROOT). Returns {check: status}."""
    run_id = uuid.uuid4().hex[:12]
    url = config.RPC
    results = {}

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
    with db.cursor() as c:
        c.execute('select equity_usd from snapshots where equity_usd is not null order by ts desc, id desc limit 1')
        r = c.fetchone()
    snap_equity = float(r['equity_usd']) if r else None
    owner = bal.get('owner')

    guarded('idle', lambda: check_idle(bot.deployable_usd(bal), snap_equity or 0.0, open_))
    guarded('gas', lambda: check_gas(float(bal.get('sol') or 0.0), config.GAS_RESERVE_SOL))

    accts = token_accounts(url, owner) if owner else []
    (mint_a, _), (mint_b, _) = bot.pool_tokens()
    keep = keep_mints(bot, mint_a, mint_b)
    empty = [a for a in accts if a['amount'] == 0 and a['mint'] not in keep]

    def equity():
        # Independent of the signer's walletUsd: the native balance and every
        # token account, priced here, plus the position's mark and accrual.
        native = (rpc(url, 'getBalance', [owner]) or {}).get('value')
        if native is None:
            return 'warn', {'note': 'native balance unreadable'}
        q = bal.get('quoteUsd') or 1.0
        px = {mint_a: bal['price'] * q, mint_b: q}
        sol_usd = px[NATIVE] if NATIVE in px else bot.dexes.jupiter_prices([NATIVE]).get(NATIVE, 0.0)
        # the pool's SPL tokens are equity; wrapped SOL, reward dust and other
        # tokens are not counted by equity and go to `uncounted`
        pool_usd = sum(a['amount'] / 10 ** a['decimals'] * px[a['mint']] for a in accts
                       if a['mint'] in px and a['mint'] != NATIVE)
        others = [a for a in accts if a['amount'] > 0 and (a['mint'] not in px or a['mint'] == NATIVE)
                  and not (a['decimals'] == 0 and a['amount'] == 1)]            # position NFTs are in the position mark
        prices = dict(bot.dexes.jupiter_prices([a['mint'] for a in others if a['mint'] != NATIVE]) if others else {})
        prices[NATIVE] = sol_usd
        dust_usd = sum(a['amount'] / 10 ** a['decimals'] * prices.get(a['mint'], 0.0) for a in others)
        rent_usd = sum(a['lamports'] for a in empty) / 1e9 * sol_usd
        uncounted = dust_usd + rent_usd
        db.set_audit_value('uncounted_usd', round(uncounted, 6))
        pos_usd = (bot.position_usd(status) or 0.0) if open_ else 0.0
        chain_total = native / 1e9 * sol_usd + pool_usd + pos_usd + \
            float((status or {}).get('feesAccrued_USD') or 0.0) + uncounted
        return check_equity(chain_total, snap_equity, uncounted)
    guarded('equity', equity)

    def flows():
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
            kind, d = classify_tx(tx, owner, known)
            d['sig'] = s['signature']
            if kind in ('deposit', 'withdrawal'):
                px = bal['price'] * (bal.get('quoteUsd') or 1.0)
                usd = abs(d['sol']) * px + abs(d['usdc'])
                ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(tx.get('blockTime') or time.time()))
                db.record_flow(ts, kind, abs(d['sol']), abs(d['usdc']), usd, px, s['signature'],
                               f"found by the flows audit; other tokens {d['other_tokens']}")
            classified.append((kind, d))
            time.sleep(0.3)
        db.set_audit_value('flows_cursor', max(sigs, key=lambda x: x.get('blockTime') or 0)['signature'])
        return check_flows(classified)
    guarded('flows', flows)

    def harvests():
        last = int(db.audit_value('harvest_cursor') or 0)
        with db.cursor() as c:
            c.execute("""select h.id, h.fee_a, h.fee_b, h.signature, p.pool from harvests h join positions p using (mint)
                         where h.id > %s and h.signature not like 'close:%%' order by h.id limit 30""", (last,))
            rows = c.fetchall()
        worst, details = 'ok', []
        for r in rows:
            m = txfees.harvested(url, [r['signature']], r['pool'], mint_a, mint_b)
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
                      "and signature is not null order by id limit 30", (last,))
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
            c.execute('select mint, dex from positions where closed_at is null order by opened_at')
            rows = c.fetchall()
        nfts = [a['mint'] for a in accts if a['decimals'] == 0 and a['amount'] == 1]
        return check_positions([r['mint'] for r in rows], nfts, [r['dex'] for r in rows])
    guarded('positions', positions)

    def band_profiles():
        with db.cursor() as c:
            c.execute("""select p.mint, p.open_reason from positions p
                         where p.closed_at is not null
                           and p.closed_at > (select coalesce(min(ts), now()) from risk_profile)
                           and not exists (select 1 from band_profile b where b.mint = p.mint and b.final)""")
            rows = c.fetchall()
        for r in rows:
            db.record_band_profile(r['mint'], 'rebalance', 'written by the audit')
        return ('warn' if rows else 'ok'), {'written': [r['mint'] for r in rows]}
    guarded('band_profile', band_profiles)

    def owed():
        with db.cursor() as c:
            c.execute("select id, usd from payouts where kind = 'owed' and ts < now() - make_interval(days => %s)",
                      (OWED_DAYS,))
            return check_owed([dict(r) for r in c.fetchall()])
    guarded('owed', owed)

    guarded('empty', lambda: check_empty(sum(a['lamports'] for a in empty), len(empty)))

    def fee_reads():
        with db.cursor() as c:
            c.execute("select count(*) n from events where kind = 'fee_read_rejected' and ts > now() - interval '1 day'")
            return check_fee_reads(c.fetchone()['n'])
    guarded('fee_reads', fee_reads)
    return results


def keep_mints(bot, mint_a, mint_b):
    """Accounts the janitor must not close even when empty: the pool's two
    tokens, the payout token, native SOL, and every reward mint seen."""
    keep = {NATIVE, USDC, mint_a, mint_b}
    try:
        keep |= set(bot.load().get('reward_mints_seen') or [])
    except Exception:
        pass
    return keep
