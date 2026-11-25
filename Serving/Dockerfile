# The model service image (#161, ADR-037). Build from the repository root, which is the context:
#   docker build -f Serving/Dockerfile --target test -t serving:test .                 # CI: no model needed
#   docker build -f Serving/Dockerfile --target runtime --build-arg MODEL_VERSION=3 \
#     --build-context model=<folder with model.joblib and metadata.json> -t serving:3 .
# The model folder enters only as the named build context `model` (docker compose: additional_contexts),
# locally and in the cloud alike. Stages: `app` (the code and packages), `test` (adds the tests, on the exact
# packages that ship), `runtime` (adds the model and what runs).
# The base is the one Model-Development/Dockerfile pins: the multi-arch index of python:3.12.13-slim, by
# digest, so a rebuilt tag cannot change the image under Model-Development/requirements.lock.
FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS app

# LightGBM loads the OpenMP runtime, a system library pip cannot install. The one apt step, as root, as in
# Model-Development/Dockerfile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
WORKDIR /app

# -c keeps every package the training image's lock pins at the lock's version, so the saved model is scored
# with the libraries it was trained with. pip check fails the build if the installed set is inconsistent.
COPY Model-Development/requirements.lock /app/Model-Development/requirements.lock
COPY Serving/requirements.txt /app/Serving/requirements.txt
RUN pip install --no-cache-dir -r Serving/requirements.txt -c Model-Development/requirements.lock \
    && pip check

# Loading and scoring the saved model (a scikit-learn FixedThresholdClassifier over a LightGBM model) imports
# scikit-learn and lightgbm only, none of Model-Development/scripts, so no module of it is copied here.
COPY docs/contracts/schemas /app/contracts/schemas
COPY Frontend/ /app/frontend/
COPY Serving/src /app/src

# A non-root user that owns nothing under /app: the packages, the code and (in runtime) the model are
# root-owned and read-only to it.
RUN useradd --create-home --uid 10001 app
USER app


FROM app AS test

USER root
# The fixture model is trained with the repository's own LightGBM wrapper (models.py), at the same path the
# training image has it. These five modules are all it imports, and only the tests use them.
COPY Serving/requirements-test.txt /app/Serving/requirements-test.txt
RUN pip install --no-cache-dir -r Serving/requirements-test.txt -c Model-Development/requirements.lock \
    && pip check
COPY Model-Development/scripts/models.py Model-Development/scripts/inner_folds.py \
     Model-Development/scripts/model_params.py Model-Development/scripts/training_data.py \
     Model-Development/scripts/seasonal_baseline.py /app/Model-Development/scripts/
COPY docs/contracts/fixtures/predict/request.json docs/contracts/fixtures/predict/response.json /app/contracts/fixtures/predict/
COPY docs/contracts/fixtures/forecasts/2026-11-26.parquet docs/contracts/fixtures/forecasts/2026-11-26.records.json /app/contracts/fixtures/forecasts/
COPY Serving/scripts /app/scripts
COPY Serving/tests /app/tests
# Only here does the user own /app, so pytest and coverage can write their files there.
RUN chown app:app /app
USER app


# Only a build that is given the model folder gets past this stage: with no `model` build context this
# stage is the one BuildKit uses, and it stops the build with the message. A given context replaces it.
FROM app AS model
RUN echo "ERROR: no model folder. Build the runtime target with --build-context model=<folder holding model.joblib and metadata.json> (docker compose: SERVE_MODEL_DIR)." >&2 \
    && exit 1


FROM app AS runtime

ARG MODEL_VERSION
RUN test -n "$MODEL_VERSION" \
    || { echo "ERROR: build arg MODEL_VERSION is required: the Vertex model version id, or 0 for a local model that is not registered." >&2; exit 1; }
ENV MODEL_VERSION=$MODEL_VERSION \
    MODEL_DIR=/app/model
LABEL org.opencontainers.image.title="boston-resilience-twin model service" \
      brt.model_version=$MODEL_VERSION

COPY --from=model model.joblib metadata.json /app/model/

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8080'), timeout=4)"]

CMD ["python", "-m", "serving"]
