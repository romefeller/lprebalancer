"""Addresses live in data files, not in code: chains/solana/solana.json for
Solana mints and programs, venues/<venue>/venue.json for a venue's pools.
This test fails when one of those addresses is written into a script again."""
import json
import pathlib
import unittest

import _fixtures

ROOT = _fixtures.ROOT
SOLANA = json.loads((ROOT / 'chains/solana/solana.json').read_text())
NOT_CODE = {'node_modules', '.git', '.claude', 'tests', 'research', 'ops', 'sql'}
# The all-zero key is the System program's id and also "no mint" in pool
# accounts (venue_api.NULL_MINT): too common a value to police.
UNPOLICED = {SOLANA['programs']['system']}


def code_files():
    for f in ROOT.rglob('*'):
        if f.suffix in ('.py', '.mjs') and not NOT_CODE & set(f.relative_to(ROOT).parts):
            yield f


def addresses():
    out = {SOLANA['native_mint']: 'native_mint', **{m: n for n, m in SOLANA['stable_mints'].items()},
           **{p: n for n, p in SOLANA['programs'].items()}}
    return {a: n for a, n in out.items() if a not in UNPOLICED}


class AddressesInData(unittest.TestCase):
    def test_no_solana_address_is_written_in_code(self):
        found = []
        for f in code_files():
            src = f.read_text()
            found += [(str(f.relative_to(ROOT)), name) for a, name in addresses().items() if a in src]
        self.assertEqual(found, [], 'move these to chains/solana/solana.json')

    def test_the_orca_fallback_pools_are_data(self):
        src = (ROOT / 'venues/orca/swap.mjs').read_text()
        venue = json.loads((ROOT / 'venues/orca/venue.json').read_text())
        for p in venue['fallback_pools']:
            self.assertNotIn(p['pool'], src, p['pair'])
            self.assertEqual({len(p['mint_a']) >= 32, len(p['mint_b']) >= 32, len(p['pool']) >= 32}, {True})

    def test_the_data_files_parse_and_name_every_program(self):
        for key in ('system', 'token', 'token_2022', 'associated_token', 'compute_budget', 'jupiter_v6',
                    'orca_whirlpool', 'raydium_clmm', 'meteora_dlmm', 'byreal_clmm', 'pancakeswap_v3'):
            self.assertIn(key, SOLANA['programs'])


if __name__ == '__main__':
    unittest.main()
