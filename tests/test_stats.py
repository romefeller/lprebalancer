"""Stats per wallet and per pool, and their sum (stats.py, db's book scope).

The sums: every dollar figure of the TOTAL is the sum of the pools' figures;
token amounts never add across different tokens; only active pools are shown;
rows from before 020 (NULL profile, wallet or config_name) are sol-usdc's.
"""
import contextlib
import datetime as dt
import io
import json
import unittest

from hypothesis import given, settings, HealthCheck, strategies as st

from _fixtures import db, reset_ledger, ensure_profile
import stats

MU_POOL = '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5'
SOL_MINT = 'So11111111111111111111111111111111111111112'
MU_MINT = 'MUmint1111111111111111111111111111111111111'
DJT_MINT = 'DJTmint111111111111111111111111111111111111'
EXTRA = ('mu-usdc', 'djt-usdc', 'base-weth-usdc')


def T(hours_ago):
    return db.now() - dt.timedelta(hours=hours_ago)


@contextlib.contextmanager
def context(profile=None, wallet_id=None):
    """db.CONTEXT for the block, restored after."""
    saved = dict(db.CONTEXT)
    db.CONTEXT.update(profile=profile, wallet_id=wallet_id)
    try:
        yield
    finally:
        db.CONTEXT.update(saved)


# The test database is shared: the wallets, claims and profiles found there
# are put back as they were after this module (setUpModule / tearDownModule).
SAVED = {}
ARRAYS = ('bands', 'dexes', 'execute_dexes', 'regime_widths', 'mints')


def setUpModule():
    with db.cursor() as cur:
        for t in ('config', 'wallets', 'wallet_claims'):
            cur.execute(f'select * from {t}')
            SAVED[t] = [dict(r) for r in cur.fetchall()]


def tearDownModule():
    insert = lambda cur, table, row, tail='': cur.execute(
        f"insert into {table} ({', '.join(row)}) {tail} values ({', '.join(['%s'] * len(row))})", list(row.values()))
    with db.cursor(commit=True) as cur:
        cur.execute('delete from wallet_claims')
        cur.execute('delete from config where not (name = any(%s))', ([r['name'] for r in SAVED['config']],))
        cur.execute('update config set enabled = false, residual_owner = false, active = false')
        cur.execute('delete from wallets where not (id = any(%s)) and id not in '
                    '(select wallet_id from config where wallet_id is not null)', ([w['id'] for w in SAVED['wallets']],))
        for w in SAVED['wallets']:
            cur.execute('delete from wallets where id = %s and id not in '
                        '(select wallet_id from config where wallet_id is not null)', (w['id'],))
            cur.execute('select 1 from wallets where id = %s', (w['id'],))
            if not cur.fetchone():
                insert(cur, 'wallets', w)
        for r in SAVED['config']:
            cur.execute('select 1 from config where name = %s', (r['name'],))
            if cur.fetchone():
                cur.execute('update config set wallet_id = %s, deposit_mint = %s where name = %s',
                            (r['wallet_id'], r['deposit_mint'], r['name']))
            else:
                insert(cur, 'config', r, 'overriding system value')
        for r in SAVED['config']:
            cur.execute('update config set enabled = %s, residual_owner = %s, active = %s where name = %s',
                        (r['enabled'], r['residual_owner'], r['active'], r['name']))
        for c in SAVED['wallet_claims']:
            insert(cur, 'wallet_claims', c)


def drop_extra():
    """Every profile disabled and off any wallet, this module's own removed:
    the pre-020 shape, with sol-usdc the active one (ensure_profile)."""
    with db.cursor(commit=True) as cur:
        cur.execute('delete from wallet_claims')
        cur.execute('delete from config where name = any(%s)', (list(EXTRA),))
        cur.execute('update config set wallet_id = null, enabled = false, residual_owner = false, deposit_mint = null')


def multi_wallet():
    """sol-usdc (enabled, residual owner) and mu-usdc (enabled) and djt-usdc
    (enabled) on wallet sol-lp; `other` stays disabled."""
    drop_extra()
    ensure_profile()
    with db.cursor(commit=True) as cur:
        cur.execute("insert into wallets (id, chain, address, secret_env, label) values ('sol-lp', 'solana', "
                    "'11111111111111111111111111111111', 'WALLET_SECRET_PATH', 'LP wallet') on conflict (id) do "
                    "update set chain = excluded.chain, label = excluded.label")
        cur.execute("update config set wallet_id = 'sol-lp', enabled = true, residual_owner = true, "
                    "deposit_mint = %s where name = 'sol-usdc'", (SOL_MINT,))
        for name, pair, mint in (('mu-usdc', 'MU/USDC', MU_MINT), ('djt-usdc', 'DJT/USDC', DJT_MINT)):
            cur.execute("insert into config (name, pool, pair_label, token_a, token_b, capital_usd, max_usd, dex, "
                        "wallet_id, enabled, deposit_mint) values (%s, %s, %s, %s, 'USDC', 100, 140, "
                        "'meteora-dlmm', 'sol-lp', true, %s)", (name, MU_POOL, pair, pair.split('/')[0], mint))


PAIRS = {'sol-usdc': 'SOL/USDC', 'mu-usdc': 'MU/USDC', 'djt-usdc': 'DJT/USDC'}


