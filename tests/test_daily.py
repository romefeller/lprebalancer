"""The daily line of the book: re-centres, fees, and value against a 50/50 hold.

Each figure is checked against a hand computation on a day built with
controlled timestamps; the report is sent once per closed day and never
blocks the loop.
"""
import datetime as dt
import unittest
from unittest import mock

from hypothesis import given, settings, HealthCheck, strategies as st

import _fixtures
import db
import rebalancer

DAY = dt.date(2026, 9, 27)
T = lambda h: dt.datetime.combine(DAY, dt.time(0), tzinfo=dt.timezone.utc) + dt.timedelta(hours=h)
POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'


def reset():
    _fixtures.reset_ledger()
    with db.cursor(commit=True) as cur:
        cur.execute('truncate payouts')


def snap(ts, equity, price, mint='M'):
    with db.cursor(commit=True) as cur:
        cur.execute('insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values (%s,%s,%s,%s,%s,%s)',
                    (ts, mint, price, True, '1', equity))


def opened(ts, mint):
    with db.cursor(commit=True) as cur:
        cur.execute('insert into positions (mint, pool, opened_at) values (%s,%s,%s)', (mint, POOL, ts))


def fee(ts, usd, mint='M'):
    with db.cursor(commit=True) as cur:
        cur.execute('insert into harvests (ts, mint, fee_a, fee_b, fee_usd, signature) values (%s,%s,0,%s,%s,%s)',
                    (ts, mint, usd, usd, f'h{ts.isoformat()}{usd}'))


def payout(ts, usd, kind='paid'):
    with db.cursor(commit=True) as cur:
        cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind) "
                    "values (%s,'sol-usdc','M','USDC','USDC',%s,%s,%s)", (ts, usd, usd, kind))


