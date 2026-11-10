"""Tests for the candidates' parameters file (#107)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402,F401  (puts Model-Development/scripts on sys.path)
import model_params as M  # noqa: E402
import training_data as D  # noqa: E402

GOOD = "logreg:\n  C_grid: [0.01, 0.1, 1.0]\n  max_iter: 500\n  year_parts: 12\n"


def test_the_committed_parameters_load():
    params = M.load(D.load_settings().model_params)
    assert set(params) == {"logreg"}
    logreg = params["logreg"]
    assert logreg["C_grid"][0] == 0.001 and logreg["C_grid"][-1] == 100.0 and len(logreg["C_grid"]) == 11
    assert (logreg["max_iter"], logreg["year_parts"]) == (2000, 12)


def test_a_good_file_loads_with_its_values(tmp_path):
    path = tmp_path / "params.yaml"
    path.write_text(GOOD, encoding="utf-8")
    assert M.load(path) == {"logreg": {"C_grid": (0.01, 0.1, 1.0), "max_iter": 500, "year_parts": 12}}


@pytest.mark.parametrize("text, message", [
    ("", "must map each model to its parameters"),
    ("logreg: [\n", "is not valid YAML"),
    ("{}\n", r"has no parameters for \['logreg'\]"),
    (GOOD + "forest:\n  trees: 10\n", r"unknown model\(s\) in params.yaml: \['forest'\]"),
    ("logreg: [0.1]\n", "logreg in params.yaml must map each parameter"),
    (GOOD + "  penalty: l1\n", r"unknown parameter\(s\) for logreg in params.yaml: \['penalty'\]"),
    ("logreg:\n  C_grid: [0.1]\n  max_iter: 500\n", r"logreg in params.yaml is missing \['year_parts'\]"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[]"), "logreg.C_grid must be a list of one or more numbers"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "0.1"), "logreg.C_grid must be a list of one or more numbers"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[0.0, 1.0]"), "logreg.C_grid must hold numbers above 0"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[1.0, 0.1]"), "logreg.C_grid must increase with no value twice"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[0.1, 0.1]"), "logreg.C_grid must increase with no value twice"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[1e-3, 1.0]"), "write decimals with a point"),
    (GOOD.replace("[0.01, 0.1, 1.0]", "[.nan]"), "logreg.C_grid must hold numbers"),
    (GOOD.replace("500", "0"), "logreg.max_iter must be a whole number of 1 or more"),
    (GOOD.replace("500", "true"), "logreg.max_iter must be a whole number of 1 or more"),
    (GOOD.replace("500", "2.5"), "logreg.max_iter must be a whole number of 1 or more"),
    (GOOD.replace("year_parts: 12", "year_parts: 1"), "logreg.year_parts must be a whole number of 2 or more"),
])
def test_a_bad_file_is_refused_naming_the_problem(tmp_path, text, message):
    path = tmp_path / "params.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(M.ParamsError, match=message):
        M.load(path)
