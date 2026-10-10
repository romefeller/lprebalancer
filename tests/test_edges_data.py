"""Edges of data and dependency failures, on the fake chain of test_multi_loop.

Covered: a poll in which GeckoTerminal, Binance and Jupiter all fail while a
position is open moves nothing, pays nothing, books no zero equity and says
the outage once; a dead keyed Polygon RPC is probed with eth_blockNumber and
replaced by the chain's public endpoint, and the event names the host only;
a gas price spike refusal from the EVM signer after a close is not a venue
failure, writes no HALT over three polls, and each poll tries the reopen
again; a disabled profile that holds a position and has no CLOSE file holds
and writes nothing; busy_hour reads the UTC hour exactly at the hour edges
and across midnight; three Jupiter 429s rescued by the Orca fallback leave
the 'swap' breaker closed and back off only Jupiter's own."""
import contextlib
import datetime as dt
import json
import time
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import audit
import calm
import chains
import config
import db
import engine
import health
import lp.board
import lp.books
import lp.housekeeping
import lp.loop
import lp.paths
import lp.signers
import lp.swaps
import lp.tape
from venues import api as venue_api
import test_multi_loop as M
import test_polygon_loop as P

FOREIGN = 'FoReiGnMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'    # a token in the wallet, outside every pool


def writes(chain, profile=None):
    """The signer calls that send a transaction (--execute), as (command, dex)."""
    return [(c['args'][0], c['dex']) for c in chain.calls
            if '--execute' in c['args'] and (profile is None or c['profile'] == profile)]


# --- 1. data outage with a position open ----------------------------------------------

