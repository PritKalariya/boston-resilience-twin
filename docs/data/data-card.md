# Data card

Every source the project uses or plans to use: where it comes from, its license and how to credit it, what period it describes, how often it changes, and what is known to be wrong or missing. Which sources were chosen is ADR-002; which may become model features is ADR-003. Facts were checked against each source on 2026-10-25 unless a row says otherwise.

## Summary

| Source | Role | License | Pipeline today |
|---|---|---|---|
| [Boston 311, legacy yearly files](#boston-311-service-requests) | Target and lagged demand, 2015 to 2026 | PDDL | Parsed (`legacy_311.py`); files downloaded by hand until #6 |
| [Boston 311, new system](#boston-311-service-requests) | Target and lagged demand from 2025-08-01 | PDDL | Parsed (`new_311.py`); export downloaded by hand until #6 |
| [NOAA GHCN-D, Boston Logan](#noaa-ghcn-d-boston-logan) | Observed weather for training | U.S. government work | Downloaded daily (`noaa_logan.py`, `ingest_noaa_logan`, #7) to `raw/weather/noaa_logan/` |
| [NWS forecasts and alerts](#nws-forecasts-and-alerts) | Live forecast weather; alerts start the event DAG | U.S. government work | Snapshots each run (`nws.py`, `ingest_nws_forecasts` and `ingest_nws_alerts`, #7) to `raw/weather/` |
| [NOAA tides, Boston gauge](#noaa-tides-boston-harbor-gauge) | Coastal water level and surge | U.S. government work | Downloaded daily (`tides.py`, `ingest_tides`, #7) to `raw/tides/` |
| [Open-Meteo](#open-meteo) | Forecast weather for validation and test | CC BY 4.0, attribution required | In review (#8, PR #76) |
| [City and Census geography and population](#city-geography-population-and-climate-ready-boston) | Units, fairness slices, flood context | PDDL | Loaded (`static_layers.py`, #9) |

## Coverage against the training years

ADR-003 uses a day-level feature only if its source covers every training year, 2015 to 2023.

| Source | Covers | Training years 2015 to 2023 |
|---|---|---|
| 311 legacy files | 2011 onward (2015 onward used) | Yes |
| 311 new system | 2025-08-01 onward | No: new-system-only fields cannot be features |
| GHCN-D Logan: rain, snow, max and min temperature, average wind | 1936 onward; every day of 2015 to 2026 present (#7 download) | Yes |
| GHCN-D Logan: snow depth (SNWD) | None at Logan from 2015, though the station has it in older years | No: not a feature |
| GHCN-D Logan: gusts | 1996 onward; WSF5 misses 8 days in the training years (4 in 2015, 1 in 2020, 3 in 2021; ADR-003), WSF2 none | Yes, gaps counted and reported |
| NWS forecasts | Live only | No: live input, not a training feature |
| NWS alerts | Live; history for 2015 to 2023 is #12 (ADR-014) | Not yet: drop the feature if #12 finds no history |
| Tides, Boston gauge | 1921 onward | Yes |
| Open-Meteo history and first-hours forecasts | Downloaded from 2021 (#76's range; ERA5 itself starts in 1940) | No: not a training feature as stored |
| Open-Meteo day-before forecasts (GFS) | 2024-01-21 onward | No: validation (2024) and test only |
| Static layers | One vintage each (below) | Fixed neighborhood descriptors, not day-level (ADR-003) |

## Boston 311 service requests

- **URL:** https://data.boston.gov/dataset/311-service-requests (Analyze Boston). Yearly CSVs "311 SERVICE REQUESTS - 2011" to "311 Service Requests - 2026", and "311 Service Requests - NEW SYSTEM" with its own data dictionary and the city's Data Transition Guide on the same page.
- **License and attribution:** Open Data Commons Public Domain Dedication and License (PDDL). No attribution required; the README credits Analyze Boston.
- **Vintage:** legacy 2015-01-01 to the latest export; the new system from 2025-08-01. `Data-Pipeline/config/published_311_counts.csv` holds the published counts of the 2026-10-13 snapshot (legacy 2015 to 2026: 3,037,969; new system: 68,542), which `validate_raw` uses as a floor (#17). The 2026 file and the new-system export grow daily: the full build on 2026-10-20 read 3,039,046 legacy and 72,373 new-system rows.
- **Update frequency:** daily (dataset page: "Updated daily starting 2011").
- **Known issues:**
  - **Migration.** The city moved case types to a new backend in waves: animals by 2025-08-01 (the first day of the new export), most departments from October 2025 to September 2026 (street lights 2025-10-14 to 22, trees 2025-10-27; W2-1). The two systems have different schemas and categories that are not one to one; `docs/data/category-map.csv` maps them and `docs/data/change-points.csv` records each category's switch date.
  - **New-system clock.** `open_date` ends in `+00` but is Boston local time (ADR-020). Every run re-checks this (`map_311.check_clock`).
  - **Legacy rows after a switch.** 2,519 legacy rows were opened on or after their category's switch date; they are kept and flagged `after_switch` (ADR-021).
  - **City data error, fixed.** The dataset page notes that from 2026-08-12 case types moved to the new system during 2026 were mistakenly dropped from the 2026 legacy file, fixed as of 2026-08-25. A 2026 legacy file downloaded in that window undercounts.
  - **Flooding has no new-system category.** Legacy flooding requests stopped (last dates in ADR-005: Roadway Flooding 2025-10-22, Catchbasin 2026-06-23), so live flooding counts are zero (#16).
  - **Snow has not been through the new system.** Winter 2025 to 2026 snow requests all went to the legacy system, and Unshoveled Sidewalk (48% of last winter's snow requests) has no new category yet. The first storm this winter is the real switch for snow_ice.
  - **Neighborhood names differ** (23 legacy, 25 new-system names, 16 shared), so requests are placed by coordinates, not by name (ADR-004, #14).
  - **Gaps and odd categories.** Animal requests are missing from 2025-06-12 to 2025-08-01. New categories keep appearing in the export (five unmapped and not storm related as of 2026-10-19); they count as `unmapped` until the map gets a row.
  - **Timestamp formats.** Legacy `open_dt` mixes whole and fractional seconds; parsing without `format="ISO8601"` loses about half the rows (W1-5).
  - **Reported demand, not need.** 311 counts what people report, and reporting rates differ by neighborhood and population (the spec's drift story lists uneven reporting). A neighborhood with fewer requests may report less, not have fewer problems. This is the main bias caveat for the project; #32 measures requests per capita by income and demographic slice, and its results belong here when they land.
  - **Location quality** (#14). Requests with no usable unit: 18,594 without coordinates, 4,849 on the City Hall default point (4,808 legacy with neighborhood "Boston", 41 new-system at "1 City Hall Plz"; listed in `Data-Pipeline/config/placeholder_locations.csv`), 2,908 outside Boston's tracts, and 299 in tract 9815.02, which is in no neighborhood. The share falls from 2.05% in 2015 to about 0.5% from 2022, so early training years are slightly undercounted. 11 legacy rows with latitude and longitude swapped are swapped back and placed.
  - **Resubmitted cases.** 1,283 legacy rows resubmitted to the new system are dropped, so each case counts once; 342 legacy rows that name a new case missing from the export are kept (W2-1).
  - **Harbor Islands has no storm requests** in any year, so it has no surges and is left out of the worst-group metric (ADR-004).

## NOAA GHCN-D, Boston Logan

- **URL:** station USW00014739 (Boston Logan International Airport) in the Global Historical Climatology Network Daily, through the NOAA Climate Data Online API v2, https://www.ncei.noaa.gov/cdo-web/api/v2/ (free token; setup comes with #4, PR #77). Station data: https://www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries&stations=USW00014739
- **License and attribution:** U.S. government work, no restrictions. Credit NOAA National Centers for Environmental Information.
- **Vintage:** 1936-01-01 to 2026-10-20 at the check, so about 5 days behind today. Elements downloaded: PRCP (rain), SNOW, TMAX, TMIN, AWND, WSF2 and WSF5 (gusts, from 1996); SNWD is requested too but Logan reports none from 2015. Stored as returned under `raw/weather/noaa_logan/<year>.json`; a year is final 30 days after it ends.
- **Update frequency:** daily, a few days behind; recent days can be revised.
- **Known issues:** WSF5 (peak gust) misses 8 days in the training years (4 in 2015, 1 in 2020, 3 in 2021; ADR-003), counted and reported, not filled; 2024 and 2025 miss 3 each. No snow depth from 2015. Observed weather in training makes backtests upper bounds (ADR-003). Daily values follow the station's local standard-time day, not the Boston calendar day used for 311; #16 decides how to align them.

## NWS forecasts and alerts

- **URL:** https://api.weather.gov. Boston (City Hall, 42.3601, -71.0589) is grid point BOX 71,101 (forecast office BOX), forecast zone MAZ015, county MAC025.
- **License and attribution:** U.S. government work. Credit the National Weather Service.
- **Vintage:** live only. Forecasts cover the next 7 days; alerts are the ones active at request time.
- **Update frequency:** gridpoint forecasts update several times a day; alerts change as they are issued.
- **Known issues:** every request needs an identifying `User-Agent` or it gets 403 (the team's value comes with #4, PR #77). No archive: a forecast that is not saved when it is current is gone, so `nws.py` (#7) stores a snapshot each run, named by NWS's issue time (`updateTime`, `updated`). Alerts are requested for forecast zone MAZ015 and county MAC025 together: severe thunderstorm, tornado and flash flood warnings are coded by county and do not come back for a zone query (ADR-014). Alert history for the training years has no source yet (#12).

## NOAA tides, Boston Harbor gauge

- **URL:** CO-OPS station 8443970 "Boston", https://tidesandcurrents.noaa.gov/stationhome.html?id=8443970, data from https://api.tidesandcurrents.noaa.gov/api/prod/datagetter (no key).
- **License and attribution:** U.S. government work. Credit NOAA Center for Operational Oceanographic Products and Services (CO-OPS).
- **Vintage:** established 1921-05-03. Tidal datums on the 1983 to 2001 epoch, in feet unless metric is requested.
- **Update frequency:** observed water level every 6 minutes, hourly heights after verification; predictions are published ahead.
- **Known issues:** times come back in GMT, local standard time or local time with daylight saving depending on the request, so the request must name one. Recent data is preliminary until verified. Surge is observed minus predicted, so `tides.py` (#7) downloads both, in metres above MLLW and GMT, in half-year files (CO-OPS refuses more than 365 days per request). Verified hourly data lags about a month (on 2026-10-26 it ended 2026-09-30).

## Open-Meteo

- **URL:** https://open-meteo.com: the Historical Weather API (ERA5), Historical Forecast API (ECMWF IFS) and Previous Runs API (GFS), at the Logan point (42.36057, -71.00975). Details in PR #76.
- **License and attribution:** CC BY 4.0. Show "Weather data by Open-Meteo.com" with a link, as the README does, plus each model's own credit: Copernicus Climate Change Service for ERA5, ECMWF (CC BY 4.0) for IFS, NOAA NCEP for GFS. PR #76 writes one attribution row per stored file to `docs/data/open-meteo.csv`.
- **Vintage:** ERA5 history and ECMWF first-hours forecasts downloaded from 2021-01-01 (#76's chosen range; the ERA5 archive itself starts in 1940, so it could cover the training years if downloaded further back); GFS day-before forecasts from 2024-01-21.
- **Update frequency:** daily; ERA5 runs about 5 days behind.
- **Known issues (from PR #76):** the archived forecasts before 2024 are first-hours data, close to the real weather, so they must not be used for validation or test. GFS has no rain or snowfall values before 2024-12-06, only total precipitation. ECMWF moved shower rain from `rain` to `showers` during 2025, so total precipitation is the consistent value across years.

## City geography, population and Climate Ready Boston

One row per layer, with source URL, vintage, row count, download date and SHA-256, is in `docs/data/static-layers.csv` (written by `static_layers.py` from `Data-Pipeline/config/static_layers.csv`).

| Layer | Publisher | Vintage | Known issues |
|---|---|---|---|
| 2020 Census tracts (207) | Boston Planning Department | 2020 Census | |
| Neighborhoods built from 2020 tracts (24) | Boston Planning Department | 2020 tracts | The unit (ADR-004); same names as the population tables |
| BPDA neighborhoods (26) | Boston Planning Department | Current release, built from zoning, zip and 2010 tract lines | The city says they are not official boundaries; not used for the unit; 1 invalid polygon repaired on load |
| Population estimates, neighborhood and tract | Boston Planning Department Research Division | Population on 2025-01-01; characteristics from ACS 2019 to 2023 5-year | After most training years, so a little look-ahead as features (ADR-003). The only population vintage, so per-capita rates for 2015 to 2023 use 2025 population and cross-year per-capita comparisons carry that error. ACS comes from these tables, no Census API call. The tract table's blank export row is dropped on load |
| Climate Ready Boston social vulnerability (180) | City of Boston | 2016, 2010 tracts | Keyed by 2010 tracts; needs a crosswalk to 2020 tracts |
| Climate Ready Boston flood extents, 9 inch sea level rise, 10% and 1% annual chance | City of Boston | 2016 (2030s scenario) | Coastal flooding only; catch basin (stormwater) flooding is not covered. Published in EPSG:3857, reprojected and repaired on load |

- **License and attribution:** PDDL for every layer (Analyze Boston). ACS characteristics are U.S. Census Bureau data republished by the city.
- **Update frequency:** when the city publishes a new release; a changed SHA-256 in `static-layers.csv` shows it, and `static_layers.py --refresh` reloads them.

## Keeping this card current

Update the matching section in the PR that adds or changes a source, a vintage or a known issue. Coverage and gap counts for features are filled in by #16 when features are built.

Several of these issues are checked on every pipeline run by the #17 suites (`validate_raw`, `validate_processed`), with bounds in `Data-Pipeline/config/validation_bounds.csv`: the new-system clock, row counts against the published counts, coordinates inside Boston, and the share of requests placed each year. A run that breaks one fails and alerts (#34).
