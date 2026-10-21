# ADR-007: The drift-threshold rule retrains from day one; the contextual bandit replaces it only if it wins on replayed history

- Status: accepted
- Date: 2026-10-04
- Owner: Pu1ni2
- Phase: setup

## Context

The forecast model has to keep up with seasons, storms, the 311 migration and uneven reporting across neighborhoods, so the loop retrains automatically and promotes a new model only when it beats the current one. Deciding when to retrain is the project's headline (ADR-001): a fairness-aware retraining controller, compared with simple schedules on held-out history and reported either way.

Three facts shape the decision:

- The live run starts on Nov 16 and the 14-day unattended run right after it, so some retraining trigger has to work from the first day.
- The controller is a research question. It may never beat a simple rule, and its reward is noisy: surges are 1 to 3 percent of cells, so a 30-day window holds only a few positives per neighborhood (ADR-004).
- The controller has two weeks of the plan: training on replayed history Nov 2 to 8, and release Nov 19 to 25 if it wins. Cost-aware retraining (Cara) is the prior art for treating retraining as a decision with a cost.

## Decision

- **From day one of the live run, the drift-threshold rule triggers retraining.** It watches drift in weather, request mix and volume (Evidently), rolling 7- and 30-day F1, precision and recall on provisional and final grades, and the 30-day worst-group recall gap. The threshold values are not set here; alert thresholds are an open decision (CLAUDE.md).
- **A safety rule always retrains** past hard accuracy or fairness limits, whatever the rule or the controller decides.
- **Promotion is unchanged:** a retrained model replaces the current one only if it beats it, with automatic rollback.
- **The contextual bandit** decides when and how to retrain, trained on replayed history. It replaces the rule through CI/CD only if it beats the rule on replayed history, on accuracy, the fairness gap and compute. It is compared with never retraining, a weekly retrain and the drift-threshold rule, and the result is reported either way.
- **Q-learning is stretch work.**

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| Never retrain | $0 | None | The model goes stale through the seasons and the 311 migration, and nothing reacts |
| Fixed weekly retrain | VM time every week, needed or not | Low | Retrains when nothing changed, and waits up to a week after a real change |
| Drift-threshold rule only | VM time only when a threshold trips | Low | Hand-set thresholds: retrains on drift that does not hurt accuracy and can miss an accuracy drop with no input drift |
| Contextual bandit from day one | VM time to train the policy | High | An untested policy controls live retraining, with no fallback if its noisy reward teaches it a bad policy |
| **Rule from day one, bandit only if it wins on replay (chosen)** | The rule's VM time plus replay training | Medium | If the bandit never wins, the rule stays and the comparison is still reported |
| Q-learning controller | More replay training than the bandit | High | Needs far more replayed episodes than 2015 to 2026 history gives; stretch |

## Consequences

- The rule and its trigger metrics must exist before Nov 16. They are built in the Nov 9 to 15 milestone (drift-threshold retraining), with the monitoring alerts.
- The replay comparison becomes the bandit's promotion test in CI/CD: same history, same promotion rule, scored on accuracy, fairness gap and compute against never, weekly and the rule.
- The replay environment is built Oct 26 to Nov 1. It extends the replay in `Data-Pipeline/scripts/live_baselines.py` (ADR-005), which already labels history day by day the way the live job would.
- The result is reported in the final docs and at the Expo whether or not the bandit wins.
- Flooding in the training label cannot be confirmed live (ADR-004, #16). Until #16 decides, a precision drop on flooding forecasts could trip the rule on a label artifact, so trigger metrics are read on the groups that live grading can confirm.

## Revisit when

- The bandit beats the rule on replayed history: CI/CD promotes it, and this ADR records the result.
- During the 14-day run the rule fires on most days, or never.
- #16 decides how flooding enters the label, or #32 changes the fairness windows behind the worst-group gap.
- Time allows Q-learning.
