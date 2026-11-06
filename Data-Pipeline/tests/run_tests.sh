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
status=0
python -m pytest -p no:cacheprovider --cov --cov-fail-under=0 --cov-report=term-missing \
  Data-Pipeline/tests Model-Development/tests || status=$?
for folder in Data-Pipeline/scripts Model-Development/scripts; do
  echo "Coverage bar (70 percent) for $folder"
  python -m coverage report --include="$folder/*" --fail-under=70 --skip-covered || status=1
done
exit "$status"
