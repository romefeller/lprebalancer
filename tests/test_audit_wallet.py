"""audit.run on a wallet's book (several profiles on one wallet): the equity
sum over every open position and the idle sleeves, the flows booked to the
profile their token routes to, the harvests read on each profile's own
mints, one position per profile, and an unknown quote price."""
import types
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import audit
import db
from test_audit import tx, tb, OWNER, PROFIT, SOL, USDC
from test_audit_runner import Base

MU = 'MUmintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
XM = 'XmintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
P_SOL = {'name': 'sol-usdc', 'mints': [SOL, USDC], 'deposit_mint': SOL, 'residual_owner': True, 'enabled': True}
P_MU = {'name': 'mu-usdc', 'mints': [MU, USDC], 'deposit_mint': MU, 'residual_owner': False, 'enabled': True}
P_X = {'name': 'x-usdc', 'mints': [XM, USDC], 'deposit_mint': XM, 'residual_owner': False, 'enabled': True}


def book(profiles, claims=None):
    return {'profiles': [dict(p) for p in profiles], 'mints': {m for p in profiles for m in p['mints']},
            'mints_of': {p['name']: list(p['mints']) for p in profiles}, 'claims': claims or {}}


class WalletBase(Base):
    def setUp(self):
        super().setUp()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate snapshots')
        self.context = dict(db.CONTEXT)
        self.addCleanup(lambda: (db.CONTEXT.clear(), db.CONTEXT.update(self.context)))
        db.set_context('sol-usdc', 'w-test')
        self.quote = USDC
        self.scales = {}

    def position(self, mint, profile, equity, pos_usd=None, accrued=None):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, config_name, pool, dex) values (%s, %s, 'POOL', 'orca')",
                        (mint, profile))
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd, position_usd, accrued_usd) "
                        "values (now(), %s, 120, true, '1', %s, %s, %s)", (mint, equity, pos_usd, accrued))

    def bot(self):
        b = super().bot()
        b.pool_tokens = lambda: ((SOL, 'SOL'), (self.quote, 'Q'))
        return b

    def run_wallet(self, wallet, harvested=None):
        cfg = types.SimpleNamespace(RPC='http://rpc.test', POOL='POOL', GAS_RESERVE_SOL=0.05, PROFIT_WALLET=PROFIT,
                                    WALLET_ID='w-test')
        self.harvest_calls = []

        def harvested_fn(url, sigs, pool, a, b):
            self.harvest_calls.append((a, b))
            return (harvested or {}).get((sigs[0], a, b))
        fx = types.SimpleNamespace(fetch=lambda url, sig, tries=3: self.txs.get(sig), harvested=harvested_fn)
        with mock.patch.object(audit, 'rpc', self.fake_rpc), mock.patch.object(audit.time, 'sleep', lambda s: None), \
                mock.patch.object(audit, 'mint_scale', lambda url, m, now=None: self.scales.get(m, 1.0)):
            return audit.run(self.bot(), db, cfg, fx, lambda ev, **kw: self.notes.append((ev, kw)), wallet=wallet)


