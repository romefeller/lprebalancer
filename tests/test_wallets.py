"""wallets.py: the sleeves of a shared wallet, the claims and the lock.

Pure rules first (who owns a mint, the split, the claim arithmetic, the
sleeve view), with hypothesis stating the invariants: the sleeves of all
profiles partition the wallet, none is negative, a claim never goes below 0
and an overdraw is reported. Then the database: claims adjusted in one
transaction, two simulated profiles writing interleaved under the lock, a
lock that times out, and a holder that dies between its balance reads."""
import json
import os
import subprocess
import sys
import threading
import time
import unittest

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import db
import wallets

EXAMPLES = int(os.environ.get('HYP_EXAMPLES', '300'))
SOL = 'So11111111111111111111111111111111111111112'
USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
MU = 'MUmintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
DJT = 'DJTmintBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'
WALLET = 'wallets-test'
ADDRESS = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'


def prof(name, mints, deposit=None, residual=False):
    return {'name': name, 'mints': mints, 'deposit_mint': deposit, 'residual_owner': residual}


SOLP = prof('sol-usdc', [SOL, USDC], SOL, residual=True)
MUP = prof('mu-usdc', [MU, USDC], MU)
DJTP = prof('djt-usdc', [DJT, USDC], DJT)
THREE = [SOLP, MUP, DJTP]


# --- who owns what ---------------------------------------------------------------------

class Ownership(unittest.TestCase):
    def test_a_mint_one_profile_uses_is_wholly_its_own(self):
        self.assertEqual(wallets.sole_owner(SOL, THREE), 'sol-usdc')
        self.assertEqual(wallets.sole_owner(MU, THREE), 'mu-usdc')
        self.assertIsNone(wallets.sole_owner(USDC, THREE))                   # shared

    def test_the_holder_is_the_deposit_profile_else_the_residual_owner(self):
        self.assertEqual(wallets.holder(USDC, THREE), 'sol-usdc')            # nobody deposits USDC
        self.assertEqual(wallets.holder(MU, THREE), 'mu-usdc')
        self.assertIsNone(wallets.holder(USDC, [MUP, DJTP]))                 # no residual owner

    def test_an_unknown_profile_may_use_any_mint(self):
        unknown = prof('new', None)
        self.assertIsNone(wallets.sole_owner(SOL, [SOLP, unknown]))         # fail closed
        # its deposit mint is still its own to hold: the residual of MU is mu-usdc's
        self.assertEqual(wallets.holder(MU, [MUP, unknown, SOLP]), 'mu-usdc')

    def test_claimed_mints_are_the_shared_ones_not_held(self):
        self.assertEqual(wallets.claimed_mints('mu-usdc', [MU, USDC], THREE), [USDC])
        self.assertEqual(wallets.claimed_mints('sol-usdc', [SOL, USDC], THREE), [])     # holds USDC's residual
        self.assertEqual(wallets.claimed_mints('sol-usdc', [SOL, USDC], [SOLP]), [])    # alone: nothing shared

    def test_a_deposit_mint_makes_a_user_even_with_unknown_mints(self):
        self.assertEqual(wallets.users(MU, [prof('new', None, MU)]), {'new'})
        self.assertEqual(wallets.users(MU, [prof('new', None, None)]), set())

    def test_a_mint_one_profile_uses_alone_is_never_claimed_even_when_it_holds_no_residual(self):
        # X is p's alone (no deposit, so its holder would be the residual owner r)
        ps = [prof('p', [DJT, USDC]), prof('r', [SOL, USDC], SOL, residual=True)]
        self.assertEqual(wallets.claimed_mints('p', [DJT, USDC], ps), [USDC])

    def test_a_mint_is_claimed_once_in_any_letter_case(self):
        a = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
        ps = [prof('p', [a.lower(), '0x4200000000000000000000000000000000000006']),
              prof('q', [a.lower(), '0x' + '1' * 40], residual=True)]
        self.assertEqual(wallets.claimed_mints('p', [a, a.lower()], ps), [a.lower()])

    def test_evm_addresses_compare_in_lower_case(self):
        a = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
        ps = [prof('base', ['0x4200000000000000000000000000000000000006', a], residual=True)]
        self.assertEqual(wallets.users(a.lower(), ps), {'base'})
        self.assertEqual(wallets.norm(a), a.lower())
        self.assertEqual(wallets.norm(USDC), USDC)

    def test_with_self_replaces_the_stored_row(self):
        stored = [prof('mu-usdc', None, MU), SOLP]
        out = wallets.with_self(stored, MUP)
        self.assertEqual(sorted(p['name'] for p in out), ['mu-usdc', 'sol-usdc'])
        self.assertEqual(next(p for p in out if p['name'] == 'mu-usdc')['mints'], [MU, USDC])


