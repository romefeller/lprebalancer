"""ops/add_vault.py: what it refuses, what it writes, and a payout change."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest

import _fixtures  # noqa: F401  (the test database guard)

import db  # noqa: E402

spec = importlib.util.spec_from_file_location('add_vault', pathlib.Path(__file__).parent.parent / 'ops' / 'add_vault.py')
add_vault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(add_vault)

VAULT = 'CeaZcxBNLpJWtxzt58qQmfMBtJY8pQLvursXTJYGQpbN'          # any address no other test uses
SETTINGS = '9Z8KJb4qTn6SDd4xvRjXkJ4o3V5y8sLtqiHqyZ2y3Bz1'
OWNER = 'HCuvA4H9Vq8ezGvTZkgD2G8qJ6FTX2EWvWCbvN3BQQ6p'
PAYER = 'FogqBWLC4y94csrniURTbGrx7ff7jFa4e2qp1GsgyAmM'
POLICY_KEY = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'
POOL = 'BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y'
POLICIES = ['3Fv8mHjCJqgXb5tNbAzVQ1QvVq6x4V5n9kGxS7mKpJ2d', 'BrX2kCH8pXVqWcFzJ7yTqv8x3Q5w2Z8h4vYJ1N6mK9pT']
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
# PROFIT's USDC account (on chain: owner PROFIT, mint USDC)
PROFIT_USDC = 'CvkYX7aRberphM2wx2Mf2f7BomxtikEqPvsM4USGYG9z'


RAYDIUM_POOL = '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv'
RAYDIUM_POLICIES = ['8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj']


def connected(**over):
    c = {'sent': True, 'vault': VAULT, 'settings': SETTINGS, 'owners': [OWNER], 'threshold': 1,
         'venues': {'meteora-dlmm': {'policies': POLICIES, 'pools': [POOL]}},
         'payouts': [], 'payer': PAYER, 'policyKey': POLICY_KEY, 'lookup': None}
    c.update(over)
    return c


def venue(policies=POLICIES, pools=(POOL,)):
    return {'policies': list(policies), 'pools': list(pools)}


def args(**over):
    a = dict(wallet='v-add', secret_env='LPBOT_V_PAYER', policy_key_env='LPBOT_V_POLICY', label=None, apply=False)
    a.update(over)
    return argparse.Namespace(**a)


def clean():
    with db.cursor(commit=True) as cur:
        cur.execute("delete from vault_policies where wallet_id = 'v-add'")
        cur.execute("delete from vaults where wallet_id = 'v-add'")
        cur.execute("delete from wallets where id = 'v-add'")


class RegisterRows(unittest.TestCase):
    def test_a_connected_vault_makes_three_rows(self):
        w, v, (p,) = add_vault.register_rows(args(), connected())
        self.assertEqual(w, ('v-add', 'solana', VAULT, 'LPBOT_V_PAYER', 'v-add'))
        self.assertEqual(v[:5], ('v-add', 'squads', SETTINGS, [OWNER], 1))
        self.assertEqual(p, ('v-add', 'meteora-dlmm', POLICIES, [POOL], []))
        self.assertIsNone(v[6])

    def test_one_policy_row_per_venue_with_the_same_payouts(self):
        _, _, rows = add_vault.register_rows(args(), connected(payouts=[PROFIT_USDC], venues={
            'meteora-dlmm': venue(), 'raydium-clmm': venue(RAYDIUM_POLICIES, [RAYDIUM_POOL])}))
        self.assertEqual(rows, [('v-add', 'meteora-dlmm', POLICIES, [POOL], [PROFIT_USDC]),
                                ('v-add', 'raydium-clmm', RAYDIUM_POLICIES, [RAYDIUM_POOL], [PROFIT_USDC])])

    def test_label_lookup_and_payouts_are_carried(self):
        w, v, p = add_vault.register_rows(args(label='Phantom test'),
                                          connected(lookup=PAYER, payouts=[PROFIT_USDC], threshold='1'))
        self.assertEqual(w[4], 'Phantom test')
        self.assertEqual(v[4:], (1, 'LPBOT_V_POLICY', PAYER))
        self.assertEqual(p[0][4], [PROFIT_USDC])
        self.assertEqual(add_vault.register_rows(args(), connected(payouts=None))[2][0][4], [])
        self.assertEqual(add_vault.addresses(None, 'x'), [])

    def test_what_it_refuses(self):
        for over, msg in (({'sent': False}, 'not a finished vault'), ({'venues': {'nowhere': venue()}}, 'no vault rules'),
                          ({'venues': {}}, 'no venues'),
                          ({'owners': [OWNER, PAYER]}, 'bot key'), ({'threshold': 2}, 'threshold'),
                          ({'owners': ['not-an-address']}, 'not addresses'),
                          ({'venues': {'meteora-dlmm': venue(pools=())}}, 'meteora-dlmm: no policies or no pools'),
                          ({'venues': {'meteora-dlmm': venue(), 'orca': venue(policies=())}}, 'orca: no policies or no pools'),
                          ({'threshold': 0}, 'threshold'),
                          ({'threshold': None}, 'threshold'), ({'owners': []}, 'threshold'),
                          ({'lookup': 'not-an-address'}, 'lookup')):
            with self.subTest(over=over), self.assertRaisesRegex(add_vault.Refused, msg):
                add_vault.register_rows(args(), connected(**over))
        for a in (args(wallet='Bad Id'), args(secret_env='lower'), args(policy_key_env=''), args(secret_env=None)):
            with self.assertRaises(add_vault.Refused):
                add_vault.register_rows(a, connected())


SAFE = '0x5FbDB2315678afecb367f032d93F642f64180aa3'
ROLES = '0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512'
EVM_OWNER = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
EVM_BOT = '0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266'
WPOL_POOL = '0x9B08288C3Be4F62bbf8d1C20Ac9C5e6f9467d8B7'
EVM_PROFIT = '0x2b35000000000000000000000000000000000142'


class Register(unittest.TestCase):
    def setUp(self):
        clean()
        self.addCleanup(clean)

    def write(self, **over):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(connected(**over), f)
        self.addCleanup(os.unlink, f.name)
        with contextlib.redirect_stdout(io.StringIO()):
            add_vault.register(args(json=f.name, apply=True))

    def test_written_and_read_back_as_the_loop_reads_it(self):
        self.write(venues={'meteora-dlmm': venue(), 'raydium-clmm': venue(RAYDIUM_POLICIES, [RAYDIUM_POOL])})
        self.assertEqual(db.wallet_row('v-add')['address'], VAULT)
        v = db.vault_row('v-add')
        self.assertEqual(v['owners'], [OWNER])
        self.assertEqual(v['venues']['meteora-dlmm']['policies'], POLICIES)
        self.assertEqual(v['venues']['raydium-clmm'], {'policies': RAYDIUM_POLICIES, 'pools': [RAYDIUM_POOL], 'payouts': []})

    def test_a_dry_run_writes_nothing(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(connected(), f)
        self.addCleanup(os.unlink, f.name)
        with contextlib.redirect_stdout(io.StringIO()):
            add_vault.register(args(json=f.name))
        self.assertIsNone(db.vault_row('v-add'))

    def test_new_payout_wallets_are_their_token_accounts(self):
        self.write()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            add_vault.payouts(argparse.Namespace(wallet='v-add', venue='meteora-dlmm', payout=PROFIT, mints=None, apply=True))
        self.assertEqual(db.vault_row('v-add')['venues']['meteora-dlmm']['payouts'], [PROFIT_USDC])
        self.assertIn('owners approve', out.getvalue())

    def test_payout_mints_and_a_dry_run(self):
        self.write()
        usdt = 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'
        with contextlib.redirect_stdout(io.StringIO()):
            add_vault.payouts(argparse.Namespace(wallet='v-add', venue='meteora-dlmm', payout=PROFIT, mints=usdt, apply=False))
        self.assertEqual(db.vault_row('v-add')['venues']['meteora-dlmm']['payouts'], [])
        with contextlib.redirect_stdout(io.StringIO()):
            add_vault.payouts(argparse.Namespace(wallet='v-add', venue='meteora-dlmm', payout=PROFIT, mints=usdt, apply=True))
        got = db.vault_row('v-add')['venues']['meteora-dlmm']['payouts']
        self.assertEqual(len(got), 1)
        self.assertNotEqual(got, [PROFIT_USDC])
        with self.assertRaisesRegex(add_vault.Refused, 'no vault policy'):
            add_vault.payouts(argparse.Namespace(wallet='v-none', venue='meteora-dlmm', payout=PROFIT, mints=None, apply=True))

    def test_payouts_for_a_venue_never_approved_are_refused(self):
        self.write()
        with self.assertRaisesRegex(add_vault.Refused, 'no vault policy for orca'):
            add_vault.payouts(argparse.Namespace(wallet='v-add', venue='orca', payout=PROFIT, mints=None, apply=True))


if __name__ == '__main__':
    unittest.main()
