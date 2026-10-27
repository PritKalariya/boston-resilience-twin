import sys

import numpy as np
import pandas as pd
import pytest

import live_baselines
from live_baselines import (
    aligned,
    break_scan,
    coverage,
    daily_counts,
    day_types,
    load_gusts,
    monthly_level,
    seasonal_baseline,
    step_table,
    surge_flags,
    trailing_baseline,
)


def combined_rows(rows):
    return pd.DataFrame(rows, columns=["open_dt", "source_system", "source_category", "category",
                                       "storm_group", "match", "after_switch"]).assign(
        open_dt=lambda d: pd.to_datetime(d["open_dt"]))


def test_daily_counts_fill_missing_days_and_skip_other_groups():
    df = combined_rows([
        ("2025-01-01 08:00", "legacy", "Tree Emergencies", "Fallen Tree or Branches", "tree", "renamed", False),
        ("2025-01-01 23:59", "new", "Fallen Tree or Branches", "Fallen Tree or Branches", "tree", "renamed", False),
        ("2025-01-03 09:00", "legacy", "Pothole", "Pothole", None, "same", False),
        ("2025-01-03 10:00", "legacy", "Tree in Park", "Pruning Request", "tree", "split", True),
    ])
    counts = daily_counts(df, "storm_group", "tree", "2025-01-01", "2025-01-04")
    assert counts.tolist() == [2, 0, 1, 0]
    dropped = daily_counts(df, "storm_group", "tree", "2025-01-01", "2025-01-04", drop_after_switch=True)
    assert dropped.tolist() == [2, 0, 0, 0]


def test_daily_counts_empty_table_is_all_zero():
    counts = daily_counts(pd.DataFrame(), "storm_group", "tree", "2025-01-01", "2025-01-03")
    assert counts.tolist() == [0, 0, 0]


def test_load_gusts_prefers_wsf5_and_converts_tenths(tmp_path):
    path = tmp_path / "ghcnd.csv"
    path.write_text('"DATE","PRCP","SNOW","TMAX","WSF2","WSF5"\n"2025-01-01","0","0","10","  150","  201"\n')
    gusts, column, threshold = load_gusts(path)
    assert column == "WSF5" and threshold == 20.0
    assert gusts.loc["2025-01-01"] == pytest.approx(20.1)


def test_load_gusts_without_wind_columns(tmp_path):
    path = tmp_path / "ghcnd.csv"
    path.write_text('"DATE","PRCP","SNOW","TMAX"\n"2025-01-01","0","0","10"\n')
    gusts, column, _ = load_gusts(path)
    assert gusts.empty and column == "none"


def test_day_types_skip_events_aftermath_and_missing_rain():
    days = pd.date_range("2025-01-01", periods=7)
    weather = pd.DataFrame({
        "prcp_mm": [0, 30, 0, 0, 0, 0, np.nan],
        "snow_mm": [0, 0, 0, 0, 0, 0, 0],
        "tmax_c": 5.0,
    }, index=days)
    gusts = pd.Series([5, 5, 5, 5, 5, 25, 5], index=days, dtype=float)
    quiet, weather_day = day_types(weather, gusts, 20.0, days)
    # rain event on day 2 blocks days 2 to 5, a gust event on day 6, rain unknown on day 7
    assert quiet.tolist() == [True, False, False, False, False, False, False]
    assert weather_day.tolist() == [False, True, True, False, False, True, True]


def aligned_frame(post_level_switch_year, pre=10.0, years=range(2015, 2026)):
    """Reference years step by 0.95, 1.0 or 1.05; the last year is the switch year."""
    offsets = [o for o in range(-56, 56) if abs(o) > 3]
    rows = {}
    for i, year in enumerate(years):
        after = post_level_switch_year if year == max(years) else pre * (0.95 + 0.05 * (i % 3))
        rows[year] = [pre if o < 0 else after for o in offsets]
    return pd.DataFrame.from_dict(rows, orient="index", columns=offsets)


def test_step_table_flags_a_jump_only_in_the_switch_year():
    per_year, summary = step_table(aligned_frame(15.0), 2025)
    assert summary["verdict"] == "shift"
    assert summary["step"] == pytest.approx(1.5)
    assert summary["rank"] == "1 of 11"
    assert per_year.loc[2018, "step"] == pytest.approx(0.95)


