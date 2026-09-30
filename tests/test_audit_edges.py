"""audit.py at its edges: every threshold hit exactly, every filter given the
row it must drop, and classify_tx against an independent oracle over random
transactions. The runner cases drive audit.run through the fakes of
test_audit_runner."""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import audit
import db
from test_audit import tx, tb, OWNER, PROFIT, POOL, SOL, USDC, RAYDIUM
from test_audit_runner import Base

EXAMPLES = int(os.environ.get('HYP_EXAMPLES', '200'))
POISONER = '8funvFQKPaEtb3zGppWiZ9obkPnkfKzLiMGTyhWU3D1h'        # imitates PROFIT
ALICE = 'ALiCE1111111111111111111111111111111111111'


# --- lookalike -----------------------------------------------------------------------

class Lookalike(unittest.TestCase):
    def test_nine_characters_are_enough(self):
        self.assertTrue(audit.lookalike('8fun12D1h', [PROFIT]))            # 9 > 8
        self.assertFalse(audit.lookalike('8funD1h', [PROFIT]))             # 7: too short to judge

    def test_any_of_the_known_is_enough(self):
        self.assertTrue(audit.lookalike(POISONER, [OWNER, PROFIT]))
        self.assertTrue(audit.lookalike(POISONER, [PROFIT, OWNER]))
        self.assertFalse(audit.lookalike(POISONER, []))

    def test_the_last_three_not_four(self):
        # the same last three, a different fourth-last: still a lookalike
        self.assertTrue(audit.lookalike('8funZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ9D1h', [PROFIT]))
        # the same first four, a different fifth: still a lookalike
        self.assertTrue(audit.lookalike('8funZkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h', [PROFIT]))

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(mid=st.text('123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz', min_size=2, max_size=40))
    def test_oracle(self, mid):
        a = PROFIT[:4] + mid + PROFIT[-3:]
        self.assertEqual(audit.lookalike(a, [PROFIT]), a != PROFIT and len(a) > 8)


# --- the small checks ----------------------------------------------------------------

class Thresholds(unittest.TestCase):
    def test_idle_exactly_at_the_floor_and_the_share(self):
        self.assertEqual(audit.check_idle(2.0, 50.0, True)[0], 'ok')                 # = $2 floor: not over
        self.assertEqual(audit.check_idle(25.0, 250.0, True)[0], 'warn')             # = 10% of 250: not a failure
        self.assertEqual(audit.check_idle(0.5, 0.9, True)[0], 'fail')                # equity under $1 still counts
        self.assertEqual(audit.check_idle(0.5, -1.0, True)[0], 'ok')                 # a negative equity is no base
        d = audit.check_idle(3.0, 300.0, True)[1]
        self.assertEqual(d, {'deployable_usd': 3.0, 'limit_usd': 6.0})
        self.assertEqual(audit.check_idle(3.0, 50.0, False), ('ok', {'note': 'no position open', 'deployable_usd': 3.0}))

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(dep=st.floats(0, 1000), eq=st.floats(0, 5000), open_=st.booleans())
    def test_idle_oracle(self, dep, eq, open_):
        want = 'ok' if not open_ else ('fail' if eq > 0 and dep > 0.10 * eq else
                                       'warn' if dep > max(2.0, 0.02 * eq) else 'ok')
        self.assertEqual(audit.check_idle(dep, eq, open_)[0], want)

    def test_gas_detail(self):
        self.assertEqual(audit.check_gas(0.05, 0.05), ('ok', {'native_sol': 0.05, 'reserve_sol': 0.05}))

    def test_equity_detail(self):
        st_, d = audit.check_equity(250.0, 245.0, 2.0)
        self.assertEqual((st_, d), ('warn', {'chain_total_usd': 250.0, 'uncounted_usd': 2.0,
                                             'snapshot_equity_usd': 245.0, 'diff_usd': 3.0}))
        self.assertEqual(audit.check_equity(1.0, None, 0.0), ('warn', {'note': 'no snapshot with equity'}))

    def test_harvest_exactly_at_the_tolerance(self):
        self.assertEqual(audit.check_harvest(0.0, 0.0, (1e-9, 0.0))[0], 'ok')
        self.assertEqual(audit.check_harvest(0.0, 0.0, (0.0, 1e-9))[0], 'ok')
        self.assertEqual(audit.check_harvest(0.0, 0.0, (-1e-9, -1e-9))[0], 'ok')
        self.assertEqual(audit.check_harvest(1.0, 2.0, (1.0, 2.0)),
                         ('ok', {'row': [1.0, 2.0], 'tx': [1.0, 2.0]}))
        self.assertEqual(audit.check_harvest(1.0, 2.0, None), ('warn', {'note': 'transaction unreadable'}))

    def test_small_checks_details(self):
        self.assertEqual(audit.check_owed([{'id': 1}]), ('warn', {'rows': [{'id': 1}]}))
        self.assertEqual(audit.check_owed([]), ('ok', {}))
        self.assertEqual(audit.check_empty(2_000_000_000, 1), ('warn', {'accounts': 1, 'reclaimable_sol': 2.0}))
        self.assertEqual(audit.check_empty(0, 0), ('ok', {'accounts': 0, 'reclaimable_sol': 0.0}))
        self.assertEqual(audit.check_fee_reads(1), ('warn', {'rejected_24h': 1}))
        self.assertEqual(audit.check_fee_reads(0), ('ok', {'rejected_24h': 0}))


