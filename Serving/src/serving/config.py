"""Settings read from the environment, and the one startup check that stops a misconfigured process.

SERVING_ROUTES picks what one process serves (ADR-037): Cloud Run invoker IAM is per service, not per
path, so `/predict` must live on a service that is not public.

  model  /health and /predict
  web    /health, GET /forecasts/{date} and the static page at /
  all    everything; for the local compose profile only

There is no default: an unset or unknown value stops the process at startup.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROUTES = ("model", "web", "all")


class ConfigError(ValueError):
    """The environment cannot start the service; the message is one line."""


@dataclass(frozen=True)
class Settings:
    routes: str
    model_dir: Path
    model_version: str
    forecasts_root: str
    frontend_dir: Path
    schemas_dir: Path

    @property
    def serves_model(self) -> bool:
        return self.routes in ("model", "all")

    @property
    def serves_web(self) -> bool:
        return self.routes in ("web", "all")


def from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Settings from env (default: the process environment). Raises ConfigError on a bad SERVING_ROUTES."""
    env = os.environ if env is None else env
    routes = env.get("SERVING_ROUTES")
    if routes not in ROUTES:
        shown = "is not set" if routes is None else f"is {routes!r}"
        raise ConfigError(f"SERVING_ROUTES {shown}; set it to one of {', '.join(ROUTES)}")
    return Settings(
        routes=routes,
        model_dir=Path(env.get("MODEL_DIR", "/app/model")),
        model_version=env.get("MODEL_VERSION", ""),
        forecasts_root=env.get("FORECASTS_ROOT", "/app/forecasts"),
        frontend_dir=Path(env.get("FRONTEND_DIR", "/app/frontend")),
        schemas_dir=Path(env.get("SCHEMAS_DIR", "/app/contracts/schemas")),
    )
