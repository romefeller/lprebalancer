"""Register a vault its owner set up (chains/solana/vault/cli.mjs setup, then finish), or change
the payout wallets an existing one approves.

A vault wallet's row names the vault as its address (where the money is) and
the bot's payer key as its secret; the vault row names the smart account, its
owners and the policy key; the policy row names, per venue, the policies and
what the owners approved. A dry run by default: it prints every row and
writes nothing.

    # after the owner sent `cli.mjs setup ...`'s transactions: `cli.mjs finish ... > vault.json` (--json - reads it from stdin)
    LPBOT_DSN=dbname=rebalancer python3 ops/add_vault.py register --wallet tangem-vault \\
        --json vault.json --secret-env LPBOT_TANGEM_PAYER_KEY --policy-key-env LPBOT_TANGEM_POLICY_KEY [--apply]

    # a new profit or fee wallet: the bot stops acting until the owners approve
    LPBOT_DSN=dbname=rebalancer python3 ops/add_vault.py payouts --wallet tangem-vault \\
        --venue meteora-dlmm --payout <wallet>[,<wallet>] [--mints <mint>[,<mint>]] [--apply]

    # one more venue on the same vault (never a second vault): stage it, then store it
    node chains/solana/vault/cli.mjs add-venue --venue raydium-clmm --pools <pool> --execute > staged.json
    LPBOT_DSN=dbname=rebalancer python3 ops/add_vault.py venue --wallet tangem-vault --json staged.json [--apply]

    # one more pool on a venue the vault has: stage it, then store it when the owners are ready to sign
    node chains/solana/vault/cli.mjs add-pool --venue raydium-clmm --pools <pool> --execute > staged.json
    LPBOT_DSN=dbname=rebalancer python3 ops/add_vault.py pool --wallet tangem-vault --json staged.json [--apply]


Then the profile: ops/add_profile.py --wallet <id> --profit-wallet <w> ..., and for
an instance that is not the operator's own: --no-special --fee-wallet <w> --fee-bps 1000.
onboard/ does all of it for a vault an owner signs up for at /bot.
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import chains  # noqa: E402
import db  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / 'chains/solana/vault/cli.mjs'
WALLET_ID = re.compile(r'^[a-z0-9][a-z0-9-]{1,40}$')
ENV_NAME = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
# kind: the venues with vault rules. A 'squads' vault is on Solana.
VENUES = {'squads': set(json.loads((ROOT / 'chains/solana/vault/rules.json').read_text())['venues'])}


class Refused(ValueError):
    """An argument the tool will not write."""


def addresses(xs, what, chain='solana'):
    xs = list(xs or [])
    bad = [x for x in xs if not chains.is_address(chain, x)]
    if bad:
        raise Refused(f'{what}: not addresses: {bad}')
    return xs


def register_rows(args, connected):
    """(wallet row, vault row, [policy row per venue]) for `connected`, the
    JSON that `cli.mjs finish` printed: a Solana vault names each
    venue's policies and pools under `venues`, a Safe its one venue. Pure."""
    if not WALLET_ID.match(args.wallet):
        raise Refused(f'wallet id {args.wallet!r}')
    for name, v in (('--secret-env', args.secret_env), ('--policy-key-env', args.policy_key_env)):
        if not ENV_NAME.match(v or ''):
            raise Refused(f'{name} {v!r} is not an environment variable name')
    if connected.get('sent') is not True:
        raise Refused('the JSON is not a finished vault: run cli.mjs finish once the owner\'s set-up is on chain')
    kind = connected.get('kind') or 'squads'
    if kind not in VENUES:
        raise Refused(f'vault kind {kind!r}')
    chain = 'solana'
    venues = connected.get('venues')
    if venues is None:
        venues = {connected.get('venue'): {'policies': connected.get('policies'), 'pools': connected.get('pools')}}
    if not venues:
        raise Refused('no venues')
    for name in venues:
        if name not in VENUES[kind]:
            raise Refused(f'venue {name!r} has no vault rules')
    vault = addresses([connected.get('vault')], 'vault', chain)[0]
    settings = addresses([connected.get('settings')], 'settings', chain)[0]
    owners = addresses(connected.get('owners'), 'owners', chain)
    threshold = int(connected.get('threshold') or 0)
    if not 1 <= threshold <= len(owners):
        raise Refused(f'{len(owners)} owners at threshold {threshold}')
    bot = {str(connected.get(k)).lower() for k in ('payer', 'policyKey')}
    if {o.lower() for o in owners} & bot:
        raise Refused('a bot key is among the owners')
    lookup = addresses([connected['lookup']], 'lookup', chain)[0] if connected.get('lookup') else None
    wallet = (args.wallet, chain, vault, args.secret_env, args.label or args.wallet)
    vault_row = (args.wallet, kind, settings, owners, threshold, args.policy_key_env, lookup)
    payouts = addresses(connected.get('payouts'), 'payouts', chain)
    policies = []
    for name, v in venues.items():
        row = (args.wallet, name, addresses(v.get('policies'), 'policies', chain), addresses(v.get('pools'), 'pools', chain), payouts)
        if not row[2] or not row[3]:
            raise Refused(f'{name}: no policies or no pools')
        policies.append(row)
    return wallet, vault_row, policies


