"""Tests for the Optuna tuning of the candidates (#109, tune.py): the settings, the search spaces, a
trial's score over its seeds, the confirmation on fresh seeds, the candidate and tuned values files,
and short studies on a temp repo."""

from __future__ import annotations

import csv
import dataclasses
import json
import sys
from pathlib import Path

import numpy as np
import optuna
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import training_helpers as H  # noqa: E402  (also puts Model-Development/scripts on sys.path)
from training_helpers import no_mlflow_server, shared_mlflow_store  # noqa: E402,F401  (autouse: MLflow stays in temp folders)
import mlflow_runs  # noqa: E402  (sets the MLflow switches before mlflow loads)
import mlflow  # noqa: E402
import model_params  # noqa: E402
import training_data as D  # noqa: E402
import tune  # noqa: E402

optuna.logging.set_verbosity(optuna.logging.WARNING)
YEARS = ("2019", "2022", "2023")
QUIET = {"log": lambda *args, **kwargs: None}
SHORT = {"trials": "2", "trial_seeds": "0", "confirm_seeds": "10;11"}  # a quick study: one seed, two to confirm


def write_tuning(tmp_path: Path, **changes: str) -> Path:
    """A tuning.csv in tmp_path with the committed settings, study_dir in tmp_path, and changes by name
    (a value of None drops the setting)."""
    with open(tune.DEFAULT_TUNING, encoding="utf-8", newline="") as handle:
        settings = {row["setting"]: row["value"] for row in csv.DictReader(handle)}
    settings.update(study_dir=str(tmp_path / "tuning"))
    settings.update(changes)
    path = tmp_path / "tuning.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["setting", "value", "options", "note"])
        for name, value in settings.items():
            if value is not None:
                writer.writerow([name, value, "", ""])
    return path


def repo(tmp_path: Path, **changes: str) -> Path:
    """A temp repo whose labels start in 2016, so the 2019 inner fold has train days before it."""
    return H.write_repo(tmp_path, label_frame=H.make_labels(first="2016-01-01"), **changes)


