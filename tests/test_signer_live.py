"""The signer against mainnet, read-only. Every command here either reads or
builds instructions without sending them; nothing costs a lamport.

Needs WALLET_SECRET_PATH (the key is loaded to derive the address; a dry-run
open still needs a funder to quote against). Skipped without it.
"""
import json
import os
import pathlib
import subprocess
import unittest

import _fixtures  # noqa: F401
from _fixtures import ADAPTIVE_POOL, BTC_QUOTE_POOL, LIVE_POOL, ROOT

SIGNER = str(ROOT / 'signer2.mjs')
HAVE_KEY = bool(os.environ.get('WALLET_SECRET_PATH')) and \
    pathlib.Path(os.environ.get('WALLET_SECRET_PATH', '')).exists()


def run(*args, timeout=180, retries=1):
    """Run the signer. A read that fails on a rate limit is retried once, so
    that the suite tests the signer and not the public RPC's mood."""
    import time
    for attempt in range(retries + 1):
        r = subprocess.run(['node', SIGNER, *args], capture_output=True, text=True, timeout=timeout)
        text = r.stdout + r.stderr
        i = text.find('{')
        j = None
        if i >= 0:
            try:
                j = json.loads(text[i:text.rindex('}') + 1])
            except Exception:
                j = None
        if r.returncode == 0 or attempt == retries or 'rate' not in text.lower() and '429' not in text:
            return r.returncode, j, text
        time.sleep(5)
    return r.returncode, j, text


class PoolInfo(unittest.TestCase):
    def test_live_pool_describes_itself(self):
        rc, j, _ = run('pool', LIVE_POOL)
        self.assertEqual(rc, 0)
        self.assertEqual((j['symbolA'], j['symbolB']), ('SOL', 'USDC'))
        self.assertEqual((j['decimalsA'], j['decimalsB']), (9, 6))
        self.assertFalse(j['adaptiveFee'])
        self.assertAlmostEqual(j['feeRate'], 0.0004)
        self.assertGreater(j['price'], 1)

    def test_adaptive_pool_is_flagged(self):
        rc, j, _ = run('pool', ADAPTIVE_POOL)
        self.assertEqual(rc, 0)
        self.assertTrue(j['adaptiveFee'])

    def test_unknown_pool_fails_loudly(self):
        rc, j, text = run('pool', 'So11111111111111111111111111111111111111112')
        self.assertNotEqual(rc, 0)
        self.assertIn('could not read pool', text)


@unittest.skipUnless(HAVE_KEY, 'WALLET_SECRET_PATH not set')
class Wallet(unittest.TestCase):
    def test_balance_for_a_dollar_quoted_pool(self):
        rc, j, _ = run('balance', LIVE_POOL)
        self.assertEqual(rc, 0)
        self.assertEqual(j['nativeSide'], 'A')
        self.assertEqual(j['quoteUsd'], 1)
        self.assertAlmostEqual(j['balanceA'], j['sol'])
        self.assertGreaterEqual(j['balanceB'], 0)
        self.assertAlmostEqual(j['walletUsd'], j['balanceA'] * j['price'] + j['balanceB'], places=2)

    def test_balance_for_a_btc_quoted_pool_prices_the_quote(self):
        rc, j, text = run('balance', BTC_QUOTE_POOL)
        self.assertEqual(rc, 0, text[-400:])
        self.assertIsNotNone(j, text[-400:])
        self.assertEqual((j['tokenA'], j['tokenB']), ('SOL', 'cbBTC'))
        self.assertGreater(j['quoteUsd'], 10_000)       # a bitcoin, not the SOL price
        self.assertLess(j['price'], 0.01)                # BTC per SOL
        # the wallet holds no cbBTC; its value is the SOL alone, in dollars
        self.assertEqual(j['balanceB'], 0)
        self.assertAlmostEqual(j['walletUsd'], j['sol'] * j['price'] * j['quoteUsd'], places=1)
        # and SOL priced through BTC agrees with SOL priced in USDC, roughly
        _, u, _ = run('balance', LIVE_POOL)
        self.assertAlmostEqual(j['price'] * j['quoteUsd'] / u['price'], 1.0, delta=0.03)

    def test_bare_balance_is_sol_only(self):
        rc, j, _ = run('balance')
        self.assertEqual(rc, 0)
        self.assertNotIn('balanceA', j)


@unittest.skipUnless(HAVE_KEY, 'WALLET_SECRET_PATH not set')
class Status(unittest.TestCase):
    def test_status_is_parseable_whether_or_not_a_position_exists(self):
        rc, j, _ = run('status')
        self.assertEqual(rc, 0)
        self.assertIn('positionMint', j)
        if j['positionMint']:
            self.assertIn(j['inRange'], (True, False))
            self.assertLess(j['lowerPrice'], j['upperPrice'])
            self.assertGreaterEqual(j.get('feesAccrued_USD', 0), 0)
            # the close quote can 429 under load; when it does, BOTH the mark
            # and the accrual are absent together, never one without the other
            self.assertEqual('positionUsd' in j, 'feesAccruedA' in j)
            # the stale on-chain counter is reported alongside the real accrual
            self.assertIn('feeOwedA', j)


@unittest.skipUnless(HAVE_KEY, 'WALLET_SECRET_PATH not set')
class DryRuns(unittest.TestCase):
    def test_open_builds_instructions_without_sending(self):
        rc, j, text = run('open', LIVE_POOL, '100', '130', '0.05', '5')
        self.assertEqual(rc, 0, text[-300:])
        self.assertFalse(j['sent'])
        self.assertGreater(j['instructions'], 0)
        self.assertIn('DRY RUN', text)
        # the deposit quote: caps 0.05 SOL / 5 USDC on a band around the price;
        # the binding side is fully used, the other partially, and the dollar
        # figure is what those amounts are worth at the pool price
        q = j['quote']
        self.assertIsNotNone(q)
        self.assertIn(q['binding'], ('A', 'B'))
        self.assertLessEqual(j['depositEstA'], 0.05 + 1e-9)
        self.assertLessEqual(j['depositEstB'], 5 + 1e-9)
        self.assertTrue(abs(j['depositEstA'] - 0.05) < 1e-9 or abs(j['depositEstB'] - 5) < 1e-9)
        self.assertGreater(j['depositUsd'], 0)
        self.assertLessEqual(j['depositUsd'], j['approxUsd'] + 1e-6)

    def test_open_refuses_an_adaptive_fee_pool_before_anything_else(self):
        rc, j, text = run('open', ADAPTIVE_POOL, '100', '130', '0.05', '5')
        self.assertNotEqual(rc, 0)
        self.assertIn('adaptive-fee', text)
        self.assertIn('6069', text)

    def test_open_refuses_a_position_over_the_cap(self):
        env = dict(os.environ, LPBOT_MAX_USD='50')
        r = subprocess.run(['node', SIGNER, 'open', LIVE_POOL, '100', '130', '5', '500'],
                           capture_output=True, text=True, timeout=180, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('exceeds cap', r.stdout + r.stderr)

    def test_close_dry_run_builds_instructions_for_the_live_position(self):
        _, s, _ = run('status')
        if not s or not s.get('positionMint'):
            self.skipTest('no open position')
        rc, j, text = run('close', s['positionMint'])
        self.assertEqual(rc, 0, text[-300:])
        self.assertFalse(j['sent'])
        self.assertGreater(j['instructions'], 0)
        self.assertIsNotNone(j['feesQuote'])

    def test_harvest_without_execute_sends_nothing(self):
        rc, j, text = run('harvest', 'anything')
        self.assertEqual(rc, 0)
        self.assertIn('DRY RUN', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
