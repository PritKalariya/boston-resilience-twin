# `/predict`

## Purpose

Turn one evening's model inputs for up to 24 neighborhoods into surge probabilities for the next day. The evening run sends the rows; the model service answers with one prediction per row.

## Location

`POST /predict` on the model service (#161), Cloud Run in project 2. JSON in, JSON out. The request carries the live `data_version`, so the service echoes it back and never computes it. Documentation only: the server code is #161's.

## Producer

- Response: #161 (the model service).
- Request: #162 (the evening run) sends it; #160 builds the rows (the evening inputs).

## Consumers

#162 reads the response into the forecast file. #166 (the smoke test) sends the fixture request to a new revision before traffic moves.

## Fields

Both bodies carry `contract`: `"predict/v1"`.

**Request**

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `contract` | string | `predict/v1` | no | |
| `data_version` | string | `sha256:` + 64 hex | no | sha256 of the live manifest file (see README, global rules) |
| `rows` | array | 1 to 24 objects | no | one per neighborhood |

Each row:

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `unit` | string | one of the 24 neighborhoods | no | |
| `forecast_date` | string | `YYYY-MM-DD`, Boston | no | the day forecast |
| `as_of` | string | `YYYY-MM-DD`, Boston | no | the evening the run is made |
| the model inputs | see below | | see below | exactly the `use_in_model = yes` features of the catalog, one key each |

The model inputs are the features of [`Data-Pipeline/config/feature_catalog.csv`](../../Data-Pipeline/config/feature_catalog.csv) whose `use_in_model` is `yes`. The catalog has no `dtype` column, so the types below come from the code that builds each feature (`Data-Pipeline/scripts/feature_table.py` for the calendar and 311 columns, `alert_history.py` for the alerts) and from the missing-value table in `docs/feature-table.md`. A test fails if the schema's input names differ from the catalog's, read at test time.

| inputs | type | range | null |
|---|---|---|---|
| `day_of_week` (Monday is 0), `month`, `day_of_year` | integer | 0 to 6, 1 to 12, 1 to 366 | never |
| `alert_snow_ice_count`, `alert_tree_count`, `alert_flooding_count` | integer | 0 or more | never |
| `alert_snow_ice_warning`, `alert_tree_warning`, `alert_flooding_warning` | integer | 0 or 1 | never |
| `snow_ice_rel_last`, `snow_ice_rel3`, `snow_ice_rel7`, `snow_ice_rel28`, `tree_rel_last`, `tree_rel3`, `tree_rel7`, `tree_rel28` | number | 0 or more | yes: missing until the neighborhood has a baseline |
| `static_population`, `static_land_sqmi`, `static_density` | number | 0 or more | yes: Harbor Islands has no census values |
| `static_poverty_share`, `static_older_adult_share`, `static_no_vehicle_share` | number | 0 to 1 | yes: as above |
| `static_flood_10pct_share`, `static_flood_1pct_share` | number | 0 to 1 | never |
| `wx_precip_mm`, `wx_snowfall_mm`, `wx_gust_max_ms` | number | 0 or more | yes: a missing forecast or gust |
| `wx_tmax_c`, `wx_tmin_c` | number | -60 to 60 | yes: a missing forecast |
| `tide_pred_max_m` | number | metres | yes: blank unless all hourly predictions are there |

**How null was decided.** An input may be null only where the training data allows it to be missing: the catalog notes ("missing until the neighborhood has a baseline", "forecasts missing ...") and the missing-value table in `docs/feature-table.md` (weather, the 311 relative columns, the six census-based static columns of Harbor Islands, tide days with a missing hour). Calendar, alert and flood-share columns are never missing in training, so the service rejects a null there. What a model does with a null depends on the model (`Model-Development/scripts/models.py`), and the service passes the null through without filling it:

- **Logistic regression** (`models.py:51-56`, used at `:76` and `:78`) imputes. The log-scaled columns and the plain columns each go through `SimpleImputer(strategy="median", add_indicator=True)`: a null takes the median of the rows the model was fit on, and a column that had nulls in those rows also gets a 0 or 1 missing flag (an input never null in train gets no flag). The calendar columns (`day_of_week`, `month`, `day_of_year`) have no imputer (`:69`, `:72`), which is one more reason they are never null.
- **XGBoost** (`:216-232`) and **LightGBM** (`:235-253`) do not impute. Each tree takes the branch it learned for a missing value, and only for an input that had nulls in train. For an input never null in train there is no learned branch: LightGBM reads it as 0 and XGBoost takes a branch no training row used, as the comment in `training_data.py:361-362` says (`check_missing_gaps`, `:356`).
- **The seasonal baseline** (`:256-258`, `seasonal_baseline.py`) reads no model input. Its probability comes from the label history, so a null input does not change it.

`training_data.check_missing_gaps` stops a training run where an input never null in train is null on a scored row. This contract applies the same rule at the door: a null where the training data had none is a 422, because no model learned a treatment for it.

**Response** (200)

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `contract` | string | `predict/v1` | no | |
| `predictions` | array | 1 to 24 objects | no | one per request row, same order |

Each prediction:

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `unit` | string | neighborhood | no | |
| `date` | string | `YYYY-MM-DD`, Boston | no | the request row's `forecast_date` |
| `probability` | number | 0 to 1 | no | surge probability |
| `prediction` | integer | 0 or 1 | no | 1 when `probability >= threshold` |
| `threshold` | number | 0 to 1 | no | the shared validation-F1 threshold of the served model |
| `model_version` | string | Vertex version id, e.g. `"1"` | no | the served version |
| `model_run_id` | string | 32 lowercase hex, `^[0-9a-f]{32}$` | no | the MLflow run id of the run that trained the served version, read from `mlflow_run_id` in the `metadata.json` baked into the image |
| `data_version` | string | `sha256:` + 64 hex | no | echoed from the request |

`model_run_id` is in the response because the VM that calls the service cannot read project 2's registry, so it could not look the run up itself. It is not the `ci-<model>` folder name: that is the training run folder (`run_id` in the same `metadata.json`), and the registry keeps both. MLflow makes its run ids with `uuid4().hex`, so they are 32 lowercase hex characters (`registry.py` accepts a wider set of plain ids, and this contract takes the narrower one the real ids have).

**Status codes**

| code | when | body |
|---|---|---|
| 200 | the request is valid and a model is loaded | the response above |
| 422 | the body fails the request schema (FastAPI's default `{"detail": [...]}`) | not part of the contract |
| 503 | no model is loaded | not part of the contract |

## Rules

- Units are unique within a request.
- All rows share one `forecast_date` and one `as_of`, and `forecast_date = as_of + 1 day`.
- The response has one prediction per request row, in the request's order, and `date` equals the row's `forecast_date`.
- `prediction` is `1` exactly when `probability >= threshold`: at or above, as in `Model-Development/scripts/threshold.py` (`predicted = p >= threshold`). A probability equal to the threshold is a surge. The fixture has such a row.
- `data_version` in each prediction equals the request's.
- One model answers one request: every prediction has the same `model_version`, `model_run_id` and `threshold`.
- Probabilities are model output, not rounded by the contract.

## Example

- Request: [fixtures/predict/request.json](fixtures/predict/request.json), all 24 units, late-November values.
- Response: [fixtures/predict/response.json](fixtures/predict/response.json), threshold `0.096`, model version `"1"`.

## Schema

- [schemas/predict-request.schema.json](schemas/predict-request.schema.json)
- [schemas/predict-response.schema.json](schemas/predict-response.schema.json)

## Versioning

`predict/v1`. A new input, a renamed input or a changed type is a breaking change: the request lists exactly the catalog's inputs, so a retrained model that uses a different set needs a new version, announced in the PR that changes the catalog's `use_in_model` column. Adding a field to the response is breaking too (no extra fields in v1).

## Where it breaks

- **A feature leaves or joins the catalog.** The service and the evening run disagree on the column set, and every request is a 422. The contract test reads the catalog, so the PR that changes `use_in_model` fails here first.
- **Fewer than 24 rows.** Allowed (1 to 24), but the forecast file must have 24, so #162 must send all of them.
- **A null where none is allowed.** A 422 for the whole request, not the one row. The evening run should check before sending.
- **`data_version` is the training one.** It passes the pattern, since both are `sha256:` plus hex, and then joins to nothing. Take it from the live manifest.
- **A folder name as the run id.** `ci-lightgbm` is the training run folder, not the MLflow run id. It fails the pattern, so the response is rejected before it reaches the forecast file. Read `mlflow_run_id`, not `run_id`, from `metadata.json`.
- **A model that is loading.** 503 until a model is loaded. The evening run retries and then alerts; it never writes a forecast file without a response.
