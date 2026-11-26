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

Standard library only, so the DAG imports it at parse time for free.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
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
