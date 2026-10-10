"""Vault instances on the loop side: what the database holds (sql/036), what
a signer is given, where swaps and payouts go, and the fee share."""
import os
import unittest
from unittest import mock

import psycopg2

import _fixtures
_fixtures.ensure_profile()

import config  # noqa: E402
import db  # noqa: E402
import fees  # noqa: E402
import lp.capital  # noqa: E402
import lp.harvest  # noqa: E402
import lp.signers  # noqa: E402

SOL = fees.NATIVE_MINT
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
FEE = 'FogqBWLC4y94csrniURTbGrx7ff7jFa4e2qp1GsgyAmM'
VAULT_ADDR = '7FLVkJmRQ6CjEyqNaNMbCXLEcEoU9LyUhFSo2gJ8xLKt'
SETTINGS = '9Z8KJb4qTn6SDd4xvRjXkJ4o3V5y8sLtqiHqyZ2y3Bz1'
POLICIES = ['3Fv8mHjCJqgXb5tNbAzVQ1QvVq6x4V5n9kGxS7mKpJ2d', 'BrX2kCH8pXVqWcFzJ7yTqv8x3Q5w2Z8h4vYJ1N6mK9pT']
DLMM_POOL = 'BGm1tav58oGcsQJehL9WXBFXF7D27vZsKefj4xJKD5Y'
OWNERS = ['HCuvA4H9Vq8ezGvTZkgD2G8qJ6FTX2EWvWCbvN3BQQ6p']


def vault(venues=None, **over):
    v = {'wallet_id': 'v-test', 'kind': 'squads', 'settings': SETTINGS, 'owners': OWNERS, 'threshold': 1,
         'policy_key_env': 'LPBOT_VAULT_TEST_POLICY_KEY', 'lookup': None,
         'venues': venues if venues is not None else
         {'meteora-dlmm': {'policies': POLICIES, 'pools': [DLMM_POOL], 'payouts': []}}}
    v.update(over)
    return v


class Database(unittest.TestCase):
    """The tables hold what a vault needs, and refuse what makes no sense."""

    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute('delete from vault_policies where wallet_id = %s', ('v-test',))
            cur.execute('delete from vaults where wallet_id = %s', ('v-test',))
            cur.execute("update config set wallet_id = null where wallet_id = 'v-test'")
            cur.execute('delete from wallets where id = %s', ('v-test',))
            cur.execute('insert into wallets (id, chain, address, secret_env) values (%s, %s, %s, %s)',
                        ('v-test', 'solana', VAULT_ADDR, 'LPBOT_VAULT_TEST_PAYER_KEY'))

    def insert_vault(self, owners=OWNERS, threshold=1):
        with db.cursor(commit=True) as cur:
            cur.execute('insert into vaults (wallet_id, kind, settings, owners, threshold, policy_key_env) '
                        'values (%s, %s, %s, %s, %s, %s)', ('v-test', 'squads', SETTINGS, owners, threshold,
                                                             'LPBOT_VAULT_TEST_POLICY_KEY'))

    def test_a_key_wallet_has_no_vault(self):
        self.assertIsNone(db.vault_row('v-test'))

    def test_a_vault_reads_back_with_its_venues(self):
        self.insert_vault()
        with db.cursor(commit=True) as cur:
            cur.execute('insert into vault_policies (wallet_id, venue, policies, pools, payouts) values (%s, %s, %s, %s, %s)',
                        ('v-test', 'meteora-dlmm', POLICIES, [DLMM_POOL], []))
        v = db.vault_row('v-test')
        self.assertEqual(v['settings'], SETTINGS)
        self.assertEqual(v['venues'], {'meteora-dlmm': {'policies': POLICIES, 'pools': [DLMM_POOL], 'payouts': []}})

    def test_an_unreachable_threshold_is_refused(self):
        with self.assertRaises(psycopg2.errors.CheckViolation):
            self.insert_vault(threshold=2)

    def test_a_policy_row_without_pools_is_refused(self):
        self.insert_vault()
        with self.assertRaises(psycopg2.errors.CheckViolation), db.cursor(commit=True) as cur:
            cur.execute('insert into vault_policies (wallet_id, venue, policies, pools) values (%s, %s, %s, %s)',
                        ('v-test', 'meteora-dlmm', POLICIES, []))

    def test_fee_rules(self):
        """A share needs a fee wallet, stays under half, and a special instance pays none."""
        for over in ({'fee_bps': 1000}, {'fee_bps': 1000, 'fee_wallet': FEE, 'special': True},
                     {'fee_bps': 6000, 'fee_wallet': FEE, 'special': False}):
            with self.subTest(over=over), self.assertRaises(psycopg2.errors.CheckViolation):
                _fixtures.ensure_profile('vault-fee-test', **over)
        _fixtures.ensure_profile('vault-fee-test', fee_bps=1000, fee_wallet=FEE, special=False)
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name = 'vault-fee-test'")
        _fixtures.ensure_profile()


