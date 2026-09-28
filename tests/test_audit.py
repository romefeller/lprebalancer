"""Audits (audit.py), the capital baseline (db.since_start), and the janitor.

Every check is tested at its thresholds with the inputs it is given; the
runner is driven end to end on the test database with a fake bot, a fake RPC
and scripted transactions, so each record, cursor and notification is seen.
"""
import datetime as dt
import json
import tempfile
import types
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures
import audit
import db
import rebalancer

OWNER = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
POOL = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj'
SOL, USDC = audit.NATIVE, audit.USDC
RAYDIUM = 'CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK'


def reset():
    _fixtures.reset_ledger()
    with db.cursor(commit=True) as cur:
        cur.execute('truncate payouts, audits, audit_state, capital_flows, risk_profile')


# --- a transaction in the jsonParsed shape ----------------------------------------------

def tx(sig='S1', keys=(OWNER,), pre=None, post=None, pre_tok=(), post_tok=(), fee=5000, err=None, block=1790000000):
    keys = list(keys)
    pre = pre or [1_000_000_000] * len(keys)
    post = post or list(pre)
    return {'blockTime': block, 'transaction': {'signatures': [sig], 'message': {'accountKeys': [{'pubkey': k} for k in keys]}},
            'meta': {'err': err, 'fee': fee, 'preBalances': pre, 'postBalances': post,
                     'preTokenBalances': list(pre_tok), 'postTokenBalances': list(post_tok)}}


def tb(i, owner, mint, amount, dec=6):
    return {'accountIndex': i, 'owner': owner, 'mint': mint, 'uiTokenAmount': {'amount': str(amount), 'decimals': dec}}


