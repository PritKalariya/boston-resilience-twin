#!/usr/bin/env bash
# The full test suite with coverage (#38), as docker-compose.yaml's test service runs it in
# the Airflow image:  docker compose run --rm --build test [pytest arguments]
# It prints the interpreter and stops unless it is Python 3.12 (ADR-013), then runs every
# test in Data-Pipeline/tests and Model-Development/tests with the coverage report and a 70 percent bar
# for each scripts folder separately (.coveragerc). Arguments replace those default folders and go to pytest as given, for example
#   docker compose run --rm test Data-Pipeline/tests/test_dvc_push.py -x --no-cov
set -euo pipefail
cd "$(dirname "$0")/../.."  # the repo root, where .coveragerc is

python - <<'PY'
import sys
from importlib.metadata import PackageNotFoundError, version

print(f"Python {sys.version.split()[0]} ({sys.executable})")
if sys.version_info[:2] != (3, 12):
    sys.exit(f"ADR-013 pins Python 3.12, but this is {sys.version_info[0]}.{sys.version_info[1]}; "
             "run the tests with: docker compose run --rm --build test")
try:
    print(f"Airflow {version('apache-airflow')}")
except PackageNotFoundError:
    print("Airflow is not installed: the DAG tests skip (they fail instead when BRT_TEST_SERVICE=1)")
PY

# With arguments: pytest as given, measuring Data-Pipeline/scripts against the 70 percent bar.
if [ "$#" -gt 0 ]; then
  exec python -m pytest -p no:cacheprovider --cov=Data-Pipeline/scripts --cov-fail-under=70 \
    --cov-report=term-missing "$@"
fi

# No arguments: the full suite, then a 70 percent bar for each scripts folder on its own (#112).
# BRT_TEST_SUITES names the component folders to test (default both); CI sets it from
# .github/scripts/test_scope.py on a pull request (#165), and an empty value tests nothing.
# Each folder's bar is checked only when its tests ran. The tests run in parallel
# (pytest-xdist, #165); pytest-cov merges the workers' coverage before the bars read it.
# BRT_TEST_WORKERS sets the worker count (0 runs in one process). The default is one per logical
# CPU, at most 8: past that a big laptop gains little and each worker holds its own copy of the
# test data. Not -n auto: with psutil installed it counts physical cores, 2 on CI's 4-vCPU runner.
# --dist worksteal lets an idle worker take tests queued for a busy one, so one slow file does
# not hold up the end of the run.
suites="${BRT_TEST_SUITES-Data-Pipeline Model-Development}"
if [ -z "$suites" ]; then
  echo "No test input changed (BRT_TEST_SUITES is empty): no tests to run."
  exit 0
fi
tests=()
cov=()
for suite in $suites; do
  case "$suite" in
    Data-Pipeline|Model-Development) tests+=("$suite/tests"); cov+=("--cov=$suite/scripts") ;;
    *) echo "Unknown suite in BRT_TEST_SUITES: $suite" >&2; exit 2 ;;
  esac
done
cpus="$(python -c 'import os; print(os.cpu_count() or 1)')"
workers="${BRT_TEST_WORKERS:-$(( cpus < 8 ? cpus : 8 ))}"
echo "Testing: $suites ($workers workers)"
status=0
python -m pytest -p no:cacheprovider -n "$workers" --dist worksteal \
  "${cov[@]}" --cov-fail-under=0 --cov-report=term-missing "${tests[@]}" || status=$?
for suite in $suites; do
  echo "Coverage bar (70 percent) for $suite/scripts"
  python -m coverage report --include="$suite/scripts/*" --fail-under=70 --skip-covered || status=1
done
exit "$status"
