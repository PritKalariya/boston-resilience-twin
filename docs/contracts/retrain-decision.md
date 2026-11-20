# Retrain decision

## Purpose

The drift-threshold rule's answer for one day: retrain or not, and why. It is what the workflow acts on and what the dashboards show.

## Location

`gs://boston-resilience-twin-data/monitoring/decisions/<date>.json` and `gs://boston-resilience-twin-data/monitoring/decisions/latest.json`, project 1. `latest.json` is byte-identical to the newest dated file.

## Producer

#170 (the retraining trigger).

## Consumers

#163 (the workflow that dispatches the retrain) and #171 (dashboards and alerts, through the `retrain_decision` metric).

## Fields

| name | type | unit or format | null | meaning |
|---|---|---|---|---|
| `contract` | string | `retrain-decision/v1` | no | |
| `date` | string | `YYYY-MM-DD`, Boston | no | the decision's date; equals the monitoring output it read |
| `retrain` | boolean | | no | whether to retrain |
| `reasons` | array of strings | `f1_decay`, `drift_persistent`, `fairness_gap`, `manual` | empty when `retrain` is false | why |
| `source` | string | `live`, `replay`, `manual` | no | `live` the real run, `replay` the calibration replay, `manual` a forced retrain |
| `forced` | boolean | | no | a person forced it |
| `cooldown_until` | string | `YYYY-MM-DD` or null | yes | no triggered retrain before this date; null when no cooldown applies |
| `thresholds_sha256` | string | 64 lowercase hex | no | sha256 of the thresholds file #170 will commit, so a decision names the thresholds it used |

`thresholds_sha256` points at a file that does not exist yet: #170 commits the thresholds file after it calibrates them on the replay, and the hash in a decision is of that file's bytes. The fixtures use a made-up hash. The thresholds themselves are not in this contract.

## Rules

- `retrain` is true **if and only if** `reasons` is not empty.
- `forced` implies `source` is `manual` and `reasons` contains `manual`.
- `reasons` has no repeats.
- `latest.json` is byte-identical to the dated file with the newest `date`.
- At most one triggered retrain a day (project spec), so a day has one decision file. How long the cooldown lasts is #170's.

## Example

- [fixtures/monitoring/decisions/2026-11-27.json](fixtures/monitoring/decisions/2026-11-27.json): `retrain` false, no reasons.
- [fixtures/monitoring/decisions/2026-12-03.json](fixtures/monitoring/decisions/2026-12-03.json): `retrain` true because of `fairness_gap`, with a cooldown.
- [fixtures/monitoring/decisions/latest.json](fixtures/monitoring/decisions/latest.json): byte-identical to the 2026-12-03 file.

## Schema

[schemas/retrain-decision.schema.json](schemas/retrain-decision.schema.json). The two rules above are in the schema (`allOf`) as well as in the tests.

## Versioning

`retrain-decision/v1`. A new reason or source value is breaking, since a reader that matches on the enum rejects it. A new threshold in the thresholds file is not a contract change: the hash records it.

## Where it breaks

- **`latest.json` behind the dated file.** If the dated file is written and the copy fails, the workflow reads yesterday's decision. Write the dated file, then copy its bytes, and fail the run if the copy fails.
- **A replay decision in the live folder.** `source: replay` decisions are for calibration. #163 acts only on `source` `live` or `manual`.
- **A decision made on a sparse monitoring output.** Null metrics must not produce a reason. `retrain` false with an empty `reasons` is the right answer.
- **A rerun for the same date.** The file is replaced. If a retrain was already dispatched on the first run, the second run must see `cooldown_until` and not dispatch again.
- **The thresholds file changes without a new hash.** The hash is then wrong for the decision. #170 computes it from the bytes at decision time.
