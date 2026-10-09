"""Edges the 2026-09-30 mutation run found untested in the breakers, the
failover, the move books and the deployment share. One test per surviving
mutant family; the reason is in each test's name."""
import time
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import config      # noqa: E402
import db          # noqa: E402
import health      # noqa: E402
import lp.board  # noqa: E402
import lp.books  # noqa: E402
import lp.regime  # noqa: E402
import lp.signers  # noqa: E402
import lp.swaps  # noqa: E402
from test_health import clear, venue, ARMED, SIGS  # noqa: E402


class HealthEdges(unittest.TestCase):
    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def test_records_with_missing_fields(self):
        self.assertEqual(health.after_failure({'key': 'k'}, 1.0)['fails'], 1)
        r = health.after_failure({'key': 'k', 'fails': 2}, 1.0)
        self.assertEqual((r['fails'], r['trips']), (3, 1))
        for rec in (None, {}, {'fails': None}, {'fails': 0}):
            self.assertEqual(health.verdict(rec, 5.0), (health.CLOSED, True, 0.0), rec)
        self.assertEqual(health.verdict({'fails': 2, 'retry_at': None}, 5.0), (health.PROBING, True, 0.0))

    def test_no_use_even_half_a_second_early(self):
        self.assertFalse(health.verdict({'fails': 1, 'retry_at': 100.0}, 99.5)[1])
        self.assertTrue(health.verdict({'fails': 1, 'retry_at': 100.0}, 100.0)[1])

    def puts(self, fn):
        seen = []
        real = db.health_put
        with mock.patch.object(db, 'health_put', lambda r: (seen.append(dict(r)), real(r))):
            fn()
        return seen

    def test_success_rewrites_only_when_it_changes_something(self):
        self.assertEqual(len(self.puts(lambda: health.record_success('k', now=1000.0))), 1)       # no last_ok yet
        self.assertEqual(self.puts(lambda: health.record_success('k', now=1030.0)), [])
        self.assertEqual(self.puts(lambda: health.record_success('k', now=1059.9)), [])
        self.assertEqual(len(self.puts(lambda: health.record_success('k', now=1060.0))), 1)       # exactly a minute
        self.assertEqual(len(self.puts(lambda: health.record_success('k', now=1120.5))), 1)
        health.record_failure('k', 'x', now=1121.0)
        seen = self.puts(lambda: health.record_success('k', now=1122.0))                        # within the minute
        self.assertEqual(len(seen), 1); self.assertEqual(seen[0]['fails'], 0)                   # a failure is cleared at once
        self.assertEqual(db.health_get('k')['fails'], 0)

    def test_summary_orders_by_state_then_key(self):
        now = time.time()
        health.record_success('aaa', now=now)
        health.record_failure('zzz', 'x', now=now)
        for _ in range(3):
            health.record_failure('mmm', 'x', now=now)
        self.assertEqual([x['key'] for x in health.summary(now)], ['mmm', 'zzz', 'aaa'])
        with mock.patch.object(db, 'health_all', lambda: [{'key': 'n', 'fails': None, 'trips': None}]):
            s = health.summary(now)
        self.assertEqual((s[0]['fails'], s[0]['trips'], s[0]['state']), (0, 0, health.CLOSED))


class ChainEdges(unittest.TestCase):
    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def call(self, answer, *args, **kw):
        seen = {}

        def fake(*a, **k):
            seen.update(args=a, **k); return answer
        with mock.patch.object(lp.signers, '_chain', fake):
            lp.signers.chain(*args, **kw)
        return seen

    def test_defaults_reach_the_signer(self):
        seen = self.call(({'signature': 'S'}, None), 'open', 'M')
        self.assertEqual((seen['timeout'], seen['dex']), (420, config.DEX))
        self.assertIsNotNone(db.health_get(f'venue:{config.DEX}'))

    def test_outcomes(self):
        k = 'venue:orca'
        for answer, fails in ((
                (None, None), 1), (({'x': 1}, 'boom'), 2), (({'signature': 'S'}, 'partial'), 0),
                ((None, 'boom'), 1), (({'noop': True}, 'boom'), 0), (({'x': 1}, None), 0)):
            self.call(answer, 'close', 'M', dex='orca')
            self.assertEqual(db.health_get(k)['fails'], fails, answer)
        self.assertEqual(db.health_get(k)['last_error'], 'boom')
        self.call((None, None), 'close', 'M', dex='orca')
        self.assertEqual(db.health_get(k)['last_error'], 'no result')

    def test_a_refusal_neither_counts_nor_clears(self):
        self.call((None, 'boom'), 'open', 'M', dex='orca')
        self.call((None, 'refused: bad'), 'open', 'M', dex='orca')
        self.assertEqual(db.health_get('venue:orca')['fails'], 1)