class Flows(unittest.TestCase):
    def test_counts_and_the_poison_list_exactly(self):
        st_, d = audit.check_flows([('dust', {}), ('poison', {'sender': 'B', 'lookalike_of': 'P'}),
                                    ('poison', {'sender': 'A', 'lookalike_of': 'P'}), ('deposit', {'x': 1})])
        self.assertEqual((st_, d), ('warn', {'counts': {'dust': 1, 'poison': 2, 'deposit': 1},
                                             'flagged': [('deposit', {'x': 1})],
                                             'poison': [{'sender': 'A', 'imitates': 'P'}, {'sender': 'B', 'imitates': 'P'}]}))
        self.assertEqual(audit.check_flows([]), ('ok', {'counts': {}, 'flagged': []}))
        self.assertEqual(len(audit.check_flows([('other', {'i': i}) for i in range(11)])[1]['flagged']), 10)


# --- payout_received ----------------------------------------------------------------

class Payout(unittest.TestCase):
    def test_only_the_profit_wallets_account_of_the_mint_counts(self):
        # the LP wallet's USDC goes down, the profit wallet's up; a second mint
        # of the profit wallet moves too
        t = tx(keys=(OWNER, PROFIT),
               pre_tok=[tb(1, OWNER, USDC, 5_000_000), tb(2, PROFIT, USDC, 1_000_000), tb(3, PROFIT, 'CAKE', 0, 9)],
               post_tok=[tb(1, OWNER, USDC, 4_700_000), tb(2, PROFIT, USDC, 1_300_000), tb(3, PROFIT, 'CAKE', 7 * 10**9, 9)])
        self.assertTrue(audit.payout_received(t, PROFIT, USDC, 0.3))
        self.assertTrue(audit.payout_received(t, PROFIT, 'CAKE', 7.0))
        self.assertFalse(audit.payout_received(t, PROFIT, USDC, 0.0))
        self.assertFalse(audit.payout_received(t, OWNER, USDC, 0.3))

    def test_exactly_at_the_tolerance(self):
        t = tx(keys=(OWNER, PROFIT), pre_tok=[tb(2, PROFIT, USDC, 0)], post_tok=[tb(2, PROFIT, USDC, 1)])
        self.assertTrue(audit.payout_received(t, PROFIT, USDC, 0.0))                    # 1e-6 off: within
        self.assertTrue(audit.payout_received(t, PROFIT, USDC, 2e-6))
        self.assertFalse(audit.payout_received(t, PROFIT, USDC, 3e-6))

    def test_decimals_are_read_per_balance(self):
        t = tx(keys=(OWNER, PROFIT), pre_tok=[tb(2, PROFIT, 'M', 0, 2)], post_tok=[tb(2, PROFIT, 'M', 150, 2)])
        self.assertTrue(audit.payout_received(t, PROFIT, 'M', 1.5))

    def test_unreadable_shapes_are_not_received(self):
        self.assertFalse(audit.payout_received({'meta': None}, PROFIT, USDC, 0.0))
        self.assertFalse(audit.payout_received({}, PROFIT, USDC, 0.0))
        t = tx(keys=(OWNER, PROFIT))
        t['meta']['preTokenBalances'] = None; t['meta']['postTokenBalances'] = None
        self.assertTrue(audit.payout_received(t, PROFIT, USDC, 0.0))                    # null lists: nothing moved


# --- classify_tx against an oracle ---------------------------------------------------