class Classify(unittest.TestCase):
    def test_known_and_failed(self):
        self.assertEqual(audit.classify_tx(tx('K'), OWNER, {'K'})[0], 'known')
        self.assertEqual(audit.classify_tx(tx(err={'x': 1}), OWNER, set())[0], 'failed')

    def test_dust_spam(self):
        t = tx(keys=('SPAMMER', OWNER), pre=[10**9, 10**9], post=[10**9 - 5001, 10**9 + 1], fee=5000)
        self.assertEqual(audit.classify_tx(t, OWNER, set())[0], 'dust')
        big = tx(keys=('SPAMMER', OWNER), pre=[10**9, 10**9], post=[10**9 - 20_001, 10**9 + 20_001])
        self.assertEqual(audit.classify_tx(big, OWNER, set())[0], 'deposit')    # more than dust: a deposit

    def test_a_bot_operation_the_ledger_missed(self):
        t = tx(keys=(OWNER, RAYDIUM), pre=[10**9, 1], post=[10**9 - 5000 + 7_600_000, 1])
        kind, d = audit.classify_tx(t, OWNER, set())
        self.assertEqual(kind, 'bot'); self.assertEqual(d['programs'], ['raydium'])
        self.assertAlmostEqual(d['sol'], 0.0076)                                  # the fee is added back for the signer

    def test_deposits_of_sol_and_usdc(self):
        t = tx(keys=('ALICE', OWNER), pre=[5 * 10**9, 10**9], post=[4 * 10**9, 2 * 10**9])
        kind, d = audit.classify_tx(t, OWNER, set())
        self.assertEqual(kind, 'deposit'); self.assertAlmostEqual(d['sol'], 1.0)
        u = tx(keys=('ALICE', OWNER), pre_tok=[tb(0, OWNER, USDC, 0)], post_tok=[tb(0, OWNER, USDC, 50_000_000)])
        kind, d = audit.classify_tx(u, OWNER, set())
        self.assertEqual(kind, 'deposit'); self.assertEqual(d['usdc'], 50.0)

    def test_a_withdrawal(self):
        t = tx(keys=(OWNER, 'BOB'), pre=[2 * 10**9, 0], post=[10**9 - 5000, 10**9])
        kind, d = audit.classify_tx(t, OWNER, set())
        self.assertEqual(kind, 'withdrawal'); self.assertAlmostEqual(d['sol'], -1.0)

    def test_wrapped_sol_counts_as_sol_and_other_tokens_are_named(self):
        t = tx(keys=('ALICE', OWNER), pre_tok=[tb(0, OWNER, SOL, 0, 9), tb(1, OWNER, 'CAKE', 0, 9)],
               post_tok=[tb(0, OWNER, SOL, 2 * 10**9, 9), tb(1, OWNER, 'CAKE', 5, 9)])
        kind, d = audit.classify_tx(t, OWNER, set())
        self.assertEqual(kind, 'deposit'); self.assertAlmostEqual(d['sol'], 2.0); self.assertEqual(d['other_tokens'], {'CAKE': 5})

    def test_anything_else_is_other(self):
        t = tx(keys=('ALICE', OWNER), pre_tok=[tb(0, OWNER, 'CAKE', 0, 9)], post_tok=[tb(0, OWNER, 'CAKE', 5, 9)])
        self.assertEqual(audit.classify_tx(t, OWNER, set())[0], 'other')         # an airdrop of some token
        noop = tx(keys=(OWNER,), pre=[10**9], post=[10**9 - 5000])
        self.assertEqual(audit.classify_tx(noop, OWNER, set())[0], 'other')      # our own tx that moved nothing

    def test_a_tx_without_the_owner_is_not_a_flow(self):
        t = tx(keys=('ALICE', 'BOB'), pre=[10**9, 0], post=[0, 10**9])
        self.assertEqual(audit.classify_tx(t, OWNER, set())[0], 'dust')

    def test_check_flows_counts_and_flags(self):
        st_, d = audit.check_flows([('known', {}), ('dust', {}), ('dust', {})])
        self.assertEqual((st_, d['counts']), ('ok', {'known': 1, 'dust': 2}))
        for k in ('deposit', 'withdrawal', 'bot', 'other'):
            self.assertEqual(audit.check_flows([('known', {}), (k, {'x': 1})])[0], 'warn', k)
        many = [('bot', {'i': i}) for i in range(15)]
        self.assertEqual(len(audit.check_flows(many)[1]['flagged']), 10)


