"""audit.run, part by part: notifications on status changes, each input and
fallback, every term of the equity sum, the flows cursor, and the harvest,
payout, position, band-profile, owed and fee-read queries."""
import json
import tempfile
import types
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import audit
import db
import rebalancer
from test_audit import reset, tx, tb, OWNER, PROFIT, POOL, SOL, USDC


class Base(unittest.TestCase):
    def setUp(self):
        reset()
        self.feed = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False); self.feed.close()
        self.notes = []
        self.bal = {'owner': OWNER, 'balanceA': 0.0592, 'balanceB': 0.5, 'sol': 0.0592, 'price': 120.0,
                    'quoteUsd': 1.0, 'nativeSide': 'A', 'walletUsd': 0.0592 * 120 + 0.5}
        self.status = {'positionMint': 'NFT1', 'positionUsd': 240.0, 'rentUsd': 0.0, 'feesAccrued_USD': 0.1}
        self.native = int(0.0592 * 1e9)
        self.accounts = []
        self.sigs = []
        self.txs = {}
        self.prices = {}
        self.harvested = {}
        self.rpc_calls = []
        self.equity(0.0592 * 120 + 0.5 + 240.0 + 0.1)

    def equity(self, e):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values (now(), 'NFT1', 120, true, '1', %s)", (e,))

    def acct(self, mint, amount, dec, lamports=2039280, prog=0):
        self.accounts.append((prog, {'pubkey': f'{mint}-{amount}', 'account': {'lamports': lamports, 'data': {'parsed': {'info': {
            'mint': mint, 'tokenAmount': {'amount': str(amount), 'decimals': dec}}}}}}))

    def fake_rpc(self, url, method, params, tries=4):
        self.rpc_calls.append((method, params))
        if method == 'getTokenAccountsByOwner':
            prog = audit.TOKEN_PROGRAMS.index(params[1]['programId'])
            return {'value': [a for p, a in self.accounts if p == prog]}
        if method == 'getBalance':
            return None if self.native is None else {'value': self.native}
        if method == 'getSignaturesForAddress':
            until = params[1].get('until')
            times = {x['signature']: x['blockTime'] for x in self.sigs}
            return [s for s in self.sigs if not until or s['blockTime'] > times[until]]
        raise AssertionError(method)

    def bot(self):
        return types.SimpleNamespace(
            wallet=lambda pool: dict(self.bal), read_status=lambda *a: (dict(self.status) if self.status else None, None),
            deployable_usd=rebalancer.deployable_usd, pool_tokens=lambda: ((SOL, 'SOL'), (USDC, 'USDC')),
            position_usd=rebalancer.position_usd, FEED=self.feed.name,
            dexes=types.SimpleNamespace(jupiter_prices=lambda mints: {m: self.prices.get(m, 0.0) for m in mints}),
            load=lambda: {'reward_mints_seen': []})

    def run_audit(self):
        cfg = types.SimpleNamespace(RPC='http://rpc.test', POOL=POOL, GAS_RESERVE_SOL=0.05, PROFIT_WALLET=PROFIT)
        fx = types.SimpleNamespace(fetch=lambda url, sig, tries=3: self.txs.get(sig),
                                   harvested=lambda url, sigs, pool, a, b: self.harvested.get(sigs[0]))
        with mock.patch.object(audit, 'rpc', self.fake_rpc), mock.patch.object(audit.time, 'sleep', lambda s: None):
            return audit.run(self.bot(), db, cfg, fx, lambda ev, **kw: self.notes.append((ev, kw)))

    def detail(self, name):
        with db.cursor() as cur:
            cur.execute('select detail from audits where check_name = %s order by id desc limit 1', (name,))
            return cur.fetchone()['detail']


