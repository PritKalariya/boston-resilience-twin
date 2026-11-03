"""Promotion gates for the model CI/CD pipeline (#112, ADR-026).

Freezes the gate thresholds and the frozen ACS poverty groups, loads them with
validation, and turns a candidate's validation metrics into one of four
decisions: promote, keep_champion, blocked or error.

Standard library only (ADR-013). Every gate comparison is made on
fractions.Fraction values parsed from the config strings; floats appear only in
the display report, rounded to 6 places. The module computes no model metrics:
it is the shared interface #106, #110 and #111 read from.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from statistics import median

# Model-Development/scripts/gates.py -> parents[2] is the repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "gates.csv"

UNGROUPED = "ungrouped"

# The gates file uses this fixed header. It is deliberately not processing.csv's
# header (setting,value,options,note): load_gates rejects any other header.
GATES_HEADER = ["key", "value", "description"]

# The 18 gate settings, in order. Each maps to a validator of its string value.
# A validator returns the parsed value or raises ValueError naming the setting.
GATE_ORDER = (
    "f1_split",
    "f1_metric",
    "f1_baseline",
    "f1_min_margin",
    "threshold_rule",
    "fairness_metric",
    "fairness_max_gap",
    "fairness_groups",
    "fairness_group_source",
    "fairness_group_sha256",
    "fairness_min_group_surges",
    "fairness_small_group_policy",
    "fairness_ungrouped_policy",
    "fairness_min_groups",
    "champion_metric",
    "champion_min_delta",
    "champion_rescore",
    "on_undefined",
)


class ConfigError(ValueError):
    """A gates.csv or poverty_groups.csv value that cannot be loaded."""


def _one_of(name: str, value: str, allowed: set[str]) -> str:
    if value not in allowed:
        raise ConfigError(f"gate {name!r}: {value!r} is not one of {sorted(allowed)}")
    return value


def _fraction(name: str, value: str) -> Fraction:
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise ConfigError(f"gate {name!r}: {value!r} is not a number") from exc


def _unit_fraction(name: str, value: str) -> Fraction:
    parsed = _fraction(name, value)
    if not 0 <= parsed <= 1:
        raise ConfigError(f"gate {name!r}: {value!r} is outside 0..1")
    return parsed


def _nonneg_fraction(name: str, value: str) -> Fraction:
    parsed = _fraction(name, value)
    if parsed < 0:
        raise ConfigError(f"gate {name!r}: {value!r} is negative")
    return parsed


def _count(name: str, value: str, minimum: int) -> int:
    parsed = _fraction(name, value)
    if parsed.denominator != 1 or parsed < minimum:
        raise ConfigError(f"gate {name!r}: {value!r} is not an integer at least {minimum}")
    return int(parsed)


def _groups(name: str, value: str) -> tuple[str, ...]:
    names = tuple(part.strip() for part in value.split(";") if part.strip())
    if not names:
        raise ConfigError(f"gate {name!r}: no group names")
    if len(set(names)) != len(names):
        raise ConfigError(f"gate {name!r}: duplicate group name in {value!r}")
    if UNGROUPED in names:
        raise ConfigError(f"gate {name!r}: {UNGROUPED!r} is not a gated group")
    return names


def _sha256_field(name: str, value: str) -> str:
    digest = value.strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ConfigError(f"gate {name!r}: {value!r} is not a sha256 digest")
    return digest


@dataclass(frozen=True)
class Gates:
    f1_split: str
    f1_metric: str
    f1_baseline: str
    f1_min_margin: Fraction
    threshold_rule: str
    fairness_metric: str
    fairness_max_gap: Fraction
    fairness_groups: tuple[str, ...]
    fairness_group_source: str
    fairness_group_sha256: str
    fairness_min_group_surges: int
    fairness_small_group_policy: str
    fairness_ungrouped_policy: str
    fairness_min_groups: int
    champion_metric: str
    champion_min_delta: Fraction
    champion_rescore: str
    on_undefined: str
    gates_sha256: str


def _read_pairs(path: Path) -> dict[str, str]:
    """The name,value pairs of a processing.csv-style file: a header row, then one
    setting per row. Fails on a duplicate or malformed row, naming the setting."""
    text = path.read_text(encoding="utf-8")
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        raise ConfigError(f"{path.name} is empty")
    if rows[0] != GATES_HEADER:
        raise ConfigError(
            f"{path.name} header {rows[0]} is not {GATES_HEADER} "
            "(the gates header is fixed and differs from processing.csv by design)"
        )
    pairs: dict[str, str] = {}
    for row in rows[1:]:  # row 0 is the header
        if not row or not row[0].strip():
            continue
        if len(row) < 2:
            raise ConfigError(f"{path.name}: row {row[0]!r} has no value")
        name = row[0].strip()
        if name in pairs:
            raise ConfigError(f"gate {name!r}: duplicated in {path.name}")
        pairs[name] = row[1].strip()
    return pairs


def load_gates(path: str | Path = DEFAULT_CONFIG) -> Gates:
    """Load and validate gates.csv and its referenced poverty groups file.

    Fails, naming the problem, on a missing, unknown or duplicate setting, a bad
    type or a value outside its allowed set or range, and on a groups file whose
    digest, group labels, uniqueness or median rule do not hold.
    """
    path = Path(path)
    raw = path.read_bytes()
    pairs = _read_pairs(path)

    unknown = sorted(set(pairs) - set(GATE_ORDER))
    if unknown:
        raise ConfigError(f"unknown gate(s): {unknown}")
    missing = [name for name in GATE_ORDER if name not in pairs]
    if missing:
        raise ConfigError(f"missing gate(s): {missing}")

    gates = Gates(
        f1_split=_one_of("f1_split", pairs["f1_split"], {"validation"}),
        f1_metric=_one_of("f1_metric", pairs["f1_metric"], {"surge_class_f1"}),
        f1_baseline=_one_of("f1_baseline", pairs["f1_baseline"], {"seasonal"}),
        f1_min_margin=_unit_fraction("f1_min_margin", pairs["f1_min_margin"]),
        threshold_rule=_one_of("threshold_rule", pairs["threshold_rule"], {"shared"}),
        fairness_metric=_one_of(
            "fairness_metric", pairs["fairness_metric"], {"worst_group_recall_gap"}
        ),
        fairness_max_gap=_unit_fraction("fairness_max_gap", pairs["fairness_max_gap"]),
        fairness_groups=_groups("fairness_groups", pairs["fairness_groups"]),
        fairness_group_source=pairs["fairness_group_source"].strip(),
        fairness_group_sha256=_sha256_field(
            "fairness_group_sha256", pairs["fairness_group_sha256"]
        ),
        fairness_min_group_surges=_count(
            "fairness_min_group_surges", pairs["fairness_min_group_surges"], 1
        ),
        fairness_small_group_policy=_one_of(
            "fairness_small_group_policy", pairs["fairness_small_group_policy"], {"pool"}
        ),
        fairness_ungrouped_policy=_one_of(
            "fairness_ungrouped_policy",
            pairs["fairness_ungrouped_policy"],
            {"exclude_and_report"},
        ),
        fairness_min_groups=_count("fairness_min_groups", pairs["fairness_min_groups"], 2),
        champion_metric=_one_of(
            "champion_metric", pairs["champion_metric"], {"surge_class_f1"}
        ),
        champion_min_delta=_nonneg_fraction(
            "champion_min_delta", pairs["champion_min_delta"]
        ),
        champion_rescore=_one_of(
            "champion_rescore", pairs["champion_rescore"], {"challenger_data_version"}
        ),
        on_undefined=_one_of("on_undefined", pairs["on_undefined"], {"fail"}),
        gates_sha256=hashlib.sha256(raw).hexdigest(),
    )
    _check_groups_file(gates)
    return gates


def _check_groups_file(gates: Gates) -> None:
    """Validate the poverty groups file against the gates settings: its digest,
    its group labels, neighborhood uniqueness and the median rule from ADR-026."""
    source = (REPO_ROOT / gates.fairness_group_source).resolve()
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ConfigError(
            f"fairness_group_source {gates.fairness_group_source!r} cannot be read"
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != gates.fairness_group_sha256:
        raise ConfigError(
            "poverty groups file does not match fairness_group_sha256 "
            f"(file {digest}, config {gates.fairness_group_sha256})"
        )
    rows = list(csv.reader(raw.decode("utf-8").splitlines()))
    seen: set[str] = set()
    shares: list[tuple[str, str, Fraction | None]] = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        if len(row) < 3:
            raise ConfigError(f"poverty groups row {row[0]!r} has fewer than 3 columns")
        hood, share_text, group = row[0].strip(), row[1].strip(), row[2].strip()
        if hood in seen:
            raise ConfigError(f"poverty groups file repeats neighborhood {hood!r}")
        seen.add(hood)
        if share_text == "":
            share = None
        else:
            try:
                share = Fraction(share_text)
            except (ValueError, ZeroDivisionError) as exc:
                raise ConfigError(
                    f"poverty groups row {hood!r} has a bad share {share_text!r}"
                ) from exc
        shares.append((hood, group, share))
    labels = {group for _, group, _ in shares}
    allowed = set(gates.fairness_groups) | {UNGROUPED}
    if labels != allowed:
        raise ConfigError(
            f"poverty groups file labels {sorted(labels)} are not {sorted(allowed)}"
        )
    graded = [share for _, group, share in shares if group != UNGROUPED]
    if any(share is None for share in graded):
        raise ConfigError("a gated neighborhood has no poverty share")
    if any(share is not None for _, group, share in shares if group == UNGROUPED):
        raise ConfigError("an ungrouped neighborhood has a poverty share")
    # The median rule (ADR-026, Step 2) is a 2-way split: a row is the higher group
    # exactly when its share is above the median of the graded rows. It is only
    # defined for two groups, so a config with another count is not median-checked.
    if len(gates.fairness_groups) != 2:
        return
    cut = median(sorted(graded))
    low, high = gates.fairness_groups  # lowest poverty first, then higher
    for hood, group, share in shares:
        if group == UNGROUPED:
            continue
        expected = high if share > cut else low
        if expected != group:
            raise ConfigError(
                f"neighborhood {hood!r} breaks the median rule: share {float(share):.6f} "
                f"against cut {float(cut):.6f} is {expected!r}, file says {group!r}"
            )


class Undefined(Exception):
    """An input the gates cannot be evaluated on (decision 'error')."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _as_count(where: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Undefined(f"{where} is not a non-negative int: {value!r}")
    return value


