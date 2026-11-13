"""Tests for the model workflow's steps and the Slack notifier (#112, ADR-028, ADR-032).

The steps run in process through their main(), on a small synthetic repo (labels, features and a
training.csv in a temp folder, real neighborhood names so poverty_groups.csv applies). The repo is
trained once per module and copied by the tests that change something. Nothing reaches a network or
the real data.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
from training_helpers import no_mlflow_server, shared_mlflow_store  # noqa: E402,F401  (autouse: MLflow stays in temp folders)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "Model-Development" / "scripts"
STEPS = SCRIPTS / "steps"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(STEPS))
import evaluate as model_evaluate  # noqa: E402  #128's evaluate.py, not a step
import gates as G  # noqa: E402
import selection  # noqa: E402
import step_common as C  # noqa: E402
import step_evaluate as s_evaluate  # noqa: E402
import bias  # noqa: E402
import step_notify as notify  # noqa: E402
import step_pull_data as s_pull  # noqa: E402
import step_registry_push as s_registry  # noqa: E402
import step_reload_check as s_reload  # noqa: E402
import step_rescore_champion as s_rescore  # noqa: E402
import step_set_champion as s_champion  # noqa: E402
import step_train as s_train  # noqa: E402
import train as model_train  # noqa: E402  #128's train.py, not a step
import training_data as D  # noqa: E402

UNITS = ("Back Bay", "Beacon Hill", "Dorchester", "Fenway", "Harbor Islands")  # two lower, two higher, one ungrouped
STEP_MODULES = (C, s_evaluate, notify, s_pull, s_registry, s_reload, s_rescore, s_champion, s_train)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)


# --- a trained synthetic repo ------------------------------------------------


@dataclass
class Built:
    root: Path
    config: Path
    art: Path

    def args(self, config: bool = True) -> list[str]:
        return ["--artifacts", str(self.art), *(["--config", str(self.config)] if config else [])]

    def settings(self) -> D.Settings:
        return D.load_settings(self.config)


def _repo(root: Path, **changes: str) -> Path:
    """A temp repo over every season from 2016 whose extra input carries the surge signal, so the
    candidates clearly beat the seasonal baseline, for the neighborhoods in UNITS."""
    labels = H.make_labels(first="2016-01-01", units=UNITS)
    features = H.make_features(labels)
    surge = labels["surge"].fillna(False).astype(bool).to_numpy()
    rng = np.random.default_rng(1)
    features["signal"] = np.where(surge, rng.normal(2.0, 1.0, len(surge)), rng.normal(0.0, 1.0, len(surge)))
    catalog = H.CATALOG + "signal,test,test,2019-01-01,pass,pass,yes,a strong signal\n"
    return H.write_repo(root, label_frame=labels, feature_frame=features, catalog=catalog,
                        **{"candidates": "logreg;lightgbm", **changes})


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The pull, train and evaluate steps run once on the synthetic repo."""
    root = tmp_path_factory.mktemp("pipeline")
    repo = Built(root, _repo(root), root / "artifacts")
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("MLFLOW_TRACKING_URI", raising=False)
        patch.delenv("GITHUB_SHA", raising=False)
        for step in (s_pull, s_train, s_evaluate):
            assert step.main(repo.args()) == 0
    return repo


@pytest.fixture
def copy(built, tmp_path) -> Built:
    """A private copy of the built repo, its paths rewritten, for a test that changes files."""
    dest = tmp_path / "copy"
    shutil.copytree(built.root, dest)
    config = dest / "training.csv"
    config.write_text(built.config.read_text(encoding="utf-8").replace(str(built.root), str(dest)), encoding="utf-8")
    return Built(dest, config, dest / "artifacts")


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_gate_report(art: Path, decision: str = "promote") -> dict:
    gates = G.load_gates()
    report = {"decision": decision, "gates_sha256": gates.gates_sha256,
              "fairness_group_sha256": gates.fairness_group_sha256}
    (art / "gate_report.json").write_text(json.dumps(report), encoding="utf-8")
    return report


# --- names: the steps never shadow #128's train.py and evaluate.py -------------


def test_step_files_do_not_shadow_the_model_modules():
    for module in (model_train, model_evaluate):
        path = Path(module.__file__).resolve()
        assert path.parent == SCRIPTS.resolve()
        assert STEPS.resolve() not in path.parents
    for step in STEP_MODULES:
        assert Path(step.__file__).resolve().parent == STEPS.resolve()
    assert sorted(p.stem for p in STEPS.glob("*.py")) == sorted(p.stem for p in STEPS.glob("step_*.py"))


