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
