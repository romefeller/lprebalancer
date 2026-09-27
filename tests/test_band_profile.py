"""band_profile: one row per band, written at a harvest and at the rebalance.

The row is the learning set for an automatic mode switch (HOT pools to WARM):
how long a band survived, the market it lived in, its mode, what it earned.
Every figure is derived from risk_profile, snapshots and harvests, so these
tests build a band's history with controlled timestamps and check each figure
against an independent computation, and that no write ever makes a second row.
"""
import datetime as dt
import math
import unittest
from unittest import mock

from hypothesis import given, settings, HealthCheck, strategies as st

import _fixtures
import db
import rebalancer

MINT, OTHER = 'BANDMINT111', 'OTHERMINT22'
POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'
T0 = dt.datetime(2026, 9, 27, 10, 0, tzinfo=dt.timezone.utc)
H = lambda h: T0 + dt.timedelta(hours=h)


def reset():
    _fixtures.reset_ledger()
    with db.cursor(commit=True) as cur:
        cur.execute('truncate band_profile, risk_profile')


def position(mint=MINT, opened=T0, closed=None):
    with db.cursor(commit=True) as cur:
        cur.execute('insert into positions (mint, pool, opened_at, closed_at, band_pct, dex) values (%s,%s,%s,%s,%s,%s)',
                    (mint, POOL, opened, closed, 1.5, 'raydium-clmm'))


def risk(ts, mode, sigma, velocity, instability=0.05, arch=2.0, arch_p=0.9, mint=MINT, **extra):
    row = {'ts': ts, 'pool': POOL, 'mint': mint, 'price': 120.0, 'mode': mode, 'sigma_5m_pct': sigma,
           'velocity': velocity, 'instability': instability, 'arch_lm_24h': arch, 'arch_lm_p_24h': arch_p, **extra}
    with db.cursor(commit=True) as cur:
        cur.execute(f"insert into risk_profile ({', '.join(row)}) values ({', '.join(['%s'] * len(row))})",
                    list(row.values()))


def snap(ts, in_range, mint=MINT):
    with db.cursor(commit=True) as cur:
        cur.execute('insert into snapshots (ts, mint, price, in_range, liquidity) values (%s,%s,%s,%s,%s)',
                    (ts, mint, 120.0, in_range, '1'))


def harvest(usd, a=0.001, b=0.1, mint=MINT):
    db.record_harvest(mint, a, b, usd, f'sig-{usd}-{mint}')


def rows():
    with db.cursor() as cur:
        cur.execute('select * from band_profile order by mint')
        return [dict(r) for r in cur.fetchall()]


