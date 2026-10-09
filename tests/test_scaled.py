"""Token-2022 tokenized stocks (MU, DJT, MSFTx) in the loop and the audits.

The signers report every amount in UI units (raw / 10^decimals x the mint's
multiplier) and keep `price` pool-native, with `uiPrice` beside it. The loop
values UI amounts at uiPrice and keeps bands pool-native; GeckoTerminal's UI
prices become pool-native before they meet a band; the audits read UI
amounts from the RPC's uiAmountString and scale a harvest's raw outflow.
Also here: the per-profile signer opt-ins (signer_env), the per-venue open
rent, and the refusals of a paused mint, which no breaker counts."""
import json
import os
import pathlib
import types
import unittest
from unittest import mock

import numpy as np

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import audit
import config
import guards
import calm
import db
import engine
import txfees
import lp.capital
import lp.harvest
import lp.paths
import lp.signers
import lp.swaps
import lp.tape

M = 1.0059                                   # MSFTx's multiplier, 2026-10-01
MSFTX = 'XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
POOL = 'D6bRhQUcR9B7bPbbqgxpE17MjyUjBtr8hHQCcJoHrrv1'


def stock_bal(**over):
    # 2 UI MSFTx at $500 a UI share; the pool-native price is 500 / M
    b = {'pool': POOL, 'price': 500.0 / M, 'uiPrice': 500.0, 'quoteUsd': 1.0, 'balanceA': 2.0, 'balanceB': 1000.0,
         'nativeSide': None, 'sol': 0.2, 'multiplierA': M, 'multiplierB': 1.0, 'tokenA': 'MSFTx', 'tokenB': 'USDC'}
    b.update(over)
    return b


class Valuation(unittest.TestCase):
    def setUp(self):
        for k, v in (('DEPLOY_ALL', True), ('MAX_USD', 100000.0), ('SIDE_CAP_FRACTION', 0.55),
                     ('GAS_RESERVE_SOL', 0.05), ('DEX', 'raydium-clmm')):
            p = mock.patch.object(config, k, v); p.start(); self.addCleanup(p.stop)

    def test_ui_amounts_are_valued_at_the_ui_price(self):
        self.assertEqual(lp.capital.ui_price(stock_bal()), 500.0)
        self.assertEqual(lp.capital.ui_price({'price': 7.0}), 7.0)                  # a plain pool: no uiPrice
        self.assertAlmostEqual(lp.capital.deployable_usd(stock_bal()), 2.0 * 500.0 + 1000.0)

    def test_caps_are_ui_amounts(self):
        a, b = lp.capital.deposit_caps(stock_bal())
        C = lp.capital.capital(stock_bal())
        self.assertAlmostEqual(a, min(2.0, C * 0.55 / 500.0))                   # divided by uiPrice, not price
        self.assertAlmostEqual(b, min(1000.0, C * 0.55))

    def test_the_close_estimate_is_valued_at_the_ui_price(self):
        s = {'closeEstA': 1.0, 'closeEstB': 100.0, 'price': 500.0 / M, 'uiPrice': 500.0, 'quoteUsd': 1.0}
        self.assertAlmostEqual(lp.capital.position_usd(s), 600.0)
        self.assertAlmostEqual(lp.capital.position_usd(dict(s, quoteUsd=2.0, rentUsd=0.5)), 1200.5)
        self.assertEqual(lp.capital.position_usd(dict(s, positionUsd=7.0, rentUsd=0.25)), 7.25)  # the signer's mark wins
        self.assertEqual(lp.capital.position_usd(dict(s, positionUsd=7.0, rentUsd=None)), 7.0)
        for missing in ('closeEstA', 'closeEstB', 'quoteUsd'):
            self.assertIsNone(lp.capital.position_usd(dict(s, **{missing: None})), missing)

    def test_the_open_guard_values_caps_at_the_ui_price(self):
        kw = dict(pool=POOL, dex='raydium-clmm', price=500.0 / M, lower=480.0 / M, upper=520.0 / M, cap_b=500.0,
                  capital_usd=1000.0, max_usd=10000.0, quote_usd=1.0, execute_dexes=('raydium-clmm',),
                  signers={'raydium-clmm': 'x'})
        # 1.0 UI share is $500: with the native price it would read $497.07 and pass a cap it breaks
        with self.assertRaisesRegex(guards.Refused, 'exceed twice the capital'):
            guards.open_request(cap_a=1.0 + 1e-3, ui_price=500.0, **dict(kw, capital_usd=500.2))
        self.assertTrue(guards.open_request(cap_a=1.0 + 1e-3, **dict(kw, capital_usd=500.2)))
        with self.assertRaises(guards.Refused):
            guards.open_request(cap_a=1.0, ui_price=float('nan'), **kw)

    def test_a_harvest_is_booked_in_ui_units(self):
        status = {'positionMint': 'P', 'whirlpool': POOL, 'price': 500.0 / M, 'uiPrice': 500.0, 'quoteUsd': 1.0,
                  'multiplierA': M, 'multiplierB': 1.0, 'positionUsd': 1000.0}
        with mock.patch.object(lp.capital, 'pool_tokens', lambda: ((MSFTX, 'MSFTx'), (USDC, 'USDC'))), \
                mock.patch.object(txfees, 'harvested', lambda *a, **k: (0.01, 2.0)), \
                mock.patch.object(lp.books, 'notify', lambda *a, **k: None):
            a, b, usd = lp.harvest.measured_fees({'signature': 's'}, status, 0.01 * M, 2.0, 7.0)
        self.assertAlmostEqual(a, 0.01 * M); self.assertEqual(b, 2.0)
        self.assertAlmostEqual(usd, 0.01 * M * 500.0 + 2.0)


