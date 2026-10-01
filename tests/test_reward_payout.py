"""distribute_rewards and txfees.inflow at their edges: the policy gates,
the programs remembered, what the harvest brought, the dust and cap
limits, what the swap is shown to have paid, and the payout or the debt.
test_rewards.Sweep covers the main path; these pin every comparison."""
import json
import os
import unittest
from unittest import mock

import _fixtures
_fixtures.ensure_profile()

import fees        # noqa: E402
import rebalancer  # noqa: E402
import txfees      # noqa: E402

SOL, USDC = fees.NATIVE_MINT, 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
RAY = '4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R'
ORCA = 'orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE'
PROFIT = '8funmDkPNBtjqfNkBEoBF16eBfyQ4vMAFrs4Nyys5D1h'
OWNER = '83HxMUUC7cn5oWKgNvUYCv52MVLUWmaUPFdCrgC4tV2f'
OTHER = '9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM'


def tb(owner, mint, ui=None, raw=None, dec=6):
    amt = {'amount': str(raw if raw is not None else int(round(ui * 10 ** dec))), 'decimals': dec}
    if ui is not None:
        amt['uiAmountString'] = str(ui)
    return {'accountIndex': 1, 'owner': owner, 'mint': mint, 'uiTokenAmount': amt}


def harvest(brought, mint=RAY):
    return {'meta': {'err': None, 'preTokenBalances': [tb(OWNER, mint, 0.0)],
                     'postTokenBalances': [tb(OWNER, mint, brought)]}}


class Run:
    """One distribute_rewards call with every edge it touches recorded."""

    def __init__(self, *, enabled=True, policy='payout', caps_rewards=True, reward_mints=(RAY,), state=None,
                 sol=0.3, owner=OWNER, held=2.0, brought=2.0, price=2.0, min_usd=1.0, max_usd=100.0,
                 payout_mint=USDC, before=({'amount': 10.0}, None), after=({'amount': 13.9}, None),
                 swap=({'signature': 's', 'bought': {'amount': 3.9}}, None), send=({'signature': 't'}, None),
                 theirs=(), address=OWNER):
        self.calls, self.payouts, self.notes = [], [], []
        self.state = {} if state is None else state
        bal = {'owner': owner} if owner else {}
        if sol is not None:
            bal['sol'] = sol
        rec = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}}
        if reward_mints is not None:
            rec['reward_mints'] = list(reward_mints)
        targets = iter([before, after])

        def chain(*a, **k):
            self.calls.append((a, k))
            if a[0] == 'balance' and a[1] not in (SOL, USDC):
                return held if isinstance(held, tuple) else ({'amount': held}, None)
            if a[0] == 'balance':
                return next(targets)
            if a[0] == 'swap':
                return swap
            if a[0] == 'send':
                return send
            raise AssertionError(a)

        caps = dict(rebalancer.config.CAPS, rewards=caps_rewards)
        with mock.patch.dict(os.environ, {'LPBOT_PROFIT_WALLET_PIN': PROFIT}), \
                mock.patch.object(rebalancer.config, 'PAYOUT_ENABLED', enabled), \
                mock.patch.object(rebalancer.config, 'REWARD_POLICY', policy), \
                mock.patch.object(rebalancer.config, 'CAPS', caps), \
                mock.patch.object(rebalancer.config, 'REWARD_MIN_USD', min_usd), \
                mock.patch.object(rebalancer.config, 'REWARD_MAX_USD', max_usd), \
                mock.patch.object(rebalancer.config, 'PAYOUT_MINT', payout_mint), \
                mock.patch.object(rebalancer.config, 'PROFIT_WALLET', PROFIT), \
                mock.patch.object(rebalancer.config, 'WALLET_ADDRESS', address), \
                mock.patch.object(rebalancer.config, 'GAS_RESERVE_SOL', 0.05), \
                mock.patch.object(rebalancer, 'pool_record', lambda: rec), \
                mock.patch.object(rebalancer, 'wallet', lambda p: dict(bal)), \
                mock.patch.object(rebalancer, 'wallet_mints', lambda: set(theirs)), \
                mock.patch.object(txfees, 'fetch', lambda rpc, s, **k: harvest(brought)), \
                mock.patch.object(rebalancer.dexes, 'jupiter_prices', lambda ms: {RAY: price} if price else {}), \
                mock.patch.object(rebalancer, 'chain', chain), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **k: self.notes.append((ev, k))), \
                mock.patch.object(rebalancer.db, 'record_payout', lambda *a, **k: self.payouts.append((a, k))):
            self.out = rebalancer.distribute_rewards(self.state, 'M', ['H'])

    @property
    def ops(self):
        return [a[0] for a, _ in self.calls]

    @property
    def sent(self):
        return [float(a[2]) for a, _ in self.calls if a[0] == 'send']

    @property
    def swapped(self):
        return [float(a[3]) for a, _ in self.calls if a[0] == 'swap']

    @property
    def events(self):
        return [e for e, _ in self.notes]


