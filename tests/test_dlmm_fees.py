"""Meteora DLMM fee counters (venues/meteora_dlmm/fees.py, sql/043).

The per-bin fee fraction, the band income of a uniform-value position, the
table's write and 14-day window, and that nothing in the venue board can
read a DLMM sample (it has its own table, never fee_growth).
"""
import json
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (paths, test database)
import db
from venues.meteora_dlmm import fees as dlmm

Q64 = 2 ** 64


def row(i, price, x, y, fx, fy, supply=None):
    """A sample bin [id, price, x, y, supply, fx, fy]; supply = value * 2^64 by default."""
    if supply is None:
        supply = int((price * x + y) * Q64)
    return [i, price, str(x), str(y), str(supply), str(fx), str(fy)]


class BinFeeFraction(unittest.TestCase):

    def test_y_fee_is_fraction_of_value(self):
        # 1,000 raw Y of value; 1 raw Y of fees per unit value * 2^64 -> 0.1%
        a = row(5, 0.1, 0, 1000, 0, 0)
        b = row(5, 0.1, 0, 1000, 0, Q64 // 1000)
        self.assertAlmostEqual(dlmm.bin_fee_fraction(a, b), 0.001, places=12)

    def test_x_fee_priced_in_y(self):
        a = row(5, 0.1, 10000, 0, 0, 0)
        b = row(5, 0.1, 10000, 0, Q64 // 100, 0)       # 0.01 raw X per unit value
        self.assertAlmostEqual(dlmm.bin_fee_fraction(a, b), 0.001, places=12)

    def test_counter_wrap(self):
        a = row(1, 1.0, 0, 10, 0, 2 ** 128 - Q64 // 1000)
        b = row(1, 1.0, 0, 10, 0, Q64 // 1000)
        self.assertAlmostEqual(dlmm.bin_fee_fraction(a, b), 0.002, places=12)

    def test_empty_bin_is_none(self):
        self.assertIsNone(dlmm.bin_fee_fraction(row(1, 1.0, 0, 0, 0, 0, supply=0), row(1, 1.0, 0, 0, 0, 5, supply=0)))

    def test_different_bins_refused(self):
        with self.assertRaises(ValueError):
            dlmm.bin_fee_fraction(row(1, 1.0, 0, 1, 0, 0), row(2, 1.0, 0, 1, 0, 0))

    @settings(max_examples=300, deadline=None)
    @given(st.integers(0, 2 ** 128 - 1), st.integers(0, 2 ** 80), st.integers(0, 2 ** 128 - 1),
           st.integers(0, 2 ** 80), st.floats(1e-6, 1e6))
    def test_never_negative_and_additive(self, fx0, dx, fy0, dy, price):
        a = row(0, price, 1, 1, fx0, fy0)
        mid = row(0, price, 1, 1, (fx0 + dx // 2) % 2 ** 128, (fy0 + dy // 2) % 2 ** 128)
        b = row(0, price, 1, 1, (fx0 + dx) % 2 ** 128, (fy0 + dy) % 2 ** 128)
        whole = dlmm.bin_fee_fraction(a, b)
        self.assertGreaterEqual(whole, 0)
        parts = dlmm.bin_fee_fraction(a, mid) + dlmm.bin_fee_fraction(mid, b)
        self.assertAlmostEqual(whole, parts, delta=1e-9 * max(1.0, whole))


def sample(ts, active, bins):
    return {'ts': ts, 'active_id': active, 'bins': bins}


class BandIncome(unittest.TestCase):

    def bins(self, fy):
        # bin step 1%: ids -3..3 at prices 1.01^i
        return [row(i, 1.01 ** i, 0, 1000, 0, fy(i)) for i in range(-3, 4)]

    def test_uniform_earnings(self):
        a = sample(0, 0, self.bins(lambda i: 0))
        b = sample(86400, 0, self.bins(lambda i: Q64 // 1000))     # 0.1% per bin in a day
        self.assertAlmostEqual(dlmm.band_income(a, b, 1.025), 0.1, places=9)

    def test_only_band_bins_count(self):
        a = sample(0, 0, self.bins(lambda i: 0))
        # the active bin earned 0.3%, its neighbours nothing, the far bins a lot
        b = sample(86400, 0, self.bins(lambda i: {0: 3 * Q64 // 1000}.get(i, 0) if abs(i) < 3 else Q64))
        self.assertAlmostEqual(dlmm.band_income(a, b, 1.005), 0.3, places=9)        # active bin only
        self.assertAlmostEqual(dlmm.band_income(a, b, 1.0101), 0.1, places=9)       # 3 bins: 0.3/3

    def test_unusable(self):
        a = sample(0, 0, self.bins(lambda i: 0))
        self.assertIsNone(dlmm.band_income(a, a, 1.01))                              # no time
        self.assertIsNone(dlmm.band_income(a, sample(60, 0, []), 1.01))              # no common bin
        self.assertIsNone(dlmm.band_income(sample(0, 99, a['bins']), a | {'ts': 60}, 1.01))   # no active bin
        self.assertIsNone(dlmm.band_income(a, a | {'ts': 60}, 1.0))                  # no band

    @settings(max_examples=200, deadline=None)
    @given(st.lists(st.integers(0, 2 ** 70), min_size=7, max_size=7), st.floats(1.001, 1.05),
           st.integers(60, 86400))
    def test_within_bin_range(self, gains, band, secs):
        a = sample(0, 0, self.bins(lambda i: 0))
        b = sample(secs, 0, self.bins(lambda i: gains[i + 3]))
        inc = dlmm.band_income(a, b, band)
        fr = [g / Q64 / secs * 86400 * 100 for g in gains]
        self.assertGreaterEqual(inc, min(fr) - 1e-9)
        self.assertLessEqual(inc, max(fr) + 1e-9)


class Table(unittest.TestCase):

    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from dlmm_fee_samples where pool like 'TEST%'")

    tearDown = setUp

    def s(self, pool='TESTpool'):
        return {'pool': pool, 'active_id': -5708, 'bin_step': 4, 'base_fee_pct': 0.04, 'dynamic_fee_pct': 0.0412,
                'dec_x': 9, 'dec_y': 6, 'bins': [row(-5708, 0.102, 23075916, 9675301501, 0, 0)]}

    def test_record_round_trip(self):
        dlmm.record(self.s())
        got = dlmm._samples('TESTpool')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['bins'][0][0], -5708)
        self.assertEqual(int(got[0]['bins'][0][4]), int(self.s()['bins'][0][4]))      # big ints exact

    def test_window_kept(self):
        dlmm.record(self.s())
        with db.cursor(commit=True) as cur:
            cur.execute("update dlmm_fee_samples set ts = now() - interval '15 days' where pool = 'TESTpool'")
        dlmm.record(self.s())
        self.assertEqual(len(dlmm._samples('TESTpool')), 1)

    def test_read_sample_refuses_other_pool(self):
        out = mock.Mock(returncode=0, stdout=json.dumps({'pool': 'OTHER', 'bins': []}), stderr='')
        with mock.patch.object(dlmm.subprocess, 'run', return_value=out):
            with self.assertRaises(RuntimeError):
                dlmm.read_sample('TESTpool')

    def test_read_sample_failure_raises(self):
        out = mock.Mock(returncode=1, stdout='', stderr='ERROR: 429')
        with mock.patch.object(dlmm.subprocess, 'run', return_value=out):
            with self.assertRaises(RuntimeError):
                dlmm.read_sample('TESTpool')

    def test_board_never_sees_dlmm(self):
        # the venue board reads fee_growth only, for venues with a known counter layout
        from venues import solana_state
        self.assertNotIn('meteora-dlmm', solana_state.FEE_LAYOUT_DEXES)
        dlmm.record(self.s())
        with db.cursor() as cur:
            cur.execute("select count(*) n from fee_growth where pool = 'TESTpool'")
            self.assertEqual(cur.fetchone()['n'], 0)


if __name__ == '__main__':
    unittest.main()
