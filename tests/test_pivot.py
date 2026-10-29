"""The kept-fee pool review and the SOL/USDC <-> SOL/USD1 pivot (owner
2026-10-29): ranking on fees x (1 - 1/R), our own dilution of the target,
the pivot candidates, the payout token following the pool, and the vault and
pin limits."""
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import chains  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import lp.board  # noqa: E402
import lp.swaps  # noqa: E402

SOL = chains.SOLANA['native_mint']
USDC = chains.SOLANA['usdc_mint']
USD1 = chains.SOLANA['stable_mints']['USD1']
P8, AQ = chains.SOLANA['pivot_pools']['raydium-clmm']
B = lp.board


class Pure(unittest.TestCase):
    def test_entry_share(self):
        # SOL/USD1 holds ~$142k at +/-1%, 8sLb ~$2.4k (2026-10-29): $600 dilutes one, not the other
        self.assertAlmostEqual(B.entry_share(142_000, 1.0, 600), 142_000 / 142_600)
        self.assertAlmostEqual(B.entry_share(2_400, 1.0, 600), 0.8)
        self.assertAlmostEqual(B.entry_share(300, 2.0, 600), 0.5)   # $2 per unit of L: $600 is 300 L
        self.assertEqual(B.entry_share(None, 1.0, 600), 1.0)
        for vpl in (None, 0, -1.0):
            self.assertEqual(B.entry_share(100, vpl, 600), 1.0)
        self.assertEqual(B.entry_share(100, 1.0, None), 1.0)
        self.assertEqual(B.entry_share(100, 1.0, -5.0), 1.0)
        self.assertEqual(B.entry_share(0, 1.0, 0), 1.0)
        self.assertEqual(B.entry_share(0, 1.0, 600), 0.0)          # an empty pool keeps nothing for us
        self.assertEqual(B.entry_share(0, 1.0, 0.5), 0.0)          # whatever the units

    def test_kept(self):
        self.assertAlmostEqual(B.kept_pct_day(1.6, 1.10), 1.6 * (1 - 1 / 1.10))
        self.assertAlmostEqual(B.kept_pct_day(1.3, 1.44, 0.5), 1.3 * 0.5 * (1 - 1 / 1.44))
        self.assertLess(B.kept_pct_day(1.5, 0.9), 0)
        self.assertIsNone(B.kept_pct_day(1.5, None))

    def test_beats(self):
        self.assertIs(True, B.beats(0.15, 0.39, 0.25))                  # SOL/USD1 vs 8sLb, 7 days to 10-29
        self.assertIs(False, B.beats(0.30, 0.36, 0.25))                 # +20%: under the ratio
        self.assertIs(False, B.beats(0.5, 0.62, 0.25))                  # +0.12 %/d but +24%: the ratio decides
        self.assertIs(True, B.beats(0.5, 0.625, 0.25))                  # exactly +25%
        self.assertIs(True, B.beats(0.5, 1.0, 0.25, min_abs=0.5))       # exactly min_abs
        self.assertIs(False, B.beats(0.5, 0.99, 0.25, min_abs=0.5))
        self.assertIs(True, B.beats(0.0, 0.2, 0.25))                    # keeping nothing: no ratio, no division
        self.assertIs(False, B.beats(0.02, 0.05, 0.25))                 # +150% but 0.03%/d: R near 1 is noise
        self.assertIs(True, B.beats(-0.05, 0.06, 0.25))                 # the held pool keeps nothing
        self.assertIs(False, B.beats(-0.05, 0.03, 0.25))                # ...but the target must add PIVOT_MIN_ABS
        self.assertIs(False, B.beats(0.2, None, 0.25))
        self.assertIs(True, B.beats(0.08, 0.16, 0.25, min_abs=0.0))     # the 6h check, 2026-10-29 12Z: quiet hours
        self.assertIs(False, B.beats(-0.2, 0.0, 0.25, min_abs=0.0))     # keeping nothing never wins
        self.assertIs(False, B.beats(-0.2, -0.1, 0.25, min_abs=0.0))
        self.assertIs(False, B.beats(None, 0.5, 0.25))

    def test_payout_mint_follows_the_stablecoin(self):
        self.assertEqual(B.payout_mint_for(USDC, SOL, USD1), USD1)
        self.assertEqual(B.payout_mint_for(USD1, SOL, USDC), USDC)
        self.assertIsNone(B.payout_mint_for(USDC, SOL, USDC))       # same stablecoin: unchanged
        self.assertIsNone(B.payout_mint_for(SOL, SOL, USD1))        # a non-stable payout token is the owner's choice
        self.assertIsNone(B.payout_mint_for('JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN', SOL, USD1))
        self.assertIsNone(B.payout_mint_for(None, SOL, USD1))
        self.assertIsNone(B.payout_mint_for('', SOL, USD1))
        self.assertIsNone(B.payout_mint_for(USDC, SOL, 'JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'))
        self.assertEqual(B.payout_mint_for(USDC, USD1, SOL), USD1)  # stablecoin as token A

    def test_left_behind_is_the_old_stablecoin(self):
        self.assertEqual(lp.swaps.left_behind([(SOL, 'SOL'), (USDC, 'USDC')], [(SOL, 'SOL'), (USD1, 'USD1')]), [USDC])
        self.assertEqual(lp.swaps.left_behind([(SOL, 'SOL'), (USD1, 'USD1')], [(SOL, 'SOL'), (USDC, 'USDC')]), [USD1])

    def test_usd1_is_a_dollar(self):
        self.assertTrue(lp.board.capital.is_stable_mint(USD1))


