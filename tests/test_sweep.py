"""Every idle capital goes into the LP (owner, 2026-09-28): foreign tokens in
the LP wallet are swapped into the pool's quote token, then deploy_idle puts
them in the band. Spam, dust, rewards and the pool's own tokens are left."""
import unittest
from unittest import mock

from hypothesis import given, settings, strategies as st

import _fixtures  # noqa: F401
import rebalancer

SOL, USDC = 'So11111111111111111111111111111111111111112', 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
JITO, MSOL, SPAM, RWD = 'J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn', 'mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So', 'SPAM', 'RWD'
POOL = {SOL, USDC}


def acc(mint, amount, dec):
    return {'mint': mint, 'amount': amount, 'decimals': dec, 'lamports': 2039280, 'pubkey': mint + 'acct', 'program': 'T'}


PRICES = {JITO: 150.0, MSOL: 160.0, SPAM: 9.0, RWD: 2.0, 'USDT': 1.0}
FACTS = {JITO: {'verified': True, 'symbol': 'JitoSOL'}, MSOL: {'verified': True, 'symbol': 'mSOL'},
         SPAM: {'verified': False, 'symbol': 'FREE'}, RWD: {'verified': True}, 'USDT': {'verified': True, 'symbol': 'USDT'}}


class Plan(unittest.TestCase):
    def test_a_verified_foreign_token_is_swept(self):
        p = rebalancer.plan_sweep([acc(JITO, 200_000_000, 9)], POOL, set(), PRICES, FACTS)
        self.assertEqual(p, [{'mint': JITO, 'amount': 0.2, 'usd': 30.0, 'symbol': 'JitoSOL'}])

    def test_what_is_never_swept(self):
        accounts = [acc(SOL, 10**9, 9), acc(USDC, 10**6, 6), acc(RWD, 10**9, 6), acc(SPAM, 10**12, 6),
                    acc('NFT', 1, 0), acc(MSOL, 6_000_000, 9), acc(JITO, 0, 9), acc('UNKNOWN', 10**9, 6)]
        # SOL/USDC: the pool's; RWD: a reward; SPAM: unverified; NFT: a position;
        # 0.006 mSOL = $0.96: dust; JITO: empty; UNKNOWN: no price, no facts
        self.assertEqual(rebalancer.plan_sweep(accounts, POOL, {RWD}, PRICES, FACTS), [])

    def test_the_dust_edge(self):
        self.assertEqual(len(rebalancer.plan_sweep([acc('USDT', 1_000_000, 6)], POOL, set(), PRICES, FACTS)), 1)   # exactly $1
        self.assertEqual(rebalancer.plan_sweep([acc('USDT', 999_999, 6)], POOL, set(), PRICES, FACTS), [])

    def test_wrapped_sol_is_not_swapped_to_itself(self):
        self.assertEqual(rebalancer.plan_sweep([acc(SOL, 10**9, 9)], {'OTHERA', USDC}, set(), {SOL: 120.0},
                                               {SOL: {'verified': True}}), [])

    def test_a_token_that_looks_like_an_nft_but_is_not(self):
        p = rebalancer.plan_sweep([acc('USDT', 1, 0), acc('BIG', 2, 0)], POOL, set(), {'USDT': 5.0, 'BIG': 5.0},
                                  {'USDT': {'verified': True}, 'BIG': {'verified': True}})
        self.assertEqual([x['mint'] for x in p], ['BIG'])                  # amount 1 with 0 decimals is skipped

    @settings(max_examples=300, deadline=None)
    @given(st.lists(st.tuples(st.sampled_from([SOL, USDC, JITO, MSOL, SPAM, RWD, 'USDT', 'NFT']),
                              st.integers(0, 10**12), st.sampled_from([0, 6, 9])), max_size=10))
    def test_nothing_forbidden_is_ever_planned(self, rows):
        facts = dict(FACTS, NFT={'verified': True})
        for p in rebalancer.plan_sweep([acc(m, a, d) for m, a, d in rows], POOL, {RWD}, dict(PRICES, NFT=1.0), facts):
            self.assertNotIn(p['mint'], POOL | {RWD, SPAM})
            self.assertGreaterEqual(p['usd'], rebalancer.SWEEP_MIN_USD)
            self.assertTrue(facts[p['mint']]['verified'])