def body(path: Path) -> list[str]:
    """A tuned values file without its comment lines, which name the run's own folders."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]


def test_the_committed_tuning_settings_load_and_name_only_candidates():
    tuning = tune.load_tuning()
    assert set(tuning.models) <= set(D.load_settings().candidates)
    assert tuning.trials >= 1 and tuning.seed >= 0 and tuning.confirm_top >= 1
    assert tuning.trial_seeds and len(tuning.confirm_seeds) >= 2 and not set(tuning.trial_seeds) & set(tuning.confirm_seeds)
    assert tuning.guard_ratio >= 1 and tuning.stable_se > 0
    assert tuning.study_dir == D.ROOT / "Model-Development" / "tuning"


@pytest.mark.parametrize("changes, message", [
    ({"models": "logreg;forest"}, "models must name each"),
    ({"models": "logreg;logreg"}, "models must name each"),
    ({"models": " ; "}, "models needs at least one model"),
    ({"trials": "0"}, "trials must be a whole number of 1 or more"),
    ({"seed": "-1"}, "seed must be a whole number of 0 or more"),
    ({"trial_seeds": ""}, "trial_seeds needs at least 1 different"),
    ({"trial_seeds": "0;0"}, "trial_seeds needs at least 1 different"),
    ({"trial_seeds": "0;x"}, "trial_seeds must be a whole number of 0 or more"),
    ({"confirm_seeds": "10"}, "confirm_seeds needs at least 2 different"),
    ({"confirm_seeds": "2;10"}, r"confirm_seeds must be fresh, but \[2\] are also trial_seeds"),
    ({"confirm_top": "0"}, "confirm_top must be a whole number of 1 or more"),
    ({"guard_ratio": "0.99"}, "guard_ratio must be a number of 1 or more"),
    ({"guard_ratio": "a lot"}, "guard_ratio must be a number"),
    ({"stable_se": "0"}, "stable_se must be a number above 0"),
    ({"study_dir": ""}, "study_dir needs a path"),
    ({"mlflow_experiment": ""}, "mlflow_experiment needs a name"),
    ({"seed": None}, "missing setting"),
    ({"pick_margin": "0.01"}, "unknown setting"),  # the Run 2 rule is gone
])
def test_a_bad_tuning_setting_is_refused_by_name(tmp_path, changes, message):
    with pytest.raises(D.SettingsError, match=message):
        tune.load_tuning(write_tuning(tmp_path, **changes))


@pytest.mark.parametrize("name", sorted(tune.SPACES))
def test_a_search_space_names_only_tunable_parameters_never_the_tree_count(name):
    written = tune.model_values(name, {key: d.low for key, d in tune.SPACES[name].items()})
    assert set(written) <= set(model_params.PARAMETERS[name])
    assert not {"max_rounds", "stopping_rounds", "n_estimators", "trees", "seed"} & set(written)


def test_the_tree_spaces_are_the_reviewed_ranges_in_their_drawing_order():
    # the sampler draws the settings in this order, so a reordered space changes every trial
    assert list(tune.SPACES["lightgbm"]) == ["learning_rate", "num_leaves", "min_child_samples", "reg_lambda",
                                             "subsample", "colsample_bytree"]
    assert list(tune.SPACES["xgboost"]) == ["learning_rate", "max_depth", "min_child_weight", "reg_lambda",
                                            "subsample", "colsample_bytree"]
    lightgbm = tune.SPACES["lightgbm"]
    assert lightgbm["learning_rate"] == tune.Float(0.01, 0.15, log=True)
    assert lightgbm["num_leaves"] == tune.Int(4, 63, log=True) and lightgbm["min_child_samples"] == tune.Int(20, 200, log=True)
    assert lightgbm["reg_lambda"] == tune.Float(0.01, 20.0, log=True)
    assert lightgbm["subsample"] == tune.Float(0.65, 1.0) and lightgbm["colsample_bytree"] == tune.Float(0.6, 1.0)


@pytest.mark.parametrize("name", sorted(tune.SPACES))
def test_every_value_a_space_can_suggest_passes_the_parameter_checks(tmp_path, name):
    study = optuna.create_study(sampler=optuna.samplers.RandomSampler(seed=1))
    lows = {key: d.low for key, d in tune.SPACES[name].items()}
    highs = {key: d.high for key, d in tune.SPACES[name].items()}
    for edge in (lows, highs):
        study.enqueue_trial(edge)
    for _ in range(25):
        trial = study.ask()
        params = {key: tune._suggest(trial, key, d) for key, d in tune.SPACES[name].items()}
        path = tmp_path / f"{trial.number}.yaml"
        tune.write_tuned(path, {name: tune.model_values(name, params)}, ["a test"])
        assert model_params.load_tuned(path)[name]  # every value passes its model_params.py check
        study.tell(trial, 0.0)


def test_the_starting_values_lie_inside_the_search_spaces():
    starting = model_params.load(D.load_settings().model_params)
    for name, space in tune.SPACES.items():
        for key, distribution in space.items():
            values = starting[name]["C_grid"] if key == "C" else [starting[name][key]]
            assert all(distribution.low <= value <= distribution.high for value in values), (name, key)


def test_a_candidate_with_a_seed_is_fitted_with_each_trial_seed_and_logreg_once():
    tuning = tune.load_tuning()
    assert tune.fit_seeds("lightgbm", tuning) == tuning.trial_seeds
    assert tune.fit_seeds("xgboost", tuning, confirm=True) == tuning.confirm_seeds
    assert tune.fit_seeds("logreg", tuning) == tune.fit_seeds("logreg", tuning, confirm=True) == (None,)


def fit(fold_f1, log_loss: float = 0.11, forward=(0.2, 0.3)) -> tune.Fit:
    """A fit with the given F1 on each fold year."""
    return tune.Fit(log_loss=log_loss, log_loss_by_fold={year: log_loss for year in YEARS},
                    folds={year: {"f1": f1, "precision": f1, "recall": f1, "threshold": 0.1, "pr_auc": f1 / 2}
                           for year, f1 in zip(YEARS, fold_f1)},
                    forward=dict(zip(YEARS[1:], forward)), chosen={"trees": 10}, seconds=1.0)


def test_a_fit_and_a_trial_average_their_folds_and_seeds():
    a, b = fit([0.2, 0.3, 0.4], log_loss=0.10, forward=(0.1, 0.3)), fit([0.4, 0.5, 0.6], log_loss=0.12, forward=(0.3, 0.5))
    assert a.f1 == pytest.approx(0.3) and a.forward_f1 == pytest.approx(0.2) and a.pr_auc == pytest.approx(0.15)
    scores = tune.means([a, b])
    assert scores["f1"] == pytest.approx(0.4) and scores["log_loss"] == pytest.approx(0.11)
    assert scores["forward_f1"] == pytest.approx(0.3) and scores["precision"] == pytest.approx(0.4)
    assert scores["fold_f1"] == pytest.approx({"2019": 0.3, "2022": 0.4, "2023": 0.5})
    assert scores["f1_sd"] == pytest.approx(np.std([0.3, 0.5], ddof=1)) and scores["seconds"] == 2.0
    assert tune.means([a])["f1_sd"] == 0.0
    assert dataclasses.replace(a, forward={}).forward_f1 == 0.0  # one fold year: nothing after it


def finished_study(name: str, trials: list[tuple[dict, float, float]]) -> optuna.Study:
    """A study of candidate name with trials given as (params, fold F1, log loss)."""
    study = optuna.create_study(direction="maximize")
    study.add_trials([optuna.trial.create_trial(params=params, distributions=tune.SPACES[name], value=f1,
                                                user_attrs={"scores": {"log_loss": loss}}) for params, f1, loss in trials])
    return study


def fixed_fits(by_setting: dict, key):
    """tune.score replaced: a setting, known by key(values), gets one fit per seed with the n-th of its
    given F1 values on every fold year, all at its given log loss."""
    def score(name, values, seeds, rows, inputs, settings):
        f1s, loss = by_setting[key(values)]
        return [fit([value] * 3, log_loss=loss) for value, _ in zip(f1s, seeds)]
    return score


@pytest.fixture
def confirming(tmp_path):
    """Settings on a temp repo, the committed tuning settings and the LightGBM starting values."""
    settings = D.load_settings(repo(tmp_path))
    starting = model_params.load(settings.model_params)
    start = {key: starting["lightgbm"][key] for key in tune.SPACES["lightgbm"]}
    return settings, tune.load_tuning(write_tuning(tmp_path)), starting, start


def confirm_lightgbm(monkeypatch, confirming, trials, fits, **changes):
    settings, tuning, starting, start = confirming
    study = finished_study("lightgbm", [({**start, "learning_rate": rate}, value, 0.11) for rate, value in trials])
    monkeypatch.setattr(tune, "score", fixed_fits(fits, lambda values: values["learning_rate"]))
    return tune.confirm("lightgbm", study, None, [], settings, dataclasses.replace(tuning, **changes),
                        starting["lightgbm"], **QUIET)


BASE = ([0.30] * 5, 0.110)  # trial 0, the starting values, on the five confirm seeds


def test_a_gain_well_above_its_seed_noise_is_an_improvement(monkeypatch, confirming):
    found = confirm_lightgbm(monkeypatch, confirming, [(0.05, 0.30), (0.02, 0.33)],
                             {0.05: BASE, 0.02: ([0.32, 0.32, 0.32, 0.32, 0.33], 0.110)})
    assert found["candidate"] == 1 and found["improvement"] and found["decision"].startswith("improvement")
    assert found["pairs"] == "confirm_seeds" and found["seeds"] == [10, 11, 12, 13, 14]
    pool = {entry["trial"]: entry for entry in found["pool"]}
    assert pool[1]["gain"] == pytest.approx(0.022) and pool[1]["gain_se"] == pytest.approx(np.std([0.02] * 4 + [0.03], ddof=1) / 5 ** 0.5)
    assert [len(entry["fits"]) for entry in found["pool"]] == [5, 5]


def test_a_gain_inside_its_seed_noise_keeps_the_starting_values(monkeypatch, confirming):
    found = confirm_lightgbm(monkeypatch, confirming, [(0.05, 0.30), (0.02, 0.35)],
                             {0.05: BASE, 0.02: ([0.33, 0.28, 0.32, 0.29, 0.31], 0.110)})
    assert found["candidate"] == 1 and not found["improvement"]  # gain 0.006, two standard errors 0.019
    assert found["decision"] == "starting values kept: the candidate's gain is within stable_se standard errors"


def test_a_setting_over_the_log_loss_guard_is_not_eligible(monkeypatch, confirming):
    found = confirm_lightgbm(monkeypatch, confirming, [(0.05, 0.30), (0.02, 0.40), (0.03, 0.35)],
                             {0.05: BASE, 0.02: ([0.40] * 5, 0.111), 0.03: ([0.33] * 5, 0.1105)})
    pool = {entry["trial"]: entry for entry in found["pool"]}
    assert not pool[1]["eligible"] and pool[2]["eligible"]  # 0.111 is over 1.005 times 0.110; 0.1105 is not
    assert found["candidate"] == 2


def test_trial_0_can_rank_first_and_then_nothing_is_tuned(monkeypatch, confirming):
    found = confirm_lightgbm(monkeypatch, confirming, [(0.05, 0.30), (0.02, 0.31)],
                             {0.05: BASE, 0.02: ([0.29] * 5, 0.110)})
    assert found["candidate"] == 0 and not found["improvement"]
    assert found["decision"] == "starting values kept: trial 0 ranked first"


def test_the_pool_is_trial_0_and_the_best_other_trials_without_repeats(monkeypatch, confirming):
    trials = [(0.05, 0.30), (0.02, 0.33), (0.05, 0.36), (0.03, 0.35), (0.04, 0.34)]  # trial 2 repeats trial 0
    fits = {rate: BASE for rate in (0.02, 0.03, 0.04, 0.05)}
    found = confirm_lightgbm(monkeypatch, confirming, trials, fits, confirm_top=2)
    assert [entry["trial"] for entry in found["pool"]] == [0, 3, 4]


def test_logreg_pairs_the_fold_years_since_it_has_no_seed(monkeypatch, confirming):
    settings, tuning, starting, _ = confirming
    study = finished_study("logreg", [({"C": 1.0, "year_parts": 12}, 0.25, 0.2), ({"C": 0.1, "year_parts": 12}, 0.28, 0.2),
                                      ({"C": 10.0, "year_parts": 12}, 0.27, 0.2)])
    folds = {1.0: [0.20, 0.25, 0.30], 0.1: [0.23, 0.28, 0.33], 10.0: [0.30, 0.20, 0.31]}

    def score(name, values, seeds, rows, inputs, settings):
        assert seeds == (None,)
        return [fit(folds[values["C_grid"][0]], log_loss=0.2)]
    monkeypatch.setattr(tune, "score", score)
    found = tune.confirm("logreg", study, None, [], settings, tuning, starting["logreg"], **QUIET)
    pool = {entry["trial"]: entry for entry in found["pool"]}
    assert found["pairs"].startswith("fold years") and found["seeds"] == []
    assert found["candidate"] == 1 and found["improvement"]  # +0.03 on every fold year
    assert not pool[2]["improvement"]  # +0.10, -0.05, +0.01: within two standard errors


def test_a_tuned_entry_holds_only_the_changed_values():
    starting = model_params.load(D.load_settings().model_params)
    start = {key: starting["lightgbm"][key] for key in tune.SPACES["lightgbm"]}
    assert tune.tuned_entry("lightgbm", start, starting["lightgbm"]) == {}
    assert tune.tuned_entry("lightgbm", {**start, "num_leaves": 31}, starting["lightgbm"]) == {"num_leaves": 31}
    logreg = {"C": 0.25, "year_parts": starting["logreg"]["year_parts"]}
    assert tune.tuned_entry("logreg", logreg, starting["logreg"]) == {"C_grid": [0.25]}  # one C fixes C


def test_the_tuned_file_is_block_yaml_that_models_build_accepts(tmp_path):
    path = tmp_path / "tuned_params.yaml"
    tune.write_tuned(path, {"lightgbm": {"learning_rate": 0.0123456789, "num_leaves": 31},
                            "logreg": {"C_grid": [0.001]}}, ["first line", "second line"])
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# first line\n# second line\n")
    assert "{" not in text and "e-" not in text  # block style; decimals with a point (model_params.py)
    settings = dataclasses.replace(D.load_settings(), tuned_params=path)
    resolved = model_params.resolve(settings.model_params, settings.tuned_params)
    assert resolved.values["lightgbm"]["learning_rate"] == 0.0123456789
    assert resolved.tuned == {"lightgbm": ["learning_rate", "num_leaves"], "logreg": ["C_grid"]}

    tune.write_tuned(path, {}, ["nothing beat the starting values"])
    assert model_params.load_tuned(path) == {}  # a comment-only file sets nothing


def test_the_committed_tuned_file_sets_nothing_so_the_starting_values_train():
    settings = D.load_settings()
    assert model_params.load_tuned(settings.tuned_params) == {}
    resolved = model_params.resolve(settings.model_params, settings.tuned_params)
    assert resolved.tuned == {} and resolved.values == model_params.load(settings.model_params)


def test_importance_is_blank_for_a_score_that_never_changed():
    study = finished_study("logreg", [({"C": 0.01, "year_parts": 6}, 0.2, 0.3), ({"C": 1.0, "year_parts": 20}, 0.2, 0.2),
                                      ({"C": 10.0, "year_parts": 12}, 0.2, 0.25)])
    shares = tune.importances(study, seed=0)
    assert set(shares) == {"C", "year_parts"}
    assert all(share["f1"] is None for share in shares.values())  # the fold F1 was 0.2 in every trial
    assert abs(sum(share["log_loss"] for share in shares.values()) - 1) < 1e-9
    assert tune.importances(finished_study("logreg", [({"C": 1.0, "year_parts": 6}, 0.2, 0.3)]), seed=0) == {}


@pytest.fixture(scope="module")
def two_trials(tmp_path_factory):
    """tune.py's main with two trials of each candidate on a temp repo with its own MLflow store."""
    tmp_path = tmp_path_factory.mktemp("tune")
    config = repo(tmp_path, mlflow_store=str(tmp_path / "mlruns"))
    tuning = write_tuning(tmp_path, **SHORT)
    with pytest.MonkeyPatch.context() as patch:  # no_mlflow_server is per test; this runs before it
        patch.delenv("MLFLOW_TRACKING_URI", raising=False)
        assert tune.main(["--config", str(config), "--tuning", str(tuning)]) == 0
    return tmp_path, D.load_settings(config), tune.load_tuning(tuning)


