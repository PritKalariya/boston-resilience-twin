"""Bias slicing on the processed 311 data and the surge labels (#32, W3-1).

There is no model yet, so this slices the data and the labels the model will learn from. Every
slice is a set of groups of the 24 ADR-004 neighborhoods:
- neighborhood: each neighborhood, compared with the city as a whole;
- poverty_share, poc_share, limited_english_share: the 2025 ACS share of each neighborhood (#9),
  above the median of the neighborhoods ("higher") or at or below it ("lower");
- flood_zone: any land in the 9-inch sea level rise 1% yearly chance flood zone, or none;
- system: the legacy era against the migration era (the first year of the new 311 system), and for
  missing locations the legacy against the new system in the migration era.

Per group and period (train 2015 to 2023, validate 2024, 2025 onward, from surge_labels):
- requests per capita: storm-related requests per 1,000 residents per year, from the label table;
- missing location: the share of storm-related requests that spatial_join could not place. A request
  without coordinates has no neighborhood, so it is counted under the neighborhoods its recorded
  311 neighborhood name covers (name_units), and left out when that name spans two groups;
- surge rate: surges among labeled neighborhood-days;
- label coverage: labeled neighborhood-days among all of them.

A tract check repeats requests per capita on the 2020 tracts, with the same ACS and flood groups.

The rates come from Fairlearn's MetricFrame. A gap is significant when it fails the four-fifths
rule (ratio_min in config/bias_slicing.csv) and its test gives p under alpha: a two-proportion test
(chi-square, or Fisher's exact test when a cell expects under 5) or, for requests per capita, an
exact rate test (binomial, conditioned on the two groups' total). A group with no surges in a period
is reported but left out of the surge rate test, as ADR-004 does for the worst-group metric.

Writes slices.csv (every rate), gaps.csv (every test) and report.md to --out-dir.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from fairlearn.metrics import MetricFrame
from scipy import stats

from feature_table import AREA_CRS, FLOOD_LAYERS, POPULATION, POVERTY
from surge_labels import PERIODS, period_of

SETTINGS = ("ratio_min", "alpha", "name_unit_min_share", "name_min_placed", "per_capita_scale", "system_era_days")
POC = ("population_b03002_003e", "population_b03002_001e")  # non-Hispanic white alone, everyone
# C16001 lists each language as its speakers, then "very well", then "less than very well".
LIMITED_ENGLISH = [f"population_c16001_{i:03d}e" for i in range(5, 39, 3)]
LANGUAGE_TOTAL = "population_c16001_001e"  # everyone aged 5 and over
MEASURES = ["poverty_share", "poc_share", "limited_english_share"]
FLOOD = "flood_1pct"
LABEL_COLUMNS = {"unit", "date", "surge"}
LOCATED_COLUMNS = {"source_system", "open_dt", "storm_group", "location", "neighborhood", "unit", "geoid20"}
PLACED = "placed"
NO_NAME = {"", "Boston"}  # citywide or blank: covers no neighborhood
METRICS = {
    "requests_per_capita": "requests per 1,000 residents a year",
    "missing_location": "share of storm requests with no location",
    "surge_rate": "surge rate of labeled days",
    "label_coverage": "share of days labeled",
}
YEAR_DAYS = 365.25


def load_settings(path: Path | str) -> dict[str, float]:
    """The thresholds in bias_slicing.csv (name, value); raises on a missing column, name or number."""
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    if not {"name", "value"} <= set(table.columns):
        raise ValueError(f"{Path(path).name} needs name and value columns")
    values = dict(zip(table["name"].str.strip(), pd.to_numeric(table["value"].str.strip(), errors="coerce")))
    missing = [name for name in SETTINGS if name not in values]
    if missing:
        raise ValueError(f"{Path(path).name} is missing settings: {missing}")
    bad = [name for name in SETTINGS if not math.isfinite(values[name]) or values[name] <= 0]
    if bad:
        raise ValueError(f"{Path(path).name} needs a number above 0 for {bad}")
    for name in ("ratio_min", "alpha", "name_unit_min_share"):
        if values[name] > 1:
            raise ValueError(f"{Path(path).name}: {name} must be at most 1, got {values[name]}")
    return {name: float(values[name]) for name in SETTINGS}


def _share(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    return numerator / denominator.where(denominator > 0)


def acs_shares(table: pd.DataFrame, key: str) -> pd.DataFrame:
    """Population and the three ACS shares of each row of a 2025 population table, keyed by key.
    A share whose numbers are missing stays missing."""
    needed = {key, POPULATION, *POVERTY, *POC, LANGUAGE_TOTAL, *LIMITED_ENGLISH}
    missing = needed - set(table.columns)
    if missing:
        raise ValueError(f"population table is missing columns: {sorted(missing)}")
    if table[key].duplicated().any():
        raise ValueError(f"population table repeats a {key}")
    n = table[sorted(needed - {key})].apply(pd.to_numeric, errors="coerce")
    limited = n[LIMITED_ENGLISH].sum(axis=1, min_count=len(LIMITED_ENGLISH))  # one gap leaves it missing
    return pd.DataFrame({
        key: table[key].astype(str).to_numpy(),
        "population": n[POPULATION].to_numpy(),
        "poverty_share": _share(n[POVERTY[0]], n[POVERTY[1]]).to_numpy(),
        "poc_share": (1 - _share(n[POC[0]], n[POC[1]])).to_numpy(),
        "limited_english_share": _share(limited, n[LANGUAGE_TOTAL]).to_numpy(),
    })


def flood_shares(shapes: gpd.GeoDataFrame, static_dir: Path) -> np.ndarray:
    """Share of each shape's area in the 1% yearly chance flood zone (as feature_table measures it)."""
    zone = gpd.read_parquet(static_dir / FLOOD_LAYERS[FLOOD]).to_crs(AREA_CRS).geometry.make_valid().union_all()
    areas = shapes.to_crs(AREA_CRS).geometry.make_valid()
    return (areas.intersection(zone).area / areas.area).to_numpy()


