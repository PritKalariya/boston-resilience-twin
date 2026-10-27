"""Place every 311 request in a 2020 tract and its ADR-004 neighborhood (#14).

Reads 311_combined.parquet from map_311.py and the static tracts and
neighborhoods from static_layers.py. Writes 311_located.parquet: the combined
table plus geoid20, unit (the neighborhood the request is counted in) and
location, which says why a request has no unit. Requests are placed by their
coordinates, not by the 311 neighborhood field, so both systems share one unit
list (ADR-004).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd

PLACED = "placed"
# Why a request has no unit, in the order they are checked.
NO_COORDINATES = "no_coordinates"
PLACEHOLDER = "placeholder"
OUTSIDE = "outside_boston"
NO_NEIGHBORHOOD = "no_neighborhood"
LOCATIONS = [PLACED, NO_COORDINATES, PLACEHOLDER, OUTSIDE, NO_NEIGHBORHOOD]

# A latitude in this range with a longitude in BOSTON_LAT is a swapped pair.
BOSTON_LAT = (41.5, 43.0)
BOSTON_LON = (-72.0, -70.0)
# About a metre. Rounding instead would split points like 42.360295 both ways.
PLACEHOLDER_TOLERANCE = 1e-5
REQUIRED_COLUMNS = {"case_id", "source_system", "open_dt", "latitude", "longitude"}


def load_placeholders(path: Path | str) -> pd.DataFrame:
    """The placeholder coordinates that stand for no real location. Raises on a missing column or a value that
    is not a number."""
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = {"latitude", "longitude"} - set(table.columns)
    if missing:
        raise ValueError(f"{Path(path).name} is missing columns: {sorted(missing)}")
    points = table[["latitude", "longitude"]].apply(pd.to_numeric, errors="coerce")
    if points.isna().any().any():
        raise ValueError(f"{Path(path).name} has a latitude or longitude that is not a number")
    return points


def tract_units(tracts: gpd.GeoDataFrame, neighborhoods: gpd.GeoDataFrame) -> pd.Series:
    """Neighborhood of each tract, by the tract's representative point.

    The neighborhoods are unions of whole 2020 tracts. The city leaves tract
    9815.02 (no population, by Suffolk Downs) out of all of them.
    """
    points = gpd.GeoDataFrame(
        {"geoid20": tracts["geoid20"].to_numpy()}, geometry=tracts.representative_point().to_numpy(), crs=tracts.crs
    )
    joined = gpd.sjoin(points, neighborhoods[["neighborhood", "geometry"]], how="left", predicate="within")
    return joined.drop_duplicates("geoid20").set_index("geoid20")["neighborhood"]


def tract_of_points(lat: pd.Series, lon: pd.Series, tracts: gpd.GeoDataFrame) -> pd.Series:
    """2020 tract of each point. A point on a shared edge goes to the lower geoid20."""
    points = gpd.GeoDataFrame(index=lat.index, geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326").to_crs(tracts.crs)
    joined = gpd.sjoin(points, tracts[["geoid20", "geometry"]], how="left", predicate="intersects")
    first = joined.sort_values("geoid20").groupby(level=0)["geoid20"].first()
    return first.reindex(lat.index)


def coordinates(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Numeric latitude and longitude, with swapped pairs put back. Also returns which rows were swapped."""
    lat = pd.to_numeric(df["latitude"], errors="coerce")
    lon = pd.to_numeric(df["longitude"], errors="coerce")
    swapped = lat.between(*BOSTON_LON) & lon.between(*BOSTON_LAT)
    return lat.where(~swapped, lon), lon.where(~swapped, lat), swapped


def locate(
    df: pd.DataFrame, tracts: gpd.GeoDataFrame, units: pd.Series, placeholders: pd.DataFrame
) -> tuple[pd.DataFrame, int]:
    """Add geoid20, unit and location to every row. Returns the table and the number of swapped pairs fixed."""
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"combined table is missing columns: {sorted(missing)}")
    lat, lon, swapped = coordinates(df)
    no_coords = lat.isna() | lon.isna()
    placeholder = pd.Series(False, index=df.index)
    for p_lat, p_lon in placeholders.itertuples(index=False):
        placeholder |= ((lat - p_lat).abs() < PLACEHOLDER_TOLERANCE) & ((lon - p_lon).abs() < PLACEHOLDER_TOLERANCE)

    # Many requests share an address, so each distinct point is placed once.
    usable = ~no_coords & ~placeholder
    points = pd.DataFrame({"lat": lat[usable], "lon": lon[usable]}).drop_duplicates()
    points["geoid20"] = tract_of_points(points["lat"], points["lon"], tracts)
    tract = pd.DataFrame({"lat": lat, "lon": lon}).merge(points, on=["lat", "lon"], how="left")["geoid20"]
    tract.index = df.index
    tract = tract.where(usable)
    unit = tract.map(units)

    location = pd.Series(PLACED, index=df.index)
    location[no_coords] = NO_COORDINATES
    location[placeholder] = PLACEHOLDER
    location[usable & tract.isna()] = OUTSIDE
    location[tract.notna() & unit.isna()] = NO_NEIGHBORHOOD
    out = df.assign(geoid20=tract.astype("string"), unit=unit.astype("string"), location=location)
    return out, int((swapped & usable).sum())


def location_report(df: pd.DataFrame) -> pd.DataFrame:
    """Rows per year and system by location, and the share with no unit."""
    year = df["open_dt"].dt.year.rename("year")
    table = df.groupby([year, df["source_system"]])["location"].value_counts().unstack(fill_value=0)
    table = table.reindex(columns=LOCATIONS, fill_value=0)
    table.insert(0, "rows", table.sum(axis=1))
    table["no_unit_pct"] = (100 * (table["rows"] - table[PLACED]) / table["rows"]).round(2)
    return table.reset_index()


def markdown(df: pd.DataFrame) -> str:
    """A table as GitHub markdown, missing values left blank."""
    cells = df.astype(object).where(df.notna(), "").astype(str)
    lines = ["| " + " | ".join(map(str, df.columns)) + " |", "|" + "---|" * len(df.columns)]
    lines += ["| " + " | ".join(row) + " |" for row in cells.itertuples(index=False)]
    return "\n".join(lines)


def main() -> None:
    """Place every combined request in a tract and unit, write 311_located.parquet and print the share without
    a unit by year."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined", default="Data-Pipeline/data/interim/311_combined.parquet")
    parser.add_argument("--tracts", default="Data-Pipeline/data/interim/static/tracts_2020.parquet")
    parser.add_argument("--neighborhoods", default="Data-Pipeline/data/interim/static/neighborhoods_by_tract20.parquet")
    parser.add_argument("--placeholders", default="Data-Pipeline/config/placeholder_locations.csv")
    parser.add_argument("--out", default="Data-Pipeline/data/interim/311_located.parquet")
    args = parser.parse_args()

    tracts = gpd.read_parquet(args.tracts)
    units = tract_units(tracts, gpd.read_parquet(args.neighborhoods))
    located, swapped = locate(pd.read_parquet(args.combined), tracts, units, load_placeholders(args.placeholders))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    located.to_parquet(out, index=False)

    counts = located["location"].value_counts().reindex(LOCATIONS, fill_value=0)
    print(f"placed {counts[PLACED]:,} of {len(located):,} requests in {located['unit'].nunique()} units; "
          f"fixed {swapped:,} swapped latitude and longitude pairs")
    print(", ".join(f"{name} {n:,}" for name, n in counts.items() if name != PLACED))
    print("\n## No location, by year\n")
    print(markdown(location_report(located)))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