def test_a_run_writes_the_candidates_the_studies_and_the_importance_but_not_the_tuned_file(two_trials):
    _, settings, tuning = two_trials
    assert sorted(p.name for p in tuning.study_dir.iterdir()) == [
        "best_params.yaml", "importance.csv", "lightgbm.json", "logreg.json", "summary.json", "xgboost.json"]
    assert not settings.tuned_params.exists()  # without --activate the file models.build reads is not written
    candidates = model_params.load_tuned(tuning.study_dir / "best_params.yaml")
    summary = json.loads((tuning.study_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["models"] == ["logreg", "xgboost", "lightgbm"] and summary["trials"] == 2
    assert summary["trial_seeds"] == [0] and summary["confirm_seeds"] == [10, 11] and summary["activated"] is False
    assert summary["data_version"] == D.data_version(settings.dvc_lock)
    for name, result in summary["results"].items():
        confirmation = result["confirmation"]
        assert result["trials"] == 2 and confirmation["candidate_trial"] in (0, 1)
        assert (name in candidates) == bool(result["tuned_values"]) == confirmation["improvement"]
        assert confirmation["pairs"] == ("fold years (no seed: one fit per setting)" if name == "logreg" else "confirm_seeds")
    with open(tuning.study_dir / "importance.csv", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {(row["model"], row["setting"]) for row in rows} == {
        (name, key) for name, space in tune.SPACES.items() for key in space}


def test_trial_0_is_the_starting_values(two_trials):
    _, settings, tuning = two_trials
    starting = model_params.load(settings.model_params)
    for name in ("xgboost", "lightgbm"):
        first = json.loads((tuning.study_dir / f"{name}.json").read_text(encoding="utf-8"))["trials"][0]
        assert first["params"] == {key: starting[name][key] for key in tune.SPACES[name]}
        assert [f["seed"] for f in first["user_attrs"]["fits"]] == [0]
    first = json.loads((tuning.study_dir / "logreg.json").read_text(encoding="utf-8"))["trials"][0]
    assert first["params"]["C"] in starting["logreg"]["C_grid"]  # the C the starting grid picks
    assert first["params"]["year_parts"] == starting["logreg"]["year_parts"]


def test_a_saved_study_rebuilds_with_its_scores_for_m10(two_trials):
    _, settings, tuning = two_trials
    record = json.loads((tuning.study_dir / "lightgbm.json").read_text(encoding="utf-8"))
    study = tune.load_study(tuning.study_dir / "lightgbm.json")
    assert study.direction == optuna.study.StudyDirection.MAXIMIZE
    assert [t.value for t in study.trials] == [t["value"] for t in record["trials"]]
    assert all({"scores", "fits"} <= set(t.user_attrs) for t in study.trials)
    assert all(t.value == pytest.approx(t.user_attrs["scores"]["f1"]) for t in study.trials)
    assert record["objective"].startswith("mean fold F1") and record["seed"] == 0
    first = record["trials"][0]["user_attrs"]["fits"][0]
    assert set(first["folds"]) == {str(year) for year in settings.inner_fold_years}
    assert set(first["forward"]) == {str(year) for year in settings.inner_fold_years[1:]}
    assert {entry["trial"] for entry in record["confirmation"]["pool"]} == {0, 1}


def test_every_study_trial_and_confirmed_setting_is_an_mlflow_run(two_trials):
    _, settings, tuning = two_trials
    client = mlflow.MlflowClient(mlflow_runs.tracking_uri(settings))
    experiment = client.get_experiment_by_name(tuning.mlflow_experiment)
    runs = client.search_runs([experiment.experiment_id])
    parents = {run.info.run_name: run for run in runs if "mlflow.parentRunId" not in run.data.tags}
    assert sorted(parents) == ["tune-lightgbm", "tune-logreg", "tune-summary", "tune-xgboost"]
    for name in ("lightgbm", "logreg", "xgboost"):
        parent = parents[f"tune-{name}"]
        children = [run for run in runs if run.data.tags.get("mlflow.parentRunId") == parent.info.run_id]
        trials = [run for run in children if "-confirm-" not in run.info.run_name]
        confirmed = [run for run in children if "-confirm-" in run.info.run_name]
        assert len(trials) == 2 and len(confirmed) == 2
        assert all({"fold_f1", "inner_log_loss", "fold_f1_2019", "pr_auc"} <= set(run.data.metrics) for run in trials)
        assert all({"gain", "gain_se", "eligible", "improvement"} <= set(run.data.metrics) for run in confirmed)
        assert {"trial_0_fold_f1", "candidate_fold_f1", "candidate_gain", "improvement"} <= set(parent.data.metrics)
        assert any(key.startswith("importance_log_loss_") for key in parent.data.metrics)
        assert parent.data.tags["decision"]
    files = {item.path for item in client.list_artifacts(parents["tune-summary"].info.run_id)}
    assert files == {"best_params.yaml", "importance.csv", "summary.json"}


def test_without_activate_the_tuned_file_is_left_as_it_is_and_with_it_gets_the_candidates(tmp_path):
    config = repo(tmp_path)
    tuned = tmp_path / "tuned_params.yaml"
    tuned.write_text("# the starting values stay: nothing is tuned\n", encoding="utf-8")
    before = tuned.read_bytes()
    tuning = write_tuning(tmp_path, **SHORT)
    arguments = ["--config", str(config), "--tuning", str(tuning), "--models", "lightgbm"]
    assert tune.main(arguments) == 0
    assert tuned.read_bytes() == before
    assert tune.main([*arguments, "--activate"]) == 0
    assert tuned.read_text(encoding="utf-8").startswith("# Activated by tune.py --activate (#109)")
    assert body(tuned) == body(tmp_path / "tuning" / "best_params.yaml")


def test_activate_copies_an_improvement_into_the_tuned_file(tmp_path, monkeypatch):
    real = tune.confirm

    def improved(*args, **kwargs):  # trial 1 made an improvement, so its values are the candidate
        found = real(*args, **kwargs)
        return {**found, "candidate": found["pool"][1]["trial"], "improvement": True, "decision": "improvement: a test"}
    monkeypatch.setattr(tune, "confirm", improved)
    settings = D.load_settings(repo(tmp_path))
    tuning = tune.load_tuning(write_tuning(tmp_path, **SHORT))
    summary = tune.tune(settings, tuning, ("lightgbm",), activate=True, **QUIET)
    entry = summary["results"]["lightgbm"]["tuned_values"]
    assert entry and summary["activated"]
    assert model_params.load_tuned(settings.tuned_params) == {"lightgbm": entry}
    assert model_params.load_tuned(tuning.study_dir / "best_params.yaml") == {"lightgbm": entry}
    assert model_params.resolve(settings.model_params, settings.tuned_params).tuned == {"lightgbm": sorted(entry)}


def test_no_validation_or_test_row_reaches_a_trial(tmp_path, monkeypatch):
    seen = []
    real = tune.evaluate

    def spy(name, values, rows, *args):
        seen.append((set(rows["split"]), rows["date"].max()))
        return real(name, values, rows, *args)

    monkeypatch.setattr(tune, "evaluate", spy)
    config = repo(tmp_path)
    assert tune.main(["--config", str(config), "--tuning", str(write_tuning(tmp_path, **SHORT)),
                      "--models", "logreg;lightgbm"]) == 0
    assert seen and all(splits == {"train"} for splits, _ in seen)
    assert max(last for _, last in seen) <= D.load_settings(config).splits["train"][1]


def test_the_same_seed_gives_the_same_trials_confirmation_and_candidates(tmp_path):
    outputs = []
    for run in ("a", "b"):
        folder = tmp_path / run
        config = repo(folder)
        assert tune.main(["--config", str(config), "--tuning", str(write_tuning(folder, **{**SHORT, "trials": "3"})),
                          "--models", "lightgbm"]) == 0
        record = json.loads((folder / "tuning" / "lightgbm.json").read_text(encoding="utf-8"))
        confirmation = [(e["trial"], e["scores"]["f1"]) for e in record["confirmation"]["pool"]]
        outputs.append(([(t["params"], t["value"]) for t in record["trials"]], confirmation,
                        body(folder / "tuning" / "best_params.yaml")))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("arguments, message", [
    (["--models", "forest"], "not in tuning.csv's models"),
    (["--trials", "0"], "--trials must be 1 or more"),
])
def test_main_names_a_bad_argument_and_exits_2(tmp_path, capsys, arguments, message):
    config = repo(tmp_path)
    assert tune.main(["--config", str(config), "--tuning", str(write_tuning(tmp_path)), *arguments]) == 2
    assert message in capsys.readouterr().err
    assert not (tmp_path / "tuned_params.yaml").exists() and not (tmp_path / "tuning" / "best_params.yaml").exists()


def test_main_names_a_bad_tuning_file_and_exits_2(tmp_path, capsys):
    config = repo(tmp_path)
    assert tune.main(["--config", str(config), "--tuning", str(write_tuning(tmp_path, trials="none"))]) == 2
    assert "trials must be a whole number" in capsys.readouterr().err
