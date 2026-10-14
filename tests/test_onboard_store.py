"""onboard/store.py: the onboarding server's database side. Names it refuses,
the vaults it counts per owner, and the one transaction that writes a vault
and its profile, or nothing; the genesis profiles are never touched."""
import contextlib
import importlib.util
import io
import json
import pathlib
import time
import unittest

import _fixtures  # noqa: F401  (the test database guard)

import db  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('onboard_store', ROOT / 'onboard' / 'store.py')
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)

SOLANA = json.loads((ROOT / 'chains/solana/solana.json').read_text())
P = SOLANA['programs']
SOL, USDC = SOLANA['native_mint'], SOLANA['usdc_mint']
OPERATOR = SOLANA['operator_profit_wallet']
POOL = SOLANA['sol_usdc_pools']['meteora-dlmm']
# Addresses only by their shape: the rows never reach a chain here.
OWNER, PROFIT, PAYER, POLICY_KEY = P['raydium_clmm'], P['byreal_clmm'], P['pancakeswap_v3'], P['jupiter_v6']
SETTINGS, VAULT, POLICY, LOOKUP = P['orca_whirlpool'], P['meteora_dlmm'], P['squads_smart_account'], P['memo']
EVM_OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
EVM_SAFE, EVM_ROLES = '0x1111111111111111111111111111111111111111', '0x2222222222222222222222222222222222222222'
EVM_POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7'
EVM_OPERATOR = '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142'
WPOL, USDT0 = '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270', '0xc2132d05d31c914a87c6611c10748aeb04b58e8f'
NAMES = ('ob-one', 'ob-two', 'ob-poly')


def clean():
    with db.cursor(commit=True) as cur:
        cur.execute('delete from onboard_deposits where onboard_id in (select id from onboard where name = any(%s))', (list(NAMES),))
        cur.execute('delete from owner_requests where profile = any(%s)', (list(NAMES),))
        cur.execute('delete from feed_latest where profile = any(%s)', (list(NAMES),))
        cur.execute('delete from onboard where name = any(%s)', (list(NAMES),))
        wallets = [n + '-vault' for n in NAMES]
        cur.execute('delete from config where name = any(%s) or name in (%s, %s)', (list(NAMES), 'ob-tpl-sol', 'ob-tpl-poly'))
        cur.execute('delete from vault_policies where wallet_id = any(%s)', (wallets,))
        cur.execute('delete from vaults where wallet_id = any(%s)', (wallets,))
        cur.execute('delete from wallets where id = any(%s) or id in (%s, %s)', (wallets, 'ob-tpl-lp', 'ob-tpl-plp'))


def templates():
    """Two templates, as the genesis profiles look: the test copy of sol-usdc
    with a Solana wallet, and one on a Polygon wallet."""
    with db.cursor(commit=True) as cur:
        cur.execute("insert into wallets (id, chain, address, secret_env, label) values "
                    "('ob-tpl-lp', 'solana', %s, 'WALLET_SECRET_PATH', 'tpl'), "
                    "('ob-tpl-plp', 'polygon', %s, 'LPBOT_POLYGON_KEY_PATH', 'tpl')", (PAYER, EVM_OPERATOR))
        for name, wallet, mint, profit in (('ob-tpl-sol', 'ob-tpl-lp', USDC, OPERATOR), ('ob-tpl-poly', 'ob-tpl-plp', USDT0, EVM_OPERATOR)):
            cur.execute("create temp table t as select * from config where name = 'sol-usdc'")
            cur.execute("alter table t drop column id")
            cur.execute("update t set name = %s, wallet_id = %s, active = false, payout_mint = %s, profit_wallet = %s, "
                        "payout_enabled = true, special = true, fee_bps = 0, fee_wallet = null, bands = '{1.02,1.05}'",
                        (name, wallet, mint, profit))
            cur.execute("select string_agg(quote_ident(attname), ', ') cols from pg_attribute where attrelid = 't'::regclass "
                        "and attnum > 0 and not attisdropped")
            cols = cur.fetchone()['cols']
            cur.execute(f'insert into config ({cols}) select {cols} from t')
            cur.execute('drop table t')


def genesis():
    with db.cursor() as cur:
        cur.execute("select * from config where name in ('sol-usdc', 'other') order by name")
        a = [dict(r) for r in cur.fetchall()]
        cur.execute("select * from wallets where id not like 'ob-%%' order by id")
        return a, [dict(r) for r in cur.fetchall()]


def connected(**over):
    c = {'sent': True, 'vault': VAULT, 'settings': SETTINGS, 'owners': [OWNER], 'threshold': 1, 'venue': 'meteora-dlmm',
         'policies': [POLICY], 'pools': [POOL], 'payouts': [PROFIT], 'payer': PAYER, 'policyKey': POLICY_KEY, 'lookup': LOOKUP}
    c.update(over)
    return c