def test_a_step_run_as_a_script_imports_the_model_modules(tmp_path):
    """Run from outside the repo, as `python .../steps/step_x.py`: sys.path starts at steps/."""
    snippet = (
        "import sys\n"
        f"sys.path[0] = {str(STEPS)!r}\n"  # what `python steps/step_train.py` sets up
        "import step_common\n"
        "import train, evaluate\n"
        "print(train.__file__)\n"
        "print(evaluate.__file__)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
    )
    assert result.returncode == 0, result.stderr
    train_file, evaluate_file = (Path(line).resolve() for line in result.stdout.splitlines())
    assert train_file == (SCRIPTS / "train.py").resolve()
    assert evaluate_file == (SCRIPTS / "evaluate.py").resolve()


def test_a_real_step_script_run_has_the_documented_command_line(copy):
    """The workflow's call shape, as a script: exit 0, the data version written, then exit 2 on a bad file."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    command = [sys.executable, str(STEPS / "step_pull_data.py"), *copy.args()]
    result = subprocess.run(command, cwd=copy.root, capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    assert (copy.art / "data_version.txt").read_text().startswith("sha256:")
    copy.settings().labels.write_bytes(b"not parquet")
    result = subprocess.run(command, cwd=copy.root, capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 2 and "is not a parquet file" in result.stderr


def test_the_stub_helpers_are_gone():
    assert not any(hasattr(C, name) for name in ("stub_outcome", "load_model", "score", "FIXTURE_ROW", "OUTCOMES"))
    text = "".join(path.read_text(encoding="utf-8") for path in STEPS.glob("*.py"))
    assert "STUB_OUTCOME" not in text and "train_scores" not in text and "model.json" not in text
    assert not (ROOT / "Model-Development" / "config" / "reload_fixture_row.json").exists()


@pytest.mark.parametrize("module, owner", [(s_rescore, "#111"), (s_registry, "#111"), (s_champion, "#111")])
def test_stub_and_interim_steps_start_with_their_interface_block(module, owner):
    lines = module.__doc__.splitlines()
    assert lines[0].startswith(f"INTERFACE FOR {owner} (") and lines[0].endswith(
        "replace this body; keep the command line and exit codes.")
    name = Path(module.__file__).name
    assert lines[1] == f"Called as: python Model-Development/scripts/steps/{name} --artifacts DIR"
    keys = [line.split(":")[0] for line in lines[2:] if line and not line.startswith(" ")]
    assert keys[:4] == ["Reads", "Writes", "Exit", "Must hold"]
    assert "Exit: 0 ok, 2 pipeline failed" in module.__doc__


def test_the_evaluate_block_names_the_groups_and_the_reconciliation():
    doc = s_evaluate.__doc__
    for text in ("groups", "excluded", "tp + fn", "true positives", "ungrouped"):
        assert text in doc


# --- common ------------------------------------------------------------------


def test_common_reads_and_rejects(tmp_path):
    (tmp_path / "data_version.txt").write_text("  \n")
    with pytest.raises(C.StepError, match="is empty"):
        C.read_data_version(tmp_path)
    with pytest.raises(C.StepError, match="cannot read"):
        C.read_data_version(tmp_path / "missing")
    (tmp_path / "list.json").write_text("[1]")
    with pytest.raises(C.StepError, match="not a JSON object"):
        C.read_object(tmp_path / "list.json")
    with pytest.raises(C.StepError, match="cannot read"):
        C.read_json(tmp_path / "missing.json")
    (tmp_path / "bad.csv").write_text("nothing")
    with pytest.raises(C.StepError, match="cannot load the training settings"):
        C.load_settings(tmp_path / "bad.csv")
    assert C.run_id("lightgbm") == "ci-lightgbm"


@pytest.mark.parametrize("error", [KeyError("winner"), TypeError("not a number"), ValueError("bad"),
                                   RuntimeError("boom")])
def test_the_runner_turns_any_exception_into_exit_2(error, capsys):
    def step(argv):
        raise error

    assert C.run(step, []) == 2
    err = capsys.readouterr().err
    assert "pipeline failed" in err and type(error).__name__ in err


def test_the_runner_keeps_exit_1_for_the_gates_and_lets_argparse_exit_through():
    assert C.run(lambda argv: None, []) == 0
    with pytest.raises(SystemExit) as exc:
        C.run(lambda argv: C.parse_artifacts("x", argv), [])
    assert exc.value.code == 2


def test_a_key_error_in_a_step_exits_2(copy, monkeypatch):
    monkeypatch.setattr(s_evaluate, "pick_winner", lambda *a, **k: {}["missing"])
    assert s_evaluate.main(copy.args()) == 2


def test_a_type_error_in_a_step_exits_2(copy, monkeypatch):
    monkeypatch.setattr(C, "read_data_version", lambda artifacts: None + 1)
    assert s_train.main(copy.args()) == 2


def test_a_truncated_parquet_exits_2(copy):
    run = copy.settings().runs_dir / read(copy.art / "evaluation.json")["winner_run"]
    path = run / "predictions.parquet"
    path.write_bytes(path.read_bytes()[:200])  # cut off: pyarrow raises ArrowInvalid, not an OSError
    assert s_reload.main(copy.args()) == 2


def test_artifacts_flag_is_required():
    with pytest.raises(SystemExit) as exc:
        s_pull.main([])
    assert exc.value.code == 2


# --- pull data -----------------------------------------------------------------


def test_pull_data_records_the_version_training_uses(built, capsys):
    assert s_pull.main(built.args()) == 0
    settings = built.settings()
    version = (built.art / "data_version.txt").read_text().strip()
    assert version == D.data_version(settings.dvc_lock) and version.startswith("sha256:")
    printed = capsys.readouterr().out
    for name in ("labels", "features", "feature_catalog", "dvc_lock"):
        assert f"{name}: " in printed


@pytest.mark.parametrize("name, content, message", [
    ("labels", None, "cannot read"),
    ("features", b"text, not parquet", "is not a parquet file"),
    ("feature_catalog", b"", "is empty"),
    ("dvc_lock", None, "cannot read"),
])
def test_pull_data_exits_2_on_a_bad_file(copy, name, content, message, capsys):
    path = getattr(copy.settings(), name)
    (copy.art / "data_version.txt").unlink()  # the copy was pulled already: a failed pull must not leave a version
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)
    assert s_pull.main(copy.args()) == 2
    assert message in capsys.readouterr().err
    assert not (copy.art / "data_version.txt").exists()


# --- train -----------------------------------------------------------------------


def test_train_writes_a_run_per_model_under_fixed_ids(built):
    settings, version = built.settings(), (built.art / "data_version.txt").read_text().strip()
    assert settings.candidates == ("logreg", "lightgbm")
    for model in ("seasonal", *settings.candidates):
        run = settings.runs_dir / C.run_id(model)
        for name in ("predictions.parquet", "metrics.json", "validation_counts.json", "run.json"):
            assert (run / name).is_file()
        assert (run / "model.joblib").is_file() == (model != "seasonal")  # only a candidate is saved
        assert read(run / "validation_counts.json")["data_version"] == version
        assert read(run / "run.json")["mlflow"]["run_id"]


def test_train_needs_the_data_version(tmp_path, built):
    empty = Built(built.root, built.config, tmp_path / "none")
    assert s_train.main(empty.args()) == 2


def test_train_exits_2_when_a_run_fails(tmp_path):
    repo = Built(tmp_path, _repo(tmp_path, candidates="nonsense"), tmp_path / "artifacts")
    assert s_pull.main(repo.args()) == 0
    assert s_train.main(repo.args()) == 2  # seasonal trains, then train.py exits 2 for the unknown model


def test_train_exits_2_when_a_run_folder_already_exists(copy):
    assert s_train.main(copy.args()) == 2  # ci-seasonal is there already: train.py refuses to overwrite it


# --- evaluate ----------------------------------------------------------------------


def _entry(model, tp, fp, fn, log_loss=0.1):
    validation = {"tp": tp, "fp": fp, "fn": fn, "log_loss": log_loss}
    return selection.Entry(model, Path("."), {}, {"validation": validation})


def test_the_winner_is_ranked_by_f1_then_log_loss_then_name():
    entries = {"a": _entry("a", 1, 1, 1, 0.2), "b": _entry("b", 1, 1, 1, 0.1), "c": _entry("c", 1, 1, 1, 0.1),
               "d": _entry("d", 3, 1, 1, 0.9)}
    assert [e.model for e in s_evaluate.pick_winner(entries, ("a", "b", "c", "d"))] == ["d", "b", "c", "a"]
    only = {"x": _entry("x", 0, 0, 0)}  # no F1 at all: still returned, gates.py then says "error"
    assert [e.model for e in s_evaluate.pick_winner(only, ("x",))] == ["x"]


def test_the_margin_is_not_applied_here(built):
    """selection.py would refuse a candidate under the margin (its exit 1); this step names the top one."""
    entries = {"seasonal": _entry("seasonal", 10, 5, 5), "weak": _entry("weak", 10, 5, 4)}
    bar, winner = selection.decide(s_evaluate.pick_winner(entries, ("weak",)), entries["seasonal"], Fraction(1, 2))
    assert winner is None  # selection.py: no winner
    assert s_evaluate.pick_winner(entries, ("weak",))[0].model == "weak"  # the step: the top candidate, gates.py judges


def test_evaluate_writes_the_gates_input_from_the_runs(built):
    settings = built.settings()
    metrics = read(built.art / "metrics.json")
    evaluation = read(built.art / "evaluation.json")
    assert sorted(metrics) == ["baseline", "candidate", "champion", "data_version", "excluded", "groups"]
    assert metrics["champion"] is None and metrics["data_version"] == read(
        settings.runs_dir / "ci-seasonal" / "validation_counts.json")["data_version"]
    for key, model in (("candidate", evaluation["winner"]), ("baseline", "seasonal")):
        counts = read(settings.runs_dir / C.run_id(model) / "validation_counts.json")
        assert metrics[key] == {name: counts[name] for name in ("tp", "fp", "fn")}
    # the winner is the first by selection.py's own ranking over the same runs
    entries = {m: selection.Entry.read(m, settings.runs_dir / C.run_id(m)) for m in settings.candidates}
    assert [e.model for e in selection.rank(list(entries.values()))] == [r["model"] for r in evaluation["ranking"]]
    assert evaluation["winner"] == evaluation["ranking"][0]["model"] and evaluation["baseline"] == "seasonal"


def test_the_group_counts_match_an_independent_count_and_reconcile(built):
    settings = built.settings()
    metrics, evaluation = read(built.art / "metrics.json"), read(built.art / "evaluation.json")
    rows = pd.read_parquet(settings.runs_dir / evaluation["winner_run"] / "predictions.parquet")
    rows = rows[rows["split"] == "validation"]
    mapping = {"Back Bay": "lower", "Beacon Hill": "lower", "Dorchester": "higher", "Fenway": "higher"}
    for group in ("lower", "higher"):
        mine = rows[rows["unit"].map(mapping) == group]
        assert metrics["groups"][group] == {"positives": int(mine["label"].sum()),
                                            "true_positives": int(((mine["label"] == 1) & (mine["prediction"] == 1)).sum())}
    assert list(metrics["excluded"]) == ["Harbor Islands"]
    assert metrics["excluded"]["Harbor Islands"]["reason"] == "ungrouped"
    parts = [*metrics["groups"].values(), *metrics["excluded"].values()]
    assert sum(p["positives"] for p in parts) == metrics["candidate"]["tp"] + metrics["candidate"]["fn"]
    assert sum(p["true_positives"] for p in parts) == metrics["candidate"]["tp"]
    assert metrics["baseline"]["tp"] + metrics["baseline"]["fn"] == metrics["candidate"]["tp"] + metrics["candidate"]["fn"]


def test_gates_take_the_evaluate_steps_metrics(built, tmp_path):
    code = G.main(["--metrics", str(built.art / "metrics.json"), "--out", str(tmp_path / "report.json")])
    report = read(tmp_path / "report.json")
    assert code in (0, 1) and report["decision"] in ("promote", "keep_champion", "blocked")  # never "error"
    assert report["data_version"] == read(built.art / "metrics.json")["data_version"]


def test_reconciliation_failure_is_caught_here():
    groups = {"lower": {"positives": 6, "true_positives": 3}, "higher": {"positives": 4, "true_positives": 2}}
    excluded = {"Harbor Islands": {"positives": 0, "true_positives": 0, "reason": "ungrouped"}}
    bias.check_reconciliation(groups, excluded, {"tp": 5, "fp": 9, "fn": 5})  # holds
    with pytest.raises(bias.BiasError, match="positives 10 do not equal the candidate's tp \\+ fn 11"):
        bias.check_reconciliation(groups, excluded, {"tp": 5, "fp": 9, "fn": 6})
    with pytest.raises(bias.BiasError, match="true positives 5 do not equal the candidate's tp 4"):
        bias.check_reconciliation(groups, excluded, {"tp": 4, "fp": 9, "fn": 6})


def test_a_neighborhood_in_no_group_fails():
    rows = pd.DataFrame({"unit": ["Back Bay", "Atlantis"], "label": [1, 1], "prediction": [1, 0]})
    mapping = {"Back Bay": "lower", "Fenway": "higher", "Harbor Islands": "ungrouped"}
    with pytest.raises(bias.BiasError, match="Atlantis"):
        bias.gate_counts(rows, mapping, ("lower", "higher"))
    groups, excluded = bias.gate_counts(rows[rows["unit"] == "Back Bay"], mapping, ("lower", "higher"))
    assert groups == {"lower": {"positives": 1, "true_positives": 1}, "higher": {"positives": 0, "true_positives": 0}}
    assert excluded == {"Harbor Islands": {"positives": 0, "true_positives": 0, "reason": "ungrouped"}}


def test_a_group_file_with_an_unknown_group_fails(tmp_path):
    path = tmp_path / "groups.csv"
    path.write_text("neighborhood,poverty_share,group\nBack Bay,0.1,middle\n")
    with pytest.raises(bias.BiasError, match="middle"):
        bias.read_poverty_groups(path, ("lower", "higher"))
    with pytest.raises(bias.BiasError, match="cannot read"):
        bias.read_poverty_groups(tmp_path / "missing.csv", ("lower", "higher"))


def test_evaluate_reads_no_test_metric(copy):
    sentinel = 0.123456789
    for model in ("seasonal", *copy.settings().candidates):
        path = copy.settings().runs_dir / C.run_id(model) / "metrics.json"
        data = read(path)
        data["test"] = {key: sentinel for key in data["test"]}  # a loud value in every test field
        path.write_text(json.dumps(data))
    (copy.art / "metrics.json").unlink()
    assert s_evaluate.main(copy.args()) == 0
    for name in ("metrics.json", "evaluation.json"):
        assert "0.123456789" not in (copy.art / name).read_text()


def test_evaluate_filters_test_rows_out_of_the_read(copy, monkeypatch):
    seen = {}
    real = pd.read_parquet

    def spy(path, *args, **kwargs):
        if str(path).endswith("predictions.parquet"):
            seen["filters"] = kwargs.get("filters")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", spy)
    assert s_evaluate.main(copy.args()) == 0
    assert seen["filters"] == [("split", "==", "validation")]


def test_evaluate_exits_2_on_a_data_version_mismatch(copy):
    (copy.art / "data_version.txt").write_text("sha256:other\n")
    assert s_evaluate.main(copy.args()) == 2


def test_evaluate_exits_2_on_a_run_trained_on_another_version(copy):
    path = copy.settings().runs_dir / "ci-seasonal" / "validation_counts.json"
    data = read(path)
    data["data_version"] = "sha256:another"
    path.write_text(json.dumps(data))
    assert s_evaluate.main(copy.args()) == 2


def test_evaluate_exits_2_without_a_run_or_a_validation_block(copy):
    path = copy.settings().runs_dir / "ci-logreg" / "metrics.json"
    path.write_text(json.dumps({"test": {}}))
    assert s_evaluate.main(copy.args()) == 2
    shutil.rmtree(copy.settings().runs_dir / "ci-logreg")
    assert s_evaluate.main(copy.args()) == 2


def test_evaluate_exits_2_on_counts_that_are_not_integers(copy):
    path = copy.settings().runs_dir / "ci-seasonal" / "validation_counts.json"
    data = read(path)
    data["tp"] = "many"
    path.write_text(json.dumps(data))
    assert s_evaluate.main(copy.args()) == 2


def test_evaluate_exits_2_on_predictions_that_disagree_with_the_counts(copy):
    run = copy.settings().runs_dir / read(copy.art / "evaluation.json")["winner_run"]
    frame = pd.read_parquet(run / "predictions.parquet")
    frame.loc[frame["split"] == "validation", "prediction"] = 0  # no true positive left: tp no longer reconciles
    frame.to_parquet(run / "predictions.parquet", index=False)
    assert s_evaluate.main(copy.args()) == 2


# --- re-score the champion (stub) ----------------------------------------------------


def test_rescore_champion_leaves_champion_null(copy):
    assert s_rescore.main(copy.args(config=False)) == 0
    assert read(copy.art / "metrics.json")["champion"] is None
    assert read(copy.art / "champion_rescore.json")["champion"] is None


def test_rescore_champion_needs_metrics_that_are_an_object(tmp_path):
    assert s_rescore.main(["--artifacts", str(tmp_path)]) == 2
    (tmp_path / "metrics.json").write_text("[1]")
    assert s_rescore.main(["--artifacts", str(tmp_path)]) == 2


# --- reload check ----------------------------------------------------------------------


class Shifted:
    """A reloaded model whose probabilities differ from the trained one's by delta."""

    def __init__(self, model, delta=0.0, invert=False):
        self.model, self.delta, self.invert = model, delta, invert

    def predict_proba(self, rows):
        out = np.array(self.model.predict_proba(rows), dtype="float64")
        out[:, 1] += self.delta
        return out

    def predict(self, rows):
        out = np.asarray(self.model.predict(rows))
        return 1 - out if self.invert else out


