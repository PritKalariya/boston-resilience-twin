# ADR-004: The modeling unit is neighborhood x day, a surge in any storm group

- Status: proposed (team sign-off requested on the PR)
- Date: 2026-10-20
- Owner: alekhyabulusu
- Phase: data

## Context

The spec's working target is a next-day surge flag for storm-related requests per neighborhood per day, and CLAUDE.md freezes the target unit by ADR before Model Development. #10 compares candidate units on the W1-5 measures: share of empty cells and surge rate. ADR-005 already fixes the baseline (method A: same day plus or minus 14 days in each of the previous 3 years, both 311 systems, by storm group, at least 20 history days) and leaves the unit to this ADR. #16 still decides k, m and the top-10% rule.

Two facts shape the comparison:

- **The 311 neighborhood field differs between systems** (23 legacy and 25 new-system names, 16 shared). Requests are therefore placed by their coordinates in the 2020 tracts and in the 24 neighborhoods the city builds from whole 2020 tracts (`neighborhoods_by_tract20`, #9). These are the same 24 names as the 2025 population tables, so every unit has ACS demographics for fairness slicing.
- **Flooding has no new-system category**, so its live counts are zero (ADR-005 escalated this to #10 and #16).

Evidence from `python Data-Pipeline/scripts/compare_units.py` (Analyze Boston files downloaded 2026-10-20; `map_311.py` combined 3,110,129 rows; days 2015-01-01 to 2026-10-18). Of 216,863 storm-related requests, 216,104 (99.7%) are placed: 546 have no coordinates, 213 fall outside Boston's tracts, and 13 are in tract 9815.02 (no population, by Suffolk Downs), which the city leaves out of every neighborhood. Labels start in 2016, the first year with 20 history days.

Surge rates use the ADR-005 baseline and the spec's analysis grid; they compare units and are not the frozen rule.

| unit | columns | empty, train 2016 to 2023 | empty, 2024 to 2026 | surge, k 2 m 3, train | surge, k 2 m 3, 2024 to 2026 | surges per column-year (median, train) | columns with no surge (train) |
|---|---|---|---|---|---|---|---|
| citywide x day x group | 3 | 37.1% | 37.5% | 5.38% | 5.58% | 23.0 | 0 |
| neighborhood x day, pooled | 24 | 59.1% | 55.7% | 3.97% | 5.12% | 17.4 | 1 |
| **neighborhood x day, any group** | 24 | 59.1% | 55.7% | 4.85% | 5.90% | 21.6 | 1 |
| neighborhood x day x group | 72 | 84.7% | 83.4% | 1.65% | 1.99% | 5.0 | 9 |
| tract x day, pooled | 207 | 89.6% | 87.6% | 1.29% | 2.25% | 4.4 | 4 |
| tract x day, any group | 207 | 89.6% | 87.6% | 1.32% | 2.27% | 4.4 | 5 |
| tract x day x group | 621 | 96.4% | 95.8% | 0.44% | 0.76% | 0.9 | 197 |

"Pooled" adds the groups before the baseline. "Any group" keeps a baseline per group (ADR-005 as written) and flags the unit-day when any group surges. Surge rates at the other grid points (k 1.5 or 2, m 3 or 5) rank the units the same way; for the chosen unit they are 3.44% to 6.00% in training.

For the chosen unit, 112 of 3,396 training surges (3.3%) come from flooding alone, and 60 of 1,446 (4.1%) in 2024 to 2026. Harbor Islands never surges; the next lowest are Chinatown (2.2 a year) and Longwood (2.4).

The W1-5 sparsity (48.8% empty for neighborhood x day, 80.7% with the group split) used the legacy neighborhood field with 17 names; the 24 tract-built neighborhoods split some of those, so they run emptier.

## Decision

- **Unit:** one cell per neighborhood per day, for the 24 neighborhoods in `neighborhoods_by_tract20` (#9). Requests are placed by coordinates (spatial join to 2020 tracts, then tract to neighborhood), not by the 311 neighborhood field. Requests without a usable location are counted and left out.
- **Surge:** a cell is a surge when at least one storm group (snow_ice, tree, flooding) surges against its own ADR-005 baseline for that neighborhood. #16 sets k, m and the top-10% rule on top; this ADR fixes only the unit and how groups combine.
- **Which group surged** is kept next to the label (for briefs and SHAP), but the model predicts one flag per cell.
- **Flooding stays in the label** on its legacy history, since it is part of the storm definition for the training years. It cannot produce live surges while it has no new-system category, which costs about 3 to 4% of surges. This ADR does not decide the flooding question; the team does (#16, Prit).
- **Harbor Islands** is forecast like every other neighborhood. It has no surges, so recall is undefined there; the fairness worst-group metric leaves out any group with no surges in the window and reports that it did.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Neighborhood x day, any group (chosen) | $0 | low | one model has to learn snow, wind and rain at once; surge rate above the spec's 1.3 to 2.9% pre-scoping range until #16 sets k and m |
| Neighborhood x day x storm group | $0 | medium | 85% empty, about 5 surges per column-year; flooding surges in 0.2% of cells and never in 7 of 24 neighborhoods; 30-day fairness windows often have no positives, so the recall gap is noisy or undefined |
| Neighborhood x day, groups pooled before the baseline | $0 | low | triggers ADR-005's revisit; large tree volumes can hide snow and flooding surges in the sum |
| Tract x day (any variant) | $0 | medium | 88 to 96% empty, about 4 surges a year at best; small groups make fairness slices unstable |
| Citywide x day x group | $0 | low | no neighborhood view, so no fairness slices and no map; fails the project's purpose |
| 311 neighborhood field instead of coordinates | $0 | low | names differ between systems, so units change at the switch; needs a hand-made name map |

## Consequences

- #14 (spatial joins) places requests with the same tract and neighborhood join, and reports the unplaced rows.
- #15 (daily counts) produces neighborhood x day x storm group counts with every day present and zeros filled; the any-group flag is built from them.
- #16 (surge labels) computes one ADR-005 baseline per neighborhood and group, applies k and m per group, and ORs the groups. It should check the surge rate against the spec's 1.3 to 2.9% range and against the top-10% rule.
- Fairness slicing (#32) uses these 24 neighborhoods and their 2025 population and ACS columns.
- The forecast API, map and briefs show 24 neighborhoods; the brief names the group that drove a surge.
- `compare_units.py` imports `seasonal_baseline` and `surge_flags` from `live_baselines.py`, so this PR merges after #63.

## Revisit when

- The team decides on flooding, or a new-system flooding category appears.
- #16 freezes k and m and the any-group surge rate lands far outside the spec's range.
- The city changes the neighborhood boundaries or moves to 2030 tracts.
- The first new-system snow season (the ADR-005 tripwire) changes snow_ice volumes enough to move a neighborhood's surge count.
