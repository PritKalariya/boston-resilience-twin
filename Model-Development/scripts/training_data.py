"""Training data for every model (#106, ADR-027): the frozen labels joined to the model inputs, with splits.

Reads labels.parquet (surge_labels.py) and features.parquet (feature_table.py), the files
Data-Pipeline/dvc.lock names, and keeps the inputs Data-Pipeline/config/feature_catalog.csv marks
use_in_model = yes. Every labeled neighborhood-day gets exactly one split by its date (ADR-003):
train, validation or test. Days in none of them, the live days after the test window, are dropped
and counted; so are unlabeled days (ADR-005: no label without 20 history days). Train rows in the
down-weighted years (2020 and 2021, ADR-003) get a smaller sample weight; every other row weighs 1.

Validation and test take their weather from the day-before forecast (ADR-003). A day of theirs with
no forecast, one missing any of forecast_inputs, cannot be forecast live either, so its rows are not
scored (scored = False): no model is scored on them and no threshold is picked on them. They stay in
the table, because the seasonal baseline still reads their labels as history, and they are counted.

Every setting comes from Model-Development/config/training.csv (load_settings), never from code;
the candidates' parameters come from the model_params.yaml it names, with M6's tuned_params.yaml
over them when that file exists (model_params.py, #107).
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

# Model-Development/scripts/training_data.py -> parents[2] is the repo root.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "Model-Development" / "config" / "training.csv"

SPLITS = ("train", "validation", "test")
FORECAST_SPLITS = ("validation", "test")  # the splits whose weather is the day-before forecast (ADR-003)
KEYS = ["unit", "date"]
ROW_COLUMNS = (*KEYS, "split", "label", "weight", "scored")  # every table column that is not a model input
THRESHOLD_RULES = ("validation_f1_max",)
SETTINGS_HEADER = ["setting", "value"]
PATH_SETTINGS = ("labels", "features", "feature_catalog", "dvc_lock", "runs_dir", "model_params", "tuned_params",
                 "mlflow_store", "reports_dir", "slices")
DATE_SETTINGS = tuple(f"{split}_{end}" for split in SPLITS for end in ("start", "end"))
SETTING_NAMES = (*PATH_SETTINGS, *DATE_SETTINGS, "down_weight_years", "down_weight", "inner_fold_years",
                 "forecast_inputs", "missing_gap_max_points", "threshold_rule", "seasonal_years",
                 "seasonal_half_width_days", "seasonal_min_days", "mlflow_experiment", "candidates", "bootstrap_resamples", "bootstrap_seed")


class SettingsError(ValueError):
    """A training.csv value that cannot be loaded."""


@dataclass(frozen=True)
class Settings:
    labels: Path
    features: Path
    feature_catalog: Path
    dvc_lock: Path
    runs_dir: Path
    model_params: Path  # the candidates' starting parameters (model_params.py)
    tuned_params: Path  # M6's tuned values over them, used only when the file exists (model_params.py)
    mlflow_store: Path  # mlflow_runs.py: the local MLflow store when MLFLOW_TRACKING_URI is unset
    splits: dict[str, tuple[pd.Timestamp, pd.Timestamp]]  # split -> (first day, last day), both inclusive
    down_weight_years: tuple[int, ...]
    down_weight: float
    inner_fold_years: tuple[int, ...]  # inner_folds.py: the train years a candidate picks its own settings on
    forecast_inputs: tuple[str, ...]  # load_table: a validation or test row missing one of these is not scored
    missing_gap_max_points: float  # check_missing_gaps: the largest train to validation or test gap allowed
    threshold_rule: str
    seasonal_years: int
    seasonal_half_width_days: int
    seasonal_min_days: int
    mlflow_experiment: str  # mlflow_runs.py: the MLflow experiment every run is logged to
    reports_dir: Path  # selection.py: where the selection report and its charts go
    candidates: tuple[str, ...]  # selection.py: the models it trains and ranks against the baseline
    slices: Path  # slices.py: the kinds of test day the selected model is also scored on
    bootstrap_resamples: int  # slices.py: draws of whole days for the F1 gain's interval
    bootstrap_seed: int


def _read_pairs(path: Path) -> dict[str, str]:
    """The setting,value pairs of a processing.csv-style file; fails on a bad header or a duplicate."""
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    if not rows or [c.strip() for c in rows[0][:2]] != SETTINGS_HEADER:
        raise SettingsError(f"{path.name} needs a header starting with {','.join(SETTINGS_HEADER)}")
    pairs: dict[str, str] = {}
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        if len(row) < 2:
            raise SettingsError(f"setting {name!r} has no value in {path.name}")
        if name in pairs:
            raise SettingsError(f"setting {name!r} is duplicated in {path.name}")
        pairs[name] = row[1].strip()
    return pairs


def _day(name: str, value: str) -> pd.Timestamp:
    try:
        return pd.Timestamp(date.fromisoformat(value))
    except ValueError:
        raise SettingsError(f"{name} must be a date as YYYY-MM-DD, got {value!r}") from None


def _whole(name: str, value: str, minimum: int) -> int:
    try:
        number = int(value)
    except ValueError:
        number = minimum - 1
    if number < minimum:
        raise SettingsError(f"{name} must be a whole number of {minimum} or more, got {value!r}")
    return number


def _train_years(name: str, value: str, train: tuple[pd.Timestamp, pd.Timestamp]) -> tuple[int, ...]:
    """Whole years separated by ;, each inside the train window; fails naming the setting."""
    years = []
    for part in value.split(";"):
        if not part.strip():
            continue
        year = _whole(name, part.strip(), 1)
        if not train[0].year <= year <= train[1].year:
            raise SettingsError(f"{name}: {year} is outside the train window")
        years.append(year)
    return tuple(years)


def load_settings(path: str | Path = DEFAULT_CONFIG, root: str | Path = ROOT) -> Settings:
    """Load and check training.csv. Relative paths are read from the repo root (root).

    Fails, naming the setting, on a missing, unknown or duplicate setting, a bad value, split windows
    out of order or overlapping, a down-weighted year outside the train window, or inner fold years
    that are missing, out of order, outside the train window, down-weighted or with no train day before.
    """
    path, root = Path(path), Path(root)
    pairs = _read_pairs(path)
    unknown = sorted(set(pairs) - set(SETTING_NAMES))
    if unknown:
        raise SettingsError(f"unknown setting(s) in {path.name}: {unknown}")
    missing = [name for name in SETTING_NAMES if name not in pairs]
    if missing:
        raise SettingsError(f"missing setting(s) in {path.name}: {missing}")

    paths = {}
    for name in PATH_SETTINGS:
        if not pairs[name]:
            raise SettingsError(f"{name} needs a path")
        paths[name] = Path(pairs[name]) if Path(pairs[name]).is_absolute() else root / pairs[name]

    splits = {}
    for split in SPLITS:
        first, last = _day(f"{split}_start", pairs[f"{split}_start"]), _day(f"{split}_end", pairs[f"{split}_end"])
        if last < first:
            raise SettingsError(f"{split}_end {last.date()} is before {split}_start {first.date()}")
        splits[split] = (first, last)
    for before, after in zip(SPLITS, SPLITS[1:]):
        if splits[after][0] <= splits[before][1]:
            raise SettingsError(f"{after}_start {splits[after][0].date()} must come after {before}_end "
                                f"{splits[before][1].date()}: a day belongs to one split only (ADR-003)")

    years = _train_years("down_weight_years", pairs["down_weight_years"], splits["train"])
    try:
        weight = float(pairs["down_weight"])
    except ValueError:
        weight = -1.0
    if not 0 < weight <= 1:
        raise SettingsError(f"down_weight must be above 0 and at most 1, got {pairs['down_weight']!r}")

    fold_years = _train_years("inner_fold_years", pairs["inner_fold_years"], splits["train"])
    if not fold_years:
        raise SettingsError("inner_fold_years needs at least one year")
    if list(fold_years) != sorted(set(fold_years)):
        raise SettingsError(f"inner_fold_years must increase with no year twice, got {pairs['inner_fold_years']!r}")
    if fold_years[0] <= splits["train"][0].year:
        raise SettingsError(f"inner_fold_years: {fold_years[0]} has no train day before it to fit on")
    down = sorted(set(fold_years) & set(years))
    if down:
        raise SettingsError(f"inner_fold_years: {down[0]} is down-weighted; "
                            "such a year is only fit on, never checked on")
    forecast = tuple(name.strip() for name in pairs["forecast_inputs"].split(";") if name.strip())
    if len(set(forecast)) != len(forecast):
        raise SettingsError(f"forecast_inputs names an input twice: {pairs['forecast_inputs']!r}")
    try:
        max_gap = float(pairs["missing_gap_max_points"])
    except ValueError:
        max_gap = -1.0
    if not 0 <= max_gap <= 100:
        raise SettingsError(f"missing_gap_max_points must be a number from 0 to 100, "
                            f"got {pairs['missing_gap_max_points']!r}")

    rule = pairs["threshold_rule"]
    if rule not in THRESHOLD_RULES:
        raise SettingsError(f"threshold_rule must be one of {list(THRESHOLD_RULES)}, got {rule!r}")
    seasonal_years = _whole("seasonal_years", pairs["seasonal_years"], 1)
    half_width = _whole("seasonal_half_width_days", pairs["seasonal_half_width_days"], 0)
    min_days = _whole("seasonal_min_days", pairs["seasonal_min_days"], 1)
    if min_days > seasonal_years * (2 * half_width + 1):
        raise SettingsError(f"seasonal_min_days {min_days} is more than the window's "
                            f"{seasonal_years * (2 * half_width + 1)} days")

    experiment = pairs["mlflow_experiment"]
    if not experiment:
        raise SettingsError("mlflow_experiment needs a name")
    candidates = tuple(name.strip() for name in pairs["candidates"].split(";") if name.strip())
    if not candidates:
        raise SettingsError("candidates needs at least one model name")
    if len(set(candidates)) != len(candidates):
        raise SettingsError(f"candidates names a model twice: {pairs['candidates']!r}")
    resamples = _whole("bootstrap_resamples", pairs["bootstrap_resamples"], 100)
    seed = _whole("bootstrap_seed", pairs["bootstrap_seed"], 0)

    return Settings(splits=splits, down_weight_years=years, down_weight=weight, inner_fold_years=fold_years,
                    forecast_inputs=forecast, missing_gap_max_points=max_gap, threshold_rule=rule,
                    seasonal_years=seasonal_years, seasonal_half_width_days=half_width, seasonal_min_days=min_days, mlflow_experiment=experiment,
                    candidates=candidates, bootstrap_resamples=resamples, bootstrap_seed=seed, **paths)


def split_of(dates: pd.Series, splits: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> pd.Series:
    """The split of each date: train, validation, test, or "" for a day in none of them."""
    days = pd.to_datetime(dates)
    out = pd.Series("", index=dates.index, dtype=object)
    for split, (first, last) in splits.items():
        out[(days >= first) & (days <= last)] = split
    return out


def check_splits(table: pd.DataFrame, splits: dict[str, tuple[pd.Timestamp, pd.Timestamp]]) -> None:
    """Fails unless every row has one known split that holds its date and no neighborhood-day repeats,
    so no row can cross splits."""
    unknown = sorted(set(table["split"]) - set(SPLITS))
    if unknown:
        raise ValueError(f"rows with an unknown split: {unknown}")
    expected = split_of(table["date"], splits)
    wrong = table["split"].to_numpy() != expected.to_numpy()
    if wrong.any():
        row = table[wrong].iloc[0]
        raise ValueError(f"{int(wrong.sum())} rows are in the wrong split, for example {row['unit']} "
                         f"{pd.Timestamp(row['date']).date()} in {row['split']!r}, which belongs to {expected[wrong].iloc[0]!r}")
    repeated = table.duplicated(KEYS)
    if repeated.any():
        row = table[repeated].iloc[0]
        raise ValueError(f"{int(repeated.sum())} neighborhood-days appear twice, for example {row['unit']} "
                         f"{pd.Timestamp(row['date']).date()}: a day can be in one split only")


def model_inputs(catalog: str | Path) -> list[str]:
    """The model inputs: the catalog's features with use_in_model = yes, in catalog order."""
    table = pd.read_csv(catalog, dtype=str, keep_default_na=False)
    missing = {"feature", "use_in_model"} - set(table.columns)
    if missing:
        raise ValueError(f"{Path(catalog).name} is missing columns: {sorted(missing)}")
    inputs = table.loc[table["use_in_model"].str.strip() == "yes", "feature"].str.strip().tolist()
    if not inputs:
        raise ValueError(f"{Path(catalog).name} marks no feature use_in_model = yes")
    return inputs


