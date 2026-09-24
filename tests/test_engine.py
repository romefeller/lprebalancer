"""The band arithmetic, on paths whose answer is known in advance."""
import math
import unittest
from unittest import mock

import numpy as np

import _fixtures  # noqa: F401
import engine


class Clmm(unittest.TestCase):
    def test_liquidity_and_amounts_round_trip(self):
        for p, pa, pb in ((100, 90, 110), (100, 50, 400), (90, 90, 110), (110, 90, 110)):
            L = engine.liquidity_for(1000.0, p, pa, pb)
            x, y = engine.amounts(L, p, pa, pb)
            self.assertAlmostEqual(x * p + y, 1000.0, places=6, msg=f'p={p}')

    def test_amounts_are_one_sided_outside_the_band(self):
        L = engine.liquidity_for(1000.0, 100, 90, 110)
        x, y = engine.amounts(L, 80, 90, 110)      # below: all base token
        self.assertGreater(x, 0); self.assertEqual(y, 0.0)
        x, y = engine.amounts(L, 120, 90, 110)     # above: all quote token
        self.assertEqual(x, 0.0); self.assertGreater(y, 0)

    def test_value_at_the_edge_is_below_hold(self):
        """A position that rides to its own edge has lost to holding 50/50.
        This is impermanent loss, and it is what the rebalance realises."""
        for k in (1.03, 1.05, 1.12, 1.40):
            L = engine.liquidity_for(1000.0, 100, 100 / k, 100 * k)
            x, y = engine.amounts(L, 100 * k, 100 / k, 100 * k)
            value = x * 100 * k + y
            hold = 500 * k + 500
            self.assertLess(value, hold, msg=f'k={k}')
            # and the loss is small for a narrow band, larger for a wide one
        loss = lambda k: 1 - (engine.amounts(engine.liquidity_for(1, 100, 100 / k, 100 * k),
                                              100 * k, 100 / k, 100 * k)[1]) / (0.5 * k + 0.5)
        self.assertLess(loss(1.03), loss(1.40))

    def test_band_concentration(self):
        self.assertAlmostEqual(engine.band_concentration(1.03), 68.2, delta=0.3)
        self.assertAlmostEqual(engine.band_concentration(1.12), 18.2, delta=0.2)
        self.assertGreater(engine.band_concentration(1.05), engine.band_concentration(1.12))

    def test_pool_concentration_is_dimensionless(self):
        # Same pool, quote priced in dollars vs. in a token worth $2: same answer
        # only if TVL is converted to quote units first.
        c1 = engine.pool_concentration(1e6, tvl_usd=1e8, price_native=100, quote_price_usd=1.0)
        c2 = engine.pool_concentration(1e6 * math.sqrt(2) / 2, tvl_usd=1e8, price_native=50,
                                       quote_price_usd=2.0)
        self.assertAlmostEqual(c1, c2, places=9)
        self.assertIsNone(engine.pool_concentration(1e6, 0, 100, 1))
        self.assertIsNone(engine.pool_concentration(1e6, 1e8, 100, 0))


