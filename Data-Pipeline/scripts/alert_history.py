"""NWS alert history for Boston from the Iowa Environmental Mesonet (#16, ADR-014, Option A+).

Download (--download): IEM's bulk VTEC download (watchwarn.py, accept=csv) for
NWS office BOX, one request per year, kept as published in
Data-Pipeline/data/raw/nws_alerts/box_<year>.csv (raw pulls, versioned by DVC
once #18 adds the stage). IEM files an alert under the year it starts, so from
15 December the next year's file is pulled too: an alert announced on
31 December that starts on 1 January is only there (ADR-014). A year is final
once it was pulled FINAL_AFTER_DAYS after it ended, since alerts that start
late in December are still updated in January (the 2015 file holds updates
from 2016-01-05); until then every download pulls it again. _downloads.json
records when each year was pulled; --refresh pulls every year. A pull without
IEM's columns (an error page) never replaces a stored year.

Prepare: keeps the rows of Boston's forecast zone MAZ015 and county MAC025,
one row per area and first bulletin, with what is known when an alert is first
sent (ADR-014): the first bulletin `utc_prodissue`, checked against the first
12 digits of `product_id`, and the first-announced end `utc_init_expire`. The
last-state fields `status`, `utc_expire` and `utc_updated` serve one check and
are then dropped, with `utc_issue`, so no feature can use them: when `status`
is CAN or UPG and `utc_expire` equals `utc_updated`, the alert was cancelled or
upgraded at `utc_updated`, kept as `ended_early_utc`. Extensions, shortened
ends and added areas are not used. docs/data/alert-map.csv maps each alert type
(VTEC phenomena and significance) to storm groups; a type missing from the map
is kept as `unmapped` and reported. Writes
Data-Pipeline/data/interim/nws_alerts.parquet and prints alerts per year and
area, to compare with ADR-014.

Features (alert_features, used by feature_table.py for the history and the
evening run alike): for each day D and storm group, the alerts whose first
bulletin came before the forecast cutoff (the evening before D, Boston time),
whose first-announced end is after D begins, and that were not cancelled or
upgraded before that same cutoff. One VTEC event counts once, even when it went
to both Boston's zone and county. The start time is never used: the archive has
only its last state (ADR-014).

Freshness (--check-nws, ADR-014): the evening run uses a pull only if it holds
every Boston alert message in #7's NWS snapshot that was sent before the
cutoff. Otherwise it fails (FreshnessError) before writing, so missing data
never becomes zero counts; the pipeline step retries it.

Data: National Weather Service (public domain), archived by the Iowa
Environmental Mesonet of Iowa State University.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from daily_counts import STORM_GROUPS
from measure_311 import markdown
from static_layers import download
from surge_labels import load_settings

WFO = "BOX"
UGCS = {"MAZ015": "zone", "MAC025": "county"}  # Boston's forecast zone and county (ADR-014)
FIRST_YEAR = 2015
URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py"
RAW_COLUMNS = ["wfo", "ugc", "phenomena", "significance", "eventid", "vtec_year", "product_id",
               "utc_prodissue", "utc_init_expire"]
LAST_STATE = ["status", "utc_expire", "utc_updated"]  # read for the A+ check only (ADR-014)
LEAKY_COLUMNS = ["utc_issue", *LAST_STATE]  # last state, never in the output
PULL_COLUMNS = [*RAW_COLUMNS, *LAST_STATE]
ENDED_EARLY = ("CAN", "UPG")  # cancelled, upgraded
MAP_COLUMNS = ["phenomena", "significance", "alert_type", "storm_groups", "note"]
NO_GROUP = "none"
UNMAPPED = "unmapped"
BOSTON = "America/New_York"
FIRST_FORMAT = "%Y-%m-%d %H:%M"  # utc_prodissue, utc_expire, utc_updated
END_FORMAT = "%Y%m%d%H%M"  # utc_init_expire, and the first 12 digits of product_id
NEXT_YEAR_FROM = (12, 15)  # (month, day) from which the next year's file is pulled too
FINAL_AFTER_DAYS = 14  # a year pulled this long after it ended no longer changes
DOWNLOADS = "_downloads.json"
VTEC_KEY = ["phenomena", "significance", "eventid", "vtec_year"]  # one VTEC event of office BOX
EVENT_KEY = [*VTEC_KEY, "first_bulletin_utc"]  # one row per area and first bulletin
OUT_COLUMNS = ["ugc", "area", "phenomena", "significance", "alert_type", "storm_groups", "eventid",
               "vtec_year", "product_id", "first_bulletin_utc", "first_end_utc", "ended_early_utc"]
FEATURE_INPUT = ["storm_groups", *EVENT_KEY, "first_end_utc", "ended_early_utc"]
NEVER = np.iinfo(np.int64).max  # a missing time: later than every cutoff
# Operational VTEC: /O.action.office.phenomena.significance.event number.start-end/
VTEC = re.compile(r"/O\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.")
SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z\.json")  # #7's nws.py names a snapshot by NWS's updated time


class FreshnessError(RuntimeError):
    """The evening pull lacks an alert message the NWS sent before the cutoff (ADR-014)."""


def year_url(year: int) -> str:
    return f"{URL}?accept=csv&wfo={WFO}&sts={year}-01-01T00:00Z&ets={year + 1}-01-01T00:00Z"


def raw_path(raw_dir: Path, year: int) -> Path:
    return raw_dir / f"box_{year}.csv"


def years_to_pull(first_year: int, today: date) -> list[int]:
    """first_year to this year and, from NEXT_YEAR_FROM on, the next year too (today in Boston)."""
    last = today.year + 1 if (today.month, today.day) >= NEXT_YEAR_FROM else today.year
    return list(range(first_year, last + 1))


def load_downloads(raw_dir: Path) -> dict:
    """When each year was pulled: {"2015": {"downloaded_at": ISO time in UTC, "rows": n}}."""
    path = raw_dir / DOWNLOADS
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_downloads(raw_dir: Path, downloads: dict) -> None:
    staging = raw_dir / f"{DOWNLOADS}.part"
    staging.write_text(json.dumps(downloads, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    staging.replace(raw_dir / DOWNLOADS)


def is_final(downloads: dict, raw_dir: Path, year: int) -> bool:
    """True once the stored year was pulled FINAL_AFTER_DAYS or more after it ended."""
    entry = downloads.get(str(year))
    if not entry or not raw_path(raw_dir, year).is_file():
        return False
    settled = datetime(year + 1, 1, 1, tzinfo=timezone.utc) + timedelta(days=FINAL_AFTER_DAYS)
    return datetime.fromisoformat(entry["downloaded_at"]) >= settled


def check_raw(path: Path) -> int:
    """Rows in a pulled year, or a ValueError if the file is not IEM's CSV (for example an error page)."""
    if path.stat().st_size == 0:
        raise ValueError(f"{path.name} is empty")
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path.name} is not a CSV: {exc}") from exc
    missing = set(PULL_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path.name} is missing IEM columns {sorted(missing)}; not the watchwarn CSV")
    return len(df)


