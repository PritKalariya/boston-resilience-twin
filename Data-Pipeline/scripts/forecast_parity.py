"""Parity of the evening GFS forecasts with the archive (#160).

    python Data-Pipeline/scripts/forecast_parity.py

An evening run stores the Previous Runs API answer for as-of A to A + 2 in the
GFS set's state folder, evening/<A>.json (open_meteo.py --evening). Once the
forecast day D = A + 1 is over, the set's year files hold the archived values
for D's hours. For every evening answer fetched from A's cutoff up to 00:00
Boston on D (a later one no longer shows what the evening saw), and every D
the year files were fetched after FINAL_AFTER, this builds D's daily values
from both with open_meteo_forecast.daily_forecasts (the availability rule of
the history) and prints how far they differ, with a count of the raw hourly
values that differ, previous_day1 and previous_day2 apart: for an hour whose
day-before run had not started at the fetch, the API fills previous_day1 with
the newest run, and the rule does not use those hours.

Exits 1 when a daily value differs by more than TOLERANCE, so a day where the
evening run would have fed the model other weather than the history shows.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

import open_meteo_forecast as om
from alert_history import cutoff_utc
from measure_311 import markdown
from surge_labels import load_settings

# Largest difference allowed per daily value, in its own unit (mm, degrees C, m/s): the archive
# stores one decimal, and a daily sum of 24 such values rounds to 2.
TOLERANCE = 0.05
# The archive holds D's values for good once every run behind them has started (the day-before run
# of D's last hour is D 00 UTC): fetched after D + 1 12:00 UTC.
FINAL_AFTER = pd.Timedelta(days=1, hours=12)


def captures(evening_dir: Path) -> dict[str, pd.Timestamp]:
    """When each as-of date's evening answer was fetched, the latest line per date in _captures.jsonl."""
    path = Path(evening_dir) / "_captures.jsonl"
    fetched = {}
    for line in path.read_text().splitlines() if path.is_file() else []:
        row = json.loads(line)
        fetched[row["as_of"]] = pd.Timestamp(row["fetched_at"])
    return fetched


def archive_fetched(state_dir: Path) -> dict[int, pd.Timestamp]:
    """When each year file of the set was last fetched."""
    files = json.loads((Path(state_dir) / "_manifest.json").read_text())["files"]
    return {int(name[:4]): pd.Timestamp(entry.get("fetched_at") or entry["downloaded_at"]) for name, entry in files.items()}


def station_hours(hourly: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """The 24 hours of Logan's station day, stamped at their end, as daily_forecasts reads them."""
    start = pd.Timestamp(day).tz_localize("UTC") + om.STATION_DAY_START
    return hourly.reindex(pd.date_range(start + pd.Timedelta(hours=1), periods=om.HOURS_PER_DAY, freq="h"))


def compare(evening: pd.DataFrame, archive: pd.DataFrame, day: pd.Timestamp, cutoff: pd.Timestamp,
            margin_hours: float) -> dict:
    """One row of the table: each daily value's absolute difference and the raw hourly values that differ."""
    both = [om.daily_forecasts(hourly, [day], [cutoff], margin_hours).iloc[0] for hourly in (evening, archive)]
    row = {"day": day.date()}
    for name in om.VALUES:
        row[name] = abs(float(both[0][name]) - float(both[1][name]))
    a, b = station_hours(om.fill_snowfall(evening), day), station_hours(om.fill_snowfall(archive), day)
    for n in om.DAYS_BEFORE:
        columns = [c for c in om.columns() if c.endswith(f"_previous_day{n}")]
        x, y = a[columns].to_numpy(), b[columns].to_numpy()
        row[f"raw_day{n}_differ"] = int((~(np.isclose(x, y) | (np.isnan(x) & np.isnan(y)))).sum())
    row["worst"] = max(row[name] for name in om.VALUES)
    return row


def parity(evening_dir, forecast_dir, state_dir, settings: dict, now: pd.Timestamp) -> tuple[pd.DataFrame, list[str]]:
    """The comparison table and the evenings left out, each with why."""
    cutoff_local, margin = settings["forecast_cutoff_local"], settings["forecast_issue_margin_hours"]
    fetched, archived = captures(evening_dir), archive_fetched(state_dir)
    rows, skipped = [], []
    for as_of, at in sorted(fetched.items()):
        day = pd.Timestamp(as_of) + pd.Timedelta(days=1)
        cutoff = cutoff_utc(pd.DatetimeIndex([day]), cutoff_local)[0]
        years = om.years_for([day])
        if at < cutoff:
            skipped.append(f"{as_of}: fetched {at}, before the cutoff {cutoff}")
            continue
        begins = pd.Timestamp(day, tz="America/New_York").tz_convert("UTC")
        if at >= begins:
            skipped.append(f"{as_of}: fetched {at}, after {day.date()} began at {begins}")
            continue
        final = day.tz_localize("UTC") + FINAL_AFTER
        if any(archived.get(year, pd.Timestamp.min.tz_localize("UTC")) < final for year in years):
            skipped.append(f"{as_of}: the year files were not fetched after {final} yet (rerun open_meteo.py)")
            continue
        evening = om._read_year(Path(evening_dir) / f"{as_of}.json")
        archive = om.read_hourly(forecast_dir, years, now.date())
        rows.append({"as_of": as_of, "evening_fetched": at, **compare(evening, archive, day, cutoff, margin)})
    return pd.DataFrame(rows), skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--evening-dir", default=f"Data-Pipeline/data/state/open_meteo/{om.SET}/evening")
    parser.add_argument("--forecast-dir", default=f"Data-Pipeline/data/raw/open_meteo/{om.SET}")
    parser.add_argument("--forecast-state", default=f"Data-Pipeline/data/state/open_meteo/{om.SET}")
    parser.add_argument("--settings", default="Data-Pipeline/config/labels_features.csv")
    args = parser.parse_args()

    table, skipped = parity(args.evening_dir, args.forecast_dir, args.forecast_state, load_settings(args.settings),
                            pd.Timestamp.now(tz="UTC"))
    for reason in skipped:
        print(f"left out {reason}")
    if table.empty:
        print("no evening to compare yet")
        return
    print(markdown(table.round({name: 3 for name in [*om.VALUES, "worst"]})))
    over = table[table["worst"] > TOLERANCE]
    print(f"\n{len(table)} evenings; largest daily difference {table['worst'].max():.3f} (tolerance {TOLERANCE})")
    if not over.empty:
        raise SystemExit(f"{len(over)} evenings differ by more than {TOLERANCE}: {', '.join(over['as_of'])}")


if __name__ == "__main__":
    main()
