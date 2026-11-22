"""Bounded, per-test verification against an isolated, patched checkout."""
import json
import importlib
from functools import lru_cache
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time


def overlay(source, target, edits):
    """Mirror unchanged files, including only bytecode for unchanged modules."""
    groups = {}
    for path, content in edits.items():
        first, _, rest = path.partition('/')
        groups.setdefault(first, {})[rest] = content
    target.mkdir(exist_ok=True)
    for item in source.iterdir():
        dest = target / item.name
        if item.name.startswith('.'):
            continue
        if item.name == '__pycache__':
            for cached in item.glob('*.pyc'):
                if cached.name.partition('.')[0] + '.py' not in groups:
                    dest.mkdir(exist_ok=True)
                    (dest / cached.name).symlink_to(cached)
        elif item.name not in groups:
            dest.symlink_to(item, target_is_directory=item.is_dir())
        elif item.is_dir():
            overlay(item, dest, groups[item.name])
        else:
            dest.write_text(groups[item.name][''])


def cpu_seconds():
    # Includes running grandchildren (notably multiprocessing test fixtures).
    try:
        with open('/sys/fs/cgroup/cpu.stat') as f:
            return int(dict(line.split() for line in f)['usage_usec']) / 1e6
    except (OSError, KeyError):
        return sum(sum(resource.getrusage(who)[:2])
                   for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN))


@lru_cache(maxsize=1)
def warm_test_framework():
    """Share only the pinned framework's code pages, before collection or configuration."""
    from _pytest.config import default_plugins
    for name in default_plugins:
        importlib.import_module('_pytest.' + name)