class Split(unittest.TestCase):
    def test_claims_then_the_residual(self):
        v, over, rest = wallets.split(100.0, USDC, THREE, {'mu-usdc': {USDC: 30.0}, 'djt-usdc': {USDC: 5.0}})
        self.assertEqual(v, {'sol-usdc': 65.0, 'mu-usdc': 30.0, 'djt-usdc': 5.0})
        self.assertEqual((over, rest), (0.0, 0.0))

    def test_an_overdraw_scales_the_claims_and_leaves_the_holder_nothing(self):
        v, over, _ = wallets.split(20.0, USDC, THREE, {'mu-usdc': {USDC: 30.0}, 'djt-usdc': {USDC: 10.0}})
        self.assertAlmostEqual(v['mu-usdc'], 15.0); self.assertAlmostEqual(v['djt-usdc'], 5.0)
        self.assertEqual(v['sol-usdc'], 0.0); self.assertAlmostEqual(over, 20.0)

    def test_the_holders_own_claim_row_is_ignored(self):
        v, *_ = wallets.split(10.0, USDC, THREE, {'sol-usdc': {USDC: 99.0}})
        self.assertEqual(v['sol-usdc'], 10.0)

    def test_a_sole_owner_takes_everything_whatever_the_claims(self):
        v, over, _ = wallets.split(3.0, MU, THREE, {'sol-usdc': {MU: 2.0}})
        self.assertEqual(v, {'sol-usdc': 0.0, 'mu-usdc': 3.0, 'djt-usdc': 0.0}); self.assertEqual(over, 0.0)

    def test_no_holder_leaves_the_remainder_unassigned(self):
        v, over, rest = wallets.split(10.0, USDC, [MUP, DJTP], {'mu-usdc': {USDC: 4.0}})
        self.assertEqual((v['mu-usdc'], v['djt-usdc'], rest, over), (4.0, 0.0, 6.0, 0.0))

    def test_no_holder_and_an_overdraw_scales_and_names_no_one_else(self):
        v, over, rest = wallets.split(10.0, USDC, [MUP, DJTP], {'mu-usdc': {USDC: 15.0}, 'djt-usdc': {USDC: 5.0}})
        self.assertEqual(set(v), {'mu-usdc', 'djt-usdc'})                   # no None key for a missing holder
        self.assertEqual((v['mu-usdc'], v['djt-usdc'], over, rest), (7.5, 2.5, 10.0, 0.0))

    def test_negative_or_missing_inputs_are_zero(self):
        v, *_ = wallets.split(None, USDC, THREE, {'mu-usdc': {USDC: -5.0}})
        self.assertEqual(sum(v.values()), 0.0)
        self.assertTrue(all(x == 0.0 for x in v.values()))


class ClaimAfter(unittest.TestCase):
    def test_floor_and_overdraw(self):
        self.assertEqual(wallets.claim_after(10.0, -3.0), (7.0, 0.0))
        self.assertEqual(wallets.claim_after(10.0, -10.0), (0.0, 0.0))
        self.assertEqual(wallets.claim_after(10.0, -12.5), (0.0, 2.5))
        self.assertEqual(wallets.claim_after(None, 4.0), (4.0, 0.0))
        self.assertEqual(wallets.claim_after(0.0, None), (0.0, 0.0))


# --- the properties ------------------------------------------------------------------------

MINTS = [SOL, USDC, MU, DJT]
amount = st.floats(0, 1e7, allow_nan=False, allow_infinity=False)


@st.composite
def wallet_profiles(draw, residual=True):
    n = draw(st.integers(1, 5))
    names = [f'p{i}' for i in range(n)]
    out, deposits = [], set()
    owner = draw(st.integers(0, n - 1)) if residual else None
    for i, name in enumerate(names):
        mints = None if draw(st.booleans()) and draw(st.booleans()) else \
            draw(st.lists(st.sampled_from(MINTS), min_size=2, max_size=2, unique=True))
        dep = draw(st.sampled_from([None] + [m for m in MINTS if m not in deposits]))
        if dep:
            deposits.add(dep)
        out.append(prof(name, mints, dep, residual=(i == owner)))
    return out


@st.composite
def claim_book(draw, profiles):
    return {p['name']: {m: draw(amount) for m in draw(st.lists(st.sampled_from(MINTS), max_size=4, unique=True))}
            for p in profiles}


