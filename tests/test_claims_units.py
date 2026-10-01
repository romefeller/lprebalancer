"""The claims core, one function at a time: the slot reads (wallets.py) and
the settle loop (rebalancer.py). test_multi_loop runs them end to end on a
fake chain; these tests pin their edges: retries, defaults, the boundaries
of every comparison, and the answers of a node that knows nothing yet."""
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import rebalancer
import wallets

SOL = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
MU = 'MUmintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'


class Rpc:
    """A scripted wallets._rpc: each method answers from its list in turn;
    an exception in the list is raised."""

    def __init__(self, **answers):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.calls = []

    def __call__(self, url, method, params, timeout):
        self.calls.append((method, params, timeout))
        a = self.answers[method].pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def token_answer(slot, amount):
    return {'context': {'slot': slot}, 'value': [{'account': {'data': {'parsed': {'info': {
        'tokenAmount': {'uiAmountString': str(amount)}}}}}}]}


class ReadBalances(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(wallets.time, 'sleep', lambda s: None)
        p.start(); self.addCleanup(p.stop)

    def test_the_slot_is_the_oldest_read(self):
        rpc = Rpc(getTokenAccountsByOwner=[token_answer(12, 1), token_answer(10, 2)])
        with mock.patch.object(wallets, '_rpc', rpc):
            self.assertEqual(wallets.read_balances('solana', 'u', 'o', [USDC, MU], SOL), ({USDC: 1.0, MU: 2.0}, 10))

    def test_no_mints_reads_nothing_and_is_at_the_head(self):
        with mock.patch.object(wallets, '_rpc', Rpc()):
            self.assertEqual(wallets.read_balances('solana', 'u', 'o', [], SOL), ({}, 0))
        with mock.patch.object(wallets, '_rpc', Rpc(eth_blockNumber=['0x4d'])):
            self.assertEqual(wallets.read_balances('base', 'u', '0x' + 'ab' * 20, [], SOL), ({}, 77))

    def test_a_head_read_is_tried_again_once(self):
        rpc = Rpc(eth_blockNumber=[OSError('x'), '0x4d'])
        with mock.patch.object(wallets, '_rpc', rpc):
            self.assertEqual(wallets.read_balances('base', 'u', '0x' + 'ab' * 20, [], SOL), ({}, 77))
        with mock.patch.object(wallets, '_rpc', Rpc(eth_blockNumber=[OSError('x'), OSError('y')])):
            self.assertIsNone(wallets.read_balances('base', 'u', '0x' + 'ab' * 20, [], SOL))
        rpc = Rpc(eth_blockNumber=[OSError('x'), OSError('y'), OSError('z'), '0x1'])
        with mock.patch.object(wallets, '_rpc', rpc):
            self.assertEqual(wallets.read_balances('base', 'u', '0x' + 'ab' * 20, [], SOL, tries=4), ({}, 1))


class WriteSlot(unittest.TestCase):
    def status(self, slot, how='confirmed'):
        return {'slot': slot, 'confirmationStatus': how, 'err': None}

    def test_solana_every_signature_confirmed_and_the_latest_slot(self):
        rpc = Rpc(getSignatureStatuses=[{'value': [self.status(5), self.status(9, 'finalized')]}])
        with mock.patch.object(wallets, '_rpc', rpc):
            self.assertEqual(wallets.write_slot('solana', 'u', ['a', 'b']), 9)
        self.assertEqual(rpc.calls[0][2], 10)                              # the default timeout
        for unknown in (None, self.status(5, 'processed'), {'slot': 5}):
            with mock.patch.object(wallets, '_rpc', Rpc(getSignatureStatuses=[{'value': [self.status(5), unknown]}])):
                self.assertIsNone(wallets.write_slot('solana', 'u', ['a', 'b']), unknown)

    def test_evm_every_receipt_and_the_latest_block(self):
        rpc = Rpc(eth_getTransactionReceipt=[{'blockNumber': '0x20'}, {'blockNumber': '0x10'}])
        with mock.patch.object(wallets, '_rpc', rpc):
            self.assertEqual(wallets.write_slot('base', 'u', ['0xa', '0xb']), 32)
        self.assertEqual([c[2] for c in rpc.calls], [10, 10])
        for missing in (None, {}, {'blockNumber': None}):
            with mock.patch.object(wallets, '_rpc', Rpc(eth_getTransactionReceipt=[{'blockNumber': '0x1'}, missing])):
                self.assertIsNone(wallets.write_slot('base', 'u', ['0xa', '0xb']), missing)

    def test_the_timeout_is_passed_on(self):
        rpc = Rpc(eth_getTransactionReceipt=[{'blockNumber': '0x1'}])
        with mock.patch.object(wallets, '_rpc', rpc):
            wallets.write_slot('base', 'u', ['0xa'], timeout=3)
        self.assertEqual(rpc.calls[0][2], 3)


class SettleState(unittest.TestCase):
    def test_a_wallet_never_written_is_at_slot_0(self):
        self.assertEqual(wallets.settle_state('no-such-wallet-ever'), (0, None))


class Pure(unittest.TestCase):
    def test_tries_in(self):
        self.assertEqual([rebalancer.tries_in(w) for w in (0, 1.9, 2, 60, -10)], [1, 1, 2, 31, 1])

    def test_signatures_of(self):
        self.assertEqual(rebalancer.signatures_of({'signatures': ['a', None, '', 5, 'b'], 'signature': 'z'}), ['a', 'b'])
        self.assertEqual(rebalancer.signatures_of({'signature': 'x'}), ['x'])
        self.assertEqual(rebalancer.signatures_of({'signatures': [], 'signature': 'x'}), ['x'])
        self.assertEqual(rebalancer.signatures_of(None), [])

    def test_held(self):
        for err in (None, '', 'refused: halted (HALT)', 'refused: claims unmeasurable (x); open not sent'):
            self.assertEqual(rebalancer.held(err), bool(err), err)


class Settle(unittest.TestCase):
    """settle_pending with the wallet's state, the node and the book stubbed."""

    def go(self, pending, settled=0, write_slots=(), reads=(), booked=None, now=None):
        books, problems, sleeps, slots = [], [], [], list(write_slots)
        reads = list(reads)
        def write_slot(chain, url, sigs):
            return slots.pop(0) if slots else None
        def read_balances(*a, **k):
            return reads.pop(0) if reads else None
        def book(wallet_id, profile, deltas, slot):
            books.append((profile, deltas, slot))
            return booked or {m: (0.0, 0.0) for m in deltas}
        patches = [mock.patch.object(config, 'WALLET_ID', 'w'),
                   mock.patch.object(wallets, 'settle_state', lambda w: (settled, pending)),
                   mock.patch.object(wallets, 'write_slot', write_slot),
                   mock.patch.object(wallets, 'read_balances', read_balances),
                   mock.patch.object(wallets, 'book', book),
                   mock.patch.object(rebalancer, 'claim_problem', lambda k, w, **d: problems.append((k, d))),
                   mock.patch.object(rebalancer.time, 'sleep', lambda s: sleeps.append(s))]
        if now is not None:
            patches.append(mock.patch.object(rebalancer.time, 'time', lambda: now))
        for p in patches:
            p.start()
        try:
            ok = rebalancer.settle_pending(rebalancer.CLAIM_SETTLE_S)
        finally:
            for p in reversed(patches):
                p.stop()
        return ok, books, problems, sleeps

    def pending(self, **over):
        p = {'profile': 'mu', 'command': 'open', 'mints': [USDC], 'before': {USDC: 0.0}, 'before_slot': 0,
             'signatures': ['S'], 'sent_at': 1000.0}
        p.update(over)
        return p

    def test_polls_wait_between_reads(self):
        ok, books, _, sleeps = self.go(self.pending(), write_slots=[None, 7], reads=[({USDC: 2.0}, 7)], now=1001.0)
        self.assertTrue(ok); self.assertEqual(books, [('mu', {USDC: 2.0}, 7)])
        self.assertEqual(sleeps, [rebalancer.CLAIM_POLL_S])

    def test_unknown_write_waits_until_it_expires_exactly(self):
        p = self.pending()
        ok, books, *_ = self.go(p, now=1000.0 + rebalancer.PENDING_EXPIRE_S, reads=[({USDC: 2.0}, 5)])
        self.assertFalse(ok); self.assertEqual(books, [])
        ok, books, *_ = self.go(p, now=1000.0 + rebalancer.PENDING_EXPIRE_S + 1, reads=[({USDC: 2.0}, 5)])
        self.assertTrue(ok); self.assertEqual(books, [('mu', {USDC: 2.0}, 5)])

    def test_expired_write_at_slot_0_is_booked_from_a_read_at_slot_0(self):
        ok, books, *_ = self.go(self.pending(sent_at=0.0), reads=[({USDC: 2.0}, 0)], now=10_000.0)
        self.assertTrue(ok); self.assertEqual(books, [('mu', {USDC: 2.0}, 0)])

    def test_a_write_without_sent_at_counts_as_old(self):
        p = self.pending(); del p['sent_at']
        ok, books, *_ = self.go(p, reads=[({USDC: 2.0}, 4)], now=10_000.0)
        self.assertTrue(ok); self.assertEqual(books, [('mu', {USDC: 2.0}, 4)])

    def test_a_move_of_exactly_dust_is_not_booked(self):
        ok, books, *_ = self.go(self.pending(), write_slots=[3], reads=[({USDC: wallets.DUST}, 3)], now=1001.0)
        self.assertTrue(ok); self.assertEqual(books, [('mu', {}, 3)])

    def test_an_overdraw_of_exactly_dust_is_not_reported(self):
        ok, _, problems, _ = self.go(self.pending(), write_slots=[3], reads=[({USDC: 2.0}, 3)], now=1001.0,
                                     booked={USDC: (0.0, wallets.DUST)})
        self.assertTrue(ok); self.assertEqual(problems, [])
        ok, _, problems, _ = self.go(self.pending(), write_slots=[3], reads=[({USDC: 2.0}, 3)], now=1001.0,
                                     booked={USDC: (0.0, 2 * wallets.DUST)})
        self.assertEqual([k for k, _ in problems], ['claim_overdraw'])


class Measure(unittest.TestCase):
    def test_defaults_take_one_read_at_any_slot(self):
        reads = []
        def read_balances(*a, **k):
            reads.append(a)
            return {USDC: 1.0}, 0
        with mock.patch.object(wallets, 'read_balances', read_balances), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            self.assertEqual(rebalancer.measure([USDC]), ({USDC: 1.0}, 0))
        self.assertEqual(len(reads), 1)


class ProfileEnabled(unittest.TestCase):
    def test_a_pre_020_profile_is_never_read(self):
        with mock.patch.object(config, 'WALLET_ID', None), \
                mock.patch.object(config, 'PROFILE', 'no-such-profile-ever'):
            self.assertTrue(rebalancer.profile_enabled())

    def test_a_profile_without_a_row_is_not_enabled(self):
        with mock.patch.object(config, 'WALLET_ID', 'w'), \
                mock.patch.object(config, 'PROFILE', 'no-such-profile-ever'):
            self.assertFalse(rebalancer.profile_enabled())

    def test_a_failed_read_changes_nothing(self):
        with mock.patch.object(config, 'WALLET_ID', 'w'), \
                mock.patch.object(rebalancer.db, 'cursor', side_effect=RuntimeError('db down')):
            self.assertIs(rebalancer.profile_enabled(), True)


if __name__ == '__main__':
    unittest.main()
