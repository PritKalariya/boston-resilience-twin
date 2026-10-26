Every source, its license and its known issues: [data-card.md](data-card.md).

# 311 category data

## category-map.csv

One row per source category in each system. Changed only by reviewed PR (CLAUDE.md).

| column | meaning |
|---|---|
| system | `legacy` (yearly files) or `new` (BCS export) |
| source_category | legacy `type` or new `service_name`, exactly as published |
| category | shared name; the new-system name when one exists |
| storm_group | `snow_ice`, `flooding`, `tree`, or blank |
| match | `same`, `renamed`, `merged` (several to one), `split` (one to several; mapped to the main one), `legacy_only`, `new_only`, `excluded` |
| note | why, when it is not obvious |

A category missing from this file maps to `unmapped` and is counted by `map_311.py`. When a new one shows up, add a row here. A category can only have one storm group.

## change-points.csv

Written by `map_311.py`. For every shared category and storm group that has rows in both systems: the first new-system date (`switch_date`), the last legacy date, and how many legacy rows came after the switch. A note marks switch dates that fall on the first day of the new export, where the real switch may be earlier.

# Static layers

## static-layers.csv

Written by `static_layers.py` from `Data-Pipeline/config/static_layers.csv`. One row per layer.

| column | meaning |
|---|---|
| name | layer name; also the parquet file name in `Data-Pipeline/data/interim/static/` |
| kind | `vector` (GeoParquet), `table` (parquet) or `document` (kept as downloaded) |
| file | raw file name in `Data-Pipeline/data/raw/static/` |
| source_url | where the file was downloaded from |
| publisher | who publishes it |
| vintage | the date or census year the data describes |
| license | all current layers are PDDL |
| source_crs | CRS of the raw file; GeoJSON without a `crs` member is EPSG:4326 |
| stored_crs | CRS of the stored layer (EPSG:4326 for vectors) |
| rows | features or rows after checks |
| retrieved | date the raw file was downloaded (UTC) |
| sha256 | hash of the raw file; a change means the source was updated |

# Open-Meteo weather

Hourly weather at Boston Logan, the point of the NOAA GHCN-D station USW00014739 (42.36057, -71.00975), from the Open-Meteo APIs. `open_meteo.py` reads the sets in `Data-Pipeline/config/open_meteo.csv` and stores each set in its own folder under `Data-Pipeline/data/raw/open_meteo/`, one JSON file per year, exactly as the API returned it: hourly, in UTC, gusts in m/s. Daily values and Boston-local days are built in #16, not here. How to run it: the runbook, "Weather download".

| set | what it is | source and model | dates | folder | validation and test |
|---|---|---|---|---|---|
| a | History: past weather rebuilt from observations (ERA5 reanalysis) | Historical Weather API, `era5` | 2021-01-01 to the last full day ERA5 has, about 5 days behind | `history_era5` | no |
| b | Archived forecasts, first-hours data: the first hours of each ECMWF IFS run joined into one series, so almost the real weather | Historical Forecast API, `ecmwf_ifs` | 2021-01-01 to 23:00 UTC on the day before the download | `first_hours_ecmwf_ifs` | no |
| c | Day-before forecasts: what GFS predicted about 1 day and about 2 days before each hour, both stored | Previous Runs API, `ncep_gfs_global` | 2024-01-21 to 23:00 UTC on the day before the download | `day_before_ncep_gfs_global` | yes |

Why three sets (Open-Meteo docs and test requests, 2026-10-21 and 22):
- The archived forecasts for 2021 to 2023 exist only as first-hours data. Each hour comes from a model run that started a few hours before it, so it is almost the real weather and would show validation and test the answer. Set b is kept and labeled, and is not used there.
- True day-before forecasts come from the Previous Runs API, which keeps what a model predicted 1, 2 or more days ahead. Its docs say most models start in January 2024, but ECMWF IFS, HRRR and GFS return no values for 2024-01-01 to 2024-01-07. GFS has full 1-day and 2-day values from 2024-01-21, so set c starts there and validation has no day-before forecasts for 2024-01-01 to 2024-01-20. ECMWF has no day-before values even on 2025-01-01, too late for validation.
- Set b uses ECMWF IFS, which has every value from 2021-01-01. GFS first-hours data starts 2021-03-23.