class IdleEdges(unittest.TestCase):
    def setUp(self):
        clear()
        for n, v in (('DEPLOY_ALL', True), ('MAX_USD', 300.0), ('SIDE_CAP_FRACTION', 0.55), ('GAS_RESERVE_SOL', 0.05),
                     ('CAPITAL_USD', 190.0), ('PAYOUT_ENABLED', False), ('REBALANCE_SWAP', True)):
            p = mock.patch.object(config, n, v); p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        clear()

    def poll(self, state, now):
        from test_health import IdleWaitsForSwaps
        return IdleWaitsForSwaps.poll(IdleWaitsForSwaps(), state, now)

    def test_a_deferral_returns_false_exactly(self):
        now = time.time()
        health.record_failure('swap', 'x', now=now)
        st = {'idle_baseline': {'mint': 'M', 'usd': 0.0}}
        from test_health import IdleWaitsForSwaps
        t = IdleWaitsForSwaps()
        import datetime as dt
        opened = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        with mock.patch.object(time, 'time', lambda: now), \
                mock.patch.object(db, 'position_opened', lambda m: opened), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: 40), \
                mock.patch.object(lp.regime, 'voluntary_move_allowed', lambda s: True), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: None):
            out = lp.swaps.deploy_idle(st, dict(t.STATUS), dict(t.WALLET), {'choice': 1.025}, 119.6)
        self.assertIs(out, False)

    def test_one_deploy_left_is_used_and_old_ones_are_pruned(self):
        now = time.time()
        state = {'idle_baseline': {'mint': 'M', 'usd': 0.0}, 'idle_deploys': [now - 10, now - 20, now - 86400.5]}
        self.assertEqual(len(self.poll(state, now)[0]), 1)                          # 2 in 24 h: one left
        self.assertEqual(state['idle_deploys'], [now - 10, now - 20, now])


class VoluntaryGap(unittest.TestCase):
    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def test_the_gap_holds_even_when_healthy(self):
        now = time.time()
        with mock.patch.object(config, 'CALM_MIN_GAP', 600):
            self.assertFalse(lp.regime.voluntary_move_allowed({'calm_times': [now - 100], 'last_rebalance': 0}))
            self.assertFalse(lp.regime.voluntary_move_allowed({'calm_times': [], 'last_rebalance': now - 599}))
            self.assertTrue(lp.regime.voluntary_move_allowed({'calm_times': [now - 700], 'last_rebalance': now - 601}))


class PickEdges(unittest.TestCase):
    def pick(self, venues, **kw):
        args = dict(execute_dexes=ARMED, signers=SIGS, min_hours=6)
        args.update(kw)
        return lp.board.failover_pick('raydium-clmm', venues, lambda d: True, **args)

    def test_a_held_venue_without_an_income_figure(self):
        held = dict(venue('raydium-clmm', 0.0, held=True), total_pct_day=None)
        self.assertEqual(self.pick([held, venue('orca', 0.5)])['dex'], 'orca')     # floor 0: any income

    def test_armed_but_without_a_signer(self):
        self.assertIsNone(self.pick([venue('raydium-clmm', 1.0, held=True), venue('orca', 2.0)], signers={'byreal': 'x'}))

    def test_hours_unknown_is_no_evidence(self):
        v = dict(venue('orca', 2.0), hours=None)
        self.assertIsNone(self.pick([v], min_hours=1))
        h = dict(venue('raydium-clmm', 1.0, held=True), hours=None)
        self.assertEqual(self.pick([h, venue('orca', 0.1, hours=1)], min_hours=1)['dex'], 'orca')   # held unproven: no floor
        v2 = dict(venue('orca', None, hours=24))
        self.assertIsNone(self.pick([venue('raydium-clmm', 1.0, held=True), v2]))


