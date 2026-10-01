"""No dead code in the bot's own Python modules (2026-09-30).

The owner said: delete every old code, and make a defence so it cannot come
back. This test is that defence. It reads the source only; it imports nothing
and touches no database or network.

1. Every top-level function, class and UPPER_CASE constant in the modules in
   OWNED has a reference outside its own definition, in a file that is NOT a
   test. A name that only a test calls is dead in production.
2. Every `case '<event>':` in telegram_bridge.mjs has an emitter: the event
   name appears as a string literal in some production file.

What counts as a reference (lenient on purpose, so the test never fails on a
live name):
  - Python files: any Name, any Attribute name (so `_db().health_get` counts),
    and any identifier inside a string literal (mock.patch strings, dispatch
    tables, SQL text, docstrings of other functions).
  - Every other text file (*.mjs, *.sql, *.json, *.md, ops/*, *.sh): any word
    token. A signer, the runbook or a migration that names it keeps it.

If this test fails: delete the name, or, when something outside this tree
reaches it (a shell command, cron, a human), add it to ALLOW with the reason.
"""
import ast
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
OWNED = ('rebalancer.py', 'db.py', 'engine.py', 'dexes.py', 'scanner.py', 'calm.py',
         'fees.py', 'guards.py', 'txfees.py', 'config.py', 'wallets.py', 'chains.py', 'audit.py')
TEXT_SUFFIXES = {'.mjs', '.js', '.sql', '.json', '.md', '.sh', '.service'}
SKIP_DIRS = {'node_modules', '__pycache__', 'research', '.git'}

# Names that nothing in this tree references, but that are reached from
# outside it. Each entry needs a reason. Keep this list short.
ALLOW = {
}

TOKEN = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _files():
    for p in ROOT.rglob('*'):
        if p.is_file() and not (SKIP_DIRS & set(p.relative_to(ROOT).parts)):
            yield p


def _is_test(p):
    return p.relative_to(ROOT).parts[0] == 'tests'


def _py_refs(tree, skip=None):
    """Identifiers a Python tree references. `skip` is (start, end) line range."""
    out = set()
    for n in ast.walk(tree):
        line = getattr(n, 'lineno', None)
        if skip and line is not None and skip[0] <= line <= skip[1]:
            continue
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.update(TOKEN.findall(n.value))
        elif isinstance(n, ast.alias):
            out.update(n.name.split('.'))
            if n.asname:
                out.add(n.asname)
    return out


def _defs(tree):
    """(name, first line, last line) for every top-level def, class and
    UPPER_CASE constant."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno, node.end_lineno
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    yield t.id, node.lineno, node.end_lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id.isupper():
            yield node.target.id, node.lineno, node.end_lineno


class Index:
    """Every production reference, once. Per owned module, the references
    that module makes to itself are kept per definition, so a definition's
    own body (recursion, its own docstring) does not keep it alive."""

    def __init__(self):
        self.global_refs = set()          # production files other than the owned ones
        self.trees = {}
        for p in _files():
            if _is_test(p):
                continue
            rel = str(p.relative_to(ROOT))
            if p.suffix == '.py':
                tree = ast.parse(p.read_text(), filename=rel)
                if rel in OWNED:
                    self.trees[rel] = tree
                else:
                    self.global_refs |= _py_refs(tree)
            elif p.suffix in TEXT_SUFFIXES or p.parent.name == 'ops':
                if p.stat().st_size < 5_000_000:
                    self.global_refs |= set(TOKEN.findall(p.read_text(errors='ignore')))

    def unreferenced(self):
        other = {rel: _py_refs(t) for rel, t in self.trees.items()}
        dead = []
        for rel, tree in self.trees.items():
            refs_elsewhere = self.global_refs.union(*(r for k, r in other.items() if k != rel))
            for name, a, b in _defs(tree):
                if name.startswith('__') or (rel, name) in ALLOW:
                    continue
                if name in refs_elsewhere or name in _py_refs(tree, skip=(a, b)):
                    continue
                dead.append(f'{rel}:{a} {name}')
        return dead


class NoDeadCode(unittest.TestCase):
    def test_every_owned_definition_is_referenced_outside_tests(self):
        dead = Index().unreferenced()
        self.assertEqual(dead, [], 'dead code (no production reference): delete it, '
                                   'or add it to ALLOW with a reason')

    def test_allow_list_entries_are_still_defined(self):
        """A stale allow-list entry hides nothing today but would hide a
        new dead name with the same spelling tomorrow."""
        for rel, name in ALLOW:
            names = {n for n, *_ in _defs(ast.parse((ROOT / rel).read_text()))}
            self.assertIn(name, names, f'{rel}:{name} in ALLOW is not defined any more')

    def test_detector_finds_a_planted_dead_function(self):
        """Mutation check on the detector itself: a function nothing calls
        must be reported; one that only calls itself must be reported too."""
        src = ('def live():\n    return 1\n\n'
               'def dead():\n    return dead()\n\n'
               'X = live()\nprint(X)\n')
        tree = ast.parse(src)
        found = [n for n, a, b in _defs(tree) if n not in _py_refs(tree, skip=(a, b))]
        self.assertEqual(found, ['dead'])

    def test_every_telegram_case_has_an_emitter(self):
        bridge = (ROOT / 'telegram_bridge.mjs').read_text()
        cases = set(re.findall(r"case '([A-Za-z_]+)'", bridge))
        self.assertTrue(cases, 'no event cases found in telegram_bridge.mjs')
        literals = set()
        for p in _files():
            if _is_test(p) or p.name == 'telegram_bridge.mjs':
                continue
            if p.suffix == '.py':
                for n in ast.walk(ast.parse(p.read_text())):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        literals.add(n.value)
            elif p.suffix in ('.mjs', '.js'):
                literals |= set(re.findall(r"""['"`]([A-Za-z_]+)['"`]""", p.read_text(errors='ignore')))
        self.assertEqual(sorted(cases - literals), [],
                         'telegram_bridge.mjs renders events that nothing emits')


if __name__ == '__main__':
    unittest.main()