def download_years(raw_dir: Path, years: list[int], now: datetime, refresh: bool = False, fetch=download) -> list[dict]:
    """Pull every year that is not final and, with refresh, every year; now is the run's time.

    Each pull lands next to its target first and replaces it only after
    check_raw passes, so a failed or wrong pull keeps the year already stored.
    A next year with no alerts yet may come back empty: the pull is recorded
    and no file is kept. Any other empty answer is an error.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    downloads = load_downloads(raw_dir)
    this_year = now.astimezone(ZoneInfo(BOSTON)).year
    pulled = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    report = []
    for year in years:
        path = raw_path(raw_dir, year)
        if not refresh and is_final(downloads, raw_dir, year):
            report.append({"year": year, "action": "kept", "rows": check_raw(path)})
            continue
        staging = path.with_name(path.name + ".download")
        try:
            fetch(year_url(year), staging)
            no_alerts_yet = year > this_year and not path.exists() and staging.stat().st_size == 0
            rows = 0 if no_alerts_yet else check_raw(staging)
        except Exception as exc:
            staging.unlink(missing_ok=True)
            exc.add_note(f"IEM pull of {year} failed; the stored file, if any, is unchanged")
            raise
        if no_alerts_yet:
            staging.unlink()
        else:
            staging.replace(path)
        downloads[str(year)] = {"downloaded_at": pulled, "rows": rows}
        save_downloads(raw_dir, downloads)
        report.append({"year": year, "action": "no alerts yet" if no_alerts_yet else "downloaded", "rows": rows})
    return report


def read_raw(raw_dir: Path, years: list[int], optional=frozenset()) -> pd.DataFrame:
    """The pulled years, each row with its pull_year. A year in optional (a next year with no
    alerts yet) may have no file."""
    frames = []
    for year in years:
        path = raw_path(raw_dir, year)
        if not path.exists():
            if year in optional:
                continue
            raise FileNotFoundError(f"{path} is missing; run alert_history.py --download")
        check_raw(path)
        frames.append(pd.read_csv(path, dtype=str, keep_default_na=False).assign(pull_year=year))
    if not frames:
        return pd.DataFrame(columns=[*PULL_COLUMNS, "pull_year"])
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


def _utc(values: pd.Series, fmt: str) -> pd.Series:
    return pd.to_datetime(values, format=fmt, errors="coerce", utc=True)


def ended_early(rows: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """The A+ check (ADR-014) on IEM's last state: when each row was cancelled or upgraded, and
    which CAN or UPG rows fail the check.

    A row ended early when status is CAN or UPG and utc_expire equals
    utc_updated; its time is utc_updated. Every other row gets no time (NaT)
    and keeps its first-bulletin behavior.
    """
    expire, updated = _utc(rows["utc_expire"], FIRST_FORMAT), _utc(rows["utc_updated"], FIRST_FORMAT)
    candidate = rows["status"].astype(str).str.strip().isin(ENDED_EARLY)
    passes = candidate & updated.notna() & (expire == updated)
    return updated.where(passes), candidate & ~passes


def prepare(raw: pd.DataFrame, alert_map: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Boston's alerts, one row per area and first bulletin, safe fields only; and alerts per pull year and area."""
    missing = set(PULL_COLUMNS) - set(raw.columns)
    if missing:
        raise ValueError(f"alert rows are missing columns: {sorted(missing)}")
    df = raw[raw["ugc"].isin(list(UGCS))].copy()
    if "pull_year" not in df:
        df["pull_year"] = pd.to_numeric(df["vtec_year"], errors="coerce")
    first, end = _utc(df["utc_prodissue"], FIRST_FORMAT), _utc(df["utc_init_expire"], END_FORMAT)
    unreadable = first.isna() | end.isna()
    if unreadable.any():
        raise ValueError(f"{int(unreadable.sum())} Boston alert rows have an unreadable utc_prodissue or "
                         "utc_init_expire; IEM may have changed its format (ADR-014, revisit)")
    if (df["product_id"].str[:12] != first.dt.strftime(END_FORMAT)).any():
        raise ValueError("utc_prodissue differs from the first 12 digits of product_id; check the IEM columns (ADR-014)")
    numbers = df[["eventid", "vtec_year"]].apply(pd.to_numeric, errors="coerce")
    if numbers.isna().any().any():
        raise ValueError("Boston alert rows have an eventid or vtec_year that is not a number")
    early, _ = ended_early(df)

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
        "ended_early_utc": early,
    })
    report = (df.assign(area=out["area"]).groupby(["pull_year", "area"]).size().unstack(fill_value=0)
              .reindex(columns=list(UGCS.values()), fill_value=0))
    # The same alert in two pulls (its start can move into another year): keep the freshest last state.
    out = (out.assign(_updated=_utc(df["utc_updated"], FIRST_FORMAT))
           .sort_values("_updated", na_position="first", kind="stable")
           .drop_duplicates(["ugc", *EVENT_KEY], keep="last").drop(columns="_updated"))
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


