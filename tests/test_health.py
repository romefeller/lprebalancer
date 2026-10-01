"""Circuit breakers and venue failover (2026-09-30). A dependency that fails
backs off exponentially and is remembered across restarts; three venue
failures in a row fail over to a venue with similar on-chain income; a
re-centre whose only purpose is a swap waits while swaps fail."""
import time
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures
_fixtures.ensure_profile()

import config      # noqa: E402
import db          # noqa: E402
import health      # noqa: E402
import rebalancer  # noqa: E402


def clear():
    with db.cursor(commit=True) as cur:
        cur.execute('truncate health')


class Core(unittest.TestCase):
    def test_cooldown_doubles_and_is_capped(self):
        self.assertEqual([health.cooldown(n) for n in range(0, 8)],
                         [0.0, 600.0, 1200.0, 2400.0, 4800.0, 9600.0, 19200.0, 21600.0])
        self.assertEqual(health.cooldown(10 ** 6), health.MAX_S)
        self.assertEqual(health.cooldown(-3), 0.0)

    def test_states(self):
        r = health.empty('k'); now = 1000.0
        self.assertEqual(health.verdict(r, now), (health.CLOSED, True, 0.0))
        r = health.after_failure(r, now, 'x')
        self.assertEqual(health.verdict(r, now)[:2], (health.BACKOFF, False))
        self.assertEqual(health.verdict(r, now + 600), (health.PROBING, True, 0.0))         # cooled: yellow, allowed
        r = health.after_failure(health.after_failure(r, now + 600, 'x'), now + 1800, 'x')
        self.assertEqual((r['fails'], r['trips']), (3, 1))
        self.assertEqual(health.verdict(r, now + 1800)[:2], (health.TRIPPED, False))
        self.assertAlmostEqual(health.verdict(r, now + 1800)[2], 2400.0)
        r = health.after_failure(r, now + 5000, 'x')
        self.assertEqual(r['trips'], 1)                                                     # counted once per trip
        r = health.after_success(r, now + 9000)
        self.assertEqual(health.verdict(r, now + 9000), (health.CLOSED, True, 0.0))
        self.assertEqual((r['fails'], r['trips'], r['last_ok']), (0, 1, now + 9000))

    def test_the_error_is_kept_short(self):
        r = health.after_failure(health.empty('k'), 1.0, 'e' * 1000)
        self.assertEqual(len(r['last_error']), 300)
        self.assertEqual(health.after_failure(health.empty('k'), 1.0, None)['last_error'], '')

    @settings(max_examples=300, deadline=None)
    @given(st.lists(st.booleans(), max_size=40), st.floats(0, 1e5))
    def test_property_fails_are_the_trailing_run_and_waits_are_bounded(self, outcomes, gap):
        r, now = health.empty('k'), 0.0
        for ok in outcomes:
            now += gap
            r = health.after_success(r, now) if ok else health.after_failure(r, now, 'x')
        trail = 0
        for ok in reversed(outcomes):
            if ok:
                break
            trail += 1
        self.assertEqual(r['fails'], trail)
        state, allowed, wait = health.verdict(r, now)
        self.assertLessEqual(wait, health.MAX_S)
        self.assertEqual(state, health.CLOSED if trail == 0 else health.TRIPPED if trail >= health.TRIP_FAILS else health.BACKOFF)
        self.assertEqual(allowed, trail == 0)
        self.assertTrue(health.verdict(r, now + health.MAX_S)[1])                         # always probed within MAX_S
        trips = 0; run = 0
        for ok in outcomes:
            run = 0 if ok else run + 1
            trips += run == health.TRIP_FAILS
        self.assertEqual(r['trips'], trips)