def _tally(where: str, block: object) -> tuple[int, int, int]:
    if not isinstance(block, dict):
        raise Undefined(f"{where} is missing")
    return (
        _as_count(f"{where}.tp", block.get("tp")),
        _as_count(f"{where}.fp", block.get("fp")),
        _as_count(f"{where}.fn", block.get("fn")),
    )


def _f1(where: str, tp: int, fp: int, fn: int) -> Fraction:
    denom = 2 * tp + fp + fn
    if denom == 0:
        raise Undefined(f"{where} F1 denominator is zero")
    return Fraction(2 * tp, denom)


def _round6(value: Fraction) -> float:
    return round(float(value), 6)


def evaluate(gates: Gates, metrics: dict) -> dict:
    """Decide promote / keep_champion / blocked / error from a candidate's
    validation metrics. Pure: no I/O, generic over any number of gated groups.
    Every comparison is on Fraction values; floats appear only for display."""
    if not isinstance(metrics, dict):
        return {
            "decision": "error",
            "reason": "metrics is not a JSON object",
            "data_version": None,
            "gates_sha256": gates.gates_sha256,
            "fairness_group_sha256": gates.fairness_group_sha256,
        }
    data_version = metrics.get("data_version")
    try:
        return _evaluate(gates, metrics, data_version)
    except Undefined as undefined:
        return {
            "decision": "error",
            "reason": undefined.reason,
            "data_version": data_version,
            "gates_sha256": gates.gates_sha256,
            "fairness_group_sha256": gates.fairness_group_sha256,
        }


