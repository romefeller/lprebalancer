"""The swing (2026-10-02): one profile on its own wallet holds DJT/USDC while
the US market is open and SOL/USDC while it is closed.

Covered: swing.py's calendar, decision and audit as properties and by case;
its tick against the test database (the MIGRATE it writes, the alerts);
rebalancer's gates on a pair-changing operator move (allow_swap, the pinned
LPBOT_SWING_POOLS, the adaptive Orca opt-in); and the loop end to end over a
fake chain: SOL/USDC -> DJT/USDC -> SOL/USDC, what each move leaves behind
sold into USDC above the gas reserve, then 50/50 and an open."""
import contextlib
import datetime as dt
import itertools
import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import db
import rebalancer
import swing
import test_multi_loop as tml
import wallets

UTC = dt.timezone.utc
NY = ZoneInfo('America/New_York')
DJT = 'DJTmintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
DJT_POOL = '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG'
OPEN = ('orca', DJT_POOL)
CLOSED = ('raydium-clmm', tml.SOL_POOL)


def ny(*a):
    return dt.datetime(*a, tzinfo=NY).astimezone(UTC)


# --- the calendar -------------------------------------------------------------------

NYSE = swing.CALENDARS['nyse']
ROW = {'profile': 'tk-swing', 'open_dex': OPEN[0], 'open_pool': OPEN[1], 'closed_dex': CLOSED[0],
       'closed_pool': CLOSED[1], 'calendar': 'nyse', 'lead_s': 300}


class Calendar(unittest.TestCase):
    def test_a_regular_day_in_summer_and_in_winter_time(self):
        self.assertFalse(NYSE.is_open(ny(2026, 10, 2, 9, 29, 59)))
        self.assertTrue(NYSE.is_open(ny(2026, 10, 2, 9, 30)))
        self.assertTrue(NYSE.is_open(ny(2026, 10, 2, 15, 59, 59)))
        self.assertFalse(NYSE.is_open(ny(2026, 10, 2, 16, 0)))
        self.assertTrue(NYSE.is_open(dt.datetime(2026, 10, 2, 13, 30, tzinfo=UTC)))     # EDT: 13:30Z
        self.assertFalse(NYSE.is_open(dt.datetime(2026, 12, 2, 13, 30, tzinfo=UTC)))    # EST: 14:30Z
        self.assertTrue(NYSE.is_open(dt.datetime(2026, 12, 2, 14, 30, tzinfo=UTC)))

    def test_weekends_holidays_and_early_closes(self):
        self.assertFalse(NYSE.is_open(ny(2026, 10, 3, 12, 0)))                    # Saturday
        self.assertFalse(NYSE.is_open(ny(2026, 10, 4, 12, 0)))                    # Sunday
        for d in ((2026, 11, 26), (2026, 12, 25), (2027, 3, 26), (2027, 7, 5), (2027, 12, 24)):
            self.assertFalse(NYSE.is_open(ny(*d, 12, 0)), d)
        self.assertTrue(NYSE.is_open(ny(2026, 11, 27, 12, 59)))
        self.assertFalse(NYSE.is_open(ny(2026, 11, 27, 13, 0)))                    # 1 pm close

    def test_the_lead_opens_it_early_but_never_closes_it_late(self):
        self.assertTrue(NYSE.is_open(ny(2026, 10, 2, 9, 25), lead_s=300))
        self.assertFalse(NYSE.is_open(ny(2026, 10, 2, 9, 24, 59), lead_s=300))
        self.assertFalse(NYSE.is_open(ny(2026, 10, 2, 16, 0), lead_s=300))
        self.assertFalse(NYSE.is_open(ny(2026, 10, 3, 9, 25), lead_s=300))        # no session Saturday

    def test_past_the_table_it_refuses_to_guess(self):
        with self.assertRaises(swing.CalendarEnded):
            NYSE.is_open(ny(2028, 1, 3, 12, 0))
        NYSE.session(NYSE.ends)                                          # the last day covered still answers
        with self.assertRaises(swing.CalendarEnded):
            NYSE.session(NYSE.ends + dt.timedelta(days=1))

    def test_every_holiday_is_a_weekday_and_no_early_close_is_a_holiday(self):
        for cal in swing.CALENDARS.values():
            self.assertTrue(all(d.weekday() < 5 and d <= cal.ends for d in cal.holidays | cal.early_closes))
            self.assertFalse(cal.holidays & cal.early_closes)

    def test_the_schema_names_every_calendar(self):
        sql = (pathlib.Path(swing.__file__).parent / 'sql' / '023_swing.sql').read_text()
        for name in swing.CALENDARS:
            self.assertIn(f"'{name}'", sql)

    @settings(max_examples=500, deadline=None)
    @given(st.datetimes(min_value=dt.datetime(2026, 1, 1), max_value=dt.datetime(2027, 12, 31, 23, 59)),
           st.integers(min_value=0, max_value=3600))
    def test_open_only_on_a_weekday_session(self, naive, lead):
        t = naive.replace(tzinfo=UTC)
        local = t.astimezone(NY)
        s = NYSE.session(local.date())
        self.assertEqual(NYSE.is_open(t), s is not None and s[0] <= local < s[1])
        self.assertEqual(NYSE.is_open(t, lead), s is not None and s[0] - dt.timedelta(seconds=lead) <= local < s[1])
        if NYSE.is_open(t):
            self.assertTrue(local.weekday() < 5 and dt.time(9, 30) <= local.time() < dt.time(16, 0))
            self.assertNotIn(local.date(), NYSE.holidays)


class Decide(unittest.TestCase):
    T_OPEN, T_CLOSED = ny(2026, 10, 5, 11, 0), ny(2026, 10, 5, 20, 0)

    def row(self, held):
        return dict(ROW, held_pool=held)

    def test_the_wanted_pool_by_the_session(self):
        self.assertEqual(swing.decide(self.T_OPEN, self.row(CLOSED[1]), None), ('request', OPEN))
        self.assertEqual(swing.decide(self.T_CLOSED, self.row(OPEN[1]), None), ('request', CLOSED))
        self.assertEqual(swing.decide(self.T_OPEN, self.row(OPEN[1]), None), ('hold', OPEN))
        self.assertEqual(swing.decide(self.T_CLOSED, self.row(CLOSED[1]), None), ('hold', CLOSED))

    def test_a_request_is_not_repeated_until_it_is_old(self):
        t = self.T_OPEN.timestamp()
        last = {'pool': OPEN[1], 'at': t - swing.REQUEST_AGAIN_S + 1}
        self.assertEqual(swing.decide(self.T_OPEN, self.row(CLOSED[1]), last)[0], 'hold')
        last['at'] = t - swing.REQUEST_AGAIN_S
        self.assertEqual(swing.decide(self.T_OPEN, self.row(CLOSED[1]), last)[0], 'request')
        other = {'pool': CLOSED[1], 'at': t}                         # the last request was the other way
        self.assertEqual(swing.decide(self.T_OPEN, self.row(CLOSED[1]), other)[0], 'request')

    def test_the_rows_lead_asks_for_the_open_before_the_bell(self):
        self.assertEqual(swing.wanted(ny(2026, 10, 5, 9, 26), ROW), OPEN)
        self.assertEqual(swing.wanted(ny(2026, 10, 5, 9, 24), ROW), CLOSED)
        self.assertEqual(swing.wanted(ny(2026, 10, 5, 9, 26), dict(ROW, lead_s=0)), CLOSED)


