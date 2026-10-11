"""Register a connected vault (chains/solana/vault/cli.mjs connect), or change
the payout wallets an existing one approves.

A vault wallet's row names the vault as its address (where the money is) and
the bot's payer key as its secret; the vault row names the smart account, its
owners and the policy key; the policy row names, per venue, the policies and
what the owners approved. A dry run by default: it prints every row and
writes nothing.

    # after `cli.mjs connect ... --execute > vault.json`
    LPBOT_DSN=dbname=rebalancer python3 ops/add_vault.py register --wallet tangem-vault \\
        --json vault.json --secret-env LPBOT_TANGEM_PAYER_KEY --policy-key-env LPBOT_TANGEM_POLICY_KEY [--apply]

    # a new profit or fee wallet: the bot stops acting until the owners approve
    LPBOT_DSN=dbname=rebalancer python3 ops/add_vault.py payouts --wallet tangem-vault \\
        --venue meteora-dlmm --payout <wallet>[,<wallet>] [--mints <mint>[,<mint>]] [--apply]

A Safe vault (chains/evm/vault/cli.mjs connect --execute > vault.json) registers the
same way. It has one key: its --policy-key-env is its --secret-env. Its payout wallets
are in the bot's role, so a new payout wallet is a new connect.

Then the profile: ops/add_profile.py --wallet <id> ..., and for an instance
that is not the operator's own: db.py set <profile> special=false
fee_wallet=<w> fee_bps=1000 (in that order).
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
# kind: the venues with vault rules. A 'squads' vault is on Solana; a 'safe' one names its
# EVM chain in the connect JSON (chains/evm/vault/cli.mjs).
VENUES = {'squads': set(json.loads((ROOT / 'chains/solana/vault/rules.json').read_text())['venues']),
          'safe': set(json.loads((ROOT / 'chains/evm/vault/rules.json').read_text())['venues'])}


class Refused(ValueError):
    """An argument the tool will not write."""


def addresses(xs, what, chain='solana'):
    xs = list(xs or [])
    bad = [x for x in xs if not chains.is_address(chain, x)]
    if bad:
        raise Refused(f'{what}: not addresses: {bad}')
    return xs


def register_rows(args, connected):
    """(wallet row, vault row, policy row) for `connected`, the JSON that
    `cli.mjs connect --execute` printed. Pure."""
    if not WALLET_ID.match(args.wallet):
        raise Refused(f'wallet id {args.wallet!r}')
    for name, v in (('--secret-env', args.secret_env), ('--policy-key-env', args.policy_key_env)):
        if not ENV_NAME.match(v or ''):
            raise Refused(f'{name} {v!r} is not an environment variable name')
    if connected.get('sent') is not True:
        raise Refused('the JSON is a dry run: connect with --execute first')
    kind = connected.get('kind') or 'squads'
    if kind not in VENUES:
        raise Refused(f'vault kind {kind!r}')
    chain = 'solana' if kind == 'squads' else connected.get('chain')
    if kind == 'safe' and (chain == 'solana' or chain not in chains.ADDRESS):
        raise Refused(f'a Safe vault on chain {chain!r}')
    if kind == 'safe' and args.policy_key_env != args.secret_env:
        raise Refused('a Safe vault has one key: --policy-key-env must be --secret-env')
    if connected.get('venue') not in VENUES[kind]:
        raise Refused(f'venue {connected.get("venue")!r} has no vault rules')
    vault = addresses([connected.get('vault')], 'vault', chain)[0]
    settings = addresses([connected.get('settings')], 'settings', chain)[0]
    owners = addresses(connected.get('owners'), 'owners', chain)
    threshold = int(connected.get('threshold') or 0)
    if not 1 <= threshold <= len(owners):
        raise Refused(f'{len(owners)} owners at threshold {threshold}')
    bot = {str(connected.get(k)).lower() for k in ('payer', 'policyKey')}
    if {o.lower() for o in owners} & bot:
        raise Refused('a bot key is among the owners')
    if kind == 'safe' and connected.get('lookup'):
        raise Refused('a Safe vault has no lookup table')
    lookup = addresses([connected['lookup']], 'lookup', chain)[0] if connected.get('lookup') else None
    wallet = (args.wallet, chain, vault, args.secret_env, args.label or args.wallet)
    vault_row = (args.wallet, kind, settings, owners, threshold, args.policy_key_env, lookup)
    policy = (args.wallet, connected['venue'], addresses(connected.get('policies'), 'policies', chain),
              addresses(connected.get('pools'), 'pools', chain), addresses(connected.get('payouts'), 'payouts', chain))
    if not policy[2] or not policy[3]:
        raise Refused('no policies or no pools')
    return wallet, vault_row, policy


def payout_accounts(wallets, mints):
    """The payout token accounts of `wallets` for `mints`, derived by the CLI."""
    args = ['node', str(CLI), 'accounts', '--wallets', ','.join(wallets)] + (['--mints', ','.join(mints)] if mints else [])
    out = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout
    return addresses(json.loads(out.strip().splitlines()[-1]), 'payout accounts')


def register(args):
    connected = json.loads(pathlib.Path(args.json).read_text())
    wallet, vault_row, policy = register_rows(args, connected)
    print('wallet:', wallet)
    print('vault:', vault_row)
    print('policy:', policy)
    if not args.apply:
        print('DRY RUN: pass --apply to write.')
        return
    with db.cursor(commit=True) as cur:
        cur.execute('insert into wallets (id, chain, address, secret_env, label) values (%s, %s, %s, %s, %s)', wallet)
        cur.execute('insert into vaults (wallet_id, kind, settings, owners, threshold, policy_key_env, lookup) '
                    'values (%s, %s, %s, %s, %s, %s, %s)', vault_row)
        cur.execute('insert into vault_policies (wallet_id, venue, policies, pools, payouts) values (%s, %s, %s, %s, %s)', policy)
    print(f'written. Set {args.secret_env} and {args.policy_key_env} in the service environment, then add the profile.')


def payouts(args):
    v = db.vault_row(args.wallet)
    if not v or args.venue not in v['venues']:
        raise Refused(f'wallet {args.wallet} has no vault policy for {args.venue}')
    if v['kind'] == 'safe':
        # The payout wallets are in the role's transfer scope: the owners change them on the
        # Safe, so a new list is a new connect, not a row edit.
        raise Refused('a Safe vault changes its payout wallets by a new connect (chains/evm/vault/cli.mjs)')
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
    args = p.parse_args(argv)
    try:
        (register if args.cmd == 'register' else payouts)(args)
    except Refused as e:
        raise SystemExit(f'refused: {e}')


if __name__ == '__main__':
    main(sys.argv[1:])
