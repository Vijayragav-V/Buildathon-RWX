"""Budgeted verification of small dialect test groups against the actual patch."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def overlay(source, target, edits):
    """Mirror only changed directory paths. Each changed module gets fresh bytecode."""
    groups = {}
    for path, content in edits.items():
        first, _, rest = path.partition('/')
        groups.setdefault(first, {})[rest] = content
    target.mkdir(exist_ok=True)
    for item in source.iterdir():
        if item.name.startswith('.') or item.name == '__pycache__':
            continue
        dest = target / item.name
        if item.name not in groups:
            dest.symlink_to(item, target_is_directory=item.is_dir())
        elif item.is_dir():
            overlay(item, dest, groups[item.name])
        else:
            dest.write_text(groups[item.name][''])


def verify(source, edits, ids, timeout):
    # The calling selector emits its original selections whenever replay is inconclusive.
    with tempfile.TemporaryDirectory(prefix='blast-replay-') as tmp:
        root = Path(tmp) / 'source'
        overlay(source, root, edits)
        result = Path(tmp) / 'passed.json'
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE='1',
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', SKIP_INTEGRATION='0')
        cmd = [sys.executable, str(Path(__file__).resolve()), str(root), str(result), *ids]
        child = subprocess.Popen(cmd, cwd=root, env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            status = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            return set()
        if status or not result.exists():
            return set()
        return set(json.loads(result.read_text())).intersection(ids)


def worker(root, destination, ids):
    import pytest
    sys.path.insert(0, str(root))
    import sqlglot
    if not Path(sqlglot.__file__).absolute().is_relative_to(root):
        return 2
    phases, rejected = {}, set()
    class Reports:
        def pytest_runtest_logreport(self, report):
            if not report.passed:
                rejected.add(report.nodeid)
            else:
                phases.setdefault(report.nodeid, set()).add(report.when)
    status = pytest.main(['--import-mode=importlib', '-p', 'no:cacheprovider',
                          '--no-header', '-q', *ids], plugins=[Reports()])
    if status not in (0, 1):
        return 1
    passed = [i for i in ids if phases.get(i) == {'setup', 'call', 'teardown'} and i not in rejected]
    destination.write_text(json.dumps(passed))
    return 0


class ReplayBudget:
    def __init__(self, count):
        self.remaining = min(16.0, count * 0.45)

    def refine(self, selector, chosen, patch, tests):
        if not chosen or self.remaining < .6:
            return chosen
        candidates = [tests[n] for n in chosen]
        if (len(candidates) > 64 or sum(t['seconds'] for t in candidates) > 2.0
                or any(not t['file'].startswith('tests/dialects/') for t in candidates)):
            return chosen
        from selector import parse_patch, apply_section
        started = time.monotonic()
        try:
            edits = {}
            for section in parse_patch(patch):
                name = section['old']
                if (not name or name != section['new'] or not name.startswith('sqlglot/')
                        or not name.endswith('.py') or '..' in Path(name).parts or name in edits):
                    return chosen
                edits[name] = apply_section(selector.baseline(name)[0], section)[0]
            passed = verify(selector.source, edits, sorted(t['id'] for t in candidates), min(1.8, self.remaining))
            return chosen.difference(t['number'] for t in candidates if t['id'] in passed)
        except (OSError, ValueError, KeyError):
            return chosen
        finally:
            self.remaining -= time.monotonic() - started


if __name__ == '__main__':
    sys.exit(worker(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]))
