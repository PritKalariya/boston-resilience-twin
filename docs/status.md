# Status

Updated: 2026-10-22 by PritKalariya. Edit this file in the PR that changes the state.

## Current milestone
Week 2: Processing (due 2026-10-18)

## Open work
| Issue | Owner | Due | State | Note |
|---|---|---|---|---|
| #1 Harden repo and team workflow | Prit | 2026-10-10 | in progress | |
| #2 Local dev environment and Python pin | Punith | 2026-10-13 | done | PR #55 merged. `docker compose up` runs Airflow 3.3.2 (LocalExecutor, Postgres 16); ADR-013 pins Python 3.12 |
| #3 Terraform project 1 | Prit | 2026-10-11 | done | PR #30 merged; ADR-015 accepted |
| #5 Fix legacy 311 parser and re-measure counts | Srikar | 2026-10-11 | done | PR #45 merged; results below |
| #11 Decision: live baselines for the new 311 format | Punith | 2026-10-13 | done | PR #63 merged; ADR-005: baseline = same weeks of the previous 3 years, both systems, by storm group |
| #13 Format mapping, legacy to new 311 | Srikar | 2026-10-14 | done | PR #52 merged; results below; new-system clock in ADR-020; flooding gap needs a team call |
| #20 Airflow on the VM | Prit | 2026-10-18 | done | PR #59 merged. Airflow stack on airflow-vm (LocalExecutor, Postgres 16; ADR-016); UI by SSH tunnel only; VM stopped. Runbook: "Airflow on the VM". After #58 merges, run the update step in the runbook on the VM |
| #23 Write ADRs 001, 009, 010, 011, 012 | Prit | 2026-10-18 | done | PR #65 merged; ADRs in docs/decisions/ |
| #29 Verify teammate IAM | Prit | 2026-10-15 | done | PR #51 merged. All 6 members granted, plus `roles/iam.serviceAccountUser` on `airflow-vm-sa` (scoped) for OS Login. Teammate start and SSH verified; VM stopped. |
| #49 Decide who gets sudo on the Airflow VM | Prit | 2026-10-17 | in review | Owner call: owner plus Srikar get `roles/compute.osAdminLogin`, bound to the `airflow-vm` instance only, through the new `vm_admins` variable. Plan-only; apply from main after merge. |
| #50 Let Kirtan add secret values | Prit | 2026-10-16 | done | PR #54 merged. secretVersionAdder for `secret_writers` on both secret slots (secret level); ADR-019. |
| #48 Lifecycle rule for noncurrent versions on the data bucket | Prit | 2026-10-18 | done | PR #56 merged; noncurrent versions in the data bucket deleted after 30 days |
| #9 Static layers | Alekhya | 2026-10-13 | done | PR #62 merged; results below |
| #10 Decision: lock the modeling unit | Alekhya | 2026-10-13 | done | PR #66 merged; ADR-004 proposed (team sign-off pending): neighborhood x day, 24 tract-built neighborhoods, surge in any storm group |
| #31 Repo layout and pipeline pins | Prit | 2026-10-16 | done | PR #58 merged; ADR-017: one shared Airflow stack at the root, each component mounts its `dags/` at `/opt/airflow/dags/<component>` in snake_case; `Data-Pipeline/requirements.txt` pinned, 94 tests pass in a clean Python 3.12 venv |

## Blocked
None.

