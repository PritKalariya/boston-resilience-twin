# Status

Updated: 2026-10-16 by Srikarmk. Edit this file in the PR that changes the state.

## Current milestone
Week 2: Processing (due 2026-10-18)

## Open work
| Issue | Owner | Due | State | Note |
|---|---|---|---|---|
| #1 Harden repo and team workflow | Prit | 2026-10-10 | in progress | |
| #3 Terraform project 1 | Prit | 2026-10-11 | done | PR #30 merged; ADR-015 accepted |
| #5 Fix legacy 311 parser and re-measure counts | Srikar | 2026-10-11 | done | PR #45 merged; results below |
| #13 Format mapping, legacy to new 311 | Srikar | 2026-10-14 | in review | results below; new-system clock and flooding gap need a team call |
| #29 Verify teammate IAM | Prit | 2026-10-15 | done | PR #51 merged. All 6 members granted, plus `roles/iam.serviceAccountUser` on `airflow-vm-sa` (scoped) for OS Login. Teammate start and SSH verified; VM stopped. |

## Blocked
None.

## Risks raised
- Legacy storm groups cover only 6.9% of legacy requests (210,049 of 3,037,969), against about 15% in the pre-scoping check. The groups now live in `docs/data/category-map.csv` (`storm_group` column) and may be narrower than the first run's. Worth a look before #10 locks the unit.
- **New-system times are Boston local, not UTC.** `open_date` ends in `+00`, but 1,283 cases resubmitted from the legacy system open a median 0.04 hours (about 2 minutes) after the legacy close time, in both EST and EDT. Real UTC would be 4 to 5 hours later. Converting from UTC would shift every new-system time 4 to 5 hours early and move 1,124 requests (1.6%) to the previous day. CLAUDE.md and #6 say the new times are UTC; the pipeline treats them as local and re-checks this on every run (`map_311.check_clock`). Needs a team OK.
- **Flooding has no new-system category.** Legacy flooding requests (mostly Catchbasin) stop after Jul 2026 and nothing in the new system replaces them, so live flooding counts are zero. Affects the target and #11.
- **Snow has not been through the new system yet.** Snow Plowing and Emergency Responder Snow Plowing exist (2 requests), but all of winter 2025 to 2026 went to the legacy system. Unshoveled Sidewalk has no new category so far. The first snow storm this winter is the real switch for snow_ice.
- Neighborhood names differ between systems: 23 legacy and 25 new-system names, 16 shared (the new system splits Allston and Brighton, adds North End, Chinatown, West End and others). #15 needs one unit list, from the spatial join or a name map, before daily counts.
- Animal requests are missing from 2025-06-12 (last legacy) to 2025-08-01 (first day of the new export).

## W2-1 results (2026-10-16)

Commands: `python Data-Pipeline/scripts/legacy_311.py`, `python Data-Pipeline/scripts/new_311.py`, then `python Data-Pipeline/scripts/map_311.py`. Data: legacy files 2015 to 2026 and the new-system export (2025-08-01 to 2026-10-12).

### Category map

`docs/data/category-map.csv` lists all 197 legacy types and 71 new-system categories. Each maps to a shared `category` (the new-system name where one exists) and a `storm_group`. A category not in the file maps to `unmapped` and is counted, so a new city category shows up in the run output instead of disappearing. New tree categories take the group of the legacy types that feed them, so `tree` means the same thing before and after the switch.

| system | rows | same | renamed | merged | split | legacy or new only | unmapped | excluded |
|---|---|---|---|---|---|---|---|---|
| legacy | 3,036,686 | 126,864 | 1,169,732 | 220,374 | 179,661 | 1,340,054 | 0 | 1 |
| new | 68,542 | 2,578 | 32,867 | 12,496 | 12,104 | 8,441 | 56 | 0 |

The 56 unmapped new-system rows have a blank category in the export. The excluded legacy row is "Test Case A". 1,283 legacy rows that were resubmitted to the new system are dropped, so those cases count once. Another 342 legacy rows name a new case that is not in the export (56 predate it); they are kept, since the legacy row is the only copy.

| storm group | legacy | new |
|---|---|---|
| snow_ice | 123,167 | 2 |
| tree | 80,227 | 6,727 |
| flooding | 6,655 | 0 |

Tree volumes carry over the Oct 2025 switch at a similar level (Mar 2025: 291 legacy, Mar 2026: 340 new; Feb 137 and 144).

### Time zone

Both systems are stored as naive Boston wall-clock time. Legacy times are already local. New-system times are kept as published (see Risks). Since Aug 2025 the median request hour is 12.7 in legacy and 13.5 in the new system; converting the new times from UTC would put it at 9.7, with 32% of requests before 7am.

### Change points