def verify(source, edits, ids, timeout, cpu_limit, stop_early=False):
    deadline = time.monotonic() + timeout
    cpu_deadline = cpu_seconds() + cpu_limit
    with tempfile.TemporaryDirectory(prefix='blast-replay-') as tmp:
        root = Path(tmp) / 'source'
        overlay(source, root, edits)
        result = Path(tmp) / 'passed.json'
        env = dict(os.environ, HOME=tmp, TMPDIR=tmp, PYTHONPATH=str(root),
                   PYTHONDONTWRITEBYTECODE='1', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                   SKIP_INTEGRATION='0')
        # Fork only the single-threaded selector on Linux. No patched module,
        # test module, or fixture has been imported in this process.
        if (sys.platform == 'linux' and 'sqlglot' not in sys.modules and 'tests' not in sys.modules
                and len(os.listdir('/proc/self/task')) == 1):
            warm_test_framework()
            pid = os.fork()
            if pid == 0:
                try:
                    os.setsid()
                    with open(os.devnull, 'wb') as sink:
                        os.dup2(sink.fileno(), 1)
                        os.dup2(sink.fileno(), 2)
                    os.chdir(root)
                    os.environ.update(env)
                    sys.dont_write_bytecode = True
                    tempfile.tempdir = None
                    os._exit(worker(root, result, ids, stop_early))
                except BaseException:
                    os._exit(2)
            child = None
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), str(root), str(result), str(int(stop_early)), *ids]
            child = subprocess.Popen(cmd, cwd=root, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            pid = child.pid
        done = False
        try:
            while time.monotonic() < deadline and cpu_seconds() < cpu_deadline:
                if child is None:
                    finished, status = os.waitpid(pid, os.WNOHANG)
                    done = bool(finished)
                else:
                    status = child.poll()
                    done = status is not None
                if done:
                    if status == 0 and result.is_file():
                        return set(json.loads(result.read_text())).intersection(ids)
                    break
                time.sleep(.025)
        finally:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                if not done:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            if not done:
                if child is None:
                    os.waitpid(pid, 0)
                else:
                    child.wait()
        return set()


def worker(root, destination, ids, stop_early=False):
    import pytest
    sys.path.insert(0, str(root))
    requested = set(ids)
    phases, rejected = {}, set()
    class Reports:
        def pytest_collection_modifyitems(self, config, items):
            # Keep pytest's native order within each module.
            other = [item for item in items if item.nodeid not in requested]
            items[:] = [item for item in items if item.nodeid in requested]
            config.hook.pytest_deselected(items=other)

        def pytest_runtest_logreport(self, report):
            if not report.passed or hasattr(report, 'wasxfail'):
                rejected.add(report.nodeid)
                # A teardown can belong to a session-, module-, or class-scoped
                # fixture. Pytest reports it against just the last test in scope.
                if report.when == 'teardown' and not report.passed:
                    rejected.update(ids)
            else:
                phases.setdefault(report.nodeid, set()).add(report.when)
    files = sorted({nodeid.split('::', 1)[0] for nodeid in ids})
    status = pytest.main(['--import-mode=importlib', '-p', 'no:cacheprovider',
                          '--no-header', '-q', '-p', 'pytest_timeout', '--timeout=180', *(['-x'] if stop_early else []), *files], plugins=[Reports()])
    modules = [module for name, module in sys.modules.copy().items()
               if name == 'sqlglot' or name.startswith('sqlglot.')]
    if not modules or any(not Path(getattr(m, '__file__', '') or '').absolute().is_relative_to(root)
                          for m in modules):
        return 2
    if status not in (0, 1) or (status and not rejected):
        return 1
    passed = [i for i in ids if phases.get(i) == {'setup', 'call', 'teardown'} and i not in rejected]
    destination.write_text(json.dumps(passed))
    return 0


def plan(chosen, tests, uncertain, collection=None):
    from selector import best_schedule
    if not chosen or len(chosen) > 128:
        return None
    collection = collection or {}
    candidates = sorted((tests[n] for n in chosen - uncertain
                         if tests[n]['file'].startswith('tests/')
                         and tests[n]['id'].startswith(tests[n]['file'] + '::')),
                        key=lambda t: (t['seconds'], t['number']))
    by_cost = []
    total = 0
    for t in candidates:
        if len(by_cost) >= 64 or t['seconds'] > 2.5 or total + t['seconds'] > 5:
            break
        by_cost.append(t)
        total += t['seconds']
    if not by_cost:
        return None
    original = best_schedule([tests[n] for n in chosen])[0]
    best = None
    for threshold in (.15, .65, 2.5):
        group = [t for t in by_cost if t['seconds'] <= threshold]
        if not group:
            continue
        retained = chosen.difference(t['number'] for t in group)
        saving = (original - best_schedule([tests[n] for n in retained])[0]) / 1000
        files = {t['file'] for t in group}
        startup = max(collection.get(f, .55) for f in files) + .08 * (len(files) - 1)
        estimate = startup + sum(t['seconds'] for t in group)
        # A group with any failing test must still pay for at least one worker.
        # Discount the part of the potential gain that requires a clean sweep.
        expected = saving - (.65 if not retained else 0.)
        if expected > estimate * 1.25:
            priority = expected / max(estimate, .1)
            if best is None or priority > best[0]:
                best = priority, group, saving, (not retained and saving < 3.5), estimate
        if len(group) == len(by_cost):
            break
    return best


def measured_plan(chosen, tests, uncertain, collection, costs, previous, hints=frozenset()):
    """Compare the marginal makespan benefit of actual, unmodified pytest invocations."""
    from selector import best_schedule
    if not chosen:
        return previous
    # Full selection is common. Only material candidates can bring down a large
    # schedule; checking every possible removal here would itself cost CPU.
    ordered = sorted((tests[n] for n in chosen - uncertain),
                     key=lambda t: (-t['seconds'], t['number']))
    if not ordered:
        return previous
    groups = [[t] for t in ordered[:3]]
    if len(ordered) <= 192:
        groups.append(ordered)
        outside = [t for t in ordered if t['number'] not in hints]
        if hints and 0 < len(outside) < len(ordered):
            groups.append(outside)
    initial = best_schedule([tests[n] for n in chosen])[0]
    best = previous
    for group in groups:
        files = {t['file'] for t in group}
        if any(not t['file'].startswith('tests/') or not t['id'].startswith(t['file']+'::')
               for t in group):
            continue
        times = [costs.get(t['id']) for t in group]
        if any(v is None for v in times):
            continue
        # Pytest imports common helpers once in a mixed-file session.
        setup = max(collection.get(f, .55) for f in files) + .035 * (len(files) - 1)
        estimate = max(.25, setup) + sum(times)
        if estimate > 3.3:
            continue
        remaining = chosen.difference(t['number'] for t in group)
        saving = (initial - best_schedule([tests[n] for n in remaining])[0]) / 1000
        gain = saving - (0 if remaining else .65)
        if gain <= 1.5 * estimate:
            continue
        proposal = gain / estimate, group, saving, not remaining and saving < 3.5, estimate
        if best is None or proposal[0] > best[0]:
            best = proposal
    return best


class ReplayBudget:
    def __init__(self, count):
        self.wall_left = min(20., max(3., count * .65))
        self.cpu_left = min(16., max(5., count * .55))
        self.planned_left = self.cpu_left
        self.proposals = {}

    def proposal(self, selector, chosen, tests, hints=frozenset()):
        key = frozenset(chosen), hints
        if key not in self.proposals:
            collection = selector.mapping.get('collection_cpu', {})
            proposal = plan(chosen, tests, selector.uncertain, collection)
            costs = selector.mapping.get('test_cpu')
            if costs is not None:
                proposal = measured_plan(chosen, tests, selector.uncertain, collection, costs, proposal, hints)
            self.proposals[key] = proposal
        return self.proposals[key]

    def refine(self, selector, chosen, patch, tests, proposal):
        if min(self.wall_left, self.cpu_left) < .6:
            return chosen
        _, group, saving, stop_early, estimate = proposal
        if estimate > self.planned_left:
            return chosen
        self.planned_left -= estimate
        from selector import parse_patch, apply_section
        started, cpu = time.monotonic(), cpu_seconds()
        try:
            edits = {}
            for section in parse_patch(patch):
                name = section['old']
                source_file = bool(name and name.startswith('sqlglot/') and name.endswith('.py'))
                fixture = bool(name and name.startswith('tests/fixtures/')
                               and selector.mapping.get('reads', {}).get(name) is not None)
                if (not (source_file or fixture) or name != section['new']
                        or Path(name).as_posix() != name or '..' in Path(name).parts or name in edits):
                    return chosen
                edits[name] = apply_section(selector.baseline(name)[0], section)[0]
            # CPU is itself charged by the scorer. Even an all-pass probe
            # cannot pay back more than this change in scheduled makespan.
            cap = min(4., self.cpu_left, saving * .88)
            # On cgroup v1/macOS, rusage charges live descendants only at exit.
            # Use the competition's two-CPU limit as a conservative wall bound.
            wall_cap = cap + .35 if Path('/sys/fs/cgroup/cpu.stat').is_file() else cap / 2
            passed = verify(selector.source, edits, sorted(t['id'] for t in group),
                            min(self.wall_left, 4.5, wall_cap), cap, stop_early)
            return chosen.difference(t['number'] for t in group if t['id'] in passed)
        except (OSError, ValueError, KeyError, ImportError):
            return chosen
        finally:
            self.wall_left -= time.monotonic() - started
            self.cpu_left -= cpu_seconds() - cpu


if __name__ == '__main__':
    sys.exit(worker(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[4:], bool(int(sys.argv[3]))))