def unit_attributes(static_dir: Path | str) -> pd.DataFrame:
    """One row per ADR-004 neighborhood: population, the ACS shares and the flood zone share."""
    static_dir = Path(static_dir)
    hoods = gpd.read_parquet(static_dir / "neighborhoods_by_tract20.parquet")[["neighborhood", "geometry"]]
    acs = acs_shares(pd.read_parquet(static_dir / "population_2025_neighborhood.parquet"), "name")
    absent = sorted(set(hoods["neighborhood"]) - set(acs["name"]))
    if absent:
        raise ValueError(f"no 2025 population row for neighborhoods {absent}")
    out = acs.set_index("name").loc[hoods["neighborhood"]].reset_index(names="unit")
    out[f"{FLOOD}_share"] = flood_shares(hoods, static_dir)
    return out


def tract_attributes(static_dir: Path | str) -> pd.DataFrame:
    """One row per 2020 tract: population, the ACS shares and the flood zone share."""
    static_dir = Path(static_dir)
    tracts = gpd.read_parquet(static_dir / "tracts_2020.parquet")
    if "geoid20" not in tracts.columns:
        raise ValueError("tracts_2020 has no geoid20 column")
    acs = acs_shares(pd.read_parquet(static_dir / "population_2025_tract.parquet"), "geoid20")
    out = tracts[["geoid20"]].astype({"geoid20": str}).merge(acs, on="geoid20", how="left")
    out[f"{FLOOD}_share"] = flood_shares(tracts, static_dir)
    return out


def median_groups(values: pd.Series) -> pd.Series:
    """"higher" above the median of the known values, "lower" at or below it; missing stays missing."""
    out = pd.Series(pd.NA, index=values.index, dtype="string")
    known = values.notna()
    if known.any():
        median = values[known].median()
        out[known] = np.where(values[known] > median, "higher", "lower")
    return out


