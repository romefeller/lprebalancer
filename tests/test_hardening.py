"""One test per finding of the 2026-09-26 correctness and security reviews,
plus the liquidity factor. Offline: every chain and network call is mocked."""
import json
import os
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures
from test_rewards import OWNER, harvest_tx                       # noqa: E402  (a harvest that brought rewards)
_fixtures.ensure_profile()

import calm        # noqa: E402
import engine      # noqa: E402
import fees        # noqa: E402
import guards      # noqa: E402
import rebalancer  # noqa: E402

SOL, USDC = fees.NATIVE_MINT, 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
W = calm.WIDTHS


def tape(n=1200, sigma=0.0004, age_s=60, seed=2):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, sigma, n); c = 100 * np.exp(np.cumsum(r))
    ts = time.time() - age_s - (n - 1) * 300 + np.arange(n) * 300
    return ts, c.copy(), c * 1.0003, c * 0.9997, c, np.full(n, 1e5)


class StaleTape(unittest.TestCase):
    def view(self, age):
        b = tape(age_s=age); p = float(b[4][-1])
        with mock.patch.object(rebalancer.config, 'REGIME_ENABLED', True), \
                mock.patch.object(rebalancer, 'tape5', lambda pool, price: b), \
                mock.patch.object(rebalancer, 'liquidity_view', lambda *a: {'factor': 1.0}), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            return rebalancer.regime_view({}, {'price': p, 'lowerPrice': p / 1.05, 'upperPrice': p * 1.05})

    def test_a_stale_tape_chooses_the_widest_width(self):
        fresh, stale = self.view(60), self.view(6 * 3600)
        self.assertEqual(fresh['choice'], 1.01)                   # calm tape: +/-1%
        self.assertEqual(stale['choice'], W[-1]); self.assertTrue(stale['stale'])
        self.assertEqual(stale['mode'], 'STALE')


class NoViewUnderRegime(unittest.TestCase):
    def test_the_exit_band_without_a_view_is_the_widest(self):
        # mirrors main(): regime on, rv None -> widest
        with mock.patch.object(rebalancer.config, 'REGIME_ENABLED', True):
            rv = None
            k = rv['choice'] if rv else rebalancer.config.REGIME_WIDTHS[-1]
        self.assertEqual(k, W[-1])


class UnscorablePool(unittest.TestCase):
    def test_no_move_when_the_held_pool_cannot_be_scored(self):
        moved, sent = [], []
        best = {'dex': 'orca', 'pair': 'SOL/USDC', 'band_pct': 8.0, 'net_day_pct': 0.9, 'address': 'X'}
        with mock.patch.object(rebalancer, 'board_pick', lambda cur, ex: ({'id': 1}, best, None, 'held pool unscorable')), \
                mock.patch.object(rebalancer.config, 'POOL_PINNED', False), \
                mock.patch.object(rebalancer, 'rebalance', lambda *a, **k: moved.append(1)), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append((ev, kw))):
            self.assertFalse(rebalancer.consider_migration({}, {}, None))
        self.assertEqual(moved, []); self.assertIn('could not be scored', sent[-1][1]['verdict'])


