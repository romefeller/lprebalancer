"""The regime's touch forecasts are recorded, resolved from the tape, and scored."""
import time
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import db          # noqa: E402
import rebalancer  # noqa: E402


class Touches(unittest.TestCase):
    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate touch_forecasts, tape5')

    def put_forecast(self, minutes_ago, price=100.0, probs=((1.0, 0.2), (2.0, 0.05)), choice=1.01):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into touch_forecasts (ts, pool, price, horizon_min, threshold, choice, probs) "
                        "values (now() - make_interval(mins => %s), 'P', %s, 120, 0.2, %s, %s::jsonb)",
                        (minutes_ago, price, choice, __import__('json').dumps([list(x) for x in probs])))

    def put_bars(self, start_minutes_ago, n, high, low):
        t0 = int(time.time()) - start_minutes_ago * 60
        with db.cursor(commit=True) as cur:
            for i in range(n):
                cur.execute("insert into tape5 values ('P', %s, 100, %s, %s, 100, 1) on conflict do nothing",
                            (t0 + i * 300, high, low))

    def test_resolves_touches_per_width_from_highs_and_lows(self):
        self.put_forecast(200)
        self.put_bars(200, 24, high=101.5, low=99.5)          # +1.5% high: +/-1% touched, +/-2% not
        self.assertEqual(db.resolve_touch_forecasts('P'), 1)
        with db.cursor() as cur:
            cur.execute("select resolved, touched from touch_forecasts")
            r = cur.fetchone()
        self.assertTrue(r['resolved']); self.assertEqual(r['touched'], [True, False])

    def test_waits_for_the_horizon_and_for_the_tape(self):
        self.put_forecast(60)                                  # horizon not passed
        self.put_forecast(200)                                 # passed, but no bars yet
        self.assertEqual(db.resolve_touch_forecasts('P'), 0)
        self.put_bars(200, 10, high=100.1, low=99.9)           # only 10 of 24 bars
        self.assertEqual(db.resolve_touch_forecasts('P'), 0)

    def test_calibration_said_and_saw(self):
        for _ in range(4):
            self.put_forecast(200, probs=((1.0, 0.25), (2.0, 0.05)))
        self.put_bars(200, 24, high=101.5, low=99.9)
        db.resolve_touch_forecasts('P')
        cal = db.touch_calibration(7, 'P')
        self.assertEqual(cal['widths']['1']['n'], 4)
        self.assertAlmostEqual(cal['widths']['1']['said'], 0.25); self.assertAlmostEqual(cal['widths']['1']['saw'], 1.0)
        self.assertAlmostEqual(cal['widths']['2']['saw'], 0.0)
        self.assertEqual(cal['chosen']['n'], 4)                # choice 1.01 = the 1% row

    def test_the_loop_records_at_most_every_ten_minutes_and_skips_stale_views(self):
        rv = {'probs': [[1.0, 0.2]], 'horizon_minutes': 120, 'threshold': 0.2, 'choice': 1.01}
        calls = []
        with mock.patch.object(rebalancer.db, 'record_touch_forecast', lambda *a: calls.append(a)), \
                mock.patch.object(rebalancer.db, 'resolve_touch_forecasts', lambda p: 0), \
                mock.patch.object(rebalancer.db, 'touch_calibration', lambda d, p: {'chosen': {'n': 0}}), \
                mock.patch.object(rebalancer, 'save', lambda s: None):
            st = {}
            rebalancer.track_touch_forecasts(st, rv, {'price': 100.0, 'whirlpool': 'P'})
            rebalancer.track_touch_forecasts(st, rv, {'price': 100.0, 'whirlpool': 'P'})
            rebalancer.track_touch_forecasts({}, dict(rv, stale=True), {'price': 100.0, 'whirlpool': 'P'})
        self.assertEqual(len(calls), 1)
