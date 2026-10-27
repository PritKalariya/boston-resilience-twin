import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

import bias_slicing
from bias_slicing import (
    LANGUAGE_TOTAL,
    LIMITED_ENGLISH,
    POC,
    acs_shares,
    bias_tables,
    gap_rows,
    group_rates,
    label_cells,
    load_settings,
    median_groups,
    name_units,
    proportion_p,
    rate_p,
    request_group,
    storm_requests,
    system_eras,
    tract_attributes,
    unit_attributes,
)
from feature_table import FLOOD_LAYERS, POPULATION, POVERTY

CONFIG = Path(__file__).resolve().parents[1] / "config" / "bias_slicing.csv"
SETTINGS = load_settings(CONFIG)

# Four neighborhoods side by side in Boston, one tract each. Poor and Crowded are poorer, more
# people of color and more limited English than Rich and Quiet; only Poor touches the flood zone.
UNITS = ["Crowded", "Poor", "Quiet", "Rich"]
SHAPES = {unit: box(-71.10 + 0.01 * i, 42.30, -71.09 + 0.01 * i, 42.31) for i, unit in enumerate(UNITS)}
TRACT_IDS = {unit: f"2502500{i}00" for i, unit in enumerate(UNITS)}
ACS = {  # population, below poverty, non-Hispanic white, limited English (per language column)
    "Crowded": (20000, 6000, 4000, 300),
    "Poor": (10000, 4000, 2000, 200),
    "Quiet": (10000, 500, 8000, 10),
    "Rich": (20000, 1000, 15000, 20),
}


def population_table(key: str, ids: dict) -> pd.DataFrame:
    rows = []
    for unit, (people, poor, white, limited) in ACS.items():
        row = {key: ids[unit], POPULATION: people, POVERTY[0]: poor, POVERTY[1]: people,
               POC[0]: white, POC[1]: people, LANGUAGE_TOTAL: people}
        row.update({column: limited for column in LIMITED_ENGLISH})
        rows.append(row)
    return pd.DataFrame(rows)


def static_dir(tmp_path: Path) -> Path:
    out = tmp_path / "static"
    out.mkdir()
    geometry = [SHAPES[unit] for unit in UNITS]
    gpd.GeoDataFrame({"neighborhood": UNITS}, geometry=geometry, crs="EPSG:4326").to_parquet(out / "neighborhoods_by_tract20.parquet")
    gpd.GeoDataFrame({"geoid20": [TRACT_IDS[u] for u in UNITS]}, geometry=geometry, crs="EPSG:4326").to_parquet(out / "tracts_2020.parquet")
    population_table("name", {u: u for u in UNITS}).to_parquet(out / "population_2025_neighborhood.parquet")
    population_table("geoid20", TRACT_IDS).to_parquet(out / "population_2025_tract.parquet")
    flood = box(-71.0905, 42.30, -71.0895, 42.31)  # the east tenth of Crowded and the west tenth of Poor
    gpd.GeoDataFrame(geometry=[flood], crs="EPSG:4326").to_parquet(out / FLOOD_LAYERS["flood_1pct"])
    return out


def labels_table(start="2015-01-01", end="2026-06-30", surges=None, requests=None) -> pd.DataFrame:
    """Labels like surge_labels writes: the first year unlabeled, a surge every 20th day everywhere
    (or on the days surges[unit] picks), and requests[unit] tree requests a day (default 1)."""
    dates = pd.date_range(start, end)
    frames = []
    for unit in UNITS:
        day = np.arange(len(dates))
        surge = pd.array(day % 20 == 0 if surges is None else surges[unit](day), dtype="boolean")
        surge[dates < pd.Timestamp("2016-01-01")] = pd.NA
        frames.append(pd.DataFrame({
            "unit": unit, "date": dates, "surge": surge,
            "requests_snow_ice": 0, "requests_tree": (requests or {}).get(unit, 1), "requests_flooding": 0,
        }))
    return pd.concat(frames, ignore_index=True)


def located_table(rows) -> pd.DataFrame:
    """311_located rows: (open_dt, source_system, recorded name, location, unit)."""
    df = pd.DataFrame(rows, columns=["open_dt", "source_system", "neighborhood", "location", "unit"])
    df["open_dt"] = pd.to_datetime(df["open_dt"])
    df["storm_group"] = "tree"
    df["geoid20"] = df["unit"].map(TRACT_IDS).astype("string")
    df["unit"] = df["unit"].astype("string")
    return df


def placed(unit, day="2020-06-01", system="legacy", name=None, n=1):
    return [(f"{day} 10:00", system, name or unit, "placed", unit)] * n


