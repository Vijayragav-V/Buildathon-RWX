import difflib
from pathlib import Path
import tempfile
import unittest

from selector import Selector, apply_section, parse_patch, schedule, PIN


def patch(old, new, filename='sqlglot/example.py'):
    return f'diff --git a/{filename} b/{filename}\n' + ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile='a/'+filename, tofile='b/'+filename))


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'sqlglot').mkdir()
        self.source = 'VALUE = 2\n\ndef f(x):\n    if x:\n        return 1\n    return 0\n\ndef g():\n    return 3\n'
        (self.root/'sqlglot/example.py').write_text(self.source)
        self.tests = [{'number': 7, 'id': 'tests/a.py::test_a', 'seconds': .1}, {'number': 19, 'id': 'tests/a.py::test_b', 'seconds': .2}]
        self.mapping = {'commit': PIN, 'tests': [t['id'] for t in self.tests], 'lines': {'sqlglot/example.py': {'4': [0], '5': [0], '6': [0], '9': [1]}}, 'reads': {}, 'uncertain': []}
        self.selector = Selector(self.tests, self.mapping, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_function_scope_and_renumbered_tests(self):
        self.assertEqual(self.selector.select(patch(self.source, self.source.replace('return 1', 'return 4'))), {7})

    def test_import_time_assignment_is_conservative(self):
        self.assertEqual(self.selector.select(patch(self.source, self.source.replace('VALUE = 2', 'VALUE = 0'))), {7, 19})

    def test_python_comment_only(self):
        self.assertEqual(self.selector.select(patch(self.source, '# comment\n'+self.source)), set())

    def test_mixed_comment_and_fixture(self):
        name = 'tests/fixtures/input.sql'
        (self.root/'tests/fixtures').mkdir(parents=True)
        (self.root/name).write_text('SELECT 1;\n')
        self.mapping['reads'][name] = [1]
        change = patch(self.source, '# comment\n'+self.source) + patch('SELECT 1;\n', '# directive changed\nSELECT 2;\n', name)
        self.assertEqual(self.selector.select(change), {19})

    def test_syntax_error_and_unknown_patch(self):
        for text in [patch(self.source, 'not valid python !\n'), 'GIT binary patch\n']:
            self.assertEqual(self.selector.select(text), {7, 19})

    def test_unmatched_source_falls_back(self):
        self.assertEqual(self.selector.select(patch(self.source.replace('VALUE = 2', 'VALUE = 5'), self.source)), {7, 19})

    def test_new_tests_are_included(self):
        self.tests.append({'number': 31, 'id': 'tests/new.py::test_new', 'seconds': 1})
        selector = Selector(self.tests, self.mapping, self.root)
        self.assertEqual(selector.select(patch(self.source, self.source.replace('return 1', 'return 9'))), {7, 31})

    def test_multiple_hunks_and_insertions(self):
        after = self.source.replace('return 1', 'return 9').replace('return 3', 'return 8\n    # note')
        section = parse_patch(patch(self.source, after))[0]
        actual, _ = apply_section(self.source, section)
        self.assertEqual(actual, after)

    def test_code_moved_to_import_scope_is_conservative(self):
        after = self.source.replace('    return 0', 'raise RuntimeError("import failure")')
        self.assertEqual(self.selector.select(patch(self.source, after)), {7, 19})

    def test_default_argument_is_conservative(self):
        after = self.source.replace('def f(x):', 'def f(x=1/0):')
        self.assertEqual(self.selector.select(patch(self.source, after)), {7, 19})

    def test_class_literal_uses_recorded_class_consumers(self):
        old = 'class Dialect:\n    FLAG = True\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:Dialect': [1]}
        self.assertEqual(self.selector.select(patch(old, old.replace('True', 'False'))), {19})

    def test_unobserved_class_falls_back(self):
        old = 'class Dialect:\n    FLAG = True\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.assertEqual(self.selector.select(patch(old, old.replace('True', 'False'))), {7, 19})

    def test_class_metaclass_change_falls_back(self):
        old = 'class Dialect(metaclass=Old):\n    FLAG = True\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:Dialect': [1]}
        new = old.replace('Old', 'New').replace('True', 'False')
        self.assertEqual(self.selector.select(patch(old, new)), {7, 19})

    def test_observed_identity_equivalence(self):
        old = 'def f(x):\n    return x is None\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['identity'] = {'sqlglot/example.py:2:11': True}
        self.assertEqual(self.selector.select(patch(old, old.replace(' is ', ' == '))), set())

    def test_unsafe_identity_keeps_tests(self):
        old = 'def f(x):\n    return x is None\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['identity'] = {'sqlglot/example.py:2:11': False}
        self.mapping['lines']['sqlglot/example.py'] = {'2': [0]}
        self.assertEqual(self.selector.select(patch(old, old.replace(' is ', ' == '))), {7})

    def test_comparison_observer_excludes_custom_equality(self):
        from tools.identity_usage import comparison_equivalent
        class Custom:
            def __eq__(self, other):
                raise AssertionError('custom equality must not execute')
        self.assertFalse(comparison_equivalent(Custom(), None))
        self.assertTrue(comparison_equivalent(object(), None))
        self.assertTrue(comparison_equivalent(None, None))
        self.assertFalse(comparison_equivalent(''.join(['equal', ' value']), ''.join(['equal', ' value'])))

    def test_class_import_arithmetic_falls_back(self):
        old = 'class Dialect:\n    FLAG = 1 / 2\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:Dialect': [1]}
        self.assertEqual(self.selector.select(patch(old, old.replace('1 / 2', '1 / 0'))), {7, 19})

    def test_value_index_preserves_membership_changes(self):
        old = 'class A:\n    TABLE = {"x": 1, "y": 2}\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:A': [0, 1]}
        self.mapping['dictionary_values'] = {'TABLE': [1]}
        self.assertEqual(self.selector.select(patch(old, old.replace('1,', '3,'))), {19})
        self.assertEqual(self.selector.select(patch(old, old.replace('"x"', '"z"'))), {7, 19})

    def test_private_global_only_when_indexed(self):
        old = '_NAME: str = "old"\n'; (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['private_globals'] = {'sqlglot/example.py:_NAME': [0]}
        self.assertEqual(self.selector.select(patch(old, old.replace('old', 'new'))), {7})
        self.assertEqual(self.selector.select(patch(old, old.replace('str', 'float'))), {7, 19})

    def test_schedule_accounts_for_worker_overhead(self):
        self.assertEqual(len(schedule(self.tests)), 1)
        tests = [{'number': i, 'seconds': 10} for i in range(8)]
        workers = schedule(tests)
        self.assertEqual(len(workers), 8)
        self.assertEqual(sorted(n for w in workers for n in w), list(range(8)))
        self.assertEqual(schedule([]), [])


if __name__ == '__main__':
    unittest.main()