## Risks raised
- Legacy storm groups cover only 6.9% of legacy requests (210,049 of 3,037,969), against about 15% in the pre-scoping check. The groups now live in `docs/data/category-map.csv` (`storm_group` column) and may be narrower than the first run's. Worth a look before #10 locks the unit.
- **New-system times are Boston local, not UTC.** `open_date` ends in `+00`, but 1,283 cases resubmitted from the legacy system open a median 2.6 minutes before the legacy close time, in both EST and EDT. Real UTC would be 4 to 5 hours later. Converting from UTC would shift every new-system time 4 to 5 hours early and move 1,124 requests (1.6%) to the previous day. The pipeline treats them as local and re-checks this on every run (`map_311.check_clock`). Recorded in ADR-020, accepted by the team on 2026-10-16; CLAUDE.md is updated to match.
- **Flooding has no new-system category.** Legacy flooding requests (mostly Catchbasin) stop after Jul 2026 and nothing in the new system replaces them, so live flooding counts are zero. Affects the target; ADR-005 escalates it, since no baseline can fix a missing source. Needs a team decision (#10, #16) before the target freeze.
- **Snow has not been through the new system yet.** Snow Plowing and Emergency Responder Snow Plowing exist (2 requests), but all of winter 2025 to 2026 went to the legacy system. Unshoveled Sidewalk (48% of last winter's snow requests) has no new category so far. The first snow storm this winter is the real switch for snow_ice; ADR-005 sets a re-check for it (runbook).
- Five new-system categories appeared after the W2-1 run (Illegal Parking 909, Abandoned Vehicles 51, Parking Meter Repairs 13, OPC Public Inquiry 2, Moving Permits 1; export of 2026-10-19). They are `unmapped` until `docs/data/category-map.csv` gets rows for them. None is storm related.
- Neighborhood names differ between systems: 23 legacy and 25 new-system names, 16 shared (the new system splits Allston and Brighton, adds North End, Chinatown, West End and others). #15 needs one unit list, from the spatial join or a name map, before daily counts.
- Animal requests are missing from 2025-06-12 (last legacy) to 2025-08-01 (first day of the new export).
- The local Airflow stack idles at about 0.95 GB of memory (scheduler 397 MiB, API server 294 MiB, DAG processor 193 MiB, Postgres 67 MiB; `docker stats`, 2026-10-16). The e2-medium has 4 GB, so tasks get roughly 2.5 GB once Ubuntu is counted (estimate). #20 should confirm headroom on the VM, and pipeline tasks should avoid loading every legacy year into memory at once.
- On all years (2015 to 2026), `map_311.py` peaks at about 3.5 GB, above the roughly 2.5 GB left for tasks on the e2-medium, and `legacy_311.py` at about 1.9 GB. #22 should split them per year or read fewer columns before they move into DAGs.
- **Four neighborhood lists.** The BPDA boundaries have 26 neighborhoods, the tract-built boundaries and the 2025 population tables have 24, and 311 has 23 legacy and 25 new-system names. ADR-004 (proposed) uses the 24 tract-built neighborhoods and places requests by coordinates, so the 311 field is not used for the unit.
- **Social vulnerability uses 2010 tracts** (180, `GEOID10`), while every other layer uses 2020 tracts (207). Joining it to 2020 tracts needs a crosswalk or an area overlay (#14).
- **Climate Ready Boston flood layers are coastal only.** Most legacy flooding requests are catch basins (stormwater), which these maps do not cover. No stormwater flood layer is on Analyze Boston.
- **The Airflow image lacks the geo packages.** geopandas, shapely, pyogrio and pyproj are not in the Airflow constraints file. `Data-Pipeline/requirements.txt` pins them at the versions the tests passed on (PR #58), but the Airflow image needs them installed before #22 runs the static-layers step in a DAG (ADR-017).
- The 2026-10-20 export moves the first new-system date of Broken Park Equipment (not storm related) from 2026-03-10 to 2026-01-09. `docs/data/change-points.csv` is not updated in #10; the next `map_311.py` run in a PR should refresh it.

## W1-10 results (2026-10-20)

Commands: `legacy_311.py`, `new_311.py`, `map_311.py` (3,110,129 combined rows), `static_layers.py`, then `compare_units.py`. Data: Analyze Boston files downloaded 2026-10-20, days 2015-01-01 to 2026-10-18. 216,104 of 216,863 storm-related requests (99.7%) are placed by coordinates; 546 have none, 213 fall outside Boston's tracts, 13 are in tract 9815.02, which has no neighborhood.

ADR-005 baseline, analysis grid only (k 2, m 3 shown; #16 freezes the rule):

| unit | columns | empty, train | empty, 2024 to 2026 | surge, train | surge, 2024 to 2026 | columns with no surge |
|---|---|---|---|---|---|---|
| citywide x day x group | 3 | 37.1% | 37.5% | 5.38% | 5.58% | 0 |
| neighborhood x day, pooled | 24 | 59.1% | 55.7% | 3.97% | 5.12% | 1 |
| **neighborhood x day, any group** | 24 | 59.1% | 55.7% | 4.85% | 5.90% | 1 |
| neighborhood x day x group | 72 | 84.7% | 83.4% | 1.65% | 1.99% | 9 |
| tract x day, any group | 207 | 89.6% | 87.6% | 1.32% | 2.27% | 5 |
| tract x day x group | 621 | 96.4% | 95.8% | 0.44% | 0.76% | 197 |

Chosen in ADR-004 (proposed): neighborhood x day, any group. About 22 surges per neighborhood a year; Harbor Islands has none. Flooding alone drives 3.3% of training surges (4.1% in 2024 to 2026). The surge rate is above the spec's 1.3 to 2.9% pre-scoping range at every grid point (3.4% to 6.0% in training), which #16 should weigh when it sets k and m.

## W1-9 results (2026-10-18)

Command: `python Data-Pipeline/scripts/static_layers.py` (add `--download` on a fresh clone). Sources, vintages, licenses and SHA-256 hashes are in `docs/data/static-layers.csv`; the layer list is `Data-Pipeline/config/static_layers.csv`. All layers are PDDL. Vector layers are stored in EPSG:4326 as GeoParquet in `Data-Pipeline/data/interim/static/`.

| layer | rows | source CRS | notes |
|---|---|---|---|
| neighborhoods_bpda | 26 | EPSG:4326 | 1 invalid polygon repaired; not official boundaries |
| neighborhoods_by_tract20 | 24 | EPSG:4326 | whole 2020 tracts |
| tracts_2020 | 207 | EPSG:4326 | |
| crb_social_vulnerability | 180 | EPSG:4326 | 2010 tracts |
| crb_flood_9in_10pct | 1 | EPSG:3857 | 9 inch sea level rise (2030s), 10 percent annual flood; repaired |
| crb_flood_9in_1pct | 1 | EPSG:3857 | 9 inch sea level rise (2030s), 1 percent annual flood; repaired |
| population_2025_neighborhood | 24 | table | 306 columns |
| population_2025_tract | 207 | table | 308 columns; 1 blank export row dropped |
| population_data_dictionary | | document | 286 variables |

ACS demographics come from the city's 2025 population estimates (population on 2025-01-01, characteristics from ACS 2019 to 2023 5-year), so no Census API key is needed. They cover race and ethnicity (B03002), age (B01001), poverty (B17001), household income brackets (B19037), tenure (B25042), vehicles (B08201), language at home (C16001) and disability (S1810). There is no median household income column.

## W1-11 results (2026-10-19)

Command: `python Data-Pipeline/scripts/live_baselines.py` (plus `--window-days 84` and `--drop-after-switch`, same verdicts). Data: legacy files 2015 to 2026 and the new-system export downloaded 2026-10-19, combined with `map_311.py`; last full day 2026-10-17; Logan GHCN-D with WSF5 gusts.

| series | switch | step after/before vs normal years | replay, method A (k 1.5) | verdict |
|---|---|---|---|---|
| tree | 2025-10-27 | 0.34 vs 0.37 to 0.70 (lower, -31%); monthly 0.82 of the previous 3 years | quiet days flagged 1.2% (normal 2.5%), weather days 15.2% (normal 16.3%) | pass |
| Street Light Outage | 2025-10-22 | 1.23 vs 0.84 to 1.34 (no shift) | quiet days 5.6% (normal 4.8%) | pass; trailing methods B and C fail |
| snow_ice | 2026-06-23 | not testable: no new-system winter; 50.4% of last winter's requests have a new category | | re-check at first snow |
| flooding | none | no new-system source | | escalated |

Decision in ADR-005: method A (same day plus or minus 14 days in each of the previous 3 years, both systems, broad storm groups).

## W2-1 results (2026-10-16)

Commands: `python Data-Pipeline/scripts/legacy_311.py`, `python Data-Pipeline/scripts/new_311.py`, then `python Data-Pipeline/scripts/map_311.py`. Data: legacy files 2015 to 2026 and the new-system export (2025-08-01 to 2026-10-12).

### Category map

`docs/data/category-map.csv` lists all 197 legacy types and 71 new-system categories. Each maps to a shared `category` (the new-system name where one exists) and a `storm_group`. A category not in the file maps to `unmapped` and is counted, so a new city category shows up in the run output instead of disappearing. New tree categories take the group of the legacy types that feed them, so `tree` means the same thing before and after the switch.

| system | rows | same | renamed | merged | split | legacy or new only | unmapped | excluded |
|---|---|---|---|---|---|---|---|---|
| legacy | 3,036,686 | 126,864 | 1,169,732 | 220,374 | 179,661 | 1,340,054 | 0 | 1 |
| new | 68,542 | 2,578 | 32,867 | 12,496 | 12,104 | 8,441 | 56 | 0 |

The 56 unmapped new-system rows have a blank category in the export. The excluded legacy row is "Test Case A". 1,283 legacy rows that were resubmitted to the new system are dropped, so those cases count once. Another 342 legacy rows name a new case that is not in the export (56 predate it); they are kept, since the legacy row is the only copy.

2,519 legacy rows were opened on or after their own category's switch date (27 categories, 20 with more than one row, up to 110 days after). Nothing links them to a new-system case, so they are kept and marked with the `after_switch` column. What to do with them is #53.

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
