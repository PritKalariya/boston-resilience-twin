# ADR-027: The training interface: frozen target, fixed splits, one prediction contract, a shared threshold and the seasonal baseline

- Status: proposed
- Date: 2026-11-03
- Owner: Pu1ni2
- Phase: model

## Context

Model Development starts from #106 (M3). M4 trains the candidate models through it, M6 tunes them, M7 checks their bias, M9 gates and promotes them and M10 explains them, so the interface has to be fixed before any model trains. CLAUDE.md requires the target to be frozen by ADR before Model Development, and ADR-026 leaves two pieces to #106: the shared threshold rule (`threshold_rule = shared` in `Model-Development/config/gates.csv`) and the validation tp, fp and fn of the seasonal baseline and of every candidate.

Before this ADR:
- `Data-Pipeline/config/surge_rules.csv` had `sd_k2_m5` (a surge when requests > mean + 2 sd of the ADR-005 baseline and at least 5 requests) chosen but marked provisional. The checkpoint review CP1 recommended it unchanged.
- `label_groups` in `labels_features.csv` was `snow_ice;tree;flooding`. ADR-004 kept flooding in the label and left the question to the team. Flooding has no new-system category (ADR-005), so a live label can never contain a flooding surge.
- ADR-003 set the splits by years and "mid-2026" but no end date, and flagged 2020 and 2021 for down-weighting without a weight.

## Decision

1. **Target frozen.** The surge rule is `sd_k2_m5`, unchanged. A neighborhood-day is a surge when snow_ice or tree surges against its own ADR-005 baseline (`label_groups = snow_ice;tree`). The model predicts one flag per neighborhood-day (ADR-004). Changing the rule or the groups takes a new ADR.

2. **Flooding leaves the label.** The legacy-only numbers, from `labels.parquet` on this data version (labels 2016-01-06 to 2026-10-25):

   | split | labeled days | surges with flooding | surges, frozen | flooding-only surges | frozen surge rate |
   |---|---|---|---|---|---|
   | train 2016 to 2023 | 70,008 | 2,407 | 2,392 | 15 | 3.42% |
   | validation 2024 | 8,784 | 423 | 421 | 2 | 4.79% |
   | test 2025-01-01 to 2026-06-30 | 13,104 | 677 | 669 | 8 | 5.11% |
   | after the test window | 2,808 | 50 | 50 | 0 | 1.78% |

   - 25 surges were flooding only, from 2016-03-15 to 2025-10-08, all from legacy flooding categories.
   - Flooding surged on 38 labeled days in all.
   - `labels.parquet` still keeps `surge_flooding` and the flooding counts, and the flooding alert features stay inputs. Only the label changes.

3. **Splits and weights.**
   - **Split dates** (`Model-Development/config/training.csv`):
     - train 2015-01-01 to 2023-12-31 (labels start 2016-01-06);
     - validation 2024-01-01 to 2024-12-31;
     - test 2025-01-01 to 2026-06-30, so "mid-2026" is now June 30.
   - **Each labeled neighborhood-day is in exactly one split** by its date. Days after the test window are live and unused here. `training_data.check_splits` fails on a day in the wrong split or in two.
   - **Weights.** Train rows in 2020 and 2021 weigh 0.5 (`down_weight`), a starting value M6 may tune. Every other row weighs 1.

4. **One prediction contract** (`Model-Development/scripts/predictions.py`). Every model writes a parquet file with these columns, in this order:

   | column | content |
   |---|---|
   | `unit` | text |
   | `date` | whole days, no time zone |
   | `model` | text |
   | `split` | train, validation or test |
   | `probability` | float64 from 0 to 1 |
   | `prediction` | int8; 1 when the probability is at or above the run's threshold |
   | `label` | int8, the frozen label |

   `validate_predictions` checks all of it, and writing or reading a file always runs it.

5. **The shared threshold rule** (`threshold_rule = validation_f1_max`).
   - A day is predicted a surge when its probability is at or above the threshold.
   - The threshold is the probability value with the highest surge-class F1, 2tp / (2tp + fp + fn), on the validation split. On a tie the higher threshold wins, so there are fewer alarms.
   - The run reuses it unchanged on test, which never picks it.
   - F1 values are compared as exact integer ratios. A validation split with no surge has no F1, so the rule fails (ADR-026, `on_undefined = fail`).
   - Each run writes the validation tp, fp and fn with its data version to `validation_counts.json`, the block the gates read for a candidate or for the baseline.

