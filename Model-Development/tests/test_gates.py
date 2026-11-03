"""Tests for the promotion gates (#112, ADR-026).

The committed config stays untouched: every negative case writes a temp copy.
"""

from __future__ import annotations

import csv
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "Model-Development" / "scripts"
CONFIG = ROOT / "Model-Development" / "config"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gates"

sys.path.insert(0, str(SCRIPTS))
import gates as G  # noqa: E402

COMMITTED_GATES = CONFIG / "gates.csv"
COMMITTED_GROUPS = CONFIG / "poverty_groups.csv"


# --- helpers ---------------------------------------------------------------


def base_values() -> dict[str, str]:
    """The committed gate settings as a name -> value mapping."""
    rows = list(csv.reader(COMMITTED_GATES.read_text().splitlines()))
    return {row[0]: row[1] for row in rows[1:] if row}


def committed_group_rows() -> list[tuple[str, str, str]]:
    rows = list(csv.reader(COMMITTED_GROUPS.read_text().splitlines()))
    return [(r[0], r[1], r[2]) for r in rows[1:] if r]


def write_groups(path: Path, rows: list[tuple[str, str, str]]) -> str:
    lines = ["neighborhood,poverty_share,group"]
    lines += [f"{hood},{share},{group}" for hood, share, group in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_gates(path: Path, values: dict[str, str]) -> Path:
    lines = ["key,value,description"]
    for name in G.GATE_ORDER:
        lines.append(f"{name},{values[name]},description of {name}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_config(
    tmp_path: Path,
    *,
    group_rows: list[tuple[str, str, str]] | None = None,
    overrides: dict[str, str] | None = None,
    groups_digest: str | None = None,
    groups_value: str | None = None,
    drop: str | None = None,
    extra: tuple[str, str] | None = None,
    duplicate: str | None = None,
) -> Path:
    """Write a temp gates.csv (and its groups file) from the committed defaults."""
    rows = group_rows if group_rows is not None else committed_group_rows()
    groups_path = tmp_path / "poverty_groups.csv"
    digest = write_groups(groups_path, rows)
    values = base_values()
    values["fairness_group_source"] = str(groups_path)
    values["fairness_group_sha256"] = groups_digest if groups_digest is not None else digest
    if groups_value is not None:
        values["fairness_groups"] = groups_value
    if overrides:
        values.update(overrides)

    gates_path = tmp_path / "gates.csv"
    order = [name for name in G.GATE_ORDER if name != drop]
    lines = ["key,value,description"]
    for name in order:
        lines.append(f"{name},{values[name]},description of {name}")
    if duplicate is not None:
        lines.append(f"{duplicate},{values[duplicate]},a second row")
    if extra is not None:
        lines.append(f"{extra[0]},{extra[1]},an unconfigured row")
    gates_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return gates_path


def metrics(**over) -> dict:
    """A both-gates-pass metrics shape (20 positives, no champion) with overrides."""
    base = {
        "data_version": "v1",
        "candidate": {"tp": 18, "fp": 5, "fn": 2},
        "baseline": {"tp": 6, "fp": 4, "fn": 14},
        "champion": None,
        "groups": {
            "lower": {"positives": 10, "true_positives": 9},
            "higher": {"positives": 10, "true_positives": 9},
        },
        "excluded": {},
    }
    base.update(over)
    return base


@pytest.fixture(scope="module")
def committed() -> G.Gates:
    return G.load_gates(COMMITTED_GATES)


# --- 1. both pass, no champion --------------------------------------------


def test_both_gates_pass_no_champion_promotes(committed):
    report = G.evaluate(committed, metrics())
    assert report["decision"] == "promote"
    assert report["f1_gate"]["pass"] and report["fairness_gate"]["pass"]


# --- 2 and 3. F1 margin boundary (Fraction, not float) --------------------


def test_f1_margin_exactly_threshold_passes(committed):
    report = G.evaluate(
        committed,
        metrics(
            candidate={"tp": 9, "fp": 11, "fn": 11},  # F1 0.45
            baseline={"tp": 6, "fp": 4, "fn": 14},  # F1 0.40, margin exactly 0.05
            groups={
                "lower": {"positives": 10, "true_positives": 8},
                "higher": {"positives": 10, "true_positives": 8},
            },
        ),
    )
    assert report["f1_gate"]["pass"] is True
    assert report["decision"] == "promote"


def test_f1_margin_just_under_threshold_blocked(committed):
    report = G.evaluate(
        committed,
        metrics(
            candidate={"tp": 9, "fp": 11, "fn": 11},  # F1 0.45
            baseline={"tp": 7, "fp": 5, "fn": 13},  # F1 0.4375, margin 0.0125
            groups={
                "lower": {"positives": 10, "true_positives": 8},
                "higher": {"positives": 10, "true_positives": 8},
            },
        ),
    )
    assert report["f1_gate"]["pass"] is False
    assert report["decision"] == "blocked"


# --- 4 and 5. Fairness gap boundary ---------------------------------------


def test_gap_exactly_threshold_passes(committed):
    report = G.evaluate(
        committed,
        metrics(
            candidate={"tp": 36, "fp": 2, "fn": 4},  # 40 positives
            baseline={"tp": 12, "fp": 8, "fn": 28},  # 40 positives, F1 0.40
            groups={
                "lower": {"positives": 20, "true_positives": 17},  # recall 0.85
                "higher": {"positives": 20, "true_positives": 14},  # recall 0.70
            },
        ),
    )
    assert report["fairness_gate"]["pass"] is True
    assert report["fairness_gate"]["gap"] == 0.15
    assert report["decision"] == "promote"


def test_gap_over_threshold_blocked_names_best_and_worst(committed):
    report = G.evaluate(
        committed,
        metrics(
            candidate={"tp": 36, "fp": 2, "fn": 4},
            baseline={"tp": 12, "fp": 8, "fn": 28},
            groups={
                "lower": {"positives": 20, "true_positives": 20},  # recall 1.00
                "higher": {"positives": 20, "true_positives": 13},  # recall 0.65
            },
        ),
    )
    assert report["decision"] == "blocked"
    assert report["fairness_gate"]["pass"] is False
    assert report["best_group"] == "lower"
    assert report["worst_group"] == "higher"


# --- 6. committed config, one small group leaves one group ----------------


def test_committed_one_small_group_leaves_one_error(committed):
    report = G.evaluate(
        committed,
        metrics(
            groups={
                "lower": {"positives": 16, "true_positives": 15},
                "higher": {"positives": 4, "true_positives": 3},
            },
        ),
    )
    assert report["decision"] == "error"
    assert "evaluable" in report["reason"]


# --- 7. pooling on a synthetic 3-group config -----------------------------

THREE = [
    ("A", "0.10", "a"),
    ("B", "0.20", "b"),
    ("C", "0.30", "c"),
    ("Island", "", "ungrouped"),
]


def three_group_gates(tmp_path):
    return G.load_gates(make_config(tmp_path, group_rows=THREE, groups_value="a;b;c"))


def test_pool_one_small_group_excluded_two_remain(tmp_path):
    gates = three_group_gates(tmp_path)
    report = G.evaluate(
        gates,
        {
            "data_version": "v1",
            "candidate": {"tp": 40, "fp": 0, "fn": 4},  # 44 positives
            "baseline": {"tp": 22, "fp": 0, "fn": 22},
            "champion": None,
            "groups": {
                "a": {"positives": 4, "true_positives": 3},
                "b": {"positives": 20, "true_positives": 18},
                "c": {"positives": 20, "true_positives": 18},
            },
            "excluded": {},
        },
    )
    assert report["decision"] != "error"
    assert len(report["groups"]) == 2
    assert report["pooled"]["status"] == "excluded"
    assert report["pooled"]["members"] == ["a"]


def test_pool_two_small_groups_pooled_two_remain(tmp_path):
    gates = three_group_gates(tmp_path)
    report = G.evaluate(
        gates,
        {
            "data_version": "v1",
            "candidate": {"tp": 24, "fp": 0, "fn": 2},  # 26 positives
            "baseline": {"tp": 13, "fp": 0, "fn": 13},
            "champion": None,
            "groups": {
                "a": {"positives": 3, "true_positives": 2},
                "b": {"positives": 3, "true_positives": 2},
                "c": {"positives": 20, "true_positives": 18},
            },
            "excluded": {},
        },
    )
    assert report["decision"] != "error"
    assert report["pooled"]["status"] == "evaluated"
    assert report["pooled"]["members"] == ["a", "b"]
    assert len(report["groups"]) == 2


def test_pool_two_small_groups_pool_too_small_error(tmp_path):
    gates = three_group_gates(tmp_path)
    report = G.evaluate(
        gates,
        {
            "data_version": "v1",
            "candidate": {"tp": 20, "fp": 0, "fn": 4},  # 24 positives
            "baseline": {"tp": 12, "fp": 0, "fn": 12},
            "champion": None,
            "groups": {
                "a": {"positives": 2, "true_positives": 1},
                "b": {"positives": 2, "true_positives": 1},
                "c": {"positives": 20, "true_positives": 18},
            },
            "excluded": {},
        },
    )
    assert report["decision"] == "error"


# --- 8. ungrouped entries reported, never pooled --------------------------


def test_ungrouped_reported_never_pooled(committed):
    report = G.evaluate(
        committed,
        metrics(
            groups={
                "lower": {"positives": 10, "true_positives": 9},
                "higher": {"positives": 9, "true_positives": 8},
            },
            excluded={"Harbor Islands": {"positives": 1, "reason": "ungrouped"}},
        ),
    )
    assert report["decision"] == "promote"
    assert report["pooled"] is None
    names = [row.get("neighborhood") for row in report["excluded"]]
    assert "Harbor Islands" in names


# --- 9. champion cases -----------------------------------------------------


def test_champion_tie_keeps_champion(committed):
    report = G.evaluate(
        committed,
        metrics(champion={"tp": 18, "fp": 5, "fn": 2, "data_version": "v1", "model_version": "m1"}),
    )
    assert report["decision"] == "keep_champion"


def test_champion_lower_f1_promotes(committed):
    report = G.evaluate(
        committed,
        metrics(champion={"tp": 10, "fp": 5, "fn": 10, "data_version": "v1", "model_version": "m1"}),
    )
    assert report["decision"] == "promote"


def test_champion_different_data_version_errors(committed):
    report = G.evaluate(
        committed,
        metrics(champion={"tp": 18, "fp": 5, "fn": 2, "data_version": "v0", "model_version": "m1"}),
    )
    assert report["decision"] == "error"
    assert "data_version" in report["reason"]


# --- 10. every input rule gives error with a reason -----------------------

INPUT_ERRORS = {
    "negative_count": metrics(candidate={"tp": -1, "fp": 5, "fn": 2}),
    "float_count": metrics(candidate={"tp": 18.0, "fp": 5, "fn": 2}),
    "bool_count": metrics(candidate={"tp": True, "fp": 5, "fn": 2}),
    "none_count": metrics(candidate={"tp": None, "fp": 5, "fn": 2}),
    "nan_count": metrics(candidate={"tp": float("nan"), "fp": 5, "fn": 2}),
    "positives_mismatch": metrics(baseline={"tp": 6, "fp": 4, "fn": 10}),
    "group_sum_mismatch": metrics(
        groups={
            "lower": {"positives": 10, "true_positives": 9},
            "higher": {"positives": 9, "true_positives": 8},
        }
    ),
    "missing_group": metrics(groups={"lower": {"positives": 20, "true_positives": 18}}),
    "unconfigured_group": metrics(
        groups={
            "lower": {"positives": 10, "true_positives": 9},
            "middle": {"positives": 10, "true_positives": 9},
        }
    ),
    "true_positives_over_positives": metrics(
        groups={
            "lower": {"positives": 10, "true_positives": 11},
            "higher": {"positives": 10, "true_positives": 9},
        }
    ),
    "f1_denominator_zero": metrics(
        candidate={"tp": 0, "fp": 0, "fn": 0},
        baseline={"tp": 0, "fp": 0, "fn": 0},
        groups={
            "lower": {"positives": 0, "true_positives": 0},
            "higher": {"positives": 0, "true_positives": 0},
        },
    ),
}


@pytest.mark.parametrize("case", sorted(INPUT_ERRORS))
def test_input_rule_errors_with_reason(committed, case):
    report = G.evaluate(committed, INPUT_ERRORS[case])
    assert report["decision"] == "error"
    assert report["reason"]


# --- 11. config and group-file errors name the problem --------------------


def test_config_missing_setting(tmp_path):
    path = make_config(tmp_path, drop="on_undefined")
    with pytest.raises(G.ConfigError, match="on_undefined"):
        G.load_gates(path)


def test_config_unknown_setting(tmp_path):
    path = make_config(tmp_path, extra=("f1_extra", "1"))
    with pytest.raises(G.ConfigError, match="unknown"):
        G.load_gates(path)


def test_config_duplicate_setting(tmp_path):
    path = make_config(tmp_path, duplicate="f1_split")
    with pytest.raises(G.ConfigError, match="f1_split"):
        G.load_gates(path)


def test_config_bad_value(tmp_path):
    path = make_config(tmp_path, overrides={"f1_min_margin": "2.0"})
    with pytest.raises(G.ConfigError, match="f1_min_margin"):
        G.load_gates(path)


def test_config_header_must_be_fixed(tmp_path):
    # A gates file with any other header (here processing.csv's) is rejected.
    path = make_config(tmp_path)
    body = path.read_text().splitlines()[1:]
    path.write_text("setting,value,options,note\n" + "\n".join(body) + "\n")
    with pytest.raises(G.ConfigError, match="header"):
        G.load_gates(path)


def test_groups_file_hash_mismatch(tmp_path):
    path = make_config(tmp_path, groups_digest="0" * 64)
    with pytest.raises(G.ConfigError, match="fairness_group_sha256"):
        G.load_gates(path)


def test_groups_file_unconfigured_label(tmp_path):
    rows = committed_group_rows()
    rows[0] = (rows[0][0], rows[0][1], "middle")
    path = make_config(tmp_path, group_rows=rows)
    with pytest.raises(G.ConfigError, match="labels"):
        G.load_gates(path)


def test_groups_file_breaks_median_rule(tmp_path):
    rows = committed_group_rows()
    # Flip the lowest-share neighborhood (South Boston Waterfront, 0.027486) to higher.
    rows = [(h, s, "higher" if h == "South Boston Waterfront" else g) for h, s, g in rows]
    path = make_config(tmp_path, group_rows=rows)
    with pytest.raises(G.ConfigError, match="median rule"):
        G.load_gates(path)


# --- 12. pin the frozen values --------------------------------------------


def test_frozen_values_are_pinned():
    note = "changing a frozen gate needs a superseding ADR (ADR-026)"
    values = base_values()
    assert values["f1_min_margin"] == "0.05", note
    assert values["fairness_max_gap"] == "0.15", note
    assert values["fairness_min_group_surges"] == "5", note
    assert values["champion_min_delta"] == "0", note
    assert values["fairness_groups"] == "lower;higher", note
    groups = [g for _, _, g in committed_group_rows()]
    assert groups.count("higher") == 11, note
    assert groups.count("lower") == 12, note
    assert groups.count("ungrouped") == 1, note


# --- 13. neighborhood coverage --------------------------------------------


def test_groups_file_neighborhoods_unique_and_count_24():
    # No committed, test-reachable file enumerates the ADR-004 neighborhoods
    # (they live in the DVC-tracked neighborhoods_by_tract20.parquet), so this
    # checks uniqueness and the count of 24 only.
    hoods = [h for h, _, _ in committed_group_rows()]
    assert len(hoods) == 24
    assert len(set(hoods)) == 24


# --- 14. CLI fixtures ------------------------------------------------------


@pytest.mark.parametrize(
    "name,code", [("pass", 0), ("fail_f1", 1), ("fail_gap", 1), ("error_input", 2)]
)
def test_cli_fixture_exit_codes(name, code):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "gates.py"), "--metrics", str(FIXTURES / f"{name}.json")],
        capture_output=True,
    )
    assert result.returncode == code


