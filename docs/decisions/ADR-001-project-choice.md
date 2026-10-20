# ADR-001: Project is Boston Resilience Twin, trimmed to one headline

- Status: accepted
- Date: 2026-10-04
- Owner: team
- Phase: setup

## Context

The team project is 80% of the IE7374 grade, and six people have about 650 working hours for the term. Ideas had to meet the course's needs (live public data, ground truth that arrives after the prediction, demographic bias slices, CPU-trainable models) and two team rules: feel relevant to the AI era, and show an improvement a judge can verify in 30 seconds. The two finalists, Boston Resilience Twin and LureLog, each got a near-complete scoping draft so the team could compare them in detail. Boston Resilience Twin won the team vote by majority.

## Decision

Build Boston Resilience Twin, proposed by Punith, trimmed to fit the team:

- Core: a daily next-day surge forecast of storm-related 311 requests per neighborhood, graded against the next day's reports, with the full MLOps loop around it.
- Single headline: a fairness-aware retraining controller (contextual bandit), compared with three baselines on held-out history and reported either way.
- Response layer: one agent plus a critic, not a coordinator with eight tools.
- About five supporting sources (ADR-002), one model registry (ADR-006), two monitoring tools (ADR-008).
- Stretch: Q-learning, the scenario explorer, multilingual messages. One past-storm replay stays as a backup demo.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Boston Resilience Twin as proposed: 8-tool agent layer, 10+ sources, scenario explorer | Higher Gemini and hosting spend | About 780 hours against 650 available | Runs out of time; ungraded agent work crowds out the graded MLOps loop |
| **Boston Resilience Twin, trimmed (chosen)** | Fits two $50 projects (ADR-009) | About 550 hours | Live surges may reflect the 311 migration rather than demand (ADR-005) |
| LureLog: new phishing domains from certificate transparency logs | A VM reading the log stream 24/7 | About 400 hours | Certificate logs show about a third of phishing sites, unlisted phishing becomes false negatives, labels take about 7 days, and bias slices are countries rather than people |
| DockSure: future Bluebikes dock availability, dropped before the final vote | Near zero | Lowest | Low novelty next to the Bluebikes app, a persistence baseline that is hard to beat at short horizons, and no station history until a collector runs |

## Consequences

- Ground truth arrives daily (provisional the next evening, final after 48 hours), so drift, grading, and retraining all fit inside the term.
- The 311 migration through fall 2026 is in scope: category mapping, UTC times in the new system, and switch dates as change points.
- The controller is a research question. If it doesn't beat the drift-threshold rule, the rule stays (ADR-007) and the result is reported.
- About 100 hours of slack. Anything beyond one agent plus a critic is stretch work.
- Area owners are set in ADR-011.

## Revisit when

- The week-1 check (ADR-005) can't separate live surges from reclassification.
- A milestone starts to slip. The stretch list and the response layer are the first places to look for cuts.
