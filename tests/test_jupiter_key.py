"""Jupiter's keyed API (2026-10-10): the free endpoint answered 429 a dozen
times an hour. With JUPITER_API_KEY in the service environment every Jupiter
call goes to the keyed endpoint with the key in a header; without it, to the
free one as before. The key never appears in a process's argv or a URL."""
import importlib
import json
import os
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import lp.signers
from venues import api as venue_api
from venues.jupiter import prices as jupiter_api

KEY = 'k' * 68
VENUE = json.loads((_fixtures.ROOT / 'venues/jupiter/venue.json').read_text())


def reloaded(env):
    """The prices module as a service with `env` imports it."""
    with mock.patch.dict(os.environ, env, clear=False):
        if VENUE['key_env'] not in env:
            os.environ.pop(VENUE['key_env'], None)
        return importlib.reload(jupiter_api)


class Endpoint(unittest.TestCase):
    def tearDown(self):
        importlib.reload(jupiter_api)

    def test_a_key_selects_the_keyed_api(self):
        self.assertEqual(reloaded({VENUE['key_env']: KEY}).JUPITER, VENUE['keyed_api'])

    def test_no_key_keeps_the_free_api(self):
        self.assertEqual(reloaded({}).JUPITER, VENUE['free_api'])

    def test_the_free_api_is_the_one_the_bot_always_used(self):
        self.assertEqual(VENUE['free_api'], 'https://lite-api.jup.ag')


class KeyNeverInArgv(unittest.TestCase):
    def run_get(self, env):
        seen = []

        def run(argv, **k):
            seen.append((argv, k)); return mock.Mock(stdout='{"ok": 1}')
        with mock.patch.dict(os.environ, env), mock.patch.object(venue_api.subprocess, 'run', run), \
                mock.patch.object(venue_api.jupgate, 'wait_turn', lambda *a, **k: None):
            if VENUE['key_env'] not in env:
                os.environ.pop(VENUE['key_env'], None)
            self.assertEqual(jupiter_api.get('https://api.jup.ag/price/v3?ids=M'), {'ok': 1})
        return seen[0]

    def test_the_key_goes_on_stdin_as_a_header(self):
        argv, k = self.run_get({VENUE['key_env']: KEY})
        self.assertNotIn(KEY, ' '.join(argv))
        self.assertEqual(k['input'], f"{VENUE['key_header']}: {KEY}\n")
        self.assertIn('@-', argv)
        self.assertEqual(argv[argv.index('--max-time') + 1], '40')          # the venue reads' own limit

    def test_no_key_sends_no_header(self):
        argv, k = self.run_get({})
        self.assertEqual(k['input'], '')
        self.assertNotIn(VENUE['key_header'], ' '.join(argv))

    def test_the_breaker_probe_sends_the_key_too(self):
        seen = []
        with mock.patch.object(jupiter_api, 'get', lambda url, timeout=40: seen.append(url) or {'outAmount': '5'}):
            self.assertEqual(lp.signers.jupiter_answers(), (True, None))
        self.assertEqual(seen, [lp.signers.PROBE_QUOTE])


if __name__ == '__main__':
    unittest.main()
