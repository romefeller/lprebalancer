"""Edges the mutation run found untested (2026-10-02): the command a mint
refusal names, the fallback swap only behind Jupiter, the swap environment
on a shared wallet, deploy_idle without a price or a wallet read, and a
venue breaker with no failure on record."""
import datetime
import json
import unittest
from unittest import mock

import _fixtures  # noqa: F401
import config
import rebalancer
from test_deploy_all import Patched, bal, SOL, USDC


class MintRefusalCommand(unittest.TestCase):
    def test_the_refusal_names_the_command_not_its_first_argument(self):
        seen = []
        with mock.patch.object(rebalancer, '_chain', lambda *a, **k: (None, 'refused: mint paused (MU)')), \
                mock.patch.object(config, 'WALLET_ID', None), \
                mock.patch.dict(rebalancer.MINT_HOLD, {'why': None}), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: seen.append((ev, kw))), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None):
            rebalancer.chain('open', 'POOL', '--execute', record=False)
        (ev, kw), = seen
        self.assertEqual((ev, kw['command']), ('mint_paused', 'open'))


class SwapEnv(Patched):
    REC = {'token_a': {'address': SOL, 'decimals': 9, 'symbol': 'SOL'},
           'token_b': {'address': USDC, 'decimals': 6, 'symbol': 'USDC'}}
    LOP = bal(0.2, 200.0)                                  # SOL short: needs a swap

    def go(self, rec, answers=(({'sent': True, 'signature': 's'}, None),), wallet_id='w'):
        calls = []
        it = iter(answers)
        with mock.patch.object(rebalancer, 'chain', lambda *a, **k: (calls.append((a, k)) or next(it))), \
                mock.patch.object(config, 'WALLET_ID', wallet_id), \
                mock.patch.object(rebalancer, 'wallet', lambda p: dict(self.LOP)), \
                mock.patch.object(rebalancer, 'notify', lambda ev, **kw: None), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer.time, 'sleep', lambda s: None):
            rebalancer.balance_wallet({'failures': 0}, dict(self.LOP), rec)
        return calls

    def test_a_shared_wallet_passes_the_hints_and_the_sleeve(self):
        (a, k), = self.go(self.REC)
        env = k['extra_env']
        self.assertEqual(set(env), {'LPBOT_TOKEN_HINTS', 'LPBOT_SLEEVE'})
        self.assertEqual(json.loads(env['LPBOT_SLEEVE']), {SOL: 0.2, USDC: 200.0})
        self.assertEqual(json.loads(env['LPBOT_TOKEN_HINTS'])[SOL]['decimals'], 9)

    def test_a_shared_wallet_without_hints_passes_the_sleeve_alone(self):
        rec = {'token_a': {'address': SOL}, 'token_b': {'address': USDC}}
        (a, k), = self.go(rec)
        self.assertEqual(set(k['extra_env']), {'LPBOT_SLEEVE'})

    def test_a_venue_swap_never_falls_back_to_orca(self):
        caps = dict(config.CAPS, swap_via='venue')
        with mock.patch.object(rebalancer, 'SWAP_FALLBACK', 'orca-swap'), \
                mock.patch.object(config, 'CAPS', caps), \
                mock.patch.object(config, 'DEX', 'raydium-clmm'):
            calls = self.go(self.REC, answers=((None, 'no route found'), (None, 'no route found')))
        self.assertEqual([k['dex'] for _, k in calls], ['raydium-clmm'])
        with mock.patch.object(rebalancer, 'SWAP_FALLBACK', 'orca-swap'):
            calls = self.go(self.REC, answers=((None, 'no route found'), (None, 'no route found')))
        self.assertEqual([k['dex'] for _, k in calls], ['jupiter', 'orca-swap'])     # the control


class DeployIdleReads(Patched):
    STATUS = {'positionMint': 'M', 'price': 119.5, 'lowerPrice': 116.0, 'upperPrice': 123.0,
              'positionUsd': 212.55, 'rentUsd': 0.0}

    def go(self, wbal):
        """Everything else allows a deploy: an old band, budget, the gap, the swap breaker."""
        moves = []
        opened = rebalancer.datetime.now(rebalancer.timezone.utc) - datetime.timedelta(hours=2)
        with mock.patch.object(rebalancer, 'rebalance', lambda *a, **k: moves.append(a)), \
                mock.patch.object(rebalancer, 'save', lambda s: None), \
                mock.patch.object(rebalancer, 'notify_book', lambda ev, **kw: None), \
                mock.patch.object(rebalancer.db, 'event', lambda *a: None), \
                mock.patch.object(rebalancer, 'calm_budget_left', lambda s: 5), \
                mock.patch.object(rebalancer, 'voluntary_move_allowed', lambda s: True), \
                mock.patch.object(rebalancer.db, 'position_opened', lambda m: opened):
            out = rebalancer.deploy_idle({'idle_baseline': {'mint': 'M', 'usd': 0.0}}, dict(self.STATUS), wbal, None, 119.5)
        return out, moves

    def test_an_unpriced_quote_deploys_nothing(self):
        self.assertEqual(self.go(dict(bal(5.0, 500.0, q=None), walletUsd=1097.5)), (False, []))

    def test_a_read_without_balances_deploys_nothing(self):
        self.assertEqual(self.go({'walletUsd': 600.0, 'balanceB': 500.0, 'price': 119.5, 'quoteUsd': 1.0}), (False, []))
        out, moves = self.go(dict(bal(5.0, 500.0), walletUsd=1097.5))                    # the control
        self.assertEqual((out, len(moves)), (True, 1))
        self.assertEqual(self.go(dict(bal(5.0, 500.0))), (False, []))       # no walletUsd


class FailoverCount(unittest.TestCase):
    def test_a_venue_with_no_failure_on_record_stays(self):
        with mock.patch.object(rebalancer.health, 'allowed', lambda key, now=None: (True, 'green', 0, {})):
            self.assertIs(rebalancer.venue_failover({}, {}), False)


if __name__ == '__main__':
    unittest.main()
