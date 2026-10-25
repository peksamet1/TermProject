"""Agent decision-flow tests with hand-written constant profiles.

Agents are attached to a real (empty) MicrogridModel so Mesa registration and
market submission work exactly as in production, but profiles are deterministic
stubs so assertions never depend on synthetic randomness.
"""

import math

import pandas as pd
import pytest

from microgrid.agents.battery import Battery
from microgrid.agents.household import (
    ConsumerAgent,
    HouseholdAgent,
    HouseholdState,
    ProsumerAgent,
)
from microgrid.config import SimulationConfig
from microgrid.comm.protocol import make_status_message
from microgrid.market.base import LoggingMarket, OrderSide
from microgrid.market.cda import CDAMarket
from microgrid.model import MicrogridModel
from microgrid.profiles.consumption import ConsumptionProfile
from microgrid.profiles.solar import SolarProfile

from .conftest import ConstantProfile

STEP_HOURS = 0.25


def empty_model() -> MicrogridModel:
    return MicrogridModel(num_prosumers=0, num_consumers=0, seed=0)


def make_battery(**overrides) -> Battery:
    params = dict(
        capacity_kwh=10.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        round_trip_efficiency=1.0,  # lossless by default: simpler arithmetic
        soc_min=0.0,
        soc_max=1.0,
        initial_soc=0.5,
        step_hours=STEP_HOURS,
    )
    params.update(overrides)
    return Battery(**params)


def make_prosumer(model, strategy, *, generation_kwh, consumption_kwh, battery=None):
    return ProsumerAgent(
        model,
        solar_profile=ConstantProfile(generation_kwh),
        consumption_profile=ConstantProfile(consumption_kwh),
        strategy=strategy,
        battery=battery,
    )


def test_base_class_is_abstract(strategy):
    with pytest.raises(TypeError):
        HouseholdAgent(
            empty_model(),
            consumption_profile=ConstantProfile(1.0),
            strategy=strategy,
        )


def test_surplus_without_battery_creates_sell_order(strategy):
    model = empty_model()
    agent = make_prosumer(model, strategy, generation_kwh=2.0, consumption_kwh=0.5)
    agent.step()

    assert agent.state is HouseholdState.SELLING
    assert len(model.market.orders) == 1
    order = model.market.orders[0]
    assert order.side is OrderSide.SELL
    assert order.quantity_kwh == pytest.approx(1.5)


def test_deficit_creates_buy_order(strategy):
    model = empty_model()
    agent = ConsumerAgent(
        model, consumption_profile=ConstantProfile(1.0), strategy=strategy
    )
    agent.step()

    assert agent.state is HouseholdState.BUYING
    order = model.market.orders[0]
    assert order.side is OrderSide.BUY
    assert order.quantity_kwh == pytest.approx(1.0)


def test_balanced_agent_stays_idle(strategy):
    model = empty_model()
    agent = make_prosumer(model, strategy, generation_kwh=1.0, consumption_kwh=1.0)
    agent.step()

    assert agent.state is HouseholdState.IDLE
    assert model.market.orders == []


def test_battery_charged_before_selling(strategy):
    """Battery-before-market priority: surplus fills the battery first and
    only the leftover is offered for sale."""
    model = empty_model()
    battery = make_battery()  # intake limit 1.25 kWh/step
    agent = make_prosumer(
        model, strategy, generation_kwh=2.5, consumption_kwh=0.5, battery=battery
    )
    agent.step()

    assert agent.battery_flow_kwh == pytest.approx(1.25)
    assert agent.residual_kwh == pytest.approx(0.75)
    order = model.market.orders[0]
    assert order.side is OrderSide.SELL
    assert order.quantity_kwh == pytest.approx(0.75)


def test_battery_discharged_before_buying(strategy):
    model = empty_model()
    battery = make_battery()  # delivery limit 1.25 kWh/step
    agent = make_prosumer(
        model, strategy, generation_kwh=0.0, consumption_kwh=2.0, battery=battery
    )
    agent.step()

    assert agent.battery_flow_kwh == pytest.approx(-1.25)
    order = model.market.orders[0]
    assert order.side is OrderSide.BUY
    assert order.quantity_kwh == pytest.approx(0.75)


def test_full_battery_deficit_covered_no_order(strategy):
    """If the battery can fully cover the deficit, the agent stays idle."""
    model = empty_model()
    battery = make_battery()
    agent = make_prosumer(
        model, strategy, generation_kwh=0.0, consumption_kwh=1.0, battery=battery
    )
    agent.step()

    assert agent.state is HouseholdState.IDLE
    assert model.market.orders == []
    assert agent.battery_flow_kwh == pytest.approx(-1.0)


def test_consumer_has_no_generation_and_nan_soc(strategy):
    model = empty_model()
    agent = ConsumerAgent(
        model, consumption_profile=ConstantProfile(0.5), strategy=strategy
    )
    assert agent.generation(0) == 0.0
    assert math.isnan(agent.soc)


def test_order_prices_stay_inside_tariff_band(strategy):
    tariff = strategy.tariff
    sell = strategy.make_order(agent_id=1, residual_kwh=1.0, step=0)
    buy = strategy.make_order(agent_id=1, residual_kwh=-1.0, step=0)
    assert tariff.feed_in_tl_per_kwh < sell.price_tl_per_kwh < tariff.retail_tl_per_kwh
    assert tariff.feed_in_tl_per_kwh < buy.price_tl_per_kwh < tariff.retail_tl_per_kwh
    # A rational market needs bid > ask headroom inside the band
    assert buy.price_tl_per_kwh > sell.price_tl_per_kwh


