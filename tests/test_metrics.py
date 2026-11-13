"""KPIs and step timing in microgrid.metrics (Phase 5, step 5)."""

import math

import pandas as pd
import pytest

from microgrid.config import SimulationConfig
from microgrid.metrics import (
    COMM_KPIS,
    STAGES,
    StageTimer,
    balance_error_kwh,
    comm_kpis,
    community_cost_tl,
    demand_fill_rate,
    grid_dependence,
    order_volumes,
    reduction_pct,
    run_kpis,
    trade_success_rate,
)
from microgrid.model import MicrogridModel


def _frames():
    """Two steps, two agents; agent 2 is IDLE with a dust residual in step 1."""
    model_df = pd.DataFrame({
        "TotalConsumption": [2.0, 2.0],
        "GridImport": [1.0, 0.0],
        "GridExport": [0.0, 1.0],
        "LocalTradedKwh": [0.5, 0.5],
    })
    index = pd.MultiIndex.from_tuples([(1, 1), (1, 2), (2, 1), (2, 2)], names=["Step", "AgentID"])
    agent_df = pd.DataFrame({
        "Generation": [0.0, 0.0, 3.0, 0.0],
        "Consumption": [1.5, 0.005, 0.5, 0.5],
        "BatteryApplied": [0.0, 0.0, 0.0, 0.0],
        "Residual": [-1.5, -0.005, 2.5, -0.5],
        "State": ["BUYING", "IDLE", "SELLING", "BUYING"],
        "BuyOrder": [1.5, 0.005, 0.0, 0.5],
        "SellOrder": [0.0, 0.0, 2.5, 0.0],
        "CashTL": [-4.0, -0.02, 3.0, -1.5],
    }, index=index)
    return model_df, agent_df


def test_order_volumes_skip_idle_dust():
    _, agent_df = _frames()
    assert order_volumes(agent_df) == pytest.approx((2.0, 2.5))


def test_kpis_on_known_frames():
    model_df, agent_df = _frames()
    assert grid_dependence(model_df) == pytest.approx(0.25)
    assert trade_success_rate(model_df, agent_df) == pytest.approx(2 * 1.0 / 4.5)
    assert demand_fill_rate(model_df, agent_df) == pytest.approx(0.5)
    assert community_cost_tl(agent_df) == pytest.approx(-1.5)  # last step only
    assert reduction_pct(0.25, 0.5) == pytest.approx(50.0)
    assert math.isnan(reduction_pct(0.25, 0.0))


def test_kpis_are_nan_without_volume():
    model_df, agent_df = _frames()
    model_df["TotalConsumption"] = 0.0
    agent_df["State"] = "IDLE"
    assert math.isnan(grid_dependence(model_df))
    assert math.isnan(trade_success_rate(model_df, agent_df))
    assert math.isnan(demand_fill_rate(model_df, agent_df))


def test_balance_error_counts_dust_and_catches_a_gap():
    # Step 1: flow -1.5 (agent 2's -0.005 is dust) vs grid -1.0 -> 0.5 off.
    # Step 2: flow 2.5 - 0.5 = 2.0 vs grid 1.0 -> 1.0 off.
    model_df, agent_df = _frames()
    assert balance_error_kwh(model_df, agent_df) == pytest.approx(1.0)
    model_df["GridImport"] = [1.5, 0.0]
    model_df["GridExport"] = [0.0, 2.0]
    assert balance_error_kwh(model_df, agent_df) == pytest.approx(0.0, abs=1e-12)


@pytest.fixture(scope="module")
def day_runs():
    """One day per in-process backend, KPIs plus the final cash of every agent."""
    out = {}
    for backend in ("logging", "cda"):
        config = SimulationConfig(market_backend=backend, num_days=1)
        with MicrogridModel(config=config) as model:
            model.run()
            out[backend] = (run_kpis(model), sum(a.cash_tl for a in model.agents))
    return out


def test_model_run_balances_and_costs(day_runs):
    for kpis, cash in day_runs.values():
        assert kpis["balance_error_kwh"] < 1e-9
        assert kpis["violations"] == 0
        assert kpis["community_cost_tl"] == pytest.approx(-cash, abs=1e-9)
        assert kpis["sell_volume_kwh"] > 0 and kpis["buy_volume_kwh"] > 0


def test_p2p_beats_the_baseline(day_runs):
    base, _ = day_runs["logging"]
    p2p, _ = day_runs["cda"]
    assert base["trades"] == 0 and base["trade_success_rate"] == 0
    assert p2p["trades"] > 0 and 0 < p2p["trade_success_rate"] <= 1
    assert p2p["grid_dependence"] < base["grid_dependence"]
    assert p2p["community_cost_tl"] < base["community_cost_tl"]
    # Same physics, same profiles: only the market differs.
    assert p2p["generation_kwh"] == base["generation_kwh"]
    assert p2p["consumption_kwh"] == base["consumption_kwh"]


def test_comm_kpis_only_for_zmq(day_runs):
    kpis, _ = day_runs["cda"]
    assert all(kpis[name] is None for name in COMM_KPIS)
    config = SimulationConfig(market_backend="cda_zmq", num_days=1)
    with MicrogridModel(config=config) as model:
        model.run(8)
        comm = comm_kpis(model.market)
    assert comm["status_loss"] == 0 and comm["order_loss"] == 0
    assert comm["trades_confirmed"] == comm["trades_proposed"]
    assert comm["barrier_p50_ms"] is not None


def test_stage_timer_times_every_stage_without_changing_results():
    config = SimulationConfig(num_days=1)
    with MicrogridModel(config=config) as plain:
        plain.run(16)
        expected = plain.datacollector.get_model_vars_dataframe()
    timer = StageTimer()
    with MicrogridModel(config=config) as timed:
        timer.attach(timed)
        timer.run(timed, 16)
        got = timed.datacollector.get_model_vars_dataframe()

    pd.testing.assert_frame_equal(got, expected)
    assert len(timer.step_ms) == 16
    for stage in STAGES:
        assert len(timer.stage_ms[stage]) == 16
    for i, step in enumerate(timer.step_ms):
        assert sum(timer.stage_ms[s][i] for s in STAGES) <= step
    summary = timer.summary()
    assert summary["step_p50_ms"] > 0 and summary["physics_p95_ms"] > 0