class Tape(unittest.TestCase):
    def tearDown(self):
        lp.capital.UI_SCALE.pop(POOL, None)
        lp.tape._TAPE5.pop(POOL, None)
        lp.tape._TAPE.pop(POOL, None)

    def test_a_signer_read_sets_the_scale(self):
        lp.capital.note_scale(stock_bal())
        self.assertAlmostEqual(lp.capital.UI_SCALE[POOL], 1 / M)
        for bad in ({'pool': POOL, 'price': 3.0}, {'pool': POOL, 'uiPrice': 3.0}, {'pool': POOL, 'price': 0, 'uiPrice': 3.0},
                    {'pool': POOL, 'price': 3.0, 'uiPrice': 0}, {'pool': POOL, 'price': -3.0, 'uiPrice': 3.0},
                    {'pool': POOL, 'price': 3.0, 'uiPrice': -3.0}, {'pool': POOL, 'price': 'x', 'uiPrice': 3.0},
                    {'price': 3.0, 'uiPrice': 1.0}, None):
            lp.capital.note_scale(bad)
            self.assertAlmostEqual(lp.capital.UI_SCALE[POOL], 1 / M, msg=str(bad))     # unchanged
        self.assertNotIn(None, lp.capital.UI_SCALE)
        lp.capital.note_scale({'whirlpool': POOL, 'pool': 'OTHER', 'price': 2.0, 'uiPrice': 1.0})
        self.assertEqual(lp.capital.UI_SCALE[POOL], 2.0)                           # a status names it whirlpool
        lp.capital.UI_SCALE.pop('OTHER', None)
        lp.capital.note_scale({'pool': POOL, 'price': 0.5, 'uiPrice': 1.0})
        self.assertEqual(lp.capital.UI_SCALE[POOL], 0.5)

    def test_native_bars_scale_prices_not_time_or_volume(self):
        bars = tuple(np.array([float(i), 10.0, 11.0, 9.0, 10.5, 7.0][i:i + 1]) for i in range(6))
        out = lp.tape.native_bars(bars, 0.5)
        self.assertEqual([float(c[0]) for c in out], [0.0, 5.0, 5.5, 4.5, 5.25, 7.0])
        self.assertIs(lp.tape.native_bars(bars, 1.0), bars)
        self.assertIsNone(lp.tape.native_bars(None, 0.5))

    def test_the_five_minute_tape_meets_the_band_in_pool_native_units(self):
        lp.capital.note_scale(stock_bal())
        native = 500.0 / M
        asked = []
        n = lp.tape.tape_bars()
        ts = np.arange(n, dtype=float) * 300 + 1.7e9
        ui = (ts, np.full(n, 500.0), np.full(n, 501.0), np.full(n, 499.0), np.full(n, 500.0), np.ones(n))

        def tape_5m(pool, live_price=None, before=None):
            asked.append(live_price)
            return ui
        with mock.patch.object(calm, 'tape_5m', tape_5m), \
                mock.patch.object(db, 'tape_load', lambda *a: None), \
                mock.patch.object(db, 'tape_store', lambda *a: None), \
                mock.patch.object(db, 'tape_prune_other_pools', lambda *a: None), \
                mock.patch.object(lp.tape, 'with_surrogate', lambda pool, b, price, pair=None: b):
            bars = lp.tape.tape5(POOL, native)
        self.assertAlmostEqual(asked[0], 500.0)                                 # Gecko's orientation check: UI
        self.assertAlmostEqual(float(bars[4][-1]), native)                      # the band's units: pool-native
        self.assertAlmostEqual(float(bars[2][-1]), 501.0 / M)

    def test_the_hourly_tape_too(self):
        lp.capital.note_scale(stock_bal())
        c = (np.arange(300.0), np.full(300, 500.0), np.ones(300))
        with mock.patch.object(engine, 'candles', lambda pool: c):
            out = lp.tape.tape(POOL)
        self.assertAlmostEqual(float(out[1][0]), 500.0 / M)