6. **The seasonal baseline.** Its probability for a neighborhood on day D is that neighborhood's share of surge days in ADR-005's window: D plus or minus 14 days in each of the previous 3 years, 87 lags from 351 to 1,109 days back.
   - It never reads D or the 350 days before it, so it cannot leak.
   - It needs 20 labeled days in the window, as a label does.
   - It learns nothing from the train rows, and it uses no feature, so it scores the same live as in a backtest.

7. **Entry points.**
   - `train.py --model <name>` trains a model registered in `models.MODELS` on the train split, predicts validation and test, and writes `Model-Development/runs/<run id>/`.
   - `evaluate.py --run <dir>` checks a run against the contract and recomputes its metrics.
   - Every setting is in `training.csv`. The data version is the sha256 of `Data-Pipeline/dvc.lock` with its line endings as LF, so a Windows checkout reports the same version as Linux.

The seasonal baseline on this data version (`sha256:b668ba8a420f`, 2026-11-03), at the shared threshold 0.034483, which is 3 surge days of the 87:

| split | rows | surges | tp | fp | fn | F1 | precision | recall | AUC |
|---|---|---|---|---|---|---|---|---|---|
| validation | 8,784 | 421 | 219 | 2,817 | 202 | 0.1267 | 0.0721 | 0.5202 | 0.6159 |
| test | 13,104 | 669 | 511 | 5,417 | 158 | 0.1549 | 0.0862 | 0.7638 | 0.6832 |

So a candidate passes the ADR-026 accuracy gate with a validation F1 of 0.1767 or more.

**Effect on the gates and the bias reports.**
- **Gate groups.** The frozen label leaves the ADR-026 groups well above the 5-surge floor in 2024: lower 223, higher 198 (ADR-026 measured 223 and 200 with flooding). The same four neighborhoods stay under 5: Chinatown 0, Longwood 2, South Boston Waterfront 4 and Harbor Islands 0.
- **#32 report.** 104 of 314 tests are now significant (105 before).
- **ADR-025's before and after comparison.**
  - All labeled days are unchanged: 52, 45 and 0 gaps of 96.
  - Surge days now show 47, 63 and 7 gaps, against 53, 64 and 7 before, on 3,529 surge days (3,554 before).
- The refreshed reports are in `Data-Pipeline/reports/bias/`.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Keep flooding in the label (ADR-004's default) | $0 | none | Training labels hold 25 flooding-only surges that live labels can never have, so the target changes at deployment |
| m 4 instead of 5 (#16's alternative) | $0 | relabel | More surges in the smallest neighborhoods, but CP1 recommended m 5 unchanged and ADR-026's gate counts were measured on it |
| A fixed 0.5 threshold | $0 | none | Models differ in calibration; the seasonal baseline's shares rarely reach 0.5, so it would almost never predict a surge and the gate would compare unlike things |
| A threshold chosen on the train split | $0 | none | A fitted model scores its own training rows too well, so the threshold comes out too high; the baseline has no training fit to score |
| A threshold that matches the training surge rate | $0 | small | Uses no validation label, but ignores each model's trade between precision and recall, while the gate compares F1 |
| A persistence baseline (surge two days earlier) | $0 | small | Knows nothing of season; the spec and ADR-026 name the seasonal baseline |
| The test window through the latest data | $0 | none | The test set would grow with every refresh, so test results would not be comparable |

## Consequences

- **#107 (M4)** adds logistic regression, LightGBM and XGBoost to `models.MODELS` behind `fit(rows, label, weight)` and `predict_proba(rows)`. It trains on the train split with the weights, and keeps the threshold rule and the contract.
- **#109 (M6)** tunes on the validation split only; it may change `down_weight`.
- **#110 (M7)** reads `predictions.parquet` and `poverty_groups.csv` to emit the per-group counts.
- **#112 (M9)** builds the gate input from `validation_counts.json`: the candidate's, and the baseline's from `train.py --model seasonal` on the same data version. The baseline is recomputed every run, never stored.
- **This PR is a data version.** Only `labels.parquet` and the three #32 reports changed. `features.parquet` is byte-identical.
- **ADR-004's flooding point** is amended here; its status line points to this ADR.
- **Runs are not committed.** `Model-Development/runs/` is gitignored, and MLflow tracking comes with #104 and #107.

## Revisit when

- CP1 or the team chooses another rule or request floor (m 4 for the smallest neighborhoods): a new ADR supersedes point 1. Rerun `dvc repro`, the bias reports and the baseline.
- The new 311 system gets a flooding category, so live flooding labels become possible.
- ADR-003 moves the validation year or the test window.
- The team needs a threshold set by a crew budget or a cost: a new rule in `training.csv` and a new ADR. The gates still compare F1.
