"""Inventory which Python functions ever entered the pinned baseline run."""
import sys


def start(root):
    monitor = sys.monitoring
    slot = next(i for i in range(6) if monitor.get_tool(i) is None and i != monitor.COVERAGE_ID)
    monitor.use_tool_id(slot, 'baseline-function-inventory')
    entered = {}
    prefix = str(root) + '/'
    def on_start(code, offset):
        path = code.co_filename
        if path.startswith(prefix):
            entered.setdefault(path[len(prefix):], set()).add(f'{code.co_name}:{code.co_firstlineno}')
        return monitor.DISABLE
    monitor.register_callback(slot, monitor.events.PY_START, on_start)
    monitor.set_events(slot, monitor.events.PY_START)
    return entered