class Figures(unittest.TestCase):
    def setUp(self):
        reset()
        position(closed=H(4))
        risk(H(0.1), 'WARM', 0.15, 0.30, instability=0.04, arch=1.0, arch_p=0.95, choice_pct=1.5, p_held=0.2,
             p_exit_6h=0.5, vol_ratio_1h_24h=1.1)
        risk(H(1.0), 'WARM', 0.20, -0.10, instability=0.06, arch=3.0, arch_p=0.50)
        risk(H(2.0), 'HOT', 0.35, 0.80, instability=0.10, arch=9.0, arch_p=0.05)
        risk(H(3.9), 'HOT', 0.30, -0.40, instability=0.08, arch=7.0, arch_p=0.10)
        risk(H(-1), 'CALM', 9.0, 9.0)                      # before the band: excluded
        risk(H(5), 'CALM', 9.0, 9.0)                       # after it closed: excluded
        risk(H(1.5), 'CALM', 9.0, 9.0, mint=OTHER)         # another band: excluded
        for t, inr in ((0.2, True), (1.2, True), (2.2, True), (3.8, False)):
            snap(H(t), inr)
        snap(H(1.3), False, mint=OTHER)
        harvest(0.10); harvest(0.30)
        harvest(5.0, mint=OTHER)
        self.row = db.record_band_profile(MINT, 'rebalance', 'price went below', at=H(4.01))

    def test_survival_is_open_to_close(self):
        self.assertAlmostEqual(self.row['survived_hours'], 4.0)

    def test_the_rebalance_is_stamped_final_with_its_reason(self):
        r = self.row
        self.assertEqual((r['last_event'], r['final'], r['exit_reason']), ('rebalance', True, 'price went below'))
        self.assertEqual(r['rebalanced_at'], H(4.01))

    def test_means_cover_the_band_only(self):
        r = self.row
        self.assertEqual(r['polls'], 4)
        self.assertAlmostEqual(r['sigma_mean'], (0.15 + 0.20 + 0.35 + 0.30) / 4)
        self.assertAlmostEqual(r['sigma_max'], 0.35)
        self.assertAlmostEqual(r['velocity_mean'], (0.30 - 0.10 + 0.80 - 0.40) / 4)
        self.assertAlmostEqual(r['velocity_abs_mean'], (0.30 + 0.10 + 0.80 + 0.40) / 4)
        self.assertAlmostEqual(r['instability_mean'], (0.04 + 0.06 + 0.10 + 0.08) / 4)
        self.assertAlmostEqual(r['arch_lm_mean'], (1 + 3 + 9 + 7) / 4)
        self.assertAlmostEqual(r['arch_lm_p_mean'], (0.95 + 0.50 + 0.05 + 0.10) / 4)
        self.assertIsNone(r['kurtosis_mean'])             # never measured: unknown, not zero

    def test_open_and_close_are_the_first_and_last_poll(self):
        r = self.row
        self.assertEqual((r['mode_open'], r['sigma_open'], r['velocity_open']), ('WARM', 0.15, 0.30))
        self.assertEqual((r['choice_pct_open'], r['p_held_open'], r['p_exit_6h_open'], r['vol_ratio_open']),
                         (1.5, 0.2, 0.5, 1.1))
        self.assertEqual((r['instability_open'], r['arch_lm_p_open']), (0.04, 0.95))
        self.assertEqual((r['mode_close'], r['sigma_close'], r['velocity_close']), ('HOT', 0.30, -0.40))

    def test_mode_main_and_share(self):
        self.assertEqual(self.row['mode_share'], {'WARM': 0.5, 'HOT': 0.5})
        self.assertEqual(self.row['mode_main'], 'HOT')           # a tie goes to the name first in order

    def test_in_range_share_and_fees(self):
        r = self.row
        self.assertAlmostEqual(float(r['in_range_share']), 0.75)
        self.assertAlmostEqual(float(r['fees_usd']), 0.40)
        self.assertAlmostEqual(float(r['fees_a']), 0.002); self.assertAlmostEqual(float(r['fees_b']), 0.2)
        self.assertEqual(r['harvests'], 2)
        self.assertAlmostEqual(r['fees_per_hour_usd'], 0.10)

    def test_one_row_however_often_it_is_written(self):
        again = db.record_band_profile(MINT, 'rebalance', 'price went below', at=H(9))
        self.assertEqual(len(rows()), 1)
        self.assertEqual(again['rebalanced_at'], H(4.01))        # the first rebalance time stands
        for k in ('survived_hours', 'polls', 'sigma_mean', 'fees_usd', 'mode_share', 'mode_open'):
            self.assertEqual(again[k], self.row[k], k)


class Edges(unittest.TestCase):
    def setUp(self):
        reset()

    def test_polls_at_the_same_instant_keep_their_order(self):
        position(closed=H(2))
        risk(H(1), 'CALM', 0.1, 0.1); risk(H(1), 'HOT', 0.9, 0.9)
        r = db.record_band_profile(MINT, 'rebalance', 'x', at=H(2))
        self.assertEqual((r['mode_open'], r['mode_close']), ('CALM', 'HOT'))

    def test_the_most_frequent_mode_is_the_main_one(self):
        position(closed=H(2))
        for t in (0.1, 0.2, 0.3):
            risk(H(t), 'WARM', 0.2, 0.0)
        risk(H(0.4), 'HOT', 0.5, 0.0)
        r = db.record_band_profile(MINT, 'rebalance', 'x', at=H(2))
        self.assertEqual(r['mode_main'], 'WARM'); self.assertEqual(r['mode_share'], {'WARM': 0.75, 'HOT': 0.25})

    def test_snapshots_outside_the_band_do_not_count(self):
        position(closed=H(2))
        snap(H(1), True)
        snap(H(-0.5), False); snap(H(2.5), False); snap(H(3.5), False)
        self.assertEqual(float(db.record_band_profile(MINT, 'rebalance', 'x', at=H(2))['in_range_share']), 1.0)

    def test_the_first_rebalance_names_the_exit(self):
        position(closed=H(2))
        db.record_band_profile(MINT, 'rebalance', 'price went below', at=H(2))
        r = db.record_band_profile(MINT, 'rebalance', 'a later reason', at=H(3))
        self.assertEqual((r['exit_reason'], r['rebalanced_at']), ('price went below', H(2)))


