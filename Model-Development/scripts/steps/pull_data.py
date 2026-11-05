"""Step: pull the data version the run trains on (#112, ADR-027). STUB.

PR 3 replaces the body with the DVC pull of the Data-Pipeline outputs and writes the
real data version. Output: `data_version.txt`.
"""

from __future__ import annotations

import os
import sys

import _common as C


def step(argv: list[str] | None) -> None:
    artifacts = C.parse_artifacts(__doc__.splitlines()[0], argv)
    C.stub_outcome()
    sha = os.environ.get("GITHUB_SHA") or "local"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "data_version.txt").write_text(f"stub-{sha}\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