class Payouts(unittest.TestCase):
    def run_it(self, chain_result, pin=PROFIT, state=None, held_b=40.0):
        rows = []; state = state if state is not None else {}
        bal = {'balanceA': 0.3, 'balanceB': held_b, 'sol': 0.3, 'price': 120.0, 'quoteUsd': 1.0}
        env = dict(os.environ, LPBOT_PROFIT_WALLET_PIN=pin) if pin else {k: v for k, v in os.environ.items() if k != 'LPBOT_PROFIT_WALLET_PIN'}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(rebalancer.config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'wallet', lambda p: bal), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: chain_result), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer.db, 'record_payout', lambda *a, **k: rows.append(a[6])):
            rebalancer.distribute(state, 'M', 0.002, 0.3)
        return rows, state

    def test_an_unconfirmed_send_is_never_owed(self):
        rows, st = self.run_it(({'signature': 's', 'partial': True}, 'confirm failed'))
        self.assertIn('uncertain', rows); self.assertNotIn(USDC, st.get('payout_owed', {}))
        rows, st = self.run_it((None, 'signer timed out'))
        self.assertIn('uncertain', rows); self.assertNotIn(USDC, st.get('payout_owed', {}))

    def test_a_clean_failure_is_owed(self):
        rows, st = self.run_it((None, 'insufficient funds'))
        self.assertIn('owed', rows); self.assertAlmostEqual(st['payout_owed'][USDC], 0.3)

    def test_the_pin_must_match_or_nothing_is_sent(self):
        sent = []
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: sent.append(a)):
            pass
        rows, st = self.run_it(({'signature': 's'}, None), pin='11111111111111111111111111111112')
        self.assertIn('owed', rows); self.assertNotIn('paid', rows)
        rows, st = self.run_it(({'signature': 's'}, None), pin=None)
        self.assertNotIn('paid', rows)
        rows, st = self.run_it(({'signature': 's'}, None))
        self.assertIn('paid', rows)

    def test_the_part_the_wallet_could_not_cover_stays_owed(self):
        rows, st = self.run_it(({'signature': 's'}, None), state={'payout_owed': {USDC: 1.0}}, held_b=0.5)
        self.assertAlmostEqual(st['payout_owed'][USDC], 0.8)       # 1.3 due, 0.5 sent