class Checks(unittest.TestCase):
    def test_idle(self):
        self.assertEqual(audit.check_idle(50.0, 240.0, False)[0], 'ok')
        self.assertEqual(audit.check_idle(4.0, 240.0, True)[0], 'ok')          # under 2% of 240 = 4.8
        self.assertEqual(audit.check_idle(4.81, 240.0, True)[0], 'warn')
        self.assertEqual(audit.check_idle(1.9, 50.0, True)[0], 'ok')           # the $2 floor
        self.assertEqual(audit.check_idle(2.1, 50.0, True)[0], 'warn')
        self.assertEqual(audit.check_idle(24.0, 240.0, True)[0], 'warn')        # exactly 10%: not yet a failure
        self.assertEqual(audit.check_idle(24.01, 240.0, True)[0], 'fail')
        self.assertEqual(audit.check_idle(3.0, 0.0, True)[0], 'warn')           # no equity figure: the floor only

    def test_gas(self):
        self.assertEqual(audit.check_gas(0.049, 0.05)[0], 'fail')
        self.assertEqual(audit.check_gas(0.05, 0.05)[0], 'ok')

    def test_equity(self):
        self.assertEqual(audit.check_equity(242.0, 241.5, 0.85)[0], 'ok')       # 242 - 0.85 - 241.5 = -0.35
        self.assertEqual(audit.check_equity(243.4, 241.5, 0.85)[0], 'warn')     # +1.05
        self.assertEqual(audit.check_equity(240.0, 241.5, 0.0)[0], 'warn')      # -1.5
        self.assertEqual(audit.check_equity(242.5, 241.5, 0.0)[0], 'ok')        # exactly 1.0
        self.assertEqual(audit.check_equity(242.0, None, 0.0)[0], 'warn')

    def test_harvest(self):
        self.assertEqual(audit.check_harvest(0.000771773, 0.086267, (0.000771773, 0.086267))[0], 'ok')
        self.assertEqual(audit.check_harvest(23.156854902, 3403.609163, (0.000771773, 0.086267))[0], 'fail')
        self.assertEqual(audit.check_harvest(0.1, 0.2, None)[0], 'warn')
        # booked from the same transaction: equal to float noise, on both sides
        self.assertEqual(audit.check_harvest(1.0, 2.0, (1.0 + 0.9e-9, 2.0))[0], 'ok')
        self.assertEqual(audit.check_harvest(1.0, 2.0, (1.0 + 1.1e-9, 2.0))[0], 'fail')
        self.assertEqual(audit.check_harvest(1.0, 2.0, (1.0, 2.0 - 0.9e-9))[0], 'ok')
        self.assertEqual(audit.check_harvest(1.0, 2.0, (1.0, 2.0 - 1.1e-9))[0], 'fail')
        self.assertEqual(audit.check_harvest(1.0, 2.0, (1.0 - 1.1e-9, 2.0))[0], 'fail')

    def test_payout_received(self):
        t = tx(keys=(OWNER, PROFIT), pre_tok=[tb(0, PROFIT, USDC, 1_000_000)], post_tok=[tb(0, PROFIT, USDC, 1_122_149)])
        self.assertTrue(audit.payout_received(t, PROFIT, USDC, 0.122149))
        self.assertFalse(audit.payout_received(t, PROFIT, USDC, 0.2))
        self.assertFalse(audit.payout_received(t, 'SOMEONE', USDC, 0.122149))
        self.assertFalse(audit.payout_received(dict(t, meta=dict(t['meta'], err={'x': 1})), PROFIT, USDC, 0.122149))
        self.assertFalse(audit.payout_received(None, PROFIT, USDC, 0.1))
        new_ata = tx(keys=(OWNER, PROFIT), post_tok=[tb(0, PROFIT, USDC, 500_000)])
        self.assertTrue(audit.payout_received(new_ata, PROFIT, USDC, 0.5))

    def test_positions(self):
        self.assertEqual(audit.check_positions(['M'], ['M'], ['raydium-clmm'])[0], 'ok')
        self.assertEqual(audit.check_positions([], [])[0], 'ok')
        st_, d = audit.check_positions(['M'], [], ['raydium-clmm'])
        self.assertEqual((st_, d['missing_on_chain']), ('fail', ['M']))
        st_, d = audit.check_positions([], ['X'])
        self.assertEqual((st_, d['orphans']), ('fail', ['X']))
        self.assertEqual(audit.check_positions(['D'], [], ['meteora-dlmm'])[0], 'ok')   # a DLMM position is no NFT
        self.assertEqual(audit.check_positions(['A', 'B'], ['A', 'B'], ['orca', 'orca'])[0], 'fail')

    def test_small_checks(self):
        self.assertEqual(audit.check_owed([])[0], 'ok'); self.assertEqual(audit.check_owed([{'id': 1}])[0], 'warn')
        self.assertEqual(audit.check_empty(0, 0)[0], 'ok')
        st_, d = audit.check_empty(7_055_440, 4)
        self.assertEqual((st_, d['reclaimable_sol']), ('warn', 0.00705544))
        self.assertEqual(audit.check_fee_reads(0)[0], 'ok'); self.assertEqual(audit.check_fee_reads(2)[0], 'warn')

    @settings(max_examples=200, deadline=None)
    @given(dep=st.floats(0, 500), eq=st.floats(0, 1000), more=st.floats(0, 100))
    def test_idle_is_monotone(self, dep, eq, more):
        order = ('ok', 'warn', 'fail')
        a = order.index(audit.check_idle(dep, eq, True)[0])
        b = order.index(audit.check_idle(dep + more, eq, True)[0])
        self.assertLessEqual(a, b)


