"""A vault's swaps in a deeper approved pool (sql/060, lp.swaps.vault_swap_pool)."""
import unittest
from unittest import mock

import _fixtures  # noqa: F401  (first: it puts lp_bot on the path)
import config
import lp.swaps

HELD, DEEP, OTHER = '8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj', '3ucNos4NbumPLZNWztqGHNFFgkHeRMBQAVemeeomsUxv', 'Czfq3xZZDmsdGdUyrNLtRhGc47cXcZtLG4crryfu44zE'
VAULT = {'kind': 'squads', 'venues': {'raydium-clmm': {'pools': [HELD, DEEP], 'policies': ['p'], 'payouts': []}}}


class VaultSwapPool(unittest.TestCase):
    def pick(self, swap_pool=DEEP, vault=VAULT, dex='raydium-clmm', swap_dex='raydium-clmm', pool=HELD):
        with mock.patch.object(config, 'SWAP_POOL', swap_pool), mock.patch.object(config, 'VAULT', vault), \
                mock.patch.object(config, 'DEX', dex), mock.patch.object(config, 'POOL', pool):
            return lp.swaps.vault_swap_pool(swap_dex)

    def test_a_vault_swaps_in_its_approved_deep_pool(self):
        self.assertEqual(self.pick(), DEEP)

    def test_otherwise_the_held_pool(self):
        self.assertIsNone(self.pick(swap_pool=None))                       # not set
        self.assertIsNone(self.pick(vault=None))                           # a key wallet routes through Jupiter
        self.assertIsNone(self.pick(swap_dex='jupiter'))                   # not the venue signer
        self.assertIsNone(self.pick(swap_pool=OTHER))                      # not approved by the owners
        self.assertIsNone(self.pick(swap_pool=HELD))                       # the held pool itself
        self.assertIsNone(self.pick(vault={'kind': 'squads', 'venues': {}}))


if __name__ == '__main__':
    unittest.main()