class Simulate(unittest.TestCase):
    def ctx(self):
        return {'tvl_usd': 1e7, 'c_pool': 20.0}

    def hours(self, n):
        return np.arange(n) * 3600

    def test_flat_price_earns_fees_and_never_rebalances(self):
        n = 24 * 10 + 1
        px = np.full(n, 100.0)
        vol = np.full(n, 1e6)
        r = engine.simulate(1.05, self.hours(n), px, vol, self.ctx(), 0.0004, 1000.0)
        self.assertEqual(r['rebalances'], 0)
        self.assertEqual(r['in_range_pct'], 100.0)
        share = (1000.0 / 1e7) * (engine.band_concentration(1.05) / 20.0)
        self.assertAlmostEqual(r['fees'], vol[1:].sum() * 0.0004 * share, places=6)
        self.assertAlmostEqual(r['position_pnl'], 0.0, places=6)
        self.assertAlmostEqual(r['cost'], 0.0)
        self.assertAlmostEqual(r['days'], 10.0)

    def test_narrower_band_earns_more_fees_when_price_is_flat(self):
        n = 241
        px = np.full(n, 100.0); vol = np.full(n, 1e6)
        f = lambda k: engine.simulate(k, self.hours(n), px, vol, self.ctx(), 0.0004, 1000.0)['fees']
        self.assertGreater(f(1.03), f(1.05))
        self.assertGreater(f(1.05), f(1.12))

    def test_one_step_out_rebalances_once_and_pays_the_swap(self):
        n = 49
        px = np.concatenate([np.full(24, 100.0), np.full(25, 110.0)])
        vol = np.zeros(n)
        r = engine.simulate(1.05, self.hours(n), px, vol, self.ctx(), 0.0004, 1000.0, 0.001)
        self.assertEqual(r['rebalances'], 1)
        self.assertGreater(r['cost'], 0)
        # the swap cost parameter is honoured
        r2 = engine.simulate(1.05, self.hours(n), px, vol, self.ctx(), 0.0004, 1000.0, 0.01)
        self.assertAlmostEqual(r2['cost'], r['cost'] * 10, delta=r['cost'] * 0.05)
        # and a band wide enough never rebalanced
        r3 = engine.simulate(1.12, self.hours(n), px, vol, self.ctx(), 0.0004, 1000.0)
        self.assertEqual(r3['rebalances'], 0)

    def test_trend_loses_to_hold_at_every_band(self):
        """The 41.6-day finding, reproduced synthetically: a steady rally with
        thin volume, and every band's vs_hold is negative."""
        n = 24 * 30 + 1
        px = 100.0 * np.exp(np.linspace(0, 0.3, n))
        vol = np.full(n, 1e5)
        for k in (1.03, 1.05, 1.12, 1.40):
            r = engine.simulate(k, self.hours(n), px, vol, self.ctx(), 0.0004, 1000.0)
            self.assertLess(r['vs_hold'], 0, msg=f'k={k}')

    def test_best_band_prefers_narrow_when_calm_and_wide_when_wild(self):
        n = 24 * 20 + 1
        rng = np.random.default_rng(7)
        vol = np.full(n, 2e6)
        calm = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
        wild = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        bands = (1.03, 1.05, 1.08, 1.12, 1.18, 1.25, 1.40)
        bc, _ = engine.best_band(self.hours(n), calm, vol, self.ctx(), 0.0004, 1000.0, bands)
        bw, _ = engine.best_band(self.hours(n), wild, vol, self.ctx(), 0.0004, 1000.0, bands)
        self.assertLess(bc['band'], bw['band'])


class Survival(unittest.TestCase):
    def test_flat_path_never_exits_and_is_censored(self):
        px = np.full(241, 100.0)
        times = engine.exit_times(px, 1.05, step_hours=24)
        self.assertTrue(all(not e for _, e in times))
        s = engine.survival(px, 1.05)
        self.assertIsNone(s['median_exit_hours'])
        self.assertEqual(s['exited'], 0)
        self.assertEqual(s['p_survive_168h'], 1.0)

    def test_step_path_exits_at_the_known_hour(self):
        px = np.concatenate([np.full(50, 100.0), np.full(50, 110.0)])
        times = engine.exit_times(px, 1.05, step_hours=10)   # origins 0,10,...,90
        self.assertEqual(times[0], (50, True))    # origin 0 leaves at the 50th hour
        self.assertEqual(times[4], (10, True))    # origin 40 leaves 10 hours later
        # origins after the step never see another exit: censored, not dropped
        self.assertEqual(times[5], (49, False))   # origin 50, 49 hours remained
        self.assertEqual(len(times), 10)

    def test_kaplan_meier_without_censoring_is_the_empirical_curve(self):
        times = [(1, True), (2, True), (2, True), (5, True)]
        curve = dict(engine.kaplan_meier(times))
        self.assertAlmostEqual(curve[1.0], 0.75)
        self.assertAlmostEqual(curve[2.0], 0.25)
        self.assertAlmostEqual(curve[5.0], 0.0)

    def test_kaplan_meier_respects_censoring(self):
        # one exit at 1 among four; two censored at 3; one exit at 4.
        # S(1)=3/4; at t=4 only one remains at risk and it dies: S(4)=0.
        curve = dict(engine.kaplan_meier([(1, True), (3, False), (3, False), (4, True)]))
        self.assertAlmostEqual(curve[1.0], 0.75)
        self.assertAlmostEqual(curve[4.0], 0.0)
        # censor everyone but the first: S stays at 3/4
        curve = dict(engine.kaplan_meier([(1, True), (3, False), (3, False), (4, False)]))
        self.assertEqual(list(curve.values()), [0.75])

    def test_wider_bands_survive_longer(self):
        rng = np.random.default_rng(3)
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 1000)))
        s = [engine.survival(px, k)['p_survive_72h'] for k in (1.03, 1.08, 1.25)]
        self.assertLess(s[0], s[1]); self.assertLess(s[1], s[2])