class Sweep(unittest.TestCase):
    BAL = {'owner': '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f', 'balanceA': 1.0, 'balanceB': 1.0, 'price': 120.0}

    def go(self, accounts, answers=(({'signature': 'SW'}, None),), state=None, pool=((SOL, 'SOL'), (USDC, 'USDC'))):
        calls, seen, events = [], [], []
        it = iter(answers)
        state = {} if state is None else state
        with mock.patch.object(rebalancer.audit, 'token_accounts', lambda url, owner: accounts), \
                mock.patch.object(rebalancer.jupiter_api, 'jupiter_prices', lambda ms: {m: PRICES.get(m, 0.0) for m in ms}), \
                mock.patch.object(rebalancer.jupiter_api, 'jupiter_token', lambda m: FACTS.get(m)), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: pool), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append((a, k)) or next(it))), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: events.append(a)), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append((ev, kw))):
            out = rebalancer.sweep_foreign(state, dict(self.BAL))
        return out, calls, seen, events, state

    def test_a_deposit_of_jitosol_is_swapped_into_the_quote_token(self):
        out, calls, seen, events, state = self.go([acc(JITO, 200_000_000, 9), acc(USDC, 5, 6)])
        (a, k), = calls
        self.assertEqual(a, ('swap', JITO, USDC, '0.200000000', '--execute')); self.assertEqual(k, {'dex': 'jupiter'})
        self.assertEqual(out[0]['signature'], 'SW')
        self.assertEqual(seen[0][0], 'SWEEP'); self.assertEqual(seen[0][1]['total_usd'], 30.0)
        self.assertEqual(events[0][0], 'SWEEP')
        self.assertIn('last_sweep', state)

    def test_a_pool_whose_quote_is_sol_sweeps_into_the_other_side(self):
        _, calls, *_ = self.go([acc(JITO, 200_000_000, 9)], pool=((USDC, 'USDC'), (SOL, 'SOL')))
        self.assertEqual(calls[0][0][2], USDC)

    def test_every_ten_minutes_at_most(self):
        state = {}
        self.go([acc(JITO, 200_000_000, 9)], state=state)
        _, calls, *_ = self.go([acc(JITO, 200_000_000, 9)], state=state)
        self.assertEqual(calls, [])
        state['last_sweep'] -= rebalancer.SWEEP_EVERY_S
        _, calls, *_ = self.go([acc(JITO, 200_000_000, 9)], state=state)
        self.assertEqual(len(calls), 1)

    def test_nothing_to_sweep_sends_nothing(self):
        out, calls, seen, _, _ = self.go([acc(USDC, 10**6, 6), acc(SPAM, 10**12, 6)])
        self.assertEqual((out, calls, seen), ([], [], []))
        out, calls, *_ = self.go([])
        self.assertEqual((out, calls), ([], []))

    def test_a_failed_swap_is_reported_and_the_rest_go_on(self):
        out, calls, seen, _, _ = self.go([acc(JITO, 200_000_000, 9), acc(MSOL, 100_000_000, 9)],
                                         answers=((None, 'price impact too high'), ({'signature': 'S2'}, None)))
        self.assertEqual(len(calls), 2); self.assertEqual([d['mint'] for d in out], [MSOL])
        self.assertEqual([e for e, _ in seen], ['sweep_failed', 'SWEEP'])
        out, _, seen, _, _ = self.go([acc(JITO, 200_000_000, 9)], answers=(({}, None),))
        self.assertEqual(out, []); self.assertEqual(seen[0][0], 'sweep_failed')

    def test_only_foreign_non_empty_tokens_are_priced(self):
        """Every price and fact read spends Jupiter budget: the pool's tokens,
        the wallet's other mints and empty accounts are never asked for."""
        asked = []
        accounts = [acc(SOL, 10**9, 9), acc(USDC, 10**6, 6), acc(JITO, 0, 9), acc(MSOL, 1, 9), acc('OTHER', 10**6, 6),
                    acc(SPAM, 10**12, 6)]
        with mock.patch.object(rebalancer, 'wallet_mints', lambda: {'OTHER'}), \
                mock.patch.object(rebalancer, 'housekeeper', lambda chore: True), \
                mock.patch.object(rebalancer.audit, 'token_accounts', lambda url, owner: accounts), \
                mock.patch.object(rebalancer.jupiter_api, 'jupiter_prices', lambda ms: (asked.append(list(ms)) or {})), \
                mock.patch.object(rebalancer.jupiter_api, 'jupiter_token', lambda m: FACTS.get(m)), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: (None, 'no')), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None):
            rebalancer.sweep_foreign({}, dict(self.BAL))
            self.assertEqual(asked, [[MSOL, SPAM]])                # one raw unit counts; 0 does not
            asked.clear()
            accounts[:] = [acc(SOL, 10**9, 9), acc(JITO, 0, 9), acc('OTHER', 10**6, 6)]
            rebalancer.sweep_foreign({}, dict(self.BAL))
            self.assertEqual(asked, [])                            # nothing foreign: no read at all

    def test_rewards_seen_are_left_to_the_payout(self):
        _, calls, *_ = self.go([acc(RWD, 10**9, 6)], state={'reward_mints_seen': [RWD]})
        self.assertEqual(calls, [])

    def test_no_owner_or_a_crash_never_raises(self):
        self.assertEqual(rebalancer.sweep_foreign({}, {}), [])
        seen = []
        with mock.patch.object(rebalancer, 'pool_tokens', side_effect=RuntimeError('api')), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append(ev)):
            self.assertEqual(rebalancer.sweep_foreign({}, dict(self.BAL)), [])
        self.assertEqual(seen, ['sweep_failed'])


