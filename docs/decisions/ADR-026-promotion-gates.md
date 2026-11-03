# ADR-026: Promotion gates for the model CI/CD pipeline, frozen before training

- Status: accepted
- Date: 2026-11-02
- Owner: PritKalariya
- Phase: model

## Context

Issue #112 builds the model CI/CD pipeline. A candidate model is promoted over the
current champion only when it clears fixed gates on accuracy and fairness. The gate
threshold values were frozen on 2026-10-30, before any model trains, so that they
cannot be tuned to a result after the fact. The frozen poverty groups and the champion
rule were set on 2026-11-02. The frozen values live in
`Model-Development/config/gates.csv` and `Model-Development/config/poverty_groups.csv`,
loaded and enforced by `Model-Development/scripts/gates.py`.

This file is the shared interface for three issues:

- #106 trains the seasonal baseline and emits tp, fp and fn on the validation split.
- #110 reads the frozen groups and emits per-group and excluded counts.
- #111 registers models, stores the gate hashes, and re-scores the champion.

The pipeline has no poverty tiers. Its only poverty grouping is a two-way split at
the median of the neighborhoods, computed at run time in
`Data-Pipeline/scripts/bias_slicing.py` (`median_groups`). The gate cannot depend on a
cut that moves whenever that code reruns, so this ADR freezes that same split once into
a committed file owned by #112.

## Decision

### The gates

