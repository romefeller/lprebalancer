"""A Unichain profile end to end, on the fake chain of test_multi_loop: Uniswap
v3 USDC/HYPE, where token A is the stablecoin and token B the volatile token
(every older profile has the stable as token B). The price is HYPE per USDC,
so a HYPE is worth 1/price dollars, and the signer's quoteUsd says so.

Covered: a HYPE deposit swaps toward 50/50 and opens through the venue signer
(uniswap-v3-unichain), with the gas ETH outside the sleeve; the position and
the equity are valued in dollars, not in HYPE; a harvest books the fees at
their dollar value and pays the USDC fees (token A) to the pinned EVM profit
wallet through the venue signer, HYPE fees reinvested; the baseline holds the
USDC as the stable side and the HYPE as the base token; the book's hold
benchmarks move with the HYPE's dollar price (1/price), not with the pool
price; an operator close on a disabled profile harvests and closes."""
import contextlib
import json
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import db
import rebalancer
import test_multi_loop as M

UUSDC = '0x078d782b760474a361dda0af3839290b0ef57ad6'
HYPE = '0x15d0e0c55a3e7ee67152ad7e89acf164253ff68d'
WETH = M.WETH                                   # Unichain's WETH has Base's address
UNI_POOL = '0x5d3e7f5da38fbf476e8b36e3b90d02fc4c1a08c3'
UNI_WALLET, UNI_ADDRESS = 'e2e-uni', '0x' + 'cd' * 20
PRICE = 0.011                                   # HYPE per USDC
HYPE_USD = 1 / PRICE                            # ~$90.91
DEX = 'uniswap-v3-unichain'


class UniChain(M.FakeChain):
    """The fake chain, with the Unichain pool answered in its own units: the
    quote token is HYPE, worth HYPE_USD; the gas is native ETH (WETH key),
    not a pool token."""

    def __init__(self, **held):
        super().__init__(**held)
        self.wallet.update({UUSDC: 0.0, HYPE: 0.0})
        self.wallet.update(held)

    def balance(self, pool):
        if pool != UNI_POOL:
            return super().balance(pool)
        a, b, eth = self.wallet[UUSDC], self.wallet[HYPE], self.wallet[WETH]
        return {'owner': UNI_ADDRESS, 'sol': eth, 'pool': pool, 'dex': DEX, 'tokenA': 'USDC', 'tokenB': 'HYPE',
                'price': PRICE, 'uiPrice': PRICE, 'quoteUsd': HYPE_USD, 'balanceA': a, 'balanceB': b,
                'nativeSide': None, 'walletUsd': round((a * PRICE + b) * HYPE_USD + eth * 2500.0, 4)}

    def status(self, pool, mint=None):
        if pool != UNI_POOL:
            return super().status(pool, mint)
        pos = self.positions.get(pool)
        if not pos or (mint and pos['mint'] != mint):
            return {'positions': 0, 'positionMint': None, 'pool': pool}
        fa, fb = pos.get('fee_a', 0.0), pos.get('fee_b', 0.0)
        return {'positionMint': pos['mint'], 'whirlpool': pool, 'pool': pool, 'dex': DEX, 'price': PRICE,
                'uiPrice': PRICE, 'lowerPrice': pos['lower'], 'upperPrice': pos['upper'], 'inRange': True,
                'quoteUsd': HYPE_USD, 'liquidity': '1', 'closeEstA': pos['a'], 'closeEstB': pos['b'],
                'positionUsd': (pos['a'] * PRICE + pos['b']) * HYPE_USD, 'rentUsd': 0.0, 'rentSol': 0.0,
                'feesAccruedA': fa, 'feesAccruedB': fb, 'feesAccrued_quote': fa * PRICE + fb,
                'feesAccrued_USD': (fa * PRICE + fb) * HYPE_USD}

    def open(self, pool, lower, upper, max_a, max_b):
        out, err = super().open(pool, lower, upper, max_a, max_b)
        if out and pool == UNI_POOL:
            pos = self.positions[pool]
            out['depositUsd'] = (pos['a'] * PRICE + pos['b']) * HYPE_USD     # dollars, not HYPE
        return out, err

    def answer(self, *args, dex=None, extra_env=None):
        if args[0] == 'harvest':
            pool = next((k for k, v in self.positions.items() if v['mint'] == args[1]), None)
            if pool == UNI_POOL:
                pos = self.positions[pool]
                self.wallet[UUSDC] += pos.pop('fee_a', 0.0)
                self.wallet[HYPE] += pos.pop('fee_b', 0.0)
        return super().answer(*args, dex=dex, extra_env=extra_env)