class Lifecycle(unittest.TestCase):
    def setUp(self):
        reset()

    def test_a_harvest_writes_the_running_figures_then_the_rebalance_finalises(self):
        position()                                              # still open
        risk(H(0.5), 'WARM', 0.2, 0.1)
        harvest(0.05)
        r1 = db.record_band_profile(MINT, 'harvest', at=H(2))
        self.assertEqual((r1['last_event'], r1['final'], r1['rebalanced_at'], r1['exit_reason']),
                         ('harvest', False, None, None))
        self.assertAlmostEqual(r1['survived_hours'], 2.0)       # so far
        risk(H(2.5), 'HOT', 0.4, 0.9)
        harvest(0.07)
        db.close_position(MINT, 'closesig', 100.0)
        with db.cursor(commit=True) as cur:
            cur.execute('update positions set closed_at = %s where mint = %s', (H(3), MINT))
        r2 = db.record_band_profile(MINT, 'rebalance', 'regime WARM: +/-1.5% -> +/-3%', at=H(3))
        self.assertEqual((r2['final'], r2['rebalanced_at'], r2['exit_reason']),
                         (True, H(3), 'regime WARM: +/-1.5% -> +/-3%'))
        self.assertAlmostEqual(r2['survived_hours'], 3.0)
        self.assertEqual(r2['polls'], 2); self.assertAlmostEqual(float(r2['fees_usd']), 0.12)
        self.assertEqual(len(rows()), 1)
        # a late harvest write never takes the rebalance back
        r3 = db.record_band_profile(MINT, 'harvest', at=H(4))
        self.assertEqual((r3['rebalanced_at'], r3['exit_reason']), (H(3), 'regime WARM: +/-1.5% -> +/-3%'))
        self.assertFalse(r3['final'])                              # the last event was a harvest
        self.assertEqual(r3['last_event'], 'harvest')

    def test_a_rebalance_of_an_open_position_is_not_final(self):
        position()
        r = db.record_band_profile(MINT, 'rebalance', 'x', at=H(1))
        self.assertFalse(r['final']); self.assertEqual(r['rebalanced_at'], H(1))

    def test_a_band_with_no_polls_has_unknowns_not_zeros(self):
        position(closed=H(2))
        r = db.record_band_profile(MINT, 'rebalance', 'x', at=H(2))
        self.assertEqual(r['polls'], 0)
        for k in ('sigma_mean', 'mode_main', 'mode_open', 'mode_share', 'in_range_share', 'sigma_open'):
            self.assertIsNone(r[k], k)
        self.assertEqual(float(r['fees_usd']), 0.0); self.assertEqual(r['harvests'], 0)

    def test_a_zero_length_band_has_no_rate(self):
        position(closed=T0)
        self.assertIsNone(db.record_band_profile(MINT, 'rebalance', 'x', at=T0)['fees_per_hour_usd'])

    def test_unknown_position_and_bad_event(self):
        self.assertIsNone(db.record_band_profile('NOPE', 'harvest'))
        self.assertEqual(rows(), [])
        with self.assertRaises(ValueError):
            db.record_band_profile(MINT, 'open')

    def test_polls_without_a_mode_are_counted_but_not_named(self):
        position(closed=H(2))
        risk(H(0.5), None, 0.2, 0.1); risk(H(1.0), 'CALM', 0.1, 0.0)
        r = db.record_band_profile(MINT, 'rebalance', 'x', at=H(2))
        self.assertEqual(r['polls'], 2); self.assertEqual(r['mode_main'], 'CALM')
        self.assertEqual(r['mode_share'], {'CALM': 0.5})


class Idempotence(unittest.TestCase):
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(st.lists(st.tuples(st.floats(0, 3.9), st.sampled_from(['CALM', 'WARM', 'HOT']),
                              st.floats(0.01, 1.0), st.floats(-2, 2)), max_size=12),
           st.lists(st.floats(0, 1), max_size=4), st.sampled_from(['harvest', 'rebalance']))
    def test_any_history_gives_one_row_and_the_same_row_twice(self, polls, fees, event):
        reset()
        position(closed=H(4))
        for t, mode, sigma, vel in polls:
            risk(H(t), mode, sigma, vel)
        for i, f in enumerate(fees):
            db.record_harvest(MINT, 0.0, f, f, f'p{i}')
        a = db.record_band_profile(MINT, event, 'r', at=H(4))
        b = db.record_band_profile(MINT, event, 'r', at=H(4))
        self.assertEqual(len(rows()), 1)
        a.pop('updated_at'); b.pop('updated_at')
        self.assertEqual(a, b)
        self.assertEqual(a['polls'], len(polls))
        if polls:
            self.assertAlmostEqual(a['sigma_mean'], sum(p[2] for p in polls) / len(polls), places=9)
            self.assertAlmostEqual(sum(a['mode_share'].values()), 1.0, places=3)
            self.assertIn(a['mode_main'], {p[1] for p in polls})
        self.assertAlmostEqual(float(a['fees_usd']), round(sum(round(f, 6) for f in fees), 6), places=5)


