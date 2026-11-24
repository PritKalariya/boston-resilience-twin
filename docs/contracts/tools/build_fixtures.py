"""Rebuild the contract fixtures that are derived from other fixtures (#159).

The sources, which a person edits: every JSON fixture and forecasts/<date>.records.json.
The derived files, which this tool rewrites:
  - forecasts/<date>.parquet, from the records, with the declared Arrow schema and the
    key-value metadata contract=forecast/v1
  - forecast-rows/<as_of>/features.parquet, from predict/request.json: the request's rows with the declared
    Arrow types (predict.md, "The forecast rows file"), no key-value metadata
  - manifests/<as_of>.json in canonical form, and manifests/latest.json as a copy of the newest
  - manifests/data_version.txt: "sha256:" plus the sha256 of the newest manifest's bytes
  - data_version in every fixture that carries one: that value
  - monitoring/decisions/latest.json, as a copy of the newest dated decision
Every other JSON fixture is written as json.dumps(obj, indent=2, ensure_ascii=False) + "\\n".

  python docs/contracts/tools/build_fixtures.py          rewrite what changed
  python docs/contracts/tools/build_fixtures.py --check  write nothing, exit 1 and name each file that would change

Only the standard library and pyarrow, so it runs in the Airflow test image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SHA = re.compile(r"sha256:[0-9a-f]{64}")
FORECAST_SCHEMA = pa.schema([
    ("unit", pa.string()), ("forecast_date", pa.date32()), ("as_of", pa.date32()), ("probability", pa.float64()),
    ("prediction", pa.int8()), ("threshold", pa.float64()), ("model_version", pa.string()),
    ("model_run_id", pa.string()), ("data_version", pa.string()), ("issued_at_utc", pa.timestamp("us", tz="UTC"))])
PARQUET_METADATA = {"contract": "forecast/v1"}
# The forecast rows file (predict.md): the never-null integer inputs are int64, every other input float64.
ROWS_INT_COLUMNS = ("day_of_week", "month", "day_of_year", "alert_snow_ice_count", "alert_snow_ice_warning",
                    "alert_tree_count", "alert_tree_warning", "alert_flooding_count", "alert_flooding_warning")
ROWS_KEYS = ("unit", "forecast_date", "as_of")


def canonical(obj) -> bytes:
    """The manifest's one dump (data-manifest.md, Rules): the bytes whose sha256 is the data_version."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pretty(obj) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def stamp_data_version(node, version: str):
    """node with every data_version string replaced by version, in place; returns node."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "data_version" and isinstance(value, str) and SHA.fullmatch(value):
                node[key] = version
            else:
                stamp_data_version(value, version)
    elif isinstance(node, list):
        for value in node:
            stamp_data_version(value, version)
    return node


def records_to_table(records: list[dict]) -> pa.Table:
    columns = {name: [] for name in FORECAST_SCHEMA.names}
    for row in records:
        for name in FORECAST_SCHEMA.names:
            value = row[name]
            if name in ("forecast_date", "as_of"):
                value = date.fromisoformat(value)
            elif name == "issued_at_utc":
                value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            columns[name].append(value)
    table = pa.table(columns, schema=FORECAST_SCHEMA)
    return table.replace_schema_metadata(PARQUET_METADATA)


def rows_schema(columns: list[str]) -> pa.Schema:
    fields = []
    for name in columns:
        if name == "unit":
            kind = pa.string()
        elif name in ("forecast_date", "as_of"):
            kind = pa.date32()
        elif name in ROWS_INT_COLUMNS:
            kind = pa.int64()
        else:
            kind = pa.float64()
        fields.append((name, kind))
    return pa.schema(fields)


def request_to_rows_table(request: dict) -> pa.Table:
    """The request's rows as the forecast rows file: the same columns in the same order, one row per unit sorted
    by unit."""
    rows = sorted(request["rows"], key=lambda row: row["unit"])
    schema = rows_schema(list(rows[0]))
    columns = {name: [] for name in schema.names}
    for row in rows:
        for name in schema.names:
            value = row[name]
            columns[name].append(date.fromisoformat(value) if name in ("forecast_date", "as_of") else value)
    return pa.table(columns, schema=schema)


def _dated(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("*.json") if p.name != "latest.json")


def planned_text_files(fixtures: Path) -> dict[Path, bytes]:
    """Every derived or normalised non-parquet file: path -> the bytes it should hold."""
    manifests = _dated(fixtures / "manifests")
    if not manifests:
        sys.exit("no manifest fixture in manifests/")
    plan: dict[Path, bytes] = {}
    for path in manifests:
        plan[path] = canonical(json.loads(path.read_text(encoding="utf-8")))
    newest = plan[manifests[-1]]
    version = "sha256:" + hashlib.sha256(newest).hexdigest()
    plan[fixtures / "manifests" / "latest.json"] = newest
    plan[fixtures / "manifests" / "data_version.txt"] = version.encode("utf-8")

    decisions = fixtures / "monitoring" / "decisions"
    dated_decisions = _dated(decisions)
    for path in dated_decisions:
        plan[path] = pretty(json.loads(path.read_text(encoding="utf-8")))
    if dated_decisions:
        plan[decisions / "latest.json"] = plan[dated_decisions[-1]]

    for path in sorted(fixtures.rglob("*.json")):
        if path in plan or path.parent.name == "manifests":
            continue
        plan[path] = pretty(stamp_data_version(json.loads(path.read_text(encoding="utf-8")), version))
    return plan


def planned_parquet(fixtures: Path, version: str) -> dict[Path, pa.Table]:
    tables = {}
    for records_path in sorted((fixtures / "forecasts").glob("*.records.json")):
        records = stamp_data_version(json.loads(records_path.read_text(encoding="utf-8")), version)
        tables[records_path.with_name(records_path.name.removesuffix(".records.json") + ".parquet")] = \
            records_to_table(records)
    request_path = fixtures / "predict" / "request.json"
    if request_path.is_file():
        request = json.loads(request_path.read_text(encoding="utf-8"))
        as_of = request["rows"][0]["as_of"]
        tables[fixtures / "forecast-rows" / as_of / "features.parquet"] = request_to_rows_table(request)
    return tables


def parquet_matches(path: Path, table: pa.Table) -> bool:
    """Same schema, key-value metadata and values. The bytes also hold the writer's version, so not compared."""
    if not path.is_file():
        return False
    return pq.read_table(path).equals(table, check_metadata=True)


def build(fixtures: Path, check: bool) -> list[Path]:
    """The files that differ from what the sources say; writes them unless check."""
    plan = planned_text_files(fixtures)
    version = plan[fixtures / "manifests" / "data_version.txt"].decode("utf-8")
    changed: list[Path] = []
    for path, data in plan.items():
        if not path.is_file() or path.read_bytes() != data:
            changed.append(path)
            if not check:
                path.write_bytes(data)
    for path, table in planned_parquet(fixtures, version).items():
        if not parquet_matches(path, table):
            changed.append(path)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                pq.write_table(table, path)
    return sorted(changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if a file would change")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="the fixtures folder")
    args = parser.parse_args(argv)
    fixtures = args.fixtures.resolve()
    changed = build(fixtures, args.check)
    names = [p.relative_to(fixtures).as_posix() for p in changed]
    if args.check:
        if names:
            print("would change:\n  " + "\n  ".join(names))
            return 1
        print("fixtures are up to date")
        return 0
    print("rewrote:\n  " + "\n  ".join(names) if names else "nothing to rewrite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
