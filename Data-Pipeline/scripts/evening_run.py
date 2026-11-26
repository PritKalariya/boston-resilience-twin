"""The evening run's switch and settings (#162).

live(): whether this machine runs the live evening run. Only airflow-vm's .env sets BRT_EVENING_RUN=1
(Deployment/terraform/startup.sh renders it; ADR-034, note of 2026-11-25), and docker-compose.yaml
passes it into the Airflow containers. Where it is not set (a laptop, CI, the test service), the
evening tasks skip, so a grader's run of daily_pipeline needs no cloud.

settings(): Data-Pipeline/config/evening_run.csv (setting, value, options, note), checked:
- predict_url: the brt-model Cloud Run service (#161, ADR-038) whose POST /predict the run calls,
  and the audience of the ID token it sends. Blank until the service is deployed; the scoring task
  fails naming the setting while it is blank.
- predict_timeout_seconds: how long one request may take, a cold start included.
- predict_retry_waits_seconds: the waits before each retry of a 5xx, a 429, a timeout or a lost
  connection, separated by ";".

summary_text(): the one Slack message a night, from what forecast_scoring.score returns: the
forecast date, how many neighborhoods are flagged and which, the model version, and the run time.

Standard library only, so the DAG imports it at parse time for free.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
BOSTON = ZoneInfo("America/New_York")
LIVE_ENV = "BRT_EVENING_RUN"
SETTINGS_PATH = REPO_ROOT / "Data-Pipeline" / "config" / "evening_run.csv"
SETTINGS = ("predict_url", "predict_timeout_seconds", "predict_retry_waits_seconds")


class SettingsError(ValueError):
    """evening_run.csv is missing a setting or holds a value the run cannot use."""


@dataclass(frozen=True)
class Settings:
    predict_url: str  # no trailing slash; "" until #161 deploys the service
    timeout_seconds: int
    retry_waits: tuple[int, ...]


def live() -> bool:
    return os.environ.get(LIVE_ENV, "").strip() == "1"


def summary_text(result: dict, started: datetime, finished: datetime) -> str:
    """The evening summary for Slack. `result` is forecast_scoring.score's return value; `started`
    is when the DAG run started and `finished` when the summary is written, both with a time zone."""
    flagged = result["flagged"]
    which = f": {', '.join(flagged)}" if flagged else ""
    minutes = max(0, round((finished - started).total_seconds() / 60))
    day = date.fromisoformat(result["forecast_date"])
    return "\n".join([
        f"Forecast for {day:%a %Y-%m-%d}: {len(flagged)} of {result['units']} neighborhoods flagged{which}.",
        f"Model version {result['model_version']} (MLflow run {result['model_run_id'][:8]}), "
        f"data version {result['data_version'][:15]}...",
        f"The evening run started at {started.astimezone(BOSTON):%H:%M} Boston time and took {minutes} min.",
        result["forecast_uri"],
    ])


def whole_number(name: str, text: str, minimum: int) -> int:
    try:
        value = int(text)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number, not {text!r}") from None
    if value < minimum:
        raise SettingsError(f"{name} must be {minimum} or more, not {value}")
    return value


def settings(path: Path = SETTINGS_PATH) -> Settings:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not {"setting", "value"} <= set(reader.fieldnames or []):
            raise SettingsError(f"{path.name} needs the columns setting and value")
        values: dict[str, str] = {}
        for row in reader:
            name = (row["setting"] or "").strip()
            if name in values:
                raise SettingsError(f"{path.name} repeats the setting {name}")
            values[name] = (row["value"] or "").strip()
    missing = [name for name in SETTINGS if name not in values]
    if missing:
        raise SettingsError(f"{path.name} is missing settings {missing}")
    url = values["predict_url"].rstrip("/")
    if url and not url.startswith("https://"):
        raise SettingsError(f"predict_url must be an https:// URL, not {url!r}")
    waits = values["predict_retry_waits_seconds"]
    return Settings(
        predict_url=url,
        timeout_seconds=whole_number("predict_timeout_seconds", values["predict_timeout_seconds"], 1),
        retry_waits=tuple(whole_number("predict_retry_waits_seconds", part.strip(), 0) for part in waits.split(";"))
        if waits else (),
    )
