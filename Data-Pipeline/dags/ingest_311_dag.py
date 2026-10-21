"""Airflow DAG for issue #6: refresh the Boston 311 raw files.

The work lives in scripts/ingest_311.py so it is tested without Airflow. The
DAG runs daily, and each file follows its own interval from
config/ingest_311_sources.csv, so most runs download only the 2026 and
new-system files and skip the rest.

Environment, read when the task runs:
  BRT_GCS_BUCKET   bucket for gs://<bucket>/raw/311/; unset keeps files local
  BRT_GCS_PREFIX   object prefix, default raw/311
  BRT_RAW_311_DIR  local raw folder, default Data-Pipeline/data/raw/311
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from airflow.sdk import dag, task
except ImportError:
    from airflow.decorators import dag, task

PIPELINE_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = str(PIPELINE_DIR / "scripts")


@dag(
    dag_id="ingest_311",
    description="Refresh legacy and new-system 311 files on their own schedules",
    schedule="@daily",
    start_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=15)},
    tags=["data", "311", "ingestion"],
)
def ingest_311_dag():
    @task
    def refresh_311_files() -> None:
        if SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, SCRIPTS_DIR)
        from ingest_311 import main

        raw_dir = os.environ.get("BRT_RAW_311_DIR", str(PIPELINE_DIR / "data" / "raw" / "311"))
        sources = str(PIPELINE_DIR / "config" / "ingest_311_sources.csv")
        if main(["--raw-dir", raw_dir, "--sources", sources]) != 0:
            raise RuntimeError("311 ingestion failed for at least one file; current files were kept. See the log above.")

    refresh_311_files()


dag_ingest_311 = ingest_311_dag()