def _patch_load(monkeypatch, **kwargs):
    import joblib

    real = joblib.load
    monkeypatch.setattr(joblib, "load", lambda path: Shifted(real(path), **kwargs))


def test_reload_check_ok(built, capsys):
    assert s_reload.main(built.args()) == 0
    out = capsys.readouterr().out
    assert "reload check ok" in out and "tolerance 1e-09" in out
    record = read(built.art / "reload_check.json")
    assert record["max_abs_difference"] <= 1e-9 and record["rows"] > 0


def test_reload_check_tolerance_is_1e_9(built, monkeypatch):
    _patch_load(monkeypatch, delta=5e-10)
    assert s_reload.main(built.args()) == 0
    _patch_load(monkeypatch, delta=2e-9)
    assert s_reload.main(built.args()) == 2


def test_reload_mismatch_exits_2(built, monkeypatch, capsys):
    _patch_load(monkeypatch, delta=1e-6)
    assert s_reload.main(built.args()) == 2
    assert "reload mismatch" in capsys.readouterr().err


def test_reload_check_compares_the_flags_too(built, monkeypatch, capsys):
    _patch_load(monkeypatch, invert=True)
    assert s_reload.main(built.args()) == 2
    assert "flags different validation rows" in capsys.readouterr().err


def test_a_changed_byte_of_the_saved_model_exits_2(copy, capsys):
    path = copy.settings().runs_dir / read(copy.art / "evaluation.json")["winner_run"] / "model.joblib"
    data = bytearray(path.read_bytes())
    data[0] ^= 1  # the first byte: the model file cannot be loaded
    path.write_bytes(bytes(data))
    assert s_reload.main(copy.args()) == 2
    assert "cannot reload and score" in capsys.readouterr().err