def begin(name='ob-one', pay=True, expires_in=600, **over):
    """A signed request, and (pay) its verified payment: state connecting."""
    r = {'chain': 'solana', 'owner': OWNER, 'name': name, 'wallet_id': name + '-vault', 'token': 'SOL', 'amount': '1.5',
         'deposit': '1.355', 'gas': '0.145', 'payer': PAYER, 'profit_wallet': PROFIT, 'special': False, 'fee_bps': 1000,
         'fee_wallet': OPERATOR, 'intent': 'the message', 'signature': 'sig', 'nonce': name + '-nonce',
         'expires': (time.time() + expires_in) * 1000}
    r.update(over)
    i = store.run('sign', r)['id']
    if pay:
        store.run('paid', {'id': i, 'payment': r['nonce'] + '-payment'})
    return i


def register(id_, name='ob-one', profit=PROFIT, special=False, conn=None, template='ob-tpl-sol', profile=None, **over):
    r = {'id': id_, 'wallet_id': name + '-vault', 'label': name, 'secret_env': 'LPBOT_VAULT_SOL_PAYER_PATH',
         'policy_key_env': 'LPBOT_VAULT_SOL_POLICY_PATH', 'connected': conn or connected(),
         'profile': {'name': name, 'template': template, 'dex': 'meteora-dlmm', 'pool': POOL, 'execute_dexes': ['meteora-dlmm'],
                     'rec': {'token_a': {'address': SOL, 'symbol': 'SOL'}, 'token_b': {'address': USDC, 'symbol': 'USDC'}},
                     'profit_wallet': profit, 'special': special, 'fee_bps': 0 if special else 1000,
                     'fee_wallet': None if special else OPERATOR}}
    r['profile'].update(profile or {})
    r.update(over)
    return store.run('register', r)


def row(sql, *args):
    with db.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