class MintRefusals(unittest.TestCase):
    def test_no_breaker_counts_a_paused_mint_or_a_hook(self):
        for e in ('refused: mint paused', 'refused: transfer hook'):
            self.assertFalse(lp.signers.counts_as_failure(e))
            self.assertTrue(lp.signers.mint_refusal(e))
        self.assertFalse(lp.signers.mint_refusal('refused: bad signer argument'))
        self.assertFalse(lp.signers.mint_refusal('program error 6069'))
        self.assertTrue(lp.signers.counts_as_failure('custom program error: 0x17b5'))     # 6069 still trips

    def test_only_a_sent_clean_write_resumes(self):
        sent = []
        lp.signers.MINT_HOLD['why'] = 'refused: mint paused'
        with mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(db, 'event', lambda *a: None):
            lp.signers.note_mint_refusal('rebalance', None, {'noop': True})                     # nothing sent
            lp.signers.note_mint_refusal('open', 'confirm failed', {'signature': 'S', 'partial': True})
            self.assertEqual((sent, lp.signers.MINT_HOLD['why']), ([], 'refused: mint paused'))
            lp.signers.note_mint_refusal('open', None, {'signature': 'S'})
        self.assertEqual((sent, lp.signers.MINT_HOLD['why']), (['mint_resumed'], None))

    def test_said_once_per_change_and_once_when_it_resumes(self):
        sent = []
        lp.signers.MINT_HOLD['why'] = None
        with mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append((ev, kw.get('reason')))), \
                mock.patch.object(db, 'event', lambda *a: None):
            for _ in range(3):
                lp.signers.note_mint_refusal('open', 'refused: mint paused', None)
            lp.signers.note_mint_refusal('open', 'refused: transfer hook', None)
            lp.signers.note_mint_refusal('open', 'RPC rate limited', None)                     # not a resume
            lp.signers.note_mint_refusal('open', None, {'signature': 'S'})
            lp.signers.note_mint_refusal('open', None, {'signature': 'S'})
        self.assertEqual(sent, [('mint_paused', 'refused: mint paused'), ('mint_paused', 'refused: transfer hook'),
                                ('mint_resumed', None)])