def test_reload_check_exits_2_without_its_inputs(copy):
    run = copy.settings().runs_dir / read(copy.art / "evaluation.json")["winner_run"]
    (run / "model.joblib").unlink()
    assert s_reload.main(copy.args()) == 2
    (copy.art / "evaluation.json").unlink()
    assert s_reload.main(copy.args()) == 2


def test_reload_check_exits_2_when_the_validation_rows_differ(copy):
    run = copy.settings().runs_dir / read(copy.art / "evaluation.json")["winner_run"]
    frame = pd.read_parquet(run / "predictions.parquet")
    frame = frame[~((frame["split"] == "validation") & (frame["unit"] == "Fenway"))]
    frame.to_parquet(run / "predictions.parquet", index=False)
    assert s_reload.main(copy.args()) == 2


def test_reload_check_exits_2_when_the_run_lists_no_inputs(copy):
    run = copy.settings().runs_dir / read(copy.art / "evaluation.json")["winner_run"]
    data = read(run / "run.json")
    data["inputs"] = []
    (run / "run.json").write_text(json.dumps(data))
    assert s_reload.main(copy.args()) == 2


# --- registry push and set champion (stubs) --------------------------------------------


def test_registry_json_carries_the_version_stamps(copy, monkeypatch, capsys):
    report = write_gate_report(copy.art)
    (copy.art / "image_digest.txt").write_text("us-east1-docker.pkg.dev/p/r/brt-train@sha256:abc\n")
    monkeypatch.setenv("GITHUB_SHA", "0123456789abcdef0123456789abcdef01234567")
    assert s_registry.main(copy.args()) == 0
    registry = read(copy.art / "registry.json")
    settings, winner_run = copy.settings(), read(copy.art / "evaluation.json")["winner_run"]
    run = read(settings.runs_dir / winner_run / "run.json")
    assert sorted(registry) == sorted(["model_version", "pushed", "model", "data_version", "git_commit",
                                       "mlflow_run_id", "image_digest", "gates_sha256", "fairness_group_sha256",
                                       "windows", "decision"])
    assert registry["pushed"] is False and registry["decision"] == "promote"
    assert registry["model"] == {"name": read(copy.art / "evaluation.json")["winner"],
                                 "path": f"runs/{winner_run}/model.joblib",
                                 "sha256": hashlib.sha256((settings.runs_dir / winner_run / "model.joblib").read_bytes()).hexdigest()}
    assert registry["data_version"] == (copy.art / "data_version.txt").read_text().strip()
    assert registry["git_commit"] == "0123456789abcdef0123456789abcdef01234567"
    assert registry["mlflow_run_id"] == run["mlflow"]["run_id"]
    assert registry["image_digest"] == "us-east1-docker.pkg.dev/p/r/brt-train@sha256:abc"
    assert (registry["gates_sha256"], registry["fairness_group_sha256"]) == (report["gates_sha256"], report["fairness_group_sha256"])
    assert registry["windows"] == {"train": {"first": "2015-01-01", "last": "2023-12-31"},
                                   "validation": {"first": "2024-01-01", "last": "2024-12-31"},
                                   "test": {"first": "2025-01-01", "last": "2026-06-30"}}
    assert "::warning::" not in capsys.readouterr().out


