"""Record native generator execution and code inspection on the unmodified checkout."""
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import tempfile

from child_observers import PoolInspections
from types import AsyncGeneratorType, FrameType, FunctionType, GeneratorType

import pytest


class GeneratorUses:
    def __init__(self, root):
        self.prefix = str(root / 'sqlglot') + '/'
        self.context = None
        self.main_thread = threading.get_ident()
        self.reading = threading.local()
        self.uses = {}
        self.opaque = set()
        self.finished = set()
        self.test_cpu = {}
        self.started = {}
        self.source_reads = {}
        self.source_opaque = set()
        self.tracked_launch = False

    @staticmethod
    def cpu_usec():
        try:
            with open('/sys/fs/cgroup/cpu.stat') as stream:
                return int(dict(line.split() for line in stream)['usage_usec'])
        except (OSError, KeyError, ValueError):
            return None

    def pytest_runtest_logstart(self, nodeid, location):
        self.started[nodeid] = self.cpu_usec()

    def pytest_runtest_logfinish(self, nodeid, location):
        start, end = self.started.pop(nodeid, None), self.cpu_usec()
        if start is not None and end is not None and end >= start:
            self.test_cpu[nodeid] = round((end - start) / 1_000_000, 4)


    def record(self, path, scope=None):
        if not path.startswith(self.prefix):
            return
        file = 'sqlglot/' + path[len(self.prefix):]
        key = f'{file}:{scope}' if scope else file
        owner = self.context if threading.get_ident() == self.main_thread else None
        self.uses.setdefault(key, set()).add(owner)

    def generator(self, code, offset, value=None):
        if not code.co_flags & (inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR) or not code.co_filename.startswith(self.prefix):
            return sys.monitoring.DISABLE
        self.record(code.co_filename, f'{code.co_name}:{code.co_firstlineno}')

    def audit(self, event, args):
        if event in ('os.fork', 'subprocess.Popen', 'os.posix_spawn', 'os.forkpty', 'os.system', 'os.exec'):
            owner = self.context if threading.get_ident() == self.main_thread else None
            self.opaque.add(owner)
            if not self.tracked_launch:
                self.source_opaque.add(owner)
            return
        if event not in ('open', 'object.__getattr__') or getattr(self.reading, 'busy', False):
            return
        self.reading.busy = True
        try:
            if event == 'object.__getattr__':
                obj, attr = args
                if (type(obj), attr) in (
                    (FunctionType, '__code__'), (GeneratorType, 'gi_code'),
                    (AsyncGeneratorType, 'ag_code'), (FrameType, 'f_code'),
                ):
                    code = getattr(obj, attr)
                    # Inserting lines can move other functions in the same file.
                    self.record(code.co_filename)
                elif (type(obj), attr) in ((GeneratorType, 'gi_frame'), (AsyncGeneratorType, 'ag_frame')):
                    code = obj.gi_code if type(obj) is GeneratorType else obj.ag_code
                    self.record(code.co_filename)
                return
            if not isinstance(args[0], (str, bytes)):
                return
            path = os.path.abspath(os.fsdecode(args[0]))
            if not path.startswith(self.prefix) or not path.endswith(('.py', '.pyc')):
                return
            if path.endswith('.pyc'):
                try:
                    path = importlib.util.source_from_cache(path)
                except ValueError:
                    path = path[:-1]
            # Ignore the loader's own reads only on the normal module execution stack.
            frame = sys._getframe(1)
            if frame.f_code.co_filename == '<frozen importlib._bootstrap_external>' and frame.f_code.co_name == 'get_data':
                parent = frame.f_back
                if (parent and parent.f_code.co_name == 'get_code' and parent.f_back
                        and parent.f_back.f_code.co_name == 'exec_module'
                        and parent.f_back.f_code.co_filename == '<frozen importlib._bootstrap_external>'):
                    return
            owner = self.context if threading.get_ident() == self.main_thread else None
            self.source_reads.setdefault('sqlglot/' + path[len(self.prefix):], set()).add(owner)
            self.record(path)
        finally:
            self.reading.busy = False

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        self.context = item.nodeid
        try:
            yield
        finally:
            self.context = None

    def pytest_runtest_logreport(self, report):
        if report.when == 'teardown':
            self.finished.add(report.nodeid)
        if not report.passed or hasattr(report, 'wasxfail'):
            self.opaque.add(report.nodeid)
            self.source_opaque.add(report.nodeid)


def build(root, mapping):
    os.chdir(root)
    sys.path.insert(0, str(root))
    obs = GeneratorUses(root)
    mon = sys.monitoring
    slot = next(i for i in range(6) if mon.get_tool(i) is None and i != mon.COVERAGE_ID)
    mon.use_tool_id(slot, 'generator dependencies')
    for event in (mon.events.PY_START, mon.events.PY_RESUME, mon.events.PY_RETURN):
        mon.register_callback(slot, event, obs.generator)
    mon.set_events(slot, mon.events.PY_START | mon.events.PY_RESUME | mon.events.PY_RETURN)
    sys.addaudithook(obs.audit)
    with tempfile.TemporaryDirectory(prefix='source-observer-') as directory:
        pools = PoolInspections(root, directory, obs)
        try:
            status = pytest.main(['tests', '-q', '--tb=line', '-p', 'no:cacheprovider',
                              '-p', 'pytest_timeout', '--timeout=180'], plugins=[obs])
        finally:
            mon.set_events(slot, 0)
            mon.free_tool_id(slot)
            complete = pools.finish(obs)
    ids = {n: i for i, n in enumerate(mapping['tests'])}
    if status or None in obs.opaque or not ids.keys() <= obs.finished:
        return {}
    mapping['test_cpu'] = obs.test_cpu
    opaque = {ids[n] for n in obs.opaque if n in ids}
    # A successful, complete observer run is required even for an empty reader set.
    def file_observers(records):
        uncertain = {ids[n] for n in obs.source_opaque if n in ids}
        return {path.relative_to(root).as_posix(): sorted(uncertain | {ids[n] for n in contexts if n in ids})
                for path in sorted((root/'sqlglot').rglob('*.py'))
                if None not in (contexts := records.get(path.relative_to(root).as_posix(), set()))}
    if complete and None not in obs.source_opaque:
        mapping['source_observers'] = file_observers(obs.source_reads)
        mapping['location_observers'] = file_observers(obs.uses)
    # Generator guards keep their original independently collected execution evidence.
    out = {}
    for key, contexts in obs.uses.items():
        file, sep, _ = key.partition(':')
        if sep and not (None in contexts or None in obs.uses.get(file, ())):
            combined = contexts.union(obs.uses.get(file, ()))
            out[key] = sorted(opaque | {ids[n] for n in combined if n in ids})
    return out


if __name__ == '__main__':
    root, dest = (Path(v).resolve() for v in sys.argv[1:])
    mapping = json.loads(dest.read_text())
    mapping['generator_users'] = build(root, mapping)
    dest.write_text(json.dumps(mapping, separators=(',', ':')))
