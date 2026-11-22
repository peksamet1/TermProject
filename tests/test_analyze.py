"""Phase 6 analysis: summaries, season windows, and a one-seed smoke run."""

import statistics
from pathlib import Path

import pandas as pd
import pytest

from experiments.analyze import (
    main,
    plot_sensitivity,
    season_windows,
    summarize,
)
from experiments.perf_scale import make_config


def _row(seed, backend, dependence, reduction, sweep="season", value="summer", season="summer"):
    return {
        "error": "",
        "sweep": sweep,
        "sweep_value": value,
        "season": season,
        "backend": backend,
        "seed": seed,
        "violations": 0,
        "balance_error_kwh": 1e-16,
        "grid_dependence": dependence,
        "dependence_reduction_pct": reduction,
        "trade_success_rate": 0.1,
        "demand_fill_rate": 0.3,
        "community_cost_tl": -10.0,
        "cost_saving_tl": 2.0 if backend == "cda" else 0.0,
        "local_traded_kwh": 1.0,
    }


def test_default_mix_is_unchanged_and_share_overrides_it():
    default = make_config(10, "cda")
    assert (default.num_prosumers, default.num_consumers) == (6, 4)
    assert default.battery.capacity_kwh == 10.0
    skewed = make_config(10, "cda", prosumer_share=0.2, battery_capacity_kwh=2.0,
                         pv_peak_kw=8.0, retail_tl_per_kwh=5.0, start_day_of_year=355)
    assert (skewed.num_prosumers, skewed.num_consumers) == (2, 8)
    assert skewed.battery.capacity_kwh == 2.0
    assert skewed.pv_peak_kw == 8.0
    assert skewed.tariff.retail_tl_per_kwh == 5.0
    assert skewed.tariff.feed_in_tl_per_kwh == 1.2
    assert skewed.start_day_of_year == 355
    with pytest.raises(ValueError):
        make_config(10, "cda", battery_capacity_kwh=0)
    with pytest.raises(ValueError):
        make_config(10, "cda", retail_tl_per_kwh=1.2)


def test_summarize_reports_mean_and_sample_std_and_skips_zmq():
    rows = [
        _row(1, "logging", 0.40, 0.0),
        _row(2, "logging", 0.60, 0.0),
        _row(1, "cda", 0.20, 50.0),
        _row(2, "cda", 0.40, 30.0),
        _row(1, "cda_zmq", 0.99, 1.0),
    ]
    summary = summarize(rows)
    assert {row["backend"] for row in summary} == {"logging", "cda"}
    cda = next(row for row in summary if row["backend"] == "cda")
    assert cda["n_seeds"] == 2
    assert cda["grid_dependence_mean"] == pytest.approx(0.30)
    assert cda["grid_dependence_std"] == pytest.approx(statistics.stdev([0.20, 0.40]))
    assert cda["dependence_reduction_pct_mean"] == pytest.approx(40.0)
    assert cda["violations_max"] == 0


def test_non_finite_kpi_is_left_out_of_the_mean():
    row = _row(1, "cda", 0.2, 40.0)
    other = _row(2, "cda", 0.4, 20.0)
    row["demand_fill_rate"] = float("nan")
    other["demand_fill_rate"] = 0.5
    summary = summarize([row, other, _row(1, "logging", 0.5, 0.0), _row(2, "logging", 0.5, 0.0)])
    cda = next(item for item in summary if item["backend"] == "cda")
    assert cda["demand_fill_rate_mean"] == pytest.approx(0.5)
    assert cda["demand_fill_rate_std"] == 0.0


def test_one_seed_has_zero_sample_std():
    summary = summarize([_row(1, "cda", 0.25, 30.0), _row(1, "logging", 0.40, 0.0)])
    cda = next(row for row in summary if row["backend"] == "cda")
    assert cda["n_seeds"] == 1
    assert cda["grid_dependence_std"] == 0.0


def test_sensitivity_plot_writes_two_panels_for_the_tariff(tmp_path):
    rows = []
    for retail, reduction, saving in ((2.0, 10.0, 5.0), (3.5, 30.0, 20.0)):
        for backend in ("logging", "cda"):
            rows.append(_row(1, backend, 0.3, reduction if backend == "cda" else 0.0,
                             sweep="retail_tl", value=retail))
            rows[-1]["cost_saving_tl"] = saving if backend == "cda" else 0.0
    summary = summarize(rows)
    path = tmp_path / "tariff.png"
    plot_sensitivity(summary, "retail_tl", path)
    assert path.stat().st_size > 0


def test_season_windows_follow_day_of_year(tmp_path):
    stamps = pd.date_range("2013-01-01", periods=96 * 400, freq="15min")
    path = tmp_path / "profiles.csv"
    pd.DataFrame({"timestamp": stamps, "household_id": "H01", "pv_kw": 0.0, "load_kw": 0.2}).to_csv(
        path, index=False
    )
    windows = season_windows(str(path), days=3, summer_start=None, winter_start=None)
    summer = pd.Timestamp(windows["summer"])
    winter = pd.Timestamp(windows["winter"])
    assert abs(summer.dayofyear - 172) <= 1
    assert abs(winter.dayofyear - 355) <= 1
    assert season_windows(None, 3, None, None) == {"summer": None, "winter": None}


def test_main_one_seed_writes_the_season_bundle(tmp_path):
    code = main([
        "--seeds", "42",
        "--days", "1",
        "--skip-sensitivity",
        "--skip-zmq",
        "--out", str(tmp_path),
    ])
    assert code == 0
    assert (tmp_path / "runs.csv").is_file()
    assert (tmp_path / "summary.csv").is_file()
    assert (tmp_path / "seasons.png").stat().st_size > 0
    text = (tmp_path / "evidence.md").read_text(encoding="utf-8")
    assert "PSD 9.1" in text
    assert "tests/test_physics.py" in text
    summary = pd.read_csv(tmp_path / "summary.csv")
    summer = summary[(summary.sweep == "season") & (summary.sweep_value == "summer")
                     & (summary.backend == "cda")].iloc[0]
    assert summer.dependence_reduction_pct_mean > 0
    assert summer.violations_max == 0
    assert not Path(tmp_path / "sensitivity_pv_kw.png").exists()
