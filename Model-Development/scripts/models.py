"""The models train.py can train by name (#106, ADR-027). M4 (#107) adds logistic regression,
LightGBM and XGBoost here behind the same interface.

Every model has two methods:
- fit(rows, label, weight): learn from the train split's rows (unit, date and the model inputs),
  their 0 or 1 labels and their sample weights (training_data.load_table);
- predict_proba(rows): one surge probability from 0 to 1 per row.

build(name, table, settings) makes a model. table is the whole labeled table of every split, which
only a history model such as the seasonal baseline reads, and only into the past.
"""

from __future__ import annotations

from seasonal_baseline import SeasonalBaseline


def _seasonal(table, settings) -> SeasonalBaseline:
    return SeasonalBaseline(table[["unit", "date", "label"]], years=settings.seasonal_years,
                            half_width=settings.seasonal_half_width_days, min_days=settings.seasonal_min_days)


MODELS = {"seasonal": _seasonal}


def build(name: str, table, settings):
    """The model registered under name; fails naming the known models."""
    if name not in MODELS:
        raise ValueError(f"unknown model {name!r}; known models: {sorted(MODELS)}")
    return MODELS[name](table, settings)
