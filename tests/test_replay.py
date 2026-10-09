"""Replay testing: recorded polls and tape through today's code.

  PollReplay    every tests/replay/polls_*.jsonl.gz line: rebalancer.poll_verdict
                on what the poll saw must give the verdict stored with it
  WidthReplay   every tests/replay/tape_*.jsonl.gz point: calm.regime_view and
                calm.regime_decide on the bars closed by then must give the
                stored width view
  Properties    poll_verdict over generated polls: pure, JSON-stable, and every
                move passes its gates
  SameAsBefore  poll_verdict against the loop's inline rules it replaced
  Recording     poll_seen, record_poll and db.record_replay_poll (sql/032)

A failure here is a changed verdict. When the change is intended, capture
new fixtures with tests/replay_capture.py and replace the old ones.
"""
import copy
import gzip
import json
import math
import pathlib
import time
import unittest
from unittest import mock

import numpy as np
from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import calm
import db
import rebalancer
import lp.polls
import lp.regime
import config
import health
import replay_capture

FIXTURES = pathlib.Path(__file__).resolve().parent / 'replay'
AT = 1_800_000_000.0
W8 = [1.01, 1.0125, 1.015, 1.02, 1.025, 1.03, 1.04, 1.05]
FAST = settings(max_examples=400, deadline=None)


def lines(path):
    with gzip.open(path, 'rt') as fh:
        return [json.loads(x) for x in fh if x.strip()]


class PollReplay(unittest.TestCase):
    def test_fixtures_exist(self):
        self.assertTrue(list(FIXTURES.glob('polls_*.jsonl.gz')))

    def test_every_recorded_poll_keeps_its_verdict(self):
        for path in sorted(FIXTURES.glob('polls_*.jsonl.gz')):
            for i, row in enumerate(lines(path)):
                with self.subTest(fixture=path.name, line=i + 1):
                    self.assertEqual(lp.polls.poll_verdict(row['seen']), row['verdict'])

    def test_every_verdict_kind_is_covered(self):
        acts, flags = set(), set()
        for path in FIXTURES.glob('polls_*.jsonl.gz'):
            for row in lines(path):
                acts.add(row['verdict']['act'])
                flags |= {k for k in ('deferred', 'harvest') if row['verdict'][k]}
        self.assertEqual(acts, {None, 'exit', 'regime_widen', 'regime_narrow', 'calm_narrow', 'calm_recentre',
                                'calm_widen', 'proactive'})
        self.assertEqual(flags, {'deferred', 'harvest'})


class WidthReplay(unittest.TestCase):
    EXACT = ('mode', 'choice', 'held', 'inside', 'move', 'bars')

    def test_fixtures_exist(self):
        self.assertTrue(list(FIXTURES.glob('tape_*.jsonl.gz')))

    def test_every_tape_point_keeps_its_width(self):
        for path in sorted(FIXTURES.glob('tape_*.jsonl.gz')):
            head, *points = lines(path)
            bars = tuple(np.asarray(a, dtype=float) for a in head['bars'])
            widths = tuple(head['widths'])
            for p in points:
                with self.subTest(fixture=path.name, at=p['at']):
                    got = replay_capture.width_view(bars, p['at'], p['price'], p['lower'], p['upper'], widths,
                                                    head['horizon'], p['threshold'], head['steps'])
                    want = p['view']
                    for k in self.EXACT:
                        self.assertEqual(got[k], want[k], k)
                    for k in ('p_held', 'sigma_5m_pct', 'velocity'):
                        self.assertAlmostEqual(got[k], want[k], delta=1e-3, msg=k)
                    self.assertEqual([w for w, _ in got['probs']], [w for w, _ in want['probs']])
                    for (_, a), (_, b) in zip(got['probs'], want['probs']):
                        self.assertTrue(a == b or abs(a - b) <= 1e-3, (a, b))

    def test_points_only_see_closed_bars(self):
        bars = (np.arange(5.0) * 300,) + tuple(np.full(5, 100.0) for _ in range(5))
        with mock.patch.object(calm, 'regime_view', side_effect=lambda b, *a, **kw: {
                **{k: None for k in replay_capture.KEPT}, 'held': None, 'inside': True, 'n': len(b[0])}):
            self.assertEqual(replay_capture.width_view(bars, 900.0, 100, 99, 101, tuple(W8), 120, 0.25, 2)['bars'], 3)
            self.assertEqual(replay_capture.width_view(bars, 899.0, 100, 99, 101, tuple(W8), 120, 0.25, 2)['bars'], 2)


