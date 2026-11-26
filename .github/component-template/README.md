# Component template (#159, ADR-037)

The pattern every new component copies for its own CI: its own image, its own job in `.github/workflows/ci.yml`, and a fixed 70 percent line-coverage bar. This folder is itself a component that runs on every pull request, so the pattern is proven each time.

```
Dockerfile              runtime stage (ships) and test stage (adds test packages and tests/)
requirements.txt        packages that ship, exact versions
requirements-test.txt   pytest and pytest-cov only, the versions in the Airflow test image
src/component_template/ the code under test
tests/                  conftest.py (blocks the network) and the tests
.dockerignore
```

The reusable workflow `.github/workflows/component-ci.yml` builds `docker build --target test` with the component folder as the context, then runs `python -m pytest tests --cov=<cov_source> --cov-fail-under=70` in the image with `--network none`. The bar is written in that file and is not an input.

## Start a new component

1. Copy this folder to the component's own top-level folder (ADR-017), for example `Serving/`, and keep `Dockerfile`, `requirements.txt`, `requirements-test.txt`, `.dockerignore`, `src/` and `tests/` at its root. Do not copy this README.
2. Rename `src/component_template/` to the component's package (`serving`), and replace `greeting.py` and `tests/test_greeting.py` with real code and tests.
3. In `requirements.txt`, pin every package that ships to an exact version. Leave `requirements-test.txt` as it is, unless the Airflow image's pytest changes. If the component's code is not under `src/` (for example `COPY app/`), change the `COPY` lines and `PYTHONPATH` in the Dockerfile.
4. Keep the base image line (the digest is the one in `Model-Development/Dockerfile`), the non-root user and `pip check`. Add a system package only in the `runtime` stage, as root, before the user is created.
5. Add the caller job to `.github/workflows/ci.yml` and its id to the `needs` of the `ci` job (runbook, "CI", "Add a component"). `Model-Development/tests/test_ci_workflows.py` fails if either is missing.
6. Run the two commands below and fix what they report before opening the pull request.

## Run it locally

From the repo root (Docker only):

```bash
docker build --target test -f .github/component-template/Dockerfile -t component-template:test .github/component-template
docker run --rm --network none component-template:test python -m pytest tests --cov=component_template --cov-fail-under=70 --cov-report=term-missing
```

The second command exits non-zero below 70 percent.
