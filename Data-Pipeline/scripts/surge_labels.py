"""Surge labels per neighborhood and day (#16, ADR-004, ADR-005).

Reads daily_counts.parquet from daily_counts.py (#15): one row per unit, day and
storm group, every day present and zero-filled. For each unit and storm group
the ADR-005 baseline (live_baselines.seasonal_baseline: the same day plus or
minus 14 days in each of the previous 3 years) is built from
`baseline_requests`, and the day's `requests` are compared with it. A group
surges under the chosen rule in Data-Pipeline/config/surge_rules.csv:

- sd: requests > mean + k * sd and requests >= m
- q90: requests > the 90th percentile of the same history and requests >= m

A cell (unit, day) is a surge when any group in `label_groups`
(Data-Pipeline/config/labels_features.csv) surges, and the groups that surged
are kept next to the label (ADR-004). A cell with fewer than 20 history days
has no baseline and no label (ADR-005): its label is missing, not false, so
label coverage can be reported. Everything is plain arithmetic on sorted data,
so the same counts always give the same labels.

The run writes labels.parquet, and stops before that when a day from --start to
--end is not in the counts (requested_days). With --grid it also prints the
surge rate of every rule in surge_rules.csv, overall and per period, storm group
and neighborhood, for the recommendation. The rule is frozen by ADR before Model
Development (CLAUDE.md); until then the chosen row is provisional.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from daily_counts import COLUMNS as COUNT_COLUMNS
from daily_counts import STORM_GROUPS
from live_baselines import seasonal_baseline
from measure_311 import markdown

RULE_COLUMNS = ["rule_id", "rule", "k", "m", "chosen", "note"]
RULE_KINDS = ("sd", "q90")
SETTINGS = ("label_groups", "feature_groups", "feature_min_lag_days", "forecast_cutoff_local",
            "forecast_issue_margin_hours", "observed_weather_lag_days")
KEY = ["unit", "date", "storm_group"]
BASE_COLUMNS = ["mean", "sd", "q90", "n"]
SPEC_RANGE = (0.013, 0.029)  # the spec's pre-scoping surge rate range
# Reporting periods by whole years, from ADR-003: train 2015 to 2023 (labels from 2016), validate 2024.
PERIODS = [("train 2015 to 2023", 2015, 2023), ("validate 2024", 2024, 2024), ("2025 onward", 2025, 9999)]
TRAIN = PERIODS[0][0]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    kind: str
    k: float | None
    m: int


def load_rules(path: Path | str) -> tuple[list[Rule], Rule]:
    """Every rule in the grid, and the one marked chosen. Raises on a malformed file."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(RULE_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing columns: {sorted(missing)}")
    if df.empty:
        raise ValueError(f"{path} lists no rules")
    rules, chosen = [], []
    for row in df.itertuples(index=False):
        rule_id, kind, k, m = row.rule_id.strip(), row.rule.strip(), row.k.strip(), row.m.strip()
        if not rule_id:
            raise ValueError(f"{path}: a rule has no rule_id")
        if kind not in RULE_KINDS:
            raise ValueError(f"{path}: rule {rule_id} has unknown rule {kind!r}; use one of {', '.join(RULE_KINDS)}")
        if not m.isdigit() or int(m) < 1:
            raise ValueError(f"{path}: rule {rule_id} needs m as a whole number of 1 or more, got {m!r}")
        if kind == "sd":
            try:
                k_value = float(k)
            except ValueError:
                k_value = 0.0
            if not math.isfinite(k_value) or k_value <= 0:  # nan or inf would pass k <= 0 and never fire
                raise ValueError(f"{path}: sd rule {rule_id} needs k above 0 as a finite number, got {k!r}")
        elif k:
            raise ValueError(f"{path}: q90 rule {rule_id} takes no k, got {k!r}")
        else:
            k_value = None
        flag = row.chosen.strip().lower()
        if flag not in ("yes", "no", ""):
            raise ValueError(f"{path}: rule {rule_id} has chosen {row.chosen!r}; use yes or no")
        rule = Rule(rule_id, kind, k_value, int(m))
        rules.append(rule)
        if flag == "yes":
            chosen.append(rule)
    ids = [rule.rule_id for rule in rules]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(f"{path} repeats rule_id {repeated}")
    if len(chosen) != 1:
        raise ValueError(f"{path} must mark exactly one rule chosen = yes, found {len(chosen)}")
    return rules, chosen[0]


