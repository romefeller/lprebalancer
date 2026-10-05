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
EXTRA = ('mu-usdc', 'djt-usdc', 'base-weth-usdc', 'sol-swing')


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
            if k in stats.HOLD_KEYS and any(r['profit_usd'] is not None and r[k] is None for r in recs):
                self.assertIsNone(t[k], k)                         # a pool with a baseline and no benchmark
            elif not known:
                self.assertIsNone(t[k], k)
            else:
                self.assertAlmostEqual(t[k], sum(known), delta=1e-3 + 1e-9 * sum(abs(x) for x in known), msg=k)
        for k in stats.COUNT_KEYS:
            self.assertEqual(t[k], sum(r[k] for r in recs))

    def test_a_hold_benchmark_sums_only_over_every_pool_with_a_baseline(self):
        base = lambda profit, vs: dict({k: None for k in stats.USD_KEYS}, profit_usd=profit, vs_hold_usd=vs,
                                       vs_hold_50_50_usd=vs)
        t = stats.total([base(1.0, 2.0), base(None, None), base(3.0, -0.5)])   # no baseline: left out
        self.assertEqual((t['vs_hold_usd'], t['vs_hold_50_50_usd'], t['profit_usd']), (1.5, 1.5, 4.0))
        t = stats.total([base(1.0, 2.0), base(3.0, None)])                     # a swing: no benchmark
        self.assertEqual((t['vs_hold_usd'], t['vs_hold_50_50_usd'], t['profit_usd']), (None, None, 4.0))
        self.assertIsNone(stats.total([base(None, None)])['vs_hold_usd'])
        self.assertEqual(stats.total([base(None, 2.0)])['vs_hold_usd'], 2.0)    # a figure with no profit adds

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
        tag = db.wallet_tag(db.wallet_row('sol-lp')['address'])
        self.assertEqual(len(tag), 10)
        self.assertIn(f'WALLET sol-lp {tag} (solana, LP wallet)', text)
        self.assertNotIn('subtotal', text)                           # one wallet: its subtotal is the TOTAL
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
        self.assertTrue(out.getvalue().startswith('{\n "ts": '), out.getvalue()[:20])   # indent=1

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
        self.assertIn('subtotal sol-lp ', stats.render(p))
        self.assertEqual([w['wallet_tag'] for w in p['wallets']],
                         ['0x2b359488', db.wallet_tag(db.wallet_row('sol-lp')['address'])])
        self.assertIn('WALLET base-lp 0x2b359488 (base)', stats.render(p))
        self.assertEqual([r['wallet_tag'] for r in p['pools']], [w['wallet_tag'] for w in p['wallets']])
        # the EVM tag in any case, the id, the whole address: one wallet
        for key in ('0x2B359488', 'base-lp', '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142'):
            self.assertEqual([r['profile'] for r in stats.portfolio(key)['pools']], ['base-weth-usdc'], key)

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
        self.assertEqual((w[0]['wallet_tag'], w[0]['address']), (None, None))     # no address: the id alone
        self.assertIn('WALLET sol-lp (solana)', stats.render(stats.portfolio()))
        # its only profile disabled, no wallets row: --wallet still knows the id from the profile
        with db.cursor(commit=True) as cur:
            cur.execute("update config set active = false, enabled = false")
        out = io.StringIO()
        with context(), contextlib.redirect_stdout(out):
            stats.main(['--wallet', 'sol-lp', '--all'])
        self.assertIn('WALLET sol-lp (solana)', out.getvalue())

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



# --- a wallet's name: its id and its address's first 10 characters (2026-10-02) ------

SOL_ADDR = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'
SWING_ADDR = 'FogqBWLC4y94csrniURTbGrx7ff7jFa4e2qp1GsgyAmM'
EVM_ADDR = '0x2b35948898e1b4897E7FC5a70e39b213dcfd0142'
ROWS = [{'id': 'sol-lp', 'address': SOL_ADDR}, {'id': 'sol-lp2', 'address': SWING_ADDR},
        {'id': 'base-lp', 'address': EVM_ADDR}]
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