class FailoverEdges(unittest.TestCase):
    def setUp(self):
        clear()
        for _ in range(3):
            health.record_failure('venue:raydium-clmm', 'x', now=time.time())

    def tearDown(self):
        clear()

    def run_it(self, status, price, venues, regime=False, choice=None, pinned=False, quote=1.0):
        seen = {'vv': [], 'reopen': [], 'rebalance': []}
        with mock.patch.object(config, 'DEX', 'raydium-clmm'), mock.patch.object(config, 'POOL', 'POOL_raydium-clmm'), \
                mock.patch.object(config, 'POOL_PINNED', pinned), mock.patch.object(config, 'EXECUTE_DEXES', ARMED), \
                mock.patch.object(config, 'REGIME_ENABLED', regime), mock.patch.object(lp.signers, 'SIGNERS', SIGS), \
                mock.patch.object(lp.board, 'venue_view', lambda p, q=1.0: (seen['vv'].append((p, q)), venues)[1]), \
                mock.patch.object(lp.regime, 'regime_choice_now', lambda *a: choice), \
                mock.patch.object(lp.paths, 'save', lambda s: None), mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: seen['rebalance'].append(k)), \
                mock.patch.object(lp.board, 'repoint', lambda row: None), \
                mock.patch.object(lp.moves, 'reopen', lambda s, r, band=None, recovering=False: seen['reopen'].append(band)):
            out = lp.board.venue_failover({}, status, price=price, quote=quote)
        return out, seen

    V = [venue('raydium-clmm', 1.0, held=True), venue('byreal', 0.9)]

    def test_every_no_move_is_false_exactly(self):
        self.assertIs(self.run_it(None, 119.6, self.V, pinned=True)[0], False)
        self.assertIs(self.run_it(None, None, self.V)[0], False)
        self.assertIs(self.run_it(None, 119.6, [self.V[0]])[0], False)
        clear()
        self.assertIs(self.run_it(None, 119.6, self.V)[0], False)

    def test_the_quote_price_reaches_the_venue_view(self):
        # 2026-10-01 (multi-wallet contract): an unknown quote price is no
        # dollar. No price, no ranking and no move.
        out, seen = self.run_it({'positionMint': 'M', 'price': 119.6}, None, self.V)
        self.assertEqual((out, seen['vv'], seen['rebalance']), (False, [], []))
        out, seen = self.run_it(None, 119.6, self.V, quote=None)
        self.assertEqual((out, seen['vv'], seen['reopen']), (False, [], []))
        _, seen = self.run_it(None, 119.6, self.V)
        self.assertEqual(seen['vv'][0], (119.6, 1.0))
        _, seen = self.run_it({'positionMint': 'M', 'price': 119.6, 'quoteUsd': 1.001}, None, self.V)
        self.assertEqual(seen['vv'][0], (119.6, 1.001))

    def test_the_width_under_regime_mode(self):
        self.assertEqual(self.run_it(None, 119.6, self.V, regime=True, choice=1.02)[1]['reopen'], [1.02])
        self.assertEqual(self.run_it(None, 119.6, self.V, regime=True, choice=None)[1]['reopen'], [config.REGIME_WIDTHS[-1]])
        self.assertEqual(self.run_it({'positionMint': 'M', 'price': 119.6, 'quoteUsd': 1.0}, None, self.V, regime=True, choice=1.03)[1]['rebalance'][0]['band'], 1.03)


class TidyEdges(unittest.TestCase):
    def test_nothing_is_nothing(self):
        self.assertIsNone(lp.books.tidy(None)); self.assertIsNone(lp.books.tidy(''))

    def test_a_program_error_starts_at_its_name_and_is_capped(self):
        t = lp.books.tidy('Simulation noise here ' + 'custom program error: 0x1771 ' + 'x' * 300)
        self.assertTrue(t.startswith('custom program error: 0x1771'), t)
        self.assertEqual(len(t), 140)


class RegimeAtMoveEdges(unittest.TestCase):
    VIEW = {'probs': [['3.0', 0.081]], 'held': 1.02}

    def test_no_band_without_a_proper_band(self):
        for lo, up in ((120.0, 120.0), (0.0, 121.0), (121.0, 120.0), (None, 121.0), (120.0, None), (-1.0, 1.0)):
            self.assertIsNone(lp.books.regime_at_move(self.VIEW, lo, up)['held'], (lo, up))

    def test_probs_as_strings_or_missing(self):
        self.assertEqual(lp.books.regime_at_move(self.VIEW, 120 / 1.03, 120 * 1.03)['p_held'], 0.081)
        self.assertIsNone(lp.books.regime_at_move({'probs': None}, 120 / 1.03, 120 * 1.03)['p_held'])
        self.assertIsNone(lp.books.regime_at_move({}, 120 / 1.03, 120 * 1.03)['p_held'])


