"""db.since_start per profile: the token A mint a flow is counted in (the
config's mints, else its deposit mint), a profile without a config row,
several profiles of a wallet added up, the day count, and record_flow's
explicit book."""
import unittest

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import db
from test_audit import reset

MU = 'MUmintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
WALLET = 'w-since'
NAMES = ('ss-mu', 'ss-two')


def cleanup():
    with db.cursor(commit=True) as cur:
        cur.execute('truncate positions, band_profile, harvests, snapshots, capital_flows, payouts')
        cur.execute('delete from config where name = any(%s)', (list(NAMES),))
        cur.execute('delete from wallets where id = %s', (WALLET,))


class Scope(unittest.TestCase):
    def setUp(self):
        reset(); cleanup()
        self.addCleanup(cleanup)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into wallets (id, chain, address, secret_env) values (%s, 'solana', '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f', 'WALLET_SECRET_PATH')",
                        (WALLET,))

    def profile(self, name, mints=None, deposit=None, enabled=True):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, wallet_id, enabled, mints, "
                        "deposit_mint) values (%s, 'POOL', 'MU/USDC', 100, 1000, %s, %s, %s, %s)",
                        (name, WALLET, enabled, mints, deposit))

    def book(self, name, ts0='2026-09-01 00:00+00', ts1='2026-09-03 00:00+00', amount=5.0, usd=50.0, equity=60.0,
             price=12.0):
        """A baseline that holds `amount` of the token in `amounts` (the sol column says 0) and one snapshot."""
        mint = f'{name}-pos'
        with db.cursor(commit=True) as cur:
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, amounts, profile, wallet_id) "
                        "values (%s, 'baseline', 0, 0, %s, 10, %s, %s, %s)",
                        (ts0, usd, f'{{"{MU}": {amount}}}', name, WALLET))
            cur.execute("insert into positions (mint, config_name, pool) values (%s, %s, 'POOL')", (mint, name))
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values (%s, %s, %s, true, '1', %s)",
                        (ts1, mint, price, equity))

    def test_the_token_a_mint_is_the_configs_first(self):
        self.profile('ss-mu', mints=[MU, USDC])
        self.book('ss-mu')
        db.record_flow('2026-09-02 00:00+00', 'deposit', 0, 0, 20.0, 10.0, 'D1', 'x', amounts={MU: 2.0},
                       profile='ss-mu', wallet_id=WALLET)
        s = db.since_start(0.0, 'ss-mu')
        self.assertEqual(s['start_sol'], 7.0)                       # 5 at the start, 2 deposited
        self.assertEqual(s['hold_start_assets_usd'], 7.0 * 12.0)

    def test_without_mints_the_deposit_mint(self):
        self.profile('ss-mu', mints=None, deposit=MU)
        self.book('ss-mu')
        self.assertEqual(db.since_start(0.0, 'ss-mu')['start_sol'], 5.0)

    def test_without_either_the_sol_column(self):
        self.profile('ss-mu', mints=None, deposit=None)
        self.book('ss-mu')
        self.assertEqual(db.since_start(0.0, 'ss-mu')['start_sol'], 0.0)

    def test_a_profile_without_a_config_row(self):
        self.assertIsNone(db.since_start(0.0, 'ss-no-row'))

    def test_unknown_uncounted_value_is_zero(self):
        self.profile('ss-mu', mints=[MU, USDC])
        self.book('ss-mu')
        s = db.since_start(None, 'ss-mu')
        self.assertEqual((s['uncounted_usd'], s['value_usd']), (0.0, 60.0))

    def test_days_over_a_long_span(self):
        self.profile('ss-mu', mints=[MU, USDC])
        self.book('ss-mu', ts0='2023-01-01 00:00+00', ts1='2025-09-27 00:00+00')          # 1000 days
        self.assertEqual(db.since_start(0.0, 'ss-mu')['days'], 1000.0)

    def test_the_profiles_of_a_wallet_add_up(self):
        self.profile('ss-mu', mints=[MU, USDC]); self.profile('ss-two', mints=[MU, USDC])
        self.book('ss-mu', usd=50.0, equity=60.0); self.book('ss-two', usd=30.0, equity=31.0)
        s = db.since_start(wallet_id=WALLET)
        self.assertEqual((s['start_usd'], s['equity_usd']), (80.0, 91.0))


class RecordFlowBook(unittest.TestCase):
    def setUp(self):
        reset()

    def test_the_named_book_wins_over_the_context(self):
        saved = dict(db.CONTEXT)
        try:
            db.set_context('sol-usdc', 'sol-lp')
            db.record_flow('2026-10-01T00:00:00Z', 'deposit', 0, 0, 1.0, 1.0, 'F1', 'x', profile='mu-usdc', wallet_id='other')
            db.record_flow('2026-10-01T00:00:00Z', 'deposit', 0, 0, 1.0, 1.0, 'F2', 'x', amounts={})
        finally:
            db.CONTEXT.clear(); db.CONTEXT.update(saved)
        with db.cursor() as cur:
            cur.execute("select signature, profile, wallet_id, amounts from capital_flows order by signature")
            rows = [tuple(r.values()) for r in cur.fetchall()]
        self.assertEqual(rows, [('F1', 'mu-usdc', 'other', None), ('F2', 'sol-usdc', 'sol-lp', {})])


if __name__ == '__main__':
    unittest.main()
