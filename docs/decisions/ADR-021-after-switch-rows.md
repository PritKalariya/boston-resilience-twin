# ADR-021: Keep legacy 311 rows opened after their category's switch date

- Status: accepted
- Date: 2026-10-21
- Owner: Pu1ni2
- Phase: data

## Context

`map_311.py` flags a legacy request `after_switch` when it was opened on or after the first new-system day of its category (#13). That first day is not when the city moved the work over. In several categories the first new-system case came weeks or months earlier, and the legacy system is still in use: it logged 16,389 requests from 2026-09-01 to 2026-10-18. Legacy cases resubmitted to the new system are already dropped by `map_311.py`, so nothing links an `after_switch` row to a new-system case. #53 decides whether these rows are kept, dropped, or kept but left out of baselines.

Evidence from `python Data-Pipeline/scripts/after_switch.py` (legacy files 2015 to 2026 and the new-system export of 2026-10-19, last full day 2026-10-17; requests placed in the 24 ADR-004 neighborhoods by coordinates; ADR-005 baseline at the analysis grid point k 2, m 3, since #16 freezes the rule):

- **2,640 rows**, 0.087% of 3,037,756 legacy rows, in 27 categories (20 with more than one row), with windows of up to 111 days. The 2,519 in #53 came from an earlier export.
- **27 are storm-related**, all Pruning Request (tree). snow_ice and flooding have none so far.

The three worst windows (most `after_switch` rows) and the storm one. The window runs from the switch to the last `after_switch` row; "before" is the same number of days just before the switch. Each cell is the category's per-neighborhood daily count divided by the same weeks of the previous 3 years (the ADR-005 baseline mean), with the surge rate in brackets.

| category | window | legacy / new-system rows in it | before | after, keep | after, drop |
|---|---|---|---|---|---|
| Improper Trash Storage | 2026-06-24 to 2026-08-10 (48 days) | 919 / 1,096 | 1.13 (5.21%) | 0.66 (3.56%) | 0.36 (0.78%) |
| Illegal Graffiti | 2026-05-24 to 2026-06-24 (32 days) | 595 / 7 | 1.24 (4.17%) | 1.68 (4.56%) | 0.02 (0%) |
| Park Groundskeeping | 2025-11-21 to 2026-03-11 (111 days) | 445 / 10 | 0.88 (1.80%) | 0.78 (0.90%) | 0.02 (0%) |
| Pruning Request (tree) | 2025-10-27 to 2026-02-12 (109 days) | 27 / 179 | 0.80 (1.57%) | 0.17 (0%) | 0.14 (0%) |

What the numbers say:

- **The rows are real requests, not copies.** In Illegal Graffiti and Park Groundskeeping the new system logged almost nothing in the window, so the legacy rows are the only record. Dropping them leaves 2% of the usual level and no surges.
- **No sign of double counting where both systems were busy.** Improper Trash Storage is the only window with many rows in both systems. With both kept it runs at 0.66 of its usual level, below the 1.13 just before the switch. Requests entered twice would push it toward twice the usual level instead.
- **Storm labels barely move.** On the ADR-004 unit (neighborhood x day, any storm group; 94,512 labeled cells), drop changes 1 label (the Pruning Request window goes from 12.88% to 12.84% surges) and exclude_from_baselines changes none. Over the coming year, either would move a neighborhood's storm-group baseline mean by at most 0.069 requests a day. Pruning Request runs far below its usual level under every policy; the tree group's level is ADR-005's concern.
- **The coming winter matters more than the past.** Snow Plowing and Emergency Responder Snow Plowing "switched" on 2026-06-23 with 2 test requests, and they held 50.5% of last winter's legacy snow requests (12,923 of 25,607). If snow requests keep going to the legacy system this winter, as all of last winter's did, every one in those categories will be flagged `after_switch`, and drop would delete half of the live snow signal.
- **exclude_from_baselines acts only a year later.** Inside every window it equals keep, because a baseline looks back 351 days or more. A year on, it leaves real requests out of the baseline, so baselines run low and surges come too easily.

## Decision

- **Keep every `after_switch` row:** it counts on its day and in later baselines. `keep` is the default `after_switch_policy` in `Data-Pipeline/config/processing.csv`.
- The processing step (#15) reads the setting with `after_switch.load_policy` and applies it with `after_switch.apply_policy`, which returns the rows to count and an `in_baseline` column marking the rows baselines may use. Under keep, every row is in both.
- The `after_switch` flag stays in the combined table, so the policy can change without reparsing.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Keep (chosen) | $0 | low | if a category is ever entered in both systems, the overlap counts twice and its surges come too easily; watched by the re-check in the runbook |
| Drop | $0 | low | deletes the only record of real requests: 98% of Illegal Graffiti and Park Groundskeeping in their windows, and half of the snow signal if this winter repeats last winter |
| Keep, but leave out of baselines (exclude_from_baselines) | $0 | low | same as keep inside the windows; a year later the baselines miss real requests, run low and give false surges |

## Consequences

- Nothing to regenerate: counts (W2-3, #15) and labels (W2-4, #16) proceed with keep, as #53 assumed.
- #15 calls `load_policy` and `apply_policy` and builds baselines only from rows with `in_baseline` true, so a policy change needs no code change.
- Changing `after_switch_policy` is a reviewed PR that updates this ADR, regenerates counts and labels, and reports the change in totals.
- `Data-Pipeline/scripts/after_switch.py` reruns the evidence (runbook: "After-switch policy").

## Revisit when

- **The first snow of winter 2026-27.** If legacy Snow Plowing requests are flagged `after_switch` while the new system logs Snow Plowing too, check whether the same requests appear in both (same place and time) before the next labels run.
- A rerun shows a window where both systems logged many rows and the category runs well above its usual level under keep (a ratio above 1.5, say): requests may be entered twice.
- The city links legacy cases to new-system cases beyond resubmissions, or retires the legacy system.
- A category-map PR changes a category that has `after_switch` rows.