def position(mint, profile, hours=30, closed=False):
    db.open_position(mint, MU_POOL, PAIRS.get(profile, 'X/USDC'), 9.5, 10.5, 2.0, f'sig-{mint}', 100.0, 'test', profile, 'meteora-dlmm')
    with db.cursor(commit=True) as cur:
        cur.execute('update positions set opened_at = %s, closed_at = %s where mint = %s',
                    (T(hours), T(1) if closed else None, mint))


def snap(mint, hours_ago, accrued_usd, equity, lp=90.0, wallet=None, accrued=(0.0, 0.0), in_range=True):
    db.snapshot(mint, 10.0, in_range, 1, accrued[0], accrued[1], accrued_usd,
                wallet if wallet is not None else equity - lp - accrued_usd, lp)
    with db.cursor(commit=True) as cur:
        cur.execute('update snapshots set ts = %s where id = (select max(id) from snapshots)', (T(hours_ago),))


def harvest(mint, hours_ago, a, b, usd):
    db.record_harvest(mint, a, b, usd, f'h-{mint}-{hours_ago}')
    with db.cursor(commit=True) as cur:
        cur.execute('update harvests set ts = %s where id = (select max(id) from harvests)', (T(hours_ago),))


def payout(profile, usd, kind='paid'):
    db.record_payout(profile, 'p', 'USDC-mint', 'USDC', usd, usd, kind)


def flow(kind, usd, profile, wallet_id, sol=0.0, usdc=0.0, hours_ago=40, amounts=None):
    with db.cursor(commit=True) as cur:
        cur.execute('insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, wallet_id, '
                    'profile, amounts) values (%s,%s,%s,%s,%s,10,%s,%s,%s,%s,%s)',
                    (T(hours_ago), kind, sol, usdc, usd, None if kind == 'baseline' else f'f-{profile}-{usd}',
                     'test', wallet_id, profile, json.dumps(amounts) if amounts else None))


def event(kind, profile):
    with db.cursor(commit=True) as cur:
        cur.execute('insert into events (ts, kind, detail, profile) values (now(), %s, %s, %s)', (kind, 't', profile))


# --- pure: the sums -------------------------------------------------------------

money = st.one_of(st.none(), st.floats(-1e6, 1e6, allow_nan=False).map(lambda x: round(x, 4)))
positive = st.one_of(st.none(), st.floats(0.01, 1e6).map(lambda x: round(x, 2)))
symbol = st.sampled_from(['SOL', 'MU', 'DJT', 'WETH', 'USDC'])


@st.composite
def pool_record(draw):
    r = {k: draw(money) for k in stats.USD_KEYS}
    r['equity_usd'] = draw(positive)
    r.update({k: draw(st.integers(0, 500)) for k in stats.COUNT_KEYS})
    r.update({k: draw(st.one_of(st.none(), st.floats(0, 1e4))) for k in stats.TOKEN_KEYS})
    r.update(token_a=draw(symbol), token_b='USDC', in_range_pct=draw(st.one_of(st.none(), st.floats(0, 100))),
             tracked_days=draw(st.one_of(st.none(), st.floats(0, 30))), profile=draw(st.text('abc-', min_size=1)),
             wallet_id=draw(st.sampled_from(['sol-lp', 'base-lp'])))
    return r


class Total(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.lists(pool_record(), max_size=8))
    def test_every_dollar_of_the_total_is_the_sum_of_the_pools(self, recs):
        t = stats.total(recs)
        self.assertEqual(t['pools'], len(recs))
        for k in stats.USD_KEYS:
            known = [r[k] for r in recs if r[k] is not None]
            if not known:
                self.assertIsNone(t[k], k)
            else:
                self.assertAlmostEqual(t[k], sum(known), delta=1e-3 + 1e-9 * sum(abs(x) for x in known), msg=k)
        for k in stats.COUNT_KEYS:
            self.assertEqual(t[k], sum(r[k] for r in recs))

    @settings(max_examples=200, deadline=None)
    @given(st.lists(pool_record(), max_size=8))
    def test_the_total_carries_no_token_amount(self, recs):
        t = stats.total(recs)
        self.assertFalse(set(t) & set(stats.TOKEN_KEYS))
        self.assertFalse(any(k.endswith(('_a', '_b')) for k in t))

    @settings(max_examples=200, deadline=None)
    @given(st.lists(pool_record(), min_size=1, max_size=8))
    def test_the_apr_is_the_summed_rate_on_the_summed_equity(self, recs):
        t = stats.total(recs)
        for apr, rate in stats.APR_KEYS:
            both = [r for r in recs if r[rate] is not None and r['equity_usd']]
            if not both:
                self.assertIsNone(t[apr])
                continue
            want = sum(r[rate] for r in both) / sum(r['equity_usd'] for r in both) * 365 * 100
            self.assertAlmostEqual(t[apr], want, delta=0.01 + abs(want) * 1e-9)

    def test_wallet_subtotals_add_up_to_the_total(self):
        recs = [dict({k: 1.5 for k in stats.USD_KEYS}, **{k: 2 for k in stats.COUNT_KEYS},
                     wallet_id=w, profile=f'p{i}', in_range_pct=None) for i, w in enumerate(['a', 'a', 'b'])]
        by_w = [stats.total([r for r in recs if r['wallet_id'] == w]) for w in ('a', 'b')]
        t = stats.total(recs)
        for k in stats.USD_KEYS:
            self.assertEqual(t[k], sum(x[k] for x in by_w))

    def test_in_range_is_weighted_by_the_days_tracked(self):
        recs = [{'in_range_pct': 100.0, 'tracked_days': 3.0}, {'in_range_pct': 0.0, 'tracked_days': 1.0},
                {'in_range_pct': None, 'tracked_days': 9.0}]
        self.assertEqual(stats.total(recs)['in_range_pct'], 75.0)
        self.assertIsNone(stats.total([{'in_range_pct': 50.0, 'tracked_days': 0}])['in_range_pct'])
        self.assertEqual(stats.total([{'in_range_pct': 50.0, 'tracked_days': None},     # unknown days weigh 0
                                      {'in_range_pct': 100.0, 'tracked_days': 1.0}])['in_range_pct'], 100.0)

    def test_no_pool(self):
        t = stats.total([])
        self.assertEqual(t['pools'], 0)
        self.assertTrue(all(t[k] is None for k in stats.USD_KEYS))