class Notifications(Base):
    def audits(self):
        return [(kw['check'], kw['status'], kw['was']) for ev, kw in self.notes if ev == 'AUDIT']

    def test_every_transition(self):
        # first run: ok checks are silent, a new warning is announced
        self.run_audit()
        first = self.audits()
        self.assertNotIn('gas', [c for c, _, _ in first])
        self.bal['sol'] = 0.01                                                # gas: ok -> fail
        self.notes.clear(); self.run_audit()
        self.assertIn(('gas', 'fail', 'ok'), self.audits())
        self.notes.clear(); self.run_audit()                                  # fail -> fail: silent
        self.assertNotIn('gas', [c for c, _, _ in self.audits()])
        self.bal['sol'] = 0.0592                                              # fail -> ok: announced
        self.notes.clear(); self.run_audit()
        self.assertIn(('gas', 'ok', 'fail'), self.audits())
        self.notes.clear(); self.run_audit()                                  # ok -> ok: silent
        self.assertNotIn('gas', [c for c, _, _ in self.audits()])
        self.assertEqual(db.audit_value('status:gas'), 'ok')

    def test_warn_to_fail_is_announced(self):
        self.bal.update(balanceB=10.0, walletUsd=0.0592 * 120 + 10.0)
        self.run_audit()
        self.assertIn(('idle', 'warn', None), self.audits())
        self.bal.update(balanceB=40.0, walletUsd=0.0592 * 120 + 40.0)
        self.notes.clear(); self.run_audit()
        self.assertIn(('idle', 'fail', 'warn'), self.audits())
        detail = [kw['detail'] for ev, kw in self.notes if ev == 'AUDIT' and kw['check'] == 'idle'][0]
        self.assertAlmostEqual(detail['deployable_usd'], 40.0 + (0.0592 - 0.059) * 120.0)   # plus the SOL above the reserve


