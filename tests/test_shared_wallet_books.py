"""Two books on one wallet: rent and fees one profile pays from another's SOL.

2026-10-02 13:19Z: the first MU/USDC open on the shared Solana wallet locked
0.0675 SOL of Meteora rent. Three bugs showed it as money lost:

  1. sol-usdc owns the wallet's SOL, so its equity fell $8.28 with no flow
     (wallets.native_giver / internal_flows now book it as internal flows);
  2. the MU signer priced that rent with one Jupiter request, and a failed
     request counted it as $0, so mu-usdc's equity flickered by $8.26 at one
     price (rebalancer.rent_usd);
  3. mu-usdc's deposit and its baseline were the same MU, so its P&L read
     -$208 on $207 of equity (db.since_start counts flows after the baseline).

Covered: the pure helpers as properties, the incident's rows replayed through
since_start, rent pricing, and the loop end to end over a fake chain whose
non-native opens pay rent and whose closes refund it."""
import json
import time
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import db
import rebalancer
import wallets
from test_multi_loop import (Fixture, FakeChain, MU, MU_POOL, POOLS, SOL, SOL_POOL, USDC, WALLET)

NATIVE = SOL
PROFILES = [{'name': 'sol-usdc', 'mints': [SOL, USDC], 'deposit_mint': SOL, 'residual_owner': True},
            {'name': 'mu-usdc', 'mints': [MU, USDC], 'deposit_mint': MU, 'residual_owner': False}]
amounts = st.floats(min_value=-50.0, max_value=50.0, allow_nan=False, allow_infinity=False)
prices = st.one_of(st.none(), st.floats(min_value=0.01, max_value=1e5, allow_nan=False, allow_infinity=False))


# --- pure: who gives, what is booked ----------------------------------------------------------

class Giver(unittest.TestCase):
    def test_a_pool_without_the_native_token_spends_the_owners(self):
        self.assertEqual(wallets.native_giver('mu-usdc', [MU, USDC], PROFILES, NATIVE), 'sol-usdc')

    def test_the_owner_and_a_native_pool_give_to_nobody(self):
        self.assertIsNone(wallets.native_giver('sol-usdc', [SOL, USDC], PROFILES, NATIVE))
        other = PROFILES + [{'name': 'jito-sol', 'mints': ['JITO', SOL], 'deposit_mint': None}]
        self.assertIsNone(wallets.native_giver('jito-sol', ['JITO', SOL], other, NATIVE))

    def test_no_known_owner_gives_nothing(self):
        alone = [{'name': 'mu-usdc', 'mints': [MU, USDC], 'deposit_mint': MU, 'residual_owner': False}]
        self.assertIsNone(wallets.native_giver('mu-usdc', [MU, USDC], alone, NATIVE))

    def test_the_sole_owner_gives_before_the_holder(self):
        # SOL is in sol-usdc's pool only, but USDC is its deposit mint and djt-usdc the residual owner
        prof = [{'name': 'sol-usdc', 'mints': [SOL, USDC], 'deposit_mint': USDC, 'residual_owner': False},
                {'name': 'djt-usdc', 'mints': ['DJT', USDC], 'deposit_mint': 'DJT', 'residual_owner': True},
                {'name': 'mu-usdc', 'mints': [MU, USDC], 'deposit_mint': MU, 'residual_owner': False}]
        self.assertEqual(wallets.native_giver('mu-usdc', [MU, USDC], prof, NATIVE), 'sol-usdc')

    def test_with_no_user_the_residual_owner_gives(self):
        prof = [{'name': 'mu-usdc', 'mints': [MU, USDC], 'deposit_mint': MU, 'residual_owner': False},
                {'name': 'djt-usdc', 'mints': ['DJT', USDC], 'deposit_mint': 'DJT', 'residual_owner': True}]
        self.assertEqual(wallets.native_giver('mu-usdc', [MU, USDC], prof, NATIVE), 'djt-usdc')
        self.assertIsNone(wallets.native_giver('djt-usdc', ['DJT', USDC], prof, NATIVE))