def unplaced(name, day="2020-06-01", system="legacy", n=1):
    return [(f"{day} 10:00", system, name, "no_coordinates", None)] * n


# Settings


def test_settings_file_loads():
    assert SETTINGS["ratio_min"] == 0.8 and SETTINGS["alpha"] == 0.01


@pytest.mark.parametrize("body, message", [
    ("setting,amount\nratio_min,0.8\n", "needs name and value columns"),
    ("name,value\nratio_min,0.8\n", "missing settings"),
    ("name,value\n" + "".join(f"{n},1\n" for n in bias_slicing.SETTINGS if n != "alpha") + "alpha,often\n", "above 0"),
    ("name,value\n" + "".join(f"{n},0.5\n" for n in bias_slicing.SETTINGS if n != "alpha") + "alpha,nan\n", "above 0"),
    ("name,value\n" + "".join(f"{n},1\n" for n in bias_slicing.SETTINGS if n != "ratio_min") + "ratio_min,1.25\n", "at most 1"),
])
def test_bad_settings_are_rejected(tmp_path, body, message):
    path = tmp_path / "bias.csv"
    path.write_text(body)
    with pytest.raises(ValueError, match=message):
        load_settings(path)


# Static attributes and groups


def test_acs_shares_are_computed_from_the_2025_columns():
    shares = acs_shares(population_table("name", {u: u for u in UNITS}), "name").set_index("name")
    assert shares.loc["Poor", "poverty_share"] == pytest.approx(0.4)
    assert shares.loc["Poor", "poc_share"] == pytest.approx(0.8)
    assert shares.loc["Poor", "limited_english_share"] == pytest.approx(200 * len(LIMITED_ENGLISH) / 10000)


def test_a_missing_language_value_leaves_the_share_missing_not_too_low():
    table = population_table("name", {u: u for u in UNITS})
    table.loc[0, LIMITED_ENGLISH[3]] = np.nan
    shares = acs_shares(table, "name")
    assert pd.isna(shares.loc[0, "limited_english_share"]) and shares["limited_english_share"].notna().sum() == 3


def test_a_population_table_without_a_column_is_rejected():
    with pytest.raises(ValueError, match="missing columns"):
        acs_shares(population_table("name", {u: u for u in UNITS}).drop(columns=POC[0]), "name")


def test_unit_and_tract_attributes_from_the_static_layers(tmp_path):
    static = static_dir(tmp_path)
    units = unit_attributes(static).set_index("unit")
    assert list(units.index) == UNITS
    assert units.loc["Crowded", "flood_1pct_share"] == pytest.approx(0.05, abs=0.01)
    assert units.loc["Rich", "flood_1pct_share"] == 0
    tracts = tract_attributes(static).set_index("geoid20")
    assert tracts.loc[TRACT_IDS["Poor"], "population"] == 10000


def test_a_neighborhood_without_a_population_row_is_rejected(tmp_path):
    static = static_dir(tmp_path)
    population_table("name", {u: u for u in UNITS}).iloc[1:].to_parquet(static / "population_2025_neighborhood.parquet")
    with pytest.raises(ValueError, match="no 2025 population row for neighborhoods"):
        unit_attributes(static)


def test_median_split_puts_the_median_itself_in_lower_and_keeps_missing_missing():
    groups = median_groups(pd.Series([0.1, 0.2, 0.3, 0.4, np.nan]))
    assert groups.tolist()[:4] == ["lower", "lower", "higher", "higher"]
    assert pd.isna(groups.iloc[4])
    odd = median_groups(pd.Series([0.1, 0.2, 0.3]))
    assert odd.tolist() == ["lower", "lower", "higher"]
    assert median_groups(pd.Series([np.nan, np.nan])).isna().all()


# Recorded names for requests without a location


def test_a_coarse_recorded_name_covers_the_units_its_placed_requests_fall_in():
    rows = (placed("Poor", name="Poor / Crowded", n=60) + placed("Crowded", name="Poor / Crowded", n=40)
            + placed("Rich", name="Poor / Crowded", n=2)   # under 5%: not covered
            + placed("Rich", name="Boston", n=50)          # citywide: covers nothing
            + placed("Quiet", name="Quiet", n=19))         # too few placed requests
    names = name_units(storm_requests(located_table(rows)), min_share=0.05, min_placed=20)
    assert names == {"Poor / Crowded": ("Crowded", "Poor")}