def _ns(values) -> np.ndarray:
    """Nanoseconds since 1970 (UTC), so both sides compare alike (parquet often reads times back
    in microseconds); a missing time becomes NEVER."""
    index = pd.DatetimeIndex(values).as_unit("ns")
    return np.where(index.isna(), NEVER, index.asi8)


def alert_features(alerts: pd.DataFrame, dates, cutoff_local: str, groups: list[str]) -> pd.DataFrame:
    """Per day D: alerts known before the cutoff the evening before D that, as first announced, run past D's start.

    An alert cancelled or upgraded before that same cutoff is left out (A+); a
    cancellation at or after the cutoff changes nothing. One VTEC event counts
    once, and counts while any of its rows (Boston's zone, its county) does.
    `_count` is how many such alerts a group has; `_warning` is 1 if one of
    them is a warning (VTEC significance W).
    """
    missing = set(FEATURE_INPUT) - set(alerts.columns)
    if missing:
        raise ValueError(f"alerts are missing columns {sorted(missing)}; rerun alert_history.py (ADR-014 A+)")
    days = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates, dtype="datetime64[ns]")).unique()).sort_values()
    cutoff, start = cutoff_utc(days, cutoff_local), day_start_utc(days)
    out = pd.DataFrame({"date": days, "forecast_cutoff_utc": cutoff})
    known_before, day_begins = _ns(cutoff)[:, None], _ns(start)[:, None]
    for group in groups:
        in_group = alerts["storm_groups"].map(lambda value, g=group: g in value.split(";")).astype(bool)
        mine = alerts.loc[in_group].sort_values(VTEC_KEY, kind="stable")  # .loc with a bool mask selects rows even when there are none
        if mine.empty:
            out[f"alert_{group}_count"] = 0
            out[f"alert_{group}_warning"] = 0
            continue
        row_active = ((_ns(mine["first_bulletin_utc"])[None, :] < known_before)
                      & (_ns(mine["first_end_utc"])[None, :] > day_begins)
                      & ~(_ns(mine["ended_early_utc"])[None, :] < known_before))
        event = mine.groupby(VTEC_KEY, sort=False).ngroup().to_numpy()
        first_rows = np.flatnonzero(np.r_[True, event[1:] != event[:-1]])
        active = np.logical_or.reduceat(row_active, first_rows, axis=1)
        warning = (mine["significance"].to_numpy()[first_rows] == "W")[None, :]
        out[f"alert_{group}_count"] = active.sum(axis=1).astype("int64")
        out[f"alert_{group}_warning"] = (active & warning).any(axis=1).astype("int64")
    return out


