"""The ledger, on an isolated database: a whole position lifecycle, and the
invariant the stats promise — TOTAL fees only ever go up."""
import time
import unittest
from decimal import Decimal

import _fixtures
from _fixtures import ADAPTIVE_POOL, BTC_QUOTE_POOL, LIVE_POOL
import db


class Config(unittest.TestCase):
    def setUp(self):
        _fixtures.ensure_profile()

    def test_exactly_one_active_profile(self):
        _fixtures.ensure_profile('other', pool='x' * 44)
        with db.cursor() as cur:
            cur.execute('select count(*) n from config where active')
            self.assertEqual(cur.fetchone()['n'], 1)
        db.activate('sol-usdc')
        self.assertEqual(db.load_config()['name'], 'sol-usdc')
        with self.assertRaises(SystemExit):
            db.activate('nope')

    def test_set_param_validates_the_column(self):
        row = db.set_param('sol-usdc', 'capital_usd', '200')
        self.assertEqual(row['capital_usd'], Decimal('200'))
        row = db.set_param('sol-usdc', 'bands', '1.04, 1.10')
        self.assertEqual([float(b) for b in row['bands']], [1.04, 1.10])
        with self.assertRaises(SystemExit):
            db.set_param('sol-usdc', 'no_such_column', 1)
        with self.assertRaises(SystemExit):
            db.set_param('sol-usdc', 'name', 'x')
        db.set_param('sol-usdc', 'capital_usd', '190')
        db.set_param('sol-usdc', 'bands', '1.03,1.05,1.08,1.12,1.18,1.25,1.40')

    def test_sanity_constraint_refuses_nonsense(self):
        import psycopg2
        for k, v in (('poll_seconds', 5), ('max_usd', 10), ('side_cap_fraction', 0.2),
                     ('swap_cost_bps', 900), ('gas_reserve_sol', -1)):
            with self.assertRaises(psycopg2.errors.CheckViolation, msg=k):
                db.set_param('sol-usdc', k, v)

    def test_add_reads_the_pair_from_orca_and_refuses_adaptive_fee(self):
        with self.assertRaises(SystemExit) as cm:
            db.add('zec', ADAPTIVE_POOL)
        self.assertIn('adaptive-fee', str(cm.exception))
        with self.assertRaises(SystemExit):
            db.add('bogus', 'notapool')
        row = db.add('sol-cbbtc', BTC_QUOTE_POOL, capital_usd=100)
        self.assertEqual(row['pair_label'], 'SOL/cbBTC')
        self.assertEqual((row['token_a'], row['token_b']), ('SOL', 'cbBTC'))
        self.assertEqual(row['max_usd'], Decimal('140'))
        self.assertFalse(row['active'])
        self.assertGreater(row['_pool']['tvl_usd'], 0)
        with db.cursor(commit=True) as cur:
            cur.execute("delete from config where name in ('sol-cbbtc', 'other')")


