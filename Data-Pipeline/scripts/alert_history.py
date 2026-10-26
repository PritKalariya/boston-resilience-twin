"""NWS alert history for Boston from the Iowa Environmental Mesonet (#16, ADR-014).

Download (--download): IEM's bulk VTEC download (watchwarn.py, accept=csv) for
NWS office BOX, one request per year, kept as published in
Data-Pipeline/data/raw/nws_alerts/box_<year>.csv (raw pulls, versioned by DVC
once #18 adds the stage). A finished year is fetched once; the current year is
fetched on every run because new alerts keep arriving; --refresh fetches every
year again. A pull without IEM's columns (an error page) never replaces a file.

Prepare: keeps the rows of Boston's forecast zone MAZ015 and county MAC025,
one row per first bulletin, with only what is known when an alert is first
sent (ADR-014): the first bulletin `utc_prodissue`, checked against the first
12 digits of `product_id`, and the first-announced end `utc_init_expire`. The
last-state fields (`utc_issue`, `utc_expire`, `utc_updated`, `status`) are
dropped, so no feature can use them. docs/data/alert-map.csv maps each alert
type (VTEC phenomena and significance) to storm groups; a type missing from the
map is kept as `unmapped` and reported. Writes
Data-Pipeline/data/interim/nws_alerts.parquet and prints alerts per year and
area, to compare with ADR-014.

Features (alert_features, used by feature_table.py): for each day D and storm
group, the alerts whose first bulletin came before the forecast cutoff (the
evening before D, Boston time) and whose first-announced end is after D begins.
The start time is never used: the archive has only its last state (ADR-014).

Data: National Weather Service (public domain), archived by the Iowa
Environmental Mesonet of Iowa State University.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from daily_counts import STORM_GROUPS
from measure_311 import markdown
from static_layers import download

WFO = "BOX"
UGCS = {"MAZ015": "zone", "MAC025": "county"}  # Boston's forecast zone and county (ADR-014)
FIRST_YEAR = 2015
URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py"
RAW_COLUMNS = ["wfo", "ugc", "phenomena", "significance", "eventid", "vtec_year", "product_id",
               "utc_prodissue", "utc_init_expire"]
LEAKY_COLUMNS = ["utc_issue", "utc_expire", "utc_updated", "status"]  # last state, ADR-014
MAP_COLUMNS = ["phenomena", "significance", "alert_type", "storm_groups", "note"]
NO_GROUP = "none"
UNMAPPED = "unmapped"
BOSTON = "America/New_York"
FIRST_FORMAT = "%Y-%m-%d %H:%M"  # utc_prodissue
END_FORMAT = "%Y%m%d%H%M"  # utc_init_expire, and the first 12 digits of product_id
EVENT_KEY = ["phenomena", "significance", "eventid", "vtec_year", "first_bulletin_utc"]
OUT_COLUMNS = ["ugc", "area", "phenomena", "significance", "alert_type", "storm_groups", "eventid",
               "vtec_year", "product_id", "first_bulletin_utc", "first_end_utc"]


def year_url(year: int) -> str:
    return f"{URL}?accept=csv&wfo={WFO}&sts={year}-01-01T00:00Z&ets={year + 1}-01-01T00:00Z"


def raw_path(raw_dir: Path, year: int) -> Path:
    return raw_dir / f"box_{year}.csv"


def check_raw(path: Path) -> int:
    """Rows in a pulled year, or a ValueError if the file is not IEM's CSV (for example an error page)."""
    if path.stat().st_size == 0:
        raise ValueError(f"{path.name} is empty")
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path.name} is not a CSV: {exc}") from exc
    missing = set(RAW_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing IEM columns {sorted(missing)}; not the watchwarn CSV")
    return len(df)


def download_years(raw_dir: Path, years: list[int], current_year: int, refresh: bool = False, fetch=download) -> list[dict]:
    """Fetch every year that is missing, the current year and, with refresh, all of them.

    Each pull lands next to its target first and replaces it only after
    check_raw passes, so a failed or wrong pull keeps the year already stored.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for year in years:
        path = raw_path(raw_dir, year)
        if path.exists() and path.stat().st_size > 0 and year < current_year and not refresh:
            report.append({"year": year, "action": "kept", "rows": check_raw(path)})
            continue
        staging = path.with_name(path.name + ".download")
        try:
            fetch(year_url(year), staging)
            rows = check_raw(staging)
        except Exception:
            staging.unlink(missing_ok=True)
            raise
        staging.replace(path)
        report.append({"year": year, "action": "downloaded", "rows": rows})
    return report


def read_raw(raw_dir: Path, years: list[int]) -> pd.DataFrame:
    frames = []
    for year in years:
        path = raw_path(raw_dir, year)
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing; run alert_history.py --download")
        check_raw(path)
        frames.append(pd.read_csv(path, dtype=str, keep_default_na=False).assign(pull_year=year))
    if not frames:
        return pd.DataFrame(columns=[*RAW_COLUMNS, "pull_year"])
    return pd.concat(frames, ignore_index=True)


def load_alert_map(path: Path | str) -> pd.DataFrame:
    """The reviewed map from alert type to storm groups. Raises on a malformed file."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(MAP_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    df = df[MAP_COLUMNS].apply(lambda column: column.str.strip())
    if (df[["phenomena", "significance", "alert_type", "storm_groups"]] == "").any().any():
        raise ValueError(f"{path} has a row with a blank phenomena, significance, alert_type or storm_groups")
    repeated = df[df.duplicated(["phenomena", "significance"], keep=False)]
    if not repeated.empty:
        pairs = sorted(set(zip(repeated["phenomena"], repeated["significance"])))
        raise ValueError(f"{path} repeats alert types {pairs}")
    for value in df["storm_groups"]:
        groups = value.split(";")
        if groups != [NO_GROUP] and (not set(groups) <= set(STORM_GROUPS) or len(set(groups)) != len(groups)):
            raise ValueError(f"{path}: storm_groups {value!r} must be {NO_GROUP} or storm groups from {STORM_GROUPS} separated by ;")
    return df


def prepare(raw: pd.DataFrame, alert_map: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Boston's alerts, one row per area and first bulletin, safe fields only; and alerts per pull year and area."""
    missing = set(RAW_COLUMNS) - set(raw.columns)
    if missing:
        raise ValueError(f"alert rows are missing columns: {sorted(missing)}")
    df = raw[raw["ugc"].isin(list(UGCS))].copy()
    if "pull_year" not in df:
        df["pull_year"] = pd.to_numeric(df["vtec_year"], errors="coerce")
    first = pd.to_datetime(df["utc_prodissue"], format=FIRST_FORMAT, errors="coerce", utc=True)
    end = pd.to_datetime(df["utc_init_expire"], format=END_FORMAT, errors="coerce", utc=True)
    unreadable = first.isna() | end.isna()
    if unreadable.any():
        raise ValueError(f"{int(unreadable.sum())} Boston alert rows have an unreadable utc_prodissue or "
                         "utc_init_expire; IEM may have changed its format (ADR-014, revisit)")
    if (df["product_id"].str[:12] != first.dt.strftime(END_FORMAT)).any():
        raise ValueError("utc_prodissue differs from the first 12 digits of product_id; check the IEM columns (ADR-014)")
    numbers = df[["eventid", "vtec_year"]].apply(pd.to_numeric, errors="coerce")
    if numbers.isna().any().any():
        raise ValueError("Boston alert rows have an eventid or vtec_year that is not a number")

    out = pd.DataFrame({
        "ugc": df["ugc"],
        "area": df["ugc"].map(UGCS),
        "phenomena": df["phenomena"].str.strip(),
        "significance": df["significance"].str.strip(),
        "eventid": numbers["eventid"].astype("int64"),
        "vtec_year": numbers["vtec_year"].astype("int64"),
        "product_id": df["product_id"],
        "first_bulletin_utc": first,
        "first_end_utc": end,
    })
    report = (df.assign(area=out["area"]).groupby(["pull_year", "area"]).size().unstack(fill_value=0)
              .reindex(columns=list(UGCS.values()), fill_value=0))
    out = out.drop_duplicates(["ugc", *EVENT_KEY])
    out = out.merge(alert_map[["phenomena", "significance", "alert_type", "storm_groups"]],
                    on=["phenomena", "significance"], how="left")
    code = out["phenomena"] + "." + out["significance"]
    out["alert_type"] = out["alert_type"].fillna(code)
    out["storm_groups"] = out["storm_groups"].fillna(UNMAPPED)
    out = out.sort_values(["first_bulletin_utc", "ugc", "phenomena", "significance", "eventid"], ignore_index=True)
    return out[OUT_COLUMNS], report


def cutoff_utc(days: pd.DatetimeIndex, cutoff_local: str) -> pd.DatetimeIndex:
    """For each day D: the evening before D at cutoff_local (HH:MM, Boston time), in UTC."""
    hours, minutes = (int(part) for part in cutoff_local.split(":"))
    local = days.normalize() - pd.Timedelta(days=1) + pd.Timedelta(hours=hours, minutes=minutes)
    return local.tz_localize(BOSTON).tz_convert("UTC")


def day_start_utc(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Midnight at the start of each day D, Boston time, in UTC."""
    return days.normalize().tz_localize(BOSTON).tz_convert("UTC")


def alert_feature_names(groups: list[str]) -> list[str]:
    return [name for group in groups for name in (f"alert_{group}_count", f"alert_{group}_warning")]


def alert_features(alerts: pd.DataFrame, dates, cutoff_local: str, groups: list[str]) -> pd.DataFrame:
    """Per day D: alerts known before the cutoff the evening before D that, as first announced, run past D's start.

    An alert counts once per event even if it went to both Boston's zone and
    county. `_count` is how many such alerts a group has; `_warning` is 1 if
    one of them is a warning (VTEC significance W).
    """
    days = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates, dtype="datetime64[ns]")).unique()).sort_values()
    cutoff, start = cutoff_utc(days, cutoff_local), day_start_utc(days)
    out = pd.DataFrame({"date": days, "forecast_cutoff_utc": cutoff})
    events = alerts.drop_duplicates(EVENT_KEY)
    # Compare nanoseconds since 1970 (UTC) on both sides; parquet often reads times back in microseconds.
    known_before = cutoff.as_unit("ns").asi8[:, None]
    day_begins = start.as_unit("ns").asi8[:, None]
    for group in groups:
        in_group = events["storm_groups"].map(lambda value, g=group: g in value.split(";")).astype(bool)
        mine = events.loc[in_group]  # .loc with a bool mask selects rows even when there are none
        first = pd.DatetimeIndex(mine["first_bulletin_utc"]).as_unit("ns").asi8[None, :]
        end = pd.DatetimeIndex(mine["first_end_utc"]).as_unit("ns").asi8[None, :]
        warning = (mine["significance"] == "W").to_numpy()[None, :]
        active = (first < known_before) & (end > day_begins)
        out[f"alert_{group}_count"] = active.sum(axis=1).astype("int64")
        out[f"alert_{group}_warning"] = (active & warning).any(axis=1).astype("int64")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/nws_alerts")
    parser.add_argument("--map", default="docs/data/alert-map.csv")
    parser.add_argument("--out", default="Data-Pipeline/data/interim/nws_alerts.parquet")
    parser.add_argument("--first-year", type=int, default=FIRST_YEAR)
    parser.add_argument("--last-year", type=int, help="default: the current year (UTC)")
    parser.add_argument("--download", action="store_true", help="fetch missing years and the current year first")
    parser.add_argument("--refresh", action="store_true", help="fetch every year again")
    args = parser.parse_args()

    current = datetime.now(timezone.utc).year
    years = list(range(args.first_year, (args.last_year or current) + 1))
    raw_dir = Path(args.raw_dir)
    if args.download or args.refresh:
        print(markdown(pd.DataFrame(download_years(raw_dir, years, current, refresh=args.refresh))))
    alerts, report = prepare(read_raw(raw_dir, years), load_alert_map(args.map))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    alerts.to_parquet(out, index=False)

    print("\n## Boston alerts per pulled year (one row per first bulletin)\n")
    print(markdown(report.reset_index().rename(columns={"pull_year": "year"})))
    unmapped = alerts[alerts["storm_groups"] == UNMAPPED]
    if not unmapped.empty:
        types = ", ".join(sorted(set(unmapped["alert_type"])))
        print(f"\nWARNING: {len(unmapped)} alerts have types missing from {args.map}: {types}")
    print(f"\n{len(alerts):,} Boston alerts ({alerts['first_bulletin_utc'].min()} to "
          f"{alerts['first_bulletin_utc'].max()}); wrote {out}")


if __name__ == "__main__":
    main()
