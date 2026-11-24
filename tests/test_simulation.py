"""Default-scene simulation: one summer day, the SoC share, and the panel scales.

These tests run the model the panel opens (6 prosumers, 4 consumers, seed 42,
day 172, in-process CDA) and check the outcomes a user can see: energy balance,
local trade against the no-P2P baseline, a winter day with less sun, and that
a household scale or a SoC edit changes the next step of that household only.
"""

import pytest

from microgrid.agents.household import ProsumerAgent
from microgrid.config import SimulationConfig
from microgrid.metrics import run_kpis
from microgrid.model import MicrogridModel

STEPS_PER_DAY = 96
NOON_STEP = 52  # 13:00, inside the daylight window


def _run(backend: str, *, day: int = 172, seed: int = 42, steps: int = STEPS_PER_DAY):
    model = MicrogridModel(
        market_backend=backend,
        start_day_of_year=day,
        seed=seed,
        num_prosumers=6,
        num_consumers=4,
    )
    model.run(steps)
    return model, run_kpis(model)


def _prosumer(model, index: int = 0) -> ProsumerAgent:
    return model.agents_by_type[ProsumerAgent][index]


def test_default_scene_matches_the_panel():
    config = SimulationConfig()
    assert config.num_prosumers == 6
    assert config.num_consumers == 4
    assert config.pv_peak_kw == pytest.approx(5.0)
    assert config.battery.capacity_kwh == pytest.approx(10.0)
    assert config.battery.initial_soc == pytest.approx(0.50)
    assert config.start_day_of_year == 172
    assert config.seed == 42
    assert config.market_backend == "cda"
    assert config.steps_per_day == STEPS_PER_DAY


def test_summer_day_balances_and_stays_inside_the_battery_window():
    model, kpis = _run("cda")
    agent_df = model.datacollector.get_agent_vars_dataframe()
    socs = agent_df["SoC"].dropna()
    prices = model.datacollector.get_model_vars_dataframe()["AvgTradePrice"].dropna()

    assert kpis["violations"] == 0
    assert kpis["balance_error_kwh"] < 1e-9
    assert kpis["generation_kwh"] > 0
    assert kpis["consumption_kwh"] > 0
    assert (socs >= model.config.battery.soc_min - 1e-9).all()
    assert (socs <= model.config.battery.soc_max + 1e-9).all()
    feed = model.config.tariff.feed_in_tl_per_kwh
    retail = model.config.tariff.retail_tl_per_kwh
    assert len(prices) > 0
    assert ((prices >= feed - 1e-9) & (prices <= retail + 1e-9)).all()


def test_p2p_trades_locally_and_cuts_the_grid_bill():
    _, baseline = _run("logging")
    _, p2p = _run("cda")

    assert baseline["local_traded_kwh"] == 0
    assert baseline["trades"] == 0
    assert p2p["local_traded_kwh"] > 0
    assert p2p["trades"] > 0
    assert p2p["grid_import_kwh"] < baseline["grid_import_kwh"]
    assert p2p["community_cost_tl"] < baseline["community_cost_tl"]
    assert p2p["demand_fill_rate"] > 0
    assert p2p["generation_kwh"] == pytest.approx(baseline["generation_kwh"])
    assert p2p["consumption_kwh"] == pytest.approx(baseline["consumption_kwh"])


def test_winter_day_generates_less_than_the_summer_day():
    _, summer = _run("cda", day=172)
    _, winter = _run("cda", day=355)
    assert winter["generation_kwh"] < summer["generation_kwh"]
    assert winter["local_traded_kwh"] < summer["local_traded_kwh"]


def test_generation_scale_doubles_one_home_and_leaves_the_others():
    plain = MicrogridModel(seed=42, start_day_of_year=172)
    scaled = MicrogridModel(seed=42, start_day_of_year=172)
    _prosumer(scaled).generation_scale = 2
    for _ in range(NOON_STEP):
        plain.step()
        scaled.step()

    plain_home = _prosumer(plain)
    scaled_home = _prosumer(scaled)
    assert plain_home.generation_kwh > 0
    assert scaled_home.generation_kwh == pytest.approx(2 * plain_home.generation_kwh)
    assert scaled_home.consumption_kwh == pytest.approx(plain_home.consumption_kwh)
    for left, right in zip(plain.agents, scaled.agents):
        if left.unique_id == plain_home.unique_id:
            continue
        assert right.generation_kwh == pytest.approx(left.generation_kwh)
        assert right.consumption_kwh == pytest.approx(left.consumption_kwh)


def test_load_scale_doubles_one_home_and_leaves_the_others():
    plain = MicrogridModel(seed=42, start_day_of_year=172)
    scaled = MicrogridModel(seed=42, start_day_of_year=172)
    _prosumer(scaled).load_scale = 2
    plain.step()
    scaled.step()

    plain_home = _prosumer(plain)
    scaled_home = _prosumer(scaled)
    assert plain_home.consumption_kwh > 0
    assert scaled_home.consumption_kwh == pytest.approx(2 * plain_home.consumption_kwh)
    assert scaled_home.generation_kwh == pytest.approx(plain_home.generation_kwh)


def test_full_battery_sells_a_surplus_instead_of_storing_it():
    model = MicrogridModel(seed=42, start_day_of_year=172)
    home = _prosumer(model)
    home.load_scale = 0
    for _ in range(STEPS_PER_DAY):
        home.battery.set_soc(home.battery.soc_max)
        model.step()
        if home.generation_kwh > 0.05:
            break
    else:
        pytest.fail("the summer day never produced a surplus to sell")

    assert home.battery_flow_kwh == pytest.approx(0.0)
    assert home.residual_kwh == pytest.approx(home.generation_kwh)
    assert home.state.value == "SELLING"


def test_half_full_battery_keeps_only_its_share_of_a_surplus():
    model = MicrogridModel(seed=42, start_day_of_year=172)
    home = _prosumer(model)
    home.load_scale = 0
    fullness = home.strategy.fullness(0.50, home.battery.soc_min, home.battery.soc_max)
    for _ in range(STEPS_PER_DAY):
        home.battery.set_soc(0.50)
        model.step()
        if home.generation_kwh > 0.05:
            break
    else:
        pytest.fail("the summer day never produced a surplus to split")

    share = (1.0 - fullness) * home.generation_kwh
    assert home.battery_flow_kwh == pytest.approx(min(share, 1.25))
    assert home.battery_flow_kwh < home.generation_kwh
    assert home.residual_kwh == pytest.approx(home.generation_kwh - home.battery_flow_kwh)