class DataOutageWithAPositionOpen(M.Fixture):
    """GeckoTerminal (hourly and five-minute), Binance and Jupiter prices all
    fail in the same poll, while e2e-sol holds a +/-5% band in range. The
    stored tape is calm and an hour old: were it trusted, regime mode would
    narrow the band at once."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(lp.housekeeping, 'run_audits', lambda state: None),   # no live chain reads
                  mock.patch.object(audit, 'token_accounts', lambda url, owner: [
                      {'pubkey': 'acct1', 'program': 'tok', 'lamports': 2039280, 'mint': FOREIGN,
                       'amount': 5_000_000, 'decimals': 6, 'ui': 5.0}]),
                  mock.patch.dict(lp.tape._TAPE, clear=True), mock.patch.dict(lp.tape._TAPE5, clear=True),
                  mock.patch.dict(lp.tape._SURR, clear=True), mock.patch.dict(lp.tape.LAST_SURROGATE, clear=True),
                  mock.patch.dict(lp.tape._LIQ, clear=True), mock.patch.dict(lp.tape._QUIET_REF, clear=True),
                  mock.patch.dict(lp.books.LAST_REGIME, clear=True)):
            p.start(); self.addCleanup(p.stop)
        self.addCleanup(self._drop_tape)
        self.asked = []

    def _drop_tape(self):
        with db.cursor(commit=True) as cur:
            cur.execute('delete from tape5 where pool = %s', (M.SOL_POOL,))

    def seed_calm_tape(self, end_age_s=3600, days=2):
        """Flat five-minute bars at $150 for `days`, the newest `end_age_s` old."""
        import numpy as np
        end = (int(time.time() - end_age_s) // calm.BAR_SECONDS) * calm.BAR_SECONDS
        ts = np.arange(end - days * 86400, end + 1, calm.BAR_SECONDS, dtype=float)
        px = 150.0 * (1 + 0.0001 * np.sin(np.arange(len(ts))))
        db.tape_store(M.SOL_POOL, (ts, px, px * 1.0001, px * 0.9999, px, np.ones(len(ts))), ts[0])

    def dead(self, url, *a, **k):
        self.asked.append(url)
        raise OSError('network unreachable')

    def outage_poll(self, n=1):
        """n polls of e2e-sol under regime mode with every data source down;
        the feed rows each poll wrote."""
        per_poll, asked = [], []
        with self.as_profile('e2e-sol'), mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.object(engine, 'curl', self.dead), mock.patch.object(venue_api, '_get', self.dead):
            for _ in range(n):
                before, self.asked = len(self.feed('e2e-sol')), []
                try:
                    lp.loop.main()
                except M.StopPoll:
                    pass
                per_poll.append(self.feed('e2e-sol')[before:])
                asked.append(self.asked)
        return per_poll, asked

    def test_an_outage_of_every_source_moves_nothing_pays_nothing_and_says_it_once(self):
        self.chain.wallet[M.SOL] = 2.0
        self.poll('e2e-sol')
        pos = self.chain.positions[M.SOL_POOL]
        self.seed_calm_tape()
        self.chain.calls.clear()
        polls, asked = self.outage_poll(n=3)
        # the outage was real: all three sources were asked in the first poll, and failed
        self.assertTrue(any('geckoterminal.com' in u for u in asked[0]), asked[0])
        self.assertTrue(any('binance' in u for u in asked[0]), asked[0])
        self.assertTrue(any('jup.ag' in u for u in asked[0]), asked[0])
        # no move: no close, no open, no swap, no harvest; the same band is held
        self.assertEqual(writes(self.chain), [])
        self.assertEqual(self.chain.positions[M.SOL_POOL], pos)
        # no payout
        self.assertEqual(self.chain.of('e2e-sol', 'send'), [])
        with db.cursor() as cur:
            cur.execute("select count(*) n from payouts where config_name = 'e2e-sol'")
            self.assertEqual(cur.fetchone()['n'], 0)
        # no book with zero equity: an unknown figure is None, never $0
        with db.cursor() as cur:
            cur.execute('select equity_usd, wallet_usd, position_usd from snapshots where mint = %s', (pos['mint'],))
            snaps = cur.fetchall()
        self.assertEqual(len(snaps), 3)
        for s in snaps:
            self.assertTrue(s['equity_usd'] is None or float(s['equity_usd']) > 0, s)
        for rows in polls:
            for r in rows:
                for k in ('equity_usd', 'equity', 'walletUsd', 'position_usd'):
                    if k in r and r[k] is not None:
                        self.assertGreater(float(r[k]), 0, (r['event'], k))
        # at most one notice of the outage per poll, and the outage is said
        stale = [[r for r in rows if r['event'] == 'TAPE_SOURCE'] for rows in polls]
        for rows in stale:
            self.assertLessEqual(len(rows), 1, rows)
        self.assertEqual(sum(len(r) for r in stale), 1)
        self.assertEqual(stale[0][0]['kind'], 'none')
        # the held band is still reported (once per POLL_SECONDS), in STALE
        books = [r for rows in polls for r in rows if r['event'] == 'in_band']
        self.assertTrue(books)
        self.assertTrue(all(b['regime']['mode'] == 'STALE' for b in books), [b['regime'] for b in books])


# --- 2. RPC failover on Polygon -------------------------------------------------------

class Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self.body


POLY_PUBLIC = chains.CHAINS['polygon']['public_rpc']
ALCHEMY_KEY = 'AlchemyPolyKey0123456789abcdef'
DEAD_URLS = (f'https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}',              # key in the path
             f'https://rpc.example-node.io/polygon?api-key={ALCHEMY_KEY}',            # key in the query
             f'https://rpc.example-node.io/{ALCHEMY_KEY}/?token={ALCHEMY_KEY}')       # both


class PolygonRpcFailover(unittest.TestCase):
    """CHAIN polygon, LPBOT_POLYGON_RPC set to a keyed endpoint that does not
    answer."""

    def probe(self, dead_url, answer_public=True, **kw):
        """probe_rpc on Polygon with `dead_url` dead; (returned url, config.RPC
        after, the JSON-RPC requests sent as (url, method), the notices)."""
        sent, notes = [], []

        def urlopen(req, timeout):
            body = json.loads(req.data)
            sent.append((req.full_url, body['method']))
            if req.full_url == dead_url:
                raise OSError(f'connection refused by {dead_url}')
            return Resp({'jsonrpc': '2.0', 'id': 1, 'result': '0x4b7c1f2'})
        env = {'LPBOT_POLYGON_RPC': dead_url}
        caps = chains.caps('polygon')
        with mock.patch('urllib.request.urlopen', urlopen), mock.patch.dict('os.environ', env), \
                mock.patch.object(config, 'CHAIN', 'polygon'), mock.patch.object(config, 'CAPS', caps), \
                mock.patch.object(config, 'RPC', dead_url), \
                mock.patch.object(config, 'PUBLIC_RPC', config.fallback_rpc('polygon', caps, env)), \
                mock.patch.object(lp.books, 'notify', lambda ev, **k: notes.append((ev, k))), \
                mock.patch('builtins.print'):
            used = lp.signers.probe_rpc(**kw)
            after = config.RPC
        return used, after, sent, notes

    def test_a_dead_keyed_endpoint_is_probed_with_eth_blocknumber_and_replaced_by_the_public_one(self):
        for url in DEAD_URLS:
            used, after, sent, notes = self.probe(url, fallback=POLY_PUBLIC)
            self.assertEqual((used, after), (POLY_PUBLIC, POLY_PUBLIC), url)
            self.assertEqual(sent, [(url, 'eth_blockNumber')], url)        # one probe, the chain's method
            self.assertEqual([e for e, _ in notes], ['rpc_fallback'])
            k = notes[0][1]
            self.assertEqual(k['host'], url.split('/')[2])                 # the host, nothing after it
            self.assertEqual(k['using'], 'polygon-bor-rpc.publicnode.com')
            self.assertNotIn(ALCHEMY_KEY, json.dumps(notes), url)          # never the key, in any field

    def test_a_working_keyed_endpoint_is_kept_and_nothing_is_said(self):
        good = 'https://polygon-mainnet.g.alchemy.com/v2/' + ALCHEMY_KEY
        used, after, sent, notes = self.probe('https://never.example/dead', fallback=POLY_PUBLIC,
                                              url=good)
        self.assertEqual(used, good)
        self.assertEqual(sent, [(good, 'eth_blockNumber')])
        self.assertEqual(notes, [])

    def test_a_working_endpoint_with_a_password_never_logs_it(self):
        # Until 2026-10-10 the success line kept the user:password@ of the URL,
        # so the service journal showed the password (lp.signers.rpc_host).
        url = f'https://lpbot:{ALCHEMY_KEY}@polygon.example-node.io/rpc'
        out = []
        with mock.patch('urllib.request.urlopen', lambda req, timeout: Resp({'result': '0x1'})), \
                mock.patch.object(config, 'CAPS', chains.caps('polygon')), \
                mock.patch('builtins.print', lambda *a, **k: out.append(' '.join(map(str, a)))):
            self.assertEqual(lp.signers.probe_rpc(url, POLY_PUBLIC), url)
        self.assertTrue(out)
        self.assertNotIn(ALCHEMY_KEY, ' '.join(out))

    def answered(self, result, url='https://own.example/rpc', fallback=POLY_PUBLIC, public=POLY_PUBLIC):
        """probe_rpc where `url` answers `result`; (returned url, requests, notices)."""
        sent, notes = [], []

        def urlopen(req, timeout):
            sent.append(req.full_url)
            return Resp({'jsonrpc': '2.0', 'id': 1, 'result': result})
        with mock.patch('urllib.request.urlopen', urlopen), \
                mock.patch.object(config, 'CAPS', chains.caps('polygon')), mock.patch.object(config, 'RPC', url), \
                mock.patch.object(config, 'PUBLIC_RPC', public), \
                mock.patch.object(lp.books, 'notify', lambda ev, **k: notes.append((ev, k))), mock.patch('builtins.print'):
            used = lp.signers.probe_rpc(url, fallback)
        return used, sent, notes

    def test_an_endpoint_that_is_the_fallback_is_not_probed(self):
        used, sent, notes = self.answered('0x1', url=POLY_PUBLIC)
        self.assertEqual((used, sent, notes), (POLY_PUBLIC, [], []))

    def test_only_a_block_number_is_an_answer(self):
        for result, kept in (('0x4b7c1f2', True), (123456, True), ('oops', False), (None, False), ([], False)):
            used, sent, notes = self.answered(result)
            self.assertEqual(used == 'https://own.example/rpc', kept, result)
            self.assertEqual([e for e, _ in notes], [] if kept else ['rpc_fallback'], result)
            if not kept:
                self.assertEqual(notes[0][1]['reason'], 'no slot in the answer', result)

    def test_the_fallback_given_wins_over_the_configured_one(self):
        used, _, notes = self.answered('oops', fallback='https://given.example', public='https://configured.example')
        self.assertEqual(used, 'https://given.example')
        self.assertEqual(notes[0][1]['using'], 'given.example')

    def test_startup_falls_back_from_a_dead_lpbot_polygon_rpc(self):
        # Until 2026-10-10 the fallback on an EVM chain was the rpc_env endpoint
        # itself, so a dead LPBOT_POLYGON_RPC was kept without a probe. The
        # fallback is now the chain's public_rpc (config.fallback_rpc).
        used, after, sent, notes = self.probe(DEAD_URLS[0])
        self.assertEqual((used, after), (POLY_PUBLIC, POLY_PUBLIC))
        self.assertEqual([e for e, _ in notes], ['rpc_fallback'])


# --- 3. gas price spike on Polygon -----------------------------------------------------

GAS_REFUSAL = 'refused: max fee 2600000000000 wei/gas exceeds LPBOT_EVM_MAX_GWEI'   # venues/uniswap_v3/signer.mjs


class SpikeChain(P.PolyChain):
    """The Polygon fake chain whose open refuses while gas is spiking, as the
    EVM signer does when maxFeePerGas is over LPBOT_EVM_MAX_GWEI."""

    def __init__(self, **held):
        super().__init__(**held)
        self.spike = False

    def answer(self, *args, dex=None, extra_env=None):
        if args[0] == 'open' and self.spike:
            self.calls.append({'profile': config.PROFILE, 'dex': dex, 'args': args, 'env': dict(extra_env or {})})
            return {'error': GAS_REFUSAL}, GAS_REFUSAL
        return super().answer(*args, dex=dex, extra_env=extra_env)


class GasSpikeOnPolygon(P.Polygon):

    def setUp(self):
        super().setUp()
        self.chain = SpikeChain()
        p = mock.patch.object(lp.signers, '_chain', self.chain)
        p.start(); self.addCleanup(p.stop)

    def test_the_gas_refusal_is_not_a_venue_failure(self):
        self.assertFalse(lp.signers.counts_as_failure(GAS_REFUSAL))

    def spiked_after_a_close(self):
        self.opened()
        self.chain.spike = True
        self.chain.calls.clear()
        (self.tmp / 'e2e-poly' / 'REBALANCE').write_text('')             # harvest, close, reopen
        self.poll('e2e-poly')
        self.assertNotIn(P.POLY_POOL, self.chain.positions)              # closed, the reopen refused

    def test_a_refused_reopen_is_tried_again_at_the_next_poll(self):
        self.spiked_after_a_close()
        self.poll('e2e-poly')                                             # the spike goes on
        self.assertEqual(len(self.chain.of('e2e-poly', 'open')), 2)       # one reopen try per poll
        self.chain.spike = False                                          # the spike ends
        self.poll('e2e-poly')
        self.assertEqual(len(self.chain.of('e2e-poly', 'open')), 3)
        self.assertIn(P.POLY_POOL, self.chain.positions)                  # the next poll reopens

    def test_every_refusal_that_sent_nothing_holds(self):
        for err in (GAS_REFUSAL, 'refused: mint paused', 'refused: transfer hook',
                    'refused: wallet sol-lp lock busy', 'refused: claims unmeasurable', 'refused: halted (x)'):
            self.assertTrue(lp.signers.held(err), err)
        for err in (None, '', 'custom program error: 0x177c', 'refused: max fee is fine'):
            self.assertFalse(lp.signers.held(err), err)

    def test_three_refused_reopens_write_no_halt(self):
        # Until 2026-10-10 the gas refusal was not held: reopen() counted it,
        # and the third poll of a spike wrote HALT although nothing was sent.
        self.spiked_after_a_close()
        self.poll('e2e-poly', n=2)                                        # three polls with the spike in all
        self.assertEqual(len(self.chain.of('e2e-poly', 'open')), 3)
        with self.as_profile('e2e-poly'):
            self.assertEqual(health.load(f'venue:{P.DEX}')['fails'], 0)  # the venue's breaker is not fed
        self.assertFalse(self.halt_all.exists())
        self.assertFalse((self.tmp / 'e2e-poly' / 'HALT').exists())


# The Polygon fixture is reused, not its tests: they run in test_polygon_loop.
for _name in [n for n in dir(P.Polygon) if n.startswith('test_')]:
    setattr(GasSpikeOnPolygon, _name, None)


# --- 4. a disabled profile with a position and no CLOSE ---------------------------------

class DisabledHold(M.Fixture):

    def disabled_and_open(self):
        self.chain.wallet[M.SOL] = 2.0
        self.poll('e2e-sol')
        self.assertIn(M.SOL_POOL, self.chain.positions)
        with db.cursor(commit=True) as cur:
            cur.execute("update config set enabled = false where name = 'e2e-sol'")
        self.chain.calls.clear()

    def test_disabled_hold_spawns_no_write_without_a_close_file(self):
        self.disabled_and_open()
        status = self.chain.status(M.SOL_POOL)
        state = {'failures': 0, 'read_failures': 0}
        with self.as_profile('e2e-sol'), mock.patch.object(lp.paths, 'save', lambda s: None):
            self.assertFalse(lp.paths.CLOSE.exists())
            lp.loop.disabled_hold(state, status)
            lp.loop.disabled_hold(state, status)
        self.assertEqual(writes(self.chain), [])
        self.assertEqual(self.chain.calls, [])                           # not even a read
        self.assertEqual(self.events('e2e-sol').count('disabled'), 1)    # said once, not per poll

    def test_a_disabled_profile_holds_its_position_over_many_polls(self):
        self.disabled_and_open()
        pos = dict(self.chain.positions[M.SOL_POOL])
        self.poll('e2e-sol', n=4)
        self.assertEqual(writes(self.chain), [])
        self.assertEqual(self.chain.positions[M.SOL_POOL], pos)
        self.assertEqual(self.events('e2e-sol').count('disabled'), 1)


# --- 5. busy_hour at the hour edges ----------------------------------------------------

PROFILE24 = st.lists(st.floats(min_value=0.1, max_value=3.0, allow_nan=False), min_size=24, max_size=24)
DAYS = st.dates(min_value=dt.date(2024, 1, 1), max_value=dt.date(2030, 12, 31))


class BusyHourEdges(unittest.TestCase):

    def busy_at(self, when, profile):
        with mock.patch.object(db, 'now', lambda: when), mock.patch.object(db, 'season', lambda: profile), \
                mock.patch.object(config, 'DEFER_MOVES_TO_QUIET_HOURS', True):
            return lp.board.busy_hour()

    @settings(max_examples=300, deadline=None)
    @given(day=DAYS, hour=st.integers(0, 23), profile=PROFILE24)
    def test_the_last_second_belongs_to_its_hour_and_the_first_to_the_next(self, day, hour, profile):
        last = dt.datetime.combine(day, dt.time(hour, 59, 59, 999999), dt.timezone.utc)
        first = last + dt.timedelta(microseconds=1)                      # xx:00:00, the next day after 23h
        self.assertEqual(first.hour, (hour + 1) % 24)
        self.assertEqual(self.busy_at(last, profile), profile[hour] > 1.0)
        self.assertEqual(self.busy_at(first, profile), profile[(hour + 1) % 24] > 1.0)
        on_the_hour = dt.datetime.combine(day, dt.time(hour, 0, 0), dt.timezone.utc)
        self.assertEqual(self.busy_at(on_the_hour, profile), profile[hour] > 1.0)

    @settings(max_examples=100, deadline=None)
    @given(day=DAYS, profile=PROFILE24)
    def test_midnight_reads_hour_zero_of_the_new_day(self, day, profile):
        profile = list(profile)
        profile[23], profile[0] = 2.0, 0.5                               # busy late, quiet at 00h
        late = dt.datetime.combine(day, dt.time(23, 59, 59), dt.timezone.utc)
        self.assertTrue(self.busy_at(late, profile))
        self.assertFalse(self.busy_at(late + dt.timedelta(seconds=1), profile))

    def test_no_profile_or_deferral_off_is_never_busy(self):
        when = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.timezone.utc)
        self.assertFalse(self.busy_at(when, None))
        with mock.patch.object(config, 'DEFER_MOVES_TO_QUIET_HOURS', False), \
                mock.patch.object(db, 'season', lambda: [3.0] * 24):
            self.assertFalse(lp.board.busy_hour())


# --- 6. the 'swap' breaker after one Jupiter outage --------------------------------------

JUPITER_429 = 'Jupiter rate limited (429): Too Many Requests'


class JupiterDownChain(M.FakeChain):
    """Jupiter answers 429 to every swap; the Orca fallback swaps."""

    def answer(self, *args, dex=None, extra_env=None):
        if args[0] == 'rebalance' and dex == 'jupiter':
            self.calls.append({'profile': config.PROFILE, 'dex': dex, 'args': args, 'env': dict(extra_env or {})})
            return {'error': JUPITER_429}, JUPITER_429
        if args[0] == 'rebalance' and dex == 'orca-swap':
            out, err = super().answer(*args, dex='jupiter', extra_env=extra_env)
            self.calls[-1]['dex'] = dex
            return out, err
        return super().answer(*args, dex=dex, extra_env=extra_env)


class SwapBreakerAfterJupiterOutage(M.Fixture):

    def setUp(self):
        super().setUp()
        self.chain = JupiterDownChain()
        for p in (mock.patch.object(lp.signers, '_chain', self.chain),
                  mock.patch.object(lp.swaps, 'SWAP_RATE_LIMIT_PAUSES', (1, 2)),   # the fixture's sleep stops at 60 s
                  mock.patch.object(lp.swaps, 'SWAP_FALLBACK', 'orca-swap'),
                  mock.patch.object(lp.housekeeping, 'run_audits', lambda state: None),
                  mock.patch.object(lp.signers, 'probe_breakers', lambda now=None: None)):   # no live quote
            p.start(); self.addCleanup(p.stop)
        with db.cursor(commit=True) as cur:
            cur.execute("delete from health where key like 'e2e-sol|%%' or key in ('swap', 'jupiter')")

    def test_three_polls_of_jupiter_429_rescued_by_orca_keep_the_swap_breaker_closed(self):
        for i in range(3):
            self.chain.positions.clear()                                  # nothing held: each poll opens
            self.chain.wallet.update({M.SOL: 2.0, M.USDC: 0.0})           # all SOL: each open swaps first
            self.poll('e2e-sol')
            self.assertIn(M.SOL_POOL, self.chain.positions, i)
        jup = [c for c in self.chain.of('e2e-sol', 'rebalance') if c['dex'] == 'jupiter']
        orca = [c for c in self.chain.of('e2e-sol', 'rebalance') if c['dex'] == 'orca-swap']
        self.assertEqual((len(jup), len(orca)), (9, 3))                   # three attempts each poll, one rescue
        with self.as_profile('e2e-sol'):
            now = time.time()
            ok, state, _w, rec = health.allowed('swap', now)
            self.assertEqual((ok, state, rec['fails']), (True, health.CLOSED, 0))
            # never failed at all, not failed and then cleared by the rescue
            self.assertIsNone(rec['last_fail'])
            self.assertEqual(rec['trips'], 0)
            ok_j, state_j, wait_j, rec_j = health.allowed('jupiter', now)
        self.assertEqual(rec_j['fails'], 3)                               # one failure per poll, not per attempt
        self.assertFalse(ok_j)
        self.assertIn(state_j, (health.BACKOFF, health.TRIPPED))
        self.assertGreater(wait_j, 0)


if __name__ == '__main__':
    unittest.main()
