# ADR-025: Self-relative 311 lags are the default model inputs; the other #32 gaps are documented or deferred

- Status: proposed
- Date: 2026-10-28
- Owner: alekhyabulusu
- Phase: data

## Context

#32 sliced the data and the surge labels before any model exists (`Data-Pipeline/reports/bias/report.md`, on the data `dvc.lock` names, 2015-01-01 to 2026-10-25). 105 of its 314 tests are significant: the lower rate is under 0.8 times the higher one and p < 0.01. #33 asks that each significant gap be mitigated or deferred to modeling with a reason, with before and after numbers.

The gaps fall into five kinds:

1. **Reporting.**
   - Higher-poverty neighborhoods file 30% fewer storm requests per resident (20.5 against 29.2 a year per 1,000 residents, 2015 to 2023), and higher-poverty tracts 43% fewer.
   - Tracts with more people of color or limited English file 21 to 29% fewer.
   - 311 counts reported demand, not need, so these areas look calmer than they are.
   - 8 of the 31 model inputs in `feature_catalog.csv` were raw 311 lag counts (`snow_ice_last` to `tree_sum28`), so the model saw this gap directly.
2. **Labels.** Surge rates are not lower where reporting is: 3.40% against 3.76% by poverty, within the rule. They are higher in neighborhoods with more people of color or limited English (4.16% against 3.06% by limited English in training). Each label compares a neighborhood with its own ADR-005 baseline, which absorbs its usual reporting level.
3. **Small neighborhoods.**
   - 36 neighborhood surge rates differ from the rest of the city.
   - The lowest are the small neighborhoods under the m 5 request floor (#16): Longwood 0.27%, Chinatown 0.41%, West End 0.93% and South Boston Waterfront 0.96% in training, against 3.65% elsewhere.
   - Chinatown had no surge in 2024, and Harbor Islands none in any period.
4. **Migration.** In the year after the first new-system storm request (2025-10-27), storm requests per resident nearly double (24.0 to 45.8) and the surge rate rises from 4.13% to 5.79%, against the same days a year earlier.
5. **Missing location.** Under 0.25% of storm requests in every group, and only a lower bound: 154 of the 1,063 storm requests without a location carry a recorded neighborhood name.

## Decision

**1. Reporting: self-relative 311 lags are the default model inputs, in place of raw counts.** They are the default, not the final choice: Model Development compares them with the raw lags before the model inputs are fixed (Consequences).
- **The new features:** `feature_table.py` adds `{group}_rel_last` and `{group}_rel{3,7,28}` for snow_ice and tree. Each is (requests + 1) / (baseline + 1) over the same days as the raw lag. The baseline is the neighborhood's own ADR-005 method A mean, the same baseline the labels use.
  - 1 means a usual day for that neighborhood, whatever it usually reports.
  - The +1 keeps a neighborhood with almost no history from dividing by zero.
- **No leakage:** the baseline of a day uses only the same weeks of the 3 previous years, so it is known before D. `check_no_leakage` recomputes these features by date, as it does for the raw lags (`relative_by_date`), and stops the build on any difference.
- **Catalog:** in `feature_catalog.csv` the 8 self-relative lags are the default model inputs, and the 8 raw lags stay in the table with `use_in_model = no`, so Model Development can switch them back after its comparison.
- **Coverage:** a neighborhood first has a baseline in January 2016, so the self-relative lags start on 2016-01-08 (`rel_last`), 01-10 (`rel3`), 01-14 (`rel7`) and 02-04 (`rel28`). The labels start on 2016-01-06. So 696 of the 70,008 labeled training rows (0.99%), from 2016-01-06 to 2016-02-03, miss at least one self-relative window (48 miss `rel_last`, 696 miss `rel28`), where the raw lags have none missing. The values stay missing, never filled (ADR-003); Model Development either drops those rows or uses a model that takes missing values.

**2. Labels: documented, not changed.** A higher surge rate relative to a neighborhood's own history is not a reporting artifact. The protection is in the model: the worst-group recall gap per neighborhood (ADR-004, ADR-008), reported by the #32 groups too.

**3. Small neighborhoods: deferred to the surge rule freeze ADR.** The cause is the fixed request floor m, which the freeze decides before Model Development. #16's numbers, the five smallest neighborhoods' surges a year in training:

| m | 3 | 4 | 5 |
|---|---|---|---|
| surges a year | 31.8 | 24.1 | 17.5 |
| neighborhoods with under 5 surges in 2024 | 3 | 3 | 4 |

Whatever m is chosen, a neighborhood with no surges in a window has no recall and is reported apart (ADR-004).

**4. Migration: deferred to drift monitoring (ADR-008).** It is a data change point, not a gap between groups of people. The two eras are different years, so the slice cannot separate the system change from that year's weather. Drift and grading use the switch dates in `docs/data/change-points.csv`.

**5. Missing location: documented, not changed.** The shares are too small and too partly measured to change the data for.

## Before and after

`python Data-Pipeline/scripts/bias_mitigation.py` (`Data-Pipeline/reports/bias/mitigation.md`) compares three versions of every 311 lag on the #32 groups (poverty, people of color, limited English, flood zone):
- **raw:** what the model used;
- **per capita:** where the reporting gap shows;
- **self-relative:** the default model inputs now.

It covers every period, storm group and window, on all 90,091 labeled neighborhood-days that have every version, and on their 3,554 surge days. A gap is a lower group mean under 0.8 times the higher one.

**What this shows, and what it does not.** This is an input-level result. Dividing by a neighborhood's own baseline puts every neighborhood near 1 on an ordinary day by construction, so equal group means on all labeled days are expected and prove little by themselves. The surge-day comparison says more: on surge days the lag is the forecast's evidence, and there the self-relative lags shrink the gaps from 53 to 7 of 96. Neither shows that the model will be fairer or forecast better. That is for Model Development to measure (Consequences).

| version | gaps, all labeled days | gaps, surge days |
|---|---|---|
| raw | 52 of 96 | 53 of 96 |
| per capita | 45 of 96 | 64 of 96 |
| self-relative | 0 of 96 | 7 of 96 |

Some of these comparisons, for the 7-day sums (lower group mean against higher, ratio):

| slice | storm group | days | raw | self-relative |
|---|---|---|---|---|
| limited English | tree | all, 2024 | 5.67 vs 10.15 (0.56) | 1.27 vs 1.37 (0.93) |
| people of color | snow_ice | all, 2025 onward | 11.50 vs 18.79 (0.61) | 1.77 vs 1.88 (0.95) |
| poverty | snow_ice | surge days, train | 23.27 vs 29.72 (0.78) | 2.48 vs 2.83 (0.88) |
| poverty | tree, per capita | all, train | 0.14 vs 0.24 (0.59) | 1.23 vs 1.25 (0.98) |

The 7 remaining gaps are all surge days in 2024. That is the smallest sample, 423 surge days split between two groups. In those 7, both groups' self-relative lags are above 1, so a storm still shows in each. Model Development should check the 2024 worst-group recall against them.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Self-relative lags (chosen) | $0 | `feature_table.py`, its catalog and tests; Piyush reviews (#16 owner) | Loses the absolute size of a storm in requests; the model keeps population and density as static features to recover scale. The +1 pulls small counts toward 1, so tiny neighborhoods' values are coarse |
| Per-capita lags | $0 | small | Keeps the reporting gap: per capita is exactly where it shows (45 of 96 gaps on all labeled days, 64 on surge days) |
| Reweight training rows by group | $0 | in Model Development | Rebalances how much each group counts, but the features still carry the gap, so a low-reporting neighborhood's storm still looks smaller |
| Defer the reporting gap to modeling | $0 | none now | Leaves the gap in the inputs and hopes the recall gap catches it; no before and after numbers before a model exists |

## Consequences

- **Owner review:** the model's 311 inputs change. Piyush reviews this ADR and the `feature_table.py` change (CLAUDE.md, owners).
- **Raw lags:** kept in the table, so Model Development can compare them against the self-relative ones. If the raw lags are turned back on as the main 311 inputs, this ADR is superseded.
- **Model Development owns the model-level check.** Before the model inputs are fixed, it trains with raw lags, with self-relative lags, and with self-relative lags plus each neighborhood's baseline level, and reports for each:
  - 2024 validation recall, overall and per #32 group (poverty, people of color, limited English, flood zone);
  - the worst-group recall gap per neighborhood (ADR-004, ADR-008);
  - the 7 surge-day input gaps left in 2024.

  The inputs chosen there are recorded in its ADR.
- **Grading keeps watching** the same #32 groups' recall in the 30-day windows (ADR-008), not only the worst neighborhood.
- **Pipeline:** the `feature_table` and `bias_slicing` stages reran (ADR-024). `dvc.lock` and the pushed data include the new columns, and the #32 report is byte-identical.
- **Evening run:** the live run (#39) builds the same features from the same baselines, so they pass ADR-003's live rule.

## Revisit when

- The surge rule freeze ADR picks m. Rerun `bias_slicing.py` and update point 3.
- Model Development has a model: compare raw against self-relative lags on validation recall per group (Consequences).
- **The baseline level.** The label needs at least m requests (#16), so a neighborhood's usual volume decides whether a surge is even possible there. With only self-relative lags, the model sees that volume only through population and density. Compare adding the baseline level (the ADR-005 mean) as a feature. It carries the reporting level itself, so check it per #32 group like the raw lags.
- The 2030 census or a new ACS vintage changes the groups.