# --- the capital baseline and flows ---------------------------------------------------

class SinceStart(unittest.TestCase):
    def setUp(self):
        reset()
        with db.cursor(commit=True) as cur:
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price) values "
                        "('2026-09-22 19:56:24+00', 'baseline', 2.102865244, 0, 248.096041, 117.98)")
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values "
                        "('2026-09-28 13:00+00', 'M', 119.2097, true, '1', 241.5048)")
            cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind) values "
                        "('2026-09-27 12:00+00', 'sol-usdc', 'M', 'USDC', 'USDC', 2.2233, 2.2233, 'paid'),"
                        "('2026-09-27 12:00+00', 'sol-usdc', 'M', 'USDC', 'USDC', 0.5, 0.5, 'reinvested'),"
                        "('2026-09-21 12:00+00', 'sol-usdc', 'M', 'USDC', 'USDC', 9.0, 9.0, 'paid')")

    def test_the_corrected_book(self):
        s = db.since_start(0.85)
        self.assertAlmostEqual(s['value_usd'], 241.5048 + 0.85 + 2.2233, places=4)   # the payout before the start is not counted
        self.assertAlmostEqual(s['profit_usd'], 241.5048 + 0.85 + 2.2233 - 248.096041, places=3)
        self.assertAlmostEqual(s['hold_start_assets_usd'], 2.102865244 * 119.2097, places=3)
        self.assertAlmostEqual(s['hold_50_50_usd'], 248.096041 * (0.5 + 0.5 * 119.2097 / 117.98), places=3)
        self.assertAlmostEqual(s['vs_hold_start_assets_usd'], s['value_usd'] - s['hold_start_assets_usd'], places=3)
        self.assertEqual(s['start_sol'], 2.102865)

    def test_a_deposit_raises_the_start_and_the_hold(self):
        db.record_flow('2026-09-25 00:00+00', 'deposit', 0.5, 20.0, 80.0, 120.0, 'DEP1', 'test')
        s = db.since_start(0.0)
        self.assertAlmostEqual(s['start_usd'], 248.096041 + 80.0, places=3)
        self.assertAlmostEqual(s['hold_start_assets_usd'], (2.102865244 + 0.5) * 119.2097 + 20.0, places=3)
        self.assertAlmostEqual(s['hold_50_50_usd'], 248.096041 * (0.5 + 0.5 * 119.2097 / 117.98) + 80.0, places=3)

    def test_a_withdrawal_lowers_them(self):
        db.record_flow('2026-09-25 00:00+00', 'withdrawal', 0.1, 0.0, 12.0, 120.0, 'WD1', 'test')
        s = db.since_start(0.0)
        self.assertAlmostEqual(s['start_usd'], 248.096041 - 12.0, places=3)
        self.assertAlmostEqual(s['hold_start_assets_usd'], (2.102865244 - 0.1) * 119.2097, places=3)

    def test_a_flow_is_recorded_once(self):
        self.assertTrue(db.record_flow('2026-09-25 00:00+00', 'deposit', 1, 0, 120, 120, 'SAME', 'a'))
        self.assertFalse(db.record_flow('2026-09-25 00:00+00', 'deposit', 1, 0, 120, 120, 'SAME', 'b'))
        with db.cursor() as cur:
            cur.execute("select count(*) n from capital_flows where signature = 'SAME'")
            self.assertEqual(cur.fetchone()['n'], 1)
        with self.assertRaises(ValueError):
            db.record_flow('2026-09-25 00:00+00', 'baseline', 1, 0, 1, 1, 'X', 'x')

    def test_no_baseline_no_answer(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate capital_flows')
        self.assertIsNone(db.since_start())

    def test_the_book_carries_it_with_the_uncounted_value(self):
        db.set_audit_value('uncounted_usd', 0.85)
        s = db.stats()['since_start']
        self.assertAlmostEqual(s['uncounted_usd'], 0.85)
        with mock.patch.object(db, 'since_start', side_effect=RuntimeError('x')):
            self.assertIsNone(db._since_start_or_none())

    def test_audit_state_and_records(self):
        self.assertIsNone(db.audit_value('k'))
        db.set_audit_value('k', 1); db.set_audit_value('k', 2)
        self.assertEqual(db.audit_value('k'), '2')
        db.record_audit('r1', 'idle', 'warn', {'a': 1})
        with db.cursor() as cur:
            cur.execute('select run_id, check_name, status, detail from audits')
            self.assertEqual([dict(r) for r in cur.fetchall()], [{'run_id': 'r1', 'check_name': 'idle', 'status': 'warn', 'detail': {'a': 1}}])
        with self.assertRaises(ValueError):
            db.record_audit('r', 'x', 'bad', {})


# --- the runner, end to end ------------------------------------------------------------

class Runner(unittest.TestCase):
    def setUp(self):
        reset()
        self.feed = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)
        self.feed.write(json.dumps({'event': 'SWAP', 'signature': 'SWAPSIG'}) + '\n'); self.feed.close()
        self.notes = []
        self.bal = {'owner': OWNER, 'balanceA': 0.0592, 'balanceB': 0.5, 'sol': 0.0592, 'price': 120.0,
                    'quoteUsd': 1.0, 'nativeSide': 'A', 'walletUsd': 0.0592 * 120 + 0.5}
        self.status = {'positionMint': 'NFT1', 'positionUsd': 240.0, 'rentUsd': 0.0, 'feesAccrued_USD': 0.1}
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at, dex) values ('NFT1', %s, now() - interval '1 hour', 'raydium-clmm')", (POOL,))
            cur.execute("insert into snapshots (ts, mint, price, in_range, liquidity, equity_usd) values (now(), 'NFT1', 120, true, '1', %s)",
                        (0.0592 * 120 + 0.5 + 240.0 + 0.1,))
        self.accounts = [
            {'pubkey': 'U', 'account': {'lamports': 2039280, 'data': {'parsed': {'info': {'mint': USDC, 'tokenAmount': {'amount': '500000', 'decimals': 6}}}}}},
            {'pubkey': 'N', 'account': {'lamports': 1513840, 'data': {'parsed': {'info': {'mint': 'NFT1', 'tokenAmount': {'amount': '1', 'decimals': 0}}}}}},
            {'pubkey': 'E', 'account': {'lamports': 2039280, 'data': {'parsed': {'info': {'mint': 'MSOL', 'tokenAmount': {'amount': '0', 'decimals': 9}}}}}},
        ]
        self.sigs = [{'signature': 'SWAPSIG', 'blockTime': 1}, {'signature': 'DUST', 'blockTime': 2}, {'signature': 'GIFT', 'blockTime': 3}]
        self.txs = {'DUST': tx('DUST', keys=('SPAM', OWNER), pre=[10**9, 10**9], post=[10**9 - 5001, 10**9 + 1]),
                    'GIFT': tx('GIFT', keys=('ALICE', OWNER), pre=[5 * 10**9, 10**9], post=[4 * 10**9, 2 * 10**9], block=1790500000)}

    def fake_rpc(self, url, method, params, tries=4):
        if method == 'getTokenAccountsByOwner':
            return {'value': self.accounts if params[1]['programId'] == audit.TOKEN_PROGRAMS[0] else []}
        if method == 'getBalance':
            return {'value': int(0.0592 * 1e9)}
        if method == 'getSignaturesForAddress':
            until = params[1].get('until')
            return [s for s in self.sigs if not until or s['blockTime'] > {x['signature']: x['blockTime'] for x in self.sigs}[until]]
        raise AssertionError(method)

    def bot(self):
        return types.SimpleNamespace(
            wallet=lambda pool: dict(self.bal), read_status=lambda *a: (dict(self.status), None),
            deployable_usd=rebalancer.deployable_usd, pool_tokens=lambda: ((SOL, 'SOL'), (USDC, 'USDC')),
            position_usd=rebalancer.position_usd, FEED=self.feed.name,
            dexes=types.SimpleNamespace(jupiter_prices=lambda mints: {m: 0.0 for m in mints}),
            load=lambda: {'reward_mints_seen': []})

    def run_audit(self):
        cfg = types.SimpleNamespace(RPC='http://rpc.test', POOL=POOL, GAS_RESERVE_SOL=0.05, PROFIT_WALLET=PROFIT)
        fx = types.SimpleNamespace(fetch=lambda url, sig, tries=3: self.txs.get(sig),
                                   harvested=lambda *a, **k: (0.0, 0.0))
        with mock.patch.object(audit, 'rpc', self.fake_rpc), mock.patch.object(audit.time, 'sleep', lambda s: None):
            return audit.run(self.bot(), db, cfg, fx, lambda ev, **kw: self.notes.append((ev, kw)))

    def rows(self):
        with db.cursor() as cur:
            cur.execute('select check_name, status, detail from audits order by id')
            return [dict(r) for r in cur.fetchall()]

    def test_one_run_checks_everything_and_records_it(self):
        res = self.run_audit()
        self.assertEqual(set(res), {'idle', 'gas', 'equity', 'flows', 'harvests', 'payouts', 'positions',
                                    'band_profile', 'owed', 'empty', 'fee_reads'})
        self.assertEqual(res['idle'], 'ok'); self.assertEqual(res['gas'], 'ok'); self.assertEqual(res['positions'], 'ok')
        self.assertEqual(res['equity'], 'ok', [r for r in self.rows() if r['check_name'] == 'equity'])
        self.assertEqual(res['empty'], 'warn')                              # the empty MSOL account
        self.assertEqual(res['flows'], 'warn')                              # the deposit
        self.assertEqual(len(self.rows()), 11)

    def test_a_deposit_is_found_and_recorded_once(self):
        self.run_audit()
        with db.cursor() as cur:
            cur.execute("select kind, sol, signature from capital_flows where kind = 'deposit'")
            rows = [dict(r) for r in cur.fetchall()]
        self.assertEqual([(r['kind'], float(r['sol']), r['signature']) for r in rows], [('deposit', 1.0, 'GIFT')])
        self.assertEqual(db.audit_value('flows_cursor'), 'GIFT')
        self.run_audit()                                                    # the cursor: nothing re-read
        with db.cursor() as cur:
            cur.execute("select count(*) n from capital_flows where kind = 'deposit'")
            self.assertEqual(cur.fetchone()['n'], 1)

    def test_uncounted_value_is_kept_for_the_book(self):
        self.run_audit()
        self.assertAlmostEqual(float(db.audit_value('uncounted_usd')), 2039280 / 1e9 * 120.0, places=5)

    def test_a_status_change_is_notified_once(self):
        self.run_audit()
        first = [kw['check'] for ev, kw in self.notes if ev == 'AUDIT']
        self.assertIn('empty', first); self.assertIn('flows', first); self.assertNotIn('gas', first)
        self.notes.clear()
        self.run_audit()
        self.assertNotIn('empty', [kw['check'] for ev, kw in self.notes if ev == 'AUDIT'])   # same status: silent
        self.accounts = self.accounts[:2]                                     # the janitor closed it
        self.notes.clear()
        self.run_audit()
        back = [(kw['check'], kw['status'], kw['was']) for ev, kw in self.notes if ev == 'AUDIT']
        self.assertIn(('empty', 'ok', 'warn'), back)                         # back to ok is reported once

    def test_idle_money_and_low_gas_are_caught(self):
        self.bal.update(balanceB=10.0, walletUsd=0.0592 * 120 + 10.0, sol=0.04)
        res = self.run_audit()
        self.assertEqual(res['idle'], 'warn'); self.assertEqual(res['gas'], 'fail')     # $10 of ~$248: 4%
        self.bal.update(balanceB=40.0, walletUsd=0.0592 * 120 + 40.0)
        self.assertEqual(self.run_audit()['idle'], 'fail')                              # 16%: over 10%

    def test_an_orphan_nft_or_a_lost_position_fails(self):
        self.accounts.append({'pubkey': 'N2', 'account': {'lamports': 1, 'data': {'parsed': {'info': {'mint': 'NFT2', 'tokenAmount': {'amount': '1', 'decimals': 0}}}}}})
        self.assertEqual(self.run_audit()['positions'], 'fail')

    def test_a_crashing_check_is_a_recorded_failure_not_a_crash(self):
        with mock.patch.object(audit, 'check_idle', side_effect=RuntimeError('boom')):
            res = self.run_audit()
        self.assertEqual(res['idle'], 'fail')
        self.assertIn('boom', [r for r in self.rows() if r['check_name'] == 'idle'][0]['detail']['error'])
        self.assertEqual(res['gas'], 'ok')                                    # the others still ran

    def test_missing_band_profiles_are_written(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into risk_profile (ts, pool, price) values (now() - interval '3 hours', %s, 120)", (POOL,))
            cur.execute("insert into positions (mint, pool, opened_at, closed_at, dex) values ('OLD', %s, now() - interval '2 hours', now() - interval '1 hour', 'raydium-clmm')", (POOL,))
        res = self.run_audit()
        self.assertEqual(res['band_profile'], 'warn')
        with db.cursor() as cur:
            cur.execute("select final, exit_reason from band_profile where mint = 'OLD'")
            self.assertEqual(dict(cur.fetchone()), {'final': True, 'exit_reason': 'written by the audit'})
        self.assertEqual(self.run_audit()['band_profile'], 'ok')

    def test_harvest_and_payout_rows_are_checked_against_their_transactions(self):
        db.record_harvest('NFT1', 23.15, 3403.6, 6237.0, 'HSIG')
        with db.cursor(commit=True) as cur:
            cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind, signature) values "
                        "(now(), 'sol-usdc', 'NFT1', %s, 'USDC', 0.3, 0.3, 'paid', 'PSIG')", (USDC,))
        self.txs['PSIG'] = tx('PSIG', keys=(OWNER, PROFIT), pre_tok=[tb(0, PROFIT, USDC, 0)], post_tok=[tb(0, PROFIT, USDC, 100_000)])
        res = self.run_audit()
        self.assertEqual(res['harvests'], 'fail'); self.assertEqual(res['payouts'], 'fail')
        self.assertEqual(self.run_audit()['harvests'], 'ok')                  # the cursor moved on

    def test_keep_mints(self):
        b = self.bot(); b.load = lambda: {'reward_mints_seen': ['RAYMINT']}
        self.assertEqual(audit.keep_mints(b, 'A', 'B'), {SOL, USDC, 'A', 'B', 'RAYMINT'})
        b.load = lambda: (_ for _ in ()).throw(RuntimeError('corrupt'))
        self.assertEqual(audit.keep_mints(b, 'A', 'B'), {SOL, USDC, 'A', 'B'})

    def test_known_signatures_come_from_the_ledger_and_the_feed(self):
        with db.cursor(commit=True) as cur:
            cur.execute("update positions set open_sig = 'OPENSIG' where mint = 'NFT1'")
        with open(self.feed.name, 'a') as fh:
            fh.write(json.dumps({'event': 'PAYOUT', 'sent': [{'signature': 'SENTSIG'}], 'signatures': ['MULTI']}) + '\n')
            fh.write('not json but mentions "signature"\n')
        k = audit.known_signatures(db, self.feed.name)
        self.assertTrue({'OPENSIG', 'SWAPSIG', 'SENTSIG', 'MULTI'} <= k)
        self.assertEqual(audit.known_signatures(db, '/nonexistent/feed')  >= {'OPENSIG'}, True)


