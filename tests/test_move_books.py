"""The book at an OPEN and a CLOSE (2026-09-30): the latest snapshot still
describes the position before the move, and the OPEN book said
"in LP $0.00 (0.0%)". The LP line at a move comes from the mark the loop just
read; a book whose parts do not add up shows no share at all."""
import ast
import pathlib
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures
_fixtures.ensure_profile()

import db          # noqa: E402
import lp.books  # noqa: E402
import lp.moves  # noqa: E402
import lp.paths  # noqa: E402
import config  # noqa: E402
from test_audit import reset  # noqa: E402

money = st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False)


class DeploymentNow(unittest.TestCase):
    @settings(max_examples=400, deadline=None)
    @given(eq=st.floats(min_value=0.01, max_value=1e6), lp=st.floats(min_value=-1e3, max_value=2e6))
    def test_parts_add_up_and_the_share_is_in_range(self, eq, lp):
        d = db.deployment_now({'equity_usd': eq}, lp)
        self.assertGreaterEqual(d['deployed_pct'], 0.0); self.assertLessEqual(d['deployed_pct'], 100.0)
        self.assertAlmostEqual(d['lp_usd'] + d['wallet_usd'], eq, delta=0.011)
        self.assertGreaterEqual(d['wallet_usd'], 0.0)
        self.assertAlmostEqual(d['deployed_pct'], min(max(lp, 0), eq) / eq * 100, delta=0.051)

    def test_open_and_close(self):
        self.assertEqual(db.deployment_now({'equity_usd': 238.76}, 150.13),
                         {'lp_usd': 150.13, 'wallet_usd': 88.63, 'deployed_pct': 62.9})
        self.assertEqual(db.deployment_now({'equity_usd': 238.76}, 0.0),
                         {'lp_usd': 0.0, 'wallet_usd': 238.76, 'deployed_pct': 0.0})

    def test_nothing_to_say_without_figures(self):
        for book, lp in (({'equity_usd': None}, 5.0), ({}, 5.0), ({'equity_usd': 0.0}, 5.0),
                         ({'equity_usd': -1.0}, 5.0), ({'equity_usd': 100.0}, None)):
            self.assertEqual(db.deployment_now(book, lp), {})


class DeploymentGuard(unittest.TestCase):
    def test_parts_that_do_not_add_up_give_no_share(self):
        ok = {'open': True, 'position_usd': 150.13, 'wallet_usd': 88.0, 'accrued_usd': 0.5}
        self.assertEqual(db._deployment(ok, 238.63)['deployed_pct'], 62.9)
        stale = dict(ok, wallet_usd=20.42)                             # the 16:22:55 OPEN book: parts short by $68
        self.assertIsNone(db._deployment(stale, 239.12)['deployed_pct'])
        edge = dict(ok, wallet_usd=88.0 + db.DEPLOYMENT_TOLERANCE_USD)
        self.assertIsNotNone(db._deployment(edge, 238.63)['deployed_pct'])
        past = dict(ok, wallet_usd=88.0 + db.DEPLOYMENT_TOLERANCE_USD + 0.01)
        self.assertIsNone(db._deployment(past, 238.63)['deployed_pct'])
        self.assertEqual(db._deployment(dict(ok, accrued_usd=None, wallet_usd=88.5), 238.63)['deployed_pct'], 62.9)

    def test_a_closed_position_is_zero_whatever_the_wallet(self):
        d = db._deployment({'open': False, 'position_usd': 150.0, 'wallet_usd': 1.0}, 240.0)
        self.assertEqual((d['lp_usd'], d['deployed_pct']), (0.0, 0.0))

    @settings(max_examples=400, deadline=None)
    @given(pos=money, wallet=money, accrued=money, is_open=st.booleans(), noise=st.floats(-5, 5))
    def test_the_share_is_in_range_and_true_or_absent(self, pos, wallet, accrued, is_open, noise):
        eq = pos + wallet + accrued + noise
        d = db._deployment({'open': is_open, 'position_usd': pos, 'wallet_usd': wallet, 'accrued_usd': accrued}, eq)
        p = d['deployed_pct']
        if p is None:
            return
        self.assertGreaterEqual(p, 0.0); self.assertLessEqual(p, 100.0)
        lp = pos if is_open else 0.0
        self.assertAlmostEqual(p, min(max(lp / eq * 100, 0), 100), delta=0.051)
        if is_open:
            self.assertLessEqual(abs(noise), db.DEPLOYMENT_TOLERANCE_USD + 1e-6)


class MoveBooks(unittest.TestCase):
    """notify_book at a move, against the test database."""

    def setUp(self):
        reset()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at, closed_at) values ('OLD', 'P', now() - interval '1 hour', now())")
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, wallet_usd, position_usd, equity_usd) "
                        "values (now(), 'OLD', 120, true, '1', 20.42, 218.70, 239.12)")
            cur.execute("insert into positions (mint, pool, opened_at) values ('NEW', 'P', now())")

    def book(self, event, **kw):
        sent = []
        with mock.patch.object(lp.books, 'notify', lambda ev, **p: sent.append(p)):
            lp.books.notify_book(event, **kw)
        return sent[0]

    def test_the_open_book_shows_the_new_position(self):
        b = self.book('OPEN', lp_now_usd=150.29)
        self.assertEqual((b['lp_usd'], b['wallet_usd'], b['deployed_pct']), (150.29, 88.83, 62.9))
        self.assertNotIn('lp_now_usd', b)

    def test_the_close_book_is_all_wallet(self):
        b = self.book('CLOSE', lp_now_usd=0.0)
        self.assertEqual((b['lp_usd'], b['wallet_usd'], b['deployed_pct']), (0.0, 239.12, 0.0))

    def test_a_poll_book_shows_a_share_only_when_its_parts_agree(self):
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set closed_at = null where mint = 'OLD'")
        self.assertEqual(self.book('in_band')['deployed_pct'], 91.5)          # 218.70 + 20.42 = 239.12
        with db.cursor(commit=True) as cur:
            cur.execute("update snapshots set equity_usd = 300.0")
        self.assertIsNone(self.book('in_band')['deployed_pct'])


