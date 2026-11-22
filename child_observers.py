"""Capture source and code inspections in spawned Python pool workers during indexing."""
import atexit
import json
import os
from pathlib import Path
import sys
from types import AsyncGeneratorType, FrameType, FunctionType, GeneratorType


def observed_file(event, args):
    """Return (filename, raw source read) for executable-code observation."""
    if event == 'object.__getattr__':
        obj, attr = args
        if (type(obj), attr) in ((FunctionType, '__code__'), (GeneratorType, 'gi_code'),
                                 (AsyncGeneratorType, 'ag_code'), (FrameType, 'f_code')):
            return getattr(obj, attr).co_filename, False
        if (type(obj), attr) in ((GeneratorType, 'gi_frame'), (AsyncGeneratorType, 'ag_frame')):
            return (obj.gi_code if type(obj) is GeneratorType else obj.ag_code).co_filename, False
        return None
    if event != 'open' or not isinstance(args[0], (str, bytes)):
        return None
    path = os.fsdecode(args[0])
    if not path.endswith(('.py', '.pyc')):
        return None
    # The audit callback calls this helper directly.
    frame = sys._getframe(2)
    loader = '<frozen importlib._bootstrap_external>'
    if frame.f_code.co_filename == loader and frame.f_code.co_name == 'get_data':
        caller = frame.f_back
        if (caller and caller.f_code.co_name == 'get_code' and caller.f_back
                and caller.f_back.f_code.co_filename == loader and caller.f_back.f_code.co_name == 'exec_module'):
            return None
    if path.endswith('.pyc'):
        import importlib.util
        try:
            path = importlib.util.source_from_cache(path)
        except ValueError:
            path = path[:-1]
    return os.path.realpath(path), True


def start_child(root, directory):
    import threading
    local = threading.local()
    prefix = str(Path(root)/'sqlglot') + '/'
    raw, metadata, unknown = set(), set(), set()
    def audit(event, args):
        if event in ('subprocess.Popen', 'os.posix_spawn', 'os.fork', 'os.system', 'os.exec'):
            unknown.add(event)
        if event not in ('open', 'object.__getattr__') or getattr(local, 'busy', False):
            return
        local.busy = True
        try:
            result = observed_file(event, args)
            if result and result[0].startswith(prefix):
                file = 'sqlglot/' + result[0][len(prefix):]
                (raw if result[1] else metadata).add(file)
        finally:
            local.busy = False
    sys.addaudithook(audit)
    def save():
        p = Path(directory) / str(os.getpid())
        p.write_text(json.dumps([sorted(raw), sorted(metadata), sorted(unknown)]))
    atexit.register(save)


class PoolInspections:
    def __init__(self, root, directory, observer):
        import multiprocessing.util
        import shutil
        import subprocess
        self.directory = Path(directory)
        self.pids = {}
        self.popen = subprocess.Popen
        self.original = multiprocessing.util.spawnv_passfds
        self.pythonpath = os.environ.get('PYTHONPATH')
        hook = self.directory/'sitecustomize.py'
        hook.write_text('import runpy\nrunpy.run_path(%r)["start_child"](%r,%r)\n' %
                        (__file__, str(root), directory))
        os.environ['PYTHONPATH'] = directory + (os.pathsep + self.pythonpath if self.pythonpath else '')
        def spawn(exe, args, passfds):
            pid = self.original(exe, args, passfds)
            # The stdlib resource tracker only consumes a control pipe and may
            # outlive pytest. Every actual pool interpreter must return a report.
            if not (len(args) >= 3 and args[-2] == '-c' and
                    str(args[-1]).startswith('from multiprocessing.resource_tracker import main;main(')
                    and str(args[-1]).removeprefix('from multiprocessing.resource_tracker import main;main(')
                    .removesuffix(')').isdigit()):
                self.pids[pid] = None
            return pid
        multiprocessing.util.spawnv_passfds = spawn
        owner = self

        class ObservedPopen(self.popen):
            def __init__(self, args, *pos, **kwargs):
                simple = (isinstance(args, (tuple, list)) and len(args) >= 3 and args[1] == '-c'
                          and not pos and not kwargs.get('shell') and kwargs.get('env') is None
                          and kwargs.get('executable') is None and isinstance(args[0], str)
                          and os.path.realpath(shutil.which(args[0]) or '') == os.path.realpath(sys.executable))
                observer.tracked_launch = simple
                try:
                    super().__init__(args, *pos, **kwargs)
                finally:
                    observer.tracked_launch = False
                if simple:
                    owner.pids[self.pid] = observer.context

        subprocess.Popen = ObservedPopen

    def finish(self, observer):
        import multiprocessing.util
        import subprocess
        subprocess.Popen = self.popen
        multiprocessing.util.spawnv_passfds = self.original
        if self.pythonpath is None:
            os.environ.pop('PYTHONPATH', None)
        else:
            os.environ['PYTHONPATH'] = self.pythonpath
        for pid, owner in self.pids.items():
            try:
                raw, metadata, unknown = json.loads((self.directory/str(pid)).read_text())
            except (ValueError, OSError):
                observer.source_opaque.add(owner)
                continue
            if unknown:
                observer.source_opaque.add(owner)
            # Pools can serve several tests. An inspection in a child is unowned.
            for file in raw:
                observer.source_reads.setdefault(file, set()).add(owner)
            for file in set(raw) | set(metadata):
                observer.uses.setdefault(file, set()).add(owner)
        return True