class Gates(unittest.TestCase):
    def test_each_gate_alone_stops_it(self):
        for over in ({'enabled': False}, {'policy': 'keep'}, {'caps_rewards': False}):
            r = Run(**over)
            self.assertIsNone(r.out, over); self.assertEqual(r.calls, [], over)
        self.assertEqual(Run().ops, ['balance', 'balance', 'swap', 'balance', 'send'])

    def test_no_payout_token_and_gas_fine_sends_nothing(self):
        r = Run(payout_mint=None)
        self.assertIsNone(r.out); self.assertEqual(r.calls, [])


class Programs(unittest.TestCase):
    def test_a_program_remembered_is_swept_when_the_pool_names_none(self):
        r = Run(reward_mints=None, state={'reward_mints_seen': [RAY]})
        self.assertEqual(r.sent, [3.9])

    def test_each_program_is_remembered_once_and_eight_at_most(self):
        r = Run(reward_mints=(RAY, RAY))
        self.assertEqual(r.state['reward_mints_seen'], [RAY])
        many = [m for m in (f'{n}' + RAY[1:] for n in range(2, 10)) if rebalancer.guards.is_address(m)]
        many += [RAY[:1] + f'{n}' + RAY[2:] for n in range(2, 6) if rebalancer.guards.is_address(RAY[:1] + f'{n}' + RAY[2:])]
        self.assertGreaterEqual(len(many), 9)
        r = Run(reward_mints=many)
        self.assertEqual(r.state['reward_mints_seen'], many[-8:])

    def test_a_remembered_entry_that_is_no_address_is_never_swept(self):
        r = Run(reward_mints=(), state={'reward_mints_seen': ['junk']})
        self.assertIsNone(r.out); self.assertEqual(r.calls, [])


class Measured(unittest.TestCase):
    def test_the_owner_falls_back_to_the_configured_address(self):
        r = Run(owner=None)
        self.assertEqual(r.sent, [3.9])
        self.assertEqual(r.state['reward_due'], {RAY: 0.0})

    def test_gas_exactly_at_the_reserve_is_not_low_and_unknown_gas_is(self):
        self.assertEqual(Run(sol=0.05).ops[-1], 'send')
        r = Run(sol=None)
        self.assertEqual(r.ops, ['balance', 'balance', 'swap', 'balance'])
        self.assertEqual([a[2] for a, _ in r.calls if a[0] == 'swap'], [SOL])

    def test_the_usd_value_is_amount_times_price(self):
        r = Run(held=2.0, price=2.0)
        self.assertEqual(r.payouts[0][0][5], 4.0)
        self.assertEqual(r.out, [{'mint': RAY, 'usd': 4.0, 'to': 'profit wallet', 'signature': 't'}])

    def test_no_price_is_no_sale(self):
        r = Run(price=None)
        self.assertEqual(r.ops, ['balance']); self.assertEqual(r.out, [])

    def test_nothing_held_is_no_sale_even_with_no_minimum(self):
        self.assertEqual(Run(held=0.0, min_usd=0.0).ops, ['balance'])

    def test_an_unreadable_reward_balance_is_no_sale(self):
        for held in ((None, 'rpc'), ({}, None)):
            r = Run(held=held, price=10.0)
            self.assertEqual(r.ops, ['balance'], held)

    def test_less_than_one_token_is_sold_when_worth_it(self):
        r = Run(held=0.5, brought=0.5, price=10.0, swap=({'signature': 's', 'bought': {'amount': 3.9}}, None))
        self.assertEqual(r.swapped, [0.5])

    def test_the_minimum_and_the_cap_are_inclusive(self):
        self.assertEqual(Run(held=0.5, brought=0.5, price=2.0, min_usd=1.0).swapped, [0.5])     # exactly $1
        self.assertEqual(Run(held=2.0, price=2.0, max_usd=4.0).swapped, [2.0])                  # exactly the cap
        self.assertEqual(Run(held=2.0, price=2.0, max_usd=3.99).events, ['reward_held'])


