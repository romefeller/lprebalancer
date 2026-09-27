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

    def test_geckoterminal_calls_are_spaced_by_the_gate(self):
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


if __name__ == '__main__':
    unittest.main()
