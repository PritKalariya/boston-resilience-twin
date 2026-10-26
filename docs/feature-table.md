# Feature table (#16): design, evidence and remaining work

**State on 2026-10-26.** #7 and #8 are merged.
- **Built and wired.** The labels, the ADR-014 A+ alerts and the weather and tide features are built. `daily_pipeline` runs all of them: `feature_table` reads the NOAA Logan, GFS day-before and tide folders, and `ingest_open_meteo` is a real task.
- **Checked against real data:**
  - #7's and #8's own download code, through integration tests;
  - the stored GFS files;
  - one real half-year of tide predictions;
  - a limited build on this machine's data.
- **Not done:**
  - a build with real NOAA Logan files (they need `NOAA_CDO_TOKEN`, and none are on this machine);
  - the evening run (#39), listed separately below.

Current numbers: `docs/status.md` (#16 row, W2-4 results). Commands: CLAUDE.md and the runbook ("Surge labels and features (#16)").

## Columns

`Data-Pipeline/config/feature_catalog.csv` has one row per column of `features.parquet`: its group, source, first day, ADR-003 history rule and live rule (`pass`, `fail`, `depends` on another issue's work, or `n/a`), and `use_in_model`:

- `yes`: a model input;
- `no`: in the table but not a model input (row keys, information columns, and features that fail the live rule);
- `excluded`: deliberately not built, with the reason.

Two tests (`test_feature_catalog_matches_the_full_table`, `test_the_feature_table_joins_every_source_by_unit_and_date`) check that the catalog and the full table match both ways and that no `yes` column fails a rule.

| group | columns | source | model |
|---|---|---|---|
| keys | `unit`, `date`, `features_last_day`, `forecast_cutoff_utc` | | no |
| calendar | day of week, month, day of year | the date | yes |
| 311 | `snow_ice_*` and `tree_*`: day D-2 and the sums over 3, 7 and 28 days ending there | `daily_counts.parquet` (#15) | yes; flooding is excluded (no live source) |
| alerts | `alert_<group>_count` and `alert_<group>_warning` for snow_ice, tree and flooding | IEM VTEC archive (ADR-014 A+) | yes |
| static | 8 neighborhood attributes | static layers (#9) | yes |
| weather | `wx_precip_mm`, `wx_snowfall_mm`, `wx_tmax_c`, `wx_tmin_c`, `wx_gust_max_ms` | GHCN-D Logan (#7) to 2023; GFS day-before forecasts (#8) from 2024 | yes |
| weather information | `wx_source`, `wx_known_by_utc` | | no |
| weather lags | `obs_*_lag2`: Logan's observations of day D-2 | GHCN-D Logan (#7) | no: they fail the live rule |
| tides | `tide_pred_max_m` (and `tide_pred_hours`, information only) | CO-OPS 8443970 predictions (#7) | yes |

## Rules

Every feature for day D uses only what is known at the cutoff, 18:00 Boston time on D-1 (`forecast_cutoff_local`), converted with `zoneinfo`: 22:00 UTC under daylight saving time and 23:00 UTC under standard time.

**Alerts (`alert_history.py`, ADR-014 Option A+).**
- An alert counts for D when all three hold:
  - its first bulletin came before the cutoff;
  - its first-announced end is after D begins;
  - it was not cancelled or upgraded before that same cutoff.
- A cancellation or upgrade counts only when IEM records status `CAN` or `UPG` with `utc_expire` equal to `utc_updated`. Every other row keeps its first-announced end.
- A cancellation at or after the cutoff changes nothing. Extensions and shortened ends are not used.
- One VTEC event counts once, even when it covers Boston's zone and county with different first bulletins.
- From 15 December the next year's file is pulled too. A year is final once it was pulled at least 14 days after it ended.
- When each year was pulled is kept in `Data-Pipeline/data/state/nws_alerts/_downloads.json`, so `raw/` holds the IEM files and nothing else, as with #7 and #8.
- The evening freshness check (`--check-nws`) stops before writing if a Boston message sent before the cutoff is not in the pull yet.

**Weather (`weather_features.py`, `open_meteo_forecast.py`, ADR-003).**
- **Which source each row uses:**
  - days up to 2023-12-31 take Logan's observed GHCN-D values for day D, so backtests on them are upper bounds;
  - every later day takes the GFS day-before forecasts, never an observation of its own day, and `wx_source` records which.
- **Which forecast each hour uses:**
  - a `previous_dayN` value for hour h comes from the newest GFS run (00, 06, 12 or 18 UTC) that started at least N days before h;
  - it counts as available 7 hours after that run started (`forecast_issue_margin_hours`);
  - so `previous_day1` comes from runs up to 12 UTC on D-1, which give D's hours up to 17:00 UTC, and `previous_day2` covers the rest;
  - `wx_known_by_utc` is the latest availability time behind the values a row keeps, and the leakage check compares it with the cutoff.

  The evidence is in "How the sources were checked".
- **How hours become days:**
  - forecast hours are combined over Logan's station day, midnight to midnight EST, the day GHCN-D values follow;
  - a day with a missing hour is blank;
  - GFS has no values before 2024-01-21 and no snowfall before 2024-12-07 (first full day), and those gaps stay blank.
- **Lags:** `obs_*_lag2` take Logan's day D-2 and stay out of the model.

**Tides (`tide_features.py`).** `tide_pred_max_m` is the highest hourly prediction of the Boston local day (23, 24 or 25 hours with daylight saving), blank unless every hour is present. Observed water levels are never read.

## How the sources were checked

- **Formats.** `test_feature_sources.py` runs #7's `noaa_logan.refresh` and `tides.refresh` and #8's `open_meteo.run_download` on fake answers in each source's format, reads the files they write with #16's readers, and builds the full table from them. If #7 or #8 change what they store, these tests fail.
- **Real files on this machine (2026-10-26).**
  - The stored GFS set-c files for 2024 to 2026 match the reader: UTC, `YYYY-MM-DDTHH:MM`, and the unit strings `mm`, `cm`, `°C` and `m/s`.
  - One real CO-OPS half-year of predictions (2025-H1, 4,344 hours), fetched through #7's own request, check and store code into a scratch folder, reads correctly: 23 hours on 2025-03-09, daily highs of 2.87 to 3.02 m.
  - Not here: NOAA Logan files (they need `NOAA_CDO_TOKEN`) and NWS alert snapshots.
- **When a GFS value became available.**
  - Open-Meteo's Previous Runs documentation defines `_previous_day1` as "the value that was predicted 24 hours before valid time".
  - Matching the stored values with single GFS runs (Open-Meteo's Single Runs API, 2026-04-15 and 2026-10-20, 48 of 48 hours) showed runs 24 to 29 hours before the hour for `previous_day1` and 48 to 53 for `previous_day2`.
  - Open-Meteo's run metadata showed the 2026-10-26 12 UTC run available 5.6 hours (GFS 0.25 degree) and 6.0 hours (0.13 degree) after it started, and Open-Meteo advises waiting 10 more minutes.
  - On the stored files, the newest run behind every day from 2024-01-21 to 2026-10-21 is 12 UTC on D-1, at least 3 hours before the cutoff.
  - Limits: the delay was measured on one run, and single runs are kept only from 2026-04-02. The 12 UTC run would have to be more than 10 hours late to miss the cutoff, and any margin from 6 to 10 hours gives the same choice of runs.

## Acceptance criteria

| criterion | state | evidence |
|---|---|---|
| Grid of k 1.5 to 2 and m 3 to 5 compared with the top-10-percent rule | met | `Data-Pipeline/config/surge_rules.csv`: 9 sd rules and 2 q90 rules; `surge_labels.py --grid` on real counts (status, W2-4 results); `test_grid_has_one_row_per_rule_from_the_same_cells` |
| Surge rate per option, overall and per slice | met | for every rule: overall, per period (train, validate, 2025 onward), per storm group, the per-neighborhood range and the neighborhoods with under 5 validation surges; per neighborhood for the chosen rule (status, W2-4 results) |
| One recommendation, deterministic label code | met | `sd_k2_m5` chosen, provisional until the freeze ADR; labels byte-identical across three real builds; `test_same_counts_give_the_same_labels_in_any_row_order`, `test_the_same_inputs_in_any_row_order_give_the_same_table` |
| pytest tests including empty, missing and malformed input | met | labels: empty, missing columns, malformed counts, rules and settings; features: empty counts, malformed counts, missing static, weather or tide days; alerts, weather, forecasts and tides: missing files, error pages, wrong units, formats and numbers |
| Feature table: counts, weather (observed and as-forecast), tides, alerts and static attributes, keyed (unit, date) | met in code and tests; real data in part | joined and checked end to end from #7's and #8's own writers (`test_feature_sources.py`); real build of counts, A+ alerts and static (103,440 rows, one per unit and day); real GFS and tide files read. **Not yet:** a real build with NOAA Logan files |
| Leakage test: every feature for D uses only data available before the cutoff (ADR-003) | met, one assumption measured | `check_no_leakage` (311 days up to D-2, cutoff before D, no observed weather of its own day after 2023, no forecast available after the cutoff) and its tests; forecast timing verified as above, the delay measured on one run |

#16 is not complete until a build with real NOAA Logan files has run.

## Remaining work for the evening run (#39)

- Schedule the evening run after 18:00 Boston time. Run `ingest_nws_alerts` before `ingest_alert_history --check-nws` (today they run side by side, and the daily DAG does not pass `--check-nws`).
- Build a feature row for tomorrow: `build_features` writes rows only for days in `daily_counts`.
- Fetch tomorrow's inputs:
  - the GFS day-before hours for tomorrow, from the same Previous Runs API (`open_meteo.py` stops at yesterday);
  - tide predictions through tomorrow (`tides.py` stops at today).

  Until then, tomorrow's weather and tides are blank, not filled.
- Optionally, check Open-Meteo's run metadata before the evening build, so a 12 UTC run that is very late is seen rather than assumed.

## Other remaining work

- A full build with real NOAA Logan files (`ingest_noaa_logan` with `NOAA_CDO_TOKEN`) and the tide history, with the coverage per feature recorded in `docs/status.md`.
- ADR-014 is still proposed (#12, PR #88). Its January sentence needs the owner: it says re-fetching the previous year in January is not needed, but under A+ a cancellation in early January of an alert that started in late December is only in that file. The code re-pulls until the year is final.
- The D-2 observed lags fail the live rule while CDO runs about 5 days behind (#7). A lag of 6 days or a faster live source would change that.
- #18: version `raw/nws_alerts/` with the other raw folders; `state/` holds only run records.
- #17: expectations for the new feature columns.
- The rule is frozen by ADR before Model Development; `sd_k2_m5` is the recommendation, not the frozen rule.
- Later: IEM files start in 2015, so on 2015-01-01 and 2015-01-02 an alert that started in 2014 is missing; `--first-year 2014` would add it.

## Assumptions still to verify

| assumption | where | how to check |
|---|---|---|
| GFS runs keep arriving within about 6 hours (measured on one run) | `forecast_issue_margin_hours` | Open-Meteo's run metadata over a few weeks; below 10 hours nothing changes |
| The `previous_dayN` run choice holds for the 2024 and 2025 archive (checked on 2026 days) | `open_meteo_forecast.py` | single runs are kept only from 2026-04-02; Open-Meteo support |
| GHCN-D Logan days run midnight to midnight EST | #7's data card | NCEI GHCN-D documentation for USW00014739 |
| Real NOAA files are metric and a non-blank quality flag marks a failed check | `weather_features.py` | one real file from `ingest_noaa_logan` |
| An IEM year stops changing 14 days after it ends | `FINAL_AFTER_DAYS` | 2015 evidence: the longest wait from first bulletin to cancellation or upgrade was 60.6 hours |
| An NWS message sent before the cutoff is in an IEM pull made after it | `check_freshness` | a few evenings of #7 snapshots and IEM pulls; the lag line logs it |
| CO-OPS predictions downloaded now match the ones published in each training year | tides | small differences when datums are updated |