def payout_accounts(wallets, mints):
    """The payout token accounts of `wallets` for `mints`, derived by the CLI."""
    args = ['node', str(CLI), 'accounts', '--wallets', ','.join(wallets)] + (['--mints', ','.join(mints)] if mints else [])
    out = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout
    return addresses(json.loads(out.strip().splitlines()[-1]), 'payout accounts')


def insert_rows(cur, wallet, vault_row, policies):
    """Write the rows of register_rows() with `cur`: the wallet, the vault and
    one policy row per venue. The caller commits."""
    cur.execute('insert into wallets (id, chain, address, secret_env, label) values (%s, %s, %s, %s, %s)', wallet)
    cur.execute('insert into vaults (wallet_id, kind, settings, owners, threshold, policy_key_env, lookup) '
                'values (%s, %s, %s, %s, %s, %s, %s)', vault_row)
    for policy in policies:
        cur.execute('insert into vault_policies (wallet_id, venue, policies, pools, payouts) values (%s, %s, %s, %s, %s)', policy)


def register(args):
    text = sys.stdin.read() if args.json == '-' else pathlib.Path(args.json).read_text()
    wallet, vault_row, policies = register_rows(args, json.loads(text))
    print('wallet:', wallet)
    print('vault:', vault_row)
    for policy in policies:
        print('policy:', policy)
    if not args.apply:
        print('DRY RUN: pass --apply to write.')
        return
    with db.cursor(commit=True) as cur:
        insert_rows(cur, wallet, vault_row, policies)
    print(f'written. Set {args.secret_env} and {args.policy_key_env} in the service environment, then add the profile.')


def payouts(args):
    v = db.vault_row(args.wallet)
    if not v or args.venue not in v['venues']:
        raise Refused(f'wallet {args.wallet} has no vault policy for {args.venue}')
    wallets = addresses([w for w in args.payout.split(',') if w], '--payout')
    mints = addresses([m for m in (args.mints or '').split(',') if m], '--mints')
    accounts = payout_accounts(wallets, mints)
    print('payouts now:', v['venues'][args.venue]['payouts'])
    print('payouts new:', accounts)
    if not args.apply:
        print('DRY RUN: pass --apply to write.')
        return
    with db.cursor(commit=True) as cur:
        cur.execute('update vault_policies set payouts = %s, updated_at = now() where wallet_id = %s and venue = %s',
                    (accounts, args.wallet, args.venue))
    print('written. The bot stops acting for this vault until the owners approve: '
          'node chains/solana/vault/cli.mjs request approve --owner <owner> --payer <payer> --policy-key <policy key>, '
          'signed on owner/index.html.')


def venue_row(v, staged):
    """The policy row of a venue staged on vault `v` (db.vault_row) from what
    `cli.mjs add-venue` printed. Refused: another vault's staging, a venue
    the vault has, a dry run of the lookup accounts it needs. Pure."""
    if staged.get('settings') != v['settings']:
        raise Refused(f"the staging is for the vault {staged.get('settings')}, not {v['settings']}")
    name = staged.get('venue')
    if name not in VENUES[v['kind']] or v['kind'] != 'squads':
        raise Refused(f'venue {name!r} has no Squads vault rules')
    if name in v['venues']:
        raise Refused(f'the vault has {name} already')
    if staged.get('sent') is not True and staged.get('lookupAdds'):
        raise Refused(f"the lookup table lacks {staged['lookupAdds']} of the venue's accounts: run add-venue with --execute first")
    policies = addresses(staged.get('policies'), 'policies')
    pools = addresses(staged.get('pools'), 'pools')
    if not policies or not pools:
        raise Refused(f'{name}: no policies or no pools')
    return (v['wallet_id'], name, policies, pools, addresses(staged.get('payouts'), 'payouts'))