def test_registry_push_warns_without_a_digest_or_a_commit(copy, capsys):
    write_gate_report(copy.art)
    assert s_registry.main(copy.args()) == 0
    registry = read(copy.art / "registry.json")
    assert registry["image_digest"] is None and registry["git_commit"] is None
    assert capsys.readouterr().out.count("::warning::") == 2


@pytest.mark.parametrize("decision", ["blocked", "error", None])
def test_registry_push_refuses_other_decisions(copy, decision):
    write_gate_report(copy.art, decision)
    assert s_registry.main(copy.args()) == 2
    assert not (copy.art / "registry.json").exists()


def test_registry_push_exits_2_without_its_inputs(copy):
    assert s_registry.main(copy.args()) == 2  # no gate report yet
    write_gate_report(copy.art)
    run = copy.settings().runs_dir / read(copy.art / "evaluation.json")["winner_run"]
    data = read(run / "run.json")
    del data["settings"]["splits"]
    (run / "run.json").write_text(json.dumps(data))
    assert s_registry.main(copy.args()) == 2
    (run / "model.joblib").unlink()
    assert s_registry.main(copy.args()) == 2


def test_set_champion_promote_writes_would_set(copy, capsys):
    write_gate_report(copy.art)
    s_registry.main(copy.args())
    assert s_champion.main(copy.args(config=False)) == 0
    assert read(copy.art / "champion.json")["action"] == "would set champion"
    assert "would set champion" in capsys.readouterr().out


