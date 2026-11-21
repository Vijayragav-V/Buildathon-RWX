"""Collect dependencies only from the unmodified, pinned SQLGlot checkout."""
import json
import os
from pathlib import Path
import sys
import platform

import concurrent.futures
from multiprocessing.reduction import ForkingPickler

os.environ["COVERAGE_CORE"] = "ctrace"

import coverage
import pytest

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
os.environ['SKIP_INTEGRATION'] = '0'
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'

class TracedPool:
    """Run submitted baseline work in-process so coverage retains the test context.

    Futures preserve exception/result behavior. Only the baseline collector uses
    this adapter; neither the original checkout nor the runtime is modified.
    """
    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*ForkingPickler.loads(ForkingPickler.dumps(initargs)))

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            fn, args, kwargs = ForkingPickler.loads(ForkingPickler.dumps((fn, args, kwargs)))
            result = fn(*args, **kwargs)
            future.set_result(ForkingPickler.loads(ForkingPickler.dumps(result)))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.shutdown()


class Collector:
    def __init__(self):
        self.context = ''
        self.reads = {}
        self.classes = {}
        self.child_runs = []
        self.identity = {}
        self.failed = set()
        self.ids = []
        self.cov = coverage.Coverage(data_file=None, source=[str(root / 'sqlglot'), str(root / 'tests')], config_file=False)

    def switch(self, item, phase):
        self.context = item.nodeid + '|' + phase
        self.cov.switch_context(self.context)

    def pytest_collection_modifyitems(self, items):
        self.ids = [i.nodeid for i in items]

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        self.switch(item, 'setup')

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        self.switch(item, 'call')

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item):
        self.switch(item, 'teardown')

    def pytest_runtest_logreport(self, report):
        if report.failed or report.skipped:
            self.failed.add(report.nodeid)

    def audit(self, event, args):
        if event != 'open' or not args or not isinstance(args[0], (str, bytes)):
            return
        try:
            path = os.fsdecode(args[0])
            if not os.path.isabs(path):
                path = os.path.join(str(root), path)
            path = os.path.normpath(path)
            prefix = str(root) + '/'
            if path.startswith(prefix):
                rel = path[len(prefix):]
                if rel.startswith('tests/') and not rel.endswith(('.py', '.pyc')):
                    self.reads.setdefault(rel, set()).add(self.context)
        except (OSError, ValueError):
            pass

if __name__ == "__main__":
    concurrent.futures.ProcessPoolExecutor = TracedPool
    c = Collector()
    sys.addaudithook(c.audit)
    from cold_scopes import start
    entered = start(root)
    from identity_usage import install as install_identity
    install_identity(root, c)
    from class_usage import install
    install(root, c)
    from child_trace import record
    record(root, c)
    c.cov.start()
    status = pytest.main(['tests', '-q', '-p', 'pytest_timeout', '--timeout=180', '-o', 'cache_dir=/tmp/blast-pytest-cache'], plugins=[c])
    c.cov.stop()
    if status not in (0, 1):
        raise SystemExit(f'Baseline collection failed: pytest exit {status}')
    ids = sorted(c.ids)
    index = {v: i for i, v in enumerate(ids)}
    groups = {}
    for nodeid in ids:
        groups.setdefault(nodeid.rsplit('::', 1)[0], []).append(index[nodeid])

    def expand(contexts):
        found = set()
        for ctx in contexts:
            if not ctx:
                continue
            nodeid, phase = ctx.rsplit('|', 1)
            if nodeid not in index:
                continue
            if phase == 'call':
                found.add(index[nodeid])
            else:
                found.update(groups[nodeid.rsplit('::', 1)[0]])
        return sorted(found)

    data = c.cov.get_data()
    files = {}
    for name in sorted(data.measured_files()):
        rel = str(Path(name).relative_to(root))
        contexts = data.contexts_by_lineno(name)
        lines = {str(n): expand(ctx) for n, ctx in contexts.items()}
        files[rel] = {k: v for k, v in lines.items() if v}
    # Merge child line coverage and mark modules touched by their imports. Early
    # attribute evaluation in a child cannot be inferred from its parent context.
    children = {}
    for ctx, report in c.child_runs:
        try:
            payload = json.loads(report.read_text())
            imported = payload['lines']
            if not imported:
                raise ValueError('no covered child source')
            for name, hits in imported.items():
                by_line = files.setdefault(name, {})
                children.setdefault(name, set()).update(expand([ctx]))
                for n in hits:
                    by_line[str(n)] = sorted(set(by_line.get(str(n), [])) | set(expand([ctx])))
            for name in payload['reads']:
                if not name.endswith(('.py', '.pyc')):
                    c.reads.setdefault(name, set()).add(ctx)
        except (OSError, ValueError, KeyError, TypeError):
            c.failed.add(ctx.rsplit('|', 1)[0])
        finally:
            report.unlink(missing_ok=True)
    reads = {k: expand(v) for k, v in sorted(c.reads.items())}
    # Collection-time fixture reads may feed every test; do not claim a narrower dependency.
    for key, contexts in c.reads.items():
        if '' in contexts:
            reads[key] = list(range(len(ids)))
    from attribute_usage import reference_map
    result = {'version': 2, 'commit': 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa',
              'python': platform.python_version(), 'tests': ids, 'lines': files,
              'identity': c.identity,
              'entered': {k: sorted(v) for k, v in entered.items()},
              'child_imports': {k: sorted(v) for k, v in children.items()},
              'attributes': reference_map(root, files),
              'classes': {k: expand(v) for k, v in c.classes.items()},
              'reads': reads, 'uncertain': sorted(c.failed), 'pytest_exit': int(status)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, separators=(',', ':')))
    print(f'Wrote {output}: {len(ids)} tests, {len(files)} source files, {len(reads)} fixture dependencies', file=sys.stderr)
