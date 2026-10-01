"""The sol-usdc book is the same before and after the per-profile scoping.

A copy of a real book (LPBOT_STATS_SOURCE, a database holding the rebalancer
schema, e.g. a rehearsal pg_dump of the live one) is dumped into scratch
databases. The pre-020 db.py (from git, at PRE_MERGE) and the new one read
them at one frozen instant:

  copy A  the source with migration 020 applied, nothing backfilled
          (NULL profile, wallet and config_name on the old rows)
  copy B  A with the deploy's backfill (wallet sol-lp, profile stamps, the
          audit_state keys prefixed)
  copy C  B plus a second enabled profile (mu-usdc) on the same wallet with
          its own position, fees, payout, flows and events

Every report of the new code for sol-usdc (no filter, the process's own
context, the explicit profile, its wallet on A and B) equals the old code's on
A, exactly. On C the sol-usdc book is still equal, and the unfiltered book is
the two profiles' books added up.

Skips without LPBOT_STATS_SOURCE, pg_dump/psql or git. The live database is
never a source: the default suite reads no live data.

    LPBOT_STATS_SOURCE=rebalancer_rehearsal tests/run.sh test_stats_equality
"""
import contextlib
import datetime as dt
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The reference: db.py as main had it just before the multi-wallet merge (the
# code the live bot ran). Pinned to a commit, not read from a directory: after
# the merge every working tree holds the new db.py, and a comparison against
# it would be new against new and pass for nothing.
PRE_MERGE = 'c2c1cc2'
SOURCE = os.environ.get('LPBOT_STATS_SOURCE', '')
LIVE_DB = 'rebalancer'
# One set of scratch copies per run (the pid): two runs at once never rebuild each other's.
COPIES = {k: f'rebalancer_stats_copy_{k}_{os.getpid()}' for k in 'abc'}


def _old_db_source():
    """db.py at PRE_MERGE, read with git (read only), or None."""
    try:
        top = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel'], check=True,
                             capture_output=True, text=True).stdout.strip()
        return subprocess.run(['git', '-C', top, 'show', f'{PRE_MERGE}:db.py'], check=True,
                              capture_output=True, text=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


BACKFILL = """
set search_path to rebalancer;
insert into wallets (id, chain, address, secret_env, label)
  values ('sol-lp', 'solana', '11111111111111111111111111111111', 'WALLET_SECRET_PATH', 'LP wallet')
  on conflict do nothing;
update config set wallet_id = 'sol-lp', enabled = true, residual_owner = true,
                  deposit_mint = 'So11111111111111111111111111111111111111112' where name = 'sol-usdc';
update positions set config_name = 'sol-usdc' where config_name is null;
update payouts set config_name = 'sol-usdc' where config_name is null;
update events set profile = 'sol-usdc' where profile is null;
update audits set profile = 'sol-usdc', wallet_id = 'sol-lp' where profile is null;
update capital_flows set profile = 'sol-usdc', wallet_id = 'sol-lp' where profile is null;
update audit_state set key = 'sol-lp|' || key where key not like '%|%';
update health set key = 'sol-usdc|' || key where key not like '%|%';
"""

# A second profile on the same wallet, relative to the frozen instant (:T).
SECOND = """
set search_path to rebalancer;
insert into config (name, pool, pair_label, token_a, token_b, capital_usd, max_usd, dex, wallet_id, enabled,
                    deposit_mint, payout_enabled, profit_wallet, payout_mint)
  values ('mu-usdc', '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5', 'MU/USDC', 'MU', 'USDC', 100, 140,
          'meteora-dlmm', 'sol-lp', true, 'MUmint1111111111111111111111111111111111111', true,
          '8funmDkP1111111111111111111111111111111111', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v');
insert into positions (mint, config_name, pool, pair_label, opened_at, lower_price, upper_price, deposit_usd, dex)
  values ('MUPOS1', 'mu-usdc', '13MEx6gjRadJNUdmToaGSzgeWHLH7FzScUQS9Mc5nYF5', 'MU/USDC',
          :T - interval '30 hours', 9.5, 10.5, 98, 'meteora-dlmm');
insert into snapshots (ts, mint, price, in_range, liquidity, accrued_a, accrued_b, accrued_usd, wallet_usd,
                       position_usd, equity_usd)
  select :T - make_interval(hours => h), 'MUPOS1', 10, h % 5 <> 0, '1', 0.01 * acc, 0.1 * acc,
         0.2 * acc, 2.0, 99.0, 101.0 + 0.2 * acc
  from generate_series(29, 1, -1) h,                -- oldest first: ids follow time, as in the bot
       lateral (select case when h > 20 then 30 - h else 20 - h end acc) x;   -- the harvest at -20h resets it
insert into harvests (ts, mint, fee_a, fee_b, fee_usd, signature)
  values (:T - interval '20 hours', 'MUPOS1', 0.05, 0.5, 1.0, 'MUHARVEST1');
insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind, to_address, signature)
  values (:T - interval '19 hours', 'mu-usdc', 'MUPOS1', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'USDC',
          0.6, 0.6, 'paid', '8funmDkP1111111111111111111111111111111111', 'MUPAY1');
insert into capital_flows (ts, kind, sol, usdc, usd, price, signature, detail, wallet_id, profile, amounts)
  values (:T - interval '31 hours', 'baseline', 0, 0, 100, 10, null, 'mu start', 'sol-lp', 'mu-usdc',
          '{"MUmint1111111111111111111111111111111111111": 5}'),
         (:T - interval '25 hours', 'deposit', 0, 0, 10, 10, 'MUDEP1', 'mu deposit', 'sol-lp', 'mu-usdc',
          '{"MUmint1111111111111111111111111111111111111": 1}');
insert into events (ts, kind, detail, profile)
  values (:T - interval '5 hours', 'REBAND', 'mu', 'mu-usdc'), (:T - interval '4 hours', 'open_failed', 'mu', 'mu-usdc');
"""


def _run(*cmd, stdin=None):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, input=stdin)