def nws_messages(snapshot: dict) -> pd.DataFrame:
    """Boston's operational VTEC events from office BOX in an NWS active-alerts answer (#7), one
    row per message and event."""
    rows = []
    for feature in snapshot.get("features") or []:
        props = feature.get("properties") or {}
        if props.get("status") != "Actual":
            continue
        areas = sorted(set((props.get("geocode") or {}).get("UGC") or []) & set(UGCS))
        if not areas:
            continue
        if not props.get("sent"):
            raise ValueError(f"NWS alert {props.get('id')} has no sent time")
        sent = pd.Timestamp(props["sent"]).tz_convert("UTC")
        for vtec in (props.get("parameters") or {}).get("VTEC") or []:
            match = VTEC.match(vtec)
            if match and match.group(2) == f"K{WFO}":
                rows.append({"action": match.group(1), "phenomena": match.group(3), "significance": match.group(4),
                             "eventid": int(match.group(5)), "areas": areas, "sent_utc": sent, "vtec": vtec})
    return pd.DataFrame(rows, columns=["action", "phenomena", "significance", "eventid", "areas", "sent_utc", "vtec"])


def check_freshness(raw: pd.DataFrame, snapshot: dict, cutoff: pd.Timestamp, pulled_at: pd.Timestamp) -> dict:
    """The pull holds every Boston alert message the NWS sent before the cutoff (ADR-014).

    raw is the pull as read_raw returns it, last state included; snapshot is
    #7's NWS active-alerts answer, taken after the cutoff. A message is in the
    pull when IEM has a row for its event in one of its Boston areas, and for a
    cancellation or upgrade when that row already shows CAN or UPG. Raises
    FreshnessError when the pull or the snapshot is older than the cutoff or a
    message is missing. Otherwise returns how many messages were checked and
    how many minutes IEM's newest update is older than the pull: the most IEM
    can have lagged.
    """
    if pulled_at < cutoff:
        raise FreshnessError(f"the IEM pull from {pulled_at} is older than the cutoff {cutoff}; pull again after it")
    taken = pd.Timestamp(snapshot["updated"]).tz_convert("UTC") if snapshot.get("updated") else None
    if taken is None or taken < cutoff:
        raise FreshnessError(f"the NWS snapshot from {taken} is older than the cutoff {cutoff}; take one after it (#7)")
    messages = nws_messages(snapshot)
    messages = messages[messages["sent_utc"] < cutoff]
    boston = raw[raw["ugc"].isin(list(UGCS))]
    ids, years = pd.to_numeric(boston["eventid"], errors="coerce"), pd.to_numeric(boston["vtec_year"], errors="coerce")
    problems = []
    for m in messages.itertuples(index=False):
        rows = boston[(boston["phenomena"].str.strip() == m.phenomena) & (boston["significance"].str.strip() == m.significance)
                      & (ids == m.eventid) & years.between(m.sent_utc.year - 1, m.sent_utc.year + 1)
                      & boston["ugc"].isin(m.areas)]
        if rows.empty:
            problems.append(f"{m.vtec} sent {m.sent_utc} is not in the pull")
        elif m.action in ENDED_EARLY and not rows["status"].astype(str).str.strip().isin(ENDED_EARLY).any():
            problems.append(f"{m.vtec} sent {m.sent_utc} is in the pull without its {m.action}")
    if problems:
        raise FreshnessError(f"the IEM pull from {pulled_at} lacks {len(problems)} NWS message(s) sent before the "
                             f"cutoff {cutoff}: {'; '.join(problems)}. Retry later; nothing was written")
    newest = _utc(raw["utc_updated"], FIRST_FORMAT).max()
    lag = None if pd.isna(newest) else int((pulled_at - newest).total_seconds() // 60)
    return {"messages": len(messages), "iem_newest_update_utc": newest, "lag_at_most_minutes": lag}


def newest_snapshot(path: Path) -> Path:
    """A snapshot file as given, or the newest one in #7's folder of them."""
    if not path.is_dir():
        return path
    snapshots = [p for p in path.rglob("*.json") if SNAPSHOT_NAME.fullmatch(p.name)]
    if not snapshots:
        raise FreshnessError(f"no NWS alert snapshot in {path}; run nws.py alerts (#7) after the cutoff")
    return max(snapshots, key=lambda p: p.name)


def pull_time(raw_dir: Path, years: list[int]) -> pd.Timestamp:
    """When the pull was made: the oldest download among the years that can still change."""
    downloads = load_downloads(raw_dir)
    open_years = [year for year in years if not is_final(downloads, raw_dir, year)] or list(years)
    unrecorded = [year for year in open_years if str(year) not in downloads]
    if unrecorded:
        raise FreshnessError(f"{raw_dir / DOWNLOADS} has no pull time for {unrecorded}; run with --download")
    return min(pd.Timestamp(downloads[str(year)]["downloaded_at"]) for year in open_years).tz_convert("UTC")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/nws_alerts")
    parser.add_argument("--map", default="docs/data/alert-map.csv")
    parser.add_argument("--out", default="Data-Pipeline/data/interim/nws_alerts.parquet")
    parser.add_argument("--first-year", type=int, default=FIRST_YEAR)
    parser.add_argument("--last-year", type=int, help="default: this year and, from 15 December, the next year too")
    parser.add_argument("--download", action="store_true", help="first pull every year that is not final")
    parser.add_argument("--refresh", action="store_true", help="pull every year again")
    parser.add_argument("--check-nws", help="#7's NWS alert snapshot, or its folder (the newest snapshot is used): the "
                        "pull must hold every Boston message sent before this evening's cutoff (ADR-014)")
    parser.add_argument("--settings", default="Data-Pipeline/config/labels_features.csv",
                        help="forecast_cutoff_local for --check-nws")
    parser.add_argument("--today", help="the run's evening as YYYY-MM-DD, Boston time; default: today")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    today = date.fromisoformat(args.today) if args.today else now.astimezone(ZoneInfo(BOSTON)).date()
    years = list(range(args.first_year, args.last_year + 1)) if args.last_year else years_to_pull(args.first_year, today)
    raw_dir = Path(args.raw_dir)
    if args.download or args.refresh:
        print(markdown(pd.DataFrame(download_years(raw_dir, years, now, refresh=args.refresh))))
    raw = read_raw(raw_dir, years, optional={year for year in years if year > today.year})
    alerts, report = prepare(raw, load_alert_map(args.map))
    if args.check_nws:
        cutoff_local = load_settings(args.settings)["forecast_cutoff_local"]
        cutoff = cutoff_utc(pd.DatetimeIndex([pd.Timestamp(today) + pd.Timedelta(days=1)]), cutoff_local)[0]
        snapshot = newest_snapshot(Path(args.check_nws))
        fresh = check_freshness(raw, json.loads(snapshot.read_text(encoding="utf-8")), cutoff, pull_time(raw_dir, years))
        print(f"freshness: the {fresh['messages']} Boston NWS message(s) in {snapshot.name} sent before {cutoff} are "
              f"all in the pull; IEM's newest update is {fresh['lag_at_most_minutes']} minutes older than the pull, "
              "the most IEM can have lagged")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    alerts.to_parquet(out, index=False)

    print("\n## Boston alerts per pulled year (one row per first bulletin)\n")
    print(markdown(report.reset_index().rename(columns={"pull_year": "year"})))
    print(f"\n{int(alerts['ended_early_utc'].notna().sum()):,} alerts were cancelled or upgraded early "
          "(A+: CAN or UPG with utc_expire = utc_updated)")
    failed = int(ended_early(raw[raw["ugc"].isin(list(UGCS))])[1].sum())
    if failed:
        print(f"WARNING: {failed} CAN or UPG rows have a utc_expire unlike their utc_updated and keep their "
              "first-announced end (ADR-014, revisit)")
    unmapped = alerts[alerts["storm_groups"] == UNMAPPED]
    if not unmapped.empty:
        types = ", ".join(sorted(set(unmapped["alert_type"])))
        print(f"\nWARNING: {len(unmapped)} alerts have types missing from {args.map}: {types}")
    print(f"\n{len(alerts):,} Boston alerts ({alerts['first_bulletin_utc'].min()} to "
          f"{alerts['first_bulletin_utc'].max()}); wrote {out}")


if __name__ == "__main__":
    main()