def build(signer, sender, lam, fee, usdc_raw, wsol_raw, cake_raw, prog, noise, fee_key=True, null_tokens=False):
    """A transaction: `signer` True means the LP wallet pays and signs;
    otherwise `sender` does. Deltas are what reach the LP wallet."""
    keys = [OWNER, 'BOB'] if signer else [sender, OWNER]
    if prog:
        keys.append(RAYDIUM)
    i = keys.index(OWNER)
    pre = [10**12] * len(keys)
    post = list(pre)
    post[i] += lam - (fee if signer else 0)
    post[1 - i] -= lam
    pre_tok, post_tok = [], []
    for mint, raw, dec in ((USDC, usdc_raw, 6), (SOL, wsol_raw, 9), ('CAKE', cake_raw, 9)):
        pre_tok.append(tb(5, OWNER, mint, 10**9, dec)); post_tok.append(tb(5, OWNER, mint, 10**9 + raw, dec))
        if noise:                                        # the counterparty's account of the same mint
            pre_tok.append(tb(6, 'BOB', mint, 10**10, dec)); post_tok.append(tb(6, 'BOB', mint, 10**10 - raw + 3, dec))
    if noise:                                            # the LP wallet's account of a mint nobody moves
        pre_tok.append(tb(7, OWNER, 'IDLE', 42, 0)); post_tok.append(tb(7, OWNER, 'IDLE', 42, 0))
    t = tx('X', keys=keys, pre=pre, post=post, pre_tok=pre_tok, post_tok=post_tok, fee=fee)
    if not fee_key:
        del t['meta']['fee']
    if null_tokens:
        t['meta']['preTokenBalances'] = None; t['meta']['postTokenBalances'] = None
    return t


def oracle(signer, sender, lam, usdc_raw, wsol_raw, cake_raw, prog, watch):
    sol = (lam + wsol_raw) / 1e9
    usdc = usdc_raw / 1e6
    if not signer and audit.lookalike(sender, [OWNER, *watch]):
        return 'poison'
    if not signer and abs(lam + wsol_raw) <= 10_000 and cake_raw == 0 and 0 <= usdc_raw <= 10_000:
        return 'dust'
    if signer and prog:
        return 'bot'
    if prog:
        return 'other'
    if sol >= 0 and usdc >= 0 and (sol > 0 or usdc > 0):
        return 'deposit'
    if sol <= 0 and usdc <= 0 and (sol < 0 or usdc < 0):
        return 'withdrawal'
    return 'other'


amounts = st.one_of(st.sampled_from([0, 1, -1, 10_000, -10_000, 10_001, -10_001, 500_000, -500_000,
                                     999_999, 1_000_000, 1_000_001, -1_000_000, 2 * 10**9, -2 * 10**9]),
                    st.integers(-3 * 10**9, 3 * 10**9))


