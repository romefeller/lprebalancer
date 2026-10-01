"""Stats for every wallet and every pool, and their sum.

    python3 stats.py                    every active pool, per wallet, and the TOTAL
    python3 stats.py --wallet sol-lp    one wallet's pools and its subtotal
    python3 stats.py --pool mu-usdc     one pool (profile)
    python3 stats.py --json             the same as JSON (portfolio())
    python3 stats.py --all              every profile, dormant and disabled ones too

A pool is active when its profile is enabled and holds an open position now.
Dormant profiles (enabled, no position) and disabled ones are listed by name
only, unless --all. Each pool's figures are its own book (db.stats with that
profile): a snapshot's wallet_usd is the profile's own sleeve of the shared
wallet, so the sums count no dollar twice. Dollars add across pools; token
amounts never do (a SOL and an MU amount are not one number), so the TOTAL and
the wallet subtotals carry dollars and counts only.

Read only: no write to the database, no chain call. The residual owner of the
first wallet emits portfolio() once a day as the PORTFOLIO event.
"""
import argparse
import json

import db

# The dollar figures of one pool. Each adds across pools.
USD_KEYS = ('equity_usd', 'lp_usd', 'idle_usd',
            'fees_today_usd', 'fees_realised_usd', 'fees_unrealised_usd', 'fees_total_usd',
            'fees_per_day_6h_usd', 'fees_per_day_24h_usd', 'fees_per_day_usd',
            'paid_usd', 'reinvested_usd', 'gas_usd',
            'pnl_usd', 'profit_usd', 'vs_hold_usd', 'vs_hold_50_50_usd',
            'deposits_usd', 'withdrawals_usd')
COUNT_KEYS = ('recentres', 'harvests', 'deposits', 'withdrawals')
# Token amounts: one pool's own tokens, never summed.
TOKEN_KEYS = ('fees_realised_a', 'fees_realised_b', 'fees_unrealised_a', 'fees_unrealised_b',
              'fees_total_a', 'fees_total_b')
# APRs recomputed on a sum: (APR key, the rate it annualises).
APR_KEYS = (('apr_6h_pct', 'fees_per_day_6h_usd'), ('apr_24h_pct', 'fees_per_day_24h_usd'),
            ('apr_pct', 'fees_per_day_usd'))


def record(profile, book, payouts, flows):
    """One pool's flat figures, from its book (db.stats), fee split
    (db.payout_totals) and flows (db.flow_totals). `profile` is its
    book_profiles row. Pure."""
    s = book.get('since_start') or {}
    return {
        'profile': profile['name'], 'wallet_id': profile['wallet'],
        'pair': book.get('position_pair') or book.get('pair'),
        'dex': book.get('position_dex') or book.get('dex'),
        'token_a': book.get('token_a'), 'token_b': book.get('token_b'),
        'equity_usd': book.get('equity_usd'), 'lp_usd': book.get('lp_usd'),
        'idle_usd': book.get('wallet_usd'),                  # the sleeve's tokens not in the LP
        'fees_today_usd': book.get('fees_today_usd'),
        **{k: book.get(k) for k in TOKEN_KEYS},
        'fees_realised_usd': book.get('fees_realised_usd'), 'fees_unrealised_usd': book.get('fees_unrealised_usd'),
        'fees_total_usd': book.get('fees_total_usd'),
        'fees_per_day_6h_usd': book.get('fees_per_day_6h_usd'),
        'fees_per_day_24h_usd': book.get('fees_per_day_24h_usd'),
        'fees_per_day_usd': book.get('fees_per_day_usd'),
        'apr_6h_pct': book.get('apr_6h_pct'), 'apr_24h_pct': book.get('apr_24h_pct'), 'apr_pct': book.get('apr_pct'),
        'paid_usd': payouts.get('paid_usd'), 'reinvested_usd': payouts.get('reinvested_usd'),
        'gas_usd': payouts.get('gas_usd'),
        'recentres': book.get('positions_opened') or 0, 'harvests': book.get('harvests') or 0,
        'positions_open_now': book.get('positions_open_now') or 0,
        'in_range_pct': book.get('in_range_pct'), 'tracked_days': book.get('tracked_days'),
        'pnl_usd': book.get('pnl_usd'), 'pnl_basis': book.get('pnl_basis'),
        'since': s.get('since'), 'profit_usd': s.get('profit_usd'), 'profit_pct': s.get('profit_pct'),
        'vs_hold_usd': s.get('vs_hold_start_assets_usd'), 'vs_hold_50_50_usd': s.get('vs_hold_50_50_usd'),
        'deposits_usd': flows.get('deposits_usd'), 'deposits': flows.get('deposits') or 0,
        'withdrawals_usd': flows.get('withdrawals_usd'), 'withdrawals': flows.get('withdrawals') or 0,
        'last_seen': book.get('last_seen'),
    }