class Classify(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.lists(st.tuples(st.booleans(), st.integers(0, 3)), max_size=10))
    def test_only_enabled_profiles_holding_a_position_are_active(self, rows):
        profiles = [{'name': f'p{i}', 'on': on} for i, (on, _) in enumerate(rows)]
        open_now = {f'p{i}': n for i, (_, n) in enumerate(rows) if n}
        active, dormant, disabled = stats.classify(profiles, open_now)
        self.assertEqual(sorted(p['name'] for p in active + dormant + disabled), sorted(p['name'] for p in profiles))
        for p, (on, n) in zip(profiles, rows):
            want = active if on and n > 0 else dormant if on else disabled
            self.assertIn(p, want)


@st.composite
def book(draw):
    b = {k: draw(money) for k in db.BOOK_USD}
    b['equity_usd'] = draw(positive)
    b.update({k: draw(st.integers(0, 99)) for k in db.BOOK_COUNTS})
    b['token_a'], b['token_b'] = draw(symbol), draw(st.sampled_from(['USDC', 'USDT']))
    for keys in db.BOOK_TOKENS.values():
        b.update({k: draw(st.floats(0, 1e4).map(lambda x: round(x, 6))) for k in keys})
    b.update({k: draw(st.sampled_from(['x', 'y'])) for k in db.BOOK_SAME})
    b.update(in_range_pct=draw(st.one_of(st.none(), st.floats(0, 100))), tracked_days=draw(st.floats(0, 9)),
             apr_pct=None, daily=[], since_start=None, split=None, by_pool=[], dexes_held=[], season=None,
             deployed_pct=draw(st.one_of(st.none(), st.floats(0, 100))), last_seen='2026-10-01T00:00:00+00:00',
             last_price=draw(positive), band={'in_range': True})
    return b


class CombineBooks(unittest.TestCase):
    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.lists(book(), min_size=2, max_size=6))
    def test_dollars_add_and_tokens_add_only_within_one_token(self, books):
        c = db.combine_books(books)
        for k in db.BOOK_USD:
            known = [b[k] for b in books if b[k] is not None]
            if known:
                self.assertAlmostEqual(c[k], sum(known), delta=1e-3 + 1e-9 * sum(abs(x) for x in known), msg=k)
            else:
                self.assertIsNone(c[k])
        for side, keys in db.BOOK_TOKENS.items():
            same = len({b[side] for b in books}) == 1
            self.assertEqual(c[side], books[0][side] if same else None)
            for k in keys:
                if same:
                    self.assertAlmostEqual(c[k], sum(b[k] for b in books), places=4)
                else:
                    self.assertIsNone(c[k], f'{k} summed across {[b[side] for b in books]}')
        for k in db.BOOK_COUNTS:
            self.assertEqual(c[k], sum(b[k] for b in books))
        self.assertIsNone(c['last_price'])
        self.assertIsNone(c['band'])

    @settings(max_examples=50, deadline=None)
    @given(book())
    def test_one_book_is_itself(self, b):
        self.assertIs(db.combine_books([b]), b)

    def test_the_share_needs_every_share(self):
        a = {'equity_usd': 100.0, 'lp_usd': 90.0, 'deployed_pct': 90.0}
        b = {'equity_usd': 100.0, 'lp_usd': 50.0, 'deployed_pct': 50.0}
        self.assertEqual(db.combine_books([a, b])['deployed_pct'], 70.0)
        self.assertIsNone(db.combine_books([a, dict(b, deployed_pct=None)])['deployed_pct'])

    def test_days_and_since_start_add(self):
        line = lambda day, n, f, h: {'day': day, 'complete': True, 'recentres': n, 'idle_redeploys': 0,
                                     'fees_usd': f, 'fees_earned_usd': f, 'equity_open': 100.0,
                                     'equity_close': 101.0, 'paid_out_usd': 0.5, 'value_change_usd': 1.5,
                                     'price_open': 1.0, 'price_close': 2.0, 'hold_50_50_usd': h,
                                     'vs_hold_usd': None if h is None else 1.0, 'fees_per_recentre_usd': None}
        d = db.combine_days([line('2026-10-01', 2, 1.0, 100.0), line('2026-10-01', 0, 3.0, 50.0)])
        self.assertEqual((d['recentres'], d['fees_usd'], d['equity_open'], d['hold_50_50_usd'], d['vs_hold_usd']),
                         (2, 4.0, 200.0, 150.0, 2.0))
        self.assertEqual(d['fees_per_recentre_usd'], 2.0)
        self.assertIsNone(d['price_open'])
        self.assertIsNone(db.combine_days([line('d', 1, 1.0, 100.0), line('d', 1, 1.0, None)])['hold_50_50_usd'])
        self.assertIsNone(db.combine_days([]))
        one = line('d', 1, 1.0, None)
        self.assertIs(db.combine_days([one]), one)
        s = lambda since, start, value: dict({k: 0.0 for k in db.SINCE_USD}, since=since, days=1.0,
                                             start_usd=start, value_usd=value, start_sol=2.0)
        c = db.combine_since([s('2026-09-22T00:00:00+00:00', 200.0, 210.0), s('2026-09-30T00:00:00+00:00', 100.0, 90.0)])
        self.assertEqual((c['since'], c['start_usd'], c['value_usd'], c['profit_pct']),
                         ('2026-09-22T00:00:00+00:00', 300.0, 300.0, 0.0))
        self.assertIsNone(c['start_sol'])                            # SOL and MU amounts do not add


