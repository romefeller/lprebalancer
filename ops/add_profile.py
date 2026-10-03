"""Register a profile, its wallet if new, and its swing if any.

The profile takes every tuning column of a template profile on the same
chain (as deploy.sh did for the stock profiles), then its own pool (read
from the venue: mints, symbols), wallet and switches. A swing profile (both
--swing-open and --swing-closed) gets a rebalancer.swing row and allow_swap;
the tool prints the systemd drop-in that pins its two pools
(LPBOT_SWING_POOLS: the profile's process refuses a pair change to any
other pool). A dry run by default: it prints every row and writes nothing.
It never touches systemd itself.

    LPBOT_DSN=dbname=rebalancer python3 ops/add_profile.py \\
        --profile sol-swing --template sol-usdc \\
        --wallet sol-lp2 --address FogqBWLC4y94csrniURTbGrx7ff7jFa4e2qp1GsgyAmM \\
        --secret-env LPBOT_SOL2_KEY_PATH --label 'SOL swing wallet' \\
        --dex raydium-clmm --pool 8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj \\
        --execute-dexes raydium-clmm,orca --signer-env LPBOT_ORCA_ADAPTIVE=1 --deposit-mint USDC \\
        --swing-open 'orca 7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG' \\
        --swing-closed 'raydium-clmm 8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj' [--apply]
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
import swing  # noqa: E402

# What a new profile never takes from its template: identity, pool, wallet
# and the switches set here (deploy.sh's SKIP, plus allow_swap).
SKIP = {'id', 'name', 'active', 'pool', 'pair_label', 'token_a', 'token_b', 'dex', 'dexes', 'updated_at',
        'wallet_id', 'enabled', 'deposit_mint', 'residual_owner', 'mints', 'pool_pinned', 'regime_enabled',
        'rebalance_swap', 'execute_dexes', 'signer_env', 'allow_swap'}
# Venues a position can be held on: every signer but the routes.
VENUES = tuple(d for d in rebalancer.SIGNERS if d not in ('jupiter', 'orca-swap', 'payout', 'janitor'))
WALLET_ID = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
SECRET_ENV = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
# An address on each chain the wallets table allows (its wallets_check).
ADDRESS = {'solana': guards.is_address,
           'base': lambda s: isinstance(s, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', s) is not None}


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


def build(args, template, rec, wallet, others):
    """(wallet row or None, config row, swing row or None) for `args`.
    `template` is the template's config row (with its wallet's chain as
    'chain'), `rec` the pool record (dexes.pool), `wallet` the existing
    wallets row or None, `others` how many profiles the wallet holds
    already. Pure; raises Refused."""
    chain = (wallet or {}).get('chain') or args.chain
    if template.get('chain') != chain:
        raise Refused(f'template {template["name"]} is on {template.get("chain")}, the wallet on {chain}')
    if chain not in ADDRESS:
        raise Refused(f'unknown chain {chain!r}')
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
    norm = (lambda m: m.lower()) if chain == 'base' else (lambda m: m)
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
    swing_row = None
    if bool(args.swing_open) != bool(args.swing_closed):
        raise Refused('a swing needs both --swing-open and --swing-closed')
    if args.swing_open:
        op, cl = pool_spec(args.swing_open, chain), pool_spec(args.swing_closed, chain)
        if op[1] == cl[1] or args.pool not in (op[1], cl[1]):
            raise Refused('the swing pools differ, and the profile starts on one of them')
        if {op[0], cl[0]} - set(execute):
            raise Refused(f'every swing venue must be in --execute-dexes {execute}')
        if args.calendar not in swing.CALENDARS:
            raise Refused(f'unknown calendar {args.calendar!r}; known: {sorted(swing.CALENDARS)}')
        swing_row = {'profile': args.profile, 'open_dex': op[0], 'open_pool': op[1], 'closed_dex': cl[0],
                     'closed_pool': cl[1], 'calendar': args.calendar, 'lead_s': args.lead_s}
    row = {c: v for c, v in template.items() if c not in SKIP | {'chain'}}
    row.update(name=args.profile, active=False, pool=args.pool, dex=args.dex, dexes=execute, execute_dexes=execute,
               pair_label=f'{a["symbol"]}/{b["symbol"]}', token_a=a['symbol'], token_b=b['symbol'],
               wallet_id=args.wallet, enabled=True, deposit_mint=deposit, residual_owner=others == 0,
               mints=mints, pool_pinned=True, regime_enabled=True, rebalance_swap=True,
               allow_swap=swing_row is not None, signer_env=signer_env(args.signer_env) or None)
    return new_wallet, row, swing_row


def drop_in(swing_row):
    """The systemd drop-in that pins a swing profile's pools. Pure."""
    return (f'# /etc/systemd/system/lp-bot@{swing_row["profile"]}.service.d/swing.conf\n'
            f'# The pools a pair-changing move of {swing_row["profile"]} may name (config.SWING_POOLS).\n'
            # a comma, not a space: systemd splits an unquoted Environment= value at
            # whitespace, and the second pool would be dropped (2026-10-02)
            f'[Service]\nEnvironment=LPBOT_SWING_POOLS={swing_row["open_pool"]},{swing_row["closed_pool"]}\n')


def parse(argv):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--profile', required=True)
    p.add_argument('--template', required=True, help='profile whose tuning is copied')
    p.add_argument('--wallet', required=True, help='wallet id (new or existing)')
    p.add_argument('--chain', default='solana', choices=('solana', 'base'))
    p.add_argument('--address'), p.add_argument('--secret-env'), p.add_argument('--label')
    p.add_argument('--dex', required=True), p.add_argument('--pool', required=True)
    p.add_argument('--execute-dexes', help='comma list; default: --dex')
    p.add_argument('--signer-env', action='append', help='KEY=VALUE, from config.SIGNER_ENV_ALLOWED')
    p.add_argument('--deposit-mint', help='mint or symbol; default: the non-stable side')
    p.add_argument('--swing-open'), p.add_argument('--swing-closed')
    p.add_argument('--calendar', default='nyse'), p.add_argument('--lead-s', type=int, default=300)
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
        new_wallet, row, swing_row = build(args, dict(template), dexes.pool(args.dex, args.pool), wallet, others)
    except Refused as e:
        raise SystemExit(f'ERROR: {e}')
    print('wallet:', new_wallet or f'{args.wallet} (exists)')
    print('profile:', {k: row[k] for k in sorted(row)})
    print('swing:', swing_row)
    if swing_row:
        print(drop_in(swing_row))
    if not args.apply:
        print('DRY RUN: pass --apply to write')
        return 0
    keys = list(row)
    # jsonb columns (signer_env and any later one) go in as JSON; a dict is not a SQL value
    vals = [psycopg2.extras.Json(row[k]) if isinstance(row[k], dict) else row[k] for k in keys]
    with db.cursor(commit=True) as cur:
        if new_wallet:
            cur.execute('insert into wallets (id, chain, address, secret_env, label) values (%s,%s,%s,%s,%s)',
                        new_wallet)
        cur.execute(f'insert into config ({", ".join(keys)}) values ({", ".join(["%s"] * len(keys))})', vals)
        if swing_row:
            cols = list(swing_row)
            cur.execute(f'insert into swing ({", ".join(cols)}) values ({", ".join(["%s"] * len(cols))})',
                        [swing_row[c] for c in cols])
    print(f'written. Next: install the drop-in above (swing), then '
          f'sudo systemctl enable --now lp-bot@{args.profile}' + (' lp-swing' if swing_row else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
