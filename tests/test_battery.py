import math

import pytest

from microgrid.agents.battery import Battery
from microgrid.config import BatteryConfig

STEP_HOURS = 0.25


def make_battery(**overrides) -> Battery:
    params = dict(
        capacity_kwh=10.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=0.90,
        soc_min=0.10,
        soc_max=0.95,
        initial_soc=0.50,
        step_hours=STEP_HOURS,
    )
    params.update(overrides)
    return Battery(**params)


def test_charge_respects_power_limit():
    battery = make_battery()
    accepted = battery.charge(100.0)
    # 5 kW for 15 minutes = 1.25 kWh max intake per step
    assert accepted == pytest.approx(5.0 * STEP_HOURS)


def test_discharge_respects_power_limit():
    battery = make_battery()
    delivered = battery.discharge(100.0)
    assert delivered == pytest.approx(5.0 * STEP_HOURS)


def test_charge_stops_at_soc_max():
    battery = make_battery(initial_soc=0.95)
    assert battery.charge(1.0) == pytest.approx(0.0)
    assert battery.soc == pytest.approx(0.95)


def test_discharge_stops_at_soc_min():
    battery = make_battery(initial_soc=0.10)
    assert battery.discharge(1.0) == pytest.approx(0.0)
    assert battery.soc == pytest.approx(0.10)


def test_round_trip_efficiency_loss():
    battery = make_battery(round_trip_efficiency=0.81, initial_soc=0.50)
    start_energy = battery.energy_kwh
    accepted = battery.charge(1.0)
    stored = battery.energy_kwh - start_energy
    # One-way efficiency is sqrt(0.81) = 0.9
    assert stored == pytest.approx(accepted * 0.9)

    delivered = battery.discharge(stored * 0.9)
    drained = (start_energy + stored) - battery.energy_kwh
    assert delivered == pytest.approx(drained * 0.9)


def test_soc_never_leaves_bounds_under_stress():
    battery = make_battery()
    for _ in range(500):
        battery.charge(10.0)
        assert battery.soc <= 0.95 + 1e-9
    for _ in range(500):
        battery.discharge(10.0)
        assert battery.soc >= 0.10 - 1e-9


def test_negative_inputs_rejected():
    battery = make_battery()
    with pytest.raises(ValueError):
        battery.charge(-0.1)
    with pytest.raises(ValueError):
        battery.discharge(-0.1)


def test_invalid_construction_rejected():
    with pytest.raises(ValueError):
        make_battery(capacity_kwh=0.0)
    with pytest.raises(ValueError):
        make_battery(round_trip_efficiency=0.0)
    with pytest.raises(ValueError):
        make_battery(soc_min=0.9, soc_max=0.5)
    with pytest.raises(ValueError):
        make_battery(initial_soc=0.05)


def test_from_config():
    battery = Battery.from_config(BatteryConfig(), step_hours=STEP_HOURS)
    assert battery.soc == pytest.approx(0.50)
    assert battery.capacity_kwh == pytest.approx(10.0)
    assert not math.isnan(battery.soc)
