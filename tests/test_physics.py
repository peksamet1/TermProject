"""SimPy physical layer (Phase 5): battery plans applied minute by minute."""

import pytest

from microgrid.agents.battery import Battery
from microgrid.config import SimulationConfig
from microgrid.model import MicrogridModel
from microgrid.physics import PhysicalSimulator, SyncError

STEP_HOURS = 0.25


class StubAgent:
    """Just what the physical layer reads: id, battery and the plan."""

    def __init__(self, unique_id: int, battery: Battery, plan_kwh: float) -> None:
        self.unique_id = unique_id
        self.battery = battery
        self.battery_flow_kwh = plan_kwh


def make_battery(**overrides) -> Battery:
    params = dict(
        capacity_kwh=10.0, max_charge_kw=5.0, max_discharge_kw=5.0,
        round_trip_efficiency=0.81, soc_min=0.1, soc_max=0.95,
        initial_soc=0.5, step_hours=STEP_HOURS,
    )
    params.update(overrides)
    return Battery(**params)


def test_slot_applies_the_full_plan_within_limits():
    sim = PhysicalSimulator(step_hours=STEP_HOURS)
    charging = StubAgent(1, make_battery(), 1.25)  # full 5 kW for 15 min
    discharging = StubAgent(2, make_battery(), -1.0)
    idle = StubAgent(3, make_battery(), 0.0)
    report = sim.run_slot(0, [charging, discharging, idle])

    results = {r.agent_id: r for r in report.batteries}
    assert results[1].applied_kwh == pytest.approx(1.25)
    assert results[2].applied_kwh == pytest.approx(-1.0)
    assert results[3].applied_kwh == 0.0
    assert report.violations == 0 and report.shortfall_kwh == pytest.approx(0.0)
    # One-way efficiency 0.9: stored = intake * 0.9, drained = delivered / 0.9.
    assert charging.battery.energy_kwh == pytest.approx(5.0 + 1.25 * 0.9)
    assert discharging.battery.energy_kwh == pytest.approx(5.0 - 1.0 / 0.9)
    assert idle.battery.energy_kwh == 5.0
    assert sim.env.now == 15


def test_minute_steps_match_one_shot_apply():
    """Without fade, 15 one-minute pieces land where one step-long apply does."""
    plan = make_battery().plan_charge(0.8)
    one_shot = make_battery()
    one_shot.charge(plan)
    sim = PhysicalSimulator(step_hours=STEP_HOURS)
    agent = StubAgent(1, make_battery(), plan)
    sim.run_slot(0, [agent])
    assert agent.battery.energy_kwh == pytest.approx(one_shot.energy_kwh, abs=1e-12)


def test_fade_inside_the_slot_shows_as_shortfall_not_violation():
    # Near the top with heavy fade: the headroom shrinks while charging.
    battery = make_battery(initial_soc=0.9, fade_per_cycle=0.05)
    plan = battery.plan_charge(10.0)
    sim = PhysicalSimulator(step_hours=STEP_HOURS)
    report = sim.run_slot(0, [StubAgent(1, battery, plan)])
    [result] = report.batteries
    assert result.shortfall_kwh > 0
    assert result.applied_kwh == pytest.approx(plan - result.shortfall_kwh)
    assert report.violations == 0
    assert battery.soc <= battery.soc_max + 1e-9


def test_out_of_order_slot_is_a_sync_error():
    sim = PhysicalSimulator(step_hours=STEP_HOURS)
    sim.run_slot(0, [])
    with pytest.raises(SyncError):
        sim.run_slot(2, [])  # slot 1 was skipped
    with pytest.raises(ValueError):
        PhysicalSimulator(step_hours=0.3 / 60)  # not a whole minute


# --- Model integration ----------------------------------------------------


