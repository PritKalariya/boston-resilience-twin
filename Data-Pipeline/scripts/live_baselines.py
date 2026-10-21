"""Test the 311 switch dates as change points and compare surge baseline methods (#11, ADR-005).

Reads the combined table from map_311.py, the switch dates in
docs/data/change-points.csv and the Logan GHCN-D daily file. Prints markdown
tables and writes no files.

The surge flags here are analysis only: k, m and the label rule belong to #16
and are frozen by ADR before Model Development (CLAUDE.md).
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from measure_311 import AFTERMATH_DAYS, HEAVY_RAIN_MM, SNOW_DAY_MM, load_weather, markdown

WINDOW_DAYS = 56  # 8 weeks either side of a switch for the step ratio
SCAN_DAYS = 84  # 12 weeks either side for the break-date scan
GAP_DAYS = 3  # days either side of a switch left out (gaps or pile-ups)
MIN_SPLIT_DAYS = 28
MIN_WINDOW_REQUESTS = 100
MIN_QUIET_DAYS = 20
MIN_REF_YEARS = 5

HISTORY_YEARS = 3  # method A: same days in each of the previous 3 years
HALF_WIDTH = 14  # method A: plus or minus 14 days around the same day
TRAILING_DAYS = 28  # methods B and C
MIN_HISTORY_DAYS = 20
GRID = [(1.5, 3), (1.5, 5), (2.0, 3), (2.0, 5)]
FIRST_WEEKS_DAYS = 56

# Fastest gust columns in GHCN-D (tenths of m/s) and the event threshold in m/s.
# 20 m/s is about 45 mph, the NWS wind advisory gust level.
GUSTS = (("WSF5", 20.0), ("WSF2", 14.0))
CARRIED_OVER = {"same", "renamed", "merged", "split"}

READ_COLUMNS = ["open_dt", "source_system", "source_category", "category", "storm_group", "match", "after_switch"]


def daily_counts(
    combined: pd.DataFrame, level: str, name: str, start, end, drop_after_switch: bool = False
) -> pd.Series:
    """Citywide requests per day for one category or storm group, every day present."""
    days = pd.date_range(start, end, freq="D")
    if combined.empty:
        return pd.Series(0, index=days, dtype="int64")
    rows = combined[combined[level].isin([name])]
    if drop_after_switch:
        rows = rows[~rows["after_switch"].fillna(False).astype(bool)]
    dates = rows["open_dt"].dt.normalize()
    return dates.value_counts().reindex(days, fill_value=0).astype("int64")


def last_full_day(combined: pd.DataFrame) -> pd.Timestamp:
    """The day before the earlier of the two systems' last request."""
    last = combined.groupby("source_system")["open_dt"].max()
    return last.min().normalize() - pd.Timedelta(days=1)


def load_gusts(path: Path | str) -> tuple[pd.Series, str, float]:
    """Fastest daily gust in m/s, preferring WSF5. Empty when the file has neither column."""
    columns = pd.read_csv(path, nrows=0).columns
    for column, threshold in GUSTS:
        if column in columns:
            raw = pd.read_csv(path, usecols=["DATE", column], dtype=str)
            speed = pd.to_numeric(raw[column].str.strip(), errors="coerce") / 10
            return pd.Series(speed.to_numpy(), index=pd.to_datetime(raw["DATE"], format="%Y-%m-%d")), column, threshold
    return pd.Series(dtype=float), "none", float("nan")


def day_types(weather: pd.DataFrame, gusts: pd.Series, threshold: float, days) -> tuple[pd.Series, pd.Series]:
    """Quiet days (no event, not within AFTERMATH_DAYS after one) and weather days (event or the day after)."""
    days = pd.DatetimeIndex(days)
    w = weather.reindex(days)
    event = (w["prcp_mm"] >= HEAVY_RAIN_MM) | (w["snow_mm"] >= SNOW_DAY_MM)
    if not gusts.empty:
        event = event | (gusts.reindex(days) >= threshold)
    event = event.fillna(False).astype(bool)
    recent = event.astype(int).rolling(AFTERMATH_DAYS + 1, min_periods=1).max().astype(bool)
    quiet = ~recent & w["prcp_mm"].notna()
    weather_day = event | event.shift(1, fill_value=False)
    return quiet, weather_day


