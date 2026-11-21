"""Label unified diffs with full-suite pytest failures in the pinned SQLGlot environment.

Inside the submission image, mount this file at /app/validation/mutation_oracle.py,
then run: python /app/validation/mutation_oracle.py --tests /data/tests.json PATCH...
Only a complete run for every patch emits scorer-compatible answers on stdout.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def run_suite(catalog, destination):
    import pytest
    root = Path.cwd()
    sys.path.insert(0, str(root))
    expected = {t['id']: t['number'] for t in json.loads(catalog.read_text())['tests']}
    completed, failures = set(), set()

    class Reports:
        def pytest_runtest_logreport(self, report):
            if report.when == 'teardown':
                completed.add(report.nodeid)
            if report.failed:
                failures.add(report.nodeid)

    status = pytest.main(['tests', '-q', '--tb=line', '-p', 'no:cacheprovider',
                          '-p', 'pytest_timeout', '--timeout=180'], plugins=[Reports()])
    if status not in (0, 1) or (status == 1) != bool(failures):
        raise RuntimeError(f'Incomplete pytest run (exit {status})')
    if expected.keys() - completed or failures - expected.keys():
        raise RuntimeError('Test reports do not match the supplied catalog')
    destination.write_text(json.dumps(sorted(expected[n] for n in failures)))


def label(source, patch, catalog):
    from replay import overlay
    from selector import apply_section, parse_patch
    edits = {}
    for section in parse_patch(patch.read_text()):
        name = section['old']
        path = Path(name or '')
        if (name != section['new'] or not name or path.is_absolute() or '..' in path.parts
                or path.as_posix() != name or name in edits or not (source / path).is_file()):
            raise ValueError('Expected an edit to an existing source file')
        edits[name] = apply_section((source / name).read_text(), section)[0]

    with tempfile.TemporaryDirectory() as tmp:
        root, result = Path(tmp)/'source', Path(tmp)/'result.json'
        overlay(source, root, edits)
        for name, text in edits.items():
            if (root / name).is_symlink() or (root / name).read_text() != text:
                raise ValueError(f'Mutation was not materialized in the overlay: {name}')
        env = dict(os.environ, PYTHONPATH=str(root), SKIP_INTEGRATION='0',
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTHONDONTWRITEBYTECODE='1')
        subprocess.run([sys.executable, __file__, '--run', str(catalog), str(result)],
                       cwd=root, env=env, stdout=sys.stderr, check=True)
        return {'failing': json.loads(result.read_text())}


if __name__ == '__main__':
    if sys.argv[1:2] == ['--run']:
        run_suite(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
        parser.add_argument('--tests', type=Path, required=True)
        parser.add_argument('patches', type=Path, nargs='+')
        args = parser.parse_args()
        if len({p.stem for p in args.patches}) != len(args.patches):
            parser.error('Patch filenames must have distinct stems')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        labels = {p.stem: label(args.source.resolve(), p, args.tests.resolve()) for p in args.patches}
        print(json.dumps({'commits': labels}))