def test_set_champion_keep_changes_nothing(copy, capsys):
    write_gate_report(copy.art, "keep_champion")
    assert s_champion.main(copy.args(config=False)) == 0
    assert not (copy.art / "champion.json").exists()
    assert "keep champion" in capsys.readouterr().out


@pytest.mark.parametrize("decision", ["blocked", "error", None])
def test_set_champion_rejects_other_decisions(tmp_path, decision):
    (tmp_path / "gate_report.json").write_text(json.dumps({"decision": decision}))
    assert s_champion.main(["--artifacts", str(tmp_path)]) == 2


def test_set_champion_rejects_a_non_object_report(tmp_path):
    (tmp_path / "gate_report.json").write_text("[]")
    assert s_champion.main(["--artifacts", str(tmp_path)]) == 2


def test_the_real_chain_end_to_end(copy, monkeypatch):
    """pull, train, evaluate on a fresh artifacts folder of the copy, then gates, reload and the stubs."""
    shutil.rmtree(copy.root / "runs")
    shutil.rmtree(copy.art)
    monkeypatch.setenv("GITHUB_SHA", "c" * 40)
    for step in (s_pull, s_train, s_evaluate):
        assert step.main(copy.args()) == 0
    assert s_rescore.main(copy.args(config=False)) == 0
    code = G.main(["--metrics", str(copy.art / "metrics.json"), "--out", str(copy.art / "gate_report.json")])
    assert code in (0, 1)
    assert s_reload.main(copy.args()) == 0
    if code == 0:
        assert s_registry.main(copy.args()) == 0 and s_champion.main(copy.args(config=False)) == 0
        assert read(copy.art / "registry.json")["git_commit"] == "c" * 40


