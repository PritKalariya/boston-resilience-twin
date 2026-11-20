# Data manifest

## Purpose

Name exactly which versioned data one evening's forecast was built from. The sha256 of the manifest file is the live `data_version` that every other contract carries.

## Location

`gs://boston-resilience-twin-data/manifests/<as_of>.json` and `gs://boston-resilience-twin-data/manifests/latest.json`, project 1. `latest.json` is byte-identical to the newest dated file. The files it points to are in the DVC remote of the same bucket, `gs://boston-resilience-twin-data/dvc` (the `url` in the repo root's `.dvc/config`).

## Producer

#162 (the evening run), with the as-of date from #160 (the evening inputs). #162 writes all three entries, including `forecast_rows`, whose file #160 builds for the as-of date.

## Consumers

#167 (the data version checks) and #169 (monitoring, which joins grades to forecasts by `data_version`).

## Fields

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `contract` | string | `manifest/v1` | no | |
| `as_of` | string | `YYYY-MM-DD`, Boston | no | the evening the data was built for |
| `created_at_utc` | string | `YYYY-MM-DDTHH:MM:SSZ` | no | when the manifest was written |
| `vm_git_commit` | string | 40 lowercase hex | no | the commit checked out on the VM |
| `dag_run_id` | string | Airflow run id | no | the `daily_pipeline` run, e.g. `scheduled__2026-11-25T23:00:00+00:00` |
| `files.labels` | object | | no | `Data-Pipeline/data/processed/labels.parquet` |
| `files.features` | object | | no | `Data-Pipeline/data/processed/features.parquet` |
| `files.forecast_rows` | object | | no | `Data-Pipeline/data/forecast/<as_of>/features.parquet` (#160): tomorrow's rows, the ones sent to `/predict`, kept out of the processed `features.parquet` |
| `files.<name>.md5` | string | 32 lowercase hex | no | DVC's md5 of the file |
| `files.<name>.size_bytes` | integer | bytes | no | |
| `files.<name>.content_path` | string | `gs://<bucket>/<prefix>/files/md5/<first 2 hex>/<other 30 hex>` | no | the full URI of the object in the bucket: the DVC remote's URL plus DVC 3's cache layout |
| `feature_catalog_sha256` | string | 64 lowercase hex | no | sha256 of `Data-Pipeline/config/feature_catalog.csv` |
| `last_final_label_date` | string | `YYYY-MM-DD`, Boston | no | the newest day whose labels are final (48 hours after the day) |

## Rules

- **Canonical form.** `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, encoded as UTF-8, no trailing newline, **no floats anywhere** (sizes are integers, hashes are strings).
- The file is stored in canonical form, so the live `data_version` is `"sha256:" + sha256(file bytes)`. The same bytes give the same version on any machine, with no re-dump.
- `latest.json` is byte-identical to `<as_of>.json` of the newest `as_of`.
- The manifest never contains its own hash. A reader that needs `data_version` computes it from the bytes.
- `content_path` is the full URI, computed from `md5` and the remote URL in `.dvc/config` (`gs://boston-resilience-twin-data/dvc`): `gs://boston-resilience-twin-data/dvc/files/md5/ab/cdef...`. It is the same form for all three entries, and a test checks each against the config.
- `last_final_label_date` is at least two days before `as_of`.
- `forecast_rows` is required. A forecast made from rows that no manifest names cannot be traced, so #162 does not write a manifest without it.
- This `data_version` is **not** the training data version (the sha256 of `Data-Pipeline/dvc.lock` in the registry's `metadata.json`); see the README.

## Example

- [fixtures/manifests/2026-11-25.json](fixtures/manifests/2026-11-25.json)
- [fixtures/manifests/latest.json](fixtures/manifests/latest.json), byte-identical to the dated file: `cmp` prints nothing.
- [fixtures/manifests/data_version.txt](fixtures/manifests/data_version.txt), the version the fixture computes to; every other fixture's `data_version` is this value.

```bash
sha256sum docs/contracts/fixtures/manifests/latest.json
```

## Schema

[schemas/data-manifest.schema.json](schemas/data-manifest.schema.json)

## Versioning

`manifest/v1`. A new field, or any change to the canonical form, changes every `data_version` ever computed, so it is a breaking change and bumps the contract. Old manifests are never rewritten.

## Where it breaks

- **Non-canonical bytes.** Pretty-printed or re-dumped with a trailing newline, the file hashes to a different version than the one forecasts carry. Write it with the one dump above, from the same code path, and test the bytes.
- **A float sneaks in.** A float prints differently across libraries (`1e6`, `1000000.0`), so the bytes and the hash change. Use integers and strings only.
- **A rerun for the same `as_of`.** The manifest file is replaced, the version changes, and forecasts already written with the old version refer to a manifest that is gone. Rerun the whole run, so the forecast is rewritten with the new version.
- **`latest.json` and the dated file differ.** Write the dated file first and copy exactly its bytes, never re-serialize.
- **The DVC cache moves.** If the remote layout or the `dvc` pin changes (DVC 3.67.1 uses `files/md5/`), `content_path` stops pointing at an object. The test reads the remote URL but not the layout, so a change of DVC major version needs a new contract version.
- **Hash of the wrong file.** Hashing `dvc.lock` instead of this manifest gives the training version, which still looks valid.