class WalletNames(unittest.TestCase):
    """db.wallet_tag, db.resolve_wallet, db.mixed_sides, stats.wallet_name. Pure."""

    def test_the_tag_is_the_first_ten_characters(self):
        self.assertEqual([db.wallet_tag(r['address']) for r in ROWS], ['83HxMUUC7c', 'FogqBWLC4y', '0x2b359488'])
        self.assertEqual(db.WALLET_TAG_LEN, 10)
        self.assertEqual(db.wallet_tag('  abc  '), 'abc')              # a short address is all of it
        for none in (None, '', '   '):
            self.assertIsNone(db.wallet_tag(none))

    @settings(max_examples=300, deadline=None)
    @given(st.text(B58, min_size=0, max_size=50))
    def test_the_tag_is_a_prefix_and_names_its_wallet(self, address):
        tag = db.wallet_tag(address)
        if not address:
            self.assertIsNone(tag)
            return
        self.assertEqual(tag, address[:10])
        self.assertTrue(address.startswith(tag))
        if len(address) >= 10:
            rows = [{'id': 'w', 'address': address}, {'id': 'v', 'address': 'z' + address}]
            self.assertEqual(db.resolve_wallet(tag, rows), 'w')
            self.assertEqual(db.resolve_wallet(address, rows), 'w')

    def test_an_id_a_tag_or_the_address_names_the_wallet(self):
        for key, want in (('sol-lp', 'sol-lp'), ('sol-lp2', 'sol-lp2'), ('83HxMUUC7c', 'sol-lp'),
                          ('FogqBWLC4y', 'sol-lp2'), (SWING_ADDR, 'sol-lp2'), ('FogqBWLC4y94', 'sol-lp2'),
                          ('0x2b359488', 'base-lp'), ('0X2B359488', 'base-lp'), (EVM_ADDR.upper(), 'base-lp')):
            self.assertEqual(db.resolve_wallet(key, ROWS), want, key)

    def test_a_key_that_names_no_wallet_is_returned_as_it_is(self):
        self.assertIsNone(db.resolve_wallet(None, ROWS))
        for key in ('Fogq', 'FogqBWLC4', 'fogqbwlc4y', 'FogqBWLC4z', 'nobody-here', ''):  # short, base58 case, other
            self.assertEqual(db.resolve_wallet(key, ROWS), key, key)
        self.assertEqual(db.resolve_wallet('FogqBWLC4y', []), 'FogqBWLC4y')
        self.assertEqual(db.resolve_wallet('FogqBWLC4y', [{'id': 'x', 'address': None}]), 'FogqBWLC4y')

    def test_an_id_wins_over_an_address(self):
        rows = ROWS + [{'id': 'FogqBWLC4y', 'address': 'zzzzzzzzzzzzzzzz'}]
        self.assertEqual(db.resolve_wallet('FogqBWLC4y', rows), 'FogqBWLC4y')

    def test_a_prefix_of_two_wallets_is_refused(self):
        rows = [{'id': 'a', 'address': 'FogqBWLC4yAAAA'}, {'id': 'b', 'address': 'FogqBWLC4yBBBB'}]
        with self.assertRaises(ValueError) as e:
            db.resolve_wallet('FogqBWLC4y', rows)
        self.assertIn('a, b', str(e.exception))
        self.assertEqual(db.resolve_wallet('FogqBWLC4yA', rows), 'a')
        evm = [{'id': 'a', 'address': '0xABCDEF0123aa'}, {'id': 'b', 'address': '0xabcdef0123bb'}]
        with self.assertRaises(ValueError):
            db.resolve_wallet('0xabcdef01', evm)                    # one EVM address in two cases
        self.assertEqual(db.resolve_wallet('0xABCDEF0123B', evm), 'b')
        same = [{'id': 'a', 'address': 'FogqBWLC4yAAAA'}, {'id': 'a', 'address': 'FogqBWLC4yAAAA'}]
        self.assertEqual(db.resolve_wallet('FogqBWLC4y', same), 'a')  # one id twice is one wallet

    def test_only_an_evm_address_ignores_case(self):
        self.assertTrue(db._address_starts('0xAbC', '0XaB'))
        self.assertTrue(db._address_starts('0XAbC', '0xab'))
        self.assertFalse(db._address_starts('FogqB', 'fogq'))
        self.assertTrue(db._address_starts('FogqB', 'Fogq'))
        self.assertFalse(db._address_starts(None, 'x'))
        self.assertFalse(db._address_starts(None, 'None'))            # no address is not the text 'None'
        self.assertFalse(db._address_starts('ab', 'abc'))

    def test_the_wallet_name(self):
        self.assertEqual(stats.wallet_name('sol-lp2', 'FogqBWLC4y'), 'sol-lp2 FogqBWLC4y')
        self.assertEqual(stats.wallet_name('sol-lp', None), 'sol-lp')
        self.assertEqual(stats.wallet_name('sol-lp', ''), 'sol-lp')

    def test_mixed_sides(self):
        self.assertEqual(db.mixed_sides(['SOL/USDC', 'DJT/USDC']), {'a': True, 'b': False})
        self.assertEqual(db.mixed_sides(['SOL/USDC', 'SOL/USDC']), {'a': False, 'b': False})
        self.assertEqual(db.mixed_sides(['SOL/USDC', 'SOL/USDT']), {'a': False, 'b': True})
        self.assertEqual(db.mixed_sides(['SOL/USDC', None, '', 'nopair']), {'a': False, 'b': False})
        self.assertEqual(db.mixed_sides([]), {'a': False, 'b': False})
        self.assertEqual(db.mixed_sides(['SOL/USDC', ' SOL / USDC ']), {'a': False, 'b': False})

    @settings(max_examples=300, deadline=None)
    @given(st.lists(st.tuples(st.sampled_from(['SOL', 'DJT', 'MU']), st.sampled_from(['USDC', 'USDT'])), max_size=6))
    def test_mixed_is_more_than_one_token_on_the_side(self, pairs):
        got = db.mixed_sides([f'{a}/{b}' for a, b in pairs])
        self.assertEqual(got, {'a': len({a for a, _ in pairs}) > 1, 'b': len({b for _, b in pairs}) > 1})

    def test_the_held_pools_lines(self):
        one = {'by_pool': [{'dex': 'orca', 'pair_label': 'DJT/USDC', 'pool': 'P' * 44}]}
        self.assertEqual(stats._held_lines(one), [])                  # one pool: the record says it all
        self.assertEqual(stats._held_lines({}), [])
        two = {'by_pool': [{'dex': 'raydium-clmm', 'pair_label': 'SOL/USDC', 'pool': 'RAYPOOL123456', 'open_now': 1,
                            'positions': 2, 'days': 1.5, 'fees_a': 0.011, 'fees_b': 1.2, 'fees_usd': 2.3,
                            'fees_per_day_usd': 1.5333, 'in_range_pct': 90.0},
                           {'dex': 'orca', 'pair_label': 'DJT/USDC', 'pool': 'ORCAPOOL9876', 'open_now': 0,
                            'positions': 1, 'days': 0.5, 'fees_a': 2.0, 'fees_b': 0.3, 'fees_usd': 0.5,
                            'fees_per_day_usd': None, 'in_range_pct': None}]}
        self.assertEqual(stats._held_lines(two), [
            '    > pool  raydium-clmm SOL/USDC RAYPOOL123 · 2 pos over 1.50d · fees 0.011 SOL 1.2 USDC $2.3000'
            ' · $1.5333/d · in range 90%',
            '      pool  orca DJT/USDC ORCAPOOL98 · 1 pos over 0.50d · fees 2 DJT 0.3 USDC $0.5000 · -/d · in range -'])
        bare = stats._held_lines({'by_pool': [{}, {'pair_label': 'X'}]})
        self.assertEqual(bare[0], '      pool  - - - · 0 pos over 0.00d · fees - A - B - · -/d · in range -')
        self.assertIn('fees - X - B -', bare[1])

    def test_a_record_carries_its_wallet_tag_and_its_pools(self):
        pools = [{'dex': 'orca', 'pool': 'P1', 'pair_label': 'DJT/USDC', 'fees_a': 2.0, 'extra': 1},
                 {'dex': 'raydium-clmm', 'pool': 'P2', 'pair_label': 'SOL/USDC'}]
        r = stats.record({'name': 'sol-swing', 'wallet': 'sol-lp2'}, {}, {}, {}, pools, 'FogqBWLC4y')
        self.assertEqual((r['wallet_id'], r['wallet_tag']), ('sol-lp2', 'FogqBWLC4y'))
        self.assertEqual([p['pool'] for p in r['by_pool']], ['P1', 'P2'])
        self.assertEqual(set(r['by_pool'][0]), set(stats.POOL_KEYS))     # only the shown figures
        self.assertEqual(r['by_pool'][0]['fees_a'], 2.0)
        self.assertIsNone(r['by_pool'][1]['fees_a'])
        r = stats.record({'name': 'x', 'wallet': 'w'}, {}, {}, {})
        self.assertEqual((r['wallet_tag'], r['by_pool']), (None, []))

    def test_since_start_benchmarks_add_only_when_every_book_has_one(self):
        s = lambda hold: dict({k: 1.0 for k in db.SINCE_USD}, since='a', days=1.0,
                              **({k: None for k in db.SINCE_HOLD} if hold is None else {}))
        c = db.combine_since([s(1.0), s(None)])
        self.assertTrue(all(c[k] is None for k in db.SINCE_HOLD), c)
        self.assertEqual((c['start_usd'], c['profit_usd']), (2.0, 2.0))   # the dollars still add
        c = db.combine_since([s(1.0), s(1.0)])
        self.assertTrue(all(c[k] == 2.0 for k in db.SINCE_HOLD), c)