class Properties(unittest.TestCase):
    @settings(max_examples=EXAMPLES, deadline=None)
    @given(st.data())
    def test_the_sleeves_partition_the_wallet(self, data):
        ps = data.draw(wallet_profiles())
        claims = data.draw(claim_book(ps))
        for mint in MINTS:
            total = data.draw(amount)
            views, over, rest = wallets.split(total, mint, ps, claims)
            self.assertEqual(set(views), {p['name'] for p in ps})
            self.assertTrue(all(v >= 0 for v in views.values()), views)
            self.assertEqual(rest, 0.0)                                    # a residual owner exists
            self.assertAlmostEqual(sum(views.values()), total, delta=1e-9 * max(1.0, total))
            self.assertGreaterEqual(over, 0.0)

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(st.data())
    def test_a_claimant_never_sees_more_than_its_claim_and_overdraw_is_detected(self, data):
        ps = data.draw(wallet_profiles())
        claims = data.draw(claim_book(ps))
        mint, total = data.draw(st.sampled_from(MINTS)), data.draw(amount)
        views, over, _ = wallets.split(total, mint, ps, claims)
        if wallets.sole_owner(mint, ps) is not None:
            return
        h = wallets.holder(mint, ps)
        asked = sum(max((claims.get(p['name']) or {}).get(mint, 0.0), 0.0) for p in ps if p['name'] != h)
        for p in ps:
            if p['name'] != h:
                self.assertLessEqual(views[p['name']], (claims.get(p['name']) or {}).get(mint, 0.0) + 1e-9)
        self.assertAlmostEqual(over, max(asked - total, 0.0), delta=1e-9 * max(1.0, asked))
        if asked <= total:
            for p in ps:
                if p['name'] != h:
                    self.assertEqual(views[p['name']], (claims.get(p['name']) or {}).get(mint, 0.0))

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(st.data())
    def test_without_a_holder_the_views_and_the_remainder_partition(self, data):
        ps = [dict(p, deposit_mint=None) for p in data.draw(wallet_profiles(residual=False))]
        claims = data.draw(claim_book(ps))
        mint, total = data.draw(st.sampled_from(MINTS)), data.draw(amount)
        views, over, rest = wallets.split(total, mint, ps, claims)
        self.assertTrue(all(v >= 0 for v in views.values()) and rest >= 0)
        if over == 0.0:
            self.assertAlmostEqual(sum(views.values()) + rest, total, delta=1e-9 * max(1.0, total))

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(claim=amount, deltas=st.lists(st.floats(-1e7, 1e7, allow_nan=False), max_size=20))
    def test_claims_never_go_negative_and_the_overdraw_is_the_shortfall(self, claim, deltas):
        c = claim
        for d in deltas:
            new, over = wallets.claim_after(c, d)
            self.assertGreaterEqual(new, 0.0); self.assertGreaterEqual(over, 0.0)
            self.assertAlmostEqual(new - over, c + d, delta=1e-6 * max(1.0, abs(c) + abs(d)))
            self.assertTrue(new == 0.0 or over == 0.0)
            c = new

    @settings(max_examples=EXAMPLES, deadline=None)
    @given(st.data())
    def test_the_sleeve_views_of_every_profile_sum_to_the_wallet(self, data):
        # Every profile reads the same wallet; each sleeve's balance of a mint
        # its pool holds is its split, and the splits add up to the wallet.
        ps = [dict(p, mints=p['mints'] or [MINTS[i % 4], MINTS[(i + 1) % 4]]) for i, p in enumerate(data.draw(wallet_profiles()))]
        claims = data.draw(claim_book(ps))
        held = {m: data.draw(amount) for m in MINTS}
        for mint in MINTS:
            seen = 0.0
            for p in ps:
                if mint not in p['mints']:
                    continue
                a, b = p['mints']
                bal = {'balanceA': held[a], 'balanceB': held[b], 'price': 2.0, 'quoteUsd': 1.0, 'walletUsd': None}
                v = wallets.sleeve(bal, p['name'], a, b, ps, claims, SOL)
                seen += v['balanceA'] if a == mint else v['balanceB']
                self.assertEqual((v['rawBalanceA'], v['rawBalanceB']), (held[a], held[b]))
            views, *_ = wallets.split(held[mint], mint, ps, claims)
            users = {p['name'] for p in ps if mint in p['mints']}
            self.assertAlmostEqual(seen, sum(v for n, v in views.items() if n in users), delta=1e-6)
            self.assertLessEqual(seen, held[mint] + 1e-6)