def _evaluate(gates: Gates, metrics: dict, data_version: object) -> dict:
    if not isinstance(data_version, str) or not data_version.strip():
        raise Undefined("top-level data_version must be a non-empty string")
    ctp, cfp, cfn = _tally("candidate", metrics.get("candidate"))
    btp, bfp, bfn = _tally("baseline", metrics.get("baseline"))
    candidate_positives = ctp + cfn
    if btp + bfn != candidate_positives:
        raise Undefined("candidate and baseline differ in positives (tp + fn)")

    champion = metrics.get("champion")
    champion_f1: Fraction | None = None
    champion_report: dict | None = None
    if champion is not None:
        htp, hfp, hfn = _tally("champion", champion)
        if htp + hfn != candidate_positives:
            raise Undefined("candidate and champion differ in positives (tp + fn)")
        if champion.get("data_version") != data_version:
            raise Undefined("champion data_version differs from the top-level data_version")
        champion_f1 = _f1("champion", htp, hfp, hfn)
        champion_report = {
            "f1": _round6(champion_f1),
            "data_version": champion.get("data_version"),
            "model_version": champion.get("model_version"),
        }

    groups_in = metrics.get("groups")
    if not isinstance(groups_in, dict):
        raise Undefined("groups is missing")
    configured = set(gates.fairness_groups)
    present = set(groups_in)
    if present != configured:
        raise Undefined(
            f"groups {sorted(present)} are not the configured groups {sorted(configured)}"
        )

    excluded_in = metrics.get("excluded") or {}
    if not isinstance(excluded_in, dict):
        raise Undefined("excluded is not a mapping")

    group_positives = 0
    group_true_positives = 0
    tallies: dict[str, tuple[int, int]] = {}
    for name in gates.fairness_groups:
        block = groups_in[name]
        if not isinstance(block, dict):
            raise Undefined(f"group {name!r} is missing counts")
        pos = _as_count(f"group {name}.positives", block.get("positives"))
        tp = _as_count(f"group {name}.true_positives", block.get("true_positives"))
        if tp > pos:
            raise Undefined(f"group {name!r} true_positives exceed positives")
        tallies[name] = (pos, tp)
        group_positives += pos
        group_true_positives += tp

    # Every excluded row shares one schema: a neighborhood or a group (one null),
    # positives, true_positives and a reason.
    excluded_report = []
    excluded_positives = 0
    excluded_true_positives = 0
    for hood, block in excluded_in.items():
        if not isinstance(block, dict):
            raise Undefined(f"excluded {hood!r} is not a mapping")
        pos = _as_count(f"excluded {hood}.positives", block.get("positives"))
        tp = _as_count(f"excluded {hood}.true_positives", block.get("true_positives"))
        if tp > pos:
            raise Undefined(f"excluded {hood!r} true_positives exceed positives")
        excluded_positives += pos
        excluded_true_positives += tp
        excluded_report.append(
            {
                "neighborhood": hood,
                "group": None,
                "positives": pos,
                "true_positives": tp,
                "reason": block.get("reason", UNGROUPED),
            }
        )

    if group_positives + excluded_positives != candidate_positives:
        raise Undefined(
            "group positives plus excluded positives do not equal the candidate's tp + fn"
        )
    if group_true_positives + excluded_true_positives != ctp:
        raise Undefined(
            "group true_positives plus excluded true_positives do not equal the candidate's tp"
        )

    floor = gates.fairness_min_group_surges
    evaluable: list[tuple[str, int, int]] = []
    small: list[tuple[str, int, int]] = []
    for name in gates.fairness_groups:
        pos, tp = tallies[name]
        (evaluable if pos >= floor else small).append((name, pos, tp))

    pooled_report = None
    if small:
        pool_pos = sum(pos for _, pos, _ in small)
        pool_tp = sum(tp for _, _, tp in small)
        members = [name for name, _, _ in small]
        if pool_pos >= floor:
            evaluable.append(("pool", pool_pos, pool_tp))
            pooled_report = {
                "group": "pool",
                "members": members,
                "positives": pool_pos,
                "true_positives": pool_tp,
                "status": "evaluated",
            }
        else:
            pooled_report = {
                "group": "pool",
                "members": members,
                "positives": pool_pos,
                "true_positives": pool_tp,
                "status": "excluded",
                "reason": f"pool positives {pool_pos} below floor {floor}",
            }
            for member in members:
                excluded_report.append(
                    {
                        "neighborhood": None,
                        "group": member,
                        "positives": tallies[member][0],
                        "true_positives": tallies[member][1],
                        "reason": "pooled_below_floor",
                    }
                )

    if len(evaluable) < gates.fairness_min_groups:
        raise Undefined(
            f"only {len(evaluable)} evaluable group(s) after pooling, "
            f"below fairness_min_groups {gates.fairness_min_groups}"
        )

    recalls = {name: Fraction(tp, pos) for name, pos, tp in evaluable}
    best = max(recalls, key=lambda name: recalls[name])
    worst = min(recalls, key=lambda name: recalls[name])
    gap = recalls[best] - recalls[worst]
    fairness_pass = gap <= gates.fairness_max_gap

    candidate_f1 = _f1("candidate", ctp, cfp, cfn)
    baseline_f1 = _f1("baseline", btp, bfp, bfn)
    margin = candidate_f1 - baseline_f1
    f1_pass = margin >= gates.f1_min_margin

    if not (f1_pass and fairness_pass):
        decision = "blocked"
    elif champion_f1 is not None and not (candidate_f1 > champion_f1 + gates.champion_min_delta):
        decision = "keep_champion"
    else:
        decision = "promote"

    group_rows = [
        {
            "group": name,
            "positives": pos,
            "true_positives": tp,
            "recall": _round6(recalls[name]),
        }
        for name, pos, tp in evaluable
    ]

    return {
        "decision": decision,
        "data_version": data_version,
        "f1_gate": {
            "metric": gates.f1_metric,
            "baseline_name": gates.f1_baseline,
            "candidate": _round6(candidate_f1),
            "baseline": _round6(baseline_f1),
            "margin": _round6(margin),
            "threshold": _round6(gates.f1_min_margin),
            "pass": f1_pass,
            "reason": (
                f"margin {_round6(margin)} "
                f"{'>=' if f1_pass else '<'} threshold {_round6(gates.f1_min_margin)}"
            ),
        },
        "fairness_gate": {
            "metric": gates.fairness_metric,
            "gap": _round6(gap),
            "threshold": _round6(gates.fairness_max_gap),
            "best_group": best,
            "worst_group": worst,
            "pass": fairness_pass,
            "reason": (
                f"gap {_round6(gap)} "
                f"{'<=' if fairness_pass else '>'} threshold {_round6(gates.fairness_max_gap)}"
            ),
        },
        "groups": group_rows,
        "pooled": pooled_report,
        "excluded": excluded_report,
        "best_group": best,
        "worst_group": worst,
        "champion": champion_report,
        "gates_sha256": gates.gates_sha256,
        "fairness_group_sha256": gates.fairness_group_sha256,
    }


def _validate_config(config: Path) -> int:
    try:
        gates = load_gates(config)
    except (ConfigError, OSError) as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    print(f"{len(GATE_ORDER)} gate settings loaded from {config}")
    print(f"gates_sha256 {gates.gates_sha256}")
    print(f"fairness_group_sha256 {gates.fairness_group_sha256}")
    return 0


_EXIT = {"promote": 0, "keep_champion": 0, "blocked": 1, "error": 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the model promotion gates (#112).")
    parser.add_argument("--metrics", help="JSON file of validation metrics")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="gates.csv path")
    parser.add_argument("--out", help="write the report JSON here instead of stdout")
    parser.add_argument(
        "--validate-config", action="store_true", help="validate the config and groups file only"
    )
    args = parser.parse_args(argv)
    config = Path(args.config)

    if args.validate_config:
        return _validate_config(config)

    if not args.metrics:
        parser.error("--metrics is required unless --validate-config is given")
    try:
        gates = load_gates(config)
    except (ConfigError, OSError) as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2

    try:
        metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
        report = evaluate(gates, metrics)
        text = json.dumps(report, indent=2, sort_keys=True)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return _EXIT[report["decision"]]
    except Exception as exc:  # unreadable file, bad JSON, or any unexpected error
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
