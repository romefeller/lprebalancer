"""One test per finding of the 2026-09-26 correctness and security reviews,
plus the liquidity factor. Offline: every chain and network call is mocked."""
import json
import os
import time
import unittest
from unittest import mock

import numpy as np

import _fixtures
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
        self.assertEqual(len(recorded), 1); self.assertEqual(recorded[0][4], 'c')
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
                mock.patch.object(rebalancer, 'wallet', lambda p: {'sol': 0.3}), \
                mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda m: {'4qQeZ5LwSz6HuupUu8jCtgXyW1mYQcNbFAW1sWZp89HL': 2.76}), \
                mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: notes.append(ev)):
            state = {}
            rebalancer.distribute_rewards(state, 'M')
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
                mock.patch.object(rebalancer.db, 'pool_stats_summary', lambda p: {'median_liquidity': med, 'readings': 10, 'tvl_then': 1e6}), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MIN', 0.6), \
                mock.patch.object(rebalancer.config, 'REGIME_LIQ_MAX', 1.25):
            return rebalancer.liquidity_view('P', 'raydium-clmm', bars)

    def test_inflow_tightens_volume_loosens_and_both_are_bounded(self):
        self.assertAlmostEqual(self.lv(100, 100, 1.0)['factor'], 1.0)
        self.assertAlmostEqual(self.lv(125, 100, 1.0)['factor'], 0.8)        # liquidity floods in
        self.assertAlmostEqual(self.lv(100, 100, 1.1)['factor'], 1.1)        # volume up
        self.assertAlmostEqual(self.lv(300, 100, 1.0)['factor'], 0.6)        # bounded below
        self.assertAlmostEqual(self.lv(50, 100, 2.0)['factor'], 1.25)        # bounded above

    def test_missing_history_is_neutral(self):
        rebalancer._LIQ.clear()
        with mock.patch.object(rebalancer.dexes, 'pool', lambda d, p: None), \
                mock.patch.object(rebalancer.db, 'pool_stats_summary', lambda p: None):
            self.assertEqual(rebalancer.liquidity_view('P', 'orca', None)['factor'], 1.0)