class Store(unittest.TestCase):
    def setUp(self):
        clean()
        templates()
        self.before = genesis()

    def tearDown(self):
        self.assertEqual(genesis(), self.before, 'a genesis profile or wallet changed')
        clean()

    def test_a_signed_request_becomes_a_vault_and_a_profile(self):
        i = begin()
        self.assertEqual(row('select state from onboard where id = %s', i)['state'], 'connecting')
        out = register(i)
        self.assertEqual(out, {'profile': 'ob-one', 'wallet_id': 'ob-one-vault', 'vault': VAULT, 'special': False,
                               'fee_bps': 1000, 'profit_wallet': PROFIT})
        w = row("select * from wallets where id = 'ob-one-vault'")
        self.assertEqual((w['chain'], w['address'], w['secret_env']), ('solana', VAULT, 'LPBOT_VAULT_SOL_PAYER_PATH'))
        v = db.vault_row('ob-one-vault')
        self.assertEqual((v['kind'], v['settings'], v['owners'], v['threshold'], v['policy_key_env'], v['lookup']),
                         ('squads', SETTINGS, [OWNER], 1, 'LPBOT_VAULT_SOL_POLICY_PATH', LOOKUP))
        self.assertEqual(v['venues'], {'meteora-dlmm': {'policies': [POLICY], 'pools': [POOL], 'payouts': [PROFIT]}})
        c = row("select * from config where name = 'ob-one'")
        t = row("select * from config where name = 'ob-tpl-sol'")
        self.assertEqual((c['wallet_id'], c['dex'], c['pool'], c['execute_dexes'], c['dexes']),
                         ('ob-one-vault', 'meteora-dlmm', POOL, ['meteora-dlmm'], ['meteora-dlmm']))
        self.assertEqual((c['profit_wallet'], c['special'], c['fee_bps'], c['fee_wallet']), (PROFIT, False, 1000, OPERATOR))
        self.assertEqual((c['enabled'], c['active'], c['pool_pinned'], c['residual_owner']), (True, False, True, True))
        self.assertEqual((c['pair_label'], c['mints'], c['deposit_mint']), ('SOL/USDC', [SOL, USDC], SOL))
        for k in ('bands', 'payout_mint', 'payout_enabled', 'max_usd', 'poll_seconds', 'regime_widths', 'harvest_interval_hours'):
            self.assertEqual(c[k], t[k], f'{k} is the template\'s')
        self.assertEqual(c['min_deploy_usd'], t['min_deploy_usd'], 'no settings: the template\'s')
        o = row('select * from onboard where id = %s', i)
        self.assertEqual((o['state'], o['vault'], o['connected']['settings']), ('registered', VAULT, SETTINGS))
        self.assertEqual(store.run('started', {'id': i}), {'started': True})
        self.assertEqual(row('select state from onboard where id = %s', i)['state'], 'started')
        got = store.run('get', {'name': 'ob-one'})['request']
        self.assertEqual((got['owner'], got['vault_row']['settings']), (OWNER, SETTINGS))
        self.assertEqual([r['name'] for r in store.run('list', {'owner': OWNER})['requests']], ['ob-one'])

    def test_a_vault_of_every_venue_gets_a_policy_row_per_venue(self):
        # a Solana connect names each venue's policies and pools; the profile opens on one and may run all
        pools = SOLANA['sol_usdc_pools']
        venues = {'raydium-clmm': {'policies': [P['token'], P['system']], 'pools': [pools['raydium-clmm']]},
                  'meteora-dlmm': {'policies': [POLICY], 'pools': [POOL]}}
        conn = connected(venues=venues)
        for k in ('venue', 'policies', 'pools'):
            conn.pop(k)
        i = begin()
        register(i, conn=conn, profile={'dex': 'raydium-clmm', 'pool': pools['raydium-clmm'], 'execute_dexes': ['raydium-clmm', 'meteora-dlmm']})
        self.assertEqual(db.vault_row('ob-one-vault')['venues'],
                         {v: dict(x, payouts=[PROFIT]) for v, x in venues.items()})
        c = row("select * from config where name = 'ob-one'")
        self.assertEqual((c['dex'], c['pool'], c['execute_dexes'], c['pool_pinned']),
                         ('raydium-clmm', pools['raydium-clmm'], ['raydium-clmm', 'meteora-dlmm'], True))

    def test_the_wallet_label_and_the_chain(self):
        i = begin()
        register(i, label='My Pool')
        self.assertEqual(row("select label from wallets where id = 'ob-one-vault'")['label'], 'My Pool')
        j = begin('ob-two')
        register(j, name='ob-two', label=None, conn=connected(vault=P['token_2022'], settings=P['associated_token'], policies=[P['compute_budget']]))
        self.assertEqual(row("select label from wallets where id = 'ob-two-vault'")['label'], 'ob-two', 'no label: the name')
        k = begin('ob-poly', chain='polygon')
        with self.assertRaisesRegex(store.Refused, 'the vault is on solana, the request on polygon'):
            register(k, name='ob-poly')

    def test_an_error_is_kept_short(self):
        i = begin()
        store.run('stuck', {'id': i, 'error': 'x' * 3000})
        self.assertEqual(len(row('select error from onboard where id = %s', i)['error']), 2000)

    def test_the_operators_own_wallet_is_special(self):
        out = register(begin(profit_wallet=OPERATOR, special=True, fee_bps=0, fee_wallet=None), profit=OPERATOR, special=True)
        self.assertEqual((out['special'], out['fee_bps'], out['profit_wallet']), (True, 0, OPERATOR))
        self.assertIsNone(row("select fee_wallet from config where name = 'ob-one'")['fee_wallet'])

    def test_a_polygon_safe(self):
        i = begin('ob-poly', chain='polygon', owner=EVM_OWNER, token='USDT0', amount='25', deposit='25', gas='4', payer=EVM_OPERATOR,
                  profit_wallet=EVM_OWNER, fee_wallet=EVM_OPERATOR)
        conn = {'sent': True, 'kind': 'safe', 'chain': 'polygon', 'venue': 'uniswap-v3-polygon', 'vault': EVM_SAFE, 'settings': EVM_SAFE,
                'owners': [EVM_OWNER], 'threshold': 1, 'policies': [EVM_ROLES], 'pools': [EVM_POOL],
                'payouts': [EVM_OWNER, EVM_OPERATOR], 'payer': EVM_OPERATOR, 'policyKey': EVM_OPERATOR, 'lookup': None}
        rec = {'token_a': {'address': WPOL, 'symbol': 'WPOL'}, 'token_b': {'address': USDT0, 'symbol': 'USDT0'}}
        store.run('register', {'id': i, 'wallet_id': 'ob-poly-vault', 'label': 'ob-poly', 'secret_env': 'LPBOT_VAULT_EVM_KEY_PATH',
                               'policy_key_env': 'LPBOT_VAULT_EVM_KEY_PATH', 'connected': conn,
                               'profile': {'name': 'ob-poly', 'template': 'ob-tpl-poly', 'dex': 'uniswap-v3-polygon', 'pool': EVM_POOL,
                                           'execute_dexes': ['uniswap-v3-polygon'], 'rec': rec, 'profit_wallet': EVM_OWNER,
                                           'special': False, 'fee_bps': 1000, 'fee_wallet': EVM_OPERATOR}})
        self.assertEqual(db.vault_row('ob-poly-vault')['kind'], 'safe')
        c = row("select * from config where name = 'ob-poly'")
        self.assertEqual((c['payout_mint'], c['deposit_mint'], c['profit_wallet'], c['fee_wallet']), (USDT0, WPOL, EVM_OWNER, EVM_OPERATOR))
        self.assertEqual(store.run('owned', {'owner': EVM_OWNER.lower()}), {'count': 1}, 'an EVM owner in any case')

    def test_nothing_is_written_when_any_part_is_refused(self):
        cases = [({'conn': connected(owners=[PROFIT])}, 'not the signer'),
                 ({'conn': connected(owners=[OWNER, PROFIT])}, 'not the signer'),
                 ({'conn': connected(sent=False)}, 'not a finished vault'),
                 ({'template': 'nope'}, 'template profile nope not found'),
                 ({'wallet_id': 'ob-two-vault'}, 'differs from the signed request'),
                 ({'profile': {'name': 'ob-two'}}, 'differs from the signed request'),
                 ({'conn': connected(owners=None)}, 'not the signer'),
                 ({'conn': connected(owners=[OWNER], venue='nowhere')}, 'no vault rules')]
        i = begin()
        for kw, msg in cases:
            with self.subTest(msg=msg), self.assertRaisesRegex(store.Refused, msg):
                register(i, **kw)
            self.assertIsNone(row("select 1 from wallets where id = 'ob-one-vault'"))
            self.assertIsNone(row("select 1 from config where name = 'ob-one'"))
            self.assertEqual(row('select state from onboard where id = %s', i)['state'], 'connecting')
        # the caller's profile values never count: the signed row's do
        out = register(i, profile={'profit_wallet': OPERATOR, 'special': True, 'fee_bps': 0, 'fee_wallet': None})
        self.assertEqual((out['profit_wallet'], out['special'], out['fee_bps']), (PROFIT, False, 1000))
        with self.assertRaisesRegex(store.Refused, 'is not connecting'):
            register(i)

    def test_names_are_taken_by_profiles_wallets_and_live_requests(self):
        t = lambda name, wallet='x-free-vault': store.run('taken', {'name': name, 'wallet_id': wallet})['taken']
        self.assertTrue(t('sol-usdc'))
        self.assertTrue(t('ob-tpl-lp'), 'a wallet id')
        self.assertTrue(t('free-name', 'sol-usdc'), 'a wallet id that is a profile name')
        self.assertFalse(t('free-name'))
        begin(pay=False)
        self.assertTrue(t('ob-one'), 'a signed request holds its name')
        self.assertTrue(t('zz', 'ob-one-vault'))
        with self.assertRaisesRegex(store.Refused, 'taken'):
            begin(nonce='another', pay=False)
        with self.assertRaises(db.psycopg2.IntegrityError):
            begin('ob-two', nonce='ob-one-nonce', pay=False)          # a nonce is used once

    def test_an_unpaid_request_gives_its_name_back_when_it_expires(self):
        old = begin(pay=False, expires_in=-1)
        self.assertFalse(store.run('taken', {'name': 'ob-one', 'wallet_id': 'x'})['taken'])
        self.assertEqual(store.run('owned', {'owner': OWNER}), {'count': 0})
        new = begin(nonce='fresh', pay=False)
        self.assertEqual(row('select state, error from onboard where id = %s', old), {'state': 'failed', 'error': 'expired unpaid'})
        self.assertEqual(row('select state from onboard where id = %s', new)['state'], 'signed')
        # a paid request never expires: its name stays
        clean(), templates()
        begin(expires_in=-1)
        self.assertTrue(store.run('taken', {'name': 'ob-one', 'wallet_id': 'x'})['taken'])

    def test_a_payment_serves_one_request_once(self):
        a = begin(pay=False)
        b = begin('ob-two', pay=False)
        self.assertEqual(store.run('paid', {'id': a, 'payment': 'P1'}), {'paid': True})
        self.assertEqual(row('select state, payment from onboard where id = %s', a), {'state': 'connecting', 'payment': 'P1'})
        with self.assertRaisesRegex(store.Refused, r'already used \(request ob-one\)'):
            store.run('paid', {'id': b, 'payment': 'P1'})
        with self.assertRaisesRegex(store.Refused, 'not waiting for a payment'):
            store.run('paid', {'id': a, 'payment': 'P2'})
        self.assertEqual(row('select state, payment from onboard where id = %s', b), {'state': 'signed', 'payment': None})
        with self.assertRaises(db.psycopg2.IntegrityError):
            with db.cursor(commit=True) as cur:                         # the table itself refuses a second use
                cur.execute("update onboard set payment = 'P1', state = 'connecting' where id = %s", (b,))

    def test_a_stuck_request_resumes_with_what_it_has(self):
        i = begin()
        with self.assertRaisesRegex(store.Refused, 'not paid and unfinished'):
            store.run('resume', {'id': begin('ob-two', pay=False)})
        self.assertEqual(store.run('connected', {'id': i, 'connected': connected()}), {'saved': True})
        self.assertEqual(store.run('stuck', {'id': i, 'error': 'database down'}), {'stuck': True})
        o = row('select * from onboard where id = %s', i)
        self.assertEqual((o['state'], o['error'], o['vault'], o['connected']['settings']), ('stuck', 'database down', VAULT, SETTINGS),
                         'stuck keeps the vault it had')
        self.assertEqual(store.run('stuck', {'id': i, 'error': 'again'}), {'stuck': False}, 'only a connecting request gets stuck')
        with self.assertRaisesRegex(store.Refused, 'is not connecting'):
            register(i)
        self.assertEqual(store.run('owned', {'owner': OWNER}), {'count': 2}, 'a stuck request counts')
        self.assertEqual(store.run('resume', {'id': i}), {'resumed': True})
        self.assertIsNone(row('select error from onboard where id = %s', i)['error'])
        self.assertEqual(register(i)['vault'], VAULT)
        with self.assertRaisesRegex(store.Refused, 'not paid and unfinished'):
            store.run('resume', {'id': i})
        self.assertEqual(store.run('connected', {'id': i, 'connected': connected(vault=LOOKUP)}), {'saved': False},
                         'a registered request keeps its vault')

    def test_a_request_keeps_its_planned_vault_and_is_planned_again_while_it_waits(self):
        plan = {'planned': {'seed': '42', 'settings': SETTINGS, 'vault': VAULT}}
        i = begin(pay=False, signature='', connected=plan)
        o = row('select signature, connected, state from onboard where id = %s', i)
        self.assertEqual((o['signature'], o['connected'], o['state']), ('', plan, 'signed'), 'no message signature: the set-up is the owner\'s')
        self.assertIsNone(row('select connected from onboard where id = %s', begin('ob-two', nonce='n2', pay=False))['connected'])
        # planned again: a fresh seed, today's gas and deposit, the name held longer
        later = (time.time() + 900) * 1000
        new = {'planned': {'seed': '43', 'settings': LOOKUP, 'vault': PROFIT}}
        self.assertEqual(store.run('planned', {'id': i, 'connected': new, 'gas': '0.1186', 'deposit': '1.3814', 'expires': later}), {'planned': True})
        o = row('select connected, gas, deposit, extract(epoch from expires_at) e from onboard where id = %s', i)
        self.assertEqual((o['connected'], str(o['gas']), str(o['deposit'])), (new, '0.1186', '1.3814'))
        self.assertAlmostEqual(float(o['e']), later / 1000, places=2)
        # only while it waits for its set-up
        store.run('paid', {'id': i, 'payment': 'P1'})
        with self.assertRaisesRegex(store.Refused, 'not waiting for its set-up'):
            store.run('planned', {'id': i, 'connected': new, 'gas': '1', 'deposit': '1', 'expires': later})
        with self.assertRaisesRegex(store.Refused, 'not waiting for its set-up'):
            store.run('planned', {'id': 999999, 'connected': new, 'gas': '1', 'deposit': '1', 'expires': later})

    def test_an_expired_request_takes_its_name_back_only_when_it_is_free(self):
        # tang0 (2026-10-13): a request signed under the old flow, expired, its owner back days later
        old = begin(pay=False, expires_in=-60)
        plan = {'planned': {'seed': '7', 'settings': SETTINGS, 'vault': VAULT}}
        later = (time.time() + 600) * 1000
        self.assertEqual(store.run('planned', {'id': old, 'connected': plan, 'gas': '0.1186', 'deposit': '1.3814', 'expires': later}), {'planned': True})
        self.assertTrue(store.run('taken', {'name': 'ob-one', 'wallet_id': 'x'})['taken'], 'held again')
        self.assertEqual(row('select state from onboard where id = %s', old)['state'], 'signed')
        # another took the name while it was free: refused, nothing written
        clean(), templates()
        old = begin(pay=False, expires_in=-60)
        other = begin(nonce='other', owner=PROFIT, pay=False)          # the expired one gave its name up
        self.assertEqual(row('select state from onboard where id = %s', old)['state'], 'failed')
        with self.assertRaisesRegex(store.Refused, 'not waiting for its set-up'):
            store.run('planned', {'id': old, 'connected': plan, 'gas': '1', 'deposit': '1', 'expires': later})
        # one expired, not yet given up, whose wallet id another request took meanwhile
        clean(), templates()
        old = begin(pay=False, expires_in=-60)
        begin('ob-two', nonce='n2', wallet_id='ob-one-vault', owner=PROFIT, pay=False)
        with self.assertRaisesRegex(store.Refused, 'was taken while this request waited'):
            store.run('planned', {'id': old, 'connected': plan, 'gas': '1', 'deposit': '1', 'expires': later})
        self.assertIsNone(row('select connected from onboard where id = %s', old)['connected'])
        self.assertTrue(other)

    def test_vaults_per_owner(self):
        self.assertEqual(store.run('owned', {'owner': OWNER}), {'count': 0})
        i = begin()
        self.assertEqual(store.run('owned', {'owner': OWNER}), {'count': 1}, 'a request still connecting counts')
        register(i)
        self.assertEqual(store.run('owned', {'owner': OWNER}), {'count': 1}, 'then its vault does, once')
        begin('ob-two', nonce='n2', pay=False)
        self.assertEqual(store.run('owned', {'owner': OWNER}), {'count': 2}, 'a signed request counts')
        self.assertEqual(store.run('owned', {'owner': OWNER.lower()}), {'count': 0}, 'a Solana address is case-sensitive')

    def test_the_command_line(self):
        for argv, stdin, code, out in ((['nope'], '{}', 2, 'unknown command'), (['taken'], '{}', 2, 'missing field'),
                                       (['taken'], '{"name": "sol-usdc", "wallet_id": "x"}', 0, '"taken": true')):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), unittest.mock.patch('sys.stdin', io.StringIO(stdin)):
                self.assertEqual(store.main(argv), code)
            self.assertIn(out, buf.getvalue())


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()