class SleeveView(unittest.TestCase):
    BAL = {'owner': ADDRESS, 'sol': 2.0, 'balanceA': 2.0, 'balanceB': 100.0, 'price': 150.0, 'quoteUsd': 1.0,
           'nativeSide': 'A', 'walletUsd': 400.0}

    def test_alone_on_the_wallet_the_read_is_unchanged(self):
        v = wallets.sleeve(self.BAL, 'sol-usdc', SOL, USDC, [SOLP], {}, SOL)
        self.assertEqual({k: v[k] for k in self.BAL}, self.BAL)
        self.assertEqual((v['rawBalanceA'], v['rawBalanceB'], v['rawWalletUsd']), (2.0, 100.0, 400.0))
        self.assertNotIn('sleeveOverdraw', v)

    def test_the_residual_owner_sees_the_wallet_less_other_claims(self):
        v = wallets.sleeve(self.BAL, 'sol-usdc', SOL, USDC, THREE, {'mu-usdc': {USDC: 30.0}}, SOL)
        self.assertEqual((v['balanceA'], v['balanceB']), (2.0, 70.0))
        self.assertAlmostEqual(v['walletUsd'], 370.0)
        self.assertEqual(v['rawWalletUsd'], 400.0)

    def test_a_claimant_sees_its_claim_and_its_own_token_and_no_gas(self):
        # the MU pool's read: 5 MU at $10, the wallet's 100 USDC, and native SOL
        # worth $300 the signer adds to walletUsd (SOL is sol-usdc's, not mu-usdc's)
        bal = {'sol': 2.0, 'balanceA': 5.0, 'balanceB': 100.0, 'price': 10.0, 'quoteUsd': 1.0,
               'nativeSide': None, 'walletUsd': 5 * 10 + 100 + 300.0}
        v = wallets.sleeve(bal, 'mu-usdc', MU, USDC, THREE, {'mu-usdc': {USDC: 30.0}}, SOL)
        self.assertEqual((v['balanceA'], v['balanceB']), (5.0, 30.0))
        self.assertAlmostEqual(v['walletUsd'], 80.0)

    def test_the_residual_owner_keeps_gas_no_pool_uses(self):
        bal = {'sol': 1.0, 'balanceA': 0.0, 'balanceB': 10.0, 'price': 10.0, 'quoteUsd': 1.0,
               'nativeSide': None, 'walletUsd': 10 + 150.0}
        owner = prof('mu-usdc', [MU, USDC], MU, residual=True)
        v = wallets.sleeve(bal, 'mu-usdc', MU, USDC, [owner, DJTP], {'djt-usdc': {USDC: 4.0}}, SOL)
        self.assertAlmostEqual(v['walletUsd'], 6.0 + 150.0)

    def test_an_unknown_quote_price_gives_no_dollar_figure(self):
        bal = dict(self.BAL, quoteUsd=None, walletUsd=None)
        v = wallets.sleeve(bal, 'sol-usdc', SOL, USDC, THREE, {'mu-usdc': {USDC: 30.0}}, SOL)
        self.assertIsNone(v['walletUsd']); self.assertEqual(v['balanceB'], 70.0)

    def test_an_overdraw_is_marked(self):
        v = wallets.sleeve(self.BAL, 'mu-usdc', MU, USDC, THREE, {'mu-usdc': {USDC: 300.0}}, SOL)
        self.assertEqual(v['balanceB'], 100.0)
        self.assertAlmostEqual(v['sleeveOverdraw'][USDC], 200.0)

    def test_the_swap_caps(self):
        caps = wallets.sleeve_caps({'balanceA': 1.5, 'balanceB': -2.0}, MU, USDC)
        self.assertEqual(caps, {MU: 1.5, USDC: 0.0})
        self.assertEqual(wallets.sleeve_caps({'balanceA': 0.5, 'balanceB': 3.0}, MU, USDC), {MU: 0.5, USDC: 3.0})
        self.assertEqual(wallets.sleeve_caps({'balanceA': None}, MU, '0xABab'), {MU: 0.0, '0xabab': 0.0})

    def test_unchanged_balances_return_the_signers_walletusd_to_the_last_digit(self):
        bal = dict(self.BAL, walletUsd=400.123456)
        self.assertEqual(wallets.sleeve(bal, 'sol-usdc', SOL, USDC, [SOLP], {}, SOL)['walletUsd'], 400.123456)

    def test_a_change_on_side_a_alone_is_revalued(self):
        # USDC as token A, claimed by mu-usdc; MU (token B) wholly its own
        bal = {'balanceA': 100.0, 'balanceB': 5.0, 'price': 0.1, 'quoteUsd': 2.0, 'nativeSide': None,
               'walletUsd': (100.0 * 0.1 + 5.0) * 2.0}
        v = wallets.sleeve(bal, 'mu-usdc', USDC, MU, [SOLP, prof('mu-usdc', [USDC, MU], MU)],
                           {'mu-usdc': {USDC: 30.0}}, SOL)
        self.assertEqual((v['balanceA'], v['balanceB']), (30.0, 5.0))
        self.assertAlmostEqual(v['walletUsd'], (30.0 * 0.1 + 5.0) * 2.0)
        self.assertNotIn('sleeveOverdraw', v)

    def test_a_change_on_side_a_with_native_b_keeps_the_native_figure(self):
        # a pool ordered USDC/SOL: A shared (claims), B native SOL wholly sol-usdc's
        bal = {'sol': 2.0, 'balanceA': 100.0, 'balanceB': 2.0, 'price': 1 / 150.0, 'quoteUsd': 150.0,
               'nativeSide': 'B', 'walletUsd': (100.0 / 150.0 + 2.0) * 150.0}
        v = wallets.sleeve(bal, 'sol-usdc', USDC, SOL, THREE, {'mu-usdc': {USDC: 30.0}}, SOL)
        self.assertEqual((v['balanceA'], v['balanceB']), (70.0, 2.0))
        self.assertAlmostEqual(v['walletUsd'], (70.0 / 150.0 + 2.0) * 150.0)

    def test_an_overdraw_on_side_a_is_marked(self):
        bal = {'balanceA': 10.0, 'balanceB': 5.0, 'price': 0.1, 'quoteUsd': 1.0, 'nativeSide': None, 'walletUsd': 1.5}
        v = wallets.sleeve(bal, 'mu-usdc', USDC, MU, [SOLP, prof('mu-usdc', [USDC, MU], MU)],
                           {'mu-usdc': {USDC: 25.0}}, SOL)
        self.assertAlmostEqual(v['sleeveOverdraw'][USDC], 15.0)

    def test_nothing_shared_but_gas_another_profile_owns_is_left_out(self):
        # mu-usdc holds the residual of USDC and owns MU: its balances are the
        # wallet's, yet the SOL sol-usdc owns is not in its dollar figure
        bal = {'sol': 1.0, 'balanceA': 2.0, 'balanceB': 10.0, 'price': 10.0, 'quoteUsd': 1.0, 'nativeSide': None,
               'walletUsd': 2.0 * 10 + 10 + 150.0}
        owner = prof('mu-usdc', [MU, USDC], MU, residual=True)
        v = wallets.sleeve(bal, 'mu-usdc', MU, USDC, [owner, prof('sol-usdc', [SOL, USDC], SOL)], {}, SOL)
        self.assertEqual((v['balanceA'], v['balanceB']), (2.0, 10.0))
        self.assertAlmostEqual(v['walletUsd'], 30.0)

    def test_the_sole_user_of_the_native_token_owns_the_gas_not_the_residual_owner(self):
        # sol pool's profile q uses SOL alone without depositing it; the
        # residual owner r (MU/USDC, no native side) must not count the SOL
        q = prof('q', [SOL, USDC])
        r = prof('r', [MU, USDC], MU, residual=True)
        bal = {'sol': 1.0, 'balanceA': 2.0, 'balanceB': 10.0, 'price': 10.0, 'quoteUsd': 1.0, 'nativeSide': None,
               'walletUsd': 30.0 + 150.0}
        v = wallets.sleeve(bal, 'r', MU, USDC, [q, r], {'q': {USDC: 4.0}}, SOL)
        self.assertAlmostEqual(v['walletUsd'], 2.0 * 10 + 6.0)

    def test_quote_and_price_unknown_or_missing_balances(self):
        claims = {'mu-usdc': {USDC: 30.0}}
        for over in ({'quoteUsd': None}, {'price': None}):
            v = wallets.sleeve(dict(self.BAL, **over), 'sol-usdc', SOL, USDC, THREE, claims, SOL)
            self.assertIsNone(v['walletUsd'])
        v = wallets.sleeve(dict(self.BAL, balanceA=None, balanceB=None), 'sol-usdc', SOL, USDC, THREE, claims, SOL)
        self.assertEqual((v['balanceA'], v['balanceB']), (0.0, 0.0))
        v = wallets.sleeve(self.BAL, 'not-on-this-wallet', SOL, USDC, THREE, claims, SOL)
        self.assertEqual((v['balanceA'], v['balanceB']), (0.0, 0.0))     # a profile not in the list sees nothing