class Persistence(unittest.TestCase):
    def setUp(self):
        clear()

    tearDown = setUp

    def test_a_restart_remembers(self):
        health.record_failure('swap', 'Indexed requests', now=100.0)
        health.record_failure('swap', 'Indexed requests', now=200.0)
        ok, st, wait, rec = health.allowed('swap', now=300.0)                # a new process reads the table
        self.assertEqual((ok, st, rec['fails']), (False, health.BACKOFF, 2))
        self.assertAlmostEqual(wait, 200.0 + 1200.0 - 300.0)
        health.record_success('swap', now=2000.0)
        self.assertEqual(health.allowed('swap', now=2000.0)[:2], (True, health.CLOSED))

    def test_a_closed_breaker_is_not_rewritten_every_poll(self):
        health.record_success('venue:x', now=1000.0)
        with mock.patch.object(db, 'health_put', side_effect=AssertionError('rewritten')):
            health.record_success('venue:x', now=1030.0)
        health.record_success('venue:x', now=1061.0)
        self.assertEqual(db.health_get('venue:x')['last_ok'], 1061.0)

    def test_a_broken_table_never_raises_and_allows(self):
        with mock.patch.object(db, 'health_get', side_effect=RuntimeError('db down')), \
                mock.patch.object(db, 'health_put', side_effect=RuntimeError('db down')), \
                mock.patch.object(db, 'health_all', side_effect=RuntimeError('db down')):
            self.assertEqual(health.record_failure('swap', 'x', now=1.0)['fails'], 1)
            self.assertEqual(health.record_success('swap', now=2.0)['fails'], 0)
            self.assertEqual(health.allowed('swap', now=3.0)[:2], (True, health.CLOSED))
            self.assertEqual(health.summary(), [])

    def test_summary_worst_first_with_emoji(self):
        for _ in range(3):
            health.record_failure('venue:orca', 'x', now=time.time())
        health.record_failure('swap', 'y', now=time.time())
        health.record_success('tape', now=time.time())
        s = health.summary()
        self.assertEqual([(x['key'], x['state'], x['emoji']) for x in s],
                         [('venue:orca', 'tripped', '🔴'), ('swap', 'backoff', '🟡'), ('tape', 'closed', '🟢')])


class ChainFeedsTheBreakers(unittest.TestCase):
    def setUp(self):
        clear()

    tearDown = setUp

    def test_keys(self):
        k = rebalancer.health_key
        self.assertEqual(k(('rebalance', 'A', 'B'), 'jupiter'), 'swap')
        self.assertIsNone(k(('quote',), 'jupiter'))
        for cmd in ('open', 'close', 'harvest'):
            self.assertEqual(k((cmd, 'M'), 'raydium-clmm'), 'venue:raydium-clmm')
        for cmd in ('status', 'balance', 'fees', ''):
            self.assertIsNone(k((cmd,), 'raydium-clmm'))
        self.assertIsNone(k((), 'orca'))

    def test_what_counts(self):
        f = rebalancer.counts_as_failure
        self.assertTrue(f('RPC endpoint refuses indexed reads (403: needs a personal token)'))
        self.assertTrue(f('custom program error: 0x1'))
        for e in ('refused: bad arg', 'HALT present: x', 'no signer for jupiter', '', None):
            self.assertFalse(f(e), e)

    def run_chain(self, answer, args=('open', 'M'), dex='raydium-clmm'):
        with mock.patch.object(rebalancer, '_chain', lambda *a, **k: answer):
            return rebalancer.chain(*args, dex=dex)

    def test_a_failure_and_a_success(self):
        self.run_chain((None, 'custom program error: 0x1'))
        self.assertEqual(db.health_get('venue:raydium-clmm')['fails'], 1)
        self.run_chain(({'signature': 'S'}, None))
        self.assertEqual(db.health_get('venue:raydium-clmm')['fails'], 0)
        self.run_chain((None, 'RPC endpoint refuses indexed reads (403: needs a personal token)'), ('rebalance', 'A', 'B', '1', '1', '--execute'), 'jupiter')
        self.assertEqual(db.health_get('swap')['fails'], 1)
        self.run_chain(({'noop': True}, None), ('rebalance',), 'jupiter')
        self.assertEqual(db.health_get('swap')['fails'], 0)

    def test_our_refusals_and_reads_do_not_count(self):
        self.run_chain((None, 'refused: newline in arg'))
        self.run_chain((None, 'HALT present: stop'))
        self.run_chain((None, 'timeout'), ('status',))
        self.assertIsNone(db.health_get('venue:raydium-clmm'))

    def test_a_sent_partial_is_not_a_venue_failure(self):
        self.run_chain(({'signature': 'S', 'partial': True}, 'partial transaction execution'))
        self.assertNotEqual((db.health_get('venue:raydium-clmm') or {}).get('fails'), 1)

    def test_the_answer_is_passed_through_unchanged(self):
        ans = ({'x': 1}, 'custom program error: 0x1')
        self.assertIs(self.run_chain(ans)[0], ans[0]); self.assertEqual(self.run_chain(ans)[1], ans[1])