class Flags(unittest.TestCase):
    def test_scope_flags(self):
        self.assertEqual(db._scope_flags(['db.py', 'stats', '--pool', 'mu-usdc']),
                         (['db.py', 'stats'], {'profile': 'mu-usdc', 'wallet_id': None}))
        self.assertEqual(db._scope_flags(['db.py', '--wallet', 'w', 'json']),
                         (['db.py', 'json'], {'profile': None, 'wallet_id': 'w'}))
        with self.assertRaises(SystemExit):
            db._scope_flags(['db.py', 'stats', '--pool'])


# --- the database: whose rows -------------------------------------------------------

class Scope(unittest.TestCase):
    def setUp(self):
        drop_extra()
        ensure_profile()

    def tearDown(self):
        drop_extra()
        ensure_profile()

    def test_a_process_reads_its_own_book(self):
        with context('mu-usdc', 'sol-lp'):
            self.assertEqual(db.book_scope(), ['mu-usdc'])
            # before the deploy the active profile's NULL wallet is the legacy wallet
            self.assertEqual(db.book_scope(wallet_id='sol-lp'), ['sol-usdc'])
            self.assertEqual(db.book_scope(wallet_id='base-lp'), [])

    def test_before_020_the_active_profile_is_the_book(self):
        with context():
            self.assertEqual(db.book_scope(), ['sol-usdc'])
            self.assertEqual(db.book_scope(wallet_id=db.LEGACY_WALLET), ['sol-usdc'])
            with db.cursor(commit=True) as cur:
                cur.execute('update config set active = false')
            try:
                self.assertEqual(db.book_scope(), [db.LEGACY_PROFILE])
            finally:
                ensure_profile()

    def test_enabled_profiles_and_wallets(self):
        multi_wallet()
        with context():
            self.assertEqual(db.book_scope(), ['djt-usdc', 'mu-usdc', 'sol-usdc'])
            self.assertEqual(db.book_scope(wallet_id='sol-lp'), ['djt-usdc', 'mu-usdc', 'sol-usdc'])
            self.assertEqual(db.book_scope(wallet_id='base-lp'), [])
            self.assertEqual(db.book_scope('mu-usdc', 'base-lp'), [])
            self.assertEqual(db.book_scope('mu-usdc', 'sol-lp'), ['mu-usdc'])
            self.assertNotIn('other', db.book_scope())


class Attribution(unittest.TestCase):
    def setUp(self):
        reset_ledger()
        multi_wallet()

    def tearDown(self):
        reset_ledger()
        drop_extra()
        ensure_profile()

    def test_rows_without_a_profile_are_the_sol_book(self):
        position('OLD', None, hours=50, closed=True)                  # pre-020: no config_name
        snap('OLD', 49, 0.4, 200.0)
        harvest('OLD', 48, 0.01, 1.0, 2.0)
        payout(None, 0.7)
        flow('baseline', 200.0, None, None, sol=2.0)
        event('REBAND', None)
        position('MU1', 'mu-usdc')
        snap('MU1', 2, 0.3, 100.0)
        harvest('MU1', 3, 0.1, 0.5, 1.0)
        payout('mu-usdc', 0.25)
        event('open_failed', 'mu-usdc')
        with context():
            sol, mu = db.stats(profile='sol-usdc'), db.stats(profile='mu-usdc')
        self.assertEqual((sol['harvests'], sol['fees_realised_usd'], sol['rebands'], sol['positions_opened']),
                         (1, 2.0, 1, 1))
        self.assertEqual((mu['harvests'], mu['fees_realised_usd'], mu['failures'], mu['positions_opened']),
                         (1, 1.0, 1, 1))
        self.assertEqual(db.payout_totals('sol-usdc')['paid_usd'], 0.7)
        self.assertEqual(db.payout_totals('mu-usdc')['paid_usd'], 0.25)
        self.assertEqual(sol['since_start']['start_usd'], 200.0)
        self.assertIsNone(mu['since_start'])                         # mu-usdc has no baseline of its own
        self.assertEqual(db.reinvested_usd('mu-usdc'), 0.0)
        self.assertEqual([r['dex'] for r in db.by_pool('mu-usdc')], ['meteora-dlmm'])
        self.assertEqual(len(db.history(10, 'sol-usdc')), 1)
        self.assertEqual(len(db.history(10, 'mu-usdc')), 1)

    def test_each_process_books_only_its_pool(self):
        position('S1', 'sol-usdc')
        snap('S1', 2, 0.5, 240.0, lp=220.0)
        position('MU1', 'mu-usdc')
        snap('MU1', 1, 0.3, 100.0, lp=95.0)
        with context('sol-usdc', 'sol-lp'):
            self.assertEqual(db.stats()['equity_usd'], 240.0)
            self.assertEqual(db.stats()['position_pool'], db.stats(profile='sol-usdc')['position_pool'])
        with context('mu-usdc', 'sol-lp'):
            self.assertEqual(db.stats()['equity_usd'], 100.0)
            self.assertEqual(db.stats()['token_a'], 'MU')
        with context():
            both = db.stats()
        self.assertEqual(both['equity_usd'], 340.0)
        self.assertEqual(both['lp_usd'], 315.0)
        self.assertIsNone(both['token_a'])
        self.assertEqual(both['token_b'], 'USDC')

    def test_the_wallets_dust_is_counted_once(self):
        flow('baseline', 200.0, 'sol-usdc', 'sol-lp', sol=2.0)
        flow('baseline', 100.0, 'mu-usdc', 'sol-lp', amounts={MU_MINT: 10.0})
        position('S1', 'sol-usdc')
        snap('S1', 2, 0.0, 200.0)
        position('MU1', 'mu-usdc')
        snap('MU1', 1, 0.0, 100.0)
        with context('sol-usdc', 'sol-lp'):
            db.set_audit_value('uncounted_usd', 0.5)
        try:
            with context():
                self.assertEqual(db.stats(profile='sol-usdc')['since_start']['uncounted_usd'], 0.5)
                self.assertEqual(db.stats(profile='mu-usdc')['since_start']['uncounted_usd'], 0.0)
                self.assertEqual(db.stats()['since_start']['uncounted_usd'], 0.5)
                self.assertEqual(db.stats(profile='mu-usdc')['since_start']['start_sol'], 10.0)
        finally:
            with db.cursor(commit=True) as cur:
                cur.execute("delete from audit_state where key = 'sol-lp|uncounted_usd'")

    def test_flows_are_per_profile(self):
        flow('deposit', 50.0, 'mu-usdc', 'sol-lp')
        flow('withdrawal', 5.0, 'mu-usdc', 'sol-lp')
        flow('deposit', 7.0, None, None)
        self.assertEqual(db.flow_totals('mu-usdc'),
                         {'deposits_usd': 50.0, 'deposits': 1, 'withdrawals_usd': 5.0, 'withdrawals': 1})
        self.assertEqual(db.flow_totals('sol-usdc')['deposits_usd'], 7.0)
        with context():
            self.assertEqual(db.flow_totals()['deposits_usd'], 57.0)