class Flows(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(delta=amounts, price=prices)
    def test_two_rows_that_cancel_or_none(self, delta, price):
        rows = wallets.internal_flows('mu-usdc', 'sol-usdc', delta, price, PROFILES, NATIVE, 'd')
        if abs(delta) <= wallets.DUST:
            self.assertEqual(rows, [])
            return
        self.assertEqual(sorted(r['kind'] for r in rows), ['internal_in', 'internal_out'])
        self.assertEqual({r['profile'] for r in rows}, {'mu-usdc', 'sol-usdc'})
        x = abs(delta)
        for r in rows:
            self.assertEqual(r['amounts'][NATIVE], x)
            self.assertGreaterEqual(r['usd'], 0.0)
        self.assertEqual(rows[0]['usd'], rows[1]['usd'])                  # one move, one value, both sides
        if price is None:
            self.assertTrue(all(r['usd'] == 0.0 and 'price unknown' in r['detail'] for r in rows))
        else:
            self.assertAlmostEqual(rows[0]['usd'], round(x * price, 6))

    @settings(max_examples=200, deadline=None)
    @given(delta=amounts)
    def test_spending_takes_from_the_giver_a_refund_gives_back(self, delta):
        rows = {r['kind']: r['profile'] for r in
                wallets.internal_flows('mu-usdc', 'sol-usdc', delta, 100.0, PROFILES, NATIVE, 'd')}
        if abs(delta) <= wallets.DUST:
            return
        want = ({'internal_out': 'sol-usdc', 'internal_in': 'mu-usdc'} if delta < 0
                else {'internal_out': 'mu-usdc', 'internal_in': 'sol-usdc'})
        self.assertEqual(rows, want)

    def test_exactly_dust_and_no_delta_book_nothing(self):
        for d in (wallets.DUST, -wallets.DUST, 0.0, None):
            self.assertEqual(wallets.internal_flows('mu-usdc', 'sol-usdc', d, 100.0, PROFILES, NATIVE, 'd'), [])
        self.assertEqual(len(wallets.internal_flows('mu-usdc', 'sol-usdc', -2 * wallets.DUST, 100.0, PROFILES,
                                                    NATIVE, 'd')), 2)

    def test_a_non_native_books_native_never_reads_as_its_token_a(self):
        rows = {r['profile']: r for r in
                wallets.internal_flows('mu-usdc', 'sol-usdc', -0.0675, 122.5, PROFILES, NATIVE, 'd')}
        self.assertEqual(rows['mu-usdc']['amounts'], {SOL: 0.0675, MU: 0.0})
        self.assertEqual(rows['mu-usdc']['sol'], 0.0)
        self.assertEqual(rows['sol-usdc']['amounts'], {SOL: 0.0675})
        self.assertEqual(rows['sol-usdc']['sol'], 0.0675)

    def test_profiles_unknown_still_book_no_token_a_amount(self):
        rows = wallets.internal_flows('mu-usdc', 'sol-usdc', -0.0675, 122.5, [], NATIVE, 'd')
        self.assertTrue(all(r['sol'] == 0.0 and r['amounts'] == {SOL: 0.0675} for r in rows))


# --- since_start on the incident's rows ---------------------------------------------------

BOOKS = ('sw-sol', 'sw-mu')
SW_WALLET = 'w-shared-books'


def cleanup():
    with db.cursor(commit=True) as cur:
        cur.execute('delete from snapshots where mint like %s', ('sw-%',))
        cur.execute('delete from positions where config_name = any(%s)', (list(BOOKS),))
        cur.execute('delete from payouts where config_name = any(%s)', (list(BOOKS),))
        cur.execute('delete from capital_flows where profile = any(%s)', (list(BOOKS),))
        cur.execute('delete from config where name = any(%s)', (list(BOOKS),))
        cur.execute('delete from wallets where id = %s', (SW_WALLET,))


class Replay(unittest.TestCase):
    """capital_flows and snapshots of 2026-10-02, as the live book held them."""

    def setUp(self):
        cleanup(); self.addCleanup(cleanup)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into wallets (id, chain, address, secret_env) values (%s, 'solana', '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f', 'WALLET_SECRET_PATH')",
                        (SW_WALLET,))
            for name, mints in (('sw-sol', [SOL, USDC]), ('sw-mu', [MU, USDC])):
                cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, wallet_id, mints) "
                            "values (%s, 'P', 'X/USDC', 100, 1000, %s, %s)", (name, SW_WALLET, mints))
                cur.execute("insert into positions (mint, config_name, pool) values (%s, %s, 'P')",
                            (f'{name}-pos', name))

    def flow(self, ts, kind, profile, usd, price, amounts, sol=0.0, usdc=0.0, sig=None):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, amounts, profile, "
                        "wallet_id) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (ts, kind, sol, usdc, usd, price, sig, json.dumps(amounts), profile, SW_WALLET))

    def snap(self, ts, profile, price, equity):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) "
                        "values (%s, %s, %s, true, '1', %s)", (ts, f'{profile}-pos', price, equity))

    def mu_rows(self):
        self.flow('2026-10-02 13:19:17+00', 'baseline', 'sw-mu', 210.461299, 1108.6345668598376,
                  {MU: 0.18983829759841814, USDC: 0.0})
        self.flow('2026-10-02 13:14:02+00', 'deposit', 'sw-mu', 206.204678, 1086.3283675309503,
                  {MU: 0.189818, USDC: 0.0}, sig='mSWfmx')

    def test_a_deposit_before_the_baseline_is_in_it_already(self):
        self.mu_rows()
        self.snap('2026-10-02 16:00+00', 'sw-mu', 1080.21, 207.75)
        s = db.since_start(0.0, 'sw-mu')
        self.assertAlmostEqual(s['start_usd'], 210.4613, places=4)       # was 416.67: the MU twice
        self.assertAlmostEqual(s['start_sol'], 0.189838, places=6)
        self.assertAlmostEqual(s['profit_usd'], 207.75 - 210.461299, places=4)

    def test_a_deposit_after_the_baseline_still_counts(self):
        self.mu_rows()
        self.flow('2026-10-03 10:00+00', 'deposit', 'sw-mu', 50.0, 1000.0, {MU: 0.05, USDC: 0.0}, sig='late')
        self.snap('2026-10-03 11:00+00', 'sw-mu', 1000.0, 260.0)
        s = db.since_start(0.0, 'sw-mu')
        self.assertAlmostEqual(s['start_usd'], 260.461299, places=4)
        self.assertAlmostEqual(s['start_sol'], 0.239838, places=6)

    def test_a_flow_at_the_baselines_instant_is_in_it(self):
        self.mu_rows()
        self.flow('2026-10-02 13:19:17+00', 'withdrawal', 'sw-mu', 5.0, 1000.0, {MU: 0.005}, sig='same')
        self.snap('2026-10-02 16:00+00', 'sw-mu', 1080.21, 207.75)
        self.assertAlmostEqual(db.since_start(0.0, 'sw-mu')['start_usd'], 210.4613, places=4)

    def test_the_rent_moves_neither_books_profit(self):
        # sol-usdc: its baseline, then the rent mu-usdc's open took (0.0675967 SOL at 122.509)
        self.flow('2026-09-22 19:56:24+00', 'baseline', 'sw-sol', 248.096041, 117.98, {}, sol=2.102865244)
        self.snap('2026-10-02 13:17:39+00', 'sw-sol', 122.48, 241.35)
        before = db.since_start(0.0, 'sw-sol')
        self.mu_rows()
        for r in wallets.internal_flows('sw-mu', 'sw-sol', -0.0675967, 122.509,
                                        [{'name': 'sw-sol', 'mints': [SOL, USDC]}, {'name': 'sw-mu', 'mints': [MU, USDC]}],
                                        SOL, 'open'):
            self.flow('2026-10-02 13:19:20+00', r['kind'], r['profile'], r['usd'], r['price'], r['amounts'], r['sol'])
        self.snap('2026-10-02 13:19:44+00', 'sw-sol', 122.509, 241.35 - 8.2810)    # the $8.28 step
        self.snap('2026-10-02 13:24:27+00', 'sw-mu', 1108.635, 218.86)               # rent counted
        sol = db.since_start(0.0, 'sw-sol')
        self.assertAlmostEqual(sol['profit_usd'], before['profit_usd'], places=2)      # no loss from mu's rent
        self.assertAlmostEqual(sol['start_sol'], 2.102865244 - 0.0675967, places=6)
        mu = db.since_start(0.0, 'sw-mu')
        self.assertAlmostEqual(mu['start_usd'], 210.461299 + 8.281, places=3)
        self.assertAlmostEqual(mu['profit_usd'], 218.86 - 218.742, places=2)           # no phantom +$8
        # the rent counts at its dollar value in mu-usdc's hold benchmark, not as MU
        self.assertAlmostEqual(mu['hold_start_assets_usd'], 0.18983829759841814 * 1108.635 + 8.281, places=3)
        self.assertAlmostEqual(mu['start_sol'], 0.189838, places=6)

    def test_a_refund_returns_the_rent_to_the_giver(self):
        prof = [{'name': 'sw-sol', 'mints': [SOL, USDC]}, {'name': 'sw-mu', 'mints': [MU, USDC]}]
        self.flow('2026-09-22 19:56:24+00', 'baseline', 'sw-sol', 248.0, 118.0, {}, sol=2.1)
        for delta, ts in ((-0.0675, '2026-10-02 13:19:20+00'), (0.0674, '2026-10-03 13:00+00')):
            for r in wallets.internal_flows('sw-mu', 'sw-sol', delta, 120.0, prof, SOL, 'x'):
                self.flow(ts, r['kind'], r['profile'], r['usd'], r['price'], r['amounts'], r['sol'])
        self.snap('2026-10-03 13:01+00', 'sw-sol', 120.0, 240.0)
        s = db.since_start(0.0, 'sw-sol')
        self.assertAlmostEqual(s['start_usd'], 248.0 - 8.1 + 8.088, places=6)
        self.assertAlmostEqual(s['start_sol'], 2.1 - 0.0001, places=6)          # mu-usdc's fee stays its cost

    def test_internal_flows_are_not_deposits_in_the_flow_totals(self):
        self.mu_rows()
        self.flow('2026-10-02 13:19:20+00', 'internal_in', 'sw-mu', 8.28, 122.5, {SOL: 0.0676, MU: 0.0})
        self.assertEqual(db.flow_totals('sw-mu')['deposits'], 1)