def test_step_table_flat_series_is_no_shift():
    _, summary = step_table(aligned_frame(10.0), 2025)
    assert summary["verdict"] == "no shift"


def test_step_table_too_few_requests_is_not_testable():
    _, summary = step_table(aligned_frame(1.0, pre=1.0), 2025)
    assert summary["verdict"] == "not testable"


def test_aligned_puts_each_year_on_the_switch_date():
    counts = pd.Series(1, index=pd.date_range("2023-01-01", "2025-12-31"))
    counts.loc["2024-10-27"] = 9
    quiet = pd.Series(True, index=counts.index)
    al = aligned(counts, quiet, "2025-10-27", [2024, 2025], before=7, after=7, gap=0)
    assert al.loc[2024, 0] == 9 and al.loc[2025, 0] == 1


def test_break_scan_finds_a_known_step():
    expected = np.full(100, 10.0)
    observed = np.r_[np.full(50, 10.0), np.full(50, 20.0)]
    result = break_scan(observed, expected, offsets=np.arange(-50, 50))
    assert abs(result["break"]) <= 1
    assert result["step_at_break"] == pytest.approx(2.0)


def test_break_scan_short_input_returns_none():
    assert break_scan(np.ones(30), np.ones(30)) is None


def test_seasonal_baseline_uses_only_earlier_years():
    counts = pd.Series(2.0, index=pd.date_range("2020-01-01", "2024-12-31"))
    counts.loc["2024-06-01"] = 500
    base = seasonal_baseline(counts)
    assert base.loc["2024-06-01", "mean"] == pytest.approx(2.0)
    assert np.isnan(base.loc["2020-06-01", "mean"])  # no earlier years yet


def test_seasonal_baseline_rejects_a_missing_day():
    counts = pd.Series(1.0, index=pd.date_range("2024-01-01", periods=10).delete(4))
    with pytest.raises(ValueError, match="one row per day"):
        seasonal_baseline(counts)


def test_trailing_baseline_excludes_today_and_masks_before_since():
    counts = pd.Series(1.0, index=pd.date_range("2025-01-01", periods=60))
    counts.iloc[40] = 100
    base = trailing_baseline(counts, days=28, min_days=20)
    assert base["mean"].iloc[40] == pytest.approx(1.0)
    since = trailing_baseline(counts, days=28, since="2025-01-31", min_days=20)
    assert np.isnan(since["mean"].iloc[45])  # only 15 days since 2025-01-31
    assert not np.isnan(since["mean"].iloc[55])


def test_surge_flags_need_both_conditions():
    idx = pd.date_range("2025-01-01", periods=4)
    counts = pd.Series([4, 9, 9, 3], index=idx)
    base = pd.DataFrame({"mean": [1.0, 2.0, np.nan, 0.0], "sd": [1.0, 1.0, 1.0, 0.0]}, index=idx)
    # day 1: above 1 + 2*1 but below m=5; day 2: surge; day 3: no baseline; day 4: sd 0, m floor decides
    assert surge_flags(counts, base, k=2.0, m=5).tolist() == [False, True, False, False]
    assert surge_flags(counts, base, k=2.0, m=3).tolist() == [True, True, False, True]


def test_monthly_level_compares_full_months_with_previous_years():
    counts = pd.Series(1, index=pd.date_range("2021-01-01", "2025-03-15"))
    counts.loc["2025-01-01":"2025-01-31"] = 2
    level = monthly_level(counts, "2024-12-20", "2025-03-15")
    # January and February 2025 are full months after the switch; March is not
    assert level["month"].tolist() == ["2025-01", "2025-02"]
    assert level["ratio"].tolist() == pytest.approx([2.0, 1.0], abs=0.02)  # Feb 2024 had 29 days


def test_coverage_share_from_match():
    df = combined_rows([
        ("2025-06-01", "legacy", "Request for Snow Plowing", "Snow Plowing", "snow_ice", "renamed", False),
        ("2025-06-02", "legacy", "Unshoveled Sidewalk", "Unshoveled Sidewalk", "snow_ice", "legacy_only", False),
        ("2025-06-03", "legacy", "Unshoveled Sidewalk", "Unshoveled Sidewalk", "snow_ice", "legacy_only", False),
        ("2025-06-04", "new", "Snow Plowing", "Snow Plowing", "snow_ice", "renamed", False),
    ])
    row = coverage(df, "snow_ice", "2026-01-01")
    assert row["requests"] == 3
    assert row["carried_over"] == pytest.approx(33.3)
    assert row["largest_without"].startswith("Unshoveled Sidewalk (67%)")


