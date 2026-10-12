"""The onboarding server's database side: onboard/server.mjs runs one command
per call, JSON in on stdin, one JSON line out on stdout. A refusal is
{"error": "..."} and exit status 2.

    taken     {name, wallet_id}          is the name or wallet id in use?
    owned     {owner}                    how many vaults this owner has or is creating
    sign      {chain, owner, name, ...}  record a signed request (state signed)
    paid      {id, payment}              its gas payment was verified on chain (state connecting)
    resume    {id}                       a stuck request goes again, with the payment it has
    connected {id, connected}            the vault exists: what its connect printed
    stuck     {id, error, connected?}    a step after the payment failed; resumable
    register  {id, connected, ...}       the vault's rows and the profile, in ONE transaction
    started   {id}                       the profile's service was started
    list      {owner}                    this owner's requests, with their vault rows
    get       {name}                     one request by profile name, with its vault rows

It only ever inserts new rows (and updates its own onboard rows): it never
changes a profile, wallet or vault it did not create.
"""
import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ops'))
import db  # noqa: E402

# A request that holds its name: anything but failed, and a signed one only
# until its intent expires (it was never paid).
HOLDS = "state <> 'failed' and not (state = 'signed' and expires_at < now())"


class Refused(ValueError):
    """A request the store will not write."""


def same_owner(column):
    """SQL true when `column` is the owner given as three parameters: a
    Solana address exactly, an EVM address in any letter case."""
    return f"({column} = %s or (%s ~ '^0x' and lower({column}) = lower(%s)))"


def taken(cur, name, wallet_id):
    """Whether `name` is a profile, a wallet id or a live request, or
    `wallet_id` a wallet id or a profile name."""
    cur.execute('select 1 from config where name in (%s, %s) union all '
                'select 1 from wallets where id in (%s, %s) union all '
                f'select 1 from onboard where {HOLDS} and (name = %s or wallet_id = %s)',
                (name, wallet_id, name, wallet_id, name, wallet_id))
    return cur.fetchone() is not None


def owned(cur, owner):
    """Vaults `owner` owns, plus its requests not yet registered."""
    cur.execute(f'select count(*) n from vaults where exists (select 1 from unnest(owners) o where {same_owner("o")})',
                (owner,) * 3)
    n = cur.fetchone()['n']
    cur.execute(f"select count(*) n from onboard where {same_owner('owner')} and {HOLDS} "
                "and state in ('signed', 'connecting', 'stuck')", (owner,) * 3)
    return n + cur.fetchone()['n']


def sign(cur, r):
    """A signed request, before its payment. A signed request for the same
    name that expired unpaid gives the name up first."""
    cur.execute("update onboard set state = 'failed', error = 'expired unpaid', updated_at = now() "
                "where name = %s and state = 'signed' and expires_at < now()", (r['name'],))
    if taken(cur, r['name'], r['wallet_id']):
        raise Refused(f'the name {r["name"]} is taken')
    cur.execute('insert into onboard (chain, owner, name, wallet_id, token, amount, deposit, gas, payer, profit_wallet, '
                'special, fee_bps, fee_wallet, intent, signature, nonce, expires_at) values (%s, %s, %s, %s, %s, %s, %s, %s, '
                '%s, %s, %s, %s, %s, %s, %s, %s, to_timestamp(%s)) returning id',
                (r['chain'], r['owner'], r['name'], r['wallet_id'], r['token'], r['amount'], r['deposit'], r['gas'], r['payer'],
                 r['profit_wallet'], r['special'], r['fee_bps'], r['fee_wallet'], r['intent'], r['signature'], r['nonce'],
                 r['expires'] / 1000))
    return {'id': cur.fetchone()['id']}


