"""Tomorrow's feature rows for the evening forecast (#160).

    python Data-Pipeline/scripts/forecast_rows.py --as-of 2026-11-24

On the evening of the as-of date A the forecast is for D = A + 1. The rows are
built by feature_table.build_features, the code that builds the history, with
its leakage checks (check_no_leakage): 311 features up to D - min_lag, alerts
first sent before the cutoff, the GFS day-before values the availability rule
allows at the cutoff, and D's predicted tides. One row per unit, sorted by unit,
written to Data-Pipeline/data/forecast/<A>/features.parquet for the scoring step
(D8) as the /predict request rows of docs/contracts/predict.md ("The forecast rows
file"): unit, forecast_date, as_of and the catalog's use_in_model = yes inputs in
catalog order, nothing else, so the evening run sends the file as it is.

The daily counts reach D - min_lag at most, so D and the days before it have no
counts. build_features needs a row for D, so the counts are extended in memory
with zero-filled days after their last one; those days are never read, since
every 311 feature stops at D - min_lag, and they never reach daily_counts,
labels or features: only --out is written. The run stops when:
- the counts end before D - min_lag (a 311 export behind), since the zero days
  would then stand in for real ones;
- the GFS year files holding D's hours were fetched before the cutoff: the
  availability rule takes a run's values as known at the cutoff, and a fetch
  before it can hold an older run's values for those hours;
- a weather or tide feature of D is missing, which the history never has after
  2024-01-21.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import open_meteo_forecast as om
from alert_history import cutoff_utc
from feature_table import build_features, static_attributes
from surge_labels import load_settings
from tide_features import TIDE_FEATURES, tide_features
from weather_features import WEATHER_FEATURES, weather_features

MANIFEST = "_manifest.json"
KEYS = ["unit", "forecast_date", "as_of"]
# predict.md: these inputs are int64 in the forecast rows file, every other input float64
INTEGER_INPUTS = {"day_of_week", "month", "day_of_year", "alert_snow_ice_count", "alert_snow_ice_warning",
                  "alert_tree_count", "alert_tree_warning", "alert_flooding_count", "alert_flooding_warning"}


def model_inputs(catalog: Path | str) -> list[str]:
    """The catalog's use_in_model = yes features, in catalog order: the request's inputs."""
    rows = pd.read_csv(catalog, dtype=str, keep_default_na=False)
    inputs = list(rows.loc[rows["use_in_model"].str.strip() == "yes", "feature"])
    if not inputs:
        raise ValueError(f"{catalog} has no use_in_model = yes feature")
    return inputs


def request_rows(table: pd.DataFrame, inputs: list[str], as_of: date) -> pa.Table:
    """build_features' rows of D as the forecast rows file: the request's fields and types, sorted by unit."""
    missing = [name for name in inputs if name not in table]
    if missing:
        raise ValueError(f"the feature table has no {', '.join(missing)}, which the catalog marks use_in_model = yes")
    rows = table.sort_values("unit", ignore_index=True)
    fields = [("unit", pa.string()), ("forecast_date", pa.date32()), ("as_of", pa.date32())]
    fields += [(name, pa.int64() if name in INTEGER_INPUTS else pa.float64()) for name in inputs]
    columns = {"unit": rows["unit"].astype(str).tolist(), "forecast_date": [as_of + timedelta(days=1)] * len(rows),
               "as_of": [as_of] * len(rows)}
    columns.update({name: rows[name].tolist() for name in inputs})
    return pa.table(columns, schema=pa.schema(fields))


def extend_counts(counts: pd.DataFrame, day: pd.Timestamp, min_lag: int) -> pd.DataFrame:
    """The counts with zero-filled days from their last day through day, for every unit and storm group."""
    dates = pd.to_datetime(counts["date"])
    last, needed = dates.max(), day - pd.Timedelta(days=min_lag)
    if last < needed:
        raise ValueError(f"daily counts end on {last.date()}, but the features of {day.date()} read 311 days up to "
                         f"{needed.date()}; rerun the 311 downloads and daily_counts (an export is behind)")
    added = pd.date_range(last + pd.Timedelta(days=1), day)
    if added.empty:
        return counts
    cells = counts.loc[dates == last, ["unit", "storm_group"]].drop_duplicates()
    zeros = cells.merge(pd.DataFrame({"date": added.astype(counts["date"].dtype)}), how="cross")
    for column in counts.columns.difference(["unit", "storm_group", "date"]):
        zeros[column] = 0
    return pd.concat([counts, zeros[counts.columns].astype(counts.dtypes.to_dict())], ignore_index=True)


def check_fetched(state_dir: Path, day: pd.Timestamp, cutoff: pd.Timestamp) -> None:
    """The GFS year files with day's hours were fetched at or after the cutoff (module docstring)."""
    path = Path(state_dir) / MANIFEST
    if not path.is_file():
        raise ValueError(f"{path} not found; run ingest_open_meteo in the evening run")
    files = json.loads(path.read_text())["files"]
    for year in om.years_for([day]):
        entry = files.get(f"{year}.json")
        fetched = entry and pd.Timestamp(entry.get("fetched_at") or entry["downloaded_at"])
        if not entry or fetched < cutoff:
            raise ValueError(f"{om.SET}/{year}.json was fetched {fetched or 'never'}, before the cutoff {cutoff}; "
                             "run ingest_open_meteo after the cutoff")