class Audit(unittest.TestCase):
    REQ = {'pool': OPEN[1], 'at': 1000.0}

    def test_nothing_before_the_deadlines_or_without_a_request(self):
        self.assertEqual(swing.audit(1000.0 + swing.SWITCH_DEADLINE_S, self.REQ, None, ['SOL']), [])
        self.assertEqual(swing.audit(9e9, None, None, ['SOL']), [])

    def test_late_and_leftover(self):
        t = 1000.0 + swing.LEFTOVER_DEADLINE_S + 1
        kinds = [k for k, _ in swing.audit(t, self.REQ, CLOSED[1], ['SOL'])]
        self.assertEqual(kinds, ['SWING_LATE', 'SWING_LEFTOVER'])
        self.assertEqual(swing.audit(t, self.REQ, OPEN[1], []), [])
        late = swing.audit(1000.0 + swing.SWITCH_DEADLINE_S + 1, self.REQ, None, ['SOL'])
        self.assertEqual([k for k, _ in late], ['SWING_LATE'])
        self.assertIn('no pool', late[0][1])


    def test_an_arrived_switch_is_never_late(self):
        # 2026-10-08 10:43Z: the switch landed at 20:04Z; a re-centre's 33 s with no position is no lateness
        t = 1000.0 + swing.SWITCH_DEADLINE_S + 883 * 60
        self.assertEqual(swing.audit(t, dict(self.REQ, arrived=1240.0), None, []), [])
        self.assertEqual(swing.audit(t, dict(self.REQ, arrived=1240.0), CLOSED[1], []), [])
        # leftovers are still checked after arrival
        self.assertEqual([k for k, _ in swing.audit(1000.0 + swing.LEFTOVER_DEADLINE_S + 1,
                                                    dict(self.REQ, arrived=1240.0), OPEN[1], ['SOL'])],
                         ['SWING_LEFTOVER'])

    def test_property_late_iff_past_the_deadline_not_arrived_and_off_the_pool(self):
        for age, arrived, pos in itertools.product((0, swing.SWITCH_DEADLINE_S, swing.SWITCH_DEADLINE_S + 1, 9e5),
                                                   (None, 1001.0), (None, OPEN[1], CLOSED[1])):
            req = dict(self.REQ, **({'arrived': arrived} if arrived else {}))
            late = 'SWING_LATE' in [k for k, _ in swing.audit(1000.0 + age, req, pos, [])]
            self.assertEqual(late, age > swing.SWITCH_DEADLINE_S and not arrived and pos != OPEN[1],
                             (age, arrived, pos))


class Tick(unittest.TestCase):
    """swing.tick_all against the test database, in a scratch run directory."""

    NAMES = ('tk-swing', 'tk-other')

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='lp_swing_'))
        for p in (mock.patch.object(swing, 'ROOT', self.tmp),
                  mock.patch.object(swing, 'FEED', self.tmp / 'run' / 'swing' / 'events.jsonl'),
                  mock.patch.object(swing, 'STATE', self.tmp / 'run' / 'swing' / 'state.json')):
            p.start(); self.addCleanup(p.stop)
        self.cleanup(); self.addCleanup(self.cleanup)
        with db.cursor(commit=True) as cur:
            for name in self.NAMES:
                cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, enabled) "
                            "values (%s, %s, 'SOL/USDC', 100, 1000, true)", (name, CLOSED[1]))
            cur.execute("insert into swing (profile, open_dex, open_pool, closed_dex, closed_pool) "
                        "values ('tk-swing', %s, %s, %s, %s)", OPEN + CLOSED)
        saved = dict(db.CONTEXT); self.addCleanup(lambda: db.CONTEXT.update(saved))

    def cleanup(self):
        with db.cursor(commit=True) as cur:
            cur.execute('delete from swing where profile = any(%s)', (list(self.NAMES),))
            cur.execute('delete from positions where config_name = any(%s)', (list(self.NAMES),))
            cur.execute('delete from events where profile = any(%s)', (list(self.NAMES),))
            cur.execute('delete from config where name = any(%s)', (list(self.NAMES),))

    def migrate(self, name='tk-swing'):
        f = self.tmp / 'run' / name / 'MIGRATE'
        return f.read_text().strip() if f.exists() else None

    def feed(self):
        f = swing.FEED
        return [(json.loads(x)['profile'], json.loads(x)['event']) for x in f.read_text().splitlines()] \
            if f.exists() else []

    def test_the_open_writes_one_migrate_and_one_swing_event_for_the_row_only(self):
        swing.tick_all(ny(2026, 10, 5, 9, 26))
        self.assertEqual(self.migrate(), f'orca {DJT_POOL}')
        self.assertIsNone(self.migrate('tk-other'))                        # no swing row: never moved
        self.assertEqual(self.feed(), [('tk-swing', 'SWING')])
        (self.tmp / 'run' / 'tk-swing' / 'MIGRATE').unlink()               # the loop took it, not done yet
        swing.tick_all(ny(2026, 10, 5, 9, 27))
        self.assertIsNone(self.migrate())
        with db.cursor() as cur:
            cur.execute("select profile, kind from events where profile = any(%s)", (list(self.NAMES),))
            self.assertEqual([(r['profile'], r['kind']) for r in cur.fetchall()], [('tk-swing', 'SWING')])

    def test_a_dry_tick_writes_nothing(self):
        swing.tick_all(ny(2026, 10, 5, 11, 0), dry=True)
        self.assertIsNone(self.migrate())
        self.assertEqual(self.feed(), [])
        self.assertFalse(swing.STATE.exists())

    def test_a_disabled_profile_or_row_is_left_alone(self):
        with db.cursor(commit=True) as cur:
            cur.execute("update config set enabled = false where name = 'tk-swing'")
        swing.tick_all(ny(2026, 10, 5, 11, 0))
        self.assertIsNone(self.migrate())
        with db.cursor(commit=True) as cur:
            cur.execute("update config set enabled = true where name = 'tk-swing'")
            cur.execute("update swing set enabled = false where profile = 'tk-swing'")
        swing.tick_all(ny(2026, 10, 5, 11, 0))
        self.assertIsNone(self.migrate())

    def test_a_late_switch_alerts_once(self):
        t = ny(2026, 10, 5, 11, 0)
        swing.tick_all(t)
        with db.cursor(commit=True) as cur:
            cur.execute("update config set pool = %s where name = 'tk-swing'", (DJT_POOL,))
        later = t + dt.timedelta(seconds=swing.LEFTOVER_DEADLINE_S + 5)
        with mock.patch.object(swing, 'left_behind', lambda profile: ['SOL']):
            swing.tick_all(later)
            swing.tick_all(later + dt.timedelta(minutes=1))
        self.assertEqual([e for _, e in self.feed()], ['SWING', 'SWING_LATE', 'SWING_LEFTOVER'])

    def test_an_ended_calendar_is_said_once_and_moves_nothing(self):
        swing.tick_all(ny(2028, 1, 3, 11, 0))
        swing.tick_all(ny(2028, 1, 3, 11, 1))
        self.assertEqual(self.feed(), [('tk-swing', 'swing_refused')])
        self.assertIsNone(self.migrate())

    def test_one_rows_failure_does_not_stop_the_next(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into swing (profile, open_dex, open_pool, closed_dex, closed_pool) "
                        "values ('tk-other', %s, %s, %s, %s)", OPEN + CLOSED)
        real = swing.tick
        def boom(row, *a, **k):
            if row['profile'] == 'tk-other':
                raise RuntimeError('x')
            return real(row, *a, **k)
        with mock.patch.object(swing, 'tick', boom):
            swing.tick_all(ny(2026, 10, 5, 11, 0))
        self.assertEqual(self.migrate(), f'orca {DJT_POOL}')

    def test_left_behind_reads_the_profiles_runtime(self):
        run = self.tmp / 'run' / 'tk-swing'
        run.mkdir(parents=True)
        (run / 'runtime.json').write_text(json.dumps({'left_behind': ['X']}))
        self.assertEqual(swing.left_behind('tk-swing'), ['X'])
        self.assertEqual(swing.left_behind('tk-none'), [])

    def test_unknown_arguments_print_the_usage(self):
        self.assertEqual(swing.main(['--help']), 2)
        self.assertEqual(self.feed(), [])


