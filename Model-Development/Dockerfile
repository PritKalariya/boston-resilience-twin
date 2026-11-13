# Training image for the model CI/CD workflow (#112, ADR-028, ADR-032). Build from the repo root:
#   docker build -f Model-Development/Dockerfile -t brt-train:local .
# Every workflow step except the DVC pull and Slack runs in it. It holds the code and the
# pinned packages, never data or credentials: the workflow mounts the data files and the
# config read-only and artifacts/ read-write (ADR-032).
FROM python:3.12.13-slim

# LightGBM loads the OpenMP runtime, a system library pip cannot install: without it
# `import lightgbm` fails with "libgomp.so.1: cannot open shared object file". The one apt
# step, as root, written as airflow.Dockerfile writes it (ADR-022, ADR-032).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# pip runs as root, before the non-root user exists in use (ADR-032); pip check fails the
# build if the installed set is inconsistent.
ENV PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    GIT_PYTHON_REFRESH=quiet \
    LOGNAME=train
# GIT_PYTHON_REFRESH: the image has no git, so train.py records no commit (the workflow's
# GITHUB_SHA is the commit of record) and MLflow's import of GitPython stays quiet.
# LOGNAME: a --user uid with no passwd entry would make MLflow warn on every run.
WORKDIR /app
COPY Model-Development/requirements.txt /app/Model-Development/requirements.txt
RUN pip install --no-cache-dir -r Model-Development/requirements.txt \
    && pip check

# gates.py finds the repo root two folders above itself, which is /app here. The build
# fails if the gates or the poverty groups do not load.
COPY Model-Development /app/Model-Development
RUN python Model-Development/scripts/gates.py --validate-config

# The workflow runs the image with --user set to the runner's uid, so artifacts/ is the
# runner's to upload; the named user is for a plain `docker run`.
RUN useradd --create-home --uid 10001 train
USER train