class IdleWaitsForSwaps(unittest.TestCase):
    """Replay 2026-09-30: every swap fails; deploy_idle must not loop."""

    def setUp(self):
        clear()
        for n, v in (('DEPLOY_ALL', True), ('MAX_USD', 300.0), ('SIDE_CAP_FRACTION', 0.55), ('GAS_RESERVE_SOL', 0.05),
                     ('CAPITAL_USD', 190.0), ('PAYOUT_ENABLED', False), ('REBALANCE_SWAP', True)):
            p = mock.patch.object(config, n, v); p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        clear()

    STATUS = {'positionMint': 'M', 'price': 119.6, 'lowerPrice': 116.8, 'upperPrice': 122.7, 'positionUsd': 150.13, 'rentUsd': 0.0}
    WALLET = {'balanceA': 0.059 + 0.68, 'balanceB': 0.5, 'price': 119.6, 'quoteUsd': 1.0, 'nativeSide': 'A',
              'tokenA': 'SOL', 'tokenB': 'USDC', 'walletUsd': (0.059 + 0.68) * 119.6 + 0.5}

    def poll(self, state, now, opened_age=3600):
        moves, sent = [], []
        import datetime as dt
        opened = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=opened_age)
        with mock.patch.object(rebalancer.time, 'time', lambda: now), \
                mock.patch.object(rebalancer.db, 'position_opened', lambda m: opened), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'calm_budget_left', lambda s: 40), \
                mock.patch.object(rebalancer, 'voluntary_move_allowed', lambda s: True), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(rebalancer, 'rebalance', lambda *a, **k: moves.append(k)):
            rebalancer.deploy_idle(state, dict(self.STATUS), dict(self.WALLET), {'choice': 1.025}, 119.6)
        return moves, sent

    def test_a_day_of_failing_swaps(self):
        state = {'idle_baseline': {'mint': 'M', 'usd': 0.0}}
        t0 = time.time(); moves = 0; deferred = 0
        for i in range(288):                                      # a poll every 5 minutes for 24 h
            now = t0 + i * 300
            m, sent = self.poll(state, now)
            if m:
                moves += 1
                health.record_failure('swap', 'RPC endpoint refuses indexed reads', now=now)   # the reopen's swap fails
            deferred += sent.count('deploy_idle_deferred')
        self.assertLessEqual(moves, rebalancer.IDLE_DEPLOYS_PER_DAY)
        self.assertGreater(moves, 0)
        self.assertLessEqual(deferred, moves + 1)                 # one message per new failure, not per poll
        self.assertEqual(db.health_get('swap')['fails'], moves)

    def test_it_deploys_once_the_swap_works_again(self):
        now = time.time()
        health.record_failure('swap', 'x', now=now - 100)
        state = {'idle_baseline': {'mint': 'M', 'usd': 0.0}}
        self.assertEqual(self.poll(state, now)[0], [])
        health.record_success('swap', now=now + 1)
        self.assertEqual(len(self.poll(state, now + 2)[0]), 1)

    def test_the_daily_cap(self):
        now = time.time()
        state = {'idle_baseline': {'mint': 'M', 'usd': 0.0}, 'idle_deploys': [now - 100 * i for i in range(3)]}
        self.assertEqual(self.poll(state, now)[0], [])
        state['idle_deploys'] = [now - 86401] * 3
        self.assertEqual(len(self.poll(state, now)[0]), 1)
        self.assertEqual(rebalancer.idle_deploys_left([now - 10, now - 86400, now - 86399], now), 1)
        self.assertEqual(rebalancer.idle_deploys_left(None, now), 3)


def venue(dex, pct, hours=24, held=False, row=True):
    return {'dex': dex, 'address': f'POOL_{dex}', 'held': held, 'hours': hours, 'total_pct_day': pct,
            'row': {'dex': dex, 'address': f'POOL_{dex}', 'pair': 'SOL/USDC'} if row else None}