def slice_groups(attrs: pd.DataFrame, key: str) -> dict[str, pd.Series]:
    """Each slice's group of every row of attrs (unit or tract attributes), indexed by key."""
    attrs = attrs.set_index(key)
    groups = {measure: median_groups(attrs[measure]) for measure in MEASURES}
    flood = attrs[f"{FLOOD}_share"]
    groups["flood_zone"] = pd.Series(np.where(flood > 0, "in flood zone", "not in flood zone"),
                                     index=attrs.index, dtype="string").mask(flood.isna())
    return groups


def name_units(storm: pd.DataFrame, min_share: float, min_placed: float) -> dict[str, tuple[str, ...]]:
    """The neighborhoods each recorded 311 neighborhood name covers: every unit holding at least
    min_share of the name's placed storm requests, for names with at least min_placed of them. The
    legacy names are coarser than the ADR-004 neighborhoods ("Allston / Brighton"), so a name can
    cover several; "Boston" and blank cover none."""
    placed = storm[(storm["location"] == PLACED) & storm["unit"].notna()]
    names = placed["neighborhood"].astype("string").str.strip()
    counts = placed.assign(name=names).groupby(["name", "unit"]).size()
    out = {}
    for name, per_unit in counts.groupby(level=0):
        per_unit = per_unit.droplevel(0)
        if name in NO_NAME or per_unit.sum() < min_placed:
            continue
        out[name] = tuple(sorted(per_unit[per_unit / per_unit.sum() >= min_share].index))
    return out


def request_group(units: pd.Series, groups: pd.Series) -> pd.Series:
    """A request's group when every unit its name covers is in the same group, else missing."""
    def one(covered):
        found = {groups.get(unit, pd.NA) for unit in covered}
        return found.pop() if len(found) == 1 and not pd.isna(next(iter(found))) else pd.NA
    return units.map(one).astype("string")


def check_columns(df: pd.DataFrame, needed: set[str], name: str) -> None:
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"{name} is missing columns: {sorted(missing)}")
    if df.empty:
        raise ValueError(f"{name} has no rows")


def label_cells(labels: pd.DataFrame, population: pd.Series, scale: float) -> pd.DataFrame:
    """One row per neighborhood-day: period, labeled, surge, storm requests and the person-years
    behind them (scaled, so requests / exposure is per scale residents a year)."""
    check_columns(labels, LABEL_COLUMNS, "labels")
    requests = [c for c in labels.columns if c.startswith("requests_")]
    if not requests:
        raise ValueError("labels have no requests_<group> columns")
    try:
        dates = pd.to_datetime(labels["date"], format="ISO8601")
    except (ValueError, TypeError) as e:
        raise ValueError(f"labels have a date that is not a date: {e}") from e
    surge = labels["surge"].astype("boolean")
    return pd.DataFrame({
        "unit": labels["unit"].astype(str),
        "date": dates,
        "period": period_of(dates).to_numpy(),
        "labeled": surge.notna().astype(int).to_numpy(),
        "surge": surge.fillna(False).astype(int).to_numpy(),
        "requests": labels[requests].apply(pd.to_numeric, errors="raise").sum(axis=1).to_numpy(),
        "exposure": (labels["unit"].map(population) / scale / YEAR_DAYS).to_numpy(),
    })


def storm_requests(located: pd.DataFrame) -> pd.DataFrame:
    """Storm-related requests with their Boston local day, period and whether spatial_join placed them."""
    check_columns(located, LOCATED_COLUMNS, "311_located")
    storm = located[located["storm_group"].notna() & (located["storm_group"].astype(str) != "")].copy()
    storm["date"] = pd.to_datetime(storm["open_dt"]).dt.normalize()
    storm["period"] = period_of(storm["date"]).to_numpy()
    storm["missing"] = (storm["location"] != PLACED).astype(int)
    return storm


