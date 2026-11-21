import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.version_info[:2] == (3, 13) and importlib.util.find_spec('pytest_timeout'),
                     'requires the pinned CPython monitoring API and pytest')
class GeneratorIndexTests(unittest.TestCase):
    def test_native_readers_source_inspection_and_unowned_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'sqlglot'
            package.mkdir()
            (package / '__init__.py').touch()
            (package / 'example.py').write_text(
                'def values():\n    yield 1\n\ndef later(): pass\n')
            (package / 'early.py').write_text(
                'def eager():\n    yield 1\n\nINITIAL = list(eager())\n')
            tests = root / 'tests'
            tests.mkdir()
            (tests / 'test_usage.py').write_text(
                'from pathlib import Path\n'
                'import subprocess, sys\n'
                'from sqlglot import example, early\n'
                'def test_run(): assert list(example.values()) == [1]\n'
                'def test_inspect(): assert example.later.__code__.co_firstlineno > 2\n'
                'def test_source(): assert "yield" in Path(example.__file__).read_text()\n'
                'def test_child(): subprocess.run([sys.executable, "-c", "pass"], check=True)\n'
                'def test_idle(): pass\n')
            names = ['run', 'inspect', 'source', 'child', 'idle']
            catalog = [f'tests/test_usage.py::test_{name}' for name in names]
            target = root / 'map.json'
            target.write_text(json.dumps({'tests': catalog}))
            script = Path(__file__).resolve().parents[1] / 'generator_index.py'
            result = subprocess.run([sys.executable, str(script), str(root), str(target)],
                                    env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode, 0, result.stdout)
            users = json.loads(target.read_text())['generator_users']
            self.assertEqual(users.get('sqlglot/example.py:values:1'), [0, 1, 2, 3], result.stdout)
            self.assertNotIn('sqlglot/early.py:eager:1', users)