class Ledger(unittest.TestCase):
    def setUp(self):
        _fixtures.ensure_profile()
        _fixtures.reset_ledger()

    def snap(self, mint, accrued_a, accrued_b, accrued_usd, price=100.0, wallet=80.0, pos=160.0):
        db.snapshot(mint, price, True, '1', accrued_a, accrued_b, accrued_usd, wallet, pos)

    def test_empty_ledger_reports_zeros_not_errors(self):
        s = db.stats()
        self.assertEqual(s['fees_total_usd'], 0.0)
        self.assertIsNone(s['fees_per_day_usd']); self.assertIsNone(s['apr_pct'])
        self.assertIsNone(s['equity_usd']); self.assertEqual(s['positions_opened'], 0)

    def test_rate_is_suppressed_for_the_first_hour(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 'sig', 190, 'test')
        self.snap('m1', 0.001, 0.1, 0.2)
        self.assertIsNone(db.stats()['fees_per_day_usd'])

    def test_lifecycle_total_is_monotonic_across_a_rebalance(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 'sig1', 190, 'test')
        self.snap('m1', 0.0, 0.0, 0.0)
        self.snap('m1', 0.001, 0.10, 0.20)
        s1 = db.stats()
        self.assertAlmostEqual(s1['fees_unrealised_usd'], 0.20)
        self.assertAlmostEqual(s1['fees_total_usd'], 0.20)
        self.assertAlmostEqual(s1['fees_today_usd'], 0.20)
        self.assertEqual(s1['fees_realised_usd'], 0.0)

        # harvest and close: the accrual becomes realised, the position's own
        # counter resets, and the total must not move.
        db.record_harvest('m1', 0.001, 0.10, 0.20, 'hsig')
        db.close_position('m1', 'csig', None)
        # Between the close and the new position's first snapshot, the latest
        # snapshot still belongs to the dead position. Its accrual is now in
        # the wallet and counted as realised; it must not count twice. This is
        # the exact state in which a $0.26 harvest was reported as $0.51.
        mid = db.stats()
        self.assertAlmostEqual(mid['fees_unrealised_usd'], 0.0)
        self.assertAlmostEqual(mid['fees_total_usd'], 0.20)
        self.assertAlmostEqual(mid['fees_today_usd'], 0.20)
        db.open_position('m2', LIVE_POOL, 'SOL/USDC', 96, 106, 5, 'sig2', 190, 'rebalance')
        self.snap('m2', 0.0, 0.0, 0.0)
        s2 = db.stats()
        self.assertAlmostEqual(s2['fees_realised_usd'], 0.20)
        self.assertAlmostEqual(s2['fees_unrealised_usd'], 0.0)
        self.assertAlmostEqual(s2['fees_total_usd'], 0.20)
        self.assertGreaterEqual(s2['fees_total_usd'], s1['fees_total_usd'])
        self.assertEqual(s2['positions_opened'], 2); self.assertEqual(s2['positions_open_now'], 1)
        self.assertEqual(s2['harvests'], 1)

        # the new position earns: total climbs from where it was
        self.snap('m2', 0.0005, 0.05, 0.10)
        s3 = db.stats()
        self.assertAlmostEqual(s3['fees_total_usd'], 0.30)
        self.assertAlmostEqual(s3['fees_total_a'], 0.0015)
        self.assertAlmostEqual(s3['fees_total_b'], 0.15)
        # Today's earnings include both positions, without double-counting harvests.
        self.assertAlmostEqual(s3['fees_today_usd'], 0.30)

    def test_token_amounts_survive_exactly(self):
        db.record_harvest('m', Decimal('0.000265000000000001'), Decimal('0.123456'), 0.05, 's')
        with db.cursor() as cur:
            cur.execute('select fee_a, fee_b from harvests')
            r = cur.fetchone()
        self.assertEqual(r['fee_a'], Decimal('0.000265000000000001'))
        self.assertEqual(r['fee_b'], Decimal('0.123456'))

    def test_equity_and_pnl(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 's', 190, 't')
        self.snap('m1', 0, 0, 0, wallet=80.0, pos=160.0)
        self.snap('m1', 0, 0, 0.1, wallet=80.5, pos=161.0)
        s = db.stats()
        self.assertAlmostEqual(s['equity_start_usd'], 240.0)
        self.assertAlmostEqual(s['equity_usd'], 241.6)
        self.assertAlmostEqual(s['pnl_usd'], 1.6)

    def test_harvest_moves_fees_without_creating_equity(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 's', 190, 't')
        self.snap('m1', 0.001, 0.1, 0.2, wallet=80, pos=160)
        before = db.stats()
        db.record_harvest('m1', 0.001, 0.1, 0.2, 'h')
        self.snap('m1', 0, 0, 0, wallet=80.2, pos=160)
        after = db.stats()
        self.assertEqual(before['equity_usd'], after['equity_usd'])
        self.assertEqual(before['fees_total_usd'], after['fees_total_usd'])
        self.assertEqual(after['pnl_usd'], 0)

    def test_recovered_close_preserves_the_confirmed_withdrawal(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 's', 190, 't')
        db.close_position('m1', 'confirmed', 191)
        with db.cursor() as cur:
            cur.execute("select * from positions where mint='m1'")
            before = dict(cur.fetchone())
        db.close_position('m1', None, 190)
        with db.cursor() as cur:
            cur.execute("select * from positions where mint='m1'")
            self.assertEqual(dict(cur.fetchone()), before)

    def test_today_excludes_inherited_fees_even_after_close_and_new_open(self):
        from datetime import timedelta
        from unittest.mock import patch
        midnight = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with patch.object(db, 'now', return_value=midnight - timedelta(hours=2)):
            db.open_position('old', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 's', 190, 't')
        with patch.object(db, 'now', return_value=midnight - timedelta(minutes=1)):
            self.snap('old', .005, .15, .65)
        with patch.object(db, 'now', return_value=midnight + timedelta(seconds=1)):
            db.record_harvest('old', .005, .17, .67, 'h')
            self.snap('old', 0, 0, 0)
            db.close_position('old', 'c', 190)
            db.open_position('new', LIVE_POOL, 'SOL/USDC', 95, 105, 1, 's2', 190, 'calm: tight band')
        with patch.object(db, 'now', return_value=midnight + timedelta(seconds=2)):
            self.snap('new', .001, .01, .11)
        with patch.object(db, 'now', return_value=midnight + timedelta(seconds=3)):
            self.assertAlmostEqual(db.stats()['fees_today_usd'], .13)
            daily = {r['day']: r for r in db.daily()}
            self.assertAlmostEqual(float(daily[midnight.date()]['fee_usd']), .13)

    def test_equity_repair_is_idempotent_and_preserves_unknown_values(self):
        from pathlib import Path
        self.snap('m', 0, .65, .65, wallet=38, pos=210)
        self.snap('m', 0, .65, .65, wallet=None, pos=210)
        with db.cursor(commit=True) as cur:
            cur.execute('update snapshots set equity_usd = wallet_usd + position_usd')
        sql = (Path(db.__file__).parent / 'sql' / '007_fee_accounting.sql').read_text()
        for _ in range(2):
            with db.cursor(commit=True) as cur:
                cur.execute(sql)
        with db.cursor() as cur:
            cur.execute('select equity_usd from snapshots order by id')
            rows = cur.fetchall()
        self.assertEqual(rows[0]['equity_usd'], Decimal('248.65'))
        self.assertIsNone(rows[1]['equity_usd'])

    def test_events_count_failures_and_rebands(self):
        db.event('REBAND', '5% -> 12%'); db.event('open_failed', 'x'); db.event('BREAKER', 'y')
        s = db.stats()
        self.assertEqual(s['rebands'], 1); self.assertEqual(s['failures'], 1)

    def test_daily_and_history(self):
        db.open_position('m1', LIVE_POOL, 'SOL/USDC', 95, 105, 5, 's', 190, 't')
        # the first snapshot lands after some accrual; that accrual still counts
        self.snap('m1', 0.0002, 0.02, 0.05); self.snap('m1', 0.001, 0.1, 0.25)
        db.record_harvest('m1', 0.001, 0.1, 0.25, 'h')
        d = db.daily()
        self.assertEqual(len(d), 1)
        # earned today: 0.25 accrued, then harvested. Once, not twice.
        self.assertAlmostEqual(float(d[0]['fee_usd']), 0.25)
        self.assertAlmostEqual(float(d[0]['fee_a']), 0.001)
        # a second position opened the same day adds its own accrual
        db.close_position('m1', 'c', None)
        db.open_position('m2', LIVE_POOL, 'SOL/USDC', 96, 106, 5, 's2', 190, 'r')
        self.snap('m2', 0.0001, 0.01, 0.03)
        self.assertAlmostEqual(float(db.daily()[0]['fee_usd']), 0.28)
        self.assertEqual(len(db.history()), 3)