class Portfolio(unittest.TestCase):
    def setUp(self):
        reset_ledger()
        multi_wallet()
        position('S1', 'sol-usdc')
        snap('S1', 3, 0.2, 240.0, lp=220.0, accrued=(0.001, 0.1))
        snap('S1', 1, 0.5, 241.0, lp=220.0, accrued=(0.002, 0.3))
        harvest('S1', 2, 0.01, 1.2, 2.4)
        position('MU1', 'mu-usdc')
        snap('MU1', 2, 0.1, 100.0, lp=95.0, accrued=(0.01, 0.05))
        snap('MU1', 1, 0.3, 101.0, lp=95.0, accrued=(0.02, 0.1))
        harvest('MU1', 2, 0.2, 0.4, 1.0)
        payout('sol-usdc', 1.0)
        payout('mu-usdc', 0.5)
        payout('mu-usdc', 0.25, 'reinvested')

    def tearDown(self):
        reset_ledger()
        drop_extra()
        ensure_profile()

    def test_only_active_pools_and_their_sum(self):
        with context('sol-usdc', 'sol-lp'):                           # the residual owner emits it
            p = stats.portfolio()
        self.assertEqual([r['profile'] for r in p['pools']], ['mu-usdc', 'sol-usdc'])
        self.assertEqual(p['dormant'], ['djt-usdc'])
        self.assertEqual(p['disabled'], ['other'])
        self.assertEqual(p['disabled_holding'], [])
        for k in stats.USD_KEYS:
            known = [r[k] for r in p['pools'] if r[k] is not None]
            self.assertAlmostEqual(p['total'][k] or 0.0, sum(known), places=4, msg=k)
        self.assertEqual(p['total']['equity_usd'], 342.0)
        self.assertEqual(p['total']['paid_usd'], 1.5)
        self.assertEqual(p['total']['reinvested_usd'], 0.25)
        self.assertEqual(len(p['wallets']), 1)
        self.assertEqual(p['wallets'][0]['subtotal'], p['total'])
        mu = p['pools'][0]
        self.assertEqual((mu['token_a'], mu['fees_realised_a'], mu['fees_unrealised_a']), ('MU', 0.2, 0.02))
        sol = p['pools'][1]
        self.assertEqual((sol['token_a'], sol['fees_realised_a']), ('SOL', 0.01))
        self.assertEqual(sol['idle_usd'], 241.0 - 220.0 - 0.5)

    def test_one_pool_and_one_wallet(self):
        self.assertEqual([r['profile'] for r in stats.portfolio(profile='mu-usdc')['pools']], ['mu-usdc'])
        self.assertEqual(stats.portfolio(wallet_id='base-lp')['pools'], [])
        self.assertEqual(stats.portfolio(profile='djt-usdc')['dormant'], ['djt-usdc'])

    def test_all_shows_dormant_and_disabled_in_full(self):
        p = stats.portfolio(show_all=True)
        self.assertEqual([r['profile'] for r in p['pools']], ['djt-usdc', 'mu-usdc', 'other', 'sol-usdc'])
        self.assertEqual(p['total']['pools'], 4)

    def test_a_disabled_profile_holding_money_is_named(self):
        position('OT1', 'other')
        p = stats.portfolio()
        self.assertEqual(p['disabled_holding'], ['other'])
        self.assertNotIn('other', [r['profile'] for r in p['pools']])
        self.assertIn('DISABLED BUT HOLDING A POSITION: other', stats.render(p))

    def test_text_and_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats.main([])
        text = out.getvalue()
        self.assertIn('WALLET sol-lp (solana, LP wallet)', text)
        self.assertIn('MU/USDC  (mu-usdc, meteora-dlmm)', text)
        self.assertIn('TOTAL  2 pools', text)
        self.assertIn('dormant 1 (djt-usdc) · disabled 1 (other)', text)
        self.assertIn('0.2 MU', text)
        total = text[text.index('TOTAL'):]
        self.assertNotIn(' MU', total)                               # no token amount in the sum
        self.assertNotIn(' SOL', total)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats.main(['--json', '--pool', 'sol-usdc'])
        self.assertEqual([r['profile'] for r in json.loads(out.getvalue())['pools']], ['sol-usdc'])

    def test_no_active_pool(self):
        reset_ledger()
        p = stats.portfolio()
        self.assertEqual(p['pools'], [])
        self.assertEqual(p['dormant'], ['djt-usdc', 'mu-usdc', 'sol-usdc'])
        self.assertIn('no active pool', stats.render(p))


