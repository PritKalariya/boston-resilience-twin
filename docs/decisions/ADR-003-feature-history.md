# ADR-003: A feature is used only if its history covers the training years

- Status: accepted
- Date: 2026-10-04
- Owner: Srikarmk
- Phase: data

## Context

Training uses 2015 to 2023, validation 2024, and test 2025 to mid-2026, split by time and never shuffled. Several useful inputs start later than 2015 or exist only live:

- Open-Meteo archived forecasts start in 2021.
- NWS alert history for 2015 to 2023 has no confirmed source yet (#12).
- New-system 311 fields start in Aug 2025 (ADR-020).

A model trained on years where a feature is missing either learns nothing from it or learns from filled-in values that never happen live. Live, the model sees forecasts, not observed weather, so the weather in training and the weather in production are not the same input either.

## Decision

- **History rule.** A day-level feature is used only if its source covers every training year, 2015 to 2023. A feature that misses a training year is dropped, not imputed or back-filled. Short gaps inside covered years (for example the 8 missing gust days at Logan) are allowed, counted and reported in the data card.
- **Weather.** Training uses observed weather (NOAA GHCN-D, Logan), which covers every training year. Validation and test swap in Open-Meteo archived forecasts for the same variables, and live uses forecasts. Because training sees the weather that happened, backtest scores are upper bounds and are reported as such.
- **311 features** (lagged counts by storm group) come from both systems through the category map, so they cover every year.
- **Static attributes** (tract and neighborhood geography, population and ACS characteristics, Climate Ready Boston layers) do not vary by day. They are allowed with their vintage recorded (`docs/data/static-layers.csv`), since the same values apply in training and live.
- A source that fails the rule can still drive the live system without being a model feature, for example NWS alerts starting the event DAG.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **History must cover the training years (chosen)** | $0 | Low | Some useful live inputs, such as alerts, may never become features |
| Use any feature, impute the missing years | $0 | Medium | The model learns from values that never occur live; backtests look better than production |
| Train only on 2021 onward, where archived forecasts exist | $0 | Low | Three fewer winters of storms, and the 2020 to 2021 years being down-weighted leaves even less |
| Forecast weather in training where it exists, observed before | $0 | Medium | Two weather regimes inside one training set; the model can learn the switch instead of the weather |

## Consequences

- #12 must find a 2015 to 2023 alert-history source or drop the alert feature; either way it is written as ADR-014.
- Validation and test results on archived forecasts are the honest estimate of live skill; training-period scores on observed weather are not.
- The data card (#43) lists each feature's coverage years and gaps.
- Features from the new 311 system alone (Aug 2025 onward) cannot be used.

## Revisit when

- #12 finds alert history for 2015 to 2023.
- An archived-forecast source back to 2015 becomes available.
- The training years change.
