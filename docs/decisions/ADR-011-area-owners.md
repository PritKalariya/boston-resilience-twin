# ADR-011: Area owners

- Status: accepted
- Date: 2026-10-04
- Owner: team
- Phase: setup

## Context

Six people share one project, and each area needs one person accountable for it.

## Decision

| Area | Owner |
|---|---|
| Data pipeline | Srikar |
| Fairness and data quality | Alekhya |
| Forecast model, plus the response agent and critic | Piyush |
| Retraining controller | Punith |
| Deployment | Prit |
| Monitoring, plus the map, dashboards, storm replay, and the 14-day run | Kirtan |

Owners can change as the work shifts.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **One named owner per area (chosen)** | $0 | Low | An absent owner slows their area |
| No fixed owners | $0 | None up front | Unowned work drifts |

## Consequences

- Whoever makes a decision writes its ADR, whatever the area.

## Revisit when

- The way the team splits the work changes.
