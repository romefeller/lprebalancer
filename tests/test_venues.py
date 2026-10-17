"""On-chain fee counters: decoding both layouts, the +/-1% income, wrap-around,
the sampling cadence and the candidate set."""
import math
import time
import unittest
from hypothesis import given, settings, strategies as st
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

from venues import solana_state       # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
from venues.jupiter import prices as jupiter_api  # noqa: E402
import lp.board  # noqa: E402
import lp.harvest  # noqa: E402

SOL, USDC = 'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
B58 = solana_state.b58


def b58decode(s):
    import base64
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    n = 0
    for ch in s:
        n = n * 58 + alphabet.index(ch)
    raw = n.to_bytes(32, 'big')
    return raw


def raydium_account(sqrt_q64, g0, g1, dec_a=9, dec_b=6, liquidity=0):
    o = 8 + 1 + 32 * 7
    raw = bytearray(solana_state.REWARD_INFOS_OFFSET + 3 * solana_state.REWARD_INFO_LEN)
    raw[73:105] = b58decode(SOL); raw[105:137] = b58decode(USDC)
    raw[o] = dec_a; raw[o + 1] = dec_b
    raw[o + 4:o + 20] = liquidity.to_bytes(16, 'little')
    raw[o + 20:o + 36] = sqrt_q64.to_bytes(16, 'little')
    raw[o + 44:o + 60] = g0.to_bytes(16, 'little'); raw[o + 60:o + 76] = g1.to_bytes(16, 'little')
    return bytes(raw)


def orca_account(sqrt_q64, g0, g1, liquidity=0):
    raw = bytearray(700)
    raw[49:65] = liquidity.to_bytes(16, 'little')
    raw[65:81] = sqrt_q64.to_bytes(16, 'little')
    raw[101:133] = b58decode(SOL); raw[181:213] = b58decode(USDC)
    raw[165:181] = g0.to_bytes(16, 'little'); raw[245:261] = g1.to_bytes(16, 'little')
    return bytes(raw)


SQRT = int(math.sqrt(121.0 * 1e-3) * 2 ** 64)       # SOL at $121 in raw units (9 vs 6 decimals)


class Decode(unittest.TestCase):
    def test_both_layouts(self):
        r = solana_state.decode_fee_state(raydium_account(SQRT, 5, 7), 'raydium')
        self.assertEqual((r['g0'], r['g1'], r['dec_a'], r['dec_b']), (5, 7, 9, 6))
        self.assertEqual((r['mint_a'], r['mint_b']), (SOL, USDC))
        o = solana_state.decode_fee_state(orca_account(SQRT, 11, 13), 'orca')
        self.assertEqual((o['g0'], o['g1'], o['mint_a'], o['mint_b']), (11, 13, SOL, USDC))
        self.assertIsNone(solana_state.decode_fee_state(b'short', 'raydium'))
        self.assertIsNone(solana_state.decode_fee_state(b'short', 'orca'))

    def test_both_layouts_say_the_pools_active_liquidity(self):
        self.assertEqual(solana_state.decode_fee_state(raydium_account(SQRT, 5, 7, liquidity=892017914459), 'raydium')['liquidity'],
                         892017914459)
        self.assertEqual(solana_state.decode_fee_state(orca_account(SQRT, 1, 1, liquidity=659880000000000), 'orca')['liquidity'],
                         659880000000000)

    def test_orca_decimals_come_from_the_mints(self):
        with mock.patch.object(solana_state, 'pool_accounts', lambda addrs: {
                'P': orca_account(SQRT, 1, 1), SOL: bytes(44) + bytes([9]), USDC: bytes(44) + bytes([6])}
                if 'P' in addrs else {SOL: bytes(44) + bytes([9]), USDC: bytes(44) + bytes([6])}):
            solana_state._MINT_DECIMALS.clear()
            st = solana_state.fee_states([('orca', 'P'), ('meteora-dlmm', 'M')])
        self.assertEqual((st['P']['dec_a'], st['P']['dec_b']), (9, 6)); self.assertNotIn('M', st)


