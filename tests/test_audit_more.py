"""Edges of the capital book, the loop hooks and the sizing, pinned one by one
(from the mutation run of 2026-09-28)."""
import datetime as dt
import unittest
from unittest import mock

import _fixtures
import config
import db
import rebalancer
from test_audit import reset

SOL, USDC = 'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'


def base(ts='2026-09-22 00:00+00', sol=2.0, usdc=0.0, usd=240.0, price=120.0):
    with db.cursor(commit=True) as cur:
        cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price) values (%s, 'baseline', %s, %s, %s, %s)",
                    (ts, sol, usdc, usd, price))


def snap(ts, equity, price):
    with db.cursor(commit=True) as cur:
        cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values (%s,'M',%s,true,'1',%s)",
                    (ts, price, equity))


def pay(ts, usd, kind):
    with db.cursor(commit=True) as cur:
        cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind) values "
                    "(%s,'sol-usdc','M','USDC','USDC',%s,%s,%s)", (ts, usd, usd, kind))


class SinceStartEdges(unittest.TestCase):
    def setUp(self):
        reset()

    def test_days_percent_and_the_latest_snapshot(self):
        base()
        snap('2026-09-23 00:00+00', 200.0, 100.0)
        snap('2026-09-24 12:00+00', 252.0, 126.0)
        s = db.since_start()
        self.assertEqual(s['days'], 2.5)
        self.assertEqual((s['equity_usd'], s['price_now']), (252.0, 126.0))
        self.assertEqual(s['profit_usd'], 12.0); self.assertEqual(s['profit_pct'], 5.0)
        self.assertEqual(s['uncounted_usd'], 0.0); self.assertEqual(s['price_start'], 120.0)
        self.assertEqual(s['hold_start_assets_usd'], 252.0); self.assertEqual(s['vs_hold_start_assets_usd'], 0.0)
        self.assertEqual(s['hold_50_50_usd'], 246.0); self.assertEqual(s['vs_hold_50_50_usd'], 6.0)
        self.assertEqual(s['since'], '2026-09-22T00:00:00+00:00')

    def test_a_snapshot_without_equity_is_skipped_and_ties_take_the_last_written(self):
        base()
        snap('2026-09-23 00:00+00', 250.0, 125.0)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values ('2026-09-24 00:00+00','M',1,true,'1',null)")
        self.assertEqual(db.since_start()['equity_usd'], 250.0)
        snap('2026-09-25 00:00+00', 251.0, 125.0); snap('2026-09-25 00:00+00', 252.0, 126.0)
        self.assertEqual(db.since_start()['equity_usd'], 252.0)

    def test_only_paid_and_uncertain_payouts_left_the_book(self):
        base(); snap('2026-09-23 00:00+00', 240.0, 120.0)
        for k, v in (('paid', 1.0), ('uncertain', 0.5), ('owed', 7.0), ('reinvested', 9.0), ('gas', 3.0), ('settled', 4.0)):
            pay('2026-09-22 12:00+00', v, k)
        self.assertEqual(db.since_start()['paid_out_usd'], 1.5)

    def test_usdc_in_the_baseline_and_in_flows(self):
        base(sol=1.0, usdc=120.0, usd=240.0)
        snap('2026-09-23 00:00+00', 240.0, 150.0)
        db.record_flow('2026-09-22 06:00+00', 'deposit', 0.0, 30.0, 30.0, 120.0, 'D', 'x')
        db.record_flow('2026-09-22 07:00+00', 'withdrawal', 0.0, 10.0, 10.0, 120.0, 'W', 'x')
        s = db.since_start()
        self.assertEqual(s['hold_start_assets_usd'], 1.0 * 150.0 + 120.0 + 20.0)
        self.assertEqual(s['start_usd'], 260.0)

    def test_a_zero_start_has_no_percent(self):
        base(sol=0.0, usd=0.0)
        snap('2026-09-23 00:00+00', 10.0, 120.0)
        self.assertIsNone(db.since_start()['profit_pct'])

    def test_no_snapshot_no_answer(self):
        base()
        self.assertIsNone(db.since_start())

    def test_the_book_uses_zero_when_no_audit_has_run(self):
        base(); snap('2026-09-23 00:00+00', 240.0, 120.0)
        s = db._since_start_or_none()
        self.assertIsNotNone(s); self.assertEqual(s['uncounted_usd'], 0.0)

    def test_old_audit_rows_are_pruned_recent_ones_kept(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into audits (ts, run_id, check_name, status) values (now() - interval '31 days', 'o', 'x', 'ok'),"
                        "(now() - interval '2 hours', 'r', 'x', 'ok')")
        db.record_audit('n', 'x', 'ok', {})
        with db.cursor() as cur:
            cur.execute('select run_id from audits order by ts')
            self.assertEqual([r['run_id'] for r in cur.fetchall()], ['r', 'n'])