class Hooks(unittest.TestCase):
    """The loop writes the profile after a harvest and after the rebalance, and
    a failure never stops a move."""

    def test_band_profile_reports_and_swallows_failures(self):
        seen = []
        with mock.patch.object(rebalancer.db, 'record_band_profile', side_effect=RuntimeError('db down')), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append((ev, kw))):
            rebalancer.band_profile('M', 'harvest')
        self.assertEqual(seen[0][0], 'band_profile_failed'); self.assertEqual(seen[0][1]['event'], 'harvest')

    def test_band_profile_passes_its_arguments(self):
        calls = []
        with mock.patch.object(rebalancer.db, 'record_band_profile', lambda *a: calls.append(a)):
            rebalancer.band_profile('M', 'rebalance', 'price went above')
        self.assertEqual(calls, [('M', 'rebalance', 'price went above')])

    def test_dividend_writes_a_harvest_profile(self):
        calls = []
        status = {'positionMint': 'M', 'whirlpool': 'P', 'price': 100.0, 'inRange': True, 'liquidity': '1',
                  'feesAccruedA': 0.001, 'feesAccruedB': 0.2, 'feesAccrued_USD': 0.3, 'positionUsd': 190.0}
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: ({'signature': 'sig'}, None)), \
                mock.patch.object(rebalancer, 'wallet', lambda p: {'walletUsd': 50.0}), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: (('A', 'SOL'), ('B', 'USDC'))), \
                mock.patch.object(rebalancer, 'distribute', lambda *a: None), \
                mock.patch.object(rebalancer, 'distribute_rewards', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'record_harvest', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'snapshot', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'record_band_profile', lambda *a: calls.append(a)):
            self.assertTrue(rebalancer.dividend({}, status))
        self.assertEqual(calls, [('M', 'harvest', None)])

    def test_a_failed_dividend_writes_nothing(self):
        calls = []
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (None, 'rpc down')), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None), \
                mock.patch.object(rebalancer.db, 'record_band_profile', lambda *a: calls.append(a)):
            self.assertFalse(rebalancer.dividend({}, {'positionMint': 'M', 'feesAccruedA': 0, 'feesAccruedB': 0,
                                                      'feesAccrued_USD': 0}))
        self.assertEqual(calls, [])

    def test_the_rebalance_writes_the_final_profile_after_the_close(self):
        order = []
        state = dict(rebalancer.STATE_DEFAULTS)
        status = {'positionMint': 'M', 'whirlpool': 'P', 'price': 100.0, 'inRange': False, 'liquidity': '1',
                  'feesAccruedA': 0.0, 'feesAccruedB': 0.0, 'feesAccrued_USD': 0.0, 'positionUsd': 190.0,
                  'lowerPrice': 99.0, 'upperPrice': 101.0}
        def chain(cmd, *a, **k):
            order.append(cmd)
            return ({'signature': 's'}, None) if cmd == 'close' else (None, 'nothing to claim')
        with mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: order.append(ev)), \
                mock.patch.object(rebalancer.db, 'close_position', lambda *a: order.append('db_close')), \
                mock.patch.object(rebalancer.db, 'record_band_profile', lambda *a: order.append(('profile',) + a)), \
                mock.patch.object(rebalancer, 'reopen', lambda *a, **k: order.append('reopen')), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            rebalancer.rebalance(state, status, 'price went above', band=1.015, calm_move=True, exit_move=True)
        i_close, i_prof = order.index('db_close'), order.index(('profile', 'M', 'rebalance', 'price went above'))
        self.assertLess(i_close, i_prof)
        self.assertEqual(sum(1 for x in order if isinstance(x, tuple)), 1)


if __name__ == '__main__':
    unittest.main()
