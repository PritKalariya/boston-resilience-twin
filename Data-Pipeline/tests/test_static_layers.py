import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon

from static_layers import (
    USER_AGENT,
    DefaultPortRedirect,
    download,
    load_layer,
    manifest_row,
    read_config,
    read_table,
    read_vector,
)

CONFIG_HEADER = "name,kind,file,url,publisher,vintage,license,key,text_columns\n"

# A small square in Boston (EPSG:4326) and the same square in EPSG:3857.
SQUARE = [[-71.06, 42.35], [-71.05, 42.35], [-71.05, 42.36], [-71.06, 42.36], [-71.06, 42.35]]
SQUARE_3857 = [[-7910183.0, 5212834.0], [-7909070.0, 5212834.0], [-7909070.0, 5214334.0], [-7910183.0, 5214334.0], [-7910183.0, 5212834.0]]
# Self-intersecting bow tie: invalid but repairable.
BOW_TIE = [[-71.06, 42.35], [-71.05, 42.36], [-71.05, 42.35], [-71.06, 42.36], [-71.06, 42.35]]


def feature(coords, **props):
    return {"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [coords]}}


def write_geojson(tmp_path, features, crs=None, name="layer.geojson"):
    collection = {"type": "FeatureCollection", "features": features}
    if crs:
        collection["crs"] = {"type": "name", "properties": {"name": crs}}
    path = tmp_path / name
    path.write_text(json.dumps(collection))
    return path


def write(tmp_path, body, name="table.csv"):
    path = tmp_path / name
    path.write_text(body)
    return path


# Vector layers

def test_vector_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="--download"):
        read_vector(tmp_path / "absent.geojson")


def test_vector_empty_file(tmp_path):
    with pytest.raises(ValueError, match="is empty"):
        read_vector(write(tmp_path, "", "layer.geojson"))


def test_vector_no_features(tmp_path):
    with pytest.raises(ValueError, match="no features"):
        read_vector(write_geojson(tmp_path, []))


def test_vector_malformed_json(tmp_path):
    with pytest.raises(ValueError, match="could not be read"):
        read_vector(write(tmp_path, '{"type": "FeatureCollection", "features": [', "layer.geojson"))


def test_vector_missing_key_column(tmp_path):
    path = write_geojson(tmp_path, [feature(SQUARE, other="a")])
    with pytest.raises(ValueError, match="missing columns"):
        read_vector(path, key="geoid20")


def test_vector_duplicate_key(tmp_path):
    path = write_geojson(tmp_path, [feature(SQUARE, geoid20="1"), feature(SQUARE, geoid20="1")])
    with pytest.raises(ValueError, match="duplicate geoid20"):
        read_vector(path, key="geoid20")


def test_vector_blank_key(tmp_path):
    path = write_geojson(tmp_path, [feature(SQUARE, geoid20="1"), feature(SQUARE, geoid20=None)])
    with pytest.raises(ValueError, match="blank geoid20"):
        read_vector(path, key="geoid20")


def test_vector_missing_geometry_is_counted(tmp_path):
    no_geometry = {"type": "Feature", "properties": {"geoid20": "2"}, "geometry": None}
    gdf, report = read_vector(write_geojson(tmp_path, [feature(SQUARE, geoid20="1"), no_geometry]), key="geoid20")
    assert gdf["geoid20"].tolist() == ["1"]
    assert report.dropped == {"missing_geometry": 1}


def test_vector_invalid_geometry_is_repaired(tmp_path):
    gdf, report = read_vector(write_geojson(tmp_path, [feature(BOW_TIE, id="a")]))
    assert report.repaired == 1
    assert gdf.is_valid.all()


def test_vector_is_reprojected_and_source_crs_recorded(tmp_path):
    gdf, report = read_vector(write_geojson(tmp_path, [feature(SQUARE_3857, id="a")], crs="EPSG:3857"))
    assert report.source_crs == "EPSG:3857"
    assert gdf.crs.to_string() == "EPSG:4326"
    minx, miny, maxx, maxy = gdf.total_bounds
    assert -71.07 < minx < maxx < -71.04 and 42.34 < miny < maxy < 42.37


def test_vector_geojson_without_crs_is_wgs84(tmp_path):
    _, report = read_vector(write_geojson(tmp_path, [feature(SQUARE, id="a")]))
    assert report.source_crs == "EPSG:4326"


def test_vector_wrong_crs_is_caught(tmp_path):
    # Web Mercator coordinates labelled as degrees land far outside Boston.
    path = write_geojson(tmp_path, [feature(SQUARE_3857, id="a")], crs="EPSG:4326")
    with pytest.raises(ValueError, match="outside Boston"):
        read_vector(path)


@pytest.mark.filterwarnings("ignore:'crs' was not provided")
def test_vector_without_crs_is_rejected(tmp_path):
    # A shapefile shipped without its .prj file carries no CRS.
    path = tmp_path / "layer.shp"
    gpd.GeoDataFrame({"id": ["a"]}, geometry=[Polygon(SQUARE)]).to_file(path)
    assert not path.with_suffix(".prj").exists()
    with pytest.raises(ValueError, match="no CRS"):
        read_vector(path)


# Tables

def test_table_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_table(tmp_path / "absent.csv")


def test_table_empty_file(tmp_path):
    with pytest.raises(ValueError, match="is empty"):
        read_table(write(tmp_path, ""))


def test_table_header_only(tmp_path):
    with pytest.raises(ValueError, match="no rows"):
        read_table(write(tmp_path, "geoid20,total\n"), key="geoid20")


def test_table_malformed_row(tmp_path):
    with pytest.raises(ValueError, match="could not be read"):
        read_table(write(tmp_path, "geoid20,total\n25025000100,10\n25025000200,20,extra\n"), key="geoid20")