def system_eras(storm: pd.DataFrame, first: pd.Timestamp, last: pd.Timestamp, days: int) -> dict[str, tuple]:
    """The migration era (from the first new-system day, at most days long, within first to last) and
    the legacy era (the same days one year earlier, so the seasons match)."""
    new = storm.loc[storm["source_system"] == "new", "date"]
    if new.empty:
        return {}
    start = max(new.min(), first)
    end = min(start + pd.Timedelta(days=days - 1), last)
    year = pd.DateOffset(years=1)
    if end < start or start - year < first:
        return {}
    return {"legacy era": (start - year, end - year), "migration era": (start, end)}


def group_rates(numerator: pd.Series, denominator: pd.Series, groups: pd.Series) -> pd.DataFrame:
    """Per group (and "all"): numerator, denominator and their ratio, with Fairlearn's MetricFrame.
    Rows without a group are left out."""
    keep = groups.notna() & denominator.notna()
    if not keep.any():
        return pd.DataFrame(columns=["group", "numerator", "denominator", "value"])
    y, den, g = numerator[keep].to_numpy(dtype=float), denominator[keep].to_numpy(dtype=float), groups[keep].to_numpy()
    frame = MetricFrame(
        metrics={"numerator": lambda t, p: t.sum(), "denominator": lambda t, p, d: d.sum(),
                 "value": lambda t, p, d: t.sum() / d.sum() if d.sum() > 0 else np.nan},
        y_true=y, y_pred=y, sensitive_features=pd.Series(g, name="group"),
        sample_params={"denominator": {"d": den}, "value": {"d": den}},
    )
    out = frame.by_group.reset_index()
    overall = pd.DataFrame([{"group": "all", **frame.overall.to_dict()}])
    return pd.concat([out, overall], ignore_index=True)


def proportion_p(k1: float, n1: float, k2: float, n2: float) -> float:
    """Two-sided p of two proportions: chi-square, or Fisher's exact test when a cell expects under 5."""
    if min(n1, n2) <= 0:
        return float("nan")
    table = np.array([[k1, n1 - k1], [k2, n2 - k2]])
    if table[:, 0].sum() == 0 or table[:, 1].sum() == 0:
        return 1.0  # nothing or everything in both: no difference to test
    expected = stats.contingency.expected_freq(table)
    if (expected < 5).any():
        return float(stats.fisher_exact(table).pvalue)
    return float(stats.chi2_contingency(table, correction=False).pvalue)


def rate_p(k1: float, e1: float, k2: float, e2: float) -> float:
    """Two-sided exact p of two Poisson rates (counts k over exposures e), conditioned on k1 + k2."""
    if min(e1, e2) <= 0:
        return float("nan")
    total = int(round(k1 + k2))
    if total == 0:
        return 1.0
    return float(stats.binomtest(int(round(k1)), total, e1 / (e1 + e2)).pvalue)


def gap_rows(rates: pd.DataFrame, metric: str, versus: str, settings: dict, skip_zero: bool) -> list[dict]:
    """The tests of one slice, metric and period. versus "pair": the lower of two groups against the
    higher; "overall": each group against the rest of the city. A ratio is the lower rate over the
    higher, so a group well above the rest fails the rule as much as one well below."""
    test = rate_p if metric == "requests_per_capita" else proportion_p
    parts = rates[(rates["group"] != "all") & (rates["denominator"] > 0)]
    if skip_zero:
        parts = parts[parts["numerator"] > 0]
    total = parts[["numerator", "denominator"]].sum()
    pairs = []
    if versus == "pair" and len(parts) == 2:
        low, high = parts.sort_values("value").to_dict("records")
        pairs.append((low, high, high["group"]))
    elif versus == "overall" and len(parts) > 2:
        for row in parts.to_dict("records"):
            rest_n, rest_d = total["numerator"] - row["numerator"], total["denominator"] - row["denominator"]
            pairs.append((row, {"numerator": rest_n, "denominator": rest_d, "value": rest_n / rest_d}, "rest of the city"))
    rows = []
    for row, other, label in pairs:
        low, high = sorted([row["value"], other["value"]])
        ratio = low / high if high > 0 else float("nan")
        p = test(row["numerator"], row["denominator"], other["numerator"], other["denominator"])
        rows.append({
            "group": row["group"], "compared_with": label, "value": row["value"], "other_value": other["value"],
            "numerator": row["numerator"], "denominator": row["denominator"],
            "ratio": ratio, "p_value": p,
            "significant": bool(ratio < settings["ratio_min"] and p < settings["alpha"]),
        })
    return rows


