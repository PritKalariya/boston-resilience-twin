#!/usr/bin/env bash
# The full test suite with coverage (#38), as docker-compose.yaml's test service runs it in
# the Airflow image:  docker compose run --rm --build test [pytest arguments]
# It prints the interpreter and stops unless it is Python 3.12 (ADR-013), then runs every
# test with the coverage report and bar in .coveragerc. Arguments replace the default
# test folder and go to pytest as given, for example
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

[ "$#" -gt 0 ] || set -- Data-Pipeline/tests  # no arguments: the full suite
exec python -m pytest -p no:cacheprovider --cov --cov-report=term-missing "$@"