class Rolling(unittest.TestCase):
    def ctx(self):
        return {'tvl_usd': 1e7, 'c_pool': 20.0}

    def test_flat_path_rolling_equals_the_single_path(self):
        n = 24 * 30 + 1
        ts = np.arange(n) * 3600; px = np.full(n, 100.0); vol = np.full(n, 1e6)
        path = engine.simulate(1.05, ts, px, vol, self.ctx(), 0.0004, 1000.0)
        roll = engine.rolling(1.05, ts, px, vol, self.ctx(), 0.0004, 1000.0, 0.001, 240, 24)
        self.assertEqual(roll['windows'], (n - 241 + 23) // 24)
        self.assertAlmostEqual(roll['median_net_day'], path['net_day_pct'], places=6)
        self.assertEqual(roll['share_positive'], 1.0)
        self.assertEqual(roll['rebal_per_day'], 0.0)

    def test_too_short_a_window_returns_none(self):
        n = 100
        ts = np.arange(n) * 3600; px = np.full(n, 100.0); vol = np.ones(n)
        self.assertIsNone(engine.rolling(1.05, ts, px, vol, self.ctx(), 0.0004, 1000.0, 0.001, 240))

    def test_edge_loss_grows_with_width_and_is_small(self):
        e = [engine.edge_loss(k) for k in (1.03, 1.05, 1.12, 1.40)]
        self.assertTrue(all(0 < x < 0.1 for x in e))
        self.assertEqual(e, sorted(e))
        self.assertAlmostEqual(engine.edge_loss(1.05), 0.0123, places=3)

    def test_choose_applies_the_churn_gate_then_yield(self):
        rows = [{'band': 1.03, 'net_day_pct': 0.9, 'rebal_per_day': 1.5},
                {'band': 1.08, 'net_day_pct': 0.4, 'rebal_per_day': 0.2},
                {'band': 1.18, 'net_day_pct': 0.3, 'rebal_per_day': 0.03}]
        self.assertEqual(engine.choose(rows, 0.5)['band'], 1.08)
        # every band churns: yield alone decides
        self.assertEqual(engine.choose(rows, 0.01)['band'], 1.03)

    def test_ladder_end_to_end_on_synthetic_pool(self):
        n = 24 * 41 + 1
        rng = np.random.default_rng(11)
        ts = np.arange(n) * 3600
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.008, n)))
        vol = np.full(n, 3e6)
        pool = {'tokenA': {'decimals': 9, 'symbol': 'SOL'},
                'tokenB': {'decimals': 6, 'symbol': 'USDC'},
                'liquidity': str(int(20 * (2e7 / (2 * px[-1] ** 0.5)) * (10 ** 15) ** 0.5)),
                'tvlUsdc': '20000000', 'price': str(px[-1]), 'feeRate': 400}
        out = engine.ladder(pool, (ts, px, vol), (1.03, 1.08, 1.18), 190.0)
        self.assertIsNotNone(out)
        rows, meta = out
        self.assertAlmostEqual(meta['c_pool'], 20.0, places=6)
        self.assertEqual([r['band'] for r in rows], [1.03, 1.08, 1.18])
        for r in rows:
            self.assertIn('median_net_day', r['roll'])
            self.assertIn('p_survive_24h', r['survival'])
            self.assertEqual(r['net_day_pct'], r['roll']['median_net_day'])
        # a pool whose candles disagree with its price is refused
        bad = dict(pool, price=str(px[-1] * 3))
        self.assertIsNone(engine.ladder(bad, (ts, px, vol), (1.05,), 190.0))




