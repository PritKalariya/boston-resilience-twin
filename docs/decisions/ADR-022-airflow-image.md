# ADR-022: Airflow runs an image built from airflow.Dockerfile with the pipeline dependencies

- Status: accepted
- Date: 2026-10-22
- Owner: PritKalariya
- Phase: setup

## Context

ADR-017 pins `Data-Pipeline/requirements.txt` from Airflow's constraints for
`apache/airflow:3.3.2-python3.12`, and pins the packages the constraints file
does not list (geopandas, iniconfig, pyogrio, pyproj, pytest, shapely) at the
versions the tests passed on. The stock Airflow image ships none of those six,
so a DAG task that runs a script importing geopandas, shapely, pyogrio or
pyproj fails on import until the image has them (ADR-017 Consequences, issue
#69). The daily DAG in #22 is blocked on this, and so is #37, the Gantt
analysis of that DAG.

Three more tools are planned against the same interpreter: Great Expectations
(#17, #35), Fairlearn (#32) and DVC with a Cloud Storage remote (#18). Whether
they can share one environment with Airflow's constraints and pandas 3.0.5 had
not been measured.

## Decision

- One image, `brt-airflow:local`, built from `airflow.Dockerfile` at the repo
  root, runs on every Airflow service in `docker-compose.yaml`. It is
  `apache/airflow:3.3.2-python3.12` (ADR-013) plus `Data-Pipeline/requirements.txt`.
- The install runs as the `airflow` user, never root. The constraints URL and
  the Airflow pin on the Dockerfile's pip command line are both derived from
  `AIRFLOW_VERSION`, which the base image sets, so bumping the `FROM` tag
  cannot leave either of those two stale or point them at the wrong
  constraints; the build fails outright if that variable is missing. It does
  not cover `Data-Pipeline/requirements.txt`: that file's `-c` line and its
  pins are edited by hand (see Revisit when). The Python version in the
  constraints file name stays literal at 3.12 (ADR-013), because the base image
  does not expose it. For this tag that resolves to
  `constraints-3.3.2/constraints-3.12.txt` and
  `apache-airflow==3.3.2`, passed on the pip command line as well as on the
  requirements file's own `-c` line. Airflow is pinned because the constraints
  file pins every provider but not Airflow itself, so without it the resolver
  is free to move Airflow. `pip check` is the last step of the build, so an
  inconsistent result fails the build instead of a DAG run.
- The image copies `Data-Pipeline/requirements.txt` and nothing else.
  `airflow.Dockerfile.dockerignore` holds the build context to that one file.
  DAG and script code stays bind-mounted (ADR-017), so it is not baked in.
- The constraints are never overridden. No `--no-deps`, no edit to a pin to
  make a package fit. A tool that cannot resolve against them does not get
  installed into this image; it gets one of the fallbacks below.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Image built from `airflow.Dockerfile` (chosen)** | $0, local build | One Dockerfile, one dockerignore, an `image:` and `build:` entry in `docker-compose.yaml` | A pulled requirements change needs a rebuild; until then a task fails with `ModuleNotFoundError`, covered by a runbook entry |
| `_PIP_ADDITIONAL_REQUIREMENTS` | $0 | Least: one environment variable | Reinstalls on every container start, including each automatic restart under `restart: unless-stopped`; needs PyPI reachable at start; Airflow's docs reserve it for quick tests |
| Prebuilt image in Artifact Registry | Registry storage, in project 2 | CI/CD plus a push step | Nothing builds or pushes images yet (ADR-009 puts Artifact Registry in project 2, and CI/CD does not exist), so every member would hand-push; a stale tag is invisible until a task fails |

## Consequences

- Measured on a laptop on 2026-10-22, with the base image already pulled:
  a cold `docker compose up -d --build --wait` (no `brt-airflow:local`) took
  1m37s for the image build plus the stack start, and exited 0 with all four
  Airflow services healthy and `airflow-init` `Exited (0)`. A
  `docker compose build` with nothing changed took 1.0s, every step cached.
- Size: the base image is 3.22 GB on disk (670 MB content), `brt-airflow:local`
  is 3.44 GB on disk (721 MB content). The six packages cost about 0.22 GB on
  disk and 51 MB of content. The build context is 747 B.
- `docker compose build` builds once, not once per service. BuildKit runs the
  COPY and the pip install a single time and exports the same tag for the four
  services, so no `pull_policy` workaround is needed.
- A change to `Data-Pipeline/requirements.txt` needs
  `docker compose up -d --build --wait`. Changing a DAG or a script does not:
  those are bind-mounted. Adding a line and rebuilding took 1m18s end to end,
  of which the pip layer was 7.5s; the base layer stayed cached.
- Every pin in the requirements file matches the installed version inside the
  container (9 of 9), `airflow version` prints 3.3.2, and
  `python -c "import geopandas, shapely, pyogrio, pyproj"` prints geopandas
  1.2.0.
- Compatibility, measured with `pip install --dry-run` inside the image
  against `apache-airflow==3.3.2`, the copied requirements file and the same
  constraints. All three resolve, one at a time and all three together, with
  no conflict and with pandas held at 3.0.5:

  | Package | Resolves | Version |
  |---|---|---|
  | great-expectations | yes | 1.24.1, pulling altair 6.3.0 and mistune 3.3.4 |
  | fairlearn | yes | 0.15.0 |
  | dvc[gs] | yes | dvc 3.67.1, dvc-gs 3.1.0, plus 38 dependencies |

  The pandas ceiling that great-expectations 1.11.3 carried is gone: 1.24.1
  asks for `pandas>=1.3.0` on Python 3.12 with no upper bound. Nothing was
  added to `Data-Pipeline/requirements.txt`; the owners of #17, #35, #32 and
  #18 add their own lines.
- If a future tool does conflict, the constraints win. In order: another
  release of that tool that resolves; the tool in its own virtual environment,
  called from a DAG through `ExternalPythonOperator` or, for DVC, as a CLI on
  PATH; or replacing the tool, for example pandas slicing instead of Fairlearn.
  That last one is the owner's call, not the image's.
- Compiled packages install from wheels, and nothing builds from source. The
  build is linux/amd64 and used the amd64 wheels. The aarch64 wheels an Apple
  Silicon laptop would need were checked with `pip download`, not by running
  an arm64 build: shapely 2.2.0, pyogrio 0.13.0 and pyproj 3.8.0 resolve to
  `manylinux_2_28_aarch64`, and geopandas 1.2.0, pytest 9.1.1 and iniconfig
  2.3.1 to `py3-none-any`. No apt package is installed.

## Revisit when

- CI/CD starts building images: decide whether the stack runs a tag from
  Artifact Registry (project 2, ADR-009) instead of building locally.
- A tool conflicts with the constraints and needs its own virtual environment.
- The Airflow tag in ADR-013 changes: the `FROM` line is not the only edit.
  `Data-Pipeline/requirements.txt` has to move with it, both its own `-c` line
  and the numpy, pandas and pyarrow pins taken from the new constraints file
  (ADR-017); otherwise pip gets two constraint files that disagree. Inside the
  Dockerfile the Airflow pin and the constraints URL still follow
  `AIRFLOW_VERSION` and need no edit. A change to the Python minor version does
  need one, because 3.12 is literal there.
