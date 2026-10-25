# ADR-003: Time-based splits, and a feature is used only if its history covers the training years

- Status: accepted
- Date: 2026-10-04
- Owner: Srikarmk
- Phase: data

## Context

The model forecasts tomorrow from what is known today, so every evaluation has to keep the future out of training. Storm seasons also differ a lot from year to year, so a split that mixes days from the same winter into train and test would score far better than the live system.

Several useful inputs start later than the 311 history or exist only live:

- Open-Meteo archived forecasts start in 2021.
- NWS alert history for the early years has no confirmed source yet (#12).
- New-system 311 fields start in Aug 2025 (ADR-020).

A model trained on years where a feature is missing either learns nothing from it or learns from filled-in values that never happen live. Live, the model sees forecasts, not observed weather, so the weather in training and the weather in production are not the same input either.

## Decision

- **Split by time, never shuffled:** train 2015 to 2023, validate 2024, test 2025 to mid-2026, live from deployment. 2020 and 2021 are flagged and down-weighted, as the spec sets out.
- **History rule.** A day-level feature is used only if its source covers every training year, 2015 to 2023. A feature that misses a training year is dropped, not imputed or back-filled. Short gaps inside covered years (for example the 8 missing gust days at Logan) are allowed, counted and reported in the data card.
- **Live rule.** A feature is used only if it is also available live with the same definition. 311 features are built per storm group from categories that carry over to the new system, and a group with no live source is dropped as a feature: flooding today, which has no new-system category (ADR-005). snow_ice lags will run lower live while about half of last winter's snow_ice requests have no new-system category; #16 decides whether to use them.
- **Weather.** Training uses observed weather (NOAA GHCN-D, Logan), which covers every training year. Validation and test swap in Open-Meteo archived forecasts for the same variables, and live uses forecasts. Because training sees the weather that happened, backtest scores are upper bounds and are reported as such.
- **311 features** (lagged counts by storm group) come from both systems through the category map, so they cover every year; they still have to pass the live rule.
- **Static attributes** (tract and neighborhood geography, population and ACS characteristics, Climate Ready Boston layers) do not vary by day. They are used as fixed neighborhood descriptors, with their vintage recorded (`docs/data/static-layers.csv`), since the same values apply in training and live. The population estimates (2025) and ACS characteristics (2019 to 2023) describe neighborhoods after most of the training years, so as model features they carry a little look-ahead. For fairness slicing that is what we want, since a neighborhood's group stays fixed across splits.
- A source that fails either rule can still drive the live system without being a model feature, for example NWS alerts starting the event DAG.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **Time-based split, history must cover the training years (chosen)** | $0 | Low | Some useful live inputs, such as alerts, may never become features |
| Random or shuffled split | $0 | Low | Days from the same storm land in train and test; scores look far better than live |
| Rolling-origin backtests only, no fixed test window | $0 | Medium | Every model choice sees the latest data, so there is no untouched estimate before deployment |
| Use any feature, impute the missing years | $0 | Medium | The model learns from values that never occur live; backtests look better than production |
| Train only on 2021 onward, where archived forecasts exist | $0 | Low | Three fewer winters of storms, and down-weighting 2020 to 2021 leaves even less |
| Forecast weather in training where it exists, observed before | $0 | Medium | Two weather regimes inside one training set; the model can learn the switch instead of the weather |

## Consequences

- Tuning and model choice use the 2024 validation year; the 2025 to mid-2026 test window is kept for the final comparison.
- Labels need baseline history (ADR-005), so labeled training days start in 2016 (ADR-004); 2015 supplies history only.
- The test window spans the 311 migration (ADR-020, ADR-005), so test results include the switch.
- #12 must find an alert-history source for the training years or drop the alert feature; either way it is written as ADR-014.
- Validation and test results on archived forecasts are the honest estimate of live skill; training-period scores on observed weather are not.
- The data card (#43) lists each feature's coverage years and gaps, and the vintage of each static attribute against the training years.
- Flooding lags stay out of the features while flooding has no new-system category; that follows the flooding decision in #16 and changes if a new category appears.
- Features from the new 311 system alone (Aug 2025 onward) cannot be used.

## Revisit when

- #12 finds alert history for the training years.
- An archived-forecast source back to 2015 becomes available.
- The training years change, for example to add 2024 once a newer validation year exists.
