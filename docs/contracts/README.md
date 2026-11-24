# Deployment contracts (#159)

The shared formats the Deployment components read and write. One page and one JSON Schema (draft 2020-12) per format, with synthetic fixtures that every schema and rule is tested against. A format changes only in its own PR, and a change that breaks a reader bumps its version.

| Contract | Page | Schema | Producer | Consumers |
|---|---|---|---|---|
| `/predict` | [predict.md](predict.md) | [request](schemas/predict-request.schema.json), [response](schemas/predict-response.schema.json) | #161 serves, #162 calls, #160 builds rows | #166 smoke test |
| Forecast file | [forecast-file.md](forecast-file.md) | [row](schemas/forecast-row.schema.json) | #162 | #169 #172 #173 #174 |
| Data manifest | [data-manifest.md](data-manifest.md) | [manifest](schemas/data-manifest.schema.json) | #162, as-of date from #160 | #167 #169 |
| Monitoring output | [monitoring-output.md](monitoring-output.md) | [monitoring](schemas/monitoring-output.schema.json) | #169 in #163's workflow | #170 #171 #173 |
| Retrain decision | [retrain-decision.md](retrain-decision.md) | [decision](schemas/retrain-decision.schema.json) | #170 | #163 #171 |
| Agent brief | [agent-brief.md](agent-brief.md) | [brief](schemas/agent-brief.schema.json) | #173 | #174 #172 |
| Metric names | [metric-names.md](metric-names.md) | [metric names](schemas/metric-names.schema.json) | #169 | #171 |

Every page has the same sections: Purpose, Location, Producer, Consumers, Fields, Rules, Example, Schema, Versioning, Where it breaks.

## Global rules

The schemas enforce the first five. The tests in [tests/test_contracts.py](tests/test_contracts.py) enforce the cross-field rules a schema cannot say.