class NotifyBookAttachments(unittest.TestCase):
    def go(self, **payload):
        sent = []
        with mock.patch.object(db, 'stats', lambda: {}), \
                mock.patch.object(health, 'summary', lambda: []), \
                mock.patch.object(lp.books, 'notify', lambda ev, **p: sent.append(p)):
            lp.books.notify_book('in_band', **payload)
        return sent[0]

    def test_calm_regime_and_venues_are_attached_unless_given(self):
        venues = [{'dex': str(i)} for i in range(6)]
        with mock.patch.object(config, 'CALM_ENABLED', True), mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.dict(lp.books.LAST_CALM, {'view': {'c': 1}}), \
                mock.patch.dict(lp.books.LAST_REGIME, {'view': {'r': 1}}), \
                mock.patch.dict(lp.books.LAST_VENUES, {'view': venues}):
            b = self.go()
            self.assertEqual((b['calm'], b['regime'], len(b['venues'])), ({'c': 1}, {'r': 1}, 4))
            b = self.go(calm={'mine': 1}, regime={'mine': 2}, venues=[{'dex': 'x'}], health=['h'])
            self.assertEqual((b['calm'], b['regime'], b['venues'], b['health']), ({'mine': 1}, {'mine': 2}, [{'dex': 'x'}], ['h']))
        with mock.patch.object(config, 'CALM_ENABLED', False), mock.patch.object(config, 'REGIME_ENABLED', False), \
                mock.patch.dict(lp.books.LAST_CALM, {'view': {'c': 1}}), \
                mock.patch.dict(lp.books.LAST_REGIME, {'view': {'r': 1}}), \
                mock.patch.dict(lp.books.LAST_VENUES, {'view': None}):
            b = self.go()
            self.assertNotIn('calm', b); self.assertNotIn('regime', b); self.assertNotIn('venues', b)


class DeploymentEdges(unittest.TestCase):
    def test_pct(self):
        for whole in (None, 0, 0.0, -1.0):
            self.assertIsNone(db._pct(5.0, whole), whole)
        self.assertEqual(db._pct(200.0, 100.0), 100.0); self.assertEqual(db._pct(-5.0, 100.0), 0.0)
        self.assertEqual(db._pct(50.0, 100.0), 50.0)

    def test_the_consistency_check_needs_a_wallet_and_an_open_position(self):
        self.assertEqual(db._deployment({'open': True, 'position_usd': 150.0, 'wallet_usd': None}, 239.0)['deployed_pct'], 62.8)
        self.assertEqual(db._deployment({'open': False, 'position_usd': 150.0, 'wallet_usd': 10.0}, 239.0)['deployed_pct'], 0.0)



class LastSurvivors(unittest.TestCase):
    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def test_summary_carries_trips(self):
        for _ in range(3):
            health.record_failure('v', 'x', now=time.time())
        self.assertEqual(health.summary()[0]['trips'], 1)

    def test_held_evidence_at_exactly_min_hours_sets_the_floor(self):
        vs = [venue('raydium-clmm', 1.0, held=True, hours=6), venue('orca', 0.5, hours=6)]
        self.assertIsNone(lp.board.failover_pick('raydium-clmm', vs, lambda d: True, execute_dexes=ARMED, signers=SIGS, min_hours=6))

    def test_a_signer_without_execute_permission_is_not_a_target(self):
        vs = [venue('raydium-clmm', 1.0, held=True), venue('pancake', 2.0)]
        self.assertIsNone(lp.board.failover_pick('raydium-clmm', vs, lambda d: True, execute_dexes=ARMED,
                                                   signers={**SIGS, 'pancake': 'x'}, min_hours=6))

    def test_a_status_without_a_position_reopens(self):
        for _ in range(3):
            health.record_failure('venue:raydium-clmm', 'x', now=time.time())
        out, seen = FailoverEdges.run_it(FailoverEdges(), {'price': 119.6, 'quoteUsd': 1.0}, None, FailoverEdges.V)
        self.assertTrue(out); self.assertEqual(seen['rebalance'], []); self.assertEqual(len(seen['reopen']), 1)

    def test_a_pair_priced_below_one(self):
        v = lp.books.regime_at_move({'probs': [['3.0', 0.1]]}, 0.8 / 1.03, 0.8 * 1.03)
        self.assertEqual((v['held'], v['p_held']), (1.03, 0.1))

    def test_no_view_no_attachment(self):
        sent = []
        with mock.patch.object(config, 'CALM_ENABLED', True), mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.dict(lp.books.LAST_CALM, {}, clear=True), mock.patch.dict(lp.books.LAST_REGIME, {}, clear=True), \
                mock.patch.object(db, 'stats', lambda: {}), mock.patch.object(health, 'summary', lambda: []), \
                mock.patch.object(lp.books, 'notify', lambda ev, **p: sent.append(p)):
            lp.books.notify_book('in_band')
        self.assertNotIn('calm', sent[0]); self.assertNotIn('regime', sent[0])

    def test_no_equity_no_share_and_no_error(self):
        d = db._deployment({'open': True, 'position_usd': 150.0, 'wallet_usd': 10.0}, None)
        self.assertEqual((d['lp_usd'], d['deployed_pct']), (150.0, None))