class Line(unittest.TestCase):
    def setUp(self):
        reset()
        snap(T(-1), 999.0, 1.0)                  # the day before: excluded
        snap(T(0.1), 240.0, 120.0)
        snap(T(12), 250.0, 130.0)
        snap(T(23.9), 238.0, 108.0)
        snap(T(24.5), 777.0, 2.0)                # the day after: excluded
        for i, h in enumerate((1, 5, 9)):
            opened(T(h), f'P{i}')
        opened(T(-2), 'OLD'); opened(T(25), 'NEW')
        fee(T(2), 0.5); fee(T(20), 0.7); fee(T(-3), 9.0); fee(T(26), 9.0)
        payout(T(3), 0.3); payout(T(4), 0.1, 'uncertain'); payout(T(5), 5.0, 'owed'); payout(T(6), 5.0, 'reinvested')
        payout(T(30), 7.0)
        self.line = db.daily_line(DAY)

    def test_counts_and_sums_cover_the_day_only(self):
        l = self.line
        self.assertEqual(l['recentres'], 3)
        self.assertAlmostEqual(l['fees_usd'], 1.2)
        self.assertAlmostEqual(l['fees_per_recentre_usd'], 0.4)
        self.assertAlmostEqual(l['paid_out_usd'], 0.4)            # paid and uncertain; owed and reinvested stay in the book

    def test_earned_is_the_accrual_basis_of_the_book_s_today(self):
        l = self.line
        start = dt.datetime.combine(DAY, dt.time(0), tzinfo=dt.timezone.utc)
        self.assertAlmostEqual(l['fees_earned_usd'], round(float(db.fees_between(start, start + dt.timedelta(days=1))['usd']), 4))
        self.assertEqual(l['idle_redeploys'], 0)

    def test_idle_redeploys_are_counted_apart(self):
        reset()
        snap(T(1), 100.0, 100.0)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at, open_reason) values ('I1', 'P', %s, 'deploy $81.54 idle'), "
                        "('R1', 'P', %s, 'regime HOT: +/-4.0%% -> +/-2.5%%')", (T(2), T(3)))
        l = db.daily_line(DAY)
        self.assertEqual((l['recentres'], l['idle_redeploys']), (2, 1))

    def test_open_and_close_are_the_first_and_last_snapshot(self):
        l = self.line
        self.assertEqual((l['equity_open'], l['price_open'], l['equity_close'], l['price_close']), (240.0, 120.0, 238.0, 108.0))

    def test_value_against_usdc_and_the_hold(self):
        l = self.line
        self.assertAlmostEqual(l['value_change_usd'], 238.0 + 0.4 - 240.0)
        hold = 240.0 * (0.5 + 0.5 * 108.0 / 120.0)
        self.assertAlmostEqual(l['hold_50_50_usd'], round(hold, 4))
        self.assertAlmostEqual(l['vs_hold_usd'], round(238.4 - hold, 4))

    def test_a_past_day_is_complete(self):
        self.assertTrue(self.line['complete'])
        self.assertEqual(self.line['day'], '2026-09-27')

    def test_a_day_without_equity_is_none(self):
        self.assertIsNone(db.daily_line(dt.date(2026, 1, 1)))

    def test_a_day_without_recentres_has_no_per_recentre_figure(self):
        reset()
        snap(T(1), 100.0, 100.0); snap(T(2), 100.0, 100.0)
        l = db.daily_line(DAY)
        self.assertEqual(l['recentres'], 0); self.assertIsNone(l['fees_per_recentre_usd'])
        self.assertEqual(l['vs_hold_usd'], 0.0)

    def test_snapshots_at_the_same_instant_keep_their_order(self):
        reset()
        snap(T(1), 100.0, 100.0); snap(T(1), 101.0, 101.0)
        l = db.daily_line(DAY)
        self.assertEqual((l['equity_open'], l['equity_close']), (100.0, 101.0))
        self.assertEqual((l['price_open'], l['price_close']), (100.0, 101.0))

    def test_a_price_under_one_still_has_a_hold(self):
        reset()
        snap(T(1), 100.0, 0.5); snap(T(2), 100.0, 1.0)
        self.assertAlmostEqual(db.daily_line(DAY)['hold_50_50_usd'], 150.0)

    def test_midnight_belongs_to_the_day_it_starts(self):
        reset()
        snap(T(0), 100.0, 100.0); snap(T(23), 100.0, 100.0)
        fee(T(0), 0.5); fee(T(24), 9.0)
        payout(T(0), 0.2); payout(T(24), 9.0)
        opened(T(0), 'A'); opened(T(24), 'B')
        l = db.daily_line(DAY)
        self.assertEqual((l['fees_usd'], l['paid_out_usd'], l['recentres']), (0.5, 0.2, 1))

    def test_a_snapshot_without_equity_is_skipped(self):
        reset()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values (%s,'M',50,true,'1',null)", (T(0.5),))
        snap(T(1), 100.0, 100.0); snap(T(2), 110.0, 110.0)
        l = db.daily_line(DAY)
        self.assertEqual((l['equity_open'], l['price_open']), (100.0, 100.0))
        reset()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values (%s,'M',50,true,'1',null)", (T(0.5),))
        self.assertIsNone(db.daily_line(DAY))

    def test_the_default_is_two_days_newest_first(self):
        reset()
        today = db.now().date()
        for k in range(4):
            start = dt.datetime.combine(today - dt.timedelta(days=k), dt.time(0), tzinfo=dt.timezone.utc)
            snap(start + dt.timedelta(minutes=1), 100.0 + k, 100.0)
        self.assertEqual([l['day'] for l in db.daily_lines()], [today.isoformat(), (today - dt.timedelta(days=1)).isoformat()])
        self.assertEqual(len(db.daily_lines(3)), 3)

    def test_a_zero_price_gives_no_hold(self):
        reset()
        snap(T(1), 100.0, 0.0); snap(T(2), 100.0, 5.0)
        l = db.daily_line(DAY)
        self.assertIsNone(l['hold_50_50_usd']); self.assertIsNone(l['vs_hold_usd'])

    def test_the_running_day_is_not_complete(self):
        reset()
        today = db.now().date()
        start = dt.datetime.combine(today, dt.time(0), tzinfo=dt.timezone.utc)
        snap(start + dt.timedelta(minutes=1), 100.0, 100.0)
        lines = db.daily_lines(2)
        self.assertEqual(lines[0]['day'], today.isoformat()); self.assertFalse(lines[0]['complete'])
        self.assertEqual(len(lines), 1)                               # yesterday has no data: skipped

    def test_the_book_carries_the_daily_lines_and_survives_their_failure(self):
        self.assertIn('daily', db.stats())
        with mock.patch.object(db, 'daily_lines', side_effect=RuntimeError('x')):
            self.assertIsNone(db._daily_or_none())
        with mock.patch.object(db, 'daily_lines', lambda n: ['L'] * n):
            self.assertEqual(db._daily_or_none(), ['L', 'L'])