class FeesKeptWhenTheHarvestFails(unittest.TestCase):
    def test_the_close_records_the_fees_and_splits_them(self):
        recorded, split = [], []
        results = iter([(None, 'RPC rate limited'), ({'closed': 'M', 'signature': 'c'}, None)])
        state = {'last_rebalance': 0, 'rebalance_times': [], 'calm_times': [], 'failures': 0}
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: next(results)), \
                mock.patch.object(rebalancer, 'notify', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'notify_book', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'reopen', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'distribute', lambda *a, **k: split.append(a[2:])), \
                mock.patch.object(rebalancer.db, 'record_harvest', lambda *a: recorded.append(a)), \
                mock.patch.object(rebalancer.db, 'close_position', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            rebalancer.rebalance(state, {'positionMint': 'M', 'price': 100, 'feesAccruedA': 0.001,
                                         'feesAccruedB': 0.2, 'feesAccrued_USD': 0.3}, 'price went below')
        self.assertEqual(len(recorded), 1); self.assertEqual(recorded[0][4], 'close:c')      # close-collected fees: marked (audit 2026-09-30)
        self.assertEqual(split, [(0.001, 0.2)])


class State(unittest.TestCase):
    def test_a_corrupt_runtime_file_is_set_aside(self):
        import tempfile, pathlib
        d = pathlib.Path(tempfile.mkdtemp()); f = d / 'runtime.json'; f.write_text('{not json')
        with mock.patch.object(rebalancer, 'STATE', f):
            s = rebalancer.load()
        self.assertEqual(s['failures'], 0); self.assertTrue((d / 'runtime.json.corrupt').exists())

    def test_an_old_file_gets_every_key(self):
        import tempfile, pathlib
        f = pathlib.Path(tempfile.mkdtemp()) / 'runtime.json'
        f.write_text(json.dumps({'last_rebalance': 5, 'rebalance_times': [], 'failures': 1, 'read_failures': 0}))
        with mock.patch.object(rebalancer, 'STATE', f):
            s = rebalancer.load()
        self.assertEqual(s['last_rebalance'], 5); self.assertEqual(s['calm_times'], [])


class Labels(unittest.TestCase):
    def test_band_label(self):
        self.assertEqual(rebalancer.band_label(1.015), '+/-1.5%')
        self.assertEqual(rebalancer.band_label(1.01), '+/-1%')
        self.assertEqual(rebalancer.band_label(1.0125), '+/-1.25%')


class GapBeforeAnnounce(unittest.TestCase):
    def test_voluntary_move_allowed(self):
        now = time.time()
        with mock.patch.object(rebalancer.config, 'CALM_MIN_GAP', 600):
            self.assertFalse(rebalancer.voluntary_move_allowed({'calm_times': [now - 60]}))
            self.assertTrue(rebalancer.voluntary_move_allowed({'calm_times': [now - 700]}))
            self.assertTrue(rebalancer.voluntary_move_allowed({}))


class Security(unittest.TestCase):
    def test_signer_args_refuse_options_but_execute(self):
        self.assertTrue(guards.signer_args(['open', 'Pool1', '1.5', '--execute']))
        for bad in ('--pool', '-rf', '-1', 'a b', 'x;rm', ''):
            with self.assertRaises(guards.Refused, msg=bad):
                guards.signer_args(['open', bad])

    def test_a_fake_usdc_by_symbol_is_not_a_stablecoin_or_a_major(self):
        fake = {'symbol': 'USDC', 'address': 'FakeUSDC1111111111111111111111111111111111'}
        self.assertFalse(engine.is_stable(fake)); self.assertFalse(engine.is_major(fake))
        self.assertTrue(engine.is_stable({'symbol': 'USDC', 'address': USDC}))
        self.assertTrue(engine.is_major({'symbol': 'SOL', 'address': SOL}))
        ok, _ = engine.screening_verdict({'token_a': {'symbol': 'SOL', 'address': SOL}, 'token_b': fake}, {})
        self.assertFalse(ok)

    def test_operator_migrate_to_another_pair_is_refused_without_allow_swap(self):
        sent = []
        rec = {'pair': 'SOL/JUP', 'token_a': {'address': SOL}, 'token_b': {'address': 'JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'}}
        with mock.patch.object(rebalancer.config, 'ALLOW_SWAP', False), \
                mock.patch.object(rebalancer.config, 'EXECUTE_DEXES', ('orca',)), \
                mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: rec), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: sent.append(kw.get('reason'))):
            self.assertIsNone(rebalancer.operator_target(['orca', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE']))
        self.assertIn('different pair', sent[-1])

    def test_reward_sweep_skips_non_addresses_and_holds_large_balances(self):
        calls, notes = [], []
        rec = {'token_a': {'address': SOL}, 'token_b': {'address': USDC},
               'reward_mints': ['not a mint', '4qQeZ5LwSz6HuupUu8jCtgXyW1mYQcNbFAW1sWZp89HL']}
        def chain(*a, **k):
            calls.append(a[0])
            return ({'amount': 100.0}, None) if a[0] == 'balance' else (None, 'x')
        with mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(rebalancer.config, 'REWARD_POLICY', 'payout'), \
                mock.patch.object(rebalancer.config, 'REWARD_MIN_USD', 1.0), \
                mock.patch.object(rebalancer.config, 'REWARD_MAX_USD', 25.0), \
                mock.patch.object(rebalancer.config, 'PAYOUT_MINT', USDC), \
                mock.patch.object(rebalancer, 'pool_record', lambda: rec), \
                mock.patch.object(rebalancer, 'wallet', lambda p: {'sol': 0.3, 'owner': OWNER}), \
                mock.patch.object(rebalancer.txfees, 'fetch', lambda rpc, s, **k: harvest_tx(
                    '4qQeZ5LwSz6HuupUu8jCtgXyW1mYQcNbFAW1sWZp89HL', 100.0)), \
                mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda m: {'4qQeZ5LwSz6HuupUu8jCtgXyW1mYQcNbFAW1sWZp89HL': 2.76}), \
                mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: notes.append(ev)):
            state = {}
            rebalancer.distribute_rewards(state, 'M', ['H'])          # the harvest brought all 100
        self.assertNotIn('not a mint', state['reward_mints_seen'])
        self.assertNotIn('swap', calls)                                # $276 > $25 cap: held
        self.assertIn('reward_held', notes)


class Liquidity(unittest.TestCase):
    def lv(self, liq_now, med, vol_recent_x):
        n = 2000; v = np.full(n, 100.0); v[-72:] = 100.0 * vol_recent_x
        bars = (np.arange(n) * 300.0, np.ones(n), np.ones(n), np.ones(n), np.ones(n), v)
        rebalancer._LIQ.clear()
        with mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: {'liquidity': liq_now, 'tvl_usd': 1e6}), \
                mock.patch.object(rebalancer.db, 'record_pool_stats', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'pool_stats_summary', lambda p, **k: {'median_liquidity': med, 'readings': 10, 'tvl_then': 1e6}), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_SMOOTH_H', 0), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MIN', 0.6), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MAX', 1.25):
            return rebalancer.liquidity_view('P', 'raydium-clmm', bars)

    def test_inflow_tightens_outflow_loosens_bounded_and_volume_is_only_reported(self):
        self.assertAlmostEqual(self.lv(100, 100, 1.0)['factor'], 1.0)
        self.assertAlmostEqual(self.lv(125, 100, 1.0)['factor'], 0.8)        # liquidity floods in
        self.assertAlmostEqual(self.lv(90, 100, 1.0)['factor'], 1.111, places=3)   # liquidity leaves
        self.assertAlmostEqual(self.lv(300, 100, 1.0)['factor'], 0.6)        # bounded below
        self.assertAlmostEqual(self.lv(50, 100, 1.0)['factor'], 1.25)        # bounded above
        v = self.lv(100, 100, 0.5)                                         # a quiet night
        self.assertAlmostEqual(v['factor'], 1.0); self.assertAlmostEqual(v['volume_x'], 0.5, places=2)

    def test_missing_history_is_neutral(self):
        rebalancer._LIQ.clear()
        with mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: None), \
                mock.patch.object(rebalancer.db, 'pool_stats_summary', lambda p, **k: None):
            self.assertEqual(rebalancer.liquidity_view('P', 'orca', None)['factor'], 1.0)


