"""A Polygon profile end to end, on the fake chain of test_multi_loop: Uniswap
v3 WPOL/USDT0 0.05%, token A the volatile WPOL, token B the stable USDT0. Gas
is native POL, outside the pool (the signer never wraps it into WPOL).

Covered: a USDT0 deposit swaps toward 50/50 and opens through the venue signer
(uniswap-v3-polygon), with the gas POL outside the sleeve; the baseline holds
no POL; a harvest books the fees in dollars and pays the USDT0 fees (token B)
to the pinned EVM profit wallet through the venue signer, WPOL fees reinvested;
with POL under the gas reserve the USDT0 fees are reinvested, nothing is sent,
and the PAYOUT row says gas_low with the reserve and what stayed (the ⛽ PAYOUT
HELD message); an operator close on a disabled profile harvests and closes."""
import contextlib
import json
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import db
import rebalancer
import test_multi_loop as M

WPOL = '0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270'
USDT0 = '0xc2132d05d31c914a87c6611c10748aeb04b58e8f'
POL = 'native-pol'                              # the fake wallet's key for the gas POL
POLY_POOL = '0x9b08288c3be4f62bbf8d1c20ac9c5e6f9467d8b7'
POLY_WALLET, POLY_ADDRESS = 'e2e-poly', '0x' + 'ef' * 20
PRICE = 0.10                                    # USDT0 per WPOL
DEX = 'uniswap-v3-polygon'


class PolyChain(M.FakeChain):
    """The fake chain, with the Polygon pool: WPOL at $0.10, USDT0 at $1; the
    gas is native POL, not a pool token."""

    def __init__(self, **held):
        super().__init__(**held)
        self.wallet.update({WPOL: 0.0, USDT0: 0.0, POL: 0.0})
        self.wallet.update(held)

    def balance(self, pool):
        if pool != POLY_POOL:
            return super().balance(pool)
        a, b, gas = self.wallet[WPOL], self.wallet[USDT0], self.wallet[POL]
        return {'owner': POLY_ADDRESS, 'sol': gas, 'eth': gas, 'chain': 'polygon', 'pool': pool, 'dex': DEX,
                'tokenA': 'WPOL', 'tokenB': 'USDT0', 'mintA': WPOL, 'mintB': USDT0,
                'price': PRICE, 'uiPrice': PRICE, 'quoteUsd': 1.0, 'balanceA': a, 'balanceB': b,
                'nativeSide': None, 'walletUsd': round(a * PRICE + b, 4)}

    def status(self, pool, mint=None):
        if pool != POLY_POOL:
            return super().status(pool, mint)
        pos = self.positions.get(pool)
        if not pos or (mint and pos['mint'] != mint):
            return {'positions': 0, 'positionMint': None, 'pool': pool}
        fa, fb = pos.get('fee_a', 0.0), pos.get('fee_b', 0.0)
        return {'positionMint': pos['mint'], 'whirlpool': pool, 'pool': pool, 'dex': DEX, 'price': PRICE,
                'uiPrice': PRICE, 'lowerPrice': pos['lower'], 'upperPrice': pos['upper'], 'inRange': True,
                'quoteUsd': 1.0, 'liquidity': '1', 'closeEstA': pos['a'], 'closeEstB': pos['b'],
                'positionUsd': pos['a'] * PRICE + pos['b'], 'rentUsd': 0.0, 'rentSol': 0.0,
                'feesAccruedA': fa, 'feesAccruedB': fb, 'feesAccrued_quote': fa * PRICE + fb,
                'feesAccrued_USD': fa * PRICE + fb}

    def answer(self, *args, dex=None, extra_env=None):
        if args[0] == 'harvest':
            pool = next((k for k, v in self.positions.items() if v['mint'] == args[1]), None)
            if pool == POLY_POOL:
                pos = self.positions[pool]
                self.wallet[WPOL] += pos.pop('fee_a', 0.0)
                self.wallet[USDT0] += pos.pop('fee_b', 0.0)
        return super().answer(*args, dex=dex, extra_env=extra_env)


