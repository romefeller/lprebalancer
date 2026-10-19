"""The fee moments (sql/057): the band's concentration, the hour's stats, the
GREAT/BAD label, the per-poll read, the spell state machine, the risk_profile
columns and the 7-day reference."""
import datetime as dt
import math
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import db
from venues import solana_state
import lp.books
import lp.moments
import lp.paths
import lp.pauses
from lp import tuning

Q64 = 2 ** 64
NOW = 1_800_000_000.0
POOL = 'MomentPool1111111111111111111111111111111111'


def sample(g0, g1, sp=1.0, ts=NOW, liquidity=None):
    return {'id': int(ts), 'g0': g0, 'g1': g1, 'sqrt_price': int(sp * Q64), 'dec_a': 9, 'dec_b': 6,
            'ts': dt.datetime.fromtimestamp(ts, dt.UTC), 'liquidity': liquidity}


def bars(t0, n=36, drift=0.0005, vol=1000.0):
    ts = np.array([t0 + 300 * i for i in range(n)], dtype=float)
    close = 100.0 * np.exp(np.cumsum(np.full(n, drift)))
    return ts, close, close * 1.001, close * 0.999, close, np.full(n, vol)


class Concentration(unittest.TestCase):
    def test_narrow_band_is_two_over_its_half_width(self):
        c = lp.moments.concentration(100.0, 98.75, 101.25)
        self.assertAlmostEqual(c, 2 / 0.0125, delta=0.5)
        self.assertGreater(lp.moments.concentration(100.0, 99, 101), c)        # narrower earns more
        self.assertEqual(lp.moments.concentration(100.0, 101, 102), 1.0)        # outside: no band figure
        self.assertEqual(lp.moments.concentration(100.0, None, None), 1.0)
        self.assertEqual(lp.moments.concentration(0, 99, 101), 1.0)


class HourStats(unittest.TestCase):
    def test_volume_range_drift_of_the_last_hour(self):
        b = bars(NOW - 3600 * 3)
        hs = lp.moments.hour_stats(b, NOW)
        self.assertEqual(hs['vol_1h_usd'], 12000.0)
        self.assertAlmostEqual(hs['drift_1h_pct'], (math.exp(12 * 0.0005) - 1) * 100, places=3)
        self.assertAlmostEqual(hs['range_1h_pct'], (b[2][-1] / b[3][-12] - 1) * 100, places=3)
        self.assertIsNone(lp.moments.hour_stats(None, NOW))
        self.assertIsNone(lp.moments.hour_stats(bars(NOW - 3600 * 3, n=5), NOW))          # too few bars
        self.assertIsNone(lp.moments.hour_stats(bars(NOW - 3600 * 9, n=36), NOW))         # none in the hour


class Classify(unittest.TestCase):
    def test_labels(self):
        c = lp.moments.classify
        self.assertEqual(c(0.09, 0.07, 2.0, 0.3, 0.6), 'GREAT')
        self.assertEqual(c(0.07, 0.07, 2.0, 0.3, 0.6), 'GREAT')             # at the p90 counts
        self.assertEqual(c(0.05, 0.07, 0.6, 0.7, 0.6), 'BAD')
        self.assertEqual(c(0.09, 0.07, 0.6, 0.7, 0.6), 'GREAT')             # GREAT wins a tie
        self.assertIsNone(c(0.05, 0.07, 0.9, 0.7, 0.6))                      # ratio not bad enough
        self.assertIsNone(c(0.05, 0.07, 0.6, 0.5, 0.6))                      # sigma not high enough
        self.assertIsNone(c(None, 0.07, None, 0.7, 0.6)); self.assertIsNone(c(0.09, None, 0.6, 0.7, None))
        self.assertIsNone(c(0.09, 0.0, 2.0, 0.3, 0.6))                       # an empty reference labels nothing
        self.assertEqual(tuning.MOMENT_BAD_RATIO, 0.8)


