# ADR-005: Surge baselines use the same weeks of the previous 3 years, both 311 systems combined

- Status: accepted
- Date: 2026-10-19
- Owner: Pu1ni2
- Phase: data

## Context

A surge is a day whose storm-related requests are far above a baseline. Live counts (from Nov 2026) come mostly from the new 311 system, while baselines come from legacy years. If the switch changed how many requests land in a storm group, ordinary days would look like surges ("reclassification"), or real surges would be missed. This is spec risk 1, and #11 decides how the baseline is built. #16 still decides k, m and the top-10% rule; this ADR only decides where the baseline's mean and sd come from.

Switch status per storm group (`docs/data/change-points.csv`, ADR-020 for the clock):

- **tree:** switched 2025-10-27; about a year of new-system data. Every legacy tree category has a new-system counterpart.
- **snow_ice:** switched 2026-06-23 on 2 test requests; no new-system winter yet. Only 50.4% of last winter's legacy snow requests have a new-system category (Unshoveled Sidewalk, 48%, has none).
- **flooding:** no new-system category. Legacy flooding stopped (Roadway Flooding 2025-10-22, Catchbasin 2026-06-23); live counts are zero.

Evidence from `python Data-Pipeline/scripts/live_baselines.py` (data to 2026-10-17; legacy files 2015 to 2026 and the new-system export, Logan GHCN-D):

**Switch dates as change points.** Quiet days only (no rain of 25.4 mm or more, snow of 25 mm or more, or gust of 20 m/s or more, and not within 3 days after one); the switch week left out; compared with the same calendar windows in 2016 to 2024.

| series | step after/before, switch year | normal years | verdict | break scan |
|---|---|---|---|---|
| tree (8 weeks) | 0.34 | 0.37 to 0.70 (median 0.48) | lower than any normal year (-31%) | no unusual break |
| tree (12 weeks) | 0.26 | 0.32 to 0.54 | lower (-35%) | |
| Street Light Outage (non-storm, switched 2025-10-22) | 1.23 | 0.84 to 1.34 | no shift | no unusual break |

Tree requests per month since the switch run at a median of 0.82 times the same month in the previous 3 years (0.40 to 1.52, driven by storms); street lights at 0.98. Tree volume is somewhat lower after the switch, never higher.

**Replay of the year after the switch.** Each day is labeled the way the live job would, using only earlier days, with the spec's grid (k 1.5 or 2, m 3 or 5); the same span in 2018-19 to 2024-25 is the reference. Quiet-day flags are false alarms; weather-day flags show storms are still caught. Values at k = 1.5 (k = 2 gives the same verdicts):

| series | method | quiet days flagged | normal years (median, max) | weather days flagged | normal years (median, min) | verdict |
|---|---|---|---|---|---|---|
| tree | A seasonal | 1.2% | 2.5%, 15.5% | 15.2% | 16.3%, 6.7% | pass |
| tree | B post-switch only | 6.6% | 6.5%, 9.4% | 15.2% | 14.5%, 11.6% | pass |
| tree | C trailing 28 days | 6.6% | 6.8%, 9.8% | 18.2% | 14.5%, 11.6% | pass |
| tree | D seasonal rescaled by 0.69 | 6.6% | 10.3%, 29.1% | 25.8% | 23.3%, 14.7% | pass |
| Street Light Outage | A seasonal | 5.6% | 4.8%, 9.6% | | | pass |
| Street Light Outage | B post-switch only | 11.7% | 8.8%, 10.0% | | | fail |
| Street Light Outage | C trailing 28 days | 12.1% | 9.2%, 11.2% | | | fail |

The verdicts do not change with 12-week windows or with the 27 tree `after_switch` rows left out (#53).

## Decision

- **Baseline (method A):** for each unit, storm group and day, the mean and sd of daily request counts on the same day plus or minus 14 days in each of the previous 3 years (87 days), using every day and both systems through `docs/data/category-map.csv`. Fewer than 20 history days means no baseline and no label.
- **Broad storm groups, not categories.** The groups are what carries across the switch; categories are split and renamed.
- **One rule for every period.** Training, validation, test and live labels use the same baseline, built from 311 counts only (no weather), so the grading job needs nothing else.
- **tree:** method A as is. The switch lowered tree volume by about 20 to 30%; lower volume can only reduce false surges, and storm days are still flagged at the normal rate.
- **snow_ice:** method A on legacy history, with a tripwire at the first new-system snow (below). The expected direction is down (missed surges, not false ones), since half of last winter's snow categories have no new home yet.
- **flooding:** no baseline can fix a missing source. Escalated to the team (#10, #16, Prit) because it changes the target; this ADR does not decide the target.
- **`after_switch` rows** stay in (the #53 default); they do not change these results.
- **Unit:** the analysis is citywide by design. The method applies to whatever unit ADR-004 locks.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| A. Same weeks of the previous 3 years, both systems (chosen) | $0 | low | a switch that raises a group's level gives false surges until the history catches up; watched by the tripwire and the live quiet-day rate |
| B. Post-switch data only (the issue's example) | $0 | low | no snow or flooding baseline this winter; fails the street-light check; cannot label training years |
| C. Trailing 28 days | $0 | low | fails the street-light check; the first snow of a season is always a surge, and one storm hides the next |
| D. A with pre-switch history rescaled by the measured step (0.69 for tree) | $0 | medium | needs a factor per group that does not exist for snow and is noisy for tree (monthly ratios 0.40 to 1.52) |

## Consequences

- #16 takes the baseline mean and sd as inputs and adds k, m and the top-10% rule on top. `live_baselines.seasonal_baseline` can be imported.
- #15 counts add both systems per storm group and keep zero-count days, since the baseline needs every day present.
- Labels for 2015 to 2017 need the 2012 to 2014 legacy files or a shorter history; #15 and #16 decide which.
- Tree runs about 20% below its old level, so tree surges may be slightly harder to reach. Monitoring should track the live weather-day surge rate against the replay range.

## Revisit when

- **The first new-system snow of 25 mm or more.** Rerun `live_baselines.py`. If snow-day counts fall outside the legacy range, or an unmapped or non-snow category jumps on snow days, open a category-map PR and consider D for snow.
- A category-map PR touches a storm group.
- The live quiet-day surge rate stays above the replay maximum for 30 days, or the weather-day rate stays below the replay minimum.
- ADR-004 pools the storm groups, or the team decides on flooding.
- Tree completes a second post-switch season (B becomes possible for tree).
