# ADR-002: 311 plus about five supporting sources, all public

- Status: accepted
- Date: 2026-10-04
- Owner: Srikarmk
- Phase: data

## Context

The forecast needs storm-related 311 demand per neighborhood per day, the weather that drives it, and enough about each neighborhood to check fairness. Scoping listed more candidate sources than six people can ingest, test and keep running by Oct 25, and some had no history for the training years (ADR-003). ADR-001 trimmed the project to about five supporting sources; this ADR records which.

What a source had to meet:

- Public and free, or free with a key the team can store in Secret Manager.
- Relevant to storm-driven 311 demand, or needed for fairness slices.
- Enough history to train on 2015 to 2023 (ADR-003), or a clear role in validation, test or live only.
- Small enough to ingest, version and test in the Data Pipeline weeks.

## Decision

**Primary: Boston 311 service requests** on Analyze Boston (PDDL). The yearly legacy files and the new-system DataStore, combined through `docs/data/category-map.csv` (ADR-020 for the new system's clock). The BARI archive on Harvard Dataverse is a fallback for history, not a pipeline source.

**Supporting sources:**

| Source | What it gives | History | Access |
|---|---|---|---|
| NOAA GHCN-D, Boston Logan (USW00014739) | Observed daily rain, snow, max temperature, gusts | 1936 onward (gusts 1996) | Free CDO token |
| NWS forecasts and alerts | Live forecasts; active alerts for the event DAG | Live; alert history is #12 (ADR-014) | Free; identifying user agent |
| NOAA tides, Boston Harbor gauge | Water level for coastal flooding | Decades | Free |
| Open-Meteo history and archived forecasts | Forecast weather as the model sees it live | Archived forecasts 2021 onward | Free; CC BY 4.0, attribution required |
| City and Census geography and population | 2020 tracts, the 24 tract-built neighborhoods, 2025 population with ACS 2019 to 2023 characteristics, Climate Ready Boston flood-risk and social vulnerability layers | Static, vintage recorded (#9) | PDDL; Census key for any ACS pull beyond the city tables |

**Left out at scoping:** MBTA transit alerts, road work, street trees, cooling and warming centers, income-restricted housing. Most have no history for 2015 to 2023, and none was needed for the core forecast.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **311 plus about five supporting sources (chosen)** | $0 | One ingestion issue per source (#6 to #9) | Some features fail ADR-003 and are dropped, NWS alert history first |
| The full scoping list, 10 or more sources | $0 | About double the ingestion and test work | Sources without training history add work but no usable features |
| 311 and weather only | $0 | Lowest | No neighborhood demographics, so no fairness slices; no flood-risk context |
| A paid weather or forecast API | Monthly fee | Similar | Breaks the $50 per member budget; archived forecasts are free from Open-Meteo |

## Consequences

- Every source has an ingestion issue with a named owner: 311 (#6), observed weather, NWS and tides (#7), Open-Meteo (#8), static layers (#9).
- Licenses carry into the README and the data card (#43): PDDL for city data, U.S. government works for NOAA, NWS and Census, CC BY 4.0 with attribution for Open-Meteo.
- Keys live in Secret Manager (ADR-015, ADR-019), never in the repo.
- Which of these become model features is decided by ADR-003, not here.
- Static layers do not come from the training years: Climate Ready Boston is from 2016, the ACS characteristics from 2019 to 2023 and the population estimates from 2025. They describe neighborhoods, not days, are used as fixed descriptors (ADR-003), and are reloaded when the source updates.

## Revisit when

- A source stops publishing or changes terms.
- #12 finds no 2015 to 2023 alert history, so NWS alerts become live-only.
- The team adds a flooding source to replace the 311 flooding categories the new system dropped.
