"""The prediction file every model writes (#106, ADR-027): the contract M6, M7, M9 and M10 build on.

One parquet row per model, neighborhood and day, these columns in this order:

| column | type | meaning |
|---|---|---|
| unit | string | the ADR-004 neighborhood |
| date | datetime64, whole days, no time zone | the day forecast (Boston local) |
| model | string | the model name train.py was given, for example seasonal |
| split | string | train, validation or test (ADR-003) |
| probability | float64 from 0 to 1 | the model's surge probability for the day |
| prediction | int8, 0 or 1 | 1 when probability is at or above the run's shared threshold (threshold.py) |
| label | int8, 0 or 1 | the frozen surge label (ADR-027) |

validate_predictions is the one check of the contract. write_predictions and read_predictions always
run it, so a file that breaks the contract is never written or read without an error naming why.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["unit", "date", "model", "split", "probability", "prediction", "label"]
SPLITS = ("train", "validation", "test")
TEXT = ("unit", "model", "split")
FLAGS = ("prediction", "label")


def make_predictions(rows: pd.DataFrame, model: str, probability, threshold: float) -> pd.DataFrame:
    """A prediction frame for rows (unit, date, split and label columns) from one model's probabilities,
    with prediction = probability at or above threshold. It is validated before it is returned."""
    probability = np.asarray(probability, dtype="float64")
    if len(probability) != len(rows):
        raise ValueError(f"{len(probability)} probabilities for {len(rows)} rows")
    out = pd.DataFrame({
        "unit": rows["unit"].astype(str).to_numpy(),
        "date": pd.to_datetime(rows["date"]).to_numpy(),
        "model": model,
        "split": rows["split"].astype(str).to_numpy(),
        "probability": probability,
        "prediction": (probability >= threshold).astype("int8"),
        "label": rows["label"].to_numpy().astype("int8"),
    })
    validate_predictions(out, threshold)
    return out


def validate_predictions(frame: pd.DataFrame, threshold: float | None = None) -> None:
    """Fails, naming the first problem, unless frame meets the contract above. With threshold, also
    checks that every prediction is probability at or above it."""
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"predictions must be a DataFrame, got {type(frame).__name__}")
    if list(frame.columns) != COLUMNS:
        missing = [c for c in COLUMNS if c not in frame.columns]
        extra = [c for c in frame.columns if c not in COLUMNS]
        raise ValueError(f"prediction columns must be {COLUMNS} in this order; missing {missing}, extra {extra}")
    if frame.empty:
        raise ValueError("predictions have no rows")
    for column in COLUMNS:
        if frame[column].isna().any():
            raise ValueError(f"{column} has {int(frame[column].isna().sum())} missing values")
    for column in TEXT:
        if not frame[column].map(lambda v: isinstance(v, str) and v != "").all():
            raise ValueError(f"{column} must be non-empty text")
    if not pd.api.types.is_datetime64_dtype(frame["date"]):
        raise ValueError(f"date must be datetime64 with no time zone, got {frame['date'].dtype}")
    if (frame["date"] != frame["date"].dt.normalize()).any():
        raise ValueError("date must be whole days with no time of day")
    unknown = sorted(set(frame["split"]) - set(SPLITS))
    if unknown:
        raise ValueError(f"split must be one of {list(SPLITS)}, got {unknown}")
    if not pd.api.types.is_float_dtype(frame["probability"]):
        raise ValueError(f"probability must be float64, got {frame['probability'].dtype}")
    outside = ~frame["probability"].between(0.0, 1.0)
    if outside.any():
        raise ValueError(f"{int(outside.sum())} probabilities are outside 0 to 1, "
                         f"for example {frame.loc[outside, 'probability'].iloc[0]}")
    for column in FLAGS:
        if not pd.api.types.is_integer_dtype(frame[column]):
            raise ValueError(f"{column} must be an integer 0 or 1, got {frame[column].dtype}")
        if not frame[column].isin([0, 1]).all():
            raise ValueError(f"{column} must be 0 or 1, got {sorted(set(frame[column]) - {0, 1})}")
    repeated = frame.duplicated(["unit", "date", "model"])
    if repeated.any():
        row = frame[repeated].iloc[0]
        raise ValueError(f"{int(repeated.sum())} rows repeat a model and neighborhood-day, "
                         f"for example {row['model']} {row['unit']} {row['date'].date()}")
    if threshold is not None:
        expected = (frame["probability"] >= threshold).astype("int8")
        if (frame["prediction"] != expected).any():
            raise ValueError(f"prediction must be probability >= {threshold} on every row")


def write_predictions(frame: pd.DataFrame, path: str | Path, threshold: float | None = None) -> Path:
    """Validate frame and write it as parquet; returns the path."""
    validate_predictions(frame, threshold)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def read_predictions(path: str | Path, threshold: float | None = None) -> pd.DataFrame:
    """Read a prediction file and validate it."""
    frame = pd.read_parquet(path)
    validate_predictions(frame, threshold)
    return frame
