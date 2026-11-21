from importlib.util import find_spec
import tempfile
import unittest
from pathlib import Path

from replay import overlay, plan, verify
from tools.attribute_usage import reference_map


class RefinementTests(unittest.TestCase):
    def test_overlay_isolated_and_discards_stale_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, dest = Path(tmp)/'base', Path(tmp)/'patch'
            mod = root/'sqlglot'
            mod.mkdir(parents=True)
            (mod/'__pycache__').mkdir()
            (mod/'foo.py').write_text('old')
            (mod/'bar.py').write_text('same')
            overlay(root, dest, {'sqlglot/foo.py': 'new'})
            self.assertEqual((dest/'sqlglot/foo.py').read_text(), 'new')
            self.assertEqual((mod/'foo.py').read_text(), 'old')
            self.assertFalse((dest/'sqlglot/__pycache__').exists())
            self.assertTrue((dest/'sqlglot/bar.py').is_symlink())

    def test_static_attribute_tracking_excludes_reflection_and_module_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'sqlglot').mkdir()
            (root/'sqlglot/a.py').write_text('class A:\n    DIRECT=1\n    LOOKUP=2\n    EARLY=3\n'
                'x=A.EARLY\ndef f(a):\n    return a.DIRECT, getattr(a,"LOOKUP")\n')
            self.assertEqual(reference_map(root, {'sqlglot/a.py': {'7':[4,7]}}), {'DIRECT':[4,7]})

    def test_replay_plan_includes_mixed_files_but_preserves_uncertain_tests(self):
        tests = {i: {'number': i, 'id': f'{name}::test_{i}', 'file': name, 'seconds': seconds}
                 for i, (name, seconds) in enumerate([
                     ('tests/test_a.py', .05), ('tests/dialects/test_b.py', .1),
                     ('tests/test_unknown.py', .01), ('tests/test_expensive.py', 21)])}
        self.assertEqual({t['number'] for t in plan({0, 1}, tests, set())[1]}, {0, 1})
        self.assertIsNone(plan(set(tests), tests, {2}))
        self.assertIsNone(plan({0}, tests, {0}))

    @unittest.skipUnless(find_spec('pytest'), 'verification requires pytest')
    def test_replay_multifile_fixture_subtest_and_spawn_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp).resolve()
            mod = source / 'sqlglot'
            mod.mkdir()
            (mod / '__init__.py').write_text('')
            (mod / 'value.py').write_text('x = 0')
            (mod / 'other.py').write_text('y = 0')
            suite = source / 'tests'
            suite.mkdir()
            (suite / 'conftest.py').write_text(
                'import pytest\nfrom sqlglot.value import x\n'
                '@pytest.fixture\ndef value(): return x\n')
            (suite / 'test_a.py').write_text(
                'import subprocess, sys, pytest\n'
                'def test_child(value):\n    assert value == 5\n'
                '    subprocess.run([sys.executable, "-c", '
                '"from sqlglot.other import y; assert y == 9"], check=True)\n'
                'def test_skip(): pytest.skip("skipped")\n'
                'def test_bad(): assert False\n')
            (suite / 'test_b.py').write_text(
                'import unittest\nclass B(unittest.TestCase):\n'
                '    def test_sub(self):\n        with self.subTest(x=1): self.fail("bad")\n')
            ids = ['tests/test_a.py::test_child', 'tests/test_a.py::test_skip',
                   'tests/test_a.py::test_bad', 'tests/test_b.py::B::test_sub']
            edits = {'sqlglot/value.py': 'x = 5', 'sqlglot/other.py': 'y = 9'}
            self.assertEqual(verify(source, edits, ids, 10, 10), {ids[0]})
            self.assertEqual(verify(source, {'sqlglot/value.py': 'x = 1'}, ids[:1], 10, 10), set())
            (suite / 'conftest.py').write_text(
                'import pytest\n@pytest.fixture(scope="session", autouse=True)\n'
                'def finalizer():\n    yield\n    raise ValueError()\n'
                '@pytest.fixture\ndef value(): return 5\n')
            self.assertEqual(verify(source, edits, ids[:2], 10, 10), set())
            self.assertEqual(verify(source, edits, ids[:1], .001, .001), set())