class Hooks(unittest.TestCase):
    def go(self, answers, state=None):
        seen, calls = [], []
        it = iter(answers)
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or next(it))), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'load', lambda: {}), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
            out = rebalancer.janitor({} if state is None else state)
        return out, calls, seen

    PLAN = {'closable': [{'mint': 'M', 'lamports': 2039280}], 'reclaimSol': 0.00203928}

    def test_a_plan_with_an_error_is_not_acted_on(self):
        out, calls, seen = self.go([(self.PLAN, 'partial read')])
        self.assertIsNone(out); self.assertEqual(len(calls), 1); self.assertEqual(seen, ['janitor_failed'])

    def test_a_signature_with_an_error_is_a_failure(self):
        out, calls, seen = self.go([(self.PLAN, None), (dict(self.PLAN, signature='S'), 'confirm failed')])
        self.assertIsNone(out); self.assertEqual(seen, ['janitor_failed'])
        out, _, seen = self.go([(self.PLAN, None), (None, None)])
        self.assertIsNone(out); self.assertEqual(seen, ['janitor_failed'])

    def test_a_crash_is_reported(self):
        with mock.patch.object(rebalancer, 'pool_tokens', side_effect=RuntimeError('api')), \
                mock.patch.object(rebalancer, 'save', lambda s: None):
            seen = []
            with mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
                self.assertIsNone(rebalancer.janitor({}))
        self.assertEqual(seen, ['janitor_failed'])

    def test_the_audit_runs_again_after_exactly_an_hour(self):
        runs = []
        with mock.patch.object(rebalancer.audit, 'run', lambda *a, **k: runs.append(1) or {}), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.time, 'time', lambda: 10_000.0):
            rebalancer.run_audits({'last_audit': 10_000.0 - 3600})
            rebalancer.run_audits({'last_audit': 10_000.0 - 3599})
            rebalancer.run_audits({})
        self.assertEqual(len(runs), 2)


class QuoteFallbacks(unittest.TestCase):
    def test_a_missing_quote_price_is_unknown_not_a_dollar(self):
        # 2026-10-01 (multi-wallet contract): a null quoteUsd is unknown on
        # every path that values money; the open never sizes from it.
        with mock.patch.object(config, 'DEPLOY_ALL', True), mock.patch.object(config, 'MAX_USD', 300.0), \
                mock.patch.object(config, 'SIDE_CAP_FRACTION', 0.55), mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05):
            b = {'balanceA': 1.0, 'balanceB': 100.0, 'price': 120.0, 'quoteUsd': None, 'nativeSide': 'A'}
            res = 0.05 + rebalancer.OPEN_RENT_HEADROOM_SOL
            self.assertIsNone(rebalancer.deployable_usd(b))
            self.assertRaises(ValueError, rebalancer.capital, b)
            self.assertRaises(TypeError, rebalancer.deposit_caps, b)
            self.assertIsNone(rebalancer.position_usd({'closeEstA': 1.0, 'closeEstB': 5.0, 'price': 120.0}))
            b1 = dict(b, quoteUsd=1.0)
            a, bb = rebalancer.deposit_caps(b1)
            self.assertAlmostEqual(a, 1.0 - res); self.assertAlmostEqual(bb, 100.0)
            b2 = dict(b, quoteUsd=2.0)
            self.assertAlmostEqual(rebalancer.deployable_usd(b2), ((1.0 - res) * 120.0 + 100.0) * 2.0)


if __name__ == '__main__':
    unittest.main()


class Deployment(unittest.TestCase):
    def test_in_lp_share_and_wallet(self):
        d = db._deployment({'open': True, 'position_usd': 221.24, 'wallet_usd': 18.73}, 240.0)
        self.assertEqual(d, {'lp_usd': 221.24, 'wallet_usd': 18.73, 'deployed_pct': 92.2})

    def test_a_closed_position_holds_nothing(self):
        d = db._deployment({'open': False, 'position_usd': 221.24, 'wallet_usd': 239.0}, 240.0)
        self.assertEqual((d['lp_usd'], d['deployed_pct']), (0.0, 0.0))

    def test_missing_figures(self):
        self.assertEqual(db._deployment(None, 240.0), {'lp_usd': None, 'wallet_usd': None, 'deployed_pct': None})
        d = db._deployment({'open': True, 'position_usd': 100.0, 'wallet_usd': None}, None)
        self.assertEqual(d, {'lp_usd': 100.0, 'wallet_usd': None, 'deployed_pct': None})
        self.assertIsNone(db._deployment({'open': True, 'position_usd': 100.0, 'wallet_usd': 1.0}, 0.0)['deployed_pct'])

    def test_the_book_carries_them(self):
        reset()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at) values ('LPM', 'P', now())")
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, wallet_usd, position_usd, equity_usd) "
                        "values (now(), 'LPM', 120, true, '1', 18.73, 221.24, 240.0)")
        s = db.stats()
        self.assertEqual((s['lp_usd'], s['wallet_usd'], s['deployed_pct']), (221.24, 18.73, 92.2))