ARMED = ('orca', 'meteora-dlmm', 'raydium-clmm', 'byreal')
SIGS = {d: 'x' for d in ARMED}


class Pick(unittest.TestCase):
    def pick(self, venues, allowed=lambda d: True, held='raydium-clmm'):
        return rebalancer.failover_pick(held, venues, allowed, execute_dexes=ARMED, signers=SIGS, min_hours=6)

    def test_the_best_similar_venue(self):
        vs = [venue('raydium-clmm', 1.0, held=True), venue('orca', 0.85), venue('byreal', 0.95), venue('meteora-dlmm', 0.5)]
        self.assertEqual(self.pick(vs)['dex'], 'byreal')
        self.assertEqual(self.pick(vs, allowed=lambda d: d != 'byreal')['dex'], 'orca')
        self.assertIsNone(self.pick([venue('raydium-clmm', 1.0, held=True), venue('orca', 0.79)]))   # under 80%
        self.assertEqual(self.pick([venue('raydium-clmm', 1.0, held=True), venue('orca', 0.80)])['dex'], 'orca')

    def test_eligibility(self):
        h = venue('raydium-clmm', 1.0, held=True)
        self.assertIsNone(self.pick([h, venue('orca', 2.0, hours=5)]))                  # too little evidence
        self.assertIsNone(self.pick([h, venue('pancake', 2.0)]))                         # not armed
        self.assertIsNone(self.pick([h, venue('orca', 2.0, row=False)]))                 # no pool record
        self.assertIsNone(self.pick([h, venue('orca', 2.0)], allowed=lambda d: False))  # its breaker is open
        self.assertIsNone(self.pick([h, dict(venue('raydium-clmm', 2.0), held=False, address='OTHER')]))   # same venue
        self.assertIsNone(self.pick([]))

    def test_without_evidence_on_the_held_venue_any_eligible_one(self):
        self.assertEqual(self.pick([venue('raydium-clmm', 1.0, held=True, hours=1), venue('orca', 0.1)])['dex'], 'orca')
        self.assertEqual(self.pick([venue('orca', 0.1)])['dex'], 'orca')

    @settings(max_examples=300, deadline=None)
    @given(st.lists(st.tuples(st.sampled_from(ARMED + ('pancake',)), st.floats(0, 5), st.integers(0, 48),
                              st.booleans(), st.booleans()), max_size=8), st.floats(0, 5), st.integers(0, 48))
    def test_property_a_pick_is_eligible_and_the_best(self, rows, held_pct, held_hours):
        vs = [venue('raydium-clmm', held_pct, hours=held_hours, held=True)] + \
             [venue(d, p, hours=h, row=r) for d, p, h, r, _ in rows]
        bad = {d for d, _, _, _, blocked in rows if blocked}
        allowed = lambda d: d not in bad                                                 # noqa: E731
        got = self.pick(vs, allowed)
        floor = held_pct * 0.8 if held_hours >= 6 else None
        eligible = [v for v in vs[1:] if v['row'] and v['hours'] >= 6 and v['dex'] in ARMED
                    and v['dex'] != 'raydium-clmm' and allowed(v['dex']) and (floor is None or v['total_pct_day'] >= floor)]
        if not eligible:
            self.assertIsNone(got)
        else:
            self.assertIn(got, eligible)
            self.assertEqual(got['total_pct_day'], max(v['total_pct_day'] for v in eligible))