class Record(unittest.TestCase):
    """stats.record: one pool's flat figures from its book. Pure."""
    PROFILE = {'name': 'mu-usdc', 'wallet': 'sol-lp'}

    def book(self, **over):
        b = {'position_pair': 'MU/USDC', 'pair': 'CFG/USDC', 'position_dex': 'orca', 'dex': 'meteora-dlmm',
             'token_a': 'MU', 'token_b': 'USDC', 'equity_usd': 10.0, 'lp_usd': 8.0, 'wallet_usd': 1.5,
             'positions_opened': 3, 'harvests': 2, 'positions_open_now': 1, 'last_seen': 'x',
             'since_start': {'since': 's', 'profit_usd': 1.0, 'profit_pct': 2.0, 'vs_hold_start_assets_usd': 0.5,
                             'vs_hold_50_50_usd': 0.25}}
        b.update(over)
        return b

    def test_the_held_pool_names_the_record(self):
        r = stats.record(self.PROFILE, self.book(), {}, {})
        self.assertEqual((r['profile'], r['wallet_id'], r['pair'], r['dex'], r['idle_usd']),
                         ('mu-usdc', 'sol-lp', 'MU/USDC', 'orca', 1.5))
        r = stats.record(self.PROFILE, self.book(position_pair=None, position_dex=None), {}, {})
        self.assertEqual((r['pair'], r['dex']), ('CFG/USDC', 'meteora-dlmm'))     # no position: the config's

    def test_counts_are_never_none(self):
        r = stats.record(self.PROFILE, self.book(positions_opened=None, harvests=None, positions_open_now=None),
                         {}, {'deposits': None, 'withdrawals': None})
        self.assertEqual([r[k] for k in ('recentres', 'harvests', 'positions_open_now', 'deposits', 'withdrawals')],
                         [0, 0, 0, 0, 0])
        r = stats.record(self.PROFILE, self.book(), {}, {'deposits': 4, 'withdrawals': 5})
        self.assertEqual([r[k] for k in ('recentres', 'harvests', 'positions_open_now', 'deposits', 'withdrawals')],
                         [3, 2, 1, 4, 5])

    def test_since_start_and_payouts(self):
        r = stats.record(self.PROFILE, self.book(), {'paid_usd': 1.0, 'reinvested_usd': 2.0, 'gas_usd': 3.0},
                         {'deposits_usd': 7.0, 'withdrawals_usd': 8.0})
        self.assertEqual([r[k] for k in ('since', 'profit_usd', 'profit_pct', 'vs_hold_usd', 'vs_hold_50_50_usd')],
                         ['s', 1.0, 2.0, 0.5, 0.25])
        self.assertEqual([r[k] for k in ('paid_usd', 'reinvested_usd', 'gas_usd', 'deposits_usd', 'withdrawals_usd')],
                         [1.0, 2.0, 3.0, 7.0, 8.0])
        r = stats.record(self.PROFILE, self.book(since_start=None), {}, {})
        self.assertIsNone(r['profit_usd'])