class Polygon(M.Fixture):

    def setUp(self):
        for p in (mock.patch.dict(M.POOLS, {POLY_POOL: {'dex': DEX, 'a': WPOL, 'b': USDT0, 'sa': 'WPOL', 'sb': 'USDT0',
                                                        'price': PRICE, 'native': None}}),
                  mock.patch.dict(M.PROFILES, {'e2e-poly': dict(pool=POLY_POOL, wallet=POLY_WALLET, deposit=WPOL,
                                                                residual=True, chain='polygon')}),
                  mock.patch.dict(M.USD, {WPOL: PRICE, USDT0: 1.0})):
            p.start(); self.addCleanup(p.stop)
        with db.cursor(commit=True) as cur:
            cur.execute("delete from harvests where mint like %s", ('POS%x' + POLY_POOL[:6],))
            cur.execute('delete from wallets where id = %s', (POLY_WALLET,))
            cur.execute("insert into wallets (id, chain, address, secret_env) values (%s, 'polygon', %s, %s)",
                        (POLY_WALLET, POLY_ADDRESS, 'LPBOT_POLYGON_KEY_PATH'))
        self.addCleanup(self._drop_wallet)
        super().setUp()
        self.chain = PolyChain()
        band = {'band': 1.05, 'net_day_pct': 0.1, 'rebal_per_day': 0.1}
        for p in (mock.patch.object(rebalancer, '_chain', self.chain),
                  mock.patch.object(rebalancer, 'best_band_for', lambda pool, dex=None: dict(
                      band, price=M.POOLS[pool]['price'], record=M.pool_record_for(pool), all_runs=[band]))):
            p.start(); self.addCleanup(p.stop)

    def _drop_wallet(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from harvests where mint like %s", ('POS%x' + POLY_POOL[:6],))
            cur.execute('delete from wallet_claims where wallet_id = %s', (POLY_WALLET,))
            cur.execute('delete from wallet_settle where wallet_id = %s', (POLY_WALLET,))
            cur.execute('delete from wallets where id = %s', (POLY_WALLET,))

    @contextlib.contextmanager
    def as_profile(self, name):
        with super().as_profile(name) as run, mock.patch.object(config, 'WALLET_ADDRESS', POLY_ADDRESS):
            yield run

    @contextlib.contextmanager
    def paying(self, reserve=2.0):
        with mock.patch.object(config, 'PAYOUT_ENABLED', True), \
                mock.patch.object(config, 'PROFIT_WALLET', M.EVM_PROFIT), \
                mock.patch.object(config, 'PAYOUT_MINT', USDT0), \
                mock.patch.object(config, 'GAS_RESERVE_SOL', reserve), \
                mock.patch.object(config, 'HARVEST_INTERVAL', 1), \
                mock.patch.object(config, 'MIN_HARVEST_USD', 0.25), \
                mock.patch.dict('os.environ', {'LPBOT_EVM_PROFIT_WALLET_PIN': M.EVM_PROFIT,
                                               'LPBOT_PROFIT_WALLET_PIN': ''}):
            yield

    def opened(self):
        self.chain.wallet.update({USDT0: 200.0, POL: 10.0})              # $200 of USDT0, 10 POL of gas
        self.poll('e2e-poly')
        self.assertIn(POLY_POOL, self.chain.positions)
        return self.chain.positions[POLY_POOL]

    def harvest(self, fee_a, fee_b, gas, reserve=2.0):
        pos = self.opened()
        pos.update(fee_a=fee_a, fee_b=fee_b)
        self.chain.wallet[POL] = gas
        seen = []
        real = rebalancer.notify
        with self.as_profile('e2e-poly'), self.paying(reserve), \
                mock.patch.object(rebalancer, 'notify', lambda e, **kw: (seen.append((e, kw)), real(e, **kw))):
            try:
                rebalancer.main()
            except M.StopPoll:
                pass
        with db.cursor() as cur:
            cur.execute("select token_mint, kind, round(usd::numeric, 4) usd from payouts "
                        "where config_name = 'e2e-poly' order by kind, token_mint")
            rows = [(r['token_mint'], r['kind'], float(r['usd'])) for r in cur.fetchall()]
        return rows, [kw for e, kw in seen if e == 'PAYOUT']

    def test_a_usdt0_deposit_swaps_and_opens_through_the_venue_signer(self):
        pos = self.opened()
        swap = self.chain.of('e2e-poly', 'rebalance')
        self.assertEqual(len(swap), 1)
        self.assertEqual(swap[0]['dex'], DEX)
        self.assertEqual(swap[0]['args'][1:3], (WPOL, USDT0))
        # the sleeve is the pool's two tokens; the gas POL is not one of them
        self.assertEqual(json.loads(swap[0]['env']['LPBOT_SLEEVE']), {WPOL: 0.0, USDT0: 200.0})
        self.assertEqual(self.chain.of('e2e-poly', 'open')[0]['dex'], DEX)
        self.assertEqual(self.chain.wallet[POL], 10.0)                   # gas untouched
        self.assertAlmostEqual(pos['a'] * PRICE, pos['b'], delta=0.05 * pos['b'])
        self.assertAlmostEqual(pos['a'] * PRICE + pos['b'], 200.0, delta=5.0)

    def test_the_baseline_holds_no_pol(self):
        self.chain.wallet.update({USDT0: 50.0, WPOL: 1000.0, POL: 10.0})
        self.poll('e2e-poly')
        with db.cursor() as cur:
            cur.execute("select sol, usdc, usd, amounts from capital_flows "
                        "where kind = 'baseline' and profile = 'e2e-poly'")
            r = cur.fetchone()
        self.assertEqual(float(r['usdc']), 50.0)                        # the stable side is token B
        self.assertAlmostEqual(float(r['usd']), 50.0 + 1000.0 * PRICE, places=4)
        self.assertEqual(r['amounts'], {WPOL: 1000.0, USDT0: 50.0})      # no POL key: gas is not capital

    def test_a_harvest_pays_the_usdt0_fees_and_reinvests_the_wpol(self):
        rows, pays = self.harvest(fee_a=10.0, fee_b=3.0, gas=10.0)       # $1 of WPOL + $3 USDT0, gas fine
        self.assertEqual(len(self.chain.of('e2e-poly', 'harvest')), 1)
        send = self.chain.of('e2e-poly', 'send')
        self.assertEqual(len(send), 1)
        self.assertEqual(send[0]['dex'], DEX)
        self.assertEqual(send[0]['args'][1:4], (USDT0, '3.000000000', M.EVM_PROFIT))
        self.assertEqual(rows, [(USDT0, 'paid', 3.0), (WPOL, 'reinvested', 1.0)])
        self.assertFalse(pays[0]['gas_low'])
        self.assertEqual(pays[0]['held'], [])

    def test_pol_under_the_reserve_holds_the_payout_with_the_pump(self):
        rows, pays = self.harvest(fee_a=10.0, fee_b=3.0, gas=1.5, reserve=2.0)
        self.assertEqual(self.chain.of('e2e-poly', 'send'), [])          # nothing sent
        self.assertEqual(rows, [(WPOL, 'reinvested', 1.0), (USDT0, 'reinvested', 3.0)])
        pay = pays[0]
        self.assertTrue(pay['gas_low'])
        self.assertEqual((pay['sol_before'], pay['gas_reserve']), (1.5, 2.0))
        self.assertEqual(pay['held'], [{'symbol': 'USDT0', 'amount': 3.0, 'usd': 3.0}])
        self.assertEqual(pay['split']['gas'], 0)                         # WPOL fees never refill POL
        self.assertEqual(rebalancer.emoji_for('PAYOUT', pay), '⛽')

    def test_an_operator_close_on_a_disabled_profile_harvests_and_closes(self):
        self.opened()
        self.chain.calls.clear()
        with db.cursor(commit=True) as cur:
            cur.execute("update config set enabled = false where name = 'e2e-poly'")
        (self.tmp / 'e2e-poly' / 'CLOSE').write_text('')
        self.poll('e2e-poly')
        writes = [(c['args'][0], c['dex']) for c in self.chain.calls if '--execute' in c['args']]
        self.assertEqual(writes, [('harvest', DEX), ('close', DEX)])
        self.assertNotIn(POLY_POOL, self.chain.positions)


if __name__ == '__main__':
    unittest.main()
