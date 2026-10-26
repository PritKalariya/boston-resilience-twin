# Feature table (#16): design notes and pending work

**State on 2026-10-26: work in progress.** The labels, the feature table, the ADR-014 A+ alert rule and the weather and tide code are in `Data-Pipeline/scripts/` and pass their unit tests. The weather and tide code has not read a real #7 or #8 file yet, the `daily_pipeline` task does not pass it the weather and tide folders, the evening run is not scheduled, and no end-to-end build on real data has run since the A+ change. Current state: `docs/status.md`. Commands: CLAUDE.md and the runbook ("Surge labels and features (#16)").

## Columns

`Data-Pipeline/config/feature_catalog.csv` has one row per column of `features.parquet`: its group, source, first day, ADR-003 history rule and live rule (`pass`, `fail`, `depends` on another issue's work, or `n/a`), and `use_in_model`:

- `yes`: a model input;
- `no`: in the table but not a model input (row keys, information columns, and features that fail the live rule);
- `excluded`: deliberately not built, with the reason.

A test (`test_feature_catalog_matches_the_full_table`) checks that the catalog and the full table match both ways and that no `yes` column fails a rule.

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
- One VTEC event (type, number, year) counts once, even when it covers Boston's zone and county with different first bulletins.
- History and the evening run use the same functions.

**Downloads and New Year.**
- IEM files an alert under the year it starts, so from 15 December the next year is pulled too.
- A year is final once it was pulled at least 14 days after it ended. In the 2015 file, updates arrived up to 2016-01-05.
- `_downloads.json` in `raw/nws_alerts/` records each pull.

**Freshness (evening run).** `--check-nws` compares the IEM pull with #7's NWS active-alerts snapshot and stops before writing when:
- the pull or the snapshot is older than the cutoff;
- a Boston message sent before the cutoff is not in the pull;
- a cancellation or upgrade sent before the cutoff does not show yet.

It logs an upper bound on IEM's lag.

**Weather (`weather_features.py` and `open_meteo_forecast.py`, ADR-003).**
- **Which source each row uses:**
  - days up to 2023-12-31 (a constant tied to ADR-003's training years) take Logan's observed GHCN-D values for day D, so backtests on them are upper bounds;
  - every later day takes the GFS day-before forecasts and never an observation of its own day, and `wx_source` records which.
- **Which forecast each hour uses:**
  - a `previous_dayN` value for hour h counts as available at h minus N days plus `forecast_issue_margin_hours` (6), and day D uses it only if that is no later than D's cutoff;
  - `previous_day1` is preferred and `previous_day2` is the fallback;
  - in Boston time, `previous_day1` covers D's hours up to about noon;
  - `wx_known_by_utc` is the latest availability time used, and the leakage check compares it with the cutoff.
- **How hours become days:**
  - forecast hours are combined over Logan's station day, midnight to midnight Eastern standard time, the day GHCN-D values follow;
  - precipitation and snowfall are summed, with snowfall converted from cm to mm;
  - temperature and gusts take the highest or lowest value;
  - a day with a missing hour is blank.
- **Gaps:**
  - GFS forecasts start on 2024-01-21, and snowfall on 2024-12-06; those gaps stay blank;
  - a GHCN-D value with a quality flag is blank;
  - nothing is filled with an observation.
- **Lags:** `obs_*_lag<k>` take Logan's day D-k (`observed_weather_lag_days`, 2). Snow depth is excluded, since Logan reports none from 2015.

**Tides (`tide_features.py`).**
- `tide_pred_max_m` is the highest hourly predicted water level of the Boston local day (23, 24 or 25 hours with daylight saving), in metres above MLLW. It is blank unless every hour is present.
- Observed water levels are never read: they come after the cutoff, and verified data lags about a month.

## Interfaces to recheck when #7 and #8 merge

The readers were written against the open pull requests and fail with a clear error when a file does not match:
- **#8 (PR #76 at `c6e2423`, `FORMAT_CHECKED` in `open_meteo_forecast.py`).** Recheck:
  - the set folder `day_before_ncep_gfs_global`;
  - the time format `YYYY-MM-DDTHH:MM`, in UTC (`utc_offset_seconds` 0);
  - the columns `<value>_previous_day1` and `_previous_day2`;
  - the unit strings `mm`, `cm`, `°C` and `m/s`;
  - `FIRST_YEAR` 2024.

  Only the `<year>.json` files in the set's raw folder are read.
- **#7 (PR #90 at `8ab8492`, `NOAA_CHECKED` and `TIDES_CHECKED`).** Recheck:
  - GHCN-D results with `date`, `datatype`, `station`, `attributes` and `value` in metric units;
  - tide predictions with `t` (GMT) and `v` (metres above MLLW) in `<year>-H1` and `<year>-H2` files;
  - NWS snapshots named by NWS's `updated` time.

Every past year (or half-year) must be stored, and a missing one stops the run. Only the current one may be absent.

## Pending work

| step | waits on | what |
|---|---|---|
| Weather and tides in the `feature_table` task | #7 and #8 merged | pass `--noaa-dir`, `--forecast-dir` and `--tides-dir` and add the three folders to `config_in` in `pipeline_steps.step_plan` (block below). Before #7 and #8 land, the folders do not exist and `check_inputs` would stop the task |
| Evening run | #39 | run `ingest_nws_alerts` before `ingest_alert_history --check-nws`, both after 18:00 (today they run side by side); a feature row for tomorrow, since `build_features` writes rows only for days in `daily_counts` |
| Tomorrow's inputs | #7, #8 | tide predictions through at least tomorrow (#7's downloads stop at today); GFS day-before hours for tomorrow from the same Previous Runs API (#8's set c ends yesterday). Until then tomorrow's weather and tides are blank live |
| First real-data build | the rows above | rebuild `nws_alerts.parquet` under A+ (the first `--download` pulls every year once), build the full table with weather and tides, and record the coverage per feature in `docs/status.md` |
| ADR-014 wording | ADR owner (#12, PR #88) | ADR-014 says re-fetching the previous year in January is not needed; under A+ it is, because a cancellation in early January of an alert that started in late December is only in the previous year's file. The code re-pulls until a year is final; the text or the code changes |
| D-2 observed lags | #7 | CDO ran about 5 days behind on 2026-10-25, so the lags cannot be filled live; a lag of 6 days or more, or a faster live source, would change their live rule |
| DVC | #18 | track `raw/nws_alerts/_downloads.json` with the IEM files |
| Validation | #17 | expectations for the new feature columns |

The `feature_table` block of `pipeline_steps.step_plan` after #7 and #8 merge (today it passes only counts, alerts, static attributes and settings):

```python
        alerts, static, out = interim / "nws_alerts.parquet", interim / "static", paths.processed / "features.parquet"
        noaa, forecasts = raw / "weather" / "noaa_logan", raw / "open_meteo" / "day_before_ncep_gfs_global"
        tides = raw / "tides" / "predicted"
        return StepPlan(
            args=["--counts", counts, "--alerts", alerts, "--static-dir", static, "--settings", settings,
                  "--noaa-dir", noaa, "--forecast-dir", forecasts, "--tides-dir", tides,
                  "--start", days.start, *end, "--out", out],
            files=StepFiles(data_in=[counts, alerts], config_in=[static, settings, noaa, forecasts, tides], data_out=[out]),
            hint="Run daily_counts for the full build, ingest_alert_history, ingest_static_layers, "
                 "ingest_noaa_logan, ingest_tides and ingest_open_meteo first.",
        )
```

PR #90 also changes `pipeline_steps.py`, `daily_pipeline.py` and `test_pipeline_steps.py` (the `SCRIPTS` and `PLANNED` tables, `step_plan` and a new `StepPlan.env` field). Keep both sides when they meet, then apply the block above.

## Assumptions to verify

| assumption | where | how to check |
|---|---|---|
| A `previous_dayN` value comes from a GFS run made at least N days before the hour and published within 6 hours; Open-Meteo states no availability time | `forecast_issue_margin_hours` | Open-Meteo's Previous Runs API documentation; a wrong value only moves the noon boundary |
| GHCN-D Logan days run midnight to midnight EST | #7's data card | NCEI GHCN-D documentation for USW00014739 |
| Open-Meteo stamps precipitation, snowfall and gusts at the end of the hour they cover | `open_meteo_forecast.py` | Open-Meteo's variable documentation ("preceding hour") |
| #8's unit strings are exactly `mm`, `cm`, `°C` and `m/s` | `VALUES` | one real set-c file; a mismatch fails loudly |
| #7's GHCN-D values are metric (the files name no unit) | `weather_features.py` | one real file; #7 requests `units=metric` |
| A non-blank GHCN-D quality flag (the second field of `attributes`) marks a failed check | `weather_features.py` | NCEI GHCN-D readme |
| An IEM year stops changing 14 days after it ends | `FINAL_AFTER_DAYS` | in 2015 the longest wait from first bulletin to cancellation or upgrade was 60.6 hours |
| IEM answers a year with no alerts with an empty body or a header only | `download_years` | both are handled; the first run after 15 December shows which |
| An NWS message sent before the cutoff is in an IEM pull made after it | `check_freshness` | compare a few evenings of #7 snapshots with IEM pulls; the lag line logs it |
| CO-OPS predictions downloaded now match the ones published in each training year | tides | small differences when datums are updated |

Known limits:
- The freshness check sees only the active alerts in #7's snapshot for zone MAZ015. A county-only alert, or a cancellation that ended an alert before the snapshot, is not checked.
- IEM files start in 2015, so on 2015-01-01 and 2015-01-02 an alert that started in 2014 is missing. Labels and 28-day lags start later.
- Weather uses Logan's standard-time day, while 311 and tides use the Boston calendar day, one hour apart under daylight saving time.

## Tests

`python -m pytest Data-Pipeline/tests` covers the #16 code with fixture files shaped like IEM's, #7's and #8's, and no network:

- **Alerts:** cancellations and upgrades before, at and after the cutoff; the three conditions; extensions; zone and county counted once; daylight saving changes; New Year pulls; an evening pull and a later pull giving identical features; the freshness check.
- **Forecasts:** the first and last hours of the target day; the noon boundary in winter and summer; the margin; fallbacks; units; gaps; file checks.
- **Weather:** the ADR-003 split; no own-day observation from 2024; lags; quality flags; missing years.
- **Tides:** local-day boundaries; 23 and 25 hour days; missing hours; New Year; missing files.
- **Feature table:** leakage checks for observed weather and late forecasts; missing weather and tide days; the catalog.

On 2026-10-26, in the Airflow 3.3.2 image (Python 3.12.14): `ruff check .` clean, 499 tests pass. Nothing was run against real downloads or in Airflow.
