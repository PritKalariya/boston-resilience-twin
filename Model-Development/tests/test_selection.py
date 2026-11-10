"""Tests for model selection (#107): ranking, the ADR-026 margin, undefined F1, reports and MLflow."""

from __future__ import annotations

import csv
import json
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
from training_helpers import no_mlflow_server, shared_mlflow_store  # noqa: E402,F401  (autouse: MLflow stays in temp folders)
import tracking  # noqa: E402
import mlflow  # noqa: E402
import selection as S  # noqa: E402
import training_data as D  # noqa: E402

MARGIN = Fraction(1, 20)


def entry(model, tp, fp, fn, tn=1000, log_loss=0.1, version="sha256:same"):
    """A trained model as selection reads it, with the given validation counts."""
    split = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "rows": tp + fp + fn + tn, "surges": tp + fn,
             "log_loss": log_loss, "f1": None, "precision": None, "recall": None, "auc": None, "pr_auc": None,
             "brier_skill": None}
    run = {"run_id": f"s-{model}", "threshold": 0.5, "data_version": version, "git": {"commit": "abc"},
           "model_details": {}, "mlflow": {"run_id": f"m-{model}", "model_uri": None},
           "costs": {"train_time_s": 1.0, "model_size_bytes": None if model == "seasonal" else 2048,
                     "score_time_evening_ms": {"median": 1.0, "p95": 2.0, "rows": 24, "repeats": 50}}}
    return S.Entry(model, Path("."), run, {"validation": split, "test": dict(split)})


def test_candidates_rank_by_their_exact_validation_f1():
    third = entry("third", tp=1, fp=2, fn=2)  # 2 / 6 = 1/3
    close = entry("close", tp=333, fp=667, fn=666)  # 666 / 1999, just under 1/3
    half = entry("half", tp=1, fp=1, fn=1)  # 2 / 4
    assert [e.model for e in S.rank([close, third, half])] == ["half", "third", "close"]
    assert third.f1 == Fraction(1, 3) and close.f1 < third.f1


def test_an_exact_tie_goes_to_the_lower_log_loss_then_the_name():
    a, b, c = entry("a", 1, 1, 1, log_loss=0.2), entry("b", 1, 1, 1, log_loss=0.1), entry("c", 1, 1, 1, log_loss=0.2)
    assert [e.model for e in S.rank([c, a, b])] == ["b", "a", "c"]


def test_the_margin_is_inclusive_and_compared_exactly():
    baseline = entry("seasonal", tp=16, fp=4, fn=4)  # F1 32/40 = 4/5, from 20 surges
    exactly = entry("exactly", tp=17, fp=3, fn=3)  # F1 34/40 = 17/20 = 4/5 + 1/20, the same 20 surges
    assert 0.8 + 0.05 > 0.85  # in floating point the bar lands above 17/20 and would refuse this candidate
    bar, winner = S.decide([exactly], baseline, MARGIN)
    assert bar == Fraction(17, 20) and winner is exactly
    under = entry("under", tp=17, fp=4, fn=3)  # F1 34/41, under the bar
    assert S.decide([under], baseline, MARGIN) == (Fraction(17, 20), None)


def test_no_candidate_wins_when_the_best_misses_the_gate():
    baseline, best = entry("seasonal", 1, 4, 4), entry("best", 1, 3, 4)
    bar, winner = S.decide([best], baseline, MARGIN)
    content = S.report("s", [best], baseline, MARGIN, bar, winner)
    assert content["winner"] is None and content["decision"].startswith("no winner: the best candidate, best")
    assert content["ranking"][0]["reaches_gate"] is False and content["gate_f1_exact"] == "1/4"


def test_an_undefined_f1_ranks_last_and_stays_undefined_never_zero():
    undefined = entry("undefined", tp=0, fp=0, fn=0)  # no surge and no alarm: no F1
    zero = entry("zero", tp=0, fp=5, fn=3)  # a real F1 of 0
    ranked = S.rank([undefined, zero])
    assert [e.model for e in ranked] == ["zero", "undefined"]
    rows = S.report("s", ranked, entry("seasonal", 1, 4, 4), MARGIN, *S.decide(ranked, entry("seasonal", 1, 4, 4), MARGIN))["ranking"]
    assert (rows[0]["validation_f1"], rows[1]["validation_f1"], rows[1]["validation_f1_exact"]) == (0.0, None, None)


def test_an_undefined_baseline_f1_stops_the_selection():
    with pytest.raises(ValueError, match="on_undefined = fail"):
        S.decide([entry("a", 1, 1, 1)], entry("seasonal", 0, 0, 0), MARGIN)


def test_models_from_two_data_versions_are_refused():
    a, baseline = entry("a", 1, 1, 1, version="sha256:one"), entry("seasonal", 1, 4, 4, version="sha256:two")
    with pytest.raises(ValueError, match="different data versions"):
        S.report("s", [a], baseline, MARGIN, *S.decide([a], baseline, MARGIN))


def test_the_gate_rule_comes_from_gates_csv(tmp_path):
    assert S.gate_rule() == ("seasonal", Fraction(1, 20))
    bad = tmp_path / "gates.csv"
    for text, message in [("key,value\nf1_baseline,seasonal\n", "needs f1_min_margin"),
                          ("key,value\nf1_baseline,seasonal\nf1_min_margin,2\n", "from 0 to 1"),
                          ("key,value\nf1_min_margin,0.05\n", "needs f1_baseline")]:
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            S.gate_rule(bad)