class Equity(WalletBase):
    def test_own_position_open_and_another_profile_idle(self):
        # sol-usdc holds NFT1; mu-usdc holds no position: its MU and its USDC claim are idle money
        self.position('NFT1', 'sol-usdc', 247.704)
        self.acct(USDC, 500_000, 6); self.acct(MU, 3_000_000, 6)
        self.prices = {MU: 10.0}
        res = self.run_wallet(book([P_SOL, P_MU], {'mu-usdc': {USDC: 0.2}}))
        d = self.detail('equity')
        self.assertEqual(res['equity'], 'ok', d)
        self.assertAlmostEqual(d['snapshot_equity_usd'], 247.704 + 30.2)                    # MU $30 + USDC $0.2
        self.assertAlmostEqual(d['chain_total_usd'], 0.0592 * 120 + 0.5 + 30.0 + 240.0 + 0.1)
        self.assertEqual(d['open_positions'], 1)

    def test_own_profile_idle_and_others_open(self):
        # sol-usdc holds nothing: its SOL and its USDC are idle; the others' positions count by their snapshots
        self.status = None
        self.position('MU1', 'mu-usdc', 31.0, None, 0.5)
        self.position('X1', 'x-usdc', 4.0, 3.0, None)
        self.acct(USDC, 500_000, 6)
        self.prices = {MU: 10.0}
        self.run_wallet(book([P_SOL, P_MU, P_X], {'mu-usdc': {USDC: 0.2}}))
        d = self.detail('equity')
        self.assertAlmostEqual(d['snapshot_equity_usd'], 35.0 + 0.0592 * 120 + 0.3)
        self.assertAlmostEqual(d['chain_total_usd'], 0.0592 * 120 + 0.5 + 0.5 + 3.0)
        self.assertEqual(d['open_positions'], 2)

    def test_every_profile_holding_books_the_snapshots_alone(self):
        self.position('NFT1', 'sol-usdc', 247.704)
        self.position('MU1', 'mu-usdc', 31.0, 30.0, 0.5)
        self.acct(USDC, 500_000, 6)
        self.run_wallet(book([P_SOL, P_MU]))
        self.assertAlmostEqual(self.detail('equity')['snapshot_equity_usd'], 247.704 + 31.0)

    def test_no_snapshot_books_the_idle_money_alone(self):
        self.status = None
        self.acct(USDC, 500_000, 6)
        self.run_wallet(book([P_SOL, P_MU]))
        self.assertAlmostEqual(self.detail('equity')['snapshot_equity_usd'], 0.0592 * 120 + 0.5)

    def test_no_snapshot_and_nothing_idle_is_no_book(self):
        self.status = None
        self.native = 0
        res = self.run_wallet(book([P_SOL, P_MU]))
        self.assertEqual(res['equity'], 'warn')
        self.assertEqual(self.detail('equity'), {'note': 'no snapshot with equity', 'open_positions': 0})

    def test_one_book_has_no_position_count(self):
        self.position('NFT1', 'sol-usdc', 247.704)
        self.run_audit()
        self.assertNotIn('open_positions', self.detail('equity'))


class UnknownQuote(WalletBase):
    def test_a_non_stable_quote_without_a_price_values_nothing(self):
        self.quote = MU
        self.bal['quoteUsd'] = None
        self.sigs = [{'signature': 'NEW', 'blockTime': 5}]
        res = self.run_audit()
        self.assertEqual((res['idle'], res['equity'], res['flows']), ('warn', 'warn', 'warn'))
        self.assertEqual(self.detail('idle'), {'note': 'quote price unknown: idle money not valued'})
        self.assertEqual(self.detail('equity'), {'note': 'quote price unknown: the wallet cannot be valued'})
        self.assertEqual(self.detail('flows'), {'note': 'quote price unknown: flows wait for a price'})
        self.assertIsNone(db.audit_value('flows_cursor'))                                  # the cursor stays

    def test_a_stable_quote_without_a_price_is_a_dollar(self):
        self.bal['quoteUsd'] = None
        res = self.run_audit()
        self.assertNotIn('note', self.detail('idle'))
        self.assertNotIn('quote price', self.detail('equity').get('note', ''))
        self.assertEqual(res['flows'], 'ok')