SWING_POOL_SOL = 'RAYsol1111111111111111111111111111111111111'
SWING_POOL_DJT = 'ORCAdjt111111111111111111111111111111111111'


class TwoSolanaWallets(unittest.TestCase):
    """sol-lp (sol-usdc, mu-usdc, djt-usdc) and sol-lp2, a second Solana
    wallet whose one profile sol-swing held DJT/USDC on Orca and now SOL/USDC
    on Raydium: the same Raydium pool sol-usdc holds."""

    def setUp(self):
        reset_ledger()
        multi_wallet()
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name = 'sol-swing'")
            cur.execute("delete from wallets where id = 'sol-lp2'")
            cur.execute("insert into wallets (id, chain, address, secret_env, label) values ('sol-lp2', 'solana', "
                        "%s, 'LPBOT_SOL_LP2_KEY', 'swing wallet')", (SWING_ADDR,))
            cur.execute("insert into config (name, pool, pair_label, token_a, token_b, capital_usd, max_usd, dex, "
                        "wallet_id, enabled, residual_owner, deposit_mint, mints) values ('sol-swing', %s, "
                        "'SOL/USDC', 'SOL', 'USDC', 100, 200, 'raydium-clmm', 'sol-lp2', true, true, %s, %s)",
                        (SWING_POOL_SOL, SOL_MINT, [SOL_MINT, USDC_MINT]))
        self.sol_tag = db.wallet_tag(db.wallet_row('sol-lp')['address'])
        # sol-usdc on sol-lp: open on the Raydium pool
        pos('S1', 'sol-usdc', SWING_POOL_SOL, 'SOL/USDC', 'raydium-clmm', hours=30)
        snap('S1', 2, 0.4, 240.0, lp=220.0, accrued=(0.002, 0.2))
        harvest('S1', 3, 0.01, 1.0, 2.0)
        # mu-usdc on sol-lp
        pos('MU1', 'mu-usdc', MU_POOL, 'MU/USDC', 'meteora-dlmm', hours=30)
        snap('MU1', 2, 0.1, 100.0, lp=95.0, accrued=(0.01, 0.05))
        harvest('MU1', 3, 0.2, 0.4, 1.0)
        # sol-swing on sol-lp2: DJT/USDC first (closed), then SOL/USDC (open)
        pos('SW1', 'sol-swing', SWING_POOL_DJT, 'DJT/USDC', 'orca', hours=30, closed_hours=20)
        snap('SW1', 25, 0.2, 150.0, lp=140.0, accrued=(1.0, 0.1))
        harvest('SW1', 21, 2.0, 0.3, 0.5)
        pos('SW2', 'sol-swing', SWING_POOL_SOL, 'SOL/USDC', 'raydium-clmm', hours=19)
        snap('SW2', 3, 0.1, 151.0, lp=140.0, accrued=(0.0005, 0.05))
        snap('SW2', 1, 0.3, 152.0, lp=140.0, accrued=(0.001, 0.2))
        harvest('SW2', 2, 0.01, 1.0, 2.0)
        payout('sol-usdc', 1.0)
        payout('sol-swing', 0.4)
        flow('baseline', 230.0, 'sol-usdc', 'sol-lp', sol=1.0, usdc=110.0)
        flow('baseline', 145.0, 'sol-swing', 'sol-lp2', amounts={DJT_MINT: 10.0, USDC_MINT: 70.0}, hours_ago=29)
        flow('deposit', 9.0, None, 'sol-lp2')                     # no profile, on sol-lp2: not sol-usdc's
        flow('deposit', 7.0, None, None)                          # no profile, no wallet: pre-020, sol-usdc's

    def tearDown(self):
        reset_ledger()
        drop_extra()
        with db.cursor(commit=True) as cur:
            cur.execute("delete from wallets where id = 'sol-lp2'")
        ensure_profile()

    def test_each_wallet_by_id_and_tag_and_the_sums(self):
        with context():
            p = stats.portfolio()
        self.assertEqual([(w['wallet_id'], w['wallet_tag'], w['chain'], w['pools']) for w in p['wallets']],
                         [('sol-lp', self.sol_tag, 'solana', ['mu-usdc', 'sol-usdc']),
                          ('sol-lp2', 'FogqBWLC4y', 'solana', ['sol-swing'])])
        self.assertEqual(p['wallets'][1]['address'], SWING_ADDR)
        by = {r['profile']: r for r in p['pools']}
        self.assertEqual({k: r['wallet_tag'] for k, r in by.items()},
                         {'mu-usdc': self.sol_tag, 'sol-usdc': self.sol_tag, 'sol-swing': 'FogqBWLC4y'})
        sub = {w['wallet_id']: w['subtotal'] for w in p['wallets']}
        self.assertEqual((sub['sol-lp']['equity_usd'], sub['sol-lp2']['equity_usd'], p['total']['equity_usd']),
                         (340.0, 152.0, 492.0))
        for k in stats.USD_KEYS:
            for wid, names in (('sol-lp', ('mu-usdc', 'sol-usdc')), ('sol-lp2', ('sol-swing',))):
                known = [by[n][k] for n in names if by[n][k] is not None]
                want = round(sum(known), 4) if known else None
                if k in stats.HOLD_KEYS and any(by[n]['profit_usd'] is not None and by[n][k] is None for n in names):
                    want = None
                self.assertEqual(sub[wid][k], want, (wid, k))
        for k in stats.USD_KEYS:
            if k in stats.HOLD_KEYS:
                continue
            known = [s[k] for s in sub.values() if s[k] is not None]
            self.assertAlmostEqual(p['total'][k] or 0.0, sum(known), places=4, msg=k)
        for k in stats.COUNT_KEYS:
            self.assertEqual(p['total'][k], sum(s[k] for s in sub.values()), k)
        self.assertEqual(p['total']['pools'], 3)
        self.assertEqual((p['total']['harvests'], p['total']['recentres']), (4, 4))
        self.assertEqual((sub['sol-lp']['paid_usd'], sub['sol-lp2']['paid_usd'], p['total']['paid_usd']),
                         (1.0, 0.4, 1.4))
        # the swing has a baseline and no benchmark (two base tokens): no hold benchmark in its sums
        self.assertIsNotNone(by['sol-swing']['profit_usd'])
        self.assertIsNone(by['sol-swing']['vs_hold_usd'])
        self.assertIsNone(sub['sol-lp2']['vs_hold_usd'])
        self.assertIsNone(p['total']['vs_hold_usd'])
        self.assertIsNotNone(by['sol-usdc']['vs_hold_usd'])
        self.assertEqual(sub['sol-lp']['vs_hold_usd'], by['sol-usdc']['vs_hold_usd'])   # mu-usdc: no baseline

    def test_the_swing_is_reported_per_pool_and_in_its_subtotal(self):
        with context():
            p = stats.portfolio()
        sw = next(r for r in p['pools'] if r['profile'] == 'sol-swing')
        self.assertEqual((sw['pair'], sw['dex'], sw['token_a'], sw['token_b']),
                         ('SOL/USDC', 'raydium-clmm', 'SOL', 'USDC'))
        pools = {x['pool']: x for x in sw['by_pool']}
        self.assertEqual(set(pools), {SWING_POOL_SOL, SWING_POOL_DJT})          # keyed on the position's pool
        djt, sol = pools[SWING_POOL_DJT], pools[SWING_POOL_SOL]
        self.assertEqual((djt['pair_label'], djt['dex'], djt['positions'], djt['open_now']), ('DJT/USDC', 'orca', 1, 0))
        self.assertEqual((djt['fees_a'], djt['fees_b'], djt['fees_usd'], djt['unrealised_usd']), (2.0, 0.3, 0.5, 0.0))
        self.assertEqual((sol['pair_label'], sol['open_now']), ('SOL/USDC', 1))
        self.assertEqual((sol['fees_a'], sol['fees_b'], sol['fees_usd']), (0.011, 1.2, 2.3))
        self.assertEqual((sol['realised_usd'], sol['unrealised_usd']), (2.0, 0.3))
        # the profile's book is the sum of its pools, in dollars; SOL and DJT do not add
        self.assertAlmostEqual(sw['fees_total_usd'], djt['fees_usd'] + sol['fees_usd'], places=4)
        self.assertEqual((sw['fees_realised_usd'], sw['fees_unrealised_usd']), (2.5, 0.3))
        self.assertEqual([sw[k] for k in ('fees_realised_a', 'fees_unrealised_a', 'fees_total_a')], [None] * 3)
        self.assertEqual((sw['fees_realised_b'], sw['fees_unrealised_b'], sw['fees_total_b']), (1.3, 0.2, 1.5))
        self.assertEqual((sw['recentres'], sw['harvests'], sw['positions_open_now']), (2, 2, 1))
        self.assertEqual(p['wallets'][1]['subtotal']['fees_total_usd'], sw['fees_total_usd'])
        # sol-usdc on the same Raydium pool is its own row in its own wallet
        su = next(r for r in p['pools'] if r['profile'] == 'sol-usdc')
        self.assertEqual([(x['pool'], x['fees_usd']) for x in su['by_pool']], [(SWING_POOL_SOL, 2.4)])
        self.assertEqual((su['fees_realised_a'], su['fees_total_a']), (0.01, 0.012))   # one token: amounts stand
        with context():
            rows = db.by_pool()
        both = sorted((r['profile'], r['positions'], r['fees_usd']) for r in rows if r['pool'] == SWING_POOL_SOL)
        self.assertEqual(both, [('sol-swing', 1, 2.3), ('sol-usdc', 1, 2.4)])            # never merged
        with context():
            book = db.stats()                                                   # every profile, summed
        self.assertEqual(sorted((x['profile'], x['pool'], x['fees_usd']) for x in book['by_pool']),
                         [('mu-usdc', MU_POOL, 1.1), ('sol-swing', SWING_POOL_DJT, 0.5),
                          ('sol-swing', SWING_POOL_SOL, 2.3), ('sol-usdc', SWING_POOL_SOL, 2.4)])
        self.assertIsNone(book['fees_total_a'])                                 # SOL, MU and DJT
        self.assertEqual(book['fees_total_b'], round(1.5 + 0.45 + 1.2, 6))      # USDC everywhere

    def test_the_wallet_filter_takes_an_id_or_a_tag(self):
        for key in ('sol-lp2', 'FogqBWLC4y', SWING_ADDR):
            p = stats.portfolio(key)
            self.assertEqual([r['profile'] for r in p['pools']], ['sol-swing'], key)
            self.assertEqual(p['total']['equity_usd'], 152.0)
        for key in ('sol-lp', self.sol_tag):
            self.assertEqual([r['profile'] for r in stats.portfolio(key)['pools']], ['mu-usdc', 'sol-usdc'], key)
        self.assertEqual(stats.portfolio('Fogq')['pools'], [])               # too short: names no wallet
        self.assertEqual(db.stats(wallet_id='sol-lp2')['equity_usd'], 152.0)
        self.assertEqual(db.book_scope(wallet_id='sol-lp2'), ['sol-swing'])
        self.assertEqual(db.book_scope('sol-swing', 'sol-lp'), [])

    def test_the_text_names_each_wallet_and_each_pool_of_the_swing(self):
        out = io.StringIO()
        with context(), contextlib.redirect_stdout(out):
            stats.main([])
        text = out.getvalue()
        self.assertIn(f'WALLET sol-lp {self.sol_tag} (solana, LP wallet)', text)
        self.assertIn('WALLET sol-lp2 FogqBWLC4y (solana, swing wallet)', text)
        self.assertIn(f'  subtotal sol-lp {self.sol_tag}  2 pools', text)
        self.assertIn('  subtotal sol-lp2 FogqBWLC4y  1 pool', text)
        self.assertIn('TOTAL  3 pools', text)
        self.assertIn('  SOL/USDC  (sol-swing, raydium-clmm)', text)
        self.assertIn(f'    > pool  raydium-clmm SOL/USDC {SWING_POOL_SOL[:10]} · 1 pos over', text)
        self.assertIn(f'      pool  orca DJT/USDC {SWING_POOL_DJT[:10]} · 1 pos over', text)
        self.assertIn('fees 2 DJT 0.3 USDC $0.5000', text)
        self.assertIn('realised 2 DJT 0.01 SOL 1.3 USDC $2.5000', text)         # each A token, no SOL+DJT sum
        swing = text[text.index('WALLET sol-lp2'):text.index('TOTAL')]
        self.assertNotIn('sol-usdc', swing)
        self.assertEqual(text.count(' pool  '), 2)                            # one-pool profiles: no pool lines
        out = io.StringIO()
        with context(), contextlib.redirect_stdout(out):
            stats.main(['--json', '--wallet', 'FogqBWLC4y'])
        j = json.loads(out.getvalue())
        self.assertEqual([(w['wallet_id'], w['wallet_tag']) for w in j['wallets']], [('sol-lp2', 'FogqBWLC4y')])

    def test_a_wallet_flag_that_names_no_wallet_or_two_stops(self):
        err = io.StringIO()
        for bad in (['--wallet', 'Fogq'], ['--wallet', 'ZZZZZZZZZZZZ'], ['--wallet', 'no-such-wallet']):
            with context(), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as e:
                stats.main(bad)
            self.assertEqual(e.exception.code, 2, bad)
        self.assertIn("no wallet 'Fogq'", err.getvalue())
        with db.cursor(commit=True) as cur:
            cur.execute("insert into wallets (id, chain, address, secret_env) values ('sol-lp3', 'solana', "
                        "'FogqBWLC4yZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ', 'LPBOT_SOL_LP3_KEY')")
        try:
            err = io.StringIO()
            with context(), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                stats.main(['--wallet', 'FogqBWLC4y'])
            self.assertIn('matches several wallets: sol-lp2, sol-lp3', err.getvalue())
        finally:
            with db.cursor(commit=True) as cur:
                cur.execute("delete from wallets where id = 'sol-lp3'")
        out = io.StringIO()
        with context(), contextlib.redirect_stdout(out):
            stats.main(['--wallet', 'sol-lp'])                                 # a profile's wallet, by id
        self.assertIn('WALLET sol-lp ', out.getvalue())

    def test_db_cli_takes_the_tag(self):
        import os
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        got = subprocess.run([sys.executable, os.path.join(here, '..', 'db.py'), 'json', '--wallet', 'FogqBWLC4y'],
                             capture_output=True, text=True, timeout=120, env=dict(os.environ))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(json.loads(got.stdout)['equity_usd'], 152.0)

    def test_a_flow_with_no_profile_is_its_wallets_not_the_legacy_book(self):
        self.assertEqual(db.flow_totals('sol-usdc')['deposits_usd'], 7.0)          # not 16: the 9 is sol-lp2's
        self.assertEqual(db.flow_totals('sol-swing')['deposits_usd'], 0.0)        # no profile: no book's
        with db.cursor(commit=True) as cur:
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, wallet_id, "
                        "profile) values (%s, 'deposit', 0, 0, 5, 10, 'f-legacy-wallet', 't', 'sol-lp', null)", (T(10),))
        self.assertEqual(db.flow_totals('sol-usdc')['deposits_usd'], 12.0)         # the legacy wallet's: sol-usdc's
        self.assertEqual(db.since_start(0.0, 'sol-usdc')['start_usd'], 230.0 + 7.0 + 5.0)   # not + 9

    def test_the_swings_since_start_has_dollars_and_no_hold(self):
        s = db.since_start(0.0, 'sol-swing')
        self.assertEqual((s['start_usd'], s['equity_usd'], s['paid_out_usd']), (145.0, 152.0, 0.4))
        self.assertAlmostEqual(s['profit_usd'], 152.0 + 0.4 - 145.0, places=4)
        for k in ('start_sol', 'price_start', 'price_now') + db.SINCE_HOLD:
            self.assertIsNone(s[k], k)
        one = db.since_start(0.0, 'sol-usdc')
        self.assertTrue(all(one[k] is not None for k in ('start_sol', 'price_start', 'price_now') + db.SINCE_HOLD))
        with context():
            both = db.since_start(wallet_id=None)
        self.assertTrue(all(both[k] is None for k in db.SINCE_HOLD))
        self.assertIsNotNone(db.since_start(wallet_id='sol-lp')['vs_hold_50_50_usd'])     # mu-usdc: no baseline

    def test_a_day_across_two_pairs_has_no_price_and_no_hold(self):
        # three days ago the swing held DJT then SOL; sol-usdc held SOL all day
        day = (db.now() - dt.timedelta(days=3)).date()
        noon = dt.datetime.combine(day, dt.time(12), tzinfo=dt.timezone.utc)
        with db.cursor(commit=True) as cur:
            for mint, h, px in (('SW1', 2, 1.5), ('SW2', 8, 150.0), ('S1', 1, 150.0), ('S1', 9, 152.0)):
                cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) "
                            "values (%s, %s, %s, true, '1', 100)", (noon - dt.timedelta(hours=10 - h), mint, px))
        sw = db.daily_line(day, 'sol-swing')
        self.assertEqual([sw[k] for k in ('price_open', 'price_close', 'hold_50_50_usd', 'vs_hold_usd')], [None] * 4)
        self.assertEqual(sw['equity_open'], 100.0)
        su = db.daily_line(day, 'sol-usdc')
        self.assertEqual((su['price_open'], su['price_close']), (150.0, 152.0))
        self.assertIsNotNone(su['vs_hold_usd'])
        with context():
            every = db.daily_line(day)                                          # every profile, one line each, added
        self.assertEqual((every['equity_open'], every['price_open'], every['vs_hold_usd']), (200.0, None, None))

    def test_the_native_price_is_of_the_profiles_pool_now(self):
        # the swing just left DJT: its last DJT snapshot is newer than its first SOL one
        with db.cursor(commit=True) as cur:
            cur.execute("delete from snapshots where mint in ('S1', 'MU1')")
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) "
                        "values (now() - interval '2 minutes', 'SW2', 151.25, true, '1', 150)")
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) "
                        "values (now() - interval '1 minute', 'SW1', 0.42, true, '1', 150)")
        self.assertEqual(db.native_price(SOL_MINT, [USDC_MINT], 900), 151.25)       # not DJT's 0.42

    def test_notify_names_the_wallet_by_its_tag(self):
        import rebalancer
        from unittest import mock
        with mock.patch.object(rebalancer.config, 'WALLET_ADDRESS', SWING_ADDR):
            rebalancer.notify('zzz_test', a=1)
        row = json.loads(rebalancer.FEED.read_text().splitlines()[-1])
        self.assertEqual((row['event'], row['wallet_tag']), ('zzz_test', 'FogqBWLC4y'))
        with mock.patch.object(rebalancer.config, 'WALLET_ADDRESS', None):
            rebalancer.notify('zzz_test', wallet_tag='given')
        self.assertEqual(json.loads(rebalancer.FEED.read_text().splitlines()[-1])['wallet_tag'], 'given')