`docs/data/change-points.csv` has the first new-system date for each shared category and storm group. Departments switched in waves: animals by 2025-08-01, street lights 2025-10-14 to 22, trees 2025-10-27, parks 2025-11-21 and 2026-03-10, public works 2026-06-23, needles 2026-09-08, signs and signals 2026-09-29. Storm groups: tree 2025-10-27, snow_ice 2026-06-23 (no snow since), flooding none.

## W1-5 results (2026-10-13)

Commands: `python Data-Pipeline/scripts/legacy_311.py` then `python Data-Pipeline/scripts/measure_311.py`. Data: legacy yearly files 2015 to 2026 from Analyze Boston, Logan GHCN-D (USW00014739). Last full day 2026-10-10.

### Row counts

Likely cause of the first-run loss: `open_dt` mixes whole seconds with 1 to 3 fractional digits. `pd.to_datetime` without a format infers it from the first row and turns the other variant into NaT. Parsing with `format="ISO8601"` fixes it, and the fixed parser loses no rows.

The first-run code is not in the repo, so the "inferred format" column below re-creates that bug rather than measuring the first run itself. It loses 1,474,743 of 3,037,969 rows (48.5%). That does not fully match the spec: the pre-scoping check counted 833,700 rows for Jan 2024 to early Oct 2026, close to the 840,543 legacy plus new-system rows now, so that run did not lose half its rows in that window. Treat "about a third" as unconfirmed.

| file | published | parsed | inferred format lost | now lost |
|---|---|---|---|---|
| 2015 | 202,066 | 202,066 | 76,212 (37.7%) | 0 |
| 2016 | 210,483 | 210,483 | 87,317 (41.5%) | 0 |
| 2017 | 245,475 | 245,475 | 113,213 (46.1%) | 0 |
| 2018 | 257,610 | 257,610 | 134,306 (52.1%) | 0 |
| 2019 | 253,782 | 253,782 | 132,819 (52.3%) | 0 |
| 2020 | 246,165 | 246,165 | 123,530 (50.2%) | 0 |
| 2021 | 269,665 | 269,665 | 136,362 (50.6%) | 0 |
| 2022 | 272,931 | 272,931 | 146,023 (53.5%) | 0 |
| 2023 | 307,791 | 307,791 | 152,894 (49.7%) | 0 |
| 2024 | 306,756 | 306,756 | 136,898 (44.6%) | 0 |
| 2025 | 276,093 | 276,093 | 90,898 (32.9%) | 0 |
| 2026 | 189,152 | 189,152 | 144,271 (76.3%) | 0 |

The new-system file has 68,542 records, matching the published count. It is not in the numbers below, since its categories need the #13 mapping.

### Sparsity

Storm-related legacy requests over 17 neighborhoods. "South Boston", "Allston", "Brighton", "Chestnut Hill" and "Mattapan" are folded into the city's combined names; "Boston" and blank (6,503 storm rows) count as no neighborhood.

| window | unit | cells | empty |
|---|---|---|---|
| 2015 to 2026 | neighborhood x day | 73,117 | 48.8% |
| 2015 to 2026 | neighborhood x day x group | 219,351 | 80.7% |
| 2024 to 2026 | neighborhood x day | 17,238 | 56.4% |
| 2024 to 2026 | neighborhood x day x group | 51,714 | 83.4% |

The first run's 77% is closest to neighborhood x day x group; with full rows it is 81 to 83%. Dropping the group split halves it.

### Storm ratios

Citywide daily counts on event days over the mean of control days in the same month. Control days within 3 days after an event are skipped, since cleanup requests keep the baseline high. Snow day: SNOW >= 25 mm, control SNOW = 0. Heavy rain: PRCP >= 25.4 mm, control PRCP < 2.5 mm. Heat: TMAX >= 32.2 C, control cooler Jun to Sep days.

Thresholds and controls are this re-measure's own; the first run's definitions are not recorded, so its ratios are shown for reference, not as a like-for-like comparison. New-system rows (Aug 2025 onward) are not included yet.

| check | event days | same day | next day | first run |
|---|---|---|---|---|
| snow_ice, snow days | 106 | 2.11x | 4.42x | 4.9x |
| flooding, heavy rain | 120 | 6.31x | 2.14x | 2.5x |
| tree, heavy rain | 120 | 1.43x | 1.26x | 1.7x |
| all requests, heat | 197 | 1.03x | 1.04x | 1.13x |

2015 to 2026 shown. For 2024 to 2026 (the first-run window): snow 1.45x / 4.36x, flooding 9.38x / 2.30x, tree 1.68x / 1.15x, heat 1.00x / 1.00x. Snow requests peak the day after the storm, flooding on the day. Heat gives almost no signal on total requests.
