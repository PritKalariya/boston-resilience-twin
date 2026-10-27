"""Load the static layers listed in Data-Pipeline/config/static_layers.csv.

Boundaries, Climate Ready Boston flood and social vulnerability layers, and
the city's 2025 population estimates (which carry the ACS demographics).
Vector layers are checked, repaired where invalid, reprojected to EPSG:4326
and written as GeoParquet; tables are written as parquet. Every layer gets a
row in docs/data/static-layers.csv with its source URL, vintage and CRS.
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyogrio.errors import DataSourceError

STORED_CRS = "EPSG:4326"
# Generous box around Boston and the harbor islands, in EPSG:4326. A layer
# outside it was most likely read in the wrong CRS.
BOSTON_BOUNDS = (-71.4, 42.1, -70.6, 42.6)
CONFIG_COLUMNS = {"name", "kind", "file", "url", "publisher", "vintage", "license", "key", "text_columns"}
KINDS = {"vector", "table", "document"}
USER_AGENT = "boston-resilience-twin (IE7374 course project)"


@dataclass
class LayerReport:
    """What loading one layer did: rows kept, the source CRS, rows dropped per reason and geometries repaired."""
    name: str
    rows: int = 0
    source_crs: str = ""
    dropped: dict[str, int] = field(default_factory=dict)
    repaired: int = 0

    def drop(self, reason: str, count) -> None:
        """Count rows dropped for reason; zero leaves no entry."""
        if int(count):
            self.dropped[reason] = self.dropped.get(reason, 0) + int(count)


def read_config(path: Path | str) -> pd.DataFrame:
    """The layer list, checked: every column present, known kinds and unique names."""
    config = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = CONFIG_COLUMNS - set(config.columns)
    if missing:
        raise ValueError(f"{Path(path).name} is missing columns: {sorted(missing)}")
    bad_kind = set(config["kind"]) - KINDS
    if bad_kind:
        raise ValueError(f"unknown layer kind: {sorted(bad_kind)}")
    if config["name"].duplicated().any():
        raise ValueError(f"duplicate layer names: {sorted(config.loc[config['name'].duplicated(), 'name'])}")
    return config


class DefaultPortRedirect(urllib.request.HTTPRedirectHandler):
    """Drop an explicit :443 from redirect targets.

    Analyze Boston redirects downloads to signed S3 URLs written as
    s3.amazonaws.com:443. urllib then sends the port in the Host header,
    the signature no longer matches, and S3 answers 403.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Follow the redirect with :443 taken out of an https target (see the class docstring)."""
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme == "https" and parts.port == 443:
            newurl = parts._replace(netloc=parts.netloc.replace(":443", "", 1)).geturl()
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, dest: Path) -> None:
    """Fetch url to dest through a .part file, so a failed download never leaves a partial dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(DefaultPortRedirect)
    with opener.open(request, timeout=120) as response, open(partial, "wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)
    partial.replace(dest)


def sha256(path: Path) -> str:
    """SHA-256 of a file, read in 1 MB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def check_file(path: Path) -> None:
    """Fail when a layer's file is missing or empty."""
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run static_layers.py --download")
    if path.stat().st_size == 0:
        raise ValueError(f"{path.name} is empty")


def check_key(df: pd.DataFrame, key: str, source: str) -> None:
    """Fail when the key column is missing or has blank values; an empty key checks nothing."""
    if not key:
        return
    if key not in df.columns:
        raise ValueError(f"{source} is missing columns: [{key!r}]")
    values = df[key].astype("string").str.strip()
    if values.isna().any() or (values == "").any():
        raise ValueError(f"{source} has blank {key} values")
    if values.duplicated().any():
        raise ValueError(f"{source} has duplicate {key} values: {sorted(values[values.duplicated()])[:5]}")


def read_vector(path: Path | str, key: str = "") -> tuple[gpd.GeoDataFrame, LayerReport]:
    """Read a vector layer: drop features without geometry, repair invalid ones, reproject to EPSG:4326 and
    check it lies in Boston."""
    path = Path(path)
    report = LayerReport(name=path.stem)
    check_file(path)
    try:
        gdf = gpd.read_file(path)
    except DataSourceError as e:
        raise ValueError(f"{path.name} could not be read as a vector layer: {e}") from e
    if gdf.empty:
        raise ValueError(f"{path.name} has no features")
    if gdf.crs is None:
        raise ValueError(f"{path.name} has no CRS")
    report.source_crs = gdf.crs.to_string()

    no_geometry = gdf.geometry.isna() | gdf.geometry.is_empty
    report.drop("missing_geometry", no_geometry.sum())
    gdf = gdf[~no_geometry]
    if gdf.empty:
        raise ValueError(f"{path.name} has no features with geometry")
    check_key(gdf, key, path.name)

    invalid = ~gdf.is_valid
    report.repaired = int(invalid.sum())
    if report.repaired:
        gdf.loc[invalid, "geometry"] = gdf.loc[invalid].geometry.make_valid()

    # shape_wkt repeats the geometry as text in the city's GeoJSON files.
    gdf = gdf.drop(columns=["shape_wkt"], errors="ignore").to_crs(STORED_CRS)
    minx, miny, maxx, maxy = gdf.total_bounds
    west, south, east, north = BOSTON_BOUNDS
    if minx < west or miny < south or maxx > east or maxy > north:
        raise ValueError(f"{path.name} lies outside Boston after reprojection; check its CRS ({report.source_crs})")

    report.rows = len(gdf)
    return gdf.reset_index(drop=True), report


def read_table(path: Path | str, key: str = "", text_columns: tuple[str, ...] = ()) -> tuple[pd.DataFrame, LayerReport]:
    """Read a table layer as text, drop blank rows, and turn every column except the key and text_columns into
    numbers."""
    path = Path(path)
    report = LayerReport(name=path.stem)
    check_file(path)
    try:
        df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    except (pd.errors.ParserError, UnicodeDecodeError) as e:
        raise ValueError(f"{path.name} could not be read as a table: {e}") from e

    # The tract table ends with a row that has only Shape_Length and
    # Shape_Area (both 0), left over from the city's GIS export.
    data_columns = [c for c in df.columns if not c.lower().startswith("shape_")]
    blank = df[data_columns].isna().all(axis=1)
    report.drop("blank_row", blank.sum())
    df = df[~blank]
    if df.empty:
        raise ValueError(f"{path.name} has no rows")
    check_key(df, key, path.name)

    for column in df.columns.difference(list(text_columns) + [key]):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    report.rows = len(df)
    return df.reset_index(drop=True), report


def load_layer(layer: pd.Series, raw_dir: Path, out_dir: Path) -> LayerReport:
    """Write one configured layer to out_dir as parquet (a document is only checked). Returns its report."""
    path = raw_dir / layer["file"]
    if layer["kind"] == "document":
        check_file(path)
        return LayerReport(name=layer["name"])

    out = out_dir / f"{layer['name']}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    if layer["kind"] == "vector":
        gdf, report = read_vector(path, layer["key"])
        gdf.to_parquet(out, index=False)
    else:
        text_columns = tuple(c for c in layer["text_columns"].split(";") if c)
        df, report = read_table(path, layer["key"], text_columns)
        df.to_parquet(out, index=False)
    report.name = layer["name"]
    return report


def manifest_row(layer: pd.Series, report: LayerReport, raw_dir: Path) -> dict:
    """The layer's row in docs/data/static-layers.csv: source, vintage, license, CRS, rows, retrieval date and
    SHA-256."""
    path = raw_dir / layer["file"]
    retrieved = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).date().isoformat()
    return {
        "name": layer["name"],
        "kind": layer["kind"],
        "file": layer["file"],
        "source_url": layer["url"],
        "publisher": layer["publisher"],
        "vintage": layer["vintage"],
        "license": layer["license"],
        "source_crs": report.source_crs,
        "stored_crs": STORED_CRS if layer["kind"] == "vector" else "",
        "rows": report.rows if layer["kind"] != "document" else "",
        "retrieved": retrieved,
        "sha256": sha256(path),
    }


