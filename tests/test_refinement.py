import tempfile
import unittest
from pathlib import Path

from replay import overlay, worker
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