class Income(unittest.TestCase):
    def state(self, g0, g1):
        return {'sqrt_price': SQRT, 'g0': g0, 'g1': g1, 'dec_a': 9, 'dec_b': 6, 'rewards': []}

    def test_matches_a_direct_calculation(self):
        # a unit of raw liquidity earning 1 raw USDC per day: compare to its +/-1% value
        g1 = 2 ** 64                                         # +1 raw USDC per unit L
        inc = solana_state.band_income(self.state(0, 0), self.state(0, g1), 86400, 1.01, 121.0, 1.0)
        sp = SQRT / 2 ** 64; sa, sb = sp / math.sqrt(1.01), sp * math.sqrt(1.01)
        value = (1 / sp - 1 / sb) / 1e9 * 121.0 + (sp - sa) / 1e6
        self.assertAlmostEqual(inc['fee_pct_day'], (1e-6 / value) * 100, places=9)
        self.assertAlmostEqual(inc['value_per_l_usd'], value, places=15)

    def test_wrap_around_and_narrower_bands_earn_more(self):
        near = 2 ** 128 - 2 ** 60
        wrapped = solana_state.band_income(self.state(near, 0), self.state(2 ** 60, 0), 3600, 1.01, 121.0, 1.0)
        plain = solana_state.band_income(self.state(0, 0), self.state(2 ** 61, 0), 3600, 1.01, 121.0, 1.0)
        self.assertAlmostEqual(wrapped['fee_pct_day'], plain['fee_pct_day'])
        wide = solana_state.band_income(self.state(0, 0), self.state(2 ** 61, 0), 3600, 1.05, 121.0, 1.0)
        self.assertGreater(plain['fee_pct_day'], wide['fee_pct_day'] * 4)
        self.assertIsNone(solana_state.band_income(self.state(0, 0), self.state(1, 1), 0, 1.01, 121.0, 1.0))