# --- rent pricing ------------------------------------------------------------------------

class Rent(unittest.TestCase):
    def setUp(self):
        rebalancer.NATIVE_PX.clear(); rebalancer.RENT_UNPRICED.clear()
        self.addCleanup(rebalancer.NATIVE_PX.clear); self.addCleanup(rebalancer.RENT_UNPRICED.clear)
        self.told = []
        p = mock.patch.object(rebalancer, 'notify', lambda ev, **kw: self.told.append(ev))
        p.start(); self.addCleanup(p.stop)

    def stat(self, rent_usd, rent_sol=0.0675):
        return {'positionUsd': 210.0, 'rentUsd': rent_usd, 'rentSol': rent_sol}

    def test_the_signers_price_is_used_and_remembered(self):
        with mock.patch.object(db, 'native_price', return_value=None):
            self.assertAlmostEqual(rebalancer.position_usd(self.stat(8.2688)), 218.2688)
            self.assertAlmostEqual(rebalancer.NATIVE_PX['px'], 8.2688 / 0.0675)
            # the next poll's Jupiter request fails: the rent keeps its value (was $0)
            self.assertAlmostEqual(rebalancer.position_usd(self.stat(None)), 218.2688, places=6)
        self.assertEqual(self.told, [])

    def test_a_null_is_priced_from_the_native_pools_snapshot(self):
        with mock.patch.object(db, 'native_price', return_value=120.0) as q:
            self.assertAlmostEqual(rebalancer.position_usd(self.stat(None)), 210.0 + 0.0675 * 120.0)
        self.assertEqual(q.call_args.args[0], config.CAPS['native_mint'])

    def test_a_cache_older_than_its_limit_is_not_used(self):
        rebalancer.NATIVE_PX.update(px=100.0, at=time.time() - rebalancer.NATIVE_PX_MAX_AGE_S - 1)
        with mock.patch.object(db, 'native_price', return_value=None):
            self.assertEqual(rebalancer.position_usd(self.stat(None)), 210.0)
            self.assertEqual(rebalancer.position_usd(self.stat(None)), 210.0)
        self.assertEqual(self.told, ['rent_unpriced'])                          # said once, not every poll

    def test_a_database_error_falls_back_to_the_cache(self):
        rebalancer.NATIVE_PX.update(px=100.0, at=time.time())
        with mock.patch.object(db, 'native_price', side_effect=RuntimeError('db')):
            self.assertAlmostEqual(rebalancer.rent_usd(self.stat(None)), 6.75)

    def test_no_rent_is_zero_without_asking(self):
        asked = []
        with mock.patch.object(rebalancer, 'native_usd', lambda: asked.append(1)):
            self.assertEqual(rebalancer.rent_usd({'rentUsd': None, 'rentSol': 0}), 0.0)
            self.assertEqual(rebalancer.rent_usd({}), 0.0)
        self.assertEqual((asked, self.told), ([], []))

    def test_a_zero_rent_usd_is_a_price_not_a_null(self):
        with mock.patch.object(rebalancer, 'native_usd', side_effect=AssertionError('asked')):
            self.assertEqual(rebalancer.rent_usd({'rentUsd': 0.0, 'rentSol': 0.0}), 0.0)

    def test_only_a_positive_price_is_remembered(self):
        for bad in (None, 0.0, -1.0):
            rebalancer.note_native_px(bad)
            self.assertEqual(rebalancer.NATIVE_PX, {})
        rebalancer.note_native_px(1e-9)
        self.assertEqual(rebalancer.NATIVE_PX['px'], 1e-9)

    def test_a_cache_exactly_its_limit_old_still_prices(self):
        with mock.patch.object(db, 'native_price', return_value=None), \
                mock.patch.object(rebalancer.time, 'time', return_value=5000.0):
            rebalancer.NATIVE_PX.update(px=100.0, at=5000.0 - rebalancer.NATIVE_PX_MAX_AGE_S)
            self.assertEqual(rebalancer.native_usd(), 100.0)
            rebalancer.NATIVE_PX.update(at=5000.0 - rebalancer.NATIVE_PX_MAX_AGE_S - 1e-3)
            self.assertIsNone(rebalancer.native_usd())

    def test_without_the_signers_dollar_figure_the_close_estimate_and_the_rent(self):
        st_ = {'closeEstA': 2.0, 'closeEstB': 30.0, 'quoteUsd': 1.5, 'price': 10.0, 'rentUsd': 4.0, 'rentSol': 0.04}
        with mock.patch.object(rebalancer, 'ui_price', lambda s: s['price']):
            self.assertAlmostEqual(rebalancer.position_usd(st_), (2.0 * 10.0 + 30.0) * 1.5 + 4.0)
            for k in ('closeEstA', 'closeEstB', 'quoteUsd'):
                self.assertIsNone(rebalancer.position_usd(dict(st_, **{k: None})))
            self.assertAlmostEqual(rebalancer.position_usd(dict(st_, closeEstA=0.0, closeEstB=0.0)), 4.0)

    @settings(max_examples=200, deadline=None)
    @given(rent_sol=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
           px=st.floats(min_value=1.0, max_value=1000.0, allow_nan=False),
           fails=st.lists(st.booleans(), min_size=1, max_size=12))
    def test_failed_signer_prices_never_move_the_mark(self, rent_sol, px, fails):
        """Within the cache's life, a run of polls whose signer price fails
        or not gives one mark: the 2026-10-02 flicker cannot happen."""
        rebalancer.NATIVE_PX.clear()
        marks = []
        with mock.patch.object(db, 'native_price', return_value=None):
            rebalancer.position_usd(self.stat(round(rent_sol * px, 4), rent_sol))
            for f in fails:
                marks.append(rebalancer.position_usd(self.stat(None if f else round(rent_sol * px, 4), rent_sol)))
        self.assertLess(max(marks) - min(marks), 1e-3)