class SignerEnv(unittest.TestCase):
    def test_only_allowlisted_keys_and_values(self):
        self.assertEqual(config.signer_env(None), {})
        self.assertEqual(config.signer_env({'LPBOT_ORCA_ADAPTIVE': '1'}), {'LPBOT_ORCA_ADAPTIVE': '1'})
        for bad in ({'LPBOT_ORCA_ADAPTIVE': 1}, {'LPBOT_ORCA_ADAPTIVE': 'yes'}, {'WALLET_SECRET_PATH': '/tmp/k'},
                    {'LPBOT_PROFIT_WALLET_PIN': 'x'}, ['LPBOT_ORCA_ADAPTIVE'], 'LPBOT_ORCA_ADAPTIVE=1'):
            with self.assertRaises(ValueError):
                config.signer_env(bad)

    def test_the_venue_signer_gets_them_and_no_other_script_does(self):
        script = pathlib.Path(__file__).resolve().parent / '_env_probe_signer.mjs'
        script.write_text("console.log(JSON.stringify({adaptive: process.env.LPBOT_ORCA_ADAPTIVE ?? null, "
                          "rpc: process.env.LPBOT_RPC ?? null, gas: process.env.LPBOT_GAS_RESERVE_NATIVE ?? null, "
                          "run: process.env.LPBOT_RUN_DIR ?? null}));\n")
        self.addCleanup(lambda: script.unlink(missing_ok=True))
        env = dict(os.environ); env.pop('LPBOT_ORCA_ADAPTIVE', None)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(config, 'SIGNER_ENV', {'LPBOT_ORCA_ADAPTIVE': '1'}), \
                mock.patch.object(config, 'DEX', 'orca'), \
                mock.patch.dict(lp.signers.SIGNERS, {'orca': str(script), 'jupiter': str(script)}):
            own, err = lp.signers._chain('balance', dex='orca')
            other, err2 = lp.signers._chain('quote', dex='jupiter')
        self.assertIsNone(err); self.assertIsNone(err2)
        self.assertEqual(own['adaptive'], '1')
        self.assertIsNone(other['adaptive'])
        self.assertEqual(own['rpc'], config.RPC)
        self.assertEqual(own['gas'], str(config.GAS_RESERVE_SOL))
        self.assertEqual((own['run'], other['run']), (str(lp.paths.RUN), str(lp.paths.RUN)))   # every signer


class SignerArgs(unittest.TestCase):
    def test_evm_addresses_and_nft_token_ids_pass_and_options_do_not(self):
        self.assertTrue(guards.signer_args(['open', '0x8815F16a662341b345894477eA818a65617f6021', '1234567',
                                            '0.5', '--execute']))
        for bad in (['--pool', 'x'], ['0x88 15'], ['-1'], ['a\nb']):
            with self.assertRaises(guards.Refused):
                guards.signer_args(bad)


class Gas(unittest.TestCase):
    def go(self, bal):
        sent = []
        state = {}
        with mock.patch.object(config, 'DEX', 'meteora-dlmm'), mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(lp.books, 'notify', lambda ev, **kw: sent.append(ev)), \
                mock.patch.object(db, 'event', lambda *a: None), mock.patch.object(lp.paths, 'save', lambda s: None):
            out = [lp.capital.gas_for_open(state, bal) for _ in range(2)]
        return out, sent, state

    def test_a_non_native_pool_needs_the_reserve_and_its_rent(self):
        self.assertEqual(self.go(stock_bal(sol=0.1))[:2], ([True, True], []))           # exactly enough
        out, sent, state = self.go(stock_bal(sol=0.0999))
        self.assertEqual((out, sent), ([False, False], ['gas_short']))                 # said once
        self.assertIn('gas_short_told', state)
        for sol in (None, 0):
            self.assertEqual(self.go(stock_bal(sol=sol))[0], [False, False])
        self.assertEqual(self.go(stock_bal(sol=None, nativeSide='A'))[0], [True, True])  # the native side keeps its own

    def test_enough_gas_clears_the_notice(self):
        state = {'gas_short_told': 1.0}
        with mock.patch.object(config, 'DEX', 'meteora-dlmm'), mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05):
            self.assertIs(lp.capital.gas_for_open(state, stock_bal(sol=1.0)), True)
        self.assertEqual(state, {})


