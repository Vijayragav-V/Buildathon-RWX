import ast
from pathlib import Path
import tempfile
import unittest

from entry_index import build, changed_keys
from patch_semantics import SourceComparison, same_compiled_module
from replay import measured_plan


class SemanticTests(unittest.TestCase):
    def test_detector_retains_changed_decorators_and_statement_shape(self):
        src = '@old\ndef f():\n    return 1\n\nclass C:\n    x = 4\n'
        for dest in (src.replace('@old', '@new'), src.replace('return 1', 'raise E'),
                     src.replace('x = 4', 'x = 5'), src.replace('x = 4', 'pass\n    x = 4')):
            a, b = SourceComparison(src, dest).differences(ast.parse(src), ast.parse(dest))
            self.assertNotEqual(ast.dump(a), ast.dump(b))
        dest = src.replace('return 1', 'return 1 # same')
        a, b = SourceComparison(src, dest).differences(ast.parse(src), ast.parse(dest))
        self.assertEqual(ast.dump(a), ast.dump(b))

    def test_compiled_equivalence_preserves_types_and_line_locations(self):
        src = 'class C:\n    A = 0xFF\n'
        self.assertTrue(same_compiled_module(src, src.replace('0xFF', '+255'), 'a.py'))
        self.assertFalse(same_compiled_module(src, src.replace('0xFF', '+254'), 'a.py'))
        self.assertFalse(same_compiled_module(src, '\n'+src, 'a.py'))
        self.assertFalse(same_compiled_module('X = True\n', 'X = 1   \n', 'a.py'))

    def test_dictionary_key_reader_is_positive_evidence_only(self):
        src = 'class C:\n    OPTIONS = {"a": 1, "b": 2}\ndef f(c, k):\n    return c.OPTIONS.get("a"), c.OPTIONS[k]\n'
        old = ast.parse(src)
        new = ast.parse(src.replace('"b": 2', '"b": 5'))
        self.assertEqual(changed_keys(old, new, 'C', 'OPTIONS'), ('b',))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'a.py').write_text(src)
            index = build(root, {'dictionary_values': {'OPTIONS': [0, 1]},
                                 'lines': {'a.py': {'4': [1]}}})
            self.assertEqual(index['OPTIONS'], {'a': [1]})

    def test_planning_is_advisory_and_keeps_uncertain_tests_out_of_replay(self):
        tests = {i: {'number': i, 'file': 'tests/a.py', 'id': f'tests/a.py::test_{i}',
                     'seconds': .5} for i in range(5)}
        chosen = set(tests)
        q = measured_plan(chosen, tests, {0}, {'tests/a.py': .12},
                          {t['id']: .01 for t in tests.values()}, None, frozenset({1}))
        self.assertIsNotNone(q)
        self.assertNotIn(0, [t['number'] for t in q[1]])
        self.assertEqual(chosen, set(tests))
