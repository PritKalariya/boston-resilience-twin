# The image every Airflow service in docker-compose.yaml runs (ADR-022).
# It is the ADR-013 base image plus the pins in Data-Pipeline/requirements.txt
# that the base image does not already ship. DAG and script code stays
# bind-mounted (ADR-017), so the only thing copied in is the requirements file.
#
# Rebuild after any change to Data-Pipeline/requirements.txt:
#   docker compose up -d --build --wait
FROM apache/airflow:3.3.2-python3.12

# Never root: pip installs into the airflow user's environment.
USER airflow

COPY --chown=airflow:0 Data-Pipeline/requirements.txt /opt/airflow/requirements.txt

# The Airflow pin and the constraints URL below both come from AIRFLOW_VERSION,
# which the base image sets, so bumping the FROM tag cannot leave either of them
# stale or point them at the wrong constraints. It does not carry over to
# Data-Pipeline/requirements.txt: that file's own -c line and its pins are
# edited by hand when the tag changes (ADR-022, Revisit when). The Python
# version stays literal: ADR-013 pins 3.12, and the base image does not carry it
# in an environment variable.
#
# apache-airflow is pinned because the constraints file pins every provider but
# not Airflow itself, so without the pin the resolver is free to move it. The
# requirements file names the same constraints on its own -c line; passing them
# here too holds the packages named on the command line to the same versions.
# pip check is the last step, so an inconsistent result fails the build.
RUN set -eu; \
    : "${AIRFLOW_VERSION:?the base image must set AIRFLOW_VERSION}"; \
    pip install --no-cache-dir \
        --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-3.12.txt" \
        "apache-airflow==${AIRFLOW_VERSION}" \
        --requirement /opt/airflow/requirements.txt; \
    pip check