class EveryMoveBookCarriesItsMark(unittest.TestCase):
    """Static: every OPEN and CLOSE book in the loop passes lp_now_usd, so a
    new move path cannot bring the stale "0%" back."""

    def test_scan(self):
        found = []
        for f in sorted(pathlib.Path(lp.paths.ROOT / 'lp').glob('*.py')):
            for node in ast.walk(ast.parse(f.read_text())):
                name = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None) if isinstance(node, ast.Call) else None
                if name == 'notify_book' and node.args \
                        and isinstance(node.args[0], ast.Constant) and node.args[0].value in ('OPEN', 'CLOSE'):
                    found.append((node.args[0].value, f'{f.name}:{node.lineno}', {k.arg for k in node.keywords}))
        self.assertGreaterEqual(len(found), 2)
        for ev, where, kws in found:
            self.assertIn('lp_now_usd', kws, f'{ev} book at lp/{where}')



class RegimeAtMove(unittest.TestCase):
    """The regime block of an OPEN book describes the band just opened; a
    CLOSE book holds nothing; moves_24h includes the move (audit 2026-09-30)."""
    VIEW = {'mode': 'HOT', 'choice': 1.03, 'held': 1.025, 'held_pct': 2.51, 'inside': True, 'p_held': 0.3,
            'probs': [[1.0, 0.9], [1.25, 0.8], [1.5, 0.6], [2.0, 0.4], [2.5, 0.2], [3.0, 0.081], [4.0, 0.05], [5.0, 0.02]],
            'moves_24h': 7}

    def test_open_describes_the_new_band(self):
        v = lp.books.regime_at_move(self.VIEW, 120 / 1.03, 120 * 1.03, 8)
        self.assertEqual((v['held'], v['held_pct'], v['inside'], v['p_held'], v['moves_24h']), (1.03, 3.0, True, 0.081, 8))
        self.assertEqual(self.VIEW['held'], 1.025)                                  # the poll's view is untouched

    def test_close_holds_nothing(self):
        v = lp.books.regime_at_move(self.VIEW, None, None, 8)
        self.assertEqual((v['held'], v['held_pct'], v['p_held'], v['inside'], v['moves_24h']), (None, None, None, False, 8))
        self.assertEqual(lp.books.regime_at_move(self.VIEW, None, None)['moves_24h'], 7)

    @settings(max_examples=300, deadline=None)
    @given(st.floats(50, 500), st.floats(1.001, 1.08))
    def test_property_held_pct_is_the_band(self, price, half):
        v = lp.books.regime_at_move(self.VIEW, price / half, price * half)
        self.assertAlmostEqual(v['held_pct'], (half - 1) * 100, delta=0.01)
        self.assertIn(v['held'], config.REGIME_WIDTHS)
        self.assertEqual(v['held'], min(config.REGIME_WIDTHS, key=lambda k: abs(k - half)))

    def test_the_book_uses_it_at_a_move_only(self):
        sent = []
        with mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.dict(lp.books.LAST_REGIME, {'view': self.VIEW}), \
                mock.patch.object(db, 'stats', lambda: {'equity_usd': 239.33}), \
                mock.patch.object(lp.books, 'notify', lambda ev, **p: sent.append(p)):
            lp.books.notify_book('OPEN', lp_now_usd=219.0, lower=120 / 1.03, upper=120 * 1.03, moves_24h_now=8)
            lp.books.notify_book('CLOSE', lp_now_usd=0.0, lower=118.0, upper=122.0, moves_24h_now=8)
            lp.books.notify_book('in_band', lower=118.0, upper=122.0)
        self.assertEqual((sent[0]['regime']['held_pct'], sent[0]['regime']['moves_24h']), (3.0, 8))
        self.assertIsNone(sent[1]['regime']['held']); self.assertEqual(sent[1]['regime']['moves_24h'], 8)
        self.assertIs(sent[2]['regime'], self.VIEW)
        for b in sent:
            self.assertNotIn('moves_24h_now', b); self.assertNotIn('lp_now_usd', b)


class CloseCountsItsMove(unittest.TestCase):
    def test_calm_times_is_recorded_before_the_close_book(self):
        src = pathlib.Path(lp.moves.__file__).read_text()
        i_book = src.index("notify_book('CLOSE'")
        i_times = src.rindex("state['calm_times'] = calm_recent + [now]", 0, i_book)
        self.assertLess(i_times, i_book)
        self.assertLess(i_book - i_times, 800)                                      # the same block, just before


if __name__ == '__main__':
    unittest.main()