def venue(args):
    """Store a venue staged on an existing vault. The bot acts on it once the
    owners approve (Approve bot rules); until then its signer refuses."""
    v = db.vault_row(args.wallet)
    if not v:
        raise Refused(f'wallet {args.wallet} has no vault')
    text = sys.stdin.read() if args.json == '-' else pathlib.Path(args.json).read_text()
    row = venue_row(v, json.loads(text))
    print('policy:', row)
    if not args.apply:
        print('DRY RUN: pass --apply to write.')
        return
    with db.cursor(commit=True) as cur:
        cur.execute('insert into vault_policies (wallet_id, venue, policies, pools, payouts) values (%s, %s, %s, %s, %s)', row)
    print(f'written. Add {row[1]} to the profile\'s execute_dexes; the owners approve with Approve bot rules.')


def pool_row(v, staged):
    """The policy row of a venue the vault has, with the pools `cli.mjs add-pool`
    staged: (policies, pools) to store. Refused: another vault's staging, a
    venue the vault has not, a row that drops or reorders a policy or a pool
    the vault approved, nothing added, a dry run of the lookup accounts it
    needs, payouts that differ. Pure."""
    if staged.get('settings') != v['settings']:
        raise Refused(f"the staging is for the vault {staged.get('settings')}, not {v['settings']}")
    name = staged.get('venue')
    if v['kind'] != 'squads' or name not in v['venues']:
        raise Refused(f'the vault has no {name!r} policy to add pools to (add-venue stages a new venue)')
    have = v['venues'][name]
    if staged.get('sent') is not True and staged.get('lookupAdds'):
        raise Refused(f"the lookup table lacks {staged['lookupAdds']} of the pools' accounts: run add-pool with --execute first")
    policies = addresses(staged.get('policies'), 'policies')
    pools = addresses(staged.get('pools'), 'pools')
    if policies[:len(have['policies'])] != have['policies'] or len(policies) < len(have['policies']):
        raise Refused(f"the staged policies {policies} do not start with the vault's {have['policies']}")
    if pools[:len(have['pools'])] != have['pools'] or len(pools) <= len(have['pools']):
        raise Refused(f"the staged pools {pools} do not add to the vault's {have['pools']}")
    if addresses(staged.get('payouts'), 'payouts') != have['payouts']:
        raise Refused('the staged payouts differ from the approved ones: change payouts with the payouts command')
    return policies, pools


def pool(args):
    """Store more pools on a venue of the vault. The bot's signer refuses the
    venue from this write until the owners approve (Approve bot rules): run it
    only when they are ready to sign."""
    v = db.vault_row(args.wallet)
    if not v:
        raise Refused(f'wallet {args.wallet} has no vault')
    text = sys.stdin.read() if args.json == '-' else pathlib.Path(args.json).read_text()
    staged = json.loads(text)
    policies, pools = pool_row(v, staged)
    size = staged.get('policyBytes')
    if size is not None and not (isinstance(size, int) and 400 <= size <= 760):
        raise Refused(f'policyBytes {size!r}: an integer from 400 to 760')
    print('policy bytes now:', v.get('policy_bytes'), 'new:', size if size is not None else v.get('policy_bytes'))
    print('policies now:', v['venues'][staged['venue']]['policies'])
    print('policies new:', policies)
    print('pools now:', v['venues'][staged['venue']]['pools'])
    print('pools new:', pools)
    if not args.apply:
        print('DRY RUN: pass --apply to write.')
        return
    with db.cursor(commit=True) as cur:
        cur.execute('update vault_policies set policies = %s, pools = %s, updated_at = now() where wallet_id = %s and venue = %s',
                    (policies, pools, args.wallet, staged['venue']))
        if size is not None:
            # the size the staging compiled with: the policies the owners approve
            cur.execute('update vaults set policy_bytes = %s where wallet_id = %s', (size, args.wallet))
    print('written. The bot holds this venue until the owners approve with Approve bot rules on the owner page.')


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('register')
    r.add_argument('--wallet', required=True), r.add_argument('--json', required=True)
    r.add_argument('--secret-env', required=True), r.add_argument('--policy-key-env', required=True)
    r.add_argument('--label'), r.add_argument('--apply', action='store_true')
    q = sub.add_parser('payouts')
    q.add_argument('--wallet', required=True), q.add_argument('--venue', required=True)
    q.add_argument('--payout', required=True), q.add_argument('--mints')
    q.add_argument('--apply', action='store_true')
    s = sub.add_parser('venue')
    s.add_argument('--wallet', required=True), s.add_argument('--json', required=True)
    s.add_argument('--apply', action='store_true')
    o = sub.add_parser('pool')
    o.add_argument('--wallet', required=True), o.add_argument('--json', required=True)
    o.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    try:
        {'register': register, 'payouts': payouts, 'venue': venue, 'pool': pool}[args.cmd](args)
    except Refused as e:
        raise SystemExit(f'refused: {e}')


if __name__ == '__main__':
    main(sys.argv[1:])