# --- notify ----------------------------------------------------------------------------

RUN = {"repo": "org/repo", "ref": "main", "run_url": "https://example.invalid/run/1"}


def notify_args(art: Path, status: str, gates_exit: str = "", main: bool = False, failed: str = "") -> list[str]:
    out = [
        "--artifacts", str(art), "--repo", RUN["repo"], "--ref", RUN["ref"],
        "--run-url", RUN["run_url"], "--job-status", status, "--gates-exit", gates_exit,
    ]
    if failed:
        out += ["--failed-step", failed]
    return out + (["--main"] if main else [])


@pytest.fixture
def gate_art(tmp_path):
    """An artifacts folder with a data version and the gate report of the fixture metrics that pass."""
    (tmp_path / "data_version.txt").write_text("sha256:abc\n")
    assert G.main(["--metrics", str(FIXTURES / "gates" / "pass.json"), "--out", str(tmp_path / "gate_report.json")]) == 0
    return tmp_path


@pytest.mark.parametrize(
    ("status", "gates_exit", "is_main", "expected"),
    [
        ("failure", "1", True, notify.GATE_FAILED),  # main: the gates step fails the job, nothing later runs
        ("success", "1", False, notify.GATE_FAILED),  # a PR whose gates exited 1 only: the job passes, Slack says so
        ("failure", "1", False, notify.PIPELINE_FAILED),  # a PR: gates exit 1, then a later step (reload check) failed
        ("failure", "2", False, notify.PIPELINE_FAILED),
        ("failure", "", True, notify.PIPELINE_FAILED),
        ("success", "0", True, notify.TRAINING_COMPLETE),
        ("success", "0", False, None),
        ("success", "", False, None),
        ("cancelled", "", True, None),
        ("cancelled", "1", True, notify.GATE_FAILED),
        ("cancelled", "1", False, None),
    ],
)
def test_route(status, gates_exit, is_main, expected):
    assert notify.route(status, gates_exit, is_main) == expected


def test_message_carries_the_run_facts(gate_art):
    text = notify.build_message(notify.TRAINING_COMPLETE, gate_art, **RUN)
    for expected in ("training complete", "org/repo", "main", "https://example.invalid/run/1", "sha256:abc",
                     "Decision: promote", "F1 margin: 0.", "gap: 0.0000"):
        assert expected in text
    assert "Failed step" not in text and "not pushed" not in text


@pytest.mark.parametrize("kind", [notify.GATE_FAILED, notify.PIPELINE_FAILED])
def test_a_failure_message_names_the_failed_step_and_says_not_pushed(gate_art, kind):
    text = notify.build_message(kind, gate_art, **RUN, failed_step="Reload check")
    assert "Failed step: Reload check" in text and "not pushed" in text
    assert notify.build_message(kind, gate_art, **RUN).count("Failed step: unknown") == 1