class Board(unittest.TestCase):
    def rec(self, **kw):
        base = {'dex': 'orca', 'kind': 'clmm', 'address': 'P1', 'pair': 'SOL/USDC',
                'token_a': {'address': 'a', 'symbol': 'SOL', 'name': 'Solana', 'decimals': 9},
                'token_b': {'address': 'b', 'symbol': 'USDC', 'name': 'USD Coin', 'decimals': 6},
                'price': 100.0, 'fee': 0.0004, 'fee_source': 'nominal', 'tvl_usd': 2e7,
                'volume_24h_usd': 5e7, 'fees_24h_usd': 2e4, 'adaptive_fee': False,
                'liquidity': 20 * (2e7 / (2 * 100 ** 0.5))}
        base.update(kw)
        return base

    def test_dlmm_concentration_matches_clmm_for_the_same_pool_shape(self):
        # A full-range position of TVL V holds V*s/4 per bin of log-width s.
        # A pool whose bins each hold 20x that is 20x concentrated.
        tvl, step = 1e7, 4
        per_bin_full = tvl * (step / 1e4) / 4
        c = engine.concentration({'kind': 'dlmm', 'tvl_usd': tvl, 'price': 100.0,
                                  'bin_step': step, 'active_bin_usd': 20 * per_bin_full}, 1.0)
        self.assertAlmostEqual(c, 20.0)
        c2 = engine.concentration({'kind': 'clmm', 'tvl_usd': tvl, 'price': 100.0,
                                   'liquidity': 20 * tvl / (2 * 10)}, 1.0)
        self.assertAlmostEqual(c2, 20.0)
        self.assertIsNone(engine.concentration({'kind': 'dlmm', 'tvl_usd': tvl, 'price': 100.0}, 1.0))

    def test_screen_token_rules(self):
        ok, why = engine.screen_token('WIF', {'name': 'dogwifhat', 'verified': True, 'tags': ['verified']})
        self.assertTrue(ok)
        ok, why = engine.screen_token('xSOL', {'name': 'Hylo 3x Leveraged SOL', 'verified': True})
        self.assertFalse(ok); self.assertIn('leveraged', why)
        ok, _ = engine.screen_token('NEW', {'name': 'New Coin', 'verified': False})
        self.assertFalse(ok)
        ok, _ = engine.screen_token('NEW', None)
        self.assertFalse(ok)
        ok, _ = engine.screen_token('X', {'name': 'X', 'verified': True, 'mint_authority_disabled': False})
        self.assertFalse(ok)

    def test_screening_verdict_is_the_worst_side(self):
        rec = {'token_a': {'symbol': 'SOL'}, 'token_b': {'symbol': 'WIF'}}
        ok, why = engine.screening_verdict(rec, {'b': {'name': 'dogwifhat', 'verified': True}})
        self.assertTrue(ok)
        ok, why = engine.screening_verdict(rec, {'b': None})
        self.assertFalse(ok)
        ok, why = engine.screening_verdict({'token_a': {'symbol': 'SOL'}, 'token_b': {'symbol': 'USDC'}}, {})
        self.assertTrue(ok); self.assertIn('majors', why)

    def test_score_board_scores_gates_and_screens(self):
        n = 24 * 41 + 1
        rng = np.random.default_rng(5)
        ts = np.arange(n) * 3600
        px = 100 * np.exp(np.cumsum(rng.normal(0, 0.008, n)))
        vol = np.full(n, 3e6)
        recs = [
            self.rec(address='P1', price=float(px[-1])),
            self.rec(address='P2', dex='raydium-clmm', tvl_usd=1e5),                 # too small
            self.rec(address='P3', dex='byreal', liquidity=None),                    # unreadable
            self.rec(address='P4', dex='meteora-dlmm', kind='dlmm', pair='SOL/WIF',
                     token_b={'address': 'w', 'symbol': 'WIF', 'name': 'dogwifhat', 'decimals': 6},
                     bin_step=4, active_bin_usd=20 * 2e7 * 4e-4 / 4, price=float(px[-1])),
            self.rec(address='P5', adaptive_fee=True),
        ]
        import dexes
        with mock.patch.object(engine, 'candles', lambda a: (ts, px, vol)), \
                mock.patch.object(dexes, 'jupiter_prices', lambda m: {'w': 1.0}), \
                mock.patch.object(dexes, 'jupiter_token',
                                  lambda m: {'name': 'dogwifhat', 'verified': True, 'tags': []}):
            board = engine.score_board(recs, 190.0, (1.05, 1.12), min_tvl=2.5e5,
                                       blocked=lambda r: 'adaptive' if r.get('adaptive_fee') else None)
        by = {r['address']: r for r in board}
        self.assertIsNotNone(by['P1']['net_day_pct'])
        self.assertEqual(by['P1']['screen_reason'], 'both tokens are majors')
        self.assertIn('below', by['P2']['skipped'])
        self.assertIn('unreadable', by['P3']['skipped'])
        self.assertTrue(by['P4']['screen_ok'])
        self.assertIn('WIF', by['P4']['screen_reason'])
        self.assertEqual(by['P5']['skipped'], 'adaptive')
        scored = [r for r in board if r.get('net_day_pct') is not None]
        self.assertEqual([r['address'] for r in scored], sorted((r['address'] for r in scored),
                         key=lambda a: -by[a]['net_day_pct']))
        self.assertIn('all_runs', by['P1'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
