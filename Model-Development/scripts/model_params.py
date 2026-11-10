"""The candidate models' parameters (#107): Model-Development/config/model_params.yaml.

The file maps each candidate to its parameters, for example

    logreg:
      C_grid: [0.001, 0.01, 0.1, 1.0]
      max_iter: 2000
      year_parts: 12

PARAMETERS lists every model and parameter the file may hold, with the check each value must
pass. The file must name every model and every parameter. M6 (#109) writes tuned_params.yaml in
the same format.

Values are read with yaml.safe_load, so the file can hold data only, never code.
"""

from __future__ import annotations

import math
from pathlib import Path

import yaml


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
    "logreg": {"C_grid": _increasing_positive, "max_iter": _whole(1), "year_parts": _whole(2)},
    "xgboost": {"learning_rate": _fraction, "max_rounds": _whole(1), "stopping_rounds": _whole(1),
                "max_depth": _whole(1), "min_child_weight": _not_negative, "reg_lambda": _not_negative,
                "subsample": _fraction, "colsample_bytree": _fraction, "seed": _whole(0)},
}


def load(path: str | Path) -> dict[str, dict]:
    """Every model's checked parameters from path, as {model: {parameter: value}}.

    Fails, naming the model or parameter, on invalid YAML, an unknown or missing model or
    parameter, or a value that fails its check.
    """
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ParamsError(f"{path.name} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ParamsError(f"{path.name} must map each model to its parameters")
    unknown = sorted(set(data) - set(PARAMETERS))
    if unknown:
        raise ParamsError(f"unknown model(s) in {path.name}: {unknown}; known: {sorted(PARAMETERS)}")
    missing = sorted(set(PARAMETERS) - set(data))
    if missing:
        raise ParamsError(f"{path.name} has no parameters for {missing}")

    params = {}
    for model, checks in PARAMETERS.items():
        given = data[model]
        if not isinstance(given, dict):
            raise ParamsError(f"{model} in {path.name} must map each parameter to its value")
        unknown = sorted(set(given) - set(checks))
        if unknown:
            raise ParamsError(f"unknown parameter(s) for {model} in {path.name}: {unknown}; known: {sorted(checks)}")
        missing = [name for name in checks if name not in given]
        if missing:
            raise ParamsError(f"{model} in {path.name} is missing {missing}")
        params[model] = {name: check(f"{model}.{name}", given[name]) for name, check in checks.items()}
    return params