def row(addr, quote):
    return {'dex': 'raydium-clmm', 'address': addr, 'pair': f"SOL/{'USD1' if quote == USD1 else 'USDC'}",
            'token_a': {'address': SOL, 'symbol': 'SOL'},
            'token_b': {'address': quote, 'symbol': 'USD1' if quote == USD1 else 'USDC'}}


def venue(addr, quote, fee, held=False, active_l=None, vpl=None, hours=24.0):
    return {'dex': 'raydium-clmm', 'address': addr, 'held': held, 'pair': row(addr, quote)['pair'],
            'total_pct_day': fee, 'fee_pct_day': fee, 'reward_pct_day': 0.0, 'hours': hours,
            'active_l': active_l, 'value_per_l_usd': vpl, 'row': None if held else row(addr, quote)}


class Review(unittest.TestCase):
    """calm_board_check on kept fees. Numbers: 7 days to 2026-10-29."""

    def go(self, venues, rs, recent=None, pinned=False, vault=None, held_quote=USDC, cap=600.0, choice=1.01,
           execute=('raydium-clmm',), quote=1.0):
        moved, told, self.asked = [], [], []
        recent = recent if recent is not None else {v['address']: v for v in venues}
        with mock.patch.object(B, 'venue_view', lambda p, q=1.0: venues), \
                mock.patch.object(B, 'venue_income',
                                  lambda a, ua, ub, band=1.01, hours=24: self.asked.append((a, ua, ub)) or recent.get(a)), \
                mock.patch.object(B.db, 'pool_r', lambda a, days=7: rs.get(a)), \
                mock.patch.object(B.db, 'event', lambda *a: None), \
                mock.patch.object(B.books, 'notify', lambda ev, **k: told.append((ev, k))), \
                mock.patch.object(B.moves, 'rebalance', lambda *a, **k: moved.append(k)), \
                mock.patch.object(B.regime, 'regime_choice_now', lambda *a: choice), \
                mock.patch.object(B.capital, 'pool_tokens', lambda: ((SOL, 'SOL'), (held_quote, 'X'))), \
                mock.patch.object(config, 'POOL', P8), \
                mock.patch.object(config, 'POOL_PINNED', pinned), \
                mock.patch.object(config, 'VAULT', vault), \
                mock.patch.object(config, 'MIGRATE_MIN_GAIN', 0.25), \
                mock.patch.object(config, 'VENUE_MIN_HOURS', 6), \
                mock.patch.object(config, 'REGIME_ENABLED', True), \
                mock.patch.object(config, 'EXECUTE_DEXES', execute), \
                mock.patch.object(config, 'REGIME_WIDTHS', (1.0125, 1.02)), \
                mock.patch.object(B.signers, 'SIGNERS', {'raydium-clmm': None, 'orca': None}):
            ok = B.calm_board_check({}, {'price': 100.0, 'quoteUsd': quote, 'positionUsd': cap})
        self.assertIsInstance(ok, bool)
        return ok, moved, told

    V = [venue(P8, USDC, 1.60, held=True), venue(AQ, USD1, 1.29, active_l=142_000, vpl=1.0)]
    R = {P8: (1.10, 168.0), AQ: (1.44, 168.0)}

    def test_fewer_fees_but_more_kept_moves_to_usd1(self):
        ok, moved, told = self.go(self.V, self.R)
        self.assertTrue(ok)
        self.assertEqual(moved[0]['target']['address'], AQ)
        self.assertEqual(moved[0]['band'], 1.01)
        self.assertIs(moved[0]['calm_move'], True)
        self.assertEqual(told[-1][0], 'MIGRATE')
        self.assertIn('R7d 1.44', told[-1][1]['best'])

    def test_evidence_limits_are_inclusive(self):
        six = [self.V[0], venue(AQ, USD1, 1.29, active_l=142_000, vpl=1.0, hours=6.0)]
        self.assertTrue(self.go(six, self.R)[0])                                     # 6 h of counters: enough
        self.assertFalse(self.go([self.V[0], dict(six[1], hours=5.9)], self.R)[0])
        self.assertTrue(self.go(self.V, {P8: (1.10, 72.0), AQ: (1.44, 72.0)})[0])     # 72 h of R: enough
        self.assertFalse(self.go(self.V, {P8: (1.10, 168.0), AQ: (1.44, 71.9)})[0])

    def test_no_recent_income_on_either_pool_stays(self):
        self.assertFalse(self.go(self.V, self.R, recent={P8: self.V[0]})[0])
        self.assertFalse(self.go(self.V, self.R, recent={AQ: self.V[1]})[0])

    def test_held_evidence_limit_is_inclusive(self):
        self.assertTrue(self.go([dict(self.V[0], hours=6.0), self.V[1]], self.R)[0])
        self.assertFalse(self.go([dict(self.V[0], hours=5.9), self.V[1]], self.R)[0])

    def test_the_position_sizes_our_dilution(self):
        # $1,000 of theirs: $100 of ours keeps 91% of the target's fees, $5,000 keeps 17%
        mid = [self.V[0], venue(AQ, USD1, 1.29, active_l=1_000, vpl=1.0)]
        self.assertTrue(self.go(mid, self.R, cap=100.0)[0])
        self.assertFalse(self.go(mid, self.R, cap=5_000.0)[0])

    def test_recent_income_is_read_in_dollars(self):
        self.go(self.V, self.R, quote=2.0)
        self.assertEqual(self.asked, [(P8, 200.0, 2.0), (AQ, 200.0, 2.0)])

    def test_the_24h_must_win_too(self):
        recent = {P8: self.V[0], AQ: venue(AQ, USD1, 5.0, active_l=142_000, vpl=1.0)}
        ok, moved, told = self.go(self.V, {P8: (1.3, 168.0), AQ: (1.3, 168.0)}, recent=recent)
        self.assertFalse(ok); self.assertEqual(moved, [])

    def test_no_quote_price_reads_nothing(self):
        self.assertIs(self.go(self.V, self.R, quote=None)[0], False)
        self.assertEqual(self.asked, [])

    def test_only_an_executable_venue(self):
        orca = [self.V[0], dict(venue(AQ, USD1, 1.29, active_l=142_000, vpl=1.0), dex='orca')]
        self.assertFalse(self.go(orca, self.R)[0])
        self.assertTrue(self.go(orca, self.R, execute=('raydium-clmm', 'orca'))[0])
        unsigned = [self.V[0], dict(self.V[1], dex='byreal')]                       # executable, no signer armed
        self.assertFalse(self.go(unsigned, self.R, execute=('raydium-clmm', 'byreal'))[0])

    def test_no_regime_choice_opens_the_narrowest_width(self):
        ok, moved, _ = self.go(self.V, self.R, choice=None)
        self.assertTrue(ok); self.assertEqual(moved[0]['band'], 1.0125)

    def test_a_pool_without_r_never_outranks_one_with_r(self):
        x = '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv'
        v = [self.V[0], venue(x, USDC, 9.0, active_l=10**9, vpl=1.0), self.V[1]]
        ok, moved, _ = self.go(v, self.R)
        self.assertTrue(ok); self.assertEqual(moved[0]['target']['address'], AQ)

    def test_same_r_ranks_on_fees_as_before(self):
        ok, moved, _ = self.go(self.V, {P8: (1.3, 168.0), AQ: (1.3, 168.0)})
        self.assertFalse(ok); self.assertEqual(moved, [])

    def test_our_own_entry_dilutes_the_target(self):
        # a thin target: $600 of ours against $300 of theirs leaves a third of its fees
        thin = [self.V[0], venue(AQ, USD1, 1.29, active_l=300, vpl=1.0)]
        ok, moved, told = self.go(thin, self.R)
        self.assertFalse(ok); self.assertEqual(moved, [])
        self.assertIn('kept fees', told[-1][1]['verdict'])

    def test_no_r_or_short_r_stays(self):
        self.assertFalse(self.go(self.V, {P8: (1.10, 168.0)})[0])                   # target has no R
        self.assertFalse(self.go(self.V, {P8: (1.10, 168.0), AQ: (1.44, 40.0)})[0])  # 40 h: too short
        ok, _, told = self.go(self.V, {AQ: (1.44, 168.0)})                         # held has no R
        self.assertFalse(ok); self.assertIn('held pool', told[-1][1]['verdict'])

    def test_target_under_r1_never_wins(self):
        ok, moved, _ = self.go(self.V, {P8: (1.10, 168.0), AQ: (0.95, 168.0)})
        self.assertFalse(ok); self.assertEqual(moved, [])

    def test_recent_hours_must_agree(self):
        recent = {P8: venue(P8, USDC, 3.0, held=True), AQ: venue(AQ, USD1, 0.4, active_l=142_000, vpl=1.0)}
        ok, moved, told = self.go(self.V, self.R, recent=recent)
        self.assertFalse(ok); self.assertEqual(moved, [])
        self.assertIn('last 6h', told[-1][1]['verdict'])

    def test_quiet_recent_hours_still_move(self):
        # 2026-10-29 12Z, live: 6h fees 0.93 (8sLb) and 0.52 (SOL/USD1): kept 0.08 vs 0.16
        recent = {P8: venue(P8, USDC, 0.93, held=True), AQ: venue(AQ, USD1, 0.52, active_l=142_000, vpl=1.0)}
        ok, moved, _ = self.go(self.V, self.R, recent=recent)
        self.assertTrue(ok); self.assertEqual(moved[0]['target']['address'], AQ)

    def test_pinned_says_what_it_would_do(self):
        ok, moved, told = self.go(self.V, self.R, pinned=True)
        self.assertFalse(ok); self.assertEqual(moved, [])
        self.assertTrue(told[-1][1]['shadow'])
        self.assertIn(f'would move to raydium-clmm {AQ}', told[-1][1]['verdict'])

    def test_a_vault_never_changes_stablecoin(self):
        ok, moved, told = self.go(self.V, self.R, vault={'kind': 'squads'})
        self.assertFalse(ok); self.assertEqual(moved, [])
        self.assertIn('vault cannot change stablecoin', told[-1][1]['verdict'])

    def test_a_vault_may_move_within_its_stablecoin(self):
        other = [self.V[0], venue('3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv', USDC, 2.5, active_l=10**9, vpl=1.0)]
        ok, moved, _ = self.go(other, {P8: (1.10, 168.0), other[1]['address']: (1.30, 168.0)}, vault={'kind': 'squads'})
        self.assertTrue(ok); self.assertEqual(len(moved), 1)

    def test_back_from_usd1_when_8slb_keeps_more(self):
        v = [venue(AQ, USD1, 1.29, held=True), venue(P8, USDC, 2.4, active_l=10**9, vpl=1.0)]
        with mock.patch.object(config, 'POOL', AQ):
            ok, moved, _ = self.go(v, {P8: (1.6, 168.0), AQ: (1.10, 168.0)}, held_quote=USD1)
        self.assertTrue(ok); self.assertEqual(moved[0]['target']['address'], P8)