class ClassifyOracle(unittest.TestCase):
    @settings(max_examples=EXAMPLES * 3, deadline=None)
    @given(signer=st.booleans(), sender=st.sampled_from([ALICE, POISONER, 'SPAM']), lam=amounts, fee=st.sampled_from([0, 5000]),
           usdc=amounts, wsol=st.sampled_from([0, 0, 1, -1, 5 * 10**8, -5 * 10**8]), cake=st.sampled_from([0, 0, 1, -1, 5]),
           prog=st.booleans(), noise=st.booleans())
    def test_kind_and_amounts(self, signer, sender, lam, fee, usdc, wsol, cake, prog, noise):
        t = build(signer, sender, lam, fee, usdc, wsol, cake, prog, noise)
        kind, d = audit.classify_tx(t, OWNER, watch=(PROFIT,))
        self.assertEqual(kind, oracle(signer, sender, lam, usdc, wsol, cake, prog, (PROFIT,)))
        self.assertLess(abs(d['sol'] - (lam + wsol) / 1e9), 5e-10)
        self.assertEqual(d['usdc'], round(usdc / 1e6, 6))
        self.assertEqual(d['other_tokens'], {'CAKE': cake} if cake else {})
        self.assertEqual(d['programs'], ['raydium'] if prog else [])
        self.assertEqual(d['signer'], signer)
        if kind == 'poison':
            self.assertEqual((d['sender'], d['lookalike_of']), (POISONER, PROFIT))
        else:
            self.assertNotIn('sender', d)

    def test_exact_edges(self):
        cases = [  # signer, lam, usdc, wsol, cake, prog -> kind
            (False, 10_000, 0, 0, 0, False, 'dust'), (False, -10_000, 0, 0, 0, False, 'dust'),
            (False, 10_001, 0, 0, 0, False, 'deposit'), (False, 0, 1, 0, 0, False, 'dust'),
            (False, 0, -1, 0, 0, False, 'withdrawal'), (False, 0, 0, 0, 1, False, 'other'),
            (False, 0, 0, 1, 0, False, 'dust'),
            (False, 0, 0, 5 * 10**8, 0, False, 'deposit'), (False, 10_000, 0, 1, 0, False, 'deposit'), (False, 5 * 10**8, -500_000, 0, 0, False, 'other'),
            (False, -5 * 10**8, 500_000, 0, 0, False, 'other'), (False, 5 * 10**8, 0, 0, 0, True, 'other'),
            (True, 5 * 10**8, -500_000, 0, 0, False, 'other'), (True, -5 * 10**8, 500_000, 0, 0, False, 'other'),
            (True, 0, -500_000, 0, 0, False, 'withdrawal'), (True, -5 * 10**8, 0, 0, 0, False, 'withdrawal'),
            (True, 0, 500_000, 0, 0, False, 'deposit'), (True, 5 * 10**8, 0, 0, 0, False, 'deposit'),
            (True, 0, 0, 0, 0, False, 'other'), (True, 0, 0, 0, 0, True, 'bot'),
        ]
        for signer, lam, usdc, wsol, cake, prog, want in cases:
            t = build(signer, ALICE, lam, 5000, usdc, wsol, cake, prog, noise=True)
            self.assertEqual(audit.classify_tx(t, OWNER, watch=(PROFIT,))[0], want, (signer, lam, usdc, wsol, cake, prog))

    def test_a_signed_transaction_is_never_poison(self):
        # even when a watched address imitates the LP wallet itself
        twin = OWNER[:4] + 'Z' * 30 + OWNER[-3:]
        t = build(True, ALICE, 0, 5000, 0, 0, 0, False, noise=False)
        self.assertEqual(audit.classify_tx(t, OWNER, watch=(twin,))[0], 'other')

    def test_poison_names_the_imitated_address_among_several(self):
        t = build(False, POISONER, 0, 5000, 100, 0, 0, False, noise=False)
        kind, d = audit.classify_tx(t, OWNER, watch=('SOMEONE', PROFIT))
        self.assertEqual((kind, d['lookalike_of'], d['sender']), ('poison', PROFIT, POISONER))

    def test_a_transaction_without_the_owner_moves_nothing(self):
        t = tx(keys=(ALICE, 'BOB'), pre=[10**9, 0], post=[0, 10**9])
        kind, d = audit.classify_tx(t, OWNER)
        self.assertEqual((kind, d['sol'], d['usdc'], d['other_tokens'], d['signer']), ('dust', 0.0, 0.0, {}, False))

    def test_a_missing_fee_and_null_token_lists(self):
        t = build(True, ALICE, -10**9, 5000, 0, 0, 0, False, noise=False, fee_key=False, null_tokens=True)
        kind, d = audit.classify_tx(t, OWNER)
        self.assertEqual((kind, d['sol'], d['usdc']), ('withdrawal', -1.000005, 0.0))    # no fee figure: nothing added back
        t = build(False, ALICE, 10**9, 5000, 0, 0, 0, False, noise=False, null_tokens=True)
        self.assertEqual(audit.classify_tx(t, OWNER)[:1], ('deposit',))

    def test_plain_string_account_keys(self):
        t = build(False, ALICE, 10**9, 5000, 0, 0, 0, False, noise=False)
        t['transaction']['message']['accountKeys'] = [k['pubkey'] for k in t['transaction']['message']['accountKeys']]
        self.assertEqual(audit.classify_tx(t, OWNER)[0], 'deposit')

    def test_failed(self):
        t = build(False, POISONER, 10**9, 5000, 0, 0, 0, False, noise=False)
        t['meta']['err'] = {'InstructionError': [0, 'Custom']}
        self.assertEqual(audit.classify_tx(t, OWNER, watch=(PROFIT,)), ('failed', {}))


# --- check_positions ----------------------------------------------------------------

