"""The last line: invariants checked at the moment money is about to move.
Every case here is the shape of a real loss, and every one must be refused."""
import pathlib
import unittest

import _fixtures  # noqa: F401
import guards

ADDR = 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE'
SIGNERS = {'orca': 'signer2.mjs', 'meteora-dlmm': 'signer_dlmm.mjs', 'jupiter': 'swap_jupiter.mjs'}
KNOWN = ('orca', 'raydium-clmm', 'byreal', 'pancakeswap-v3-solana', 'meteora-dlmm')


def ok_open(**over):
    kw = dict(pool=ADDR, dex='orca', price=115.0, lower=106.0, upper=124.0, cap_a=0.9, cap_b=104.0,
              capital_usd=190.0, max_usd=260.0, quote_usd=1.0, execute_dexes=('orca',), signers=SIGNERS)
    kw.update(over)
    return kw


class Addresses(unittest.TestCase):
    def test_base58_shapes(self):
        self.assertTrue(guards.is_address(ADDR))
        self.assertTrue(guards.is_address('So11111111111111111111111111111111111111112'))
        for bad in ('', 'x' * 20, ADDR + '\n', ADDR.replace('C', '0'), ADDR.replace('z', 'O'), None, 42, 'a' * 45):
            self.assertFalse(guards.is_address(bad), bad)


class SignerArgs(unittest.TestCase):
    def test_accepts_the_arguments_the_loop_sends(self):
        self.assertTrue(guards.signer_args(('status',)))
        self.assertTrue(guards.signer_args(('open', ADDR, '105.993000', '123.630200', '0.912884000', '84.188666000', '--execute')))
        self.assertTrue(guards.signer_args(('rebalance', ADDR, ADDR, '104.5', '104.5')))

    def test_refuses_whitespace_control_and_junk(self):
        for bad in (('status', ADDR + '\n'), ('open', 'a b'), ('close', ''), ('x' * 200,), (None,), (b'x',),
                    ('harvest', 'mint;rm'), ('status', '$(id)'), ('open', "'")):
            with self.assertRaises(guards.Refused, msg=bad):
                guards.signer_args(bad)


class Inside(unittest.TestCase):
    def test_scripts_must_live_in_the_bot_directory(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        self.assertTrue(guards.inside(root / 'signer2.mjs', root))
        with self.assertRaises(guards.Refused):
            guards.inside('/tmp/evil.mjs', root)
        with self.assertRaises(guards.Refused):
            guards.inside(root / '..' / 'x.mjs', root)


class OpenRequest(unittest.TestCase):
    def test_a_sane_open_passes(self):
        self.assertTrue(guards.open_request(**ok_open()))

    def test_price_outside_the_band(self):
        for lo, hi in ((120, 130), (90, 110), (115, 124), (106, 115)):
            with self.assertRaises(guards.Refused):
                guards.open_request(**ok_open(lower=lo, upper=hi))

    def test_absurd_band_or_prices(self):
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(lower=10, upper=1000))
        for bad in (0, -1, float('nan'), float('inf'), None, 'x'):
            with self.assertRaises(guards.Refused, msg=bad):
                guards.open_request(**ok_open(price=bad))
            with self.assertRaises(guards.Refused, msg=bad):
                guards.open_request(**ok_open(quote_usd=bad))

    def test_caps_above_the_capital_or_the_ceiling(self):
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(cap_b=1000.0))
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(cap_a=3.0, cap_b=300.0, capital_usd=190))
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(cap_a=-0.1))
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(cap_b=float('nan')))
        # a non-dollar quote is valued through quote_usd
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(cap_a=0.001, cap_b=0.004, price=0.0014, quote_usd=100_000.0))
        self.assertTrue(guards.open_request(**ok_open(cap_a=0.0009, cap_b=0.001, price=0.0014,
                                                       lower=0.0013, upper=0.0015, quote_usd=100_000.0)))

    def test_dex_must_have_an_armed_signer(self):
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(dex='raydium-clmm'))
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(dex='meteora-dlmm', execute_dexes=('orca',)))
        self.assertTrue(guards.open_request(**ok_open(dex='meteora-dlmm', execute_dexes=('orca', 'meteora-dlmm'))))

    def test_pool_must_be_an_address(self):
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(pool='not-a-pool'))

    def test_model_price_must_agree_with_the_live_price(self):
        self.assertTrue(guards.open_request(**ok_open(model_price=116.0)))
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(model_price=200.0))
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(model_price=119.0))       # 3.5% off
        with self.assertRaises(guards.Refused):
            guards.open_request(**ok_open(model_price=float('nan')))


class MigrationTarget(unittest.TestCase):
    def target(self, **over):
        t = {'dex': 'meteora-dlmm', 'address': '5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6', 'pair': 'SOL/USDC',
             'token_a': {'address': 'So11111111111111111111111111111111111111112', 'symbol': 'SOL'},
             'token_b': {'address': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'symbol': 'USDC'}}
        t.update(over)
        return t

    def check(self, t, execute=('orca', 'meteora-dlmm')):
        return guards.migration_target(t, execute_dexes=execute, signers=SIGNERS, known=KNOWN)

    def test_a_board_row_with_an_armed_signer_passes(self):
        self.assertTrue(self.check(self.target()))

    def test_refusals(self):
        with self.assertRaises(guards.Refused):
            self.check(self.target(dex='raydium-clmm'))          # no signer
        with self.assertRaises(guards.Refused):
            self.check(self.target(), execute=('orca',))          # not armed
        with self.assertRaises(guards.Refused):
            self.check(self.target(dex='jupiter'))                # a swap route
        with self.assertRaises(guards.Refused):
            self.check(self.target(dex='binance'))                # unknown
        with self.assertRaises(guards.Refused):
            self.check(self.target(address='nope'))
        with self.assertRaises(guards.Refused):
            self.check(self.target(token_a={'address': 'x', 'symbol': 'SOL'}))
        with self.assertRaises(guards.Refused):
            self.check(self.target(token_b={'address': 'So11111111111111111111111111111111111111112', 'symbol': 'SOL'}))
        with self.assertRaises(guards.Refused):
            self.check(self.target(token_b={'address': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v', 'symbol': ''}))
        with self.assertRaises(guards.Refused):
            self.check('not a dict')
        with self.assertRaises(guards.Refused):
            self.check(self.target(token_b=None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