class Headroom(unittest.TestCase):
    def test_per_venue(self):
        self.assertEqual(lp.capital.open_headroom('meteora-dlmm'), 0.05)
        self.assertEqual(lp.capital.open_headroom('raydium-clmm'), lp.capital.OPEN_RENT_HEADROOM_SOL)
        self.assertEqual(lp.capital.open_headroom(None), lp.capital.OPEN_RENT_HEADROOM_SOL)
        with mock.patch.object(config, 'DEX', 'meteora-dlmm'), mock.patch.object(config, 'GAS_RESERVE_SOL', 0.05):
            self.assertAlmostEqual(lp.capital.native_reserve({}), 0.1)
            self.assertEqual(lp.capital.native_reserve({'nativeReserve': 0.2}), 0.2)

    def test_aerodrome_keeps_no_rent_back(self):
        # An Aerodrome position holds no rent: 0.009 ETH (~$25) never deployed
        # and a shifted swap target, before (review, 2026-10-02).
        self.assertEqual(lp.capital.open_headroom('aerodrome-slipstream'), 0.0)
        bal = {'balanceA': 0.1, 'balanceB': 0.0, 'price': 2500.0, 'quoteUsd': 1.0, 'nativeSide': 'A'}
        with mock.patch.object(config, 'DEX', 'aerodrome-slipstream'), mock.patch.object(config, 'GAS_RESERVE_SOL', 0.003):
            self.assertAlmostEqual(lp.capital.native_reserve(bal), 0.003)
            self.assertAlmostEqual(lp.capital.deployable_usd(bal), (0.1 - 0.003) * 2500.0)