def total(records):
    """The sum of pool records: every dollar figure (None when no pool knows
    it), every count, and the APRs recomputed over the pools that have both
    the rate and an equity. No token amount. Pure."""
    out = {'pools': len(records)}
    for k in USD_KEYS:
        known = [float(r[k]) for r in records if r.get(k) is not None]
        out[k] = round(sum(known), 4) if known else None
    for k in COUNT_KEYS:
        out[k] = sum(int(r.get(k) or 0) for r in records)
    for apr, rate in APR_KEYS:
        known = [(float(r[rate]), float(r['equity_usd'])) for r in records
                 if r.get(rate) is not None and r.get('equity_usd')]
        eq = sum(e for _, e in known)
        out[apr] = round(sum(x for x, _ in known) / eq * 365 * 100, 2) if eq > 0 else None
    ranged = [(float(r['in_range_pct']), float(r.get('tracked_days') or 0)) for r in records
              if r.get('in_range_pct') is not None]
    weight = sum(d for _, d in ranged)
    out['in_range_pct'] = round(sum(p * d for p, d in ranged) / weight, 1) if weight > 0 else None
    return out


def classify(profiles, open_now):
    """Split book_profiles rows into active (enabled and holding a position),
    dormant (enabled, no position) and disabled. `open_now` is
    {profile: open positions}. Pure."""
    active, dormant, disabled = [], [], []
    for p in profiles:
        holds = open_now.get(p['name'], 0) > 0
        (active if p['on'] and holds else dormant if p['on'] else disabled).append(p)
    return active, dormant, disabled


def portfolio(wallet_id=None, profile=None, show_all=False):
    """Every wallet's active pools with their figures, a subtotal per wallet
    and the TOTAL, as one dict (the PORTFOLIO event). Dormant and disabled
    profiles are listed by name; with show_all their figures are in too."""
    rows = [p for p in db.book_profiles(wallet_id, include_disabled=True) if profile is None or p['name'] == profile]
    open_now = db.open_by_profile()
    active, dormant, disabled = classify(rows, open_now)
    shown = rows if show_all else active
    records = [record(p, db.stats(profile=p['name']), db.payout_totals(p['name']), db.flow_totals(p['name']))
               for p in shown]
    known = {w['id']: w for w in db.wallets()}
    wallets = []
    for wid in sorted({r['wallet_id'] for r in records}):
        mine = [r for r in records if r['wallet_id'] == wid]
        w = known.get(wid, {})
        wallets.append({'wallet_id': wid, 'chain': w.get('chain') or 'solana', 'label': w.get('label'),
                        'pools': [r['profile'] for r in mine], 'subtotal': total(mine)})
    return {'ts': db.now().isoformat(timespec='seconds'), 'pools': records, 'wallets': wallets,
            'total': total(records),
            'dormant': [p['name'] for p in dormant], 'disabled': [p['name'] for p in disabled],
            # money sitting in a pool no process runs: worth a look
            'disabled_holding': [p['name'] for p in disabled if open_now.get(p['name'], 0) > 0]}


def _usd(x, d=2):
    return '-' if x is None else f'${x:,.{d}f}'


def _signed(x):
    return '-' if x is None else f'{x:+,.2f}'


def _pct(x, d=0):
    return '-' if x is None else f'{x:.{d}f}%'


def _tok(x, sym):
    return f'- {sym}' if x is None else f'{x:.6f}'.rstrip('0').rstrip('.') + f' {sym}'


def _sum_lines(t, label):
    return [f'{label}  {t["pools"]} pool{"" if t["pools"] == 1 else "s"}',
            f'    equity  {_usd(t["equity_usd"])} · in LP {_usd(t["lp_usd"])} · idle {_usd(t["idle_usd"])}',
            f'    fees    realised {_usd(t["fees_realised_usd"], 4)} · unrealised {_usd(t["fees_unrealised_usd"], 4)}'
            f' · total {_usd(t["fees_total_usd"], 4)} · today {_usd(t["fees_today_usd"], 4)}',
            f'    rate    6h {_usd(t["fees_per_day_6h_usd"], 4)}/d · 24h {_usd(t["fees_per_day_24h_usd"], 4)}/d'
            f' · since start {_usd(t["fees_per_day_usd"], 4)}/d · APR 24h {_pct(t["apr_24h_pct"])}'
            f' · since start {_pct(t["apr_pct"])}',
            f'    payouts paid {_usd(t["paid_usd"], 4)} · reinvested {_usd(t["reinvested_usd"], 4)}'
            f' · gas {_usd(t["gas_usd"], 4)}',
            f'    P&L     {_signed(t["profit_usd"])} since start · vs holding the start assets {_signed(t["vs_hold_usd"])}'
            f' · vs 50/50 {_signed(t["vs_hold_50_50_usd"])}',
            f'    flows   deposits {_usd(t["deposits_usd"])} ({t["deposits"]}) · withdrawals'
            f' {_usd(t["withdrawals_usd"])} ({t["withdrawals"]}) · {t["recentres"]} re-centres'
            f' · {t["harvests"]} harvests · in range {_pct(t["in_range_pct"])}']