def slice_metrics(name: str, groups: pd.Series, cells: pd.DataFrame, storm: pd.DataFrame,
                  periods: list[str], settings: dict, versus: str) -> tuple[list[dict], list[dict]]:
    """Every metric of one neighborhood slice in every period: its rates and its tests.
    groups maps each unit to its group in this slice; a request goes by request_group."""
    cell_group = cells["unit"].map(groups).astype("string")
    request_groups = request_group(storm["covers"], groups) if len(storm) else pd.Series(dtype="string")
    rates, gaps = [], []
    for period in periods:
        in_period = cells["period"] == period
        labeled = in_period & (cells["labeled"] == 1)
        requests_in = (storm["period"] == period) if len(storm) else pd.Series(dtype=bool)
        parts = {
            "requests_per_capita": (cells.loc[in_period, "requests"], cells.loc[in_period, "exposure"], cell_group[in_period]),
            "missing_location": (storm.loc[requests_in, "missing"], pd.Series(1.0, index=storm.index[requests_in]),
                                 request_groups[requests_in]),
            "surge_rate": (cells.loc[labeled, "surge"], pd.Series(1.0, index=cells.index[labeled]), cell_group[labeled]),
            "label_coverage": (cells.loc[in_period, "labeled"], pd.Series(1.0, index=cells.index[in_period]),
                               cell_group[in_period]),
        }
        for metric, (num, den, g) in parts.items():
            table = group_rates(num, den, g)
            for row in table.to_dict("records"):
                rates.append({"slice": name, "metric": metric, "period": period, **row})
            for row in gap_rows(table, metric, versus, settings, skip_zero=metric == "surge_rate"):
                gaps.append({"slice": name, "metric": metric, "period": period, **row})
    return rates, gaps


def system_metrics(cells: pd.DataFrame, storm: pd.DataFrame, eras: dict, settings: dict) -> tuple[list[dict], list[dict]]:
    """The system slice: the legacy era against the migration era for the label table's metrics, and
    the legacy against the new system for missing locations in the migration era."""
    rates, gaps = [], []
    if not eras:
        return rates, gaps
    era = pd.Series(pd.NA, index=cells.index, dtype="string")
    for name, (start, end) in eras.items():
        era[cells["date"].between(start, end)] = name
    start, end = eras["migration era"]
    migration = storm["date"].between(start, end) if len(storm) else pd.Series(dtype=bool)
    system = storm.loc[migration, "source_system"].map({"legacy": "legacy system", "new": "new system"}).astype("string")
    labeled = cells["labeled"] == 1
    ones = pd.Series(1.0, index=cells.index)
    parts = {
        "requests_per_capita": (cells["requests"], cells["exposure"], era),
        "missing_location": (storm.loc[migration, "missing"], pd.Series(1.0, index=system.index), system),
        "surge_rate": (cells.loc[labeled, "surge"], ones[labeled], era[labeled]),
        "label_coverage": (cells["labeled"], ones, era),
    }
    period = f"{start.date()} to {end.date()} and a year earlier"
    for metric, (num, den, g) in parts.items():
        table = group_rates(num, den, g)
        label = f"{start.date()} to {end.date()}" if metric == "missing_location" else period
        for row in table.to_dict("records"):
            rates.append({"slice": "system", "metric": metric, "period": label, **row})
        for row in gap_rows(table, metric, "pair", settings, skip_zero=metric == "surge_rate"):
            gaps.append({"slice": "system", "metric": metric, "period": label, **row})
    return rates, gaps