USDC_MINT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def pos(mint, profile, pool, pair, dex, hours=30, closed_hours=None):
    """A position of `profile` on `pool`, opened `hours` ago (closed
    `closed_hours` ago)."""
    db.open_position(mint, pool, pair, 9.5, 10.5, 2.0, f'sig-{mint}', 100.0, 'test', profile, dex)
    with db.cursor(commit=True) as cur:
        cur.execute('update positions set opened_at = %s, closed_at = %s where mint = %s',
                    (T(hours), T(closed_hours) if closed_hours is not None else None, mint))



class SwingTokens(unittest.TestCase):
    """2026-10-05: sol-swing held SOL/USDC and DJT/USDC. SOL and DJT do not add,
    so db.stats has no A-side amount, and the fee lines showed '- DJT'. Each
    A token now shows on its own, summed over its pools."""

    POOLS = [{'pair_label': 'DJT/USDC', 'realised_a': 0.046633, 'unrealised_a': 0.034904, 'fees_a': 0.081537},
             {'pair_label': 'SOL/USDC', 'realised_a': 0.025958, 'unrealised_a': 0.0, 'fees_a': 0.025958}]

    def rec(self, pools=POOLS, a=None):
        return {'fees_realised_a': a, 'fees_unrealised_a': a, 'fees_total_a': a, 'by_pool': pools}

    def test_replay_sol_swing(self):
        r = self.rec()
        self.assertEqual(stats._side_a(r, 'fees_realised_a', 'DJT'), '0.046633 DJT 0.025958 SOL')
        self.assertEqual(stats._side_a(r, 'fees_unrealised_a', 'DJT'), '0.034904 DJT')        # 0 SOL is noise
        self.assertEqual(stats._side_a(r, 'fees_total_a', 'DJT'), '0.081537 DJT 0.025958 SOL')

    def test_one_pair_unchanged(self):
        r = self.rec(a=1.5)
        self.assertEqual(stats._side_a(r, 'fees_total_a', 'SOL'), '1.5 SOL')
        self.assertEqual(stats._side_a(self.rec(pools=[]), 'fees_total_a', 'SOL'), '- SOL')
        self.assertEqual(stats._side_a(self.rec(pools=[{'pair_label': 'SOL/USDC', 'fees_a': None}]),
                                       'fees_total_a', 'SOL'), '- SOL')

    def test_all_zero_still_shown(self):
        r = self.rec(pools=[{'pair_label': 'SOL/USDC', 'unrealised_a': 0.0}])
        self.assertEqual(stats._side_a(r, 'fees_unrealised_a', 'SOL'), '0 SOL')

    def test_same_token_in_two_pools_adds(self):
        pools = [{'pair_label': 'SOL/USDC', 'fees_a': 0.25}, {'pair_label': 'SOL/USDC', 'fees_a': 0.5},
                 {'pair_label': 'DJT/USDC', 'fees_a': 1.0}]
        self.assertEqual(stats._side_a(self.rec(pools=pools), 'fees_total_a', 'DJT'), '1 DJT 0.75 SOL')

    @settings(max_examples=200, deadline=None)
    @given(st.lists(st.tuples(st.sampled_from(['SOL', 'DJT', 'MU']),
                              st.floats(min_value=0, max_value=1e6, allow_nan=False)), min_size=1, max_size=6))
    def test_property_each_token_its_own_sum(self, rows):
        pools = [{'pair_label': f'{t}/USDC', 'fees_a': v} for t, v in rows]
        out = stats._side_a(self.rec(pools=pools), 'fees_total_a', 'X')
        self.assertNotIn('-', out)
        parts = out.split()
        got = {parts[i + 1]: float(parts[i]) for i in range(0, len(parts), 2)}
        want = {}
        for t, v in rows:
            want[t] = want.get(t, 0.0) + v
        if any(want.values()):
            want = {t: v for t, v in want.items() if v}
        self.assertEqual(set(got), set(want))
        for t in want:
            self.assertAlmostEqual(got[t], round(want[t], 6), places=5)
        vals = [float(parts[i]) for i in range(0, len(parts), 2)]
        self.assertEqual(vals, sorted(vals, reverse=True))                                  # largest first

    def test_missing_fields(self):
        self.assertEqual(stats._side_a({'fees_total_a': None}, 'fees_total_a', 'SOL'), '- SOL')      # no by_pool
        self.assertEqual(stats._side_a({'fees_total_a': 2.0}, 'fees_total_a', 'SOL'), '2 SOL')
        self.assertEqual(stats._side_a(self.rec(pools=[{'pair_label': None, 'fees_a': 1.0}]), 'fees_total_a', 'S'),
                         '1 ?')
        self.assertEqual(stats._side_a(self.rec(pools=[{'fees_a': 1.0}]), 'fees_total_a', 'S'), '1 ?')
        self.assertEqual(stats._side_a(self.rec(pools=[{'pair_label': '/USDC', 'fees_a': 1.0}]), 'fees_total_a', 'S'),
                         '1 ?')

    def test_render_has_no_dash_for_swing(self):
        r = {'profile': 'sol-swing', 'pair': 'DJT/USDC', 'dex': 'orca', 'token_a': 'DJT', 'token_b': 'USDC',
             'positions_open_now': 1, 'equity_usd': 1, 'lp_usd': 1, 'idle_usd': 0,
             'fees_realised_a': None, 'fees_realised_b': 3.28, 'fees_realised_usd': 6.8,
             'fees_unrealised_a': None, 'fees_unrealised_b': 0.13, 'fees_unrealised_usd': 0.4,
             'fees_total_a': None, 'fees_total_b': 3.41, 'fees_total_usd': 7.2, 'fees_today_usd': 2,
             'fees_per_day_6h_usd': 1, 'fees_per_day_24h_usd': 1, 'fees_per_day_usd': 1,
             'apr_24h_pct': 1, 'apr_pct': 1, 'paid_usd': 1, 'reinvested_usd': 1, 'gas_usd': 0,
             'profit_usd': 1, 'since': None, 'vs_hold_usd': None, 'vs_hold_50_50_usd': None,
             'deposits_usd': 0, 'deposits': 0, 'withdrawals_usd': 0, 'withdrawals': 0,
             'recentres': 1, 'harvests': 1, 'in_range_pct': 100, 'tracked_days': 1,
             'by_pool': [dict(p, dex='orca', pool='x', positions=1, open_now=0, days=1, fees_b=1, fees_usd=1,
                              realised_usd=1, unrealised_usd=0, fees_per_day_usd=1, apr_pct=1, in_range_pct=1)
                         for p in self.POOLS]}
        fees = [l for l in stats._pool_lines(r) if 'realised' in l or 'total ' in l]
        self.assertEqual(len(fees), 3)
        for l in fees:
            self.assertNotIn('- DJT', l)
        self.assertIn('realised 0.046633 DJT 0.025958 SOL 3.28 USDC', fees[0])
        self.assertIn('total 0.081537 DJT 0.025958 SOL 3.41 USDC', fees[2])
        bare = dict(r, token_a=None, token_b=None, fees_realised_a=1.0, by_pool=[])
        self.assertIn('realised 1 A 3.28 B', stats._pool_lines(bare)[2])
        named = dict(r, fees_realised_a=1.0, by_pool=[])
        self.assertIn('realised 1 DJT 3.28 USDC', stats._pool_lines(named)[2])