def _pool_lines(r):
    a, b = r['token_a'] or 'A', r['token_b'] or 'B'
    return [f'  {r["pair"] or "-"}  ({r["profile"]}, {r["dex"] or "-"})'
            f'{"" if r["positions_open_now"] else "  no position"}',
            f'    equity  {_usd(r["equity_usd"])} · in LP {_usd(r["lp_usd"])} · idle {_usd(r["idle_usd"])}',
            f'    fees    realised {_tok(r["fees_realised_a"], a)} {_tok(r["fees_realised_b"], b)}'
            f' {_usd(r["fees_realised_usd"], 4)}',
            f'            unrealised {_tok(r["fees_unrealised_a"], a)} {_tok(r["fees_unrealised_b"], b)}'
            f' {_usd(r["fees_unrealised_usd"], 4)}',
            f'            total {_tok(r["fees_total_a"], a)} {_tok(r["fees_total_b"], b)}'
            f' {_usd(r["fees_total_usd"], 4)} · today {_usd(r["fees_today_usd"], 4)}',
            f'    rate    6h {_usd(r["fees_per_day_6h_usd"], 4)}/d · 24h {_usd(r["fees_per_day_24h_usd"], 4)}/d'
            f' · since start {_usd(r["fees_per_day_usd"], 4)}/d · APR 24h {_pct(r["apr_24h_pct"])}'
            f' · since start {_pct(r["apr_pct"])}',
            f'    payouts paid {_usd(r["paid_usd"], 4)} · reinvested {_usd(r["reinvested_usd"], 4)}'
            f' · gas {_usd(r["gas_usd"], 4)}',
            f'    P&L     {_signed(r["profit_usd"])} since {str(r["since"] or "-")[:10]}'
            f' · vs holding the start assets {_signed(r["vs_hold_usd"])} · vs 50/50 {_signed(r["vs_hold_50_50_usd"])}',
            f'    flows   deposits {_usd(r["deposits_usd"])} ({r["deposits"]}) · withdrawals'
            f' {_usd(r["withdrawals_usd"])} ({r["withdrawals"]}) · {r["recentres"]} re-centres'
            f' · {r["harvests"]} harvests · in range {_pct(r["in_range_pct"])} over {r["tracked_days"] or 0:.1f}d']


def render(p):
    """portfolio() as text. Pure."""
    out = []
    by_name = {r['profile']: r for r in p['pools']}
    for w in p['wallets']:
        out.append(f'WALLET {w["wallet_id"]} ({w["chain"]}{", " + w["label"] if w["label"] else ""})')
        for name in w['pools']:
            out += _pool_lines(by_name[name])
        if len(p['wallets']) > 1:
            out += _sum_lines(w['subtotal'], f'  subtotal {w["wallet_id"]}')
    out += _sum_lines(p['total'], 'TOTAL')
    if not p['pools']:
        out.append('no active pool')
    names = lambda xs: f'{len(xs)}' + (f' ({", ".join(xs)})' if xs else '')
    out.append(f'dormant {names(p["dormant"])} · disabled {names(p["disabled"])}'
               + (f' · DISABLED BUT HOLDING A POSITION: {", ".join(p["disabled_holding"])}'
                  if p['disabled_holding'] else ''))
    return '\n'.join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Stats per wallet and per pool, and their sum.')
    ap.add_argument('--wallet', help='one wallet id')
    ap.add_argument('--pool', help='one profile name')
    ap.add_argument('--json', action='store_true', help='print portfolio() as JSON')
    ap.add_argument('--all', action='store_true', help='include dormant and disabled profiles in full')
    a = ap.parse_args(argv)
    p = portfolio(a.wallet, a.pool, a.all)
    print(json.dumps(p, indent=1, default=str) if a.json else render(p))


if __name__ == '__main__':
    main()