def tract_metrics(tracts: pd.DataFrame, storm: pd.DataFrame, cells: pd.DataFrame, periods: list[str],
                  settings: dict) -> tuple[list[dict], list[dict]]:
    """Requests per capita on the 2020 tracts: placed storm requests per tract and period over the
    tract's 2025 population, for the same ACS and flood groups as the neighborhoods."""
    rates, gaps = [], []
    placed = storm[(storm["missing"] == 0) & storm["geoid20"].notna()] if len(storm) else storm
    groups = slice_groups(tracts, "geoid20")
    population = tracts.set_index("geoid20")["population"]
    for period in periods:
        days = cells.loc[cells["period"] == period, "date"].nunique()
        if not days:
            continue
        counts = placed[placed["period"] == period].groupby(placed["geoid20"].astype(str)).size() if len(placed) else pd.Series(dtype=float)
        requests = counts.reindex(population.index, fill_value=0).astype(float)
        exposure = population * days / YEAR_DAYS / settings["per_capita_scale"]
        for name, g in groups.items():
            table = group_rates(requests, exposure, g.reindex(population.index))
            for row in table.to_dict("records"):
                rates.append({"slice": f"tract {name}", "metric": "requests_per_capita", "period": period, **row})
            for row in gap_rows(table, "requests_per_capita", "pair", settings, skip_zero=False):
                gaps.append({"slice": f"tract {name}", "metric": "requests_per_capita", "period": period, **row})
    return rates, gaps