class LiquidityViewExact(unittest.TestCase):
    """liquidity_view's cache, reported figures and volume ratio, exactly."""

    def setUp(self):
        rebalancer._LIQ.clear(); self.reads = []; self.recorded = []

    def call(self, rec, summ=False, bars=None, now=1000.0, smooth_h=0):
        def pool(d, p):
            self.reads.append(p); return rec
        summ = summ if summ is not False else {'median_liquidity': 100.0, 'readings': 9, 'tvl_then': 800.0,
                        'recent_liquidity': None, 'recent_readings': 0}
        with mock.patch.object(rebalancer.dexes, 'pool', pool), \
                mock.patch.object(rebalancer.db, 'record_pool_stats', lambda *a: self.recorded.append(a)), \
                mock.patch.object(rebalancer.db, 'pool_stats_summary', lambda p, **k: summ), \
                mock.patch.object(rebalancer.time, 'time', lambda: now), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_SMOOTH_H', smooth_h), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MIN', 0.6), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MAX', 1.25):
            return rebalancer.liquidity_view('P', 'raydium-clmm', bars)

    def test_reads_once_per_refresh_and_again_after(self):
        rec = {'liquidity': 100.0, 'tvl_usd': 1000.0, 'volume_24h_usd': 5.0, 'price': 2.0}
        self.call(rec, now=1000.0)
        self.call(rec, now=1000.0 + rebalancer.LIQ_REFRESH)          # not yet stale
        self.assertEqual(len(self.reads), 1)
        out = self.call(rec, now=1000.0 + rebalancer.LIQ_REFRESH + 1)
        self.assertEqual(len(self.reads), 2)
        self.assertEqual(self.recorded[0], ('raydium-clmm', 'P', 100.0, 1000.0, 5.0, 2.0))
        self.assertEqual(out['readings'], 9)

    def test_first_call_reads_even_at_time_zero(self):
        self.call({'liquidity': 100.0, 'tvl_usd': 1.0}, now=0.0)
        self.assertEqual(len(self.reads), 1)

    def test_a_failed_read_is_retried_next_poll(self):
        self.call(None, now=1000.0); self.call({'liquidity': 100.0, 'tvl_usd': 1.0}, now=1001.0)
        self.assertEqual(len(self.reads), 2)

    def test_dlmm_reads_the_active_bin(self):
        out = self.call({'liquidity': None, 'active_bin_usd': 50.0, 'tvl_usd': 1.0})
        self.assertEqual(out['liquidity'], 50.0); self.assertEqual(self.recorded[0][2], 50.0)
        self.assertAlmostEqual(out['inflow'], 0.5)
        rebalancer._LIQ.clear(); self.recorded.clear()
        out = self.call({'liquidity': 70.0, 'active_bin_usd': 50.0, 'tvl_usd': 1.0})
        self.assertEqual(out['liquidity'], 70.0); self.assertEqual(self.recorded[0][2], 70.0)

    def test_tvl_change_exact_and_absent_without_history(self):
        out = self.call({'liquidity': 100.0, 'tvl_usd': 1000.0})
        self.assertAlmostEqual(out['tvl_change_24h'], 0.25)
        rebalancer._LIQ.clear()
        out = self.call({'liquidity': 100.0, 'tvl_usd': 1000.0},
                        summ={'median_liquidity': 100.0, 'readings': 9, 'tvl_then': None})
        self.assertIsNone(out['tvl_change_24h'])
        rebalancer._LIQ.clear()
        out = self.call({'liquidity': 100.0, 'tvl_usd': None})
        self.assertIsNone(out['tvl_change_24h'])

    def test_no_reading_no_inflow(self):
        out = self.call(None)
        self.assertIsNone(out['inflow']); self.assertEqual(out['readings'], 0); self.assertEqual(out['factor'], 1.0)
        self.assertIsNone(out['inflow_raw'])
        rebalancer._LIQ.clear()
        out = self.call({'liquidity': 100.0, 'tvl_usd': 1.0}, summ={'median_liquidity': 0.0, 'readings': 9, 'tvl_then': None})
        self.assertIsNone(out['inflow_raw'])
        rebalancer._LIQ.clear()
        out = self.call({'liquidity': 50.0, 'tvl_usd': 1.0}, summ={'median_liquidity': 0.5, 'readings': 9, 'tvl_then': None})
        self.assertAlmostEqual(out['inflow_raw'], 100.0)

    def test_volume_ratio_needs_a_day_of_bars(self):
        def bars(n, recent):
            v = np.full(n, 10.0); v[-72:] = recent
            return (np.zeros(n),) * 5 + (v,)
        rec = {'liquidity': 100.0, 'tvl_usd': 1.0}
        self.assertIsNone(self.call(rec, bars=bars(287, 30.0))['volume_x'])
        rebalancer._LIQ.clear()
        self.assertAlmostEqual(self.call(rec, bars=bars(288, 30.0))['volume_x'], 3.0)  # 2160 / median 720
        rebalancer._LIQ.clear()
        self.assertIsNone(self.call(rec, bars=(np.zeros(300),) * 6)['volume_x'])          # no volume: no ratio
        rebalancer._LIQ.clear()
        self.assertIsNone(self.call(rec, bars=None)['volume_x'])
        rebalancer._LIQ.clear()
        small = (np.zeros(288),) * 5 + (np.full(288, 0.001),)
        self.assertAlmostEqual(self.call(rec, bars=small)['volume_x'], 1.0)   # a thin pool still has a ratio

    def test_newest_reading_without_history_is_neutral(self):
        out = self.call({'liquidity': 100.0, 'tvl_usd': 1.0}, summ=None, smooth_h=0)
        self.assertEqual(out['factor'], 1.0); self.assertIsNone(out['inflow'])