class NativePrice(unittest.TestCase):
    """db.native_price against the test database."""

    def setUp(self):
        Replay.setUp(self)

    snap = Replay.snap

    def test_the_newest_fresh_native_stable_snapshot(self):
        self.snap(db.now(), 'sw-sol', 121.5, 240.0)
        self.snap(db.now(), 'sw-mu', 1080.0, 210.0)                              # MU/USDC: not native
        self.assertEqual(db.native_price(SOL, [USDC], 900), 121.5)

    def test_an_old_snapshot_is_no_price(self):
        self.snap('2020-01-01 00:00+00', 'sw-sol', 121.5, 240.0)
        self.assertIsNone(db.native_price(SOL, [USDC], 900))

    def test_a_non_stable_quote_is_no_price(self):
        self.snap(db.now(), 'sw-sol', 121.5, 240.0)
        self.assertIsNone(db.native_price(SOL, ['NOTSTABLE'], 900))


# --- the loop, end to end: rent paid from the owner's SOL ---------------------------------

RENT = 0.0675
FEE = 0.00001


class RentChain(FakeChain):
    """FakeChain whose writes on a pool without the native token pay the
    fee, and whose open locks RENT and close refunds it, from the wallet's
    SOL; its status reports the rent with no price (a failed Jupiter
    request), as signer_dlmm does."""

    def __init__(self, **held):
        super().__init__(**held)
        self.rent = {}

    def answer(self, *args, dex=None, extra_env=None):
        native_pool = POOLS[config.POOL]['native'] is not None
        out, err = super().answer(*args, dex=dex, extra_env=extra_env)
        cmd = args[0]
        if out and out.get('signature') and not native_pool:
            self.wallet[SOL] -= FEE
            if cmd == 'open':
                self.wallet[SOL] -= RENT
                self.rent[config.POOL] = RENT
            if cmd == 'close':
                self.wallet[SOL] += self.rent.pop(config.POOL, 0.0)
        if cmd == 'status' and out and out.get('positionMint') and config.POOL in self.rent:
            out = dict(out, rentSol=self.rent[config.POOL], rentUsd=None)
        return out, err