# --- the database ------------------------------------------------------------------------

def setup_wallet():
    with db.cursor(commit=True) as cur:
        cur.execute('delete from wallet_claims where wallet_id = %s', (WALLET,))
        cur.execute('delete from wallet_settle where wallet_id = %s', (WALLET,))
        cur.execute('insert into wallets (id, chain, address, secret_env) values (%s, %s, %s, %s) '
                    'on conflict (id) do nothing', (WALLET, 'solana', ADDRESS, 'WALLET_SECRET_PATH'))
        for name in ('wt-a', 'wt-b'):
            cur.execute("insert into config (name, pool, pair_label, capital_usd, max_usd, wallet_id) "
                        "values (%s, 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE', 'X/USDC', 100, 200, %s) "
                        "on conflict (name) do update set wallet_id = excluded.wallet_id", (name, WALLET))


def teardown_wallet():
    with db.cursor(commit=True) as cur:
        cur.execute('delete from wallet_claims where wallet_id = %s', (WALLET,))
        cur.execute('delete from wallet_settle where wallet_id = %s', (WALLET,))
        cur.execute("delete from config where name in ('wt-a', 'wt-b')")
        cur.execute('delete from wallets where id = %s', (WALLET,))


class Claims(unittest.TestCase):
    def setUp(self):
        setup_wallet(); self.addCleanup(teardown_wallet)

    def test_adjust_floors_at_zero_and_reports_the_overdraw(self):
        self.assertEqual(wallets.adjust(WALLET, 'wt-a', {USDC: 12.5}), {USDC: (12.5, 0.0)})
        self.assertEqual(wallets.adjust(WALLET, 'wt-a', {USDC: -2.5}), {USDC: (10.0, 0.0)})
        self.assertEqual(wallets.adjust(WALLET, 'wt-a', {USDC: -15.0}), {USDC: (0.0, 5.0)})
        self.assertEqual(wallets.claims(WALLET), {'wt-a': {USDC: 0.0}})

    def test_claims_are_per_profile_and_mint(self):
        wallets.adjust(WALLET, 'wt-a', {USDC: 1.0, MU: 2.0})
        wallets.adjust(WALLET, 'wt-b', {USDC: 3.0})
        self.assertEqual(wallets.claims(WALLET), {'wt-a': {USDC: 1.0, MU: 2.0}, 'wt-b': {USDC: 3.0}})

    def test_mints_are_registered_and_read_back(self):
        wallets.register_mints('wt-a', [MU, USDC])
        with db.cursor(commit=True) as cur:
            cur.execute("update config set enabled = true where name in ('wt-a', 'wt-b')")
        rows = {p['name']: p for p in wallets.wallet_profiles(WALLET)}
        self.assertEqual(rows['wt-a']['mints'], [MU, USDC]); self.assertIsNone(rows['wt-b']['mints'])

    def test_a_disabled_profile_is_still_one_of_the_wallets(self):
        # its claim and its mints stay its own (fail closed): the split and
        # the sweep must keep counting them
        self.assertEqual([(p['name'], p['enabled']) for p in wallets.wallet_profiles(WALLET)],
                         [('wt-a', False), ('wt-b', False)])


