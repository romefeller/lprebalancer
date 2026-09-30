"""The keyed Helius RPC (owner, 2026-09-30): built from KAMINO_RPC_KEY, never
shown in a feed row, a log line or an event, and replaced by the public
endpoint at startup when it does not answer."""
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import config      # noqa: E402
import db          # noqa: E402
import rebalancer  # noqa: E402

KEY = '0123abcd-4567-89ef-0123-456789abcdef'
URL = f'https://mainnet.helius-rpc.com/?api-key={KEY}'


class KeyedUrl(unittest.TestCase):
    def test_built_from_the_key(self):
        self.assertEqual(config.keyed_rpc({'KAMINO_RPC_KEY': KEY}), URL)
        self.assertEqual(config.keyed_rpc({'KAMINO_RPC_KEY': f'  {KEY}\n'}), URL)

    def test_no_key_or_not_a_key(self):
        for v in (None, '', 'short', 'has space in it 1234567', 'x' * 129, 'a/b?c=d&e=0123456789', "q'uote0123456789"):
            env = {} if v is None else {'KAMINO_RPC_KEY': v}
            self.assertIsNone(config.keyed_rpc(env), v)


class Redaction(unittest.TestCase):
    def test_redact(self):
        self.assertEqual(rebalancer.redact(URL), 'https://mainnet.helius-rpc.com/?api-key=***')
        self.assertEqual(rebalancer.redact(f'x {URL}&y=1 "api_key={KEY}" API-KEY={KEY}'),
                         'x https://mainnet.helius-rpc.com/?api-key=***&y=1 "api_key=***" API-KEY=***')
        self.assertEqual(rebalancer.redact('nothing here'), 'nothing here')
        self.assertNotIn(KEY, rebalancer.redact({'u': URL}))

    def test_feed_and_log_never_show_the_key(self):
        with tempfile.TemporaryDirectory() as d:
            feed = pathlib.Path(d) / 'events.jsonl'
            out = io.StringIO()
            with mock.patch.object(rebalancer, 'FEED', feed), mock.patch('sys.stdout', out):
                rebalancer.notify('open_failed', reason=f'fetch failed at {URL}', nested={'url': URL})
            text = feed.read_text()
            self.assertNotIn(KEY, text); self.assertNotIn(KEY, out.getvalue())
            self.assertEqual(json.loads(text)['event'], 'open_failed')          # still valid JSON

    def test_events_table_never_shows_the_key(self):
        _fixtures.reset_ledger()
        db.event('swap_skipped', f'boom {URL}')
        with db.cursor() as cur:
            cur.execute("select detail from events where kind = 'swap_skipped'")
            d = cur.fetchone()['detail']
        self.assertNotIn(KEY, d); self.assertIn('api-key=***', d)


class Probe(unittest.TestCase):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def run_probe(self, answer):
        sent = []
        def urlopen(req, timeout):
            if isinstance(answer, Exception):
                raise answer
            return self.Resp(json.dumps(answer).encode())
        with mock.patch('urllib.request.urlopen', urlopen), mock.patch.object(config, 'RPC', URL), \
                mock.patch.object(config, 'PUBLIC_RPC', 'https://api.mainnet-beta.solana.com'), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append((ev, kw))), \
                mock.patch('builtins.print'):
            used = rebalancer.probe_rpc()
            rpc_after = config.RPC
        return used, rpc_after, sent

    def test_a_working_key_is_kept(self):
        used, after, sent = self.run_probe({'jsonrpc': '2.0', 'result': 123456, 'id': 1})
        self.assertEqual((used, after, sent), (URL, URL, []))

    def test_a_bad_key_falls_back_and_says_so_without_the_key(self):
        import urllib.error
        for answer in (urllib.error.HTTPError(URL, 401, 'Unauthorized', {}, None), {'error': {'code': -32401}},
                       TimeoutError('timed out'), ValueError(f'bad {URL}')):
            used, after, sent = self.run_probe(answer)
            self.assertEqual((used, after), ('https://api.mainnet-beta.solana.com',) * 2, answer)
            self.assertEqual(sent[0][0], 'rpc_fallback')
            self.assertEqual(sent[0][1]['host'], 'mainnet.helius-rpc.com')
            self.assertNotIn(KEY, json.dumps(sent))

    def test_the_public_endpoint_is_not_probed(self):
        with mock.patch('urllib.request.urlopen', side_effect=AssertionError('probed')):
            self.assertEqual(rebalancer.probe_rpc('https://api.mainnet-beta.solana.com', 'https://api.mainnet-beta.solana.com'),
                             'https://api.mainnet-beta.solana.com')

    def test_startup_probes_before_anything_else(self):
        src = pathlib.Path(rebalancer.__file__).read_text()
        i = src.index('def main():')
        self.assertLess(src.index('    probe_rpc()', i), src.index('    state = load()', i))


if __name__ == '__main__':
    unittest.main()
