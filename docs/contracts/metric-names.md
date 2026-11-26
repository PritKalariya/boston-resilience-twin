# Metric names

## Purpose

The Cloud Monitoring custom metrics the system writes, so the writer and the dashboards and alert policies agree on names, kinds, units and labels.

## Location

Cloud Monitoring in project 1. Every metric type is the prefix `custom.googleapis.com/brt/` plus the name, for example `custom.googleapis.com/brt/f1`. The descriptors are in [fixtures/metrics/metric-names.json](fixtures/metrics/metric-names.json); #171 creates the metric descriptors from them.

## Producer

#169 (the monitoring workflow, in #163's scheduled workflow) writes every metric, `forecast_freshness_hours` included. The metrics are written as `ci-writer`, which needs `roles/monitoring.metricWriter` on project 1, granted under #169.

## Consumers

#171 (dashboards and alert policies).

## Fields

The file is `{contract, prefix, metrics: [descriptor]}`. Each descriptor:

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `name` | string | one of the ten below | no | appended to the prefix |
| `metric_kind` | string | `GAUGE` | no | all are gauges, one point per run |
| `value_type` | string | `DOUBLE` or `INT64` | no | |
| `unit` | string | UCUM, `1` or `h` | no | `1` for a ratio or a count |
| `labels` | array | `{key, allowed_values}` | empty array when none | a label's values are a closed list |
| `producer` | string | `#<issue>` | no | |
| `written_when` | string | | no | |

| metric | type | unit | labels | producer | source field |
|---|---|---|---|---|---|
| `f1` | DOUBLE | `1` | `window`: `7d`, `30d` | #169 | `windows.<w>.f1.value` of the [monitoring output](monitoring-output.md) |
| `precision` | DOUBLE | `1` | `window`: `7d`, `30d` | #169 | `windows.<w>.precision.value` |
| `recall` | DOUBLE | `1` | `window`: `7d`, `30d` | #169 | `windows.<w>.recall.value` |
| `positives` | INT64 | `1` | `window`: `7d`, `30d` | #169 | `windows.<w>.positives` |
| `fairness_gap` | DOUBLE | `1` | none | #169 | `fairness.gap.value` (the recall gap, 30 days) |
| `group_fpr` | DOUBLE | `1` | `group`: `lower`, `higher` | #169 | `fairness.groups.<g>.fpr.value` (30 days) |
| `fpr_gap` | DOUBLE | `1` | none | #169 | `fairness.fpr_gap.value` (30 days) |
| `drift_share` | DOUBLE | `1` | none | #169 | `drift.share.value` |
| `retrain_decision` | INT64 | `1` | `source`: `live`, `replay`, `manual` | #169 | `retrain` of the [retrain decision](retrain-decision.md): 1 or 0 |
| `forecast_freshness_hours` | DOUBLE | `h` | none | #169 | hours from the latest forecast file's `issued_at_utc` to the monitor run's time |

`f1`, `precision` and `recall` carry the `window` label as well as `positives`, because the monitoring output has both windows and two points of one metric type with the same labels in the same minute would collide. `group_fpr` carries the `group` label for the same reason: its two groups are two points. The `group` label's values are the group keys of [`Model-Development/config/poverty_groups.csv`](../../Model-Development/config/poverty_groups.csv) (`lower` and `higher`; `ungrouped` is no group and writes no point), and a test reads them from the file.

## Rules

- **A null metric writes no point.** A value of `null` in the monitoring output (`insufficient data`, `no data`, `undefined`) is not written as 0 or as a missing-value marker: there is simply no point for that run.
- **Alert policies must treat a missing point explicitly.** A policy on `f1`, `fairness_gap`, `group_fpr` or `fpr_gap` sees no data on sparse days. Each alert condition must set what missing data means (Cloud Monitoring's `evaluationMissingData`: inactive, active or no-op) and not leave it to the default, and a separate absence condition on `positives` and `forecast_freshness_hours` catches a monitor that stopped, and a threshold condition on `forecast_freshness_hours` catches a missed evening.
- `positives` is always written, with 0 when the window has no final positives, so its absence means the monitor did not run.
- `forecast_freshness_hours` is written on every monitoring run, from the newest file in `forecasts/`. A missed evening writes no new forecast file, so the next monitor run reports a larger value instead of no point. Before the first forecast file exists there is nothing to measure, and no point is written.
- `retrain_decision` is written after #170 writes the day's decision file; its `source` label is the decision's `source`.
- Names are the exact strings of the table; a new metric is a new descriptor in this contract first.

## Example

[fixtures/metrics/metric-names.json](fixtures/metrics/metric-names.json)

## Schema

[schemas/metric-names.schema.json](schemas/metric-names.schema.json)

## Versioning

`metric-names/v1`. Amended in v1 on 2026-11-24, before any monitoring file was written, to add the false-positive rate (#159, PR #195). A renamed metric or label leaves old points under the old name and starts a new series, so dashboards break silently; that is breaking. A new allowed label value is also breaking, since a policy that filters on the list misses it.

## Where it breaks

- **No point on sparse days.** Charts show gaps and alert policies with the wrong no-data setting never fire (or always fire). See the rules above.
- **Two windows without the label.** Two points for the same metric and time with the same labels: one is dropped by Cloud Monitoring. The `window` label prevents it.
- **Metric descriptor drift.** A metric type's labels and value type are fixed at its first write. A descriptor that disagrees with this file rejects the write.
- **Wrong project.** The metrics live in project 1; writing them from a project 2 service creates them there.
- **A missed evening.** The monitor runs on its own schedule, so it still writes `forecast_freshness_hours`, and the value grows. A policy that waits for an absent point never sees a missed evening; use the threshold condition.