# --- generated polls -------------------------------------------------------

widths_st = st.just(W8)
knobs_st = st.fixed_dictionaries({
    'regime_enabled': st.booleans(), 'calm_enabled': st.booleans(), 'widths': widths_st,
    'steps': st.integers(1, 3), 'calm_band': st.just(1.01), 'calm_threshold': st.sampled_from([0.2, 0.25]),
    'calm_min_gap': st.sampled_from([0, 600]), 'calm_max_moves': st.integers(0, 4),
    'proactive_threshold': st.sampled_from([0, 0.5]), 'harvest_interval': st.sampled_from([0, 86400]),
    'min_harvest_usd': st.sampled_from([0, 0.25])})
prob = st.one_of(st.none(), st.floats(0, 1))
regime_st = st.one_of(st.none(), st.fixed_dictionaries({
    'mode': st.sampled_from(['CALM', 'WARM', 'HOT', 'STALE']), 'choice': st.sampled_from(W8),
    'choice_pct': st.just(1.0), 'held': st.one_of(st.none(), st.sampled_from(W8)), 'held_pct': st.just(1.0),
    'inside': st.booleans(), 'stale': st.one_of(st.none(), st.booleans()), 'p_held': prob,
    'sigma_5m_pct': st.just(0.1), 'velocity': st.just(0.0)}))
calm_st = st.one_of(st.none(), st.fixed_dictionaries({
    'calm': st.booleans(), 'tight_held': st.booleans(), 'p_touch': prob, 'p_touch_fresh': prob,
    'threshold': st.sampled_from([0.2, 0.25]), 'sigma_5m_pct': st.just(0.05), 'cut_pct': st.just(0.08)}))
fc_st = st.one_of(st.none(), st.fixed_dictionaries({'act': st.booleans(), 'p_exit_horizon': prob}))
ago = st.floats(0, 200000)


@st.composite
def seen_st(draw):
    lower = draw(st.floats(50, 150)); upper = lower * draw(st.floats(1.001, 1.2))
    price = draw(st.one_of(st.floats(lower, upper), st.floats(10, 300)))
    return {'at': AT, 'knobs': draw(knobs_st),
            'band': {'price': price, 'lower': lower, 'upper': upper, 'in_range': lower <= price <= upper,
                     'fees_usd': draw(st.one_of(st.none(), st.floats(0, 10)))},
            'regime': draw(regime_st), 'calm': draw(calm_st), 'forecast': draw(fc_st),
            'gates': {'calm_times': [AT - a for a in draw(st.lists(ago, max_size=6))],
                      'last_rebalance': AT - draw(ago), 'last_harvest': AT - draw(ago),
                      'breaker_ok': draw(st.booleans()), 'busy': draw(st.one_of(st.none(), st.booleans()))}}


VOLUNTARY = {'regime_widen', 'regime_narrow', 'calm_narrow', 'calm_recentre', 'calm_widen'}


class Properties(unittest.TestCase):
    @FAST
    @given(seen_st())
    def test_pure_and_json_stable(self, s):
        before = copy.deepcopy(s)
        a = lp.polls.poll_verdict(s)
        self.assertEqual(s, before)                               # reads, never writes
        self.assertEqual(lp.polls.poll_verdict(s), a)
        self.assertEqual(lp.polls.poll_verdict(json.loads(json.dumps(s))), a)
        self.assertEqual(json.loads(json.dumps(a)), a)

    @FAST
    @given(seen_st())
    def test_exit_exactly_when_out_of_range(self, s):
        v = lp.polls.poll_verdict(s)
        self.assertEqual(v['act'] == 'exit', not s['band']['in_range'])
        if v['act'] == 'exit':
            self.assertEqual(v['side'], 'above' if s['band']['price'] > s['band']['upper'] else 'below')
            if s['knobs']['regime_enabled']:
                self.assertIn(v['band'], W8)
        else:
            self.assertIsNone(v['side'])

    @FAST
    @given(seen_st())
    def test_every_voluntary_move_passes_its_gates(self, s):
        v = lp.polls.poll_verdict(s)
        if v['act'] not in VOLUNTARY:
            return
        g, k = s['gates'], s['knobs']
        self.assertTrue(g['breaker_ok'])
        self.assertTrue(lp.regime.move_gap_ok(g['calm_times'], g['last_rebalance'], AT, k['calm_min_gap']))
        if v['act'].startswith('regime') or v['act'] in ('calm_narrow', 'calm_recentre'):
            self.assertGreater(lp.regime.moves_left(g['calm_times'], AT, k['calm_max_moves']), 0)
        if v['act'].startswith('calm'):
            self.assertIsNone(s['regime'])                        # regime mode owns the band when it has a view
            self.assertTrue(k['calm_enabled'])
        if v['act'].startswith('regime'):
            self.assertFalse(s['regime'].get('stale'))                # a stale tape moves no band

    @FAST
    @given(seen_st())
    def test_harvest_and_deferral_only_without_a_move(self, s):
        v = lp.polls.poll_verdict(s)
        if v['harvest'] or v['deferred']:
            self.assertIsNone(v['act'])
        if v['deferred']:
            self.assertTrue(s['gates']['busy'])
            self.assertLess(s['forecast']['p_exit_horizon'] or 0, 0.9)


