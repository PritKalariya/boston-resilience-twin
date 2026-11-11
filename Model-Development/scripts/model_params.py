"""The candidate models' parameters (#107): the starting values in model_params.yaml, with M6's
tuned values (#109) from tuned_params.yaml over them when that file exists.

    python Model-Development/scripts/model_params.py [--config Model-Development/config/training.csv]

checks both files (their paths are in training.csv), prints every model's parameters with the
tuned ones marked, and exits 0, or 2 naming the problem.

Both files map each candidate to its parameters, for example

    logreg:
      C_grid: [0.001, 0.01, 0.1, 1.0]
      max_iter: 2000
      year_parts: 12

PARAMETERS lists every model and parameter they may hold, with the check each value must pass.
- model_params.yaml must name every model and every parameter.
- tuned_params.yaml may name only some. Each value it gives replaces the starting one; the rest
  keep their starting values. An empty file sets nothing.
- The number of trees is not a parameter: it always comes from the inner-fold search
  (inner_folds.stop_early). Tune learning_rate and the tree settings instead. A C_grid of one
  value fixes logistic regression's C.

Values are read with yaml.safe_load, so the files can hold data only, never code. A run records
which values came from tuned_params.yaml and that file's sha256 (with LF line endings, as the
data version), so the exact file can be traced.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

import training_data


class ParamsError(ValueError):
    """A parameters file that cannot be used."""


def _number(name: str, value, kind: str = "a number") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        hint = "; write decimals with a point (0.001, not 1e-3), YAML reads 1e-3 as text" if isinstance(value, str) else ""
        raise ParamsError(f"{name} must be {kind}, got {value!r}{hint}")
    return float(value)


def _fraction(name: str, value) -> float:
    number = _number(name, value)
    if not 0 < number <= 1:
        raise ParamsError(f"{name} must be above 0 and at most 1, got {value!r}")
    return number


def _not_negative(name: str, value) -> float:
    number = _number(name, value)
    if number < 0:
        raise ParamsError(f"{name} must be 0 or more, got {value!r}")
    return number


def _increasing_positive(name: str, value) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ParamsError(f"{name} must be a list of one or more numbers, got {value!r}")
    numbers = [_number(name, item, "a list of numbers") for item in value]
    if min(numbers) <= 0:
        raise ParamsError(f"{name} must hold numbers above 0, got {value!r}")
    if numbers != sorted(set(numbers)):
        raise ParamsError(f"{name} must increase with no value twice, got {value!r}")
    return tuple(numbers)


def _whole(minimum: int):
    def check(name: str, value) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ParamsError(f"{name} must be a whole number of {minimum} or more, got {value!r}")
        return value
    return check


PARAMETERS = {
    # year_parts + 1 knots, and a periodic cubic spline needs more than 3 knots: at least 3 parts
    "logreg": {"C_grid": _increasing_positive, "max_iter": _whole(1), "year_parts": _whole(3)},
    "xgboost": {"learning_rate": _fraction, "max_rounds": _whole(1), "stopping_rounds": _whole(1),
                "max_depth": _whole(1), "min_child_weight": _not_negative, "reg_lambda": _not_negative,
                "subsample": _fraction, "colsample_bytree": _fraction, "seed": _whole(0)},
    "lightgbm": {"learning_rate": _fraction, "max_rounds": _whole(1), "stopping_rounds": _whole(1),
                 "num_leaves": _whole(2), "min_child_samples": _whole(1), "reg_lambda": _not_negative,
                 "subsample": _fraction, "subsample_freq": _whole(0), "colsample_bytree": _fraction,
                 "seed": _whole(0)},
}


def _checked(path: Path, complete: bool) -> dict[str, dict]:
    """The checked parameters in path, as {model: {parameter: value}}. complete: every model and
    parameter must be there (the starting values); otherwise any of them may be (the tuned ones)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ParamsError(f"{path.name} is not valid YAML: {exc}") from None
    if data is None and not complete:
        return {}
    if not isinstance(data, dict):
        raise ParamsError(f"{path.name} must map each model to its parameters")
    unknown = sorted(set(data) - set(PARAMETERS))
    if unknown:
        raise ParamsError(f"unknown model(s) in {path.name}: {unknown}; known: {sorted(PARAMETERS)}")
    missing = sorted(set(PARAMETERS) - set(data))
    if missing and complete:
        raise ParamsError(f"{path.name} has no parameters for {missing}")

    params = {}
    for model, checks in PARAMETERS.items():
        if model not in data:
            continue
        given = data[model]
        if not isinstance(given, dict):
            raise ParamsError(f"{model} in {path.name} must map each parameter to its value")
        unknown = sorted(set(given) - set(checks))
        if unknown:
            raise ParamsError(f"unknown parameter(s) for {model} in {path.name}: {unknown}; known: {sorted(checks)}")
        missing = [name for name in checks if name not in given]
        if missing and complete:
            raise ParamsError(f"{model} in {path.name} is missing {missing}")
        params[model] = {name: check(f"{model}.{name}", given[name]) for name, check in checks.items() if name in given}
    return params


def load(path: str | Path) -> dict[str, dict]:
    """Every model's checked starting values from path (model_params.yaml), as {model: {parameter: value}}.

    Fails, naming the model or parameter, on invalid YAML, an unknown or missing model or
    parameter, or a value that fails its check.
    """
    return _checked(Path(path), complete=True)


def load_tuned(path: str | Path) -> dict[str, dict]:
    """The checked tuned values in path (tuned_params.yaml): any models and parameters, each value
    passing the same check as a starting value. M6's own tests can call this on its file."""
    return _checked(Path(path), complete=False)


@dataclass(frozen=True)
class Resolved:
    values: dict[str, dict]  # model -> parameter -> the value a run uses
    tuned: dict[str, list[str]]  # model -> the parameters tuned_params.yaml set, sorted
    tuned_sha256: str | None  # of tuned_params.yaml with LF line endings; None when the file is absent

    def record(self, model: str) -> dict:
        """What a run of model records: its values, the ones tuned and the tuned file's sha256."""
        return {"values": self.values[model], "tuned": self.tuned.get(model, []), "tuned_sha256": self.tuned_sha256}


def resolve(defaults: str | Path, tuned: str | Path) -> Resolved:
    """The starting values in defaults with the tuned values over them when the tuned file exists."""
    values = load(defaults)
    tuned = Path(tuned)
    if not tuned.exists():
        return Resolved(values, {}, None)
    tuned_values = load_tuned(tuned)
    for model, given in tuned_values.items():
        values[model].update(given)
    sha256 = "sha256:" + hashlib.sha256(tuned.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return Resolved(values, {model: sorted(given) for model, given in tuned_values.items() if given}, sha256)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the candidates' parameter files and print the parameters (#107).")
    parser.add_argument("--config", default=str(training_data.DEFAULT_CONFIG), help="training.csv path")
    args = parser.parse_args(argv)
    try:
        settings = training_data.load_settings(args.config)
        resolved = resolve(settings.model_params, settings.tuned_params)
    except (training_data.SettingsError, ParamsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"starting values: {settings.model_params.as_posix()}")
    if resolved.tuned_sha256:
        print(f"tuned values: {settings.tuned_params.as_posix()} ({resolved.tuned_sha256})")
    else:
        print(f"tuned values: none ({settings.tuned_params.as_posix()} does not exist)")
    for model, values in resolved.values.items():
        print(f"{model}:")
        for name, value in values.items():
            mark = "  (tuned)" if name in resolved.tuned.get(model, []) else ""
            print(f"  {name}: {list(value) if isinstance(value, tuple) else value}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
