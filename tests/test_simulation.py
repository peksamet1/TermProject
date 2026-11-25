"""Default-scene simulation: one summer day, the SoC share, and the panel scales.

These tests run the model the panel opens (6 prosumers, 4 consumers, seed 42,
day 172, in-process CDA) and check the outcomes a user can see: energy balance,
local trade against the no-P2P baseline, a winter day with less sun, and that
a household scale or a SoC edit changes the next step of that household only.
"""

import threading
import time

import pandas as pd
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


def test_generation_scale_leaves_a_readable_history():
    model = MicrogridModel(seed=42, start_day_of_year=172)
    model.run(5)
    _prosumer(model).generation_scale = 2.0
    frame = pd.DataFrame(model.copy_model_vars())
    records, _names = model.copy_agent_records()
    assert len(frame) == 5
    assert len(records) == 5 * len(list(model.agents))
    assert len(set(map(len, model.copy_model_vars().values()))) == 1


def test_history_copy_waits_for_the_step_being_recorded():
    """Play writes history on a thread. A chart read must not see a half row."""
    model = MicrogridModel(seed=42, start_day_of_year=172)
    model.step()
    column = next(iter(model.datacollector.model_vars))
    entered = threading.Event()

    class _SlowList(list):
        def append(self, item):
            super().append(item)
            entered.set()
            time.sleep(0.2)

    model.datacollector.model_vars[column] = _SlowList(model.datacollector.model_vars[column])
    worker = threading.Thread(target=model.step)
    worker.start()
    assert entered.wait(2)
    snapshot = model.copy_model_vars()
    worker.join(2)
    assert not worker.is_alive()
    lengths = {len(values) for values in snapshot.values()}
    assert lengths == {2}


def test_a_recorded_step_keeps_its_own_clock_and_households():
    """Day 180 at 13:00 is one stored step, not the live end of the run."""
    from app import _rows_at, moment_text

    assert moment_text(0, 96, 172) == "Day 1 · 00:00 · day of year 172 · step 0"
    assert moment_text(52, 96, 172) == "Day 1 · 13:00 · day of year 172 · step 52"
    assert moment_text(96, 96, 172) == "Day 2 · 00:00 · day of year 173 · step 96"
    last = 180 * 96 - 1
    assert moment_text(last, 96, 172) == "Day 180 · 23:45 · day of year 351 · step 17279"

    model = MicrogridModel(seed=42, start_day_of_year=172, num_prosumers=1, num_consumers=0)
    model.run(54)
    agent_df = model.datacollector.get_agent_vars_dataframe()
    noon = _rows_at(agent_df, NOON_STEP)
    end = _rows_at(agent_df, 53)
    home = _prosumer(model)

    assert noon is not None and end is not None
    assert float(noon["Generation"].iloc[0]) > 0
    assert list(end["Label"]) == [home.label]
    assert float(end["SoC"].iloc[0]) == pytest.approx(home.soc)
    assert float(end["CashTL"].iloc[0]) == pytest.approx(home.cash_tl)


def test_report_slice_matches_the_full_run_and_keeps_noon_cash():
    """The panel's prefix uses the same KPI functions as the finished run."""
    from app import report_kpis, slice_history

    model = MicrogridModel(seed=42, start_day_of_year=172)
    model.run(54)
    kpis = run_kpis(model)
    model_df = model.datacollector.get_model_vars_dataframe()
    agent_df = model.datacollector.get_agent_vars_dataframe()
    full_model, full_agent = slice_history(model_df, agent_df, 53)
    got = report_kpis(full_model, full_agent)
    noon_model, noon_agent = slice_history(model_df, agent_df, NOON_STEP)
    noon = report_kpis(noon_model, noon_agent)

    assert got["demand_fill_rate"] == pytest.approx(kpis["demand_fill_rate"])
    assert got["trade_success_rate"] == pytest.approx(kpis["trade_success_rate"])
    assert got["community_cost_tl"] == pytest.approx(kpis["community_cost_tl"])
    assert got["violations"] == pytest.approx(kpis["violations"])
    assert got["balance_error_kwh"] == pytest.approx(kpis["balance_error_kwh"])
    assert noon["community_cost_tl"] != pytest.approx(got["community_cost_tl"])