def test_the_charts_handle_a_baseline_without_a_model_and_a_row_without_cases(tmp_path):
    ranked, baseline = [entry("b", 2, 1, 1), entry("a", 1, 1, 1)], entry("seasonal", 1, 4, 4)
    bar, winner = S.decide(ranked, baseline, MARGIN)
    rows = S.comparison_rows(ranked, baseline, winner, bar)
    assert [(r["model"], r["role"]) for r in rows] == [("b", "selected"), ("a", "candidate"), ("seasonal", "baseline")]
    S.write_csv(rows, tmp_path / "comparison.csv")
    with open(tmp_path / "comparison.csv", encoding="utf-8", newline="") as handle:
        assert [r["model_size_bytes"] for r in csv.DictReader(handle)] == ["2048", "2048", ""]
    S.write_comparison(rows, bar, MARGIN, tmp_path / "comparison.png")
    S.write_confusion(entry("quiet", tp=0, fp=3, fn=0), False, tmp_path / "confusion.png")  # no surge: shares of 0
    for name in ("comparison.png", "confusion.png"):
        assert (tmp_path / name).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def _signal_repo(tmp_path):
    """A temp repo over every season from 2016 whose one extra input carries the surge signal, so the
    candidates clearly beat the seasonal baseline. The features also hold what the slice rules read:
    winter alerts on surge days, winter frost, and heat only on July days without a surge."""
    labels = H.make_labels(first="2016-01-01")
    features = H.make_features(labels)
    surge = labels["surge"].fillna(False).astype(bool).to_numpy()
    rng = np.random.default_rng(1)
    features["signal"] = np.where(surge, rng.normal(2.0, 1.0, len(surge)), rng.normal(0.0, 1.0, len(surge)))
    winter = features["date"].dt.month.isin([1, 2, 12]).to_numpy()
    day_has_surge = labels.assign(s=surge).groupby("date")["s"].transform("max").to_numpy()
    features = features.assign(alert_snow_ice_count=(winter & surge).astype(int), alert_tree_count=0,
                               alert_flooding_count=0, wx_snowfall_mm=0.0, wx_precip_mm=0.0, wx_gust_max_ms=5.0,
                               wx_tmin_c=np.where(winter, -3.0, 10.0),
                               wx_tmax_c=np.where((features["date"].dt.month == 7).to_numpy() & ~day_has_surge, 35.0, 20.0))
    catalog = H.CATALOG + "signal,test,test,2019-01-01,pass,pass,yes,a strong signal\n"
    return H.write_repo(tmp_path, label_frame=labels, feature_frame=features, catalog=catalog)


def test_a_selection_trains_every_model_logs_them_and_writes_the_reports(tmp_path, capsys):
    config = _signal_repo(tmp_path)
    out = tmp_path / "reports"
    assert S.main(["--config", str(config), "--id", "sel-e2e", "--reports-dir", str(out)]) == 0
    printed = capsys.readouterr().out
    content = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert content["winner"] in {"logreg", "xgboost", "lightgbm"} and f"{content['winner']} wins" in printed
    assert [row["model"] for row in content["ranking"]][0] == content["winner"]
    assert sorted(row["model"] for row in content["ranking"]) == ["lightgbm", "logreg", "xgboost"]
    assert content["baseline"]["model"] == "seasonal" and content["rule"]["f1_min_margin"] == "1/20"
    for row in content["ranking"]:
        assert row["costs"]["model_size_bytes"] > 0 and (tmp_path / "runs" / row["run_folder"]).is_dir()
    for name in ("comparison.png", "confusion_matrix.png"):
        assert (out / name).read_bytes()[:4] == b"\x89PNG"
    tested = content["test_slices"]
    assert (tested["model"], tested["selected"], tested["baseline"]) == (content["winner"], True, "seasonal")
    records = {record["slice"]: record for record in tested["slices"]}
    assert list(records) == ["all", "storm", "freeze", "heat"] and records["all"]["days"] == 546
    heat = records["heat"]  # July days without a surge: recall is undefined, never 0
    assert heat["days"] > 0 and heat["surges"] == 0 and heat["model"]["recall"] is None
    assert heat["model"]["f1"] in (None, 0.0) and "test window by kind of day" in printed
    client = mlflow.MlflowClient(tracking.tracking_uri(D.load_settings(config)))
    experiment = client.get_experiment_by_name("model-training").experiment_id
    (parent,) = client.search_runs([experiment], filter_string="attributes.run_name = 'sel-e2e'")
    assert parent.data.tags["winner"] == content["winner"] and parent.data.params["f1_min_margin"] == "1/20"
    assert parent.data.metrics["test_all_f1"] == pytest.approx(records["all"]["model"]["f1"])
    assert {a.path for a in client.list_artifacts(parent.info.run_id)} == {
        "selection.json", "comparison.csv", "comparison.png", "confusion_matrix.png", "test_slices.csv"}
    children = client.search_runs([experiment], filter_string=f"tags.mlflow.parentRunId = '{parent.info.run_id}'")
    assert sorted(c.data.tags["model"] for c in children) == ["lightgbm", "logreg", "seasonal", "xgboost"]


def test_main_exits_1_without_a_winner_and_2_on_an_error(tmp_path, monkeypatch, capsys):
    baseline, best = entry("seasonal", 1, 4, 4), entry("best", 1, 3, 4)
    content = S.report("s", [best], baseline, MARGIN, *S.decide([best], baseline, MARGIN))
    content["test_slices"] = {"model": "best", "selected": False, "baseline": "seasonal", "slices": []}
    monkeypatch.setattr(S, "run_selection", lambda settings, selection_id, reports_dir: (content, tmp_path))
    assert S.main(["--config", str(H.write_repo(tmp_path))]) == 1
    assert "no winner" in capsys.readouterr().out
    assert S.main(["--config", str(tmp_path / "missing.csv")]) == 2
    assert "error:" in capsys.readouterr().err
