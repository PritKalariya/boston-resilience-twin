# Agent brief

## Purpose

The daily brief for the person who reviews the forecast: which neighborhoods are flagged, which inputs go with each flag, how the model is doing, and a short text. Every number in the text is a claim with a source, so a deterministic critic can check it. Advisory only: a person reviews every brief.

## Location

`gs://boston-resilience-twin-data/forecasts/<forecast_date>.brief.json`, project 1, beside the [forecast file](forecast-file.md).

## Producer

#173, the agent service (Gemini on Vertex AI writes the text; the claims and fields are built from files, not by the model).

## Consumers

#174 (the critic that checks the claims) and #172 (the map shows the brief).

## Fields

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `contract` | string | `brief/v1` | no | |
| `forecast_date` | string | `YYYY-MM-DD`, Boston | no | the day the forecast is for |
| `model_version` | string | Vertex version id | no | from the forecast file |
| `data_version` | string | `sha256:` + 64 hex | no | from the forecast file |
| `flagged` | array | | empty when nothing is flagged | the units with `prediction` 1, most probable first |
| `flagged[].unit` | string | neighborhood | no | |
| `flagged[].probability` | number | 0 to 1 | no | from the forecast file |
| `flagged[].associations` | array | | may be empty | model inputs that go with the flag |
| `flagged[].associations[].feature` | string | a `use_in_model` input | no | |
| `flagged[].associations[].value` | number | the input's own unit | no | the input's value for that unit and day, from the forecast rows file |
| `model_health` | object | | no | `f1`, `recall` and `fairness_gap`, copied from the latest monitoring output |
| `model_health.<name>` | metric object | 0 to 1 | value null when the source's is | the same object as in the monitoring file; `no data` when there is no monitoring file yet |
| `text` | string | | no | the brief for a human |
| `claims` | array | | no | one per number in `text` |
| `claims[].id` | string | `c1`, `c2`, ... | no | unique in the brief |
| `claims[].value` | number | | no | the source value, unrounded |
| `claims[].display` | string | decimal, e.g. `0.19` | no | the value as shown in `text` |
| `claims[].source.file` | string | full `gs://boston-resilience-twin-data/...` URI, see below | no | where the value comes from |
| `claims[].source.field` | string | column name or dotted path | no | |
| `claims[].source.row` | object `{unit, date}` or null | | yes | the row for a column; null for a path into a JSON file |

**Inputs are associations, not causes.** `associations` lists the inputs that go with a flag, such as a high recent tree-request count or a strong gust forecast. The model learned correlations; nothing in the brief says an input caused the surge. The text must say so, and the map must not word it as a cause.

**Allowed source files.** `source.file` is always a full URI in the data bucket, never a bucket-relative path. Three kinds:

- the forecast file, `gs://boston-resilience-twin-data/forecasts/<forecast_date>.parquet`: `field` is a column, `row` is `{unit, date: forecast_date}`.
- a monitoring file, `gs://boston-resilience-twin-data/monitoring/<date>.json`: `field` is a dotted path such as `windows.30d.f1.value`, `row` is null.
- the forecast rows file, the manifest's `files.forecast_rows.content_path` of the manifest the forecast was built from (a `gs://boston-resilience-twin-data/dvc/files/md5/...` URI): `field` is the input column, `row` is `{unit, date}`, where `date` matches the file's `forecast_date` column. Its columns are in [predict.md](predict.md#the-forecast-rows-file).

Any other file is not allowed. Input (association) claims cite the forecast rows file, never the processed features file: the rows for the forecast day are not in it.

## Rules

The critic rule, as #174 implements it, for every claim:

1. `value` equals the source value **exactly**, not rounded.
2. `display` equals `value` rounded to the decimals shown: with `d` digits after the point in `display` (0 when there is no point), `display == f"{value:.{d}f}"`.
3. `display` appears in `text` as a whole number: not inside a longer number or a word (`0.1` does not match in `0.14`, and `1` does not match in `14.8`).

Also:

- Claim ids are unique.
- Every association has a claim for the same unit, input and value, citing the forecast rows file.
- An input whose value is null for the unit and day is never an association: #173 skips it. `associations[].value` is a number, never null.
- `flagged` equals the forecast file's rows with `prediction` 1, ordered by `probability` descending and then `unit`; `flagged[].probability` equals the file's.
- `model_health` is copied from the latest monitoring output at the time the brief is made. With no monitoring output yet (the first evening) each of its three metrics is `{"value": null, "reason": "no data"}`.
- `data_version` and `model_version` equal the forecast file's.
- The v1 rule does not check that `text` has no other numbers; #174 may add that.

## Example

[fixtures/forecasts/2026-11-26.brief.json](fixtures/forecasts/2026-11-26.brief.json): three flagged neighborhoods, one at exactly the threshold. The test maps each `gs://` source to a fixture by stripping the bucket prefix: claims on the forecast file are checked against the forecast fixture, claims on a monitoring file against the monitoring fixture, and claims on the forecast rows file against [fixtures/forecast-rows/2026-11-25/features.parquet](fixtures/forecast-rows/2026-11-25/features.parquet) (the same rows as `fixtures/predict/request.json`), by `unit`, column and `forecast_date`. A claim left unchecked fails the test.

## Schema

[schemas/agent-brief.schema.json](schemas/agent-brief.schema.json)

## Versioning

`brief/v1`. A new field is breaking (no extra fields in v1). A looser critic rule, such as allowing a rounded `value`, is breaking for #174 and bumps the version.

## Where it breaks

- **The model rounds a number.** The text shows `0.19` for a claim whose `display` is `0.187`. Rule 3 fails and the brief is rejected. The text is written from `display` strings, not free-form numbers.
- **A number in the text with no claim.** v1 does not catch it; a reader trusts the text more than it should. #174 decides whether to add it.
- **A rerun of the forecast.** The brief is stale against the new forecast file. The brief carries `data_version` and `model_version`, so a reader can see the mismatch, and #173 reruns after #162 does.
- **Monitoring is missing.** Before the first graded day there is no file to copy; the `no data` metrics say so, instead of a made-up number.
- **A source path that moves.** A claim that points at a monitoring field path breaks when the monitoring contract changes. The critic reads the file at the claim's `source.file`, so the path is checked, not trusted.
- **Causal wording.** The associations are not causes; a brief that says "because" about an input is wrong even if every number is right, and the critic does not catch it.