def test_a_request_whose_name_spans_two_groups_has_no_group():
    groups = pd.Series({"Crowded": "higher", "Poor": "higher", "Rich": "lower"})
    covers = pd.Series([("Crowded", "Poor"), ("Poor", "Rich"), (), ("Elsewhere",)])
    assert request_group(covers, groups).tolist()[:1] == ["higher"]
    assert request_group(covers, groups)[1:].isna().all()


# Rates and tests


def test_group_rates_match_a_plain_groupby():
    num = pd.Series([1, 0, 1, 1, 0, 0], dtype=float)
    den = pd.Series([1, 1, 1, 2, 2, 2], dtype=float)
    groups = pd.Series(["a", "a", "a", "b", "b", None], dtype="string")
    rates = group_rates(num, den, groups).set_index("group")
    assert rates.loc["a", "value"] == pytest.approx(2 / 3)
    assert rates.loc["b", "value"] == pytest.approx(1 / 4)
    assert rates.loc["all", "numerator"] == 3 and rates.loc["all", "denominator"] == 7  # no group: left out


def test_group_rates_of_nothing_are_empty():
    assert group_rates(pd.Series(dtype=float), pd.Series(dtype=float), pd.Series(dtype="string")).empty


def test_proportion_test_uses_fisher_for_small_counts_and_chi_square_for_large():
    assert proportion_p(1, 10, 9, 10) == pytest.approx(0.000347, abs=1e-6)  # Fisher's exact
    assert proportion_p(100, 1000, 100, 1000) == pytest.approx(1.0)
    assert proportion_p(50, 1000, 100, 1000) < 1e-4
    assert proportion_p(0, 100, 0, 100) == 1.0
    assert np.isnan(proportion_p(1, 0, 1, 10))


def test_rate_test_conditions_on_the_two_counts():
    assert rate_p(100, 10, 100, 10) == pytest.approx(1.0)
    assert rate_p(50, 10, 100, 10) < 1e-4
    assert rate_p(0, 10, 0, 10) == 1.0
    assert np.isnan(rate_p(5, 0, 5, 10))


def rates_of(rows):
    return pd.DataFrame(rows, columns=["group", "numerator", "denominator", "value"])


def test_a_gap_needs_both_the_four_fifths_rule_and_the_test():
    big = rates_of([("lower", 50, 1000, 0.05), ("higher", 100, 1000, 0.10)])
    [row] = gap_rows(big, "surge_rate", "pair", SETTINGS, skip_zero=True)
    assert row["group"] == "lower" and row["ratio"] == pytest.approx(0.5) and row["significant"]
    small = rates_of([("lower", 1, 20, 0.05), ("higher", 2, 20, 0.10)])  # same ratio, too few to tell
    assert not gap_rows(small, "surge_rate", "pair", SETTINGS, skip_zero=True)[0]["significant"]
    close = rates_of([("lower", 9000, 100000, 0.09), ("higher", 10000, 100000, 0.10)])  # sure, but within 4/5
    [row] = gap_rows(close, "surge_rate", "pair", SETTINGS, skip_zero=True)
    assert row["p_value"] < 0.01 and not row["significant"]


def test_each_neighborhood_is_tested_against_the_rest_of_the_city():
    rates = rates_of([("A", 10, 1000, 0.01), ("B", 50, 1000, 0.05), ("C", 50, 1000, 0.05), ("D", 50, 1000, 0.05)])
    rows = {row["group"]: row for row in gap_rows(rates, "surge_rate", "overall", SETTINGS, skip_zero=True)}
    assert rows["A"]["other_value"] == pytest.approx(150 / 3000) and rows["A"]["significant"]
    assert not rows["B"]["significant"]


def test_a_group_with_no_surges_is_left_out_of_the_surge_test_only():
    rates = rates_of([("none", 0, 1000, 0.0), ("some", 100, 1000, 0.1)])
    assert gap_rows(rates, "surge_rate", "pair", SETTINGS, skip_zero=True) == []
    assert gap_rows(rates, "missing_location", "pair", SETTINGS, skip_zero=False)[0]["significant"]


# Input tables


def test_labels_without_a_column_or_rows_are_rejected():
    with pytest.raises(ValueError, match="labels is missing columns: \\['surge'\\]"):
        label_cells(labels_table().drop(columns="surge"), pd.Series(dtype=float), 1000)
    with pytest.raises(ValueError, match="labels has no rows"):
        label_cells(labels_table().iloc[0:0], pd.Series(dtype=float), 1000)
    with pytest.raises(ValueError, match="no requests_<group> columns"):
        label_cells(labels_table().drop(columns=["requests_snow_ice", "requests_tree", "requests_flooding"]),
                    pd.Series(dtype=float), 1000)