class Settings(unittest.TestCase):
    """onboard.json chains.<chain>.settings: tuning over the template, nothing else."""

    def setUp(self):
        clean()

    def tearDown(self):
        clean()

    def test_the_profile_takes_the_settings_and_a_bad_one_writes_nothing(self):
        templates()
        i = begin()
        register(i, profile={'settings': {'min_deploy_usd': 2}})
        self.assertEqual(float(row("select min_deploy_usd from config where name = 'ob-one'")['min_deploy_usd']), 2.0)
        clean()
        templates()
        i = begin()
        with self.assertRaisesRegex(store.Refused, 'pool'):
            register(i, profile={'settings': {'pool': 'X'}})
        self.assertIsNone(row("select 1 from config where name = 'ob-one'"))
        self.assertIsNone(row("select 1 from wallets where id = 'ob-one-vault'"))

    def test_tuning_columns_are_set_and_nothing_else_is(self):
        row = {'name': 'ob', 'pool': 'P', 'min_deploy_usd': 5, 'bands': [1], 'profit_wallet': 'W', 'special': True,
               'fee_bps': 0, 'fee_wallet': None}
        self.assertEqual(store.with_settings(row, {}, set()), row)
        got = store.with_settings(row, {'min_deploy_usd': 2}, {'name', 'pool'})
        self.assertEqual((got['min_deploy_usd'], got['bands'], row['min_deploy_usd']), (2, [1], 5))
        for bad in ('name', 'pool', 'profit_wallet', 'special', 'fee_bps', 'fee_wallet', 'no_such_column'):
            with self.assertRaisesRegex(store.Refused, bad):
                store.with_settings(row, {bad: 1}, {'name', 'pool'})


