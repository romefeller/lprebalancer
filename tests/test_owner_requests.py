"""The loop and the owner's page (sql/040): a vault the owners revoked stops
the bot once and cleanly (one REVOKED line, exit 0, no failure, no HALT); an
exit the owner signed closes the position into the vault and stops; a vault
profile keeps its newest book and DAILY rows where the page reads them."""
import json
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import config  # noqa: E402
import db  # noqa: E402
from lp import books, loop, signers  # noqa: E402

SOL_REVOKED = ('Error: the owners revoked the bot: policy 3Fv8mHjCJqgXb5tNbAzVQ1QvVq6x4V5n9kGxS7mKpJ2d no longer lists '
               'the bot\'s policy key')
EVM_REVOKED = 'refused: the owners revoked the bot: the Safe 0x11 has not enabled the Roles module 0x22'


class Revoked(unittest.TestCase):
    def test_a_revoked_vault_is_recognised_on_both_chains(self):
        for err in (SOL_REVOKED, EVM_REVOKED, books.tidy(SOL_REVOKED)):
            self.assertTrue(signers.revoked(err), err)
        for err in (None, '', 'refused: halted', 'policy X differs from the rules', 'owners revoked', 'the owner revoked the bot'):
            self.assertFalse(signers.revoked(err), err)

    def test_a_revoked_write_holds_and_counts_no_failure(self):
        self.assertTrue(signers.held(SOL_REVOKED))
        self.assertTrue(signers.held(EVM_REVOKED))
        self.assertFalse(signers.counts_as_failure(SOL_REVOKED))
        self.assertFalse(signers.counts_as_failure(EVM_REVOKED))
        self.assertTrue(signers.counts_as_failure('rpc timeout'), 'any other error still counts')

    def test_the_loop_stops_once_on_a_revoked_vault(self):
        notes = []
        with mock.patch.object(loop.paths, 'halted', return_value=None), \
                mock.patch.object(loop.config, 'require_wallet'), mock.patch.object(loop.signers, 'probe_rpc'), \
                mock.patch.object(loop.paths, 'load', return_value=dict(loop.paths.STATE_DEFAULTS)), \
                mock.patch.object(loop.books, 'notify_book'), \
                mock.patch.object(loop.books, 'notify', side_effect=lambda e, **k: notes.append((e, k))), \
                mock.patch.object(loop.books, 'halt') as halt, \
                mock.patch.object(loop.db, 'event'), \
                mock.patch.object(loop.config, 'VAULT', {'kind': 'squads', 'venues': {}}), \
                mock.patch.object(loop.db, 'owner_exit_pending', return_value=7), \
                mock.patch.object(loop.db, 'owner_request_done') as done, \
                mock.patch.object(loop.signers, 'read_status', return_value=(None, SOL_REVOKED)), \
                mock.patch.object(loop.scanner, 'Scanner'):
            self.assertEqual(loop.main(), 0)
        self.assertEqual([e for e, _ in notes], ['REVOKED'], 'one line, no status_unreadable, no breaker')
        halt.assert_not_called()
        done.assert_called_once()
        self.assertEqual(done.call_args[0][0], 7, 'the pending exit is closed with it')


class Exit(unittest.TestCase):
    def run_exit(self, status, after, pending=5):
        notes = []
        with mock.patch.object(loop.config, 'VAULT', {'kind': 'squads', 'venues': {}}), \
                mock.patch.object(loop.db, 'owner_exit_pending', return_value=pending), \
                mock.patch.object(loop.db, 'owner_request_done') as done, mock.patch.object(loop.db, 'event'), \
                mock.patch.object(loop.moves, 'rebalance') as rebalance, \
                mock.patch.object(loop.signers, 'read_status', return_value=after), \
                mock.patch.object(loop.books, 'notify', side_effect=lambda e, **k: notes.append(e)):
            code = loop.exit_for_owner({}, status, pending)
        return code, rebalance, done, notes

    def test_an_exit_closes_into_the_vault_and_stops(self):
        code, rebalance, done, notes = self.run_exit({'positionMint': 'M'}, ({'positionMint': None}, None))
        self.assertEqual(code, 0)
        rebalance.assert_called_once()
        self.assertEqual(rebalance.call_args.kwargs, {'close_only': True, 'operator': True}, 'a close only, never a reopen, no gap wait')
        done.assert_called_once()
        self.assertEqual(notes, ['OWNER_EXIT'])

    def test_a_close_that_did_not_land_keeps_the_request(self):
        for after in (({'positionMint': 'M'}, None), (None, 'rpc down')):
            code, _, done, notes = self.run_exit({'positionMint': 'M'}, after)
            self.assertIsNone(code)
            done.assert_not_called()
            self.assertEqual(notes, ['owner_exit_pending'])

    def test_no_position_stops_at_once(self):
        code, rebalance, done, notes = self.run_exit({'positionMint': None}, None)
        self.assertEqual(code, 0)
        rebalance.assert_not_called()
        done.assert_called_once()

    def test_a_key_wallet_reads_no_request(self):
        with mock.patch.object(loop.config, 'VAULT', None), mock.patch.object(loop.db, 'owner_exit_pending') as read:
            self.assertIsNone(loop.owner_exit())
        read.assert_not_called()
        with mock.patch.object(loop.config, 'VAULT', {'kind': 'safe'}), \
                mock.patch.object(loop.db, 'owner_exit_pending', side_effect=RuntimeError('db down')):
            self.assertIsNone(loop.owner_exit(), 'an unreadable request is no request')