def test_the_message_never_carries_a_test_metric(gate_art):
    report = read(gate_art / "gate_report.json")
    report.update(test_f1=0.987654, test={"f1": 0.987654})
    (gate_art / "gate_report.json").write_text(json.dumps(report))
    for kind in (notify.GATE_FAILED, notify.PIPELINE_FAILED, notify.TRAINING_COMPLETE):
        assert "0.987654" not in notify.build_message(kind, gate_art, **RUN, failed_step="Gates")


def test_message_without_artifacts_says_na(tmp_path):
    text = notify.build_message(notify.PIPELINE_FAILED, tmp_path, **RUN)
    assert "Data version: n/a" in text and "Decision: n/a" in text and "F1 margin: n/a" in text


def test_message_for_an_error_report(tmp_path):
    shutil.copy(FIXTURES / "pipeline" / "gate_report_error.json", tmp_path / "gate_report.json")
    text = notify.build_message(notify.GATE_FAILED, tmp_path, **RUN)
    assert "Decision: error" in text and "F1 margin: n/a" in text


def test_message_escapes_slack_characters(tmp_path):
    text = notify.build_message(notify.PIPELINE_FAILED, tmp_path, "a&b", "<x>", "u", failed_step="<step>")
    assert "a&amp;b" in text and "&lt;x&gt;" in text and "&lt;step&gt;" in text


def test_notify_without_webhook_warns_and_exits_0(gate_art, capsys):
    assert notify.main(notify_args(gate_art, "success", "0", main=True)) == 0
    out = capsys.readouterr().out
    assert "::warning::SLACK_ALERT_WEBHOOK is empty" in out
    assert "Model pipeline: training complete" in out


def test_notify_posts_one_payload_on_a_failure(gate_art, monkeypatch):
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append((url, payload)))
    assert notify.main(notify_args(gate_art, "failure", "", main=True, failed="Train the baseline and the candidates")) == 0
    (url, payload), = sent
    assert url == "https://example.invalid/hook" and list(payload) == ["text"]
    assert "pipeline failed" in payload["text"] and "Failed step: Train the baseline" in payload["text"]
    assert "not pushed" in payload["text"] and "example.invalid/hook" not in payload["text"]


def test_notify_pr_success_sends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda *a: pytest.fail("posted"))
    assert notify.main(notify_args(tmp_path, "success", "0")) == 0
    assert "no Slack message" in capsys.readouterr().out


def test_notify_gate_failure_on_a_passing_pr_job(gate_art, monkeypatch):
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append(payload))
    assert notify.main(notify_args(gate_art, "success", "1", failed="Gates")) == 0
    assert "gate failed" in sent[0]["text"] and "Failed step: Gates" in sent[0]["text"]


def test_notify_missing_artifacts_dir_still_sends_pipeline_failed(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append(payload))
    missing = tmp_path / "does-not-exist"
    assert notify.main(notify_args(missing, "failure", "", main=True)) == 0
    assert len(sent) == 1 and "pipeline failed" in sent[0]["text"] and "Data version: n/a" in sent[0]["text"]
    assert not missing.exists()


def test_notify_empty_gates_exit_is_pipeline_failed(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "post", lambda url, payload: sent.append(payload))
    argv = notify_args(tmp_path, "failure", "")
    assert notify.main(argv) == 0
    assert "pipeline failed" in sent[0]["text"]
    sent.clear()  # the flag left out altogether behaves the same
    without = [a for i, a in enumerate(argv) if a != "--gates-exit" and argv[i - 1] != "--gates-exit"]
    assert notify.main(without) == 0
    assert "pipeline failed" in sent[0]["text"]


def test_notify_undecodable_data_version_does_not_crash(tmp_path):
    (tmp_path / "data_version.txt").write_bytes(b"\xff\xfe")
    assert "Data version: n/a" in notify.build_message(notify.PIPELINE_FAILED, tmp_path, **RUN)


def test_notify_rejects_a_non_https_webhook(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(notify.WEBHOOK_ENV, "ftp://example.invalid/secret-token")
    monkeypatch.setattr(notify, "post", lambda *a: pytest.fail("posted"))
    assert notify.main(notify_args(tmp_path, "failure", "")) == 0
    out = capsys.readouterr().out
    assert "not an https URL" in out and "secret-token" not in out


def test_notify_post_failure_is_a_warning_without_the_url(tmp_path, monkeypatch, capsys):
    def boom(url, payload):
        raise OSError(f"cannot reach {url}")

    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/secret-token")
    monkeypatch.setattr(notify, "post", boom)
    assert notify.main(notify_args(tmp_path, "failure", "")) == 0
    out = capsys.readouterr().out
    assert "::warning::Slack post failed (OSError)" in out and "secret-token" not in out