class OwnerPage(unittest.TestCase):
    """The owner's page (sql/040): deposits verified on chain set the cap,
    exit requests, the bot's latest rows."""

    def setUp(self):
        clean()
        templates()
        self.before = genesis()

    def tearDown(self):
        self.assertEqual(genesis(), self.before, 'a genesis profile or wallet changed')
        clean()

    def test_cap_usd_lets_all_of_the_capital_deploy(self):
        # capital = min(base, max_usd / (2 * side_cap)): the cap leaves every deposited dollar, times the headroom
        for total, side, head in ((2.0, 0.55, 3.0), (1400.0, 0.55, 3.0), (0.37, 0.6, 1.0), (123.456, 0.55, 2.5)):
            cap = store.cap_usd(total, side, head)
            self.assertGreaterEqual(cap / (2 * side), total * head - 1e-9)
            self.assertLess(cap / (2 * side), total * head + 0.01 / (2 * side) + 1e-9, 'rounded up by under a cent')
            self.assertEqual(round(cap, 2), cap)
        self.assertEqual(store.cap_usd(2.0, 0.55, 3.0), 6.6)
        for bad in ((0, 0.55, 3), (-1, 0.55, 3), (2, 0, 3), (2, 0.55, 0.5)):
            with self.assertRaises(store.Refused):
                store.cap_usd(*bad)

    def test_deposits_set_the_cap_once_per_transaction(self):
        i = begin()
        register(i)
        prices = {SOL: 100.0, USDC: 1.0}
        original = db.token_prices
        db.token_prices = lambda mints: {m: prices[m] for m in mints if m in prices}
        try:
            out = store.run('deposited', {'id': i, 'signature': 'sig-1', 'token': 'SOL', 'mint': SOL, 'amount': '1.355', 'headroom': 3})
            self.assertEqual(out, {'recorded': True, 'max_usd': store.cap_usd(135.5, 0.55, 3)})
            self.assertEqual(float(row("select max_usd from config where name = 'ob-one'")['max_usd']), out['max_usd'])
            again = store.run('deposited', {'id': i, 'signature': 'sig-1', 'token': 'SOL', 'mint': SOL, 'amount': '1.355', 'headroom': 3})
            self.assertEqual(again, {'recorded': False, 'max_usd': out['max_usd']}, 'the same transaction counts once')
            more = store.run('deposited', {'id': i, 'signature': 'sig-2', 'token': 'USDC', 'mint': USDC, 'amount': '1400', 'headroom': 3})
            self.assertEqual(more['max_usd'], store.cap_usd(1535.5, 0.55, 3), 'every deposit, at its own price')
            # an unpriced token is kept, adds no dollars
            prices.pop(SOL)
            store.run('deposited', {'id': i, 'signature': 'sig-3', 'token': 'SOL', 'mint': SOL, 'amount': '5', 'headroom': 3})
            self.assertEqual(float(row("select max_usd from config where name = 'ob-one'")['max_usd']), more['max_usd'])
            self.assertEqual(row('select count(*) n from onboard_deposits where onboard_id = %s', i)['n'], 3)
        finally:
            db.token_prices = original
        j = begin('ob-two')
        with self.assertRaisesRegex(store.Refused, 'has no vault yet'):
            store.run('deposited', {'id': j, 'signature': 'sig-x', 'token': 'SOL', 'mint': SOL, 'amount': '1', 'headroom': 3})

    def test_register_takes_a_provisional_cap_from_the_signed_deposit(self):
        original = db.token_prices
        db.token_prices = lambda mints: {m: 100.0 for m in mints}
        try:
            i = begin()
            register(i, cap={'mint': SOL, 'headroom': 3})
        finally:
            db.token_prices = original
        self.assertEqual(float(row("select max_usd from config where name = 'ob-one'")['max_usd']), store.cap_usd(135.5, 0.55, 3))
        j = begin('ob-two')
        register(j, name='ob-two', conn=connected(vault=P['token_2022'], settings=P['associated_token'], policies=[P['compute_budget']]))
        t = row("select max_usd from config where name = 'ob-tpl-sol'")['max_usd']
        self.assertEqual(row("select max_usd from config where name = 'ob-two'")['max_usd'], t, 'no cap asked: the template\'s')

    def test_a_deposit_under_the_template_capital_brings_the_capital_down(self):
        # 2026-10-14, tang0: 0.18 SOL capped max_usd under the template's capital_usd ($216),
        # and config_sane refused the register
        original = db.token_prices
        db.token_prices = lambda mints: {m: 1.0 for m in mints}
        try:
            i = begin()
            register(i, cap={'mint': SOL, 'headroom': 3})
            r = row("select capital_usd, max_usd from config where name = 'ob-one'")
            self.assertEqual(float(r['max_usd']), store.cap_usd(1.355, 0.55, 3))
            self.assertEqual(float(r['capital_usd']), float(r['max_usd']), 'the capital comes down with the cap')
            store.run('deposited', {'id': i, 'signature': 'sig-big', 'token': 'USDC', 'mint': USDC, 'amount': '1400', 'headroom': 3})
            r2 = row("select capital_usd, max_usd from config where name = 'ob-one'")
            self.assertEqual(float(r2['capital_usd']), float(r['capital_usd']), 'a larger cap never raises the capital')
            self.assertGreater(float(r2['max_usd']), float(r['max_usd']))
        finally:
            db.token_prices = original

    def test_an_exit_request_once_pending_and_done_by_the_loop_or_a_start(self):
        i = begin()
        register(i)
        self.assertEqual(store.run('exits', {'name': 'ob-one'}), {'exit': None})
        self.assertEqual(store.run('exit', {'name': 'ob-one', 'nonce': 'n1', 'signature': 's1'}), {'requested': True, 'new': True})
        self.assertEqual(store.run('exit', {'name': 'ob-one', 'nonce': 'n2', 'signature': 's2'}), {'requested': True, 'new': False},
                         'one pending at a time')
        pending = db.owner_exit_pending('ob-one')
        self.assertIsNotNone(pending)
        self.assertIsNone(store.run('exits', {'name': 'ob-one'})['exit']['done_at'])
        db.owner_request_done(pending, 'closed')
        e = store.run('exits', {'name': 'ob-one'})['exit']
        self.assertEqual(e['outcome'], 'closed')
        self.assertIsNotNone(e['done_at'])
        self.assertIsNone(db.owner_exit_pending('ob-one'))
        # a start after a new request cancels it: the owner wants the bot back
        store.run('exit', {'name': 'ob-one', 'nonce': 'n3', 'signature': 's3'})
        store.run('started', {'id': i})
        self.assertIsNone(db.owner_exit_pending('ob-one'))
        self.assertRegex(store.run('exits', {'name': 'ob-one'})['exit']['outcome'], 'cancelled')
        with self.assertRaisesRegex(store.Refused, 'no profile'):
            store.run('exit', {'name': 'ob-two', 'nonce': 'n4', 'signature': 's4'})

    def test_the_feed_rows_the_page_shows(self):
        i = begin()
        register(i)
        self.assertEqual(store.run('feed', {'name': 'ob-one'}), {'feed': {}})
        book = {'event': 'in_band', 'fees_total_usd': 1.2, 'equity_usd': 10}
        db.feed_latest_put('ob-one', 'book', json.dumps(book))
        db.feed_latest_put('ob-one', 'DAILY', json.dumps({'event': 'DAILY', 'day': 'x'}))
        db.feed_latest_put('ob-one', 'book', json.dumps(dict(book, equity_usd=11)))
        f = store.run('feed', {'name': 'ob-one'})['feed']
        self.assertEqual(f['book']['row']['equity_usd'], 11, 'the newest replaces the last')
        self.assertEqual(f['DAILY']['row']['day'], 'x')
        self.assertEqual(store.feed_kind(book), 'book')
        self.assertEqual(store.feed_kind({'event': 'DAILY'}), 'DAILY')
        self.assertEqual(store.feed_kind({'event': 'REVOKED'}), 'REVOKED')
        self.assertIsNone(store.feed_kind({'event': 'in_band', 'fees_total_usd': 1}))
        self.assertIsNone(store.feed_kind('not a row'))

    def test_the_feed_file_when_the_database_has_none(self):
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp())
        original = store.ROOT
        try:
            store.ROOT = d
            (d / 'run' / 'ob-two').mkdir(parents=True)
            lines = [json.dumps({'event': 'DAILY', 'day': 'old'}), 'not json', json.dumps({'event': 'in_band', 'fees_total_usd': 1, 'equity_usd': 5}),
                     json.dumps({'event': 'DAILY', 'day': 'new'}), json.dumps({'event': 'TAPE_SOURCE'})]
            (d / 'run' / 'ob-two' / 'events.jsonl').write_text('\n'.join(lines) + '\n')
            f = store.feed_from_file('ob-two')
            self.assertEqual((f['book']['row']['equity_usd'], f['DAILY']['row']['day']), (5, 'new'))
            self.assertEqual(store.feed_from_file('nobody'), {})
        finally:
            store.ROOT = original