def data_version(dvc_lock: str | Path) -> str:
    """The data version of a run: the sha256 of dvc.lock with its line endings as LF, so a Windows
    checkout (core.autocrlf) and a Linux one give the same version for the same lock."""
    text = Path(dvc_lock).read_bytes().replace(b"\r\n", b"\n")
    return "sha256:" + hashlib.sha256(text).hexdigest()


def _keyed(frame: pd.DataFrame, columns: list[str], name: str) -> pd.DataFrame:
    """frame's columns with a checked unit and day key; fails naming the problem."""
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"{name} is missing columns: {missing}")
    if frame.empty:
        raise ValueError(f"{name} has no rows")
    out = frame[columns].copy()
    days = pd.to_datetime(out["date"], errors="coerce")
    if days.isna().any():
        raise ValueError(f"{name}: {int(days.isna().sum())} rows have a date that does not parse, "
                         f"for example {out.loc[days.isna(), 'date'].iloc[0]!r}")
    if (days != days.dt.normalize()).any():
        raise ValueError(f"{name}: dates must be whole days with no time of day")
    out["date"] = days
    if out["unit"].isna().any():
        raise ValueError(f"{name}: {int(out['unit'].isna().sum())} rows have no unit")
    repeated = out.duplicated(KEYS)
    if repeated.any():
        row = out[repeated].iloc[0]
        raise ValueError(f"{name}: {int(repeated.sum())} rows repeat a neighborhood-day, "
                         f"for example {row['unit']} {row['date'].date()}")
    return out