class Positions(unittest.TestCase):
    def test_detail_exactly(self):
        st_, d = audit.check_positions(['D', 'M'], ['M', 'X'], ['meteora-dlmm', 'raydium-clmm'], dlmm_live=[])
        self.assertEqual((st_, d), ('fail', {'db_open': ['D', 'M'], 'nfts': ['M', 'X'],
                                             'missing_on_chain': ['D'], 'orphans': ['X']}))

    def test_a_live_dlmm_account_is_no_orphan_and_no_nft_is_needed(self):
        self.assertEqual(audit.check_positions(['D'], [], ['meteora-dlmm'], dlmm_live=['D', 'OTHER'])[0], 'ok')
        self.assertEqual(audit.check_positions(['M'], ['M'], ['orca'], dlmm_live=['M'])[0], 'ok')
        st_, d = audit.check_positions(['M'], [], ['orca'], dlmm_live=['M'])          # an NFT venue needs its NFT
        self.assertEqual((st_, d['missing_on_chain']), ('fail', ['M']))

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(rows=st.lists(st.tuples(st.sampled_from('ABCDE'), st.sampled_from(['orca', 'meteora-dlmm'])), max_size=3, unique_by=lambda r: r[0]),
           nfts=st.sets(st.sampled_from('ABCDEF')), live=st.sets(st.sampled_from('ABCDEF')))
    def test_oracle(self, rows, nfts, live):
        mints = [m for m, _ in rows]
        missing = [m for m, dx in rows if dx != 'meteora-dlmm' and m not in nfts] + \
                  [m for m, dx in rows if dx == 'meteora-dlmm' and m not in live]
        orphans = [m for m in nfts if m not in mints]
        st_, d = audit.check_positions(mints, sorted(nfts), [dx for _, dx in rows], dlmm_live=sorted(live))
        self.assertEqual(sorted(d['missing_on_chain']), sorted(missing))
        self.assertEqual(sorted(d['orphans']), sorted(orphans))
        self.assertEqual(st_, 'fail' if missing or orphans or len(mints) > 1 else 'ok')


# --- known_signatures ----------------------------------------------------------------

class KnownSignatures(unittest.TestCase):
    def setUp(self):
        _fixtures.reset_ledger()
        self.feed = tempfile.NamedTemporaryFile('w', suffix='.jsonl', delete=False)

    def tearDown(self):
        os.unlink(self.feed.name)

    def test_every_source_and_nothing_else(self):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at, dex, open_sig, close_sig) values "
                        "('P1', %s, now(), 'orca', 'OPEN1', 'CLOSE1'), ('P2', %s, now(), 'orca', null, null)", (POOL, POOL))
            cur.execute("insert into capital_flows (ts, kind, sol, usdc, usd, price, signature) values "
                        "(now(), 'deposit', 1, 0, 1, 1, 'FLOW1')")
        db.record_harvest('P1', 0.1, 0.1, 0.1, 'HARV1')
        with db.cursor(commit=True) as cur:
            cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind, signature) values "
                        "(now(), 's', 'P1', 'U', 'USDC', 1, 1, 'paid', 'PAY1')")
        for r in ({'signature': 'ONE'}, {'signature': None, 'signatures': ['TWO', 7, None]},
                  {'signature': '', 'sent': [{'signature': 'THREE'}, {'signature': None}, {}, 'bare']},
                  {'signatures': None, 'sent': None}):
            self.feed.write(json.dumps(r) + '\n')
        self.feed.write('{"signature": broken\n')
        self.feed.write(json.dumps({'event': 'X', 'sig': 'NOPE'}) + '\n')
        self.feed.close()
        self.assertEqual(audit.known_signatures(db, self.feed.name),
                         {'OPEN1', 'CLOSE1', 'FLOW1', 'HARV1', 'PAY1', 'ONE', 'TWO', 'THREE'})


# --- audit.run, edge by edge -------------------------------------------------------

DLMM = audit.DLMM_PROGRAM


class Run(Base):
    """The runner with a Jupiter that omits unpriced mints (as the real one
    does), a record of every price and token-facts request, and position
    accounts for Meteora."""
    def setUp(self):
        super().setUp()
        self.priced, self.faced = [], []
        self.infos = {}
        self.load = {'reward_mints_seen': []}

    def fake_rpc(self, url, method, params, tries=4):
        if method == 'getAccountInfo':
            self.rpc_calls.append((method, params))
            return self.infos.get(params[0])
        return super().fake_rpc(url, method, params, tries)

    def bot(self):
        b = super().bot()
        b.dexes = types.SimpleNamespace(
            jupiter_prices=lambda mints: (self.priced.append(list(mints)) or {m: self.prices[m] for m in mints if m in self.prices}),
            jupiter_token=lambda m: (self.faced.append(m) or self.facts.get(m)))
        b.load = lambda: self.load
        return b

    def position(self, mint, dex):
        with db.cursor(commit=True) as cur:
            cur.execute("insert into positions (mint, pool, opened_at, dex) values (%s, %s, now() - interval '1 hour', %s)",
                        (mint, POOL, dex))