if __name__ == '__main__':
    unittest.main()


class PlanEdges(unittest.TestCase):
    def test_a_pool_token_with_value_is_never_swept(self):
        self.assertEqual(rebalancer.plan_sweep([acc(USDC, 50 * 10**6, 6), acc(SOL, 10**9, 9)], POOL, set(),
                                               {USDC: 1.0, SOL: 120.0}, {USDC: {'verified': True}, SOL: {'verified': True}}), [])

    def test_one_raw_unit_of_a_decimal_token_is_no_nft(self):
        p = rebalancer.plan_sweep([acc('PRICEY', 1, 6)], POOL, set(), {'PRICEY': 2_000_000.0}, {'PRICEY': {'verified': True}})
        self.assertEqual([x['mint'] for x in p], ['PRICEY'])

    def test_a_verified_token_without_a_price_or_a_priced_one_without_facts_is_left(self):
        self.assertEqual(rebalancer.plan_sweep([acc('NOPRICE', 10 * 10**6, 6)], POOL, set(), {}, {'NOPRICE': {'verified': True}}), [])
        self.assertEqual(rebalancer.plan_sweep([acc('NOFACTS', 10 * 10**6, 6)], POOL, set(), {'NOFACTS': 1.0}, {}), [])


class SweepEdges(Sweep):
    def test_the_timer_edge_and_its_answer(self):
        state = {'last_sweep': 1000.0}
        with mock.patch.object(rebalancer.time, 'time', lambda: 1000.0 + rebalancer.SWEEP_EVERY_S - 1):
            out, calls, *_ = self.go([acc(JITO, 200_000_000, 9)], state=state)
        self.assertEqual((out, calls), ([], []))
        with mock.patch.object(rebalancer.time, 'time', lambda: 1000.0 + rebalancer.SWEEP_EVERY_S):
            _, calls, *_ = self.go([acc(JITO, 200_000_000, 9)], state=state)
        self.assertEqual(len(calls), 1)
        with mock.patch.object(rebalancer.time, 'time', lambda: float(rebalancer.SWEEP_EVERY_S)):
            _, calls, *_ = self.go([acc(JITO, 200_000_000, 9)], state={})            # never swept: runs
        self.assertEqual(len(calls), 1)

    def test_no_owner_reads_nothing(self):
        calls = []
        with mock.patch.object(rebalancer.audit, 'token_accounts', lambda *a: calls.append('read') or [acc(JITO, 200_000_000, 9)]), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: calls.append('swap')), \
                mock.patch.object(rebalancer, 'save', lambda s: None):
            self.assertEqual(rebalancer.sweep_foreign({}, {'balanceA': 1.0}), [])
        self.assertEqual(calls, [])

    def test_a_signature_with_an_error_is_a_failure(self):
        out, _, seen, events, _ = self.go([acc(JITO, 200_000_000, 9)], answers=(({'signature': 'S'}, 'confirm timeout'),))
        self.assertEqual(out, []); self.assertEqual(seen[0][0], 'sweep_failed'); self.assertEqual(events, [])


