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
   environment, and run its baseline tests with `SKIP_INTEGRATION=0`. The on-disk checkout is
   unchanged; build-only observers record class use and comparison behavior.
2. Collect per-test Python line coverage and fixture file reads. Setup/teardown
   dependencies are expanded to the test class. A synchronous Future-compatible
   pool adapter keeps baseline work visible to the tracer when tests normally
   dispatch it to a process pool. Arguments and results round-trip through the
   multiprocessing serializer to retain serialization dependencies. The collector also records covered lines and file reads in supported Python
   subprocesses. Other launches and incomplete runs retain conservative coverage.
3. At runtime, validate and apply each patch in memory against the original
   source. Entire-file Python AST equality identifies comment/formatting-only
   edits. SQL fixtures, including their comments and metadata, are never treated
   as Python comments. Every file in a mixed patch is processed.
4. For function-body changes, select tests covering the affected expression,
   falling back to function coverage when needed. Fixture changes select recorded
   readers. Literal changes within existing class assignments use baseline class
   consumers, including inheritance and cached dialect instances. For a dictionary edit that preserves every literal key, a build-time index
   identifies code that reads the values. The analysis follows supported aliases
   and excludes settings with unresolved consumers. A separate index covers
   private constants whose readers are confined to their defining module.
   Unsupported import changes, structural edits, syntax errors, and missing
   dependency information retain broader coverage. Newly supplied test IDs remain conservative.
   Identity-to-equality edits can skip tests only when every observed baseline
   operand pair was equivalent under known, side-effect-free equality methods.
   Custom equality methods are never invoked by this observer.
5. Try longest-processing-time scheduling for each count from 1 through 8, using
   integer milliseconds and the exact 2-second worker overhead. Choose the
   cheapest resulting schedule. An empty selection emits no workers.

For small groups of dialect tests, the selector can run pytest on a temporary
copy of the changed modules under `/tmp`. A time budget limits this work. It
removes a test only after pytest confirms all phases passed. A timeout, failed
subtest, collection error, or missing report preserves the original selection. All maps are derived from the pinned baseline, never from patch
identities, edit categories, or known failing-test labels. Sample answers are used
only by the external scorer. They are excluded from the Docker build context.

## Local development without Docker

```sh
python3 -m unittest discover -s tests -v
# In an environment containing the locked dependencies and coverage==7.17.4:
python tools/build_map.py /path/to/pinned/sqlglot /tmp/dependencies.json
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 SKIP_INTEGRATION=0 python value_index.py /path/to/pinned/sqlglot /tmp/dependencies.json /tmp/runtime-map.json
python selector.py --source /path/to/pinned/sqlglot \
  --map /tmp/runtime-map.json --data sample > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

The final image regenerates the map using the competition's pinned Python 3.13
image. A native macOS development run cannot certify Linux container behavior.

## Limitations

Baseline coverage is a dependency heuristic, not a proof for arbitrary programs.
Class-consumer and observed-comparison optimizations are also baseline
   heuristics, not proofs for unobserved inputs. Import-time factory errors or
   metaclass side effects from changed constants can escape class-use maps.
   Dynamic imports, reflection, source inspection, cross-test state, and changes
that alter previously unexecuted paths can escape a baseline map. Conservative
fallbacks reduce this risk at the cost of additional tests. LPT worker scheduling
is deterministic but does not claim a globally optimal partition.

## Submit

Use this public repository and its exact 40-character commit on the competition
track page. Keep submitted commits publicly available until results are published.
Only a successful official run establishes the competition score; local scoring
reports simulated time, and the server additionally charges container CPU time.

## Validation

The cached Linux amd64 build completed `make sample` at **105.320 simulated
seconds with zero missed commits** across the 30 public patches. The preceding
local image scored 108.347 seconds on those same inputs. The two expensive
integration-test selections remain because the sample labels record failures.
The competition adds container CPU time and evaluates a different batch.
See `validation/value-index-results.json` for the independent comparisons.