class KeepRewardAccounts(unittest.TestCase):
    def test_the_pools_reward_mints_are_kept_even_ended(self):
        import audit, types
        bot = types.SimpleNamespace(load=lambda: {}, pool_record=lambda: {'reward_mints': ['RAYMINT', None]})
        self.assertIn('RAYMINT', audit.keep_mints(bot, 'A', 'B'))
        self.assertNotIn(None, audit.keep_mints(bot, 'A', 'B'))
        broken = types.SimpleNamespace(load=lambda: {}, pool_record=lambda: (_ for _ in ()).throw(RuntimeError('api')))
        self.assertEqual(audit.keep_mints(broken, 'A', 'B'), {audit.NATIVE, audit.USDC, 'A', 'B'})
        empty = types.SimpleNamespace(load=lambda: {}, pool_record=lambda: {'reward_mints': None})
        self.assertEqual(audit.keep_mints(empty, 'A', 'B'), {audit.NATIVE, audit.USDC, 'A', 'B'})


class JanitorKeepsWhatComesBack(unittest.TestCase):
    def test_a_recreated_mint_is_kept_from_then_on(self):
        calls, seen = [], []
        answers = iter([({'closable': [{'mint': 'RAY', 'lamports': 1488440}, {'mint': 'MSOL', 'lamports': 2039280}], 'reclaimSol': 0.0035}, None),
                        ({'closable': [{'mint': 'MSOL', 'lamports': 2039280}], 'reclaimSol': 0.00204}, None),
                        ({'closable': [{'mint': 'MSOL', 'lamports': 2039280}], 'reclaimSol': 0.00204, 'signature': 'J'}, None)])
        state = {'janitor_closed': ['RAY']}
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or next(answers))), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'load', lambda: state), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
            out = rebalancer.janitor(state)
        self.assertEqual(state['janitor_keep'], ['RAY'])
        self.assertIn('RAY', calls[1]); self.assertIn('RAY', calls[2])        # kept in the re-plan and the close
        self.assertEqual(out['signature'], 'J')
        self.assertEqual(state['janitor_closed'], ['MSOL', 'RAY'])
        import audit, types
        self.assertIn('RAY', audit.keep_mints(types.SimpleNamespace(load=lambda: state, pool_record=lambda: {}), 'A', 'B'))


class JanitorReplan(unittest.TestCase):
    def go(self, answers, state):
        calls, seen = [], []
        it = iter(answers)
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or next(it))), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'load', lambda: state), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
            out = rebalancer.janitor(state)
        return out, calls, seen

    FIRST = ({'closable': [{'mint': 'RAY', 'lamports': 1}], 'reclaimSol': 1e-9}, None)

    def test_a_failed_replan_closes_nothing(self):
        for bad in ((None, 'rpc'), ({'closable': []}, 'partial'), (None, None)):
            out, calls, seen = self.go([self.FIRST, bad], {'janitor_closed': ['RAY']})
            self.assertIsNone(out); self.assertEqual(len(calls), 2, bad); self.assertEqual(seen, ['janitor_failed'])

    def test_a_replan_with_nothing_left_sends_nothing(self):
        out, calls, seen = self.go([self.FIRST, ({'closable': [], 'reclaimSol': 0}, None)], {'janitor_closed': ['RAY']})
        self.assertEqual(out, {'closable': [], 'reclaimSol': 0}); self.assertEqual(len(calls), 2); self.assertEqual(seen, [])

    def test_a_plan_without_a_list_is_nothing_to_close(self):
        out, calls, seen = self.go([({'closable': None}, None)], {'janitor_closed': ['RAY']})
        self.assertEqual(out, {'closable': None}); self.assertEqual(seen, []); self.assertEqual(len(calls), 1)

    def test_the_keep_list_grows_and_is_never_replaced(self):
        state = {'janitor_closed': ['RAY'], 'janitor_keep': ['OLD']}
        self.go([self.FIRST, ({'closable': []}, None)], state)
        self.assertEqual(state['janitor_keep'], ['OLD', 'RAY'])


class JanitorUnsignedClose(JanitorReplan):
    def test_an_unsigned_close_records_nothing_as_closed(self):
        state = {}
        out, calls, seen = self.go([({'closable': [{'mint': 'MSOL', 'lamports': 1}], 'reclaimSol': 1e-9}, None),
                                    ({'closable': [{'mint': 'MSOL', 'lamports': 1}], 'reclaimSol': 1e-9}, None)], state)
        self.assertIsNone(out); self.assertEqual(seen, ['janitor_failed'])
        self.assertNotIn('janitor_closed', state)
