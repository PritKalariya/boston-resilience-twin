#!/usr/bin/env python3
"""Check the running service against the training code, to 1e-9 (#161).

Scores a /predict request two ways and compares per unit: here, the training way (the request rows cast to
the Arrow types of the training features file, then the saved model's predict_proba, as
Model-Development/scripts/steps/step_reload_check.py scores it), and by POSTing the same request to the
service. It shares no code with the service's frame builder. PASS exits 0, FAIL exits 1.

Run it in the training image, which has the model's libraries (from the repository root, with the model
folder made by Serving/scripts/local_model.py and the service up on 127.0.0.1:8081):

  docker run --rm --network host --entrypoint python \\
    -v "$PWD/Serving/scripts:/parity:ro" -v "$HOME/brt-serve-model:/model:ro" \\
    -v "$PWD/docs/contracts/fixtures/predict:/request:ro" \\
    -v "$PWD/Data-Pipeline/data/processed/features.parquet:/features.parquet:ro" \\
    brt-train:local /parity/parity.py --request /request/request.json --model-dir /model \\
    --url http://127.0.0.1:8081

For a Cloud Run service that is not public add --token "$(gcloud auth print-identity-token)" and use its
https URL (no --network host needed).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TOLERANCE = 1e-9
KEYS = ("unit", "forecast_date", "as_of")


def reference(request: dict, model_dir: str, features_path: str) -> dict[str, float]:
    """unit -> probability, scored the training way."""
    import joblib
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    model = joblib.load(f"{model_dir}/model.joblib")
    columns = [str(name) for name in model.feature_names_in_]
    schema = pq.read_schema(features_path)
    rows = request["rows"]
    table = pa.table({name: pa.array([row[name] for row in rows]).cast(schema.field(name).type)
                      for name in columns})
    frame = table.to_pandas()[columns]
    probability = np.asarray(model.predict_proba(frame), dtype="float64")[:, 1]
    return {row["unit"]: float(p) for row, p in zip(rows, probability, strict=True)}


def served(request: dict, url: str, token: str | None) -> dict[str, float]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    post = urllib.request.Request(url.rstrip("/") + "/predict", data=json.dumps(request).encode("utf-8"),
                                  headers=headers, method="POST")
    try:
        with urllib.request.urlopen(post, timeout=60) as response:
            if response.status != 200:  # urlopen raises only on 4xx and 5xx; a 204 or a 3xx body is not an answer
                sys.exit(f"FAIL: /predict answered {response.status}, not 200")
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        sys.exit(f"FAIL: /predict answered {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}")
    except urllib.error.URLError as exc:
        sys.exit(f"FAIL: cannot reach {url}: {exc.reason}")
    sent = [row["unit"] for row in request["rows"]]
    got = [item["unit"] for item in body["predictions"]]
    if got != sent:
        sys.exit(f"FAIL: the response's units {got} are not the request's {sent}, in order")
    return {item["unit"]: item["probability"] for item in body["predictions"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--request", required=True, help="a predict/v1 request file")
    parser.add_argument("--model-dir", required=True, help="the folder holding model.joblib")
    parser.add_argument("--url", required=True, help="the service's base URL")
    parser.add_argument("--token", help="an identity token, sent as a bearer token")
    parser.add_argument("--features", default="/features.parquet",
                        help="the training features file, for its Arrow types (default /features.parquet)")
    args = parser.parse_args(argv)
    with open(args.request, encoding="utf-8") as stream:
        request = json.load(stream)
    expected = reference(request, args.model_dir, args.features)
    actual = served(request, args.url, args.token)
    worst = 0.0
    for unit, want in expected.items():
        difference = abs(want - actual[unit])
        worst = max(worst, difference) if difference == difference else float("inf")
        print(f"{unit:<26} training {want:.17g}  service {actual[unit]:.17g}  difference {difference:.3g}")
    passed = worst <= TOLERANCE
    print(f"max absolute difference {worst:.3g} over {len(expected)} units (tolerance {TOLERANCE:g}): "
          f"{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