class Board(unittest.TestCase):
    def setUp(self):
        _fixtures.ensure_profile()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate scan_pools, scan_runs')

    def rows(self):
        return [
            {'dex': 'meteora-dlmm', 'kind': 'dlmm', 'address': 'M1', 'pair': 'SOL/USDC', 'fee': 0.00048,
             'tvl_usd': 6.9e6, 'volume_24h_usd': 5.5e7, 'c_pool': 18.5, 'band': 1.08, 'band_pct': 8.0,
             'net_day_pct': 0.582, 'rebal_per_day': 0.14, 'p25_net_day': 0.29, 'worst_net_day': -0.1,
             'share_positive': 0.88, 'p_survive_168h': 0.38, 'executable': False, 'screen_ok': True,
             'screen_reason': 'both tokens are majors',
             'token_a': {'address': 'a', 'symbol': 'SOL'}, 'token_b': {'address': 'b', 'symbol': 'USDC'}},
            {'dex': 'orca', 'kind': 'clmm', 'address': 'O1', 'pair': 'SOL/USDC', 'fee': 0.0004,
             'tvl_usd': 2.6e7, 'volume_24h_usd': 1.6e8, 'c_pool': 21.1, 'band': 1.18, 'band_pct': 18.0,
             'net_day_pct': 0.386, 'rebal_per_day': 0.03, 'p25_net_day': 0.08, 'worst_net_day': -0.2,
             'share_positive': 0.81, 'p_survive_168h': 0.72, 'executable': True, 'screen_ok': True,
             'screen_reason': 'both tokens are majors',
             'token_a': {'address': 'a', 'symbol': 'SOL'}, 'token_b': {'address': 'b', 'symbol': 'USDC'}},
            {'dex': 'orca', 'kind': 'clmm', 'address': 'O2', 'pair': 'ZEC/USDC', 'tvl_usd': 2.8e6,
             'volume_24h_usd': 1.7e7, 'net_day_pct': None, 'skipped': 'adaptive-fee pool'},
        ]

    def test_record_and_read_back_in_rank_order(self):
        run_id = db.record_scan('sol-usdc', ('orca', 'meteora-dlmm'), self.rows(),
                                {'byreal': 'timeout'}, 123.4, listed=3)
        run, rows = db.latest_scan()
        self.assertEqual(run['id'], run_id)
        self.assertEqual(run['pools_scored'], 2)
        self.assertEqual(run['pools_listed'], 3)
        self.assertEqual(run['errors'], {'byreal': 'timeout'})
        self.assertEqual(run['best']['address'], 'M1')
        self.assertEqual([r['address'] for r in rows], ['M1', 'O1', 'O2'])
        self.assertEqual(rows[0]['rank'], 1)
        self.assertTrue(rows[1]['executable']); self.assertFalse(rows[0]['executable'])
        self.assertEqual(rows[2]['skipped'], 'adaptive-fee pool')
        self.assertAlmostEqual(rows[0]['net_day_pct'], 0.582)
        self.assertEqual(rows[0]['token_b']['symbol'], 'USDC')      # detail survives

    def test_stale_board_is_reported_as_empty(self):
        db.record_scan('sol-usdc', ('orca',), self.rows(), {}, 1.0, listed=3)
        with db.cursor(commit=True) as cur:
            cur.execute("update scan_runs set ts = now() - interval '2 days'")
        run, rows = db.latest_scan(max_age_seconds=3600)
        self.assertIsNotNone(run); self.assertEqual(rows, [])
        self.assertEqual(db.latest_scan(), (db.latest_scan()[0], db.latest_scan()[1]))

    def test_scan_history_per_pool(self):
        db.record_scan('sol-usdc', ('orca',), self.rows(), {}, 1.0, listed=3)
        db.record_scan('sol-usdc', ('orca',), self.rows(), {}, 1.0, listed=3)
        self.assertEqual(len(db.scan_history('O1')), 2)
        self.assertEqual(db.scan_history('nope'), [])

    def test_repoint_moves_the_profile(self):
        row = db.repoint('sol-usdc', 'meteora-dlmm', 'M1', 'SOL/USDC', 'SOL', 'USDC')
        self.assertEqual((row['dex'], row['pool']), ('meteora-dlmm', 'M1'))
        with self.assertRaises(SystemExit):
            db.repoint('nope', 'orca', 'x', 'a/b', 'a', 'b')
        db.repoint('sol-usdc', 'orca', LIVE_POOL, 'SOL/USDC', 'SOL', 'USDC')

    def test_positions_carry_their_dex(self):
        _fixtures.reset_ledger()
        db.open_position('mintX', 'M1', 'SOL/USDC', 100, 120, 8, 'sig', 190, 'test',
                         config_name='sol-usdc', dex='meteora-dlmm')
        with db.cursor() as cur:
            cur.execute("select dex from positions where mint = 'mintX'")
            self.assertEqual(cur.fetchone()['dex'], 'meteora-dlmm')
        _fixtures.reset_ledger()




