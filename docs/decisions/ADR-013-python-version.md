# ADR-013: Python 3.12 for all project code and images

- Status: accepted. Its open item on aligning `Data-Pipeline/requirements.txt` with the Airflow constraints is closed by ADR-017.
- Date: 2026-10-16
- Owner: Pu1ni2
- Phase: setup

## Context

Issue #2 asks for one Python version for the whole project, pinned in an ADR and checked against the Airflow image. The project spec listed it as an open choice. The same version has to work for Airflow DAGs and pipeline scripts, model training (LightGBM, XGBoost, Optuna, SHAP), tracking (MLflow), data checks (Great Expectations, Evidently, Fairlearn), DVC, the Vertex AI SDK, the Cloud Run images, CI (#21), and every member's local environment.

Airflow 3.3.2 is the latest stable release (2026-09-17); Airflow 2 reached end of life on 2026-04-22. It publishes images for Python 3.10 to 3.14, and its unsuffixed tag `apache/airflow:3.3.2` runs Python 3.13, so the image tag has to name the Python version explicitly.

Python support of the planned libraries, checked on PyPI on 2026-10-16:

| Library | Latest | 3.11 | 3.12 | 3.13 | 3.14 |
|---|---|---|---|---|---|
| apache-airflow | 3.3.2 | yes | yes | yes | yes |
| great-expectations | 1.24.0 | yes | yes | yes | yes, only since 1.24.0 (2026-10-07) |
| xgboost | 3.4.1 | no, needs 3.12+ | yes | yes | yes |
| shap | 0.53.0 | no, needs 3.12+ | yes | yes | yes |
| mlflow | 3.17.1 | yes | yes | installs, not declared | installs, not declared |
| evidently | 0.7.23 | yes | yes | yes, not tested on Windows | not declared |
| ruptures (candidate for change-point checks) | 1.1.10 | yes | yes | yes | no |
| fairlearn, lightgbm, optuna, dvc, dvc-gs, pandas, pyarrow, scikit-learn, statsmodels, google-cloud-aiplatform, google-cloud-storage | current | yes | yes | yes | yes |

3.12 and 3.13 are the only versions every library supports, and 3.12 is the only one every library both declares and tests (MLflow's CI stops at 3.12). Both are in the security-fix-only phase; 3.12 reaches end of life in October 2028, after the course ends in December 2026.

## Decision

- **Python 3.12** for all project code: Airflow DAGs and pipeline scripts, model training, Cloud Run services, CI, and local environments.
- **Airflow image** `apache/airflow:3.3.2-python3.12`, pinned in `docker-compose.yaml`.
- **`.python-version`** at the repo root says `3.12`. Tools that read it (pyenv, uv, and the `python-version-file` input of `actions/setup-python`) follow it.
- **Cloud Run images** use Python 3.12; base image decided in the serving ADR.
- **Checked against the image on 2026-10-16:** inside `apache/airflow:3.3.2-python3.12`, `python --version` prints `Python 3.12.14` and `airflow version` prints `3.3.2`. The `hello` DAG run `verify_20261016T223438Z` succeeded, including its `check_python_pin` task, which fails when the image's Python is not 3.12. Its log reads "Python 3.12.14 matches the ADR-013 pin".

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Python 3.11 | $0 | Medium: hold xgboost at 3.2.0 and shap at 0.51.0 | The newest xgboost and shap refuse to install, so the project stays on older releases all term |
| Python 3.12 (chosen) | $0 | Low: one tag suffix | Security fixes only; end of life (2028-10) is after the course |
| Python 3.13 | $0 | Low: it is the default Airflow tag | MLflow does not declare it and Evidently does not test it on Windows, so a break would show up at run time instead of install time |
| Python 3.14 | $0 | High | ruptures does not install; MLflow and Evidently do not declare it; Great Expectations added it nine days before this decision |

## Consequences

- Every member needs Python 3.12 for running scripts and tests outside Docker, for example `uv python install 3.12`, `conda create -n brt python=3.12`, or the python.org 3.12.10 installer (the last 3.12 release with Windows installers).
- The Airflow image ships pandas 3.0.5 and pyarrow 25.0.1, while `Data-Pipeline/requirements.txt` only asks for `pandas>=2.2`. The current 14 tests pass inside the image (checked 2026-10-16). When pipeline scripts move into DAGs (#22), align the requirements with the Airflow constraints file (https://raw.githubusercontent.com/apache/airflow/constraints-3.3.2/constraints-3.12.txt).
- Changing the Airflow tag or the Python minor version is a PR that updates this ADR, `.python-version`, and `PINNED_PYTHON` in `Data-Pipeline/dags/hello.py`.
- CI (#21) should read `.python-version` instead of hard-coding a version.

## Revisit when

- A required library drops Python 3.12 or needs 3.13 or newer.
- Airflow stops publishing 3.12 images.
- MLflow and Evidently declare and test 3.13 on all platforms and the team wants the default Airflow image.
- The project continues toward October 2028.