class Lock(unittest.TestCase):
    def test_held_and_released_on_every_exit(self):
        with wallets.wallet_lock(WALLET, wait_s=1):
            pass
        with self.assertRaises(RuntimeError):
            with wallets.wallet_lock(WALLET, wait_s=1):
                raise RuntimeError('the write failed')
        with wallets.wallet_lock(WALLET, wait_s=1):        # free again
            pass

    def test_a_busy_lock_times_out_with_a_clear_error_and_never_runs_the_block(self):
        ran = []
        with wallets.wallet_lock(WALLET, wait_s=1):
            t0 = time.monotonic()
            with self.assertRaisesRegex(wallets.LockTimeout, f'wallet {WALLET} lock busy for 0 s'):
                with wallets.wallet_lock(WALLET, wait_s=0.3, poll_s=0.05):
                    ran.append(1)
            self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(ran, [])

    def test_the_lock_holds_no_transaction_open(self):
        # A session held for minutes must be idle, not "idle in transaction":
        # a server timeout on open transactions would end it and drop the lock.
        with wallets.wallet_lock(WALLET, wait_s=1):
            with db.cursor() as cur:
                cur.execute("""select a.state from pg_locks l join pg_stat_activity a on a.pid = l.pid
                               where l.locktype = 'advisory' and l.classid = %s and l.pid <> pg_backend_pid()
                                 and a.datname = current_database()""",           # other test databases lock too
                            (wallets.LOCK_NAMESPACE,))
                self.assertEqual([r['state'] for r in cur.fetchall()], ['idle'])

    def test_another_wallet_is_another_lock(self):
        with wallets.wallet_lock(WALLET, wait_s=1):
            with wallets.wallet_lock('another-wallet', wait_s=0.3):
                pass

    def test_a_holder_that_dies_releases_the_lock(self):
        # A process killed between its balance reads (SIGKILL: no finally runs)
        # must not wedge the wallet: the session ends, the lock goes with it.
        code = ('import sys, time; sys.path.insert(0, %r); import wallets\n'
                'with wallets.wallet_lock(%r):\n'
                '    print("held", flush=True); time.sleep(60)\n') % (str(_fixtures.ROOT), WALLET)
        p = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True, env=os.environ)
        try:
            self.assertEqual(p.stdout.readline().strip(), 'held')
            with self.assertRaises(wallets.LockTimeout):
                with wallets.wallet_lock(WALLET, wait_s=0.3, poll_s=0.05):
                    pass
            p.kill(); p.wait(10)
            with wallets.wallet_lock(WALLET, wait_s=10, poll_s=0.05):
                pass
        finally:
            if p.poll() is None:
                p.kill(); p.wait(10)
            p.stdout.close()

    def test_an_unreachable_database_is_a_lock_error(self):
        saved = db.DSN
        db.DSN = 'dbname=no_such_database_test'
        try:
            with self.assertRaisesRegex(wallets.LockError, 'lock unavailable'):
                with wallets.wallet_lock(WALLET, wait_s=0.3):
                    pass
        finally:
            db.DSN = saved