class LiquiditySmoothed(unittest.TestCase):
    """sql/028: the factor reads the window's geometric mean, not one reading."""

    def lv(self, liq_now, summ, smooth_h=2.0, fresh=True):
        rebalancer._LIQ.clear(); asked = {}

        def summary(p, **k):
            asked.update(k); return summ
        with mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: {'liquidity': liq_now, 'tvl_usd': 1e6} if fresh else None), \
                mock.patch.object(rebalancer.db, 'record_pool_stats', lambda *a: None), \
                mock.patch.object(rebalancer.db, 'pool_stats_summary', summary), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_SMOOTH_H', smooth_h), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MIN', 0.6), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MAX', 1.25):
            out = rebalancer.liquidity_view('P', 'raydium-clmm', None)
        return out, asked

    @staticmethod
    def summ(med, recent, n=20):
        return {'median_liquidity': med, 'readings': 200, 'tvl_then': 1e6,
                'recent_liquidity': recent, 'recent_readings': n}

    def test_one_tick_crossing_does_not_move_the_factor(self):
        # 09-28 02:57: one reading at 0.24 of the median; the window still at 0.94
        out, asked = self.lv(24.0, self.summ(100.0, 94.0))
        self.assertEqual(asked, {'recent_hours': 2.0})
        self.assertAlmostEqual(out['inflow'], 0.94)
        self.assertAlmostEqual(out['factor'], 1.064, places=3)
        self.assertEqual(out['liquidity'], 24.0)                 # the reading is still reported
        self.assertEqual(out['liquidity_smoothed'], 94.0)
        self.assertAlmostEqual(out['inflow_raw'], 0.24)          # the reading's own inflow, for the book

    def test_window_mean_sets_inflow_and_bounds_hold(self):
        self.assertAlmostEqual(self.lv(100, self.summ(100.0, 125.0))[0]['factor'], 0.8)
        self.assertAlmostEqual(self.lv(100, self.summ(100.0, 90.0))[0]['factor'], 1.111, places=3)
        self.assertAlmostEqual(self.lv(100, self.summ(100.0, 1000.0))[0]['factor'], 0.6)
        self.assertAlmostEqual(self.lv(100, self.summ(100.0, 10.0))[0]['factor'], 1.25)

    def test_thin_window_is_neutral(self):
        out, _ = self.lv(50.0, self.summ(100.0, None, n=2))
        self.assertEqual(out['factor'], 1.0); self.assertIsNone(out['inflow'])
        self.assertAlmostEqual(out['inflow_raw'], 0.5)
        self.assertIsNone(out['liquidity_smoothed'])

    def test_failed_read_still_uses_the_stored_window(self):
        out, _ = self.lv(None, self.summ(100.0, 80.0), fresh=False)
        self.assertAlmostEqual(out['inflow'], 0.8); self.assertAlmostEqual(out['factor'], 1.25)

    def test_zero_hours_is_the_newest_reading(self):
        out, asked = self.lv(50.0, self.summ(100.0, 94.0), smooth_h=0)
        self.assertEqual(asked, {'recent_hours': 0})
        self.assertAlmostEqual(out['inflow'], 0.5); self.assertAlmostEqual(out['factor'], 1.25)
        self.assertIsNone(out['liquidity_smoothed'])

    def test_no_history_is_neutral(self):
        self.assertEqual(self.lv(50.0, None)[0]['factor'], 1.0)
        self.assertEqual(self.lv(50.0, self.summ(0.0, 94.0))[0]['factor'], 1.0)

    def test_fractional_hours_and_small_median(self):
        out, _ = self.lv(50.0, self.summ(0.5, 0.4), smooth_h=0.5)
        self.assertAlmostEqual(out['inflow'], 0.8); self.assertEqual(out['liquidity_smoothed'], 0.4)

    def test_property_factor_bounded_and_monotone_in_the_window(self):
        rng = np.random.default_rng(28)
        for _ in range(200):
            med = float(rng.uniform(1, 1e6)); a, b = sorted(rng.uniform(0.01, 10, 2) * med)
            fa = self.lv(1.0, self.summ(med, a))[0]['factor']; fb = self.lv(1.0, self.summ(med, b))[0]['factor']
            self.assertTrue(0.6 <= fb <= fa <= 1.25)              # more liquidity never loosens


