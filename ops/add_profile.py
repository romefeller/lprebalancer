"""Register a profile and its wallet if new.

The profile takes every tuning column of a template profile, then its own
pool (read from the venue: mints, symbols), wallet and switches. A dry run by
default: it prints every row and writes nothing. It never touches systemd.

    LPBOT_DSN=dbname=rebalancer python3 ops/add_profile.py \\
        --profile sol-lp3 --template sol-usdc \\
        --wallet sol-lp3 --address <address> --secret-env LPBOT_SOL3_KEY_PATH --label 'third SOL wallet' \\
        --dex raydium-clmm --pool 8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj \\
        --execute-dexes raydium-clmm,orca --deposit-mint USDC [--apply]
"""
import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import psycopg2.extras  # noqa: E402

import config  # noqa: E402  (signer_env's allowlist)
import db  # noqa: E402
import dexes  # noqa: E402
import engine  # noqa: E402
import guards  # noqa: E402
import rebalancer  # noqa: E402
import lp.signers  # noqa: E402
import wallets  # noqa: E402

# What a new profile never takes from its template: identity, pool, wallet
# and the switches set here (deploy.sh's SKIP, plus allow_swap).
SKIP = {'id', 'name', 'active', 'pool', 'pair_label', 'token_a', 'token_b', 'dex', 'dexes', 'updated_at',
        'wallet_id', 'enabled', 'deposit_mint', 'residual_owner', 'mints', 'pool_pinned', 'regime_enabled',
        'rebalance_swap', 'execute_dexes', 'signer_env', 'allow_swap', 'special', 'fee_bps', 'fee_wallet'}
# Venues a position can be held on: every signer but the routes.
VENUES = tuple(d for d in lp.signers.SIGNERS if d not in ('jupiter', 'orca-swap', 'payout', 'janitor'))
WALLET_ID = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
SECRET_ENV = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
# An address on each chain the wallets table allows (its wallets_check).
ADDRESS = {'solana': guards.is_address}


class Refused(ValueError):
    """An argument the tool will not write."""


def pool_spec(spec, chain):
    """'<dex> <pool>' -> (dex, pool), checked for `chain`. Pure."""
    parts = (spec or '').split()
    if len(parts) != 2 or parts[0] not in VENUES or not ADDRESS[chain](parts[1]):
        raise Refused(f'a pool is "<known dex> <address>", got {spec!r}')
    return parts[0], parts[1]


def signer_env(items):
    """['K=V', ...] -> {K: V}, through config.signer_env's allowlist. Pure."""
    env = dict(i.split('=', 1) for i in items or [] if '=' in i)
    if len(env) != len(items or []):
        raise Refused(f'--signer-env takes KEY=VALUE, got {items!r}')
    try:
        return config.signer_env(env) if env else {}
    except ValueError as e:
        raise Refused(str(e)) from None


def fee_terms(args, chain, vault):
    """(special, fee_bps, fee_wallet) for `args` (sql/036). An instance is
    the operator's own (special, no fee share) unless --no-special; only a
    vault instance may be anything else. Pure; raises Refused."""
    special = True if getattr(args, 'special', None) is None else bool(args.special)
    bps = getattr(args, 'fee_bps', None) or 0
    to = getattr(args, 'fee_wallet', None)
    if special and (bps or to):
        raise Refused('a special instance pays no fee share: no --fee-bps or --fee-wallet')
    if not special and not vault:
        raise Refused('only a vault instance can be --no-special (a key wallet pays no fee share)')
    if not 0 <= bps <= 5000:
        raise Refused(f'--fee-bps {bps} is not between 0 and 5000')
    if bps and not ADDRESS[chain](to):
        raise Refused(f'--fee-wallet {to!r} is not a {chain} address')
    return special, bps, to if bps else None