def test_daily_counts_put_midnight_on_the_next_day_and_keep_the_end_day():
    df = combined_rows([
        ("2025-01-01 23:59:59", "legacy", "Tree Emergencies", "Fallen Tree or Branches", "tree", "renamed", False),
        ("2025-01-02 00:00:00", "new", "Fallen Tree or Branches", "Fallen Tree or Branches", "tree", "renamed", False),
        ("2025-01-02 23:59:59", "new", "Fallen Tree or Branches", "Fallen Tree or Branches", "tree", "renamed", False),
        ("2025-01-03 00:00:00", "new", "Fallen Tree or Branches", "Fallen Tree or Branches", "tree", "renamed", False),
    ])
    assert daily_counts(df, "storm_group", "tree", "2025-01-01", "2025-01-02").tolist() == [1, 2]


def test_load_gusts_reads_blank_or_malformed_values_as_missing(tmp_path):
    path = tmp_path / "ghcnd.csv"
    path.write_text('"DATE","WSF5"\n"2015-01-01","  223"\n"2015-01-02","    "\n"2015-01-03","gust"\n')
    gusts, column, threshold = load_gusts(path)
    assert (column, threshold) == ("WSF5", 20.0)
    assert gusts.iloc[0] == pytest.approx(22.3)
    assert gusts.iloc[1:].isna().all()


def synthetic_inputs(tmp_path, seed=7):
    """Citywide requests from 2015 to January 2026, the systems switching on 2025-06-01, plus
    the change points and a Logan file with a heavy rain day every 20 days and a gust every 50."""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2015-01-01", "2026-01-31")
    switch = pd.Timestamp("2025-06-01")
    rows = []
    for day in days:
        system = "new" if day >= switch else "legacy"
        rows += [(day + pd.Timedelta(hours=9), system, "Tree Emergencies", "Fallen Tree or Branches", "tree",
                  "renamed", False)] * int(rng.poisson(3))
        rows += [(day + pd.Timedelta(hours=10), system, "Street Light Outages", "Street Light Outage", None,
                  "same", False)] * int(rng.poisson(4))
        if day < switch:
            rows += [(day + pd.Timedelta(hours=11), "legacy", "Catchbasin", "Catchbasin", "flooding",
                      "legacy_only", False)] * int(rng.poisson(1))
    rows.append((pd.Timestamp("2026-01-31 12:00"), "legacy", "Pothole", "Pothole", None, "same", False))
    combined_rows(rows).to_parquet(tmp_path / "combined.parquet", index=False)
    (tmp_path / "change-points.csv").write_text(
        "level,name,switch_date\nstorm_group,tree,2025-06-01\ncategory,Street Light Outage,2025-06-01\n"
        "storm_group,snow_ice,2026-06-23\n")
    weather = pd.DataFrame({"DATE": days.strftime("%Y-%m-%d"), "PRCP": "0", "SNOW": "0", "TMAX": "200", "WSF5": "100"})
    weather.loc[::20, "PRCP"] = "300"
    weather.loc[::50, "WSF5"] = "250"
    weather.to_csv(tmp_path / "ghcnd.csv", index=False)


def test_main_prints_every_table_behind_adr_005(tmp_path, monkeypatch, capsys):
    synthetic_inputs(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "live_baselines.py", "--combined", str(tmp_path / "combined.parquet"), "--weather", str(tmp_path / "ghcnd.csv"),
        "--change-points", str(tmp_path / "change-points.csv")])
    live_baselines.main()
    out = capsys.readouterr().out
    assert "last full day used: 2026-01-30" in out
    assert "- gust column: WSF5 (event at 20.0 m/s or more)" in out
    for heading in ("## Weather event days within 21 days of each switch", "## Daily requests around each switch, by system",
                    "## Switch dates as change points", "### Break-date scan (12 weeks either side)",
                    "## Carry-over coverage", "## Surge rates by baseline method"):
        assert heading in out
    assert "| A seasonal | 1.5 | 3 |" in out and "| B post-switch only | 2.0 | 5 |" in out
