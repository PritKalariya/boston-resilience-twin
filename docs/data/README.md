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

# NWS alerts

## alert-map.csv

One row per NWS alert type (VTEC phenomena and significance), for `alert_history.py` (#16, ADR-014). Changed only by reviewed PR, like `category-map.csv`.

| column | meaning |
|---|---|
| phenomena | VTEC phenomena code, for example `WS` (winter storm) or `CF` (coastal flood) |
| significance | `W` warning, `A` watch, `Y` advisory, `S` statement |
| alert_type | the alert's name |
| storm_groups | `snow_ice`, `tree`, `flooding`, several separated by `;`, or `none` |
| note | why, when it is not obvious |

An alert type missing from this file is kept as `unmapped`, feeds no feature, and is named in the run's output; add a row for it. NWS renamed several types during 2015 to 2026 (ADR-014), so old and new names both have rows. Some rows list types Boston has not had yet, so a first one is mapped.

Data: National Weather Service (public domain), archived by the Iowa Environmental Mesonet of Iowa State University. Alert-based values are not official NWS warnings.

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
