"""ops/add_profile.py: a profile, its wallet and its swing, from a template.

Covered: build() refuses every argument it must (wallet id, address, secret
env, chain, a pool that quotes no stablecoin, venues, the swing's pools and
calendar, signer env outside the allowlist), copies the template's tuning
but never its identity, and main() writes the three rows to the test
database in one transaction, or nothing on a dry run."""
import argparse
import importlib.util
import pathlib
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import db

spec = importlib.util.spec_from_file_location('add_profile', pathlib.Path(__file__).parent.parent / 'ops' / 'add_profile.py')
ap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ap)

SOL = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
DJT = 'DJTu7vi8norVzdVAffgvb39VP7wjKeTsgaMBJrzfxvoF'
SOL_POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'
DJT_POOL = '7gkB2D1SqhUYgKrSpDU5cma4tK9efijHouYituABdJcG'
ADDR = 'FogqBWLC4y94csrniURTbGrx7ff7jFa4e2qp1GsgyAmM'
REC = {'token_a': {'address': SOL, 'symbol': 'SOL'}, 'token_b': {'address': USDC, 'symbol': 'USDC'}}
TEMPLATE = {'id': 1, 'name': 'tpl', 'chain': 'solana', 'pool': 'X', 'capital_usd': 216, 'bands': [1.03],
            'regime_threshold': 0.2, 'profit_wallet': '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h',
            'payout_mint': USDC, 'payout_enabled': True, 'gas_reserve_sol': 0.05, 'allow_swap': False, 'wallet_id': 'sol-lp',
            'residual_owner': True, 'enabled': True, 'signer_env': {'X': '1'}}


def args(**kw):
    a = dict(profile='ap-swing', template='tpl', wallet='ap-lp2', chain='solana', address=ADDR,
             secret_env='LPBOT_SOL2_KEY_PATH', label=None, dex='raydium-clmm', pool=SOL_POOL,
             execute_dexes='raydium-clmm,orca', signer_env=['LPBOT_ORCA_ADAPTIVE=1'], deposit_mint='USDC',
             swing_open=f'orca {DJT_POOL}', swing_closed=f'raydium-clmm {SOL_POOL}', calendar='nyse', lead_s=300,
             apply=False)
    a.update(kw)
    return argparse.Namespace(**a)