class ByPoolExact(unittest.TestCase):
    """db.by_pool on figures worked by hand: one row per profile and pool."""

    def setUp(self):
        reset_ledger()
        multi_wallet()
        # P1: A closed (in range half the time), B and B2 open
        pos('A', 'sol-usdc', 'P1', 'SOL/USDC', 'orca', hours=48, closed_hours=24)
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set deposit_usd = 100, withdraw_usd = 103 where mint = 'A'")
        snap('A', 40, 0.0, 100.0, lp=99.0, in_range=True)
        snap('A', 30, 0.0, 100.0, lp=99.0, in_range=False)
        harvest('A', 25, 0.01, 1.0, 1.0)
        pos('B', 'sol-usdc', 'P1', 'SOL/USDC', 'orca', hours=24)
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set deposit_usd = 300 where mint = 'B'")
        snap('B', 20, 0.2, 310.0, lp=298.0)
        snap('B', 10, 0.3, 311.0, lp=299.0)
        harvest('B', 15, 0.002, 0.5, 0.5)
        pos('B2', 'sol-usdc', 'P1', 'SOL/USDC', 'orca', hours=12)
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set deposit_usd = 50 where mint = 'B2'")
        snap('B2', 5, 0.1, 60.0, lp=51.0)
        # P2: no deposit on record
        pos('C', 'sol-usdc', 'P2', 'SOL/USDC', 'orca', hours=20)
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set deposit_usd = null where mint = 'C'")
        snap('C', 6, 0.0, 20.0, lp=20.0)
        # P3: a deposit under a dollar
        pos('E', 'sol-usdc', 'P3', 'SOL/USDC', 'orca', hours=6)
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set deposit_usd = 0.5 where mint = 'E'")
        snap('E', 1, 0.01, 0.6, lp=0.5)
        # P4: half an hour, closed: too short for a rate, no equity
        pos('D', 'sol-usdc', 'P4', 'SOL/USDC', 'orca', hours=0.5, closed_hours=0.2)
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set deposit_usd = 10, withdraw_usd = 10 where mint = 'D'")

    def tearDown(self):
        reset_ledger()
        drop_extra()
        ensure_profile()

    def test_every_figure_of_every_pool(self):
        rows = db.by_pool('sol-usdc')
        self.assertEqual([r['pool'] for r in rows], ['P4', 'P3', 'P1', 'P2'])    # newest position first
        p4, p3, p1, p2 = rows
        self.assertEqual((p1['positions'], p1['open_now'], p1['profile']), (3, 2, 'sol-usdc'))
        self.assertAlmostEqual(p1['days'], 2.5, places=2)
        self.assertEqual((p1['realised_usd'], p1['unrealised_usd'], p1['fees_usd']), (1.5, 0.4, 1.9))
        self.assertEqual((p1['realised_a'], p1['realised_b'], p1['unrealised_a'], p1['unrealised_b']),
                         (0.012, 1.5, 0.0, 0.0))
        self.assertAlmostEqual(p1['fees_per_day_usd'], 1.9 / 2.5, places=3)
        self.assertAlmostEqual(p1['apr_pct'], 1.9 / 2.5 / 170.0 * 365 * 100, delta=0.2)   # on the days-weighted deposit
        self.assertEqual(p1['in_range_pct'], 83.3)                              # mean of 50%, 100%, 100%
        self.assertEqual(p1['equity_usd'], 311.0)                               # the open ones' latest, highest
        self.assertEqual(p1['deposit_usd'], 450.0)
        self.assertEqual(p1['position_pnl_usd'], 3.0)                           # 103-100 + 299-300 + 51-50
        self.assertEqual(p1['pnl_usd'], 4.9)
        self.assertEqual(p1['unpriced'], 0)
        self.assertLess(abs((p1['last_seen'] - db.now()).total_seconds()), 120)  # open now
        self.assertLess(abs((p1['first_opened'] - T(48)).total_seconds()), 120)
        self.assertEqual((p2['position_pnl_usd'], p2['pnl_usd'], p2['unpriced'], p2['apr_pct']), (None, None, 1, None))
        self.assertEqual((p2['fees_per_day_usd'], p2['deposit_usd'], p2['equity_usd']), (0.0, 0.0, 20.0))
        self.assertAlmostEqual(p3['apr_pct'], 0.01 / 0.25 / 0.5 * 365 * 100, delta=20)
        self.assertEqual((p4['fees_per_day_usd'], p4['apr_pct'], p4['equity_usd'], p4['in_range_pct']),
                         (None, None, None, None))
        self.assertEqual((p4['position_pnl_usd'], p4['pnl_usd']), (0.0, 0.0))
        self.assertLess(abs((p4['last_seen'] - T(0.2)).total_seconds()), 120)    # closed: when it closed



