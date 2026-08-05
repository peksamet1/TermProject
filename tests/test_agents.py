"""Agent decision-flow tests with hand-written constant profiles.

Agents are attached to a real (empty) MicrogridModel so Mesa registration and
market submission work exactly as in production, but profiles are deterministic
stubs so assertions never depend on synthetic randomness.
"""

import math

import pytest

from microgrid.agents.battery import Battery
from microgrid.agents.household import (
    ConsumerAgent,
    HouseholdAgent,
    HouseholdState,
    ProsumerAgent,
)
from microgrid.config import SimulationConfig
from microgrid.market.base import OrderSide
from microgrid.model import MicrogridModel

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