class Interleaved(unittest.TestCase):
    """Two simulated profiles of one wallet, each in its own thread, write
    against one shared balance. Every write takes the lock, reads before,
    moves tokens, reads after and books the difference to its own claim. The
    claims must end as each profile's own sum (floored), whatever the
    interleaving, and the partition must hold."""

    def setUp(self):
        setup_wallet(); self.addCleanup(teardown_wallet)

    def test_claims_follow_each_profiles_own_moves(self):
        wallet = {USDC: 1000.0}
        moves = {'wt-a': [5.0, -2.0, 7.5, -1.0, 3.0, -4.0], 'wt-b': [10.0, -3.0, 2.0, -6.0, 1.5, 0.5]}
        errors = []

        def run(name):
            try:
                for d in moves[name]:
                    with wallets.wallet_lock(WALLET, wait_s=30, poll_s=0.01):
                        before = wallet[USDC]
                        time.sleep(0.01)                       # another thread would interleave here
                        wallet[USDC] = before + d
                        after = wallet[USDC]
                        wallets.adjust(WALLET, name, {USDC: after - before})
            except Exception as e:                             # pragma: no cover - reported below
                errors.append(e)

        ts = [threading.Thread(target=run, args=(n,)) for n in moves]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(errors, [])
        c = wallets.claims(WALLET)
        self.assertAlmostEqual(c['wt-a'][USDC], sum(moves['wt-a']))
        self.assertAlmostEqual(c['wt-b'][USDC], sum(moves['wt-b']))
        ps = [prof('wt-a', [MU, USDC], MU), prof('wt-b', [DJT, USDC], DJT), prof('owner', [SOL, USDC], SOL, True)]
        views, over, _ = wallets.split(wallet[USDC], USDC, ps, c)
        self.assertEqual(over, 0.0)
        self.assertAlmostEqual(views['owner'], 1000.0)          # the owner's money never moved
        self.assertAlmostEqual(sum(views.values()), wallet[USDC])


