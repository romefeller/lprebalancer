"""wallet(): a balance read and the claims read after it that straddle a write
of the wallet give no dollar figure (2026-10-02 20:15Z: sol-usdc equity read
-$5.84 for one snapshot while djt-usdc's open was landed and not booked)."""
import unittest
from unittest import mock

from hypothesis import given, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import rebalancer
import wallets

VIEW = {'balanceA': 1.0, 'balanceB': 5.0, 'walletUsd': 124.0}
U = rebalancer.UNREADABLE
PENDING = {'profile': 'djt-usdc', 'command': 'open', 'signatures': []}


class Guard(unittest.TestCase):
    def test_a_quiet_wallet_keeps_its_figure(self):
        self.assertIs(rebalancer.unsettled_guard(VIEW, (7, None), (7, None)), VIEW)

    def test_a_pending_write_before_or_after_hides_the_figure(self):
        for before, after in (((7, PENDING), (7, PENDING)), ((7, PENDING), (8, None)), ((7, None), (7, PENDING))):
            v = rebalancer.unsettled_guard(VIEW, before, after)
            self.assertIsNone(v['walletUsd'])
            self.assertTrue(v['unsettled'])
            self.assertEqual((v['balanceA'], v['balanceB']), (1.0, 5.0))       # the balances stay as read

    def test_a_write_booked_between_the_reads_hides_the_figure(self):
        self.assertIsNone(rebalancer.unsettled_guard(VIEW, (7, None), (8, None))['walletUsd'])

    def test_an_unreadable_settle_state_hides_the_figure(self):
        for before, after in ((U, (7, None)), ((7, None), U), (U, U)):
            self.assertIsNone(rebalancer.unsettled_guard(VIEW, before, after)['walletUsd'])

    def test_no_wallet_or_no_figure_is_left_alone(self):
        self.assertIs(rebalancer.unsettled_guard(VIEW, None, None), VIEW)
        self.assertEqual(rebalancer.unsettled_guard({}, (7, PENDING), (7, PENDING)), {})

    def test_the_input_is_not_changed(self):
        v = dict(VIEW)
        rebalancer.unsettled_guard(v, (7, PENDING), (7, PENDING))
        self.assertEqual(v, VIEW)

    @given(st.integers(0, 10), st.integers(0, 10), st.booleans(), st.booleans())
    def test_the_figure_survives_only_a_quiet_unchanged_wallet(self, s0, s1, p0, p1):
        v = rebalancer.unsettled_guard(VIEW, (s0, PENDING if p0 else None), (s1, PENDING if p1 else None))
        quiet = s0 == s1 and not p0 and not p1
        self.assertEqual(v['walletUsd'] is not None, quiet)


class Mark(unittest.TestCase):
    def test_no_wallet_is_none(self):
        with mock.patch.object(config, 'WALLET_ID', None):
            self.assertIsNone(rebalancer.settle_mark())

    def test_a_database_error_is_unreadable(self):
        with mock.patch.object(config, 'WALLET_ID', 'sol-lp'), \
             mock.patch.object(wallets, 'settle_state', side_effect=RuntimeError('db down')):
            self.assertEqual(rebalancer.settle_mark(), rebalancer.UNREADABLE)

    def test_the_state_is_passed_through(self):
        with mock.patch.object(config, 'WALLET_ID', 'sol-lp'), \
             mock.patch.object(wallets, 'settle_state', return_value=(9, None)):
            self.assertEqual(rebalancer.settle_mark(), (9, None))


class Wallet(unittest.TestCase):
    """wallet() brackets the balance read and the sleeve (claims) read."""
    def run_wallet(self, marks):
        seq = iter(marks)
        with mock.patch.object(config, 'WALLET_ID', 'sol-lp'), \
             mock.patch.object(wallets, 'settle_state', side_effect=lambda w: next(seq)), \
             mock.patch.object(rebalancer, 'chain', return_value=(dict(VIEW), None)), \
             mock.patch.object(rebalancer, 'note_scale'), \
             mock.patch.object(rebalancer, 'sleeve_of', side_effect=lambda b: b):
            return rebalancer.wallet('POOL')

    def test_a_write_landing_during_the_read_gives_no_figure(self):
        self.assertIsNone(self.run_wallet([(7, None), (7, PENDING)])['walletUsd'])

    def test_a_quiet_read_keeps_the_figure(self):
        self.assertEqual(self.run_wallet([(7, None), (7, None)])['walletUsd'], 124.0)

    def retried(self, first):
        reads = iter([(first, None), (dict(VIEW, walletUsd=99.0), None)])
        with mock.patch.object(config, 'WALLET_ID', 'sol-lp'), \
             mock.patch.object(wallets, 'settle_state', return_value=(7, None)), \
             mock.patch.object(rebalancer, 'chain', side_effect=lambda *a: next(reads)), \
             mock.patch.object(rebalancer, 'note_scale'), \
             mock.patch.object(rebalancer.time, 'sleep') as slept, \
             mock.patch.object(rebalancer, 'sleeve_of', side_effect=lambda b: b):
            return rebalancer.wallet('POOL'), slept.call_count

    def test_a_failed_or_partial_read_is_tried_once_more(self):
        for first in (None, {}, {'balanceB': 5.0}):
            self.assertEqual(self.retried(first), (dict(VIEW, walletUsd=99.0), 1))

    def test_a_full_first_read_is_not_retried(self):
        self.assertEqual(self.retried(dict(VIEW)), (VIEW, 0))

    def test_two_failed_reads_give_an_empty_view(self):
        with mock.patch.object(config, 'WALLET_ID', 'sol-lp'), \
             mock.patch.object(wallets, 'settle_state', return_value=(7, None)), \
             mock.patch.object(rebalancer, 'chain', return_value=(None, 'down')), \
             mock.patch.object(rebalancer, 'note_scale'), \
             mock.patch.object(rebalancer.time, 'sleep'), \
             mock.patch.object(rebalancer, 'sleeve_of', side_effect=lambda b: b) as sl:
            self.assertEqual(rebalancer.wallet('POOL'), {})
            sl.assert_called_once_with({})


if __name__ == '__main__':
    unittest.main()
