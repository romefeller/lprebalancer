"""A mock must replace the name where the code looks it up. Every
patch.object(<module>, 'name') on dexes or a venue module must name something
that module defines itself: a name it only imports would be replaced in the
wrong place, and the real function would run under a test that thinks it is
mocked."""
import ast
import importlib
import pathlib
import re
import unittest

import _fixtures

TESTS = pathlib.Path(__file__).resolve().parent
# the alias tests and modules use -> the module it names
ALIASES = {'dexes': 'dexes', 'venue_api': 'venues.api', 'solana_state': 'venues.solana_state',
           'jupiter_api': 'venues.jupiter.prices', 'jupiter': 'venues.jupiter.prices', 'evm': 'venues.evm',
           'orca_pools': 'venues.orca.pools', 'raydium_pools': 'venues.raydium_clmm.pools',
           'byreal_pools': 'venues.byreal.pools', 'pancake_pools': 'venues.pancakeswap_v3.pools',
           'meteora_pools': 'venues.meteora_dlmm.pools', 'aerodrome_pools': 'venues.aerodrome.pools',
           'uniswap_pools': 'venues.uniswap_v3.pools',
           **{m: f'lp.{m}' for m in ('paths', 'tuning', 'books', 'signers', 'capital', 'tape', 'regime', 'pauses',
                                     'harvest', 'swaps', 'board', 'moves', 'polls', 'housekeeping', 'loop')}}
PATCH = re.compile(r"(?:patch\.object|self\.patch)\(([\w.]+), '(\w+)'")


def own_names(module):
    """Names a module binds itself at top level: defs, classes, assignments."""
    tree = ast.parse(pathlib.Path(importlib.import_module(module).__file__).read_text())
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                out |= {e.id for e in ast.walk(t) if isinstance(e, ast.Name)}
        elif isinstance(node, ast.Import):          # a module the code calls through (subprocess, urllib)
            out |= {(a.asname or a.name).split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):      # a name the module's own code looks up (datetime)
            out |= {a.asname or a.name for a in node.names}
    return out


class PatchTargets(unittest.TestCase):
    def test_every_venue_mock_names_its_home(self):
        wrong = []
        for f in sorted(TESTS.glob('test_*.py')):
            for target, name in PATCH.findall(f.read_text()):
                module = ALIASES.get(target.split('.')[-1])
                if module and name not in own_names(module):
                    wrong.append(f'{f.name}: {target}, {name!r} is not defined in {module}')
        self.assertEqual(wrong, [])


class AuditBotView(unittest.TestCase):
    def test_the_view_has_everything_the_audits_read(self):
        import lp.housekeeping
        src = (_fixtures.ROOT / 'audit.py').read_text()
        wanted = set(re.findall(r'\bbot\.([A-Za-z_]+)', src))
        view = lp.housekeeping.bot_view()
        self.assertEqual(sorted(n for n in wanted if not hasattr(view, n)), [])


if __name__ == '__main__':
    unittest.main()