class Inputs(Base):
    def test_no_snapshot_uses_the_idle_floor(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate snapshots')
        self.bal.update(balanceB=2.5, walletUsd=0)
        self.assertEqual(self.run_audit()['idle'], 'warn')                   # $2.5 over the $2 floor
        self.assertEqual(self.detail('idle')['limit_usd'], 2.0)

    def test_no_position_is_not_idle_and_has_no_position_mark(self):
        self.status = None
        self.bal.update(balanceB=100.0)
        self.assertEqual(self.run_audit()['idle'], 'ok')
        self.assertEqual(self.detail('equity')['chain_total_usd'], round(0.0592 * 120 + 0.0, 4))

    def test_gas_reads_native_sol_and_none_is_zero(self):
        self.bal['sol'] = None
        self.assertEqual(self.run_audit()['gas'], 'fail')
        self.assertEqual(self.detail('gas')['native_sol'], 0.0)

    def test_without_an_owner_no_accounts_are_read(self):
        self.bal['owner'] = None
        self.acct('MSOL', 0, 9)
        res = self.run_audit()
        self.assertNotIn('getTokenAccountsByOwner', [m for m, _ in self.rpc_calls])
        self.assertEqual(res['empty'], 'ok')


class Equity(Base):
    def test_every_term(self):
        self.acct(USDC, 500_000, 6)                                          # 0.5 USDC: the pool's quote
        self.acct('NFT1', 1, 0, lamports=1_513_840, prog=1)                  # the position NFT: in the position mark
        self.acct('CAKE', 3_272_695, 9)                                      # dust: priced
        self.acct(SOL, 10_000_000, 9)                                        # 0.01 wrapped SOL: uncounted
        self.acct('MSOL', 0, 9, lamports=2_000_000)                          # empty: its rent is uncounted
        self.acct(USDC + 'x', 0, 6, lamports=7)                              # an empty account of another mint
        self.prices = {'CAKE': 2.0}
        res = self.run_audit()
        d = self.detail('equity')
        dust = 3_272_695 / 1e9 * 2.0 + 0.01 * 120.0
        rent = (2_000_000 + 7) / 1e9 * 120.0
        self.assertAlmostEqual(d['uncounted_usd'], round(dust + rent, 4), places=4)
        chain = self.native / 1e9 * 120.0 + 0.5 + 240.0 + 0.1 + dust + rent
        self.assertAlmostEqual(d['chain_total_usd'], round(chain, 4), places=4)
        self.assertEqual(res['equity'], 'ok')
        self.assertAlmostEqual(float(db.audit_value('uncounted_usd')), dust + rent, places=5)

    def test_an_uncounted_asset_the_snapshot_misses_is_caught(self):
        self.acct(USDC, 50_000_000, 6)                                       # 50 USDC the snapshot did not see
        self.assertEqual(self.run_audit()['equity'], 'warn')
        self.assertAlmostEqual(self.detail('equity')['diff_usd'], 49.5, places=3)

    def test_an_unreadable_native_balance_is_a_warning(self):
        self.native = None
        self.assertEqual(self.run_audit()['equity'], 'warn')

    def test_a_pool_without_sol_prices_sol_from_jupiter(self):
        self.prices = {SOL: 150.0}
        self.acct(SOL, 10_000_000, 9)
        with mock.patch.object(self, 'bot', lambda: types.SimpleNamespace(**{**vars(Base.bot(self)),
                                                                             'pool_tokens': lambda: (('JUP', 'JUP'), (USDC, 'USDC'))})):
            self.run_audit()
        self.assertAlmostEqual(self.detail('equity')['uncounted_usd'], 0.01 * 150.0, places=4)


class Flows(Base):
    def test_the_cursor_is_passed_and_moves_to_the_newest(self):
        self.sigs = [{'signature': 'B', 'blockTime': 2}, {'signature': 'A', 'blockTime': 1}]
        self.txs = {'A': tx('A', keys=('SPAM', OWNER), pre=[10**9, 10**9], post=[10**9 - 5001, 10**9 + 1]),
                    'B': tx('B', keys=('SPAM', OWNER), pre=[10**9, 10**9], post=[10**9 - 5001, 10**9 + 1])}
        self.run_audit()
        self.assertEqual(db.audit_value('flows_cursor'), 'B')
        self.assertEqual(self.detail('flows')['counts'], {'dust': 2})
        self.rpc_calls.clear()
        self.assertEqual(self.run_audit()['flows'], 'ok')
        sig_call = [p for m, p in self.rpc_calls if m == 'getSignaturesForAddress'][0]
        self.assertEqual(sig_call[1], {'limit': 200, 'until': 'B'})
        self.assertEqual(self.detail('flows'), {'new': 0})

    def test_an_unreadable_transaction_is_flagged(self):
        self.sigs = [{'signature': 'Q', 'blockTime': 1}]
        self.assertEqual(self.run_audit()['flows'], 'warn')
        self.assertEqual(self.detail('flows')['flagged'][0][1]['note'], 'unreadable')

    def test_a_deposit_is_valued_and_timed(self):
        self.sigs = [{'signature': 'G', 'blockTime': 1790500000}]
        self.txs = {'G': tx('G', keys=('ALICE', OWNER), pre=[5 * 10**9, 10**9], post=[4 * 10**9, 2 * 10**9],
                            pre_tok=[tb(2, OWNER, USDC, 10_000_000)], post_tok=[tb(2, OWNER, USDC, 30_000_000)], block=1790500000)}
        self.run_audit()
        with db.cursor() as cur:
            cur.execute("select ts, kind, sol, usdc, usd, price from capital_flows where signature = 'G'")
            r = dict(cur.fetchone())
        self.assertEqual((r['kind'], float(r['sol']), float(r['usdc'])), ('deposit', 1.0, 20.0))
        self.assertAlmostEqual(float(r['usd']), 1.0 * 120.0 + 20.0); self.assertEqual(r['price'], 120.0)
        self.assertEqual(int(r['ts'].timestamp()), 1790500000)

    def test_known_signatures_are_not_fetched(self):
        with open(self.feed.name, 'w') as fh:
            fh.write(json.dumps({'event': 'SWAP', 'signature': 'K'}) + '\n')
        self.sigs = [{'signature': 'K', 'blockTime': 1}]
        fetched = []
        self.txs = types.SimpleNamespace()
        cfg = types.SimpleNamespace(RPC='http://rpc.test', POOL=POOL, GAS_RESERVE_SOL=0.05, PROFIT_WALLET=PROFIT)
        fx = types.SimpleNamespace(fetch=lambda url, sig, tries=3: fetched.append(sig), harvested=lambda *a: None)
        with mock.patch.object(audit, 'rpc', self.fake_rpc), mock.patch.object(audit.time, 'sleep', lambda s: None):
            res = audit.run(self.bot(), db, cfg, fx, lambda ev, **kw: None)
        self.assertEqual(fetched, []); self.assertEqual(res['flows'], 'ok')


class Ledger(Base):
    def position(self, mint, closed=False, dex='raydium-clmm'):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at, closed_at, dex) values "
                        "(%s, %s, now() - interval '3 hours', " + ("now() - interval '1 hour'" if closed else 'null') + ", %s)",
                        (mint, POOL, dex))

    def test_harvests_take_the_worst_status_and_move_the_cursor(self):
        self.position('NFT1')
        db.record_harvest('NFT1', 0.001, 0.1, 0.22, 'H1')
        db.record_harvest('NFT1', 0.002, 0.2, 0.44, 'H2')
        db.record_harvest('NFT1', 0.0, 0.0, 0.0, 'close:NFT1')               # collected by a close: not a harvest tx
        self.harvested = {'H1': None, 'H2': (0.002, 0.2)}
        self.assertEqual(self.run_audit()['harvests'], 'warn')                # an unreadable one
        d = self.detail('harvests')
        self.assertEqual(d['checked'], 2); self.assertEqual(len(d['problems']), 1)
        with db.cursor() as cur:
            cur.execute("select max(id) m from harvests where signature = 'H2'")
            self.assertEqual(int(db.audit_value('harvest_cursor')), cur.fetchone()['m'])
        db.record_harvest('NFT1', 0.003, 0.3, 0.66, 'H3'); db.record_harvest('NFT1', 0.004, 0.4, 0.88, 'H4')
        self.harvested = {'H3': None, 'H4': (9.0, 9.0)}
        self.assertEqual(self.run_audit()['harvests'], 'fail')                # warn and fail: fail

    def test_payouts_check_only_paid_rows_with_signatures(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind, signature) values "
                        "(now(), 's', 'M', %s, 'USDC', 0.3, 0.3, 'paid', 'P1'),"
                        "(now(), 's', 'M', %s, 'USDC', 0.3, 0.3, 'owed', 'P2'),"
                        "(now(), 's', 'M', %s, 'USDC', 0.3, 0.3, 'paid', null)", (USDC, USDC, USDC))
        self.txs['P1'] = tx('P1', keys=(OWNER, PROFIT), pre_tok=[tb(0, PROFIT, USDC, 0)], post_tok=[tb(0, PROFIT, USDC, 300_000)])
        self.assertEqual(self.run_audit()['payouts'], 'ok')
        self.assertEqual(self.detail('payouts')['checked'], 1)
        self.assertEqual(self.run_audit()['payouts'], 'ok')
        self.assertEqual(self.detail('payouts')['checked'], 0)                # the cursor moved

    def test_positions_compare_only_nft_shaped_accounts(self):
        self.position('NFT1')
        self.acct('NFT1', 1, 0, prog=1)
        self.acct('SOMETOKEN', 1, 6)                                          # one unit of a 6-decimal token: no NFT
        self.acct('BIGNFT', 2, 0)                                             # two units: no NFT
        self.assertEqual(self.run_audit()['positions'], 'ok')

    def test_band_profiles_are_written_only_where_missing(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into risk_profile (ts, pool, price) values (now() - interval '5 hours', %s, 120)", (POOL,))
        self.position('OPEN')                                                 # still open: not yet
        self.position('DONE', closed=True)
        db.record_band_profile('DONE', 'rebalance', 'x')                      # already final: left alone
        self.position('MISSING', closed=True)
        self.run_audit()
        self.assertEqual(self.detail('band_profile')['written'], ['MISSING'])
        with db.cursor() as cur:
            cur.execute("select mint, exit_reason from band_profile order by mint")
            self.assertEqual([(r['mint'], r['exit_reason']) for r in cur.fetchall()],
                             [('DONE', 'x'), ('MISSING', 'written by the audit')])

    def test_bands_before_the_risk_record_are_not_touched(self):
        self.position('ANCIENT', closed=True)
        with db.cursor(commit=True) as cur:
            cur.execute("insert into risk_profile (ts, pool, price) values (now() - interval '30 minutes', %s, 120)", (POOL,))
        self.run_audit()
        self.assertEqual(self.detail('band_profile')['written'], [])

    def test_owed_only_after_three_days(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind) values "
                        "(now() - interval '2 days', 's', 'M', 'U', 'USDC', 1, 1, 'owed'),"
                        "(now() - interval '4 days', 's', 'M', 'U', 'USDC', 2, 2, 'owed')")
        self.assertEqual(self.run_audit()['owed'], 'warn')
        self.assertEqual([float(r['usd']) for r in self.detail('owed')['rows']], [2.0])

    def test_fee_reads_count_the_last_day(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into events (ts, kind, detail) values (now() - interval '2 hours', 'fee_read_rejected', 'x'),"
                        "(now() - interval '2 days', 'fee_read_rejected', 'x'), (now(), 'other', 'x')")
        self.assertEqual(self.run_audit()['fee_reads'], 'warn')
        self.assertEqual(self.detail('fee_reads'), {'rejected_24h': 1})


if __name__ == '__main__':
    unittest.main()