def aligned(counts: pd.Series, mask: pd.Series, switch, years, before: int, after: int, gap: int) -> pd.DataFrame:
    """One row per year, one column per day offset from the switch date; masked-out days are NaN."""
    switch = pd.Timestamp(switch)
    offsets = [o for o in range(-before, after) if gap == 0 or abs(o) > gap]
    values = counts.astype(float).where(mask.reindex(counts.index, fill_value=False))
    rows = {}
    for year in years:
        anchor = switch - pd.DateOffset(years=switch.year - year)
        dates = [anchor + pd.Timedelta(days=o) for o in offsets]
        rows[year] = values.reindex(dates).to_numpy()
    return pd.DataFrame.from_dict(rows, orient="index", columns=offsets)


def step_table(al: pd.DataFrame, switch_year: int) -> tuple[pd.DataFrame, dict]:
    """Mean after over mean before, per year, and a verdict for the switch year."""
    pre, post = al.loc[:, al.columns < 0], al.loc[:, al.columns >= 0]
    per_year = pd.DataFrame({
        "pre": pre.mean(axis=1),
        "post": post.mean(axis=1),
        "pre_days": pre.notna().sum(axis=1),
        "post_days": post.notna().sum(axis=1),
        "pre_requests": pre.sum(axis=1),
        "post_requests": post.sum(axis=1),
    })
    per_year["step"] = per_year["post"] / per_year["pre"].where(per_year["pre"] > 0)
    usable = (per_year["pre_days"] >= MIN_QUIET_DAYS) & (per_year["post_days"] >= MIN_QUIET_DAYS)
    ref = per_year.loc[usable & (per_year.index != switch_year), "step"].dropna()

    summary = {"step": np.nan, "ref_median": np.nan, "ref_min": np.nan, "ref_max": np.nan,
               "rank": "", "excess": np.nan, "verdict": "not testable"}
    if switch_year not in per_year.index:
        return per_year, summary
    row = per_year.loc[switch_year]
    enough = (
        usable.get(switch_year, False)
        and row["pre_requests"] >= MIN_WINDOW_REQUESTS
        and row["post_requests"] >= MIN_WINDOW_REQUESTS
        and len(ref) >= MIN_REF_YEARS
        and pd.notna(row["step"])
    )
    if not enough:
        return per_year, summary
    all_steps = pd.concat([ref, pd.Series({switch_year: row["step"]})])
    summary.update({
        "step": row["step"],
        "ref_median": ref.median(),
        "ref_min": ref.min(),
        "ref_max": ref.max(),
        "rank": f"{int(all_steps.rank(ascending=False)[switch_year])} of {len(all_steps)}",
        "excess": row["step"] / ref.median() - 1,
        "verdict": "shift" if not ref.min() <= row["step"] <= ref.max() else "no shift",
    })
    return per_year, summary


