"""The edge watch: between polls, within edge_watch_pct of a band edge, the
loop reads the pool's price every edge_watch_seconds and polls at once when
it leaves the band (calm.near_edge, calm.watch_verdict, rebalancer.edge_sleep,
rebalancer.pool_price_now; sql/025)."""
import unittest
from unittest import mock

from hypothesis import given, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import config
from venues import solana_state
import rebalancer

STATUS = {'price': 100.0, 'lowerPrice': 99.0, 'upperPrice': 101.0, 'whirlpool': 'POOL'}


class Zone(unittest.TestCase):
    def test_near_either_edge_inside_the_band(self):
        self.assertTrue(calm.near_edge(100.8, 99.0, 101.0, 0.25))      # 0.2 from the top, 0.25% of 100.8 is 0.252
        self.assertTrue(calm.near_edge(99.2, 99.0, 101.0, 0.25))
        self.assertFalse(calm.near_edge(100.0, 99.0, 101.0, 0.25))
        self.assertTrue(calm.near_edge(101.0, 99.0, 101.0, 0.25))       # on the edge: still inside

    def test_the_zone_boundary_is_inclusive(self):
        self.assertTrue(calm.near_edge(100.0, 99.0, 100.25, 0.25))
        self.assertFalse(calm.near_edge(100.0, 99.0, 100.2501, 0.25))

    def test_off_outside_or_a_bad_band(self):
        for args in ((100.0, 99.0, 101.0, 0), (100.0, 99.0, 101.0, -1), (100.0, 99.0, 101.0, None),
                     (102.0, 99.0, 101.0, 5), (98.0, 99.0, 101.0, 5), (100.0, 101.0, 99.0, 5),
                     (100.0, 0.0, 101.0, 5), (100.0, None, 101.0, 5), (0.0, 99.0, 101.0, 5), (None, 99.0, 101.0, 5)):
            self.assertIs(calm.near_edge(*args), False, args)

    def test_verdicts(self):
        for p, want in ((101.01, 'exit'), (98.99, 'exit'), (100.9, 'near'), (99.1, 'near'), (100.0, 'away'),
                        (None, 'away'), (0.0, 'away'), (-1.0, 'away')):
            self.assertEqual(calm.watch_verdict(p, 99.0, 101.0, 0.25), want, p)

    @given(st.floats(50, 150), st.floats(0.01, 10))
    def test_a_verdict_agrees_with_the_zone(self, p, pct):
        v = calm.watch_verdict(p, 99.0, 101.0, pct)
        self.assertEqual(v == 'exit', not 99.0 <= p <= 101.0)
        self.assertEqual(v == 'near', calm.near_edge(p, 99.0, 101.0, pct))


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def sleep(self, s):
        self.slept.append(round(s, 6))
        self.t += s

    def now(self):
        return self.t


class Sleep(unittest.TestCase):
    def run_watch(self, prices, status=STATUS, total=120, pct=0.25, step=15):
        c, seq = Clock(), iter(prices)
        with mock.patch.multiple(config, EDGE_WATCH_PCT=pct, EDGE_WATCH_S=step), \
             mock.patch.object(rebalancer, 'notify') as note:
            out = rebalancer.edge_sleep(total, status, read=lambda: next(seq), sleep=c.sleep, clock=c.now)
        return out, c, note

    def test_off_or_away_from_the_edges_sleeps_the_poll(self):
        out, c, _ = self.run_watch([], pct=0)
        self.assertEqual((out, c.slept), ('off', [120]))
        out, c, _ = self.run_watch([], status=dict(STATUS, price=100.0))
        self.assertEqual((out, c.slept), ('off', [120]))

    def test_a_crossing_polls_at_once(self):
        out, c, note = self.run_watch([100.9, 100.95, 101.02], status=dict(STATUS, price=100.8))
        self.assertEqual(out, 'exit')
        self.assertEqual(c.slept, [15, 15, 15])
        self.assertEqual(note.call_args[0][0], 'edge_watch')

    def test_back_to_the_middle_sleeps_out_the_rest(self):
        out, c, _ = self.run_watch([100.9, 100.0], status=dict(STATUS, price=100.8))
        self.assertEqual(out, 'slept')
        self.assertEqual(c.slept, [15, 15, 90])
        self.assertEqual(c.t, 120)

    def test_staying_near_watches_until_the_poll_is_due(self):
        out, c, _ = self.run_watch([100.9] * 20, status=dict(STATUS, price=100.8))
        self.assertEqual(out, 'slept')
        self.assertEqual(c.t, 120)
        self.assertEqual(len(c.slept), 8)                             # 8 reads of 15 s, the last one not read

    def test_a_failed_or_foreign_read_ends_the_watch(self):
        for bad in (None, 300.0, 0.5, 102.9):
            out, c, _ = self.run_watch([bad], status=dict(STATUS, price=100.8))
            self.assertEqual((out, c.slept, c.t), ('slept', [15, 105], 120), bad)

    def test_a_step_longer_than_the_poll_is_cut(self):
        out, c, _ = self.run_watch([], status=dict(STATUS, price=100.8), total=10, step=15)
        self.assertEqual((out, c.slept), ('slept', [10]))

    def test_the_default_reader_reads_this_pool(self):
        c = Clock()
        with mock.patch.multiple(config, EDGE_WATCH_PCT=0.25, EDGE_WATCH_S=15, DEX='raydium-clmm', POOL='CFG'), \
             mock.patch.object(rebalancer, 'pool_price_now', return_value=101.5) as read, \
             mock.patch.object(rebalancer, 'notify'):
            self.assertEqual(rebalancer.edge_sleep(120, dict(STATUS, price=100.8), sleep=c.sleep, clock=c.now), 'exit')
        read.assert_called_once_with('raydium-clmm', 'POOL')


