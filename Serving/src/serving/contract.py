"""The /predict contract as the service enforces it (docs/contracts/predict.md).

The JSON Schemas are the request validator, so there is no second copy of the contract in code. What a
schema cannot say (unique units, one forecast_date and as_of, forecast_date = as_of + 1 day) is checked
here, and so is the match between a request's inputs and the served model's.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROW_KEYS = ("unit", "forecast_date", "as_of")


def pointer(parts) -> str:
    """A JSON pointer such as /rows/3/month; empty for the document itself."""
    return "".join(f"/{part}" for part in parts)


class Contract:
    """The request validator, the response validator and what the request schema says about each input."""

    def __init__(self, schemas_dir: Path):
        schemas_dir = Path(schemas_dir)
        request = json.loads((schemas_dir / "predict-request.schema.json").read_text(encoding="utf-8"))
        response = json.loads((schemas_dir / "predict-response.schema.json").read_text(encoding="utf-8"))
        checker = FormatChecker()
        self.request_validator = Draft202012Validator(request, format_checker=checker)
        self.response_validator = Draft202012Validator(response, format_checker=checker)
        self.model_version_pattern = response["$defs"]["model_version"]["pattern"]
        self.run_id_pattern = response["$defs"]["run_id"]["pattern"]
        self.features: tuple[str, ...] = tuple(request["$defs"]["feature"]["enum"])
        properties = request["$defs"]["row"]["properties"]
        # An input the schema types as an integer is int64 in the frame; every other input is float64,
        # as in the forecast rows file (predict.md, "The forecast rows file").
        self.dtypes = {name: ("int64" if properties[name]["type"] == "integer" else "float64")
                       for name in self.features}

    def schema_errors(self, body) -> list[dict]:
        """One {path, message} per way body breaks the request schema, in path order."""
        errors = sorted(self.request_validator.iter_errors(body), key=lambda e: [str(p) for p in e.absolute_path])
        return [{"path": pointer(e.absolute_path), "message": e.message} for e in errors]


def cross_field_errors(body: dict) -> list[dict]:
    """The rules of predict.md that a schema cannot say. body has already passed the schema."""
    rows = body["rows"]
    errors = []
    seen: dict[str, int] = {}
    for index, row in enumerate(rows):
        if row["unit"] in seen:
            errors.append({"path": f"/rows/{index}/unit",
                           "message": f"unit {row['unit']!r} is repeated (first at row {seen[row['unit']]})"})
        seen.setdefault(row["unit"], index)
    for key in ("forecast_date", "as_of"):
        values = {row[key] for row in rows}
        if len(values) > 1:
            errors.append({"path": "/rows", "message": f"every row must have the same {key}, got {sorted(values)}"})
    if not errors:
        forecast_date, as_of = date.fromisoformat(rows[0]["forecast_date"]), date.fromisoformat(rows[0]["as_of"])
        if forecast_date != as_of + timedelta(days=1):
            errors.append({"path": "/rows/0/forecast_date",
                           "message": f"forecast_date {forecast_date} must be as_of {as_of} plus one day"})
    return errors


def feature_set_errors(rows: list[dict], features) -> list[dict]:
    """Each row's inputs must be exactly the served model's features, as a set."""
    wanted = set(features)
    errors = []
    for index, row in enumerate(rows):
        have = set(row) - set(ROW_KEYS)
        for name in sorted(wanted - have):
            errors.append({"path": f"/rows/{index}/{name}", "message": f"the served model needs input {name!r}"})
        for name in sorted(have - wanted):
            errors.append({"path": f"/rows/{index}/{name}", "message": f"the served model does not take input {name!r}"})
    return errors
