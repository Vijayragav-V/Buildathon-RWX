"""Measure cold pytest collection on the pinned checkout, without running test bodies."""
import json
import os
from pathlib import Path
import sys

import pytest


def measure(source, mapping):
    os.chdir(source)
    sys.path.insert(0, str(source))
    files = sorted({node.split('::', 1)[0] for node in mapping['tests']})
    costs = {}
    for filename in files:
        pid = os.fork()
        if pid == 0:
            try:
                with open(os.devnull, 'wb') as sink:
                    os.dup2(sink.fileno(), 1)
                    os.dup2(sink.fileno(), 2)
                result = pytest.main(['--collect-only', '--import-mode=importlib',
                                      '-p', 'no:cacheprovider', '-q', filename])
                os._exit(int(result))
            except BaseException:
                os._exit(2)
        _, status, usage = os.wait4(pid, 0)
        if not status:
            costs[filename] = round(usage.ru_utime + usage.ru_stime, 2)
    return costs


if __name__ == '__main__':
    src, data = (Path(arg).resolve() for arg in sys.argv[1:])
    mapping = json.loads(data.read_text())
    mapping['collection_cpu'] = measure(src, mapping)
    data.write_text(json.dumps(mapping, separators=(',', ':')))
