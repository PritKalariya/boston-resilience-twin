# Monitoring output

## Purpose

The daily record of how the served model is doing: accuracy over the last 7 and 30 days against final labels, the recall gap between poverty groups, and drift. The retrain rule, the dashboards and the agent read it.

## Location

`gs://boston-resilience-twin-data/monitoring/<date>.json`, project 1. `<date>` is the Boston date of the monitoring run.

## Producer

#169, in #163's scheduled GitHub workflow.

## Consumers

#170 (the retrain rule), #171 (dashboards and alert policies, through the metrics in [metric-names.md](metric-names.md)) and #173 (the agent copies model health into the brief).

## Fields

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `contract` | string | `monitoring/v1` | no | |
| `date` | string | `YYYY-MM-DD`, Boston | no | the monitoring run's date |
| `model_version` | string | Vertex version id | no | the version graded |
| `data_version` | string | `sha256:` + 64 hex | no | the live manifest's sha256 for the newest forecast graded |
| `windows.7d`, `windows.30d` | object | | no | graded forecasts, final labels only |
| `windows.<w>.start`, `windows.<w>.end` | string | `YYYY-MM-DD`, Boston | no | the first and last forecast date of the window, both inclusive. `end` is the newest forecast date whose labels are final when the monitor runs; both windows of one file share it |
| `windows.<w>.positives` | integer | surge neighborhood-days | no | `tp + fn` |
| `windows.<w>.tp`, `fp`, `fn` | integer | neighborhood-days | no | counts at the forecast's own threshold |
| `windows.<w>.f1`, `precision`, `recall` | metric object | 0 to 1 | value null when `positives < 5` or undefined | |
| `fairness.groups.higher`, `fairness.groups.lower` | object | | no | the poverty groups of `poverty_groups.csv`; Harbor Islands is in neither |
| `fairness.groups.<g>.positives` | integer | surge neighborhood-days, 30 days | no | |
| `fairness.groups.<g>.recall` | metric object | 0 to 1 | value null when the group has `positives < 5` | 30-day recall of the group |
| `fairness.gap` | metric object | 0 to 1 | value null when either recall is null | absolute difference of the two group recalls |
| `drift.share` | metric object | 0 to 1 | value null with no earlier rows to compare | `len(drifted_features)` divided by the number of `use_in_model` columns in the feature catalog: the share of model inputs that drifted |
| `drift.drifted_features` | array of strings | catalog input names | empty when none | a subset of the catalog's `use_in_model` columns, in catalog order |
| `drift.prediction_drift.detected` | boolean | | no | |
| `drift.prediction_drift.score` | number | method-specific | yes | null when the method could not run |
| `drift.prediction_drift.method` | string | | no | the test used, e.g. `wasserstein` |

A metric object is `{"value": <number>, "reason": null}` or `{"value": null, "reason": "<reason>"}`. The reasons are in the README's global rules.

## Rules

- Only **final** labels count (48 hours after the day). A window with no final positives has `positives: 0`, and its metrics are `null` with `"insufficient data"`, even when nothing is graded yet.
- **Windows.** `end` is the newest forecast date whose labels are final when the monitor runs, so `end <= date - 2 days`. The 7-day window is the 7 forecast dates ending at `end` inclusive (`start = end - 6 days`) and the 30-day window is the 30 dates ending at `end` (`start = end - 29 days`). Both windows of one file have the same `end`. The fairness groups use the 30-day window.
- `positives = tp + fn` in every window; the 30-day window holds at least as many positives as the 7-day one.
- **Fewer than 5 positives** in a window or group, zero included: the metric's value is `null` and its reason is `"insufficient data"`. Never `0`. No performance or fairness metric ever carries `"no data"`; that reason is only for drift with no earlier rows to compare (see the README).
- `f1 = 2tp / (2tp + fp + fn)`, `precision = tp / (tp + fp)`, `recall = tp / (tp + fn)`, as in `Model-Development/scripts/threshold.py`. With enough positives but a zero denominator (no flags, for precision), the metric is `null` with `"undefined"`.
- `fairness.gap = |recall(lower) - recall(higher)|`, over the 30-day window. If either group recall is null, the gap is null with `"insufficient data"`.
- The sum of the groups' positives is at most the 30-day window's (an ungrouped unit counts in the window but in no group).
- `drifted_features` is a subset of the catalog's `use_in_model` columns, with no repeats.
- A null metric writes **no Cloud Monitoring point** (see [metric-names.md](metric-names.md)).

## Example

- [fixtures/monitoring/2026-11-27.json](fixtures/monitoring/2026-11-27.json): sparse. No final labels yet, so every performance and fairness metric is null with `insufficient data` (zero positives), and drift has nothing to compare (`no data`).
- [fixtures/monitoring/2026-12-03.json](fixtures/monitoring/2026-12-03.json): enough positives, a recall gap between the groups, and some drift.

## Schema

[schemas/monitoring-output.schema.json](schemas/monitoring-output.schema.json)

## Versioning

`monitoring/v1`. A new metric or window is a new field, which no-extra-fields makes breaking. A change to a window length, to how `end` is chosen, or to the 5-positive rule changes what the numbers mean without changing the shape: that needs a new version too, because #170's rule reads them.

## Where it breaks

- **Sparse start.** In the first days after go-live nothing is final, and every performance and fairness metric is null with `insufficient data`. #170 must read a null as "no decision on this metric", never as 0, and #171's alert policies must handle the missing point.
- **A rerun for the same date** overwrites the file. #170's decision for that date may have read the old numbers; rerun the decision after the monitoring output.
- **A late label correction.** Labels are final after 48 hours, but the 311 data can still change. The output is a snapshot of what was final at `date`; it is not recomputed afterwards.
- **A group with few positives.** With 5 positives a recall moves by 0.2 per surge day, so the gap swings. The rule that reads it, and its thresholds, are #170's.
- **Two model versions in the window.** After a promotion, forecasts from the old version are still in the 30-day window. `model_version` names the version of the newest forecast, and the windows grade each forecast against its own threshold.
- **Evidently fails.** A tool error is a workflow failure, not a null: `null` with `"no data"` is only for drift with no earlier rows to compare.
