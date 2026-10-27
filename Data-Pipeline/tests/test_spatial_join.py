import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

import spatial_join
from spatial_join import (
    LOCATIONS,
    NO_COORDINATES,
    NO_NEIGHBORHOOD,
    OUTSIDE,
    PLACED,
    PLACEHOLDER,
    load_placeholders,
    locate,
    location_report,
    tract_units,
)

STATIC = Path(__file__).resolve().parents[1] / "data" / "interim" / "static"

# Three tracts side by side; the third is in no neighborhood, like 9815.02.
TRACTS = gpd.GeoDataFrame(
    {"geoid20": ["25025000100", "25025000200", "25025981502"]},
    geometry=[box(-71.10, 42.30, -71.05, 42.35), box(-71.05, 42.30, -71.00, 42.35), box(-71.00, 42.30, -70.95, 42.35)],
    crs="EPSG:4326",
)
NEIGHBORHOODS = gpd.GeoDataFrame(
    {"neighborhood": ["West", "East"]},
    geometry=[box(-71.10, 42.30, -71.05, 42.35), box(-71.05, 42.30, -71.00, 42.35)],
    crs="EPSG:4326",
)
PLACEHOLDERS = pd.DataFrame({"latitude": [42.32], "longitude": [-71.08]})


def requests(points):
    n = len(points)
    return pd.DataFrame({
        "case_id": [str(i) for i in range(n)],
        "source_system": ["legacy"] * n,
        "open_dt": pd.to_datetime(["2024-01-01"] * n),
        "latitude": [p[0] for p in points],
        "longitude": [p[1] for p in points],
    })


def run(points):
    return locate(requests(points), TRACTS, tract_units(TRACTS, NEIGHBORHOODS), PLACEHOLDERS)


def test_each_request_gets_its_tract_and_unit():
    out, _ = run([(42.31, -71.09), (42.33, -71.02)])
    assert out["geoid20"].tolist() == ["25025000100", "25025000200"]
    assert out["unit"].tolist() == ["West", "East"]
    assert (out["location"] == PLACED).all()


def test_rows_without_a_unit_say_why():
    out, _ = run([
        (np.nan, -71.09),       # no latitude
        (42.320004, -71.080003),  # within a metre of the placeholder
        (38.90, -76.99),        # Washington
        (42.32, -70.97),        # the tract with no neighborhood
    ])
    assert out["location"].tolist() == [NO_COORDINATES, PLACEHOLDER, OUTSIDE, NO_NEIGHBORHOOD]
    assert out["unit"].isna().all()
    assert out["geoid20"].isna().tolist() == [True, True, True, False]


def test_unparseable_coordinates_count_as_missing():
    df = requests([(42.31, -71.09), (42.31, -71.09)])
    df["latitude"] = ["42.31", "north"]
    out, _ = locate(df, TRACTS, tract_units(TRACTS, NEIGHBORHOODS), PLACEHOLDERS)
    assert out["location"].tolist() == [PLACED, NO_COORDINATES]


def test_swapped_latitude_and_longitude_are_put_back():
    out, swapped = run([(-71.09, 42.31)])
    assert swapped == 1
    assert out.loc[0, "unit"] == "West"


def test_point_on_a_shared_edge_goes_to_the_lower_tract():
    out, _ = run([(42.32, -71.05)])
    assert out.loc[0, "geoid20"] == "25025000100"


def test_duplicate_points_are_placed_alike_and_row_order_kept():
    points = [(42.33, -71.02), (42.31, -71.09), (42.33, -71.02)]
    out, _ = run(points)
    assert out["case_id"].tolist() == ["0", "1", "2"]
    assert out["unit"].tolist() == ["East", "West", "East"]


def test_empty_input():
    out, swapped = run([])
    assert out.empty and swapped == 0
    assert {"geoid20", "unit", "location"} <= set(out.columns)


def test_missing_columns_are_rejected():
    with pytest.raises(ValueError, match="latitude"):
        locate(requests([(42.31, -71.09)]).drop(columns="latitude"), TRACTS, pd.Series(dtype=str), PLACEHOLDERS)