class HourView(unittest.TestCase):
    def test_fee_pct_uses_the_counters_and_the_band(self):
        first, last = sample(0, 0, 0.5, NOW - 3500, 10 ** 12), sample(2 * Q64, 3 * Q64, 0.5, NOW, 10 ** 12)
        y = solana_state.fee_yield(first, last, 120.0, 1.0)
        status = {'whirlpool': POOL, 'price': 120.0, 'quoteUsd': 1.0, 'lowerPrice': 118.5, 'upperPrice': 121.5,
                  'liquidity': 4 * 10 ** 11}
        with mock.patch.object(db, 'fee_state_span', lambda pool, hours: (first, last, 3500)), \
                mock.patch.object(db, 'tape_load', lambda pool, t0: bars(NOW - 3 * 3600)), \
                mock.patch.object(lp.pauses, 'fee_loss_recent', lambda pool, a, b, hours: 1.7):
            hv = lp.moments.hour_view(status, {}, now=NOW)
        self.assertAlmostEqual(hv['fee_pct_1h'], y * lp.moments.concentration(120.0, 118.5, 121.5) * 100 * 3600 / 3500, places=4)
        self.assertEqual(hv['l_share'], 0.4)
        self.assertEqual(hv['fee_loss_1h'], 1.7)
        self.assertEqual(hv['vol_1h_usd'], 12000.0)

    def test_short_coverage_or_no_quote_leaves_none_and_a_failure_is_told(self):
        first, last = sample(0, 0, 0.5, NOW - 600), sample(Q64, Q64, 0.5, NOW)
        status = {'whirlpool': POOL, 'price': 120.0, 'quoteUsd': 1.0}
        with mock.patch.object(db, 'fee_state_span', lambda pool, hours: (first, last, 600)), \
                mock.patch.object(db, 'tape_load', lambda pool, t0: None), \
                mock.patch.object(lp.pauses, 'fee_loss_recent', lambda pool, a, b, hours: None):
            hv = lp.moments.hour_view(status, {}, now=NOW)
        self.assertEqual(hv, {k: None for k in hv})
        told = []
        with mock.patch.object(db, 'fee_state_span', mock.Mock(side_effect=RuntimeError('db'))), \
                mock.patch.object(lp.books, 'notify', lambda k, **kw: told.append(k)):
            hv = lp.moments.hour_view(status, {}, now=NOW)
        self.assertEqual(told, ['moment_unread']); self.assertIsNone(hv['fee_pct_1h'])