class Candidates(unittest.TestCase):
    def setUp(self):
        B._HOME_ROWS.clear()

    def tearDown(self):
        B._HOME_ROWS.clear()

    def cands(self, pool, held):
        recs = {P8: row(P8, USDC), AQ: row(AQ, USD1)}
        with mock.patch.object(db, 'latest_scan', lambda max_age_seconds=None: ({'id': 1}, [])), \
                mock.patch.object(B.capital, 'pool_tokens', lambda: ((SOL, 'SOL'), (held, 'X'))), \
                mock.patch.object(B.dexes, 'pool', lambda d, a: recs.get(a) or row(a, USDC)), \
                mock.patch.object(config, 'POOL', pool), \
                mock.patch.object(config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(config, 'ALLOW_SWAP', False):
            return {a for _, a, _ in B.venue_candidates()}

    def test_a_pivot_pool_sees_the_other(self):
        self.assertIn(AQ, self.cands(P8, USDC))
        self.assertIn(P8, self.cands(AQ, USD1))

    def test_other_pools_see_no_pivot(self):
        self.assertNotIn(AQ, self.cands('3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv', USDC))


class Ledger(unittest.TestCase):
    def test_pool_r_from_pool_hourly(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from pool_hourly where pool in ('RP', 'RQ')")
            for h in range(100):
                cur.execute("insert into pool_hourly (pool, hour, dex, mint_a, mint_b, fee_yield, var_sum, secs) "
                            "values ('RP', date_trunc('hour', now()) - make_interval(hours => %s), 'x', 'a', 'b', "
                            "0.0003, 0.0016, 3600)", (h,))
            cur.execute("insert into pool_hourly (pool, hour, dex, mint_a, mint_b, fee_yield, var_sum, secs) "
                        "values ('RP', now() - interval '9 days', 'x', 'a', 'b', 9, 0.0001, 3600), "
                        "('RQ', date_trunc('hour', now()), 'x', 'a', 'b', 0.001, 0, 3600)")
        r, h = db.pool_r('RP', 7)
        self.assertAlmostEqual(r, 0.0003 / (0.0016 / 8)); self.assertAlmostEqual(h, 100.0)   # the 9-day row is out
        self.assertEqual(db.pool_r('RQ', 7), (None, 1.0))                                     # no variance: no R
        self.assertIsNone(db.pool_r('NONE', 7))
        with db.cursor(commit=True) as cur:
            cur.execute("delete from pool_hourly where pool in ('RP', 'RQ')")

    def test_repoint_moves_mints_and_payout_token(self):
        _fixtures.ensure_profile()
        name = config.PROFILE
        before = db.load_config(name)
        try:
            db.repoint(name, 'raydium-clmm', AQ, 'SOL/USD1', 'SOL', 'USD1', mints=[SOL, USD1], payout_mint=USD1)
            c = db.load_config(name)
            self.assertEqual((c['pool'], list(c['mints']), c['payout_mint']), (AQ, [SOL, USD1], USD1))
            db.repoint(name, 'raydium-clmm', P8, 'SOL/USDC', 'SOL', 'USDC')            # None: unchanged
            c = db.load_config(name)
            self.assertEqual((c['pool'], list(c['mints']), c['payout_mint']), (P8, [SOL, USD1], USD1))
        finally:
            with db.cursor(commit=True) as cur:
                cur.execute('update config set dex = %s, pool = %s, pair_label = %s, token_a = %s, token_b = %s, '
                            'mints = %s, payout_mint = %s where name = %s',
                            (before['dex'], before['pool'], before['pair_label'], before['token_a'], before['token_b'],
                             before['mints'], before['payout_mint'], name))

    def test_board_repoint_passes_the_pair_change(self):
        got = {}
        with mock.patch.object(B.db, 'repoint', lambda *a, **k: got.update(args=a, **k)), \
                mock.patch.object(B.guards, 'migration_target', lambda *a, **k: True), \
                mock.patch.object(B.config, 'reload', lambda: None), \
                mock.patch.object(config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(config, 'DEX', 'raydium-clmm'), \
                mock.patch.object(config, 'POOL', AQ):
            B.repoint(row(AQ, USD1))
        self.assertEqual((got['mints'], got['payout_mint']), ([SOL, USD1], USD1))


if __name__ == '__main__':
    unittest.main()