def _ylogy(y: np.ndarray, e: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(y > 0, y * np.log(np.where(y > 0, y, 1) / e), 0.0)


def break_scan(y, e, offsets=None, min_days: int = MIN_SPLIT_DAYS) -> dict | None:
    """Best single break in observed counts y against expected counts e (two-segment Poisson gain)."""
    y, e = np.asarray(y, dtype=float), np.asarray(e, dtype=float)
    n = len(y)
    if n < 2 * min_days or len(e) != n or (e <= 0).any():
        return None
    offsets = np.arange(n) if offsets is None else np.asarray(offsets)
    cy, ce = np.cumsum(y), np.cumsum(e)
    taus = np.arange(min_days, n - min_days + 1)
    y1, e1 = cy[taus - 1], ce[taus - 1]
    y2, e2 = cy[-1] - y1, ce[-1] - e1
    gains = _ylogy(y1, e1) + _ylogy(y2, e2) - _ylogy(np.array([cy[-1]]), np.array([ce[-1]]))[0]
    best = int(np.argmax(gains))
    t = taus[best]
    before = y1[best] / e1[best]
    after = y2[best] / e2[best]
    return {
        "break": int(offsets[t]),
        "gain": float(gains[best]),
        "step_at_break": float(after / before) if before > 0 else float("nan"),
    }


def scan_table(al: pd.DataFrame, switch_year: int) -> dict:
    """Break scan for the switch year against leave-one-out expectations from the other years."""
    reference = [y for y in al.index if y != switch_year and al.loc[y].notna().sum() >= 2 * MIN_SPLIT_DAYS]

    def scan(year):
        others = [y for y in reference if y != year]
        if len(others) < MIN_REF_YEARS - 1:
            return None
        expected = al.loc[others].mean(axis=0).rolling(7, center=True, min_periods=1).mean()
        observed = al.loc[year]
        keep = observed.notna() & expected.notna() & (expected > 0)
        return break_scan(observed[keep].to_numpy(), expected[keep].to_numpy(), observed.index[keep].to_numpy())

    result = scan(switch_year) if switch_year in al.index else None
    ref_gains = [r["gain"] for r in (scan(y) for y in reference) if r]
    out = {"break": "", "days_from_switch": "", "step_at_break": np.nan, "gain": np.nan,
           "ref_max_gain": max(ref_gains) if ref_gains else np.nan, "verdict": "not testable"}
    if result is None or len(ref_gains) < MIN_REF_YEARS:
        return out
    near = abs(result["break"]) <= 14
    unusual = result["gain"] > max(ref_gains)
    out.update({
        "break": result["break"],
        "days_from_switch": result["break"],
        "step_at_break": result["step_at_break"],
        "gain": result["gain"],
        "verdict": ("change point at the switch" if near else "break elsewhere") if unusual else "no unusual break",
    })
    return out


def _check_index(counts: pd.Series) -> None:
    idx = counts.index
    if not isinstance(idx, pd.DatetimeIndex) or not idx.is_unique or (len(idx) > 1 and (idx.to_series().diff().dropna() != pd.Timedelta(days=1)).any()):
        raise ValueError("counts need exactly one row per day")


def seasonal_baseline(
    counts: pd.Series, years: int = HISTORY_YEARS, half_width: int = HALF_WIDTH, min_days: int = MIN_HISTORY_DAYS
) -> pd.DataFrame:
    """Method A: mean, sd and 90th percentile of the same +-half_width days in each of the previous years.

    Every lag is at least 365 - half_width days back, so no day sees itself or later days.
    """
    _check_index(counts)
    values = counts.astype(float)
    stacked = np.vstack([
        values.shift(365 * y + o).to_numpy()
        for y in range(1, years + 1)
        for o in range(-half_width, half_width + 1)
    ])
    n = np.sum(~np.isnan(stacked), axis=0)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns at the start of the series
        mean = np.nanmean(stacked, axis=0)
        sd = np.nanstd(stacked, axis=0, ddof=1)
        q90 = np.nanquantile(stacked, 0.9, axis=0)
    base = pd.DataFrame({"mean": mean, "sd": sd, "q90": q90, "n": n}, index=counts.index)
    base.loc[base["n"] < min_days, ["mean", "sd", "q90"]] = np.nan
    return base


def trailing_baseline(
    counts: pd.Series, days: int = TRAILING_DAYS, since=None, min_days: int = MIN_HISTORY_DAYS
) -> pd.DataFrame:
    """Methods B and C: the previous `days` days (excluding today). With `since`, earlier history is ignored."""
    _check_index(counts)
    values = counts.astype(float)
    if since is not None:
        values = values.where(values.index >= pd.Timestamp(since))
    window = values.shift(1).rolling(days, min_periods=min_days)
    return pd.DataFrame({
        "mean": window.mean(),
        "sd": window.std(),
        "q90": window.quantile(0.9),
        "n": window.count(),
    })


def surge_flags(counts: pd.Series, base: pd.DataFrame, k: float, m: int) -> pd.Series:
    """Analysis only; the label rule is #16's (CLAUDE.md freeze). No baseline means no surge."""
    threshold = base["mean"] + k * base["sd"].fillna(0)
    flags = (counts > threshold) & (counts >= m) & base["mean"].notna()
    return flags.fillna(False).astype(bool)


def _rate(flags: pd.Series, mask: pd.Series) -> float:
    picked = flags[mask.reindex(flags.index, fill_value=False)]
    return float(picked.mean()) if len(picked) else float("nan")


def replay(
    counts: pd.Series,
    quiet: pd.Series,
    weather_day: pd.Series,
    switch,
    last_day,
    check_weather: bool = True,
    rho: float | None = None,
) -> pd.DataFrame:
    """Surge rates in the year after the switch versus the same span in earlier years, per method and grid point."""
    switch, last_day = pd.Timestamp(switch), pd.Timestamp(last_day)
    span = last_day - switch
    first_day = counts.index[0]

    spans = {0: (switch, last_day)}
    back = 1
    while True:
        start = switch - pd.DateOffset(years=back)
        if start - pd.DateOffset(years=HISTORY_YEARS) - pd.Timedelta(days=HALF_WIDTH) < first_day:
            break
        spans[back] = (start, start + span)
        back += 1

    methods = {"A seasonal": seasonal_baseline(counts), "C trailing 28d": trailing_baseline(counts)}
    if rho is not None:
        scaled = counts.astype(float).where(counts.index >= switch, counts * rho)
        methods["D rescaled A"] = seasonal_baseline(scaled)

    rows = []
    method_names = list(methods) + ["B post-switch only"]
    for name in method_names:
        for k, m in GRID:
            result = {}
            for back, (start, end) in spans.items():
                if name == "B post-switch only":
                    base = trailing_baseline(counts, since=start)
                else:
                    base = methods[name]
                window = slice(start, end)
                flags = surge_flags(counts[window], base[window], k, m)
                first = slice(start, start + pd.Timedelta(days=FIRST_WEEKS_DAYS - 1))
                result[back] = {
                    "quiet": _rate(flags, quiet),
                    "first8": _rate(surge_flags(counts[first], base[first], k, m), quiet),
                    "weather": _rate(flags, weather_day),
                    "no_base": int(base.loc[window, "mean"].isna().sum()),
                    "quiet_days": int(quiet.reindex(flags.index, fill_value=False).sum()),
                }
            ref = pd.DataFrame({b: r for b, r in result.items() if b != 0}).T
            now = result[0]
            ok_quiet = now["quiet"] <= ref["quiet"].max()
            ok_weather = (not check_weather) or now["weather"] >= ref["weather"].min()
            rows.append({
                "method": name,
                "k": k,
                "m": m,
                "quiet": round(now["quiet"], 3),
                "ref_median": round(ref["quiet"].median(), 3),
                "ref_max": round(ref["quiet"].max(), 3),
                "first_8_weeks": round(now["first8"], 3),
                "weather": round(now["weather"], 3) if check_weather else "",
                "ref_median_weather": round(ref["weather"].median(), 3) if check_weather else "",
                "ref_min_weather": round(ref["weather"].min(), 3) if check_weather else "",
                "excess_false": round((now["quiet"] - ref["quiet"].median()) * now["quiet_days"], 1),
                "no_baseline_days": now["no_base"],
                "ref_years": len(ref),
                "verdict": "pass" if ok_quiet and ok_weather else "fail",
            })
    return pd.DataFrame(rows)


def monthly_level(counts: pd.Series, switch, last_day, years: int = HISTORY_YEARS) -> pd.DataFrame:
    """Requests per full month since the switch over the mean of the same month in the previous years."""
    switch, last_day = pd.Timestamp(switch), pd.Timestamp(last_day)
    monthly = counts.resample("MS").sum()
    first = (switch + pd.offsets.MonthBegin(1)).normalize()
    months = [m for m in monthly.index if first <= m and m + pd.offsets.MonthEnd(0) <= last_day]
    rows = []
    for month in months:
        past = [month - pd.DateOffset(years=y) for y in range(1, years + 1)]
        reference = monthly.reindex(past)
        if reference.isna().any() or reference.mean() == 0:
            continue
        rows.append({
            "month": month.strftime("%Y-%m"),
            "requests": int(monthly[month]),
            "previous_years_mean": round(reference.mean(), 1),
            "ratio": round(monthly[month] / reference.mean(), 2),
        })
    return pd.DataFrame(rows, columns=["month", "requests", "previous_years_mean", "ratio"])


def coverage(combined: pd.DataFrame, group: str, anchor) -> dict:
    """Share of a storm group's legacy requests in the 12 months before `anchor` whose category carried over."""
    anchor = pd.Timestamp(anchor)
    rows = combined[
        combined["storm_group"].isin([group])
        & combined["source_system"].isin(["legacy"])
        & (combined["open_dt"] >= anchor - pd.Timedelta(days=365))
        & (combined["open_dt"] < anchor)
    ]
    if rows.empty:
        return {"group": group, "window": "", "requests": 0, "carried_over": np.nan, "largest_without": ""}
    carried = rows["match"].isin(CARRIED_OVER)
    without = rows.loc[~carried, "source_category"].value_counts()
    largest = ", ".join(f"{c} ({100 * n / len(rows):.0f}%)" for c, n in without.head(3).items())
    return {
        "group": group,
        "window": f"{(anchor - pd.Timedelta(days=365)).date()} to {(anchor - pd.Timedelta(days=1)).date()}",
        "requests": len(rows),
        "carried_over": round(100 * carried.mean(), 1),
        "largest_without": largest,
    }


def switch_date(points: pd.DataFrame, level: str, name: str):
    row = points[(points["level"] == level) & (points["name"] == name)]
    return pd.Timestamp(row["switch_date"].iloc[0]) if len(row) else None


def _fmt(df: pd.DataFrame, digits: int = 2) -> pd.DataFrame:
    return df.apply(lambda c: c.round(digits) if pd.api.types.is_float_dtype(c) else c)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined", default="Data-Pipeline/data/interim/311_combined.parquet")
    parser.add_argument("--weather", default="Data-Pipeline/data/raw/weather/ghcnd_USW00014739.csv")
    parser.add_argument("--change-points", default="docs/data/change-points.csv")
    parser.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--drop-after-switch", action="store_true", help="leave out legacy rows flagged after_switch (#53)")
    args = parser.parse_args()

    combined = pd.read_parquet(args.combined, columns=READ_COLUMNS)
    points = pd.read_csv(args.change_points, dtype=str, keep_default_na=False)
    weather = load_weather(args.weather)
    gusts, gust_column, gust_threshold = load_gusts(args.weather)

    last = last_full_day(combined)
    start = pd.Timestamp("2015-01-01")
    days = pd.date_range(start, last, freq="D")
    quiet, weather_day = day_types(weather, gusts, gust_threshold, days)
    newest = combined.groupby("source_system")["open_dt"].max().dt.date.to_dict()

    print("## Data\n")
    print(f"- last request per system: {newest}; last full day used: {last.date()}")
    print(f"- gust column: {gust_column} (event at {gust_threshold} m/s or more); quiet days {int(quiet.sum())} of {len(days)}")
    print(f"- after_switch rows {'left out' if args.drop_after_switch else 'kept'}; step window {args.window_days} days\n")

    series = [
        ("tree", "storm_group", "tree", True),
        ("Street Light Outage", "category", "Street Light Outage", False),
    ]
    switches = {label: switch_date(points, level, name) for label, level, name, _ in series}
    counts = {
        label: daily_counts(combined, level, name, start, last, args.drop_after_switch)
        for label, level, name, _ in series
    }

    print("## Weather event days within 21 days of each switch\n")
    rows = []
    for label, switch in switches.items():
        near = pd.date_range(switch - pd.Timedelta(days=21), switch + pd.Timedelta(days=21))
        w = weather.reindex(near)
        g = gusts.reindex(near) if not gusts.empty else pd.Series(np.nan, index=near)
        event = (w["prcp_mm"] >= HEAVY_RAIN_MM) | (w["snow_mm"] >= SNOW_DAY_MM) | (g >= gust_threshold)
        for day in near[event.fillna(False).to_numpy()]:
            rows.append({"series": label, "date": day.date(), "prcp_mm": w.at[day, "prcp_mm"],
                         "snow_mm": w.at[day, "snow_mm"], "gust_ms": g.at[day]})
    print(markdown(pd.DataFrame(rows, columns=["series", "date", "prcp_mm", "snow_mm", "gust_ms"])) + "\n")

    print("## Daily requests around each switch, by system\n")
    for label, level, name, _ in series:
        switch = switches[label]
        near = pd.date_range(switch - pd.Timedelta(days=7), switch + pd.Timedelta(days=7))
        part = {
            system: daily_counts(combined[combined["source_system"] == system], level, name, near[0], near[-1])
            for system in ("legacy", "new")
        }
        table = pd.DataFrame({"date": near.date, "legacy": part["legacy"].to_numpy(), "new": part["new"].to_numpy()})
        print(f"{label} (switch {switch.date()})\n")
        print(markdown(table) + "\n")

    print("## Switch dates as change points\n")
    step_rows, by_year, scan_rows = [], {}, []
    for label, level, name, _ in series:
        switch = switches[label]
        years = range(start.year + 1, switch.year + 1)
        al = aligned(counts[label], quiet, switch, years, args.window_days, args.window_days, GAP_DAYS)
        per_year, summary = step_table(al, switch.year)
        by_year[label] = per_year["step"]
        step_rows.append({"series": label, "switch": switch.date(),
                          "pre": per_year.at[switch.year, "pre"], "post": per_year.at[switch.year, "post"], **summary})
        scan_al = aligned(counts[label], quiet, switch, years, SCAN_DAYS, SCAN_DAYS, GAP_DAYS)
        scan_rows.append({"series": label, "switch": switch.date(), **scan_table(scan_al, switch.year)})

    snow_switch = switch_date(points, "storm_group", "snow_ice")
    step_rows.append({"series": "snow_ice", "switch": snow_switch.date() if snow_switch is not None else "",
                      "verdict": "not testable: no new-system winter yet"})
    flood = combined[combined["storm_group"].isin(["flooding"])]
    flood_last = flood[flood["source_system"] == "legacy"].groupby("source_category")["open_dt"].max().dt.date
    flood_new = int((flood["source_system"] == "new").sum())
    step_rows.append({"series": "flooding", "switch": "none",
                      "verdict": f"no new-system source ({flood_new} new rows; last legacy: "
                                 + ", ".join(f"{c} {d}" for c, d in flood_last.items()) + ")"})
    print(markdown(_fmt(pd.DataFrame(step_rows))) + "\n")

    print("### Step ratio by year\n")
    years_table = pd.DataFrame(by_year)
    years_table.index.name = "year"
    print(markdown(_fmt(years_table.reset_index())) + "\n")

    print("### Break-date scan (12 weeks either side)\n")
    print(markdown(_fmt(pd.DataFrame(scan_rows))) + "\n")

    print("### Monthly level since the switch (all days, over the same month in the previous 3 years)\n")
    for label in counts:
        level = monthly_level(counts[label], switches[label], last)
        print(f"{label}: median ratio {level['ratio'].median():.2f} over {len(level)} months\n")
        print(markdown(level) + "\n")

    print("## Carry-over coverage (legacy categories with a new-system counterpart)\n")
    flood_anchor = pd.Timestamp(max(flood_last)) + pd.Timedelta(days=1) if len(flood_last) else last
    cov = [
        coverage(combined, "tree", switches["tree"]),
        coverage(combined, "snow_ice", snow_switch if snow_switch is not None else last),
        coverage(combined, "flooding", flood_anchor),
    ]
    print(markdown(pd.DataFrame(cov)) + "\n")

    print("## Surge rates by baseline method (replay of the year after the switch)\n")
    print("quiet: share of quiet days flagged (false alarms). weather: share of weather days flagged.")
    print("ref: the same calendar span in earlier years, with the same method.\n")
    for (label, level, name, check_weather), row in zip(series, step_rows):
        rho = 1 + row["excess"] if row["verdict"] == "shift" else None
        table = replay(counts[label], quiet, weather_day, switches[label], last, check_weather, rho)
        print(f"{label} (switch {switches[label].date()}, {(last - switches[label]).days} days after)\n")
        print(markdown(table) + "\n")


if __name__ == "__main__":
    main()