class Audits(unittest.TestCase):
    def test_ui_amounts_come_from_the_rpc_string(self):
        self.assertEqual(audit.ui_amount({'amount': '100000000', 'decimals': 8, 'uiAmountString': '1.0059'}), 1.0059)
        self.assertEqual(audit.ui_amount({'amount': '1500000', 'decimals': 6}), 1.5)
        self.assertEqual(audit.human({'amount': 10**8, 'decimals': 8, 'ui': 1.0059}), 1.0059)
        self.assertEqual(audit.human({'amount': 10**8, 'decimals': 8}), 1.0)

    def test_the_multiplier_of_a_mint(self):
        ext = lambda now_ts: {'value': {'data': {'parsed': {'info': {'extensions': [
            {'extension': 'scaledUiAmountConfig', 'state': {'multiplier': '1.0059', 'newMultiplier': '2.0118',
                                                            'newMultiplierEffectiveTimestamp': now_ts}}]}}}}}
        with mock.patch.object(audit, 'rpc', lambda url, m, p, tries=4: ext(2_000_000_000)):
            self.assertEqual(audit.mint_scale('u', MSFTX, now=1_900_000_000), 1.0059)
            self.assertEqual(audit.mint_scale('u', MSFTX, now=2_000_000_000), 2.0118)   # from its timestamp on
        with mock.patch.object(audit, 'rpc', lambda *a, **k: {'value': {'data': {'parsed': {'info': {}}}}}):
            self.assertEqual(audit.mint_scale('u', 'PLAIN'), 1.0)
        with mock.patch.object(audit, 'rpc', lambda *a, **k: None):
            self.assertIsNone(audit.mint_scale('u', MSFTX))
        self.assertEqual(audit.mint_scale('u', USDC), 1.0)                             # no read for a plain mint

    def test_a_stock_deposit_is_a_flow_in_ui_units(self):
        from test_audit import tx, OWNER
        tb = lambda i, amount, ui: {'accountIndex': i, 'owner': OWNER, 'mint': MSFTX,
                                     'uiTokenAmount': {'amount': str(amount), 'decimals': 8, 'uiAmountString': ui}}
        t = tx(keys=('SENDER', OWNER), pre_tok=[tb(1, 0, '0')], post_tok=[tb(1, 10**8, '1.0059')])
        kind, d = audit.classify_tx(t, OWNER, capital=(MSFTX,))
        self.assertEqual((kind, d['amounts'], d['other_tokens']), ('deposit', {MSFTX: 1.0059}, {}))
        kind, d = audit.classify_tx(t, OWNER)
        self.assertEqual(kind, 'other')                            # outside the wallet's mints: not capital

    def test_a_flow_counts_only_the_wallets_own_accounts_after_minus_before(self):
        from test_audit import tx, OWNER
        tb = lambda i, owner, mint, ui: {'accountIndex': i, 'owner': owner, 'mint': mint,
                                         'uiTokenAmount': {'amount': '1', 'decimals': 8, 'uiAmountString': ui}}
        t = tx(keys=('SENDER', OWNER),
               pre_tok=[tb(1, OWNER, MSFTX, '1.0'), tb(2, 'SENDER', MSFTX, '9.0'), tb(3, OWNER, 'OTHER', '5')],
               post_tok=[tb(1, OWNER, MSFTX, '2.0059'), tb(2, 'SENDER', MSFTX, '7.9941'), tb(3, OWNER, 'OTHER', '5')])
        # the raw amounts above do not move (1 == 1): the UI strings carry the flow
        t['meta']['postTokenBalances'][0]['uiTokenAmount']['amount'] = '2'
        kind, d = audit.classify_tx(t, OWNER, capital=(MSFTX,))
        self.assertEqual(kind, 'deposit')
        self.assertAlmostEqual(d['amounts'][MSFTX], 1.0059)
        # another token of the owner moving in the same transaction is not this mint's amount
        t['meta']['postTokenBalances'][2]['uiTokenAmount'].update(amount='2', uiAmountString='6')
        kind, d = audit.classify_tx(t, OWNER, capital=(MSFTX,))
        self.assertAlmostEqual(d['amounts'][MSFTX], 1.0059)

    def test_a_token_account_created_by_the_deposit_has_no_pre_balance(self):
        from test_audit import tx, OWNER
        t = tx(keys=('SENDER', OWNER), post_tok=[{'accountIndex': 1, 'owner': OWNER, 'mint': MSFTX,
                                                 'uiTokenAmount': {'amount': '100000000', 'decimals': 8,
                                                                   'uiAmountString': '1.0059'}}])
        t['meta']['preTokenBalances'] = None
        kind, d = audit.classify_tx(t, OWNER, capital=(MSFTX,))
        self.assertEqual((kind, d['amounts']), ('deposit', {MSFTX: 1.0059}))

    def test_a_payout_of_a_scaled_mint_is_checked_in_ui_units(self):
        from test_audit import tx, OWNER, PROFIT
        tb = lambda amount, ui: {'accountIndex': 1, 'owner': PROFIT, 'mint': MSFTX,
                                 'uiTokenAmount': {'amount': str(amount), 'decimals': 8, 'uiAmountString': ui}}
        t = tx(keys=(OWNER, PROFIT), pre_tok=[tb(0, '0')], post_tok=[tb(10**8, '1.0059')])
        self.assertTrue(audit.payout_received(t, PROFIT, MSFTX, 1.0059))
        self.assertFalse(audit.payout_received(t, PROFIT, MSFTX, 1.0))

    def test_the_sweep_values_ui_amounts(self):
        acct = {'mint': MSFTX, 'amount': 10**8, 'decimals': 8, 'ui': 1.0059}
        p = lp.swaps.plan_sweep([acct], {USDC}, set(), {MSFTX: 500.0}, {MSFTX: {'verified': True}})
        self.assertEqual((p[0]['amount'], p[0]['usd']), (1.0059, round(1.0059 * 500.0, 4)))


if __name__ == '__main__':
    unittest.main()


