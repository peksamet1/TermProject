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
    """Half-full battery keeps half the surplus. The 1.25 kWh power cap is above that share."""
    model = empty_model()
    battery = make_battery()  # s = 0.5, intake limit 1.25 kWh/step
    agent = make_prosumer(
        model, strategy, generation_kwh=2.5, consumption_kwh=0.5, battery=battery
    )
    agent.step()

    assert agent.battery_flow_kwh == pytest.approx(1.0)
    assert agent.residual_kwh == pytest.approx(1.0)
    # The agent only plans: SimPy applies the flow in the physical slot.
    assert battery.soc == pytest.approx(0.5)
    order = model.market.orders[0]
    assert order.side is OrderSide.SELL
    assert order.quantity_kwh == pytest.approx(1.0)


def test_battery_discharged_before_buying(strategy):
    """Half-full battery covers half a 2 kWh deficit; the rest is bought."""
    model = empty_model()
    battery = make_battery()  # s = 0.5, delivery limit 1.25 kWh/step
    agent = make_prosumer(
        model, strategy, generation_kwh=0.0, consumption_kwh=2.0, battery=battery
    )
    agent.step()

    assert agent.battery_flow_kwh == pytest.approx(-1.0)
    assert agent.residual_kwh == pytest.approx(-1.0)
    assert battery.soc == pytest.approx(0.5)  # planned, not applied yet
    order = model.market.orders[0]
    assert order.side is OrderSide.BUY
    assert order.quantity_kwh == pytest.approx(1.0)


def test_half_full_battery_shares_a_small_deficit(strategy):
    """A 1 kWh deficit at s = 0.5 is split: 0.5 from the battery, 0.5 from the market."""
    model = empty_model()
    battery = make_battery()
    agent = make_prosumer(
        model, strategy, generation_kwh=0.0, consumption_kwh=1.0, battery=battery
    )
    agent.step()

    assert agent.battery_flow_kwh == pytest.approx(-0.5)
    assert agent.state is HouseholdState.BUYING
    assert model.market.orders[0].quantity_kwh == pytest.approx(0.5)


def test_full_battery_deficit_covered_no_order(strategy):
    """A full battery covers a deficit that fits under the power limit, so the agent stays idle."""
    model = empty_model()
    battery = make_battery(initial_soc=1.0)
    agent = make_prosumer(
        model, strategy, generation_kwh=0.0, consumption_kwh=1.0, battery=battery
    )
    agent.step()

    assert agent.state is HouseholdState.IDLE
    assert model.market.orders == []
    assert agent.battery_flow_kwh == pytest.approx(-1.0)


def test_empty_battery_charges_the_surplus_up_to_the_power_cap(strategy):
    """s = 0 asks for the whole surplus; the 1.25 kWh power cap still clips it."""
    model = empty_model()
    battery = make_battery(initial_soc=0.0)
    agent = make_prosumer(
        model, strategy, generation_kwh=2.5, consumption_kwh=0.5, battery=battery
    )
    agent.step()

    assert agent.battery_flow_kwh == pytest.approx(1.25)
    assert model.market.orders[0].side is OrderSide.SELL
    assert model.market.orders[0].quantity_kwh == pytest.approx(0.75)


def test_full_battery_sells_the_whole_surplus(strategy):
    """s = 1 asks for no charge, so the surplus is sold."""
    model = empty_model()
    battery = make_battery(initial_soc=1.0)
    agent = make_prosumer(
        model, strategy, generation_kwh=2.5, consumption_kwh=0.5, battery=battery
    )
    agent.step()

    assert agent.battery_flow_kwh == pytest.approx(0.0)
    assert model.market.orders[0].side is OrderSide.SELL
    assert model.market.orders[0].quantity_kwh == pytest.approx(2.0)


def test_missing_fullness_offers_the_whole_net(strategy):
    assert strategy.battery_target(2.0, None) == pytest.approx(2.0)
    assert strategy.battery_target(-1.5, None) == pytest.approx(-1.5)


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


