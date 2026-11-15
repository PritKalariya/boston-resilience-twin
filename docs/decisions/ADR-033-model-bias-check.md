# ADR-033: The model bias check reuses the ADR-026 gate, reports #32's other groups, and picks the winner after it

- Status: proposed
- Date: 2026-11-12
- Owner: alekhyabulusu
- Phase: model

## Context

#110 (M7) asks for the bias part of model selection:
- per-slice recall, precision and F1 on the selected model's validation and test predictions;
- the worst-group recall gap against ADR-026's frozen 0.15;
- a check the model workflow (#112, ADR-032) can call;
- selection after bias, and mitigation if the gap is over the limit.

ADR-026 froze the gate: the two ACS poverty groups of `poverty_groups.csv` (#32's median split), gates.csv's floor of 5 validation surges with pooling, and a 0.15 limit, computed exactly by `gates.evaluate()`. #138 left an interim per-group count in `step_evaluate.py` for #110 to replace.

Two constraints shape the design:
- **The training image has neither geopandas nor the static layers** (ADR-032 mounts only the labels, features, catalog, `dvc.lock` and `Model-Development/config`), so the model side cannot run #32's `bias_slicing.py` at run time.
- **The workflow's evaluate step reads no test number** (ADR-032), so test results cannot enter the decision.

## Decision

**1. One gate implementation.** `bias.bias_check()` turns a model's validation predictions into the per-group counts `gates.evaluate()` takes and returns its fairness gate. The floor, pooling, limit and exact arithmetic are the gate's own; `bias.py` has no constants for them. Since the accuracy gate is not its concern, it passes the candidate's own counts as the baseline block and reads only the fairness part of the report.

**2. The reported groups come from #32, frozen in a file.**
- **The file:** `Data-Pipeline/scripts/model_groups.py`, run on the host, writes `Model-Development/config/slice_groups.csv` with #32's functions from `bias_slicing.py`: each neighborhood's people of color, limited English and flood zone group, and whether it is a student area.
- **A script of its own, not a DVC stage,** so `bias_slicing.py` and `Data-Pipeline/dvc.lock` are unchanged and the data version stays the same. The data version is the lock's sha256, so a change to a stage's code would change it for the same labels and features.
- **The student-area rule** is #32's: at least twice the city's share of college and graduate students, ACS B14002. That gives Allston, Downtown, Fenway, Longwood and Mission Hill, not a hand-named list.
- **Drift is caught:** a Data-Pipeline test regenerates the file from the static layers and fails if it differs. It needs the static layers, so it skips in CI and runs on a host that has them.
- **Poverty stays in `poverty_groups.csv`,** the file gates.csv checks by sha256. `gates.poverty_groups()` is its one reader, for the gate and the bias check alike.

**3. What is gated and what is reported.**
- **Gated:** the poverty groups only (ADR-026).
- **Reported, not gated:** people of color, limited English, flood zone and each neighborhood, from Fairlearn's `MetricFrame` on validation and test. A group under the floor is merged with its family's other small groups, as the gate pools them.
- **A diagnostic, not a gate:** the higher-poverty group in two parts, its student areas and the rest (#126).

**4. ADR-026's reopen conditions are checked, not enforced.** `bias.py` reports a neighborhood with at least the floor of validation surges whose recall is more than the limit below its own poverty group's, and the two-part split differing by more than the limit. Both conditions apply only while the gate passes, as ADR-026 words them; a failed gate blocks the model instead. A condition holding does not block promotion. It goes to ADR-026's owner, as ADR-026 says.

**5. Selection after bias.** `step_evaluate.py` ranks the candidates by selection.py's rule (validation F1, then log loss, then name), runs `bias_check` on each, and picks the first that passes. If none passes, it keeps the best F1 so that `gates.py` blocks it and the run says why. `evaluation.json` records each candidate's gap and how the winner was chosen.

**6. Validation in the workflow, both splits from the CLI.**
- **In the workflow,** `step_evaluate.py` writes the winner's report on validation only (`bias/` in the artifacts) and logs it to the run's MLflow run.
- **From the command line,** `python Model-Development/scripts/bias.py --run <run folder>` writes the validation and test report for the selected run and logs it to the same MLflow run.
- **Both** write `bias.json`, `bias_slices.csv`, `bias_report.md` and `bias_recall.png`.

**7. The residual reporting bias is stated in every report.** It's storm requests per 1,000 residents a year by poverty group, from the raw 311 lags and the population in `features.parquet` (#32, ADR-025).

**8. No mitigation now.** #110 asks for mitigation only when the gap is over the limit, and it is not (below).

## Results on this data version

Data version `sha256:a719afeb...`, the candidates trained in the training image by `step_train.py`.

**The fairness gate (validation):**

| candidate | validation F1 | worst-group recall gap | gate |
|---|---|---|---|
| lightgbm (selected) | 0.3435 | 0.043 (lower 80/197 = 0.406, higher 66/182 = 0.363) | pass |
| xgboost | 0.3317 | 0.046 | pass |
| logreg | 0.2888 | 0.015 | pass |

Every candidate passes, so selection after bias keeps LightGBM. `gates.py` on the step's `metrics.json` gives `promote`, gap 0.043454.

**Reported groups,** recall on validation and test:

| family | group | validation | test |
|---|---|---|---|
| poverty (gated) | higher | 0.363 | 0.718 |
| poverty (gated) | lower | 0.406 | 0.701 |
| people of color | higher | 0.420 | 0.737 |
| people of color | lower | 0.341 | 0.677 |
| limited English | higher | 0.423 | 0.734 |
| limited English | lower | 0.329 | 0.681 |
| flood zone | in | 0.392 | 0.722 |
| flood zone | not in | 0.381 | 0.700 |
| higher poverty, two parts | student areas | 0.267 (12/45) | 0.730 |
| higher poverty, two parts | other | 0.394 (54/137) | 0.710 |

The groups #32 worried about, more people of color and more limited English, get higher recall, not lower. The student-area part is 0.127 below the rest on validation, under the 0.15 trigger, and above it on test.

**ADR-026 reopen conditions:** the neighborhood condition holds for two neighborhoods on validation:

| neighborhood | group | surges | recall | group recall |
|---|---|---|---|---|
| West End | lower | 9 | 0.111 | 0.406 |
| Fenway | higher | 16 | 0.188 | 0.363 |

On test they reach 0.667 and 0.852. With 9 and 16 surges a recall moves by 0.06 to 0.11 per surge, so this may be noise. ADR-026's owner decided not to reopen it (ADR-026's note of 2026-11-12, from #139's review); this ADR does not change the gate.

**Residual reporting bias:** storm requests per 1,000 residents a year, higher against lower poverty, are 19.76 against 28.33 in training (0.70), 15.72 against 25.04 in validation (0.63) and 34.16 against 44.69 in test (0.76). That matches #32. The model's 311 inputs are self-relative (ADR-025), but the labels still come from reported requests, so a neighborhood that reports less reaches the surge floor less often.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Reuse `gates.evaluate()` (chosen) | $0 | small | One definition of the gap; a change to the gate changes the check with it |
| A second fairness implementation in `bias.py` | $0 | small | Two definitions of the same gap can disagree on a boundary case; ADR-026 uses exact fractions for that reason |
| Compute the groups in the image (add geopandas and mount the static layers) | $0 | medium | A larger image and a second copy of #32's logic at run time, for groups that change only with the ACS vintage |
| Mitigate now (reweighting or per-group thresholds) | $0 | medium | No gap to correct (0.043 against 0.15); any change would trade F1 for no measured gain |
| Gate on every neighborhood | $0 | none | 0 to 36 validation surges per neighborhood make the gate fire on noise (ADR-026's own alternative) |

## Consequences

- `step_evaluate.py`'s interim counts are gone; `metrics.json` keeps #138's shape and rules, now from `bias.gate_counts()`.
- `fairlearn==0.15.0` is in the training image (the pin #32 uses).
- A change to #32's ACS groups or the student-area rule means rerunning `model_groups.py`; the Data-Pipeline test fails until then (on a host with the static layers).
- Selection after bias picks a model, but `gates.py` still decides promotion.
- The neighborhood condition holding for West End and Fenway went to ADR-026's owner on #139, who did not reopen the gate (ADR-026, note of 2026-11-12).

## Revisit when

- The surge rule freeze changes the labels, or the candidates change: rerun and update the results here.
- A candidate fails the gate: then #110's mitigation (reweighting, per-group thresholds or resampling) applies, with before and after numbers.
- ADR-026's owner reopens the gate after the West End and Fenway conditions.
