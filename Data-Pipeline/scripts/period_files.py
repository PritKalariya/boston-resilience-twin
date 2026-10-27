"""Which stored period files the #16 readers use: #7's NOAA Logan years and tide half-years, #8's GFS years.

One rule for weather_features.read_observed, open_meteo_forecast.read_hourly and
tide_features.read_predictions. Every file of a period before the current one
must be stored: a gap there is a missing download, not missing weather, so the
run stops. The current period's file may be absent (not downloaded yet), and
so may a later one; their days stay blank, and a warning names each such file
and the features it leaves blank, so a blank stretch shows in the task log.
The downloads themselves are #7's and #8's; nothing here fetches a file.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


def stored_periods(folder: Path, periods: list, current, fetch: str, blank: str) -> list:
    """The periods whose <period>.json is in folder, in the order given.

    periods and current compare in time order: years as numbers, half-years as
    strings such as 2026-H2. A missing file before current raises
    FileNotFoundError, with fetch saying how to get it; a missing current or
    later one is skipped with a warning naming the file and blank, the features
    its days are missing.
    """
    folder = Path(folder)
    absent = [period for period in periods if not (folder / f"{period}.json").is_file()]
    missing = [period for period in absent if period < current]
    if missing:
        raise FileNotFoundError(f"{folder} lacks {', '.join(f'{period}.json' for period in missing)}; {fetch}")
    for period in absent:
        log.warning("%s is not stored yet (current period %s), so %s stay blank on its days; %s",
                    folder / f"{period}.json", current, blank, fetch)
    return [period for period in periods if period not in absent]