def forecast_rows(counts, alerts, static, settings: dict, noaa_dir, forecast_dir, forecast_state, tides_dir,
                  as_of: date) -> pd.DataFrame:
    """build_features' row of every unit for the day after as_of, all its columns, checked (module docstring)."""
    day = pd.Timestamp(as_of + timedelta(days=1))
    cutoff_local, min_lag = settings["forecast_cutoff_local"], settings["feature_min_lag_days"]
    check_fetched(forecast_state, day, cutoff_utc(pd.DatetimeIndex([day]), cutoff_local)[0])
    weather = weather_features([day], cutoff_local, noaa_dir, forecast_dir, settings["observed_weather_lag_days"],
                               settings["forecast_issue_margin_hours"], as_of)
    tides = tide_features([day], tides_dir, as_of)
    table = build_features(extend_counts(counts, day, min_lag), settings["feature_groups"], min_lag, alerts=alerts,
                           cutoff_local=cutoff_local, static=static, start=day, end=day, weather=weather, tides=tides)
    missing = [name for name in (*WEATHER_FEATURES, *TIDE_FEATURES) if table[name].isna().any()]
    if missing:
        raise ValueError(f"{day.date()} has no {', '.join(missing)}; check the evening's Open-Meteo and tide downloads")
    units = counts["unit"].nunique()
    if len(table) != units:
        raise AssertionError(f"{len(table)} rows for {units} units")
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--counts", default="Data-Pipeline/data/processed/daily_counts.parquet")
    parser.add_argument("--alerts", default="Data-Pipeline/data/interim/nws_alerts.parquet")
    parser.add_argument("--static-dir", default="Data-Pipeline/data/interim/static")
    parser.add_argument("--settings", default="Data-Pipeline/config/labels_features.csv")
    parser.add_argument("--noaa-dir", default="Data-Pipeline/data/raw/weather/noaa_logan")
    parser.add_argument("--forecast-dir", default=f"Data-Pipeline/data/raw/open_meteo/{om.SET}")
    parser.add_argument("--forecast-state", default=f"Data-Pipeline/data/state/open_meteo/{om.SET}",
                        help="the GFS set's state folder, for when its year files were fetched")
    parser.add_argument("--tides-dir", default="Data-Pipeline/data/raw/tides/predicted")
    parser.add_argument("--catalog", default="Data-Pipeline/config/feature_catalog.csv",
                        help="the feature catalog: its use_in_model = yes features are the request's inputs")
    parser.add_argument("--as-of", required=True, type=date.fromisoformat, help="the evening, YYYY-MM-DD in Boston")
    parser.add_argument("--out", help="default: Data-Pipeline/data/forecast/<as-of>/features.parquet")
    args = parser.parse_args()

    out = Path(args.out or f"Data-Pipeline/data/forecast/{args.as_of}/features.parquet")
    inputs = model_inputs(args.catalog)
    table = forecast_rows(pd.read_parquet(args.counts), pd.read_parquet(args.alerts), static_attributes(args.static_dir),
                          load_settings(args.settings), args.noaa_dir, args.forecast_dir, args.forecast_state,
                          args.tides_dir, args.as_of)
    rows = request_rows(table, inputs, args.as_of)
    out.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(rows, out)
    print(f"{rows.num_rows} rows for {table['date'].iloc[0].date()} (as of {args.as_of}), {len(inputs)} inputs, 311 days "
          f"up to {table['features_last_day'].iloc[0].date()}, cutoff {table['forecast_cutoff_utc'].iloc[0]}; wrote {out}")


if __name__ == "__main__":
    main()
