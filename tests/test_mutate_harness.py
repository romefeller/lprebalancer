"""tests/mutate.py: mutant identity and the EQUIVALENT table.

A mutant's key is (target, function, description, stripped line, occurrence).
It must not depend on the line number, and two mutants on one line must never
share a key. These tests run no mutant; they only generate them.
"""
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

import mutate as M

PY_SRC = '''\
import os


def f(a, b, c):
    x = 1
    return a and b and (c or 0)


def g(y):
    if y < 3:
        return y
    return 0
'''

JS_SRC = '''\
export function f(a, b) {
  if (a < b) return a;
  return b;
}
'''

SQL_SRC = '''\
def q(cur):
    cur.execute('select x from t order by x asc')
    return cur.fetchall()
'''


def py(src, fns=('f', 'g'), target='t'):
    return M.identify(target, src, M.py_mutants(src, list(fns)))


def insert_above(src, where, lines):
    i = src.index(where)
    return src[:i] + lines + src[i:]


class Identity(unittest.TestCase):
    def assertSameKeys(self, before, after):
        self.assertEqual([m.key for m in before], [m.key for m in after])

    def test_lines_above_the_function_keep_every_key(self):
        before = py(PY_SRC)
        after = py(insert_above(PY_SRC, 'def f', '# a comment\nZ = 5\n\n\n'))
        self.assertTrue(before)
        self.assertSameKeys(before, after)
        self.assertEqual([m.line + 4 for m in before], [m.line for m in after])

    def test_lines_above_inside_the_function_keep_every_key(self):
        before = [m for m in py(PY_SRC) if m.function == 'f' and 'return' in m.text]
        after = [m for m in py(insert_above(PY_SRC, '    x = 1', '    w = [7]\n    # note\n'))
                 if m.function == 'f' and 'return' in m.text]
        self.assertTrue(before)
        self.assertSameKeys(before, after)

    def test_the_line_number_is_not_in_the_key(self):
        for m in py(PY_SRC):
            self.assertNotIn('@L', m.desc)
            self.assertNotIn(m.line, m.key[2:4])

    def test_js_and_sql_keys_survive_lines_above(self):
        js = lambda s: M.identify('j', s, M.js_mutants(s, ['f']))
        self.assertSameKeys(js(JS_SRC), js('// header\nconst k = 1;\n\n' + JS_SRC))
        sql = lambda s: M.identify('s', s, M.sql_mutants(s, ['q']))
        self.assertTrue(sql(SQL_SRC))
        self.assertSameKeys(sql(SQL_SRC), sql('import x\n\n\n' + SQL_SRC))

    def test_same_description_on_one_line_gets_distinct_keys(self):
        drops = [m for m in py(PY_SRC) if m.function == 'f' and m.desc == 'drop operand 1']
        self.assertEqual(len(drops), 2)                    # `a and b and (...)` and `c or 0`
        self.assertEqual(drops[0].line, drops[1].line)
        self.assertEqual(drops[0].text, drops[1].text)
        self.assertEqual(drops[0].legacy, drops[1].legacy)  # the old key could not tell them apart
        self.assertNotEqual(drops[0].key, drops[1].key)
        self.assertEqual([m.occ for m in drops], [0, 1])
        self.assertNotEqual(drops[0].code, drops[1].code)

    def test_every_key_in_a_file_is_unique(self):
        ms = py(PY_SRC)
        self.assertEqual(len(ms), len({m.key for m in ms}))

    def test_every_real_target_has_unique_keys(self):
        for name in M.TARGETS:
            ms = M.target_mutants(name)
            self.assertEqual(len(ms), len({m.key for m in ms}), name)

    def test_the_label_shows_line_and_occurrence(self):
        a, b = [m for m in py(PY_SRC) if m.function == 'f' and m.desc == 'drop operand 1']
        self.assertEqual(a.label(), f't f drop operand 1 @L{a.line}')
        self.assertEqual(b.label(), f't f drop operand 1 @L{b.line} #1')


class Equivalence(unittest.TestCase):
    def setUp(self):
        self.ms = py(PY_SRC)
        self.a, self.b = [m for m in self.ms if m.function == 'f' and m.desc == 'drop operand 1']

    def test_an_entry_matches_only_its_own_mutant(self):
        reasons, warnings = M.match_equivalent(self.ms, {self.a.key: 'why'})
        self.assertEqual(reasons, {self.a.key: 'why'})
        self.assertNotIn(self.b.key, reasons)
        self.assertEqual(warnings, [])

    def test_an_entry_survives_lines_inserted_above(self):
        after = py(insert_above(PY_SRC, 'def f', '\n\n# moved\n'))
        reasons, warnings = M.match_equivalent(after, {self.b.key: 'why'})
        self.assertEqual(list(reasons), [self.b.key])
        self.assertEqual(warnings, [])

    def test_an_entry_for_an_edited_line_is_stale_not_silent(self):
        edited = PY_SRC.replace('(c or 0)', '(c or 5)')
        reasons, warnings = M.match_equivalent(py(edited), {self.a.key: 'why'})
        self.assertEqual(reasons, {})
        self.assertTrue(any('stale' in w for w in warnings), warnings)

    def test_an_old_style_key_still_matches_with_a_warning(self):
        m = next(m for m in self.ms if m.function == 'g' and m.desc == 'swap Lt->LtE')
        reasons, warnings = M.match_equivalent(self.ms, {m.old_key: 'why'})
        self.assertEqual(reasons, {m.key: 'why'})
        self.assertTrue(any('old-style' in w and repr(m.key) in w for w in warnings), warnings)

    def test_an_old_style_key_shared_by_two_mutants_is_flagged(self):
        reasons, warnings = M.match_equivalent(self.ms, {self.a.old_key: 'why'})
        self.assertEqual(set(reasons), {self.a.key, self.b.key})
        self.assertTrue(any('matches 2 mutants' in w for w in warnings), warnings)

    def test_entries_of_targets_not_run_do_not_warn(self):
        _, warnings = M.match_equivalent(self.ms, {('other', 'f', 'x', 'y', 0): 'why'}, ['t'])
        self.assertEqual(warnings, [])

    def test_a_malformed_key_is_refused(self):
        with self.assertRaises(SystemExit):
            M.match_equivalent(self.ms, {('t', 'f'): 'why'})

    def test_the_real_table_is_well_formed(self):
        for k, reason in M.EQUIVALENT.items():
            self.assertIn(len(k), (3, 5), k)
            self.assertTrue(reason and isinstance(reason, str), k)
            self.assertIn(k[0], M.TARGETS, k)
            if len(k) == 5:
                self.assertTrue(all(isinstance(x, str) for x in k[:4]) and isinstance(k[4], int), k)
                self.assertNotRegex(k[2], r' @L?\d+$', k)
        for k in M.EQUIVALENT_OLD:
            self.assertEqual(len(k), 3, k)