class CombineExact(unittest.TestCase):
    """combine_books / combine_days / combine_since on figures worked by hand."""

    def book(self, **over):
        b = {k: None for k in db.BOOK_USD}
        b.update({k: 0 for k in db.BOOK_COUNTS}, token_a='SOL', token_b='USDC', in_range_pct=None,
                 tracked_days=None, daily=None, since_start=None, split=None, by_pool=None, dexes_held=None,
                 season=None, deployed_pct=None, last_seen=None)
        b.update({k: None for keys in db.BOOK_TOKENS.values() for k in keys})
        b.update(over)
        return b

    def test_apr_is_annualised_on_the_books_with_a_rate_and_an_equity(self):
        a = self.book(fees_per_day_usd=2.0, equity_usd=100.0, fees_per_day_24h_usd=1.0)
        b = self.book(fees_per_day_usd=1.0, equity_usd=None, fees_per_day_24h_usd=None)      # no equity: left out
        c = self.book(fees_per_day_usd=None, equity_usd=300.0, fees_per_day_24h_usd=3.0)     # no rate: left out of apr
        x = db.combine_books([a, b, c])
        self.assertEqual(x['apr_pct'], 730.0)                       # 2 / 100 * 365 * 100
        self.assertEqual(x['apr_24h_pct'], 365.0)                   # (1 + 3) / 400 * 365 * 100
        self.assertIsNone(x['apr_6h_pct'])
        self.assertIsNone(db.combine_books([self.book(), self.book(fees_per_day_usd=1.0, equity_usd=0.0)])['apr_pct'])
        small = db.combine_books([self.book(), self.book(fees_per_day_usd=0.001, equity_usd=0.5)])   # under $1
        self.assertEqual(small['apr_pct'], 73.0)

    def test_in_range_days_and_times(self):
        x = db.combine_books([self.book(in_range_pct=90.0, tracked_days=3.0, last_seen='2026-10-01T02:00'),
                              self.book(in_range_pct=50.0, tracked_days=1.0, last_seen='2026-10-01T03:00'),
                              self.book(in_range_pct=None, tracked_days=9.0, last_seen=None)])
        self.assertEqual(x['in_range_pct'], 80.0)
        self.assertEqual(x['tracked_days'], 9.0)
        self.assertEqual(x['last_seen'], '2026-10-01T03:00')
        self.assertIsNone(db.combine_books([self.book(in_range_pct=10.0, tracked_days=0.0), self.book()])['in_range_pct'])
        self.assertEqual(db.combine_books([self.book(in_range_pct=10.0, tracked_days=0.5),          # under a day
                                           self.book()])['in_range_pct'], 10.0)

    def test_lists_and_the_split(self):
        line = lambda day: {'day': day, 'complete': True, 'recentres': 1, 'idle_redeploys': 0,
                            **{k: 1.0 for k in db.DAY_USD}, 'price_open': 1.0, 'price_close': 1.0,
                            'hold_50_50_usd': 1.0, 'vs_hold_usd': 0.0, 'fees_per_recentre_usd': 1.0}
        split = {'paid_usd': 0.12345, 'paid_today_usd': 0.0, 'reinvested_usd': 1.0, 'gas_usd': 0.5, 'payouts': 2}
        x = db.combine_books([
            self.book(daily=[line('2026-10-02'), line('2026-10-01')], by_pool=[{'p': 1}], dexes_held=['orca'],
                      split=split),
            self.book(daily=None, by_pool=None, dexes_held=None, split=None),
            self.book(daily=[line('2026-10-02')], by_pool=[{'p': 2}], dexes_held=['byreal', 'orca'],
                      split=dict(split, paid_usd=0.1, payouts=1))])
        self.assertEqual([d['day'] for d in x['daily']], ['2026-10-02', '2026-10-01'])        # newest first
        self.assertEqual(x['daily'][0]['recentres'], 2)
        self.assertEqual(x['by_pool'], [{'p': 1}, {'p': 2}])
        self.assertEqual(x['dexes_held'], ['byreal', 'orca'])
        self.assertEqual(x['split'], {'paid_usd': 0.2235, 'paid_today_usd': 0.0, 'reinvested_usd': 2.0,
                                      'gas_usd': 1.0, 'payouts': 3})
        self.assertIsInstance(x['split']['payouts'], int)
        one = db.combine_books([self.book(split=split), self.book(split=None)])        # one book pays out
        self.assertEqual(one['split'], dict(split, paid_usd=0.1235))

    def test_the_share_from_the_summed_marks(self):
        x = db.combine_books([self.book(lp_usd=None, equity_usd=50.0, deployed_pct=0.0),
                              self.book(lp_usd=None, equity_usd=50.0, deployed_pct=0.0)])
        self.assertEqual(x['deployed_pct'], 0.0)

    def test_token_amounts_keep_six_digits(self):
        x = db.combine_books([self.book(fees_total_a=0.1234561), self.book(fees_total_a=0.0000001)])
        self.assertEqual(x['fees_total_a'], 0.123456)
        self.assertEqual(db._sum_known([0.123456]), 0.1235)
        self.assertIsNone(db._sum_known([None, None]))

    def test_a_day_line_of_several_pools(self):
        mk = lambda complete: {'day': 'd', 'complete': complete, 'recentres': 1, 'idle_redeploys': 1,
                               **{k: 1.0 for k in db.DAY_USD}, 'hold_50_50_usd': 1.0, 'vs_hold_usd': 0.0}
        self.assertFalse(db.combine_days([mk(True), mk(False)])['complete'])
        self.assertTrue(db.combine_days([mk(True), mk(True)])['complete'])
        self.assertEqual(db.combine_days([mk(True), mk(True)])['idle_redeploys'], 2)
        with self.assertRaises(ValueError):
            db.combine_days([mk(True), dict(mk(True), day='e')])

    def test_since_start_of_several_pools(self):
        s = lambda since, days, start, value: dict({k: 0.0 for k in db.SINCE_USD}, since=since, days=days,
                                                   start_usd=start, value_usd=value)
        one = s('a', 1.0, 100.0, 110.0)
        self.assertIs(db.combine_since([one]), one)
        self.assertIsNone(db.combine_since([]))
        c = db.combine_since([one, s('b', 3.0, 300.0, 290.0)])
        self.assertEqual((c['since'], c['days'], c['profit_pct']), ('a', 3.0, 0.0))
        c = db.combine_since([one, s('b', 3.0, 300.0, 330.0)])
        self.assertEqual(c['profit_pct'], 10.0)
        self.assertIsNone(db.combine_since([s('a', 1, 0.0, 1.0), s('b', 1, 0.0, 1.0)])['profit_pct'])