class RunRecord(Run):
    def test_warn_back_to_ok_is_announced(self):
        self.bal['sol'] = 0.01
        self.run_audit()
        db.set_audit_value('status:gas', 'warn')
        self.bal['sol'] = 0.0592
        self.notes.clear(); self.run_audit()
        self.assertIn(('gas', 'ok', 'warn'), [(kw['check'], kw['status'], kw['was']) for ev, kw in self.notes])
        self.assertEqual(db.audit_value('status:gas'), 'ok')

    def test_a_crash_is_recorded_with_its_type(self):
        with mock.patch.object(audit, 'check_gas', side_effect=KeyError('x' * 400)):
            self.assertEqual(self.run_audit()['gas'], 'fail')
        err = self.detail('gas')['error']
        self.assertTrue(err.startswith('KeyError: '))
        self.assertEqual(len(err), len('KeyError: ') + 300)


class RunIdle(Run):
    def test_a_status_without_a_position_mint_is_no_position(self):
        self.status = {'positionMint': None, 'positionUsd': 240.0, 'feesAccrued_USD': 0.1}
        self.bal.update(balanceB=100.0)
        self.assertEqual(self.run_audit()['idle'], 'ok')
        self.assertEqual(self.detail('idle')['note'], 'no position open')
        self.assertEqual(self.detail('equity')['chain_total_usd'], round(0.0592 * 120 + 0.1, 4))   # no mark, the accrual kept

    def test_the_excused_leftover_is_exact_and_never_negative(self):
        self.load = {'idle_baseline': {'mint': 'NFT1', 'usd': 10.0}}
        self.run_audit()
        d = self.detail('idle')
        self.assertEqual((d['deployable_usd'], d['tolerance_leftover_usd']), (0.0, 10.0))
        self.load = {'idle_baseline': {'mint': 'NFT1', 'usd': None}}
        self.run_audit()
        d = self.detail('idle')
        self.assertEqual(d['tolerance_leftover_usd'], 0.0)
        self.assertAlmostEqual(d['deployable_usd'], 0.5 + (0.0592 - 0.059) * 120.0, places=4)

    def test_no_position_excuses_nothing(self):
        self.status = None
        self.load = {'idle_baseline': {'usd': 5.0}}                        # no mint: equal to no position's
        self.run_audit()
        self.assertEqual(self.detail('idle')['tolerance_leftover_usd'], 0.0)

    def test_foreign_idle_adds_to_the_wallet_and_asks_only_for_foreign_held_tokens(self):
        self.acct('JITO', 200_000_000, 9); self.acct('EMPTY', 0, 9); self.acct(USDC, 500_000, 6); self.acct(SOL, 5, 9)
        self.prices = {'JITO': 150.0}; self.facts = {'JITO': {'verified': True, 'symbol': 'JitoSOL'}}
        self.run_audit()
        d = self.detail('idle')
        self.assertAlmostEqual(d['deployable_usd'], 30.0 + 0.5 + (0.0592 - 0.059) * 120.0, places=4)
        self.assertEqual(self.priced[0], ['JITO'])
        self.assertEqual(self.faced, ['JITO'])

    def test_a_reward_token_is_not_swept(self):
        self.acct('RAY', 10 * 10**6, 6)
        self.prices = {'RAY': 2.0}; self.facts = {'RAY': {'verified': True}}
        self.load = {'reward_mints_seen': ['RAY']}
        self.run_audit()
        self.assertEqual(self.detail('idle')['foreign_usd'], 0.0)

    def test_nothing_foreign_asks_nothing(self):
        self.acct(USDC, 500_000, 6)
        self.run_audit()
        self.assertEqual((self.priced, self.faced), ([], []))