class Property(unittest.TestCase):
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.lists(st.tuples(st.floats(0, 23.99), st.floats(100, 400), st.floats(50, 200)), min_size=1, max_size=8),
           st.lists(st.floats(0, 2), max_size=5), st.integers(0, 6))
    def test_value_minus_hold_is_the_definition(self, snaps, fees, n_open):
        reset()
        snaps = sorted(snaps, key=lambda x: x[0])
        for h, e, p in snaps:
            snap(T(h), e, p)
        for i, f in enumerate(fees):
            fee(T(1 + i), f)
        for i in range(n_open):
            opened(T(2 + i), f'Q{i}')
        l = db.daily_line(DAY)
        first = min(snaps, key=lambda x: x[0]); last = max(snaps, key=lambda x: x[0])
        if sum(1 for s in snaps if s[0] == first[0]) > 1 or sum(1 for s in snaps if s[0] == last[0]) > 1:
            return                                                   # ties are ordered by insert: covered above
        hold = first[1] * (0.5 + 0.5 * last[2] / first[2])
        self.assertAlmostEqual(l['vs_hold_usd'], round(last[1] - hold, 4), places=3)
        self.assertEqual(l['recentres'], n_open)
        self.assertAlmostEqual(l['fees_usd'], round(sum(round(f, 6) for f in fees), 4), places=3)


class Report(unittest.TestCase):
    def run_it(self, state, line, today=DAY + dt.timedelta(days=1)):
        seen, events, saved = [], [], []
        class FakeDT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return dt.datetime.combine(today, dt.time(0, 10), tzinfo=dt.timezone.utc)
        with mock.patch.object(rebalancer, 'datetime', FakeDT), \
                mock.patch.object(rebalancer.db, 'daily_line', lambda d: (seen.append(('asked', d)) or line)), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(rebalancer, 'save', lambda s: saved.append(dict(s))), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append((ev, kw))):
            out = rebalancer.daily_report(state)
        return out, seen, events, saved

    LINE = {'day': '2026-09-27', 'recentres': 9, 'fees_usd': 1.98, 'vs_hold_usd': -0.34}

    def test_reports_yesterday_once(self):
        state = {}
        out, seen, events, saved = self.run_it(state, dict(self.LINE))
        self.assertEqual(out['day'], '2026-09-27')
        self.assertEqual(seen[0], ('asked', DAY))
        self.assertEqual(seen[1][0], 'DAILY'); self.assertEqual(seen[1][1]['recentres'], 9)
        self.assertEqual(events[0][0], 'DAILY'); self.assertIn('9 re-centres', events[0][1]); self.assertIn('-0.34', events[0][1])
        self.assertEqual(state['last_daily'], '2026-09-27'); self.assertEqual(saved[-1]['last_daily'], '2026-09-27')
        out2, seen2, events2, _ = self.run_it(state, dict(self.LINE))
        self.assertIsNone(out2); self.assertEqual(seen2, []); self.assertEqual(events2, [])

    def test_a_day_without_data_is_marked_and_not_sent(self):
        state = {}
        out, seen, events, _ = self.run_it(state, None)
        self.assertIsNone(out); self.assertEqual(events, [])
        self.assertEqual([e for e in seen if e[0] != 'asked'], [])
        self.assertEqual(state['last_daily'], '2026-09-27')

    def test_a_failure_is_reported_and_swallowed(self):
        seen = []
        with mock.patch.object(rebalancer.db, 'daily_line', side_effect=RuntimeError('db down')), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
            self.assertIsNone(rebalancer.daily_report({}))
        self.assertEqual(seen, ['daily_report_failed'])


if __name__ == '__main__':
    unittest.main()