def _load(name, path, dsn):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DSN = dsn
    return mod


def _freeze(mod, at):
    """Every SQL now() and the module's now() read `at`: the old and the new
    code see one instant, so their figures can be compared exactly."""
    orig = mod.cursor
    lit = f"'{at.isoformat()}'::timestamptz"

    class Frozen:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, sql, args=None):
            return self._cur.execute(sql.replace('now()', lit), args)

        def __getattr__(self, k):
            return getattr(self._cur, k)

    @contextlib.contextmanager
    def cursor(commit=False):
        with orig(commit) as cur:
            yield Frozen(cur)
    mod.cursor = cursor
    mod.now = lambda: at


def _printed(fn, *a, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*a, **kw)
    return out.getvalue()


@unittest.skipUnless(SOURCE, 'set LPBOT_STATS_SOURCE to a copy of a real book (never the live database)')
@unittest.skipUnless(shutil.which('pg_dump') and shutil.which('psql'), 'needs pg_dump and psql')
class OldEqualsNew(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SOURCE in (LIVE_DB, *COPIES.values()):
            raise unittest.SkipTest(f'{SOURCE} cannot be the source: use a copy of the live book')
        old_src = _old_db_source()
        if old_src is None or 'def book_scope(' in old_src:
            raise unittest.SkipTest(f'no pre-merge db.py at {PRE_MERGE} in this repository')
        tmp = pathlib.Path(tempfile.mkdtemp(prefix='stats_eq_'))
        cls.tmp = tmp
        try:
            _run('pg_dump', '-d', SOURCE, '-n', 'rebalancer', '-Fc', '-f', str(tmp / 'book.dump'))
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            raise unittest.SkipTest(f'cannot read {SOURCE}: {getattr(e, "stderr", e)}')
        for db_name in COPIES.values():
            _run('dropdb', '--if-exists', db_name)
            _run('createdb', db_name)
            _run('pg_restore', '--no-owner', '-d', db_name, str(tmp / 'book.dump'))
            _run('psql', '-q', '-v', 'ON_ERROR_STOP=1', '-d', db_name, '-f', str(ROOT / 'sql' / '020_multi_wallet.sql'))
        _run('psql', '-q', '-v', 'ON_ERROR_STOP=1', '-d', COPIES['b'], stdin=BACKFILL)
        _run('psql', '-q', '-v', 'ON_ERROR_STOP=1', '-d', COPIES['c'], stdin=BACKFILL)
        last = _run('psql', '-tA', '-d', COPIES['a'], '-c', 'select max(ts) from rebalancer.snapshots').stdout.strip()
        if not last:
            raise unittest.SkipTest('the source book has no snapshot')
        cls.at = dt.datetime.fromisoformat(last).astimezone(dt.timezone.utc) + dt.timedelta(minutes=2)
        _run('psql', '-q', '-v', 'ON_ERROR_STOP=1', '-d', COPIES['c'],
             stdin=SECOND.replace(':T', f"'{cls.at.isoformat()}'::timestamptz"))
        (tmp / 'db_pre020.py').write_text(old_src)
        cls.old = _load('db_pre020', tmp / 'db_pre020.py', f"dbname={COPIES['a']}")
        cls.new = {k: _load(f'db_new_{k}', ROOT / 'db.py', f'dbname={v}') for k, v in COPIES.items()}
        for m in (cls.old, *cls.new.values()):
            _freeze(m, cls.at)
        first = _run('psql', '-tA', '-d', COPIES['a'], '-c',
                     "select min(ts)::date from rebalancer.snapshots").stdout.strip()
        d0 = dt.date.fromisoformat(first)
        cls.days = [d0 + dt.timedelta(days=k) for k in range((cls.at.date() - d0).days + 1)]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for db_name in COPIES.values():
            subprocess.run(['dropdb', '--if-exists', db_name], capture_output=True)

    def reports(self, m, **scope):
        """Every report the book is made of, for one scope."""
        day0 = self.at.replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            'stats': m.stats(**scope),
            'by_pool': m.by_pool(**scope),
            'fees_today': m.fees_between(day0, None, **scope),
            'fees_week': m.fees_between(day0 - dt.timedelta(days=7), day0 - dt.timedelta(days=1), **scope),
            'trailing': [m.trailing_rate(h, **scope) for h in (6, 24, 168)],
            'daily_line': [m.daily_line(d, **scope) for d in self.days],
            'daily_lines': m.daily_lines(5, **scope),
            'since_start': [m.since_start(0.0, **scope), m.since_start(1.25, **scope)],
            'since_start_book': m._since_start_or_none(**scope),
            'payouts': m.payout_totals(**scope),
            'daily': m.daily(**scope),
            'history': m.history(50, **scope),
            'printed': _printed(m._print_stats, **scope),
        }

    def old_reports(self):
        if not hasattr(type(self), 'want'):
            o = self.old
            r = self.reports(o)
            # the old payout readers took a profile name (None: every row)
            r['payouts_named'] = o.payout_totals('sol-usdc')
            r['reinvested'] = o.reinvested_usd('sol-usdc')
            type(self).want = r
        return type(self).want

    def new_reports(self, m, **scope):
        r = self.reports(m, **scope)
        r['payouts_named'] = m.payout_totals('sol-usdc')
        r['reinvested'] = m.reinvested_usd('sol-usdc')
        return r

    def assertSameBook(self, want, got, label):
        self.assertEqual(set(want), set(got))
        for k in want:
            self.assertEqual(want[k], got[k], f'{label}: {k} differs')

    def test_the_book_has_data(self):
        s = self.old.stats()
        self.assertGreater(s['harvests'], 0)
        self.assertIsNotNone(s['equity_usd'])
        self.assertIsNotNone(s['since_start'])

    def test_unbackfilled_copy_is_identical(self):
        want = self.old_reports()
        m = self.new['a']
        for label, ctx, scope in (('no filter, no context', None, {}),
                                  ('own context', ('sol-usdc', None), {}),
                                  ('explicit profile', None, {'profile': 'sol-usdc'}),
                                  ('legacy wallet', None, {'wallet_id': 'sol-lp'})):
            with self.subTest(label):
                m.CONTEXT.update(profile=None, wallet_id=None)
                if ctx:
                    m.set_context(*ctx)
                self.assertSameBook(want, self.new_reports(m, **scope), label)
        m.CONTEXT.update(profile=None, wallet_id=None)

    def test_backfilled_copy_is_identical(self):
        want = self.old_reports()
        m = self.new['b']
        for label, ctx, scope in (('no filter, no context', None, {}),
                                  ('own context', ('sol-usdc', 'sol-lp'), {}),
                                  ('explicit profile', None, {'profile': 'sol-usdc'}),
                                  ('its wallet', None, {'wallet_id': 'sol-lp'}),
                                  ('profile and wallet', None, {'profile': 'sol-usdc', 'wallet_id': 'sol-lp'})):
            with self.subTest(label):
                m.CONTEXT.update(profile=None, wallet_id=None)
                if ctx:
                    m.set_context(*ctx)
                self.assertSameBook(want, self.new_reports(m, **scope), label)
        m.CONTEXT.update(profile=None, wallet_id=None)

    def test_a_second_profile_leaves_the_sol_book_unchanged(self):
        want = self.old_reports()
        m = self.new['c']
        for label, ctx, scope in (('own context', ('sol-usdc', 'sol-lp'), {}),
                                  ('explicit profile', None, {'profile': 'sol-usdc'})):
            with self.subTest(label):
                m.CONTEXT.update(profile=None, wallet_id=None)
                if ctx:
                    m.set_context(*ctx)
                self.assertSameBook(want, self.new_reports(m, **scope), label)
        m.CONTEXT.update(profile=None, wallet_id=None)

    def test_forecasts_are_identical(self):
        want = self.old.forecasts()                 # slow (a self-join on the tape): once per copy
        self.assertEqual(want, self.new['a'].forecasts())
        self.assertEqual(want, self.new['b'].forecasts(profile='sol-usdc'))
        self.assertEqual(want, self.new['c'].forecasts(profile='sol-usdc'))

    def test_the_legacy_rows_are_what_makes_it_equal(self):
        # copy A holds a position with no config_name and every pre-020 row
        # with no profile: attributing them elsewhere changes the book
        m = self.new['a']
        n_null = int(_run('psql', '-tA', '-d', COPIES['a'], '-c',
                          'select count(*) from rebalancer.positions where config_name is null').stdout)
        if not n_null:
            self.skipTest('the source book has no position without a profile')
        want = self.old.stats()
        try:
            m.LEGACY_PROFILE = 'nobody'
            got = m.stats(profile='sol-usdc')
        finally:
            m.LEGACY_PROFILE = 'sol-usdc'
        self.assertLess(got['positions_opened'], want['positions_opened'])
        self.assertEqual(m.stats(profile='sol-usdc'), want)

    def test_the_second_profile_has_its_own_book(self):
        m = self.new['c']
        mu = m.stats(profile='mu-usdc')
        self.assertEqual((mu['token_a'], mu['token_b'], mu['pair']), ('MU', 'USDC', 'MU/USDC'))
        self.assertEqual(mu['harvests'], 1)
        self.assertEqual(mu['fees_realised_usd'], 1.0)
        self.assertEqual(mu['fees_unrealised_usd'], 3.8)            # the latest snapshot, one hour old
        self.assertEqual((mu['fees_unrealised_a'], mu['fees_unrealised_b']), (0.19, 1.9))
        self.assertEqual(mu['fees_total_usd'], 4.8)
        self.assertEqual(mu['equity_usd'], 104.8)
        self.assertEqual((mu['lp_usd'], mu['wallet_usd']), (99.0, 2.0))
        self.assertEqual((mu['rebands'], mu['failures']), (1, 1))
        self.assertEqual(mu['positions_open_now'], 1)
        self.assertEqual(mu['split']['paid_usd'], 0.6)
        s = mu['since_start']
        self.assertEqual(s['start_usd'], 110.0)                      # baseline 100 + deposit 10
        self.assertEqual(s['start_sol'], 6.0)                        # 5 MU + 1 MU from the flows' amounts
        self.assertEqual(s['uncounted_usd'], 0.0)                    # the wallet's dust is sol-usdc's (residual owner)
        self.assertEqual(s['paid_out_usd'], 0.6)
        self.assertEqual(m.flow_totals(profile='mu-usdc'),
                         {'deposits_usd': 10.0, 'deposits': 1, 'withdrawals_usd': 0.0, 'withdrawals': 0})

    def test_every_enabled_profile_adds_up(self):
        m = self.new['c']
        m.CONTEXT.update(profile=None, wallet_id=None)
        sol, mu, both = m.stats(profile='sol-usdc'), m.stats(profile='mu-usdc'), m.stats()
        self.assertEqual(both, m.combine_books([mu, sol]))           # book_profiles orders by name
        for k in m.BOOK_USD:
            parts = [x[k] for x in (sol, mu) if x[k] is not None]
            if parts:
                self.assertAlmostEqual(both[k], sum(parts), places=3, msg=k)
        self.assertEqual(both['token_b'], 'USDC')
        self.assertIsNone(both['token_a'])                           # SOL and MU do not add
        self.assertIsNone(both['fees_total_a'])
        self.assertAlmostEqual(both['fees_total_b'], sol['fees_total_b'] + mu['fees_total_b'], places=5)
        self.assertEqual(both['harvests'], sol['harvests'] + mu['harvests'])
        self.assertEqual(m.stats(wallet_id='sol-lp'), both)
        self.assertEqual(m.stats(wallet_id='base-lp')['harvests'], 0)
        self.assertEqual(m.stats(profile='mu-usdc', wallet_id='base-lp')['harvests'], 0)
        for k in ('paid_usd', 'reinvested_usd', 'gas_usd'):
            self.assertAlmostEqual(m.payout_totals()[k],
                                   m.payout_totals('sol-usdc')[k] + m.payout_totals('mu-usdc')[k], places=4)


if __name__ == '__main__':
    unittest.main()
