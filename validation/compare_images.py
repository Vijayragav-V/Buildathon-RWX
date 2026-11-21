"""Benchmark a local submission image on one labeled batch under scorer restrictions."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from score import evaluate_run

# Read cgroup counters inside the container. They include forked workers.
MONITOR = """import json, os, runpy, sys
from pathlib import Path
sys.path.insert(0, '/app')
sys.argv = ['/app/selector.py']
runpy.run_path(sys.argv[0], run_name='__main__')
cg = Path('/sys/fs/cgroup')
cpu = int(dict(s.split() for s in (cg/'cpu.stat').read_text().splitlines())['usage_usec'])
print('RWX_METRICS '+json.dumps([cpu, int((cg/'memory.peak').read_text())]), file=sys.stderr)
"""


def measure(image, data, repeat):
    tests = json.loads((data/'tests.json').read_text())['tests']
    answers = json.loads((data/'answers.json').read_text())['commits']
    command = ['docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'none',
               '--cpus', '2', '--memory', '4096m', '--memory-swap', '4096m', '--pids-limit', '256',
               '--read-only', '--tmpfs', '/tmp:rw,size=64m,mode=1777', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges', '--user', '65534:65534', '--env', 'HOME=/tmp',
               '-v', f'{data}/commits:/data/commits:ro', '-v', f'{data}/tests.json:/data/tests.json:ro',
               image, 'python', '-c', MONITOR]
    runs = []
    for _ in range(repeat):
        start = time.monotonic()
        proc = subprocess.run(command, capture_output=True, timeout=185, check=True)
        wall = time.monotonic() - start
        result = evaluate_run(proc.stdout, tests, answers)
        if not result['ok']:
            raise ValueError(result)
        cpu, peak = json.loads(proc.stderr.decode().rsplit('RWX_METRICS ', 1)[1])
        selections = {row['commit']: sum(map(len, row['workers'])) for row in
                      (json.loads(line) for line in proc.stdout.splitlines())}
        sim = result['simulated_milliseconds'] / 1000
        runs.append(dict(simulated=sim, cpu=cpu/1e6, total=sim+cpu/1e6, peak_bytes=peak,
                         wall=wall, misses=result['missed_commits'], selected=selections,
                         sha256=hashlib.sha256(proc.stdout).hexdigest()))
    return dict(image=image, patches=len(answers), median_total=statistics.median(r['total'] for r in runs),
                deterministic=len({r['sha256'] for r in runs}) == 1, runs=runs)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('data', type=Path)
    parser.add_argument('--repeat', type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(measure(args.image, args.data.resolve(), args.repeat), indent=2))