class Round3(unittest.TestCase):
    def setUp(self):
        clear()

    def tearDown(self):
        clear()

    def test_record_success_returns_the_record_and_takes_missing_fields(self):
        health.record_success('k', now=1000.0)
        r = health.record_success('k', now=1010.0)                      # the skip path
        self.assertEqual((r['key'], r['fails'], r['last_ok']), ('k', 0, 1000.0))
        with mock.patch.object(health, 'load', lambda key: {'key': key, 'fails': None, 'last_ok': 1000.0}):
            self.assertEqual(health.record_success('k', now=1010.0)['fails'], None)   # skipped: fails None counts as 0

    def test_summary_carries_fails(self):
        for _ in range(3):
            health.record_failure('v', 'x', now=time.time())
        self.assertEqual(health.summary()[0]['fails'], 3)

    def deferrals(self, state, now):
        from test_health import IdleWaitsForSwaps
        sent = []
        t = IdleWaitsForSwaps()
        import datetime as dt
        opened = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        with mock.patch.object(time, 'time', lambda: now), \
                mock.patch.object(db, 'position_opened', lambda m: opened), \
                mock.patch.object(db, 'event', lambda *a: None), \
                mock.patch.object(lp.regime, 'calm_budget_left', lambda s: 40), \
                mock.patch.object(lp.regime, 'voluntary_move_allowed', lambda s: True), \
                mock.patch.object(lp.paths, 'save', lambda s: None), \
                mock.patch.object(lp.books, 'notify', lambda ev, **k: sent.append((ev, k))), \
                mock.patch.object(lp.moves, 'rebalance', lambda *a, **k: None):
            for dt_ in (0, 300, 600):
                lp.swaps.deploy_idle(state, dict(t.STATUS), dict(t.WALLET), {'choice': 1.025}, 119.6)
        return [k for ev, k in sent if ev == 'deploy_idle_deferred']

    def test_one_deferral_message_per_cause_with_its_reason(self):
        for n, v in (('DEPLOY_ALL', True), ('MAX_USD', 300.0), ('SIDE_CAP_FRACTION', 0.55), ('GAS_RESERVE_SOL', 0.05),
                     ('CAPITAL_USD', 190.0), ('PAYOUT_ENABLED', False), ('REBALANCE_SWAP', True)):
            p = mock.patch.object(config, n, v); p.start(); self.addCleanup(p.stop)
        now = time.time()
        health.record_failure('swap', 'x', now=now)
        d = self.deferrals({'idle_baseline': {'mint': 'M', 'usd': 0.0}}, now)
        self.assertEqual(len(d), 1); self.assertIn('the swap failed 1x in a row', d[0]['reason'])
        clear()
        d = self.deferrals({'idle_baseline': {'mint': 'M', 'usd': 0.0}, 'idle_deploys': [now - 1, now - 2, now - 3]}, now)
        self.assertEqual(len(d), 1); self.assertIn('3 idle deploys in 24 h already', d[0]['reason'])

    def test_unknown_incomes_rank_last(self):
        h = dict(venue('raydium-clmm', 1.0, held=True), hours=1)
        vs = [h, dict(venue('orca', None), total_pct_day=None), venue('byreal', 0.5)]
        self.assertEqual(lp.board.failover_pick('raydium-clmm', vs, lambda d: True, execute_dexes=ARMED,
                                                  signers=SIGS, min_hours=6)['dex'], 'byreal')

    def test_a_failover_move_is_a_voluntary_move(self):
        for _ in range(3):
            health.record_failure('venue:raydium-clmm', 'x', now=time.time())
        _, seen = FailoverEdges.run_it(FailoverEdges(), {'positionMint': 'M', 'price': 119.6, 'quoteUsd': 1.0}, None, FailoverEdges.V)
        self.assertIs(seen['rebalance'][0]['calm_move'], True)


if __name__ == '__main__':
    unittest.main()