def test_placeholder_config(tmp_path):
    good = tmp_path / "good.csv"
    good.write_text("latitude,longitude,note\n42.360295,-71.05827,City Hall\n")
    assert load_placeholders(good).iloc[0].tolist() == [42.360295, -71.05827]
    bad = tmp_path / "bad.csv"
    bad.write_text("latitude,longitude\nnorth,-71.05827\n")
    with pytest.raises(ValueError, match="not a number"):
        load_placeholders(bad)
    with pytest.raises(ValueError, match="missing columns"):
        (tmp_path / "cols.csv").write_text("lat,lon\n1,2\n")
        load_placeholders(tmp_path / "cols.csv")


def test_report_counts_every_row_once():
    out, _ = run([(42.31, -71.09), (np.nan, np.nan), (38.90, -76.99), (42.33, -71.02)])
    report = location_report(out)
    assert report["rows"].sum() == 4
    assert report[LOCATIONS].sum(axis=1).tolist() == report["rows"].tolist()
    assert report.loc[0, "no_unit_pct"] == 50.0


# Checked against the Census Bureau geocoder (2020 tracts) and each landmark's
# known neighborhood, independently of the city's boundary files.
LANDMARKS = [
    ("Fenway Park", 42.34665, -71.09719, "25025010206", "Fenway"),
    ("Massachusetts State House", 42.35770, -71.06380, "25025020302", "Beacon Hill"),
    ("Faneuil Hall", 42.36005, -71.05616, "25025030302", "Downtown"),
    ("Boston Public Library, Copley", 42.34940, -71.07800, "25025010600", "Back Bay"),
    ("Bunker Hill Monument", 42.37628, -71.06073, "25025040100", "Charlestown"),
    ("JFK Presidential Library", 42.31630, -71.03400, "25025090901", "Dorchester"),
    ("Old North Church", 42.36632, -71.05442, "25025030400", "North End"),
    ("Mattapan Square", 42.26750, -71.09240, "25025101002", "Mattapan"),
    ("Cleary Square", 42.25600, -71.12450, "25025140300", "Hyde Park"),
    ("Maverick Square", 42.36910, -71.03960, "25025050400", "East Boston"),
    ("Brighton Center", 42.34920, -71.15280, "25025000601", "Brighton"),
    ("Nubian Square", 42.32930, -71.08390, "25025080601", "Roxbury"),
]


@pytest.mark.skipif(not (STATIC / "tracts_2020.parquet").exists(), reason="static layers not loaded (static_layers.py)")
def test_hand_checked_landmarks():
    tracts = gpd.read_parquet(STATIC / "tracts_2020.parquet")
    units = tract_units(tracts, gpd.read_parquet(STATIC / "neighborhoods_by_tract20.parquet"))
    out, _ = locate(requests([(lat, lon) for _, lat, lon, _, _ in LANDMARKS]), tracts, units, PLACEHOLDERS)
    assert out["geoid20"].tolist() == [tract for *_, tract, _ in LANDMARKS]
    assert out["unit"].tolist() == [unit for *_, unit in LANDMARKS]


def test_main_places_requests_and_writes_the_located_table(tmp_path, monkeypatch, capsys):
    # The spatial_join task runs this main() (pipeline_steps.run_step).
    TRACTS.to_parquet(tmp_path / "tracts.parquet")
    NEIGHBORHOODS.to_parquet(tmp_path / "hoods.parquet")
    requests([(42.31, -71.09), (42.33, -71.02), (np.nan, np.nan)]).to_parquet(tmp_path / "combined.parquet", index=False)
    (tmp_path / "placeholders.csv").write_text("latitude,longitude\n42.32,-71.08\n")
    out = tmp_path / "interim" / "311_located.parquet"
    monkeypatch.setattr(sys, "argv", [
        "spatial_join.py", "--combined", str(tmp_path / "combined.parquet"), "--tracts", str(tmp_path / "tracts.parquet"),
        "--neighborhoods", str(tmp_path / "hoods.parquet"), "--placeholders", str(tmp_path / "placeholders.csv"),
        "--out", str(out)])
    spatial_join.main()
    located = pd.read_parquet(out)
    assert located["location"].tolist() == [PLACED, PLACED, NO_COORDINATES]
    assert located["unit"].tolist()[:2] == ["West", "East"]
    printed = capsys.readouterr().out
    assert "placed 2 of 3 requests in 2 units" in printed
    assert "| year | source_system | rows |" in printed