- **Contract field.** Every JSON object at the top level of a contract file has `contract`: `"<name>/v1"`. The names are `predict/v1`, `forecast/v1`, `manifest/v1`, `monitoring/v1`, `retrain-decision/v1`, `brief/v1` and `metric-names/v1`. The forecast parquet carries `contract=forecast/v1` as file key-value metadata, not as a column. `fixtures/forecasts/<date>.records.json` exists only in this repo, as the reviewable source of the parquet fixture; it is a bare JSON array of rows, so it has no top-level object to put the field on, and the bucket never holds it. A breaking change bumps the version.
- **No extra fields.** `additionalProperties: false` on every object in v1. A reader never has to guess what an unknown field means.
- **Dates and times.** Dates are `YYYY-MM-DD` on the Boston calendar. Timestamps are UTC, `YYYY-MM-DDTHH:MM:SSZ`; a schema checks them with a pattern as well as `format: date-time`, because the format check needs a validator package the Airflow image does not have.
- **Units.** `unit` is a neighborhood name exactly as in [`Model-Development/config/poverty_groups.csv`](../../Model-Development/config/poverty_groups.csv): 24 values, an enum in every schema that has one. Harbor Islands is one of the 24 but has no poverty group (`ungrouped`), so it is a unit but in neither fairness group.
- **Data version.** `data_version` is `"sha256:"` plus 64 lowercase hex characters: the sha256 of the bytes of the live manifest file (`manifests/<as_of>.json`). This is **not** the training data version. The training data version is the sha256 of `Data-Pipeline/dvc.lock`, kept in the registry's `metadata.json` (`data_version`), which `model_version` leads to. Both start with `sha256:`, so never compare one with the other: the live one says which data a forecast was made from, the training one says which data the model was fitted on.
- **Model version and run id.** `model_version` is the Vertex AI Model Registry version id, a decimal string such as `"1"` (the `modelVersionId` the registry client returns as `str`). It is not the `surge-forecast@1` reference that `registry.py` also prints. `model_run_id` is the MLflow run id of the run that trained that version: 32 lowercase hex characters, `^[0-9a-f]{32}$`, the form MLflow gives (`mlflow_run_id` in the registry's `metadata.json`). `/predict` returns it, because the VM cannot read project 2's registry. It is not `ci-<model>`, which is the training run folder.
- **A metric that cannot be computed** is `{"value": null, "reason": "<reason>"}`. Otherwise it is `{"value": <number>, "reason": null}`. Never `0` for a metric with no data, and never a bare `null`. The reasons are few:

  | reason | meaning |
  |---|---|
  | `insufficient data` | fewer than 5 positives (surge days, after final labels) in the window or the poverty group, zero included, even when nothing is graded yet |
  | `no data` | only two cases: drift with no earlier rows to compare against, and the brief's `model_health` before the first monitoring file exists. Never a performance or fairness metric in a monitoring file |
  | `undefined` | enough data, but the formula has a zero denominator, for example precision when nothing was flagged |

- **Where files live.** Everything except `/predict` is in the data bucket `gs://boston-resilience-twin-data` in project 1. `/predict` is the model service on Cloud Run in project 2.

## Fixtures

The fixtures in [fixtures/](fixtures) are **synthetic**. No real forecast, label or model produced them, and the numbers mean nothing. They mirror the bucket layout, so a consumer can copy the folder and read it as if it were the bucket. They use the threshold `0.096` and `model_version` `"1"`. Every fixture that has a `data_version` carries the same one, the sha256 of [`fixtures/manifests/latest.json`](fixtures/manifests/latest.json), so a consumer can test a join across contracts. [`tools/build_fixtures.py`](tools/build_fixtures.py) keeps the derived files (the parquet files, both manifests and `latest.json` copies, `data_version.txt` and every `data_version`) in line with the sources; see "Changing a contract". The one exception to the bucket layout is [`fixtures/forecast-rows/2026-11-25/features.parquet`](fixtures/forecast-rows/2026-11-25/features.parquet): the forecast rows file lives in DVC (`Data-Pipeline/data/forecast/<as_of>/features.parquet`, named by the manifest's `files.forecast_rows`), not at the bucket's top level. The tool builds it from `predict/request.json` with the declared Arrow types ([predict.md](predict.md#the-forecast-rows-file)), and `--check` compares it by schema and values. The manifest fixture's synthetic `forecast_rows` md5 is not tied to it, since parquet bytes change with the pyarrow version. Each fixture is consistent in itself, but the files are not one history: the 2026-11-27 and 2026-12-03 monitoring files, for example, are two separate examples (sparse and rich) of one model version.

## Tests

The tests use only `jsonschema` and `pyarrow`, which the Airflow image already has. CI runs them in their own step, `Contract tests (#159)`, right after the main test step in the same job:

```bash
docker compose run --rm test docs/contracts/tests --no-cov
```

On the host, `python -m pytest docs/contracts/tests` works in any environment that has `jsonschema` and `pyarrow`. The `test` service mounts `docs/contracts` read-only.

## Changing a contract

1. Edit the schema in [schemas/](schemas), the page, and the source fixtures: the JSON fixtures and `fixtures/forecasts/<date>.records.json`. Never edit a derived file by hand: a parquet file, `manifests/latest.json`, `manifests/data_version.txt`, `monitoring/decisions/latest.json`, or any `data_version`.
2. Rebuild the derived files. `--check` writes nothing and exits 1, naming each file that would change; without it the tool rewrites them. The `test` service mounts `docs/contracts` read-only, and a second `-v` on the same path does not override it, so the rewrite mounts the fixtures folder at another path and points the tool at it:

   ```bash
   docker compose run --rm --entrypoint python test docs/contracts/tools/build_fixtures.py --check
   docker compose run --rm --user "$(id -u):0" -v "$PWD/docs/contracts/fixtures:/work/fixtures" \
     --entrypoint python test docs/contracts/tools/build_fixtures.py --fixtures /work/fixtures
   ```

   Any environment with `pyarrow` can run `python docs/contracts/tools/build_fixtures.py` directly. A changed manifest changes `data_version` everywhere; that is expected.
3. Run the tests: `docker compose run --rm test docs/contracts/tests --no-cov`.
4. If a reader breaks (a removed or retyped field, a new required field, a changed rule), bump the version in the schema's `contract` const, the page, the fixtures and the tests, and say so in the PR.
