"""Tests for CSV-backed profiles and the csv mode of the model."""

import numpy as np
import pandas as pd
import pytest

from microgrid.config import SimulationConfig
from microgrid.model import MicrogridModel
from microgrid.profiles.timeseries import (
    ProfileDataError,
    ProfileTable,
    TimeSeriesProfile,
    generate_synthetic_csv,
)

STEP_HOURS = 0.25
STEPS_PER_DAY = 96


def write_csv(path, households=("H01", "H02"), steps=STEPS_PER_DAY, start="2024-01-01"):
    """Tiny profile CSV: pv = household number, load = step index / 100."""
    timestamps = pd.date_range(start, periods=steps, freq="15min")
    rows = []
    for n, household in enumerate(households, start=1):
        for step, ts in enumerate(timestamps):
            rows.append((ts, household, float(n), step / 100))
    pd.DataFrame(rows, columns=["timestamp", "household_id", "pv_kw", "load_kw"]).to_csv(
        path, index=False
    )
    return path


def load(path, **kwargs):
    return ProfileTable.from_csv(path, step_hours=STEP_HOURS, steps_per_day=STEPS_PER_DAY, **kwargs)


def test_csv_value_equals_file_value(tmp_path):
    table = load(write_csv(tmp_path / "p.csv"))
    # kW in the file -> kWh per 15-minute step
    assert table.solar_profile(1).energy_at(0) == pytest.approx(2.0 * STEP_HOURS)
    assert table.consumption_profile(0).energy_at(40) == pytest.approx(0.40 * STEP_HOURS)


def test_household_index_wraps_mod_k(tmp_path):
    table = load(write_csv(tmp_path / "p.csv"))
    assert table.solar_profile(2).energy_at(0) == table.solar_profile(0).energy_at(0)
    assert table.solar_profile(3).energy_at(0) == table.solar_profile(1).energy_at(0)


def test_series_wraps_around_when_data_runs_out():
    profile = TimeSeriesProfile(np.array([1.0, 2.0, 3.0]), STEP_HOURS)
    assert profile.energy_at(3) == profile.energy_at(0)
    assert profile.energy_at(7) == pytest.approx(2.0 * STEP_HOURS)


def test_less_than_one_day_is_rejected(tmp_path):
    with pytest.raises(ProfileDataError, match="at least one day"):
        load(write_csv(tmp_path / "p.csv", steps=50))


def test_missing_file_and_columns_are_rejected(tmp_path):
    with pytest.raises(ProfileDataError, match="not found"):
        load(tmp_path / "nope.csv")
    bad = tmp_path / "bad.csv"
    bad.write_text("timestamp,household_id,pv_kw\n2024-01-01,H01,1.0\n")
    with pytest.raises(ProfileDataError, match="missing columns"):
        load(bad)


def test_bad_values_are_rejected(tmp_path):
    path = write_csv(tmp_path / "p.csv")
    df = pd.read_csv(path)
    df.loc[5, "load_kw"] = -1.0
    df.to_csv(path, index=False)
    with pytest.raises(ProfileDataError, match="non-negative"):
        load(path)
    df.loc[5, "load_kw"] = None
    df.to_csv(path, index=False)
    with pytest.raises(ProfileDataError, match="non-numeric"):
        load(path)


def test_wrong_step_spacing_is_rejected(tmp_path):
    path = tmp_path / "p.csv"
    timestamps = pd.date_range("2024-01-01", periods=STEPS_PER_DAY, freq="30min")
    pd.DataFrame({"timestamp": timestamps, "household_id": "H01", "pv_kw": 0.0,
                  "load_kw": 0.2}).to_csv(path, index=False)
    with pytest.raises(ProfileDataError, match="evenly"):
        load(path)


def test_start_skips_earlier_rows(tmp_path):
    path = write_csv(tmp_path / "p.csv", steps=2 * STEPS_PER_DAY)
    table = load(path, start="2024-01-02")
    assert len(table.consumption_profile(0)) == STEPS_PER_DAY
    assert table.consumption_profile(0).energy_at(0) == pytest.approx(0.96 * STEP_HOURS)


def test_generate_synthetic_csv_round_trip(tmp_path):
    path = generate_synthetic_csv(tmp_path / "s.csv", households=3, days=2, seed=1)
    table = load(path)
    assert table.household_ids == ["H01", "H02", "H03"]
    load_kwh = sum(table.consumption_profile(0).energy_at(s) for s in range(STEPS_PER_DAY))
    assert 4.0 < load_kwh < 9.0


def csv_config(path, **overrides) -> SimulationConfig:
    config = SimulationConfig(profile_source="csv", profile_csv_path=str(path), num_days=1)
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_model_uses_csv_profiles(tmp_path):
    path = write_csv(tmp_path / "p.csv")
    model = MicrogridModel(config=csv_config(path), num_prosumers=2, num_consumers=1)
    model.step()
    # Prosumers H01 (1 kW) and H02 (2 kW); the consumer has no PV.
    assert model.total_generation_kwh == pytest.approx(3.0 * STEP_HOURS)


@pytest.mark.parametrize("source", ["synthetic", "csv"])
def test_energy_balance_and_soc_for_both_sources(tmp_path, source):
    path = generate_synthetic_csv(tmp_path / "s.csv", households=10, days=1, seed=3)
    config = csv_config(path) if source == "csv" else SimulationConfig(num_days=1)
    model = MicrogridModel(config=config)
    for _ in range(config.steps_per_day):
        model.step()
        residual_sum = sum(
            a.generation_kwh - a.consumption_kwh - a.battery_flow_kwh for a in model.agents
        )
        grid_net = model.grid_export_kwh - model.grid_import_kwh
        assert residual_sum == pytest.approx(grid_net, abs=0.01 * len(model.agents) + 1e-9)
    socs = model.datacollector.get_agent_vars_dataframe()["SoC"].dropna()
    assert socs.between(config.battery.soc_min - 1e-9, config.battery.soc_max + 1e-9).all()


def test_unknown_source_or_missing_path_is_rejected():
    with pytest.raises(ValueError, match="profile_source"):
        MicrogridModel(config=SimulationConfig(profile_source="excel"))
    with pytest.raises(ValueError, match="profile_csv_path"):
        MicrogridModel(config=SimulationConfig(profile_source="csv"))