class Defaults(unittest.TestCase):
    def test_the_module_s_sleep_is_used_when_none_is_given(self):
        with mock.patch.multiple(config, EDGE_WATCH_PCT=0), mock.patch.object(rebalancer.time, 'sleep') as sl:
            self.assertEqual(rebalancer.edge_sleep(42, STATUS), 'off')
        sl.assert_called_once_with(42)


class Boundaries(unittest.TestCase):
    def test_on_an_edge_is_near_not_an_exit(self):
        self.assertEqual(calm.watch_verdict(99.0, 99.0, 101.0, 0.25), 'near')
        self.assertEqual(calm.watch_verdict(101.0, 99.0, 101.0, 0.25), 'near')

    def test_a_price_under_one_is_a_price(self):
        self.assertEqual(calm.watch_verdict(0.5, 0.6, 0.7, 1), 'exit')
        self.assertEqual(calm.watch_verdict(0.65, 0.6, 0.7, 1), 'away')
        self.assertTrue(calm.near_edge(0.5, 0.4999, 0.6, 1))

    def test_a_tiny_zone_and_a_tiny_band(self):
        self.assertTrue(calm.near_edge(100.0, 99.0, 100.0, 1e-9))
        self.assertFalse(calm.near_edge(100.0, 100.0, 100.0, 0))
        self.assertTrue(calm.near_edge(100.0, 100.0, 100.0, 0.01))

    def test_a_read_exactly_at_the_match_limit_is_kept(self):
        c = Clock()
        with mock.patch.multiple(config, EDGE_WATCH_PCT=5, EDGE_WATCH_S=15), mock.patch.object(rebalancer, 'notify'):
            out = rebalancer.edge_sleep(30, {'price': 100.0, 'lowerPrice': 97.0, 'upperPrice': 101.0},
                                        read=lambda: 102.0, sleep=c.sleep, clock=c.now)
        self.assertEqual((out, c.slept), ('exit', [15]))               # 2% away is the same price, and outside

    def test_without_a_pool_in_the_status_the_profile_s_pool_is_read(self):
        c = Clock()
        with mock.patch.multiple(config, EDGE_WATCH_PCT=0.25, EDGE_WATCH_S=15, DEX='orca', POOL='CFG'), \
             mock.patch.object(rebalancer, 'pool_price_now', return_value=100.0) as read:
            rebalancer.edge_sleep(30, {'price': 100.8, 'lowerPrice': 99.0, 'upperPrice': 101.0}, sleep=c.sleep, clock=c.now)
        read.assert_called_with('orca', 'CFG')

    def test_the_module_s_clock_is_used_when_none_is_given(self):
        t = [0.0]
        def fake_sleep(s):
            t[0] += s
        with mock.patch.multiple(config, EDGE_WATCH_PCT=0.25, EDGE_WATCH_S=15), \
             mock.patch.object(rebalancer.time, 'sleep', side_effect=fake_sleep), \
             mock.patch.object(rebalancer.time, 'monotonic', side_effect=lambda: t[0]), \
             mock.patch.object(rebalancer, 'notify'):
            out = rebalancer.edge_sleep(60, dict(STATUS, price=100.8), read=lambda: 100.9)
        self.assertEqual((out, t[0]), ('slept', 60.0))


class PoolPrice(unittest.TestCase):
    def test_the_price_from_the_sqrt_price(self):
        sp = int((120.0 * 1e-3) ** 0.5 * solana_state.Q64)
        with mock.patch.object(solana_state, 'fee_states', return_value={'P': {'sqrt_price': sp, 'dec_a': 9, 'dec_b': 6}}):
            self.assertAlmostEqual(rebalancer.pool_price_now('raydium-clmm', 'P'), 120.0, places=6)

    def test_no_state_or_a_failure_is_none(self):
        with mock.patch.object(solana_state, 'fee_states', return_value={}):
            self.assertIsNone(rebalancer.pool_price_now('meteora-dlmm', 'P'))
        with mock.patch.object(solana_state, 'fee_states', side_effect=OSError('rpc')):
            self.assertIsNone(rebalancer.pool_price_now('raydium-clmm', 'P'))


class Config(unittest.TestCase):
    def test_the_summary_names_the_watch(self):
        self.assertIn('edge_watch_pct', config.summary())
        self.assertIn('edge_watch_seconds', config.summary())


if __name__ == '__main__':
    unittest.main()