def build(args, template, rec, wallet, others, vault=False):
    """(wallet row or None, config row) for `args`.
    `template` is the template's config row (with its wallet's chain as
    'chain'), `rec` the pool record (dexes.pool), `wallet` the existing
    wallets row or None, `others` how many profiles the wallet holds
    already, `vault` whether that wallet is a vault. Pure; raises Refused."""
    chain = (wallet or {}).get('chain') or args.chain
    if chain not in ADDRESS:
        raise Refused(f'unknown chain {chain!r}')
    # A template on another chain (the first profile of a new chain) gives its
    # tuning only: the payout token and the profit wallet must be valid here.
    cross = template.get('chain') != chain
    if cross and not getattr(args, 'cross_chain', False):
        raise Refused(f'template {template["name"]} is on {template.get("chain")}, the wallet on {chain}'
                      ' (pass --cross-chain to take its tuning only)')
    if cross and not ADDRESS[chain](template.get('profit_wallet') or ''):
        raise Refused(f'template profit wallet {template.get("profit_wallet")!r} is not a {chain} address')
    if not ADDRESS[chain](args.pool):
        raise Refused(f'--pool {args.pool!r} is not a {chain} address')
    new_wallet = None
    if wallet is None:
        if not (WALLET_ID.match(args.wallet) and ADDRESS[chain](args.address or '')
                and SECRET_ENV.match(args.secret_env or '')):
            raise Refused('a new wallet needs a valid --wallet id, --address and --secret-env')
        new_wallet = (args.wallet, chain, args.address, args.secret_env, args.label or args.wallet)
    elif args.address and args.address != wallet['address']:
        raise Refused(f'wallet {args.wallet} exists with address {wallet["address"]}, not {args.address}')
    a, b = rec['token_a'], rec['token_b']
    norm = wallets.norm
    mints = [norm(a['address']), norm(b['address'])]
    stables = {norm(m) for m in engine.STABLE_MINTS}
    if not set(mints) & stables:
        raise Refused(f'the pool quotes no stablecoin ({a["symbol"]}/{b["symbol"]})')
    by_symbol = {a['symbol'].upper(): mints[0], b['symbol'].upper(): mints[1]}
    deposit = (by_symbol.get(args.deposit_mint.upper()) or norm(args.deposit_mint)) if args.deposit_mint else \
        (mints[0] if mints[1] in stables else mints[1])
    execute = [d for d in (args.execute_dexes or args.dex).split(',') if d]
    if args.dex not in execute or any(d not in VENUES for d in execute):
        raise Refused(f'--execute-dexes {execute} must be known venues and include {args.dex}')
    special, fee_bps, fee_wallet = fee_terms(args, chain, vault)
    row = {c: v for c, v in template.items() if c not in SKIP | {'chain'}}
    if getattr(args, 'profit_wallet', None):
        if not ADDRESS[chain](args.profit_wallet):
            raise Refused(f'--profit-wallet {args.profit_wallet!r} is not a {chain} address')
        row['profit_wallet'] = args.profit_wallet
    if cross:
        # the template's payout token is another chain's: pay in this pool's stablecoin
        row['payout_mint'] = next(m for m in mints if m in stables)
    if getattr(args, 'payout_mint', None):
        row['payout_mint'] = by_symbol.get(args.payout_mint.upper()) or norm(args.payout_mint)
        if row['payout_mint'] not in mints:
            raise Refused(f'--payout-mint {args.payout_mint} is not one of the pool\'s tokens')
    if getattr(args, 'max_usd', None) is not None:
        if not args.max_usd > 0:
            raise Refused(f'--max-usd must be positive, got {args.max_usd}')
        row['max_usd'] = args.max_usd
    row.update(name=args.profile, active=False, pool=args.pool, dex=args.dex, dexes=execute, execute_dexes=execute,
               pair_label=f'{a["symbol"]}/{b["symbol"]}', token_a=a['symbol'], token_b=b['symbol'],
               wallet_id=args.wallet, enabled=True, deposit_mint=deposit, residual_owner=others == 0,
               mints=mints, pool_pinned=True, regime_enabled=True, rebalance_swap=True,
               allow_swap=False, signer_env=signer_env(args.signer_env) or None,
               special=special, fee_bps=fee_bps, fee_wallet=fee_wallet)
    return new_wallet, row