def bias_tables(labels: pd.DataFrame, located: pd.DataFrame, units: pd.DataFrame, tracts: pd.DataFrame,
                settings: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Every rate (slices) and every test (gaps), plus what the report says about the inputs."""
    cells = label_cells(labels, units.set_index("unit")["population"], settings["per_capita_scale"])
    storm = storm_requests(located)
    first, last = cells["date"].min(), cells["date"].max()
    storm = storm[storm["date"].between(first, last)].copy()
    names = name_units(storm, settings["name_unit_min_share"], settings["name_min_placed"])
    storm["covers"] = storm["neighborhood"].astype("string").str.strip().map(names).map(
        lambda covered: covered if isinstance(covered, tuple) else ())
    periods = [name for name, *_ in PERIODS if (cells["period"] == name).any()]

    slices = {"neighborhood": pd.Series(units["unit"].to_numpy(), index=units["unit"], dtype="string")}
    slices.update(slice_groups(units, "unit"))
    rates, gaps = [], []
    for name, groups in slices.items():
        r, g = slice_metrics(name, groups, cells, storm, periods, settings,
                             versus="overall" if name == "neighborhood" else "pair")
        rates += r
        gaps += g
    eras = system_eras(storm, first, last, int(settings["system_era_days"]))
    for r, g in (system_metrics(cells, storm, eras, settings), tract_metrics(tracts, storm, cells, periods, settings)):
        rates += r
        gaps += g

    unplaced = storm[storm["missing"] == 1]
    groups_of = {name: groups for name, groups in slices.items()}
    info = {
        "first": first.date(), "last": last.date(), "cells": len(cells), "storm": len(storm),
        "unplaced": len(unplaced), "unplaced_named": int(unplaced["covers"].map(len).gt(0).sum()),
        "names": names, "eras": eras,
        "groups": {name: g.dropna().groupby(g.dropna()).apply(lambda s: sorted(s.index)).to_dict()
                   for name, g in groups_of.items() if name != "neighborhood"},
        "no_population": sorted(units.loc[units["population"].isna(), "unit"]),
    }
    return pd.DataFrame(rates), pd.DataFrame(gaps), info


def _fmt(metric: str, value) -> str:
    if pd.isna(value):
        return ""
    return f"{value:.1f}" if metric == "requests_per_capita" else f"{100 * value:.2f}%"


def markdown(df: pd.DataFrame) -> str:
    """A table as GitHub markdown, missing values left blank."""
    cells = df.astype(object).where(df.notna(), "").astype(str)
    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(row) + " |" for row in cells.itertuples(index=False)]
    return "\n".join(lines)


def report(rates: pd.DataFrame, gaps: pd.DataFrame, info: dict, settings: dict) -> str:
    """The markdown report: inputs, significant gaps with their numbers, then every rate per metric."""
    lines = [
        "# Bias slicing on data and labels (#32)", "",
        f"Neighborhood-days {info['first']} to {info['last']}: {info['cells']:,} cells; "
        f"{info['storm']:,} storm-related requests, {info['unplaced']:,} without a location, "
        f"{info['unplaced_named']:,} of those with a recorded neighborhood name that covers a neighborhood.",
        f"Significant gap: ratio under {settings['ratio_min']:g} (four-fifths rule) and p under {settings['alpha']:g}.",
        "",
    ]
    if info["no_population"]:
        lines += [f"No 2025 population or ACS values (left out of per capita and ACS slices): {', '.join(info['no_population'])}.", ""]
    for name, groups in info["groups"].items():
        lines.append(f"- **{name}:** " + "; ".join(f"{group} ({len(units)}): {', '.join(units)}" for group, units in groups.items()))
    if info["eras"]:
        lines.append("- **system:** " + "; ".join(f"{name} {a.date()} to {b.date()}" for name, (a, b) in info["eras"].items()))
    lines += ["", "## Significant gaps", ""]
    found = gaps[gaps["significant"]] if len(gaps) else gaps
    if found.empty:
        lines.append("None.")
    else:
        table = pd.DataFrame({
            "slice": found["slice"], "metric": found["metric"].map(METRICS), "period": found["period"],
            "group": found["group"],
            "group value": [_fmt(m, v) for m, v in zip(found["metric"], found["value"])],
            "compared with": found["compared_with"],
            "their value": [_fmt(m, v) for m, v in zip(found["metric"], found["other_value"])],
            "ratio": found["ratio"].round(2), "p": found["p_value"].map(lambda p: f"{p:.1e}"),
        })
        lines.append(markdown(table))
    for metric, title in METRICS.items():
        part = rates[rates["metric"] == metric] if len(rates) else rates
        if part.empty:
            continue
        wide = part.pivot_table(index=["slice", "group"], columns="period", values="value", aggfunc="first", sort=False)
        wide = wide.apply(lambda col: col.map(lambda v: _fmt(metric, v)))
        lines += ["", f"## {title[0].upper()}{title[1:]}", "", markdown(wide.reset_index())]
    lines += ["", "## Recorded neighborhood names used for requests without a location", "",
              markdown(pd.DataFrame([(name, ", ".join(units)) for name, units in sorted(info["names"].items())],
                                    columns=["recorded name", "neighborhoods it covers"]))]
    return "\n".join(lines) + "\n"


def main() -> None:
    """Slice the labels and located requests, write slices.csv, gaps.csv and report.md to --out-dir,
    and print the significant gaps."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", default="Data-Pipeline/data/processed/labels.parquet")
    parser.add_argument("--located", default="Data-Pipeline/data/interim/311_located.parquet")
    parser.add_argument("--static-dir", default="Data-Pipeline/data/interim/static")
    parser.add_argument("--config", default="Data-Pipeline/config/bias_slicing.csv")
    parser.add_argument("--out-dir", default="Data-Pipeline/data/interim/reports/bias")
    args = parser.parse_args()

    settings = load_settings(args.config)
    import pyarrow.parquet as pq

    labels = pd.read_parquet(args.labels)
    # Only the columns used (311_located is large); a missing one is named by storm_requests.
    present = sorted(LOCATED_COLUMNS & set(pq.read_schema(args.located).names))
    located = pd.read_parquet(args.located, columns=present)
    rates, gaps, info = bias_tables(labels, located, unit_attributes(args.static_dir),
                                    tract_attributes(args.static_dir), settings)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rates.to_csv(out / "slices.csv", index=False)
    gaps.to_csv(out / "gaps.csv", index=False)
    text = report(rates, gaps, info, settings)
    (out / "report.md").write_text(text, encoding="utf-8")
    found = int(gaps["significant"].sum()) if len(gaps) else 0
    print(f"{len(rates):,} rates and {len(gaps):,} tests; {found} significant gaps; wrote {out}")


if __name__ == "__main__":
    main()