# --- the loop's inline rules, as they stood before poll_verdict -----------------

def inline_rules(s):
    """The decisions main() made inline (main at ec61e41), on the same inputs,
    with the one change made since: a stale tape moves no band that is inside
    (it only blocked narrowing; 2026-10-06)."""
    k, b, g = s['knobs'], s['band'], s['gates']
    rv, cv, fc = s['regime'], s['calm'], s['forecast']
    now = s['at']
    used = [t for t in g['calm_times'] if now - t < 86400]
    budget = max(k['calm_max_moves'] - len(used), 0)
    last_any = max([g['last_rebalance']] + used)
    allowed = now - last_any >= k['calm_min_gap'] and g['breaker_ok']
    tight = bool(k['regime_enabled']) or bool(cv and cv.get('tight_held'))
    if not b['in_range']:
        side = 'above' if b['price'] > b['upper'] else 'below'
        if k['regime_enabled']:
            band = rv['choice'] if rv else k['widths'][-1]
        elif tight:
            band = None if (not cv or not cv.get('calm') or budget <= 0
                            or (cv.get('p_touch_fresh') is not None and cv['p_touch_fresh'] >= k['calm_threshold'])) \
                else k['calm_band']
        else:
            band = None
        return ('exit', band, side, False, False)
    ract = calm.regime_decide(rv, widths=k['widths'], steps=k['steps']) if rv else None
    if ract and rv.get('stale'):
        ract = None
    if ract and budget > 0 and allowed:
        return ('regime_' + ract, rv['choice'], None, False, False)
    act = None if rv else calm.decide(cv, enabled=k['calm_enabled'], budget_left=budget)
    if act and not allowed:
        act = None
    if act:
        return ('calm_' + act, None if act == 'widen' else k['calm_band'], None, False, False)
    deferred = False
    if fc and fc.get('act') and k['proactive_threshold']:
        if g['busy'] and (fc.get('p_exit_horizon') or 0) < 0.9:
            deferred = True
        else:
            return ('proactive', None, None, False, False)
    due = bool(k['harvest_interval']) and (b['fees_usd'] or 0) >= k['min_harvest_usd'] \
        and now - g['last_harvest'] >= k['harvest_interval']
    return (None, None, None, deferred, due)


class SameAsBefore(unittest.TestCase):
    @settings(max_examples=3000, deadline=None)
    @given(seen_st())
    def test_poll_verdict_is_the_inline_rules(self, s):
        v = lp.polls.poll_verdict(s)
        self.assertEqual((v['act'], v['band'], v['side'], v['deferred'], v['harvest']), inline_rules(s))

    def test_the_wrappers_still_read_state_and_config(self):
        now = time.time()
        with mock.patch.object(config, 'CALM_MAX_MOVES', 2), \
                mock.patch.object(config, 'CALM_MIN_GAP', 600), \
                mock.patch.object(config, 'CALM_BAND', 1.01), \
                mock.patch.object(config, 'CALM_THRESHOLD', 0.25), \
                mock.patch.object(health, 'allowed', lambda key, now=None: (True, 'closed', 0, None)):
            st_ = {'calm_times': [now - 100, now - 90000], 'last_rebalance': 0}
            self.assertEqual(lp.regime.calm_budget_left(st_), 1)
            self.assertFalse(lp.regime.voluntary_move_allowed(st_))
            self.assertTrue(lp.regime.voluntary_move_allowed({'calm_times': [now - 700], 'last_rebalance': 0}))
            self.assertEqual(lp.regime.calm_reopen_band({'calm': True, 'p_touch_fresh': 0.1}, st_), 1.01)
            self.assertIsNone(lp.regime.calm_reopen_band({'calm': True, 'p_touch_fresh': 0.1},
                                                          {'calm_times': [now - 1, now - 2]}))