def insert_rows(cur, new_wallet, row):
    """Write what build() returned with `cur`; the caller commits."""
    keys = list(row)
    # jsonb columns (signer_env and any later one) go in as JSON; a dict is not a SQL value
    vals = [psycopg2.extras.Json(row[k]) if isinstance(row[k], dict) else row[k] for k in keys]
    if new_wallet:
        cur.execute('insert into wallets (id, chain, address, secret_env, label) values (%s,%s,%s,%s,%s)', new_wallet)
    cur.execute(f'insert into config ({", ".join(keys)}) values ({", ".join(["%s"] * len(keys))})', vals)


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--profile', required=True)
    p.add_argument('--template', required=True, help='profile whose tuning is copied')
    p.add_argument('--wallet', required=True, help='wallet id (new or existing)')
    p.add_argument('--chain', default='solana', choices=tuple(ADDRESS))
    p.add_argument('--cross-chain', action='store_true', help='take the tuning of a template on another chain')
    p.add_argument('--payout-mint', help='mint or symbol; default: the template\'s (another chain: the stablecoin)')
    p.add_argument('--max-usd', type=float, help='the signer\'s per-open ceiling; default: the template\'s')
    p.add_argument('--profit-wallet', help='where payouts go; default: the template\'s')
    p.add_argument('--special', action=argparse.BooleanOptionalAction, default=None,
                   help='the operator\'s own instance (default); --no-special for a vault that pays a fee share')
    p.add_argument('--fee-bps', type=int, help='share of the payout-token fees for --fee-wallet (with --no-special)')
    p.add_argument('--fee-wallet')
    p.add_argument('--address'), p.add_argument('--secret-env'), p.add_argument('--label')
    p.add_argument('--dex', required=True), p.add_argument('--pool', required=True)
    p.add_argument('--execute-dexes', help='comma list; default: --dex')
    p.add_argument('--signer-env', action='append', help='KEY=VALUE, from config.SIGNER_ENV_ALLOWED')
    p.add_argument('--deposit-mint', help='mint or symbol; default: the non-stable side')
    p.add_argument('--apply', action='store_true')
    return p.parse_args(argv)


def main(argv):
    args = parse(argv)
    with db.cursor() as cur:
        cur.execute('select c.*, w.chain from config c left join wallets w on w.id = c.wallet_id where c.name = %s',
                    (args.template,))
        template = cur.fetchone()
        cur.execute('select * from wallets where id = %s', (args.wallet,))
        wallet = cur.fetchone()
        cur.execute('select count(*) n from config where wallet_id = %s', (args.wallet,))
        others = cur.fetchone()['n']
        cur.execute('select 1 from config where name = %s', (args.profile,))
        exists = cur.fetchone() is not None
    if not template:
        raise SystemExit(f'ERROR: template profile {args.template} not found')
    if exists:
        raise SystemExit(f'ERROR: profile {args.profile} exists; nothing written')
    try:
        new_wallet, row = build(args, dict(template), dexes.pool(args.dex, args.pool), wallet, others,
                                vault=db.vault_row(args.wallet) is not None)
    except Refused as e:
        raise SystemExit(f'ERROR: {e}')
    print('wallet:', new_wallet or f'{args.wallet} (exists)')
    print('profile:', {k: row[k] for k in sorted(row)})
    if not args.apply:
        print('DRY RUN: pass --apply to write')
        return 0
    with db.cursor(commit=True) as cur:
        insert_rows(cur, new_wallet, row)
    print(f'written. Next: sudo systemctl enable --now lp-bot@{args.profile}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