class RunEquity(Run):
    def total(self):
        return self.detail('equity')['chain_total_usd']

    def test_a_quote_worth_two_dollars(self):
        self.bal['quoteUsd'] = 2.0
        self.acct(USDC, 500_000, 6)
        self.acct('MSOL', 0, 9, lamports=1_000_000_000)                    # 1 SOL of rent at $240
        self.run_audit()
        d = self.detail('equity')
        self.assertAlmostEqual(d['uncounted_usd'], 240.0, places=4)
        self.assertAlmostEqual(d['chain_total_usd'], self.native / 1e9 * 240.0 + 1.0 + 240.0 + 0.1 + 240.0, places=4)

    def test_no_quote_price_is_a_dollar(self):
        self.bal['quoteUsd'] = None
        self.acct(USDC, 500_000, 6)
        self.run_audit()
        self.assertAlmostEqual(self.total(), self.native / 1e9 * 120.0 + 0.5 + 240.0 + 0.1, places=4)

    def test_an_unpriced_token_and_an_nft_add_nothing_and_only_held_tokens_are_priced(self):
        self.acct('NOPRICE', 5 * 10**9, 9)
        self.acct('NFT1', 1, 0, prog=1)
        self.acct('CAKE', 0, 9); self.acct(SOL, 0, 9); self.acct(SOL, 3 * 10**9, 9, lamports=0)
        self.acct('CAKE2', 10**9, 9)
        self.prices = {'NFT1': 99.0, 'CAKE': 5.0, 'CAKE2': 1.0}
        self.run_audit()
        d = self.detail('equity')
        self.assertAlmostEqual(d['uncounted_usd'], 3 * 120.0 + 1.0 + 2039280 / 1e9 * 120.0, places=4)
        self.assertEqual(self.priced[-1], ['NOPRICE', 'CAKE2'])                   # the equity read: no NFT, nothing empty

    def test_no_foreign_token_asks_no_price(self):
        self.acct(USDC, 500_000, 6)
        self.run_audit()
        self.assertEqual(self.priced, [])

    def test_a_pool_without_sol_and_no_sol_price(self):
        self.acct(SOL, 10**9, 9)
        b = self.bot(); b.pool_tokens = lambda: (('JUP', 'JUP'), (USDC, 'USDC'))
        with mock.patch.object(self, 'bot', lambda: b):
            self.run_audit()
        self.assertEqual(self.detail('equity')['uncounted_usd'], 0.0)

    def test_a_position_without_a_mark_and_without_accrual(self):
        self.status = {'positionMint': 'NFT1'}
        self.run_audit()
        self.assertAlmostEqual(self.total(), self.native / 1e9 * 120.0, places=4)

    def test_empty_accounts_of_kept_mints_are_not_empty(self):
        self.acct(USDC, 0, 6); self.acct(SOL, 0, 9); self.acct('RAY', 0, 6)
        self.load = {'reward_mints_seen': ['RAY']}
        self.assertEqual(self.run_audit()['empty'], 'ok')
        self.assertEqual(self.detail('empty'), {'accounts': 0, 'reclaimable_sol': 0.0})
        self.acct('JUNK', 0, 6, lamports=2_000_000)
        self.assertEqual(self.run_audit()['empty'], 'warn')
        self.assertEqual(self.detail('empty'), {'accounts': 1, 'reclaimable_sol': 0.002})