class Spells(unittest.TestCase):
    def polls(self, seq, state=None):
        """seq: (fee_pct_1h, fee_loss_3h, sigma) per poll with a fixed reference."""
        state = {} if state is None else state
        events, books_sent, labels = [], [], []
        ref = {'fee_med': 0.04, 'fee_p90': 0.07, 'sigma_med': 0.3, 'sigma_p90': 0.6, 'n': 500, 'days': 7}
        status = {'whirlpool': POOL, 'price': 100.0, 'quoteUsd': 1.0}
        with mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(db, 'event', lambda k, d: events.append((k, d))), \
                mock.patch.object(db, 'moment_reference', lambda pool, days=7: ref), \
                mock.patch.object(lp.books, 'notify_book', lambda k, **kw: books_sent.append((k, kw))):
            for fee, r3, sig in seq:
                hv = {'fee_pct_1h': fee, 'fee_loss_1h': None, 'l_share': 0.4, 'vol_1h_usd': 30000.0,
                      'range_1h_pct': 0.8, 'drift_1h_pct': 0.1}
                with mock.patch.object(lp.moments, 'hour_view', lambda status, rv: dict(hv)):
                    rv = {'mode': 'WARM', 'held_pct': 1.25, 'choice_pct': 1.25, 'fee_loss_3h': r3}
                    labels.append(lp.moments.record(state, status, rv, {'rms_1h_pct': sig}))
        return labels, events, books_sent, state, rv

    def test_a_great_spell_starts_once_and_ends_once(self):
        labels, events, sent, state, rv = self.polls([(0.05, 1.5, 0.3), (0.08, 1.5, 0.3), (0.09, 1.6, 0.3), (0.05, 1.5, 0.3)])
        self.assertEqual(labels, [None, 'GREAT', 'GREAT', None])
        self.assertEqual([k for k, _ in events], ['FEE_MOMENT', 'FEE_MOMENT'])
        self.assertIn('GREAT spell: fees 0.08%/h', events[0][1]); self.assertIn('GREAT spell ended after', events[1][1])
        self.assertEqual([(k, kw['phase'], kw['kind']) for k, kw in sent], [('FEE_MOMENT', 'start', 'GREAT'), ('FEE_MOMENT', 'end', 'GREAT')])
        self.assertEqual(sent[0][1]['fee_pct_1h'], 0.08); self.assertEqual(sent[0][1]['ref']['fee_p90'], 0.07)
        self.assertEqual(sent[0][1]['sigma_1h_pct'], 0.3); self.assertEqual(sent[1][1]['next'], None)
        self.assertNotIn('moment', state)
        self.assertEqual((rv['moment'], rv['fee_pct_1h'], rv['moment_ref']['fee_p90']), (None, 0.05, 0.07))

    def test_great_to_bad_ends_one_spell_and_starts_the_next(self):
        labels, events, sent, state, _ = self.polls([(0.08, 1.5, 0.3), (0.02, 0.5, 0.9)])
        self.assertEqual(labels, ['GREAT', 'BAD'])
        self.assertEqual([kw['phase'] for _, kw in sent], ['start', 'end', 'start'])
        self.assertEqual(sent[1][1]['next'], 'BAD'); self.assertEqual(state['moment']['kind'], 'BAD')

    def test_no_view_and_a_failed_reference(self):
        self.assertIsNone(lp.moments.record({}, {'price': 1}, None, {}))
        told = []
        with mock.patch.object(lp.moments, 'hour_view', lambda status, rv: {'fee_pct_1h': 0.1, 'fee_loss_1h': None,
                                                                            'l_share': None, 'vol_1h_usd': None,
                                                                            'range_1h_pct': None, 'drift_1h_pct': None}), \
                mock.patch.object(db, 'moment_reference', mock.Mock(side_effect=RuntimeError('db'))), \
                mock.patch.object(lp.books, 'notify', lambda k, **kw: told.append(k)), \
                mock.patch.object(lp.paths, 'save', lambda s: None):
            rv = {'mode': 'CALM'}
            self.assertIsNone(lp.moments.record({}, {'whirlpool': POOL, 'price': 1, 'quoteUsd': 1}, rv, {}))
        self.assertEqual(told, ['moment_unread']); self.assertEqual(rv['moment_ref'], {}); self.assertIsNone(rv['moment'])

    def test_risk_row_carries_the_hour(self):
        v = {'fee_pct_1h': 0.081, 'fee_loss_1h': 2.1, 'vol_1h_usd': 31000.0, 'range_1h_pct': 0.8, 'drift_1h_pct': -0.2,
             'l_share': 0.41, 'moment': 'GREAT'}
        row = db.risk_row(v, {}, {})
        for k, x in v.items():
            self.assertEqual(row[k], x); self.assertIn(k, db.RISK_COLUMNS)


class Reference(unittest.TestCase):
    """moment_reference against the test database."""

    def setUp(self):
        db._MOMENT_REF.clear()
        with db.cursor(commit=True) as cur:
            cur.execute('delete from risk_profile where pool = %s', (POOL,))

    tearDown = setUp

    def test_percentiles_count_and_cache(self):
        with db.cursor(commit=True) as cur:
            for i in range(1, 11):
                cur.execute('insert into risk_profile (ts, pool, price, fee_pct_1h, rms_1h_pct) values (now() - make_interval(hours => %s), %s, 100, %s, %s)',
                            (i, POOL, i / 100, i / 10))
            cur.execute('insert into risk_profile (ts, pool, price, fee_pct_1h) values (now() - interval \'9 days\', %s, 100, 9.9)', (POOL,))
            cur.execute('insert into risk_profile (ts, pool, price, rms_1h_pct) values (now(), %s, 100, 9.9)', (POOL,))
        ref = db.moment_reference(POOL, days=7)
        self.assertEqual(ref['n'], 10)
        self.assertAlmostEqual(ref['fee_med'], 0.055, places=5); self.assertAlmostEqual(ref['fee_p90'], 0.091, places=5)
        self.assertAlmostEqual(ref['sigma_p90'], 0.91, places=5)
        with db.cursor(commit=True) as cur:
            cur.execute('delete from risk_profile where pool = %s', (POOL,))
        self.assertEqual(db.moment_reference(POOL, days=7)['n'], 10)          # cached
        db._MOMENT_REF.clear()
        self.assertEqual(db.moment_reference(POOL, days=7), {})               # empty: no reference


if __name__ == '__main__':
    unittest.main()