class Failover(unittest.TestCase):
    def setUp(self):
        clear()

    tearDown = setUp

    def trip(self, dex='raydium-clmm'):
        for _ in range(3):
            health.record_failure(f'venue:{dex}', 'custom program error: 0x1', now=time.time())

    def go(self, state, status, venues, pinned=False):
        calls = {'rebalance': [], 'repoint': [], 'reopen': [], 'notify': []}
        with mock.patch.object(rebalancer.config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(rebalancer.config, 'POOL', 'POOL_raydium-clmm'), \
                mock.patch.object(rebalancer.config, 'POOL_PINNED', pinned), \
                mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ARMED), \
                mock.patch.object(rebalancer.config, 'REGIME_ENABLED', False), \
                mock.patch.object(rebalancer, 'SIGNERS', SIGS), \
                mock.patch.object(rebalancer, 'venue_view', lambda p, q=1.0: venues), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: calls['notify'].append(ev)), \
                mock.patch.object(rebalancer, 'rebalance', lambda *a, **k: calls['rebalance'].append(k)), \
                mock.patch.object(rebalancer, 'repoint', lambda row: (calls['repoint'].append(row),
                                                                      setattr(rebalancer.config, 'POOL', row['address']),
                                                                      setattr(rebalancer.config, 'DEX', row['dex']))), \
                mock.patch.object(rebalancer, 'reopen', lambda s, r, band=None, recovering=False: calls['reopen'].append(r)):
            out = rebalancer.venue_failover(state, status, price=119.6, quote=1.0)
        return out, calls

    VENUES = [venue('raydium-clmm', 1.0, held=True), venue('byreal', 0.9)]

    def test_healthy_or_backing_off_does_nothing(self):
        out, c = self.go({}, None, self.VENUES)
        self.assertFalse(out); self.assertEqual(c['notify'], [])
        health.record_failure('venue:raydium-clmm', 'x', now=time.time())
        health.record_failure('venue:raydium-clmm', 'x', now=time.time())
        self.assertFalse(self.go({}, None, self.VENUES)[0])

    def test_tripped_without_a_position_repoints_and_reopens_with_the_intent(self):
        self.trip()
        state = {'pending_reopen': {'pool': 'POOL_raydium-clmm', 'dex': 'raydium-clmm', 'mint': 'M', 'closed': True}}
        out, c = self.go(state, None, self.VENUES)
        self.assertTrue(out)
        self.assertEqual(c['repoint'][0]['dex'], 'byreal'); self.assertEqual(len(c['reopen']), 1)
        self.assertEqual((state['pending_reopen']['dex'], state['pending_reopen']['pool']), ('byreal', 'POOL_byreal'))
        self.assertIn('FAILOVER', c['notify'])

    def test_tripped_with_a_position_moves_it(self):
        self.trip()
        out, c = self.go({}, {'positionMint': 'M', 'price': 119.6, 'quoteUsd': 1.0}, self.VENUES)
        self.assertTrue(out)
        self.assertEqual(c['rebalance'][0]['target']['dex'], 'byreal'); self.assertEqual(c['repoint'], [])

    def test_no_target_says_so_once_per_failure(self):
        self.trip(); state = {}
        vs = [venue('raydium-clmm', 1.0, held=True), venue('byreal', 0.5)]
        self.assertFalse(self.go(state, None, vs)[0])
        self.assertEqual(self.go(state, None, vs)[1]['notify'], [])                     # second poll: silent
        c = self.go(state, None, vs)[1]
        self.assertEqual(c['rebalance'] + c['repoint'] + c['reopen'], [])
        health.record_failure('venue:raydium-clmm', 'again', now=time.time() + 1)
        self.assertIn('failover_none', self.go(state, None, vs)[1]['notify'])

    def test_a_pinned_pool_never_fails_over(self):
        self.trip()
        out, c = self.go({}, None, self.VENUES, pinned=True)
        self.assertFalse(out); self.assertEqual(c['repoint'], []); self.assertIn('failover_none', c['notify'])

    def test_a_tripped_target_is_not_chosen(self):
        self.trip(); self.trip('byreal')
        out, c = self.go({}, None, self.VENUES)
        self.assertFalse(out)

    def test_no_price_no_move(self):
        self.trip()
        with mock.patch.object(rebalancer, 'venue_view', side_effect=AssertionError('read')):
            self.assertFalse(rebalancer.venue_failover({}, None, price=None))


class VoluntaryMovesRespectTheVenue(unittest.TestCase):
    def setUp(self):
        clear()

    tearDown = setUp

    def test_backoff_blocks_voluntary_moves(self):
        state = {'calm_times': [], 'last_rebalance': 0}
        with mock.patch.object(rebalancer.config, 'DEX', 'raydium-clmm'):
            self.assertTrue(rebalancer.voluntary_move_allowed(state))
            health.record_failure('venue:raydium-clmm', 'x')
            self.assertFalse(rebalancer.voluntary_move_allowed(state))
            health.record_success('venue:raydium-clmm')
            self.assertTrue(rebalancer.voluntary_move_allowed(state))


if __name__ == '__main__':
    unittest.main()