| value | unit | set a (ERA5) | set b (ECMWF IFS) | set c (GFS, `_previous_day1` and `_previous_day2`) |
|---|---|---|---|---|
| precipitation: rain, showers and snow as water | mm | every hour | every hour | every hour |
| rain | mm | every hour | every hour | from 2024-12-06 18:00 (1 day ahead) and 2024-12-07 18:00 (2 days ahead) |
| showers | mm | not offered by the history API | none 2021 to 2024; 2,232 of 8,760 hours in 2025; every hour in 2026 | every hour |
| snowfall | cm | every hour | every hour | as rain |
| temperature_2m | °C | every hour | every hour | every hour |
| wind_gusts_10m | m/s | every hour | every hour | every hour |

First full day of each set c value (one-day checks, 2026-10-22):

| set c value | 1 day ahead | 2 days ahead |
|---|---|---|
| precipitation, showers, temperature_2m, wind_gusts_10m | 2024-01-21 (2024-01-20 has the 1-day values but only 12 of 24 hours of the 2-day values) | 2024-01-21 |
| rain, snowfall | 2024-12-07 (first hour 2024-12-06 18:00) | 2024-12-08 (first hour 2024-12-07 18:00) |

Set c from 2024-01-21 to early December 2024 has no rain or snowfall, only precipitation, so #16 must decide how to handle snow there, for example from precipitation and temperature.

In sets a and b, rain plus showers plus snowfall as water (cm times 10 / 7) adds up to precipitation in every year. ECMWF moves shower rain out of `rain` into `showers` during 2025, so `precipitation`, or rain plus showers, is the value that means the same in every year. NOAA's PRCP, used in training, is total precipitation.

Each set also has `_manifest.json` (download time, start date, last hour stored, grid point, hours with data per value, whether the last day is complete) and `_runs.jsonl` (one line per run) under `Data-Pipeline/data/state/open_meteo/<set>/`, so the raw folder holds only the year files.

## open-meteo.csv

Written by `open_meteo.py`, one row per stored file. The data card (#43) can take these rows as they are. It is written only after a run with no failure and only when it changes; with `--only`, the other sets keep their rows.

| column | meaning |
|---|---|
| set, file | set name, and the year file under `Data-Pipeline/data/raw/open_meteo/` |
| label, validation_test | what the set is, and whether validation and test may use it |
| api_url, model | the Open-Meteo API and model |
| place, latitude, longitude | the point asked for |
| grid_latitude, grid_longitude, grid_elevation | the model grid point Open-Meteo answered with |
| first_hour, last_hour, hours | coverage, in UTC |
| values_with_data, values_without_data | which values have data in this file, with units; a value that does not fill the file also shows its hours with data and its first and last hour |
| license, license_url | CC BY 4.0 and its licence page |
| attribution, attribution_url | "Weather data by Open-Meteo.com" and https://open-meteo.com/ |
| credit, credit_url | the data provider's credit: the Copernicus line for ERA5, with the download year, ECMWF for ECMWF IFS, and NOAA NCEP for GFS (all listed on Open-Meteo's licence page) |
| changes | what #8 changed: nothing, the files are stored as returned |
| downloaded_at | when the file was downloaded (UTC) |
| sha256 | hash of the stored file |

Open-Meteo's licence asks for a link next to any place its data are shown, `<a href="https://open-meteo.com/">Weather data by Open-Meteo.com</a>`, a link to the licence, and a note of any changes. The root README has the line; the dashboard, map and briefs need it too.