class ByPool(unittest.TestCase):
    def test_fees_and_time_are_split_by_pool_and_dex(self):
        _fixtures.ensure_profile()
        _fixtures.reset_ledger()
        db.open_position('o1', 'POOL_O', 'SOL/USDC', 100, 120, 8, 's', 190, 'x', dex='orca')
        db.snapshot('o1', 110, True, 1, 0.0, 0.0, 0.5, 50, 190)
        db.record_harvest('o1', 0, 1.0, 1.0, 'h1')
        db.close_position('o1', 'c', None)
        db.open_position('m1', 'POOL_M', 'SOL/USDC', 100, 120, 8, 's', 194, 'x', dex='meteora-dlmm')
        db.snapshot('m1', 110, True, 1, 0.0, 0.0, 0.25, 40, 194)
        db.snapshot('m1', 130, False, 1, 0.0, 0.0, 0.30, 40, 194)
        rows = db.by_pool()
        by = {r['dex']: r for r in rows}
        self.assertEqual(set(by), {'orca', 'meteora-dlmm'})
        self.assertEqual(by['orca']['open_now'], 0)
        self.assertAlmostEqual(by['orca']['fees_usd'], 1.0)          # realised only, closed
        self.assertEqual(by['meteora-dlmm']['open_now'], 1)
        self.assertAlmostEqual(by['meteora-dlmm']['fees_usd'], 0.30)  # latest unrealised
        self.assertEqual(by['meteora-dlmm']['in_range_pct'], 50.0)
        self.assertEqual(rows[0]['dex'], 'meteora-dlmm')               # most recent first
        s = db.stats()
        self.assertEqual(s['position_dex'], 'meteora-dlmm')
        self.assertEqual(s['dexes_held'], ['meteora-dlmm', 'orca'])
        self.assertEqual(len(s['by_pool']), 2)
        d = db.daily()
        self.assertIn('meteora-dlmm SOL/USDC', d[0]['pools'])
        _fixtures.reset_ledger()

    def test_trailing_rate_is_the_rise_of_fees_to_date(self):
        _fixtures.ensure_profile()
        _fixtures.reset_ledger()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, pair_label, opened_at, deposit_usd, dex) "
                        "values ('t1', 'P', 'SOL/USDC', now() - interval '5 hours', 190, 'orca')")
            # accrual 0 -> 0.30 over 5h, then a harvest turns it realised: the
            # rate must not move on the harvest and must not double count it
            cur.execute("insert into snapshots (ts, mint, accrued_usd, in_range) values "
                        "(now() - interval '5 hours', 't1', 0.0, true), "
                        "(now() - interval '3 hours', 't1', 0.12, true), "
                        "(now() - interval '1 hour', 't1', 0.24, true)")
            cur.execute("insert into harvests (ts, mint, fee_usd) values (now() - interval '30 minutes', 't1', 0.30)")
            cur.execute("insert into snapshots (ts, mint, accrued_usd, in_range) values (now(), 't1', 0.0, true)")
        t = db.trailing_rate(6)
        self.assertAlmostEqual(t['fees_usd'], 0.30, places=3)
        self.assertAlmostEqual(t['fees_per_day_usd'], 0.30 / (5 / 24), delta=0.05)
        t24 = db.trailing_rate(24)
        self.assertAlmostEqual(t24['fees_usd'], 0.30, places=3)
        # a window with a single point has no rate
        self.assertIsNone(db.trailing_rate(0))
        s = db.stats()
        self.assertIsNotNone(s['fees_per_day_6h_usd'])
        _fixtures.reset_ledger()