class Loop(Fixture):
    def setUp(self):
        super().setUp()
        self.chain = RentChain()
        for p in (mock.patch.object(rebalancer, '_chain', self.chain),
                  mock.patch.object(wallets, '_rpc', lambda *a: self.chain.rpc(*a)),
                  mock.patch.dict(rebalancer.NATIVE_PX, clear=True)):
            p.start(); self.addCleanup(p.stop)

    def internal(self):
        with db.cursor() as cur:
            cur.execute("select kind, profile, sol, usd, amounts, signature, detail from capital_flows "
                        "where kind like 'internal%%' order by id")
            return cur.fetchall()

    def snapshot(self, name):
        """The poll's snapshot of `name` (rebalancer.main): its sleeve and its mark."""
        pool = {'e2e-sol': SOL_POOL, 'e2e-mu': MU_POOL}[name]
        with self.as_profile(name):
            st_, _ = rebalancer.chain('status')
            db.snapshot(st_['positionMint'], POOLS[pool]['price'], True, '1', 0, 0, 0,
                        rebalancer.wallet(pool)['walletUsd'], rebalancer.position_usd(st_))

    def open_both(self):
        self.chain.wallet.update({SOL: 2.0})
        self.poll('e2e-sol')                                            # sol-usdc opens
        self.assertIn(SOL_POOL, self.chain.positions)
        self.snapshot('e2e-sol')                                        # and marks SOL at 150
        self.chain.wallet[MU] = 10.0
        sol_before = self.chain.wallet[SOL]
        self.poll('e2e-mu')
        self.assertIn(MU_POOL, self.chain.positions)
        return sol_before

    def test_a_mu_open_books_its_rent_and_fees_from_sol_usdcs_sleeve(self):
        sol_before = self.open_both()
        spent = sol_before - self.chain.wallet[SOL]
        self.assertGreater(spent, RENT)                                   # rent and two fees (swap, open)
        rows = self.internal()
        outs = [r for r in rows if r['kind'] == 'internal_out']
        ins = [r for r in rows if r['kind'] == 'internal_in']
        self.assertEqual({r['profile'] for r in outs}, {'e2e-sol'})
        self.assertEqual({r['profile'] for r in ins}, {'e2e-mu'})
        self.assertAlmostEqual(sum(r['amounts'][SOL] for r in outs), spent, places=9)
        self.assertAlmostEqual(sum(float(r['usd']) for r in outs), spent * 150.0, places=4)
        self.assertTrue(all(r['signature'] is None and 'by e2e-mu' in r['detail'] for r in rows))
        sigs = sorted(self.chain.sig_slot, key=self.chain.sig_slot.get)[-2:]      # mu-usdc's swap, open
        self.assertEqual({r['detail'] for r in rows},
                         {f'rebalance by e2e-mu: {sigs[0]}', f'open by e2e-mu: {sigs[1]}'})
        self.assertTrue(all(r['amounts'].get(MU) == 0.0 for r in ins))

    def test_the_rent_is_in_mu_usdcs_mark_although_the_signer_gave_no_price(self):
        self.open_both()
        with self.as_profile('e2e-mu'):
            st_, _ = rebalancer.chain('status')                          # rentSol, no rentUsd (RentChain)
            self.assertIsNone(st_['rentUsd'])
            mark = rebalancer.position_usd(st_)
        self.assertAlmostEqual(mark, st_['positionUsd'] + RENT * 150.0, places=6)

    def test_neither_book_moves_when_mu_opens(self):
        self.chain.wallet.update({SOL: 2.0})
        self.poll('e2e-sol')
        self.snapshot('e2e-sol')
        sol0 = db.since_start(0.0, 'e2e-sol')
        self.chain.wallet[MU] = 10.0
        self.poll('e2e-mu')                                              # swap and open: rent and fees in SOL
        self.snapshot('e2e-sol')                                         # sol-usdc's next poll
        sol1 = db.since_start(0.0, 'e2e-sol')
        self.assertLess(sol1['equity_usd'], sol0['equity_usd'] - RENT * 150.0 + 1e-6)   # the SOL left
        self.assertAlmostEqual(sol1['profit_usd'], sol0['profit_usd'], places=4)       # and is no loss
        self.snapshot('e2e-mu')
        mu = db.since_start(0.0, 'e2e-mu')
        # mu-usdc bears only its two writes' fees: not -$RENT, not +$RENT, not its $100 twice
        self.assertAlmostEqual(mu['profit_usd'], -2 * FEE * 150.0, delta=1e-4)
        with mock.patch.object(db, 'native_price', return_value=None):
            self.snapshot('e2e-mu')                                      # no fresh price: the cache marks it
        self.assertAlmostEqual(db.since_start(0.0, 'e2e-mu')['profit_usd'], mu['profit_usd'], places=4)

    def test_a_close_refunds_the_rent_to_the_giver(self):
        self.open_both()
        mint = self.chain.positions[MU_POOL]['mint']
        with self.as_profile('e2e-mu'):
            out, err = rebalancer.chain('close', mint, '--execute')
        self.assertIsNone(err)
        last = self.internal()[-2:]
        self.assertEqual({(r['kind'], r['profile']) for r in last},
                         {('internal_out', 'e2e-mu'), ('internal_in', 'e2e-sol')})
        self.assertAlmostEqual(last[0]['amounts'][SOL], RENT - FEE, places=9)

    def test_sol_usdcs_own_writes_book_no_internal_flow(self):
        self.chain.wallet.update({SOL: 2.0})
        self.poll('e2e-sol')
        self.assertEqual(self.internal(), [])

    def test_a_crash_between_send_and_booking_books_the_flows_once(self):
        self.chain.wallet.update({SOL: 2.0})
        self.poll('e2e-sol')
        self.chain.wallet[MU] = 10.0
        self.chain.lags = [0, 0] + [1] * 400                             # before: two reads; then never the slot
        with self.as_profile('e2e-mu'):
            out, _ = rebalancer.chain('rebalance', MU, USDC, '50', '50', '--execute', dex='jupiter',
                                      extra_env={'LPBOT_SLEEVE': json.dumps({MU: 10.0, USDC: 0.0})})
        self.assertTrue(out['sent'])
        self.assertEqual(self.internal(), [])
        self.assertIsNotNone(wallets.settle_state(WALLET)[1]['native'])
        self.chain.lags = []
        with self.as_profile('e2e-sol'):                                 # another process books it
            self.assertTrue(rebalancer.settle_pending(0))
            self.assertTrue(rebalancer.settle_pending(0))                # and only once
        rows = self.internal()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]['amounts'][SOL], FEE, places=9)

    def test_a_write_without_signatures_says_so(self):
        p = {'profile': 'e2e-mu', 'command': 'open', 'native': {'mint': SOL, 'giver': 'e2e-sol'}}
        with mock.patch.object(rebalancer, 'native_usd', return_value=100.0):
            for sigs in (None, []):
                rows = rebalancer.native_flows(dict(p, signatures=sigs), -0.01)
                self.assertEqual({r['detail'] for r in rows}, {'open by e2e-mu: no signature'})
            rows = rebalancer.native_flows(dict(p, signatures=['a', 'b']), -0.01)
            self.assertEqual({r['detail'] for r in rows}, {'open by e2e-mu: a b'})

    def test_a_pending_write_from_before_the_change_books_as_before(self):
        wallets.set_pending(WALLET, {'profile': 'e2e-mu', 'command': 'open', 'mints': [USDC],
                                     'before': {USDC: 0.0}, 'before_slot': self.chain.slot, 'signatures': []})
        with self.as_profile('e2e-sol'):
            self.assertTrue(rebalancer.settle_pending(0))
        self.assertEqual(self.internal(), [])


if __name__ == '__main__':
    unittest.main()