class Latest(unittest.TestCase):
    def test_the_kind_of_a_row(self):
        self.assertEqual(books.latest_kind('in_band', {'fees_total_usd': 1, 'equity_usd': 2}), 'book')
        self.assertEqual(books.latest_kind('DAILY', {}), 'DAILY')
        self.assertEqual(books.latest_kind('REVOKED', {}), 'REVOKED')
        self.assertEqual(books.latest_kind('OWNER_EXIT', {}), 'OWNER_EXIT')
        self.assertIsNone(books.latest_kind('TAPE_SOURCE', {}))
        self.assertIsNone(books.latest_kind('in_band', {'fees_total_usd': 1}), 'a book has equity too')

    def test_only_a_vault_keeps_rows_and_a_failure_never_raises(self):
        row = {'event': 'in_band', 'fees_total_usd': 1, 'equity_usd': 2}
        with mock.patch.object(books.config, 'VAULT', None), mock.patch.object(books.db, 'feed_latest_put') as put:
            books.keep_latest('in_band', row, json.dumps(row))
        put.assert_not_called()
        with mock.patch.object(books.config, 'VAULT', {'kind': 'squads'}), mock.patch.object(books.db, 'feed_latest_put') as put:
            books.keep_latest('in_band', row, json.dumps(row))
            books.keep_latest('TAPE_SOURCE', {}, '{}')
        self.assertEqual(put.call_count, 1)
        self.assertEqual(put.call_args[0][:2], (config.PROFILE, 'book'))
        with mock.patch.object(books.config, 'VAULT', {'kind': 'squads'}), \
                mock.patch.object(books.db, 'feed_latest_put', side_effect=RuntimeError('db down')):
            books.keep_latest('DAILY', {}, '{}')            # never raises

    def test_the_database_keeps_the_newest_row_of_each_kind(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from feed_latest where profile = 'or-test'")
        try:
            db.feed_latest_put('or-test', 'book', json.dumps({'a': 1}))
            db.feed_latest_put('or-test', 'book', json.dumps({'a': 2}))
            with db.cursor() as cur:
                cur.execute("select row from feed_latest where profile = 'or-test'")
                self.assertEqual([r['row'] for r in cur.fetchall()], [{'a': 2}])
        finally:
            with db.cursor(commit=True) as cur:
                cur.execute("delete from feed_latest where profile = 'or-test'")


class HeldAndKinds(unittest.TestCase):
    def test_held_is_each_reason_on_its_own(self):
        self.assertTrue(signers.held('refused: halted (HALT)'), 'a wait refusal')
        self.assertTrue(signers.held('refused: wallet sol-lp lock busy'))
        self.assertTrue(signers.held(SOL_REVOKED), 'a revoke')
        with mock.patch.object(signers, 'mint_refusal', return_value=True):
            self.assertTrue(signers.held('anything'), 'a mint refusal')
        for err in (None, '', 'rpc timeout', 'refused: not enough SOL'):
            self.assertFalse(signers.held(err), err)

    def test_a_book_has_fees_and_equity(self):
        self.assertIsNone(books.latest_kind('in_band', {'equity_usd': 1}))


if __name__ == '__main__':
    unittest.main()
