"""engine.rate_limited: every shape of a 429, and never a crash.

2026-09-27 08:50Z and 15:06Z: GeckoTerminal answered {"status": 429} and the
old check, written for {"status": {"error_code": 429}}, raised
AttributeError: 'int' object has no attribute 'get'. The board scan failed.
"""
import json
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401
import engine

JSON = st.recursive(st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
                    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.sampled_from(
                        ['status', 'error_code', 'data', 'error', 'x']), c, max_size=4), max_leaves=20)


class RateLimited(unittest.TestCase):
    def test_every_known_shape_of_a_429(self):
        for d in ({'status': {'error_code': 429}}, {'status': {'error_code': '429'}}, {'status': 429},
                  {'status': '429'}, {'status': ' 429 '}, {'error_code': 429}, {'status': None, 'error_code': 429}):
            self.assertTrue(engine.rate_limited(d), d)

    def test_other_answers_are_not(self):
        for d in (None, 429, '429', [429], {}, {'status': 200}, {'status': {'error_code': 404}},
                  {'status': {'error_message': 'x'}}, {'data': {'status': 429}}, {'status': [429]},
                  {'status': {'error_code': [429]}}, {'error_code': {'x': 429}}, {'status': True}):
            self.assertFalse(engine.rate_limited(d), d)

    @settings(max_examples=500, deadline=None)
    @given(JSON)
    def test_any_json_never_raises(self, d):
        self.assertIn(engine.rate_limited(d), (True, False))

    def curl_with(self, bodies):
        runs = iter(bodies)
        sleeps = []
        fake = lambda *a, **k: mock.Mock(stdout=next(runs))
        with mock.patch.object(engine.subprocess, 'run', fake), \
                mock.patch.object(engine.time, 'sleep', lambda s: sleeps.append(s)):
            return engine.curl('https://example.test/x', retries=2), sleeps

    def test_curl_retries_the_int_shape_then_returns_the_answer(self):
        got, sleeps = self.curl_with([json.dumps({'status': 429}), json.dumps({'data': 1})])
        self.assertEqual(got, {'data': 1}); self.assertEqual(sleeps, [5.0])

    def test_curl_gives_none_after_every_retry_is_limited(self):
        got, sleeps = self.curl_with([json.dumps({'status': 429})] * 3)
        self.assertIsNone(got); self.assertEqual(sleeps, [5.0, 10.0, 15.0])

    def test_curl_passes_other_answers_through(self):
        self.assertEqual(self.curl_with(['[1, 2]'])[0], [1, 2])
        self.assertEqual(self.curl_with([json.dumps({'status': 200, 'data': 3})])[0], {'status': 200, 'data': 3})
        self.assertIsNone(self.curl_with(['not json'])[0])


class Curl(unittest.TestCase):
    def test_default_is_three_attempts_and_output_is_captured_as_text(self):
        calls = []
        def fake(cmd, **k):
            calls.append((cmd, k)); return mock.Mock(stdout=json.dumps({'status': 429}))
        with mock.patch.object(engine.subprocess, 'run', fake), mock.patch.object(engine.time, 'sleep', lambda s: None):
            self.assertIsNone(engine.curl('https://example.test/x'))
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1], {'capture_output': True, 'text': True})
        self.assertEqual(calls[0][0][-1], 'https://example.test/x')

    def test_the_timeout_is_40s_by_default_and_the_callers_otherwise(self):
        calls = []
        def fake(cmd, **k):
            calls.append(cmd); return mock.Mock(stdout='[]')
        with mock.patch.object(engine.subprocess, 'run', fake):
            engine.curl('https://example.test/x')
            engine.curl('https://example.test/y', max_time=10)
        at = [c.index('--max-time') for c in calls]
        self.assertEqual([c[i + 1] for c, i in zip(calls, at)], ['40', '10'])

    def test_geckoterminal_calls_are_spaced_by_the_gate(self):
        with engine.db.cursor(commit=True) as cur:
            cur.execute("delete from rate_gate where name = 'gecko'")        # a fresh gate, whatever ran before
        clock = [1000.0]
        sleeps = []
        def sleep(s):
            sleeps.append(round(s, 6)); clock[0] += s
        with mock.patch.object(engine.subprocess, 'run', lambda *a, **k: mock.Mock(stdout='{}')), \
                mock.patch.object(engine.time, 'time', lambda: clock[0]), \
                mock.patch.object(engine.time, 'sleep', sleep), \
                mock.patch.object(engine, '_GECKO_LAST', [0.0]):
            engine.curl('https://api.geckoterminal.com/a')
            self.assertEqual(sleeps, [])                          # first call: no wait
            clock[0] += 1.8                                       # a 0.3 s wait is still a wait
            engine.curl('https://api.geckoterminal.com/b')
            self.assertEqual(sleeps, [round(engine.GECKO_SPACING - 1.8, 6)])
            clock[0] += engine.GECKO_SPACING                      # a full gap later: no wait
            engine.curl('https://api.geckoterminal.com/c')
            self.assertEqual(len(sleeps), 1)
            self.assertEqual(engine._GECKO_LAST[0], clock[0])
            engine.curl('https://example.test/not-gecko')        # other hosts skip the gate
            self.assertEqual(len(sleeps), 1)



