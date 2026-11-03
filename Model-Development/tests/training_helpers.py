"""Shared builders for the training interface tests (#106): a small labeled table and a temp repo.

The tests never read the real data or the network: write_repo puts labels, features, a catalog, a
dvc.lock and a training.csv in a temp folder, with the committed settings unless a test changes one.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import training_data  # noqa: E402

UNITS = ("Alpha", "Beta")
CATALOG = (
    "feature,group,source,coverage_start,history_rule,live_rule,use_in_model,note\n"
    "unit,key,test,2019-01-01,n/a,n/a,no,row key\n"
    "date,key,test,2019-01-01,n/a,n/a,no,row key\n"
    "day_of_week,calendar,test,2019-01-01,pass,pass,yes,a model input\n"
    "kept_out,test,test,2019-01-01,pass,pass,no,not a model input\n"
)


def make_labels(first: str = "2019-01-01", last: str = "2026-09-30", units=UNITS, seed: int = 0) -> pd.DataFrame:
    """Labels like surge_labels.py writes: unit, date and a nullable surge, with winter surges and the
    first 6 days of each unit unlabeled (no baseline yet, ADR-005)."""
    days = pd.date_range(first, last)
    rng = np.random.default_rng(seed)
    frames = []
    for unit in units:
        chance = np.where(days.month.isin([12, 1, 2]), 0.25, 0.03)
        surge = pd.array(rng.random(len(days)) < chance, dtype="boolean")
        surge[:6] = pd.NA
        frames.append(pd.DataFrame({"unit": unit, "date": days, "surge": surge, "rule_id": "sd_k2_m5"}))
    return pd.concat(frames, ignore_index=True)


def make_features(labels: pd.DataFrame) -> pd.DataFrame:
    """Features like feature_table.py writes, for the same neighborhood-days: one model input and one kept out."""
    out = labels[["unit", "date"]].copy()
    out["features_last_day"] = out["date"] - pd.Timedelta(days=2)
    out["day_of_week"] = out["date"].dt.dayofweek
    out["kept_out"] = 1.0
    return out


def committed_settings() -> dict[str, str]:
    """The committed training.csv as name -> value."""
    with open(training_data.DEFAULT_CONFIG, encoding="utf-8", newline="") as handle:
        return {row["setting"]: row["value"] for row in csv.DictReader(handle)}


def write_repo(tmp_path: Path, label_frame: pd.DataFrame | None = None, feature_frame: pd.DataFrame | None = None,
               catalog: str = CATALOG, **changes: str) -> Path:
    """A temp repo with the files training.csv names; returns its training.csv. changes replace
    settings by name (a value of None drops the setting)."""
    labels = make_labels() if label_frame is None else label_frame
    features = make_features(labels) if feature_frame is None else feature_frame
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    labels.to_parquet(data / "labels.parquet", index=False)
    features.to_parquet(data / "features.parquet", index=False)
    (data / "feature_catalog.csv").write_text(catalog, encoding="utf-8")
    (data / "dvc.lock").write_text("schema: '2.0'\nstages: {}\n", encoding="utf-8")
    settings = committed_settings()
    settings.update(labels=str(data / "labels.parquet"), features=str(data / "features.parquet"),
                    feature_catalog=str(data / "feature_catalog.csv"), dvc_lock=str(data / "dvc.lock"),
                    runs_dir=str(tmp_path / "runs"))
    settings.update(changes)
    config = tmp_path / "training.csv"
    with open(config, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["setting", "value", "options", "note"])
        for name, value in settings.items():
            if value is not None:
                writer.writerow([name, value, "", ""])
    return config