def ssnap(mint, hours_ago, price, equity, accrued=(0.0, 0.0, 0.0), lp=None, wallet=None, in_range=True,
          band=None, p_exit=(None, None, None)):
    """A snapshot with every column given (db.snapshot prices nothing by itself)."""
    with db.cursor(commit=True) as cur:
        cur.execute('insert into snapshots (ts, mint, price, in_range, liquidity, accrued_a, accrued_b, accrued_usd, '
                    'wallet_usd, position_usd, equity_usd, band_position, p_exit_6h, p_exit_24h, p_exit_72h) '
                    "values (%s,%s,%s,%s,'1',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (T(hours_ago), mint, price, in_range, *accrued, wallet, lp, equity, band, *p_exit))


class StatsExact(unittest.TestCase):
    """db.stats of one profile on figures worked by hand."""

    def setUp(self):
        reset_ledger()
        multi_wallet()
        # long-lived, so a 1-in-86400 (or 1-in-3600) slip in a day (an hour) shows in the rounding
        pos('Z', 'sol-usdc', 'PZ', 'SOL/USDC', 'byreal', hours=2400)                 # open, never snapshotted
        pos('Y', 'sol-usdc', 'P', 'SOL/USDC', 'orca', hours=720, closed_hours=50)
        pos('X', 'sol-usdc', 'P', 'SOL/USDC', 'orca', hours=480)
        for i in range(9):                                                           # nine more pools, newest
            pos(f'Q{i}', 'sol-usdc', f'PQ{i}', 'SOL/USDC', 'orca', hours=2, closed_hours=1.5)
        ssnap('Y', 70, 9.0, None)                                                    # unpriced: not the start
        ssnap('Y', 60, 9.5, 201.0, in_range=False)
        ssnap('X', 30, 139.0, 204.0, (0.03, 0.05, 0.1), lp=190.0)
        ssnap('X', 24.5, 140.0, 205.0, (0.05, 0.1, 0.2), lp=190.0)
        ssnap('X', 10, 144.0, 207.0, (0.07, 0.2, 0.4), lp=190.0)
        ssnap('X', 6.5, 145.0, 208.0, (0.08, 0.3, 0.6), lp=190.0)
        ssnap('X', 5, 150.0, 210.0, (0.1, 0.5, 1.25), lp=190.0)
        ssnap('X', 1, 151.0, 212.0, (0.2000004, 0.6, 1.5), lp=195.0, wallet=15.5, in_range=False,
              band=0.25, p_exit=(0.1, 0.3, 0.6))
        harvest('Y', 55, 0.0101, 1.0, 2.5)
        harvest('X', 3, 0.01, 0.5, 2.0)

    def tearDown(self):
        reset_ledger()
        drop_extra()
        ensure_profile()

    def book(self, **kw):
        from unittest import mock
        with context(), mock.patch.object(db, 'season', return_value=[2.0] * 24):
            return db.stats(profile=kw.pop('profile', 'sol-usdc'), **kw)

    def test_every_figure(self):
        s = self.book()
        self.assertEqual((s['harvests'], s['fees_realised_usd'], s['fees_realised_a'], s['fees_realised_b']),
                         (2, 4.5, 0.0201, 1.5))
        self.assertEqual((s['fees_unrealised_usd'], s['fees_unrealised_a'], s['fees_unrealised_b']), (1.5, 0.2, 0.6))
        self.assertEqual((s['fees_total_usd'], s['fees_total_a'], s['fees_total_b']), (6.0, 0.2201, 2.1))
        self.assertEqual(s['tracked_days'], round(2399 / 24, 3))                  # from the first open to the last poll
        self.assertAlmostEqual(s['fees_per_day_usd'], 6.0 / (2399 / 24), places=4)
        self.assertAlmostEqual(s['apr_pct'], 6.0 / (2399 / 24) / 212.0 * 365 * 100, delta=0.02)
        self.assertEqual((s['equity_usd'], s['equity_start_usd']), (212.0, 201.0))
        self.assertEqual((s['lp_usd'], s['wallet_usd'], s['deployed_pct']), (195.0, 15.5, 92.0))
        self.assertEqual((s['pnl_usd'], s['pnl_basis']), (11.0, 'first snapshot'))
        self.assertEqual(s['in_range_pct'], 75.0)                                   # 6 of 8 snapshots
        self.assertEqual((s['positions_opened'], s['positions_open_now']), (12, 2))
        self.assertEqual((s['position_dex'], s['position_pair'], s['position_pool']), ('orca', 'SOL/USDC', 'P'))
        self.assertEqual(s['dexes_held'], ['byreal', 'orca'])
        self.assertEqual(len(s['by_pool']), 8)                                      # of 11 pools
        self.assertEqual((s['pnl_all_pools_usd'], s['position_pnl_all_pools_usd']), (101.0, 95.0))
        self.assertEqual(s['last_price'], 151.0)
        self.assertEqual(s['last_seen'][:16], T(1).isoformat()[:16])
        self.assertEqual(s['band'], {'in_range': False, 'position': 0.25, 'p_exit_6h': 0.1, 'p_exit_24h': 0.3,
                                     'p_exit_72h': 0.6, 'hours_alive': 479.0})
        self.assertEqual((s['token_a'], s['token_b']), ('SOL', 'USDC'))
        self.assertEqual(self.book(token_a='XSOL', token_b='XUSD')['token_a'], 'XSOL')
        self.assertEqual(self.book(token_a='XSOL', token_b='XUSD')['token_b'], 'XUSD')
        rate = {h: db.trailing_rate(h, 'sol-usdc')['fees_per_day_usd'] for h in (6, 7, 12, 24, 25, 48)}
        self.assertEqual(len({rate[6], rate[7], rate[12]}), 3, rate)                 # the windows tell apart
        self.assertEqual(len({rate[24], rate[25], rate[48]}), 3, rate)
        self.assertEqual((s['fees_per_day_6h_usd'], s['fees_per_day_24h_usd']), (rate[6], rate[24]))
        self.assertEqual(s['apr_6h_pct'], round(rate[6] / 212.0 * 365 * 100, 2))
        self.assertEqual(s['apr_24h_pct'], round(rate[24] / 212.0 * 365 * 100, 2))
        self.assertEqual(s['expected_next_hours_fees_per_day_usd'], round(rate[24] * 2.0, 4))

    def test_a_failed_wallet_read_keeps_the_last_priced_equity(self):
        ssnap('X', 0.5, 152.0, None, (0.3, 0.7, 1.6))
        s = self.book()
        self.assertEqual((s['equity_usd'], s['fees_unrealised_usd'], s['last_price']), (212.0, 1.6, 152.0))

    def test_a_closed_position_has_no_unrealised_and_no_band(self):
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set closed_at = %s where mint in ('X', 'Z')", (T(0.2),))
        s = self.book()
        self.assertEqual((s['fees_unrealised_usd'], s['fees_unrealised_a'], s['fees_unrealised_b']), (0.0, 0.0, 0.0))
        self.assertEqual((s['fees_total_usd'], s['band'], s['lp_usd']), (4.5, None, 0.0))

    def test_an_empty_book(self):
        s = self.book(profile='nobody')                                             # no config row, no rows
        self.assertEqual((s['token_a'], s['token_b'], s['pair']), ('A', 'B', None))
        self.assertEqual([s[k] for k in ('last_price', 'in_range_pct', 'tracked_days', 'equity_usd', 'band',
                                         'fees_per_day_usd', 'last_seen')], [None] * 7)
        pos('G', 'djt-usdc', 'PG', 'DJT/USDC', 'orca', hours=5)                    # a position, no snapshot
        s = self.book(profile='djt-usdc')
        self.assertEqual((s['tracked_days'], s['last_price'], s['in_range_pct'], s['positions_open_now']),
                         (None, None, None, 1))

    def test_a_book_seen_once_at_its_open(self):
        pos('M', 'mu-usdc', MU_POOL, 'MU/USDC', 'meteora-dlmm', hours=3)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) "
                        "select opened_at, 'M', 10, true, '1', 50 from positions where mint = 'M'")
        s = self.book(profile='mu-usdc')
        self.assertEqual((s['tracked_days'], s['fees_per_day_usd'], s['apr_pct']), (0.0, None, None))


if __name__ == '__main__':
    unittest.main()
