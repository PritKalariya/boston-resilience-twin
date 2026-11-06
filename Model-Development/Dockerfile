# Training image for the model CI/CD workflow (#112, ADR-028). Build from the repo root:
#   docker build -f Model-Development/Dockerfile -t brt-train:local .
# Every workflow step except Slack runs in it. Pinned requirements arrive with PR 3.
FROM python:3.12.13-slim

WORKDIR /app
COPY Model-Development /app/Model-Development

# gates.py finds the repo root two folders above itself, which is /app here. The
# build fails if the gates or the poverty groups do not load.
RUN python Model-Development/scripts/gates.py --validate-config

RUN useradd --create-home --uid 10001 train
USER train