def test_cli_validate_config_reports_counts_and_hashes():
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "gates.py"), "--validate-config"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "18" in result.stdout
    assert G.load_gates(COMMITTED_GATES).gates_sha256 in result.stdout


# --- 15. non-gate failures exit 2, not 1 ----------------------------------


def _run_metrics(path):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "gates.py"), "--metrics", str(path)],
        capture_output=True,
    ).returncode


def test_cli_missing_metrics_file_exits_2(tmp_path):
    assert _run_metrics(tmp_path / "does_not_exist.json") == 2


def test_cli_bad_json_exits_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert _run_metrics(bad) == 2


def test_cli_json_list_exits_2(tmp_path):
    lst = tmp_path / "list.json"
    lst.write_text("[1, 2, 3]", encoding="utf-8")
    assert _run_metrics(lst) == 2


def test_metrics_not_a_dict_is_error(committed):
    report = G.evaluate(committed, [1, 2, 3])
    assert report["decision"] == "error"


def test_missing_data_version_is_error(committed):
    payload = metrics()
    payload.pop("data_version")
    assert G.evaluate(committed, payload)["decision"] == "error"


def test_fairness_min_groups_below_two_fails(tmp_path):
    path = make_config(tmp_path, overrides={"fairness_min_groups": "1"})
    with pytest.raises(G.ConfigError, match="fairness_min_groups"):
        G.load_gates(path)