class Paid(unittest.TestCase):
    def test_what_arrived_is_paid_when_the_quote_promised_more(self):
        r = Run(swap=({'signature': 's', 'bought': {'amount': 5.0}}, None))
        self.assertEqual(r.sent, [3.9])

    def test_the_quote_caps_what_is_paid(self):
        self.assertEqual(Run(swap=({'signature': 's', 'bought': {'amount': 1.5}}, None)).sent, [1.5])

    def test_an_unread_before_counts_from_zero(self):
        r = Run(before=(None, 'rpc'), after=({'amount': 3.0}, None), swap=({'signature': 's', 'bought': {'amount': 5.0}}, None))
        self.assertEqual(r.sent, [3.0])
        r = Run(before=({}, None), after=({'amount': 3.0}, None), swap=({'signature': 's', 'bought': {'amount': 5.0}}, None))
        self.assertEqual(r.sent, [3.0])

    def test_an_unread_after_pays_nothing(self):
        for after in ((None, 'rpc'), ({}, None)):
            r = Run(before=({}, None), after=after, swap=({'signature': 's', 'bought': {'amount': 5.0}}, None))
            self.assertEqual(r.sent, [], after); self.assertEqual(r.events, ['reward_swap_failed'], after)

    def test_a_balance_that_fell_pays_nothing(self):
        r = Run(before=({'amount': 10.0}, None), after=({'amount': 9.0}, None))
        self.assertEqual((r.sent, r.events), ([], ['reward_swap_failed']))

    def test_nothing_measured_pays_nothing(self):
        r = Run(before=({'amount': 10.0}, None), after=({'amount': 10.0}, None))
        self.assertEqual((r.sent, r.events), ([], ['reward_swap_failed']))

    def test_half_a_token_measured_is_paid(self):
        r = Run(before=({'amount': 10.0}, None), after=({'amount': 10.5}, None))
        self.assertEqual(r.sent, [0.5])

    def test_a_swap_without_a_quote_pays_nothing(self):
        r = Run(swap=({'signature': 's'}, None))
        self.assertEqual((r.sent, r.events), ([], ['reward_swap_failed']))

    def test_every_swap_failure_sends_nothing(self):
        for swap in (({'signature': 's', 'bought': {'amount': 3.9}}, 'late error'),
                     ({'bought': {'amount': 3.9}}, None), (None, None), (None, 'impact')):
            r = Run(swap=swap)
            self.assertEqual((r.sent, r.events), ([], ['reward_swap_failed']), swap)
            self.assertEqual(r.state['reward_due'], {RAY: 2.0}, swap)

    def test_what_is_left_due_after_a_partial_sale(self):
        r = Run(held=1.5, brought=2.0)
        self.assertEqual(r.swapped, [1.5]); self.assertAlmostEqual(r.state['reward_due'][RAY], 0.5)