@pytest.mark.parametrize("backend", ["cda", "logging", "cda_zmq"])
def test_model_physical_balance_and_clocks(backend):
    """gen - cons - applied battery flow = export - import (+ dust).

    Grid totals include the battery fallback, so a shortfall needs no slack.

    The battery ledger check catches a battery applied twice or not at all.
    """
    config = SimulationConfig(market_backend=backend)
    with MicrogridModel(config=config) as model:
        slot = model.physics.slot_minutes
        for _ in range(config.steps_per_day):
            batteries = {a.unique_id: a.battery for a in model.agents if a.battery}
            before = {i: b.energy_kwh for i, b in batteries.items()}
            fade_before = {i: b.fade_loss_kwh for i, b in batteries.items()}
            model.step()

            assert model.physics.env.now == model.time * slot == model.current_step * slot
            assert model.constraint_violations == 0

            flow = sum(a.generation_kwh - a.consumption_kwh - a.battery_applied_kwh
                       for a in model.agents)
            grid = model.grid_export_kwh - model.grid_import_kwh
            dust = 0.01 * len(model.agents)
            assert abs(flow - grid) <= dust + 1e-9

            for agent in model.agents:
                if agent.battery is None:
                    continue
                b, i = agent.battery, agent.unique_id
                eta = b.one_way_efficiency
                applied = agent.battery_applied_kwh
                expected = applied * eta if applied > 0 else applied / eta
                expected -= b.fade_loss_kwh - fade_before[i]
                assert b.energy_kwh - before[i] == pytest.approx(expected, abs=1e-9)


def test_soc_changes_in_the_slot_not_in_the_agent_step():
    with MicrogridModel(config=SimulationConfig()) as model:
        prosumers = [a for a in model.agents if a.battery is not None]
        after_market = {}
        settle = model._market_settle

        def recording_settle():
            after_market.update({a.unique_id: a.soc for a in prosumers})
            settle()

        model._market_settle = recording_settle
        moved = 0
        for _ in range(model.config.steps_per_day):
            start = {a.unique_id: a.soc for a in prosumers}
            model.step()
            assert after_market == start  # agents only planned
            moved += sum(a.soc != start[a.unique_id] for a in prosumers if a.battery_flow_kwh)
        assert moved > 0  # and the slot did apply the plans


def test_clock_drift_is_detected():
    with MicrogridModel(config=SimulationConfig()) as model:
        model.step()
        model.physics.env.run(until=model.physics.env.now + 1)  # SimPy runs ahead
        with pytest.raises(SyncError):
            model.step()


def test_shortfall_falls_back_to_grid_and_is_paid():
    """Heavy fade makes batteries fall short; that volume is exported at
    feed-in (charge) or imported at retail (discharge), and the books close."""
    config = SimulationConfig(market_backend="logging")  # no local trades
    config.battery.fade_per_cycle = 0.02
    tariff = config.tariff
    with MicrogridModel(config=config) as model:
        shortfall_seen = 0.0
        for _ in range(config.steps_per_day):
            cash_before = sum(a.cash_tl for a in model.agents)
            model.step()
            assert model.fallback_import_kwh + model.fallback_export_kwh == pytest.approx(
                model.battery_shortfall_kwh, abs=1e-12
            )
            # Per-agent: the fallback goes the battery's planned direction.
            charge_short = sum(a.battery_shortfall_kwh for a in model.agents
                               if a.battery_flow_kwh > 0)
            assert model.fallback_export_kwh == pytest.approx(charge_short, abs=1e-12)
            # Cash closes: every grid kWh is paid at its tariff (dust aside).
            paid = sum(a.cash_tl for a in model.agents) - cash_before
            expected = (tariff.feed_in_tl_per_kwh * model.grid_export_kwh
                        - tariff.retail_tl_per_kwh * model.grid_import_kwh)
            dust = 0.01 * len(model.agents) * tariff.retail_tl_per_kwh
            assert paid == pytest.approx(expected, abs=dust)
            shortfall_seen += model.battery_shortfall_kwh
        assert shortfall_seen > 0  # the scenario did exercise the fallback


def test_no_fade_means_no_fallback():
    config = SimulationConfig()
    config.battery.fade_per_cycle = 0.0
    with MicrogridModel(config=config) as model:
        model.run(config.steps_per_day)
        df = model.datacollector.get_model_vars_dataframe()
        assert df["FallbackImport"].sum() == df["FallbackExport"].sum() == 0.0