class SignerEnvironment(unittest.TestCase):
    """What a Solana signer gets to act for a vault."""

    def test_no_vault_no_environment(self):
        with mock.patch.object(config, 'VAULT', None):
            self.assertEqual(config.vault_env('meteora-dlmm'), {})

    def test_the_venues_policies_pools_and_key(self):
        with mock.patch.object(config, 'VAULT', vault()), \
                mock.patch.dict(os.environ, {'LPBOT_VAULT_TEST_POLICY_KEY': '/keys/policy.json'}):
            env = config.vault_env('meteora-dlmm')
        self.assertEqual(env['LPBOT_VAULT_SETTINGS'], SETTINGS)
        self.assertEqual(env['LPBOT_VAULT_POLICIES'], ','.join(POLICIES))
        self.assertEqual(env['LPBOT_VAULT_POOLS'], DLMM_POOL)
        self.assertEqual(env['LPBOT_VAULT_POLICY_KEY'], '/keys/policy.json')
        self.assertEqual(env['LPBOT_VAULT_VENUE'], 'meteora-dlmm')
        self.assertEqual(env['LPBOT_VAULT_LOOKUP'], '')
        with mock.patch.object(config, 'VAULT', vault(lookup=DLMM_POOL)), \
                mock.patch.dict(os.environ, {'LPBOT_VAULT_TEST_POLICY_KEY': '/keys/policy.json'}):
            self.assertEqual(config.vault_env('meteora-dlmm')['LPBOT_VAULT_LOOKUP'], DLMM_POOL)

    def test_an_unapproved_venue_or_a_missing_key_is_refused(self):
        with mock.patch.object(config, 'VAULT', vault()), \
                mock.patch.dict(os.environ, {'LPBOT_VAULT_TEST_POLICY_KEY': '/keys/policy.json'}):
            with self.assertRaisesRegex(ValueError, 'no approved policy for orca'):
                config.vault_env('orca')
        with mock.patch.object(config, 'VAULT', vault()), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LPBOT_VAULT_TEST_POLICY_KEY', None)
            with self.assertRaisesRegex(ValueError, 'policy key'):
                config.vault_env('meteora-dlmm')

    def run_chain(self, *args, dex=None, fee_bps=0):
        seen = {}

        def fake_run(cmd, **kw):
            seen['cmd'], seen['env'] = cmd, kw['env']
            return mock.Mock(stdout='{"ok": true}', stderr='', returncode=0)
        with mock.patch.object(config, 'VAULT', vault()), mock.patch.object(config, 'DEX', 'meteora-dlmm'), \
                mock.patch.object(config, 'FEE_BPS', fee_bps), mock.patch.object(config, 'FEE_WALLET', FEE), \
                mock.patch.dict(os.environ, {'LPBOT_VAULT_TEST_POLICY_KEY': '/keys/policy.json'}), \
                mock.patch.object(lp.signers.subprocess, 'run', fake_run):
            out = lp.signers._chain(*args, dex=dex)
        return out, seen

    def test_every_signer_call_carries_the_vault(self):
        (out, err), seen = self.run_chain('status')
        self.assertIsNone(err)
        self.assertEqual(seen['env']['LPBOT_VAULT_SETTINGS'], SETTINGS)
        self.assertNotIn('LPBOT_FEE_WALLET', seen['env'])

    def test_a_payout_uses_the_profiles_venue_and_names_the_fee_wallet(self):
        (out, err), seen = self.run_chain('send', USDC, '1', PROFIT, dex='payout', fee_bps=1000)
        self.assertIsNone(err)
        self.assertEqual(seen['env']['LPBOT_VAULT_VENUE'], 'meteora-dlmm')
        self.assertEqual(seen['env']['LPBOT_FEE_WALLET'], FEE)

    def test_aggregator_swaps_are_refused_and_swaps_route_to_the_venue(self):
        for dex in ('jupiter', 'orca-swap'):
            (out, err), seen = self.run_chain('rebalance', SOL, USDC, '1', '1', dex=dex)
            self.assertRegex(err, 'swaps only on a pool its owners approved')
            self.assertEqual(seen, {})
        with mock.patch.object(config, 'VAULT', vault()), mock.patch.object(config, 'DEX', 'meteora-dlmm'):
            self.assertEqual(lp.signers.route('swap'), 'meteora-dlmm')
            self.assertFalse(lp.signers.housekeeper('sweep'))
            self.assertFalse(lp.signers.housekeeper('janitor'))

    def test_a_key_wallet_routes_and_keeps_house_as_its_chain_says(self):
        caps = dict(config.CAPS, swap_via='jupiter', payout_via='venue', sweep=True, janitor=True, audit=False)
        with mock.patch.object(config, 'VAULT', None), mock.patch.object(config, 'DEX', 'orca'), \
                mock.patch.object(config, 'CAPS', caps), mock.patch.object(config, 'RESIDUAL_OWNER', True):
            self.assertEqual(lp.signers.route('swap'), 'jupiter')
            self.assertEqual(lp.signers.route('payout'), 'orca')
            self.assertIs(lp.signers.housekeeper('sweep'), True)
            self.assertIs(lp.signers.housekeeper('janitor'), True)
            self.assertIs(lp.signers.housekeeper('audit'), False)
            with mock.patch.object(config, 'RESIDUAL_OWNER', False):
                self.assertIs(lp.signers.housekeeper('sweep'), False)
        with mock.patch.object(config, 'VAULT', vault()), mock.patch.object(config, 'DEX', 'meteora-dlmm'), \
                mock.patch.object(config, 'CAPS', caps), mock.patch.object(config, 'RESIDUAL_OWNER', True):
            self.assertEqual(lp.signers.route('swap'), 'meteora-dlmm')
            self.assertIs(lp.signers.housekeeper('audit'), False)
            with mock.patch.object(config, 'CAPS', dict(caps, payout_via='payout')):
                self.assertEqual(lp.signers.route('payout'), 'payout')
            with mock.patch.object(config, 'CAPS', dict(caps, audit=True)):
                self.assertIs(lp.signers.housekeeper('audit'), True)

    def test_a_venue_without_a_policy_is_refused_before_spawning(self):
        (out, err), seen = self.run_chain('status', dex='orca')
        self.assertRegex(err, 'no approved policy for orca')
        self.assertEqual(seen, {})