def load_table(settings: Settings) -> tuple[pd.DataFrame, dict]:
    """The labeled rows of every split, with columns unit, date, split, label (0 or 1), weight, scored
    and the model inputs, sorted by date and unit; and counts of what was read, dropped and not scored."""
    inputs = model_inputs(settings.feature_catalog)
    strangers = [name for name in settings.forecast_inputs if name not in inputs]
    if strangers:
        raise ValueError(f"forecast_inputs names {strangers}, not model inputs in {settings.feature_catalog.name}")
    labels = _keyed(pd.read_parquet(settings.labels), [*KEYS, "surge"], settings.labels.name)
    features = _keyed(pd.read_parquet(settings.features), [*KEYS, *inputs], settings.features.name)

    joined = labels.merge(features, on=KEYS, how="outer", indicator=True)
    lonely = joined["_merge"] != "both"
    if lonely.any():
        row = joined[lonely].iloc[0]
        side = "labels" if row["_merge"] == "left_only" else "features"
        raise ValueError(f"labels and features disagree on {int(lonely.sum())} neighborhood-days, for example "
                         f"{row['unit']} {row['date'].date()}, which only the {side} have")
    joined = joined.drop(columns="_merge")

    labeled = joined["surge"].notna()
    joined = joined[labeled].copy()
    joined["split"] = split_of(joined["date"], settings.splits)
    in_split = joined["split"] != ""
    counts = {"rows read": len(labels), "unlabeled dropped": int((~labeled).sum()),
              "outside every split dropped": int((~in_split).sum())}
    table = joined[in_split].copy()
    if table.empty:
        raise ValueError("no labeled row falls in any split")

    table["label"] = table["surge"].astype(bool).astype("int8")
    weight = pd.Series(1.0, index=table.index)
    down = (table["split"] == "train") & table["date"].dt.year.isin(settings.down_weight_years)
    weight[down] = settings.down_weight
    table["weight"] = weight
    no_forecast = table[list(settings.forecast_inputs)].isna().any(axis=1)
    table["scored"] = ~(table["split"].isin(FORECAST_SPLITS) & no_forecast)
    table = table[[*ROW_COLUMNS, *inputs]].sort_values(["date", "unit"], ignore_index=True)
    check_splits(table, settings.splits)
    for split in SPLITS:
        counts[f"{split} rows"] = int((table["split"] == split).sum())
        counts[f"{split} surges"] = int(table.loc[table["split"] == split, "label"].sum())
    counts["down-weighted train rows"] = int(down.sum())
    for split in FORECAST_SPLITS:
        days = table.loc[(table["split"] == split) & ~table["scored"], "date"]
        counts[f"{split} rows not scored"] = len(days)
        counts[f"{split} days not scored"] = int(days.nunique())
        counts[f"{split} days not scored, first and last"] = (
            [days.min().date().isoformat(), days.max().date().isoformat()] if len(days) else None)
    return table, counts