# --- rebalancer: what a pair-changing move may do ------------------------------------

class LeftBehind(unittest.TestCase):
    def test_the_old_pools_mints_the_new_one_lacks(self):
        old, new = ((tml.SOL, 'SOL'), (tml.USDC, 'USDC')), ((DJT, 'DJT'), (tml.USDC, 'USDC'))
        self.assertEqual(rebalancer.left_behind(old, new), [tml.SOL])
        self.assertEqual(rebalancer.left_behind(new, old), [DJT])
        self.assertEqual(rebalancer.left_behind(old, old), [])


POOLS = dict(tml.POOLS, **{DJT_POOL: {'dex': 'orca', 'a': DJT, 'b': tml.USDC, 'sa': 'DJT', 'sb': 'USDC',
                                      'price': 9.0, 'native': None}})
USD = dict(tml.USD, **{DJT: 9.0})
SWING = {'e2e-swing': dict(pool=tml.SOL_POOL, wallet='e2e-lp2', deposit=tml.USDC, residual=True, chain='solana')}


class Loop(tml.Fixture):
    """sol-swing over the fake chain: its own wallet, two pools, two pairs."""

    def setUp(self):
        for p in (mock.patch.dict(tml.POOLS, POOLS), mock.patch.dict(tml.USD, USD),
                  mock.patch.dict(tml.PROFILES, SWING)):
            p.start(); self.addCleanup(p.stop)
        self.drop_wallet()
        with db.cursor(commit=True) as cur:                              # before the fixture's config rows
            cur.execute("insert into wallets (id, chain, address, secret_env) values ('e2e-lp2', 'solana', %s, "
                        "'LPBOT_SOL2_KEY_PATH')", ('FogqBWLC4y94csrniURTbGrx7ff7jFa4e2qp1GsgyAmM',))
        super().setUp()
        self.chain.wallet[DJT] = 0.0
        with db.cursor(commit=True) as cur:
            cur.execute("update config set allow_swap = true, execute_dexes = '{raydium-clmm,orca}', "
                        "signer_env = '{\"LPBOT_ORCA_ADAPTIVE\": \"1\"}' where name = 'e2e-swing'")
        self.addCleanup(self.drop_wallet)
        band = {'band': 1.05, 'net_day_pct': 0.1, 'rebal_per_day': 0.1}
        for p in (mock.patch.object(rebalancer.dexes, 'pool', lambda dex, pool: dict(tml.pool_record_for(pool),
                                                                                       adaptive_fee=(dex == 'orca'))),
                  mock.patch.object(rebalancer.jupiter_api, 'jupiter_prices', lambda ms: {m: USD.get(m, 0.0) for m in ms}),
                  mock.patch.object(rebalancer, 'best_band_for', lambda pool, dex=None: dict(
                      band, price=POOLS[pool]['price'], record=tml.pool_record_for(pool), all_runs=[band])),
                  mock.patch.object(config, 'reload', self.reload)):
            p.start(); self.addCleanup(p.stop)

    @staticmethod
    def reload():
        """config.reload in a test: the repointed pool from the config row, the rest as patched."""
        with db.cursor() as cur:
            cur.execute('select dex, pool, pair_label from config where name = %s', (config.PROFILE,))
            r = cur.fetchone()
        config.DEX, config.POOL, config.PAIR_LABEL = r['dex'], r['pool'], r['pair_label']

    @contextlib.contextmanager
    def as_profile(self, name):
        with super().as_profile(name) as run:
            if name != 'e2e-swing':
                yield run
                return
            with db.cursor() as cur:                                     # its pool as the process loads it
                cur.execute("select dex, pool, pair_label from config where name = 'e2e-swing'")
                r = cur.fetchone()
            with mock.patch.multiple(config, EXECUTE_DEXES=('raydium-clmm', 'orca'), ALLOW_SWAP=True,
                                     SWING_POOLS=getattr(self, 'swing_pools', (DJT_POOL, tml.SOL_POOL)),
                                     SIGNER_ENV={'LPBOT_ORCA_ADAPTIVE': '1'}, DEX=r['dex'], POOL=r['pool'],
                                     PAIR_LABEL=r['pair_label']):
                yield run

    def drop_wallet(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from wallet_settle where wallet_id = 'e2e-lp2'")
            cur.execute("delete from snapshots where mint in (select mint from positions where config_name = 'e2e-swing')")
            cur.execute("delete from band_profile where mint in (select mint from positions where config_name = 'e2e-swing')")
            cur.execute("delete from positions where config_name = 'e2e-swing'")
            cur.execute("delete from capital_flows where profile = 'e2e-swing'")
            cur.execute("delete from events where profile = 'e2e-swing'")
            cur.execute("delete from config where name = 'e2e-swing'")
            cur.execute("delete from wallets where id = 'e2e-lp2'")

    @contextlib.contextmanager
    def pins(self, pools=(DJT_POOL, tml.SOL_POOL)):
        """The service environment's LPBOT_SWING_POOLS for the polls inside."""
        self.swing_pools = tuple(pools)
        try:
            yield
        finally:
            self.swing_pools = (DJT_POOL, tml.SOL_POOL)

    def held(self):
        return {p: v for p, v in self.chain.positions.items()}

    def move(self, dex, pool):
        """Write MIGRATE as swing.py does, then poll the profile."""
        with self.as_profile('e2e-swing') as run:
            (run / 'MIGRATE').write_text(f'{dex} {pool}\n')
        with self.pins():
            self.poll('e2e-swing')

    def test_the_swing_moves_sol_to_djt_and_back_selling_what_each_pair_leaves(self):
        self.chain.wallet.update({tml.USDC: 200.0, tml.SOL: 0.1})
        with self.pins():
            self.poll('e2e-swing')                                         # USDC arrives: 50/50 SOL/USDC, open
        self.assertIn(tml.SOL_POOL, self.chain.positions)
        self.move('orca', DJT_POOL)                                        # the bell
        self.assertNotIn(tml.SOL_POOL, self.chain.positions)
        self.assertIn(DJT_POOL, self.chain.positions)
        self.assertAlmostEqual(self.chain.wallet[tml.SOL], 0.05 + rebalancer.open_headroom('orca') + rebalancer.LEFT_BEHIND_GAS_MARGIN, places=6)  # reserve + rent
        pos = self.chain.positions[DJT_POOL]
        self.assertGreater(pos['a'] * 9.0 + pos['b'], 190.0)              # the capital is in the DJT band
        ev = self.events('e2e-swing')
        self.assertIn('LEFT_BEHIND_SOLD', ev)
        with self.as_profile('e2e-swing'):
            self.assertEqual(json.loads((rebalancer.STATE).read_text()).get('left_behind'), [])
        self.move('raydium-clmm', tml.SOL_POOL)                           # the close
        self.assertIn(tml.SOL_POOL, self.chain.positions)
        self.assertNotIn(DJT_POOL, self.chain.positions)
        self.assertLess(self.chain.wallet[DJT] * 9.0, rebalancer.SWEEP_MIN_USD)   # DJT sold, not idle
        with db.cursor() as cur:
            cur.execute("select pool from positions where config_name = 'e2e-swing' order by opened_at")
            self.assertEqual([r['pool'] for r in cur.fetchall()], [tml.SOL_POOL, DJT_POOL, tml.SOL_POOL])

    def test_an_operator_move_skips_the_gap_but_not_the_ceiling(self):
        self.chain.wallet.update({tml.USDC: 200.0, tml.SOL: 0.1})
        with self.pins():
            self.poll('e2e-swing')
        self.move('orca', DJT_POOL)                                        # right after the open: no gap
        self.assertIn(DJT_POOL, self.chain.positions)
        self.assertNotIn('rebalance_deferred', self.events('e2e-swing'))
        with self.as_profile('e2e-swing'):
            st_ = json.loads(rebalancer.STATE.read_text())
            st_['rebalance_times'] = [time.time()] * 6                     # at max_rebalances_per_day
            rebalancer.STATE.write_text(json.dumps(st_))
        self.move('raydium-clmm', tml.SOL_POOL)
        self.assertIn(DJT_POOL, self.chain.positions)                      # the ceiling halts it
        self.assertIn('BREAKER', self.events('e2e-swing'))

    def test_usdc_alone_waits_for_gas_without_a_failure(self):
        self.chain.wallet.update({tml.USDC: 200.0, tml.SOL: 0.0})
        for _ in range(4):
            with self.pins():
                self.poll('e2e-swing')
        self.assertEqual([c for c in self.chain.calls if '--execute' in c['args']], [])
        self.assertEqual(self.events('e2e-swing').count('gas_short'), 1)        # said once
        with self.as_profile('e2e-swing'):
            st_ = json.loads(rebalancer.STATE.read_text())
        self.assertEqual(st_.get('failures', 0), 0)
        self.chain.wallet[tml.SOL] = 0.06                                         # gas arrives
        with self.pins():
            self.poll('e2e-swing')
        self.assertIn(tml.SOL_POOL, self.chain.positions)

    def test_a_pool_outside_the_pins_is_refused_and_nothing_moves(self):
        self.chain.wallet.update({tml.USDC: 200.0, tml.SOL: 0.1})
        with self.pins():
            self.poll('e2e-swing')
        before = self.held()
        with self.as_profile('e2e-swing') as run:
            (run / 'MIGRATE').write_text(f'orca {DJT_POOL}\n')
        with self.pins(pools=(tml.SOL_POOL,)):
            self.poll('e2e-swing')
        self.assertEqual(self.held(), before)
        self.assertIn('migrate_refused', self.events('e2e-swing'))

    def test_with_nothing_held_the_move_still_happens_and_sells_first(self):
        self.chain.wallet.update({tml.SOL: 1.4, tml.USDC: 0.0})            # all of it in SOL, nothing held
        self.move('orca', DJT_POOL)
        self.assertIn(DJT_POOL, self.chain.positions)
        self.assertAlmostEqual(self.chain.wallet[tml.SOL], 0.05 + rebalancer.open_headroom('orca') + rebalancer.LEFT_BEHIND_GAS_MARGIN, places=6)
        with db.cursor() as cur:
            cur.execute("select kind from events where profile = 'e2e-swing' and kind = 'MIGRATE_REQUESTED'")
            self.assertEqual(len(cur.fetchall()), 1)

    def test_on_a_shared_wallet_nothing_left_behind_is_sold(self):
        with self.as_profile('e2e-mu') as run:
            state = {'left_behind': [tml.SOL]}
            calls = []
            with mock.patch.object(rebalancer, 'chain', lambda *a, **k: calls.append(a) or (None, None)), \
                    mock.patch.object(rebalancer, 'save', lambda s: None):
                self.assertFalse(rebalancer.sell_left_behind(state, force=True))
            self.assertEqual(calls, [])
            self.assertEqual(state['left_behind'], [tml.SOL])
        self.assertIn('left_behind_held', self.events('e2e-mu'))

    def test_an_unsold_leftover_waits_its_retry_time(self):
        with self.as_profile('e2e-swing'):
            state = {'left_behind': [DJT], 'left_behind_at': time.time()}
            with mock.patch.object(rebalancer, 'save', lambda s: None), \
                    mock.patch.object(rebalancer.wallets, 'read_balances', side_effect=AssertionError('read')):
                self.assertFalse(rebalancer.sell_left_behind(state))
            state['left_behind_at'] = time.time() - rebalancer.LEFT_BEHIND_RETRY_S - 1
            fail = mock.patch.object(rebalancer, 'chain', lambda *a, **k: (None, 'jupiter 429'))
            self.chain.wallet[DJT] = 5.0
            with mock.patch.object(rebalancer, 'save', lambda s: None), fail:
                self.assertFalse(rebalancer.sell_left_behind(state))
            self.assertEqual(state['left_behind'], [DJT])                 # kept for the next try
        self.assertIn('left_behind_unsold', self.events('e2e-swing'))

    def test_dust_leaves_the_list_without_a_swap(self):
        with self.as_profile('e2e-swing'):
            self.chain.wallet[DJT] = 0.01                                  # $0.09
            state = {'left_behind': [DJT]}
            with mock.patch.object(rebalancer, 'save', lambda s: None), \
                    mock.patch.object(rebalancer, 'chain', side_effect=AssertionError('swapped dust')):
                self.assertFalse(rebalancer.sell_left_behind(state, force=True))
            self.assertEqual(state['left_behind'], [])


class Gates(unittest.TestCase):
    """operator_target's gates for a pair-changing move."""

    def target(self, pool=DJT_POOL, allow=True, pins=(DJT_POOL,), adaptive_opt=True, adaptive=True):
        rec = dict(tml.pool_record_for(tml.SOL_POOL), address=pool, pair='DJT/USDC', adaptive_fee=adaptive,
                   token_a={'address': DJT, 'symbol': 'DJT', 'decimals': 6})
        told = []
        with mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: rec), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((tml.SOL, 'SOL'), (tml.USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: told.append(kw.get('reason'))), \
                mock.patch.multiple(config, ALLOW_SWAP=allow, SWING_POOLS=tuple(pins), EXECUTE_DEXES=('orca',),
                                    SIGNER_ENV={'LPBOT_ORCA_ADAPTIVE': '1'} if adaptive_opt else {}):
            return rebalancer.operator_target(['orca', pool]), told

    def test_a_pinned_pool_with_allow_swap_and_the_opt_in_is_a_target(self):
        t, told = self.target()
        self.assertEqual((t['address'], t['token_a']['address']), (DJT_POOL, DJT))
        self.assertEqual(told, [])

    def test_each_gate_refuses_alone(self):
        for kw, why in ((dict(allow=False), 'allow_swap is off'), (dict(pins=()), 'LPBOT_SWING_POOLS'),
                        (dict(adaptive_opt=False), 'adaptive-fee')):
            t, told = self.target(**kw)
            self.assertIsNone(t, kw)
            self.assertIn(why, told[0])

    def test_a_non_adaptive_pool_needs_no_opt_in(self):
        t, _ = self.target(adaptive_opt=False, adaptive=False)
        self.assertIsNotNone(t)

    def test_the_pins_come_from_the_environment_only(self):
        with mock.patch.dict('os.environ', {'LPBOT_SWING_POOLS': f'{DJT_POOL}, {tml.SOL_POOL}'}):
            import importlib
            importlib.reload(config)
            try:
                self.assertEqual(config.SWING_POOLS, (DJT_POOL, tml.SOL_POOL))
            finally:
                with mock.patch.dict('os.environ', {'LPBOT_SWING_POOLS': ''}):
                    importlib.reload(config)
        self.assertEqual(config.SWING_POOLS, ())


if __name__ == '__main__':
    unittest.main()


class SellLeftBehind(unittest.TestCase):
    """sell_left_behind with every collaborator stubbed: what it reads, what
    it sells, at which cap, and what stays on the list."""

    NATIVE = tml.SOL

    def run_it(self, todo, have, px=None, tokens=((DJT, 'DJT'), (tml.USDC, 'USDC')), shared=False, mints=(),
               answers=None, force=True, at=None):
        state = {'left_behind': list(todo)}
        if at is not None:
            state['left_behind_at'] = at
        calls, told = [], []
        answers = dict(answers or {})
        def chain(*a, **k):
            calls.append((a, json.loads(k['extra_env']['LPBOT_SLEEVE'])))
            return answers.get(a[1], ({'signature': f'sig-{a[1][:3]}'}, None))
        with mock.patch.object(rebalancer, 'claim_mints', lambda: (None if mints is None else list(mints), 'why', shared)), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: tokens), \
                mock.patch.object(rebalancer.wallets, 'read_balances', lambda *a: None if have is None else (have, 1)), \
                mock.patch.object(rebalancer.jupiter_api, 'jupiter_prices', lambda ms: px if px is not None else {}), \
                mock.patch.object(rebalancer, 'chain', chain), mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: told.append((ev, kw))), \
                mock.patch.multiple(config, CAPS=dict(config.CAPS, native_mint=self.NATIVE), DEX='orca',
                                    GAS_RESERVE_SOL=0.05):
            out = rebalancer.sell_left_behind(state, force=force)
        return out, state, calls, told

    def test_nothing_on_the_list_reads_nothing(self):
        out, _, calls, told = self.run_it([], None)
        self.assertIs(out, False); self.assertEqual((calls, told), ([], []))

    def test_the_retry_waits_exactly_its_time(self):
        now = time.time()
        out, st_, calls, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, force=False, at=now - 10)
        self.assertIs(out, False); self.assertEqual(calls, []); self.assertEqual(st_['left_behind_at'], now - 10)
        with mock.patch.object(rebalancer.time, 'time', return_value=now + rebalancer.LEFT_BEHIND_RETRY_S):
            out, _, calls, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, force=False, at=now)
        self.assertIs(out, True); self.assertEqual(len(calls), 1)
        out, _, calls, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, force=False)      # never tried: now
        self.assertIs(out, True)

    def test_a_shared_wallet_or_unknown_mints_sell_nothing(self):
        for kw in (dict(shared=True), dict(mints=None)):
            out, st_, calls, told = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, **kw)
            self.assertIs(out, False); self.assertEqual(calls, []); self.assertEqual(st_['left_behind'], [DJT])
            self.assertEqual(told[0][0], 'left_behind_held')

    def test_an_unreadable_balance_sells_nothing(self):
        out, st_, calls, told = self.run_it([DJT], None, {DJT: 9.0})
        self.assertIs(out, False); self.assertEqual(calls, [])
        self.assertEqual((told[0][0], told[0][1]['reason']), ('left_behind_unsold', 'balance unreadable'))

    def test_the_quote_is_the_stable_side_of_either_order(self):
        usdt = 'Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB'
        for tokens, quote in ((((usdt, 'USDT'), (tml.USDC, 'USDC')), tml.USDC),                 # both stable: B
                              (((DJT, 'DJT'), (tml.USDC, 'USDC')), tml.USDC),
                              (((tml.USDC, 'USDC'), (DJT, 'DJT')), tml.USDC),
                              (((tml.SOL, 'SOL'), (DJT, 'DJT')), DJT)):
            _, _, calls, _ = self.run_it([tml.MU], {tml.MU: 5.0}, {tml.MU: 9.0}, tokens=tokens)
            self.assertEqual(calls[0][0][:5], ('rebalance', tml.MU, quote, '0', '1000000'), tokens)

    def test_the_native_cap_keeps_reserve_rent_and_margin(self):
        have = 1.0
        _, st_, calls, _ = self.run_it([self.NATIVE], {self.NATIVE: have}, {self.NATIVE: 150.0},
                                       tokens=((DJT, 'DJT'), (tml.USDC, 'USDC')))
        cap = have - rebalancer.open_headroom('orca') - rebalancer.LEFT_BEHIND_GAS_MARGIN
        self.assertEqual(calls[0][1], {self.NATIVE: cap, tml.USDC: 0.0})
        self.assertEqual(calls[0][0][5:], ('--execute',))
        self.assertEqual(st_['left_behind'], [])

    def test_a_non_native_cap_is_the_whole_balance(self):
        _, _, calls, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, tokens=((tml.SOL, 'SOL'), (tml.USDC, 'USDC')))
        self.assertEqual(calls[0][1], {DJT: 5.0, tml.USDC: 0.0})

    def test_dust_unpriced_and_missing_balances(self):
        out, st_, calls, _ = self.run_it([DJT], {DJT: 0.1}, {DJT: 9.0})           # $0.90: dust
        self.assertEqual((out, st_['left_behind'], calls), (False, [], []))
        out, st_, calls, _ = self.run_it([DJT], {DJT: 0.5}, {})                   # no price: sold anyway
        self.assertEqual((out, len(calls)), (True, 1))
        min_amt = rebalancer.SWEEP_MIN_USD / 9.0
        out, _, calls, _ = self.run_it([DJT], {DJT: min_amt}, {DJT: 9.0})          # exactly the minimum: sold
        self.assertEqual(len(calls), 1)
        out, st_, calls, _ = self.run_it([DJT], {}, {DJT: 9.0})                   # nothing held: off the list
        self.assertEqual((out, st_['left_behind'], calls), (False, [], []))
        native_low = rebalancer.open_headroom('orca') + rebalancer.LEFT_BEHIND_GAS_MARGIN + 0.05
        out, st_, calls, _ = self.run_it([self.NATIVE], {self.NATIVE: native_low}, {self.NATIVE: 150.0})
        self.assertEqual((out, st_['left_behind'], calls), (False, [], []))       # only the reserve left

    def test_force_ignores_a_recent_try(self):
        out, _, calls, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, force=True, at=time.time())
        self.assertEqual((out, len(calls)), (True, 1))

    def test_nothing_held_and_no_price_is_off_the_list_without_a_swap(self):
        out, st_, calls, _ = self.run_it([DJT], {}, {})
        self.assertEqual((out, st_['left_behind'], calls), (False, [], []))

    def test_an_unpriced_sale_reports_no_dollar_value(self):
        _, _, _, told = self.run_it([DJT], {DJT: 0.5}, {})
        sold = [kw for ev, kw in told if ev == 'LEFT_BEHIND_SOLD'][0]
        self.assertEqual((sold['usd'], sold['amount'], sold['into']), (None, 0.5, tml.USDC))
        _, _, _, told = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0})
        self.assertEqual([kw for ev, kw in told if ev == 'LEFT_BEHIND_SOLD'][0]['usd'], 45.0)

    def test_a_partial_failure_keeps_only_the_unsold(self):
        answers = {DJT: (None, 'jupiter 429'), tml.MU: ({'noop': True}, None)}
        out, st_, calls, told = self.run_it([DJT, tml.MU, tml.JITO], {DJT: 5.0, tml.MU: 5.0, tml.JITO: 5.0},
                                            {DJT: 9.0, tml.MU: 9.0, tml.JITO: 9.0}, answers=answers)
        self.assertIs(out, True)                                                    # JITO sold
        self.assertEqual(st_['left_behind'], [DJT])
        # Jupiter sent nothing for DJT: the Orca fallback tries it too (2026-10-09), and fails here
        self.assertEqual([e for e, _ in told], ['swap_fallback', 'left_behind_unsold', 'LEFT_BEHIND_SOLD'])
        self.assertEqual(len([a for a, _ in calls if a[1] == DJT]), 2)               # Jupiter, then Orca
        out, st_, _, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, answers={DJT: ({'signature': 's'}, 'late')})
        self.assertEqual((out, st_['left_behind']), (False, [DJT]))                # a signature with an error

    def test_a_noop_only_is_no_sale(self):
        out, st_, _, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, answers={DJT: ({'noop': True}, None)})
        self.assertEqual((out, st_['left_behind']), (False, []))
        out, st_, _, _ = self.run_it([DJT], {DJT: 5.0}, {DJT: 9.0}, answers={DJT: ({}, None)})
        self.assertEqual((out, st_['left_behind']), (False, [DJT]))                # no answer: kept


