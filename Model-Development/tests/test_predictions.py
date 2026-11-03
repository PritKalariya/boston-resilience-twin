"""Tests for the prediction file contract (#106, ADR-027)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers  # noqa: E402,F401  (puts Model-Development/scripts on sys.path)
import predictions as P  # noqa: E402


def good(rows: int = 4) -> pd.DataFrame:
    rows_in = pd.DataFrame({
        "unit": ["Alpha", "Beta"] * (rows // 2),
        "date": pd.to_datetime(["2024-01-01", "2024-01-01", "2025-01-02", "2025-01-02"][:rows]),
        "split": ["validation", "validation", "test", "test"][:rows],
        "label": [1, 0, 0, 1][:rows],
    })
    return P.make_predictions(rows_in, "seasonal", [0.9, 0.1, 0.4, 0.5][:rows], threshold=0.4)


def test_a_good_file_passes_and_round_trips(tmp_path):
    frame = good()
    assert list(frame.columns) == P.COLUMNS
    assert frame["prediction"].tolist() == [1, 0, 1, 1]  # probability >= 0.4
    assert frame["prediction"].dtype == "int8" and frame["label"].dtype == "int8"
    path = P.write_predictions(frame, tmp_path / "run" / "predictions.parquet", threshold=0.4)
    back = P.read_predictions(path, threshold=0.4)
    pd.testing.assert_frame_equal(back.reset_index(drop=True), frame, check_dtype=False)


def _break(kind: str) -> pd.DataFrame:
    frame = good()
    if kind == "missing column":
        return frame.drop(columns="label")
    if kind == "extra column":
        return frame.assign(extra=1)
    if kind == "columns out of order":
        return frame[["date", "unit", *P.COLUMNS[2:]]]
    if kind == "no rows":
        return frame.head(0)
    if kind == "missing unit":
        frame["unit"] = frame["unit"].astype(object)
        frame.loc[0, "unit"] = None
    elif kind == "empty model":
        frame.loc[0, "model"] = ""
    elif kind == "date with a time":
        frame.loc[0, "date"] = frame.loc[0, "date"] + pd.Timedelta(hours=1)
    elif kind == "date with a time zone":
        frame["date"] = frame["date"].dt.tz_localize("UTC")
    elif kind == "date as text":
        frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    elif kind == "unknown split":
        frame.loc[0, "split"] = "live"
    elif kind == "probability above 1":
        frame.loc[0, "probability"] = 1.5
    elif kind == "probability below 0":
        frame.loc[0, "probability"] = -0.1
    elif kind == "missing probability":
        frame.loc[0, "probability"] = np.nan
    elif kind == "probability as text":
        frame["probability"] = frame["probability"].astype(str)
    elif kind == "prediction not 0 or 1":
        frame["prediction"] = frame["prediction"].astype("int64")
        frame.loc[0, "prediction"] = 2
    elif kind == "label as float":
        frame["label"] = frame["label"].astype("float64")
    elif kind == "repeated day":
        frame.loc[1, ["unit", "date"]] = frame.loc[0, ["unit", "date"]].to_numpy()
    return frame


@pytest.mark.parametrize("kind, message", [
    ("missing column", r"missing \['label'\]"),
    ("extra column", r"extra \['extra'\]"),
    ("columns out of order", "in this order"),
    ("no rows", "no rows"),
    ("missing unit", "unit has 1 missing"),
    ("empty model", "model must be non-empty text"),
    ("date with a time", "whole days"),
    ("date with a time zone", "no time zone"),
    ("date as text", "datetime64"),
    ("unknown split", r"split must be one of"),
    ("probability above 1", "outside 0 to 1"),
    ("probability below 0", "outside 0 to 1"),
    ("missing probability", "probability has 1 missing"),
    ("probability as text", "probability must be float64"),
    ("prediction not 0 or 1", "prediction must be 0 or 1"),
    ("label as float", "label must be an integer"),
    ("repeated day", "repeat a model and neighborhood-day"),
])
def test_a_broken_file_is_refused_by_name(kind, message):
    with pytest.raises(ValueError, match=message):
        P.validate_predictions(_break(kind))


def test_predictions_must_follow_the_threshold():
    frame = good()
    frame.loc[1, "prediction"] = 1  # probability 0.1 is below 0.4
    with pytest.raises(ValueError, match="prediction must be probability >= 0.4"):
        P.validate_predictions(frame, threshold=0.4)


def test_a_broken_file_on_disk_is_refused_when_read(tmp_path):
    path = tmp_path / "predictions.parquet"
    _break("probability above 1").to_parquet(path, index=False)
    with pytest.raises(ValueError, match="outside 0 to 1"):
        P.read_predictions(path)


def test_make_predictions_needs_one_probability_per_row():
    rows = good()[["unit", "date", "split", "label"]]
    with pytest.raises(ValueError, match="3 probabilities for 4 rows"):
        P.make_predictions(rows, "seasonal", [0.1, 0.2, 0.3], threshold=0.5)


def test_a_non_frame_is_refused():
    with pytest.raises(ValueError, match="must be a DataFrame"):
        P.validate_predictions([1, 2, 3])