class Gas(unittest.TestCase):
    """A vault's gas is its payer key's; the vault keeps only its floor."""

    def test_the_payer_key_must_cover_reserve_and_rent(self):
        state = {}
        with mock.patch.object(config, 'VAULT', vault()), mock.patch.object(config, 'DEX', 'meteora-dlmm'), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', 0.01), \
                mock.patch.object(lp.paths, 'save', lambda s: None), mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(db, 'event', lambda *a, **k: None):
            need = 0.01 + lp.capital.open_headroom('meteora-dlmm')
            # plenty of SOL in the vault does not pay the payer's fees
            self.assertFalse(lp.capital.gas_for_open(state, {'sol': 5.0, 'gasSol': need - 0.001, 'nativeSide': 'A'}))
            self.assertTrue(lp.capital.gas_for_open(state, {'sol': 0.0, 'gasSol': need, 'nativeSide': 'A'}))

    def gas(self, bal, state=None, vault_on=False):
        told = []
        state = {} if state is None else state
        with mock.patch.object(config, 'VAULT', vault() if vault_on else None), \
                mock.patch.object(config, 'DEX', 'meteora-dlmm'), mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: told.append(k['reason'])), \
                mock.patch.object(db, 'event', lambda *a, **k: None):
            return lp.capital.gas_for_open(state, bal), told

    def test_a_key_wallet_pays_gas_from_its_native_balance(self):
        self.assertEqual(self.gas({'sol': 0.05, 'nativeSide': 'A'}), (True, []))
        self.assertFalse(self.gas({'sol': 0.049, 'nativeSide': 'A'})[0])
        # no plain 'sol' read: the pool's native side is the gas
        self.assertTrue(self.gas({'balanceA': 0.05, 'balanceB': 0.0, 'nativeSide': 'A'})[0])
        self.assertFalse(self.gas({'balanceA': 0.0, 'balanceB': 0.05, 'nativeSide': 'A'})[0])
        self.assertTrue(self.gas({'balanceA': 0.0, 'balanceB': 0.05, 'nativeSide': 'B'})[0])
        self.assertFalse(self.gas({'nativeSide': 'A'})[0])
        # a pool without the native token keeps the wallet's own reserve
        with mock.patch.object(lp.capital, 'native_reserve', lambda bal: 0.2):
            self.assertFalse(self.gas({'sol': 0.1, 'balanceA': 1.0, 'nativeSide': None})[0])
            self.assertTrue(self.gas({'sol': 0.2, 'nativeSide': None})[0])

    def test_a_shortage_is_told_once_and_forgotten_when_topped_up(self):
        state = {}
        ok, told = self.gas({'gasSol': 0.0}, state, vault_on=True)
        self.assertFalse(ok)
        self.assertEqual(len(told), 1)
        self.assertIn("fee payer key", told[0])
        self.assertIn('gas_short_told', state)
        self.assertEqual(self.gas({}, state, vault_on=True), (False, []))
        self.assertTrue(self.gas({'gasSol': 1.0}, state, vault_on=True)[0])
        self.assertNotIn('gas_short_told', state)
        ok, told = self.gas({'sol': 0.0, 'nativeSide': 'A'}, {})
        self.assertIn('in the wallet', told[0])

    def test_a_key_wallets_pin_must_be_the_profit_wallet(self):
        env = config.CAPS['pin_env']
        cases = ((PROFIT, PROFIT, True), ('', PROFIT, False), (FEE, PROFIT, False), ('nonsense', 'nonsense', False))
        for pin, profit, want in cases:
            with mock.patch.object(config, 'VAULT', None), mock.patch.object(config, 'PROFIT_WALLET', profit), \
                    mock.patch.dict(os.environ, {env: pin}):
                self.assertIs(lp.capital.profit_wallet_pinned(), want, (pin, profit))

    def test_the_payout_pin_is_the_policy(self):
        with mock.patch.object(config, 'VAULT', vault()), mock.patch.object(config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(config.CAPS['pin_env'], None)
            self.assertTrue(lp.capital.profit_wallet_pinned())


class FeeShare(unittest.TestCase):
    """fees.fee_share: the share of every paid part, nothing else touched."""

    PARTS = [{'mint': USDC, 'symbol': 'USDC', 'amount': 3.0, 'usd': 3.0, 'kind': 'paid'},
             {'mint': SOL, 'symbol': 'SOL', 'amount': 0.02, 'usd': 2.4, 'kind': 'reinvested'},
             {'mint': SOL, 'symbol': 'SOL', 'amount': 0.01, 'usd': 1.2, 'kind': 'gas'}]

    def test_ten_percent_of_the_paid_part(self):
        out = fees.fee_share(self.PARTS, 1000)
        by = {(p['symbol'], p['kind']): p for p in out}
        self.assertAlmostEqual(by[('USDC', 'paid')]['amount'], 2.7)
        self.assertAlmostEqual(by[('USDC', 'fee')]['amount'], 0.3)
        self.assertAlmostEqual(by[('USDC', 'fee')]['usd'], 0.3)
        self.assertEqual(by[('SOL', 'reinvested')], self.PARTS[1])
        self.assertEqual(by[('SOL', 'gas')], self.PARTS[2])

    def test_amounts_and_values_are_kept_for_any_share(self):
        for bps in (0, 1, 250, 1000, 3333, 5000):
            out = fees.fee_share(self.PARTS, bps)
            for kind_set, want in ((('paid', 'fee'), 3.0), (('reinvested',), 0.02), (('gas',), 0.01)):
                got = sum(p['amount'] for p in out if p['kind'] in kind_set)
                self.assertAlmostEqual(got, want, msg=f'{bps} {kind_set}')
            self.assertAlmostEqual(sum(p['usd'] for p in out), 6.6, msg=bps)

    def test_no_share_is_the_split_unchanged(self):
        self.assertEqual(fees.fee_share(self.PARTS, 0), self.PARTS)

    def test_a_share_outside_the_bounds_is_refused(self):
        for bps in (-1, 5001):
            with self.assertRaises(ValueError):
                fees.fee_share(self.PARTS, bps)

    def test_an_unpriced_part_stays_unpriced(self):
        out = fees.fee_share([dict(self.PARTS[0], usd=None)], 1000)
        self.assertTrue(all(p['usd'] is None for p in out))

    def test_a_value_follows_the_parts_price(self):
        out = fees.fee_share([dict(self.PARTS[0], amount=4.0, usd=2.0)], 2500)
        self.assertEqual([(p['kind'], p['amount'], p['usd']) for p in out], [('paid', 3.0, 1.5), ('fee', 1.0, 0.5)])

    def test_an_empty_paid_part_makes_no_parts(self):
        self.assertEqual(fees.fee_share([dict(self.PARTS[0], amount=0.0, usd=0.0)], 1000), [])


class Distribute(unittest.TestCase):
    """A harvest of a non-special vault instance: profit wallet and fee wallet."""

    def run_it(self, results, fee_bps=1000, owed=None, usdc_held=40.0):
        rows, calls, state = [], [], {'payout_owed': dict(owed or {})}
        bal = {'balanceA': 1.0, 'balanceB': usdc_held, 'sol': 1.0, 'gasSol': 0.01, 'price': 120.0, 'quoteUsd': 1.0}
        it = iter(results)
        with mock.patch.object(config, 'VAULT', vault()), mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(config, 'PAYOUT_MINT', USDC), mock.patch.object(config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(config, 'FEE_BPS', fee_bps), mock.patch.object(config, 'FEE_WALLET', FEE), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(lp.capital, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(lp.capital, 'wallet', lambda p: bal), \
                mock.patch.object(lp.signers, 'chain', lambda *a, **k: (calls.append(a) or next(it))), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: None), \
                mock.patch.object(db, 'record_payout', lambda *a, **k: rows.append((a[6], round(a[4], 9), k.get('to_address')))):
            lp.harvest.distribute(state, 'M', 0.002, 3.0)
        return rows, calls, state

    def test_ninety_percent_to_profit_ten_to_the_fee_wallet(self):
        rows, calls, _ = self.run_it([({'signature': 's1'}, None), ({'signature': 's2'}, None)])
        self.assertEqual([(c[1], c[2], c[3]) for c in calls], [(USDC, '2.700000000', PROFIT), (USDC, '0.300000000', FEE)])
        self.assertIn(('paid', 2.7, PROFIT), rows)
        self.assertIn(('fee', 0.3, FEE), rows)

    def test_a_vaults_low_balance_never_holds_its_payout_for_gas(self):
        """The vault's SOL is no gas: fees are paid even with little SOL in it."""
        rows, calls, _ = self.run_it([({'signature': 's1'}, None), ({'signature': 's2'}, None)])
        self.assertNotIn('gas', [r[0] for r in rows])

    def test_a_failed_fee_transfer_is_owed_on_its_own_account(self):
        rows, _, state = self.run_it([({'signature': 's1'}, None), (None, 'rpc down')])
        self.assertAlmostEqual(state['payout_owed'][f'fee:{USDC}'], 0.3)
        self.assertNotIn(USDC, state['payout_owed'])
        # next harvest: the fee share owed is paid with the new one
        rows, calls, state = self.run_it([({'signature': 's3'}, None), ({'signature': 's4'}, None)], owed=state['payout_owed'])
        self.assertEqual(calls[1][2], '0.600000000')
        self.assertNotIn(f'fee:{USDC}', state['payout_owed'])

    def test_profit_and_fee_together_never_exceed_what_the_vault_holds(self):
        rows, calls, state = self.run_it([({'signature': 's1'}, None), ({'signature': 's2'}, None)],
                                         owed={USDC: 1.0, f'fee:{USDC}': 0.5}, usdc_held=3.5)
        self.assertLessEqual(sum(float(c[2]) for c in calls), 3.5 + 1e-9)
        # the fee share the vault could not cover stays owed with the old one
        self.assertAlmostEqual(state['payout_owed'][f'fee:{USDC}'], 0.8)
        self.assertAlmostEqual(state['payout_owed'][USDC], 0.2)

    def test_a_special_instance_pays_no_share(self):
        rows, calls, _ = self.run_it([({'signature': 's1'}, None)], fee_bps=0)
        self.assertEqual([(c[2], c[3]) for c in calls], [('3.000000000', PROFIT)])


if __name__ == '__main__':
    unittest.main()