class Build(unittest.TestCase):
    def test_the_swing_on_a_new_wallet(self):
        w, row, sw = ap.build(args(), TEMPLATE, REC, None, 0)
        self.assertEqual(w, ('ap-lp2', 'solana', ADDR, 'LPBOT_SOL2_KEY_PATH', 'ap-lp2'))
        self.assertEqual((row['name'], row['wallet_id'], row['pool'], row['dex'], row['pair_label']),
                         ('ap-swing', 'ap-lp2', SOL_POOL, 'raydium-clmm', 'SOL/USDC'))
        self.assertEqual((row['mints'], row['deposit_mint'], row['residual_owner'], row['allow_swap']),
                         ([SOL, USDC], USDC, True, True))
        self.assertEqual(row['execute_dexes'], ['raydium-clmm', 'orca'])
        self.assertEqual(row['signer_env'], {'LPBOT_ORCA_ADAPTIVE': '1'})
        self.assertEqual(sw, {'profile': 'ap-swing', 'open_dex': 'orca', 'open_pool': DJT_POOL,
                              'closed_dex': 'raydium-clmm', 'closed_pool': SOL_POOL, 'calendar': 'nyse', 'lead_s': 300})

    def test_tuning_is_copied_identity_never(self):
        _, row, _ = ap.build(args(), TEMPLATE, REC, None, 0)
        for k in ('capital_usd', 'bands', 'regime_threshold', 'profit_wallet', 'payout_mint', 'payout_enabled',
                  'gas_reserve_sol'):
            self.assertEqual(row[k], TEMPLATE[k], k)
        self.assertNotIn('id', row); self.assertNotIn('chain', row)
        self.assertEqual((row['active'], row['enabled'], row['pool_pinned']), (False, True, True))

    def test_without_a_swing_no_row_and_no_swap(self):
        _, row, sw = ap.build(args(swing_open=None, swing_closed=None, signer_env=None, deposit_mint=None),
                              TEMPLATE, REC, None, 0)
        self.assertIsNone(sw)
        self.assertEqual((row['allow_swap'], row['signer_env'], row['deposit_mint']), (False, None, SOL))

    def test_an_existing_wallet_shares_and_must_match(self):
        wallet = {'id': 'ap-lp2', 'chain': 'solana', 'address': ADDR}
        w, row, _ = ap.build(args(), TEMPLATE, REC, wallet, 2)
        self.assertIsNone(w)
        self.assertFalse(row['residual_owner'])
        with self.assertRaisesRegex(ap.Refused, 'exists with address'):
            ap.build(args(address='83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'), TEMPLATE, REC, wallet, 2)

    def test_refusals(self):
        cases = [
            (dict(wallet='Bad_ID'), None, 'new wallet'),
            (dict(address='not an address'), None, 'new wallet'),
            (dict(secret_env='lower'), None, 'new wallet'),
            (dict(chain='base'), None, 'template'),
            (dict(execute_dexes='orca'), None, 'execute-dexes'),
            (dict(pool='0x' + 'ab' * 20), None, 'not a solana address'),
            (dict(execute_dexes='raydium-clmm,nowhere'), None, 'execute-dexes'),
            (dict(swing_closed=None), None, 'both'),
            (dict(swing_open=f'orca {SOL_POOL}'), None, 'differ'),
            (dict(swing_open=f'orca {DJT_POOL}', swing_closed=f'orca {DJT_POOL}'), None, 'differ'),
            (dict(swing_open=f'orca {DJT_POOL}', swing_closed=f'raydium-clmm {DJT_POOL}x'), None, 'pool is'),
            (dict(swing_open=f'jupiter {DJT_POOL}'), None, 'pool is'),
            (dict(execute_dexes='raydium-clmm'), None, 'swing venue'),
            (dict(calendar='lse'), None, 'calendar'),
            (dict(signer_env=['LPBOT_ANYTHING=1']), None, 'signer'),
            (dict(signer_env=['NOEQUALS']), None, 'KEY=VALUE'),
        ]
        for kw, _, why in cases:
            with self.assertRaisesRegex(ap.Refused, why, msg=kw):
                ap.build(args(**kw), TEMPLATE, REC, None, 0)
        no_stable = {'token_a': {'address': SOL, 'symbol': 'SOL'}, 'token_b': {'address': DJT, 'symbol': 'DJT'}}
        with self.assertRaisesRegex(ap.Refused, 'stablecoin'):
            ap.build(args(), TEMPLATE, no_stable, None, 0)

    def test_a_deposit_mint_by_symbol_or_address(self):
        self.assertEqual(ap.build(args(deposit_mint='sol'), TEMPLATE, REC, None, 0)[1]['deposit_mint'], SOL)
        self.assertEqual(ap.build(args(deposit_mint=USDC), TEMPLATE, REC, None, 0)[1]['deposit_mint'], USDC)

    def test_base_addresses_are_lower_case(self):
        rec = {'token_a': {'address': '0x4200000000000000000000000000000000000006', 'symbol': 'WETH'},
               'token_b': {'address': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913', 'symbol': 'USDC'}}
        _, row, _ = ap.build(args(chain='base', address='0x' + 'ab' * 20, dex='aerodrome-slipstream',
                                  pool='0x3fe04a59ebd38cf06080a6f60a98d124eb59392a', execute_dexes=None,
                                  swing_open=None, swing_closed=None, signer_env=None, deposit_mint=None),
                             dict(TEMPLATE, chain='base'), rec, None, 0)
        self.assertEqual(row['mints'], ['0x4200000000000000000000000000000000000006',
                                        '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'])
        self.assertEqual(row['deposit_mint'], '0x4200000000000000000000000000000000000006')

    def test_more_refusals_and_the_order_of_checks(self):
        plain = dict(swing_open=None, swing_closed=None, signer_env=None)
        for kw, why in ((dict(plain, execute_dexes='orca'), 'execute-dexes'),
                        (dict(plain, execute_dexes='raydium-clmm,nowhere'), 'execute-dexes'),
                        (dict(address=None), 'new wallet'), (dict(secret_env=None), 'new wallet'),
                        (dict(swing_open=f'orca {DJT_POOL}', swing_closed=f'orca {ADDR}'), 'starts on one'),
                        (dict(swing_open=f'orca {DJT_POOL} x'), 'pool is'), (dict(swing_open='orca'), 'pool is')):
            with self.assertRaisesRegex(ap.Refused, why, msg=kw):
                ap.build(args(**kw), TEMPLATE, REC, None, 0)
        with self.assertRaisesRegex(ap.Refused, 'unknown chain'):
            ap.build(args(), dict(TEMPLATE, chain='evm2'), REC, {'chain': 'evm2', 'address': ADDR}, 1)
        with self.assertRaises(ap.Refused):
            ap.pool_spec(None, 'solana')
        with self.assertRaisesRegex(ap.Refused, 'signer'):                    # K=V=W: the value is '1=2'
            ap.signer_env(['LPBOT_ORCA_ADAPTIVE=1=2'])

    def test_the_wallet_rows_chain_wins_over_the_flag(self):
        base = {'id': 'ap-lp2', 'chain': 'base', 'address': '0x' + 'ab' * 20}
        rec = {'token_a': {'address': '0x4200000000000000000000000000000000000006', 'symbol': 'WETH'},
               'token_b': {'address': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913', 'symbol': 'USDC'}}
        w, row, _ = ap.build(args(chain='solana', address=None, dex='aerodrome-slipstream',
                                  pool='0x3fe04a59ebd38cf06080a6f60a98d124eb59392a', execute_dexes=None,
                                  swing_open=None, swing_closed=None, signer_env=None, deposit_mint=None),
                             dict(TEMPLATE, chain='base'), rec, base, 1)
        self.assertIsNone(w)
        self.assertEqual(row['execute_dexes'], ['aerodrome-slipstream'])

    def test_an_existing_wallet_needs_no_address(self):
        w, row, _ = ap.build(args(address=None), TEMPLATE, REC, {'chain': 'solana', 'address': ADDR}, 1)
        self.assertEqual((w, row['wallet_id']), (None, 'ap-lp2'))

    def test_the_label_and_the_switches(self):
        w, row, _ = ap.build(args(label='Swing wallet'), TEMPLATE, REC, None, 0)
        self.assertEqual(w[4], 'Swing wallet')
        self.assertEqual((row['pool_pinned'], row['regime_enabled'], row['rebalance_swap']), (True, True, True))

    def test_the_swing_may_start_on_its_open_pool(self):
        _, row, sw = ap.build(args(pool=DJT_POOL, dex='orca'), TEMPLATE, REC, None, 0)
        self.assertEqual((row['pool'], sw['open_pool']), (DJT_POOL, DJT_POOL))

    def test_the_default_deposit_is_the_side_that_is_not_stable(self):
        rev = {'token_a': {'address': USDC, 'symbol': 'USDC'}, 'token_b': {'address': DJT, 'symbol': 'DJT'}}
        _, row, _ = ap.build(args(deposit_mint=None), TEMPLATE, rev, None, 0)
        self.assertEqual(row['deposit_mint'], DJT)

    def test_the_drop_in_pins_both_pools(self):
        text = ap.drop_in(ap.build(args(), TEMPLATE, REC, None, 0)[2])
        self.assertIn('lp-bot@ap-swing.service.d', text)
        self.assertIn(f'Environment=LPBOT_SWING_POOLS={DJT_POOL} {SOL_POOL}', text)

    def test_venues_are_the_signers_without_the_routes(self):
        self.assertIn('orca', ap.VENUES); self.assertIn('aerodrome-slipstream', ap.VENUES)
        for route in ('jupiter', 'orca-swap', 'payout', 'janitor'):
            self.assertNotIn(route, ap.VENUES)


class Main(unittest.TestCase):
    """main() against the test database."""

    def cleanup(self):
        with db.cursor(commit=True) as cur:
            cur.execute("delete from swing where profile = 'ap-swing'")
            cur.execute("delete from config where name in ('ap-swing', 'ap-tpl')")
            cur.execute("delete from wallets where id in ('ap-lp2', 'ap-lp1')")

    def setUp(self):
        self.cleanup(); self.addCleanup(self.cleanup)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into wallets (id, chain, address, secret_env) values ('ap-lp1', 'solana', "
                        "'83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f', 'WALLET_SECRET_PATH')")
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, wallet_id, enabled) "
                        "values ('ap-tpl', %s, 'SOL/USDC', 216, 600, 'ap-lp1', true)", (SOL_POOL,))
        p = mock.patch.object(ap.dexes, 'pool', lambda dex, pool: REC)
        p.start(); self.addCleanup(p.stop)

    def argv(self, *extra):
        return ['--profile', 'ap-swing', '--template', 'ap-tpl', '--wallet', 'ap-lp2', '--address', ADDR,
                '--secret-env', 'LPBOT_SOL2_KEY_PATH', '--dex', 'raydium-clmm', '--pool', SOL_POOL,
                '--execute-dexes', 'raydium-clmm,orca', '--signer-env', 'LPBOT_ORCA_ADAPTIVE=1',
                '--deposit-mint', 'USDC', '--swing-open', f'orca {DJT_POOL}',
                '--swing-closed', f'raydium-clmm {SOL_POOL}', *extra]

    def rows(self):
        with db.cursor() as cur:
            cur.execute("select (select count(*) from wallets where id = 'ap-lp2') w, "
                        "(select count(*) from config where name = 'ap-swing') c, "
                        "(select count(*) from swing where profile = 'ap-swing') s")
            return tuple(cur.fetchone().values())

    def test_a_dry_run_writes_nothing(self):
        self.assertEqual(ap.main(self.argv()), 0)
        self.assertEqual(self.rows(), (0, 0, 0))

    def test_apply_writes_the_three_rows_and_a_second_run_refuses(self):
        self.assertEqual(ap.main(self.argv('--apply')), 0)
        self.assertEqual(self.rows(), (1, 1, 1))
        with db.cursor() as cur:
            cur.execute("select capital_usd, max_usd, signer_env, allow_swap from config where name = 'ap-swing'")
            r = cur.fetchone()
        self.assertEqual((float(r['capital_usd']), float(r['max_usd']), r['signer_env'], r['allow_swap']),
                         (216.0, 600.0, {'LPBOT_ORCA_ADAPTIVE': '1'}, True))
        with self.assertRaises(SystemExit):
            ap.main(self.argv('--apply'))

    def test_the_output_names_the_wallet_and_the_drop_in(self):
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ap.main(self.argv())
        text = out.getvalue()
        self.assertIn(f"wallet: ('ap-lp2', 'solana', '{ADDR}'", text)
        self.assertIn(f'Environment=LPBOT_SWING_POOLS={DJT_POOL} {SOL_POOL}', text)
        out = io.StringIO()
        argv = self.argv()
        for flag in ('--swing-open', '--swing-closed', '--address'):
            i = argv.index(flag); del argv[i:i + 2]
        argv[argv.index('ap-lp2')] = 'ap-lp1'
        with contextlib.redirect_stdout(out):
            ap.main(argv)
        self.assertIn('wallet: ap-lp1 (exists)', out.getvalue())
        self.assertNotIn('LPBOT_SWING_POOLS', out.getvalue())

    def test_no_signer_env_is_sql_null(self):
        argv = self.argv('--apply')
        for flag in ('--swing-open', '--swing-closed', '--signer-env'):
            i = argv.index(flag); del argv[i:i + 2]
        ap.main(argv)
        with db.cursor() as cur:
            cur.execute("select signer_env is null n, allow_swap from config where name = 'ap-swing'")
            r = cur.fetchone()
        self.assertEqual((r['n'], r['allow_swap']), (True, False))
        self.assertEqual(self.rows(), (1, 1, 0))

    def test_a_refused_build_writes_nothing(self):
        with self.assertRaises(SystemExit):
            ap.main(self.argv('--calendar', 'lse', '--apply'))
        self.assertEqual(self.rows(), (0, 0, 0))

    def test_an_unknown_template_writes_nothing(self):
        argv = self.argv('--apply')
        argv[argv.index('ap-tpl')] = 'ap-none'
        with self.assertRaises(SystemExit):
            ap.main(argv)
        self.assertEqual(self.rows(), (0, 0, 0))


if __name__ == '__main__':
    unittest.main()