def main() -> None:
    """Load every configured layer (fetching missing ones with --download, all of them with --refresh) and
    write the manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="Data-Pipeline/config/static_layers.csv")
    parser.add_argument("--raw-dir", default="Data-Pipeline/data/raw/static")
    parser.add_argument("--out-dir", default="Data-Pipeline/data/interim/static")
    parser.add_argument("--manifest", default="docs/data/static-layers.csv")
    parser.add_argument("--download", action="store_true", help="fetch layers whose file is missing")
    parser.add_argument("--refresh", action="store_true", help="fetch every layer again")
    args = parser.parse_args()

    config = read_config(args.config)
    raw_dir, out_dir = Path(args.raw_dir), Path(args.out_dir)

    if args.download or args.refresh:
        for _, layer in config.iterrows():
            dest = raw_dir / layer["file"]
            if args.refresh or not dest.exists():
                download(layer["url"], dest)
                print(f"downloaded {layer['file']} ({dest.stat().st_size:,} bytes)")

    rows, summary = [], []
    for _, layer in config.iterrows():
        report = load_layer(layer, raw_dir, out_dir)
        rows.append(manifest_row(layer, report, raw_dir))
        summary.append({
            "layer": report.name,
            "rows": report.rows,
            "source_crs": report.source_crs,
            "repaired": report.repaired,
            "dropped": ", ".join(f"{k} {v}" for k, v in report.dropped.items()),
        })

    manifest = Path(args.manifest)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(manifest, index=False)
    print(pd.DataFrame(summary).to_string(index=False))
    print(f"wrote {len(rows)} layers to {manifest} and {out_dir}")


if __name__ == "__main__":
    main()