# --- poll_seen and the record --------------------------------------------------

STATUS = {'positionMint': 'M', 'whirlpool': 'POOL', 'price': 100.0, 'lowerPrice': 98.0, 'upperPrice': 102.0,
          'inRange': True, 'feesAccrued_USD': np.float64(0.3), 'liquidity': '5'}
RV = {'mode': 'WARM', 'choice': 1.02, 'choice_pct': 2.0, 'held': 1.02, 'held_pct': 2.0, 'inside': True,
      'p_held': np.float64(0.2), 'sigma_5m_pct': 0.1, 'velocity': 0.0, 'probs': [[1.0, 0.5]],
      'liquidity': {'factor': 1.0}, 'p_exit': {6: 0.1}, 'data': {'source': 'gecko'}}
CV = {'calm': np.bool_(True), 'tight_held': False, 'p_touch': None, 'p_touch_fresh': 0.1, 'threshold': 0.25,
      'sigma_5m_pct': 0.05, 'cut_pct': 0.08, 'budget_left': 3, 'volume_1h': 9.0}


class Recording(unittest.TestCase):
    def setUp(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate replay_polls')

    def seen(self, fc=None, busy=False, allowed=True, threshold=0.5):
        asked = []
        with mock.patch.object(lp.board, 'busy_hour', lambda: asked.append(1) or busy), \
                mock.patch.object(config, 'PROACTIVE_THRESHOLD', threshold), \
                mock.patch.object(health, 'allowed', lambda key, now=None: (allowed, 'x', 0, None)):
            s = lp.polls.poll_seen({'calm_times': [time.time() - 10, time.time() - 90000], 'last_rebalance': 5,
                                      'last_harvest': 7}, STATUS, RV, CV, fc)
        return s, asked

    def test_seen_is_plain_json_with_only_the_listed_fields(self):
        s, _ = self.seen(fc={'act': np.bool_(False), 'p_exit_horizon': np.float64(0.3), 'other': 1})
        self.assertEqual(json.loads(json.dumps(s)), s)
        self.assertEqual(set(s['regime']), set(lp.polls.SEEN_REGIME))
        self.assertEqual(set(s['calm']), set(lp.polls.SEEN_CALM))
        self.assertEqual(s['forecast'], {'act': False, 'p_exit_horizon': 0.3})
        self.assertEqual(s['band'], {'price': 100.0, 'lower': 98.0, 'upper': 102.0, 'in_range': True, 'fees_usd': 0.3})
        # a day-old move is out of the window, the ten-second-old one in it
        self.assertEqual(len(s['gates']['calm_times']), 1)
        self.assertLess(time.time() - s['gates']['calm_times'][0], 60)
        self.assertEqual((s['gates']['last_rebalance'], s['gates']['last_harvest']), (5, 7))
        self.assertEqual(s['knobs'], lp.polls.poll_knobs())

    def test_the_hour_is_read_only_for_a_live_proactive_case(self):
        for fc, threshold, want in (({'act': True, 'p_exit_horizon': 0.6}, 0.5, [1]),
                                    ({'act': False, 'p_exit_horizon': 0.6}, 0.5, []),
                                    ({'act': True, 'p_exit_horizon': 0.6}, 0, []), (None, 0.5, [])):
            s, asked = self.seen(fc=fc, busy=True, threshold=threshold)
            self.assertEqual(asked, want)
            self.assertEqual(s['gates']['busy'], True if want else None)

    def test_the_breaker_is_read(self):
        self.assertFalse(self.seen(allowed=False)[0]['gates']['breaker_ok'])
        self.assertTrue(self.seen(allowed=True)[0]['gates']['breaker_ok'])

    def test_no_views_and_out_of_range(self):
        with mock.patch.object(health, 'allowed', lambda key, now=None: (True, 'x', 0, None)):
            s = lp.polls.poll_seen({}, dict(STATUS, inRange=False, feesAccrued_USD=None), None, None, None)
        self.assertEqual((s['regime'], s['calm'], s['forecast']), (None, None, None))
        self.assertFalse(s['band']['in_range'])
        self.assertEqual(s['gates']['calm_times'], [])
        self.assertEqual((s['gates']['last_rebalance'], s['gates']['last_harvest']), (0, 0))

    def test_record_poll_stores_and_prunes(self):
        s, _ = self.seen()
        v = lp.polls.poll_verdict(s)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into replay_polls (ts, profile, pool, seen, verdict) values "
                        "(now() - interval '15 days', %s, 'P', '{}', '{}'), "
                        "(now() - interval '15 days', 'someone-else', 'P', '{}', '{}'), "
                        "(now() - interval '13 days', %s, 'P', '{}', '{}')",
                        (db.CONTEXT['profile'], db.CONTEXT['profile']))
        lp.polls.record_poll('POOL', s, v)
        with db.cursor() as cur:
            cur.execute('select profile, pool, seen, verdict, ts > now() - interval \'1 minute\' new '
                        'from replay_polls order by ts')
            rows = cur.fetchall()
        self.assertEqual([(r['profile'], r['new']) for r in rows],
                         [('someone-else', False), (db.CONTEXT['profile'], False), (db.CONTEXT['profile'], True)])
        self.assertEqual((rows[-1]['pool'], rows[-1]['seen'], rows[-1]['verdict']), ('POOL', s, v))
        self.assertEqual(lp.polls.poll_verdict(rows[-1]['seen']), v)

    def test_a_failed_record_is_said_and_never_raised(self):
        said = []
        with mock.patch.object(db, 'record_replay_poll', side_effect=RuntimeError('db down')), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: said.append((ev, kw['reason']))):
            lp.polls.record_poll('POOL', {}, {})
        self.assertEqual(said, [('replay_record_failed', 'RuntimeError: db down')])


