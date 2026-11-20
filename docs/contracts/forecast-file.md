# Forecast file

## Purpose

The record of one evening's forecast: 24 rows, one per neighborhood, for one forecast date. Grading, drift checks, the agent brief and the map all read it.

## Location

`gs://boston-resilience-twin-data/forecasts/<forecast_date>.parquet`, project 1. Beside it: `forecasts/<forecast_date>.brief.json` (the [agent brief](agent-brief.md)). The bucket holds no other forecast file for the date.

## Producer

#162, the evening run, from the `/predict` response and the evening's manifest.

## Consumers

#169 (grading and monitoring), #172 (the map), #173 (the agent), #174 (the critic and the brief).

## Fields

Columns and Arrow types, in this order. `contract=forecast/v1` is parquet file key-value metadata, not a column.

| name | Arrow type | unit or format | null | meaning |
|---|---|---|---|---|
| `unit` | string | one of the 24 neighborhoods | no | |
| `forecast_date` | date32 | Boston calendar | no | the day forecast |
| `as_of` | date32 | Boston calendar | no | the evening the forecast was made; `forecast_date - 1 day` |
| `probability` | float64 | 0 to 1 | no | surge probability |
| `prediction` | int8 | 0 or 1 | no | 1 when `probability >= threshold` |
| `threshold` | float64 | 0 to 1 | no | the served model's shared threshold |
| `model_version` | string | Vertex version id | no | e.g. `"1"` |
| `model_run_id` | string | 32 lowercase hex | no | the MLflow run id of the run that trained that version, copied by #162 from the `/predict` response (see [predict.md](predict.md)) |
| `data_version` | string | `sha256:` + 64 hex | no | sha256 of the live manifest file |
| `issued_at_utc` | timestamp[us, tz=UTC] | UTC | no | when #162 wrote the file |

`prediction` is `int8` because `Model-Development/scripts/predictions.py` (`make_predictions`) writes it as `int8`, and the forecast file follows it so a model's prediction frame and the forecast file compare without a cast.

`records.json` exists only in this repo, as the reviewable source of the parquet fixture. It is a JSON array of 24 objects with the same names, dates as `YYYY-MM-DD` strings and `issued_at_utc` as `YYYY-MM-DDTHH:MM:SSZ`. #162 never writes it: the bucket has the parquet only.

## Rules

- 24 rows, sorted by `unit` (plain string order), each unit once.
- All rows share one `forecast_date`, one `as_of`, one `model_version`, one `data_version` and one `issued_at_utc`; `forecast_date = as_of + 1 day`.
- `prediction = 1` exactly when `probability >= threshold` (at or above, as `threshold.py`).
- `data_version` equals the sha256 of `manifests/<as_of>.json` in the same bucket.
- The parquet fixture equals its `records.json` source row for row. A test reads both and compares; `tools/build_fixtures.py` rebuilds the parquet from the records.
- A rerun for the same date **replaces** the file: last write wins.

## Example

- [fixtures/forecasts/2026-11-26.parquet](fixtures/forecasts/2026-11-26.parquet)
- [fixtures/forecasts/2026-11-26.records.json](fixtures/forecasts/2026-11-26.records.json)

The fixture has one unit exactly on the threshold (prediction 1) and one just under it (prediction 0).

## Schema

[schemas/forecast-row.schema.json](schemas/forecast-row.schema.json) describes one row of the parquet as JSON (the rows of `records.json`). The Arrow schema above is checked directly by the test, and the key-value metadata with it.

## Versioning

`forecast/v1`. Adding, removing, renaming or retyping a column bumps it, and the new files carry `contract=forecast/v1` replaced by the new value. Readers check the metadata before they read columns.

## Where it breaks

- **A rerun replaces the file.** The first run's grades, brief and map read the old rows, and the new rows can differ (a later `issued_at_utc`, a different model or data version). The bucket has object versioning (`Deployment/terraform/storage.tf`), so the replaced file stays as a noncurrent generation for 30 days, but no reader sees it unless it asks for a generation. Anything that cached a forecast must key on `issued_at_utc` and `data_version`, not only the date. A rerun after grading started can make a grade refer to rows that no longer exist.
- **Fewer or more than 24 rows.** A unit missing from `/predict` leaves a hole: #162 must fail the run instead of writing 23 rows.
- **A reader that drops the metadata.** pandas `to_parquet` round trips can lose the key-value metadata. Readers that rewrite the file must keep it.
- **A probability equal to the threshold.** Readers must use `>=`; a reader that recomputes `prediction` with `>` disagrees on that row.
