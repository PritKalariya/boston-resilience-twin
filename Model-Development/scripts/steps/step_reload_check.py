"""Step: reload the saved winner and check it scores like the model that was trained (#112, ADR-028, ADR-032).

In a fresh process (this step is its own `docker run`), loads the winner's `model.joblib`, scores
the run's scored validation rows from the mounted data and compares with the probabilities training
wrote to `predictions.parquet`, at an absolute tolerance of 1e-9 on `probability` and exactly on
`prediction`. A model file that cannot be loaded or scored, a missing row, or any difference exits 2.
It has no side effects, so it runs on every ref: a broken artifact fails before it can be merged.

The predictions file has a probability column, so the check is on probabilities, not only labels.

Reads: `evaluation.json` (`winner`, `winner_run`), `runs_dir/<winner_run>/model.joblib`,
`runs_dir/<winner_run>/predictions.parquet` (validation rows) and `run.json` (`inputs`), the mounted data.
Writes: `reload_check.json` (rows, tolerance, largest difference).
"""

from __future__ import annotations

import sys

import step_common as C

TOLERANCE = 1e-9


def step(argv: list[str] | None) -> None:
    artifacts, config = C.parse_artifacts(__doc__.splitlines()[0], argv, with_config=True)
    import joblib
    import numpy as np
    import pandas as pd
    import predictions
    import training_data

    settings = C.load_settings(config)
    evaluation = C.read_object(artifacts / "evaluation.json")
    folder = settings.runs_dir / str(evaluation.get("winner_run"))
    inputs = C.read_object(folder / "run.json").get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise C.StepError(f"{folder / 'run.json'} lists no model inputs")

    try:
        table, _ = training_data.load_table(settings)
        rows = table[(table["split"] == "validation") & table["scored"]].reset_index(drop=True)
        trained = predictions.read_predictions(folder / "predictions.parquet")  # the contract checks
        trained = trained[trained["split"] == "validation"]
        model = joblib.load(folder / "model.joblib")
        probability = np.asarray(model.predict_proba(rows[inputs]), dtype="float64")[:, 1]
        flag = np.asarray(model.predict(rows[inputs]), dtype="int64")
    except Exception as exc:  # a corrupt or missing model file or data: the artifact is unusable
        raise C.StepError(f"cannot reload and score {folder / 'model.joblib'}: {type(exc).__name__}: {exc}") from exc

    reloaded = pd.DataFrame({"unit": rows["unit"].astype(str), "date": pd.to_datetime(rows["date"]),
                             "reloaded": probability, "reloaded_flag": flag})
    joined = trained.merge(reloaded, on=["unit", "date"], how="outer", indicator=True)
    if (joined["_merge"] != "both").any():
        raise C.StepError(f"the reloaded model's validation rows differ from training's: "
                          f"{int((joined['_merge'] != 'both').sum())} neighborhood-days are in only one")
    difference = float((joined["probability"] - joined["reloaded"]).abs().max())
    if not difference <= TOLERANCE:  # also catches a NaN
        raise C.StepError(f"reload mismatch: the largest probability difference is {difference!r} "
                          f"(tolerance {TOLERANCE}) over {len(joined)} validation rows")
    if (joined["prediction"].astype("int64") != joined["reloaded_flag"]).any():
        raise C.StepError("reload mismatch: the reloaded model flags different validation rows than training did")
    C.write_json(artifacts / "reload_check.json", {"model": evaluation["winner"], "rows": len(joined),
                                                   "tolerance": TOLERANCE, "max_abs_difference": difference,
                                                   "compared": "probability and prediction"})
    print(f"reload check ok: {evaluation['winner']}, {len(joined)} validation rows, largest probability "
          f"difference {difference:.3g} (tolerance {TOLERANCE})")


def main(argv: list[str] | None = None) -> int:
    return C.run(step, argv)


if __name__ == "__main__":
    sys.exit(main())