class PureHelpers(unittest.TestCase):
    def test_moves_left(self):
        self.assertEqual(lp.regime.moves_left([AT - 1, AT - 86399, AT - 86400, AT - 90000], AT, 3), 1)
        self.assertEqual(lp.regime.moves_left([AT - 1] * 5, AT, 3), 0)
        self.assertEqual(lp.regime.moves_left([], AT, 0), 0)

    def test_move_gap_ok(self):
        self.assertTrue(lp.regime.move_gap_ok([], AT - 600, AT, 600))
        self.assertFalse(lp.regime.move_gap_ok([], AT - 599, AT, 600))
        self.assertFalse(lp.regime.move_gap_ok([AT - 100], AT - 9999, AT, 600))
        self.assertTrue(lp.regime.move_gap_ok([AT - 100000], AT - 9999, AT, 600))   # a day-old move does not count
        self.assertTrue(lp.regime.move_gap_ok([AT - 700], 0, AT, 600))

    def test_tight_reopen(self):
        self.assertEqual(lp.regime.tight_reopen({'calm': True, 'p_touch_fresh': 0.1}, 1, 1.01, 0.25), 1.01)
        self.assertEqual(lp.regime.tight_reopen({'calm': True, 'p_touch_fresh': None}, 1, 1.01, 0.25), 1.01)
        self.assertIsNone(lp.regime.tight_reopen({'calm': True, 'p_touch_fresh': 0.25}, 1, 1.01, 0.25))
        self.assertIsNone(lp.regime.tight_reopen({'calm': True, 'p_touch_fresh': 0.1}, 0, 1.01, 0.25))
        self.assertIsNone(lp.regime.tight_reopen({'calm': False, 'p_touch_fresh': 0.1}, 1, 1.01, 0.25))
        self.assertIsNone(lp.regime.tight_reopen(None, 1, 1.01, 0.25))

    def test_plain(self):
        self.assertEqual(json.dumps({'a': np.bool_(True), 'b': np.int64(3), 'c': math.inf}, default=lp.polls._plain),
                         '{"a": true, "b": 3, "c": Infinity}')
        self.assertEqual(lp.polls._plain(object.__new__(type('X', (), {'__str__': lambda self: 'x'}))), 'x')


if __name__ == '__main__':
    unittest.main()
