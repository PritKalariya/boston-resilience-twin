#!/usr/bin/env bash
# Run one command in the training image with the mounts every model workflow step uses
# (#112, ADR-028, ADR-032). The prefix lives here once; model.yml calls it for each step:
#   IMAGE=brt-train:ci .github/scripts/in_image.sh python Model-Development/scripts/steps/step_train.py --artifacts artifacts
# Run it from the repo root, after the DVC pull. The image holds no data and no credentials:
#   - the four files training reads outside Model-Development, read-only (the labels and
#     features DVC pulled, the feature catalog and dvc.lock, which names the data version);
#   - Model-Development/config, read-only;
#   - artifacts/ (or the folder ARTIFACTS_DIR names, for a local run outside the checkout), writable, and its runs, mlruns and reports folders over the folders the
#     training code writes (MLFLOW_TRACKING_URI stays empty, so MLflow uses its local store,
#     artifacts/mlruns, which the workflow uploads).
# --user is the runner's uid, so everything written is the runner's to upload.
set -euo pipefail
: "${IMAGE:?set IMAGE to the training image}"
# Create the folder first: resolving a path that does not exist yet fails.
mkdir -p "${ARTIFACTS_DIR:-$PWD/artifacts}"
art="$(realpath "${ARTIFACTS_DIR:-$PWD/artifacts}")"
mkdir -p "$art/runs" "$art/mlruns" "$art/reports"
# --mount, not -v: a missing source is an error ("bind source path does not exist"), where -v
# makes Docker create a root-owned directory in its place. A path with a comma cannot be a
# --mount source; none of the repo's paths has one.
exec docker run --rm --user "$(id -u):$(id -g)" \
  -e GITHUB_SHA \
  --mount "type=bind,source=$PWD/Data-Pipeline/data/processed/labels.parquet,target=/app/Data-Pipeline/data/processed/labels.parquet,readonly" \
  --mount "type=bind,source=$PWD/Data-Pipeline/data/processed/features.parquet,target=/app/Data-Pipeline/data/processed/features.parquet,readonly" \
  --mount "type=bind,source=$PWD/Data-Pipeline/config/feature_catalog.csv,target=/app/Data-Pipeline/config/feature_catalog.csv,readonly" \
  --mount "type=bind,source=$PWD/Data-Pipeline/dvc.lock,target=/app/Data-Pipeline/dvc.lock,readonly" \
  --mount "type=bind,source=$PWD/Model-Development/config,target=/app/Model-Development/config,readonly" \
  --mount "type=bind,source=$art,target=/app/artifacts" \
  --mount "type=bind,source=$art/runs,target=/app/Model-Development/runs" \
  --mount "type=bind,source=$art/mlruns,target=/app/Model-Development/mlruns" \
  --mount "type=bind,source=$art/reports,target=/app/Model-Development/reports" \
  "$IMAGE" "$@"
