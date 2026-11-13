"""Step: train the baseline and every candidate (#112, ADR-028, ADR-032).

Runs #128's `train.py` (its main, so the output and the exit code are the command line's)
for the baseline named by gates.csv (`f1_baseline`, seasonal) and for each model in
`candidates` of training.csv, with fixed run ids `ci-<model>`, all on the data version of
the mounted files. Each run is written to `runs_dir/ci-<model>/` (the workflow mounts it
from `artifacts/runs`) and logged to MLflow's local store (`MLFLOW_TRACKING_URI` stays
empty, so `artifacts/mlruns` holds it). Any run that fails, or leaves no run folder, exits 2.

Reads: `data_version.txt` (the pull step's), the mounted data, training.csv.
Writes: `runs/ci-<model>/` (predictions.parquet, metrics.json, validation_counts.json,
run.json, and model.joblib for a candidate), `mlruns/`.
"""

from __future__ import annotations

import sys

import step_common as C


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(__doc__.splitlines()[0], argv, with_config=True)
    import gates
    import train

    C.read_data_version(artifacts)  # the pull step ran
    settings = C.load_settings(config)
    try:
        baseline = gates.load_gates().f1_baseline
    except (OSError, ValueError) as exc:
        raise C.StepError(f"cannot load the gates: {exc}") from exc
    config_args = ["--config", str(config)] if config else []
    for model in (baseline, *settings.candidates):
        print(f"== train {model} as {C.run_id(model)}", flush=True)
        code = train.main(["--model", model, "--run-id", C.run_id(model), *config_args])
        if code != 0:
            raise C.StepError(f"train.py --model {model} exited {code}")
        if not (settings.runs_dir / C.run_id(model) / "run.json").is_file():
            raise C.StepError(f"train.py wrote no run folder {settings.runs_dir / C.run_id(model)}")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