class RepointLeftovers(unittest.TestCase):
    def test_new_leftovers_join_the_unsold_ones(self):
        state = {'left_behind': [tml.MU], 'left_behind_at': 5}
        seq = iter([((tml.SOL, 'SOL'), (tml.USDC, 'USDC')), ((DJT, 'DJT'), (tml.USDC, 'USDC'))])
        with mock.patch.object(rebalancer, 'pool_tokens', lambda: next(seq)), \
                mock.patch.object(rebalancer, 'repoint', lambda t: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None):
            rebalancer.repoint_with_leftovers(state, {'dex': 'orca'})
        self.assertEqual(state['left_behind'], sorted([tml.MU, tml.SOL]))
        self.assertNotIn('left_behind_at', state)                                  # the next sale is now


    def test_a_reopen_intent_moves_with_the_pool(self):
        # A failed open held through an FOMC window that ends at the NYSE
        # close: the swing repoints, and the intent must not halt the loop.
        seq = iter([((DJT, 'DJT'), (tml.USDC, 'USDC')), ((tml.SOL, 'SOL'), (tml.USDC, 'USDC'))])
        pending = {'mint': 'M', 'pool': 'DJTPOOL', 'dex': 'orca', 'band': 1.01, 'reason': 'x',
                   'started_at': time.time() - 60, 'withdraw_usd': 10.0, 'closed': True}      # fresh: not dropped
        state, saved, halted = {'pending_reopen': dict(pending)}, [], []

        def repoint(t):
            config.POOL, config.DEX = t['address'], t['dex']
        with mock.patch.object(rebalancer, 'pool_tokens', lambda: next(seq)), \
                mock.patch.object(rebalancer, 'repoint', repoint), \
                mock.patch.object(rebalancer, 'save', lambda s: saved.append(dict(s.get('pending_reopen') or {}))), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(config, 'POOL', 'DJTPOOL'), mock.patch.object(config, 'DEX', 'orca'):
            rebalancer.repoint_with_leftovers(state, {'dex': 'raydium-clmm', 'address': 'SOLPOOL'})
            self.assertEqual(saved[-1]['pool'], 'SOLPOOL')                         # saved, not only in memory
            reopened = []
            with mock.patch.object(rebalancer, 'halt', lambda why: halted.append(why)), \
                    mock.patch.object(rebalancer, 'reopen', lambda *a, **k: reopened.append(k.get('band'))):
                self.assertIs(rebalancer.resume_reopen(state), True)
        self.assertEqual(halted, []); self.assertEqual(reopened, [1.01])        # reopened on the new pool, its band
        self.assertEqual({k: v for k, v in state.get('pending_reopen', pending).items() if k not in ('pool', 'dex')},
                         {k: v for k, v in pending.items() if k not in ('pool', 'dex')})

    def test_no_intent_stays_no_intent(self):
        seq = iter([((DJT, 'DJT'), (tml.USDC, 'USDC')), ((tml.SOL, 'SOL'), (tml.USDC, 'USDC'))])
        state = {}
        with mock.patch.object(rebalancer, 'pool_tokens', lambda: next(seq)), \
                mock.patch.object(rebalancer, 'repoint', lambda t: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None):
            rebalancer.repoint_with_leftovers(state, {'dex': 'orca'})
        self.assertNotIn('pending_reopen', state)


