# Blast Radius: baseline dependency selector

A submission for the RWX Builders Buildathon. It maps changes in the pinned
SQLGlot source to baseline test dependencies and chooses the lowest-cost worker
count under the published scoring formula.

## Run the official sample

```sh
make sample
```

Requires Docker, make and Python 3.9+. The image builds for `linux/amd64`, then
runs as `nobody` with no network, a read-only filesystem, 2 CPUs, 4 GiB RAM,
and a 64 MiB temporary filesystem. `make check` checks the output format.
The GitHub Actions workflow runs regression tests and `make sample` on Linux.

## How it works

1. During the image build, fetch SQLGlot at
   `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, install the supplied locked test
   environment, and run its unmodified tests with `SKIP_INTEGRATION=0`.
2. Collect per-test Python line coverage and fixture file reads. Setup/teardown
   dependencies are expanded to the test class. A synchronous Future-compatible
   pool adapter keeps baseline work visible to the tracer when tests normally
   dispatch it to a process pool. Arguments and results round-trip through the
   multiprocessing serializer to retain serialization dependencies. Actual subprocess launches and incomplete
   baseline tests are marked uncertain and selected conservatively.
3. At runtime, validate and apply each patch in memory against the original
   source. Entire-file Python AST equality identifies comment/formatting-only
   edits. SQL fixtures, including their comments and metadata, are never treated
   as Python comments. Every file in a mixed patch is processed.
4. For function-body changes, select tests that executed any part of the affected
   function. Fixture changes select recorded readers. Module/class assignments,
   imports, syntax errors, unknown files, unsupported patches, and missing coverage
   fall back to all tests. Newly supplied test IDs are included conservatively.
5. Try longest-processing-time scheduling for each count from 1 through 8, using
   integer milliseconds and the exact 2-second worker overhead. Choose the
   cheapest resulting schedule. An empty selection emits no workers.

Runtime does not execute patched code, access the network, write files, or read
sample answers. All maps are derived from the pinned baseline, never from patch
identities, edit categories, or known failing-test labels. Sample answers are used
only by the external scorer. They are excluded from the Docker build context.

## Local development without Docker

```sh
python3 -m unittest discover -s tests -v
# In an environment containing the locked dependencies and coverage==7.17.4:
python tools/build_map.py /path/to/pinned/sqlglot /tmp/dependencies.json
python selector.py --source /path/to/pinned/sqlglot \
  --map /tmp/dependencies.json --data sample > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

The final image regenerates the map using the competition's pinned Python 3.13
image. A native macOS development run cannot certify Linux container behavior.

## Limitations

Baseline coverage is a dependency heuristic, not a proof for arbitrary programs.
Dynamic imports, reflection, source inspection, cross-test state, and changes
that alter previously unexecuted paths can escape a baseline map. Conservative
fallbacks reduce this risk at the cost of additional tests. LPT worker scheduling
is deterministic but does not claim a globally optimal partition.

## Submit

Use this public repository and its exact 40-character commit on the competition
track page. Keep submitted commits publicly available until results are published.
Only a successful official run establishes the competition score; local scoring
reports simulated time, and the server additionally charges container CPU time.
