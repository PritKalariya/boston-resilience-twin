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
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import training_data  # noqa: E402

UNITS = ("Alpha", "Beta")
MLFLOW_STORE: Path | None = None  # conftest.py's session store; write_repo uses it unless told otherwise
CATALOG = (
    "feature,group,source,coverage_start,history_rule,live_rule,use_in_model,note\n"
    "unit,key,test,2019-01-01,n/a,n/a,no,row key\n"
    "date,key,test,2019-01-01,n/a,n/a,no,row key\n"
    "day_of_week,calendar,test,2019-01-01,pass,pass,yes,a model input\n"
    "kept_out,test,test,2019-01-01,pass,pass,no,not a model input\n"
)


@pytest.fixture(scope="session", autouse=True)
def shared_mlflow_store(tmp_path_factory):
    """One local MLflow store for the session, which write_repo uses unless a test names its own:
    creating a store runs MLflow's database migrations, a few seconds each time. A test module that
    trains imports this fixture and no_mlflow_server; they live here, not in a conftest.py, because
    Data-Pipeline/tests imports its own conftest by that name in the same pytest session."""
    global MLFLOW_STORE
    MLFLOW_STORE = tmp_path_factory.mktemp("mlruns")
    yield MLFLOW_STORE
    MLFLOW_STORE = None


@pytest.fixture(autouse=True)
def no_mlflow_server(monkeypatch):
    """No test logs to a server named in the caller's environment."""
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)


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
    settings by name (a value of None drops the setting). tuned_params points into the temp repo,
    where no file is until a test writes one, so the committed tuned values never reach a test.
    mlflow_store is the session's shared store, never the repo's. forecast_inputs is empty, since
    the test catalog has no forecast input, unless a test names some."""
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
                    runs_dir=str(tmp_path / "runs"), tuned_params=str(tmp_path / "tuned_params.yaml"),
                    mlflow_store=str(MLFLOW_STORE or tmp_path / "mlruns"), forecast_inputs="")
    settings.update(changes)
    config = tmp_path / "training.csv"
    with open(config, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["setting", "value", "options", "note"])
        for name, value in settings.items():
            if value is not None:
                writer.writerow([name, value, "", ""])
    return config


def forecast_repo(tmp_path: Path, **changes: str) -> Path:
    """write_repo with the forecast input wx_tmin_c in the catalog, missing on 2024-01-01 to 01-03
    (validation days with no forecast) and on 2021-05-05 (observed weather can miss a train day)."""
    labels = make_labels()
    features = make_features(labels)
    features["wx_tmin_c"] = 1.0
    gone = features["date"].between("2024-01-01", "2024-01-03") | (features["date"] == "2021-05-05")
    features.loc[gone, "wx_tmin_c"] = np.nan
    catalog = CATALOG + "wx_tmin_c,weather,test,2019-01-01,pass,pass,yes,a forecast input\n"
    return write_repo(tmp_path, label_frame=labels, feature_frame=features, catalog=catalog,
                      **{"forecast_inputs": "wx_tmin_c", **changes})