def paid(cur, r):
    """The request's payment, verified by the server: it moves to connecting.
    A payment any request already holds is refused."""
    cur.execute('select name from onboard where payment = %s', (r['payment'],))
    other = cur.fetchone()
    if other:
        raise Refused(f'this payment was already used (request {other["name"]})')
    cur.execute("update onboard set state = 'connecting', payment = %s, updated_at = now() where id = %s and state = 'signed'",
                (r['payment'], r['id']))
    if cur.rowcount != 1:
        raise Refused(f'request {r["id"]} is not waiting for a payment')
    return {'paid': True}


def resume(cur, r):
    """A paid request whose setup stopped goes again (the server knows it is
    not running)."""
    cur.execute("update onboard set state = 'connecting', error = null, updated_at = now() "
                "where id = %s and state in ('stuck', 'connecting')", (r['id'],))
    if cur.rowcount != 1:
        raise Refused(f'request {r["id"]} is not paid and unfinished')
    return {'resumed': True}


def connected(cur, r):
    cur.execute("update onboard set connected = %s, vault = %s, updated_at = now() where id = %s and state = 'connecting'",
                (json.dumps(r['connected']), r['connected'].get('vault'), r['id']))
    return {'saved': cur.rowcount == 1}


def stuck(cur, r):
    cur.execute("update onboard set state = 'stuck', error = %s, connected = coalesce(%s, connected), updated_at = now() "
                "where id = %s and state = 'connecting'",
                (str(r['error'])[:2000], json.dumps(r['connected']) if r.get('connected') else None, r['id']))
    return {'stuck': cur.rowcount == 1}


def profile_args(p, wallet_id, chain):
    """add_profile's arguments for the request's profile. Pure."""
    return argparse.Namespace(
        profile=p['name'], template=p['template'], wallet=wallet_id, chain=chain, cross_chain=False,
        payout_mint=None, max_usd=None, address=None, secret_env=None, label=None, dex=p['dex'], pool=p['pool'],
        execute_dexes=','.join(p['execute_dexes']), signer_env=None, deposit_mint=None, swing_open=None,
        swing_closed=None, calendar='nyse', lead_s=300, profit_wallet=p['profit_wallet'],
        special=p['special'], fee_bps=p['fee_bps'] or None, fee_wallet=p['fee_wallet'])


def with_settings(row, settings, skip):
    """The profile row with the chain's onboarding settings (onboard.json
    chains.<chain>.settings) over the template's tuning. Only tuning columns the
    row already has: never its identity, pool, wallet or the signed terms. Pure."""
    signed = {'profit_wallet', 'special', 'fee_bps', 'fee_wallet'}
    bad = sorted(k for k in settings if k not in row or k in skip or k in signed)
    if bad:
        raise Refused(f'onboarding settings name no tuning column: {", ".join(bad)}')
    return {**row, **settings}


