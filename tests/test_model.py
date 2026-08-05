"""Model-level integration tests: full-day runs, invariants, reproducibility.

The energy-conservation invariant is the main regression guard for Phases 4-5:
whatever changes in the market or the co-simulation, per-step energy balance
must keep holding.
"""

import pytest

from microgrid.agents.household import ConsumerAgent, ProsumerAgent
from microgrid.config import SimulationConfig
from microgrid.model import MicrogridModel


def test_full_day_runs_without_errors(config):
    model = MicrogridModel(config=config)
    model.run(config.steps_per_day)
    df = model.datacollector.get_model_vars_dataframe()
    assert len(df) == config.steps_per_day


def test_agent_counts_and_types(config):
    model = MicrogridModel(config=config)
    assert len(model.agents_by_type[ProsumerAgent]) == config.num_prosumers
    assert len(model.agents_by_type[ConsumerAgent]) == config.num_consumers


def test_energy_conservation_each_step(config):
    """generation - consumption - battery_flow == residual == export - import."""
    model = MicrogridModel(config=config)
    for _ in range(config.steps_per_day):
        model.step()
        lhs = sum(
            a.generation_kwh - a.consumption_kwh - a.battery_flow_kwh
            for a in model.agents
        )
        rhs = model.grid_export_kwh - model.grid_import_kwh
        assert lhs == pytest.approx(rhs, abs=1e-9)


def test_soc_stays_within_bounds(config):
    model = MicrogridModel(config=config)
    model.run(config.steps_per_day)
    agent_df = model.datacollector.get_agent_vars_dataframe()
    socs = agent_df["SoC"].dropna()
    assert (socs >= config.battery.soc_min - 1e-9).all()
    assert (socs <= config.battery.soc_max + 1e-9).all()


def test_daytime_sells_evening_buys(config):
    """Sanity check on emergent behavior: sell orders happen around noon,
    buy orders dominate the evening peak."""
    model = MicrogridModel(config=config)
    model.run(config.steps_per_day)
    df = model.datacollector.get_model_vars_dataframe()

    noon_window = df.iloc[44:56]  # 11:00 - 14:00
    evening_window = df.iloc[76:88]  # 19:00 - 22:00
    assert noon_window["SellOrders"].sum() > 0
    assert evening_window["BuyOrders"].sum() > 0
    assert evening_window["BuyOrders"].sum() > evening_window["SellOrders"].sum()


def test_same_seed_reproducible():
    def run_once() -> list[float]:
        model = MicrogridModel(config=SimulationConfig(), seed=123)
        model.run(48)
        return model.datacollector.get_model_vars_dataframe()["TotalGeneration"].tolist()

    assert run_once() == pytest.approx(run_once())


def test_datacollector_reports_all_measures(config):
    model = MicrogridModel(config=config)
    model.run(4)
    df = model.datacollector.get_model_vars_dataframe()
    for column in (
        "TotalGeneration",
        "TotalConsumption",
        "GridImport",
        "GridExport",
        "BuyOrders",
        "SellOrders",
        "AvgProsumerSoC",
    ):
        assert column in df.columns


def test_keyword_overrides_win_over_config():
    model = MicrogridModel(num_prosumers=2, num_consumers=1, seed=1)
    assert len(model.agents_by_type[ProsumerAgent]) == 2
    assert len(model.agents_by_type[ConsumerAgent]) == 1
