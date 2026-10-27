"""Save NWS forecast and alert snapshots for Boston (#7).

The NWS API keeps no archive, so each run saves what is current, exactly as
returned, under --raw-dir/<UTC date of the stamp>/<stamp>.json:

    forecasts  the raw gridpoint data for Boston City Hall (office BOX), found
               through /points: hourly rain, snowfall, gusts and temperature,
               the variables the Logan history has. Named by its updateTime.
    alerts     the active alerts for forecast zone MAZ015 and county MAC025
               (Suffolk County), named by the collection's updated time.

A snapshot is named by NWS's own issue time, so a rerun with nothing new finds
the same file and writes nothing. Every request sends the identifying
User-Agent from NWS_USER_AGENT (credentials.require; docs/data-access.md);
without one NWS answers 403.

    python Data-Pipeline/scripts/nws.py forecasts
    python Data-Pipeline/scripts/nws.py alerts
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import raw_fetch as rf
from credentials import NWS_USER_AGENT, require

API = "https://api.weather.gov"
POINT = "42.3601,-71.0589"  # Boston City Hall
ZONES = ("MAZ015", "MAC025")  # Boston's forecast zone and county (ADR-014); county-coded warnings
# (severe thunderstorm, tornado, flash flood) only come back by county
GRID_VALUES = ("quantitativePrecipitation", "snowfallAmount", "windGust", "temperature")

log = logging.getLogger(__name__)


def stamp(value, what: str) -> datetime:
    """An NWS timestamp as UTC; FetchError when it is missing or not a time."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        raise rf.FetchError(f"{what}: {value!r} is not a timestamp") from None
    if moment.tzinfo is None:
        raise rf.FetchError(f"{what}: {value!r} has no time zone")
    return moment.astimezone(timezone.utc)


def snapshot_path(raw_dir: Path, moment: datetime) -> Path:
    """Where a snapshot taken at moment goes: a folder per UTC date and a file named by the UTC time."""
    return raw_dir / moment.strftime("%Y-%m-%d") / f"{moment.strftime('%Y%m%dT%H%M%SZ')}.json"


def save(raw_dir: Path, moment: datetime, body: bytes) -> tuple[str, Path]:
    """Store a snapshot unless the same bytes are already there. Returns the action (saved, updated or
    unchanged) and the path."""
    path = snapshot_path(raw_dir, moment)
    if path.exists() and rf.sha256(path.read_bytes()) == rf.sha256(body):
        return "unchanged", path
    action = "updated" if path.exists() else "saved"
    rf.write_atomic(path, body)
    return action, path


def grid_url(fetch) -> str:
    """The forecastGridData address the NWS points API gives for POINT."""
    point = rf.parse_json(fetch(f"{API}/points/{POINT}"), "points")
    url = (point.get("properties") or {}).get("forecastGridData") if isinstance(point, dict) else None
    if not isinstance(url, str) or not url.startswith(API):
        raise rf.FetchError("points: the answer has no forecastGridData link")
    return url


def forecasts(raw_dir: Path, fetch) -> tuple[str, Path]:
    """Save the current gridpoint forecast, named by its update time, once it holds every value in
    GRID_VALUES."""
    body = fetch(grid_url(fetch))
    data = rf.parse_json(body, "gridpoint")
    props = data.get("properties") if isinstance(data, dict) else None
    if not isinstance(props, dict):
        raise rf.FetchError("gridpoint: the answer has no properties")
    missing = [v for v in GRID_VALUES if not isinstance(props.get(v), dict) or not isinstance(props[v].get("values"), list)]
    if missing:
        raise rf.FetchError(f"gridpoint: the answer lacks {missing}")
    if not any(props[v]["values"] for v in GRID_VALUES):
        raise rf.FetchError("gridpoint: every forecast value list is empty")
    return save(raw_dir, stamp(props.get("updateTime"), "gridpoint updateTime"), body)


def alerts(raw_dir: Path, fetch) -> tuple[str, Path]:
    """Save the active alerts for Boston's zone and county, named by their updated time, once the answer is a
    FeatureCollection."""
    body = fetch(f"{API}/alerts/active?zone={','.join(ZONES)}")
    data = rf.parse_json(body, "alerts")
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise rf.FetchError("alerts: the answer is not a FeatureCollection with a features list")
    return save(raw_dir, stamp(data.get("updated"), "alerts updated"), body)


KINDS = {
    "forecasts": (forecasts, "Data-Pipeline/data/raw/weather/nws_forecasts"),
    "alerts": (alerts, "Data-Pipeline/data/raw/weather/nws_alerts"),
}


def main(argv: list[str] | None = None) -> None:
    """Save one NWS snapshot (forecasts or alerts) with the User-Agent from NWS_USER_AGENT."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=sorted(KINDS))
    parser.add_argument("--raw-dir", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run, default_dir = KINDS[args.kind]
    headers = {"User-Agent": require(NWS_USER_AGENT), "Accept": "application/geo+json"}
    action, path = run(args.raw_dir or Path(default_dir), lambda url: rf.get(url, headers))
    print(f"NWS {args.kind}: {action} {path}")


if __name__ == "__main__":
    main()
