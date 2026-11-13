import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "steps"))

import bias  # noqa: E402
import gates  # noqa: E402
import predictions  # noqa: E402
import step_evaluate as s_evaluate  # noqa: E402

LOADED = gates.load_gates()
GROUPS = bias.load_groups(LOADED)
POVERTY = GROUPS["poverty"].to_dict()
HOODS = sorted(GROUPS.index)
SPLITS = {"train": (pd.Timestamp("2016-01-01"), pd.Timestamp("2023-12-31")),
          "validation": (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31")),
          "test": (pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31"))}


def rows_for(recall_of, surges=20, days=60, split="validation", start="2024-01-01"):
    """Predictions for every neighborhood: surges surge days each (none in Harbor Islands), of which a share
    recall_of(neighborhood) is caught, plus a false alarm on every tenth quiet day."""
    out = []
    dates = pd.date_range(start, periods=days)
    for hood in HOODS:
        n = 0 if POVERTY[hood] == gates.UNGROUPED else surges
        caught = int(round(recall_of(hood) * n))
        for i, day in enumerate(dates):
            label = int(i < n)
            prediction = int(i < caught) if label else int(i % 10 == 0)
            out.append({"unit": hood, "date": day, "split": split, "label": label, "prediction": prediction})
    return pd.DataFrame(out)


def by_group(higher, lower):
    return lambda hood: higher if POVERTY[hood] == "higher" else lower


# Groups


def test_the_groups_cover_the_same_neighborhoods_as_the_gate():
    assert set(GROUPS.columns) == {"poverty", "poc", "limited_english", "flood_zone", "student_area"}
    assert len(GROUPS) == 24 and GROUPS.loc["Harbor Islands", "poverty"] == gates.UNGROUPED
    assert sorted(GROUPS.index[GROUPS["student_area"] == bias.STUDENT]) == [
        "Allston", "Downtown", "Fenway", "Longwood", "Mission Hill"]  # #32's rule, not a hand-named list


@pytest.mark.parametrize("edit, message", [
    (lambda t: t.replace("neighborhood,poc", "hood,poc"), "needs the header"),
    (lambda t: t.replace("Allston,higher", "Allston,"), "blank cell"),
    (lambda t: t + t.splitlines()[1] + "\n", "repeats a neighborhood"),
    (lambda t: "\n".join(line for line in t.splitlines() if not line.startswith("Allston")) + "\n", "different neighborhoods"),
])
def test_a_bad_groups_file_is_rejected(tmp_path, edit, message):
    path = tmp_path / "slice_groups.csv"
    path.write_text(edit(bias.SLICE_GROUPS.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(bias.BiasError, match=message):
        bias.load_groups(LOADED, path)


def test_a_missing_groups_file_is_named(tmp_path):
    with pytest.raises(bias.BiasError, match="cannot read"):
        bias.load_groups(LOADED, tmp_path / "none.csv")


# The gate


def test_the_counts_reconcile_and_take_the_gate_shape():
    rows = rows_for(by_group(0.5, 0.5))
    groups, excluded = bias.gate_counts(rows, POVERTY, LOADED.fairness_groups)
    assert set(groups) == set(LOADED.fairness_groups) and list(excluded) == ["Harbor Islands"]
    bias.check_reconciliation(groups, excluded, bias.confusion(rows))
    assert groups["higher"] == {"positives": 11 * 20, "true_positives": 11 * 10}


def test_bias_check_passes_a_small_gap_and_fails_one_over_the_limit():
    close = bias.bias_check(rows_for(by_group(0.40, 0.45)), LOADED, POVERTY, "sha256:x")
    assert close["pass"] and close["gap"] == pytest.approx(0.05) and close["worst_group"] == "higher"
    wide = bias.bias_check(rows_for(by_group(0.20, 0.45)), LOADED, POVERTY, "sha256:x")
    assert not wide["pass"] and wide["gap"] == pytest.approx(0.25) and wide["threshold"] == pytest.approx(0.15)
    assert wide["counts"]["groups"]["higher"]["true_positives"] == 11 * 4


def test_bias_check_takes_the_limit_from_gates_csv_inclusively():
    # 0.15 exactly passes, as gates.py's Fraction comparison does
    check = bias.bias_check(rows_for(by_group(0.30, 0.45)), LOADED, POVERTY, "sha256:x")
    assert check["gap"] == pytest.approx(0.15) and check["pass"]


def test_bias_check_reads_validation_rows_only_and_fails_without_them():
    rows = pd.concat([rows_for(by_group(0.4, 0.45)), rows_for(by_group(0.0, 1.0), split="test", start="2025-01-01")])
    assert bias.bias_check(rows, LOADED, POVERTY, "sha256:x")["pass"]  # the test rows would fail it
    with pytest.raises(bias.BiasError, match="no validation rows"):
        bias.bias_check(rows[rows["split"] == "test"], LOADED, POVERTY, "sha256:x")


def test_a_gate_that_cannot_be_evaluated_raises():
    rows = rows_for(by_group(0.5, 0.5))
    rows.loc[rows["unit"].map(POVERTY) == "higher", "label"] = 0  # no higher surge: under the floor, pooled away
    with pytest.raises(bias.BiasError, match="cannot be evaluated"):
        bias.bias_check(rows, LOADED, POVERTY, "sha256:x")


# Reported slices


def test_small_groups_are_merged_as_the_gate_pools_them():
    group = pd.Series(["a", "a", "b", "c", gates.UNGROUPED])
    surge = pd.Series([3, 3, 1, 1, 9])
    assert bias.merged(group, surge, 5).tolist()[:4] == ["a", "a", "merged: b + c", "merged: b + c"]
    assert pd.isna(bias.merged(group, surge, 5).iloc[4])  # ungrouped is dropped


def test_slice_table_matches_a_hand_count():
    table = bias.slice_table(rows_for(by_group(0.25, 0.5)), GROUPS, LOADED.fairness_min_group_surges, ("validation",))
    higher = table[(table["family"] == "poverty") & (table["group"] == "higher")].iloc[0]
    assert (higher["surges"], higher["true_positives"]) == (220, 55) and higher["recall"] == pytest.approx(0.25)
    assert higher["precision"] == pytest.approx(55 / (55 + 11 * 4))  # 4 false alarms on 40 quiet days each
    assert set(table["family"]) == {*bias.FAMILIES, bias.NEIGHBORHOOD, bias.TWO_PARTS}
    two = table[table["family"] == bias.TWO_PARTS].set_index("group")
    assert two.loc["student areas", "surges"] == 5 * 20 and two.loc["other", "surges"] == 6 * 20
    assert "Harbor Islands" not in set(table.loc[table["family"] == bias.NEIGHBORHOOD, "group"])


def test_no_surge_gives_a_missing_recall_not_zero():
    rows = rows_for(by_group(0.5, 0.5))
    rows.loc[rows["unit"] == "Back Bay", "label"] = 0
    table = bias.slice_table(rows, GROUPS, 5, ("validation",))
    back_bay = table[(table["family"] == bias.NEIGHBORHOOD) & (table["group"] == "Back Bay")].iloc[0]
    assert back_bay["surges"] == 0 and np.isnan(back_bay["recall"])


def test_the_reopen_conditions_of_adr_026():
    weak = {"West End", "Fenway"}
    rows = rows_for(lambda hood: 0.05 if hood in weak else 0.45)
    table = bias.slice_table(rows, GROUPS, LOADED.fairness_min_group_surges, ("validation",))
    found = bias.triggers(table, GROUPS, LOADED)
    assert {item["neighborhood"] for item in found if item["condition"] == "neighborhood below its group"} == weak
    students = rows_for(lambda hood: 0.1 if GROUPS.loc[hood, "student_area"] == bias.STUDENT else 0.5)
    found = bias.triggers(bias.slice_table(students, GROUPS, 5, ("validation",)), GROUPS, LOADED)
    assert any(item["condition"] == "higher group's two parts differ" for item in found)
    level = bias.slice_table(rows_for(by_group(0.45, 0.45)), GROUPS, 5, ("validation",))
    assert bias.triggers(level, GROUPS, LOADED) == []


def test_no_reopen_condition_while_the_gate_fails():
    rows = rows_for(lambda hood: 0.05 if hood == "West End" else 0.45)
    table = bias.slice_table(rows, GROUPS, LOADED.fairness_min_group_surges, ("validation",))
    assert bias.triggers(table, GROUPS, LOADED, gate_passes=True)
    assert bias.triggers(table, GROUPS, LOADED, gate_passes=False) == []  # "while the gate passes" (ADR-026)


def test_the_reopen_line_counts_neighborhoods_not_conditions():
    found = [{"condition": "neighborhood below its group", "neighborhood": "West End"},
             {"condition": "neighborhood below its group", "neighborhood": "Fenway"}]
    assert bias.reopen_line(found) == ("ADR-026 neighborhood condition holds for West End, Fenway; "
                                       "two-part condition does not hold")
    assert bias.reopen_line([]) == "ADR-026 neighborhood condition holds for no neighborhood; two-part condition does not hold"


def test_a_neighborhood_under_the_floor_does_not_trigger():
    rows = rows_for(lambda hood: 0.0 if hood == "West End" else 0.5, surges=4)
    table = bias.slice_table(rows, GROUPS, 5, ("validation",))
    assert not any(item.get("neighborhood") == "West End" for item in bias.triggers(table, GROUPS, LOADED))


def features_for(per_resident):
    """Features with the raw lags at per_resident(neighborhood) requests per resident a day."""
    out = []
    for hood in HOODS:
        population = 10000.0 if POVERTY[hood] != gates.UNGROUPED else np.nan
        for day in pd.date_range("2016-01-01", "2025-12-31", freq="30D"):
            out.append({"unit": hood, "date": day, bias.POPULATION: population,
                        "snow_ice_last": per_resident(hood) * 10000, "tree_last": 0.0})
    return pd.DataFrame(out)


def test_the_reporting_bias_is_requests_per_1000_residents_a_year():
    features = features_for(lambda hood: 0.0001 if POVERTY[hood] == "higher" else 0.0002)
    found = {(r["split"], r["group"]): r["per_1000_a_year"] for r in bias.reporting_bias(features, GROUPS, SPLITS)}
    assert found[("train", "higher")] == pytest.approx(0.1 * 365.25)
    assert found[("train", "higher")] / found[("train", "lower")] == pytest.approx(0.5)
    with pytest.raises(bias.BiasError, match="missing columns"):
        bias.reporting_bias(features.drop(columns="tree_last"), GROUPS, SPLITS)


# A run folder


def run_folder(tmp_path, recall_higher=0.4, recall_lower=0.45):
    run = tmp_path / "run"
    run.mkdir()
    rows = pd.concat([rows_for(by_group(recall_higher, recall_lower)),
                      rows_for(by_group(0.7, 0.7), split="test", start="2025-01-01")], ignore_index=True)
    frame = predictions.make_predictions(rows, "lightgbm", rows["prediction"].astype(float), 0.5)
    predictions.write_predictions(frame, run / "predictions.parquet")
    (run / "run.json").write_text(json.dumps({"model": "lightgbm", "run_id": "ci-lightgbm", "threshold": 0.5,
                                              "data_version": "sha256:x", "mlflow": {"run_id": "abc"}}))
    features = tmp_path / "features.parquet"
    features_for(lambda hood: 0.0001).to_parquet(features)
    return run, features


def test_analyse_writes_the_four_files_and_keeps_only_the_asked_splits(tmp_path):
    run, features = run_folder(tmp_path)
    content, files = bias.analyse(run, features, SPLITS, tmp_path / "out", loaded=LOADED)
    assert [f.name for f in files] == ["bias.json", "bias_slices.csv", "bias_report.md", "bias_recall.png"]
    assert all(f.exists() and f.stat().st_size for f in files)
    assert content["gate"]["pass"] and set(pd.read_csv(files[1])["split"]) == {"validation", "test"}
    text = files[2].read_text()
    assert "Fairness gate (ADR-026):** pass" in text and "## Residual reporting bias" in text
    assert "higher over lower" in text
    only, files = bias.analyse(run, features, SPLITS, tmp_path / "val", loaded=LOADED, report_splits=("validation",))
    assert set(pd.read_csv(files[1])["split"]) == {"validation"} and only["splits"] == ["validation"]
    assert {r["split"] for r in only["reporting_bias"]} == {"train", "validation"}


def test_analyse_says_when_the_reporting_bias_cannot_be_measured(tmp_path):
    run, features = run_folder(tmp_path)
    pd.read_parquet(features).drop(columns=bias.POPULATION).to_parquet(features)
    content, files = bias.analyse(run, features, SPLITS, tmp_path / "out", loaded=LOADED)
    assert content["reporting_bias"] == [] and "Not available" in files[2].read_text()


def settings_for(features):
    return SimpleNamespace(features=features, splits=SPLITS, mlflow_experiment="x", mlflow_store=Path("unused"))


@pytest.mark.parametrize("recall_higher, code", [(0.40, 0), (0.10, 1)])
def test_main_exits_0_on_a_pass_and_1_on_a_fail(tmp_path, monkeypatch, capsys, recall_higher, code):
    import training_data

    run, features = run_folder(tmp_path, recall_higher)
    monkeypatch.setattr(training_data, "load_settings", lambda path: settings_for(features))
    assert bias.main(["--run", str(run), "--out-dir", str(tmp_path / "out"), "--no-mlflow"]) == code
    assert ("pass" if code == 0 else "FAIL") in capsys.readouterr().out


def test_main_exits_2_on_a_broken_run(tmp_path, monkeypatch, capsys):
    import training_data

    run, features = run_folder(tmp_path)
    (run / "run.json").write_text("{}")
    monkeypatch.setattr(training_data, "load_settings", lambda path: settings_for(features))
    assert bias.main(["--run", str(run), "--no-mlflow"]) == 2
    assert "error" in capsys.readouterr().err


def test_main_logs_the_files_to_the_runs_mlflow_run(tmp_path, monkeypatch):
    import training_data

    run, features = run_folder(tmp_path)
    logged = []
    monkeypatch.setattr(training_data, "load_settings", lambda path: settings_for(features))
    monkeypatch.setattr(bias, "log_files", lambda run_dir, files, settings: logged.append((run_dir, [f.name for f in files])))
    assert bias.main(["--run", str(run), "--out-dir", str(tmp_path / "out")]) == 0
    assert logged == [(run, ["bias.json", "bias_slices.csv", "bias_report.md", "bias_recall.png"])]


def test_log_files_logs_to_the_runs_own_mlflow_run(tmp_path):
    import mlflow

    import tracking

    run, features = run_folder(tmp_path)
    settings = SimpleNamespace(features=features, splits=SPLITS, mlflow_experiment="bias-test",
                               mlflow_store=tmp_path / "mlruns")
    tracking.setup(settings.mlflow_experiment, settings.mlflow_store)
    with mlflow.start_run() as started:
        pass
    data = json.loads((run / "run.json").read_text())
    data["mlflow"]["run_id"] = started.info.run_id
    (run / "run.json").write_text(json.dumps(data))
    _, files = bias.analyse(run, features, SPLITS, tmp_path / "out", loaded=LOADED)
    bias.log_files(run, list(reversed(files)), settings)  # found by name, not by position
    logged = mlflow.get_run(started.info.run_id)
    assert logged.data.tags["bias_gate"] == "pass" and logged.data.tags["bias_reopen_two_parts"] == "does not hold"
    assert logged.data.metrics["validation_worst_group_recall_gap"] == pytest.approx(0.05)
    names = {item.path for item in mlflow.MlflowClient().list_artifacts(started.info.run_id, "bias")}
    assert names == {f"bias/{f.name}" for f in files}
    with pytest.raises(bias.BiasError, match="no bias.json"):
        bias.log_files(run, files[1:], settings)


def test_analyse_reuses_a_check_it_is_given(tmp_path, monkeypatch):
    run, features = run_folder(tmp_path)
    check = bias.bias_check(pd.read_parquet(run / "predictions.parquet"), LOADED, POVERTY, "sha256:x")
    monkeypatch.setattr(bias, "bias_check", lambda *a, **k: pytest.fail("the check was computed again"))
    content, _ = bias.analyse(run, features, SPLITS, tmp_path / "out", loaded=LOADED, check=check)
    assert content["gate"]["gap"] == check["gap"]


# Selection after bias


def entry(model):
    return SimpleNamespace(model=model)


def test_the_best_f1_that_passes_the_bias_check_is_selected():
    ranked = [entry("lightgbm"), entry("xgboost"), entry("logreg")]
    passing = {"lightgbm": {"pass": True}, "xgboost": {"pass": True}, "logreg": {"pass": True}}
    assert s_evaluate.pick_after_bias(ranked, passing)[0].model == "lightgbm"
    first_fails = {**passing, "lightgbm": {"pass": False}}
    winner, why = s_evaluate.pick_after_bias(ranked, first_fails)
    assert winner.model == "xgboost" and "lightgbm failed it" in why
    none_pass = {name: {"pass": False} for name in passing}
    winner, why = s_evaluate.pick_after_bias(ranked, none_pass)
    assert winner.model == "lightgbm" and "gates.py blocks it" in why