def test_soc_moves_the_price_and_stays_inside_the_band(strategy):
    tariff = strategy.tariff
    band = tariff.retail_tl_per_kwh - tariff.feed_in_tl_per_kwh
    empty = dict(soc=0.10, soc_min=0.10, soc_max=0.95)
    full = dict(soc=0.95, soc_min=0.10, soc_max=0.95)
    empty_ask = strategy.make_order(agent_id=1, residual_kwh=1.0, step=0, **empty)
    full_ask = strategy.make_order(agent_id=1, residual_kwh=1.0, step=0, **full)
    empty_bid = strategy.make_order(agent_id=1, residual_kwh=-1.0, step=0, **empty)
    full_bid = strategy.make_order(agent_id=1, residual_kwh=-1.0, step=0, **full)
    assert empty_ask.price_tl_per_kwh == pytest.approx(
        tariff.feed_in_tl_per_kwh + strategy.sell_margin * band
    )
    assert full_ask.price_tl_per_kwh == pytest.approx(tariff.feed_in_tl_per_kwh)
    assert empty_bid.price_tl_per_kwh == pytest.approx(tariff.retail_tl_per_kwh)
    assert full_bid.price_tl_per_kwh == pytest.approx(
        tariff.retail_tl_per_kwh - strategy.buy_margin * band
    )
    assert full_ask.price_tl_per_kwh < empty_ask.price_tl_per_kwh
    assert empty_bid.price_tl_per_kwh > full_bid.price_tl_per_kwh
    # Default margins still leave every bid above every ask.
    assert empty_bid.price_tl_per_kwh > empty_ask.price_tl_per_kwh
    assert full_bid.price_tl_per_kwh > full_ask.price_tl_per_kwh


def test_prosumer_order_uses_its_start_of_step_soc(strategy):
    model = empty_model()
    low = make_prosumer(
        model, strategy, generation_kwh=3.0, consumption_kwh=0.0,
        battery=make_battery(initial_soc=0.10, soc_min=0.10, soc_max=0.95),
    )
    high = make_prosumer(
        model, strategy, generation_kwh=3.0, consumption_kwh=0.0,
        battery=make_battery(initial_soc=0.95, soc_min=0.10, soc_max=0.95),
    )
    low.step()
    high.step()
    assert model.market.orders[0].price_tl_per_kwh > model.market.orders[1].price_tl_per_kwh


def test_nan_soc_keeps_the_fixed_margin(strategy):
    fixed = strategy.make_order(agent_id=1, residual_kwh=1.0, step=0)
    nan_soc = strategy.make_order(
        agent_id=1, residual_kwh=1.0, step=0, soc=math.nan, soc_min=0.1, soc_max=0.95
    )
    assert nan_soc.price_tl_per_kwh == pytest.approx(fixed.price_tl_per_kwh)


class CountingProfile(ConstantProfile):
    def __init__(self, kwh: float) -> None:
        super().__init__(kwh)
        self.reads = 0

    def energy_at(self, step: int) -> float:
        self.reads += 1
        return super().energy_at(step)


def test_generation_scale_doubles_and_reads_the_profile_once(strategy):
    model = empty_model()
    solar = CountingProfile(1.5)
    load = CountingProfile(0.5)
    agent = ProsumerAgent(
        model,
        solar_profile=solar,
        consumption_profile=load,
        strategy=strategy,
        battery=make_battery(),
    )
    agent.generation_scale = 2
    agent.step()

    assert solar.reads == 1
    assert load.reads == 1
    assert agent.generation_kwh == pytest.approx(3.0)
    assert agent.consumption_kwh == pytest.approx(0.5)


def test_load_scale_doubles_and_reads_the_profile_once(strategy):
    model = empty_model()
    solar = CountingProfile(1.0)
    load = CountingProfile(0.5)
    agent = ProsumerAgent(
        model,
        solar_profile=solar,
        consumption_profile=load,
        strategy=strategy,
        battery=make_battery(),
    )
    agent.load_scale = 2
    agent.step()

    assert solar.reads == 1
    assert load.reads == 1
    assert agent.generation_kwh == pytest.approx(1.0)
    assert agent.consumption_kwh == pytest.approx(1.0)


def test_negative_scale_is_rejected(strategy):
    model = empty_model()
    agent = make_prosumer(model, strategy, generation_kwh=1.0, consumption_kwh=0.5)
    with pytest.raises(ValueError):
        agent.generation_scale = -1
    with pytest.raises(ValueError):
        agent.load_scale = math.nan


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
