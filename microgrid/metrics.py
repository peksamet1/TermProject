"""KPIs of a finished run (Architecture 4.6 and 8).

Everything here is computed from the DataCollector output and, for
``cda_zmq``, from the market's comm counters, so the same functions serve the
Phase 5 performance tests and the Phase 6 analysis.

Definitions (Architecture 8, row 5):

- Grid dependence = grid import / total consumption.
- Reduction vs baseline (%) = (baseline - value) / baseline * 100, same seed
  and same data, ``logging`` backend as the baseline.
- Trade success rate = order volume filled locally / total order volume. Each
  local kWh fills one BUY and one SELL order, so it counts twice.
- Demand fill rate = local kWh / BUY order volume: the share of the
  households' unmet demand that neighbours supplied. In a sunny period SELL
  volume far exceeds BUY volume, which caps the trade success rate well
  below 1 even when most demand is met locally.
- Community cost (TL) = what all households paid in total: - sum of final
  ``cash_tl``. Local trades move money inside the community and cancel out,
  so only grid flows change it.

Energy balance: residuals below ``min_order_kwh`` send no order ("dust") but
are still paid at the grid tariff. With them taken out, every step must
satisfy  gen - cons - applied battery - dust = export - import  exactly.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import pandas as pd

from microgrid.agents.household import HouseholdState
from microgrid.comm.node import percentile

STAGES = ("agents", "market", "physics", "collect")
# Model method of each stage (see MicrogridModel.step).
_STAGE_METHODS = {
    "agents": "_agents_step",
    "market": "_market_settle",
    "physics": "_physics_slot",
    "collect": "_collect",
}
_IDLE = HouseholdState.IDLE.value


# --- KPIs from the DataCollector ------------------------------------------


def order_volumes(agent_df: pd.DataFrame) -> tuple[float, float]:
    """(BUY kWh, SELL kWh) actually sent to the market over the run."""
    active = agent_df[agent_df["State"] != _IDLE]
    return float(active["BuyOrder"].sum()), float(active["SellOrder"].sum())


def grid_dependence(model_df: pd.DataFrame) -> float:
    """Grid import / total consumption; nan if nothing was consumed."""
    consumption = model_df["TotalConsumption"].sum()
    return float(model_df["GridImport"].sum() / consumption) if consumption > 0 else math.nan


def reduction_pct(value: float, baseline: float) -> float:
    """How much lower ``value`` is than ``baseline``, in percent."""
    if baseline == 0 or math.isnan(baseline) or math.isnan(value):
        return math.nan
    return 100.0 * (baseline - value) / baseline


def trade_success_rate(model_df: pd.DataFrame, agent_df: pd.DataFrame) -> float:
    """Order volume filled locally / total order volume; nan without orders."""
    buy, sell = order_volumes(agent_df)
    if buy + sell <= 0:
        return math.nan
    return float(2.0 * model_df["LocalTradedKwh"].sum() / (buy + sell))


def demand_fill_rate(model_df: pd.DataFrame, agent_df: pd.DataFrame) -> float:
    """Local kWh / BUY order volume; nan without BUY orders."""
    buy, _ = order_volumes(agent_df)
    return float(model_df["LocalTradedKwh"].sum() / buy) if buy > 0 else math.nan


def community_cost_tl(agent_df: pd.DataFrame) -> float:
    """Total energy bill of the community: - sum of the final cash balances."""
    last_step = agent_df.index.get_level_values("Step").max()
    return float(-agent_df.xs(last_step, level="Step")["CashTL"].sum())


def balance_error_kwh(model_df: pd.DataFrame, agent_df: pd.DataFrame) -> float:
    """Largest per-step |gen - cons - applied battery - dust - (export - import)|."""
    dust = agent_df["Residual"].where(agent_df["State"] == _IDLE, 0.0)
    flow = agent_df["Generation"] - agent_df["Consumption"] - agent_df["BatteryApplied"] - dust
    per_step = flow.groupby(level="Step").sum()
    grid = model_df["GridExport"] - model_df["GridImport"]
    # model_df is indexed 0..n-1, the agent frame by Mesa time 1..n.
    error = per_step.to_numpy() - grid.to_numpy()
    return float(abs(error).max()) if len(error) else 0.0


def run_kpis(model) -> dict[str, float | int | None]:
    """All KPIs of a finished ``MicrogridModel`` run, as one flat dict."""
    model_df = model.datacollector.get_model_vars_dataframe()
    agent_df = model.datacollector.get_agent_vars_dataframe()
    buy, sell = order_volumes(agent_df)
    out: dict[str, float | int | None] = {
        "generation_kwh": float(model_df["TotalGeneration"].sum()),
        "consumption_kwh": float(model_df["TotalConsumption"].sum()),
        "grid_import_kwh": float(model_df["GridImport"].sum()),
        "grid_export_kwh": float(model_df["GridExport"].sum()),
        "local_traded_kwh": float(model_df["LocalTradedKwh"].sum()),
        "trades": int(model_df["LocalTradeCount"].sum()),
        "buy_volume_kwh": buy,
        "sell_volume_kwh": sell,
        "grid_dependence": grid_dependence(model_df),
        "trade_success_rate": trade_success_rate(model_df, agent_df),
        "demand_fill_rate": demand_fill_rate(model_df, agent_df),
        "community_cost_tl": community_cost_tl(agent_df),
        "violations": int(model_df["ConstraintViolations"].sum()),
        "fallback_import_kwh": float(model_df["FallbackImport"].sum()),
        "fallback_export_kwh": float(model_df["FallbackExport"].sum()),
        "fade_loss_kwh": float(model_df["BatteryFadeLoss"].sum()),
        "balance_error_kwh": balance_error_kwh(model_df, agent_df),
    }
    out.update(comm_kpis(model.market))
    return out


# --- Communication (cda_zmq only) ------------------------------------------

COMM_KPIS = (
    "status_loss", "order_loss", "timeouts", "late", "protocol_errors",
    "trades_proposed", "trades_confirmed", "trades_nacked", "trades_timed_out",
    "latency_p50_ms", "latency_p95_ms", "rtt_p50_ms", "rtt_p95_ms",
    "barrier_p50_ms", "barrier_p95_ms", "handshake_ms",
)  # fmt: skip


def comm_kpis(market) -> dict[str, float | int | None]:
    """Comm counters of a ZeroMQ market; every value None for in-process ones."""
    if not hasattr(market, "comm_summary"):
        return dict.fromkeys(COMM_KPIS)
    summary = market.comm_summary()
    return {name: summary[name] for name in COMM_KPIS}


# --- Step timing ------------------------------------------------------------


@dataclass
class StageTimer:
    """Wall time per step and per stage, in ms (time.perf_counter)."""

    step_ms: list[float] = field(default_factory=list)
    stage_ms: dict[str, list[float]] = field(
        default_factory=lambda: {stage: [] for stage in STAGES}
    )

    def attach(self, model) -> None:
        """Time each stage method of ``model`` from now on.

        Shadows the bound methods on the instance only; ``step()`` looks them
        up through ``self`` and so calls the timed versions, in the same order.
        """
        for stage, name in _STAGE_METHODS.items():
            setattr(model, name, self._timed(getattr(model, name), self.stage_ms[stage]))

    @staticmethod
    def _timed(method, samples: list[float]):
        def run():
            t0 = time.perf_counter()
            method()
            samples.append(1000.0 * (time.perf_counter() - t0))

        return run

    def run(self, model, steps: int) -> None:
        """Run ``steps`` model steps, timing every whole step."""
        for _ in range(steps):
            t0 = time.perf_counter()
            model.step()
            self.step_ms.append(1000.0 * (time.perf_counter() - t0))

    def summary(self) -> dict[str, float | None]:
        out = {
            "step_p50_ms": percentile(self.step_ms, 50),
            "step_p95_ms": percentile(self.step_ms, 95),
        }
        for stage in STAGES:
            out[f"{stage}_p50_ms"] = percentile(self.stage_ms[stage], 50)
            out[f"{stage}_p95_ms"] = percentile(self.stage_ms[stage], 95)
        return out
