# ADR-037: Evening forecast inputs: one as-of date, tomorrow's GFS and tides from the same sources, rows from the history code

- Status: proposed
- Date: 2026-11-20
- Owner: Srikarmk
- Phase: deploy

## Context

`daily_pipeline` builds the history: labels and features for every day the 311 counts cover. The
evening run on the VM (D8) also needs the row for tomorrow, D = A + 1 on the evening of A, built
from what is known at the 18:00 cutoff (ADR-003, ADR-014), so the model service can score it.
Four things stood in the way (#160):

- **No shared date.** Each download and reader took "today" from the clock, some in UTC
  (Open-Meteo's `yesterday`, the tide half-years, the feature readers' current period) and one in
  Boston (`alert_history.py`). The run D8 schedules at 18:05 Boston starts at 23:05 UTC in winter,
  so tasks that start after 00:00 UTC would see the next day.
- **No tomorrow.** The GFS day-before set ended at 23:00 UTC yesterday and the tide predictions at
  today, while D's Boston station day runs to 05:00 UTC on D + 1.
- **No freshness check in the run.** `alert_history.py --check-nws` (ADR-014) existed, but no task
  ran it.
- **Counts that stop early.** The counts ended on the day before the earlier of the two 311
  systems' last request. On 2026-11-19 the legacy system's last request was on 2026-11-10 and the
  new system's that morning: legacy now holds only categories outside the storm groups, so the
  counts stopped on 2026-11-09, and D's features, which read 311 days up to D - 2, could not be built.

What the Previous Runs API returns for hours ahead (checked 2026-11-19 at 23:15 EST): every hour of
the next four days has values, and from 2026-11-21 on `previous_day1` equals `previous_day2`. For an
hour whose day-before run has not started, the API fills both from the newest run. The same
hour's archived value comes later, from its own run.

## Decision

1. **One as-of date per run.** `DateRange.as_of` is the Boston date of the DAG run's `run_after`,
   the moment it was triggered or scheduled, unless conf passes `as_of`. Every task computes it
   from `run_after`, so a run that crosses UTC midnight keeps one date. The downloads
   (`noaa_logan.py`, `tides.py`, `open_meteo.py`, `alert_history.py`), `feature_table.py` and
   `validate_data.py --latest` take it as an argument instead of reading the clock. A DVC stage
   takes `--as-of` or `BRT_AS_OF`, else the Boston date when the stage starts.
2. **An evening run** is one triggered at or after the as-of date's cutoff (`forecast_cutoff_local`,
   18:00 Boston). Only an evening run checks the IEM pull against `ingest_nws_alerts`' snapshot
   (ADR-014; the snapshot task now runs first), stores the evening GFS answer, and writes
   tomorrow's rows. A run by hand earlier in the day builds the history only.
3. **The GFS day-before set ends tomorrow** (end rule `tomorrow`: 23:00 UTC on A + 2), from the same
   Previous Runs API and the same request. Its current year is downloaded again on every run, so
   hours stored as forecasts are replaced by archived values once their day is over. The
   availability rule of `open_meteo_forecast.daily_forecasts` is unchanged: day D uses a
   `previous_day1` value only if its run started 7 hours before the cutoff, otherwise
   `previous_day2`, so the hours the API fills from a newer run are never used.
4. **Tide predictions run to A + 2**; observed tides are unchanged (fetched to A, never a feature).
5. **Tomorrow's rows come from the history code.** `forecast_rows.py` calls
   `feature_table.build_features` with its leakage checks, on the full build's counts extended in
   memory with zero-filled days after their last day, and writes
   `Data-Pipeline/data/forecast/<as_of>/features.parquet` as the `/predict` request rows of
   `docs/contracts/predict.md` ("The forecast rows file": `unit`, `forecast_date`, `as_of` and
   the catalog's `use_in_model = yes` inputs, with the contract's types), so the evening run
   sends the file as it is. It stops when the counts end before
   D - 2, when the GFS year files with D's hours were fetched before the cutoff, or when a weather
   or tide feature of D is missing. The zero days are never read (every 311 feature stops at D - 2)
   and never written to `daily_counts`, `labels` or `features`. The step is not a DVC stage: its
   rows come from one evening's live downloads.
6. **Parity is measured.** An evening run stores the GFS answer for A to A + 2 in the set's state
   folder (`evening/<as_of>.json`). `forecast_parity.py` compares each one, once D is over and the
   year files were fetched after D + 1 12:00 UTC, with the archive through the same rule. The
   tolerance is 0.05 per daily value, in its own unit (mm, °C, m/s): the API stores one decimal.
7. **The counts end on the new system's last full day** once every storm group both systems carry
   has switched (the `storm_group` rows of `change-points.csv`, the last on 2026-06-23). Before
   that, the earlier system still sets the end. Flooding, the one storm group with no new-system
   category, counts zero after legacy's last flooding request. It is in neither the labels nor the
   311 features (ADR-027, `labels_features.csv`).

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| As-of date from the clock in each task (before) | none | none | a run past UTC midnight mixes two dates; silent |
| As-of date from XCom, set by `check_range` | none | low | every task depends on one task's output; a cleared task reads a stale value |
| Tomorrow from Open-Meteo's forecast API (`api.open-meteo.com`) | none | low | for GFS it returns no `previous_dayN` values (null on 2026-11-19), so the training rule cannot be applied |
| Tomorrow's GFS from the newest run, ignoring the availability rule | none | low | the model sees fresher forecasts live than in training: skew, not visible |
| Counts end at the earlier system's last request (before) | none | none | live counts stop at legacy's last request (2026-11-09 on 2026-11-19); tomorrow's rows fail every evening |
| Counts end when legacy is a set number of days behind | none | low | one stray legacy request pulls the end back again |

## Consequences

- The evening run fails loudly instead of writing a row from missing or early data: an IEM pull
  without an NWS message, GFS fetched before 18:00, counts behind D - 2, or a missing weather or
  tide value. D8's Slack alert reports it, and a rerun after the cause clears writes the rows.
- The GFS raw folder now holds forecasts for the last two days, replaced on the next run. A full
  build on the same raw files gives the same labels and features: no history day reads those
  hours. On the locked inputs (`a719afeb`), the counts' new end adds 2026-10-26 to the counts, labels
  and features (legacy's last request was 2026-10-26 22:28, the new system's 2026-10-27 08:27),
  and every other row is unchanged.
- The rows file follows the `/predict` request contract (#159). A change to the catalog's
  `use_in_model` column changes the file's columns and is a new contract version, announced in
  the PR that changes the catalog.
- One more Open-Meteo request per evening (the evening answer).

## Revisit when

- `forecast_parity.py` shows a day over the tolerance: the API filled an hour the rule uses, or
  GFS runs arrived more than 7 hours late.
- The legacy 311 system starts receiving a storm category again (a new `change-points.csv` storm
  group row), or the city moves flooding requests to a new-system category.
- D8 moves the evening run's start past 23:59 Boston, which would change its as-of date.