class CrossProcessGate(unittest.TestCase):
    """The GeckoTerminal gate is a row in Postgres: every profile's process
    reserves its slot there, so N processes keep one process's pace."""

    def setUp(self):
        with engine.db.cursor(commit=True) as cur:
            cur.execute("delete from rate_gate where name like 'test-%'")

    def tearDown(self):
        self.setUp()

    def test_slots_are_reserved_one_spacing_apart(self):
        waits = [engine.rate_gate('test-a', 2.0) for _ in range(4)]
        self.assertLess(waits[0], 0.05)
        for k in range(1, 4):
            self.assertAlmostEqual(waits[k], 2.0 * k, delta=0.5)

    def test_concurrent_callers_never_share_a_slot(self):
        import threading
        got, lock = [], threading.Lock()

        def call():
            t0 = __import__('time').time()
            w = engine.rate_gate('test-b', 1.0)
            with lock:
                got.append(t0 + w)
        ts = [threading.Thread(target=call) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        got.sort()
        self.assertEqual(len(got), 8)
        self.assertTrue(all(b - a >= 1.0 - 0.05 for a, b in zip(got, got[1:])), got)

    def test_a_reservation_from_another_clock_is_reset_not_waited_on(self):
        with engine.db.cursor(commit=True) as cur:
            cur.execute("insert into rate_gate (name, next_at) values ('test-c', %s)",
                        (__import__('time').time() + 10 * engine.GATE_MAX_WAIT_S,))
        self.assertLess(engine.rate_gate('test-c', 2.0), 0.05)

    def test_without_the_database_the_process_gate_still_spaces_calls(self):
        clock, sleeps = [5000.0], []
        def sleep(s):
            sleeps.append(round(s, 6)); clock[0] += s
        def down(*a, **k):
            raise RuntimeError('database down')
        with mock.patch.object(engine.subprocess, 'run', lambda *a, **k: mock.Mock(stdout='{}')), \
                mock.patch.object(engine, 'rate_gate', down), \
                mock.patch.object(engine.time, 'time', lambda: clock[0]), \
                mock.patch.object(engine.time, 'sleep', sleep), mock.patch.object(engine, '_GECKO_LAST', [0.0]):
            engine.curl('https://api.geckoterminal.com/a')
            engine.curl('https://api.geckoterminal.com/b')
        self.assertEqual(sleeps, [engine.GECKO_SPACING])


class Network(unittest.TestCase):
    """No GeckoTerminal URL names 'solana' on a Base profile's paths."""

    def tearDown(self):
        engine.use_network('solana')

    def test_every_gecko_url_follows_the_profiles_network(self):
        import calm
        urls = []
        def fake(url, accept='application/json', retries=2, max_time=40):
            urls.append(url); return None
        engine.use_network('base')
        with mock.patch.object(engine, 'curl', fake):
            engine.candles('0xpool')
            calm.tape_5m('0xpool')
            engine.pool_quote_price({'token_b': {'address': '0xabc', 'symbol': 'X'}})
        self.assertEqual(len(urls), 3)
        for u in urls:
            self.assertIn('/networks/base/', u); self.assertNotIn('solana', u)
        self.assertTrue(urls[2].startswith('https://api.geckoterminal.com/api/v2/simple/networks/base/token_price/'))

    def test_base_usdc_is_a_stablecoin_by_mint_in_any_case(self):
        self.assertTrue(engine.is_stable({'address': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'}))
        self.assertFalse(engine.is_stable({'address': '0x4200000000000000000000000000000000000006'}))


if __name__ == '__main__':
    unittest.main()