class Season(unittest.TestCase):
    def test_profile_round_trips_and_outlook_reads_the_hour(self):
        _fixtures.ensure_profile()
        with db.cursor(commit=True) as cur:
            cur.execute('truncate scan_pools, scan_runs')
        prof = [0.7] * 12 + [1.3] * 12
        db.record_scan('sol-usdc', ('orca',), [], {}, 1.0, listed=0, season=prof)
        self.assertEqual(db.season(), prof)
        o = db.season_outlook(prof, hour=3)
        self.assertTrue(o['quiet']); self.assertEqual(o['now_x'], 0.7)
        self.assertAlmostEqual(o['next_x'], 0.7)
        o = db.season_outlook(prof, hour=11)
        self.assertTrue(o['quiet']); self.assertAlmostEqual(o['next_x'], 1.3)   # the peak is coming
        o = db.season_outlook(prof, hour=15)
        self.assertFalse(o['quiet'])
        self.assertEqual(o['peak_hour_utc'], 12); self.assertEqual(o['trough_hour_utc'], 0)
        self.assertIsNone(db.season_outlook(None)); self.assertIsNone(db.season_outlook([1.0] * 5))
        s = db.stats()
        self.assertIsNotNone(s['season'])
        with db.cursor(commit=True) as cur:
            cur.execute('truncate scan_pools, scan_runs')
        self.assertIsNone(db.season())