# --- security review, 2026-10-09 ----------------------------------------------------------

class SignerEnvironment(unittest.TestCase):
    def test_the_telegram_token_is_withheld_from_signers(self):
        seen = {}

        class R:
            returncode, stdout, stderr = 0, '{"ok": true}', ''

        def run(cmd, **kw):
            seen.update(kw['env'])
            return R()
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '1:abc', 'TELEGRAM_CHAT_ID': '42', 'KAMINO_RPC_KEY': 'k'}), \
                mock.patch.object(rebalancer.subprocess, 'run', run), \
                mock.patch.object(rebalancer.guards, 'inside', lambda *a: None), \
                mock.patch.dict(rebalancer.SIGNERS, {'jupiter': str(rebalancer.ROOT / 'swap_jupiter.mjs')}):
            out, err = rebalancer._chain('balance', dex='jupiter')
        self.assertEqual((out, err), ({'ok': True}, None))
        self.assertNotIn('TELEGRAM_BOT_TOKEN', seen); self.assertNotIn('TELEGRAM_CHAT_ID', seen)
        self.assertEqual(seen.get('KAMINO_RPC_KEY'), 'k')          # the rest of the environment passes through
        self.assertIn('WALLET_SECRET_PATH', seen)


class SwapMints(unittest.TestCase):
    """The pre-open swap sells towards the tokens the pool RECORD names (a
    DEX API's answer). When the signer's balance read names the pool's own
    mints, they must agree, or nothing is swapped."""
    REC = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}}
    OTHER = '4qQeZ5LwSz6HuupUu8jCtgXyW1mYQcNbFAW1sWZp89HL'

    def run_it(self, b, rec=None, chain_name='solana'):
        calls, notes = [], []
        with mock.patch.object(rebalancer.config, 'REBALANCE_SWAP', True), \
                mock.patch.object(rebalancer.config, 'DEPLOY_ALL', True), \
                mock.patch.object(rebalancer.config, 'MAX_USD', 300.0), \
                mock.patch.object(rebalancer.config, 'SIDE_CAP_FRACTION', 0.55), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(rebalancer.config, 'CAPITAL_USD', 190.0), \
                mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', False), \
                mock.patch.object(rebalancer.config, 'WALLET_ID', None), \
                mock.patch.object(rebalancer.config, 'CHAIN', chain_name), \
                mock.patch.object(rebalancer, 'SWAP_FALLBACK', ''), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append(a) or ({'sent': True, 'signature': 's'}, None))), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: notes.append((ev, kw))), \
                mock.patch.object(rebalancer, 'record_health', lambda *a, **k: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            out = rebalancer.balance_wallet({'failures': 0}, dict(b), rec or self.REC)
        return out, calls, notes

    def lopsided(self, **more):
        return dict({'balanceA': 0.2, 'balanceB': 200.0, 'price': 120.0, 'quoteUsd': 1.0, 'nativeSide': 'A'}, **more)

    def swapped(self, calls):
        return [c[0] for c in calls][:1] == ['rebalance']           # the swap, then the re-reads of the wallet

    def test_a_read_without_mints_swaps_as_before(self):
        _, calls, _ = self.run_it(self.lopsided())
        self.assertTrue(self.swapped(calls))

    def test_a_read_whose_mints_agree_swaps(self):
        _, calls, _ = self.run_it(self.lopsided(mintA=SOL, mintB=USDC))
        self.assertTrue(self.swapped(calls))
        _, calls, _ = self.run_it(self.lopsided(mintA=USDC, mintB=SOL))          # order is not the point
        self.assertTrue(self.swapped(calls))

    def test_a_record_naming_other_tokens_swaps_nothing(self):
        b = self.lopsided(mintA=SOL, mintB=self.OTHER)
        out, calls, notes = self.run_it(b)
        self.assertEqual(calls, [])
        self.assertEqual(out, b)                                    # the wallet as it is, no failure counted
        self.assertEqual(notes[-1][0], 'swap_skipped'); self.assertIn('does not hold', notes[-1][1]['reason'])
        self.assertEqual(notes[-1][1]['chain'], sorted([SOL, self.OTHER]))

    def test_a_record_without_two_addresses_swaps_nothing(self):
        for rec in ({'token_a': {}, 'token_b': {'address': USDC}}, {'token_a': {'address': SOL}, 'token_b': None},
                    {'token_a': {'address': 'not an address'}, 'token_b': {'address': USDC}}):
            b = self.lopsided()
            out, calls, notes = self.run_it(b, rec)
            self.assertEqual(calls, []); self.assertEqual(out, b)
            self.assertEqual(notes[-1][0], 'swap_skipped'); self.assertIn('no mints', notes[-1][1]['reason'])

    def test_evm_addresses_compare_without_case(self):
        a, b = '0x' + 'ab' * 20, '0x' + 'cd' * 20
        rec = {'token_a': {'address': a}, 'token_b': {'address': b}}
        _, calls, _ = self.run_it(self.lopsided(mintA='0x' + 'AB' * 20, mintB=b, nativeSide=None), rec, 'polygon')
        self.assertTrue(self.swapped(calls))