class Reads(unittest.TestCase):
    def test_a_failed_read_is_none_never_a_guess(self):
        def boom(*a, **k):
            raise OSError('rpc down')
        saved = wallets.READERS['solana']
        wallets.READERS['solana'] = boom
        try:
            self.assertIsNone(wallets.read_balances('solana', 'http://x', ADDRESS, [USDC], SOL, tries=2))
        finally:
            wallets.READERS['solana'] = saved

    def test_no_owner_or_unknown_chain_reads_nothing(self):
        calls = []
        saved = wallets.READERS['solana']
        wallets.READERS['solana'] = lambda *a: calls.append(a) or 1.0
        sleeps, saved_sleep = [], wallets.time.sleep
        wallets.time.sleep = sleeps.append
        try:
            self.assertIsNone(wallets.read_balances('solana', 'http://x', None, [USDC], SOL))
            self.assertIsNone(wallets.read_balances('solana', 'http://x', '', [USDC], SOL))
            self.assertIsNone(wallets.read_balances('mars', 'http://x', ADDRESS, [USDC], SOL))
        finally:
            wallets.READERS['solana'], wallets.time.sleep = saved, saved_sleep
        self.assertEqual((calls, sleeps), ([], []))                       # not one request, not one retry

    def test_two_tries_ten_seconds_each_then_none(self):
        calls, sleeps = [], []

        def flaky(url, owner, mint, native, timeout, at):
            calls.append(timeout)
            if len(calls) == 1:
                raise OSError('one blip')
            return 3.5, 77
        saved, saved_sleep = wallets.READERS['solana'], wallets.time.sleep
        wallets.READERS['solana'], wallets.time.sleep = flaky, sleeps.append
        try:
            self.assertEqual(wallets.read_balances('solana', 'u', ADDRESS, [USDC], SOL), ({USDC: 3.5}, 77))
            self.assertEqual((calls, sleeps), ([10, 10], [1.0]))
            calls.clear()
            wallets.READERS['solana'] = lambda *a: calls.append(a) or (_ for _ in ()).throw(OSError('down'))
            self.assertIsNone(wallets.read_balances('solana', 'u', ADDRESS, [USDC], SOL))
            self.assertEqual(len(calls), 2)
        finally:
            wallets.READERS['solana'], wallets.time.sleep = saved, saved_sleep

    def test_the_solana_reader_sums_ui_amounts_and_native_lamports(self):
        calls = []

        def rpc(url, method, params, timeout):
            calls.append(method)
            if method == 'getBalance':
                return {'context': {'slot': 90}, 'value': 1_500_000_000}
            return {'context': {'slot': 91},
                    'value': [{'account': {'data': {'parsed': {'info': {'tokenAmount': {'uiAmountString': '1.25'}}}}}},
                              {'account': {'data': {'parsed': {'info': {'tokenAmount': {'uiAmountString': '0.75'}}}}}}]}
        saved = wallets._rpc
        wallets._rpc = rpc
        try:
            self.assertEqual(wallets.read_balances('solana', 'u', ADDRESS, [USDC, SOL], SOL),
                             ({USDC: 2.0, SOL: 1.5}, 90))                     # the oldest read's slot
        finally:
            wallets._rpc = saved
        self.assertEqual(calls, ['getTokenAccountsByOwner', 'getBalance'])

    def test_the_evm_reader_counts_native_eth_as_weth(self):
        weth, owner = '0x4200000000000000000000000000000000000006', '0x' + 'ab' * 20

        seen = []

        def rpc(url, method, params, timeout):
            if method == 'eth_blockNumber':
                return hex(500)
            if method == 'eth_getBalance':
                assert params[1] == hex(500)
                return hex(2 * 10 ** 18)
            data = params[0]['data']
            assert params[1] == hex(500)                                   # every read pinned to one block
            seen.append(data)
            return hex(18) if data == '0x313ce567' else hex(5 * 10 ** 17)
        saved = wallets._rpc
        wallets._rpc = rpc
        try:
            self.assertEqual(wallets.read_balances('base', 'u', owner, [weth], weth), ({weth: 2.5}, 500))
        finally:
            wallets._rpc = saved
        # balanceOf(owner): the selector and the owner left-padded to 32 bytes
        self.assertEqual(seen, ['0x313ce567', '0x70a08231' + '0' * 24 + 'ab' * 20])


if __name__ == '__main__':
    unittest.main()


class Node(unittest.TestCase):
    """The real wallets._rpc against a JSON-RPC server that, like a Solana
    node, answers finalized commitment (the default) ~32 slots behind
    confirmed: the claims must be read at confirmed, with the slot."""

    def setUp(self):
        import http.server
        import threading
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(req)
                m, params = req['method'], req['params']
                confirmed = isinstance(params[-1], dict) and params[-1].get('commitment') == 'confirmed'
                slot = 132 if confirmed else 100
                if m == 'getBalance':
                    res = {'context': {'slot': slot}, 'value': 2_000_000_000 if confirmed else 1_000_000_000}
                elif m == 'getTokenAccountsByOwner':
                    ui = '57.5' if confirmed else '7.5'                   # the swap's USDC: confirmed only
                    res = {'context': {'slot': slot}, 'value': [{'account': {'data': {'parsed': {'info': {
                        'tokenAmount': {'uiAmountString': ui}}}}}}]}
                elif m == 'getSignatureStatuses':
                    history = isinstance(params[-1], dict) and params[-1].get('searchTransactionHistory')
                    res = {'value': [{'slot': 131, 'confirmationStatus': 'confirmed', 'err': None} if history else None
                                     for _ in params[0]]}
                else:
                    res = None
                body = json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': res}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

        self.server = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def test_balances_are_read_at_confirmed_with_their_slot(self):
        got = wallets.read_balances('solana', self.url, ADDRESS, [USDC, SOL], SOL)
        self.assertEqual(got, ({USDC: 57.5, SOL: 2.0}, 132))
        self.assertTrue(all(r['params'][-1].get('commitment') == 'confirmed' for r in self.seen))

    def test_a_write_slot_comes_from_its_confirmed_status_searching_history(self):
        self.assertEqual(wallets.write_slot('solana', self.url, ['SIG1', 'SIG2']), 131)
        self.assertIsNone(wallets.write_slot('solana', self.url, []))
        self.assertIsNone(wallets.write_slot('solana', 'http://127.0.0.1:9', ['SIG1']))   # unreachable
