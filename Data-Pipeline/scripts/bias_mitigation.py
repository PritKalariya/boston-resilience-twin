"""Before and after numbers for the #33 mitigation of the reporting gap (ADR-025).

#32 found that poorer and more diverse neighborhoods file fewer storm requests per resident, and
the raw 311 lag features (feature_table) carry that gap into the model. #33 replaces them in the
model with self-relative lags: the same counts over the neighborhood's own ADR-005 baseline.

This compares the two on the #32 slices (bias_slicing's ACS and flood zone groups). For each slice,
period, storm group and window it takes the mean of three versions of the lag in each group:
- raw: the count the model used before (feature_table's `{group}_last` and `{group}_sum{w}`);
- per capita: the same per 1,000 residents, where the reporting gap shows;
- self-relative: `{group}_rel_last` and `{group}_rel{w}`, the default model inputs now.

A gap is the lower group mean over the higher one, under the four-fifths rule (ratio_min in
config/bias_slicing.csv). The means are over the neighborhood-days that have a label and every
version of the lag, so all three versions are compared on the same days. They are given on all of
those days and on surge days only, where the lag is the forecast's evidence. No p value: a group
mean over thousands of correlated days says little a test could add (see #32's report).

Writes mitigation.csv (every mean) and mitigation.md to --out-dir.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from bias_slicing import MEASURES, load_settings, markdown, slice_groups, unit_attributes
from feature_table import WINDOWS, feature_names, relative_names
from surge_labels import PERIODS, load_settings as load_label_settings, period_of

SLICES = [*MEASURES, "flood_zone"]
VERSIONS = ["raw", "per capita", "self-relative"]
DAYS = ["all labeled days", "surge days"]
SHOWN_WINDOW = 7  # the report's tables; mitigation.csv has every window


def lag_columns(groups: list[str]) -> list[tuple[str, str, str, str]]:
    """(storm group, window, raw column, self-relative column) for every 311 lag."""
    windows = ["last", *map(str, WINDOWS)]
    raw, relative = feature_names(groups), relative_names(groups)
    return [(group, window, raw[i * len(windows) + j], relative[i * len(windows) + j])
            for i, group in enumerate(groups) for j, window in enumerate(windows)]


def comparison_rows(features: pd.DataFrame, labels: pd.DataFrame, units: pd.DataFrame,
                    groups: list[str]) -> pd.DataFrame:
    """One row per neighborhood-day with a label and every lag version: unit, period, surge, and per lag
    its raw, per capita and self-relative values."""
    needed = {"unit", "date", *[c for _, _, raw, rel in lag_columns(groups) for c in (raw, rel)]}
    missing = needed - set(features.columns)
    if missing:
        raise ValueError(f"features are missing columns: {sorted(missing)}; rerun feature_table.py")
    if not {"unit", "date", "surge"} <= set(labels.columns):
        raise ValueError("labels need unit, date and surge columns; rerun surge_labels.py")
    if features.empty or labels.empty:
        raise ValueError("features and labels need rows")
    rows = features[sorted(needed)].merge(labels[["unit", "date", "surge"]], on=["unit", "date"], how="inner")
    rows = rows[rows["surge"].notna()].copy()
    population = rows["unit"].map(units.set_index("unit")["population"])
    out = pd.DataFrame({"unit": rows["unit"].to_numpy(), "period": period_of(rows["date"]).to_numpy(),
                        "surge": rows["surge"].astype(bool).to_numpy()})
    for group, window, raw, rel in lag_columns(groups):
        out[f"{raw}|raw"] = rows[raw].to_numpy(dtype=float)
        out[f"{raw}|per capita"] = (rows[raw] / population * 1000).to_numpy(dtype=float)
        out[f"{raw}|self-relative"] = rows[rel].to_numpy(dtype=float)
    value_columns = [c for c in out.columns if "|" in c]
    return out[np.isfinite(out[value_columns]).all(axis=1)].reset_index(drop=True)


def mitigation_table(rows: pd.DataFrame, units: pd.DataFrame, groups: list[str], ratio_min: float) -> pd.DataFrame:
    """Group means and gaps of every lag version, per slice, period, day set, storm group and window."""
    out = []
    slices = slice_groups(units, "unit")
    periods = [name for name, *_ in PERIODS if (rows["period"] == name).any()]
    for name in SLICES:
        group_of = rows["unit"].map(slices[name])
        for period in periods:
            for days in DAYS:
                keep = (rows["period"] == period) & group_of.notna() & (rows["surge"] if days == "surge days" else True)
                for storm, window, raw, _ in lag_columns(groups):
                    for version in VERSIONS:
                        means = rows.loc[keep, f"{raw}|{version}"].groupby(group_of[keep]).agg(["mean", "size"])
                        if len(means) != 2:
                            continue
                        low, high = means["mean"].min(), means["mean"].max()
                        ratio = low / high if high > 0 else float("nan")
                        out.append({
                            "slice": name, "period": period, "days": days, "storm_group": storm, "window": window,
                            "version": version, "lower_group": means["mean"].idxmin(), "lower_mean": low,
                            "higher_group": means["mean"].idxmax(), "higher_mean": high,
                            "rows": int(means["size"].sum()), "ratio": ratio,
                            "gap": bool(ratio < ratio_min),
                        })
    return pd.DataFrame(out)


def report(table: pd.DataFrame, rows: pd.DataFrame, ratio_min: float) -> str:
    """The markdown report: how many gaps each version has, then the 7-day sums per slice."""
    lines = ["# Bias mitigation: self-relative 311 lags (#33, ADR-025)", "",
             f"{len(rows):,} labeled neighborhood-days with every lag version, {int(rows['surge'].sum()):,} of them surge "
             f"days. A gap is a lower group mean under {ratio_min:g} times the higher one (four-fifths rule).", ""]
    if table.empty:
        return "\n".join(lines + ["No group had rows to compare."]) + "\n"
    counts = (table.groupby(["days", "version"])["gap"].agg(["sum", "size"])
              .reindex(pd.MultiIndex.from_product([DAYS, VERSIONS])).reset_index())
    counts.columns = ["days", "version", "gaps", "comparisons"]
    lines += ["## Gaps by version (every slice, period, storm group and window)", "", markdown(counts), ""]
    shown = table[table["window"] == str(SHOWN_WINDOW)].copy()
    shown["value"] = [f"{lo:.2f} vs {hi:.2f} ({r:.2f}{', gap' if g else ''})" for lo, hi, r, g in
                      zip(shown["lower_mean"], shown["higher_mean"], shown["ratio"], shown["gap"])]
    for days in DAYS:
        part = shown[shown["days"] == days]
        wide = part.pivot_table(index=["slice", "storm_group", "version"], columns="period", values="value",
                                aggfunc="first", sort=False).reset_index()
        lines += [f"## {SHOWN_WINDOW}-day sums, {days} (lower group mean vs higher, ratio)", "", markdown(wide), ""]
    return "\n".join(lines)


def main() -> None:
    """Compare the raw, per capita and self-relative lags on the #32 slices and write mitigation.csv and
    mitigation.md to --out-dir."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", default="Data-Pipeline/data/processed/features.parquet")
    parser.add_argument("--labels", default="Data-Pipeline/data/processed/labels.parquet")
    parser.add_argument("--static-dir", default="Data-Pipeline/data/interim/static")
    parser.add_argument("--config", default="Data-Pipeline/config/bias_slicing.csv")
    parser.add_argument("--settings", default="Data-Pipeline/config/labels_features.csv")
    parser.add_argument("--out-dir", default="Data-Pipeline/data/interim/reports/bias")
    args = parser.parse_args()

    ratio_min = load_settings(args.config)["ratio_min"]
    groups = load_label_settings(args.settings)["feature_groups"]
    units = unit_attributes(args.static_dir)
    rows = comparison_rows(pd.read_parquet(args.features), pd.read_parquet(args.labels), units, groups)
    table = mitigation_table(rows, units, groups, ratio_min)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "mitigation.csv", index=False)
    (out / "mitigation.md").write_text(report(table, rows, ratio_min), encoding="utf-8")
    gaps = table.groupby("version")["gap"].sum().reindex(VERSIONS, fill_value=0) if len(table) else pd.Series(dtype=int)
    print(f"{len(table):,} comparisons; gaps: " + ", ".join(f"{v} {int(gaps.get(v, 0))}" for v in VERSIONS)
          + f"; wrote {out}")


if __name__ == "__main__":
    main()