class Unichain(M.Fixture):

    def setUp(self):
        for p in (mock.patch.dict(M.POOLS, {UNI_POOL: {'dex': DEX, 'a': UUSDC, 'b': HYPE, 'sa': 'USDC', 'sb': 'HYPE',
                                                       'price': PRICE, 'native': None}}),
                  mock.patch.dict(M.PROFILES, {'e2e-uni': dict(pool=UNI_POOL, wallet=UNI_WALLET, deposit=HYPE,
                                                               residual=True, chain='unichain')}),
                  mock.patch.dict(M.USD, {UUSDC: 1.0, HYPE: HYPE_USD})):
            p.start(); self.addCleanup(p.stop)
        with db.cursor(commit=True) as cur:
            # the fake chain names positions POS<n>x0x5d3e: a harvest row of an earlier test must not stay
            cur.execute("delete from harvests where mint like %s", ('POS%x' + UNI_POOL[:6],))
            cur.execute('delete from wallets where id = %s', (UNI_WALLET,))
            cur.execute("insert into wallets (id, chain, address, secret_env) values (%s, 'unichain', %s, %s)",
                        (UNI_WALLET, UNI_ADDRESS, 'LPBOT_UNICHAIN_KEY_PATH'))
        self.addCleanup(self._drop_wallet)
        super().setUp()
        self.chain = UniChain()
        band = {'band': 1.05, 'net_day_pct': 0.1, 'rebal_per_day': 0.1}
        for p in (mock.patch.object(rebalancer, '_chain', self.chain),
                  mock.patch.object(rebalancer, 'best_band_for', lambda pool, dex=None: dict(
                      band, price=M.POOLS[pool]['price'], record=M.pool_record_for(pool), all_runs=[band]))):
            p.start(); self.addCleanup(p.stop)

    def _drop_wallet(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from harvests where mint like %s", ('POS%x' + UNI_POOL[:6],))
            cur.execute('delete from wallet_claims where wallet_id = %s', (UNI_WALLET,))
            cur.execute('delete from wallet_settle where wallet_id = %s', (UNI_WALLET,))
            cur.execute('delete from wallets where id = %s', (UNI_WALLET,))

    @contextlib.contextmanager
    def as_profile(self, name):
        with super().as_profile(name) as run, mock.patch.object(config, 'WALLET_ADDRESS', UNI_ADDRESS):
            yield run

    @contextlib.contextmanager
    def paying(self):
        with mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(config, 'PROFIT_WALLET', M.EVM_PROFIT), \
                mock.patch.object(config, 'PAYOUT_MINT', UUSDC), \
                mock.patch.object(config, 'HARVEST_INTERVAL', 1), \
                mock.patch.object(config, 'MIN_HARVEST_USD', 0.25), \
                mock.patch.dict('os.environ', {'LPBOT_EVM_PROFIT_WALLET_PIN': M.EVM_PROFIT,
                                               'LPBOT_PROFIT_WALLET_PIN': ''}):
            yield

    def opened(self):
        self.chain.wallet.update({HYPE: 2.2, WETH: 0.06})               # $200 of HYPE, gas ETH kept
        self.poll('e2e-uni')
        self.assertIn(UNI_POOL, self.chain.positions)
        return self.chain.positions[UNI_POOL]

    def test_a_hype_deposit_swaps_and_opens_through_the_venue_signer(self):
        pos = self.opened()
        swap = self.chain.of('e2e-uni', 'rebalance')
        self.assertEqual(len(swap), 1)
        self.assertEqual(swap[0]['dex'], DEX)
        self.assertEqual(swap[0]['args'][1:3], (UUSDC, HYPE))
        # the sleeve is the pool's two tokens; the gas ETH is not one of them
        self.assertEqual(json.loads(swap[0]['env']['LPBOT_SLEEVE']), {UUSDC: 0.0, HYPE: 2.2})
        self.assertEqual(self.chain.of('e2e-uni', 'open')[0]['dex'], DEX)
        self.assertEqual(self.chain.wallet[WETH], 0.06)
        # both sides of the band hold about half of the $200, in dollars
        self.assertAlmostEqual(pos['a'], pos['b'] * HYPE_USD, delta=0.05 * pos['a'])
        self.assertAlmostEqual(pos['a'] + pos['b'] * HYPE_USD, 200.0, delta=5.0)

    def test_the_position_and_equity_are_valued_in_dollars(self):
        self.opened()
        self.poll('e2e-uni')                                             # in band: a snapshot
        with self.as_profile('e2e-uni'):
            st = self.chain.status(UNI_POOL)
            self.assertAlmostEqual(rebalancer.position_usd(st), st['positionUsd'], places=6)
            bal = rebalancer.wallet(UNI_POOL)
            self.assertAlmostEqual(rebalancer.quote_price(bal), HYPE_USD, places=6)
            self.assertAlmostEqual(rebalancer.deployable_usd(bal),
                                   self.chain.wallet[UUSDC] + self.chain.wallet[HYPE] * HYPE_USD, places=6)
        with db.cursor() as cur:
            cur.execute("select equity_usd, position_usd from snapshots s join positions p using (mint) "
                        "where p.config_name = 'e2e-uni' order by s.id desc limit 1")
            snap = cur.fetchone()
        self.assertIsNotNone(snap)
        self.assertGreater(float(snap['position_usd']), 150.0)            # dollars: ~$200, never ~2 HYPE

    def test_a_null_quote_price_from_the_signer_is_one_over_price_for_a_stable_token_a(self):
        with self.as_profile('e2e-uni'):
            self.assertAlmostEqual(rebalancer.quote_price({'quoteUsd': None, 'price': PRICE}), HYPE_USD, places=6)
            self.assertEqual(rebalancer.stable_quote_usd(UUSDC, HYPE, 0), None)
            self.assertEqual(rebalancer.stable_quote_usd(HYPE, UUSDC, PRICE), 1.0)
            self.assertEqual(rebalancer.stable_quote_usd(HYPE, M.WETH, PRICE), None)

    def test_a_harvest_books_dollars_and_pays_the_usdc_fees_through_the_venue_signer(self):
        pos = self.opened()
        pos.update(fee_a=3.0, fee_b=0.011)                               # $3 USDC + $1 of HYPE
        usdc_before = self.chain.wallet[UUSDC]
        with self.as_profile('e2e-uni'), self.paying():
            try:
                rebalancer.main()
            except M.StopPoll:
                pass
        self.assertEqual(len(self.chain.of('e2e-uni', 'harvest')), 1)
        with db.cursor() as cur:
            cur.execute("select fee_a, fee_b, fee_usd from harvests h join positions p using (mint) "
                        "where p.config_name = 'e2e-uni'")
            h = cur.fetchone()
        self.assertEqual((float(h['fee_a']), float(h['fee_b'])), (3.0, 0.011))
        self.assertAlmostEqual(float(h['fee_usd']), 4.0, places=4)
        send = self.chain.of('e2e-uni', 'send')
        self.assertEqual(len(send), 1)
        self.assertEqual(send[0]['dex'], DEX)
        self.assertEqual(send[0]['args'][1:4], (UUSDC, '3.000000000', M.EVM_PROFIT))
        self.assertAlmostEqual(self.chain.wallet[UUSDC], usdc_before, places=9)     # the USDC fees left
        with db.cursor() as cur:
            cur.execute("select token_mint, kind, round(usd::numeric, 4) usd from payouts "
                        "where config_name = 'e2e-uni' order by kind")
            rows = [(r["token_mint"], r["kind"], float(r["usd"])) for r in cur.fetchall()]
        self.assertEqual(rows, [(UUSDC, 'paid', 3.0), (HYPE, 'reinvested', 1.0)])

    def test_the_baseline_holds_hype_as_the_base_token_and_usdc_as_the_stable_side(self):
        self.chain.wallet.update({UUSDC: 50.0, HYPE: 1.1, WETH: 0.06})
        self.poll('e2e-uni')
        with db.cursor() as cur:
            cur.execute("select sol, usdc, usd, price, amounts from capital_flows "
                        "where kind = 'baseline' and profile = 'e2e-uni'")
            r = cur.fetchone()
        self.assertEqual(float(r['sol']), 0.0)                          # ETH is not a pool token
        self.assertEqual(float(r['usdc']), 50.0)                        # the stable side is token A
        self.assertAlmostEqual(float(r['usd']), 50.0 + 1.1 * HYPE_USD, places=4)
        self.assertEqual(r['amounts'], {UUSDC: 50.0, HYPE: 1.1})

    def test_the_hold_benchmarks_move_with_the_hype_dollar_price(self):
        self.chain.wallet.update({UUSDC: 50.0, HYPE: 1.1, WETH: 0.06})
        self.poll('e2e-uni')
        with db.cursor(commit=True) as cur:
            cur.execute("update config set mints = %s where name = 'e2e-uni'", ([UUSDC, HYPE],))
            cur.execute("select mint from positions where config_name = 'e2e-uni'")
            mint = cur.fetchone()['mint']
            # HYPE doubles: the pool price (HYPE per USDC) halves
            cur.execute("insert into snapshots (ts, mint, price, equity_usd) values "
                        "(now() + interval '1 second', %s, %s, 200.0), (now() + interval '2 seconds', %s, %s, 250.0)",
                        (mint, PRICE, mint, PRICE / 2))
        s = db.since_start(profile='e2e-uni')
        base_usd = 50.0 + 1.1 * HYPE_USD
        self.assertAlmostEqual(s['hold_start_assets_usd'], 50.0 + 1.1 * HYPE_USD * 2, places=3)
        self.assertAlmostEqual(s['hold_50_50_usd'], base_usd * (0.5 + 0.5 * 2), places=3)
        with db.cursor() as cur:
            cur.execute("select min(ts)::date d from snapshots where mint = %s", (mint,))
            day = cur.fetchone()['d']
        line = db.daily_line(day, profile='e2e-uni')
        self.assertEqual((line['equity_open'], line['equity_close']), (200.0, 250.0))
        self.assertAlmostEqual(line['hold_50_50_usd'], 200.0 * (0.5 + 0.5 * 2), places=3)    # not 200 x 0.75

    def test_an_operator_close_on_a_disabled_profile_harvests_and_closes(self):
        self.opened()
        self.chain.calls.clear()
        with db.cursor(commit=True) as cur:
            cur.execute("update config set enabled = false where name = 'e2e-uni'")
        (self.tmp / 'e2e-uni' / 'CLOSE').write_text('')
        self.poll('e2e-uni')
        writes = [(c['args'][0], c['dex']) for c in self.chain.calls if '--execute' in c['args']]
        self.assertEqual(writes, [('harvest', DEX), ('close', DEX)])
        self.assertNotIn(UNI_POOL, self.chain.positions)


class QuoteFallbacks(M.Fixture):
    """A null quoteUsd from the signer, filled from the mints: priced at the
    UI price when the read carries one, else at the pool price."""

    def setUp(self):
        super().setUp()
        tokens = mock.patch.object(rebalancer, 'pool_tokens', lambda: ((UUSDC, 'USDC'), (HYPE, 'HYPE')))
        tokens.start(); self.addCleanup(tokens.stop)

    def test_quote_price_prefers_the_ui_price(self):
        self.assertAlmostEqual(rebalancer.quote_price({'quoteUsd': None, 'uiPrice': 0.01, 'price': 0.02}), 100.0)
        self.assertAlmostEqual(rebalancer.quote_price({'quoteUsd': None, 'price': 0.02}), 50.0)

    def test_sleeve_of_fills_the_quote_from_the_ui_price_else_the_price(self):
        with mock.patch.object(config, 'WALLET_ID', None):
            a = rebalancer.sleeve_of({'balanceA': 1.0, 'balanceB': 1.0, 'quoteUsd': None, 'uiPrice': 0.01, 'price': 0.02})
            b = rebalancer.sleeve_of({'balanceA': 1.0, 'balanceB': 1.0, 'quoteUsd': None, 'price': 0.02})
        self.assertAlmostEqual(a['quoteUsd'], 100.0)
        self.assertAlmostEqual(b['quoteUsd'], 50.0)
        self.assertEqual(a['quoteUsdSource'], 'stable mint')


class StableFirst(unittest.TestCase):
    """db.stable_first and db.volatile_usd, the pure parts of the book's fix."""

    def test_token_a_stable_only(self):
        self.assertTrue(db.stable_first([UUSDC, HYPE]))
        self.assertFalse(db.stable_first([HYPE, UUSDC]))
        self.assertFalse(db.stable_first([M.SOL, M.USDC]))
        self.assertFalse(db.stable_first([UUSDC, M.BUSDC]))           # two stables: no volatile side
        self.assertFalse(db.stable_first(None))
        self.assertFalse(db.stable_first([UUSDC]))
        self.assertFalse(db.stable_first([HYPE, M.WETH]))               # no stablecoin at all

    def test_an_unknown_profile_has_no_mints(self):
        with db.cursor() as cur:
            self.assertIsNone(db._profile_mints(cur, 'no-such-profile'))

    def test_volatile_dollar_price(self):
        self.assertEqual(db.volatile_usd(150.0, False), 150.0)
        self.assertAlmostEqual(db.volatile_usd(0.011, True), 1 / 0.011)
        self.assertEqual(db.volatile_usd(0.0, True), 0.0)

    def test_engine_quote_for_a_stable_token_a(self):
        import engine
        rec = {'token_a': {'address': UUSDC}, 'token_b': {'address': HYPE}, 'price': PRICE}
        self.assertAlmostEqual(engine.stable_quote(rec), HYPE_USD)
        self.assertAlmostEqual(engine.pool_quote_price(rec), HYPE_USD)
        self.assertEqual(engine.stable_quote({'token_a': {'address': HYPE}, 'token_b': {'address': UUSDC}}), 1.0)
        self.assertIsNone(engine.stable_quote({'token_a': {'address': HYPE}, 'token_b': {'address': WETH},
                                               'price': 1.0}))
        self.assertIsNone(engine.stable_quote(dict(rec, price=0)))
        self.assertIsNone(engine.stable_quote(dict(rec, price=None)))

    def test_pool_quote_price_asks_gecko_for_a_volatile_quote(self):
        import engine
        rec = {'token_a': {'address': HYPE}, 'token_b': {'address': M.WETH, 'symbol': 'WETH'}, 'price': 1.0}
        answer = lambda p: {'data': {'attributes': {'token_prices': {M.WETH: p}}}}
        with mock.patch.object(engine, 'curl', return_value=answer('2500.5')) as c:
            self.assertEqual(engine.pool_quote_price(rec), 2500.5)
        with mock.patch.object(engine, 'curl', return_value=answer('0.5')):
            self.assertEqual(engine.pool_quote_price(rec), 0.5)             # a quote token under a dollar
        self.assertIn(M.WETH, c.call_args[0][0])
        for bad in (answer('0'), answer(None), {}, None, {'data': None}, {'data': {'attributes': {}}}):
            with mock.patch.object(engine, 'curl', return_value=bad):
                self.assertIsNone(engine.pool_quote_price(rec), bad)
        with mock.patch.object(engine, 'curl', return_value=answer('x')):
            self.assertIsNone(engine.pool_quote_price(rec))
        with mock.patch.object(engine, 'curl') as c:
            self.assertIsNone(engine.pool_quote_price({'token_b': {'symbol': 'WETH'}, 'price': 1.0}))
            self.assertIsNone(engine.pool_quote_price({'price': 1.0}))
        c.assert_not_called()


if __name__ == '__main__':
    unittest.main()