def test_malformed_labels_are_rejected():
    bad_date = labels_table().astype({"date": str})
    bad_date.loc[3, "date"] = "not a day"
    with pytest.raises(ValueError, match="not a date"):
        label_cells(bad_date, pd.Series(dtype=float), 1000)
    bad_count = labels_table().astype({"requests_tree": object})
    bad_count.loc[3, "requests_tree"] = "many"
    with pytest.raises(ValueError):
        label_cells(bad_count, pd.Series(dtype=float), 1000)


def test_unlabeled_days_count_against_coverage_not_as_false_surges():
    cells = label_cells(labels_table(end="2016-12-31"), pd.Series({u: 1000.0 for u in UNITS}), 1000)
    first_year = cells["date"].dt.year == 2015
    assert (cells.loc[first_year, "labeled"] == 0).all() and (cells.loc[~first_year, "labeled"] == 1).all()
    assert (cells.loc[first_year, "surge"] == 0).all()


def test_located_without_a_column_or_rows_is_rejected():
    with pytest.raises(ValueError, match="311_located is missing columns"):
        storm_requests(located_table(placed("Poor")).drop(columns="location"))
    with pytest.raises(ValueError, match="311_located has no rows"):
        storm_requests(located_table(placed("Poor")).iloc[0:0])


def test_a_request_counts_on_its_boston_local_day_at_the_period_boundary():
    # open_dt is Boston local time (ADR-020), so the last second of 2023 is a training day.
    storm = storm_requests(located_table([("2023-12-31 23:59:59", "legacy", "Poor", "placed", "Poor"),
                                          ("2024-01-01 00:00:00", "legacy", "Poor", "placed", "Poor")]))
    assert storm["period"].tolist() == ["train 2015 to 2023", "validate 2024"]
    assert storm["date"].dt.day.tolist() == [31, 1]


def test_non_storm_requests_are_left_out():
    df = located_table(placed("Poor", n=2))
    df.loc[0, "storm_group"] = None
    assert len(storm_requests(df)) == 1


def test_eras_match_the_seasons_one_year_apart():
    storm = storm_requests(located_table(placed("Poor", day="2025-10-27", system="new")
                                         + placed("Poor", day="2024-01-01")))
    eras = system_eras(storm, pd.Timestamp("2015-01-01"), pd.Timestamp("2026-06-30"), 365)
    assert eras["migration era"] == (pd.Timestamp("2025-10-27"), pd.Timestamp("2026-06-30"))
    assert eras["legacy era"] == (pd.Timestamp("2024-10-27"), pd.Timestamp("2025-06-30"))
    assert system_eras(storm_requests(located_table(placed("Poor"))), pd.Timestamp("2015-01-01"),
                       pd.Timestamp("2026-06-30"), 365) == {}


# The whole slicing


def world(tmp_path, requests=None, surges=None, located_rows=None):
    static = static_dir(tmp_path)
    rows = located_rows if located_rows is not None else [r for unit in UNITS for r in placed(unit, n=30)]
    return bias_tables(labels_table(surges=surges, requests=requests), located_table(rows),
                       unit_attributes(static), tract_attributes(static), SETTINGS)


def test_a_planted_per_capita_gap_is_found_and_listed_with_numbers(tmp_path):
    # Poor and Crowded have 30,000 residents and 2 requests a day; Rich and Quiet 30,000 and 6.
    rates, gaps, _ = world(tmp_path, requests={"Crowded": 1, "Poor": 1, "Quiet": 3, "Rich": 3})
    found = gaps[gaps["significant"] & (gaps["slice"] == "poverty_share") & (gaps["metric"] == "requests_per_capita")]
    assert set(found["period"]) == {"train 2015 to 2023", "validate 2024", "2025 onward"}
    train = found[found["period"] == "train 2015 to 2023"].iloc[0]
    assert train["group"] == "higher" and train["ratio"] == pytest.approx(1 / 3)
    assert train["value"] == pytest.approx(2 * 365.25 / 30000 * 1000)  # per 1,000 residents a year
    # The tract check counts the placed requests in 311_located (30 per unit, the same everywhere),
    # not the label table's counts, so it finds no gap here.
    tract = gaps[(gaps["slice"] == "tract poverty_share") & (gaps["period"] == "train 2015 to 2023")].iloc[0]
    assert tract["numerator"] == 60 and tract["ratio"] == pytest.approx(1.0) and not tract["significant"]