class OperatorTarget(unittest.TestCase):
    """operator_target's other refusals and a same-pair move."""

    def call(self, spec, rec=None, tokens=((tml.SOL, 'SOL'), (tml.USDC, 'USDC')), raises=False, **cfg):
        rec = rec if rec is not None else dict(tml.pool_record_for(tml.SOL_POOL), adaptive_fee=False)
        told = []
        def pool(d, p):
            if raises:
                raise RuntimeError('api')
            return rec
        def toks():
            if tokens is None:
                raise RuntimeError('no record')
            return tokens
        conf = dict(ALLOW_SWAP=False, SWING_POOLS=(), EXECUTE_DEXES=('orca', 'raydium-clmm'), SIGNER_ENV={})
        conf.update(cfg)
        with mock.patch.object(rebalancer.dexes, 'pool', pool), mock.patch.object(rebalancer, 'pool_tokens', toks), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: told.append(kw.get('reason'))), \
                mock.patch.multiple(config, **conf):
            return rebalancer.operator_target(spec), told

    def test_a_same_pair_move_needs_no_allow_swap(self):
        t, told = self.call(['orca', tml.SOL_POOL])
        self.assertEqual((t['dex'], t['address'], told), ('orca', tml.SOL_POOL, []))

    def test_malformed_specs_and_venues(self):
        for spec, why in ((['orca'], 'must contain'), (['orca', tml.SOL_POOL, 'x'], 'must contain'),
                          (['orca', 'bad address'], 'must contain'), (['nowhere', tml.SOL_POOL], 'no armed signer'),
                          (['byreal', tml.SOL_POOL], 'no armed signer')):
            t, told = self.call(spec)
            self.assertIsNone(t, spec); self.assertIn(why, told[0])
        t, told = self.call(['jupiter', tml.SOL_POOL], EXECUTE_DEXES=('jupiter',))
        self.assertIsNone(t); self.assertIn('swap route', told[0])
        t, told = self.call(['nowhere', tml.SOL_POOL], EXECUTE_DEXES=('nowhere',))      # armed, no signer
        self.assertIsNone(t); self.assertIn('no armed signer', told[0])

    def test_an_unknown_or_unreadable_pool(self):
        for kw in (dict(rec={}), dict(raises=True)):
            t, told = self.call(['orca', tml.SOL_POOL], **kw)
            self.assertIsNone(t); self.assertIn('does not know', told[0])

    def test_an_adaptive_pool_without_the_opt_in_on_another_venue_is_fine(self):
        rec = dict(tml.pool_record_for(tml.SOL_POOL), adaptive_fee=True)
        t, _ = self.call(['raydium-clmm', tml.SOL_POOL], rec=rec)
        self.assertIsNotNone(t)

    def test_unknown_held_tokens_count_as_a_pair_change(self):
        t, told = self.call(['orca', tml.SOL_POOL], tokens=None)
        self.assertIsNone(t); self.assertIn('allow_swap is off', told[0])
        t, _ = self.call(['orca', tml.SOL_POOL], tokens=None, ALLOW_SWAP=True, SWING_POOLS=(tml.SOL_POOL,))
        self.assertIsNotNone(t)

    def test_the_target_mints_are_compared_normalised(self):
        evm = {'address': '0x3fe04a59ebd38cf06080a6f60a98d124eb59392a', 'pair': 'WETH/USDC',
               'token_a': {'address': '0x4200000000000000000000000000000000000006', 'symbol': 'WETH'},
               'token_b': {'address': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', 'symbol': 'USDC'}}
        held = (('0x4200000000000000000000000000000000000006', 'WETH'), ('0x833589fcd6edb6e08f4c7c32d4f71b54bda02913', 'USDC'))
        with mock.patch.object(rebalancer.guards, 'is_address', lambda s: True):
            t, told = self.call(['orca', evm['address']], rec=evm, tokens=held)
        self.assertIsNotNone(t, told)
        for side in ('token_a', 'token_b'):
            missing = dict(evm, **{side: None})
            with mock.patch.object(rebalancer.guards, 'is_address', lambda s: True):
                t, told = self.call(['orca', evm['address']], rec=missing, tokens=held)
            self.assertIsNone(t, side); self.assertIn('different pair', told[0])


class Gap(Loop):
    """A move the operator did not ask for still waits its gap."""

    def test_a_rebalance_trigger_right_after_a_switch_waits(self):
        self.chain.wallet.update({tml.USDC: 200.0, tml.SOL: 0.1})
        with self.pins():
            self.poll('e2e-swing')
        self.move('orca', DJT_POOL)
        with self.as_profile('e2e-swing') as run:
            (run / 'REBALANCE').touch()
        with self.pins():
            self.poll('e2e-swing')
        self.assertIn('rebalance_deferred', self.events('e2e-swing'))
        self.assertIn(DJT_POOL, self.chain.positions)


class GapRule(unittest.TestCase):
    """rebalance()'s gap: who waits it."""

    def go(self, **kw):
        told = []
        state = {'rebalance_times': [], 'calm_times': [], 'last_rebalance': time.time() - 10, 'failures': 0}
        with mock.patch.object(rebalancer, 'notify', lambda ev, **k: told.append(ev)), \
                mock.patch.object(rebalancer, 'chain', side_effect=AssertionError('no write past the gap')), \
                mock.patch.multiple(config, MIN_REBALANCE_GAP=3600, CALM_MIN_GAP=600, REGIME_ENABLED=kw.pop('regime')):
            try:
                rebalancer.rebalance(state, {'positionMint': 'M'}, 'test', **kw)
            except AssertionError:
                return 'moved'
        return 'deferred' if 'rebalance_deferred' in told else told

    def test_only_an_operator_move_or_a_regime_exit_skips_the_gap(self):
        self.assertEqual(self.go(regime=False), 'deferred')
        self.assertEqual(self.go(regime=False, exit_move=True), 'deferred')       # no regime: an exit waits
        self.assertEqual(self.go(regime=True, exit_move=True), 'moved')
        self.assertEqual(self.go(regime=False, operator=True), 'moved')
        self.assertEqual(self.go(regime=True, calm_move=True), 'deferred')


class TickMore(Tick):
    """tick()'s answers, its dry runs and main()."""

    def row(self, held=CLOSED[1], **kw):
        return dict(dict(ROW, held_pool=held, profile_enabled=True, wallet_id=None, position_pool=None), **kw)

    def test_tick_answers_and_writes_when_not_dry(self):
        (self.tmp / 'run' / 'tk-swing').mkdir(parents=True)                      # the run directory exists
        state = {}
        self.assertEqual(swing.tick(self.row(), state, ny(2026, 10, 5, 11, 0)), 'request')
        self.assertEqual(self.migrate(), f'orca {DJT_POOL}')
        self.assertEqual(state['request']['dex'], 'orca')
        self.assertEqual(state['request']['pool'], DJT_POOL)
        self.assertEqual(state['request']['from'], CLOSED[1])
        rows = [json.loads(x) for x in swing.FEED.read_text().splitlines()]
        self.assertEqual((rows[0]['market'], rows[0]['to'], rows[0]['held']), ('open', f'orca {DJT_POOL}', CLOSED[1]))
        self.assertEqual(swing.tick(self.row(held=DJT_POOL), state, ny(2026, 10, 5, 11, 1)), 'hold')
        self.assertEqual(swing.tick(self.row(held=DJT_POOL), {}, ny(2026, 10, 5, 20, 0)), 'request')
        rows = [json.loads(x) for x in swing.FEED.read_text().splitlines()]
        self.assertEqual(rows[-1]['market'], 'closed')

    def test_a_recentre_gap_after_arrival_raises_no_late_alert(self):
        # the 2026-10-08 false alarm: switch, position on the pool, then a re-centre closes it for a moment
        (self.tmp / 'run' / 'tk-swing').mkdir(parents=True)
        state = {}
        self.assertEqual(swing.tick(self.row(), state, ny(2026, 10, 5, 11, 0)), 'request')
        swing.tick(self.row(held=DJT_POOL, position_pool=DJT_POOL), state, ny(2026, 10, 5, 11, 4))
        self.assertTrue(state['request']['arrived'])
        swing.tick(self.row(held=DJT_POOL, position_pool=None), state, ny(2026, 10, 5, 15, 43))
        self.assertNotIn('SWING_LATE', [e for _, e in self.feed()])

    def test_a_switch_that_never_arrives_is_still_late(self):
        (self.tmp / 'run' / 'tk-swing').mkdir(parents=True)
        state = {}
        swing.tick(self.row(), state, ny(2026, 10, 5, 11, 0))
        swing.tick(self.row(held=DJT_POOL, position_pool=CLOSED[1]), state, ny(2026, 10, 5, 11, 4))
        swing.tick(self.row(held=DJT_POOL, position_pool=None), state, ny(2026, 10, 5, 11, 20))
        self.assertNotIn('arrived', state['request'])
        self.assertEqual([e for _, e in self.feed()].count('SWING_LATE'), 1)

    def test_a_dry_tick_answers_without_writing(self):
        state = {}
        self.assertEqual(swing.tick(self.row(), state, ny(2026, 10, 5, 11, 0), dry=True), 'request')
        self.assertEqual(swing.tick(self.row(held=DJT_POOL), state, ny(2026, 10, 5, 11, 0), dry=True), 'hold')
        self.assertEqual((state, self.migrate(), self.feed()), ({}, None, []))
        self.assertIsNone(swing.tick(self.row(profile_enabled=False), state, ny(2026, 10, 5, 11, 0)))

    def test_a_dry_run_past_the_calendar_says_nothing(self):
        swing.tick_all(ny(2028, 1, 3, 11, 0), dry=True)
        self.assertEqual(self.feed(), [])

    def test_a_request_without_a_time_is_old(self):
        self.assertEqual(swing.decide(ny(2026, 10, 5, 11, 0), self.row(), {'pool': DJT_POOL})[0], 'request')

    def test_a_leftover_exactly_at_its_deadline_is_not_yet_late(self):
        req = {'pool': OPEN[1], 'at': 1000.0}
        self.assertEqual(swing.audit(1000.0 + swing.LEFTOVER_DEADLINE_S, req, OPEN[1], ['SOL']), [])

    def test_a_null_left_behind_is_none(self):
        run = self.tmp / 'run' / 'tk-swing'
        run.mkdir(parents=True)
        (run / 'runtime.json').write_text(json.dumps({'left_behind': None}))
        self.assertEqual(swing.left_behind('tk-swing'), [])

    def test_main_once_is_one_dry_pass_and_the_service_loops(self):
        calls = []
        with mock.patch.object(swing, 'tick_all', lambda now, dry=False: calls.append(dry)):
            self.assertEqual(swing.main(['--once']), 0)
            self.assertEqual(swing.main(['--once', '--x']), 2)
            class Stop(Exception):
                pass
            def sleep(s):
                raise Stop()
            with mock.patch.object(swing.time, 'sleep', sleep), self.assertRaises(Stop):
                swing.main([])
        self.assertEqual(calls, [True, False])


class GasForOpen(unittest.TestCase):
    """gas_for_open on a pool that holds the native token: the reserve must
    be in the wallet before any write (every signer refuses below it)."""

    def ok(self, **bal):
        with mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05):
            return rebalancer.gas_for_open({}, bal)

    def test_the_wallets_sol_decides_when_it_is_read(self):
        self.assertFalse(self.ok(nativeSide='A', sol=0.01, balanceA=5.0, balanceB=100.0))
        self.assertTrue(self.ok(nativeSide='A', sol=0.05, balanceA=0.0, balanceB=100.0))

    def test_without_it_the_native_side_of_the_pool(self):
        self.assertTrue(self.ok(nativeSide='B', balanceA=0.0, balanceB=1.0))
        self.assertFalse(self.ok(nativeSide='B', balanceA=1.0, balanceB=0.0))
        self.assertTrue(self.ok(nativeSide='A', balanceA=1.0, balanceB=0.0))
        self.assertFalse(self.ok(nativeSide='A', balanceA=0.0, balanceB=1.0))
