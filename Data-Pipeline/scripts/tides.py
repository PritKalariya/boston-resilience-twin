"""Download hourly water levels for the NOAA Boston tide gauge (#7).

Station 8443970 "Boston" (CO-OPS), from 2015-01-01: the verified hourly
observed height (`hourly_height`) and the astronomical prediction
(`predictions`, hourly), so storm surge is observed minus predicted. Metres
above mean lower low water (MLLW), times in GMT. No key is needed.

Each product has its own folder under --raw-dir with one JSON file per
half-year, exactly as CO-OPS returned it: the API refuses more than 365 days
per request, so a leap year cannot be fetched whole. Verified observations lag
about a month, so an observed half-year is final 45 days after it ends;
predictions are final once the half-year is over. The manifests that record
this live in --state-dir, one folder per product, not in the raw folders
(raw_fetch has the rerun rules).

    python Data-Pipeline/scripts/tides.py
"""

from __future__ import annotations

import argparse
import logging
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import raw_fetch as rf

STATION = "8443970"
FIRST_DAY = date(2015, 1, 1)
API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
APPLICATION = "boston-resilience-twin"
# folder: (product parameters, the answer's list key, days after a half-year ends until it is final)
PRODUCTS = {
    "observed": ({"product": "hourly_height"}, "data", 45),
    "predicted": ({"product": "predictions", "interval": "h"}, "predictions", 0),
}
NO_DATA = "No data was found"

log = logging.getLogger(__name__)


def request_url(params: dict, period: rf.Period) -> str:
    """The CO-OPS address for one product and half-year: station 8443970, GMT, metric, MLLW datum."""
    query = {
        "station": STATION, "begin_date": period.start.strftime("%Y%m%d"), "end_date": period.end.strftime("%Y%m%d"),
        "datum": "MLLW", "units": "metric", "time_zone": "gmt", "format": "json", "application": APPLICATION, **params,
    }
    return f"{API}?{urllib.parse.urlencode(query)}"


def check_answer(body: bytes, key: str, period: rf.Period) -> int | None:
    """Rows in a CO-OPS answer, None when CO-OPS has no data yet for the period,
    or FetchError when the answer must not be stored."""
    what = f"{period.name}"
    data = rf.parse_json(body, what)
    if not isinstance(data, dict):
        raise rf.FetchError(f"{what}: the answer is not a JSON object")
    if "error" in data:
        message = str((data["error"] or {}).get("message", data["error"])).strip()
        if message.startswith(NO_DATA):
            return None
        raise rf.FetchError(f"{what}: CO-OPS refused the request: {message}")
    rows = data.get(key)
    if not isinstance(rows, list) or not rows:
        raise rf.FetchError(f"{what}: the answer has no {key!r} list")
    bad = [r for r in rows if not (isinstance(r, dict) and {"t", "v"} <= set(r))]
    if bad:
        raise rf.FetchError(f"{what}: {len(bad)} rows lack a time or value")
    first, last = rows[0]["t"][:10], rows[-1]["t"][:10]
    if first < period.start.isoformat() or last > period.end.isoformat():
        raise rf.FetchError(f"{what}: rows run from {first} to {last}, outside {period.start} to {period.end}")
    return len(rows)


def refresh(raw_dir: Path, state_dir: Path, fetch, today: date, now: datetime) -> list[dict]:
    """Bring both products up to today. Returns one record per half-year."""
    records = []
    for folder_name, (params, key, settle_days) in PRODUCTS.items():
        folder, state = raw_dir / folder_name, state_dir / folder_name
        manifest = rf.load_manifest(state)
        for period in rf.half_years(FIRST_DAY, today):
            filename = f"{period.name}.json"
            record = {"product": folder_name, "file": filename}
            if rf.is_final(manifest.get(filename), period, settle_days) and (folder / filename).exists():
                records.append({**record, "action": "final"})
                continue
            body = fetch(request_url(params, period))
            rows = check_answer(body, key, period)
            if rows is None:
                if period.end < today - timedelta(days=settle_days + 30):
                    raise rf.FetchError(f"{folder_name} {period.name}: CO-OPS has no data for a period long over")
                records.append({**record, "action": "no data yet"})
                continue
            action = rf.store(folder, state, manifest, filename, period, body, now, rows=rows)
            records.append({**record, "action": action, "rows": rows})
    return records


def main(argv: list[str] | None = None) -> None:
    """Bring both tide products up to today and print what changed (the ingest_tides task)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-dir", type=Path, default=Path("Data-Pipeline/data/raw/tides"))
    parser.add_argument("--state-dir", type=Path, default=Path("Data-Pipeline/data/state/tides"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = rf.utc_now()
    records = refresh(args.raw_dir, args.state_dir, lambda url: rf.get(url, {"User-Agent": APPLICATION}), now.date(), now)
    changed = [r for r in records if r["action"] in ("downloaded", "updated")]
    print(f"tides {STATION}: {len(changed)} half-year files downloaded or updated, "
          f"{sum(r['action'] == 'final' for r in records)} final, "
          f"{sum(r['action'] == 'unchanged' for r in records)} unchanged")
    for r in records:
        if r["action"] != "final":
            print(f"  {r['product']}/{r['file']}: {r['action']}" + (f" ({r['rows']:,} hours)" if "rows" in r else ""))


if __name__ == "__main__":
    main()
