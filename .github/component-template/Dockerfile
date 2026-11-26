# Template component image (#159, ADR-037). Build the test target with the component folder as context:
#   docker build --target test -t component-template:test .github/component-template
# Two stages: `runtime` is what ships (requirements.txt and src/ only); `test` adds the test
# packages and tests/ on top of it, so the tests run on the exact packages that ship.
# The base is the one Model-Development/Dockerfile pins: the multi-arch index of
# python:3.12.13-slim, by digest, so a rebuilt tag cannot change the image.

FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS runtime

ENV PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
WORKDIR /app

# pip runs as root; pip check fails the build if the installed set is inconsistent.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip check

COPY src/ src/

# A non-root user. /app is its own, so pytest and coverage can write their files there; the
# packages and src/ stay root-owned.
RUN useradd --create-home --uid 10001 app \
    && chown app:app /app
USER app


FROM runtime AS test

USER root
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt \
    && pip check
COPY tests/ tests/
USER app