class OtherWallet(unittest.TestCase):
    """A second wallet (base-lp, chain base) with its own residual owner."""

    def setUp(self):
        reset_ledger()
        multi_wallet()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into wallets (id, chain, address, secret_env, label) values ('base-lp', 'base', "
                        "'0x2b35948898e1b4897E7FC5a70e39b213dcfd0142', 'LPBOT_EVM_KEY_PATH', null) on conflict (id) do "
                        "update set chain = excluded.chain, label = excluded.label")
            cur.execute("insert into config (name, pool, pair_label, token_a, token_b, capital_usd, max_usd, dex, "
                        "wallet_id, enabled, residual_owner, deposit_mint) values ('base-weth-usdc', "
                        "'0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59', 'WETH/USDC', 'WETH', 'USDC', 100, 140, "
                        "'aerodrome-slipstream', 'base-lp', true, true, '0x4200000000000000000000000000000000000006')")
            cur.execute("select key, value from audit_state where key in "
                        "('uncounted_usd', 'sol-lp|uncounted_usd', 'base-lp|uncounted_usd')")
            self.audit = {r['key']: r['value'] for r in cur.fetchall()}
            cur.execute("delete from audit_state where key in "
                        "('uncounted_usd', 'sol-lp|uncounted_usd', 'base-lp|uncounted_usd')")

    def tearDown(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from audit_state where key in "
                        "('uncounted_usd', 'sol-lp|uncounted_usd', 'base-lp|uncounted_usd')")
            for k, v in self.audit.items():
                cur.execute('insert into audit_state (key, value) values (%s, %s)', (k, v))
        reset_ledger()
        drop_extra()
        ensure_profile()

    def audit_value(self, key, value):
        with db.cursor(commit=True) as cur:
            cur.execute('insert into audit_state (key, value, ts) values (%s, %s, now()) on conflict (key) do '
                        'update set value = excluded.value', (key, value))

    def test_each_profile_in_its_own_wallet(self):
        with context():
            self.assertEqual(db.book_scope(wallet_id='base-lp'), ['base-weth-usdc'])
            self.assertEqual(db.book_scope('base-weth-usdc', 'base-lp'), ['base-weth-usdc'])
            self.assertEqual(db.book_scope('mu-usdc', 'base-lp'), [])      # mu-usdc is on sol-lp
            self.assertEqual(db.book_scope('base-weth-usdc', 'sol-lp'), [])
            with db.cursor(commit=True) as cur:
                cur.execute("update config set enabled = false where name = 'base-weth-usdc'")
            # a disabled profile still belongs to its wallet: its own book is readable
            self.assertEqual(db.book_scope('base-weth-usdc', 'base-lp'), ['base-weth-usdc'])
            self.assertEqual(db.book_scope(wallet_id='base-lp'), [])

    def test_the_portfolio_names_each_wallet_and_its_chain(self):
        position('S1', 'sol-usdc')
        snap('S1', 1, 0.0, 100.0)
        position('B1', 'base-weth-usdc')
        snap('B1', 1, 0.0, 50.0)
        p = stats.portfolio()
        self.assertEqual([(w['wallet_id'], w['chain'], w['label'], w['pools']) for w in p['wallets']],
                         [('base-lp', 'base', None, ['base-weth-usdc']), ('sol-lp', 'solana', 'LP wallet', ['sol-usdc'])])
        self.assertEqual(p['total']['equity_usd'], 150.0)
        self.assertEqual(p['wallets'][0]['subtotal']['equity_usd'], 50.0)
        self.assertIn('subtotal sol-lp', stats.render(p))

    def test_a_wallet_not_yet_registered_is_on_solana(self):
        drop_extra()
        ensure_profile()                                             # sol-usdc before the deploy: no wallet row
        with db.cursor(commit=True) as cur:
            cur.execute("delete from wallets where id = 'sol-lp'")
        position('S1', 'sol-usdc')
        snap('S1', 1, 0.0, 100.0)
        with context():
            w = stats.portfolio()['wallets']
        self.assertEqual([(x['wallet_id'], x['chain'], x['label']) for x in w], [('sol-lp', 'solana', None)])

    def test_the_wallets_dust_belongs_to_its_residual_owner(self):
        self.assertEqual(db._uncounted_usd('base-weth-usdc'), 0.0)                      # nothing kept yet
        self.audit_value('uncounted_usd', '0.3')                                          # a pre-020 key
        self.assertEqual(db._uncounted_usd('sol-usdc'), 0.3)                              # the legacy wallet reads it
        self.assertEqual(db._uncounted_usd('base-weth-usdc'), 0.0)                        # another wallet does not
        self.assertEqual(db._uncounted_usd('nobody'), 0.3)                                # no config row: pre-020
        self.audit_value('sol-lp|uncounted_usd', '0.5')
        self.assertEqual(db._uncounted_usd('sol-usdc'), 0.5)                              # the wallet's own key first
        self.audit_value('base-lp|uncounted_usd', '')
        self.assertEqual(db._uncounted_usd('base-weth-usdc'), 0.0)
        self.audit_value('base-lp|uncounted_usd', '1.25')
        self.assertEqual(db._uncounted_usd('base-weth-usdc'), 1.25)
        got = db._uncounted_usd('mu-usdc')                                                # not the residual owner
        self.assertIsInstance(got, float)
        self.assertEqual(got, 0.0)


if __name__ == '__main__':
    unittest.main()