def test_equal_neighborhoods_give_no_significant_gap(tmp_path):
    _, gaps, info = world(tmp_path, requests={"Crowded": 2, "Poor": 1, "Quiet": 1, "Rich": 2})
    label_gaps = gaps[gaps["metric"].isin(["surge_rate", "label_coverage"])]
    assert not label_gaps["significant"].any()
    assert info["groups"]["poverty_share"] == {"higher": ["Crowded", "Poor"], "lower": ["Quiet", "Rich"]}
    assert info["groups"]["flood_zone"] == {"in flood zone": ["Crowded", "Poor"], "not in flood zone": ["Quiet", "Rich"]}


def test_a_planted_surge_gap_is_found(tmp_path):
    rare = lambda day: day % 50 == 0  # noqa: E731
    often = lambda day: day % 10 == 0  # noqa: E731
    _, gaps, _ = world(tmp_path, surges={"Crowded": rare, "Poor": rare, "Quiet": often, "Rich": often})
    train = gaps[(gaps["slice"] == "poverty_share") & (gaps["metric"] == "surge_rate")
                 & (gaps["period"] == "train 2015 to 2023")].iloc[0]
    assert train["significant"] and train["ratio"] == pytest.approx(0.2, abs=0.02)


def test_missing_locations_count_under_the_neighborhoods_their_name_covers(tmp_path):
    rows = [r for unit in UNITS for r in placed(unit, n=300)]
    rows += unplaced("Poor", n=30) + unplaced("Boston", n=100) + unplaced(None, n=100)
    rates, gaps, info = world(tmp_path, located_rows=rows)
    assert info["unplaced"] == 230 and info["unplaced_named"] == 30
    poverty = rates[(rates["slice"] == "poverty_share") & (rates["metric"] == "missing_location")
                    & (rates["period"] == "train 2015 to 2023")].set_index("group")
    assert poverty.loc["higher", "numerator"] == 30 and poverty.loc["higher", "denominator"] == 630
    assert poverty.loc["lower", "value"] == 0


def test_the_system_slice_compares_the_eras_and_the_systems(tmp_path):
    rows = [r for unit in UNITS for r in placed(unit, n=30)]
    rows += placed("Poor", day="2025-10-27", system="new", n=10) + placed("Poor", day="2025-11-01", n=10)
    rows += unplaced("Poor", day="2025-11-02", n=5)
    rates, _, info = world(tmp_path, located_rows=rows)
    assert set(info["eras"]) == {"legacy era", "migration era"}
    system = rates[(rates["slice"] == "system") & (rates["metric"] == "missing_location")].set_index("group")
    assert system.loc["legacy system", "numerator"] == 5 and system.loc["new system", "numerator"] == 0


def test_main_writes_the_report_and_both_tables(tmp_path, monkeypatch, capsys):
    static = static_dir(tmp_path)
    labels_table(requests={"Crowded": 1, "Poor": 1, "Quiet": 3, "Rich": 3}).to_parquet(tmp_path / "labels.parquet")
    located_table([r for unit in UNITS for r in placed(unit, n=30)]).to_parquet(tmp_path / "located.parquet")
    out = tmp_path / "reports" / "bias"
    monkeypatch.setattr(sys, "argv", [
        "bias_slicing.py", "--labels", str(tmp_path / "labels.parquet"), "--located", str(tmp_path / "located.parquet"),
        "--static-dir", str(static), "--config", str(CONFIG), "--out-dir", str(out)])
    bias_slicing.main()
    assert {p.name for p in out.iterdir()} == {"slices.csv", "gaps.csv", "report.md"}
    report = (out / "report.md").read_text()
    assert "## Significant gaps" in report and "| poverty_share | requests per 1,000 residents a year |" in report
    assert "significant gaps; wrote" in capsys.readouterr().out
    gaps = pd.read_csv(out / "gaps.csv")
    assert {"slice", "metric", "period", "group", "ratio", "p_value", "significant"} <= set(gaps.columns)


def test_main_names_a_column_missing_from_311_located(tmp_path, monkeypatch):
    static = static_dir(tmp_path)
    labels_table().to_parquet(tmp_path / "labels.parquet")
    located_table(placed("Poor")).drop(columns="location").to_parquet(tmp_path / "located.parquet")
    monkeypatch.setattr(sys, "argv", [
        "bias_slicing.py", "--labels", str(tmp_path / "labels.parquet"), "--located", str(tmp_path / "located.parquet"),
        "--static-dir", str(static), "--config", str(CONFIG), "--out-dir", str(tmp_path / "out")])
    with pytest.raises(ValueError, match="311_located is missing columns: \\['location'\\]"):
        bias_slicing.main()
    assert not (tmp_path / "out").exists()
