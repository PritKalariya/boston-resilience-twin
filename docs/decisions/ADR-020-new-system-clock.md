# ADR-020: New-system 311 times are Boston local, not UTC

- Status: proposed
- Date: 2026-10-16
- Owner: Srikarmk
- Phase: data

## Context

The new 311 system (BCS case ids) publishes `open_date` and `close_date` with a `+00` suffix, for example `2026-07-24 10:48:07.169+00`. CLAUDE.md, the spec and #13 assumed that means UTC and asked for a conversion to Boston time. The DataStore API returns the same strings as a text field, so this is how the city publishes it, not a side effect of the download.

Legacy times are Boston local. When a legacy case moved to the new system, its closure reason names the new case ("Resubmitted as BCS-00264059"). The new case is opened while the legacy one is being closed, so for these pairs the two clocks can be compared directly.

1,283 pairs match a case in the export (2026-10-16 data). New open time minus legacy close time:

| Legacy close in | pairs | median | 10th to 90th percentile | within 60 min | 3.5 to 5.5 h |
|---|---|---|---|---|---|
| EDT (UTC-4) | 1,060 | -2.5 min | -6.8 to -1.4 min | 97.7% | 0% |
| EST (UTC-5) | 223 | -3.4 min | -10.4 to -1.7 min | 96.0% | 0% |

If the `+00` times were real UTC, the gap would be about +4 h in EDT and +5 h in EST. It is a few minutes in both, so the published times are Boston wall-clock time with a wrong suffix.

## Decision

- New-system times are read as Boston local wall-clock time. The `+00` is ignored, nothing is shifted (`new_311.CLOCK = "local"`).
- Both systems are stored as naive Boston wall-clock timestamps, so `open_dt.dt.date` is the Boston date for every row.
- Raw files stay as published. This only changes how they are parsed.
- Every run re-checks the clock: `map_311.check_clock` fails the run if the median gap over resubmitted cases is more than 30 minutes (with at least 20 pairs). A value with a non-zero offset is dropped and counted as `unexpected_offset` instead of guessed at.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Treat as local, re-check every run (chosen) | $0 | low | if the city starts publishing real UTC, the check fails loudly and `CLOCK = "utc"` switches to conversion (already tested) |
| Treat as UTC and convert, as the criterion says | $0 | low | every new-system time moves 4 to 5 hours early; 1,124 requests (1.6%) land on the previous day, and hour-of-day patterns no longer match legacy |
| Ask the city and wait | $0 | unknown | blocks #6, #11 and W2-3 on an outside answer; the data already answers it |

## Consequences

- #6 still stores the raw files untouched. Anything that parses new-system times uses `new_311.to_boston`, not its own UTC conversion.
- Daily counts (W2-3) and labels (W2-4) get the right Boston date for requests near midnight.
- The check depends on resubmitted pairs. They stop growing once the migration ends, but the existing ones stay in the legacy files, so the check keeps working on the full history.
- CLAUDE.md's data rule and the spec are updated to match; #13's criterion 3 is amended after this merges.

## Revisit when
- `check_clock` fails, or the export starts using a non-zero offset.
- The city documents the field's time zone.
- The new system publishes an archive that goes back before 2025-08-01.