class SweepGoesOn(Sweep):
    def test_an_empty_or_missing_answer_fails_that_token_only(self):
        for bad in ((None, None), ({}, None)):
            out, calls, seen, _, _ = self.go([acc(JITO, 200_000_000, 9), acc(MSOL, 100_000_000, 9)],
                                             answers=(bad, ({'signature': 'S2'}, None)))
            self.assertEqual(len(calls), 2, bad); self.assertEqual([d['mint'] for d in out], [MSOL], bad)


class SweepAsksLittle(unittest.TestCase):
    """What sweep_foreign asks Jupiter about: prices for foreign tokens held,
    facts only for those worth a sweep (mutation gaps, 2026-10-01)."""
    BAL = Sweep.BAL

    def ask(self, accounts, prices):
        priced, facts = [], []
        with mock.patch.object(rebalancer.audit, 'token_accounts', lambda url, owner: accounts), \
                mock.patch.object(rebalancer.jupiter_api, 'jupiter_prices',
                                  lambda ms: priced.append(list(ms)) or {m: prices[m] for m in ms if m in prices}), \
                mock.patch.object(rebalancer.jupiter_api, 'jupiter_token', lambda m: facts.append(m) or {'verified': True}), \
                mock.patch.object(rebalancer, 'pool_tokens', lambda: ((SOL, 'SOL'), (USDC, 'USDC'))), \
                mock.patch.object(rebalancer, 'chain', lambda *a, **k: ({'signature': 'SW'}, None)), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None):
            out = rebalancer.sweep_foreign({}, dict(self.BAL))
        return priced, facts, [d['mint'] for d in out]

    def test_pool_tokens_and_empty_accounts_are_not_priced(self):
        priced, facts, out = self.ask([acc(SOL, 10 ** 9, 9), acc(USDC, 10 ** 6, 6), acc(JITO, 0, 9)], PRICES)
        self.assertEqual((priced, facts, out), ([], [], []))                   # no Jupiter call at all

    def test_one_raw_unit_is_priced(self):
        priced, *_ = self.ask([acc(SOL, 10 ** 9, 9), acc(MSOL, 1, 9)], PRICES)
        self.assertEqual(priced, [[MSOL]])

    def test_facts_from_exactly_sweep_min_usd(self):
        self.assertEqual(rebalancer.SWEEP_MIN_USD, 1.0)
        _, facts, _ = self.ask([acc('USDT', 1_000_000, 6), acc('TINY', 100, 6)], {'USDT': 1.0, 'TINY': 1.0})
        self.assertEqual(facts, ['USDT'])                                      # $1.00 exactly; $0.0001 is dust

    def test_a_token_without_a_price_is_dust_and_the_rest_go_on(self):
        _, facts, out = self.ask([acc('NOPRICE', 5_000_000, 6), acc(JITO, 200_000_000, 9)],
                                 {'NOPRICE': None, JITO: 150.0})
        self.assertEqual((facts, out), ([JITO], [JITO]))