class RunFlows(Run):
    def test_a_withdrawal_with_a_two_dollar_quote(self):
        self.bal['quoteUsd'] = 2.0
        self.sigs = [{'signature': 'W', 'blockTime': 1790500000}]
        self.txs = {'W': tx('W', keys=(OWNER, 'BOB'), pre=[3 * 10**9, 0], post=[2 * 10**9 - 5000, 10**9],
                            pre_tok=[tb(2, OWNER, USDC, 30_000_000)], post_tok=[tb(2, OWNER, USDC, 10_000_000)], block=1790500000)}
        self.run_audit()
        with db.cursor() as cur:
            cur.execute("select kind, sol, usdc, usd, price from capital_flows where signature = 'W'")
            r = dict(cur.fetchone())
        self.assertEqual((r['kind'], float(r['sol']), float(r['usdc'])), ('withdrawal', 1.0, 20.0))
        self.assertAlmostEqual(float(r['usd']), 1.0 * 240.0 + 20.0); self.assertEqual(float(r['price']), 240.0)

    def test_no_quote_price_and_no_block_time(self):
        import time as _t
        self.bal['quoteUsd'] = None
        self.sigs = [{'signature': 'G', 'blockTime': 5}]
        t = tx('G', keys=('ALICE', OWNER), pre=[5 * 10**9, 10**9], post=[4 * 10**9, 2 * 10**9]); t['blockTime'] = None
        self.txs = {'G': t}
        before = _t.time()
        self.run_audit()
        with db.cursor() as cur:
            cur.execute("select ts, usd, price from capital_flows where signature = 'G'")
            r = dict(cur.fetchone())
        self.assertEqual(float(r['price']), 120.0); self.assertAlmostEqual(float(r['usd']), 120.0)
        self.assertLess(abs(r['ts'].timestamp() - before), 60)

    def test_poison_through_the_runner_is_warned_and_not_recorded(self):
        self.sigs = [{'signature': 'PZ', 'blockTime': 1}]
        self.txs = {'PZ': tx('PZ', keys=(POISONER, OWNER), pre_tok=[tb(0, OWNER, USDC, 0)], post_tok=[tb(0, OWNER, USDC, 50_000_000)])}
        self.assertEqual(self.run_audit()['flows'], 'warn')
        d = self.detail('flows')
        self.assertEqual((d['counts'], d['poison']), ({'poison': 1}, [{'sender': POISONER, 'imitates': PROFIT}]))
        with db.cursor() as cur:
            cur.execute('select count(*) n from capital_flows')
            self.assertEqual(cur.fetchone()['n'], 0)

    def test_a_known_signature_from_the_ledger_is_counted_not_fetched(self):
        db.record_harvest('NFT1', 0.1, 0.1, 0.1, 'HK')
        self.sigs = [{'signature': 'HK', 'blockTime': 1}, {'signature': 'G', 'blockTime': 2}]
        self.txs = {'G': tx('G', keys=('ALICE', OWNER), pre=[5 * 10**9, 10**9], post=[4 * 10**9, 2 * 10**9])}
        self.run_audit()
        d = self.detail('flows')
        self.assertEqual(d['counts'], {'known': 1, 'deposit': 1})
        self.assertEqual(d['flagged'][0][1]['sig'], 'G')


class RunLedger(Run):
    def test_the_first_harvest_and_payout_rows_are_checked(self):
        with db.cursor(commit=True) as cur:
            cur.execute('truncate harvests, payouts restart identity')
        self.position('NFT1', 'raydium-clmm'); self.acct('NFT1', 1, 0)
        db.record_harvest('NFT1', 0.001, 0.1, 0.22, 'H1')
        with db.cursor(commit=True) as cur:
            cur.execute("insert into payouts (ts, config_name, position, token_mint, symbol, amount, usd, kind, signature) values "
                        "(now(), 's', 'M', %s, 'USDC', 0.3, 0.3, 'paid', 'P1')", (USDC,))
        self.harvested = {'H1': (0.001, 0.1)}
        self.assertEqual(self.run_audit()['harvests'], 'ok')
        self.assertEqual(self.detail('harvests'), {'checked': 1, 'problems': []})
        self.assertEqual(self.detail('payouts'), {'checked': 1, 'not_received': [1]})

    def dlmm(self, info):
        self.position('DPOS', 'meteora-dlmm')
        self.infos = {'DPOS': info}
        res = self.run_audit()
        return res['positions'], self.detail('positions')

    def test_a_live_dlmm_account(self):
        st_, d = self.dlmm({'value': {'owner': DLMM, 'lamports': 1}})
        self.assertEqual((st_, d['missing_on_chain']), ('ok', []))
        self.assertEqual([p for m, p in self.rpc_calls if m == 'getAccountInfo'], [['DPOS', {'encoding': 'base64'}]])

    def test_a_dead_dlmm_account(self):
        for v in (None, {'owner': DLMM, 'lamports': 0}, {'owner': DLMM, 'lamports': None}, {'owner': DLMM},
                  {'owner': 'SOMEONE', 'lamports': 10**7}):
            reset_all()
            st_, d = self.dlmm({'value': v})
            self.assertEqual((st_, d['missing_on_chain']), ('fail', ['DPOS']), v)

    def test_an_unreadable_dlmm_account_is_a_warning(self):
        st_, d = self.dlmm(None)
        self.assertEqual((st_, d), ('warn', {'note': 'DLMM position account unreadable', 'mint': 'DPOS'}))

    def test_other_venues_read_no_account(self):
        self.position('NFT1', 'orca'); self.acct('NFT1', 1, 0)
        self.assertEqual(self.run_audit()['positions'], 'ok')
        self.assertNotIn('getAccountInfo', [m for m, _ in self.rpc_calls])


def reset_all():
    with db.cursor(commit=True) as cur:
        cur.execute('truncate positions, band_profile, harvests, audits, audit_state')
