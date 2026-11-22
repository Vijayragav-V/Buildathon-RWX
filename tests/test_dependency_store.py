import json
from pathlib import Path
import tempfile
import unittest

from dependency_store import compile_map, load
from selector import apply_section, parse_patch


class DependencyStoreTests(unittest.TestCase):
    def test_missing_empty_and_renumbered_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, db = Path(tmp)/'map.json', Path(tmp)/'map.sqlite'
            src.write_text(json.dumps({'version': 2, 'tests': ['b', 'a'],
                                       'lines': {'p.py': {'1': [1]}, 'empty': []}}))
            compile_map(src, db)
            out = load(db)
            self.addCleanup(out.close)
            self.assertEqual(out['tests'], ['b', 'a'])
            self.assertEqual(out['lines'].get('p.py'), {'1': [1]})
            self.assertIn('empty', out['lines'])
            self.assertNotIn('absent', out['lines'])
            self.assertIsNone(out['lines'].get('absent'))
            self.assertEqual(out['lines'].get('absent', [3]), [3])

    def test_inconsistent_new_hunk_coordinates_are_rejected(self):
        text = 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +17 @@\n-a\n+b\n'
        with self.assertRaises(ValueError):
            apply_section('a\n', parse_patch(text)[0])