class Migration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.dir.name)
        (self.root / 'x.py').write_text(PY_SRC)
        self.targets = mock.patch.dict(M.TARGETS, {'t': ('x.py', ['f', 'g'], None)}, clear=True)
        self.targets.start()
        self.ms = M.target_mutants('t', self.root)

    def tearDown(self):
        self.targets.stop()
        self.dir.cleanup()

    def test_a_unique_old_entry_resolves_to_its_mutant(self):
        m = next(m for m in self.ms if m.function == 'g' and m.desc == 'swap Lt->LtE')
        resolved, unresolved = M.migrate({m.old_key: 'why'}, self.root)
        self.assertEqual(resolved, {m.key: 'why'})
        self.assertEqual(unresolved, [])

    def test_a_shared_old_entry_is_listed_not_guessed(self):
        a = next(m for m in self.ms if m.function == 'f' and m.desc == 'drop operand 1')
        resolved, unresolved = M.migrate({a.old_key: 'why'}, self.root)
        self.assertEqual(resolved, {})
        self.assertEqual([e for e, _ in unresolved], [a.old_key])
        self.assertIn('2 mutants', unresolved[0][1])

    def test_a_stale_line_is_listed_not_guessed(self):
        m = next(m for m in self.ms if m.function == 'g' and m.desc == 'swap Lt->LtE')
        (self.root / 'x.py').write_text('\n\n' + PY_SRC)     # the mutant moved 2 lines down
        resolved, unresolved = M.migrate({m.old_key: 'why'}, self.root)
        self.assertEqual(resolved, {})
        self.assertEqual([e for e, _ in unresolved], [m.old_key])
        self.assertIn(f'lines [{m.line + 2}]', unresolved[0][1])

    def test_new_style_entries_are_left_alone(self):
        m = self.ms[0]
        self.assertEqual(M.migrate({m.key: 'why'}, self.root), ({}, []))

    def test_an_unknown_target_is_listed(self):
        _, unresolved = M.migrate({('nope', 'f', 'x @L1'): 'why'}, self.root)
        self.assertEqual(unresolved[0][1], 'unknown target')


GRANDCHILD = """
import subprocess, sys, time
g = subprocess.Popen([sys.executable, '-c', {code!r}], {redirect})
open('pid', 'w').write(str(g.pid))
{tail}
"""


def alive(pid):
    """Whether `pid` runs (a zombie waiting for its reaper does not)."""
    try:
        state = pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != 'Z'


class Timeouts(unittest.TestCase):
    """A mutant's test run that times out leaves no process behind."""

    def run_cmd(self, code, redirect='', tail='time.sleep(300)', timeout=1):
        root = pathlib.Path(tempfile.mkdtemp(prefix='mut_harness_'))
        self.addCleanup(shutil.rmtree, root, True)
        (root / 'tests').mkdir()
        script = GRANDCHILD.format(code=code, redirect=redirect, tail=tail)
        with mock.patch.object(M, 'KILL_GRACE_S', 1):
            rc = M.run_tests(root, 'unused_test', [M.PY, '-c', script], timeout=timeout)
        pid = int((root / 'tests' / 'pid').read_text())
        deadline = M.time.monotonic() + 5
        while alive(pid) and M.time.monotonic() < deadline:
            M.time.sleep(0.05)
        return rc, pid

    def test_a_sleeping_grandchild_dies_with_the_timeout(self):
        rc, pid = self.run_cmd('import time; time.sleep(300)')
        self.assertEqual(rc, 'timeout')                     # counted as KILLED, as before
        self.assertFalse(alive(pid))

    def test_a_grandchild_that_ignores_sigterm_is_killed(self):
        code = 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'
        rc, pid = self.run_cmd(code)
        self.assertEqual(rc, 'timeout'); self.assertFalse(alive(pid))

    def test_a_straggler_after_a_normal_exit_is_killed(self):
        rc, pid = self.run_cmd('import time; time.sleep(300)', redirect='stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL',
                               tail='sys.exit(3)', timeout=30)
        self.assertEqual(rc, 3); self.assertFalse(alive(pid))

    def test_a_timeout_counts_as_killed(self):
        self.assertFalse(M.survived('timeout'))
        self.assertFalse(M.survived(1)); self.assertTrue(M.survived(0))


if __name__ == '__main__':
    unittest.main()