class Debt(unittest.TestCase):
    def test_a_failed_send_is_owed(self):
        r = Run(send=({}, None))
        self.assertEqual(r.state['payout_owed'], {USDC: 3.9})
        (args, kw), = r.payouts
        self.assertEqual((args[6], kw['detail']), ('owed', 'no signature'))
        r = Run(send=({'signature': 't'}, 'confirm timeout'), state={'payout_owed': {USDC: 1.0}})
        self.assertAlmostEqual(r.state['payout_owed'][USDC], 4.9)
        self.assertEqual(r.payouts[0][1]['detail'], 'confirm timeout')
        r = Run(send=(None, None))
        self.assertEqual(r.payouts[0][0][6], 'owed')
        r = Run(send=({'status': 'unknown'}, None))                       # an answer without a signature
        self.assertEqual((r.payouts[0][0][6], r.state['payout_owed']), ('owed', {USDC: 3.9}))
        self.assertEqual(r.events, ['payout_failed'])

    def test_the_summary_only_when_something_was_paid(self):
        r = Run()
        self.assertEqual(r.events, ['REWARD_PAYOUT']); self.assertEqual(len(r.out), 1)
        r = Run(held=0.1)
        self.assertEqual((r.events, r.out), ([], []))


class Inflow(unittest.TestCase):
    def test_ui_units(self):
        self.assertEqual(txfees._ui({'amount': '1500000', 'decimals': 6, 'uiAmountString': '2.5'}), 2.5)
        self.assertEqual(txfees._ui({'amount': '1500000', 'decimals': 6}), 1.5)
        self.assertEqual(txfees._ui({'amount': '1500000', 'decimals': 6, 'uiAmountString': ''}), 1.5)
        self.assertEqual(txfees._ui({'amount': '1500000', 'decimals': 6, 'uiAmountString': None}), 1.5)

    def test_post_less_pre_of_the_owner_and_mint_only(self):
        tx = {'meta': {'preTokenBalances': [tb(OWNER, RAY, 1.0), tb(OTHER, RAY, 0.0), tb(OWNER, ORCA, 0.0)],
                       'postTokenBalances': [tb(OWNER, RAY, 3.0), tb(OTHER, RAY, 50.0), tb(OWNER, ORCA, 7.0)]}}
        self.assertEqual(txfees.inflow('u', ['a'], OWNER, [RAY], fetcher=lambda rpc, s: tx), {RAY: 2.0})

    def test_no_signature_or_owner_is_unmeasured(self):
        f = lambda rpc, s: harvest(1.0)
        self.assertIsNone(txfees.inflow('u', None, OWNER, [RAY], fetcher=f))
        self.assertIsNone(txfees.inflow('u', ['', None], OWNER, [RAY], fetcher=f))
        self.assertIsNone(txfees.inflow('u', ['a'], None, [RAY], fetcher=f))

    def test_an_unreadable_transaction_is_unmeasured(self):
        for tx in (None, {'meta': None}, {}):
            self.assertIsNone(txfees.inflow('u', ['a'], OWNER, [RAY], fetcher=lambda rpc, s: tx), tx)

    def test_a_transaction_without_balances_brought_nothing(self):
        self.assertEqual(txfees.inflow('u', ['a'], OWNER, [RAY], fetcher=lambda rpc, s: {'meta': {}}), {RAY: 0.0})

    def test_the_given_fetcher_is_used(self):
        with mock.patch.object(txfees, 'fetch', side_effect=AssertionError('the default fetcher')):
            self.assertEqual(txfees.inflow('u', ['a'], OWNER, [RAY], fetcher=lambda rpc, s: harvest(1.0)), {RAY: 1.0})

    def test_raw_amounts_without_ui_strings(self):
        tx = {'meta': {'preTokenBalances': [], 'postTokenBalances': [tb(OWNER, RAY, raw=2_500_000, dec=6)]}}
        self.assertEqual(txfees.inflow('u', ['a'], OWNER, [RAY], fetcher=lambda rpc, s: tx), {RAY: 2.5})


if __name__ == '__main__':
    unittest.main()