class Flows(WalletBase):
    def deposit_usdc(self, sig='D', amount=5_000_000):
        self.sigs = [{'signature': sig, 'blockTime': 5}]
        self.txs = {sig: tx(sig, keys=('SENDER', OWNER), pre_tok=[tb(1, OWNER, USDC, 0)],
                            post_tok=[tb(1, OWNER, USDC, amount)])}

    def flow(self, sig='D'):
        with db.cursor() as cur:
            cur.execute('select sol, usdc, usd, price, amounts, profile from capital_flows where signature = %s', (sig,))
            return cur.fetchone()

    def test_a_sol_deposit_is_the_sol_profiles_in_sol(self):
        self.sigs = [{'signature': 'D', 'blockTime': 5}]
        self.txs = {'D': tx('D', keys=('SENDER', OWNER), pre=[5 * 10**9, 10**9], post=[4 * 10**9 - 5000, 2 * 10**9])}
        self.run_wallet(book([P_SOL, P_MU]))
        r = self.flow()
        self.assertEqual((float(r['sol']), float(r['price']), r['profile']), (1.0, 120.0, 'sol-usdc'))
        self.assertEqual(r['amounts'], {SOL: 1.0, USDC: 0.0})

    def test_without_a_residual_owner_usdc_is_this_process_profiles(self):
        self.deposit_usdc()
        self.run_wallet(book([dict(P_SOL, residual_owner=False), P_MU]))
        r = self.flow()
        self.assertEqual((float(r['sol']), float(r['usdc']), float(r['price']), r['profile']), (0.0, 5.0, 120.0, 'sol-usdc'))
        self.assertEqual(r['amounts'], {SOL: 0.0, USDC: 5.0})

    def test_a_token_profile_gets_none_of_its_token_from_a_usdc_flow(self):
        db.set_context('mu-usdc', 'w-test')
        self.deposit_usdc()
        self.run_wallet(book([dict(P_SOL, residual_owner=False), P_MU]))
        r = self.flow()
        self.assertEqual((r['profile'], r['price']), ('mu-usdc', None))
        self.assertEqual(r['amounts'], {MU: 0.0, USDC: 5.0})

    def test_a_process_profile_outside_the_book_books_usdc_only(self):
        db.set_context('ghost', 'w-test')
        self.deposit_usdc()
        res = self.run_wallet(book([dict(P_SOL, residual_owner=False), P_MU]))
        self.assertNotEqual(res['flows'], 'fail', self.detail('flows'))
        r = self.flow()
        self.assertEqual((r['profile'], r['amounts']), ('ghost', {USDC: 5.0}))

    def test_a_token_without_a_price_adds_no_dollars(self):
        self.sigs = [{'signature': 'D', 'blockTime': 5}]
        self.txs = {'D': tx('D', keys=('SENDER', OWNER), pre_tok=[tb(1, OWNER, MU, 0), tb(2, OWNER, USDC, 0)],
                            post_tok=[tb(1, OWNER, MU, 2_000_000), tb(2, OWNER, USDC, 1_000_000)])}
        b = self.bot()
        b.dexes = types.SimpleNamespace(jupiter_prices=lambda mints: {}, jupiter_token=lambda m: None)
        with mock.patch.object(self, 'bot', lambda: b):
            self.run_wallet(book([P_SOL, P_MU]))
        r = self.flow()
        self.assertEqual((float(r['usd']), r['profile']), (1.0, 'mu-usdc'))


class Harvests(WalletBase):
    def harvest(self, mint, profile, fee_a, fee_b, sig):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, config_name, pool, dex, closed_at) values (%s, %s, 'POOL', 'orca', now())",
                        (mint, profile))
            cur.execute('insert into harvests (ts, mint, fee_a, fee_b, fee_usd, signature) values (now(), %s, %s, %s, 0, %s)',
                        (mint, fee_a, fee_b, sig))

    def test_each_profiles_harvest_is_read_on_its_own_mints_in_ui_units(self):
        self.harvest('MU1', 'mu-usdc', 2.0, 3.0, 'HV')
        self.scales = {MU: 2.0, USDC: 0.5}            # both sides scaled (a test value for the quote side)
        res = self.run_wallet(book([P_SOL, P_MU]), harvested={('HV', MU, USDC): (1.0, 6.0)})
        self.assertEqual(self.harvest_calls, [(MU, USDC)])
        self.assertEqual(res['harvests'], 'ok', self.detail('harvests'))

    def test_an_unreadable_harvest_is_a_warning(self):
        self.harvest('MU1', 'mu-usdc', 2.0, 3.0, 'HV')
        res = self.run_wallet(book([P_SOL, P_MU]))
        self.assertEqual(res['harvests'], 'warn')


class Positions(WalletBase):
    def test_one_position_per_profile_on_a_shared_wallet_is_fine(self):
        self.position('NFT1', 'sol-usdc', 247.704)
        self.position('MU1', 'mu-usdc', 31.0, 30.0, 0.5)
        self.acct('NFT1', 1, 0); self.acct('MU1', 1, 0)
        res = self.run_wallet(book([P_SOL, P_MU]))
        self.assertEqual(res['positions'], 'ok', self.detail('positions'))


if __name__ == '__main__':
    unittest.main()