# --- the loop's hooks -------------------------------------------------------------------

class Hooks(unittest.TestCase):
    def setUp(self):
        self.seen, self.calls, self.events = [], [], []

    def patches(self, answers):
        it = iter(answers)
        return [mock.patch.object(rebalancer, 'chain', lambda *a, **k: (self.calls.append((a, k)) or next(it))),
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))),
                mock.patch.object(rebalancer, 'save', lambda s: None),
                mock.patch.object(rebalancer, 'load', lambda: {'reward_mints_seen': ['R1']}),
                mock.patch.object(rebalancer.db, 'event', lambda *a: self.events.append(a)),
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: self.seen.append((ev, kw)))]

    def go(self, state, answers):
        ps = self.patches(answers)
        for p in ps:
            p.start()
        try:
            return rebalancer.janitor(state)
        finally:
            for p in reversed(ps):
                p.stop()

    def test_janitor_closes_only_when_there_is_rent_and_once_a_day(self):
        plan = {'closable': [{'mint': 'MSOL', 'lamports': 2039280}], 'reclaimSol': 0.00203928}
        state = {}
        out = self.go(state, [(plan, None), (dict(plan, signature='JSIG', sent=True), None)])
        self.assertEqual(out['signature'], 'JSIG')
        (a1, k1), (a2, k2) = self.calls
        self.assertEqual(a1[0], 'close-empty'); self.assertNotIn('--execute', a1); self.assertEqual(k1['dex'], 'janitor')
        self.assertIn('--execute', a2); self.assertEqual(set(a2[1:-1]), {SOL, USDC, 'R1'})
        self.assertEqual(self.seen[0][0], 'JANITOR'); self.assertEqual(self.events[0][0], 'JANITOR')
        self.assertEqual(state['last_audit'], 0)                              # a fresh audit next poll
        self.calls.clear()
        self.assertIsNone(self.go(state, []))                                 # same day: nothing
        self.assertEqual(self.calls, [])

    def test_janitor_with_nothing_to_close_sends_nothing(self):
        state = {'last_audit': 123.0}
        out = self.go(state, [({'closable': [], 'reclaimSol': 0}, None)])
        self.assertEqual(state['last_audit'], 123.0)
        self.assertEqual(len(self.calls), 1); self.assertEqual(out['closable'], [])
        self.assertEqual(self.seen, [])

    def test_janitor_failures_are_reported(self):
        self.assertIsNone(self.go({}, [(None, 'rpc down')]))
        self.assertEqual(self.seen[-1][0], 'janitor_failed')
        plan = {'closable': [{'mint': 'M', 'lamports': 1}], 'reclaimSol': 1e-9}
        self.assertIsNone(self.go({}, [(plan, None), ({'sent': False}, None)]))
        self.assertEqual(self.seen[-1][0], 'janitor_failed')

    def test_run_audits_is_hourly_and_never_raises(self):
        runs = []
        with mock.patch.object(rebalancer.audit, 'run', lambda *a: runs.append(a) or {'idle': 'ok'}), \
                mock.patch.object(rebalancer, 'save', lambda s: None):
            state = {}
            self.assertEqual(rebalancer.run_audits(state), {'idle': 'ok'})
            self.assertIsNone(rebalancer.run_audits(state))
            state['last_audit'] = 0
            rebalancer.run_audits(state)
        self.assertEqual(len(runs), 2)
        seen = []
        with mock.patch.object(rebalancer.audit, 'run', side_effect=RuntimeError('x')), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
            self.assertIsNone(rebalancer.run_audits({}))
        self.assertEqual(seen, ['audit_failed'])


if __name__ == '__main__':
    unittest.main()