def test_tiny_residual_produces_no_order(strategy):
    assert strategy.make_order(agent_id=1, residual_kwh=0.001, step=0) is None
    assert strategy.make_order(agent_id=1, residual_kwh=-0.001, step=0) is None


# --- publish_status hook (Phase 3, step 6) --------------------------------


def _snapshot(agent) -> tuple:
    """What a STATUS would carry; NaN soc (no battery) as None so it compares."""
    soc = None if math.isnan(agent.soc) else agent.soc
    return (
        agent.state,
        agent.generation_kwh,
        agent.consumption_kwh,
        agent.residual_kwh,
        soc,
    )


class _RecordingCDA(CDAMarket):
    """CDA backend that records what every publish_status call saw."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: dict[tuple[int, int], list[tuple]] = {}

    def publish_status(self, agent) -> None:
        key = (agent.model.current_step, agent.unique_id)
        self.statuses.setdefault(key, []).append(_snapshot(agent))


class _RecordingLogging(LoggingMarket):
    def __init__(self) -> None:
        super().__init__()
        self.statuses: dict[tuple[int, int], list[tuple]] = {}

    publish_status = _RecordingCDA.publish_status


@pytest.mark.parametrize("market_cls", [_RecordingCDA, _RecordingLogging])
def test_every_agent_publishes_its_own_step_last_idle_included(market_cls, monkeypatch):
    # Called last in step(), so it reports this step's values. Called first,
    # it would report the previous step's state and residual: Phase 4's
    # barrier would then wait for an ORDER that never comes, or miss one.
    end_of_step: dict[tuple[int, int], tuple] = {}
    original_step = HouseholdAgent.step

    def recording_step(self):
        original_step(self)
        end_of_step[(self.model.current_step, self.unique_id)] = _snapshot(self)

    monkeypatch.setattr(HouseholdAgent, "step", recording_step)
    model = MicrogridModel(seed=42)
    model.market = market_cls()
    steps = model.config.steps_per_day  # one day: night, sunrise, noon surplus
    model.run(steps)

    ids = {agent.unique_id for agent in model.agents}
    assert set(model.market.statuses) == {(s, i) for s in range(steps) for i in ids}
    for key, reported in model.market.statuses.items():
        assert reported == [end_of_step[key]]  # exactly once, with this step's values
    for step in range(steps):
        # A non-IDLE STATUS commits its sender to an ORDER in the same step.
        committed = {
            i for i in ids if model.market.statuses[(step, i)][0][0] is not HouseholdState.IDLE
        }
        assert committed == {o.agent_id for o in model.market.orders_at(step)}
    states = {reported[0][0] for reported in model.market.statuses.values()}
    assert HouseholdState.IDLE in states  # IDLE agents report too


class _StatusBuildingCDA(CDAMarket):
    """Fills the hook the way Phase 4's ZmqMarket will: a STATUS built from
    the agent's cached fields only. The run must not change by one value."""

    def __init__(self) -> None:
        super().__init__()
        self.sent = 0

    def publish_status(self, agent) -> None:
        make_status_message(
            agent_id=agent.unique_id,
            step=agent.model.current_step,
            generation_kwh=agent.generation_kwh,
            consumption_kwh=agent.consumption_kwh,
            residual_kwh=agent.residual_kwh,
            state=agent.state.value,
            soc=agent.soc,
        )
        self.sent += 1


class _StatusBuildingLogging(LoggingMarket):
    def __init__(self) -> None:
        super().__init__()
        self.sent = 0

    publish_status = _StatusBuildingCDA.publish_status


@pytest.mark.parametrize(
    ("backend", "market_cls"),
    [("cda", _StatusBuildingCDA), ("logging", _StatusBuildingLogging)],
)
def test_status_hook_does_not_shift_the_shared_rng(backend, market_cls, monkeypatch):
    steps = 48

    def run(market=None):
        model = MicrogridModel(market_backend=backend, seed=123)
        if market is not None:
            model.market = market
        model.run(steps)
        dc = model.datacollector
        return model, dc.get_model_vars_dataframe(), dc.get_agent_vars_dataframe()

    _, plain_model_df, plain_agent_df = run()

    # A second read inside the hook would show up here before anywhere else.
    reads = {"solar": 0, "consumption": 0}
    for cls, key in ((SolarProfile, "solar"), (ConsumptionProfile, "consumption")):
        original = cls.energy_at

        def counting(self, step, _original=original, _key=key):
            reads[_key] += 1
            return _original(self, step)

        monkeypatch.setattr(cls, "energy_at", counting)

    hooked, hooked_model_df, hooked_agent_df = run(market_cls())
    assert hooked.market.sent == steps * len(hooked.agents)
    num_prosumers = len(hooked.agents_by_type[ProsumerAgent])
    assert reads == {
        "solar": steps * num_prosumers,
        "consumption": steps * len(hooked.agents),
    }
    pd.testing.assert_frame_equal(hooked_model_df, plain_model_df)
    pd.testing.assert_frame_equal(hooked_agent_df, plain_agent_df)
