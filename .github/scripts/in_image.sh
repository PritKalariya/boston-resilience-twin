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
art="$(realpath "${ARTIFACTS_DIR:-$PWD/artifacts}")"
mkdir -p "$art/runs" "$art/mlruns" "$art/reports"
exec docker run --rm --user "$(id -u):$(id -g)" \
  -e GITHUB_SHA \
  -v "$PWD/Data-Pipeline/data/processed/labels.parquet:/app/Data-Pipeline/data/processed/labels.parquet:ro" \
  -v "$PWD/Data-Pipeline/data/processed/features.parquet:/app/Data-Pipeline/data/processed/features.parquet:ro" \
  -v "$PWD/Data-Pipeline/config/feature_catalog.csv:/app/Data-Pipeline/config/feature_catalog.csv:ro" \
  -v "$PWD/Data-Pipeline/dvc.lock:/app/Data-Pipeline/dvc.lock:ro" \
  -v "$PWD/Model-Development/config:/app/Model-Development/config:ro" \
  -v "$art:/app/artifacts" \
  -v "$art/runs:/app/Model-Development/runs" \
  -v "$art/mlruns:/app/Model-Development/mlruns" \
  -v "$art/reports:/app/Model-Development/reports" \
  "$IMAGE" "$@"