class Positions(unittest.TestCase):
    def test_one_position_per_profile(self):
        ok = audit.check_positions(['A', 'B'], ['A', 'B'], ['orca', 'orca'], [], db_open_profiles=['sol-usdc', 'mu-usdc'])
        self.assertEqual(ok[0], 'ok'); self.assertNotIn('more_than_one', ok[1])
        st, d = audit.check_positions(['A', 'B', 'C'], ['A', 'B', 'C'], ['orca'] * 3, [],
                                      db_open_profiles=['sol-usdc', 'mu-usdc', 'sol-usdc'])
        self.assertEqual((st, d['more_than_one']), ('fail', ['sol-usdc']))
        self.assertEqual(audit.check_positions(['A'], ['A'], ['orca'], [], db_open_profiles=['sol-usdc'])[0], 'ok')
        self.assertEqual(audit.check_positions([], [], [], [], db_open_profiles=[])[0], 'ok')


class Books(unittest.TestCase):
    """The book's price is the UI price; a stock deposit is the stock
    profile's flow."""

    def test_the_snapshot_price_is_the_ui_price(self):
        status = {'positionMint': 'P', 'whirlpool': POOL, 'price': 500.0 / M, 'uiPrice': 500.0, 'quoteUsd': 1.0,
                  'inRange': True, 'liquidity': '1', 'positionUsd': 1000.0, 'feesAccruedA': 0.0, 'feesAccruedB': 1.0,
                  'feesAccrued_USD': 1.0}
        snaps = []
        quiet = lambda *a, **k: None
        with mock.patch.object(lp.signers, 'chain', lambda *a, **k: ({'signature': 'H'}, None)), \
                mock.patch.object(lp.harvest, 'measured_fees', lambda out, st, a, b, usd: (a, b, usd)), \
                mock.patch.object(db, 'record_harvest', quiet), \
                mock.patch.object(db, 'snapshot', lambda *a, **k: snaps.append(a)), \
                mock.patch.object(lp.capital, 'wallet', lambda pool: {'walletUsd': 1.0}), \
                mock.patch.object(db, 'event', quiet), mock.patch.object(lp.harvest, 'band_profile', quiet), \
                mock.patch.object(lp.books, 'notify_book', quiet), mock.patch.object(lp.harvest, 'distribute', quiet), \
                mock.patch.object(lp.harvest, 'distribute_rewards', quiet), mock.patch.object(lp.paths, 'save', quiet):
            self.assertTrue(lp.harvest.dividend({}, status))
        self.assertEqual(snaps[0][1], 500.0)

    def test_a_flow_belongs_to_the_profile_its_token_routes_to(self):
        ps = [{'name': 'sol-usdc', 'mints': ['So11111111111111111111111111111111111111112', USDC],
               'deposit_mint': 'So11111111111111111111111111111111111111112', 'residual_owner': True},
              {'name': 'msftx-usdc', 'mints': [MSFTX, USDC], 'deposit_mint': MSFTX, 'residual_owner': False}]
        self.assertEqual(audit.flow_owner({'sol': 0.0, 'usdc': 5.0, 'amounts': {MSFTX: 1.0}}, ps), 'msftx-usdc')
        self.assertEqual(audit.flow_owner({'sol': 0.5, 'usdc': 0.0}, ps), 'sol-usdc')
        self.assertEqual(audit.flow_owner({'sol': 0.0, 'usdc': 5.0}, ps), 'sol-usdc')            # USDC: the residual
        self.assertIsNone(audit.flow_owner({'sol': 0.0, 'usdc': 5.0}, ps[1:]))                    # no residual owner

    def test_record_flow_books_the_named_profile_without_touching_the_context(self):
        saved = dict(db.CONTEXT)
        try:
            db.set_context('sol-usdc', 'sol-lp')
            db.record_flow('2026-10-01T00:00:00Z', 'deposit', 0, 0, 1.0, 1.0, 'FLOWSIG', 'x',
                                      amounts={MSFTX: 1.0}, profile='msftx-usdc', wallet_id='sol-lp')
            db.record_flow('2026-10-01T00:00:00Z', 'deposit', 0, 0, 1.0, 1.0, 'FLOWSIG2', 'x')
            self.assertEqual(db.CONTEXT, {'profile': 'sol-usdc', 'wallet_id': 'sol-lp'})
            with db.cursor(commit=True) as cur:
                cur.execute("select signature, profile, wallet_id from capital_flows where signature like 'FLOWSIG%%' "
                            "order by signature")
                rows = [tuple(r.values()) for r in cur.fetchall()]
                cur.execute("delete from capital_flows where signature like 'FLOWSIG%%'")
            self.assertEqual(rows, [('FLOWSIG', 'msftx-usdc', 'sol-lp'), ('FLOWSIG2', 'sol-usdc', 'sol-lp')])
        finally:
            db.CONTEXT.clear(); db.CONTEXT.update(saved)