def parse_groups(value: str, name: str) -> list[str]:
    groups = [g.strip() for g in value.split(";") if g.strip()]
    if not groups:
        raise ValueError(f"{name} names no storm group")
    unknown = sorted(set(groups) - set(STORM_GROUPS))
    if unknown:
        raise ValueError(f"{name} has unknown storm groups {unknown}; use {', '.join(STORM_GROUPS)}")
    if len(set(groups)) != len(groups):
        raise ValueError(f"{name} repeats a storm group: {value!r}")
    return [g for g in STORM_GROUPS if g in groups]


def parse_clock(value: str, name: str) -> str:
    """An HH:MM time of day, 00:00 to 23:59."""
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts) or int(parts[0]) > 23 or int(parts[1]) > 59:
        raise ValueError(f"{name} must be a time of day as HH:MM (00:00 to 23:59), got {value!r}")
    return value


def load_settings(path: Path | str) -> dict:
    """Every setting in SETTINGS from the #16 settings file, checked."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not {"setting", "value"} <= set(df.columns):
        raise ValueError(f"{path} needs the columns setting and value")
    names = df["setting"].str.strip()
    repeated = sorted(set(names[names.duplicated()]))
    if repeated:
        raise ValueError(f"{path} repeats settings {repeated}")
    values = dict(zip(names, df["value"].str.strip()))
    missing = [name for name in SETTINGS if name not in values]
    if missing:
        raise ValueError(f"{path} is missing settings {missing}")
    lag = values["feature_min_lag_days"]
    if not lag.isdigit() or int(lag) < 1:
        raise ValueError(f"feature_min_lag_days must be a whole number of 1 or more, got {lag!r}")
    margin = values["forecast_issue_margin_hours"]
    if not margin.isdigit() or int(margin) > 24:
        raise ValueError(f"forecast_issue_margin_hours must be a whole number of hours from 0 to 24, got {margin!r}")
    weather_lag = values["observed_weather_lag_days"]
    if not weather_lag.isdigit() or int(weather_lag) < 2:
        raise ValueError(f"observed_weather_lag_days must be a whole number of 2 or more, got {weather_lag!r}")
    return {
        "label_groups": parse_groups(values["label_groups"], "label_groups"),
        "feature_groups": parse_groups(values["feature_groups"], "feature_groups"),
        "feature_min_lag_days": int(lag),
        "forecast_cutoff_local": parse_clock(values["forecast_cutoff_local"], "forecast_cutoff_local"),
        "forecast_issue_margin_hours": int(margin),
        "observed_weather_lag_days": int(weather_lag),
    }


def check_counts(counts: pd.DataFrame) -> pd.DataFrame:
    """The daily counts with checked types, or a ValueError naming what is wrong."""
    missing = set(COUNT_COLUMNS) - set(counts.columns)
    if missing:
        raise ValueError(f"daily counts are missing columns: {sorted(missing)}; rerun daily_counts.py")
    df = counts[COUNT_COLUMNS].copy()
    if df.empty:
        return df.assign(date=pd.to_datetime(df["date"]), requests=df["requests"].astype("int64"),
                         baseline_requests=df["baseline_requests"].astype("int64"))
    if df[KEY].isna().any().any():
        raise ValueError("daily counts have rows with no unit, date or storm group")
    unknown = sorted(set(df["storm_group"]) - set(STORM_GROUPS))
    if unknown:
        raise ValueError(f"unknown storm groups {unknown}; daily counts use {', '.join(STORM_GROUPS)}")
    if not pd.api.types.is_datetime64_any_dtype(df["date"]):
        try:
            df["date"] = pd.to_datetime(df["date"], format="ISO8601")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"daily counts have a date that is not a date: {exc}") from exc
    if (df["date"] != df["date"].dt.normalize()).any():
        raise ValueError("daily counts have dates with a time of day; one row per day is expected")
    for column in ("requests", "baseline_requests"):
        values = pd.to_numeric(df[column], errors="coerce")
        if values.isna().any() or (values < 0).any() or (values != values.round()).any():
            raise ValueError(f"{column} must be whole numbers of 0 or more")
        df[column] = values.astype("int64")
    if (df["baseline_requests"] > df["requests"]).any():
        raise ValueError("baseline_requests is above requests in some rows; rerun daily_counts.py")
    if df.duplicated(KEY).any():
        raise ValueError("daily counts repeat a (unit, date, storm_group) row")
    days = (df["date"].max() - df["date"].min()).days + 1
    if len(df) != df["unit"].nunique() * days * len(STORM_GROUPS):
        raise ValueError("every unit needs every storm group on every day, zero-filled; rerun daily_counts.py")
    return df.sort_values(KEY, ignore_index=True)


def requested_days(counts: pd.DataFrame, start=None, end=None) -> pd.DatetimeIndex:
    """Every day from start to end, both included (by default the counts' first and last day), or a
    ValueError naming the days the counts lack, so an empty or partial table never passes as a run.

    In the DAG a slice reads the full build's counts (pipeline_steps), so a day after their last one
    means the full build is older than the slice. check_counts requires every unit and storm group on
    every day from the counts' first day to their last, so each day in that span is complete. Days
    before start are not asked for: they only feed baselines and lags, and the first days have none.
    """
    dates = pd.to_datetime(counts["date"], format="ISO8601")
    if dates.empty:
        raise ValueError("daily counts have no rows; rerun daily_counts.py")
    first, last = dates.min(), dates.max()
    rebuild = "rerun daily_counts.py so the counts cover them (a DAG slice reads the full build's counts)"
    start = first if start is None else pd.Timestamp(start)
    if end is None and start > last:
        raise ValueError(f"daily counts end on {last.date()}, before start {start.date()}; {rebuild}")
    end = last if end is None else pd.Timestamp(end)
    if start > end:
        raise ValueError(f"start {start.date()} is after end {end.date()}")
    days = pd.date_range(start, end)
    outside = [part for part in (days[days < first], days[days > last]) if len(part)]
    if outside:
        spans = " and ".join(f"{part[0].date()} to {part[-1].date()}" for part in outside)
        raise ValueError(f"daily counts cover {first.date()} to {last.date()}, so {sum(map(len, outside))} of the "
                         f"{len(days)} days from {start.date()} to {end.date()} have no counts ({spans}); {rebuild}")
    return days


def group_baselines(counts: pd.DataFrame) -> pd.DataFrame:
    """mean, sd, q90 and n of every unit, day and storm group (ADR-005 method A), next to its requests."""
    frames = []
    for (unit, group), part in counts.groupby(["unit", "storm_group"], sort=True):
        part = part.sort_values("date")
        base = seasonal_baseline(part.set_index("date")["baseline_requests"])
        frame = part[KEY + ["requests"]].reset_index(drop=True)
        frames.append(pd.concat([frame, base[BASE_COLUMNS].reset_index(drop=True)], axis=1))
    if not frames:
        return pd.DataFrame({"unit": pd.Series(dtype=object), "date": pd.Series(dtype="datetime64[ns]"),
                             "storm_group": pd.Series(dtype=object), "requests": pd.Series(dtype="int64"),
                             **{c: pd.Series(dtype=float) for c in BASE_COLUMNS}})
    return pd.concat(frames, ignore_index=True)


def group_flags(cells: pd.DataFrame, rule: Rule) -> pd.Series:
    """True or False where a baseline exists, missing where none does (ADR-005: no baseline, no label)."""
    if rule.kind == "sd":
        threshold = cells["mean"] + rule.k * cells["sd"].fillna(0)
    else:
        threshold = cells["q90"]
    flags = (cells["requests"] > threshold) & (cells["requests"] >= rule.m)
    return flags.astype("boolean").where(cells["mean"].notna(), pd.NA)


def label_cells(cells: pd.DataFrame, rule: Rule, label_groups: list[str]) -> pd.DataFrame:
    """One row per unit and day: the label, the groups that surged and each group's numbers."""
    flagged = cells.assign(flag=group_flags(cells, rule))
    keys = flagged[["unit", "date", "n"]].drop_duplicates(["unit", "date"]).set_index(["unit", "date"])
    wide = keys.rename(columns={"n": "history_days"})
    for group in STORM_GROUPS:
        part = flagged[flagged["storm_group"] == group].set_index(["unit", "date"])
        part = part[["requests", "mean", "sd", "q90", "flag"]].rename(
            columns={"requests": f"requests_{group}", "mean": f"mean_{group}", "sd": f"sd_{group}",
                     "q90": f"q90_{group}", "flag": f"surge_{group}"})
        wide = wide.join(part)

    # Nullable OR: False | NA is NA and True | NA is True, so a cell without a baseline stays
    # unlabeled (ADR-005) unless a group surges. Never fillna(False) here: that would turn missing
    # labels into false ones. Every group has the same history days, so a cell is labeled in all or none.
    surge = pd.Series(False, index=wide.index, dtype="boolean")
    for group in label_groups:
        surge = surge | wide[f"surge_{group}"].astype("boolean")
    hits = pd.DataFrame({g: wide[f"surge_{g}"].astype("boolean").fillna(False).astype(bool) for g in label_groups},
                        index=wide.index)
    names = pd.Series([";".join(g for g, hit in zip(label_groups, row) if hit) for row in hits.itertuples(index=False)],
                      index=wide.index, dtype="string")

    wide.insert(0, "rule_id", rule.rule_id)
    wide.insert(0, "surge_groups", names.where(surge.notna(), pd.NA))
    wide.insert(0, "surge", surge)
    out = wide.reset_index()
    out["history_days"] = out["history_days"].astype("int64")
    for group in STORM_GROUPS:
        out[f"requests_{group}"] = out[f"requests_{group}"].astype("int64")
        out[f"surge_{group}"] = out[f"surge_{group}"].astype("boolean")
    return out.sort_values(["unit", "date"], ignore_index=True)


def build_labels(cells: pd.DataFrame, rule: Rule, label_groups: list[str], start=None, end=None) -> pd.DataFrame:
    """Labels for start to end, from group_baselines. Days before start still serve as baseline history."""
    labels = label_cells(cells, rule, label_groups)
    if start is not None:
        labels = labels[labels["date"] >= pd.Timestamp(start)]
    if end is not None:
        labels = labels[labels["date"] <= pd.Timestamp(end)]
    return labels.reset_index(drop=True)


def period_of(dates: pd.Series) -> pd.Series:
    """The reporting period of each date; "" for a date in none of them."""
    years = pd.to_datetime(dates).dt.year
    out = pd.Series("", index=dates.index, dtype=object)
    for name, first, last in PERIODS:
        out[(years >= first) & (years <= last)] = name
    return out


def _rate(flags: pd.Series) -> float:
    return float(flags.mean()) if len(flags) else float("nan")


def _pct(value: float) -> str:
    return "" if pd.isna(value) else f"{100 * value:.2f}%"


def rule_summary(labels: pd.DataFrame, rule: Rule) -> dict:
    """Surge rate of one rule over the labeled cells, overall and per period, and what drives it."""
    labeled = labels[labels["surge"].notna()]
    surge = labeled["surge"].astype(bool)
    period = period_of(labeled["date"])
    row = {"rule": rule.rule_id, "labeled cells": len(labeled), "surges": int(surge.sum()), "surge rate": _pct(_rate(surge))}
    for name, _, _ in PERIODS:
        row[name] = _pct(_rate(surge[period == name]))
    for group in STORM_GROUPS:
        flags = labeled[f"surge_{group}"].fillna(False).astype(bool)
        row[f"{group} surges"] = int(flags.sum())
        row[f"{group} rate"] = _pct(_rate(flags))
    row["flooding only"] = int((labeled["surge_groups"] == "flooding").sum())
    train = labeled[period == TRAIN]
    per_unit = train.groupby("unit")["surge"].apply(lambda s: int(s.astype(bool).sum()))
    row["units with no train surge"] = int((per_unit == 0).sum())
    unit_rates = train.groupby("unit")["surge"].apply(lambda s: _rate(s.astype(bool)))
    row["train rate per unit, lowest to highest"] = (f"{_pct(unit_rates.min())} to {_pct(unit_rates.max())}"
                                                    if len(unit_rates) else "")
    validate = labeled[period == PERIODS[1][0]].groupby("unit")["surge"].apply(lambda s: int(s.astype(bool).sum()))
    row["units with under 5 validate surges"] = int((validate < 5).sum())
    train_rate = _rate(train["surge"].astype(bool))
    row["train in spec range"] = "" if pd.isna(train_rate) else ("yes" if SPEC_RANGE[0] <= train_rate <= SPEC_RANGE[1] else "no")
    return row


def grid_table(cells: pd.DataFrame, rules: list[Rule], label_groups: list[str]) -> pd.DataFrame:
    """One row per rule in the grid, all from the same baselines (group_baselines)."""
    return pd.DataFrame([rule_summary(label_cells(cells, rule, label_groups), rule) for rule in rules])


def unit_table(labels: pd.DataFrame) -> pd.DataFrame:
    """Per neighborhood: label coverage and surge rate, train and later."""
    period = period_of(labels["date"])
    rows = []
    for unit, part in labels.groupby("unit", sort=True):
        labeled = part["surge"].notna()
        row = {"unit": unit, "label coverage": _pct(_rate(labeled))}
        for name, _, _ in PERIODS:
            picked = part.loc[labeled & (period[part.index] == name), "surge"].astype(bool)
            row[name] = _pct(_rate(picked))
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", default="Data-Pipeline/data/processed/daily_counts.parquet")
    parser.add_argument("--rules", default="Data-Pipeline/config/surge_rules.csv")
    parser.add_argument("--settings", default="Data-Pipeline/config/labels_features.csv")
    parser.add_argument("--start", help="first day to write; earlier days still serve as baseline history")
    parser.add_argument("--end", help="last day to write")
    parser.add_argument("--out", default="Data-Pipeline/data/processed/labels.parquet")
    parser.add_argument("--grid", action="store_true", help="also print the surge rate of every rule in the grid")
    args = parser.parse_args()

    rules, chosen = load_rules(args.rules)
    settings = load_settings(args.settings)
    counts = check_counts(pd.read_parquet(args.counts))
    requested_days(counts, args.start, args.end)
    cells = group_baselines(counts)
    labels = build_labels(cells, chosen, settings["label_groups"], args.start, args.end)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    labels.to_parquet(out, index=False)

    labeled = labels["surge"].notna()
    print(f"rule {chosen.rule_id} (chosen in {args.rules}); label groups: {', '.join(settings['label_groups'])}")
    print(f"{len(labels):,} cells, {int(labeled.sum()):,} labeled ({_pct(_rate(labeled))}), "
          f"{int(labels.loc[labeled, 'surge'].astype(bool).sum()):,} surges "
          f"({_pct(_rate(labels.loc[labeled, 'surge'].astype(bool)))}); wrote {out}")
    if args.grid:
        print("\n## Surge rate per rule (labeled cells only)\n")
        print(markdown(grid_table(cells, rules, settings["label_groups"])))
        print(f"\n## Per neighborhood, rule {chosen.rule_id}\n")
        print(markdown(unit_table(labels)))


if __name__ == "__main__":
    main()
