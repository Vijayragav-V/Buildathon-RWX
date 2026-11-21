"""Collect dependencies from supported baseline Python -c children."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def record(root, collector):
    original = subprocess.Popen
    class TrackedPopen(original):
        def __init__(self, args, *pos, **kwargs):
            context = collector.context
            plain = (isinstance(args, (list, tuple)) and len(args) == 3
                     and args[0] in ('python', sys.executable) and args[1] == '-c'
                     and not kwargs.get('shell') and kwargs.get('executable') is None)
            if context and plain:
                fd, name = tempfile.mkstemp(prefix='blast-child-', suffix='.json')
                os.close(fd)
                collector.child_runs.append((context, Path(name)))
                script = 'import runpy; runpy.run_path(%r)["observe"](%r,%r,%r)' % (
                    str(Path(__file__).resolve()), str(root), name, args[2])
                args = [args[0], '-c', script]
            elif context:
                collector.failed.add(context.rsplit('|', 1)[0])
            super().__init__(args, *pos, **kwargs)
    subprocess.Popen = TrackedPopen


def observe(root, destination, code):
    import coverage
    accessed = set()
    def audit(event, args):
        if event == 'open' and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path.startswith(root + '/'):
                accessed.add(os.path.relpath(path, root))
    sys.addaudithook(audit)
    cov = coverage.Coverage(config_file=False, data_file=None, include=[root + '/*'])
    cov.start()
    exec(compile(code, '<string>', 'exec'), vars(sys.modules['__main__']))
    cov.stop()
    data = cov.get_data()
    lines = {os.path.relpath(p, root): data.lines(p) for p in data.measured_files()}
    Path(destination).write_text(json.dumps({'lines': lines, 'reads': sorted(accessed)}))