def test_table_missing_key_column(tmp_path):
    with pytest.raises(ValueError, match="missing columns"):
        read_table(write(tmp_path, "tract,total\n1,10\n"), key="geoid20")


def test_table_blank_export_row_is_dropped(tmp_path):
    body = "geoid20,name,total,Shape_Length,Shape_Area\n25025000100,1,10,0.1,0.2\n,,,0,0\n"
    df, report = read_table(write(tmp_path, body), key="geoid20", text_columns=("name",))
    assert df["geoid20"].tolist() == ["25025000100"]
    assert report.dropped == {"blank_row": 1}


def test_table_numbers_parsed_and_text_kept(tmp_path):
    body = "geoid20,name,total\n02502500100,0100.00,10.5\n02502500200,0200.00,n/a\n"
    df, _ = read_table(write(tmp_path, body), key="geoid20", text_columns=("name",))
    assert df["geoid20"].tolist() == ["02502500100", "02502500200"]
    assert df["name"].tolist() == ["0100.00", "0200.00"]
    assert df["total"].iloc[0] == 10.5 and pd.isna(df["total"].iloc[1])


# Config and manifest

def test_config_missing_column(tmp_path):
    with pytest.raises(ValueError, match="missing columns"):
        read_config(write(tmp_path, "name,kind,file\nx,vector,x.geojson\n", "config.csv"))


def test_config_unknown_kind(tmp_path):
    body = CONFIG_HEADER + "x,raster,x.tif,https://example.org/x,pub,2020,PDDL,,\n"
    with pytest.raises(ValueError, match="unknown layer kind"):
        read_config(write(tmp_path, body, "config.csv"))


def test_config_duplicate_name(tmp_path):
    row = "x,vector,x.geojson,https://example.org/x,pub,2020,PDDL,,\n"
    with pytest.raises(ValueError, match="duplicate layer names"):
        read_config(write(tmp_path, CONFIG_HEADER + row + row, "config.csv"))


def test_manifest_records_source_vintage_and_crs(tmp_path):
    raw, out = tmp_path / "raw", tmp_path / "out"
    raw.mkdir()
    write_geojson(raw, [feature(SQUARE_3857, id="a")], crs="EPSG:3857", name="flood.geojson")
    body = CONFIG_HEADER + "flood,vector,flood.geojson,https://example.org/flood,City,2016 scenario,PDDL,id,\n"
    layer = read_config(write(tmp_path, body, "config.csv")).iloc[0]

    report = load_layer(layer, raw, out)
    row = manifest_row(layer, report, raw)

    assert row["source_url"] == "https://example.org/flood"
    assert row["vintage"] == "2016 scenario"
    assert row["source_crs"] == "EPSG:3857" and row["stored_crs"] == "EPSG:4326"
    assert row["rows"] == 1 and len(row["sha256"]) == 64
    assert gpd.read_parquet(out / "flood.parquet").crs.to_string() == "EPSG:4326"


def test_redirect_drops_default_https_port():
    req = urllib.request.Request("https://data.boston.gov/x")
    new = DefaultPortRedirect().redirect_request(req, None, 302, "Found", {}, "https://s3.amazonaws.com:443/bucket/f.xlsx?X-Amz-Signature=a%2Fb")
    assert new.full_url == "https://s3.amazonaws.com/bucket/f.xlsx?X-Amz-Signature=a%2Fb"
    other = DefaultPortRedirect().redirect_request(req, None, 302, "Found", {}, "https://example.org:8443/f")
    assert other.full_url == "https://example.org:8443/f"


class LayerServer(BaseHTTPRequestHandler):
    """A local stand-in for a layer's download address (loopback is allowed in tests)."""

    status, body, agents = 200, b"name,value\nA,1\n", []

    def do_GET(self):
        LayerServer.agents.append(self.headers["User-Agent"])
        self.send_response(self.status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


def serve_layer(status):
    LayerServer.status, LayerServer.agents = status, []
    server = HTTPServer(("127.0.0.1", 0), LayerServer)
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/population.csv"


def test_download_writes_the_file_through_a_part_file(tmp_path):
    # The real download path of `ingest_static_layers --download`.
    server, url = serve_layer(200)
    dest = tmp_path / "raw" / "population.csv"
    try:
        download(url, dest)
    finally:
        server.server_close()
    assert dest.read_bytes() == LayerServer.body
    assert sorted(p.name for p in dest.parent.iterdir()) == ["population.csv"]  # no .part file left
    assert LayerServer.agents == [USER_AGENT]


def test_a_failed_download_leaves_no_file(tmp_path):
    server, url = serve_layer(404)
    dest = tmp_path / "raw" / "population.csv"
    try:
        with pytest.raises(urllib.error.HTTPError):
            download(url, dest)
    finally:
        server.server_close()
    assert list(dest.parent.iterdir()) == []


def test_a_document_layer_is_only_checked(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "notes.pdf").write_bytes(b"%PDF-1.4")
    layer = pd.Series({"name": "flood_notes", "kind": "document", "file": "notes.pdf", "key": "", "text_columns": ""})
    report = load_layer(layer, raw, tmp_path / "out")
    assert report.name == "flood_notes" and report.rows == 0
    assert not (tmp_path / "out").exists()  # nothing written for a document
    with pytest.raises(FileNotFoundError):
        load_layer(layer.copy().replace({"notes.pdf": "missing.pdf"}), raw, tmp_path / "out")


def test_vector_with_no_geometry_left_is_rejected(tmp_path):
    path = write_geojson(tmp_path, [{"type": "Feature", "properties": {"id": 1}, "geometry": None}])
    with pytest.raises(ValueError, match="has no features with geometry"):
        read_vector(path)
