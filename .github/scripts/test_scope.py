"""Which test folders a pull request needs (#165).

The CI job "Lint and test (Python 3.12)" runs `docker compose run --rm test`, which can see
only the two component folders and the shared files the test service mounts or builds from
(docker-compose.yaml). So a pull request runs a component's tests only when it changes that
component, a file the component's tests read from the other one, or a shared file; a change
to a shared file runs every test. Main, and any diff that cannot be computed, runs every test.

    python .github/scripts/test_scope.py --base <sha> --head <sha>
    python .github/scripts/test_scope.py --all

Prints the folders to test, space separated (empty when no test input changed), and with
--github-output appends `suites=<folders>` to the file GITHUB_OUTPUT names.
test_ci_scope.py fails when a component starts reading a file of the other one that is not
listed below.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

COMPONENTS = ("Data-Pipeline", "Model-Development")

# Everything outside the two component folders that the test image or the test service
# reads (docker-compose.yaml `test`: its build and its mounts), plus the files inside them
# that every test run uses: the image's pins and the runner script. A change here runs all.
SHARED = (
    ".coveragerc",
    ".dvc/config",
    ".env.example",
    ".github/",
    ".python-version",
    "airflow.Dockerfile",
    "airflow.Dockerfile.dockerignore",
    "docker-compose.yaml",
    "docs/contracts/",
    "docs/data/",
    "Data-Pipeline/requirements.txt",
    "Data-Pipeline/tests/run_tests.sh",
)

# Files a component's code or tests read from the other component's folder.
READS_FROM_OTHER = {
    "Data-Pipeline": (
        "Model-Development/config/slice_groups.csv",
    ),
    "Model-Development": (
        "Data-Pipeline/config/feature_catalog.csv",
        "Data-Pipeline/config/processing.csv",
        "Data-Pipeline/dvc.lock",
        "Data-Pipeline/scripts/alerts.py",
    ),
}


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in prefixes)


def scope(changed: list[str]) -> list[str]:
    """The component folders whose tests the changed paths need, in COMPONENTS order."""
    if any(_matches(path, SHARED) for path in changed):
        return list(COMPONENTS)
    return [
        component
        for component in COMPONENTS
        if any(
            path.startswith(component + "/") or _matches(path, READS_FROM_OTHER[component])
            for path in changed
        )
    ]


def changed_files(base: str, head: str) -> list[str]:
    """Paths changed between the merge base of base and head, and head."""
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="the pull request's base commit")
    parser.add_argument("--head", help="the pull request's head commit")
    parser.add_argument("--all", action="store_true", help="every test folder (main)")
    parser.add_argument("--github-output", action="store_true",
                        help="also append suites=... to $GITHUB_OUTPUT")
    args = parser.parse_args(argv)

    if args.all:
        suites, why = list(COMPONENTS), "a full run was asked for"
    elif not (args.base and args.head):
        parser.error("give --base and --head, or --all")
    else:
        try:
            changed = changed_files(args.base, args.head)
        except subprocess.CalledProcessError as err:
            suites, why = list(COMPONENTS), f"the diff failed ({err.stderr.strip()}), so every test runs"
        else:
            suites = scope(changed)
            why = f"{len(changed)} changed file(s)"
            if any(_matches(path, SHARED) for path in changed):
                why += ", a shared file among them"

    print(f"test scope: {' '.join(suites) or 'none'} ({why})", file=sys.stderr)
    print(" ".join(suites))
    if args.github_output:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"suites={' '.join(suites)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