- **Accuracy gate.** On the ADR-003 validation split (2024), the candidate's
  surge-class F1, `2tp / (2tp + fp + fn)`, must beat the seasonal baseline's F1 from
  #106 by at least 0.05 absolute points, inclusive. Both models are thresholded by the
  same shared rule on the validation split (`threshold_rule = shared`, implemented in
  #106), so the comparison is like for like. The baseline F1 is recomputed each run,
  never stored.
- **Fairness gate.** Over the whole validation split, the worst-group recall gap (best
  group recall minus worst) across the frozen ACS poverty groups must be at most 0.15,
  inclusive.
- **Group floor and pooling.** A group with fewer than 5 validation surges is pooled:
  all under-floor groups form one pool. A pool still under 5 is excluded and reported.
  Neighborhoods with no group (the ungrouped) are reported and never join a pool. If
  fewer than 2 evaluable groups remain, the gate cannot be evaluated.
- **Champion rule.** The challenger replaces the champion only if its F1 is strictly
  greater (`champion_min_delta = 0`); a tie keeps the champion. The champion is
  re-scored on the challenger's data version and validation rows
  (`champion_rescore = challenger_data_version`), so both F1 values come from the same
  rows.
- **Undefined blocks.** Anything that cannot be evaluated (`on_undefined = fail`)
  returns `error` and blocks promotion; it never passes silently.

All gate comparisons are made on exact `fractions.Fraction` values parsed from the
config strings. Floats appear only in the display report, rounded to 6 places. Two
boundary cases make this necessary: a margin of exactly 0.05 and a gap of exactly 0.15
both pass on Fraction values, while in float `0.45 - 0.40 >= 0.05` is False and
`0.85 - 0.70` is 0.15000000000000002.

The `gates.csv` row `fairness_group_sha256` holds the sha256 of `poverty_groups.csv`,
so the loader fails if the groups file is edited without a matching hash update. The
loader also re-derives the median split from the groups file and fails if any row
breaks it.

### The frozen poverty groups

The groups were computed with the pipeline's own `acs_shares` and `median_groups`
(unedited), fed the same inputs `bias_slicing` uses, in the stack image:

```
docker compose run --rm --no-deps --entrypoint python airflow-scheduler - <<'PY'
from pathlib import Path
from bias_slicing import unit_attributes, slice_groups
attrs = unit_attributes(Path("/opt/airflow/repo/Data-Pipeline/data/interim/static"))
groups = slice_groups(attrs, "unit")["poverty_share"]
for row in attrs[["unit", "poverty_share"]].sort_values("unit").itertuples(index=False):
    print(row.unit, round(row.poverty_share, 6), groups.get(row.unit, "ungrouped"))
PY
```

- **ACS source:** the Boston Planning Department Research Division 2025 neighborhood
  population table (Analyze Boston,
  `boston_population_estimates_2025_neighborhood_level.csv`), whose demographics come
  from ACS 2019 to 2023 5-year estimates.
- **Share:** poverty status table B17001, people below poverty
  (`income_b17001_002e`) over people with a determined poverty status
  (`income_b17001_001e`).
- **Median of the 23 graded neighborhoods:** 0.153954. A neighborhood is `higher`
  exactly when its share is above the median, `lower` at or below it. East Boston sits
  exactly at the median (0.153954) and is therefore `lower`, because the split is a
  strict greater-than.
- **Groups (lowest poverty first):**
  - **lower (12):** Back Bay, Beacon Hill, Charlestown, East Boston, Hyde Park,
    Jamaica Plain, North End, Roslindale, South Boston, South Boston Waterfront,
    West End, West Roxbury.
  - **higher (11):** Allston, Brighton, Chinatown, Dorchester, Downtown, Fenway,
    Longwood, Mattapan, Mission Hill, Roxbury, South End.
  - **ungrouped (1):** Harbor Islands, which has no ACS poverty share.

This reproduces the committed `Data-Pipeline/reports/bias/report.md` (11 higher, 12
lower, Harbor Islands ungrouped).

### Validation surge counts

The 2024 counts below are measured from `labels.parquet` under the provisional surge
rule; #106 freezes the final one. Both gated groups are well above the 5-surge floor,
so the fairness gate is evaluable without pooling:

- **lower:** 223 surges across 12 neighborhoods.
- **higher:** 200 surges across 11 neighborhoods.

Per neighborhood (provisional surge rule; #106 freezes the final one):

| group | neighborhood | 2024 surges |
|---|---|---|
| lower | Back Bay | 13 |
| lower | Beacon Hill | 12 |
| lower | Charlestown | 21 |
| lower | East Boston | 34 |
| lower | Hyde Park | 21 |
| lower | Jamaica Plain | 25 |
| lower | North End | 7 |
| lower | Roslindale | 24 |
| lower | South Boston | 26 |
| lower | South Boston Waterfront | 4 |
| lower | West End | 9 |
| lower | West Roxbury | 27 |
| higher | Allston | 12 |
| higher | Brighton | 33 |
| higher | Chinatown | 0 |
| higher | Dorchester | 38 |
| higher | Downtown | 10 |
| higher | Fenway | 18 |
| higher | Longwood | 2 |
| higher | Mattapan | 15 |
| higher | Mission Hill | 10 |
| higher | Roxbury | 35 |
| higher | South End | 27 |
| ungrouped | Harbor Islands | 0 |

Three gated neighborhoods fall under 5 (Chinatown 0, Longwood 2, South Boston
Waterfront 4), as does the already-ungrouped Harbor Islands (0). This is exactly why
neighborhoods are not the gated unit (Alternatives). The group floor and the "reopen
if" conditions below guard the case where #106's frozen rule leaves a group under 5.

## Alternatives

- **Neighborhoods as the gated groups.** Measured 2024 surges range from 0 to 38, with
  three gated neighborhoods under 5 (Chinatown 0, Longwood 2, South Boston Waterfront 4)
  plus the ungrouped Harbor Islands (0). One missed event in a 5-surge neighborhood
  moves its recall by 0.20, more than the 0.15 allowance, so the gate would fail on
  noise. Neighborhoods are reported by #110 instead, not gated.
- **Three poverty tiers (tertiles, frozen the same way).** A rule only the gate uses,
  so the data-level bias report and the gate would disagree on "higher poverty". About
  140 surges per tertile against about 210 per group here (223 lower, 200 higher), so
  the two-way split keeps more positives per gated group.
- **The run-time median, as `bias_slicing` computes it.** The cut moves with any ACS or
  neighborhood change, so a gate result would depend on when it ran. Freezing the split
  into a hashed file removes that.
- **Waiting for #110 to define the groups.** Puts a cross-member dependency on the
  critical path of this PR, which must land before any model trains.
- **All ACS families gated (people of color, limited English, flood zone).** More
  comparisons mean more false failures. They are reported by #110 instead.
- **30-day windows for the training gate.** Quiet months leave every group under the
  floor, and the training gate compares models, not days. 30-day windows stay the live
  metric (ADR-008), with their groups set at Deployment planning.
- **Merging a small group into the adjacent one, or dropping it.** Merging hides the
  group the gate protects; dropping passes silently. Pooling then excluding keeps an
  under-floor group visible in the report.
- **Review-only freeze.** A threshold edit is one line in a large diff and easy to
  miss. Here a changed groups file also fails at load through the hash.
- **The champion rule set later in #111.** It would be set after seeing results. It is
  frozen here instead.
- **A positive champion delta.** It would reduce churn from noise, but there is one
  promotion per model change and a tie already keeps the champion, so a delta is not
  needed.
- **A stored baseline F1.** It goes stale on the next `dvc.lock` change; the baseline
  is recomputed each run instead.
- **Float comparisons.** The two boundary cases above show why exact fractions are used.

## Consequences

- #106 implements `threshold_rule = shared` and emits tp, fp and fn on the validation
  split.
- #110 reads the groups from `poverty_groups.csv` and emits per-group and excluded
  counts in the `evaluate()` input shape. It reports neighborhoods and the other ACS
  families without gating on them.
- #111 stores `gates_sha256` with each registered model and re-scores the champion on
  the challenger's data version. `gates.csv` carries the groups file's hash, so
  `gates_sha256` covers both files.
- The pipeline's bias report keeps its run-time median for data reporting; the gate
  keeps the frozen file.
- Where it breaks:
  - One neighborhood can fail inside a group that passes, and only #110's report shows
    it.
  - ACS poverty counts off-campus students, so a student-heavy neighborhood can sit in
    `higher` without being a low-reporting one. 5 of the 11 higher-poverty neighborhoods
    are university areas (Allston, Brighton, Fenway, Longwood, Mission Hill), so #110's
    neighborhood report, not this group-level gate, is the check on whether any of them
    masks a recall problem.

## Reopen if

- the ACS vintage or the poverty share computation changes, or the pipeline's run-time
  median regroups any neighborhood
- either group has under 5 validation surges under #106's frozen rule
- the validation split changes (ADR-003)
- #110 reports a neighborhood with at least 5 validation surges whose recall is more
  than 0.15 below its own group's while the gate passes