from test_audit_runner import Base as RunnerBase                      # noqa: E402
from test_audit import tx as audit_tx, OWNER                          # noqa: E402


class StockFlows(RunnerBase):
    def test_a_stock_deposit_is_recorded_for_the_stock_profile_in_its_units(self):
        sol_mint = 'So11111111111111111111111111111111111111112'
        tb = lambda amount, ui: {'accountIndex': 1, 'owner': OWNER, 'mint': MSFTX,
                                 'uiTokenAmount': {'amount': str(amount), 'decimals': 8, 'uiAmountString': ui}}
        self.sigs = [{'signature': 'DEP', 'blockTime': 5}]
        self.txs = {'DEP': audit_tx('DEP', keys=('SENDER', OWNER), pre_tok=[tb(0, '0')], post_tok=[tb(10**8, '1.0059')])}
        self.prices = {MSFTX: 500.0}
        book = {'profiles': [
            {'name': 'sol-usdc', 'mints': [sol_mint, USDC], 'deposit_mint': sol_mint, 'residual_owner': True, 'enabled': True},
            {'name': 'msftx-usdc', 'mints': [MSFTX, USDC], 'deposit_mint': MSFTX, 'residual_owner': False, 'enabled': True}],
            'mints': {sol_mint, USDC, MSFTX}, 'mints_of': {'sol-usdc': [sol_mint, USDC], 'msftx-usdc': [MSFTX, USDC]},
            'claims': {}}
        cfg = types.SimpleNamespace(RPC='http://rpc.test', POOL='POOL', GAS_RESERVE_SOL=0.05, PROFIT_WALLET='P' * 44,
                                    WALLET_ID='sol-lp')
        fx = types.SimpleNamespace(fetch=lambda url, sig, tries=3: self.txs.get(sig), harvested=lambda *a: None)
        saved = dict(db.CONTEXT)
        db.set_context('sol-usdc', 'sol-lp')
        seen = []                                          # the process's context while the flow is written
        real = db.record_flow

        def watched(*a, **k):
            seen.append(dict(db.CONTEXT))
            return real(*a, **k)
        try:
            with mock.patch.object(audit, 'rpc', self.fake_rpc), mock.patch.object(audit.time, 'sleep', lambda s: None), \
                    mock.patch.object(db, 'record_flow', watched):
                audit.run(self.bot(), db, cfg, fx, lambda ev, **kw: None, wallet=book)
            self.assertEqual(seen, [{'profile': 'sol-usdc', 'wallet_id': 'sol-lp'}])   # never switched
            self.assertEqual(db.CONTEXT['profile'], 'sol-usdc')
        finally:
            db.CONTEXT.clear(); db.CONTEXT.update(saved)
        with db.cursor() as cur:
            cur.execute("select kind, sol, usdc, usd, price, amounts, profile, wallet_id from capital_flows "
                        "where signature = 'DEP'")
            r = cur.fetchone()
        self.assertEqual((r['kind'], r['profile'], r['wallet_id']), ('deposit', 'msftx-usdc', 'sol-lp'))
        self.assertEqual((float(r['sol']), float(r['usdc'])), (0.0, 0.0))
        self.assertAlmostEqual(r['amounts'][MSFTX], 1.0059)
        self.assertAlmostEqual(float(r['usd']), 1.0059 * 500.0, places=4)
        self.assertEqual(r['price'], 500.0)