if __name__ == '__main__':
    unittest.main(verbosity=2)


class Forecasts(unittest.TestCase):
    """The survival record: forecasts are stored with the poll and checked
    against what the same position did next."""

    def setUp(self):
        _fixtures.ensure_profile()
        _fixtures.reset_ledger()

    def test_snapshot_keeps_the_forecast_and_stats_shows_it(self):
        db.open_position('M', LIVE_POOL, 'SOL/USDC', 95.0, 105.0, 5.0, 'sig', 190.0, 'test')
        db.snapshot('M', 100.0, True, 1, 0, 0, 0, 50.0, 190.0,
                    forecast={'p_exit_6h': 0.1, 'p_exit_24h_regime': 0.3, 'p_exit_24h': 0.4,
                              'p_exit_72h': 0.6, 'position': 0.2})
        b = db.stats()['band']
        self.assertEqual(b['p_exit_6h'], 0.1); self.assertEqual(b['p_exit_24h'], 0.3)   # regime figure preferred
        self.assertEqual(b['p_exit_72h'], 0.6); self.assertEqual(b['position'], 0.2)
        self.assertTrue(b['in_range']); self.assertIsNotNone(b['hours_alive'])
        self.assertAlmostEqual(db.position_open_price('M'), (95.0 * 105.0) ** 0.5)
        self.assertIsNotNone(db.position_opened('M'))

    def test_calibration_counts_exits_within_the_horizon_and_censors_the_rest(self):
        from datetime import timedelta
        t0 = db.now() - timedelta(hours=30)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at) values ('A', 'p', %s), ('B', 'p', %s)", (t0, t0))
            # A: forecast 0.9 at t0, out of range 2h later -> an exit within 6h
            cur.execute("insert into snapshots (ts, mint, price, in_range, p_exit_6h) values (%s,'A',100,true,0.9)", (t0,))
            cur.execute("insert into snapshots (ts, mint, price, in_range) values (%s,'A',110,false)", (t0 + timedelta(hours=2),))
            # B: forecast 0.1 at t0, still inside 8h later -> survived the 6h horizon
            cur.execute("insert into snapshots (ts, mint, price, in_range, p_exit_6h) values (%s,'B',100,true,0.1)", (t0,))
            cur.execute("insert into snapshots (ts, mint, price, in_range) values (%s,'B',101,true)", (t0 + timedelta(hours=8),))
            # B again: a forecast 20 minutes ago cannot be resolved yet -> censored
            cur.execute("insert into snapshots (ts, mint, price, in_range, p_exit_6h) values (%s,'B',100,true,0.5)",
                        (db.now() - timedelta(minutes=20),))
        r = db.forecasts(horizons=(6,))[6]
        self.assertEqual(r['n'], 2)
        rates = {b['p_mean']: b['exit_rate'] for b in r['buckets']}
        self.assertEqual(rates[0.9], 1.0); self.assertEqual(rates[0.1], 0.0)
        self.assertAlmostEqual(r['brier'], (0.01 + 0.01) / 2, places=6)