def register(cur, r):
    """The vault's rows (add_vault: wallet, vault, a policy row per venue) and the profile (add_profile, from
    its template), or nothing: one transaction."""
    # add_profile imports config, which loads a profile at import: the template's.
    os.environ.setdefault('LPBOT_PROFILE', r['profile']['template'])
    import add_profile
    import add_vault
    cur.execute("select * from onboard where id = %s and state = 'connecting' for update", (r['id'],))
    row = cur.fetchone()
    if not row:
        raise Refused(f'request {r["id"]} is not connecting')
    c = r['connected']
    # what the owner signed, from the row: never from the caller
    p = dict(r['profile'], name=row['name'], profit_wallet=row['profit_wallet'], special=row['special'],
             fee_bps=row['fee_bps'], fee_wallet=row['fee_wallet'])
    if r['profile'].get('name') != row['name'] or r['wallet_id'] != row['wallet_id']:
        raise Refused('the profile or wallet differs from the signed request')
    if [o.lower() for o in c.get('owners') or []] != [row['owner'].lower()]:
        raise Refused(f'the vault\'s owners {c.get("owners")} are not the signer {row["owner"]}')
    vargs = argparse.Namespace(wallet=r['wallet_id'], secret_env=r['secret_env'], policy_key_env=r['policy_key_env'],
                               label=r.get('label') or p['name'])
    try:
        wallet, vault_row, policies = add_vault.register_rows(vargs, c)
    except add_vault.Refused as e:
        raise Refused(str(e)) from None
    if wallet[1] != row['chain']:
        raise Refused(f'the vault is on {wallet[1]}, the request on {row["chain"]}')
    cur.execute('select c.*, w.chain from config c left join wallets w on w.id = c.wallet_id where c.name = %s',
                (p['template'],))
    template = cur.fetchone()
    if not template:
        raise Refused(f'template profile {p["template"]} not found')
    add_vault.insert_rows(cur, wallet, vault_row, policies)
    cur.execute('select * from wallets where id = %s', (r['wallet_id'],))
    try:
        _, prow, _ = add_profile.build(profile_args(p, r['wallet_id'], row['chain']), dict(template), p['rec'],
                                       dict(cur.fetchone()), 0, vault=True)
    except add_profile.Refused as e:
        raise Refused(str(e)) from None
    prow = with_settings(prow, p.get('settings') or {}, add_profile.SKIP)
    add_profile.insert_rows(cur, None, prow, None)
    cur.execute("update onboard set state = 'registered', vault = %s, connected = %s, updated_at = now() where id = %s",
                (c['vault'], json.dumps(c), r['id']))
    return {'profile': p['name'], 'wallet_id': r['wallet_id'], 'vault': c['vault'],
            'special': prow['special'], 'fee_bps': prow['fee_bps'], 'profit_wallet': prow['profit_wallet']}


def started(cur, r):
    cur.execute("update onboard set state = 'started', updated_at = now() where id = %s and state in "
                "('registered', 'started')", (r['id'],))
    return {'started': cur.rowcount == 1}


def _with_vault(cur, rows):
    out = []
    for o in rows:
        o = dict(o, amount=str(o['amount']), deposit=str(o['deposit']), gas=str(o['gas']),
                 created_at=o['created_at'].timestamp(), expires_at=o['expires_at'].timestamp(), updated_at=None)
        o['vault_row'] = None
        if o['wallet_id'] and o['state'] in ('registered', 'started'):
            v = db.vault_row(o['wallet_id'])
            o['vault_row'] = v and dict(v, created_at=None)
        out.append(o)
    return out


def listing(cur, r):
    cur.execute(f"select * from onboard where {same_owner('owner')} and state <> 'failed' order by id", (r['owner'],) * 3)
    return {'requests': _with_vault(cur, cur.fetchall())}


def get(cur, r):
    cur.execute("select * from onboard where name = %s and state <> 'failed'", (r['name'],))
    rows = _with_vault(cur, cur.fetchall())
    return {'request': rows[0] if rows else None}


COMMANDS = {
    'taken': lambda cur, r: {'taken': taken(cur, r['name'], r['wallet_id'])},
    'owned': lambda cur, r: {'count': owned(cur, r['owner'])},
    'sign': sign, 'paid': paid, 'resume': resume, 'connected': connected, 'stuck': stuck, 'register': register,
    'started': started, 'list': listing, 'get': get,
}
WRITES = {'sign', 'paid', 'resume', 'connected', 'stuck', 'register', 'started'}


def run(command, request):
    """One command; the result as a dict. Raises Refused."""
    fn = COMMANDS.get(command)
    if not fn:
        raise Refused(f'unknown command {command!r}')
    with db.cursor(commit=command in WRITES) as cur:
        return fn(cur, request)


def main(argv):
    try:
        out = run(argv[0] if argv else '', json.loads(sys.stdin.read() or '{}'))
    except (Refused, KeyError) as e:
        print(json.dumps({'error': str(e) if isinstance(e, Refused) else f'missing field {e}'}))
        return 2
    except db.psycopg2.IntegrityError as e:
        print(json.dumps({'error': f'refused by the database: {e.diag.message_primary}'}))
        return 2
    print(json.dumps(out, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
