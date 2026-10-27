"""API keys for pipeline tasks, read from environment variables.

docker-compose.yaml passes them from `.env` to every Airflow container; the
names are listed in `.env.example`. Tasks call `require` when they run, not
at import, so a DAG still loads without its keys and only the task that needs
a missing key fails. How to get each key: docs/data-access.md.
"""

from __future__ import annotations

import os

NOAA_CDO_TOKEN = "NOAA_CDO_TOKEN"
CENSUS_API_KEY = "CENSUS_API_KEY"
NWS_USER_AGENT = "NWS_USER_AGENT"
KEYS = (NOAA_CDO_TOKEN, CENSUS_API_KEY, NWS_USER_AGENT)


class MissingCredentialError(RuntimeError):
    """A key the pipeline needs is not set. The message names the key, never a value."""


def require(name: str) -> str:
    """Return the value of `name`, without surrounding whitespace.

    A trailing newline would make NOAA and Census reject the key, so it is
    stripped. Unset, empty and whitespace-only values all raise.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingCredentialError(
            f"{name} is not set. Add it to .env (names in .env.example), "
            "then recreate the stack with `docker compose up -d --wait`. "
            "See docs/data-access.md."
        )
    return value
