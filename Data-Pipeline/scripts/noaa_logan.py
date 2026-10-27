"""Download NOAA GHCN-D daily weather for Boston Logan (#7).

Station USW00014739 through the NOAA Climate Data Online API v2, from
2015-01-01: rain, snow, snow depth, max and min temperature, average wind and
gusts (DATATYPES), in metric units. The token comes from NOAA_CDO_TOKEN
(credentials.require; docs/data-access.md). This is the observed weather the
model trains on (ADR-003).

One JSON file per year under --raw-dir. CDO pages at 1,000 rows, so a year
takes about three requests; the file holds the first page's metadata and every
page's results in order, with the values unchanged. NOAA revises recent days,
so a year is final 30 days after it ends; the manifest that records this
lives in --state-dir, not in the raw folder (raw_fetch has the rerun rules). CDO
allows 5 requests a second, so pages are spaced.

    python Data-Pipeline/scripts/noaa_logan.py
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import raw_fetch as rf
from credentials import NOAA_CDO_TOKEN, require

STATION = "GHCND:USW00014739"
FIRST_DAY = date(2015, 1, 1)
DATATYPES = ("PRCP", "SNOW", "SNWD", "TMAX", "TMIN", "AWND", "WSF2", "WSF5")
API = "https://www.ncei.noaa.gov/cdo-web/api/v2/data"
PAGE = 1000
SETTLE_DAYS = 30
PAUSE_SECONDS = 0.25

log = logging.getLogger(__name__)


def request_url(period: rf.Period, offset: int) -> str:
    """The CDO API address for one page of a year: Logan's GHCN-D daily values in metric units, PAGE results
    from offset."""
    query = [("datasetid", "GHCND"), ("stationid", STATION), ("startdate", period.start.isoformat()),
             ("enddate", period.end.isoformat()), ("units", "metric"), ("limit", PAGE), ("offset", offset),
             *(("datatypeid", d) for d in DATATYPES)]
    return f"{API}?{urllib.parse.urlencode(query)}"


def check_page(page, period: rf.Period) -> tuple[list, int]:
    """(results, total count) of one CDO page. An empty object means no data."""
    if not isinstance(page, dict):
        raise rf.FetchError(f"{period.name}: the answer is not a JSON object")
    if not page:
        return [], 0
    try:
        total = int(page["metadata"]["resultset"]["count"])
    except (KeyError, TypeError, ValueError):
        raise rf.FetchError(f"{period.name}: the answer has no metadata.resultset.count") from None
    results = page.get("results")
    if not isinstance(results, list) or not results:
        raise rf.FetchError(f"{period.name}: the answer has no results list")
    for row in results:
        if not (isinstance(row, dict) and {"date", "datatype", "value"} <= set(row)):
            raise rf.FetchError(f"{period.name}: a result lacks a date, datatype or value: {row!r}"[:300])
        day = str(row["date"])[:10]
        if not period.start.isoformat() <= day <= period.end.isoformat():
            raise rf.FetchError(f"{period.name}: a result on {day} is outside {period.start} to {period.end}")
        if row["datatype"] not in DATATYPES:
            raise rf.FetchError(f"{period.name}: unexpected datatype {row['datatype']!r}")
    return results, total


def fetch_year(period: rf.Period, fetch, pause=time.sleep) -> bytes | None:
    """Every page of one year joined into one answer, or None when CDO has no data."""
    first, results, total, offset = None, [], None, 1
    while True:
        page = rf.parse_json(fetch(request_url(period, offset)), period.name)
        rows, count = check_page(page, period)
        if first is None:
            first, total = page, count
        if not rows:
            break
        results += rows
        offset += PAGE
        if offset > total:
            break
        pause(PAUSE_SECONDS)
    if not results:
        return None
    if len(results) != total:
        raise rf.FetchError(f"{period.name}: got {len(results)} results, CDO said {total}")
    return (json.dumps({"metadata": first["metadata"], "results": results}, indent=1) + "\n").encode()


def refresh(raw_dir: Path, state_dir: Path, fetch, today: date, now: datetime, pause=time.sleep) -> list[dict]:
    """Bring the station up to today. Returns one record per year."""
    manifest = rf.load_manifest(state_dir)
    records = []
    for period in rf.years(FIRST_DAY, today):
        filename = f"{period.name}.json"
        if rf.is_final(manifest.get(filename), period, SETTLE_DAYS) and (raw_dir / filename).exists():
            records.append({"file": filename, "action": "final"})
            continue
        body = fetch_year(period, fetch, pause)
        if body is None:
            if period.end < today - timedelta(days=SETTLE_DAYS):
                raise rf.FetchError(f"{period.name}: CDO has no data for a year long over")
            records.append({"file": filename, "action": "no data yet"})
            continue
        rows = len(json.loads(body)["results"])
        records.append({"file": filename, "action": rf.store(raw_dir, state_dir, manifest, filename, period, body, now, rows=rows), "rows": rows})
    return records


def main(argv: list[str] | None = None) -> None:
    """Bring Logan's yearly files up to today with the NOAA_CDO_TOKEN key and print what changed (the
    ingest_noaa_logan task)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-dir", type=Path, default=Path("Data-Pipeline/data/raw/weather/noaa_logan"))
    parser.add_argument("--state-dir", type=Path, default=Path("Data-Pipeline/data/state/weather/noaa_logan"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    headers = {"token": require(NOAA_CDO_TOKEN), "User-Agent": "boston-resilience-twin"}
    now = rf.utc_now()
    records = refresh(args.raw_dir, args.state_dir, lambda url: rf.get(url, headers), now.date(), now)
    changed = [f"{r['file']} {r['action']}" + (f" ({r['rows']:,} values)" if "rows" in r else "")
               for r in records if r["action"] != "final"]
    print(f"NOAA {STATION}: " + (", ".join(changed) if changed else "every year final"))


if __name__ == "__main__":
    main()