class OwnerPageEdges(unittest.TestCase):
    """The edges of the owner's page rows: no price, no profile, the
    feed file read from its newest end, the database first."""

    def setUp(self):
        clean()
        templates()

    def tearDown(self):
        clean()

    def test_no_dollar_figure_keeps_the_cap(self):
        i = begin()
        register(i)
        before = row("select max_usd from config where name = 'ob-one'")['max_usd']
        original = db.token_prices
        db.token_prices = lambda mints: {}
        try:
            out = store.run('deposited', {'id': i, 'signature': 'sig-n', 'token': 'SOL', 'mint': SOL, 'amount': '1', 'headroom': 3})
        finally:
            db.token_prices = original
        self.assertEqual(out, {'recorded': True, 'max_usd': None})
        self.assertEqual(row("select max_usd from config where name = 'ob-one'")['max_usd'], before)
        with db.cursor(commit=True) as cur:
            self.assertIsNone(store.cap(cur, i, 3), 'nothing priced, nothing provisional')
            self.assertIsNone(store.cap(cur, i, 3, provisional=None))

    def test_a_request_without_a_profile_gets_no_cap(self):
        i = begin()
        register(i)
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name = 'ob-one'")
        with db.cursor() as cur:
            with self.assertRaisesRegex(store.Refused, 'has no profile'):
                store.cap(cur, i, 3, provisional=10.0)

    def test_an_exit_reads_back_as_numbers(self):
        i = begin()
        register(i)
        store.run('exit', {'name': 'ob-one', 'nonce': 'n9', 'signature': 's9'})
        e = store.run('exits', {'name': 'ob-one'})['exit']
        self.assertIsInstance(e['created_at'], float)
        self.assertIsNone(e['done_at'])
        db.owner_request_done(db.owner_exit_pending('ob-one'), 'x' * 600)
        e = store.run('exits', {'name': 'ob-one'})['exit']
        self.assertIsInstance(e['done_at'], float)
        self.assertEqual(len(e['outcome']), 500, 'an outcome is kept short')
        json.dumps(e)

    def test_the_feed_file_newest_first_whole_lines_only(self):
        import tempfile
        d = pathlib.Path(tempfile.mkdtemp())
        original, tail = store.ROOT, store.FEED_TAIL_BYTES
        try:
            store.ROOT = d
            (d / 'run' / 'ob-two').mkdir(parents=True)
            f = d / 'run' / 'ob-two' / 'events.jsonl'
            book = lambda eq: json.dumps({'event': 'in_band', 'fees_total_usd': 1, 'equity_usd': eq})
            # two books before the newest DAILY: the newest book wins
            f.write_text('\n'.join([json.dumps({'event': 'DAILY', 'day': 'd'}), book(1), book(2), json.dumps({'event': 'TAPE_SOURCE'})]) + '\n')
            out = store.feed_from_file('ob-two')
            self.assertEqual(set(out), {'book', 'DAILY'})
            self.assertEqual(out['book']['row']['equity_usd'], 2)
            # the only line: read from its first byte
            f.write_text(book(7) + '\n')
            self.assertEqual(store.feed_from_file('ob-two')['book']['row']['equity_usd'], 7)
            # a tail that starts inside a line drops that line, keeps the whole ones after it
            store.FEED_TAIL_BYTES = len(book(8)) + 3
            f.write_text(book(8) + '\n' + book(9) + '\n')
            self.assertEqual(store.feed_from_file('ob-two')['book']['row']['equity_usd'], 9)
        finally:
            store.ROOT, store.FEED_TAIL_BYTES = original, tail

    def test_the_database_first_the_file_when_it_holds_no_book(self):
        import tempfile
        i = begin()
        register(i)
        d = pathlib.Path(tempfile.mkdtemp())
        (d / 'run' / 'ob-one').mkdir(parents=True)
        (d / 'run' / 'ob-one' / 'events.jsonl').write_text(json.dumps({'event': 'in_band', 'fees_total_usd': 1, 'equity_usd': 3}) + '\n')
        original = store.ROOT
        try:
            store.ROOT = d
            db.feed_latest_put('ob-one', 'DAILY', json.dumps({'event': 'DAILY', 'day': 'db'}))
            f = store.run('feed', {'name': 'ob-one'})['feed']
            self.assertEqual((f['book']['row']['equity_usd'], f['DAILY']['row']['day']), (3, 'db'), 'no book in the database: the file\'s')
            db.feed_latest_put('ob-one', 'book', json.dumps({'event': 'in_band', 'fees_total_usd': 1, 'equity_usd': 4}))
            self.assertEqual(store.run('feed', {'name': 'ob-one'})['feed']['book']['row']['equity_usd'], 4, 'the database\'s first')
        finally:
            store.ROOT = original

    def test_the_kind_needs_both_figures(self):
        self.assertIsNone(store.feed_kind({'equity_usd': 1}))
        self.assertIsNone(store.feed_kind({'fees_total_usd': 1}))
        self.assertEqual(store.feed_kind({'fees_total_usd': 1, 'equity_usd': 1}), 'book')