class Sampling(unittest.TestCase):
    def test_cadence_and_candidates(self):
        calls = []
        rows = [{'address': 'R2', 'dex': 'raydium-clmm', 'screen_ok': True, 'skipped': None, 'pair': 'SOL/USDC',
                 'token_a': {'address': SOL}, 'token_b': {'address': USDC}},
                {'address': 'M1', 'dex': 'meteora-dlmm', 'screen_ok': True, 'skipped': None,
                 'token_a': {'address': SOL}, 'token_b': {'address': USDC}},
                {'address': 'X1', 'dex': 'orca', 'screen_ok': True, 'skipped': None,
                 'token_a': {'address': SOL}, 'token_b': {'address': 'JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'}},
                {'address': 'U1', 'dex': 'orca', 'screen_ok': False, 'skipped': None,
                 'token_a': {'address': SOL}, 'token_b': {'address': USDC}}]
        with mock.patch.object(db, 'latest_scan', lambda max_age_seconds=None: ({'id': 1}, rows)), \
                mock.patch.object(lp.capital, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(lp.board, 'home_rows', lambda mints: []), \
                mock.patch.object(config, 'POOL', 'HELD'), \
                mock.patch.object(config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(config, 'ALLOW_SWAP', False):
            got = {a for _, a, _ in lp.board.venue_candidates()}
            self.assertEqual(got, {'HELD', 'R2'})           # Meteora: no counters; X1: other pair; U1: unscreened
            with mock.patch.object(solana_state, 'fee_states', lambda pools: calls.append(pools) or {}), \
                    mock.patch.object(lp.paths, 'save', lambda s: None), \
                    mock.patch.object(config, 'VENUE_SAMPLE_S', 600):
                st = {'last_fee_sample': time.time() - 30}
                lp.board.sample_fee_growth(st); self.assertEqual(calls, [])
                st['last_fee_sample'] = time.time() - 700
                lp.board.sample_fee_growth(st); self.assertEqual(len(calls), 1)


class HomePools(unittest.TestCase):
    """The SOL/USDC home pools stay candidates when the scan screens them out
    (8sLb on the API's volume, 2026-10-16)."""

    def setUp(self):
        lp.board._HOME_ROWS.clear()

    def rec(self, dex, address):
        return {'pair': 'SOL/USDC', 'token_a': {'address': SOL}, 'token_b': {'address': USDC}}

    def test_home_pools_join_an_empty_board(self):
        home = lp.board.chains.SOLANA['sol_usdc_pools']
        with mock.patch.object(db, 'latest_scan', lambda max_age_seconds=None: ({'id': 1}, [])), \
                mock.patch.object(lp.capital, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(lp.board.dexes, 'pool', self.rec), \
                mock.patch.object(config, 'POOL', 'HELD'), \
                mock.patch.object(config, 'DEX', 'raydium-clmm'):
            got = {a: row for _, a, row in lp.board.venue_candidates()}
        want = {a for d, a in home.items() if d in solana_state.FEE_LAYOUT_DEXES}
        self.assertEqual(set(got), {'HELD'} | want)
        self.assertNotIn(home['meteora-dlmm'], got)           # no counters on DLMM
        self.assertIsNone(got['HELD'])                         # the held pool stays the held row
        r = got[home['raydium-clmm']]
        self.assertEqual((r['dex'], r['address']), ('raydium-clmm', home['raydium-clmm']))

    def test_other_pairs_get_none_and_a_failed_read_is_retried(self):
        self.assertEqual(lp.board.home_rows({SOL, 'MU'}), [])
        self.assertEqual(lp.board.home_rows(None), [])
        with mock.patch.object(lp.board.dexes, 'pool', mock.Mock(side_effect=RuntimeError('api down'))):
            self.assertEqual(lp.board.home_rows({SOL, USDC}), [])
        calls = []
        with mock.patch.object(lp.board.dexes, 'pool', lambda d, a: calls.append(a) or self.rec(d, a)):
            first = lp.board.home_rows({SOL, USDC})
            lp.board.home_rows({SOL, USDC})
        self.assertTrue(first)
        self.assertEqual(len(calls), len(first))              # one API read per pool, then cached


class RecentGain(unittest.TestCase):
    """A calm move needs the gain over 24 hours AND over the recent hours."""

    def run_check(self, recent_held, recent_top):
        venues = [{'dex': 'raydium-clmm', 'address': 'HELD', 'held': True, 'pair': 'SOL/USDC',
                   'total_pct_day': 3.0, 'rank_pct_day': 3.0, 'hours': 24.0, 'row': None},
                  {'dex': 'raydium-clmm', 'address': 'TOP', 'held': False, 'pair': 'SOL/USDC',
                   'total_pct_day': 4.5, 'rank_pct_day': 4.5, 'hours': 24.0, 'row': {'dex': 'raydium-clmm', 'address': 'TOP'}}]
        recent = {'HELD': recent_held, 'TOP': recent_top}
        moved, told = [], []
        with mock.patch.object(lp.board, 'venue_view', lambda p, q, capital_usd=None: venues), \
                mock.patch.object(lp.board, 'venue_income',
                                  lambda a, ua, ub, band=1.01, hours=24, **k: recent[a] and {'total_pct_day': recent[a],
                                                                                           'rank_pct_day': recent[a]}), \
                mock.patch.object(lp.board.moves, 'rebalance', lambda *a, **k: moved.append(k)), \
                mock.patch.object(lp.board.books, 'notify', lambda kind, **k: told.append((kind, k))), \
                mock.patch.object(lp.board.db, 'event', lambda *a: None), \
                mock.patch.object(config, 'POOL_PINNED', False), \
                mock.patch.object(config, 'REGIME_ENABLED', False), \
                mock.patch.object(config, 'MIGRATE_MIN_GAIN', 0.25), \
                mock.patch.object(config, 'VENUE_MIN_HOURS', 6), \
                mock.patch.object(config, 'EXECUTE_DEXES', ('raydium-clmm',)), \
                mock.patch.object(lp.board.signers, 'SIGNERS', {'raydium-clmm': None}):
            ok = lp.board.calm_board_check({}, {'price': 105.0, 'quoteUsd': 1.0})
        return ok, moved, told

    def test_a_24h_win_lost_in_the_recent_hours_stays(self):
        ok, moved, told = self.run_check(recent_held=2.6, recent_top=1.3)
        self.assertFalse(ok)
        self.assertEqual(moved, [])
        self.assertIn('last 6h', told[-1][1]['verdict'])

    def test_a_win_over_both_windows_moves(self):
        ok, moved, _ = self.run_check(recent_held=2.0, recent_top=3.0)
        self.assertTrue(ok)
        self.assertEqual(len(moved), 1)

    def test_no_recent_income_stays(self):
        ok, moved, _ = self.run_check(recent_held=None, recent_top=3.0)
        self.assertFalse(ok)
        self.assertEqual(moved, [])


class Crowding(unittest.TestCase):
    """The board ranks what a bot adds to all our bots together (fleet_factor):
    2026-10-17, our four bots held 65% of 8sLb's active liquidity."""
    F = staticmethod(lp.board.fleet_factor)

    def test_the_table(self):
        # tang0 in 8sLb with the others: L 8.95e11, ours 5.83e11, tang0 3.48e11
        stay = self.F(True, 8.95e11, 5.83e11, 3.48e11)
        self.assertAlmostEqual(stay, (8.95e11 - 5.83e11) / (8.95e11 - 3.48e11))          # ~0.57
        # 3ucNos: ours 0.1%, tang0 joins
        join = self.F(False, 1.0581e14, 1.9e11, 3.48e11)
        self.assertGreater(join, 0.99)
        # an empty pool of ours, a pool all ours, unknown figures
        self.assertEqual(self.F(False, 1e12, 0, 0), 1.0)
        self.assertEqual(self.F(True, 1e12, 1e12, 1e11), 0.0)                           # all ours: nothing from outsiders
        for bad in ((False, None, 1, 1), (False, 0, 1, 1), (False, 1e12, None, 1), (False, 1e12, 0, None), (False, 1e12, 0, -1)):
            self.assertIsNone(self.F(*bad), bad)
        self.assertEqual(self.F(True, 1e12, 1e12, 1e12), 0.0)                           # the bot is the whole pool

    @settings(max_examples=400, deadline=None)
    @given(st.floats(1e6, 1e16), st.floats(0, 1), st.floats(0, 1), st.booleans())
    def test_property_bounds_and_monotone(self, total, ours_share, bot_share, held):
        ours = total * ours_share
        bot = (ours if held else total) * bot_share
        f = self.F(held, total, ours, bot)
        self.assertTrue(0.0 <= f <= 1.0)
        # more of ours in the pool never raises it
        more = self.F(held, total, min(total, ours * 1.5 + 1), bot)
        self.assertLessEqual(more, f + 1e-12)
        # joining and staying measure the same fees: the counters' rate per unit of liquidity is F / total
        # before the join and F / (total + bot) after it, so factor x rate is the same on both sides
        if not held and bot > 0:
            stay = self.F(True, total + bot, ours + bot, bot)
            self.assertAlmostEqual(stay / (total + bot), f / total, delta=1e-12 / total)

    def test_a_crowded_thin_pool_ranks_under_a_deep_one(self):
        # THIN pays twice per unit of liquidity but our bots are 60% of it; DEEP is all outsiders
        incomes = {'THIN': (4.0, 1.0e12, 6.0e11), 'DEEP': (2.0, 1.0e14, 0)}
        def income(a, ua, ub, band=1.01, hours=24, capital_usd=None, held=False):
            total, l, ours = incomes[a]
            f = lp.board.fleet_factor(held, l, ours, capital_usd / 1e-9) if capital_usd else None
            return {'fee_pct_day': total, 'reward_pct_day': 0.0, 'total_pct_day': total, 'hours': 24.0,
                    'fleet_factor': f, 'rank_pct_day': total * f if f is not None else total}
        cands = [('raydium-clmm', 'THIN', None), ('raydium-clmm', 'DEEP', None)]
        with mock.patch.object(lp.board, 'venue_candidates', lambda: cands), \
                mock.patch.object(lp.board, 'venue_income', income), \
                mock.patch.object(config, 'POOL', 'ELSEWHERE'):
            plain = lp.board.venue_view(105.0, 1.0)
            crowded = lp.board.venue_view(105.0, 1.0, capital_usd=600.0)
        self.assertEqual([v['address'] for v in plain], ['THIN', 'DEEP'])            # per unit of liquidity
        self.assertEqual([v['address'] for v in crowded], ['DEEP', 'THIN'])          # what it adds to our bots

    def test_failover_ranks_on_the_fleet_figure(self):
        venues = [{'dex': 'raydium-clmm', 'held': True, 'total_pct_day': 3.0, 'rank_pct_day': 3.0, 'hours': 24},
                  {'dex': 'orca', 'held': False, 'total_pct_day': 5.0, 'rank_pct_day': 1.0, 'hours': 24, 'row': {'dex': 'orca'}},
                  {'dex': 'byreal', 'held': False, 'total_pct_day': 2.9, 'rank_pct_day': 2.9, 'hours': 24, 'row': {'dex': 'byreal'}}]
        pick = lp.board.failover_pick('raydium-clmm', venues, lambda d: True, execute_dexes=('orca', 'byreal'),
                                      signers={'orca': 1, 'byreal': 1}, min_hours=6)
        self.assertEqual(pick['dex'], 'byreal', 'orca pays more per unit but mostly from our own bots')

    def test_our_liquidity_sums_every_profiles_open_positions_latest_snapshot(self):
        _fixtures.ensure_profile()
        with db.cursor(commit=True) as cur:
            cur.execute("delete from snapshots where mint like 'CROWD%'")
            cur.execute("delete from positions where mint like 'CROWD%'")
            for mint, closed, ls in (('CROWD1', None, (10, 30)), ('CROWD2', None, (50,)), ('CROWD3', 'now()', (999,))):
                cur.execute(f"insert into positions (mint, config_name, pool, opened_at, closed_at, dex) values "
                            f"(%s, 'sol-usdc', 'CROWDPOOL', now() - interval '1 hour', {closed or 'null'}, 'raydium-clmm')", (mint,))
                for i, l in enumerate(ls):
                    cur.execute("insert into snapshots (ts, mint, liquidity) values (now() - make_interval(mins => %s), %s, %s)",
                                (10 - i, mint, l))
        try:
            self.assertEqual(db.our_liquidity('CROWDPOOL'), 30 + 50)                  # the latest of each open one
            self.assertEqual(db.our_liquidity('NOPOOL'), 0)
        finally:
            with db.cursor(commit=True) as cur:
                cur.execute("delete from snapshots where mint like 'CROWD%'")
                cur.execute("delete from positions where mint like 'CROWD%'")


class Ledger(unittest.TestCase):
    def test_samples_span_and_fourteen_day_window(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate fee_growth')
            for age in ('3 days', '13 days 23 hours', '14 days 1 hour', '20 days'):
                cur.execute("insert into fee_growth (ts, dex, pool, sqrt_price, g0, g1, dec_a, dec_b, mint_a, mint_b) "
                            f"values (now() - interval '{age}', 'orca', 'OLD', 1, 0, 0, 9, 6, 'a', 'b')")
        st = {'sqrt_price': SQRT, 'g0': 1, 'g1': 2, 'dec_a': 9, 'dec_b': 6, 'mint_a': SOL, 'mint_b': USDC, 'rewards': []}
        db.record_fee_state('orca', 'P', st)
        with db.cursor() as cur:
            cur.execute("select count(*) n from fee_growth where pool = 'OLD'")
            self.assertEqual(cur.fetchone()['n'], 2)                    # kept 14 days: 3 d and 13 d 23 h stay
        self.assertEqual(db.FEE_GROWTH_KEEP_DAYS, 14)
        self.assertIsNone(db.fee_state_span('P'))            # one sample of P: no span yet
        db.record_fee_state('orca', 'P', dict(st, g0=5))
        first, last, secs = db.fee_state_span('P')
        self.assertEqual((int(first['g0']), int(last['g0'])), (1, 5)); self.assertGreaterEqual(secs, 0)


class RewardPrices(unittest.TestCase):
    def test_a_failed_fetch_uses_the_last_good_price_for_six_hours(self):
        lp.harvest._REWARD_PX.clear()
        with mock.patch.object(jupiter_api, 'jupiter_prices', lambda m: {'CAKE': 2.7}):
            self.assertEqual(lp.harvest.reward_prices(['CAKE']), {'CAKE': 2.7})
        with mock.patch.object(jupiter_api, 'jupiter_prices', lambda m: {}):
            self.assertEqual(lp.harvest.reward_prices(['CAKE']), {'CAKE': 2.7})
        lp.harvest._REWARD_PX['CAKE'] = (time.time() - 7 * 3600, 2.7)
        with mock.patch.object(jupiter_api, 'jupiter_prices', lambda m: {}):
            self.assertEqual(lp.harvest.reward_prices(['CAKE']), {})
        lp.harvest._REWARD_PX.clear()