def unscored_note(counts: dict, split: str) -> str | None:
    """One line on the days of split left unscored for want of a forecast, or None when there are none."""
    days = counts.get(f"{split} days not scored", 0)
    if not days:
        return None
    first, last = counts[f"{split} days not scored, first and last"]
    return (f"{split}: {days} days not scored ({counts[f'{split} rows not scored']} rows, no day-before forecast), "
            f"{first} to {last}")


def input_columns(table: pd.DataFrame) -> list[str]:
    """The model inputs of a load_table table: every column but the row columns, in table order."""
    return [column for column in table.columns if column not in ROW_COLUMNS]


def check_missing_gaps(table: pd.DataFrame, inputs: list[str], max_points: float) -> None:
    """Checks the inputs of the scored rows across the splits, since a model fills a missing value
    from what it learned on train. Fails when:

    - an input never missing in train is missing on a scored validation or test row: a model has
      learned nothing for that case (LightGBM reads it as 0, XGBoost takes a branch no row trained,
      the logistic regression's median comes with no flag), so the score there is made up. The 20
      days of January 2024 with no GFS forecast were such a case until forecast_inputs left them
      unscored (#107 review);
    - an input's missing share in validation or test is more than max_points percentage points
      (missing_gap_max_points) from its share in train, like the 2024 GFS snowfall gap (341 of 366
      days, fixed in #107)."""
    table = table[table["scored"]]
    share = table[inputs].isna().groupby(table["split"]).mean() * 100
    if "train" not in share.index:
        return
    complete = [name for name in inputs if share.loc["train", name] == 0]
    for split in SPLITS[1:]:
        if split not in share.index:
            continue
        holes = [name for name in complete if share.loc[split, name] > 0]
        if holes:
            rows = table[table["split"] == split]
            days = rows.loc[rows[holes[0]].isna(), "date"]
            raise ValueError(f"{len(holes)} input(s) never missing in train are missing on scored {split} rows, "
                             f"first {holes[0]}: on {days.nunique()} days from {days.min().date()} to "
                             f"{days.max().date()}; fill the gap, or name the input in forecast_inputs if those "
                             "days have no forecast")
        gap = (share.loc[split] - share.loc["train"]).abs()
        over = gap[gap > max_points].sort_values(ascending=False)
        if len(over):
            worst = over.index[0]
            raise ValueError(f"{len(over)} input(s) differ in missing share between train and {split} by more "
                             f"than missing_gap_max_points ({max_points:g}), worst {worst}: missing on "
                             f"{share.loc[split, worst]:.1f}% of {split} rows against "
                             f"{share.loc['train', worst]:.1f}% of train rows")
